"""Tests for CLI module."""

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from graftpunk.cli.main import app
from graftpunk.exceptions import GraftpunkError, SessionExpiredError, SessionNotFoundError

runner = CliRunner()


class TestVersionCommand:
    """Tests for version command."""

    def test_version_command(self):
        """Test that version command outputs version info."""
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert "graftpunk" in result.output


class TestListCommand:
    """Tests for list command."""

    @patch("graftpunk.cli.main.list_sessions_with_metadata")
    def test_list_empty(self, mock_list):
        """Test list command with no sessions."""
        mock_list.return_value = []

        result = runner.invoke(app, ["list"])

        assert result.exit_code == 0
        assert "No Sessions" in result.output or "No sessions" in result.output

    @patch("graftpunk.cli.main.list_sessions_with_metadata")
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

    @patch("graftpunk.cli.main.list_sessions_with_metadata")
    def test_list_json_output(self, mock_list):
        """Test list command with JSON output."""
        mock_list.return_value = [{"name": "test", "domain": "example.com"}]

        result = runner.invoke(app, ["list", "--json"])

        assert result.exit_code == 0
        assert '"name": "test"' in result.output


class TestShowCommand:
    """Tests for show command."""

    @patch("graftpunk.cli.main.get_session_metadata")
    def test_show_not_found(self, mock_get):
        """Test show command with non-existent session."""
        mock_get.return_value = None

        result = runner.invoke(app, ["show", "non-existent"])

        assert result.exit_code == 1
        assert "not found" in result.output

    @patch("graftpunk.cli.main.get_session_metadata")
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

    @patch("graftpunk.cli.main.clear_session_cache")
    def test_clear_specific_session(self, mock_clear):
        """Test clearing a specific session."""
        mock_clear.return_value = ["test-session"]

        result = runner.invoke(app, ["clear", "test-session", "--force"])

        assert result.exit_code == 0
        assert "Removed session" in result.output
        mock_clear.assert_called_once_with("test-session")

    @patch("graftpunk.cli.main.clear_session_cache")
    def test_clear_not_found(self, mock_clear):
        """Test clearing a session that doesn't exist."""
        mock_clear.return_value = []

        result = runner.invoke(app, ["clear", "non-existent", "--force"])

        assert result.exit_code == 0
        assert "not found" in result.output

    @patch("graftpunk.cli.main.list_sessions")
    def test_clear_all_empty(self, mock_list):
        """Test clearing all when no sessions exist."""
        mock_list.return_value = []

        result = runner.invoke(app, ["clear", "--force"])

        assert result.exit_code == 0
        assert "No sessions to clear" in result.output


class TestExportCommand:
    """Tests for export command."""

    @patch("graftpunk.cli.main.load_session")
    def test_export_not_found(self, mock_load):
        """Test export command with non-existent session."""
        mock_load.side_effect = SessionNotFoundError("Session not found")

        result = runner.invoke(app, ["export", "non-existent"])

        assert result.exit_code == 1
        assert "not found" in result.output

    @patch("graftpunk.cli.main.load_session")
    def test_export_expired(self, mock_load):
        """Test export command with expired session."""
        mock_load.side_effect = SessionExpiredError("Session expired")

        result = runner.invoke(app, ["export", "expired"])

        assert result.exit_code == 1
        assert "expired" in result.output

    @patch("graftpunk.cli.main.load_session")
    def test_export_graftpunk_error(self, mock_load):
        """Test export command with graftpunk error."""
        mock_load.side_effect = GraftpunkError("Some graftpunk error")

        result = runner.invoke(app, ["export", "broken"])

        assert result.exit_code == 1
        assert "Failed to load" in result.output


class TestKeepaliveCommands:
    """Tests for keepalive subcommands."""

    @patch("graftpunk.cli.keepalive_commands.read_keepalive_pid")
    def test_keepalive_status_not_running(self, mock_pid):
        """Test keepalive status when not running."""
        mock_pid.return_value = None

        result = runner.invoke(app, ["keepalive", "status"])

        assert result.exit_code == 0
        assert "not running" in result.output

    @patch("graftpunk.cli.keepalive_commands.read_keepalive_pid")
    @patch("graftpunk.cli.keepalive_commands.read_keepalive_state")
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

    @patch("graftpunk.cli.keepalive_commands.read_keepalive_pid")
    def test_keepalive_stop_not_running(self, mock_pid):
        """Test keepalive stop when not running."""
        mock_pid.return_value = None

        result = runner.invoke(app, ["keepalive", "stop"])

        assert result.exit_code == 0
        assert "not running" in result.output


class TestPluginsCommand:
    """Tests for plugins command."""

    @patch("graftpunk.cli.main.discover_storage_backends")
    @patch("graftpunk.cli.main.discover_keepalive_handlers")
    @patch("graftpunk.cli.main.discover_site_plugins")
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

    @patch("graftpunk.cli.main.get_settings")
    def test_config_command(self, mock_settings):
        """Test config command output."""
        mock_settings.return_value = MagicMock(
            config_dir="/home/user/.config/graftpunk",
            sessions_dir="/home/user/.config/graftpunk/sessions",
            storage_backend="local",
            session_ttl_hours=720,
            log_level="INFO",
        )

        result = runner.invoke(app, ["config"])

        assert result.exit_code == 0
        assert "Configuration" in result.output
        assert "local" in result.output


