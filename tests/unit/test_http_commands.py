"""Tests for graftpunk.cli.http_commands."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest
import requests

from graftpunk.cli.http_commands import (
    _ROLE_ALIASES,
    _dispatch_request,
    _make_request,
    _print_response,
    _resolve_json_body,
    _resolve_role_name,
    _save_observe_data,
)


class TestResolveJsonBody:
    """Tests for _resolve_json_body."""

    def test_inline_json(self) -> None:
        result = _resolve_json_body('{"key": "value"}')
        assert result == '{"key": "value"}'

    def test_from_file(self, tmp_path: pytest.TempPathFactory) -> None:
        json_file = tmp_path / "data.json"  # type: ignore[operator]
        json_file.write_text('{"from": "file"}')
        result = _resolve_json_body(f"@{json_file}")
        assert result == '{"from": "file"}'

    def test_from_stdin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO('{"from": "stdin"}'))
        result = _resolve_json_body("@-")
        assert result == '{"from": "stdin"}'

    def test_missing_file(self) -> None:
        import typer

        with pytest.raises(typer.BadParameter, match="File not found"):
            _resolve_json_body("@/nonexistent/file.json")


class TestMakeRequest:
    """Tests for _make_request."""

    @patch("graftpunk.cli.http_commands.load_session_for_api")
    @patch("graftpunk.cli.plugin_commands._registered_plugins_for_teardown", [])
    @patch("graftpunk.cli.plugin_commands._plugin_session_map", {})
    def test_get_basic(self, mock_load: MagicMock) -> None:
        mock_session = MagicMock(spec=requests.Session)
        mock_session.headers = {}
        mock_response = MagicMock(spec=requests.Response)
        mock_response.status_code = 200
        mock_session.request.return_value = mock_response
        mock_load.return_value = mock_session

        response = _make_request("GET", "https://example.com", session_name="test-session")

        mock_load.assert_called_once_with("test-session")
        assert response == mock_response

    @patch("graftpunk.cli.http_commands.load_session_for_api")
    @patch("graftpunk.cli.plugin_commands._registered_plugins_for_teardown", [])
    @patch("graftpunk.cli.plugin_commands._plugin_session_map", {})
    def test_post_with_json_body(self, mock_load: MagicMock) -> None:
        mock_session = MagicMock(spec=requests.Session)
        mock_session.headers = {}
        mock_response = MagicMock(spec=requests.Response)
        mock_response.status_code = 200
        mock_session.request.return_value = mock_response
        mock_load.return_value = mock_session

        response = _make_request(
            "POST",
            "https://example.com/api",
            session_name="test-session",
            json_body='{"key": "value"}',
        )

        assert response == mock_response
        mock_session.request.assert_called_once()
        call_kwargs = mock_session.request.call_args
        assert call_kwargs[1]["data"] == '{"key": "value"}'
        assert mock_session.headers["Content-Type"] == "application/json"

    @patch("graftpunk.cli.http_commands.load_session_for_api")
    @patch("graftpunk.cli.plugin_commands._registered_plugins_for_teardown", [])
    @patch("graftpunk.cli.plugin_commands._plugin_session_map", {})
    def test_no_browser_headers_clears_roles(self, mock_load: MagicMock) -> None:
        mock_session = MagicMock(spec=requests.Session)
        mock_session.headers = {}
        mock_session.clear_header_roles = MagicMock()
        mock_response = MagicMock(spec=requests.Response)
        mock_response.status_code = 200
        mock_session.request.return_value = mock_response
        mock_load.return_value = mock_session

        _make_request(
            "GET",
            "https://example.com",
            session_name="test-session",
            browser_headers=False,
        )

        # Header roles should have been cleared via public method
        mock_session.clear_header_roles.assert_called_once()

    @patch("graftpunk.cli.http_commands.load_session_for_api")
    @patch("graftpunk.cli.plugin_commands._registered_plugins_for_teardown", [])
    @patch("graftpunk.cli.plugin_commands._plugin_session_map", {})
    def test_extra_headers(self, mock_load: MagicMock) -> None:
        mock_session = MagicMock(spec=requests.Session)
        mock_session.headers = {}
        mock_response = MagicMock(spec=requests.Response)
        mock_response.status_code = 200
        mock_session.request.return_value = mock_response
        mock_load.return_value = mock_session

        _make_request(
            "GET",
            "https://example.com",
            session_name="test-session",
            extra_headers=["X-Custom: my-value", "Authorization: Bearer abc"],
        )

        assert mock_session.headers["X-Custom"] == "my-value"
        assert mock_session.headers["Authorization"] == "Bearer abc"

    @patch("graftpunk.cli.http_commands.load_session_for_api")
    @patch("graftpunk.tokens.clear_cached_tokens")
    @patch("graftpunk.tokens.prepare_session")
    def test_token_refresh_on_403(
        self, mock_prepare: MagicMock, mock_clear: MagicMock, mock_load: MagicMock
    ) -> None:
        mock_session = MagicMock(spec=requests.Session)
        mock_session.headers = {}
        mock_session.site_name = "test-plugin"

        # First response is 403, second is 200
        mock_403 = MagicMock(spec=requests.Response)
        mock_403.status_code = 403
        mock_200 = MagicMock(spec=requests.Response)
        mock_200.status_code = 200
        mock_session.request.side_effect = [mock_403, mock_200]
        mock_load.return_value = mock_session

        # Create a mock plugin with token_config
        mock_plugin = MagicMock()
        mock_plugin.site_name = "test-plugin"
        mock_plugin.base_url = "https://example.com"
        mock_token_config = MagicMock()
        mock_plugin.token_config = mock_token_config

        with (
            patch(
                "graftpunk.cli.plugin_commands._plugin_session_map",
                {"test-plugin": "test-session"},
            ),
            patch(
                "graftpunk.cli.plugin_commands._registered_plugins_for_teardown",
                [mock_plugin],
            ),
        ):
            response = _make_request(
                "GET",
                "https://example.com/api",
                session_name="test-session",
            )

        assert response == mock_200
        mock_clear.assert_called_once_with(mock_session)
        # prepare_session called twice: initial + retry
        assert mock_prepare.call_count == 2


class TestMakeRequestErrorPaths:
    """Tests for _make_request error/guard paths."""

    @patch("graftpunk.cli.http_commands.resolve_session", return_value=None)
    def test_no_session_without_flag_exits(self, mock_resolve: MagicMock) -> None:
        """Exits with error when no session can be resolved and --no-session not set."""
        import typer

        with pytest.raises(typer.Exit) as exc_info:
            _make_request("GET", "https://example.com")

        assert exc_info.value.exit_code == 1

    @patch("graftpunk.cli.plugin_commands._registered_plugins_for_teardown", [])
    @patch("graftpunk.cli.plugin_commands._plugin_session_map", {})
    def test_no_session_flag_uses_bare_session(self) -> None:
        """With no_session=True, uses a bare requests.Session."""
        mock_response = MagicMock(spec=requests.Response)
        mock_response.status_code = 200

        with patch.object(requests.Session, "request", return_value=mock_response) as mock_req:
            response = _make_request("GET", "https://example.com", no_session=True)

        assert response == mock_response
        mock_req.assert_called_once()

    def test_no_session_flag_skips_token_injection(self) -> None:
        """With no_session=True, token injection is skipped entirely."""
        mock_response = MagicMock(spec=requests.Response)
        mock_response.status_code = 200

        with (
            patch.object(requests.Session, "request", return_value=mock_response),
            patch("graftpunk.cli.plugin_commands.get_plugin_for_session") as mock_get_plugin,
        ):
            _make_request("GET", "https://example.com", no_session=True)

        mock_get_plugin.assert_not_called()

    @patch("graftpunk.cli.http_commands.load_session_for_api")
    @patch("graftpunk.cli.plugin_commands._registered_plugins_for_teardown", [])
    @patch("graftpunk.cli.plugin_commands._plugin_session_map", {})
    def test_invalid_header_format_exits_with_error(self, mock_load: MagicMock) -> None:
        """Exits with error when header has no colon separator."""
        import typer

        mock_session = MagicMock(spec=requests.Session)
        mock_session.headers = {}
        mock_load.return_value = mock_session

        with pytest.raises(typer.Exit) as exc_info:
            _make_request(
                "GET",
                "https://example.com",
                session_name="test-session",
                extra_headers=["InvalidHeaderNoColon"],
            )

        assert exc_info.value.exit_code == 1

    @patch("graftpunk.cli.http_commands.load_session_for_api")
    def test_session_load_failure_exits_with_error(self, mock_load: MagicMock) -> None:
        """Exits with error when session loading raises an exception."""
        import typer

        mock_load.side_effect = FileNotFoundError("Session file not found")

        with pytest.raises(typer.Exit) as exc_info:
            _make_request("GET", "https://example.com", session_name="nonexistent")

        assert exc_info.value.exit_code == 1
        mock_load.assert_called_once_with("nonexistent")


class TestSaveObserveData:
    """Tests for _save_observe_data."""

    def test_saves_har_entry(self, tmp_path: pytest.TempPathFactory) -> None:
        mock_response = MagicMock(spec=requests.Response)
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.content = b'{"ok": true}'
        mock_response.text = '{"ok": true}'
        mock_response.elapsed = MagicMock()
        mock_response.elapsed.total_seconds.return_value = 0.5
        mock_response.request = MagicMock()
        mock_response.request.headers = {"User-Agent": "test"}

        with patch("graftpunk.cli.http_commands.OBSERVE_BASE_DIR", tmp_path):
            result = _save_observe_data("test-session", "GET", "https://example.com", mock_response)

        assert result is not None
        assert result.run_dir.exists()

    def test_includes_request_body(self, tmp_path: pytest.TempPathFactory) -> None:
        mock_response = MagicMock(spec=requests.Response)
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.content = b'{"created": true}'
        mock_response.text = '{"created": true}'
        mock_response.elapsed = MagicMock()
        mock_response.elapsed.total_seconds.return_value = 0.3
        mock_response.request = MagicMock()
        mock_response.request.headers = {
            "User-Agent": "test",
            "Content-Type": "application/json",
        }

        with patch("graftpunk.cli.http_commands.OBSERVE_BASE_DIR", tmp_path):
            result = _save_observe_data(
                "test-session",
                "POST",
                "https://example.com/api",
                mock_response,
                request_body='{"name": "test"}',
            )

        assert result is not None
        # Read the HAR file and verify postData is present
        import json

        har_path = result.run_dir / "network.har"
        har_data = json.loads(har_path.read_text())
        entry = har_data["log"]["entries"][0]
        assert "postData" in entry["request"]
        assert entry["request"]["postData"]["text"] == '{"name": "test"}'


class TestPrintResponse:
    """Tests for _print_response."""

    def test_body_only(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_response = MagicMock(spec=requests.Response)
        mock_response.text = '{"data": "value"}'

        _print_response(mock_response, body_only=True)

        captured = capsys.readouterr()
        assert captured.out == '{"data": "value"}'

    def test_verbose(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_response = MagicMock(spec=requests.Response)
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.ok = True
        mock_response.text = "response body"
        mock_response.elapsed = MagicMock()
        mock_response.elapsed.total_seconds.return_value = 0.1
        mock_response.request = MagicMock()
        mock_response.request.method = "GET"
        mock_response.request.url = "https://example.com"
        mock_response.request.headers = {"User-Agent": "test"}
        mock_response.headers = {"Content-Type": "text/plain"}

        _print_response(mock_response, verbose=True)

        captured = capsys.readouterr()
        assert "response body" in captured.out

    def test_default_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_response = MagicMock(spec=requests.Response)
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.ok = True
        mock_response.text = "hello"
        mock_response.elapsed = MagicMock()
        mock_response.elapsed.total_seconds.return_value = 0.2

        _print_response(mock_response)

        captured = capsys.readouterr()
        assert "hello" in captured.out


class TestHttpCommandCLI:
    """CLI-level tests for gp http commands with --no-session flag."""

    def test_http_get_no_session_flag_proceeds(self) -> None:
        """gp http get --no-session URL should make an unauthenticated request."""
        from typer.testing import CliRunner

        from graftpunk.cli.main import app

        runner = CliRunner()
        mock_response = MagicMock(spec=requests.Response)
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.ok = True
        mock_response.text = "ok"
        mock_response.elapsed = MagicMock()
        mock_response.elapsed.total_seconds.return_value = 0.1
        mock_response.request = MagicMock()
        mock_response.request.headers = {}
        mock_response.headers = {}
        mock_response.content = b"ok"

        with (
            patch.object(requests.Session, "request", return_value=mock_response),
            patch("graftpunk.cli.http_commands.OBSERVE_BASE_DIR", "/tmp/test-observe"),  # noqa: S108
            patch("graftpunk.cli.http_commands._save_observe_data"),
        ):
            result = runner.invoke(app, ["http", "get", "--no-session", "https://example.com"])

        assert result.exit_code == 0

    def test_http_get_session_and_no_session_conflict(self) -> None:
        """gp http get --session X --no-session URL should fail with conflict error."""
        import re

        from typer.testing import CliRunner

        from graftpunk.cli.main import app

        runner = CliRunner()
        result = runner.invoke(
            app,
            ["http", "get", "--session", "mysite", "--no-session", "https://example.com"],
        )
        assert result.exit_code == 1
        output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "Cannot use --session and --no-session" in output

    def test_http_get_no_session_flag_in_help(self) -> None:
        """--no-session flag should appear in gp http get --help."""
        import re

        from typer.testing import CliRunner

        from graftpunk.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["http", "get", "--help"])
        assert result.exit_code == 0
        output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "--no-session" in output


class TestDispatchRequest:
    """Tests for _dispatch_request."""

    def test_no_role_uses_session_request(self) -> None:
        mock_session = MagicMock()
        mock_response = MagicMock(spec=requests.Response)
        mock_session.request.return_value = mock_response

        result = _dispatch_request(mock_session, "GET", "https://example.com", timeout=30)

        mock_session.request.assert_called_once_with("GET", "https://example.com", timeout=30)
        assert result == mock_response

    def test_xhr_role_calls_request_with_role(self) -> None:
        mock_session = MagicMock()
        mock_response = MagicMock(spec=requests.Response)
        mock_session.request_with_role.return_value = mock_response

        result = _dispatch_request(
            mock_session, "GET", "https://example.com/api", role="xhr", timeout=30
        )

        mock_session.request_with_role.assert_called_once_with(
            "xhr", "GET", "https://example.com/api", timeout=30
        )
        mock_session.request.assert_not_called()
        assert result == mock_response

    def test_navigate_alias_resolves_to_navigation(self) -> None:
        mock_session = MagicMock()
        mock_response = MagicMock(spec=requests.Response)
        mock_session.request_with_role.return_value = mock_response

        _dispatch_request(
            mock_session, "GET", "https://example.com/page", role="navigate", timeout=30
        )

        mock_session.request_with_role.assert_called_once_with(
            "navigation", "GET", "https://example.com/page", timeout=30
        )

    def test_form_role_calls_request_with_role(self) -> None:
        mock_session = MagicMock()
        mock_response = MagicMock(spec=requests.Response)
        mock_session.request_with_role.return_value = mock_response

        result = _dispatch_request(
            mock_session, "POST", "https://example.com/submit", role="form", timeout=30
        )

        mock_session.request_with_role.assert_called_once_with(
            "form", "POST", "https://example.com/submit", timeout=30
        )
        assert result == mock_response

    def test_custom_role_passes_through(self) -> None:
        """Custom role names are passed directly to request_with_role."""
        mock_session = MagicMock()
        mock_response = MagicMock(spec=requests.Response)
        mock_session.request_with_role.return_value = mock_response

        result = _dispatch_request(
            mock_session, "GET", "https://example.com/api", role="my-api", timeout=30
        )

        mock_session.request_with_role.assert_called_once_with(
            "my-api", "GET", "https://example.com/api", timeout=30
        )
        assert result == mock_response

    def test_role_without_support_raises_value_error(self) -> None:
        """Session without request_with_role raises ValueError."""
        mock_session = MagicMock(spec=requests.Session)

        with pytest.raises(ValueError, match="--role 'xhr' requires a GraftpunkSession"):
            _dispatch_request(mock_session, "GET", "https://example.com", role="xhr", timeout=30)


class TestMakeRequestWithRole:
    """Tests for _make_request with --role flag."""

    @patch("graftpunk.cli.http_commands.load_session_for_api")
    @patch("graftpunk.cli.plugin_commands._registered_plugins_for_teardown", [])
    @patch("graftpunk.cli.plugin_commands._plugin_session_map", {})
    def test_role_xhr_dispatches_via_request_with_role(self, mock_load: MagicMock) -> None:
        mock_session = MagicMock()
        mock_session.headers = {}
        mock_response = MagicMock(spec=requests.Response)
        mock_response.status_code = 200
        mock_session.request_with_role.return_value = mock_response
        mock_load.return_value = mock_session

        response = _make_request(
            "GET",
            "https://example.com/api",
            session_name="test-session",
            role="xhr",
        )

        mock_session.request_with_role.assert_called_once()
        mock_session.request.assert_not_called()
        assert response == mock_response

    @patch("graftpunk.cli.http_commands.load_session_for_api")
    @patch("graftpunk.cli.plugin_commands._registered_plugins_for_teardown", [])
    @patch("graftpunk.cli.plugin_commands._plugin_session_map", {})
    def test_role_none_uses_session_request(self, mock_load: MagicMock) -> None:
        mock_session = MagicMock()
        mock_session.headers = {}
        mock_response = MagicMock(spec=requests.Response)
        mock_response.status_code = 200
        mock_session.request.return_value = mock_response
        mock_load.return_value = mock_session

        response = _make_request(
            "GET",
            "https://example.com",
            session_name="test-session",
        )

        mock_session.request.assert_called_once()
        assert response == mock_response

    @patch("graftpunk.cli.http_commands.load_session_for_api")
    def test_plugin_header_roles_merged_into_session(self, mock_load: MagicMock) -> None:
        """Plugin's header_roles dict is merged via merge_header_roles()."""
        mock_session = MagicMock()
        mock_session.headers = {}
        mock_session._gp_header_roles = {"xhr": {"Accept": "application/json"}}
        mock_session.merge_header_roles = MagicMock()
        mock_response = MagicMock(spec=requests.Response)
        mock_response.status_code = 200
        mock_session.request_with_role.return_value = mock_response
        mock_load.return_value = mock_session

        mock_plugin = MagicMock()
        mock_plugin.site_name = "test-plugin"
        mock_plugin.base_url = "https://example.com"
        mock_plugin.token_config = None
        mock_plugin.header_roles = {
            "api": {"Accept": "application/json", "X-API-Version": "2"},
        }

        with (
            patch(
                "graftpunk.cli.plugin_commands._plugin_session_map",
                {"test-plugin": "test-session"},
            ),
            patch(
                "graftpunk.cli.plugin_commands._registered_plugins_for_teardown",
                [mock_plugin],
            ),
        ):
            _make_request(
                "GET",
                "https://example.com/api",
                session_name="test-session",
                role="api",
            )

        # Plugin's roles should have been merged via public method
        mock_session.merge_header_roles.assert_called_once_with(
            {"api": {"Accept": "application/json", "X-API-Version": "2"}}
        )


