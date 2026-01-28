"""Tests for plugin CLI command registration."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import typer

from graftpunk.plugins.cli_plugin import CommandSpec, ParamSpec, SitePlugin, command


class MockPlugin(SitePlugin):
    """Mock plugin for testing."""

    site_name = "mocksite"
    session_name = "mocksession"
    help_text = "Mock plugin for testing"

    @command(help="List items")
    def items(self, session: Any) -> dict[str, list[int]]:
        return {"items": [1, 2, 3]}

    @command(
        help="Get item by ID",
        params=[ParamSpec(name="item_id", param_type=int, required=True, is_option=False)],
    )
    def item(self, session: Any, item_id: int) -> dict[str, int]:
        return {"id": item_id}


class TestYAMLSitePlugin:
    """Tests for YAMLSitePlugin adapter."""

    def test_requires_session_true(self, isolated_config: Path) -> None:
        """Test that session is required when session_name is set."""
        from graftpunk.plugins.yaml_loader import YAMLCommandDef, YAMLPluginDef
        from graftpunk.plugins.yaml_plugin import YAMLSitePlugin

        plugin_def = YAMLPluginDef(
            site_name="test",
            session_name="testsession",
            help_text="Test",
            base_url="",
            headers={},
            commands=[
                YAMLCommandDef(name="cmd", help_text="", method="GET", url="/test", params=[])
            ],
        )
        plugin = YAMLSitePlugin(plugin_def)
        assert plugin.requires_session is True

    def test_requires_session_false(self, isolated_config: Path) -> None:
        """Test that session is not required when session_name is empty."""
        from graftpunk.plugins.yaml_loader import YAMLCommandDef, YAMLPluginDef
        from graftpunk.plugins.yaml_plugin import YAMLSitePlugin

        plugin_def = YAMLPluginDef(
            site_name="test",
            session_name="",  # empty = no session required
            help_text="Test",
            base_url="",
            headers={},
            commands=[
                YAMLCommandDef(name="cmd", help_text="", method="GET", url="/test", params=[])
            ],
        )
        plugin = YAMLSitePlugin(plugin_def)
        assert plugin.requires_session is False

    def test_get_commands(self, isolated_config: Path) -> None:
        """Test that get_commands returns CommandSpec objects."""
        from graftpunk.plugins.yaml_loader import YAMLCommandDef, YAMLPluginDef
        from graftpunk.plugins.yaml_plugin import YAMLSitePlugin

        plugin_def = YAMLPluginDef(
            site_name="test",
            session_name="",
            help_text="Test",
            base_url="",
            headers={},
            commands=[
                YAMLCommandDef(
                    name="list",
                    help_text="List things",
                    method="GET",
                    url="/things",
                    params=[],
                ),
                YAMLCommandDef(
                    name="get",
                    help_text="Get thing",
                    method="GET",
                    url="/things/{id}",
                    params=[
                        {
                            "name": "id",
                            "type": "int",
                            "required": True,
                            "default": None,
                            "help": "ID",
                            "is_option": False,
                        }
                    ],
                ),
            ],
        )
        plugin = YAMLSitePlugin(plugin_def)
        commands = plugin.get_commands()

        assert len(commands) == 2
        assert "list" in commands
        assert "get" in commands

        # Check command spec
        get_cmd = commands["get"]
        assert get_cmd.name == "get"
        assert get_cmd.help_text == "Get thing"
        assert len(get_cmd.params) == 1
        assert get_cmd.params[0].name == "id"
        assert get_cmd.params[0].param_type is int


class TestPluginRegistration:
    """Tests for plugin command registration."""

    def test_register_no_plugins(self, isolated_config: Path) -> None:
        """Test registration with no plugins."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        app = typer.Typer()

        with (
            patch("graftpunk.cli.plugin_commands.discover_cli_plugins") as mock_py,
            patch("graftpunk.cli.plugin_commands.create_yaml_plugins") as mock_yaml,
        ):
            mock_py.return_value = {}
            mock_yaml.return_value = ([], [])
            registered = register_plugin_commands(app, notify_errors=False)

        assert registered == {}

    def test_register_python_plugin(self, isolated_config: Path) -> None:
        """Test registering a Python plugin."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        app = typer.Typer()

        with (
            patch("graftpunk.cli.plugin_commands.discover_cli_plugins") as mock_py,
            patch("graftpunk.cli.plugin_commands.create_yaml_plugins") as mock_yaml,
        ):
            mock_py.return_value = {"mock": MockPlugin}
            mock_yaml.return_value = ([], [])
            registered = register_plugin_commands(app, notify_errors=False)

        assert "mocksite" in registered
        assert registered["mocksite"] == "Mock plugin for testing"

    def test_register_yaml_plugin(self, isolated_config: Path) -> None:
        """Test registering a YAML plugin."""
        from graftpunk.cli.plugin_commands import register_plugin_commands
        from graftpunk.plugins.yaml_loader import YAMLCommandDef, YAMLPluginDef
        from graftpunk.plugins.yaml_plugin import YAMLSitePlugin

        app = typer.Typer()

        yaml_plugin = YAMLSitePlugin(
            YAMLPluginDef(
                site_name="yamltest",
                session_name="",
                help_text="YAML test plugin",
                base_url="https://api.test.com",
                headers={},
                commands=[
                    YAMLCommandDef(
                        name="test",
                        help_text="Test command",
                        method="GET",
                        url="/test",
                        params=[],
                    )
                ],
            )
        )

        with (
            patch("graftpunk.cli.plugin_commands.discover_cli_plugins") as mock_py,
            patch("graftpunk.cli.plugin_commands.create_yaml_plugins") as mock_yaml,
        ):
            mock_py.return_value = {}
            mock_yaml.return_value = ([yaml_plugin], [])
            registered = register_plugin_commands(app, notify_errors=False)

        assert "yamltest" in registered

    def test_duplicate_site_names(self, isolated_config: Path) -> None:
        """Test that duplicate site names are handled (first wins)."""
        from graftpunk.cli.plugin_commands import register_plugin_commands
        from graftpunk.plugins.yaml_loader import YAMLCommandDef, YAMLPluginDef
        from graftpunk.plugins.yaml_plugin import YAMLSitePlugin

        app = typer.Typer()

        # Create two plugins with same site_name
        yaml_plugin = YAMLSitePlugin(
            YAMLPluginDef(
                site_name="mocksite",  # Same as MockPlugin
                session_name="",
                help_text="YAML version",
                base_url="",
                headers={},
                commands=[
                    YAMLCommandDef(name="cmd", help_text="", method="GET", url="/", params=[])
                ],
            )
        )

        with (
            patch("graftpunk.cli.plugin_commands.discover_cli_plugins") as mock_py,
            patch("graftpunk.cli.plugin_commands.create_yaml_plugins") as mock_yaml,
        ):
            mock_py.return_value = {"mock": MockPlugin}
            mock_yaml.return_value = ([yaml_plugin], [])
            registered = register_plugin_commands(app, notify_errors=False)

        # Only one should be registered (Python plugin first)
        assert len(registered) == 1
        assert "mocksite" in registered
        assert registered["mocksite"] == "Mock plugin for testing"

    def test_inject_plugin_commands(self, isolated_config: Path) -> None:
        """Test that inject_plugin_commands adds registered plugins to Click group."""
        import click

        from graftpunk.cli.plugin_commands import (
            _plugin_groups,
            inject_plugin_commands,
            register_plugin_commands,
        )

        app = typer.Typer()

        # Clear any previously registered plugins
        _plugin_groups.clear()

        with (
            patch("graftpunk.cli.plugin_commands.discover_cli_plugins") as mock_py,
            patch("graftpunk.cli.plugin_commands.create_yaml_plugins") as mock_yaml,
        ):
            mock_py.return_value = {"mock": MockPlugin}
            mock_yaml.return_value = ([], [])
            register_plugin_commands(app, notify_errors=False)

        # Create a Click group and inject plugins
        click_group = click.Group(name="test")
        inject_plugin_commands(click_group)

        # Verify plugin was added to the Click group
        assert "mocksite" in click_group.commands
        plugin_group = click_group.commands["mocksite"]
        assert isinstance(plugin_group, click.Group)
        assert "items" in plugin_group.commands
        assert "item" in plugin_group.commands


class TestCommandExecution:
    """Tests for command execution."""

    def test_command_with_session(self, isolated_config: Path) -> None:
        """Test command execution with mocked session."""
        from graftpunk.cli.plugin_commands import _create_click_command

        mock_session = MagicMock()
        mock_plugin = MockPlugin()
        mock_plugin.get_session = MagicMock(  # type: ignore[method-assign]
            return_value=mock_session
        )

        cmd_spec = CommandSpec(
            name="test",
            handler=lambda session: {"result": "success"},
            help_text="Test command",
            params=[],
        )

        click_cmd = _create_click_command(mock_plugin, cmd_spec)
        assert click_cmd.name == "test"
        assert click_cmd.help == "Test command"

    def test_command_with_params(self, isolated_config: Path) -> None:
        """Test command with parameters."""
        from graftpunk.cli.plugin_commands import _create_click_command
        from graftpunk.plugins.cli_plugin import ParamSpec

        mock_plugin = MockPlugin()

        cmd_spec = CommandSpec(
            name="get",
            handler=lambda session, item_id: {"id": item_id},
            help_text="Get item",
            params=[ParamSpec(name="item_id", param_type=int, required=True, is_option=False)],
        )

        click_cmd = _create_click_command(mock_plugin, cmd_spec)

        # Check params were added
        param_names = [p.name for p in click_cmd.params]
        assert "item_id" in param_names
        assert "format" in param_names  # --format option always added


class TestCreateYamlPlugins:
    """Tests for create_yaml_plugins function."""

    def test_create_from_discovered(self, isolated_config: Path) -> None:
        """Test creating plugins from discovered YAML files."""
        from graftpunk.plugins.yaml_plugin import create_yaml_plugins

        # Create a YAML plugin file
        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()

        yaml_content = """
