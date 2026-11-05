"""Grep-gate tests: defaults are generic, no client/personal/company names in
the shipped tree, the tracked tree is R17-clean, and the dashboard JS stays
inside the catalog desktop-surface rules (rule 08).

These tests intentionally use only the standard library so they run under any
Python (run them from the plugin root with the hermes venv that has the
plugin's dependencies; test_settings needs fastapi/pydantic).
"""

from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

# Strings that must NOT survive as hardcoded values anywhere in the shipped
# tree (code + docs + manifests). This file (the checker's own fixture list)
# and tests/ generally are the one allowed place they appear.
FORBIDDEN_STRINGS = [
    # client / company / personal names (mission constraint 3)
    "Smart Polar Distributors",
    "smart-polar-distributors",
    "OKIN",
    "okinent",
    "AI Clients",
    "General-Docs",
    "Kingpin",
    "Obsidian-Reports",
    "FundGen",
    "JARVIS",
    "TARS",
    "Tjongarero",
    "fromthejump",
    # personal numbers
    "2450",
    "81 892",
    # hardcoded absolute personal paths
    "/Users/",
]

# The shipped surface = everything the catalog pins, minus the tests (the
# checker itself) and VCS internals.
SCAN_EXCLUDE_PARTS = {"tests", ".git", "__pycache__", ".pytest_cache"}

# Catalog rule 08 desktop-surface forbidden constructs (simple literal checks
# are enough here: the file is kept free of them, and `hermes plugins
# validate` runs the full lint at admission).
JS_FORBIDDEN_TOKENS = [
    "eval(",
    "new Function",
    "import(",
    "document.body",
    "document.querySelector",
    "document.querySelectorAll",
    "document.getElementById",
    "getElementsBy",
    "MutationObserver",
    "Object.defineProperty",
    "Storage.prototype",
    "data-slot",
    "data-tour",
    "data-sidebar",
    "data-testid",
    "data-tree-",
    "<script",
    "webview",
]

# R17: nothing compiled or machine-generated may be tracked.
R17_TRACKED_FORBIDDEN = (".pyc", ".pyo", ".DS_Store")


def _shipped_files() -> list[Path]:
    """Every file of the shipped tree (no tests/, no VCS, no caches)."""
    out = []
    for path in sorted(PLUGIN_ROOT.rglob("*")):
        rel = path.relative_to(PLUGIN_ROOT).parts
        if any(part in SCAN_EXCLUDE_PARTS or part.startswith(".") for part in rel):
            # hidden dirs (.git, caches) are excluded; the root .gitignore
            # itself is a shipped single hidden *file* and stays in scope.
            if not (len(rel) == 1 and rel[0] == ".gitignore"):
                continue
        if path.is_file():
            out.append(path)
    return out


def _git_tracked_files() -> list[str]:
    """Files git tracks (the R17 test's scope: what a pin would ship)."""
    try:
        out = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "ls-files", "-z"],
            cwd=PLUGIN_ROOT, capture_output=True, check=True, timeout=60,
            stdin=subprocess.DEVNULL,
        ).stdout
        return [os.fsdecode(p) for p in out.split(b"\0") if p]
    except (OSError, subprocess.SubprocessError):
        return []


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


def _yaml_mapping(text: str, key: str) -> str:
    """Tiny indentation-based reader for one top-level mapping value in a
    simple YAML manifest (no dependency on a YAML library)."""
    lines = text.splitlines()
    in_block = False
    collected = []
    indent = 0
    for line in lines:
        if not in_block:
            if re.match(rf"^{re.escape(key)}:\s*(\S.*)?$", line):
                in_block = True
                indent = len(line) - len(line.lstrip())
                rest = line.split(":", 1)[1].strip()
                if rest:
                    return rest
                continue
        else:
            stripped = line.strip()
            if stripped and (len(line) - len(line.lstrip())) <= indent:
                break
            if stripped:
                collected.append(stripped)
    return " ".join(collected)


def _config_schema_defaults(yaml_text: str) -> dict:
    """{setting key: (default value text, type text)} from config_schema."""
    defaults: dict = {}
    lines = yaml_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^  ([A-Za-z0-9_]+):\s*$", line)
        if m and _yaml_mapping(yaml_text, "config_schema"):
            # a config_schema entry if we are inside the config_schema block
            block = []
            j = i + 1
            while j < len(lines):
                if re.match(r"^    \S", lines[j]) or lines[j].strip() == "":
                    if lines[j].strip():
                        block.append(lines[j].strip())
                    j += 1
                else:
                    break
            entry = " ".join(block)
            dm = re.search(
                r" default: (.+?)(?= (?:label|description|required|type): |$)",
                entry)
            # ``(?:^| )``: the joined entry line starts with ``type:``.
            tm = re.search(
                r"(?:^| )type: (.+?)(?= (?:default|required|label|description): |$)",
                entry)
            defaults[m.group(1)] = (
                dm.group(1).strip().strip('"').strip("'") if dm else None,
                tm.group(1).strip().strip('"').strip("'") if tm else None,
            )
            i = j
            continue
        i += 1
    return defaults


