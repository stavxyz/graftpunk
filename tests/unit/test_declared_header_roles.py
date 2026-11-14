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


def test_gp_http_layers_a_declared_built_in_role_on_another_origin() -> None:
    from graftpunk.cli.http_commands import _make_request

    session, wire = _session()
    with (
        patch(
            "graftpunk.cli.http_commands.load_session_for_api_resolved",
            side_effect=echo_loaded_session(session),
        ),
        patch("graftpunk.cli.plugin_commands.get_plugin_for_session", return_value=_plugin()),
    ):
        _make_request(
            "GET", "https://api.myshop.example/v2/me", session_name="declshop", role="xhr"
        )
    sent = _sent_to(wire, "https://api.myshop.example/v2/me")
    assert sent.headers["X-Shop-Build"] == "declared"
    assert sent.headers["Sec-Fetch-Mode"] == "cors"
    assert sent.headers["Sec-Fetch-Site"] == "same-site"
    assert sent.headers["Origin"] == "https://myshop.example"


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


def _applied(
    captured: dict[str, dict[str, str]], declared: dict[str, dict[str, str]]
) -> tuple[GraftpunkSession, _Wire]:
    """A session as a plugin command or the client sets it up."""
    from types import SimpleNamespace

    from graftpunk.graftpunk_session import apply_plugin_context

    session = GraftpunkSession(header_roles={k: dict(v) for k, v in captured.items()})
    plugin = SimpleNamespace(
        base_url="https://myshop.example", header_roles=declared, site_name="declshop"
    )
    apply_plugin_context(session, plugin, declared_roles_override_captured=False)
    wire = _Wire()
    session.mount("https://", wire)
    return session, wire


class TestDeclaredRolesInCommandsAndTheClient:
    """On the site's own origin a command sends what 1.42.8 sent; elsewhere a
    declared role of the name is used (refined ruling, round 3)."""

    def test_a_login_that_captured_nothing_still_sends_a_plain_get_unchanged(self) -> None:
        declared = {"xhr": {"Accept": "application/json", "X-Api-Version": "2"}}
        session, wire = _applied({}, declared)
        bare = GraftpunkSession(base_url="https://myshop.example")
        bare_wire = _Wire()
        bare.mount("https://", bare_wire)
        session.get("https://myshop.example/page")
        bare.get("https://myshop.example/page")
        [sent], [expected] = wire.sent, bare_wire.sent
        assert dict(sent.headers) == dict(expected.headers)
        assert "Sec-Fetch-Mode" not in sent.headers

    def test_a_declared_built_in_name_the_login_did_not_capture(self) -> None:
        captured = {"navigation": {"Accept": "text/html", "X-Shop-Build": "captured"}}
        declared = {"xhr": {"Accept": "application/json", "X-Api-Version": "2"}}
        session, wire = _applied(captured, declared)
        session.xhr("GET", "https://myshop.example/api/orders")
        session.xhr("GET", "https://api.myshop.example/v2/orders")
        own = _sent_to(wire, "https://myshop.example/api/orders")
        assert own.headers["X-Requested-With"] == "XMLHttpRequest"
        assert "X-Api-Version" not in own.headers
        other = _sent_to(wire, "https://api.myshop.example/v2/orders")
        # The built-in xhr role as the cross-origin rule adjusts it, with the
        # declared headers merged over it (the declared Accept wins).
        assert other.headers["X-Api-Version"] == "2"
        assert other.headers["Accept"] == "application/json"
        assert other.headers["Sec-Fetch-Mode"] == "cors"
        assert other.headers["Sec-Fetch-Dest"] == "empty"
        assert other.headers["Sec-Fetch-Site"] == "same-site"
        assert other.headers["Origin"] == "https://myshop.example"
        assert "X-Requested-With" not in other.headers

    def test_a_declared_role_under_a_captured_name(self) -> None:
        captured = {"xhr": {"Accept": "application/json", "X-Shop-Build": "captured"}}
        declared = {"xhr": {"Sec-Fetch-Mode": "cors", "X-Api-Key": "k1"}}
        session, wire = _applied(captured, declared)
        session.xhr("GET", "https://myshop.example/me")
        session.xhr("GET", "https://api.myshop.example/v2/me")
        own = _sent_to(wire, "https://myshop.example/me")
        assert own.headers["X-Shop-Build"] == "captured"
        assert "X-Api-Key" not in own.headers
        other = _sent_to(wire, "https://api.myshop.example/v2/me")
        assert other.headers["X-Api-Key"] == "k1"
        assert "X-Shop-Build" not in other.headers
        assert other.headers["Origin"] == "https://myshop.example"
        assert other.headers["Sec-Fetch-Dest"] == "empty"

    def test_a_declared_header_replaces_the_built_in_one_in_any_case(self) -> None:
        declared = {"xhr": {"accept": "text/csv", "sec-fetch-site": "none"}}
        session, wire = _applied({}, declared)
        session.xhr("GET", "https://api.myshop.example/v2/export")
        sent = _sent_to(wire, "https://api.myshop.example/v2/export")
        assert [k for k in sent.headers if k.lower() == "accept"] == ["accept"]
        assert sent.headers["Accept"] == "text/csv"
        # Sec-Fetch-Site is always the computed relation to the target.
        assert sent.headers["Sec-Fetch-Site"] == "same-site"
        assert sent.headers["Sec-Fetch-Mode"] == "cors"

    def test_a_declared_custom_name_is_sent_as_declared_elsewhere(self) -> None:
        session, wire = _applied({}, {"api": {"X-Api-Key": "k1"}})
        session.request_with_role("api", "GET", "https://api.myshop.example/v2")
        sent = _sent_to(wire, "https://api.myshop.example/v2")
        assert sent.headers["X-Api-Key"] == "k1"
        assert "Sec-Fetch-Mode" not in sent.headers

    def test_a_custom_name_only_the_plugin_declares_is_used_when_named(
        self, captured_logs: list[dict[str, Any]]
    ) -> None:
        session, wire = _applied({}, {"api": {"X-Api-Key": "k1"}})
        session.request_with_role("api", "GET", "https://myshop.example/api")
        session.request_with_role("nosuch", "GET", "https://myshop.example/other")
        assert _sent_to(wire, "https://myshop.example/api").headers["X-Api-Key"] == "k1"
        [unknown] = [e for e in captured_logs if e["event"] == "unknown_role_no_headers_applied"]
        assert unknown["role"] == "nosuch"
        assert unknown["plugin_roles"] == ["api"]


