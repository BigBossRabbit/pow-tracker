"""
POW Tracker plugin root.
"""

from __future__ import annotations

from hermes_cli.plugins import PluginRegistration


def register(ctx: PluginRegistration) -> None:
    """Dashboard + backend plugin — the dashboard section in plugin.yaml drives
    frontend/backend wiring; register() exists for plugin-manager load hygiene."""
    pass