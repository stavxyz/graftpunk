# tests/unit/cli_harness.py
"""Shared CLI harness: register one plugin on a fresh app and invoke it.

Used by test_plugin_runtime_session.py, test_login_identity.py and
test_multi_account_e2e.py — one copy, so the harness cannot drift.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

if TYPE_CHECKING:
    from graftpunk.session import BrowserSession


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


def cacheable_browser_session(
    identifier: str | None = None, backend_type: str = "nodriver"
) -> BrowserSession:
    """A session whose rider attributes survive the cache's pickle round-trip.

    A plain ``requests.Session`` pickles through its ``__attrs__`` whitelist,
    which drops the rider attributes, so a test that caches one and loads it
    back cannot tell two accounts apart: the account identifier is gone.
    ``BrowserSession`` round-trips them in ``__getstate__``/``__setstate__``.
    Built with ``__new__`` so no browser process starts and no driver is
    required: this is a cache fixture, not a live session.

    Args:
        identifier: The account identifier to stamp, or None for a session
            with no recorded account.
        backend_type: The backend the cached session claims to come from.

    Returns:
        A ``BrowserSession`` ready to hand to ``cache_session``.
    """
    import requests

    from graftpunk.session import BrowserSession
    from graftpunk.session_identity import GP_ACCOUNT_ATTR

    session = BrowserSession.__new__(BrowserSession)
    requests.Session.__init__(session)
    session._backend_type = backend_type
    if identifier is not None:
        setattr(session, GP_ACCOUNT_ATTR, identifier)
    return session