site_name: testplugin
session_name: ""
commands:
  ping:
    url: "/ping"
"""
        (plugins_dir / "test.yaml").write_text(yaml_content)

        plugins, errors = create_yaml_plugins()

        assert len(plugins) == 1
        assert plugins[0].site_name == "testplugin"
        assert plugins[0].requires_session is False
        assert errors == []

    def test_create_returns_errors_for_invalid_files(self, isolated_config: Path) -> None:
        """Test that errors are returned for invalid YAML files."""
        from graftpunk.plugins.yaml_plugin import create_yaml_plugins

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()

        # Valid plugin
        (plugins_dir / "valid.yaml").write_text(
            "site_name: valid\nsession_name: ''\ncommands:\n  cmd:\n    url: /a"
        )
        # Invalid plugin (missing commands)
        (plugins_dir / "invalid.yaml").write_text("site_name: invalid")

        plugins, errors = create_yaml_plugins()

        assert len(plugins) == 1
        assert plugins[0].site_name == "valid"
        assert len(errors) == 1
        assert "invalid.yaml" in str(errors[0].filepath)


class TestPluginDiscoveryErrors:
    """Tests for plugin discovery error handling and notifications."""

    def test_plugin_discovery_error_dataclass(self) -> None:
        """Test PluginDiscoveryError dataclass."""
        from graftpunk.cli.plugin_commands import PluginDiscoveryError

        error = PluginDiscoveryError(
            plugin_name="test-plugin",
            error="Failed to load",
            phase="instantiation",
        )
        assert error.plugin_name == "test-plugin"
        assert error.error == "Failed to load"
        assert error.phase == "instantiation"

    def test_plugin_discovery_result_dataclass(self) -> None:
        """Test PluginDiscoveryResult dataclass."""
        from graftpunk.cli.plugin_commands import PluginDiscoveryError, PluginDiscoveryResult

        result = PluginDiscoveryResult()
        assert result.registered == {}
        assert result.errors == []
        assert result.has_errors is False

        result.add_error("plugin", "error message", "discovery")
        assert result.has_errors is True
        assert len(result.errors) == 1
        assert isinstance(result.errors[0], PluginDiscoveryError)

    def test_failed_plugin_instantiation_collected(self, isolated_config: Path) -> None:
        """Test that failed plugin instantiation is collected as error."""
        from graftpunk.cli.plugin_commands import register_plugin_commands
        from graftpunk.exceptions import PluginError

        class FailingPlugin:
            def __init__(self) -> None:
                raise PluginError("Test error")

        app = typer.Typer()

        with (
            patch("graftpunk.cli.plugin_commands.discover_cli_plugins") as mock_py,
            patch("graftpunk.cli.plugin_commands.create_yaml_plugins") as mock_yaml,
            patch("graftpunk.cli.plugin_commands._notify_plugin_errors") as mock_notify,
        ):
            mock_py.return_value = {"failing": FailingPlugin}
            mock_yaml.return_value = ([], [])
            registered = register_plugin_commands(app, notify_errors=True)

        # Plugin should not be registered
        assert "failing" not in registered

        # Error notification should be called
        mock_notify.assert_called_once()
        result = mock_notify.call_args[0][0]
        assert result.has_errors is True
        assert any("failing" in e.plugin_name for e in result.errors)

    def test_notify_errors_disabled(self, isolated_config: Path) -> None:
        """Test that notify_errors=False suppresses error output."""
        from graftpunk.cli.plugin_commands import register_plugin_commands
        from graftpunk.exceptions import PluginError

        class FailingPlugin:
            def __init__(self) -> None:
                raise PluginError("Test error")

        app = typer.Typer()

        with (
            patch("graftpunk.cli.plugin_commands.discover_cli_plugins") as mock_py,
            patch("graftpunk.cli.plugin_commands.create_yaml_plugins") as mock_yaml,
            patch("graftpunk.cli.plugin_commands._notify_plugin_errors") as mock_notify,
        ):
            mock_py.return_value = {"failing": FailingPlugin}
            mock_yaml.return_value = ([], [])
            register_plugin_commands(app, notify_errors=False)

        # Error notification should NOT be called
        mock_notify.assert_not_called()

    def test_notify_plugin_errors_truncates_output(self) -> None:
        """Test that error notification truncates to first 3 errors."""
        from graftpunk.cli.plugin_commands import (
            PluginDiscoveryResult,
            _notify_plugin_errors,
        )

        result = PluginDiscoveryResult()
        for i in range(5):
            result.add_error(f"plugin{i}", f"error{i}", "discovery")

        with patch("graftpunk.cli.plugin_commands.console") as mock_console:
            _notify_plugin_errors(result)

            calls = mock_console.print.call_args_list
            # First call should show total count
            assert "5 plugin(s) failed" in str(calls[0])
            # Should show exactly 3 detailed errors (calls 1, 2, 3)
            error_calls = [c for c in calls[1:4] if "plugin" in str(c)]
            assert len(error_calls) == 3
            # Last call should show "... and N more"
            assert "2 more" in str(calls[-1])

    def test_notify_plugin_errors_no_truncation_for_few(self) -> None:
        """Test that 3 or fewer errors are shown without truncation."""
        from graftpunk.cli.plugin_commands import (
            PluginDiscoveryResult,
            _notify_plugin_errors,
        )

        result = PluginDiscoveryResult()
        result.add_error("plugin1", "error1", "discovery")
        result.add_error("plugin2", "error2", "discovery")

        with patch("graftpunk.cli.plugin_commands.console") as mock_console:
            _notify_plugin_errors(result)

            calls = mock_console.print.call_args_list
            # Should show count + 2 errors = 3 calls total
            assert len(calls) == 3
            # Should NOT show "more" message
            assert not any("more" in str(c) for c in calls)

    def test_discovery_phase_failure_collected(self, isolated_config: Path) -> None:
        """Test that discovery-level failures are collected as errors."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        app = typer.Typer()

        with (
            patch("graftpunk.cli.plugin_commands.discover_cli_plugins") as mock_py,
            patch("graftpunk.cli.plugin_commands.create_yaml_plugins") as mock_yaml,
            patch("graftpunk.cli.plugin_commands._notify_plugin_errors") as mock_notify,
        ):
            mock_py.side_effect = RuntimeError("Discovery failed")
            mock_yaml.return_value = ([], [])
            registered = register_plugin_commands(app, notify_errors=True)

        assert registered == {}
        mock_notify.assert_called_once()
        result = mock_notify.call_args[0][0]
        assert any(e.phase == "discovery" for e in result.errors)
        assert any("Discovery failed" in e.error for e in result.errors)

    def test_unexpected_exception_during_instantiation(self, isolated_config: Path) -> None:
        """Test that unexpected exceptions (not PluginError) are collected."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        class UnexpectedlyFailingPlugin:
            def __init__(self) -> None:
                raise RuntimeError("Unexpected error")

        app = typer.Typer()

        with (
            patch("graftpunk.cli.plugin_commands.discover_cli_plugins") as mock_py,
            patch("graftpunk.cli.plugin_commands.create_yaml_plugins") as mock_yaml,
            patch("graftpunk.cli.plugin_commands._notify_plugin_errors") as mock_notify,
        ):
            mock_py.return_value = {"unexpected": UnexpectedlyFailingPlugin}
            mock_yaml.return_value = ([], [])
            registered = register_plugin_commands(app, notify_errors=True)

        assert "unexpected" not in registered
        mock_notify.assert_called_once()
        result = mock_notify.call_args[0][0]
        assert any("Unexpected error" in e.error for e in result.errors)


class TestPythonPluginDiscovery:
    """Tests for Python plugin auto-discovery integration."""

    def test_register_python_plugin_from_file(self, isolated_config: Path) -> None:
        """Test registering a Python plugin discovered from file."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        # Create a plugin file in the config plugins directory
        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()

        plugin_code = """
from graftpunk.plugins import SitePlugin, command

class FilePlugin(SitePlugin):
    site_name = "fileplugin"
    session_name = ""
    help_text = "Plugin from file"

    @command(help="Test command")
    def test(self, session):
        return {"test": True}
"""
        (plugins_dir / "file_plugin.py").write_text(plugin_code)

        app = typer.Typer()
        registered = register_plugin_commands(app, notify_errors=False)

        assert "fileplugin" in registered
        assert registered["fileplugin"] == "Plugin from file"
