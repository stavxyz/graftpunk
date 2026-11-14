"""A plugin's declared ``header_roles`` reach plugin commands, the Python client and
``gp http`` through one step (``apply_plugin_context``)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
import requests

from graftpunk.graftpunk_session import GraftpunkSession
from tests.unit.cli_harness import echo_loaded_session
from tests.unit.cli_harness import invoke_plugin_app as _invoke

# What the login captured on the site's own origin.
_CAPTURED = {"xhr": {"Accept": "application/json", "X-Shop-Build": "captured"}}


class _Wire(requests.adapters.BaseAdapter):
    """Records each request and answers 200 with a JSON ``{}``."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[requests.PreparedRequest] = []

    def send(self, request, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.sent.append(request)
        response = requests.Response()
        response.status_code = 200
        response.request = request
        response.url = request.url
        response.headers["Content-Type"] = "application/json"
        response._content = b"{}"
        return response

    def close(self) -> None:
        pass


def _session() -> tuple[GraftpunkSession, _Wire]:
    session = GraftpunkSession(header_roles={k: dict(v) for k, v in _CAPTURED.items()})
    wire = _Wire()
    session.mount("https://", wire)
    return session, wire


def _sent_to(wire: _Wire, url: str) -> requests.PreparedRequest:
    [sent] = [r for r in wire.sent if r.url == url]
    return sent


def _plugin() -> Any:
    from graftpunk.plugins.cli_plugin import SitePlugin, command

    class ShopPlugin(SitePlugin):
        site_name = "declshop"
        base_url = "https://myshop.example"
        session_name = "declshop"
        requires_session = True
        header_roles = {  # noqa: RUF012 - a plugin's class-level declaration
            "api": {"Sec-Fetch-Mode": "cors", "X-Api-Key": "k1"},
            "xhr": {"Accept": "application/json", "X-Shop-Build": "declared"},
        }

        @command(help="Call the API host and the site")
        def calls(self, ctx):  # noqa: ANN001, ANN202
            ctx.session.request_with_role("api", "GET", "https://api.myshop.example/x")
            ctx.session.request_with_role("api", "GET", "https://myshop.example/api")
            ctx.session.xhr("GET", "https://myshop.example/me")
            return {"ok": True}

    return ShopPlugin()


@pytest.fixture(autouse=True)
def _no_ambient_session(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # noqa: ANN001
    monkeypatch.delenv("GRAFTPUNK_SESSION", raising=False)
    monkeypatch.chdir(tmp_path)


def _assert_command_requests(wire: _Wire) -> None:
    cross = _sent_to(wire, "https://api.myshop.example/x")
    assert cross.headers["X-Api-Key"] == "k1"
    assert cross.headers["Origin"] == "https://myshop.example"
    assert cross.headers["Sec-Fetch-Site"] == "same-site"
    own = _sent_to(wire, "https://myshop.example/api")
    assert own.headers["X-Api-Key"] == "k1"
    # A captured role of the same name keeps precedence in a command.
    assert _sent_to(wire, "https://myshop.example/me").headers["X-Shop-Build"] == "captured"


@patch("graftpunk.cli.plugin_runtime.update_session_cookies")
@patch("graftpunk.cli.plugin_runtime.list_sessions", return_value=["declshop"])
def test_a_plugin_command_gets_the_declared_roles_where_nothing_was_captured(
    _ls: Any, _update: Any
) -> None:
    session, wire = _session()
    with patch(
        "graftpunk.cli.plugin_runtime.load_session_for_api_resolved",
        side_effect=echo_loaded_session(session),
    ):
        result = _invoke(_plugin(), ["declshop", "calls"])
    assert result.exit_code == 0, result.output
    _assert_command_requests(wire)


def test_the_python_client_gets_them_the_same_way() -> None:
    from graftpunk.client import GraftpunkClient

    session, wire = _session()
    with (
        patch("graftpunk.client.get_plugin", return_value=_plugin()),
        patch("graftpunk.client.list_sessions", return_value=["declshop"]),
        patch(
            "graftpunk.client.load_session_for_api_resolved",
            side_effect=echo_loaded_session(session),
        ),
        patch("graftpunk.client.update_session_cookies"),
    ):
        GraftpunkClient("declshop").execute("calls")
    _assert_command_requests(wire)


def test_gp_http_lets_a_declared_role_replace_the_captured_one() -> None:
    from graftpunk.cli.http_commands import _make_request

    session, wire = _session()
    with (
        patch(
            "graftpunk.cli.http_commands.load_session_for_api_resolved",
            side_effect=echo_loaded_session(session),
        ),
        patch("graftpunk.cli.plugin_commands.get_plugin_for_session", return_value=_plugin()),
    ):
        _make_request("GET", "https://myshop.example/me", session_name="declshop", role="xhr")
    assert _sent_to(wire, "https://myshop.example/me").headers["X-Shop-Build"] == "declared"