class TestRoleCLI:
    """CLI-level tests for --role flag."""

    def test_role_flag_in_help(self) -> None:
        import re

        from typer.testing import CliRunner

        from graftpunk.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["http", "get", "--help"])
        assert result.exit_code == 0
        output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "--role" in output

    def test_role_with_no_session_exits_with_error(self) -> None:
        """--role requires a session (GraftpunkSession)."""
        import re

        from typer.testing import CliRunner

        from graftpunk.cli.main import app

        runner = CliRunner()
        result = runner.invoke(
            app,
            ["http", "get", "--no-session", "--role", "xhr", "https://example.com"],
        )
        assert result.exit_code == 1
        output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "--role requires a session" in output

    def test_role_xhr_via_cli(self) -> None:
        from typer.testing import CliRunner

        from graftpunk.cli.main import app

        runner = CliRunner()
        mock_response = MagicMock(spec=requests.Response)
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.ok = True
        mock_response.text = '{"status": "ok"}'
        mock_response.elapsed = MagicMock()
        mock_response.elapsed.total_seconds.return_value = 0.1
        mock_response.request = MagicMock()
        mock_response.request.headers = {}
        mock_response.headers = {}
        mock_response.content = b'{"status": "ok"}'

        mock_session = MagicMock()
        mock_session.headers = {}
        mock_session.request_with_role.return_value = mock_response

        with (
            patch("graftpunk.cli.http_commands.load_session_for_api", return_value=mock_session),
            patch("graftpunk.cli.http_commands._save_observe_data"),
            patch("graftpunk.cli.plugin_commands._registered_plugins_for_teardown", []),
            patch("graftpunk.cli.plugin_commands._plugin_session_map", {}),
        ):
            result = runner.invoke(
                app,
                [
                    "http",
                    "get",
                    "--session",
                    "test-session",
                    "--role",
                    "xhr",
                    "https://example.com/api",
                ],
            )

        assert result.exit_code == 0
        mock_session.request_with_role.assert_called_once()


