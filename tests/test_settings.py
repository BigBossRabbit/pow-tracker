"""Settings-override tests: every deploy-specific value is a plugin setting
(``plugins.entries.pow-tracker.settings``), with generic fallback defaults,
and the per-client DB seeding honours the configured overrides.

``dashboard/plugin_api.py`` reads its settings at import time (plugin-mount
time in a real gateway), so the tests patch
``hermes_cli.config.load_config_readonly`` BEFORE importing the module from a
temporary directory tree, then assert:

* ``clients_root`` (and every other setting) resolves from the config;
* a missing/invalid value falls back to the generic default;
* a fresh client DB is seeded with the *configured* rate / currency /
  company (the de-hardcoded defaults);
* ``legacy_clients_root`` migrates an old registry, and a client folder that
  already holds a pre-1.0 ``.spd-time/time.db`` keeps it in place;
* ``__init__.py`` imports nothing from hermes and ``register()`` registers
  nothing (catalog rule 06: declared capabilities match reality).

Run from the plugin root under a Python that has fastapi + pydantic (the
hermes venv), e.g.::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
API_FILE = PLUGIN_ROOT / "dashboard" / "plugin_api.py"


def _read_api() -> str:
    return API_FILE.read_text(encoding="utf-8")

MODULE_NAME = "pow_tracker_plugin_api_under_test"

# The settings-override tests need hermes' config reader (patched) and the
# plugin's own dependencies (fastapi/pydantic); everywhere else they are
# skipped rather than failed (the grep-gate stays pure stdlib).
try:  # pragma: no cover - depends on the interpreter the tests run under
    import hermes_cli.config  # noqa: F401
    import fastapi  # noqa: F401
    import pydantic  # noqa: F401
    HAVE_HERMES = True
except Exception:
    HAVE_HERMES = False


def _load_api(config: dict, home: Path):
    """Import dashboard/plugin_api.py with hermes' config reader patched.

    ``home`` is pinned over the real HOME so any ``~`` expansion (a setting
    falling back to its generic default) lands inside the caller's test
    sandbox instead of the real user's home: the module tail runs
    ``_init_db()`` at import time and must never write outside the test tmp.

    The config-reader patch stays active for the imported module's whole
    lifetime (settings are read at import AND on later ``_setting()`` calls);
    the returned module carries the patchers and the test's ``tearDown``
    stops them.
    """
    spec = importlib.util.spec_from_file_location(MODULE_NAME, API_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    patchers = [
        mock.patch.dict(os.environ, {"HOME": str(home)}),
        mock.patch("hermes_cli.config.load_config_readonly",
                   return_value=config),
    ]
    for patcher in patchers:
        patcher.start()
    try:
        spec.loader.exec_module(module)
    except BaseException:
        for patcher in patchers:
            patcher.stop()
        raise
    module.__pow_test_patchers__ = patchers
    return module


def _stop_patchers(module) -> None:
    for patcher in getattr(module, "__pow_test_patchers__", []) or []:
        try:
            patcher.stop()
        except RuntimeError:
            pass  # already stopped (e.g. a nested with-mock ended early)


class _RecordingCtx:
    """Minimal register() context that records every registration call."""

    def __init__(self):
        self.calls = []

    def register_tool(self, *a, **k):
        self.calls.append(("tool", a, k))

    def register_hook(self, *a, **k):
        self.calls.append(("hook", a, k))

    def register_middleware(self, *a, **k):
        self.calls.append(("middleware", a, k))

    def register_command(self, *a, **k):
        self.calls.append(("command", a, k))

    def __getattr__(self, name):
        raise AttributeError(name)


class PluginRootTests(unittest.TestCase):
    """__init__.py: no hermes imports, register() registers nothing."""

    def test_init_imports_nothing_from_hermes(self):
        # AST-based (prose in the docstring may mention hermes; only actual
        # import statements count for the private-import rules).
        import ast

        tree = ast.parse((PLUGIN_ROOT / "__init__.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                target = getattr(node, "module", None) or ",".join(
                    a.name for a in node.names)
                self.assertFalse(
                    target == "hermes" or target.startswith("hermes."),
                    msg=f"__init__.py imports hermes: {target!r}")

    def test_register_registers_nothing(self):
        spec = importlib.util.spec_from_file_location(
            "pow_tracker_root_under_test", PLUGIN_ROOT / "__init__.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["pow_tracker_root_under_test"] = module
        spec.loader.exec_module(module)
        ctx = _RecordingCtx()
        module.register(ctx)
        self.assertEqual(ctx.calls, [],
                         "register() must register no tools/hooks/middleware")


@unittest.skipUnless(
    HAVE_HERMES,
    "hermes_cli/fastapi/pydantic not importable (run under the hermes venv)")
class SettingsOverrideTests(unittest.TestCase):
    """clients_root + the new defaults, end to end."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pow-settings-test-")
        self.tmp = Path(self._tmp.name)
        self.clients = self.tmp / "clients"
        self.home = self.tmp / "pinned-home"
        self.home.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self):
        # Stop the auto-capture thread the module tail may have started.
        module = sys.modules.get(MODULE_NAME)
        if module is not None:
            try:
                module._AUTO_STOP.set()
            except Exception:
                pass
            _stop_patchers(module)
        sys.modules.pop(MODULE_NAME, None)

    def _config(self, **overrides) -> dict:
        settings = {
            "clients_root": str(self.clients),
            "legacy_clients_root": "",
            "ignore_dirs": ["Docs", "Archive", "Research", "Templates"],
            "default_rate": 250.0,
            "currency": "EUR",
            "company": "Test Company",
        }
        settings.update(overrides)
        return {"plugins": {"entries": {"pow-tracker": {"settings": settings}}}}

    # ── defaults ─────────────────────────────────────────────────────────────

    def test_generic_fallback_defaults(self):
        # No hermes config for this plugin at all: every default is generic,
        # and the (generic) clients root resolves under the pinned test home.
        api = _load_api({}, home=self.home)
        self.assertEqual(api.SETTING_DEFAULTS["clients_root"],
                         "~/Documents/Clients")
        self.assertEqual(api.SETTING_DEFAULTS["legacy_clients_root"], "")
        self.assertEqual(api.SETTING_DEFAULTS["ignore_dirs"],
                         ["Docs", "Archive", "Research", "Templates"])
        self.assertEqual(api.SETTING_DEFAULTS["default_rate"], 100.0)
        self.assertEqual(api.SETTING_DEFAULTS["currency"], "USD")
        self.assertEqual(api.SETTING_DEFAULTS["company"], "My Company")
        # No personal path leaked into a default...
        self.assertNotIn("/Users/", str(api.SETTING_DEFAULTS))
        # ...and the fallback root lands under the pinned (test) home, i.e.
        # nothing outside the configured root is ever written.
        self.assertEqual(api.CLIENTS_ROOT,
                         self.home / "Documents" / "Clients")
        self.assertEqual(api.REGISTRY_DB,
                         self.home / "Documents" / "Clients" /
                         ".pow-tracker" / "clients.db")

    def test_no_hermes_config_falls_back_to_generic(self):
        # Plugin missing from the config entirely.
        api = _load_api({"plugins": {"entries": {}}}, home=self.home)
        self.assertEqual(api._plugin_settings(), {})
        self.assertEqual(api._setting("default_rate"), 100.0)
        self.assertEqual(api._setting("currency"), "USD")
        self.assertEqual(api._setting("company"), "My Company")
        # A failing config reader is survived read-only (never a crash).
        with mock.patch("hermes_cli.config.load_config_readonly",
                        side_effect=RuntimeError("no config")):
            self.assertEqual(api._plugin_settings(), {})

    # ── clients_root override ────────────────────────────────────────────────

    def test_clients_root_setting_overrides_default(self):
        api = _load_api(self._config(), home=self.home)
        self.assertEqual(api.CLIENTS_ROOT, self.clients)
        self.assertEqual(api.REGISTRY_DB,
                         self.clients / ".pow-tracker" / "clients.db")
        # The module tail ran _init_db() against the configured root.
        self.assertTrue(api.REGISTRY_DB.is_file(),
                        "registry must be created under the configured root")

    def test_bad_clients_root_type_falls_back(self):
        api = _load_api(self._config(clients_root=12345), home=self.home)
        self.assertEqual(api.CLIENTS_ROOT,
                         self.home / "Documents" / "Clients")

    def test_default_rate_type_guard(self):
        api = _load_api(self._config(default_rate="not-a-number"),
                        home=self.home)
        self.assertEqual(api._setting("default_rate"), 100.0)
        # bool may not satisfy a float setting
        api2 = _load_api(self._config(default_rate=True), home=self.home)
        self.assertEqual(api2._setting("default_rate"), 100.0)

    def test_ignore_dirs_setting_overrides_default(self):
        api = _load_api(self._config(ignore_dirs=["Alpha", "Beta"]),
                        home=self.home)
        self.assertEqual(api._setting("ignore_dirs"), ["Alpha", "Beta"])

    # ── per-client DB seeding honours the settings ───────────────────────────

    def test_fresh_client_db_seeded_from_settings(self):
        api = _load_api(self._config(), home=self.home)
        db = api.REGISTRY_DB
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute("SELECT name, slug, folder FROM clients").fetchall()
            self.assertEqual(rows, [("Default", "default",
                                     str(self.clients / "Default"))])
            client_db = self.clients / "Default" / ".pow-time" / "time.db"
            self.assertTrue(client_db.is_file())
            c2 = sqlite3.connect(str(client_db))
            try:
                seeded = dict(c2.execute(
                    "SELECT key, value FROM settings").fetchall())
            finally:
                c2.close()
        finally:
            conn.close()
        self.assertEqual(seeded["rate_nad"], "250.0")   # not any personal rate
        self.assertEqual(seeded["currency"], "EUR")     # not any personal currency
        self.assertEqual(seeded["company"], "Test Company")
        self.assertEqual(seeded["client"], "Default")

    # ── auto-capture consent default ─────────────────────────────────────────

    def test_created_client_seeds_auto_capture_off(self):
        api = _load_api(self._config(), home=self.home)
        asyncio.run(api.create_client(api.ClientCreate(name="Fresh Client")))
        db = self.clients / "fresh-client" / ".pow-time" / "time.db"
        self.assertTrue(db.is_file())
        conn = sqlite3.connect(str(db))
        try:
            seeded = dict(conn.execute("SELECT key, value FROM settings").fetchall())
        finally:
            conn.close()
        self.assertEqual(seeded["auto_capture"], "0")

    def test_discovered_client_seeds_auto_capture_off(self):
        # A folder dropped into the clients root is registered by discovery and
        # must arrive with capture OFF too (the privacy-safe default), never on.
        api = _load_api(self._config(), home=self.home)
        (self.clients / "Dropped In").mkdir(parents=True)
        conn = api._reg_connect()
        try:
            api._discover_clients(conn)
            conn.commit()
            row = conn.execute(
                "SELECT * FROM clients WHERE slug='dropped-in'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "discovery must register the folder")
        db = api._db_for_row(row)
        api._ensure_db(db)
        conn = sqlite3.connect(str(db))
        try:
            seeded = dict(conn.execute("SELECT key, value FROM settings").fetchall())
        finally:
            conn.close()
        self.assertEqual(seeded["auto_capture"], "0")

    # ── legacy migration (generic) ────────────────────────────────────────────

    def test_legacy_clients_root_migrates_registry(self):
        api = _load_api(self._config(legacy_clients_root=""), home=self.home)
        # No legacy root configured: nothing to migrate.
        self.assertIsNone(api._legacy_root())

        # Build an old-style registry with one client.
        old_root = self.tmp / "old-clients"
        old_reg_dir = old_root / ".pow-tracker"
        old_reg_dir.mkdir(parents=True)
        old_db = old_reg_dir / "clients.db"
        conn = sqlite3.connect(str(old_db))
        conn.executescript(
            """
            CREATE TABLE clients (
                id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                slug TEXT NOT NULL UNIQUE, folder TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
            INSERT INTO clients(id, name, slug, folder, active, created_at)
            VALUES('cl_old_1', 'Existing Client', 'existing-client',
                   'folder-kept-as-recorded', 1, 1.0);
            """)
        conn.commit()
        conn.close()

        # Reset the current registry, point the setting at the old root.
        api.REGISTRY_DB.unlink()
        with mock.patch.object(api, "_setting",
                               side_effect=self._legacy_setting(api, old_root)):
            api._migrate_legacy_registry()

        self.assertTrue(api.REGISTRY_DB.is_file(),
                        "old registry must be copied to the current root")
        conn = sqlite3.connect(str(api.REGISTRY_DB))
        try:
            rows = conn.execute("SELECT name, folder FROM clients").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [("Existing Client", "folder-kept-as-recorded")])

    def _legacy_setting(self, api, old_root):
        real = api._setting

        def setting(key, default=None, settings=None):
            if key == "legacy_clients_root":
                return str(old_root)
            return real(key, default, settings)
        return setting

    def test_legacy_data_dir_adopted_in_place(self):
        api = _load_api(self._config(), home=self.home)
        # A folder that already holds a pre-1.0 data dir keeps it.
        legacy_folder = self.tmp / "adopted"
        (legacy_folder / ".spd-time").mkdir(parents=True)
        (legacy_folder / ".spd-time" / "time.db").write_bytes(b"")
        row = {"folder": str(legacy_folder)}
        self.assertEqual(
            api._db_for_row(row),
            legacy_folder / ".spd-time" / "time.db")
        # A fresh folder gets the current data layout.
        fresh = self.tmp / "fresh"
        fresh.mkdir()
        self.assertEqual(
            api._db_for_row({"folder": str(fresh)}),
            fresh / ".pow-time" / "time.db")