class TestImportHarCommand:
    """Tests for import-har command."""

    def test_import_har_help(self):
        """Test that import-har command exists and shows help."""
        result = runner.invoke(app, ["import-har", "--help"])
        assert result.exit_code == 0
        assert "Import HAR file" in result.output
        assert "--format" in result.output
        assert "--dry-run" in result.output

    def test_import_har_file_not_found(self, tmp_path):
        """Test import-har with non-existent file."""
        result = runner.invoke(app, ["import-har", str(tmp_path / "nonexistent.har")])
        # Typer returns exit code 2 for validation errors (file doesn't exist)
        assert result.exit_code in (1, 2)
        assert "not found" in result.output.lower() or "does not exist" in result.output.lower()

    def test_import_har_invalid_format(self, tmp_path):
        """Test import-har with invalid format type."""
        har_file = tmp_path / "test.har"
        har_file.write_text('{"log": {"entries": []}}')

        result = runner.invoke(app, ["import-har", str(har_file), "--format", "invalid"])
        assert result.exit_code == 1
        assert "Invalid format" in result.output
        assert "python, yaml" in result.output

    def test_import_har_invalid_json(self, tmp_path):
        """Test import-har with invalid JSON file."""
        har_file = tmp_path / "test.har"
        har_file.write_text("not valid json")

        result = runner.invoke(app, ["import-har", str(har_file)])
        assert result.exit_code == 1
        assert "Failed to parse" in result.output

    def test_import_har_empty_entries(self, tmp_path):
        """Test import-har with HAR file containing no entries."""
        har_file = tmp_path / "test.har"
        har_file.write_text('{"log": {"entries": []}}')

        result = runner.invoke(app, ["import-har", str(har_file)])
        assert result.exit_code == 1
        assert "No HTTP entries" in result.output

    def test_import_har_dry_run(self, tmp_path):
        """Test import-har dry run mode."""
        import json

        har_content = json.dumps(
            {
                "log": {
                    "entries": [
                        {
                            "startedDateTime": "2024-01-01T00:00:00Z",
                            "request": {
                                "method": "GET",
                                "url": "https://example.com/api/test",
                                "headers": [],
                                "cookies": [],
                            },
                            "response": {
                                "status": 200,
                                "statusText": "OK",
                                "headers": [{"name": "Content-Type", "value": "application/json"}],
                                "cookies": [],
                                "content": {"text": "{}"},
                            },
                        }
                    ]
                }
            }
        )
        har_file = tmp_path / "test.har"
        har_file.write_text(har_content)

        result = runner.invoke(
            app, ["import-har", str(har_file), "--name", "testsite", "--dry-run"]
        )

        assert result.exit_code == 0
        assert "Dry run" in result.output or "class TestsitePlugin" in result.output

    def test_import_har_yaml_format(self, tmp_path):
        """Test import-har with YAML output format."""
        import json

        har_content = json.dumps(
            {
                "log": {
                    "entries": [
                        {
                            "startedDateTime": "2024-01-01T00:00:00Z",
                            "request": {
                                "method": "GET",
                                "url": "https://example.com/api/users",
                                "headers": [],
                                "cookies": [],
                            },
                            "response": {
                                "status": 200,
                                "statusText": "OK",
                                "headers": [{"name": "Content-Type", "value": "application/json"}],
                                "cookies": [],
                                "content": {"text": "[]"},
                            },
                        }
                    ]
                }
            }
        )
        har_file = tmp_path / "test.har"
        har_file.write_text(har_content)

        result = runner.invoke(
            app,
            ["import-har", str(har_file), "--name", "testsite", "--format", "yaml", "--dry-run"],
        )

        assert result.exit_code == 0
        assert "site_name:" in result.output or "Dry run" in result.output

    def test_import_har_success_writes_file(self, tmp_path):
        """Test import-har writes plugin file on success."""
        import json

        har_content = json.dumps(
            {
                "log": {
                    "entries": [
                        {
                            "startedDateTime": "2024-01-01T00:00:00Z",
                            "request": {
                                "method": "GET",
                                "url": "https://api.example.com/v1/users",
                                "headers": [],
                                "cookies": [],
                            },
                            "response": {
                                "status": 200,
                                "statusText": "OK",
                                "headers": [{"name": "Content-Type", "value": "application/json"}],
                                "cookies": [],
                                "content": {"text": "[]"},
                            },
                        }
                    ]
                }
            }
        )
        har_file = tmp_path / "test.har"
        har_file.write_text(har_content)

        output_file = tmp_path / "testsite.py"
        result = runner.invoke(
            app,
            ["import-har", str(har_file), "--name", "testsite", "--output", str(output_file)],
        )

        assert result.exit_code == 0
        assert output_file.exists()
        content = output_file.read_text()
        assert "class TestsitePlugin" in content
        assert "SitePlugin" in content