# Declarations copied from a recording of the site's own pages.
_COPIED_ROLES = {
    "xhr": {"Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin", "X-Api-Version": "2"},
    "navigation": {"Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"},
}


def _gp_http_sent(role: str, url: str) -> requests.PreparedRequest:
    """What gp http sends for *role* to *url* with a plugin declaring _COPIED_ROLES."""
    from types import SimpleNamespace

    from graftpunk.cli.http_commands import _make_request

    session = GraftpunkSession()
    wire = _Wire()
    session.mount("https://", wire)
    plugin = SimpleNamespace(
        base_url="https://myshop.example",
        header_roles=_COPIED_ROLES,
        site_name="declshop",
        token_config=None,
    )
    with (
        patch(
            "graftpunk.cli.http_commands.load_session_for_api_resolved",
            side_effect=echo_loaded_session(session),
        ),
        patch("graftpunk.cli.plugin_commands.get_plugin_for_session", return_value=plugin),
    ):
        _make_request("GET", url, session_name="declshop", role=role)
    return _sent_to(wire, url)


def _command_sent(role: str, url: str) -> requests.PreparedRequest:
    session, wire = _applied({"navigation": {"Accept": "text/html"}}, _COPIED_ROLES)
    session.request_with_role(role, "GET", url)
    return _sent_to(wire, url)


@pytest.mark.parametrize("path", [_command_sent, _gp_http_sent], ids=["command", "gp-http"])
def test_a_declared_sec_fetch_site_is_replaced_by_the_computed_one(path: Any) -> None:
    sent = path("xhr", "https://api.myshop.example/v2/orders")
    assert sent.headers["Sec-Fetch-Site"] == "same-site"
    assert sent.headers["X-Api-Version"] == "2"


@pytest.mark.parametrize("path", [_command_sent, _gp_http_sent], ids=["command", "gp-http"])
def test_origin_follows_the_merged_fetch_mode(path: Any) -> None:
    """A declared navigation with Sec-Fetch-Mode: cors is a CORS GET, which Chrome
    sends with an Origin."""
    sent = path("navigation", "https://api.myshop.example/v2/page")
    assert sent.headers["Sec-Fetch-Mode"] == "cors"
    assert sent.headers["Origin"] == "https://myshop.example"
    assert sent.headers["Sec-Fetch-Site"] == "same-site"