class _FakeWS:
    """Just the WebSocket surface the auth gate touches."""

    def __init__(self, params=None):
        self.query_params = params or {}
        self.headers = {}
        self.client = None
        self.closed = None

    async def close(self, code=1000, reason=""):
        self.closed = code

    async def accept(self):
        return None


class WebSocketAuthGate(unittest.TestCase):
    """/events refuses an upgrade with no (or a bad) credential, before accept.

    The pane reads client names and time entries, so an unauthenticated local
    process must not get the stream: the upgrade is checked with the dashboard's
    canonical WS gate and refused with 4401.
    """

    def test_auth_check_precedes_accept_in_source(self):
        # Structural, hermes-free: the check must gate the accept, not follow it.
        src = _read_api()
        body = src.split("async def events_ws(ws: WebSocket):", 1)
        self.assertEqual(len(body), 2, "events_ws handler missing")
        handler = body[1]
        check = handler.index("_ws_upgrade_authorized(ws)")
        accept = handler.index("await ws.accept()")
        self.assertLess(check, accept, "credential must be checked before ws.accept()")
        self.assertIn("_WS_AUTH_REFUSED = 4401", src)
        self.assertIn("close(code=_WS_AUTH_REFUSED)", src)

    @unittest.skipUnless(
        HAVE_HERMES,
        "hermes_cli/fastapi/pydantic not importable (run under the hermes venv)")
    def test_no_credential_is_refused_4401(self):
        api = self._api()
        ws = _FakeWS()
        asyncio.run(api.events_ws(ws))
        self.assertEqual(ws.closed, 4401)

    @unittest.skipUnless(
        HAVE_HERMES,
        "hermes_cli/fastapi/pydantic not importable (run under the hermes venv)")
    def test_wrong_token_is_refused_4401(self):
        api = self._api()
        ws = _FakeWS({"token": "not-the-token"})
        with mock.patch("hermes_cli.web_server._SESSION_TOKEN", "the-real-token"):
            asyncio.run(api.events_ws(ws))
        self.assertEqual(ws.closed, 4401)

    @unittest.skipUnless(
        HAVE_HERMES,
        "hermes_cli/fastapi/pydantic not importable (run under the hermes venv)")
    def test_matching_token_is_accepted(self):
        api = self._api()
        ws = _FakeWS({"token": "the-real-token"})
        from hermes_cli.web_server import app
        with mock.patch("hermes_cli.web_server._SESSION_TOKEN", "the-real-token"), \
                mock.patch.object(app.state, "auth_required", False, create=True):
            self.assertTrue(api._ws_upgrade_authorized(ws))

    # ── helpers ───────────────────────────────────────────────────────────────

    def _api(self):
        tmp = tempfile.TemporaryDirectory(prefix="pow-ws-test-")
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name) / "home"
        home.mkdir(parents=True)
        api = _load_api({}, home=home)
        self.addCleanup(lambda: _stop_patchers(api))
        try:
            api._AUTO_STOP.set()
        except Exception:
            pass
        return api


if __name__ == "__main__":
    unittest.main(verbosity=2)
