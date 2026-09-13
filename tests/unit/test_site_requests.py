"""SiteRequests and the CommandContext delegates (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests
import structlog
from structlog.testing import capture_logs

from graftpunk.exceptions import CommandError, SessionRejectedError, UnexpectedResponseError
from graftpunk.graftpunk_session import GraftpunkSession
from graftpunk.plugins.cli_plugin import CommandContext
from graftpunk.plugins.site_requests import SiteRequests


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        content_type: str = "application/json",
        text: str = "{}",
        request_method: str = "GET",
        url: str = "https://myshop.example.com/x",
    ) -> None:
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        self.text = text
        self.url = url
        self.request = requests.PreparedRequest()
        self.request.method = request_method
        self.request.url = url

    def json(self) -> Any:
        return json.loads(self.text)


class _RoleAwareSession:
    """A duck-typed session with request_with_role, like GraftpunkSession."""

    def __init__(self, response: _FakeResponse) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._response = response

    def request_with_role(self, role: str, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((role, method, url))
        return self._response


class TestJsonHappyPath:
    def test_2xx_json_returns_parsed_body(self) -> None:
        session = _RoleAwareSession(_FakeResponse(200, text=json.dumps({"a": 1})))
        result = SiteRequests(session, "myshop", "https://myshop.example.com").json(
            "GET", "/orders"
        )
        assert result == {"a": 1}

    def test_relative_url_resolved_against_base_url(self) -> None:
        session = _RoleAwareSession(_FakeResponse(200))
        SiteRequests(session, "myshop", "https://myshop.example.com/").json("GET", "/orders")
        assert session.calls[0][2] == "https://myshop.example.com/orders"

    def test_absolute_url_used_as_is(self) -> None:
        session = _RoleAwareSession(_FakeResponse(200))
        SiteRequests(session, "myshop", "https://myshop.example.com").json(
            "GET", "https://other.example.com/x"
        )
        assert session.calls[0][2] == "https://other.example.com/x"

    def test_default_role_is_xhr(self) -> None:
        session = _RoleAwareSession(_FakeResponse(200))
        SiteRequests(session, "myshop", "https://myshop.example.com").json("GET", "/orders")
        assert session.calls[0][0] == "xhr"

    def test_explicit_role_honoured(self) -> None:
        session = _RoleAwareSession(_FakeResponse(200))
        SiteRequests(session, "myshop", "https://myshop.example.com").json(
            "POST", "/submit", role="form"
        )
        assert session.calls[0][0] == "form"


class TestTextHappyPath:
    def test_returns_any_2xx_body_as_text(self) -> None:
        session = _RoleAwareSession(
            _FakeResponse(200, content_type="text/html", text="<html></html>")
        )
        result = SiteRequests(session, "myshop", "https://myshop.example.com").text("GET", "/page")
        assert result == "<html></html>"

    def test_default_role_is_navigation(self) -> None:
        session = _RoleAwareSession(_FakeResponse(200, content_type="text/html"))
        SiteRequests(session, "myshop", "https://myshop.example.com").text("GET", "/page")
        assert session.calls[0][0] == "navigation"


class TestRejection:
    @pytest.mark.parametrize("status", [401, 403])
    def test_401_and_403_raise_session_rejected(self, status: int) -> None:
        session = _RoleAwareSession(_FakeResponse(status))
        with pytest.raises(SessionRejectedError) as exc:
            SiteRequests(session, "myshop", "https://myshop.example.com").json("GET", "/orders")
        assert "gp myshop login" in str(exc.value)
        assert str(status) in str(exc.value)

    def test_text_also_raises_on_401(self) -> None:
        session = _RoleAwareSession(_FakeResponse(401, content_type="text/html"))
        with pytest.raises(SessionRejectedError):
            SiteRequests(session, "myshop", "https://myshop.example.com").text("GET", "/page")

    def test_html_endpoint_2xx_is_not_mistaken_for_rejection_by_text(self) -> None:
        session = _RoleAwareSession(_FakeResponse(200, content_type="text/html", text="<p>ok</p>"))
        result = SiteRequests(session, "myshop", "https://myshop.example.com").text("GET", "/page")
        assert result == "<p>ok</p>"

    def test_other_4xx_raises_command_error(self) -> None:
        session = _RoleAwareSession(_FakeResponse(404))
        with pytest.raises(CommandError) as exc:
            SiteRequests(session, "myshop", "https://myshop.example.com").json("GET", "/missing")
        assert not isinstance(exc.value, SessionRejectedError)
        assert "404" in exc.value.user_message

    def test_5xx_raises_command_error(self) -> None:
        session = _RoleAwareSession(_FakeResponse(500))
        with pytest.raises(CommandError):
            SiteRequests(session, "myshop", "https://myshop.example.com").json("GET", "/x")


class TestUnexpectedResponse:
    def test_2xx_non_json_body_raises_unexpected_response(self) -> None:
        session = _RoleAwareSession(_FakeResponse(200, content_type="text/csv", text="a,b\n1,2"))
        with pytest.raises(UnexpectedResponseError) as exc:
            SiteRequests(session, "myshop", "https://myshop.example.com").json("GET", "/export")
        assert "text/csv" in str(exc.value)

    def test_a_truncated_json_body_raises_unexpected_response(self) -> None:
        """A declared content type is not a guarantee: the JSONDecodeError used
        to escape as a traceback."""
        session = _RoleAwareSession(_FakeResponse(200, text='{"orders": [{"id": 1}'))
        with pytest.raises(UnexpectedResponseError) as exc:
            SiteRequests(session, "myshop", "https://myshop.example.com").json("GET", "/orders")
        assert "application/json" in str(exc.value)

    def test_2xx_login_page_raises_session_rejected_not_unexpected_response(self) -> None:
        login_html = '<form><input type="password" name="pw"></form>'
        session = _RoleAwareSession(_FakeResponse(200, content_type="text/html", text=login_html))
        with pytest.raises(SessionRejectedError):
            SiteRequests(session, "myshop", "https://myshop.example.com").json("GET", "/orders")


class _RecordingAdapter(requests.adapters.HTTPAdapter):
    """Answers every request from memory, keeping the PreparedRequest.

    The assertions below are about the bytes `requests` actually builds (the
    query string, the form body), so the test has to go through a real
    `requests.Session` and stop at the transport rather than at a fake session
    object. No socket is opened: `send` never calls up.
    """

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[requests.PreparedRequest] = []

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        self.sent.append(request)
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = b"{}"
        response.url = request.url or ""
        response.request = request
        return response


class TestParamsAndDataNormalisation:
    """A stub declares `keyword_search: bool | None = None` and passes it
    straight through, so the helper owns the spelling (polish round 2,
    2026-09-12)."""

    @staticmethod
    def _session() -> tuple[requests.Session, _RecordingAdapter]:
        session = requests.Session()
        adapter = _RecordingAdapter()
        session.mount("https://", adapter)
        return session, adapter

    def test_a_false_query_parameter_is_sent_as_the_site_spells_it(self) -> None:
        session, adapter = self._session()
        SiteRequests(session, "myshop", "https://myshop.example.com").json(
            "GET", "/results", params={"keywordSearch": False, "searchType": True}
        )
        url = adapter.sent[0].url or ""
        assert "keywordSearch=false" in url
        assert "searchType=true" in url
        assert "True" not in url and "False" not in url

    def test_a_none_query_parameter_is_left_out_entirely(self) -> None:
        session, adapter = self._session()
        SiteRequests(session, "myshop", "https://myshop.example.com").json(
            "GET", "/results", params={"keywordSearch": False, "searchValue": None, "page": 2}
        )
        url = adapter.sent[0].url or ""
        assert "searchValue" not in url
        assert "keywordSearch=false" in url
        assert "page=2" in url

    def test_a_form_body_is_normalised_the_same_way(self) -> None:
        session, adapter = self._session()
        SiteRequests(session, "myshop", "https://myshop.example.com").json(
            "POST", "/results", data={"keywordSearch": False, "searchValue": None, "page": 2}
        )
        body = adapter.sent[0].body
        assert body == "keywordSearch=false&page=2"

    def test_text_normalises_its_query_too(self) -> None:
        session, adapter = self._session()
        SiteRequests(session, "myshop", "https://myshop.example.com").text(
            "GET", "/page", params={"keywordSearch": False, "searchValue": None}
        )
        url = adapter.sent[0].url or ""
        assert "keywordSearch=false" in url
        assert "searchValue" not in url

    def test_a_repeated_parameter_keeps_every_value(self) -> None:
        session, adapter = self._session()
        SiteRequests(session, "myshop", "https://myshop.example.com").json(
            "GET", "/results", params={"tag": ["new", None, True]}
        )
        url = adapter.sent[0].url or ""
        assert "tag=new" in url
        assert "tag=true" in url
        assert url.count("tag=") == 2

    def test_a_raw_string_body_is_passed_through(self) -> None:
        session, adapter = self._session()
        SiteRequests(session, "myshop", "https://myshop.example.com").json(
            "POST", "/results", data="keywordSearch=false"
        )
        assert adapter.sent[0].body == "keywordSearch=false"


class TestPlainSessionHasNoRoles:
    def test_plain_session_gets_the_request_without_role_headers(self) -> None:
        class _PlainSession:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
                self.calls.append((method, url))
                return _FakeResponse(200)

        session = _PlainSession()
        SiteRequests(session, "myshop", "https://myshop.example.com").json("GET", "/orders")
        assert session.calls == [("GET", "https://myshop.example.com/orders")]

    def test_warning_logged_once_per_session_not_once_per_call(self) -> None:
        """Captures through structlog, not stdio.

        `graftpunk.cli.main` points structlog's `PrintLoggerFactory` at
        stderr as an import side effect; whether that import has happened
        yet in a given pytest-xdist worker is not deterministic, so a
        `capsys` assertion on a specific stream flakes. `capture_logs()` is
        the pattern used elsewhere in this suite for the same reason (see
        `tests/unit/test_storage_local.py`, `test_nodriver_backend.py`,
        `test_login_identity.py`). `reset_defaults()` first lifts the
        library's import-time level filter, matching
        `test_storage_local.py`'s precedent, even though this event is
        already at WARNING.
        """

        class _PlainSession:
            def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
                return _FakeResponse(200)

        session = _PlainSession()
        structlog.reset_defaults()
        with capture_logs() as logs:
            policy = SiteRequests(session, "myshop", "https://myshop.example.com")
            policy.json("GET", "/a")
            policy.json("GET", "/b")
            second_policy = SiteRequests(session, "myshop", "https://myshop.example.com")
            second_policy.json("GET", "/c")
        warnings = [event for event in logs if event["event"] == "session_roles_unavailable"]
        assert len(warnings) == 1


class TestCommandContextDelegates:
    def test_request_json_delegates_to_site_requests(self) -> None:
        session = _RoleAwareSession(_FakeResponse(200, text=json.dumps({"ok": True})))
        ctx = CommandContext(
            session=session,  # type: ignore[arg-type]
            plugin_name="myshop",
            command_name="orders",
            api_version=1,
            base_url="https://myshop.example.com",
        )
        assert ctx.request_json("GET", "/orders") == {"ok": True}

    def test_request_text_delegates_to_site_requests(self) -> None:
        session = _RoleAwareSession(_FakeResponse(200, content_type="text/html", text="<p>hi</p>"))
        ctx = CommandContext(
            session=session,  # type: ignore[arg-type]
            plugin_name="myshop",
            command_name="page",
            api_version=1,
            base_url="https://myshop.example.com",
        )
        assert ctx.request_text("GET", "/page") == "<p>hi</p>"

    def test_default_context_session_is_a_graftpunk_session_end_to_end(self) -> None:
        """A real GraftpunkSession wired through request_with_role, not just the fake."""
        session = GraftpunkSession()
        ctx = CommandContext(
            session=session,
            plugin_name="myshop",
            command_name="orders",
            api_version=1,
            base_url="https://myshop.example.com",
        )
        assert hasattr(ctx.session, "request_with_role")


class TestCliOneLineRendering:
    def test_session_rejected_renders_as_one_line_through_the_cli(self) -> None:
        from graftpunk.plugins import SitePlugin, command
        from tests.unit.cli_harness import invoke_plugin_app

        class _RejectingPlugin(SitePlugin):
            site_name = "myshop"
            session_name = "myshop"
            help_text = "test"
            requires_session = False

            @command(help="Fetch orders")
            def orders(self, ctx: CommandContext) -> dict:
                raise SessionRejectedError("myshop", "GET", "/orders", 401)

        result = invoke_plugin_app(_RejectingPlugin(), ["myshop", "orders"])
        assert result.exit_code == 1
        assert "gp myshop login" in result.output
        assert result.output.count("\n") <= 2

    def test_a_truncated_json_body_renders_as_one_line_through_the_cli(self) -> None:
        from graftpunk.plugins import SitePlugin, command
        from tests.unit.cli_harness import invoke_plugin_app

        class _TruncatingPlugin(SitePlugin):
            site_name = "myshop"
            session_name = "myshop"
            help_text = "test"
            requires_session = False

            @command(help="Fetch orders")
            def orders(self, ctx: CommandContext) -> dict:
                session = _RoleAwareSession(_FakeResponse(200, text='{"orders": [{"id": 1}'))
                return SiteRequests(session, "myshop", "https://myshop.example.com").json(
                    "GET", "/orders"
                )

        result = invoke_plugin_app(_TruncatingPlugin(), ["myshop", "orders"])
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "not JSON" in result.output
        assert result.output.count("\n") <= 2