class GenericDefaultsGate(unittest.TestCase):
    """Every plugin default is generic: no personal path, name or number."""

    def test_manifest_version_and_version(self):
        text = _read(PLUGIN_ROOT / "plugin.yaml")
        self.assertIn('version: "0.3.0"', text)
        self.assertIn("manifest_version: 2", text)
        self.assertIn('requires_hermes: ">=0.22.6"', text)
        self.assertIn("capabilities: []", text)

    def test_config_schema_defaults_are_generic(self):
        text = _read(PLUGIN_ROOT / "plugin.yaml")
        defaults = _config_schema_defaults(text)
        self.assertEqual(
            sorted(defaults),
            sorted(["clients_root", "legacy_clients_root", "ignore_dirs",
                    "default_rate", "currency", "company"]),
            "config_schema must declare exactly the six settings",
        )
        self.assertEqual(defaults["clients_root"][0], "~/Documents/Clients")
        self.assertEqual(defaults["clients_root"][1], "str")
        self.assertEqual(defaults["legacy_clients_root"][0], "")
        self.assertEqual(defaults["ignore_dirs"][0], "[Docs, Archive, Research, Templates]")
        self.assertEqual(defaults["ignore_dirs"][1], "list")
        self.assertEqual(defaults["default_rate"][0], "100.0")
        self.assertEqual(defaults["default_rate"][1], "float")
        self.assertEqual(defaults["currency"][0], "USD")
        self.assertEqual(defaults["currency"][1], "str")
        self.assertEqual(defaults["company"][0], "My Company")
        self.assertEqual(defaults["company"][1], "str")

    def test_python_dependencies_floors_and_bounds(self):
        text = _read(PLUGIN_ROOT / "plugin.yaml")
        deps = re.findall(r'^\s+- "(.+)"$', text, re.M)
        self.assertIn("fastapi>=0.104.0,<1", deps)
        self.assertIn("pydantic>=2.0,<3", deps)
        self.assertEqual(len(deps), 2, "only what the code imports at module level")
        for dep in deps:
            self.assertRegex(dep, r",<", f"python dependency {dep!r} needs an upper bound")

    def test_dashboard_manifest_version_and_neutral_description(self):
        import json

        data = json.loads(_read(PLUGIN_ROOT / "dashboard" / "manifest.json"))
        self.assertEqual(data["version"], "0.3.0")
        self.assertEqual(data["name"], "pow-tracker")
        self.assertEqual(data["api"], "plugin_api.py")
        self.assertEqual(data["entry"], "plugin.js")
        for forbidden in FORBIDDEN_STRINGS:
            self.assertNotIn(
                forbidden, json.dumps(data),
                f"dashboard/manifest.json leaks {forbidden!r}",
            )

    def test_no_hardcoded_paths_in_api_defaults(self):
        api = _read(PLUGIN_ROOT / "dashboard" / "plugin_api.py")
        self.assertIn('"clients_root": "~/Documents/Clients"', api)
        self.assertIn('"default_rate": 100.0', api)
        self.assertIn('"currency": "USD"', api)
        self.assertIn('"company": "My Company"', api)


class NoNamesInShippedTreeGate(unittest.TestCase):
    """No client names / personal names / company names anywhere shipped."""

    def test_no_forbidden_strings_in_shipped_tree(self):
        files = _shipped_files()
        self.assertGreater(len(files), 6, "the shipped tree unexpectedly tiny")
        for path in files:
            text = _read(path)
            for forbidden in FORBIDDEN_STRINGS:
                self.assertNotIn(
                    forbidden, text,
                    f"{path.relative_to(PLUGIN_ROOT)} leaks {forbidden!r}",
                )


class DesktopSurfaceGate(unittest.TestCase):
    """dashboard/plugin.js stays inside the catalog desktop-surface rules."""

    def test_no_forbidden_js_tokens(self):
        js = _read(PLUGIN_ROOT / "dashboard" / "plugin.js")
        self.assertTrue(js, "dashboard/plugin.js missing/unreadable")
        for token in JS_FORBIDDEN_TOKENS:
            self.assertNotIn(token, js, f"plugin.js uses forbidden {token!r}")

    def test_only_sdk_react_static_imports(self):
        js = _read(PLUGIN_ROOT / "dashboard" / "plugin.js")
        for line in js.splitlines():
            stripped = line.strip()
            if stripped.startswith("import ") and " from " in stripped:
                spec = stripped.split(" from ", 1)[1].strip().strip("';\"")
                self.assertIn(
                    spec, ("@hermes/plugin-sdk", "react", "react/jsx-runtime"),
                    f"plugin.js imports non-SDK specifier {spec!r}",
                )

    def test_no_bare_default_reference(self):
        """The old `register('pow-tracker', default)` tail was a SyntaxError
        (bare `default` is not a valid identifier); the default export is a
        named const now."""
        js = _read(PLUGIN_ROOT / "dashboard" / "plugin.js")
        self.assertNotIn(", default)", js)
        self.assertRegex(js, r"const plugin = \{")
        self.assertRegex(js, r"export default plugin")
        self.assertRegex(js, r"register\('pow-tracker', plugin\)")


class CleanTreeGate(unittest.TestCase):
    """R17: no compiled binaries, no bytecode, no junk in the tracked tree."""

    def test_no_tracked_junk(self):
        tracked = _git_tracked_files()
        if not tracked:
            self.skipTest("not a git checkout (no pins to test)")
        for rel in tracked:
            for suffix in R17_TRACKED_FORBIDDEN:
                self.assertFalse(rel.endswith(suffix), f"tracked junk: {rel}")
            self.assertNotIn("__pycache__/", rel.replace(os.sep, "/"), rel)
            self.assertFalse(os.path.basename(rel).startswith("._"), rel)

    def test_no_binaries_in_worktree(self):
        for path in _shipped_files():
            rel = path.relative_to(PLUGIN_ROOT).as_posix()
            self.assertFalse(rel.endswith(R17_TRACKED_FORBIDDEN), rel)
            self.assertNotIn("__pycache__", rel)
            self.assertFalse(path.name.startswith("._"), rel)
            self.assertNotEqual(path.name, ".DS_Store", rel)

    def test_no_symlinks_in_shipped_tree(self):
        for path in _shipped_files():
            self.assertFalse(path.is_symlink(), f"symlink in the tree: {path}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
