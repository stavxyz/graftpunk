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
            mock_yaml.return_value = []
            registered = register_plugin_commands(app)

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
            mock_yaml.return_value = []
            registered = register_plugin_commands(app)

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
            mock_yaml.return_value = [yaml_plugin]
            registered = register_plugin_commands(app)

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
            mock_yaml.return_value = [yaml_plugin]
            registered = register_plugin_commands(app)

        # Only one should be registered (Python plugin first)
        assert len(registered) == 1
        assert "mocksite" in registered
        assert registered["mocksite"] == "Mock plugin for testing"


class TestCommandExecution:
    """Tests for command execution."""

    def test_command_with_session(self, isolated_config: Path) -> None:
        """Test command execution with mocked session."""
        from graftpunk.cli.plugin_commands import _create_click_command

        mock_session = MagicMock()
        mock_plugin = MockPlugin()
        mock_plugin.get_session = MagicMock(return_value=mock_session)  # type: ignore[method-assign]

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

        plugins = create_yaml_plugins()

        assert len(plugins) == 1
        assert plugins[0].site_name == "testplugin"
        assert plugins[0].requires_session is False
