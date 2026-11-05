"""
POW Tracker plugin root.
"""

from __future__ import annotations


def register(ctx) -> None:
    """Dashboard + backend plugin — the dashboard section in plugin.yaml drives
    frontend/backend wiring; register() exists for plugin-manager load hygiene.

    No hermes imports here on purpose: `hermes_cli.plugins`'s registration
    context is not in the catalog's public-plugin-import allowlist, and a
    pass-only register() needs no type annotation.
    """
    pass
