# tests/unit/cli_harness.py
"""Shared CLI harness: register one plugin on a fresh app and invoke it.

Used by test_plugin_runtime_session.py, test_login_identity.py and
test_multi_account_e2e.py — one copy, so the harness cannot drift.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch


def echo_loaded_session(session: Any = None) -> Any:
    """A ``side_effect`` for a patched ``load_session_for_api_resolved``.

    The real loader returns ``(session, loaded_name)``: the name it actually
    loaded from, which the caller then keys its write-backs off (#182). On an
    exact hit that IS the requested name, so a test that only needs the load
    stubbed echoes the name back rather than hard-coding one.
    """
    import requests

    loaded = requests.Session() if session is None else session
    return lambda name, **_kwargs: (loaded, name)


def invoke_plugin_app(plugin, argv, **env):  # noqa: ANN001, ANN002, ANN003, ANN201
    import typer
    from typer.testing import CliRunner

    from graftpunk.cli.plugin_commands import register_plugin_commands

    app = typer.Typer()
    with patch("graftpunk.cli.plugin_commands.discover_all_plugins", return_value=(plugin,)):
        register_plugin_commands(app, notify_errors=False)
    return CliRunner().invoke(app, argv, env={"COLUMNS": "200", **env})