class TestResolveRoleName:
    """Tests for _resolve_role_name and _ROLE_ALIASES."""

    def test_navigate_resolves_to_navigation(self) -> None:
        assert _resolve_role_name("navigate") == "navigation"

    def test_xhr_passes_through(self) -> None:
        assert _resolve_role_name("xhr") == "xhr"

    def test_form_passes_through(self) -> None:
        assert _resolve_role_name("form") == "form"

    def test_custom_name_passes_through(self) -> None:
        assert _resolve_role_name("my-custom-api") == "my-custom-api"

    def test_aliases_only_contains_navigate(self) -> None:
        assert _ROLE_ALIASES == {"navigate": "navigation"}


class TestRoleHelpText:
    """Tests for _role_help_text."""

    def test_includes_registered_roles(self) -> None:
        from graftpunk.cli.http_commands import _role_help_text

        text = _role_help_text()
        assert "navigation" in text
        assert "xhr" in text
        assert "form" in text
        assert "plugin-defined" in text

    def test_reflects_custom_registered_role(self) -> None:
        from graftpunk.cli.http_commands import _role_help_text
        from graftpunk.graftpunk_session import _ROLE_REGISTRY, register_role

        saved = dict(_ROLE_REGISTRY)
        try:
            register_role("custom-api", {"Accept": "application/json"})
            text = _role_help_text()
            assert "custom-api" in text
        finally:
            _ROLE_REGISTRY.clear()
            _ROLE_REGISTRY.update(saved)


