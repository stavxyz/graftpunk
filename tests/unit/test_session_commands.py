"""Tests for session command Backend/Location display."""

import json
import re
from unittest.mock import patch

from typer.testing import CliRunner

from graftpunk.cli.session_commands import session_app
from graftpunk.exceptions import StorageError

runner = CliRunner()


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _make_session(
    *,
    name: str = "mysite",
    domain: str = "example.com",
    status: str = "active",
    cookie_count: int = 5,
    modified_at: str = "2026-02-11T12:00:00",
    created_at: str = "2026-02-11T10:00:00",
    expires_at: str = "2026-03-11T10:00:00",
    storage_backend: str = "local",
    storage_location: str = "~/.config/graftpunk/sessions",
    cookie_domains: list[str] | None = None,
) -> dict:
    """Build a session metadata dict with sensible defaults."""
    return {
        "name": name,
        "domain": domain,
        "status": status,
        "cookie_count": cookie_count,
        "modified_at": modified_at,
        "created_at": created_at,
        "expires_at": expires_at,
        "storage_backend": storage_backend,
        "storage_location": storage_location,
        "cookie_domains": cookie_domains or [],
    }


class TestSessionListDisplay:
    """Tests for Backend/Location columns in session list output."""

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_list_shows_backend_column(self, mock_list) -> None:
        """Backend column header and value appear in table output."""
        mock_list.return_value = [_make_session(storage_backend="local")]

        result = runner.invoke(session_app, ["list"])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "Backend" in output
        assert "local" in output

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_list_shows_location_column(self, mock_list) -> None:
        """Location column header and value appear in table output."""
        mock_list.return_value = [
            _make_session(storage_location="s3://b"),
        ]

        result = runner.invoke(session_app, ["list"])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "Location" in output
        assert "s3://b" in output

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_list_missing_storage_fields_shows_dash(self, mock_list) -> None:
        """Empty storage fields render as em-dash in table output."""
        mock_list.return_value = [
            _make_session(storage_backend="", storage_location=""),
        ]

        result = runner.invoke(session_app, ["list"])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        # The em-dash character should appear for both missing fields
        assert "\u2014" in output

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_list_json_includes_storage_fields(self, mock_list) -> None:
        """JSON output includes storage_backend and storage_location keys."""
        mock_list.return_value = [
            _make_session(storage_backend="s3", storage_location="s3://my-bucket"),
        ]

        result = runner.invoke(session_app, ["list", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["storage_backend"] == "s3"
        assert data[0]["storage_location"] == "s3://my-bucket"


class TestSessionShowDisplay:
    """Tests for Backend/Location fields in session show output."""

    @patch("graftpunk.cli.session_commands.get_session_metadata")
    @patch("graftpunk.cli.session_commands.resolve_session_name", side_effect=lambda n: n)
    def test_show_includes_backend_and_location(self, _mock_resolve, mock_get) -> None:
        """Show panel includes backend and location values."""
        mock_get.return_value = _make_session(
            storage_backend="s3",
            storage_location="s3://my-bucket",
        )

        result = runner.invoke(session_app, ["show", "mysite"])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "Backend" in output
        assert "s3" in output
        assert "Location" in output
        assert "s3://my-bucket" in output

    @patch("graftpunk.cli.session_commands.get_session_metadata")
    @patch("graftpunk.cli.session_commands.resolve_session_name", side_effect=lambda n: n)
    def test_show_empty_storage_fields_shows_dash(self, _mock_resolve, mock_get) -> None:
        """Empty storage fields render as em-dash in show output."""
        mock_get.return_value = _make_session(
            storage_backend="",
            storage_location="",
        )

        result = runner.invoke(session_app, ["show", "mysite"])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        # Count em-dashes: domain has value so we expect dashes for backend and location
        # Plus the existing "—" for other empty fields. Just verify em-dash is present.
        assert "\u2014" in output

    @patch("graftpunk.cli.session_commands.get_session_metadata")
    @patch("graftpunk.cli.session_commands.resolve_session_name", side_effect=lambda n: n)
    def test_show_json_includes_storage_fields(self, _mock_resolve, mock_get) -> None:
        """JSON output includes storage_backend and storage_location keys."""
        mock_get.return_value = _make_session(
            storage_backend="supabase",
            storage_location="supabase://project.supabase.co",
        )

        result = runner.invoke(session_app, ["show", "mysite", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["storage_backend"] == "supabase"
        assert data["storage_location"] == "supabase://project.supabase.co"


class TestStorageBackendFlag:
    """Tests for --storage-backend flag via Typer callback."""

    def test_bare_session_shows_help(self) -> None:
        """Invoking session with no args shows usage help and exits 0."""
        result = runner.invoke(session_app, [])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "Usage" in output or "session" in output

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_flag_passes_override(self, mock_list) -> None:
        """--storage-backend value is forwarded to backend-using commands."""
        mock_list.return_value = []

        result = runner.invoke(session_app, ["--storage-backend", "s3", "list"])

        assert result.exit_code == 0
        mock_list.assert_called_once_with(backend_override="s3")

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_flag_with_missing_credentials_shows_error(self, mock_list) -> None:
        """ValueError from missing credentials produces friendly error."""
        mock_list.side_effect = ValueError("Missing GRAFTPUNK_S3_BUCKET")

        result = runner.invoke(session_app, ["--storage-backend", "s3", "list"])

        assert result.exit_code == 1
        output = strip_ansi(result.output)
        assert "Storage backend error" in output
        assert "Missing GRAFTPUNK_S3_BUCKET" in output

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_list_storage_error_shows_friendly_message(self, mock_list) -> None:
        """StorageError from backend produces friendly error, not a traceback."""
        mock_list.side_effect = StorageError("S3 connection refused")

        result = runner.invoke(session_app, ["--storage-backend", "s3", "list"])

        assert result.exit_code == 1
        output = strip_ansi(result.output)
        assert "Storage backend error" in output
        assert "S3 connection refused" in output


class TestShowErrorHandling:
    """Tests for error handling in session show command."""

    @patch("graftpunk.cli.session_commands.get_session_metadata")
    @patch("graftpunk.cli.session_commands.resolve_session_name", side_effect=lambda n: n)
    def test_show_value_error_shows_friendly_message(self, _mock_resolve, mock_get) -> None:
        """ValueError from backend produces friendly error."""
        mock_get.side_effect = ValueError("Missing GRAFTPUNK_S3_BUCKET")

        result = runner.invoke(session_app, ["--storage-backend", "s3", "show", "mysite"])

        assert result.exit_code == 1
        output = strip_ansi(result.output)
        assert "Storage backend error" in output

    @patch("graftpunk.cli.session_commands.get_session_metadata")
    @patch("graftpunk.cli.session_commands.resolve_session_name", side_effect=lambda n: n)
    def test_show_storage_error_shows_friendly_message(self, _mock_resolve, mock_get) -> None:
        """StorageError from backend produces friendly error."""
        mock_get.side_effect = StorageError("S3 timeout")

        result = runner.invoke(session_app, ["--storage-backend", "s3", "show", "mysite"])

        assert result.exit_code == 1
        output = strip_ansi(result.output)
        assert "Storage backend error" in output
        assert "S3 timeout" in output


class TestClearErrorHandling:
    """Tests for error handling in session clear command."""

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_clear_value_error_shows_friendly_message(self, mock_list) -> None:
        """ValueError from backend produces friendly error."""
        mock_list.side_effect = ValueError("Missing GRAFTPUNK_S3_BUCKET")

        result = runner.invoke(session_app, ["--storage-backend", "s3", "clear", "--all"])

        assert result.exit_code == 1
        output = strip_ansi(result.output)
        assert "Storage backend error" in output

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_clear_storage_error_shows_friendly_message(self, mock_list) -> None:
        """StorageError from backend produces friendly error."""
        mock_list.side_effect = StorageError("S3 connection refused")

        result = runner.invoke(session_app, ["--storage-backend", "s3", "clear", "--all"])

        assert result.exit_code == 1
        output = strip_ansi(result.output)
        assert "Storage backend error" in output
        assert "S3 connection refused" in output


class TestJsonOutputIsMachineReadable:
    """--json must be valid JSON regardless of terminal width (#145 sibling)."""

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_list_json_long_values_are_not_wrapped(self, mock_list) -> None:
        long_value = "https://example.com/" + "p" * 300
        mock_list.return_value = [_make_session(name="mysite", storage_location=long_value)]
        result = runner.invoke(session_app, ["list", "--json"], env={"COLUMNS": "80"})
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)[0]["storage_location"] == long_value

    @patch("graftpunk.cli.session_commands.get_session_metadata")
    @patch("graftpunk.cli.session_commands.resolve_session_name", side_effect=lambda n: n)
    def test_show_json_long_values_are_not_wrapped(self, _mock_resolve, mock_get) -> None:
        long_value = "https://example.com/" + "p" * 300
        mock_get.return_value = _make_session(name="mysite", storage_location=long_value)
        result = runner.invoke(session_app, ["show", "mysite", "--json"], env={"COLUMNS": "80"})
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["storage_location"] == long_value
