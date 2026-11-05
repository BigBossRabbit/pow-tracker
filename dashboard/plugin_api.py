"""POW Tracker (Proof of Work) — backend API.

Mounted at /api/plugins/pow-tracker/ by the dashboard plugin system.
Multi-client: each client gets its own SQLite DB under
<clients-root>/<client-slug>/.pow-time/time.db, so billable data travels
with the client folder. The active client is selected via /clients +
/client; all task/entry/invoice routes operate on the active client's DB.

Deploy-specific values (the clients root, the folders auto-discovery
ignores, the rate/currency/company defaults seeded into per-client DBs,
and an optional legacy clients root to migrate a registry from) are
plugin settings — declared in `config_schema` in `plugin.yaml`, rendered
by the Desktop's Settings → Plugins form, and read here from
`plugins.entries.<plugin-id>.settings` with generic fallback defaults
(see SETTING_DEFAULTS). No personal path or name is hardcoded.

Entries are OPEN (timer running, end_ts NULL) or CLOSED (duration fixed).
An optional background auto-capture thread opens/closes `auto` entries
driven by folder activity in the active client's folder.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

router = APIRouter()

# ── Settings ──────────────────────────────────────────────────────────────────
# Every deploy-specific value is a plugin setting declared in `config_schema`
# (plugin.yaml) so the Desktop renders it under Settings → Plugins. Values live
# under `plugins.entries.<plugin-id>.settings` in the active profile's
# config.yaml and are read read-only via `load_config_readonly()` — the pattern
# the plugin developer guide documents for a dashboard plugin's `plugin_api.py`
# half. These generic defaults are the last fallback (fresh installs).
# Settings are read once at plugin-mount time: saving a new value requires a
# gateway restart (or plugin reload) to take effect.
PLUGIN_ID = "pow-tracker"
SETTING_DEFAULTS: dict = {
    # Root folder holding one sub-directory per tracked client.
    "clients_root": "~/Documents/Clients",
    # Optional previous clients root to migrate an existing client registry
    # from (empty on a fresh install). Migrated rows keep their recorded
    # folder paths; a client whose folder holds a pre-1.0 data dir keeps its
    # existing DB where it is.
    "legacy_clients_root": "",
    # Organisational folders under the clients root that the client
    # auto-discovery must ignore (they are not billable clients).
    "ignore_dirs": ["Docs", "Archive", "Research", "Templates"],
    # Per-client DB seeding defaults: hourly rate, its currency label, and
    # the company name printed on invoices.
    "default_rate": 100.0,
    "currency": "USD",
    "company": "My Company",
}
# Mirrors the manifest `config_schema` types (a `float` setting accepts an int
# too); anything else in config.yaml falls back to the generic default.
_SETTING_TYPES: dict = {
    "clients_root": (str,),
    "legacy_clients_root": (str,),
    "ignore_dirs": (list,),
    "default_rate": (int, float),
    "currency": (str,),
    "company": (str,),
}


def _plugin_settings() -> dict:
    """`plugins.entries.<plugin-id>.settings`, never mutated (read-only load)."""
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        entry = (cfg.get("plugins") or {}).get("entries", {}).get(PLUGIN_ID) or {}
        settings = entry.get("settings") if isinstance(entry, dict) else None
        return settings if isinstance(settings, dict) else {}
    except Exception:  # pragma: no cover — running without a hermes config
        return {}


def _setting(key: str, default: Any = None, settings: Optional[dict] = None) -> Any:
    """One plugin setting, type-guarded, with its generic fallback default."""
    if default is None:
        default = SETTING_DEFAULTS.get(key)
    value = (settings if settings is not None else _plugin_settings()).get(key, default)
    expected = _SETTING_TYPES.get(key)
    if expected is None:
        return value
    # bool is an int subclass — never let True satisfy a float/int setting.
    if isinstance(value, bool) and bool not in expected:
        return default
    if not isinstance(value, expected):
        return default
    return value


# ── Storage ───────────────────────────────────────────────────────────────────
CLIENTS_ROOT = Path(
    str(_setting("clients_root") or "~/Documents/Clients")
).expanduser()
REGISTRY_DB = CLIENTS_ROOT / ".pow-tracker" / "clients.db"
DATA_SUBDIR = ".pow-time"
# A pre-1.0 data layout keeps a client's time DB under `.spd-time/`; those
# existing DBs are adopted in place (found by existence), everything else
# uses DATA_SUBDIR.
LEGACY_DATA_SUBDIR = ".spd-time"

# ── Tunables (named constants) ────────────────────────────────────────────────
WS_QUEUE_MAX = 256                 # per-subscriber event backlog
WS_HEARTBEAT_S = 1.0               # heartbeat/poll interval on each socket
AUTO_POLL_S = 5.0                  # auto-capture tick period
AUTO_DEBOUNCE_S = 2.0              # ignore bursts newer than this offset
AUTO_IDLE_DEFAULT_S = 900.0        # close auto-entry after 15 min idle
STATE_ENTRY_LIMIT_DEFAULT = 500    # /state recent-entry cap
WALK_MAX_FILES = 20000             # safety bound for the mtime walk
TRANSIENT_SUFFIXES = (
    ".tmp", ".swp", ".swo", ".part", ".partial", ".crdownload",
    ".download", ".bak", "~",
)
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".spd-time"}

# ── Live event hub ────────────────────────────────────────────────────────────
# Subscribers live on the asyncio event loop; producers may be daemon threads,
# so publishing bridges through the captured loop (thread-safe).
_SUBS: "set[asyncio.Queue]" = set()
_MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None


def _set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Record the event loop that owns the subscriber queues."""
    global _MAIN_LOOP
    _MAIN_LOOP = loop


def _publish(event: dict) -> None:
    """Fan out an event to every connected WS subscriber.

    Safe to call from any thread: if we're off the main loop the payload is
    marshalled onto it. Dead/full queues are dropped silently.
    """
    msg = json.dumps(event)

    def _fanout() -> None:
        dead = []
        for q in list(_SUBS):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            _SUBS.discard(q)

    loop = _MAIN_LOOP
    if loop is not None and loop.is_running():
        try:
            loop.call_soon_threadsafe(_fanout)
            return
        except RuntimeError:
            pass  # loop shutting down; fall through to best-effort
    _fanout()


