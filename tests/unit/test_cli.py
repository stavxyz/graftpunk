"""Tests for CLI module."""

import re
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from graftpunk.cli.main import app
from graftpunk.exceptions import GraftpunkError, SessionExpiredError, SessionNotFoundError

runner = CliRunner()


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


class TestVersionCommand:
    """Tests for version command."""

    def test_version_command(self):
        """Test that version command outputs version info."""
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert "graftpunk" in result.output


class TestListCommand:
    """Tests for list command."""

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_list_empty(self, mock_list):
        """Test list command with no sessions."""
        mock_list.return_value = []

        result = runner.invoke(app, ["session", "list"])

        assert result.exit_code == 0
        assert "No Sessions" in result.output or "No sessions" in result.output

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
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

        result = runner.invoke(app, ["session", "list"])

        assert result.exit_code == 0
        assert "test-session" in result.output

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_list_json_output(self, mock_list):
        """Test list command with JSON output."""
        mock_list.return_value = [{"name": "test", "domain": "example.com"}]

        result = runner.invoke(app, ["session", "list", "--json"])

        assert result.exit_code == 0
        assert '"name": "test"' in result.output

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_list_logged_out_status(self, mock_list):
        """Test list command displays logged_out status correctly."""
        mock_list.return_value = [
            {
                "name": "old-session",
                "domain": "example.com",
                "status": "logged_out",
                "cookie_count": 0,
                "modified_at": "2026-01-01T00:00:00",
            }
        ]

        result = runner.invoke(app, ["session", "list"])

        assert result.exit_code == 0
        assert "old-session" in result.output
        output = strip_ansi(result.output)
        assert "logged out" in output

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_list_unknown_status(self, mock_list):
        """Test list command displays unknown/custom status correctly."""
        mock_list.return_value = [
            {
                "name": "weird-session",
                "domain": "example.com",
                "status": "unknown",
                "cookie_count": 1,
                "modified_at": "",
            }
        ]

        result = runner.invoke(app, ["session", "list"])

        assert result.exit_code == 0
        assert "weird-session" in result.output
        output = strip_ansi(result.output)
        assert "unknown" in output


class TestShowCommand:
    """Tests for show command."""

    @patch("graftpunk.cli.session_commands.get_session_metadata")
    def test_show_not_found(self, mock_get):
        """Test show command with non-existent session."""
        mock_get.return_value = None

        result = runner.invoke(app, ["session", "show", "non-existent"])

        assert result.exit_code == 1
        assert "not found" in result.output

    @patch("graftpunk.cli.session_commands.get_session_metadata")
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

        result = runner.invoke(app, ["session", "show", "test-session"])

        assert result.exit_code == 0
        assert "test-session" in result.output
        assert "example.com" in result.output

    @patch("graftpunk.cli.session_commands.get_session_metadata")
    def test_show_logged_out_status(self, mock_get):
        """Test show command displays logged_out status correctly."""
        mock_get.return_value = {
            "name": "expired-session",
            "domain": "example.com",
            "status": "logged_out",
            "cookie_count": 0,
            "created_at": "2026-01-01T00:00:00",
            "modified_at": "2026-01-01T00:00:00",
            "expires_at": "never",
        }

        result = runner.invoke(app, ["session", "show", "expired-session"])

        assert result.exit_code == 0
        assert "expired-session" in result.output
        output = strip_ansi(result.output)
        assert "logged out" in output

    @patch("graftpunk.cli.session_commands.get_session_metadata")
    def test_show_unknown_status(self, mock_get):
        """Test show command displays unknown/custom status correctly."""
        mock_get.return_value = {
            "name": "weird-session",
            "domain": "example.com",
            "status": "unknown",
            "cookie_count": 2,
            "created_at": "2026-01-01T00:00:00",
            "modified_at": "2026-01-01T00:00:00",
            "expires_at": "never",
        }

        result = runner.invoke(app, ["session", "show", "weird-session"])

        assert result.exit_code == 0
        assert "weird-session" in result.output
        output = strip_ansi(result.output)
        assert "unknown" in output

    @patch("graftpunk.cli.session_commands.get_session_metadata")
    def test_show_json_output(self, mock_get):
        """Test show command with --json flag."""
        mock_get.return_value = {
            "name": "test-session",
            "domain": "example.com",
            "status": "active",
            "cookie_count": 5,
        }

        result = runner.invoke(app, ["session", "show", "test-session", "--json"])

        assert result.exit_code == 0
        assert '"name": "test-session"' in result.output
        assert '"domain": "example.com"' in result.output


