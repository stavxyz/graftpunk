"""Tests for graftpunk.cli.http_commands."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest
import requests

from graftpunk.cli.http_commands import (
    _make_request,
    _print_response,
    _resolve_json_body,
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
    def test_no_browser_headers_clears_profiles(self, mock_load: MagicMock) -> None:
        mock_session = MagicMock(spec=requests.Session)
        mock_session.headers = {}
        mock_session._gp_header_profiles = {"navigation": {"User-Agent": "Test"}}
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

        # Header profiles should have been cleared
        assert mock_session._gp_header_profiles == {}

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
    def test_no_session_exits_with_error(self, mock_resolve: MagicMock) -> None:
        """Exits with error when no session name, no env var, no default."""
        import typer

        with pytest.raises(typer.Exit) as exc_info:
            _make_request("GET", "https://example.com")

        assert exc_info.value.exit_code == 1

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


def test_default_browser_headers_removed() -> None:
    """DEFAULT_BROWSER_HEADERS should no longer exist."""
    import graftpunk.cli.http_commands as mod

    assert not hasattr(mod, "DEFAULT_BROWSER_HEADERS")