# ── DB helpers ────────────────────────────────────────────────────────────────
_DB_READY = False
_DB_LOCK = threading.Lock()


def _now() -> float:
    return time.time()


def _iso(ts: Optional[float]) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Client registry + per-client DB ───────────────────────────────────────────
_REG_LOCK = threading.Lock()
_DB_INITED: set[str] = set()


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "client"


def _reg_connect() -> sqlite3.Connection:
    REGISTRY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(REGISTRY_DB), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init_registry() -> None:
    """Create the client registry.

    Also auto-discovers any subdirectory of CLIENTS_ROOT that isn't already
    registered, so manually added client folders (or folders created outside
    the plugin) appear in the desktop picker without a restart.
    """
    # Migrate a registry DB from a previous clients root (the optional
    # `legacy_clients_root` setting) if one is configured and exists.
    _migrate_legacy_registry()

    conn = _reg_connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS clients (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL UNIQUE,
                slug        TEXT NOT NULL UNIQUE,
                folder      TEXT NOT NULL,
                active      INTEGER NOT NULL DEFAULT 0,
                created_at  REAL NOT NULL
            );
            """
        )
        if not conn.execute("SELECT 1 FROM clients LIMIT 1").fetchone():
            # Fresh install: seed one default client under the clients root.
            conn.execute(
                "INSERT INTO clients(id, name, slug, folder, active, created_at) "
                "VALUES(?, 'Default', 'default', ?, 1, ?)",
                (_new_id("cl"), str(CLIENTS_ROOT / "Default"), _now()),
            )
        elif not conn.execute("SELECT 1 FROM clients WHERE active=1").fetchone():
            first = conn.execute("SELECT id FROM clients ORDER BY created_at").fetchone()
            conn.execute("UPDATE clients SET active=1 WHERE id=?", (first["id"],))

        # Auto-discover any subdirectory of CLIENTS_ROOT that isn't registered yet.
        _discover_clients(conn)

        conn.commit()
    finally:
        conn.close()


def _legacy_root() -> Optional[Path]:
    """The configured previous clients root (registry migration source), if any."""
    raw = str(_setting("legacy_clients_root") or "").strip()
    return Path(raw).expanduser() if raw else None


def _migrate_legacy_registry() -> None:
    """If the registry DB lived at a previous clients root (the optional
    `legacy_clients_root` setting), copy it to the current location so
    existing registrations survive the clients-root move.

    Also re-copies if the current DB exists but is empty (0 bytes) or has no tables —
    the gateway's boot-time file touch can create a stub before the plugin runs.
    """
    legacy_root = _legacy_root()
    if legacy_root is None:
        return
    old_db = legacy_root / ".pow-tracker" / "clients.db"
    if not old_db.exists():
        return
    REGISTRY_DB.parent.mkdir(parents=True, exist_ok=True)
    needs_copy = False
    if not REGISTRY_DB.exists():
        needs_copy = True
    else:
        try:
            sz = REGISTRY_DB.stat().st_size
            if sz == 0:
                needs_copy = True
            else:
                # Verify the DB has the clients table; if not, replace it.
                import sqlite3
                conn = sqlite3.connect(str(REGISTRY_DB), timeout=5)
                try:
                    has_table = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='clients'"
                    ).fetchone() is not None
                finally:
                    conn.close()
                if not has_table:
                    needs_copy = True
        except sqlite3.DatabaseError:
            needs_copy = True
    if needs_copy:
        import shutil
        shutil.copy2(str(old_db), str(REGISTRY_DB))


def _discover_clients(conn: sqlite3.Connection) -> None:
    """Register any subdirectory of CLIENTS_ROOT that is not yet in the registry.

    A directory qualifies if:
    - It is a direct child of CLIENTS_ROOT (one level deep).
    - Its name does not start with '.'.
    - It is not an organisational folder (the `ignore_dirs` setting).
    - It is not already present (by folder path) in the clients table.
    """
    existing = {
        row["folder"]
        for row in conn.execute("SELECT folder FROM clients").fetchall()
    }
    # Organisational folders under the clients root that are not billable
    # clients (the `ignore_dirs` setting).
    ignore_dirs = {str(d) for d in _setting("ignore_dirs") if str(d).strip()}
    try:
        children = [p for p in CLIENTS_ROOT.iterdir()
                    if p.is_dir()
                    and not p.name.startswith(".")
                    and p.name not in ignore_dirs]
    except OSError:
        return
    now = _now()
    for child in sorted(children, key=lambda p: p.name.lower()):
        if str(child) in existing:
            continue
        slug = _slugify(child.name)
        if conn.execute("SELECT 1 FROM clients WHERE slug=?",
                        (slug,)).fetchone():
            continue
        try:
            conn.execute(
                "INSERT INTO clients(id, name, slug, folder, active, created_at) "
                "VALUES(?, ?, ?, ?, 0, ?)",
                (_new_id("cl"), child.name, slug, str(child), now),
            )
        except sqlite3.IntegrityError:
            # Race: another insert won the slug. Skip.
            continue


def _active_client() -> sqlite3.Row:
    _init_registry()
    conn = _reg_connect()
    try:
        row = conn.execute("SELECT * FROM clients WHERE active=1").fetchone()
        if not row:
            raise HTTPException(500, "no active client")
        return row
    finally:
        conn.close()


def _resolve_db(client_id: Optional[str] = None) -> tuple[sqlite3.Row, Path]:
    """Resolve (client_row, db_path). client_id overrides the active client."""
    _init_registry()
    conn = _reg_connect()
    try:
        if client_id:
            row = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
            if not row:
                raise HTTPException(404, f"client not found: {client_id}")
        else:
            row = conn.execute("SELECT * FROM clients WHERE active=1").fetchone()
            if not row:
                raise HTTPException(500, "no active client")
        folder = Path(row["folder"])
        db = _db_for_row(row, _folder=folder)
        return row, db
    finally:
        conn.close()


def _connect_for(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _connect() -> sqlite3.Connection:
    _, db = _resolve_db()
    return _connect_for(db)


def _db_for_row(row: sqlite3.Row, *, _folder: Optional[Path] = None) -> Path:
    """A client's time DB: its folder's `.pow-time/time.db`, or the pre-1.0
    `.spd-time/time.db` when that is what the folder already holds (legacy
    data is adopted in place)."""
    folder = Path(row["folder"]) if _folder is None else _folder
    legacy_db = folder / LEGACY_DATA_SUBDIR / "time.db"
    if legacy_db.exists():
        return legacy_db
    return folder / DATA_SUBDIR / "time.db"


def _all_clients() -> list[sqlite3.Row]:
    """Return every registered client row (for all-folder auto-capture)."""
    _init_registry()
    conn = _reg_connect()
    try:
        return conn.execute("SELECT * FROM clients ORDER BY name").fetchall()
    finally:
        conn.close()


def _ensure_db(db: Path) -> None:
    """Ensure schema + defaults exist for a given client DB (idempotent)."""
    key = str(db)
    if key in _DB_INITED:
        return
    with _DB_LOCK:
        if key in _DB_INITED:
            return
        conn = _connect_for(db)
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id          TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    billable    INTEGER NOT NULL DEFAULT 1,
                    created_at  REAL NOT NULL,
                    archived    INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS entries (
                    id          TEXT PRIMARY KEY,
                    task_id     TEXT NOT NULL REFERENCES tasks(id),
                    start_ts    REAL NOT NULL,
                    end_ts      REAL,
                    duration_s  REAL,
                    source      TEXT NOT NULL DEFAULT 'manual',
                    note        TEXT NOT NULL DEFAULT '',
                    rate_nad    REAL,
                    created_at  REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entries_task ON entries(task_id);
                CREATE INDEX IF NOT EXISTS idx_entries_start ON entries(start_ts);
                CREATE INDEX IF NOT EXISTS idx_entries_end ON entries(end_ts);
                """
            )
            defaults = {
                "rate_nad": str(_setting("default_rate")),
                "currency": str(_setting("currency")),
                "company": str(_setting("company")),
                "client": db.parent.parent.stem,
                "auto_capture": "1",
                "auto_task_id": "",
                "auto_idle_s": str(int(AUTO_IDLE_DEFAULT_S)),
            }
            for k, v in defaults.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v)
                )
            # Guarantee a default auto-capture task exists so all-folder capture
            # always has a task to attribute entries to, with zero manual setup.
            row = conn.execute(
                "SELECT id FROM tasks WHERE name='Auto-captured work' AND archived=0 "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                tid = _new_id("task")
                conn.execute(
                    "INSERT INTO tasks(id, name, billable, created_at, archived) "
                    "VALUES(?, 'Auto-captured work', 1, ?, 0)",
                    (tid, _now()),
                )
            conn.commit()
        finally:
            conn.close()
        _DB_INITED.add(key)


def _init_db() -> None:
    """Ensure schema exists for the ACTIVE client's DB (idempotent, cheap)."""
    try:
        _, db = _resolve_db()
    except HTTPException:
        return
    _ensure_db(db)


def _get_settings(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def _set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _settings_rate(conn: sqlite3.Connection) -> float:
    try:
        return float(_get_settings(conn).get("rate_nad") or 0)
    except (TypeError, ValueError):
        return 0.0


def _row_to_entry(r: sqlite3.Row, rate_nad: float) -> dict:
    duration = r["duration_s"]
    if duration is None and r["end_ts"] is not None:
        duration = max(0.0, float(r["end_ts"]) - float(r["start_ts"]))
    running = r["end_ts"] is None
    rate = r["rate_nad"] if r["rate_nad"] is not None else rate_nad
    amount = (
        round(duration / 3600.0 * (rate or 0), 2) if (not running and duration) else None
    )
    return {
        "id": r["id"],
        "task_id": r["task_id"],
        "start_ts": r["start_ts"],
        "end_ts": r["end_ts"],
        "duration_s": duration,
        "running": running,
        "source": r["source"],
        "note": r["note"],
        "rate_nad": rate,
        "amount_nad": amount,
        "created_at": r["created_at"],
    }


def _entry_with_task(conn: sqlite3.Connection, entry_id: str, rate_nad: float) -> dict:
    r = conn.execute(
        "SELECT e.*, t.name AS task_name FROM entries e "
        "JOIN tasks t ON t.id = e.task_id WHERE e.id = ?",
        (entry_id,),
    ).fetchone()
    if not r:
        return {}
    d = _row_to_entry(r, rate_nad)
    d["task_name"] = r["task_name"]
    return d


# ── Request models ────────────────────────────────────────────────────────────
class TaskCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    billable: bool = True


class TaskUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    billable: Optional[bool] = None
    archived: Optional[bool] = None


class EntryCreate(BaseModel):
    task_id: str
    note: str = ""
    source: str = Field("manual", pattern="^(manual|auto)$")
    start_ts: Optional[float] = None
    end_ts: Optional[float] = None
    duration_s: Optional[float] = Field(None, ge=0)


class EntryUpdate(BaseModel):
    note: Optional[str] = None
    start_ts: Optional[float] = None
    end_ts: Optional[float] = None
    duration_s: Optional[float] = Field(None, ge=0)
    rate_nad: Optional[float] = Field(None, ge=0)


class SettingsUpdate(BaseModel):
    rate_nad: Optional[float] = Field(None, ge=0)
    currency: Optional[str] = Field(None, min_length=1, max_length=8)
    company: Optional[str] = Field(None, max_length=200)
    client: Optional[str] = Field(None, max_length=200)
    auto_capture: Optional[bool] = None
    auto_task_id: Optional[str] = None
    auto_idle_s: Optional[float] = Field(None, ge=60)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{int(_now() * 1000)}_{os.urandom(3).hex()}"


# ── Routes ────────────────────────────────────────────────────────────────────
@router.get("/health")
async def health():
    _init_db()
    _, db = _resolve_db()
    return {"ok": True, "db": str(db)}


@router.get("/clients")
async def list_clients():
    _init_registry()
    conn = _reg_connect()
    try:
        rows = conn.execute(
            "SELECT id, name, slug, folder, active, created_at FROM clients ORDER BY name"
        ).fetchall()
        return {"clients": [dict(r) for r in rows]}
    finally:
        conn.close()


class ClientCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


@router.post("/clients")
async def create_client(body: ClientCreate):
    """Register a client. Its folder is created under the clients root and a
    fresh per-client time DB is seeded."""
    _init_registry()
    name = body.name.strip()
    slug = _slugify(name)
    conn = _reg_connect()
    try:
        if conn.execute("SELECT 1 FROM clients WHERE name=?", (name,)).fetchone():
            raise HTTPException(409, f"client already exists: {name}")
        folder = CLIENTS_ROOT / slug
        cid = _new_id("cl")
        conn.execute(
            "INSERT INTO clients(id, name, slug, folder, active, created_at) VALUES(?,?,?,0,?,?)",
            (cid, name, slug, str(folder), _now()),
        )
        # Fix column order mistake defensively: set folder explicitly.
        conn.execute("UPDATE clients SET folder=? WHERE id=?", (str(folder), cid))
        conn.commit()
    finally:
        conn.close()
    # Seed the client DB.
    db = folder / DATA_SUBDIR / "time.db"
    conn2 = _connect_for(db)
    try:
        conn2.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, name TEXT NOT NULL,
                billable INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL, archived INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS entries (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                start_ts REAL NOT NULL, end_ts REAL, duration_s REAL,
                source TEXT NOT NULL DEFAULT 'manual', note TEXT NOT NULL DEFAULT '',
                rate_nad REAL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_entries_task ON entries(task_id);
            CREATE INDEX IF NOT EXISTS idx_entries_start ON entries(start_ts);
            CREATE INDEX IF NOT EXISTS idx_entries_end ON entries(end_ts);
            """
        )
        for k, v in {
            "rate_nad": str(_setting("default_rate")), "currency": str(_setting("currency")),
            "company": str(_setting("company")),
            "client": name, "auto_capture": "0", "auto_task_id": "",
            "auto_idle_s": str(int(AUTO_IDLE_DEFAULT_S)),
        }.items():
            conn2.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v)
            )
        conn2.commit()
    finally:
        conn2.close()
    _publish({"type": "client_created", "client_id": cid})
    return {"client": {"id": cid, "name": name, "slug": slug, "folder": str(folder), "active": 0}}


@router.post("/clients/{client_id}/activate")
async def activate_client(client_id: str):
    """Switch the active client. Any running auto-entry is closed first."""
    global _AUTO_THREAD
    _init_registry()
    # Close any running auto entry on the old client before switching.
    try:
        settings = _get_settings_cached()
        if settings.get("auto_capture") == "1":
            conn = _connect()
            try:
                open_rows = conn.execute(
                    "SELECT id FROM entries WHERE end_ts IS NULL AND source='auto'"
                ).fetchall()
                for r in open_rows:
                    _close_auto(r["id"])
            finally:
                conn.close()
    except Exception:
        pass
    conn = _reg_connect()
    try:
        if not conn.execute("SELECT 1 FROM clients WHERE id=?", (client_id,)).fetchone():
            raise HTTPException(404, "client not found")
        conn.execute("UPDATE clients SET active=0")
        conn.execute("UPDATE clients SET active=1 WHERE id=?", (client_id,))
        conn.commit()
        row = dict(conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone())
    finally:
        conn.close()
    # Re-point schema init at the new client's DB.
    _DB_INITED.clear()
    _init_db()
    _publish({"type": "client_activated", "client": row})
    return {"client": row}


@router.get("/state")
async def get_state(limit: int = STATE_ENTRY_LIMIT_DEFAULT):
    """Snapshot: settings, tasks, recent entries, running timers, aggregates."""
    _init_db()
    conn = _connect()
    try:
        settings = _get_settings(conn)
        rate = _settings_rate(conn)
        tasks = conn.execute(
            "SELECT * FROM tasks ORDER BY archived ASC, created_at ASC"
        ).fetchall()
        entries = conn.execute(
            "SELECT e.*, t.name AS task_name FROM entries e "
            "JOIN tasks t ON t.id = e.task_id "
            "ORDER BY (e.end_ts IS NULL) DESC, e.start_ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        entries_out = [_row_to_entry(r, rate) | {"task_name": r["task_name"]} for r in entries]
        running = [e for e in entries_out if e["running"]]
        return {
            "settings": settings,
            "rate_nad": rate,
            "tasks": [dict(t) for t in tasks],
            "entries": entries_out,
            "running": running,
            "counts": {
                "tasks": len(tasks),
                "entries": len(entries_out),
                "running": len(running),
            },
        }
    finally:
        conn.close()


@router.get("/tasks")
async def list_tasks():
    _init_db()
    conn = _connect()
    try:
        tasks = conn.execute(
            "SELECT * FROM tasks ORDER BY archived ASC, created_at ASC"
        ).fetchall()
        return {"tasks": [dict(t) for t in tasks]}
    finally:
        conn.close()


@router.post("/tasks")
async def create_task(body: TaskCreate):
    _init_db()
    conn = _connect()
    try:
        tid = _new_id("task")
        conn.execute(
            "INSERT INTO tasks(id, name, billable, created_at, archived) "
            "VALUES(?, ?, ?, ?, 0)",
            (tid, body.name.strip(), 1 if body.billable else 0, _now()),
        )
        conn.commit()
        task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        _publish({"type": "task_created", "task": task})
        return {"task": task}
    finally:
        conn.close()


@router.patch("/tasks/{task_id}")
async def update_task(task_id: str, body: TaskUpdate):
    _init_db()
    conn = _connect()
    try:
        cur = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not cur:
            raise HTTPException(404, "task not found")
        if body.name is not None:
            conn.execute("UPDATE tasks SET name=? WHERE id=?", (body.name.strip(), task_id))
        if body.billable is not None:
            conn.execute(
                "UPDATE tasks SET billable=? WHERE id=?",
                (1 if body.billable else 0, task_id),
            )
        if body.archived is not None:
            conn.execute(
                "UPDATE tasks SET archived=? WHERE id=?",
                (1 if body.archived else 0, task_id),
            )
        conn.commit()
        task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
        _publish({"type": "task_updated", "task": task})
        return {"task": task}
    finally:
        conn.close()


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str):
    _init_db()
    conn = _connect()
    try:
        # Single transaction: entries then task, relying on FK order.
        conn.execute("DELETE FROM entries WHERE task_id=?", (task_id,))
        deleted = conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        if deleted.rowcount == 0:
            conn.rollback()
            raise HTTPException(404, "task not found")
        conn.commit()
        _publish({"type": "task_deleted", "task_id": task_id})
        return {"ok": True}
    finally:
        conn.close()


@router.get("/entries")
async def list_entries(
    task_id: Optional[str] = Query(None),
    limit: int = STATE_ENTRY_LIMIT_DEFAULT,
):
    _init_db()
    conn = _connect()
    try:
        rate = _settings_rate(conn)
        if task_id:
            rows = conn.execute(
                "SELECT e.*, t.name AS task_name FROM entries e "
                "JOIN tasks t ON t.id = e.task_id WHERE e.task_id=? "
                "ORDER BY e.start_ts DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT e.*, t.name AS task_name FROM entries e "
                "JOIN tasks t ON t.id = e.task_id ORDER BY e.start_ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return {
            "entries": [
                _row_to_entry(r, rate) | {"task_name": r["task_name"]} for r in rows
            ]
        }
    finally:
        conn.close()


def _validate_span(start: float, end: Optional[float], dur: Optional[float]) -> tuple[float, Optional[float], Optional[float]]:
    """Normalise create/update timestamps; returns (start, end, duration)."""
    if end is None and dur is None:
        return start, None, None  # running timer
    if end is not None and dur is None:
        dur = max(0.0, end - start)
    elif dur is not None and end is None:
        end = start + dur
    if end is not None and end < start:
        raise HTTPException(422, "end_ts precedes start_ts")
    return start, end, dur


@router.post("/entries")
async def create_entry(body: EntryCreate):
    """Create an entry. No start_ts -> starts now (a timer). Closed entries
    carry end_ts and/or duration_s (backfill / auto-capture)."""
    _init_db()
    conn = _connect()
    try:
        cur = conn.execute("SELECT * FROM tasks WHERE id=?", (body.task_id,)).fetchone()
        if not cur:
            raise HTTPException(404, "task not found")

        start = body.start_ts if body.start_ts is not None else _now()
        start, end, dur = _validate_span(start, body.end_ts, body.duration_s)

        # Enforce ONE running timer per process: BEGIN IMMEDIATE serialises
        # writers, the conditional count makes the check atomic with insert.
        if end is None:
            conn.execute("BEGIN IMMEDIATE")
            open_count = conn.execute(
                "SELECT COUNT(*) AS n FROM entries WHERE end_ts IS NULL AND task_id=?",
                (body.task_id,),
            ).fetchone()["n"]
            if open_count:
                raise HTTPException(409, "a timer is already running for this task")
        eid = _new_id("ent")
        conn.execute(
            "INSERT INTO entries(id, task_id, start_ts, end_ts, duration_s, source, note, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (eid, body.task_id, start, end, dur, body.source, body.note or "", _now()),
        )
        conn.commit()
        entry = _entry_with_task(conn, eid, _settings_rate(conn))
        _publish({"type": "entry_created", "entry": entry})
        return {"entry": entry}
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()


@router.patch("/entries/{entry_id}")
async def update_entry(entry_id: str, body: EntryUpdate):
    """Edit an existing entry: note, rate, start/end, or duration."""
    _init_db()
    conn = _connect()
    try:
        cur = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if not cur:
            raise HTTPException(404, "entry not found")

        start = body.start_ts if body.start_ts is not None else cur["start_ts"]
        end = body.end_ts if body.end_ts is not None else cur["end_ts"]
        dur = body.duration_s
        if body.duration_s is None and (body.start_ts is not None or body.end_ts is not None):
            # Recompute duration from the new span unless explicitly overridden.
            if end is not None:
                dur = max(0.0, end - start)
            else:
                dur = None  # still running after edits
        start, end, dur = _validate_span(start, end, dur)

        conn.execute(
            "UPDATE entries SET note=?, start_ts=?, end_ts=?, duration_s=?, rate_nad=? WHERE id=?",
            (
                body.note if body.note is not None else cur["note"],
                start,
                end,
                dur,
                body.rate_nad if body.rate_nad is not None else cur["rate_nad"],
                entry_id,
            ),
        )
        conn.commit()
        entry = _entry_with_task(conn, entry_id, _settings_rate(conn))
        _publish({"type": "entry_updated", "entry": entry})
        return {"entry": entry}
    finally:
        conn.close()


@router.post("/entries/{entry_id}/stop")
async def stop_entry(entry_id: str, body: dict | None = None):
    """Stop a running timer. Conditional UPDATE prevents double-stop races."""
    _init_db()
    conn = _connect()
    try:
        cur = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if not cur:
            raise HTTPException(404, "entry not found")
        if cur["end_ts"] is not None:
            return {"entry": _entry_with_task(conn, entry_id, _settings_rate(conn))}
        end_ts = _now()
        dur = max(0.0, end_ts - cur["start_ts"])
        params: list[Any] = [end_ts, dur]
        note_sql = ""
        note = (body or {}).get("note")
        if note is not None:
            note_sql = ", note=?"
            params.append(note)
        params.append(entry_id)
        cur2 = conn.execute(
            f"UPDATE entries SET end_ts=?, duration_s={dur}{note_sql} "
            "WHERE id=? AND end_ts IS NULL",
            ([end_ts] + ([note] if note is not None else []) + [entry_id]),
        )
        if cur2.rowcount == 0:
            # Another path stopped it first; report current state.
            return {"entry": _entry_with_task(conn, entry_id, _settings_rate(conn))}
        conn.commit()
        entry = _entry_with_task(conn, entry_id, _settings_rate(conn))
        _publish({"type": "entry_updated", "entry": entry})
        return {"entry": entry}
    finally:
        conn.close()


@router.delete("/entries/{entry_id}")
async def delete_entry(entry_id: str):
    _init_db()
    conn = _connect()
    try:
        deleted = conn.execute("DELETE FROM entries WHERE id=?", (entry_id,))
        if deleted.rowcount == 0:
            conn.rollback()
            raise HTTPException(404, "entry not found")
        conn.commit()
        _publish({"type": "entry_deleted", "entry_id": entry_id})
        return {"ok": True}
    finally:
        conn.close()


@router.get("/invoice")
async def get_invoice(
    from_ts: Optional[float] = Query(None),
    to_ts: Optional[float] = Query(None),
    task_id: Optional[str] = Query(None),
):
    """Billable aggregate. Per-entry effective rates are honoured: each row's
    amount uses its own override or the global rate — never a blended MAX()."""
    _init_db()
    conn = _connect()
    try:
        rate = _settings_rate(conn)
        settings = _get_settings(conn)
        where = ["e.end_ts IS NOT NULL"]
        args: list[Any] = []
        if from_ts is not None:
            where.append("e.start_ts >= ?")
            args.append(from_ts)
        if to_ts is not None:
            where.append("e.end_ts <= ?")
            args.append(to_ts)
        if task_id:
            where.append("e.task_id = ?")
            args.append(task_id)

        rows = conn.execute(
            f"""
            SELECT e.task_id, t.name AS task_name, t.billable AS billable,
                   SUM(e.duration_s) AS total_s, COUNT(*) AS n,
                   SUM(COALESCE(e.duration_s,0)/3600.0 *
                       COALESCE(e.rate_nad, ?)) AS amount_nad_exact
            FROM entries e
            JOIN tasks t ON t.id = e.task_id
            WHERE {' AND '.join(where)}
            GROUP BY e.task_id, t.name, t.billable
            ORDER BY total_s DESC
            """,
            [rate] + args,
        ).fetchall()

        lines = []
        grand_s = 0.0
        grand_billable_nad = 0.0
        grand_nonbillable_s = 0.0
        for r in rows:
            total_s = float(r["total_s"] or 0)
            billable = bool(r["billable"])
            amount = round(float(r["amount_nad_exact"] or 0), 2) if billable else 0.0
            grand_s += total_s
            if billable:
                grand_billable_nad += amount
            else:
                grand_nonbillable_s += total_s
            lines.append({
                "task_id": r["task_id"],
                "task_name": r["task_name"],
                "billable": billable,
                "hours": round(total_s / 3600.0, 3),
                "seconds": total_s,
                "entries": r["n"],
                "amount_nad": amount,
            })
        return {
            "currency": settings.get("currency") or str(_setting("currency")),
            "company": settings.get("company") or str(_setting("company")),
            "client": settings.get("client") or "",
            "from_ts": from_ts,
            "to_ts": to_ts,
            "rate_nad": rate,
            "lines": lines,
            "total_hours": round(grand_s / 3600.0, 3),
            "total_seconds": grand_s,
            "total_nad": round(grand_billable_nad, 2),
            "nonbillable_hours": round(grand_nonbillable_s / 3600.0, 3),
            "generated_at": _now(),
        }
    finally:
        conn.close()


@router.get("/invoice.csv")
async def invoice_csv(
    from_ts: Optional[float] = Query(None),
    to_ts: Optional[float] = Query(None),
    task_id: Optional[str] = Query(None),
):
    inv = await get_invoice(from_ts=from_ts, to_ts=to_ts, task_id=task_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"Invoice: {inv['company']} -> {inv['client']}"])
    w.writerow([f"Currency: {inv['currency']}  |  Generated: {_iso(inv['generated_at'])}"])
    w.writerow([])
    w.writerow(["Task", "Billable", "Hours", "Entries", f"Amount ({inv['currency']})"])
    for ln in inv["lines"]:
        w.writerow([
            ln["task_name"],
            "yes" if ln["billable"] else "no",
            f"{ln['hours']:.3f}",
            ln["entries"],
            f"{ln['amount_nad']:.2f}",
        ])
    w.writerow([])
    w.writerow(["TOTAL", "", f"{inv['total_hours']:.3f}", "", f"{inv['total_nad']:.2f}"])
    return Response(
        content=buf.getvalue().encode("utf-8"),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=\"pow-invoice.csv\""},
    )


@router.get("/invoice.pdf")
async def invoice_pdf(
    from_ts: Optional[float] = Query(None),
    to_ts: Optional[float] = Query(None),
    task_id: Optional[str] = Query(None),
):
    inv = await get_invoice(from_ts=from_ts, to_ts=to_ts, task_id=task_id)
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.platypus import (
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except Exception as exc:  # pragma: no cover
        raise HTTPException(500, f"reportlab unavailable: {exc}")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=40, rightMargin=40, topMargin=40, bottomMargin=40,
        title=f"POW Invoice {inv['client']}",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("t", parent=styles["Title"], fontSize=20, spaceAfter=4)
    sub_style = ParagraphStyle(
        "s", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#64748b")
    )
    elems = [
        Paragraph(f"{inv['client']} — Time Invoice", title_style),
        Paragraph(
            f"{inv['company']} &nbsp;|&nbsp; {inv['currency']} &nbsp;|&nbsp; "
            f"Generated {_iso(inv['generated_at'])}",
            sub_style,
        ),
    ]
    if inv.get("from_ts") or inv.get("to_ts"):
        span = (
            f"Period: {_iso(inv['from_ts']) or 'start'} → "
            f"{_iso(inv['to_ts']) or 'now'}"
        )
        elems.append(Paragraph(span, sub_style))
    elems.append(Spacer(1, 12))

    head = ["Task", "Billable", "Hours", "Entries", f"Amount ({inv['currency']})"]
    data = [head]
    for ln in inv["lines"]:
        data.append([
            ln["task_name"],
            "yes" if ln["billable"] else "no",
            f"{ln['hours']:.2f}",
            str(ln["entries"]),
            f"{ln['amount_nad']:.2f}",
        ])
    data.append(["TOTAL", "", f"{inv['total_hours']:.2f}", "", f"{inv['total_nad']:.2f}"])

    table = Table(data, colWidths=[190, 60, 65, 60, 105])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a1d28")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#2d3348")),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("ALIGN", (4, 0), (4, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.HexColor("#14171f"), colors.HexColor("#1a1d28")]),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#0f1117")),
        ("TEXTCOLOR", (0, -1), (-1, -1), colors.HexColor("#6c5ce7")),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    elems.append(table)
    doc.build(elems)
    return Response(
        content=buf.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=\"pow-invoice.pdf\""},
    )


@router.get("/export.csv")
async def export_all_csv():
    """Full entry-level export (open + closed) for accounting."""
    _init_db()
    conn = _connect()
    try:
        rate = _settings_rate(conn)
        rows = conn.execute(
            "SELECT e.*, t.name AS task_name FROM entries e "
            "JOIN tasks t ON t.id = e.task_id ORDER BY e.start_ts ASC"
        ).fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "task_id", "task_name", "start", "end", "duration_s",
                    "running", "source", "note", "rate_nad", "amount_nad"])
        for r in rows:
            e = _row_to_entry(r, rate)
            w.writerow([
                e["id"], e["task_id"], r["task_name"], _iso(e["start_ts"]),
                _iso(e["end_ts"]), e["duration_s"] if e["duration_s"] is not None else "",
                "yes" if e["running"] else "no", e["source"], e["note"],
                e["rate_nad"], e["amount_nad"] if e["amount_nad"] is not None else "",
            ])
        return Response(
            content=buf.getvalue().encode("utf-8"),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=\"pow-time-entries.csv\""},
        )
    finally:
        conn.close()


@router.get("/settings")
async def get_settings_route():
    _init_db()
    conn = _connect()
    try:
        settings = _get_settings(conn)
        return {"settings": settings, "rate_nad": _settings_rate(conn)}
    finally:
        conn.close()


@router.patch("/settings")
async def update_settings(body: SettingsUpdate):
    _init_db()
    conn = _connect()
    try:
        mapping = []
        if body.rate_nad is not None:
            mapping.append(("rate_nad", str(body.rate_nad)))
        if body.currency is not None:
            mapping.append(("currency", body.currency))
        if body.company is not None:
            mapping.append(("company", body.company))
        if body.client is not None:
            mapping.append(("client", body.client))
        if body.auto_capture is not None:
            mapping.append(("auto_capture", "1" if body.auto_capture else "0"))
        if body.auto_task_id is not None:
            mapping.append(("auto_task_id", body.auto_task_id))
        if body.auto_idle_s is not None:
            mapping.append(("auto_idle_s", str(body.auto_idle_s)))
        for k, v in mapping:
            _set_setting(conn, k, v)
        if mapping:
            conn.commit()
        settings = _get_settings(conn)
        _publish({"type": "settings_updated", "settings": settings})
        return {"settings": settings, "rate_nad": _settings_rate(conn)}
    finally:
        conn.close()


# ── Background auto-capture ───────────────────────────────────────────────────
_AUTO_THREAD: Optional[threading.Thread] = None
_AUTO_STOP = threading.Event()
_AUTO_LOCK = threading.Lock()


def _is_transient(name: str) -> bool:
    low = name.lower()
    return low.endswith(TRANSIENT_SUFFIXES) or low.startswith(".~") or low.startswith("~$")


def _newest_mtime(base: Path) -> float:
    """Newest mtime under base, skipping VCS/build dirs, our data dir and
    transient editor artifacts. Bounded walk for pathological trees."""
    newest = 0.0
    seen = 0
    try:
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                if _is_transient(f):
                    continue
                seen += 1
                if seen > WALK_MAX_FILES:
                    log.debug("mtime walk hit file cap at %s", root)
                    return newest
                try:
                    mt = os.stat(os.path.join(root, f)).st_mtime
                except OSError:
                    continue
                if mt > newest:
                    newest = mt
    except Exception:
        pass
    return newest


def _get_settings_cached() -> dict:
    conn = _connect()
    try:
        return _get_settings(conn)
    finally:
        conn.close()


def _settings_for_db(db: Path) -> dict:
    conn = _connect_for(db)
    try:
        return _get_settings(conn)
    finally:
        conn.close()


def _rate_for_db(db: Path) -> float:
    try:
        return float(_settings_for_db(db).get("rate_nad") or 0)
    except (TypeError, ValueError):
        return 0.0


def _entry_with_task_in(db: Path, entry_id: str, rate: float) -> dict:
    conn = _connect_for(db)
    try:
        return _entry_with_task(conn, entry_id, rate)
    finally:
        conn.close()


def _open_auto_in(db: Path, task_id: str) -> Optional[str]:
    conn = _connect_for(db)
    try:
        eid = _new_id("ent")
        conn.execute(
            "INSERT INTO entries(id, task_id, start_ts, end_ts, duration_s, source, note, created_at) "
            "VALUES(?, ?, ?, NULL, NULL, 'auto', ?, ?)",
            (eid, task_id, _now(), "auto-captured from folder activity", _now()),
        )
        conn.commit()
    finally:
        conn.close()
    entry = _entry_with_task_in(db, eid, _rate_for_db(db))
    _publish({"type": "entry_created", "entry": entry})
    _publish({"type": "auto_status", "running": True})
    return eid


def _close_auto_in(db: Path, entry_id: str) -> None:
    conn = _connect_for(db)
    try:
        end = _now()
        # Conditional update: never clobber a timer that was stopped manually
        # between our read and write (the known auto-vs-manual race).
        cur = conn.execute("SELECT start_ts FROM entries WHERE id=?", (entry_id,)).fetchone()
        if cur:
            dur = max(0.0, end - cur["start_ts"])
            upd = conn.execute(
                "UPDATE entries SET end_ts=?, duration_s=? WHERE id=? AND end_ts IS NULL",
                (end, dur, entry_id),
            )
            if upd.rowcount:
                conn.commit()
            else:
                conn.rollback()
    finally:
        conn.close()
    entry = _entry_with_task_in(db, entry_id, _rate_for_db(db))
    if entry:
        _publish({"type": "entry_updated", "entry": entry})
    _publish({"type": "auto_status", "running": False})


def _open_auto(task_id: str) -> Optional[str]:
    _, db = _resolve_db()
    return _open_auto_in(db, task_id)


def _close_auto(entry_id: str) -> None:
    _, db = _resolve_db()
    _close_auto_in(db, entry_id)


def _settings_rate_from_cached() -> float:
    try:
        return float(_get_settings_cached().get("rate_nad") or 0)
    except (TypeError, ValueError):
        return 0.0


def _entry_with_task_safe(entry_id: str, rate: float) -> dict:
    conn = _connect()
    try:
        return _entry_with_task(conn, entry_id, rate)
    finally:
        conn.close()


def _default_auto_task_id(db: Path) -> str:
    """Return (creating if needed) the default auto-capture task id for a
    client DB, so all-folder capture never stalls on a missing task selection."""
    conn = _connect_for(db)
    try:
        row = conn.execute(
            "SELECT id FROM tasks WHERE name='Auto-captured work' AND archived=0 "
            "ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row:
            return row["id"]
        tid = _new_id("task")
        conn.execute(
            "INSERT INTO tasks(id, name, billable, created_at, archived) "
            "VALUES(?, 'Auto-captured work', 1, ?, 0)",
            (tid, _now()),
        )
        conn.commit()
        return tid
    finally:
        conn.close()


def _auto_capture_loop() -> None:
    """Poll EVERY registered client folder's mtimes; open an auto-entry in that
    client's own DB on fresh activity, close it after the configured idle window.
    Debounced so rapid save-bursts don't churn entries. Handles any number of
    folders under CLIENTS_ROOT — no client goes untracked."""
    # Per-folder state: newest mtime observed and the open auto entry id.
    _clients: list[sqlite3.Row] = []
    state: dict[str, tuple[float, Optional[str]]] = {}
    while not _AUTO_STOP.is_set():
        try:
            _AUTO_STOP.wait(AUTO_POLL_S)
            if _AUTO_STOP.is_set():
                break
            # Refresh the client registry each tick so newly added folders/clients
            # are picked up automatically without a restart.
            try:
                _clients = _all_clients()
            except Exception:
                pass
            now = _now()
            for row in _clients:
                folder = Path(row["folder"])
                db = _db_for_row(row)
                _ensure_db(db)
                settings = _settings_for_db(db)
                if settings.get("auto_capture") != "1":
                    # Auto-capture off: make sure any open entry is closed.
                    if state.get(str(db)):
                        _, eid = state[str(db)]
                        if eid:
                            _close_auto_in(db, eid)
                        state[str(db)] = (0.0, None)
                    continue
                task_id = settings.get("auto_task_id") or ""
                if not task_id:
                    task_id = _default_auto_task_id(db)
                if not task_id:
                    continue
                try:
                    idle_s = max(
                        60.0, float(settings.get("auto_idle_s") or AUTO_IDLE_DEFAULT_S)
                    )
                except (TypeError, ValueError):
                    idle_s = AUTO_IDLE_DEFAULT_S

                newest = _newest_mtime(folder)
                last_activity, open_entry_id = state.get(str(db), (0.0, None))
                # Debounce: only treat activity as new if clearly fresher than the
                # last recorded burst (absorbs multi-save storms).
                if newest and newest > last_activity + AUTO_DEBOUNCE_S:
                    last_activity = newest
                if open_entry_id is None:
                    if newest and (now - last_activity) < idle_s:
                        open_entry_id = _open_auto_in(db, task_id)
                elif (now - last_activity) >= idle_s:
                    _close_auto_in(db, open_entry_id)
                    open_entry_id = None
                state[str(db)] = (last_activity, open_entry_id)
        except Exception as exc:  # pragma: no cover
            log.debug("auto-capture tick error: %s", exc)
    # Shutdown: close anything still open.
    for db, (_la, eid) in state.items():
        if eid:
            try:
                _close_auto_in(Path(db), eid)
            except Exception:
                pass


def start_auto_capture() -> None:
    global _AUTO_THREAD
    with _AUTO_LOCK:
        if _AUTO_THREAD and _AUTO_THREAD.is_alive():
            return
        _AUTO_STOP.clear()
        t = threading.Thread(target=_auto_capture_loop, name="pow-auto-capture", daemon=True)
        t.start()
        _AUTO_THREAD = t


# ── Live events WebSocket ─────────────────────────────────────────────────────
@router.websocket("/events")
async def events_ws(ws: WebSocket):
    await ws.accept()
    _set_main_loop(asyncio.get_running_loop())
    q: asyncio.Queue = asyncio.Queue(maxsize=WS_QUEUE_MAX)
    _SUBS.add(q)
    try:
        snap = await get_state()
        await ws.send_text(json.dumps({"type": "snapshot", **snap}))
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=WS_HEARTBEAT_S)
                await ws.send_text(msg)
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"type": "ping", "t": _now()}))
    except WebSocketDisconnect:
        pass
    finally:
        _SUBS.discard(q)


# Init once at plugin-mount time.
try:
    _init_db()
    start_auto_capture()
except Exception as exc:  # pragma: no cover
    log.warning("POW Tracker init failed: %s", exc)