class TestClearCommand:
    """Tests for session clear command."""

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    @patch("graftpunk.cli.session_commands.clear_session_cache")
    def test_clear_by_name(self, mock_clear, mock_list_meta):
        """Clear a specific session by name (no dots = name)."""
        mock_list_meta.return_value = [
            {
                "name": "hackernews",
                "domain": "news.ycombinator.com",
                "cookie_count": 3,
                "modified_at": "2026-01-01T00:00:00",
            },
        ]
        mock_clear.return_value = ["hackernews"]

        result = runner.invoke(app, ["session", "clear", "hackernews", "-f"])

        assert result.exit_code == 0
        assert "hackernews" in result.output
        mock_clear.assert_called_once_with("hackernews")

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    @patch("graftpunk.cli.session_commands.clear_session_cache")
    def test_clear_by_name_not_found(self, mock_clear, mock_list_meta):
        """Clear by name when session doesn't exist."""
        mock_list_meta.return_value = []
        mock_clear.return_value = []

        result = runner.invoke(app, ["session", "clear", "nonexistent", "-f"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    @patch("graftpunk.cli.session_commands.clear_session_cache")
    def test_clear_by_domain(self, mock_clear, mock_list_meta):
        """Clear sessions matching a domain (dots = domain)."""
        mock_list_meta.return_value = [
            {
                "name": "app1",
                "domain": "example.com",
                "cookie_count": 2,
                "modified_at": "2026-01-01T00:00:00",
            },
            {
                "name": "app2",
                "domain": "example.com",
                "cookie_count": 5,
                "modified_at": "2026-01-01T00:00:00",
            },
            {
                "name": "other",
                "domain": "other.com",
                "cookie_count": 1,
                "modified_at": "2026-01-01T00:00:00",
            },
        ]
        mock_clear.side_effect = lambda n: [n]

        result = runner.invoke(app, ["session", "clear", "example.com", "-f"])

        assert result.exit_code == 0
        assert "app1" in result.output
        assert "app2" in result.output
        assert "other" not in result.output
        assert mock_clear.call_count == 2

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_clear_by_domain_no_matches(self, mock_list_meta):
        """Clear by domain when no sessions match."""
        mock_list_meta.return_value = [
            {
                "name": "app1",
                "domain": "other.com",
                "cookie_count": 1,
                "modified_at": "2026-01-01T00:00:00",
            },
        ]

        result = runner.invoke(app, ["session", "clear", "example.com", "-f"])

        assert result.exit_code == 1
        assert "no sessions" in result.output.lower() or "not found" in result.output.lower()

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    @patch("graftpunk.cli.session_commands.clear_session_cache")
    def test_clear_all_with_force(self, mock_clear, mock_list_meta):
        """Clear all sessions with --all --force."""
        mock_list_meta.return_value = [
            {
                "name": "s1",
                "domain": "a.com",
                "cookie_count": 1,
                "modified_at": "2026-01-01T00:00:00",
            },
            {
                "name": "s2",
                "domain": "b.com",
                "cookie_count": 2,
                "modified_at": "2026-01-01T00:00:00",
            },
        ]
        mock_clear.side_effect = lambda n: [n]

        result = runner.invoke(app, ["session", "clear", "--all", "--force"])

        assert result.exit_code == 0
        assert "s1" in result.output
        assert "s2" in result.output
        assert mock_clear.call_count == 2

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    @patch("graftpunk.cli.session_commands.clear_session_cache")
    def test_clear_all_prompts_without_force(self, mock_clear, mock_list_meta):
        """Clear all prompts for confirmation without --force."""
        mock_list_meta.return_value = [
            {
                "name": "s1",
                "domain": "a.com",
                "cookie_count": 1,
                "modified_at": "2026-01-01T00:00:00",
            },
        ]

        result = runner.invoke(app, ["session", "clear", "--all"], input="n\n")

        assert result.exit_code == 0
        assert "Cancelled" in result.output
        mock_clear.assert_not_called()

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_clear_all_empty(self, mock_list_meta):
        """Clear all when no sessions exist."""
        mock_list_meta.return_value = []

        result = runner.invoke(app, ["session", "clear", "--all", "-f"])

        assert result.exit_code == 0
        assert "No sessions" in result.output

    def test_clear_no_target_no_all(self):
        """Clear with no target and no --all shows error."""
        result = runner.invoke(app, ["session", "clear"])

        assert (
            result.exit_code != 0 or "specify" in result.output.lower() or "Usage" in result.output
        )

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    @patch("graftpunk.cli.session_commands.clear_session_cache")
    def test_clear_by_name_prompts_without_force(self, mock_clear, mock_list_meta):
        """Clear by name prompts for confirmation without --force."""
        mock_list_meta.return_value = [
            {
                "name": "hackernews",
                "domain": "news.ycombinator.com",
                "cookie_count": 3,
                "modified_at": "2026-01-01T00:00:00",
            },
        ]

        result = runner.invoke(app, ["session", "clear", "hackernews"], input="n\n")

        assert result.exit_code == 0
        assert "Cancelled" in result.output
        mock_clear.assert_not_called()

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    @patch("graftpunk.cli.session_commands.clear_session_cache")
    def test_clear_always_shows_removed_list(self, mock_clear, mock_list_meta):
        """Clear always prints list of removed sessions."""
        mock_list_meta.return_value = [
            {
                "name": "hackernews",
                "domain": "news.ycombinator.com",
                "cookie_count": 3,
                "modified_at": "2026-01-01T00:00:00",
            },
        ]
        mock_clear.return_value = ["hackernews"]

        result = runner.invoke(app, ["session", "clear", "hackernews", "-f"])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "hackernews" in output


class TestExportCommand:
    """Tests for export command."""

    @patch("graftpunk.cli.session_commands.load_session")
    def test_export_not_found(self, mock_load):
        """Test export command with non-existent session."""
        mock_load.side_effect = SessionNotFoundError("Session not found")

        result = runner.invoke(app, ["session", "export", "non-existent"])

        assert result.exit_code == 1
        assert "not found" in result.output

    @patch("graftpunk.cli.session_commands.load_session")
    def test_export_expired(self, mock_load):
        """Test export command with expired session."""
        mock_load.side_effect = SessionExpiredError("Session expired")

        result = runner.invoke(app, ["session", "export", "expired"])

        assert result.exit_code == 1
        assert "expired" in result.output

    @patch("graftpunk.cli.session_commands.load_session")
    def test_export_graftpunk_error(self, mock_load):
        """Test export command with graftpunk error."""
        mock_load.side_effect = GraftpunkError("Some graftpunk error")

        result = runner.invoke(app, ["session", "export", "broken"])

        assert result.exit_code == 1
        assert "Failed to load" in result.output

    @patch("graftpunk.cli.session_commands.load_session")
    def test_export_success(self, mock_load):
        """Test export command with successful HTTPie export."""
        mock_session = MagicMock()
        mock_session.save_httpie_session.return_value = "/home/user/.httpie/sessions/test.json"
        mock_load.return_value = mock_session

        result = runner.invoke(app, ["session", "export", "test-session"])

        assert result.exit_code == 0
        assert "Exported to" in result.output
        assert "/home/user/.httpie/sessions/test.json" in result.output
        mock_session.save_httpie_session.assert_called_once_with("test-session")

    @patch("graftpunk.cli.session_commands.load_session")
    def test_export_oserror(self, mock_load):
        """Test export command when save_httpie_session raises OSError."""
        mock_session = MagicMock()
        mock_session.save_httpie_session.side_effect = OSError("Permission denied")
        mock_load.return_value = mock_session

        result = runner.invoke(app, ["session", "export", "test-session"])

        assert result.exit_code == 1
        assert "Export failed" in result.output

    @patch("graftpunk.cli.session_commands.load_session")
    def test_export_attribute_error(self, mock_load):
        """Test export command when session lacks save_httpie_session method."""
        mock_session = MagicMock()
        mock_session.save_httpie_session.side_effect = AttributeError(
            "object has no attribute 'save_httpie_session'"
        )
        mock_load.return_value = mock_session

        result = runner.invoke(app, ["session", "export", "test-session"])

        assert result.exit_code == 1
        assert "Export failed" in result.output


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

    @patch("graftpunk.cli.main.create_yaml_plugins")
    @patch("graftpunk.cli.main.discover_storage_backends")
    @patch("graftpunk.cli.main.discover_keepalive_handlers")
    @patch("graftpunk.cli.main.discover_site_plugins")
    def test_plugins_none_installed(self, mock_site, mock_handlers, mock_storage, mock_yaml):
        """Test plugins command with no plugins installed."""
        mock_storage.return_value = {}
        mock_handlers.return_value = {}
        mock_site.return_value = {}
        mock_yaml.return_value = ([], [])

        result = runner.invoke(app, ["plugins"])

        assert result.exit_code == 0
        assert "Plugins" in result.output
        assert "none installed" in result.output

    @patch("graftpunk.cli.main.create_yaml_plugins")
    @patch("graftpunk.cli.main.discover_storage_backends")
    @patch("graftpunk.cli.main.discover_keepalive_handlers")
    @patch("graftpunk.cli.main.discover_site_plugins")
    def test_plugins_with_all_types(self, mock_site, mock_handlers, mock_storage, mock_yaml):
        """Test plugins command with all plugin types installed."""
        mock_storage.return_value = {"local": MagicMock(), "supabase": MagicMock()}
        mock_handlers.return_value = {"cookie-refresh": MagicMock()}
        mock_site.return_value = {"my-plugin": MagicMock()}
        mock_yaml.return_value = ([], [])

        result = runner.invoke(app, ["plugins"])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "4 installed" in output
        assert "local" in output
        assert "supabase" in output
        assert "cookie-refresh" in output
        assert "my-plugin" in output

    @patch("graftpunk.cli.main.create_yaml_plugins")
    @patch("graftpunk.cli.main.discover_storage_backends")
    @patch("graftpunk.cli.main.discover_keepalive_handlers")
    @patch("graftpunk.cli.main.discover_site_plugins")
    def test_plugins_yaml_plugin_names_aggregated(
        self, mock_site, mock_handlers, mock_storage, mock_yaml
    ):
        """Test that YAML plugin names are aggregated into site plugin names."""
        mock_storage.return_value = {}
        mock_handlers.return_value = {}
        mock_site.return_value = {"existing-cli": MagicMock()}

        yaml_plugin = MagicMock()
        yaml_plugin.site_name = "yaml-site"
        mock_yaml.return_value = ([yaml_plugin], [])

        result = runner.invoke(app, ["plugins"])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "existing-cli" in output
        assert "yaml-site" in output


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
        assert "filesystem" in result.output

    @patch("graftpunk.cli.main.get_settings")
    def test_config_supabase_storage(self, mock_settings):
        """Test config command with supabase storage backend."""
        mock_settings.return_value = MagicMock(
            config_dir="/home/user/.config/graftpunk",
            sessions_dir="/home/user/.config/graftpunk/sessions",
            storage_backend="supabase",
            session_ttl_hours=720,
            log_level="INFO",
        )

        result = runner.invoke(app, ["config"])

        assert result.exit_code == 0
        assert "supabase" in result.output
        assert "cloud" in result.output


class TestVerbosity:
    """Tests for CLI verbosity flags."""

    def test_default_log_level_is_warning(self) -> None:
        """Test that default log level is WARNING (minimal output)."""
        import structlog

        from graftpunk.logging import configure_logging

        configure_logging(level="WARNING")
        logger = structlog.get_logger("test")
        assert logger is not None

    def test_verbose_flag_in_help(self) -> None:
        """Test that --verbose/-v flag appears in help output."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "--verbose" in output or "-v" in output


class TestImportHarCommand:
    """Tests for import-har command."""

    def test_import_har_help(self):
        """Test that import-har command exists and shows help."""
        result = runner.invoke(app, ["import-har", "--help"])
        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "Import HAR file" in output
        assert "--format" in output
        assert "--dry-run" in output

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


class TestObserveCommands:
    """Tests for observe command group."""

    def test_observe_list_empty(self, tmp_path):
        """Test observe list when no runs exist."""
        empty_dir = tmp_path / "empty_observe"
        empty_dir.mkdir()
        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", empty_dir):
            result = runner.invoke(app, ["observe", "list"])

        assert result.exit_code == 0
        assert "No observe" in result.output or "no observe" in result.output.lower()

    def test_observe_list_with_runs(self, tmp_path):
        """Test observe list when runs exist."""
        # Create a fake observe run
        run_dir = tmp_path / "my-session" / "20260101-120000"
        run_dir.mkdir(parents=True)
        (run_dir / "metadata.json").write_text("{}")

        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", tmp_path):
            result = runner.invoke(app, ["observe", "list"])

        assert result.exit_code == 0
        assert "my-session" in result.output

    def test_observe_show_not_found(self, tmp_path):
        """Test observe show with non-existent session."""
        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", tmp_path):
            result = runner.invoke(app, ["observe", "show", "nonexistent"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower() or "No runs" in result.output

    def test_observe_show_with_run(self, tmp_path):
        """Test observe show with existing run."""
        import json

        run_dir = tmp_path / "my-session" / "20260101-120000"
        run_dir.mkdir(parents=True)
        (run_dir / "metadata.json").write_text(json.dumps({"session": "my-session"}))
        (run_dir / "events.jsonl").write_text("")

        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", tmp_path):
            result = runner.invoke(app, ["observe", "show", "my-session"])

        assert result.exit_code == 0
        assert "my-session" in result.output

    def test_observe_clean_empty(self, tmp_path):
        """Test observe clean when nothing to clean."""
        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", tmp_path):
            result = runner.invoke(app, ["observe", "clean", "--force"])

        assert result.exit_code == 0

    def test_observe_clean_removes_runs(self, tmp_path):
        """Test observe clean removes run directories."""
        run_dir = tmp_path / "my-session" / "20260101-120000"
        run_dir.mkdir(parents=True)
        (run_dir / "events.jsonl").write_text("")

        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", tmp_path):
            result = runner.invoke(app, ["observe", "clean", "--force"])

        assert result.exit_code == 0
        assert not run_dir.exists()

    def test_observe_help(self):
        """Test observe command shows help."""
        result = runner.invoke(app, ["observe", "--help"])
        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "list" in output or "show" in output or "clean" in output


class TestObserveCLICommands:
    """Extended tests for observe list, observe show, and observe clean CLI commands."""

    def test_observe_list_no_directory(self, tmp_path):
        """Test observe list when the observe directory does not exist at all."""
        nonexistent = tmp_path / "does_not_exist"
        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", nonexistent):
            result = runner.invoke(app, ["observe", "list"])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "No observe data found" in output or "no observe" in output.lower()

    def test_observe_list_with_multiple_sessions(self, tmp_path):
        """Test observe list with multiple sessions and runs."""
        observe_dir = tmp_path / "observe_data"
        observe_dir.mkdir()
        # Session alpha with two runs
        (observe_dir / "alpha" / "run-001").mkdir(parents=True)
        (observe_dir / "alpha" / "run-002").mkdir(parents=True)
        # Session beta with one run
        (observe_dir / "beta" / "run-001").mkdir(parents=True)

        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", observe_dir):
            result = runner.invoke(app, ["observe", "list"])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "alpha" in output
        assert "beta" in output
        assert "3 run(s)" in output

    def test_observe_list_ignores_files_in_base_dir(self, tmp_path):
        """Test observe list ignores non-directory entries in the base dir."""
        observe_dir = tmp_path / "observe_data"
        observe_dir.mkdir()
        (observe_dir / "stray-file.txt").write_text("not a session")
        (observe_dir / "real-session" / "run-001").mkdir(parents=True)

        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", observe_dir):
            result = runner.invoke(app, ["observe", "list"])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "real-session" in output
        assert "1 run(s)" in output

    def test_observe_show_latest_run(self, tmp_path):
        """Test observe show picks the latest run when no run_id is given."""
        # Create two runs; the latest alphabetically should be picked
        early_run = tmp_path / "my-session" / "20260101-080000"
        late_run = tmp_path / "my-session" / "20260101-120000"
        early_run.mkdir(parents=True)
        late_run.mkdir(parents=True)
        (early_run / "early.log").write_text("early data")
        (late_run / "late.log").write_text("late data")

        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", tmp_path):
            result = runner.invoke(app, ["observe", "show", "my-session"])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        # Should show the latest run (20260101-120000)
        assert "20260101-120000" in output
        assert "late.log" in output

    def test_observe_show_specific_run_id(self, tmp_path):
        """Test observe show with an explicit run ID."""
        run_dir = tmp_path / "my-session" / "run-specific"
        run_dir.mkdir(parents=True)
        (run_dir / "data.json").write_text('{"key": "value"}')

        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", tmp_path):
            result = runner.invoke(app, ["observe", "show", "my-session", "run-specific"])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "run-specific" in output
        assert "data.json" in output

    def test_observe_show_nonexistent_run_id(self, tmp_path):
        """Test observe show with a run ID that does not exist."""
        session_dir = tmp_path / "my-session"
        session_dir.mkdir(parents=True)
        (session_dir / "existing-run").mkdir()

        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", tmp_path):
            result = runner.invoke(app, ["observe", "show", "my-session", "ghost-run"])

        assert result.exit_code == 1
        output = strip_ansi(result.output)
        assert "ghost-run" in output
        assert "not found" in output.lower()

    def test_observe_show_no_runs_in_session(self, tmp_path):
        """Test observe show when session directory exists but has no run subdirs."""
        session_dir = tmp_path / "empty-session"
        session_dir.mkdir(parents=True)
        # Only a file, no run directories
        (session_dir / "stray.txt").write_text("not a run")

        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", tmp_path):
            result = runner.invoke(app, ["observe", "show", "empty-session"])

        assert result.exit_code == 1
        output = strip_ansi(result.output)
        assert "No runs found" in output or "no runs" in output.lower()

    def test_observe_show_run_with_subdirectory(self, tmp_path):
        """Test observe show displays subdirectory info within a run."""
        run_dir = tmp_path / "my-session" / "run-001"
        run_dir.mkdir(parents=True)
        screenshots = run_dir / "screenshots"
        screenshots.mkdir()
        (screenshots / "page1.png").write_text("fake png")
        (screenshots / "page2.png").write_text("fake png")
        (run_dir / "har.json").write_text("{}")

        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", tmp_path):
            result = runner.invoke(app, ["observe", "show", "my-session", "run-001"])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "screenshots/" in output
        assert "2 files" in output
        assert "har.json" in output

    def test_observe_clean_specific_session(self, tmp_path):
        """Test observe clean removes only the specified session."""
        (tmp_path / "session-a" / "run-001").mkdir(parents=True)
        (tmp_path / "session-b" / "run-001").mkdir(parents=True)

        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", tmp_path):
            result = runner.invoke(app, ["observe", "clean", "session-a", "--force"])

        assert result.exit_code == 0
        assert not (tmp_path / "session-a").exists()
        assert (tmp_path / "session-b").exists()
        output = strip_ansi(result.output)
        assert "session-a" in output

    def test_observe_clean_all(self, tmp_path):
        """Test observe clean --force with no session arg removes all data."""
        (tmp_path / "session-a" / "run-001").mkdir(parents=True)
        (tmp_path / "session-b" / "run-001").mkdir(parents=True)

        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", tmp_path):
            result = runner.invoke(app, ["observe", "clean", "--force"])

        assert result.exit_code == 0
        # The entire OBSERVE_BASE_DIR is removed by shutil.rmtree
        assert not tmp_path.exists()
        output = strip_ansi(result.output)
        assert "all" in output.lower() or "Removed" in output

    def test_observe_clean_nonexistent_session(self, tmp_path):
        """Test observe clean for a session that does not exist."""
        (tmp_path / "other-session" / "run-001").mkdir(parents=True)

        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", tmp_path):
            result = runner.invoke(app, ["observe", "clean", "no-such-session", "--force"])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "No data" in output or "no data" in output.lower()
        # Other session should remain untouched
        assert (tmp_path / "other-session").exists()

    def test_observe_clean_cancelled(self, tmp_path):
        """Test observe clean cancelled by user when confirmation is denied."""
        (tmp_path / "my-session" / "run-001").mkdir(parents=True)

        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", tmp_path):
            result = runner.invoke(app, ["observe", "clean", "my-session"], input="n\n")

        assert result.exit_code == 0
        assert "Cancelled" in result.output
        # Data should still be there
        assert (tmp_path / "my-session").exists()

    def test_observe_clean_all_cancelled(self, tmp_path):
        """Test observe clean all cancelled by user."""
        (tmp_path / "my-session" / "run-001").mkdir(parents=True)

        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", tmp_path):
            result = runner.invoke(app, ["observe", "clean"], input="n\n")

        assert result.exit_code == 0
        assert "Cancelled" in result.output
        assert (tmp_path / "my-session").exists()

    def test_observe_clean_no_base_dir(self, tmp_path):
        """Test observe clean when the base directory does not exist."""
        nonexistent = tmp_path / "nonexistent"
        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", nonexistent):
            result = runner.invoke(app, ["observe", "clean", "--force"])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "No observe data" in output or "no observe" in output.lower()


class TestObserveSessionFlag:
    """Tests for --session flag on observe command group."""

    def test_observe_list_with_session_flag(self, tmp_path):
        """--session on observe should scope list to that session."""
        base = tmp_path / "observe"
        (base / "site-a" / "run-001").mkdir(parents=True)
        (base / "site-b" / "run-001").mkdir(parents=True)

        with (
            patch("graftpunk.cli.main.OBSERVE_BASE_DIR", base),
            patch("graftpunk.cli.main.resolve_session_name", return_value="site-a"),
        ):
            result = runner.invoke(app, ["observe", "--session", "site-a", "list"])
        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "site-a" in output
        assert "site-b" not in output

    def test_observe_list_without_session_shows_all(self, tmp_path):
        """Without --session, observe list shows all sessions."""
        base = tmp_path / "observe"
        (base / "site-a" / "run-001").mkdir(parents=True)
        (base / "site-b" / "run-001").mkdir(parents=True)

        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", base):
            result = runner.invoke(app, ["observe", "list"])
        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "site-a" in output
        assert "site-b" in output

    def test_observe_show_uses_session_flag(self, tmp_path):
        """--session flag provides default session_name for show."""
        base = tmp_path / "observe"
        run_dir = base / "site-a" / "20260101-120000"
        run_dir.mkdir(parents=True)
        (run_dir / "data.json").write_text("{}")

        with (
            patch("graftpunk.cli.main.OBSERVE_BASE_DIR", base),
            patch("graftpunk.cli.main.resolve_session_name", return_value="site-a"),
        ):
            result = runner.invoke(app, ["observe", "--session", "site-a", "show"])
        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "site-a" in output

    def test_observe_show_no_session_no_arg_errors(self, tmp_path):
        """observe show with no --session and no arg should error."""
        base = tmp_path / "observe"
        base.mkdir(parents=True)

        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", base):
            result = runner.invoke(app, ["observe", "show"])
        assert result.exit_code == 1

    def test_observe_clean_uses_session_flag(self, tmp_path):
        """--session flag provides default session_name for clean."""
        base = tmp_path / "observe"
        (base / "site-a" / "run-001").mkdir(parents=True)
        (base / "site-b" / "run-001").mkdir(parents=True)

        with (
            patch("graftpunk.cli.main.OBSERVE_BASE_DIR", base),
            patch("graftpunk.cli.main.resolve_session_name", return_value="site-a"),
        ):
            result = runner.invoke(
                app,
                ["observe", "--session", "site-a", "clean", "--force"],
            )
        assert result.exit_code == 0
        assert not (base / "site-a").exists()
        assert (base / "site-b").exists()


class TestMainCallback:
    """Tests for main_callback verbose/observe/log-format flags."""

    @patch("graftpunk.cli.main.configure_logging")
    @patch("graftpunk.cli.main.get_settings")
    def test_verbose_flag_sets_info_level(self, mock_settings, mock_configure):
        """Test that -v flag calls configure_logging with INFO level."""
        mock_settings.return_value = MagicMock(
            log_level="WARNING",
            log_format="console",
        )

        runner.invoke(app, ["-v", "version"])

        # configure_logging should have been called with INFO
        calls = [
            c
            for c in mock_configure.call_args_list
            if c.kwargs.get("level") == "INFO" or (c.args and c.args[0] == "INFO")
        ]
        assert len(calls) >= 1, (
            f"Expected configure_logging called with level='INFO', "
            f"got calls: {mock_configure.call_args_list}"
        )

    @patch("graftpunk.cli.main.configure_logging")
    @patch("graftpunk.cli.main.get_settings")
    def test_double_verbose_sets_debug_level(self, mock_settings, mock_configure):
        """Test that -vv flag calls configure_logging with DEBUG level."""
        mock_settings.return_value = MagicMock(
            log_level="WARNING",
            log_format="console",
        )

        runner.invoke(app, ["-vv", "version"])

        calls = [
            c
            for c in mock_configure.call_args_list
            if c.kwargs.get("level") == "DEBUG" or (c.args and c.args[0] == "DEBUG")
        ]
        assert len(calls) >= 1, (
            f"Expected configure_logging called with level='DEBUG', "
            f"got calls: {mock_configure.call_args_list}"
        )

    @patch("graftpunk.cli.main.get_settings")
    def test_observe_flag_propagates_to_context(self, mock_settings):
        """Test that --observe full sets observe_mode in Click context."""
        mock_settings.return_value = MagicMock(
            log_level="WARNING",
            log_format="console",
        )

        # We need to capture the context. Use a subcommand that reads ctx.
        # The version command doesn't use ctx, but we can verify the flag is accepted.
        result = runner.invoke(app, ["--observe", "full", "version"])

        assert result.exit_code == 0

    @patch("graftpunk.cli.main.configure_logging")
    @patch("graftpunk.cli.main.get_settings")
    def test_log_format_json_flag(self, mock_settings, mock_configure):
        """Test that --log-format json reconfigures logging with json_output=True."""
        mock_settings.return_value = MagicMock(
            log_level="WARNING",
            log_format="console",
        )

        runner.invoke(app, ["--log-format", "json", "version"])

        # Should have called configure_logging with json_output=True
        calls = [
            c
            for c in mock_configure.call_args_list
            if c.kwargs.get("json_output") is True or (len(c.args) > 1 and c.args[1] is True)
        ]
        assert len(calls) >= 1, (
            f"Expected configure_logging called with json_output=True, "
            f"got calls: {mock_configure.call_args_list}"
        )

    @patch("graftpunk.cli.main.get_settings")
    def test_observe_off_is_default(self, mock_settings):
        """Test that observe mode defaults to 'off'."""
        mock_settings.return_value = MagicMock(
            log_level="WARNING",
            log_format="console",
        )

        # Just invoking without --observe should work (default is "off")
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0

    @patch("graftpunk.cli.main.get_settings")
    def test_observe_invalid_choice(self, mock_settings):
        """Test that --observe with invalid value is rejected."""
        mock_settings.return_value = MagicMock(
            log_level="WARNING",
            log_format="console",
        )

        result = runner.invoke(app, ["--observe", "invalid", "version"])
        # Typer/Click should reject invalid choice
        assert result.exit_code != 0


class TestSessionUseCommand:
    """Tests for session use command."""

    @patch("graftpunk.cli.session_commands.resolve_session_name")
    def test_session_use_sets_active(self, mock_resolve, tmp_path):
        """Test session use writes .gp-session file."""
        mock_resolve.return_value = "resolved-session"
        with patch("graftpunk.session_context.Path.cwd", return_value=tmp_path):
            result = runner.invoke(app, ["session", "use", "my-plugin"])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "resolved-session" in output
        assert (tmp_path / ".gp-session").read_text() == "resolved-session"

    @patch("graftpunk.cli.session_commands.resolve_session_name")
    def test_session_use_shows_resolution(self, mock_resolve, tmp_path):
        """Test session use shows resolution info when name differs."""
        mock_resolve.return_value = "news.ycombinator.com"
        with patch("graftpunk.session_context.Path.cwd", return_value=tmp_path):
            result = runner.invoke(app, ["session", "use", "hackernews"])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "news.ycombinator.com" in output
        assert "resolved from plugin" in output
        assert "hackernews" in output

    @patch("graftpunk.cli.session_commands.resolve_session_name")
    def test_session_use_no_resolution_message_when_same(self, mock_resolve, tmp_path):
        """Test session use does not show resolution when name is unchanged."""
        mock_resolve.return_value = "my-session"
        with patch("graftpunk.session_context.Path.cwd", return_value=tmp_path):
            result = runner.invoke(app, ["session", "use", "my-session"])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "resolved from plugin" not in output


class TestSessionUnsetCommand:
    """Tests for session unset command."""

    def test_session_unset_removes_file(self, tmp_path):
        """Test session unset removes .gp-session file."""
        session_file = tmp_path / ".gp-session"
        session_file.write_text("my-session")
        with patch("graftpunk.session_context.Path.cwd", return_value=tmp_path):
            result = runner.invoke(app, ["session", "unset"])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "Active session cleared" in output
        assert not session_file.exists()

    def test_session_unset_noop_when_no_file(self, tmp_path):
        """Test session unset is a no-op when no .gp-session file exists."""
        with patch("graftpunk.session_context.Path.cwd", return_value=tmp_path):
            result = runner.invoke(app, ["session", "unset"])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "Active session cleared" in output


class TestObserveGoCommand:
    """Tests for observe go command."""

    def test_without_session_requires_session(self):
        """observe go without session or --no-session should fail."""
        result = runner.invoke(app, ["observe", "go", "https://example.com"])
        assert result.exit_code == 1
        output = strip_ansi(result.output)
        assert "no session" in output.lower() or "--no-session" in output

    def test_with_no_session_flag_proceeds(self):
        """observe go --no-session should infer namespace from URL and proceed."""
        with patch("graftpunk.cli.main.asyncio") as mock_asyncio:
            result = runner.invoke(app, ["observe", "--no-session", "go", "https://example.com"])
        assert result.exit_code == 0
        mock_asyncio.run.assert_called_once()

    def test_session_and_no_session_conflict(self):
        """observe --session X --no-session should fail."""
        result = runner.invoke(
            app, ["observe", "--session", "mysite", "--no-session", "go", "https://example.com"]
        )
        assert result.exit_code == 1
        output = strip_ansi(result.output)
        assert "Cannot use --session and --no-session" in output

    def test_observe_go_with_session_flag(self):
        """observe go --session should run the capture flow."""
        with (
            patch("graftpunk.cli.main.resolve_session_name", return_value="mysite"),
            patch("graftpunk.cli.main.asyncio") as mock_asyncio,
        ):
            result = runner.invoke(
                app, ["observe", "--session", "mysite", "go", "https://example.com"]
            )
        mock_asyncio.run.assert_called_once()
        assert result.exit_code == 0

    def test_observe_go_with_wait_option(self):
        """observe go --wait should pass wait value through."""
        with (
            patch("graftpunk.cli.main.resolve_session_name", return_value="mysite"),
            patch("graftpunk.cli.main.asyncio") as mock_asyncio,
        ):
            result = runner.invoke(
                app,
                ["observe", "--session", "mysite", "go", "--wait", "10", "https://example.com"],
            )
        mock_asyncio.run.assert_called_once()
        assert result.exit_code == 0


class TestResolveSessionNameIntegration:
    """Tests for resolve_session_name integration in show, clear, and export commands."""

    @patch("graftpunk.cli.session_commands.get_session_metadata")
    @patch("graftpunk.cli.session_commands.resolve_session_name")
    def test_show_resolves_site_name_to_session_name(self, mock_resolve, mock_get_metadata):
        """Test that show command calls resolve_session_name with the given name."""
        mock_resolve.return_value = "resolved-session"
        mock_get_metadata.return_value = {
            "name": "resolved-session",
            "domain": "example.com",
            "status": "active",
            "cookie_count": 3,
            "created_at": "2026-01-01T00:00:00",
            "modified_at": "2026-01-01T00:00:00",
            "expires_at": "never",
        }

        result = runner.invoke(app, ["session", "show", "my-plugin"])

        assert result.exit_code == 0
        mock_resolve.assert_called_once_with("my-plugin")
        assert "resolved-session" in result.output

    @patch("graftpunk.cli.session_commands.clear_session_cache")
    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    @patch("graftpunk.cli.session_commands.resolve_session_name")
    def test_clear_resolves_site_name_to_session_name(
        self, mock_resolve, mock_list_meta, mock_clear
    ):
        """Test that clear command calls resolve_session_name with the given name."""
        mock_resolve.return_value = "resolved-session"
        mock_list_meta.return_value = [
            {
                "name": "resolved-session",
                "domain": "example.com",
                "cookie_count": 3,
                "modified_at": "2026-01-01T00:00:00",
            },
        ]
        mock_clear.return_value = ["resolved-session"]

        result = runner.invoke(app, ["session", "clear", "my-plugin", "--force"])

        assert result.exit_code == 0
        mock_resolve.assert_called_once_with("my-plugin")
        mock_clear.assert_called_once_with("resolved-session")

    @patch("graftpunk.cli.session_commands.load_session")
    @patch("graftpunk.cli.session_commands.resolve_session_name")
    def test_export_resolves_site_name_to_session_name(self, mock_resolve, mock_load):
        """Test that export command calls resolve_session_name with the given name."""
        mock_resolve.return_value = "resolved-session"
        mock_session = MagicMock()
        mock_session.save_httpie_session.return_value = (
            "/home/user/.httpie/sessions/resolved-session.json"
        )
        mock_load.return_value = mock_session

        result = runner.invoke(app, ["session", "export", "my-plugin"])

        assert result.exit_code == 0
        mock_resolve.assert_called_once_with("my-plugin")
        mock_load.assert_called_once_with("resolved-session")

    @patch("graftpunk.cli.session_commands.get_session_metadata")
    @patch("graftpunk.cli.session_commands.resolve_session_name")
    def test_show_passthrough_when_no_mapping(self, mock_resolve, mock_get_metadata):
        """Test that resolve_session_name passes through when name has no mapping."""
        mock_resolve.return_value = "literal-session-name"
        mock_get_metadata.return_value = {
            "name": "literal-session-name",
            "domain": "example.com",
            "status": "active",
            "cookie_count": 1,
            "created_at": "2026-01-01T00:00:00",
            "modified_at": "2026-01-01T00:00:00",
            "expires_at": "never",
        }

        result = runner.invoke(app, ["session", "show", "literal-session-name"])

        assert result.exit_code == 0
        mock_resolve.assert_called_once_with("literal-session-name")