class TestTokenRetryWithRole:
    """Tests for 403 token retry when --role is used."""

    @patch("graftpunk.cli.http_commands.load_session_for_api")
    @patch("graftpunk.tokens.clear_cached_tokens")
    @patch("graftpunk.tokens.prepare_session")
    def test_403_retry_preserves_role(
        self, mock_prepare: MagicMock, mock_clear: MagicMock, mock_load: MagicMock
    ) -> None:
        """When a 403 triggers token retry, the retry uses the same role."""
        mock_session = MagicMock()
        mock_session.headers = {}
        mock_session.request_with_role = MagicMock()

        mock_403 = MagicMock(spec=requests.Response)
        mock_403.status_code = 403
        mock_200 = MagicMock(spec=requests.Response)
        mock_200.status_code = 200
        mock_session.request_with_role.side_effect = [mock_403, mock_200]
        mock_load.return_value = mock_session

        mock_plugin = MagicMock()
        mock_plugin.site_name = "test-plugin"
        mock_plugin.base_url = "https://example.com"
        mock_plugin.token_config = MagicMock()
        mock_plugin.header_roles = None

        with (
            patch(
                "graftpunk.cli.plugin_commands._plugin_session_map",
                {"test-plugin": "test-session"},
            ),
            patch(
                "graftpunk.cli.plugin_commands._registered_plugins_for_teardown",
                [mock_plugin],
            ),
        ):
            response = _make_request(
                "GET",
                "https://example.com/api",
                session_name="test-session",
                role="xhr",
            )

        assert response == mock_200
        # Both initial and retry calls should use request_with_role
        assert mock_session.request_with_role.call_count == 2
        for call in mock_session.request_with_role.call_args_list:
            assert call[0][0] == "xhr"


def test_default_browser_headers_removed() -> None:
    """DEFAULT_BROWSER_HEADERS should no longer exist."""
    import graftpunk.cli.http_commands as mod

    assert not hasattr(mod, "DEFAULT_BROWSER_HEADERS")
