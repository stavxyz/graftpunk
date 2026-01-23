"""Tests for CLI module."""

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from bsc.cli.main import app
from bsc.exceptions import BSCError, SessionExpiredError, SessionNotFoundError

runner = CliRunner()


class TestVersionCommand:
    """Tests for version command."""

    def test_version_command(self):
        """Test that version command outputs version info."""
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert "BSC" in result.output


class TestListCommand:
    """Tests for list command."""

    @patch("bsc.cli.main.list_sessions_with_metadata")
    def test_list_empty(self, mock_list):
        """Test list command with no sessions."""
        mock_list.return_value = []

        result = runner.invoke(app, ["list"])

        assert result.exit_code == 0
        assert "No Sessions" in result.output or "No sessions" in result.output

    @patch("bsc.cli.main.list_sessions_with_metadata")
    def test_list_with_sessions(self, mock_list):
        """Test list command with sessions."""
        mock_list.return_value = [
            {
                "name": "test-session",
                "domain": "example.com",
                "status": "active",
                "cookie_count": 5,
                "modified_at": "2026-01-01T00:00:00",
            }
        ]

        result = runner.invoke(app, ["list"])

        assert result.exit_code == 0
        assert "test-session" in result.output

    @patch("bsc.cli.main.list_sessions_with_metadata")
    def test_list_json_output(self, mock_list):
        """Test list command with JSON output."""
        mock_list.return_value = [{"name": "test", "domain": "example.com"}]

        result = runner.invoke(app, ["list", "--json"])

        assert result.exit_code == 0
        assert '"name": "test"' in result.output


class TestShowCommand:
    """Tests for show command."""

    @patch("bsc.cli.main.get_session_metadata")
    def test_show_not_found(self, mock_get):
        """Test show command with non-existent session."""
        mock_get.return_value = None

        result = runner.invoke(app, ["show", "non-existent"])

        assert result.exit_code == 1
        assert "not found" in result.output

    @patch("bsc.cli.main.get_session_metadata")
    def test_show_success(self, mock_get):
        """Test show command with existing session."""
        mock_get.return_value = {
            "name": "test-session",
            "domain": "example.com",
            "status": "active",
            "cookie_count": 5,
            "created_at": "2026-01-01T00:00:00",
            "modified_at": "2026-01-01T00:00:00",
            "expires_at": "2026-01-02T00:00:00",
            "cookie_domains": [".example.com"],
        }

        result = runner.invoke(app, ["show", "test-session"])

        assert result.exit_code == 0
        assert "test-session" in result.output
        assert "example.com" in result.output


class TestClearCommand:
    """Tests for clear command."""

    @patch("bsc.cli.main.clear_session_cache")
    def test_clear_specific_session(self, mock_clear):
        """Test clearing a specific session."""
        mock_clear.return_value = ["test-session"]

        result = runner.invoke(app, ["clear", "test-session", "--force"])

        assert result.exit_code == 0
        assert "Removed session" in result.output
        mock_clear.assert_called_once_with("test-session")

    @patch("bsc.cli.main.clear_session_cache")
    def test_clear_not_found(self, mock_clear):
        """Test clearing a session that doesn't exist."""
        mock_clear.return_value = []

        result = runner.invoke(app, ["clear", "non-existent", "--force"])

        assert result.exit_code == 0
        assert "not found" in result.output

    @patch("bsc.cli.main.list_sessions")
    def test_clear_all_empty(self, mock_list):
        """Test clearing all when no sessions exist."""
        mock_list.return_value = []

        result = runner.invoke(app, ["clear", "--force"])

        assert result.exit_code == 0
        assert "No sessions to clear" in result.output


class TestExportCommand:
    """Tests for export command."""

    @patch("bsc.cli.main.load_session")
    def test_export_not_found(self, mock_load):
        """Test export command with non-existent session."""
        mock_load.side_effect = SessionNotFoundError("Session not found")

        result = runner.invoke(app, ["export", "non-existent"])

        assert result.exit_code == 1
        assert "not found" in result.output

    @patch("bsc.cli.main.load_session")
    def test_export_expired(self, mock_load):
        """Test export command with expired session."""
        mock_load.side_effect = SessionExpiredError("Session expired")

        result = runner.invoke(app, ["export", "expired"])

        assert result.exit_code == 1
        assert "expired" in result.output

    @patch("bsc.cli.main.load_session")
    def test_export_bsc_error(self, mock_load):
        """Test export command with BSC error."""
        mock_load.side_effect = BSCError("Some BSC error")

        result = runner.invoke(app, ["export", "broken"])

        assert result.exit_code == 1
        assert "Failed to load" in result.output


class TestKeepaliveCommands:
    """Tests for keepalive subcommands."""

    @patch("bsc.cli.main.read_keepalive_pid")
    def test_keepalive_status_not_running(self, mock_pid):
        """Test keepalive status when not running."""
        mock_pid.return_value = None

        result = runner.invoke(app, ["keepalive", "status"])

        assert result.exit_code == 0
        assert "not running" in result.output

    @patch("bsc.cli.main.read_keepalive_pid")
    @patch("bsc.cli.main.read_keepalive_state")
    def test_keepalive_status_running(self, mock_state, mock_pid):
        """Test keepalive status when running."""
        mock_pid.return_value = 12345
        mock_state.return_value = MagicMock(
            daemon_status=MagicMock(value="running"),
            current_session="test-session",
            interval=25,
            watch=False,
            max_switches=10,
        )

        result = runner.invoke(app, ["keepalive", "status"])

        assert result.exit_code == 0
        assert "12345" in result.output
        assert "running" in result.output

    @patch("bsc.cli.main.read_keepalive_pid")
    def test_keepalive_stop_not_running(self, mock_pid):
        """Test keepalive stop when not running."""
        mock_pid.return_value = None

        result = runner.invoke(app, ["keepalive", "stop"])

        assert result.exit_code == 0
        assert "not running" in result.output


class TestPluginsCommand:
    """Tests for plugins command."""

    @patch("bsc.cli.main.discover_storage_backends")
    @patch("bsc.cli.main.discover_keepalive_handlers")
    @patch("bsc.cli.main.discover_site_plugins")
    def test_plugins_none_installed(self, mock_site, mock_handlers, mock_storage):
        """Test plugins command with no plugins installed."""
        mock_storage.return_value = {}
        mock_handlers.return_value = {}
        mock_site.return_value = {}

        result = runner.invoke(app, ["plugins"])

        assert result.exit_code == 0
        assert "Plugins" in result.output
        assert "none installed" in result.output


class TestConfigCommand:
    """Tests for config command."""

    @patch("bsc.cli.main.get_settings")
    def test_config_command(self, mock_settings):
        """Test config command output."""
        mock_settings.return_value = MagicMock(
            config_dir="/home/user/.config/bsc",
            sessions_dir="/home/user/.config/bsc/sessions",
            storage_backend="local",
            session_ttl_hours=720,
            log_level="INFO",
        )

        result = runner.invoke(app, ["config"])

        assert result.exit_code == 0
        assert "Configuration" in result.output
        assert "local" in result.output
