"""Tests for plugin CLI command registration."""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests
import typer
from typer.testing import CliRunner as TyperCliRunner

from graftpunk.cli.command_factory import synthesize_command_fn
from graftpunk.cli.plugin_runtime import run_plugin_command
from graftpunk.exceptions import CommandError, PluginError
from graftpunk.plugins.cli_plugin import (
    CommandContext,
    CommandSpec,
    LoginConfig,
    LoginStep,
    PluginParamSpec,
    SitePlugin,
    command,
)

DISCOVER_ALL = "graftpunk.cli.plugin_commands.discover_all_plugins"


def build_command_app(plugin, cmd_spec):
    """Test helper for PARSE→BODY FORWARDING tests only.

    Registration-level behavior (help text, hidden/deprecated/epilog, group
    nesting, collisions) must be tested through the production wiring
    (`_build_site_app` with a single-command plugin), NOT this helper —
    otherwise the suite exercises a test-local copy of the wiring and stays
    green while production drifts.

    NOTE: the returned app always has exactly one registered command and no
    registered groups/callback, so Typer's `get_command()` collapses it to a
    bare `Command` (not a `Group`) -- invoke with `TyperCliRunner().invoke(app,
    [*args])`, WITHOUT the command name (passing the name raises "Got
    unexpected extra argument").
    """

    def body(ctx: typer.Context, **kwargs) -> None:
        run_plugin_command(plugin, cmd_spec, ctx, **kwargs)

    fn = synthesize_command_fn(
        name=cmd_spec.name,
        param_specs=list(cmd_spec.params),
        body=body,
        plugin_name=plugin.site_name,
    )
    app = typer.Typer()
    app.command(name=cmd_spec.name)(fn)
    return app


def _site_plugin_with_commands(site_name: str, specs: list) -> SitePlugin:
    """Build a minimal, login-less SitePlugin whose get_commands() is fixed.

    Used by registration-level tests (_build_site_app / register_plugin_commands)
    that need precise control over CommandSpec.group paths without going
    through the @command decorator's group-class discovery machinery.
    """

    class _Plugin(SitePlugin):
        pass

    _Plugin.site_name = site_name
    _Plugin.session_name = site_name
    _Plugin.help_text = "Test"
    plugin = _Plugin()
    plugin.get_commands = lambda: specs  # type: ignore[method-assign]
    return plugin


class MockPlugin(SitePlugin):
    """Mock plugin for testing."""

    site_name = "mocksite"
    session_name = "mocksession"
    help_text = "Mock plugin for testing"

    @command(help="List items")
    def items(self, ctx: Any) -> dict[str, list[int]]:
        return {"items": [1, 2, 3]}

    @command(
        help="Get item by ID",
        params=[PluginParamSpec.argument("item_id", type=int, required=True)],
    )
    def item(self, ctx: Any, item_id: int) -> dict[str, int]:
        return {"id": item_id}


class TestYAMLPluginFactory:
    """Tests for YAML-based SitePlugin instances created via create_yaml_site_plugin."""

    def test_requires_session_true(self, isolated_config: Path) -> None:
        """Test that session is required when session_name is set."""
        from graftpunk.plugins.cli_plugin import build_plugin_config
        from graftpunk.plugins.yaml_loader import YAMLCommandDef
        from graftpunk.plugins.yaml_plugin import create_yaml_site_plugin

        config = build_plugin_config(site_name="test", session_name="testsession", help_text="Test")
        commands = [YAMLCommandDef(name="cmd", help_text="", method="GET", url="/test", params=())]
        plugin = create_yaml_site_plugin(config, commands)
        assert plugin.requires_session is True

    def test_requires_session_false(self, isolated_config: Path) -> None:
        """Test that session is not required when requires_session is False."""
        from graftpunk.plugins.cli_plugin import build_plugin_config
        from graftpunk.plugins.yaml_loader import YAMLCommandDef
        from graftpunk.plugins.yaml_plugin import create_yaml_site_plugin

        config = build_plugin_config(
            site_name="test", session_name="test", help_text="Test", requires_session=False
        )
        commands = [YAMLCommandDef(name="cmd", help_text="", method="GET", url="/test", params=())]
        plugin = create_yaml_site_plugin(config, commands)
        assert plugin.requires_session is False

    def test_get_commands(self, isolated_config: Path) -> None:
        """Test that get_commands returns CommandSpec objects."""
        from graftpunk.plugins.cli_plugin import build_plugin_config
        from graftpunk.plugins.yaml_loader import YAMLCommandDef, YAMLParamDef
        from graftpunk.plugins.yaml_plugin import create_yaml_site_plugin

        config = build_plugin_config(site_name="test", session_name="test", help_text="Test")
        commands = [
            YAMLCommandDef(
                name="list",
                help_text="List things",
                method="GET",
                url="/things",
                params=(),
            ),
            YAMLCommandDef(
                name="get",
                help_text="Get thing",
                method="GET",
                url="/things/{id}",
                params=(
                    YAMLParamDef(
                        name="id",
                        type="int",
                        required=True,
                        default=None,
                        help="ID",
                        is_option=False,
                    ),
                ),
            ),
        ]
        plugin = create_yaml_site_plugin(config, commands)
        result = {c.name: c for c in plugin.get_commands()}

        assert len(result) == 2
        assert "list" in result
        assert "get" in result

        # Check command spec
        get_cmd = result["get"]
        assert get_cmd.name == "get"
        assert get_cmd.help_text == "Get thing"
        assert len(get_cmd.params) == 1
        assert get_cmd.params[0].name == "id"
        assert get_cmd.params[0].click_kwargs["type"] is int


class TestPluginRegistration:
    """Tests for plugin command registration."""

    def test_register_no_plugins(self, isolated_config: Path) -> None:
        """Test registration with no plugins."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        app = typer.Typer()

        with patch(DISCOVER_ALL, return_value=()):
            registered = register_plugin_commands(app, notify_errors=False)

        assert registered == {}

    def test_register_python_plugin(self, isolated_config: Path) -> None:
        """Test registering a Python plugin."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        app = typer.Typer()

        with patch(DISCOVER_ALL, return_value=(MockPlugin(),)):
            registered = register_plugin_commands(app, notify_errors=False)

        assert "mocksite" in registered
        assert registered["mocksite"] == "Mock plugin for testing"

    def test_register_yaml_plugin(self, isolated_config: Path) -> None:
        """Test registering a YAML plugin."""
        from graftpunk.cli.plugin_commands import register_plugin_commands
        from graftpunk.plugins.cli_plugin import build_plugin_config
        from graftpunk.plugins.yaml_loader import YAMLCommandDef
        from graftpunk.plugins.yaml_plugin import create_yaml_site_plugin

        app = typer.Typer()

        config = build_plugin_config(
            site_name="yamltest",
            help_text="YAML test plugin",
            base_url="https://api.test.com",
        )
        cmds = [
            YAMLCommandDef(
                name="test", help_text="Test command", method="GET", url="/test", params=()
            )
        ]
        yaml_plugin = create_yaml_site_plugin(config, cmds)

        with patch(DISCOVER_ALL, return_value=(yaml_plugin,)):
            registered = register_plugin_commands(app, notify_errors=False)

        assert "yamltest" in registered

    def test_duplicate_site_names_raises_plugin_error(self, isolated_config: Path) -> None:
        """Test that duplicate site names raise PluginError."""
        from graftpunk.cli.plugin_commands import register_plugin_commands
        from graftpunk.plugins.cli_plugin import build_plugin_config
        from graftpunk.plugins.yaml_loader import YAMLCommandDef
        from graftpunk.plugins.yaml_plugin import create_yaml_site_plugin

        app = typer.Typer()

        # Create YAML plugin with same site_name as MockPlugin
        config = build_plugin_config(
            site_name="mocksite",
            help_text="YAML version",
        )
        yaml_plugin = create_yaml_site_plugin(
            config,
            [YAMLCommandDef(name="cmd", help_text="", method="GET", url="/", params=())],
        )

        with (
            patch(DISCOVER_ALL, return_value=(MockPlugin(), yaml_plugin)),
            pytest.raises(PluginError, match="Plugin name collision.*mocksite"),
        ):
            register_plugin_commands(app, notify_errors=False)

    def test_register_populates_session_map(self, isolated_config: Path) -> None:
        """Test that register_plugin_commands populates _plugin_session_map."""
        from graftpunk.cli.plugin_commands import (
            _plugin_session_map,
            register_plugin_commands,
            resolve_session_name,
        )

        class HNPlugin(SitePlugin):
            site_name = "hn"
            session_name = "hackernews"
            help_text = "HN plugin"

            @command(help="List items")
            def items(self, ctx: Any) -> dict[str, list[int]]:
                return {"items": []}

        app = typer.Typer()

        with patch(DISCOVER_ALL, return_value=(HNPlugin(),)):
            register_plugin_commands(app, notify_errors=False)

        try:
            assert resolve_session_name("hn") == "hackernews"
            assert resolve_session_name("unknown") == "unknown"
        finally:
            _plugin_session_map.pop("hn", None)

    def test_plugin_groups_registered_on_app(self, isolated_config: Path) -> None:
        """Test that plugin groups are registered on the Typer app as sub-apps."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        app = typer.Typer()

        with patch(DISCOVER_ALL, return_value=(MockPlugin(),)):
            register_plugin_commands(app, notify_errors=False)

        # Verify plugin group was mounted on the app
        group_names = [g.name for g in app.registered_groups]
        assert "mocksite" in group_names
        plugin_group = next(g for g in app.registered_groups if g.name == "mocksite")
        cmd_names = [c.name for c in plugin_group.typer_instance.registered_commands]
        assert "items" in cmd_names
        assert "item" in cmd_names


class TestCommandExecution:
    """Tests for command execution."""

    def test_command_with_session(self, isolated_config: Path) -> None:
        """Test command execution with mocked session."""
        mock_session = MagicMock()
        mock_plugin = MockPlugin()
        mock_plugin.get_session = MagicMock(  # type: ignore[method-assign]
            return_value=mock_session
        )

        cmd_spec = CommandSpec(
            name="test",
            handler=lambda ctx: {"result": "success"},
            click_kwargs={"help": "Test command"},
            params=(),
        )

        app = build_command_app(mock_plugin, cmd_spec)
        assert app.registered_commands[0].name == "test"

        # build_command_app is parse->body forwarding only (no help_text) --
        # help text is a registration-level concern, covered by
        # TestClickKwargsPassthrough through _build_site_app. Here, verify
        # the mocked session is actually used to execute the command.
        result = TyperCliRunner().invoke(app, [])
        assert result.exit_code == 0
        mock_plugin.get_session.assert_called_once()

    def test_command_with_params(self, isolated_config: Path) -> None:
        """Test command with parameters."""
        import typer.main

        from graftpunk.plugins.cli_plugin import PluginParamSpec

        mock_plugin = MockPlugin()

        cmd_spec = CommandSpec(
            name="get",
            handler=lambda ctx, item_id: {"id": item_id},
            click_kwargs={"help": "Get item"},
            params=(PluginParamSpec.argument("item_id", type=int),),
        )

        app = build_command_app(mock_plugin, cmd_spec)
        click_cmd = typer.main.get_command(app)

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
requires_session: false
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

    def test_failed_plugin_filtered_by_discovery(self, isolated_config: Path) -> None:
        """Test that failed plugins are filtered out by discover_all_plugins."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        # discover_all_plugins filters out bad plugins, returning empty tuple
        app = typer.Typer()

        with patch(DISCOVER_ALL, return_value=()):
            registered = register_plugin_commands(app, notify_errors=True)

        # No plugins should be registered
        assert registered == {}

    def test_notify_errors_disabled(self, isolated_config: Path) -> None:
        """Test that notify_errors=False suppresses error output."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        app = typer.Typer()

        with (
            patch(DISCOVER_ALL, return_value=()),
            patch("graftpunk.cli.plugin_commands._notify_plugin_errors") as mock_notify,
        ):
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

        with patch("graftpunk.cli.plugin_commands.gp_console") as mock_console:
            _notify_plugin_errors(result)

            # First call: warn with total count
            mock_console.warn.assert_called_once()
            assert "5 plugin(s) failed" in str(mock_console.warn.call_args)

            # info calls: 3 detailed errors + 1 "... and N more"
            info_calls = mock_console.info.call_args_list
            assert len(info_calls) == 4
            # First 3 info calls are plugin errors
            error_calls = [c for c in info_calls[:3] if "plugin" in str(c)]
            assert len(error_calls) == 3
            # Last info call shows "... and N more"
            assert "2 more" in str(info_calls[-1])

    def test_notify_plugin_errors_no_truncation_for_few(self) -> None:
        """Test that 3 or fewer errors are shown without truncation."""
        from graftpunk.cli.plugin_commands import (
            PluginDiscoveryResult,
            _notify_plugin_errors,
        )

        result = PluginDiscoveryResult()
        result.add_error("plugin1", "error1", "discovery")
        result.add_error("plugin2", "error2", "discovery")

        with patch("graftpunk.cli.plugin_commands.gp_console") as mock_console:
            _notify_plugin_errors(result)

            # 1 warn call for the header
            mock_console.warn.assert_called_once()
            # 2 info calls for the errors, no "more" message
            info_calls = mock_console.info.call_args_list
            assert len(info_calls) == 2
            assert not any("more" in str(c) for c in info_calls)

    def test_discovery_failure_results_in_no_plugins(self, isolated_config: Path) -> None:
        """Test that discovery failures (handled by discover_all_plugins) result in empty."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        app = typer.Typer()

        # discover_all_plugins handles failures internally and returns empty
        with patch(DISCOVER_ALL, return_value=()):
            registered = register_plugin_commands(app, notify_errors=True)

        assert registered == {}

    def test_discovery_returns_only_valid_plugins(self, isolated_config: Path) -> None:
        """Test that only valid plugins from discover_all_plugins are registered."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        app = typer.Typer()

        # discover_all_plugins filters out bad plugins, only returns valid ones
        with patch(DISCOVER_ALL, return_value=(MockPlugin(),)):
            registered = register_plugin_commands(app, notify_errors=True)

        assert "mocksite" in registered

    def test_empty_discovery_no_errors(self, isolated_config: Path) -> None:
        """Test that empty discovery does not produce errors."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        app = typer.Typer()

        with (
            patch(DISCOVER_ALL, return_value=()),
            patch("graftpunk.cli.plugin_commands._notify_plugin_errors") as mock_notify,
        ):
            registered = register_plugin_commands(app, notify_errors=True)

        assert registered == {}
        # Notify is called but result has no errors
        mock_notify.assert_called_once()
        result = mock_notify.call_args[0][0]
        assert result.has_errors is False


class TestDeclarativeLogin:
    """Tests for declarative login spec on SitePlugin."""

    def test_declarative_attrs_exist(self) -> None:
        """Test SitePlugin has login attribute and backend."""
        assert hasattr(SitePlugin, "backend")
        assert hasattr(SitePlugin, "login_config")

    def test_declarative_attrs_defaults(self) -> None:
        """Test declarative login attributes default to None/selenium."""
        assert SitePlugin.backend == "selenium"
        assert SitePlugin.login_config is None

    def test_declarative_login_detected(self) -> None:
        """Test that a plugin with LoginConfig is detected."""
        from graftpunk.plugins.cli_plugin import has_declarative_login

        class DeclarativePlugin(SitePlugin):
            site_name = "decl"
            session_name = "decl"
            help_text = "Test"
            base_url = "https://example.com"
            backend = "nodriver"
            login_config = LoginConfig(
                steps=[
                    LoginStep(
                        fields={"username": "#user", "password": "#pass"},
                        submit="#submit",
                    )
                ],
                url="/login",
                failure="Bad login.",
            )

        plugin = DeclarativePlugin()
        assert has_declarative_login(plugin) is True

    def test_declarative_login_not_detected_without_fields(self) -> None:
        """Test that plugin without login is not declarative."""
        from graftpunk.plugins.cli_plugin import has_declarative_login

        class NoFieldsPlugin(SitePlugin):
            site_name = "nofields"
            session_name = "nofields"
            help_text = "Test"

        plugin = NoFieldsPlugin()
        assert has_declarative_login(plugin) is False


class TestAutoLoginCommand:
    """Tests for auto-generated login command feature."""

    def test_has_login_method_true(self) -> None:
        """Test detection of plugins with login method."""
        from graftpunk.cli.login_commands import has_login_method

        class PluginWithLogin(SitePlugin):
            site_name = "testlogin"
            session_name = "test"
            help_text = "Test"

            def login(self, credentials: dict[str, str]) -> bool:
                return True

        plugin = PluginWithLogin()
        assert has_login_method(plugin) is True

    def test_has_login_method_false_no_method(self) -> None:
        """Test plugins without login method return False."""
        from graftpunk.cli.login_commands import has_login_method

        class PluginWithoutLogin(SitePlugin):
            site_name = "nologin"
            session_name = "test"
            help_text = "Test"

        plugin = PluginWithoutLogin()
        assert has_login_method(plugin) is False

    def test_has_login_method_false_decorated_command(self) -> None:
        """Test that login decorated with @command is not auto-added."""
        from graftpunk.cli.login_commands import has_login_method

        class PluginWithCommandLogin(SitePlugin):
            site_name = "cmdlogin"
            session_name = "test"
            help_text = "Test"

            @command(help="Login command")
            def login(self, ctx: Any) -> dict[str, bool]:
                return {"success": True}

        plugin = PluginWithCommandLogin()
        assert has_login_method(plugin) is False

    def test_create_login_fn(self) -> None:
        """Test creation of login command with correct structure."""
        import inspect

        from graftpunk.cli.login_commands import create_login_fn

        class PluginWithLogin(SitePlugin):
            site_name = "testlogin"
            session_name = "test"
            help_text = "Test"

            def login(self, credentials: dict[str, str]) -> bool:
                """Log in to testlogin site."""
                return True

        plugin = PluginWithLogin()
        fn = create_login_fn(plugin, plugin.login, {"username": "", "password": ""})

        assert fn.__name__ == "login"
        assert "Log in to testlogin site" in fn.__doc__

        # No params beyond ctx -- credentials are gathered via prompts/envvars
        # at runtime (include_builtin_options=False, zero param_specs).
        params = [p for p in inspect.signature(fn).parameters if p != "ctx"]
        assert params == []

    def test_login_command_envvar_resolution(self) -> None:
        """Test that environment variables are resolved at runtime."""
        import os

        import typer

        from graftpunk.cli.login_commands import create_login_fn

        class PluginWithLogin(SitePlugin):
            site_name = "my-test-site"
            session_name = "test"
            help_text = "Test"

            def login(self, credentials: dict[str, str]) -> bool:
                return credentials.get("username") == "envuser"

        plugin = PluginWithLogin()
        fn = create_login_fn(plugin, plugin.login, {"username": "", "password": ""})

        with (
            patch("graftpunk.cli.plugin_commands.gp_console"),
            patch("graftpunk.cli.login_commands.Status"),
            patch.dict(
                os.environ,
                {"MY_TEST_SITE_USERNAME": "envuser", "MY_TEST_SITE_PASSWORD": "envpass"},
            ),
        ):
            app = typer.Typer()
            app.command(name="login")(fn)
            runner = TyperCliRunner()
            result = runner.invoke(app, [])
            assert result.exit_code == 0

    def test_login_command_envvar_override(self) -> None:
        """Test that plugins can override login envvar names."""
        import os

        import typer

        from graftpunk.cli.login_commands import create_login_fn

        class PluginWithCustomEnvvars(SitePlugin):
            site_name = "mybank"
            session_name = "mybank"
            help_text = "Test"
            username_envvar = "MYBANK_USER"
            password_envvar = "MYBANK_PASS"  # noqa: S105

            def login(self, credentials: dict[str, str]) -> bool:
                return credentials.get("username") == "bankuser"

        plugin = PluginWithCustomEnvvars()
        fn = create_login_fn(plugin, plugin.login, {"username": "", "password": ""})

        with (
            patch("graftpunk.cli.plugin_commands.gp_console"),
            patch("graftpunk.cli.login_commands.Status"),
            patch.dict(
                os.environ,
                {"MYBANK_USER": "bankuser", "MYBANK_PASS": "bankpass"},
            ),
        ):
            app = typer.Typer()
            app.command(name="login")(fn)
            runner = TyperCliRunner()
            result = runner.invoke(app, [])
            assert result.exit_code == 0

    def test_login_command_registered_for_plugin(self, isolated_config: Path) -> None:
        """Test that login command is auto-registered for plugins with login method."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        class PluginWithLogin(SitePlugin):
            site_name = "autologin"
            session_name = "test"
            help_text = "Test"

            def login(self, credentials: dict[str, str]) -> bool:
                return True

            @command(help="List items")
            def items(self, ctx: Any) -> dict[str, list[int]]:
                return {"items": []}

        app = typer.Typer()

        with patch(DISCOVER_ALL, return_value=(PluginWithLogin(),)):
            registered = register_plugin_commands(app, notify_errors=False)

        assert "autologin" in registered

        plugin_group = next(g for g in app.registered_groups if g.name == "autologin")
        cmd_names = [c.name for c in plugin_group.typer_instance.registered_commands]
        assert "login" in cmd_names
        assert "items" in cmd_names

        # Drive through register_plugin_commands (production wiring) and
        # confirm the login command is actually invocable under the site
        # sub-app (not just present in the registry).
        runner = TyperCliRunner()
        help_result = runner.invoke(app, ["autologin", "login", "--help"])
        assert help_result.exit_code == 0


class TestLoginCommandOutput:
    """Tests for Rich login command output."""

    def test_login_success_uses_console_success(self) -> None:
        """Test that successful login uses console.success."""
        import os

        import typer

        from graftpunk.cli.login_commands import create_login_fn

        class PluginWithLogin(SitePlugin):
            site_name = "richtest"
            session_name = "test"
            help_text = "Test"

            def login(self, credentials: dict[str, str]) -> bool:
                return True

        plugin = PluginWithLogin()
        fn = create_login_fn(plugin, plugin.login, {"username": "", "password": ""})

        with (
            patch("graftpunk.cli.login_commands.gp_console") as mock_console,
            patch("graftpunk.cli.login_commands.Status"),
            patch.dict(os.environ, {"RICHTEST_USERNAME": "u", "RICHTEST_PASSWORD": "p"}),
        ):
            app = typer.Typer()
            app.command(name="login")(fn)
            runner = TyperCliRunner()
            runner.invoke(app, [])
            mock_console.success.assert_called_once()

    def test_login_failure_uses_console_error(self) -> None:
        """Test that failed login uses console.error and exits with code 1."""
        import os

        import typer

        from graftpunk.cli.login_commands import create_login_fn

        class PluginWithLogin(SitePlugin):
            site_name = "richtest"
            session_name = "test"
            help_text = "Test"

            def login(self, credentials: dict[str, str]) -> bool:
                return False

        plugin = PluginWithLogin()
        fn = create_login_fn(plugin, plugin.login, {"username": "", "password": ""})

        with (
            patch("graftpunk.cli.login_commands.gp_console") as mock_console,
            patch("graftpunk.cli.login_commands.Status"),
            patch.dict(os.environ, {"RICHTEST_USERNAME": "u", "RICHTEST_PASSWORD": "p"}),
        ):
            app = typer.Typer()
            app.command(name="login")(fn)
            runner = TyperCliRunner()
            result = runner.invoke(app, [])
            mock_console.error.assert_called_once()
            assert result.exit_code == 1

    def test_login_exception_uses_console_error(self) -> None:
        """Test that login exception uses console.error."""
        import os

        import typer

        from graftpunk.cli.login_commands import create_login_fn

        class PluginWithLogin(SitePlugin):
            site_name = "richtest"
            session_name = "test"
            help_text = "Test"

            def login(self, credentials: dict[str, str]) -> bool:
                raise RuntimeError("Connection failed")

        plugin = PluginWithLogin()
        fn = create_login_fn(plugin, plugin.login, {"username": "", "password": ""})

        with (
            patch("graftpunk.cli.login_commands.gp_console") as mock_console,
            patch("graftpunk.cli.login_commands.Status"),
            patch.dict(os.environ, {"RICHTEST_USERNAME": "u", "RICHTEST_PASSWORD": "p"}),
        ):
            app = typer.Typer()
            app.command(name="login")(fn)
            runner = TyperCliRunner()
            runner.invoke(app, [])
            mock_console.error.assert_called_once()


class TestCommandOutputConsole:
    """Tests for command execution using console module."""

    def test_session_not_found_uses_console_error(self, isolated_config: Path) -> None:
        """Test that session-not-found uses console.error."""
        from graftpunk.exceptions import SessionNotFoundError

        mock_plugin = MockPlugin()
        mock_plugin.get_session = MagicMock(  # type: ignore[method-assign]
            side_effect=SessionNotFoundError("mocksession")
        )

        cmd_spec = CommandSpec(
            name="test",
            handler=lambda ctx: {"ok": True},
            click_kwargs={"help": "Test"},
            params=(),
        )
        app = build_command_app(mock_plugin, cmd_spec)

        with patch("graftpunk.cli.plugin_runtime.gp_console") as mock_con:
            runner = TyperCliRunner()
            runner.invoke(app, [])
            mock_con.error.assert_called_once()


class TestDeclarativeLoginRegistration:
    """Tests for auto-generating login from declarative attributes."""

    def test_declarative_plugin_gets_login_command(self, isolated_config: Path) -> None:
        """Test that declarative login plugin gets auto-generated login command."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        class DeclPlugin(SitePlugin):
            site_name = "decltest"
            session_name = "decltest"
            help_text = "Declarative test"
            base_url = "https://example.com"
            backend = "selenium"
            login_config = LoginConfig(
                steps=[
                    LoginStep(
                        fields={"username": "#user", "password": "#pass"},
                        submit="#submit",
                    )
                ],
                url="/login",
                failure="Bad login.",
            )

            @command(help="List items")
            def items(self, ctx: Any) -> dict[str, list[int]]:
                return {"items": []}

        app = typer.Typer()

        with patch(DISCOVER_ALL, return_value=(DeclPlugin(),)):
            registered = register_plugin_commands(app, notify_errors=False)

        assert "decltest" in registered

        plugin_group = next(g for g in app.registered_groups if g.name == "decltest")
        cmd_names = [c.name for c in plugin_group.typer_instance.registered_commands]
        assert "login" in cmd_names
        assert "items" in cmd_names

    def test_explicit_login_overrides_declarative(self, isolated_config: Path) -> None:
        """Test that an explicit login() method takes precedence over declarative."""
        from graftpunk.cli.login_commands import has_login_method

        class PluginWithBoth(SitePlugin):
            site_name = "both"
            session_name = "both"
            help_text = "Test"
            base_url = "https://example.com"
            login_config = LoginConfig(
                steps=[
                    LoginStep(
                        fields={"username": "#user", "password": "#pass"},
                        submit="#submit",
                    )
                ],
                url="/login",
            )

            def login(self, credentials: dict[str, str]) -> bool:
                return True

        plugin = PluginWithBoth()
        # The explicit login() should be detected by _has_login_method
        assert has_login_method(plugin) is True


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
    def test(self, ctx):
        return {"test": True}
"""
        (plugins_dir / "file_plugin.py").write_text(plugin_code)

        app = typer.Typer()
        registered = register_plugin_commands(app, notify_errors=False)

        assert "fileplugin" in registered
        assert registered["fileplugin"] == "Plugin from file"


class TestBrowserSessionContextManager:
    """Tests for browser_session() context manager escape hatch."""

    @pytest.mark.asyncio
    async def test_browser_session_async(self) -> None:
        """Test async browser_session context manager."""

        class AsyncPlugin(SitePlugin):
            site_name = "asynctest"
            session_name = "asynctest"
            help_text = "Test"
            base_url = "https://example.com"
            backend = "nodriver"

        plugin = AsyncPlugin()

        with (
            patch("graftpunk.BrowserSession") as mock_bs,
            patch("graftpunk.cache_session"),
        ):
            instance = mock_bs.return_value
            instance.start_async = AsyncMock()
            instance.driver = MagicMock()
            mock_tab = AsyncMock()
            instance.driver.get = AsyncMock(return_value=mock_tab)
            instance.transfer_nodriver_cookies_to_session = AsyncMock()
            instance.driver.stop = MagicMock()

            async with plugin.browser_session() as (session, tab):
                assert session is instance
                assert tab is mock_tab

            # Verify cleanup happened
            instance.transfer_nodriver_cookies_to_session.assert_awaited_once()

    def test_browser_session_sync(self) -> None:
        """Test sync browser_session_sync context manager."""

        class SyncPlugin(SitePlugin):
            site_name = "synctest"
            session_name = "synctest"
            help_text = "Test"
            base_url = "https://example.com"
            backend = "selenium"

        plugin = SyncPlugin()

        with (
            patch("graftpunk.BrowserSession") as mock_bs,
            patch("graftpunk.cache_session"),
        ):
            instance = mock_bs.return_value
            instance.driver = MagicMock()
            instance.transfer_driver_cookies_to_session = MagicMock()
            instance.quit = MagicMock()

            with plugin.browser_session_sync() as (session, driver):
                assert session is instance
                assert driver is instance.driver

            # Verify cleanup happened
            instance.transfer_driver_cookies_to_session.assert_called_once()


class TestAsyncLoginCommand:
    """Tests for async login method handling in login command."""

    def test_async_login_method_invoked(self) -> None:
        """Test that async login methods are run via asyncio.run."""
        import os

        import typer

        from graftpunk.cli.login_commands import create_login_fn

        class AsyncLoginPlugin(SitePlugin):
            site_name = "asynclogin"
            session_name = "test"
            help_text = "Test"

            async def login(self, credentials: dict[str, str]) -> bool:
                return True

        plugin = AsyncLoginPlugin()
        # Wrap the real async login method with a MagicMock so calling it
        # does not produce an unawaited coroutine (asyncio is mocked, so
        # the coroutine would never be awaited).
        mock_login = MagicMock(return_value=True)
        fn = create_login_fn(plugin, mock_login, {"username": "", "password": ""})

        with (
            patch("graftpunk.cli.login_commands.gp_console") as mock_console,
            patch("graftpunk.cli.login_commands.Status"),
            patch("graftpunk.cli.login_commands.asyncio") as mock_asyncio,
            patch.dict(os.environ, {"ASYNCLOGIN_USERNAME": "u", "ASYNCLOGIN_PASSWORD": "p"}),
        ):
            mock_asyncio.iscoroutinefunction.return_value = True
            mock_asyncio.run.return_value = True

            app = typer.Typer()
            app.command(name="login")(fn)
            runner = TyperCliRunner()
            result = runner.invoke(app, [])

            mock_asyncio.run.assert_called_once()
            mock_console.success.assert_called_once()
            assert result.exit_code == 0


class TestYAMLLoginValidation:
    """Tests for YAML login block validation with steps."""

    def test_login_block_missing_steps_raises(self, isolated_config: Path) -> None:
        """Test that login block without steps raises PluginError."""
        from graftpunk.plugins.yaml_loader import parse_yaml_plugin

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()
        yaml_file = plugins_dir / "bad_login.yaml"
        yaml_file.write_text(
            "site_name: test\ncommands:\n  cmd:\n    url: /a\nlogin:\n  url: /login\n"
        )

        with pytest.raises(Exception, match="missing required 'steps' field"):
            parse_yaml_plugin(yaml_file)

    def test_login_block_empty_steps_raises(self, isolated_config: Path) -> None:
        """Test that login block with empty steps raises PluginError."""
        from graftpunk.plugins.yaml_loader import parse_yaml_plugin

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()
        yaml_file = plugins_dir / "bad_login.yaml"
        yaml_file.write_text(
            "site_name: test\ncommands:\n  cmd:\n    url: /a\nlogin:\n  url: /login\n  steps: []\n"
        )

        with pytest.raises(Exception, match="must contain at least one step"):
            parse_yaml_plugin(yaml_file)

    def test_login_step_invalid_raises(self, isolated_config: Path) -> None:
        """Test that invalid step (no fields and no submit) raises PluginError."""
        from graftpunk.plugins.yaml_loader import parse_yaml_plugin

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()
        yaml_file = plugins_dir / "bad_login.yaml"
        yaml_file.write_text(
            "site_name: test\ncommands:\n  cmd:\n    url: /a\n"
            "login:\n  url: /login\n  steps:\n    - wait_for: '#form'\n"
        )

        with pytest.raises(Exception, match="step #1 is invalid"):
            parse_yaml_plugin(yaml_file)

    def test_valid_login_block_parses(self, isolated_config: Path) -> None:
        """Test that a complete login block parses successfully."""
        from graftpunk.plugins.yaml_loader import parse_yaml_plugin

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()
        yaml_file = plugins_dir / "good_login.yaml"
        yaml_file.write_text(
            "site_name: test\nsession_name: test\nbase_url: https://example.com\n"
            "commands:\n  cmd:\n    url: /a\n"
            "login:\n  url: /login\n  failure: 'Bad login'\n"
            "  steps:\n    - fields:\n        username: '#u'\n        password: '#p'\n"
            "      submit: '#s'\n"
        )

        config, commands, headers = parse_yaml_plugin(yaml_file)
        assert config.login_config is not None
        assert config.login_config.url == "/login"
        assert len(config.login_config.steps) == 1
        assert config.login_config.steps[0].fields == {"username": "#u", "password": "#p"}
        assert config.login_config.steps[0].submit == "#s"
        assert config.login_config.failure == "Bad login"

    def test_non_dict_login_block_raises(self, isolated_config: Path) -> None:
        """Test that non-dict login block raises PluginError."""
        from graftpunk.plugins.yaml_loader import parse_yaml_plugin

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()
        yaml_file = plugins_dir / "bad_login_type.yaml"
        yaml_file.write_text("site_name: test\ncommands:\n  cmd:\n    url: /a\nlogin: true\n")

        with pytest.raises(Exception, match="must be a mapping.*not bool"):
            parse_yaml_plugin(yaml_file)


class TestMutableLoginFieldsProtection:
    """Tests for __init_subclass__ LoginConfig isolation."""

    def test_subclass_without_login_gets_none(self) -> None:
        """Test subclass without login config gets None."""

        class PluginA(SitePlugin):
            site_name = "a"

        class PluginB(SitePlugin):
            site_name = "b"

        assert PluginA.login_config is None
        assert PluginB.login_config is None

    def test_subclass_with_login_gets_own_instance(self) -> None:
        """Test subclass that sets login gets its own LoginConfig."""

        class PluginC(SitePlugin):
            site_name = "c"
            login_config = LoginConfig(
                steps=[
                    LoginStep(
                        fields={"username": "#u", "password": "#p"},
                        submit="#btn",
                    )
                ],
                url="/login",
            )

        assert PluginC.login_config is not None
        assert PluginC.login_config.steps[0].fields == {"username": "#u", "password": "#p"}


class TestResolveSessionName:
    """Tests for resolve_session_name alias resolution."""

    def test_resolves_site_name_to_session_name(self) -> None:
        """Test that a registered plugin's site_name resolves to its session_name."""
        from graftpunk.cli.plugin_commands import _plugin_session_map, resolve_session_name

        _plugin_session_map["hn"] = "hackernews"
        try:
            assert resolve_session_name("hn") == "hackernews"
        finally:
            _plugin_session_map.pop("hn", None)

    def test_returns_name_unchanged_when_no_mapping(self) -> None:
        """Test that unknown names pass through unchanged."""
        from graftpunk.cli.plugin_commands import resolve_session_name

        assert resolve_session_name("some-session") == "some-session"

    def test_direct_session_name_still_works(self) -> None:
        """Test that using a session_name directly still works (no double-mapping)."""
        from graftpunk.cli.plugin_commands import _plugin_session_map, resolve_session_name

        _plugin_session_map["hn"] = "hackernews"
        try:
            # Using the session_name directly should pass through
            assert resolve_session_name("hackernews") == "hackernews"
        finally:
            _plugin_session_map.pop("hn", None)


class TestInferSiteName:
    """Tests for infer_site_name URL-to-name utility."""

    def test_full_url(self) -> None:
        """Test inference from a full URL."""
        from graftpunk.plugins import infer_site_name

        assert infer_site_name("https://httpbin.org") == "httpbin"

    def test_strips_www(self) -> None:
        """Test that www prefix is stripped."""
        from graftpunk.plugins import infer_site_name

        assert infer_site_name("https://www.example.com") == "example"

    def test_strips_api(self) -> None:
        """Test that api prefix is stripped."""
        from graftpunk.plugins import infer_site_name

        assert infer_site_name("https://api.myservice.com") == "myservice"

    def test_bare_domain(self) -> None:
        """Test inference from a bare domain (no scheme)."""
        from graftpunk.plugins import infer_site_name

        assert infer_site_name("github.com") == "github"

    def test_hyphen_to_underscore(self) -> None:
        """Test that hyphens are replaced with underscores."""
        from graftpunk.plugins import infer_site_name

        assert infer_site_name("https://my-cool-site.com") == "my_cool_site"

    def test_empty_string(self) -> None:
        """Test that empty string returns empty string."""
        from graftpunk.plugins import infer_site_name

        assert infer_site_name("") == ""

    def test_subdomain(self) -> None:
        """Test with non-standard subdomain."""
        from graftpunk.plugins import infer_site_name

        assert infer_site_name("https://news.ycombinator.com") == "ycombinator"


class TestSiteNameAutoInference:
    """Tests for auto-inference of site_name from base_url."""

    def test_python_plugin_infers_site_name(self) -> None:
        """Test that SitePlugin subclass infers site_name from base_url."""

        class AutoPlugin(SitePlugin):
            base_url = "https://httpbin.org"

        assert AutoPlugin.site_name == "httpbin"

    def test_python_plugin_explicit_site_name_preserved(self) -> None:
        """Test that explicit site_name is not overridden by inference."""

        class ExplicitPlugin(SitePlugin):
            site_name = "myname"
            base_url = "https://httpbin.org"

        assert ExplicitPlugin.site_name == "myname"

    def test_python_plugin_no_base_url_no_inference(self) -> None:
        """Test that missing base_url does not crash."""

        class NoUrlPlugin(SitePlugin):
            pass

        assert NoUrlPlugin.site_name == ""

    def test_yaml_plugin_infers_site_name(self, isolated_config: Path) -> None:
        """Test that YAML plugin infers site_name from base_url."""
        from graftpunk.plugins.yaml_loader import parse_yaml_plugin

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir(exist_ok=True)
        yaml_file = plugins_dir / "test.yaml"
        yaml_file.write_text(
            'base_url: "https://httpbin.org"\ncommands:\n  ping:\n    url: "/ping"\n'
        )

        config, commands, headers = parse_yaml_plugin(yaml_file)
        assert config.site_name == "httpbin"

    def test_yaml_plugin_explicit_site_name_preserved(self, isolated_config: Path) -> None:
        """Test that explicit site_name in YAML is not overridden."""
        from graftpunk.plugins.yaml_loader import parse_yaml_plugin

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir(exist_ok=True)
        yaml_file = plugins_dir / "test.yaml"
        yaml_file.write_text(
            "site_name: myname\n"
            'base_url: "https://httpbin.org"\n'
            "commands:\n"
            "  ping:\n"
            '    url: "/ping"\n'
        )

        config, commands, headers = parse_yaml_plugin(yaml_file)
        assert config.site_name == "myname"

    def test_yaml_plugin_no_site_name_no_base_url_infers_from_filename(
        self, isolated_config: Path
    ) -> None:
        """Test that missing site_name and base_url infers site_name from filename."""
        from graftpunk.plugins.yaml_loader import parse_yaml_plugin

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir(exist_ok=True)
        yaml_file = plugins_dir / "myapi.yaml"
        yaml_file.write_text("commands:\n  ping:\n    url: /ping\n")

        config, commands, _headers = parse_yaml_plugin(yaml_file)
        assert config.site_name == "myapi"


class TestPythonLoaderSysModulesCleanup:
    """Tests for sys.modules cleanup on failed module load."""

    def test_failed_exec_cleans_sys_modules(self, isolated_config: Path) -> None:
        """Test that failed exec_module removes module from sys.modules."""
        import sys

        from graftpunk.plugins.python_loader import _load_module_from_file

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()

        bad_plugin = plugins_dir / "bad_syntax.py"
        bad_plugin.write_text("raise RuntimeError('broken at import')")

        module_name = f"graftpunk_plugin_{bad_plugin.stem}"
        assert module_name not in sys.modules

        with pytest.raises(RuntimeError, match="broken at import"):
            _load_module_from_file(bad_plugin)

        # Module should be cleaned up from sys.modules
        assert module_name not in sys.modules


class TestSitePluginConfigIntegration:
    """Tests that SitePlugin.__init_subclass__ uses build_plugin_config."""

    def test_session_name_defaults_to_site_name(self) -> None:
        """Python plugins get session_name defaulted to site_name."""

        class MyPlugin(SitePlugin):
            site_name = "mysite"

        assert MyPlugin.session_name == "mysite"

    def test_help_text_auto_generated(self) -> None:
        """Python plugins get help_text auto-generated."""

        class MyPlugin(SitePlugin):
            site_name = "mysite"

        assert MyPlugin.help_text == "Commands for mysite"

    def test_explicit_session_name_not_overridden(self) -> None:
        """Explicit session_name is preserved."""

        class MyPlugin(SitePlugin):
            site_name = "hn"
            session_name = "hackernews"

        assert MyPlugin.session_name == "hackernews"

    def test_explicit_help_text_not_overridden(self) -> None:
        """Explicit help_text is preserved."""

        class MyPlugin(SitePlugin):
            site_name = "mysite"
            help_text = "Custom help"

        assert MyPlugin.help_text == "Custom help"

    def test_plugin_config_stored(self) -> None:
        """_plugin_config is stored on the class."""
        from graftpunk.plugins.cli_plugin import PluginConfig

        class MyPlugin(SitePlugin):
            site_name = "mysite"

        assert isinstance(MyPlugin._plugin_config, PluginConfig)
        assert MyPlugin._plugin_config.site_name == "mysite"

    def test_site_name_inferred_from_base_url(self) -> None:
        """site_name inferred from base_url (existing behavior preserved)."""

        class MyPlugin(SitePlugin):
            base_url = "https://httpbin.org"

        assert MyPlugin.site_name == "httpbin"
        assert MyPlugin.session_name == "httpbin"
        assert MyPlugin.help_text == "Commands for httpbin"


class TestBuildPluginConfig:
    """Tests for build_plugin_config shared factory."""

    def test_minimal_config(self) -> None:
        """site_name is the only truly required field."""
        from graftpunk.plugins.cli_plugin import PluginConfig, build_plugin_config

        config = build_plugin_config(site_name="mysite")
        assert isinstance(config, PluginConfig)
        assert config.site_name == "mysite"
        assert config.session_name == "mysite"  # defaults to site_name
        assert config.help_text == "Commands for mysite"

    def test_explicit_session_name_preserved(self) -> None:
        """Explicit session_name is not overridden."""
        from graftpunk.plugins.cli_plugin import build_plugin_config

        config = build_plugin_config(site_name="hn", session_name="hackernews")
        assert config.session_name == "hackernews"

    def test_explicit_help_text_preserved(self) -> None:
        """Explicit help_text is not overridden."""
        from graftpunk.plugins.cli_plugin import build_plugin_config

        config = build_plugin_config(site_name="mysite", help_text="Custom help")
        assert config.help_text == "Custom help"

    def test_site_name_inferred_from_base_url(self) -> None:
        """site_name is inferred from base_url when not provided."""
        from graftpunk.plugins.cli_plugin import build_plugin_config

        config = build_plugin_config(base_url="https://httpbin.org")
        assert config.site_name == "httpbin"

    def test_missing_site_name_and_base_url_raises(self) -> None:
        """Raises PluginError when site_name cannot be determined."""
        from graftpunk.plugins.cli_plugin import build_plugin_config

        with pytest.raises(PluginError, match="site_name"):
            build_plugin_config()

    def test_site_name_inferred_from_filename(self) -> None:
        """site_name falls back to filename stem when no site_name or base_url."""
        from pathlib import Path

        from graftpunk.plugins.cli_plugin import build_plugin_config

        config = build_plugin_config(source_filepath=Path("/plugins/httpbin.yaml"))
        assert config.site_name == "httpbin"
        assert config.session_name == "httpbin"

    def test_site_name_explicit_over_filename(self) -> None:
        """Explicit site_name takes precedence over filename."""
        from pathlib import Path

        from graftpunk.plugins.cli_plugin import build_plugin_config

        config = build_plugin_config(
            site_name="mysite", source_filepath=Path("/plugins/httpbin.yaml")
        )
        assert config.site_name == "mysite"

    def test_site_name_base_url_over_filename(self) -> None:
        """base_url inference takes precedence over filename."""
        from pathlib import Path

        from graftpunk.plugins.cli_plugin import build_plugin_config

        config = build_plugin_config(
            base_url="https://httpbin.org", source_filepath=Path("/plugins/other.yaml")
        )
        assert config.site_name == "httpbin"

    def test_unknown_kwargs_ignored(self) -> None:
        """Extra kwargs not in PluginConfig fields are silently ignored."""
        from graftpunk.plugins.cli_plugin import build_plugin_config

        config = build_plugin_config(site_name="mysite", commands=[], headers={}, unknown_field=42)
        assert config.site_name == "mysite"

    def test_requires_session_default_true(self) -> None:
        """requires_session defaults to True."""
        from graftpunk.plugins.cli_plugin import build_plugin_config

        config = build_plugin_config(site_name="mysite")
        assert config.requires_session is True

    def test_requires_session_explicit_false(self) -> None:
        """requires_session can be set to False."""
        from graftpunk.plugins.cli_plugin import build_plugin_config

        config = build_plugin_config(site_name="mysite", requires_session=False)
        assert config.requires_session is False

    def test_login_fields_preserved(self) -> None:
        """Login fields are stored on PluginConfig via LoginConfig."""
        from graftpunk.plugins.cli_plugin import build_plugin_config

        login = LoginConfig(
            steps=[
                LoginStep(
                    fields={"username": "#user", "password": "#pass"},
                    submit="#submit",
                )
            ],
            url="/login",
        )
        config = build_plugin_config(
            site_name="mysite",
            login_config=login,
        )
        assert config.login_config is not None
        assert config.login_config.url == "/login"
        assert config.login_config.steps[0].fields == {"username": "#user", "password": "#pass"}
        assert config.login_config.steps[0].submit == "#submit"

    def test_empty_site_name_string_raises(self) -> None:
        """Empty string site_name raises after inference attempt."""
        from graftpunk.plugins.cli_plugin import build_plugin_config

        with pytest.raises(PluginError, match="site_name"):
            build_plugin_config(site_name="")


class TestIntrospectParams:
    """Tests for _introspect_params method."""

    def test_typed_params_produce_correct_paramspec(self) -> None:
        """Method with typed params creates correct PluginParamSpec list."""

        class MyPlugin(SitePlugin):
            site_name = "test"
            session_name = "test"
            help_text = "Test"
            base_url = "https://example.com"
            requires_session = False

            def my_command(self, ctx: object, count: int, name: str) -> None:
                pass

        plugin = MyPlugin()
        params = plugin._introspect_params(plugin.my_command)
        assert len(params) == 2
        assert params[0].name == "count"
        assert params[0].click_kwargs["type"] is int
        assert params[0].click_kwargs["required"] is True
        assert params[1].name == "name"
        assert params[1].click_kwargs["type"] is str
        assert params[1].click_kwargs["required"] is True

    def test_params_with_defaults(self) -> None:
        """Method with defaults sets required=False and correct default."""

        class MyPlugin(SitePlugin):
            site_name = "test2"
            session_name = "test2"
            help_text = "Test"
            base_url = "https://example.com"
            requires_session = False

            def my_command(self, ctx: object, limit: int = 10, tag: str = "latest") -> None:
                pass

        plugin = MyPlugin()
        params = plugin._introspect_params(plugin.my_command)
        assert len(params) == 2
        assert params[0].name == "limit"
        assert params[0].click_kwargs["type"] is int
        assert params[0].click_kwargs["required"] is False
        assert params[0].click_kwargs["default"] == 10
        assert params[1].name == "tag"
        assert params[1].click_kwargs["default"] == "latest"

    def test_no_annotations_defaults_to_str(self) -> None:
        """Method without type annotations uses str as default type."""

        class MyPlugin(SitePlugin):
            site_name = "test3"
            session_name = "test3"
            help_text = "Test"
            base_url = "https://example.com"
            requires_session = False

            def my_command(self, ctx, query):
                pass

        plugin = MyPlugin()
        params = plugin._introspect_params(plugin.my_command)
        assert len(params) == 1
        assert params[0].name == "query"
        assert params[0].click_kwargs["type"] is str

    def test_only_self_and_ctx_returns_empty(self) -> None:
        """Method with only self and ctx returns empty params list."""

        class MyPlugin(SitePlugin):
            site_name = "test4"
            session_name = "test4"
            help_text = "Test"
            base_url = "https://example.com"
            requires_session = False

            def my_command(self, ctx: object) -> None:
                pass

        plugin = MyPlugin()
        params = plugin._introspect_params(plugin.my_command)
        assert len(params) == 0


class TestIntrospectParamsClickKwargs:
    """Tests for _introspect_params producing click_kwargs."""

    def test_typed_param_produces_click_kwargs(self) -> None:
        """Typed param introspection produces click_kwargs with type."""

        class FakePlugin(SitePlugin):
            site_name = "test-introspect-1"

            @command(help="test")
            def my_cmd(self, ctx: CommandContext, count: int) -> None: ...

        plugin = FakePlugin()
        cmds = plugin.get_commands()
        param = cmds[0].params[0]
        assert param.name == "count"
        assert param.is_option is True
        assert param.click_kwargs["type"] is int
        assert param.click_kwargs["required"] is True

    def test_default_value_in_click_kwargs(self) -> None:
        """Params with defaults populate click_kwargs."""

        class FakePlugin(SitePlugin):
            site_name = "test-introspect-2"

            @command(help="test")
            def my_cmd(self, ctx: CommandContext, limit: int = 10) -> None: ...

        plugin = FakePlugin()
        cmds = plugin.get_commands()
        param = cmds[0].params[0]
        assert param.click_kwargs["default"] == 10
        assert param.click_kwargs["required"] is False

    def test_bool_with_false_default_is_flag(self) -> None:
        """Bool param with default=False becomes a flag."""

        class FakePlugin(SitePlugin):
            site_name = "test-introspect-3"

            @command(help="test")
            def my_cmd(self, ctx: CommandContext, verbose: bool = False) -> None: ...

        plugin = FakePlugin()
        cmds = plugin.get_commands()
        param = cmds[0].params[0]
        assert param.click_kwargs.get("is_flag") is True

    def test_unannotated_defaults_to_str(self) -> None:
        """Params without type annotations default to str."""

        class FakePlugin(SitePlugin):
            site_name = "test-introspect-4"

            @command(help="test")
            def my_cmd(self, ctx: CommandContext, query="") -> None: ...  # noqa: ANN001

        plugin = FakePlugin()
        cmds = plugin.get_commands()
        param = cmds[0].params[0]
        assert param.click_kwargs["type"] is str


class TestBrowserSessionErrorPaths:
    """Tests for browser_session context managers on exception paths."""

    @pytest.mark.asyncio
    async def test_browser_session_async_no_cache_on_exception(self) -> None:
        """On exception, cookies should NOT be transferred and session NOT cached."""

        class TestPlugin(SitePlugin):
            site_name = "errtest"
            session_name = "errtest"
            help_text = "Test"
            base_url = "https://example.com"
            backend = "nodriver"

        plugin = TestPlugin()

        with (
            patch("graftpunk.BrowserSession") as mock_bs,
            patch("graftpunk.cache_session") as mock_cache,
        ):
            instance = mock_bs.return_value
            instance.start_async = AsyncMock()
            instance.driver = MagicMock()
            instance.driver.get = AsyncMock(return_value=MagicMock())
            instance.driver.stop = MagicMock()
            instance.transfer_nodriver_cookies_to_session = AsyncMock()

            with pytest.raises(ValueError, match="test error"):
                async with plugin.browser_session() as (session, tab):
                    raise ValueError("test error")

            # Cookies NOT transferred, session NOT cached
            instance.transfer_nodriver_cookies_to_session.assert_not_called()
            mock_cache.assert_not_called()
            # But cleanup DID happen
            instance.driver.stop.assert_called_once()

    def test_browser_session_sync_no_cache_on_exception(self) -> None:
        """On exception in sync context, cookies NOT transferred but cleanup happens."""

        class TestPlugin(SitePlugin):
            site_name = "errtest2"
            session_name = "errtest2"
            help_text = "Test"
            base_url = "https://example.com"
            backend = "selenium"

        plugin = TestPlugin()

        with (
            patch("graftpunk.BrowserSession") as mock_bs,
            patch("graftpunk.cache_session") as mock_cache,
        ):
            instance = mock_bs.return_value
            instance.driver = MagicMock()
            instance.transfer_driver_cookies_to_session = MagicMock()
            instance.quit = MagicMock()

            with (
                pytest.raises(ValueError, match="test error"),
                plugin.browser_session_sync() as (session, driver),
            ):
                raise ValueError("test error")

            instance.transfer_driver_cookies_to_session.assert_not_called()
            mock_cache.assert_not_called()
            instance.quit.assert_called_once()


class TestCreateYamlPluginsPartialFailure:
    """Tests for create_yaml_plugins per-plugin error handling."""

    def test_one_broken_plugin_does_not_block_others(self) -> None:
        """A single plugin creation failure should not prevent other plugins from loading."""
        from graftpunk.plugins.cli_plugin import build_plugin_config
        from graftpunk.plugins.yaml_loader import (
            YAMLCommandDef,
            YAMLDiscoveryResult,
            YAMLPluginBundle,
        )
        from graftpunk.plugins.yaml_plugin import create_yaml_plugins, create_yaml_site_plugin

        config1 = build_plugin_config(site_name="good1", base_url="https://good1.com")
        config2 = build_plugin_config(site_name="bad", base_url="https://bad.com")
        config3 = build_plugin_config(site_name="good2", base_url="https://good2.com")

        cmd = YAMLCommandDef(name="test", help_text="test", method="GET", url="/test")
        result = YAMLDiscoveryResult(
            plugins=[
                YAMLPluginBundle(config1, [cmd], {}),
                YAMLPluginBundle(config2, [cmd], {}),
                YAMLPluginBundle(config3, [cmd], {}),
            ]
        )

        call_count = 0
        original_create = create_yaml_site_plugin

        def mock_create(config, commands, headers=None):
            nonlocal call_count
            call_count += 1
            if config.site_name == "bad":
                raise PluginError("Simulated creation failure")
            return original_create(config, commands, headers)

        with (
            patch("graftpunk.plugins.yaml_plugin.discover_yaml_plugins", return_value=result),
            patch("graftpunk.plugins.yaml_plugin.create_yaml_site_plugin", side_effect=mock_create),
        ):
            plugins, errors = create_yaml_plugins()

        assert len(plugins) == 2  # good1 and good2 survived
        assert len(errors) == 1  # bad was recorded as error
        assert "bad" in errors[0].error or "Simulated" in errors[0].error


class TestSiteNameCollisionDetection:
    """Tests for site_name collision detection in register_plugin_commands."""

    def test_duplicate_site_name_raises_plugin_error(self, isolated_config: Path) -> None:
        """Two plugins with the same site_name should raise PluginError."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        class PluginA(SitePlugin):
            site_name = "samename"
            session_name = "a"
            help_text = "Plugin A"

            @command(help="A command")
            def cmd(self, ctx: Any) -> dict[str, str]:
                return {"source": "a"}

        class PluginB(SitePlugin):
            site_name = "samename"
            session_name = "b"
            help_text = "Plugin B"

            @command(help="B command")
            def cmd(self, ctx: Any) -> dict[str, str]:
                return {"source": "b"}

        app = typer.Typer()

        with (
            patch(DISCOVER_ALL, return_value=(PluginA(), PluginB())),
            pytest.raises(PluginError, match="Plugin name collision.*samename"),
        ):
            register_plugin_commands(app, notify_errors=False)

    def test_collision_error_message_includes_source(self, isolated_config: Path) -> None:
        """Error message should mention both plugin sources."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        class PluginX(SitePlugin):
            site_name = "collision"
            session_name = "x"
            help_text = "Plugin X"

            @command(help="X command")
            def cmd(self, ctx: Any) -> dict[str, str]:
                return {"source": "x"}

        class PluginY(SitePlugin):
            site_name = "collision"
            session_name = "y"
            help_text = "Plugin Y"

            @command(help="Y command")
            def cmd(self, ctx: Any) -> dict[str, str]:
                return {"source": "y"}

        app = typer.Typer()

        with (
            patch(DISCOVER_ALL, return_value=(PluginX(), PluginY())),
            pytest.raises(PluginError, match="already registered by.*New source:"),
        ):
            register_plugin_commands(app, notify_errors=False)

    def test_different_site_names_no_collision(self, isolated_config: Path) -> None:
        """Plugins with different site_names register fine."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        class PluginAlpha(SitePlugin):
            site_name = "alpha"
            session_name = "alpha"
            help_text = "Alpha plugin"

            @command(help="Alpha command")
            def cmd(self, ctx: Any) -> dict[str, str]:
                return {"source": "alpha"}

        class PluginBeta(SitePlugin):
            site_name = "beta"
            session_name = "beta"
            help_text = "Beta plugin"

            @command(help="Beta command")
            def cmd(self, ctx: Any) -> dict[str, str]:
                return {"source": "beta"}

        app = typer.Typer()

        with patch(DISCOVER_ALL, return_value=(PluginAlpha(), PluginBeta())):
            registered = register_plugin_commands(app, notify_errors=False)

        assert "alpha" in registered
        assert "beta" in registered
        assert len(registered) == 2

    def test_registered_plugin_sources_cleared_between_calls(self, isolated_config: Path) -> None:
        """_registered_plugin_sources is cleared at start of register_plugin_commands."""
        from graftpunk.cli.plugin_commands import (
            _registered_plugin_sources,
            register_plugin_commands,
        )

        app = typer.Typer()

        with patch(DISCOVER_ALL, return_value=(MockPlugin(),)):
            # First call registers mocksite
            register_plugin_commands(app, notify_errors=False)
            assert "mocksite" in _registered_plugin_sources

            # Second call should NOT raise collision -- dict is cleared
            register_plugin_commands(app, notify_errors=False)
            assert "mocksite" in _registered_plugin_sources


class TestPluginMounting:
    """Tests for register_plugin_commands mounting plugin sub-apps via app.add_typer.

    Supersedes the deleted GraftpunkApp tests:
    - ``test_add_plugin_group_duplicate_raises_value_error`` is superseded by
      ``TestPluginRegistration::test_duplicate_site_names_raises_plugin_error``
      -- site-name collisions are now detected in register_plugin_commands
      itself, there is no separate add_plugin_group() registry to guard.
    - ``test_plugin_groups_injected_into_click_app`` and
      ``test_usage_error_caught_and_exits_with_code_2`` tested
      GraftpunkApp.__call__'s custom machinery (typer.main.get_command()
      injection of a hand-built click.Group, and a bespoke
      click.UsageError -> gp_console.error + SystemExit(2) funnel). That
      machinery is gone: app.add_typer() mounts plugin sub-apps directly on
      a plain typer.Typer, and Typer/Click's own dispatch -- including its
      default usage-error handling -- takes over end to end.
    """

    def test_registered_plugin_command_is_invocable(self, isolated_config: Path) -> None:
        """A plugin mounted via register_plugin_commands is invocable end-to-end."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        plugin = MockPlugin()
        plugin.get_session = MagicMock(  # type: ignore[method-assign]
            return_value=requests.Session()
        )
        app = typer.Typer()

        with patch(DISCOVER_ALL, return_value=(plugin,)):
            register_plugin_commands(app, notify_errors=False)

        result = TyperCliRunner().invoke(app, ["mocksite", "items"])
        assert result.exit_code == 0

    def test_unknown_subcommand_exits_with_code_2(self, isolated_config: Path) -> None:
        """An unknown subcommand under a mounted plugin exits with code 2."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        app = typer.Typer()

        with patch(DISCOVER_ALL, return_value=(MockPlugin(),)):
            register_plugin_commands(app, notify_errors=False)

        result = TyperCliRunner().invoke(app, ["mocksite", "bogus"])
        assert result.exit_code == 2


class TestSynthesizedCommandCallback:
    """Tests for run_plugin_command's error paths, driven via a synthesized command."""

    def test_plugin_error_during_session_load_exits_1(self, isolated_config: Path) -> None:
        """PluginError during get_session exits with code 1."""
        mock_plugin = MockPlugin()
        mock_plugin.get_session = MagicMock(  # type: ignore[method-assign]
            side_effect=PluginError("session corrupted")
        )

        cmd_spec = CommandSpec(
            name="test",
            handler=lambda ctx: {"ok": True},
            click_kwargs={"help": "Test"},
            params=(),
        )
        app = build_command_app(mock_plugin, cmd_spec)

        with patch("graftpunk.cli.plugin_runtime.gp_console") as mock_console:
            runner = TyperCliRunner()
            result = runner.invoke(app, [])

            assert result.exit_code == 1
            mock_console.error.assert_called_once()
            assert "Plugin error" in str(mock_console.error.call_args)

    def test_generic_exception_during_session_load_exits_1_and_logs(
        self, isolated_config: Path
    ) -> None:
        """Generic Exception during get_session exits with code 1 and logs."""
        mock_plugin = MockPlugin()
        mock_plugin.get_session = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("unexpected db error")
        )

        cmd_spec = CommandSpec(
            name="test",
            handler=lambda ctx: {"ok": True},
            click_kwargs={"help": "Test"},
            params=(),
        )
        app = build_command_app(mock_plugin, cmd_spec)

        with (
            patch("graftpunk.cli.plugin_runtime.gp_console") as mock_console,
            patch("graftpunk.cli.plugin_runtime.LOG") as mock_log,
        ):
            runner = TyperCliRunner()
            result = runner.invoke(app, [])

            assert result.exit_code == 1
            mock_console.error.assert_called_once()
            assert "Failed to load session" in str(mock_console.error.call_args)
            mock_log.exception.assert_called_once()

    def test_plain_requests_session_sets_driver_none(self, isolated_config: Path) -> None:
        """A plain requests.Session (no .driver attr) should not crash the callback."""
        plain_session = requests.Session()
        mock_plugin = MockPlugin()
        mock_plugin.get_session = MagicMock(  # type: ignore[method-assign]
            return_value=plain_session
        )

        cmd_spec = CommandSpec(
            name="test",
            handler=lambda ctx, **kwargs: {"ok": True},
            click_kwargs={"help": "Test"},
            params=(),
        )
        app = build_command_app(mock_plugin, cmd_spec)

        with patch("graftpunk.cli.plugin_runtime.build_observe_context") as mock_build:
            runner = TyperCliRunner()
            result = runner.invoke(app, [])

            assert result.exit_code == 0
            # build_observe_context should have been called with driver=None
            mock_build.assert_called_once()
            _, kwargs = mock_build.call_args
            if not kwargs:
                args = mock_build.call_args[0]
                assert args[2] is None  # driver arg
            else:
                assert kwargs.get("driver") is None

    def test_handler_exception_exits_1(self, isolated_config: Path) -> None:
        """Exception raised by the command handler exits with code 1."""
        mock_session = MagicMock()
        mock_session.driver = MagicMock()
        mock_plugin = MockPlugin()
        mock_plugin.get_session = MagicMock(  # type: ignore[method-assign]
            return_value=mock_session
        )

        def failing_handler(ctx: Any, **kwargs: Any) -> None:
            raise RuntimeError("handler blew up")

        cmd_spec = CommandSpec(
            name="test",
            handler=failing_handler,
            click_kwargs={"help": "Test"},
            params=(),
        )
        app = build_command_app(mock_plugin, cmd_spec)

        with (
            patch("graftpunk.cli.plugin_runtime.gp_console") as mock_console,
            patch("graftpunk.cli.plugin_runtime.LOG") as mock_log,
            patch("graftpunk.cli.plugin_runtime.build_observe_context"),
        ):
            runner = TyperCliRunner()
            result = runner.invoke(app, [])

            assert result.exit_code == 1
            mock_console.error.assert_called_once()
            assert "Command failed" in str(mock_console.error.call_args)
            mock_log.exception.assert_called_once()


class TestLoginEmptyEnvvar:
    """Tests for login credential resolution with empty env vars."""

    def test_login_empty_envvar_falls_through_to_prompt(self) -> None:
        """When env var is set to empty string, prompt is shown instead."""
        import os

        import typer

        from graftpunk.cli.login_commands import create_login_fn

        class PluginWithLogin(SitePlugin):
            site_name = "emptyenv"
            session_name = "test"
            help_text = "Test"

            def login(self, credentials: dict[str, str]) -> bool:
                return True

        plugin = PluginWithLogin()
        fn = create_login_fn(plugin, plugin.login, {"username": "", "password": ""})

        with (
            patch("graftpunk.cli.plugin_commands.gp_console"),
            patch("graftpunk.cli.login_commands.Status"),
            patch.dict(
                os.environ,
                {"EMPTYENV_USERNAME": "", "EMPTYENV_PASSWORD": ""},
            ),
            patch("typer.prompt", return_value="prompted_value") as mock_prompt,
        ):
            app = typer.Typer()
            app.command(name="login")(fn)
            runner = TyperCliRunner()
            result = runner.invoke(app, [])
            assert result.exit_code == 0
            # typer.prompt should have been called for both fields since env vars were empty
            assert mock_prompt.call_count == 2


class TestIndividualCommandRegistrationFailure:
    """Tests for individual command registration failure handling."""

    def test_individual_command_registration_failure_collected(self, isolated_config: Path) -> None:
        """A single command registration failure doesn't block others."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        class TwoCommandPlugin(SitePlugin):
            site_name = "twocmd"
            session_name = "twocmd"
            help_text = "Two commands"

            @command(help="Good command")
            def good_cmd(self, ctx: Any) -> dict[str, str]:
                return {"status": "ok"}

            @command(help="Bad command")
            def bad_cmd(self, ctx: Any) -> dict[str, str]:
                return {"status": "bad"}

        app = typer.Typer()

        with (
            patch(DISCOVER_ALL, return_value=(TwoCommandPlugin(),)),
            patch("graftpunk.cli.plugin_commands._notify_plugin_errors") as mock_notify,
            patch(
                "graftpunk.cli.command_factory.synthesize_command_fn",
                side_effect=[RuntimeError("registration boom"), MagicMock()],
            ),
        ):
            registered = register_plugin_commands(app, notify_errors=True)

        # Plugin should still be registered (the second command succeeded)
        assert "twocmd" in registered

        # Error notification should have been called with the failure
        mock_notify.assert_called_once()
        result = mock_notify.call_args[0][0]
        assert result.has_errors is True
        assert any("twocmd" in e.plugin_name for e in result.errors)
        assert any("registration boom" in e.error for e in result.errors)


class TestLoginRegistrationFailure:
    """Tests for login command registration failure handling."""

    def test_login_registration_failure_collected(self, isolated_config: Path) -> None:
        """Login command registration failure doesn't block other commands."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        class DeclPluginWithCmd(SitePlugin):
            site_name = "loginboom"
            session_name = "loginboom"
            help_text = "Declarative login test"
            base_url = "https://example.com"
            backend = "selenium"
            login_config = LoginConfig(
                steps=[
                    LoginStep(
                        fields={"username": "#user", "password": "#pass"},
                        submit="#submit",
                    )
                ],
                url="/login",
                failure="Bad login.",
            )

            @command(help="List items")
            def items(self, ctx: Any) -> dict[str, list[int]]:
                return {"items": []}

        app = typer.Typer()

        with (
            patch(DISCOVER_ALL, return_value=(DeclPluginWithCmd(),)),
            patch("graftpunk.cli.plugin_commands._notify_plugin_errors") as mock_notify,
            patch(
                "graftpunk.cli.login_commands.generate_login_method",
                side_effect=RuntimeError("login generation failed"),
            ),
        ):
            registered = register_plugin_commands(app, notify_errors=True)

        # Plugin should still be registered (regular commands work)
        assert "loginboom" in registered

        # The plugin group should have the items command but not login
        plugin_group = next(g for g in app.registered_groups if g.name == "loginboom")
        cmd_names = [c.name for c in plugin_group.typer_instance.registered_commands]
        assert "items" in cmd_names
        assert "login" not in cmd_names

        # Error should be collected
        mock_notify.assert_called_once()
        result = mock_notify.call_args[0][0]
        assert result.has_errors is True
        assert any("loginboom.login" in e.plugin_name for e in result.errors)
        assert any("login generation failed" in e.error for e in result.errors)


class TestLifecycleHooks:
    """Tests for setup() and teardown() lifecycle hooks."""

    def test_setup_called_during_registration(self, isolated_config: Path) -> None:
        """setup() is called after a plugin's commands are registered."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        setup_called = False

        class SetupPlugin(SitePlugin):
            site_name = "setuptest"
            session_name = "setuptest"
            help_text = "Test"

            def setup(self) -> None:
                nonlocal setup_called
                setup_called = True

            @command(help="Test command")
            def cmd(self, ctx: Any) -> dict[str, str]:
                return {"ok": "yes"}

        app = typer.Typer()

        with patch(DISCOVER_ALL, return_value=(SetupPlugin(),)):
            registered = register_plugin_commands(app, notify_errors=False)

        assert setup_called is True
        assert "setuptest" in registered

    def test_setup_failure_skips_plugin_and_adds_error(self, isolated_config: Path) -> None:
        """If setup() raises, the plugin is skipped and error is recorded."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        class FailSetupPlugin(SitePlugin):
            site_name = "failsetup"
            session_name = "failsetup"
            help_text = "Test"

            def setup(self) -> None:
                raise RuntimeError("setup boom")

            @command(help="Test command")
            def cmd(self, ctx: Any) -> dict[str, str]:
                return {"ok": "yes"}

        app = typer.Typer()

        with (
            patch(DISCOVER_ALL, return_value=(FailSetupPlugin(),)),
            patch("graftpunk.cli.plugin_commands._notify_plugin_errors") as mock_notify,
        ):
            registered = register_plugin_commands(app, notify_errors=True)

        # Plugin should NOT be registered
        assert "failsetup" not in registered

        # Error should be collected
        mock_notify.assert_called_once()
        result = mock_notify.call_args[0][0]
        assert result.has_errors is True
        assert any("setup() failed" in e.error for e in result.errors)

    def test_teardown_called_for_all_plugins_in_reverse_order(self, isolated_config: Path) -> None:
        """teardown() is called in reverse registration order."""
        from graftpunk.cli.plugin_commands import (
            _registered_plugins_for_teardown,
            _teardown_all_plugins,
        )

        teardown_order: list[str] = []

        class PluginFirst(SitePlugin):
            site_name = "first"
            session_name = "first"
            help_text = "First"

            def teardown(self) -> None:
                teardown_order.append("first")

            @command(help="cmd")
            def cmd(self, ctx: Any) -> dict[str, str]:
                return {}

        class PluginSecond(SitePlugin):
            site_name = "second"
            session_name = "second"
            help_text = "Second"

            def teardown(self) -> None:
                teardown_order.append("second")

            @command(help="cmd")
            def cmd(self, ctx: Any) -> dict[str, str]:
                return {}

        # Simulate registration order
        _registered_plugins_for_teardown.clear()
        _registered_plugins_for_teardown.append(PluginFirst())
        _registered_plugins_for_teardown.append(PluginSecond())

        _teardown_all_plugins()

        assert teardown_order == ["second", "first"]
        _registered_plugins_for_teardown.clear()

    def test_teardown_failure_does_not_prevent_other_teardowns(self, isolated_config: Path) -> None:
        """If one teardown() raises, others still get called."""
        from graftpunk.cli.plugin_commands import (
            _registered_plugins_for_teardown,
            _teardown_all_plugins,
        )

        teardown_calls: list[str] = []

        class PluginOk1(SitePlugin):
            site_name = "ok1"
            session_name = "ok1"
            help_text = "Ok1"

            def teardown(self) -> None:
                teardown_calls.append("ok1")

            @command(help="cmd")
            def cmd(self, ctx: Any) -> dict[str, str]:
                return {}

        class PluginBad(SitePlugin):
            site_name = "bad"
            session_name = "bad"
            help_text = "Bad"

            def teardown(self) -> None:
                teardown_calls.append("bad")
                raise RuntimeError("teardown boom")

            @command(help="cmd")
            def cmd(self, ctx: Any) -> dict[str, str]:
                return {}

        class PluginOk2(SitePlugin):
            site_name = "ok2"
            session_name = "ok2"
            help_text = "Ok2"

            def teardown(self) -> None:
                teardown_calls.append("ok2")

            @command(help="cmd")
            def cmd(self, ctx: Any) -> dict[str, str]:
                return {}

        _registered_plugins_for_teardown.clear()
        _registered_plugins_for_teardown.append(PluginOk1())
        _registered_plugins_for_teardown.append(PluginBad())
        _registered_plugins_for_teardown.append(PluginOk2())

        # Should not raise
        _teardown_all_plugins()

        # All three should have been called (reversed order)
        assert teardown_calls == ["ok2", "bad", "ok1"]
        _registered_plugins_for_teardown.clear()

    def test_setup_failure_plugin_not_in_teardown_list(self, isolated_config: Path) -> None:
        """A plugin whose setup() fails should NOT be in the teardown list."""
        from graftpunk.cli.plugin_commands import (
            _registered_plugins_for_teardown,
            register_plugin_commands,
        )

        class FailSetup2(SitePlugin):
            site_name = "failsetup2"
            session_name = "failsetup2"
            help_text = "Test"

            def setup(self) -> None:
                raise RuntimeError("setup boom")

            @command(help="cmd")
            def cmd(self, ctx: Any) -> dict[str, str]:
                return {}

        app = typer.Typer()

        with patch(DISCOVER_ALL, return_value=(FailSetup2(),)):
            register_plugin_commands(app, notify_errors=False)

        assert all(p.site_name != "failsetup2" for p in _registered_plugins_for_teardown)


class TestAPIVersionCheck:
    """Tests for API version check via discover_all_plugins filtering."""

    def test_unsupported_api_version_filtered_by_discovery(self, isolated_config: Path) -> None:
        """Plugin with unsupported api_version is filtered by discover_all_plugins."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        # discover_all_plugins filters unsupported api_versions,
        # so register_plugin_commands never sees them
        app = typer.Typer()
        with patch(DISCOVER_ALL, return_value=()):
            registered = register_plugin_commands(app, notify_errors=False)

        assert "mocksite" not in registered


class TestCommandContextPopulation:
    """Tests for CommandContext has base_url and config populated."""

    def test_context_has_base_url_and_config(self, isolated_config: Path) -> None:
        """CommandContext receives base_url and _plugin_config from plugin."""
        captured_ctx: list[CommandContext] = []

        def capturing_handler(ctx: CommandContext, **kwargs: Any) -> dict[str, str]:
            captured_ctx.append(ctx)
            return {"ok": True}

        mock_plugin = MockPlugin()
        mock_plugin.base_url = "https://example.com"  # type: ignore[attr-defined]
        mock_plugin.get_session = MagicMock(  # type: ignore[method-assign]
            return_value=requests.Session()
        )

        cmd_spec = CommandSpec(
            name="test",
            handler=capturing_handler,
            click_kwargs={"help": "Test"},
            params=(),
        )
        app = build_command_app(mock_plugin, cmd_spec)

        runner = TyperCliRunner()
        result = runner.invoke(app, [])
        assert result.exit_code == 0
        assert len(captured_ctx) == 1
        assert captured_ctx[0].base_url == "https://example.com"
        assert captured_ctx[0].config is not None
        assert captured_ctx[0].config.site_name == "mocksite"


class TestCommandErrorCatch:
    """Tests for CommandError caught before PluginError."""

    def test_command_error_displays_user_message(self, isolated_config: Path) -> None:
        """CommandError shows user_message and exits 1."""

        def failing_handler(ctx: Any, **kwargs: Any) -> None:
            raise CommandError("Amount must be positive")

        mock_plugin = MockPlugin()
        mock_plugin.get_session = MagicMock(  # type: ignore[method-assign]
            return_value=requests.Session()
        )

        cmd_spec = CommandSpec(
            name="test",
            handler=failing_handler,
            click_kwargs={"help": "Test"},
            params=(),
        )
        app = build_command_app(mock_plugin, cmd_spec)

        with patch("graftpunk.cli.plugin_runtime.gp_console") as mock_console:
            runner = TyperCliRunner()
            result = runner.invoke(app, [])

            assert result.exit_code == 1
            mock_console.error.assert_called_once_with("Amount must be positive")

    def test_plugin_error_still_caught(self, isolated_config: Path) -> None:
        """PluginError (non-CommandError) still caught with 'Plugin error' prefix."""

        def failing_handler(ctx: Any, **kwargs: Any) -> None:
            raise PluginError("something broke")

        mock_plugin = MockPlugin()
        mock_plugin.get_session = MagicMock(  # type: ignore[method-assign]
            return_value=requests.Session()
        )

        cmd_spec = CommandSpec(
            name="test",
            handler=failing_handler,
            click_kwargs={"help": "Test"},
            params=(),
        )
        app = build_command_app(mock_plugin, cmd_spec)

        with patch("graftpunk.cli.plugin_runtime.gp_console") as mock_console:
            runner = TyperCliRunner()
            result = runner.invoke(app, [])

            assert result.exit_code == 1
            mock_console.error.assert_called_once()
            assert "Plugin error" in str(mock_console.error.call_args)


class TestPerCommandRequiresSession:
    """Tests for per-command requires_session override."""

    def test_requires_session_false_gets_plain_session(self, isolated_config: Path) -> None:
        """Command with requires_session=False uses plain requests.Session."""
        captured_sessions: list[requests.Session] = []

        def handler(ctx: CommandContext, **kwargs: Any) -> dict[str, str]:
            captured_sessions.append(ctx.session)
            return {"ok": True}

        mock_plugin = MockPlugin()
        # Plugin requires_session=True by default, but command overrides
        cmd_spec = CommandSpec(
            name="test",
            handler=handler,
            click_kwargs={"help": "Test"},
            params=(),
            requires_session=False,
        )
        app = build_command_app(mock_plugin, cmd_spec)

        runner = TyperCliRunner()
        result = runner.invoke(app, [])

        assert result.exit_code == 0
        assert len(captured_sessions) == 1
        # Should be a plain session, not loaded from cache
        assert isinstance(captured_sessions[0], requests.Session)
        # get_session should NOT have been called
        # (MockPlugin.get_session is NOT mocked, so if called it would
        # try to load from cache and fail -- the fact we got exit_code 0
        # confirms it wasn't called)

    def test_requires_session_none_inherits_from_plugin(self, isolated_config: Path) -> None:
        """Command with requires_session=None inherits plugin.requires_session."""
        mock_session = MagicMock(spec=requests.Session)
        mock_plugin = MockPlugin()
        mock_plugin.get_session = MagicMock(  # type: ignore[method-assign]
            return_value=mock_session
        )

        captured_sessions: list[Any] = []

        def handler(ctx: CommandContext, **kwargs: Any) -> dict[str, str]:
            captured_sessions.append(ctx.session)
            return {"ok": True}

        # requires_session=None means inherit from plugin (True)
        cmd_spec = CommandSpec(
            name="test",
            handler=handler,
            click_kwargs={"help": "Test"},
            params=(),
            requires_session=None,
        )
        app = build_command_app(mock_plugin, cmd_spec)

        runner = TyperCliRunner()
        result = runner.invoke(app, [])

        assert result.exit_code == 0
        mock_plugin.get_session.assert_called_once()
        assert captured_sessions[0] is mock_session


class TestEnsureGroupHierarchy:
    """Tests for _build_site_app's dotted-group nesting (production wiring).

    _ensure_group_hierarchy (hand-built nested click.Group()s) is deleted;
    dotted CommandSpec.group paths now become nested Typer sub-apps built
    directly by _build_site_app, so these tests drive that function instead.
    """

    def test_single_level(self) -> None:
        """Single-level group path creates one nested Typer sub-app."""
        from graftpunk.cli.plugin_commands import PluginDiscoveryResult, _build_site_app

        spec = CommandSpec(
            name="list",
            handler=lambda ctx: {"ok": True},
            group="accounts",
            requires_session=False,
        )
        plugin = _site_plugin_with_commands("hierarchy1", [spec])
        site_app = _build_site_app(plugin, PluginDiscoveryResult())

        group_names = [g.name for g in site_app.registered_groups]
        assert "accounts" in group_names

        accounts_app = next(
            g for g in site_app.registered_groups if g.name == "accounts"
        ).typer_instance
        assert "list" in [c.name for c in accounts_app.registered_commands]

        run = TyperCliRunner().invoke(site_app, ["accounts", "list"])
        assert run.exit_code == 0

    def test_nested_levels(self) -> None:
        """Dotted group path creates multiple nested Typer sub-apps."""
        from graftpunk.cli.plugin_commands import PluginDiscoveryResult, _build_site_app

        spec = CommandSpec(
            name="list",
            handler=lambda ctx: {"ok": True},
            group="accounts.statements",
            requires_session=False,
        )
        plugin = _site_plugin_with_commands("hierarchy2", [spec])
        site_app = _build_site_app(plugin, PluginDiscoveryResult())

        accounts_app = next(
            g for g in site_app.registered_groups if g.name == "accounts"
        ).typer_instance
        assert "statements" in [g.name for g in accounts_app.registered_groups]

        statements_app = next(
            g for g in accounts_app.registered_groups if g.name == "statements"
        ).typer_instance
        assert "list" in [c.name for c in statements_app.registered_commands]

        run = TyperCliRunner().invoke(site_app, ["accounts", "statements", "list"])
        assert run.exit_code == 0

    def test_reuses_existing_group(self) -> None:
        """Two commands sharing a dotted group path share one Typer sub-app."""
        from graftpunk.cli.plugin_commands import PluginDiscoveryResult, _build_site_app

        specs = [
            CommandSpec(
                name="list",
                handler=lambda ctx: {"ok": True},
                group="accounts",
                requires_session=False,
            ),
            CommandSpec(
                name="show",
                handler=lambda ctx: {"ok": True},
                group="accounts",
                requires_session=False,
            ),
        ]
        plugin = _site_plugin_with_commands("hierarchy3", specs)
        site_app = _build_site_app(plugin, PluginDiscoveryResult())

        account_groups = [g for g in site_app.registered_groups if g.name == "accounts"]
        assert len(account_groups) == 1
        cmd_names = [c.name for c in account_groups[0].typer_instance.registered_commands]
        assert set(cmd_names) == {"list", "show"}

    def test_group_command_name_collision_raises_plugin_error(self) -> None:
        """A group path colliding with an existing command name raises PluginError."""
        from graftpunk.cli.plugin_commands import PluginDiscoveryResult, _build_site_app

        specs = [
            CommandSpec(name="accounts", handler=lambda ctx: {"ok": True}, requires_session=False),
            CommandSpec(
                name="sub",
                handler=lambda ctx: {"ok": True},
                group="accounts",
                requires_session=False,
            ),
        ]
        plugin = _site_plugin_with_commands("hierarchy4", specs)

        with pytest.raises(PluginError, match="collides with an existing command"):
            _build_site_app(plugin, PluginDiscoveryResult())

    def test_plugin_error_skips_whole_plugin_in_registration(self, isolated_config: Path) -> None:
        """register_plugin_commands converts an escaped PluginError into a whole-plugin skip."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        specs = [
            CommandSpec(name="accounts", handler=lambda ctx: {"ok": True}, requires_session=False),
            CommandSpec(
                name="sub",
                handler=lambda ctx: {"ok": True},
                group="accounts",
                requires_session=False,
            ),
        ]
        plugin = _site_plugin_with_commands("hierarchy5", specs)

        app = typer.Typer()
        with (
            patch(DISCOVER_ALL, return_value=(plugin,)),
            patch("graftpunk.cli.plugin_commands._notify_plugin_errors") as mock_notify,
        ):
            registered = register_plugin_commands(app, notify_errors=True)

        assert "hierarchy5" not in registered
        assert "hierarchy5" not in [g.name for g in app.registered_groups]

        mock_notify.assert_called_once()
        result = mock_notify.call_args[0][0]
        assert result.has_errors is True
        assert any(e.plugin_name == "hierarchy5" for e in result.errors)

    def test_login_resolution_plugin_error_skips_whole_plugin(self, isolated_config: Path) -> None:
        """A PluginError raised while resolving/registering login must escape
        the login block's except-Exception, not be swallowed there -- the
        module's single-owner policy says PluginError = contract violation
        that skips the WHOLE plugin (mirrors
        test_plugin_error_skips_whole_plugin_in_registration, but for the
        login-registration block instead of the leaf-command loop).
        """
        from graftpunk.cli.plugin_commands import register_plugin_commands

        specs = [
            CommandSpec(name="items", handler=lambda ctx: {"ok": True}, requires_session=False),
        ]
        plugin = _site_plugin_with_commands("loginboom", specs)

        app = typer.Typer()
        with (
            patch(DISCOVER_ALL, return_value=(plugin,)),
            patch(
                "graftpunk.cli.plugin_commands.resolve_login_callable",
                side_effect=PluginError("boom"),
            ),
            patch("graftpunk.cli.plugin_commands._notify_plugin_errors") as mock_notify,
        ):
            registered = register_plugin_commands(app, notify_errors=True)

        assert "loginboom" not in registered
        assert "loginboom" not in [g.name for g in app.registered_groups]

        mock_notify.assert_called_once()
        result = mock_notify.call_args[0][0]
        assert result.has_errors is True
        assert any(e.plugin_name == "loginboom" for e in result.errors)

    def test_login_name_collision_skips_whole_plugin(self, isolated_config: Path) -> None:
        """A plugin with both a root command named 'login' and a login_config
        would otherwise double-register 'login' on the site Typer app;
        the guard treats this as a name collision -- a PluginError that
        skips the whole plugin, consistent with every other collision.
        """
        from graftpunk.cli.plugin_commands import register_plugin_commands

        specs = [
            CommandSpec(name="login", handler=lambda ctx: {"ok": True}, requires_session=False),
        ]
        plugin = _site_plugin_with_commands("logincollide", specs)
        plugin.login_config = LoginConfig(  # type: ignore[attr-defined]
            steps=[LoginStep(fields={"username": "#u", "password": "#p"}, submit="#s")],
            url="/login",
            failure="Bad login",
        )

        app = typer.Typer()
        with (
            patch(DISCOVER_ALL, return_value=(plugin,)),
            patch("graftpunk.cli.plugin_commands._notify_plugin_errors") as mock_notify,
        ):
            registered = register_plugin_commands(app, notify_errors=True)

        assert "logincollide" not in registered
        assert "logincollide" not in [g.name for g in app.registered_groups]

        mock_notify.assert_called_once()
        result = mock_notify.call_args[0][0]
        assert result.has_errors is True
        assert any(
            e.plugin_name == "logincollide" and "login" in e.error.lower() for e in result.errors
        )


class TestYAMLCommandsHaveGroupNone:
    """Tests that YAML-generated commands have group=None."""

    def test_yaml_commands_group_none(self, isolated_config: Path) -> None:
        """YAML-generated CommandSpecs have group=None."""
        from graftpunk.plugins.cli_plugin import build_plugin_config
        from graftpunk.plugins.yaml_loader import YAMLCommandDef
        from graftpunk.plugins.yaml_plugin import create_yaml_site_plugin

        config = build_plugin_config(site_name="test", session_name="test", help_text="Test")
        commands = [
            YAMLCommandDef(name="list", help_text="List", method="GET", url="/list", params=()),
        ]
        plugin = create_yaml_site_plugin(config, commands)
        specs = plugin.get_commands()

        assert len(specs) == 1
        assert specs[0].group is None


class TestTokenAutoInjection:
    """Tests for automatic token injection in plugin commands."""

    def test_command_with_token_config_injects_tokens(self, isolated_config: Path) -> None:
        """Plugin with token_config gets prepare_session called before command execution."""

        mock_session = MagicMock()
        mock_session.driver = MagicMock()
        mock_plugin = MockPlugin()
        mock_plugin.get_session = MagicMock(  # type: ignore[method-assign]
            return_value=mock_session
        )
        # Attach a token_config to trigger injection
        mock_plugin.token_config = MagicMock()  # type: ignore[attr-defined]
        mock_plugin.base_url = "https://example.com"  # type: ignore[attr-defined]

        handler_called = []

        def handler(ctx: Any, **kwargs: Any) -> dict[str, str]:
            handler_called.append(True)
            return {"ok": True}

        cmd_spec = CommandSpec(
            name="test",
            handler=handler,
            click_kwargs={"help": "Test"},
            params=(),
        )
        app = build_command_app(mock_plugin, cmd_spec)

        with patch("graftpunk.tokens.prepare_session") as mock_prepare:
            mock_prepare.return_value = mock_session

            runner = TyperCliRunner()
            result = runner.invoke(app, [])

            assert result.exit_code == 0, result.output
            assert handler_called
            mock_prepare.assert_called_once_with(
                mock_session, mock_plugin.token_config, "https://example.com"
            )

    def test_command_without_token_config_skips_injection(self, isolated_config: Path) -> None:
        """Plugin without token_config is unaffected."""

        mock_session = MagicMock()
        mock_session.driver = MagicMock()
        mock_plugin = MockPlugin()
        mock_plugin.get_session = MagicMock(  # type: ignore[method-assign]
            return_value=mock_session
        )
        # No token_config attribute — default MockPlugin has none

        cmd_spec = CommandSpec(
            name="test",
            handler=lambda ctx, **kwargs: {"ok": True},
            click_kwargs={"help": "Test"},
            params=(),
        )
        app = build_command_app(mock_plugin, cmd_spec)

        with patch("graftpunk.tokens.prepare_session") as mock_prepare:
            runner = TyperCliRunner()
            result = runner.invoke(app, [])

            assert result.exit_code == 0
            mock_prepare.assert_not_called()

    def test_token_extraction_failure_shows_error(self, isolated_config: Path) -> None:
        """Token extraction ValueError shows user-friendly error and exits."""

        mock_session = MagicMock()
        mock_session.driver = MagicMock()
        mock_plugin = MockPlugin()
        mock_plugin.get_session = MagicMock(  # type: ignore[method-assign]
            return_value=mock_session
        )
        mock_plugin.token_config = MagicMock()  # type: ignore[attr-defined]

        cmd_spec = CommandSpec(
            name="test",
            handler=lambda ctx, **kwargs: {"ok": True},
            click_kwargs={"help": "Test"},
            params=(),
        )
        app = build_command_app(mock_plugin, cmd_spec)

        with (
            patch(
                "graftpunk.tokens.prepare_session",
                side_effect=ValueError("CSRF token not found in response"),
            ),
            patch("graftpunk.cli.plugin_runtime.gp_console") as mock_console,
        ):
            runner = TyperCliRunner()
            result = runner.invoke(app, [])

            assert result.exit_code == 1
            mock_console.error.assert_called_once()
            assert "Token extraction failed" in str(mock_console.error.call_args)


class TestTokenRefreshOn403:
    """Tests for 403 retry logic that clears and re-extracts tokens."""

    def _make_403_error(self) -> requests.exceptions.HTTPError:
        """Create an HTTPError with a 403 response."""
        resp = requests.Response()
        resp.status_code = 403
        return requests.exceptions.HTTPError(response=resp)

    def test_403_retries_with_fresh_token(self, isolated_config: Path) -> None:
        """HTTPError 403 clears tokens, re-extracts, and retries once."""

        mock_session = MagicMock()
        mock_session.driver = MagicMock()
        mock_plugin = MockPlugin()
        mock_plugin.get_session = MagicMock(return_value=mock_session)  # type: ignore[method-assign]
        mock_plugin.token_config = MagicMock()  # type: ignore[attr-defined]
        mock_plugin.base_url = "https://example.com"  # type: ignore[attr-defined]

        # First call raises 403, second succeeds
        call_count = 0

        def handler(ctx: Any, **kwargs: Any) -> dict[str, str]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise self._make_403_error()
            return {"ok": True}

        cmd_spec = CommandSpec(
            name="test",
            handler=handler,
            click_kwargs={"help": "Test"},
            params=(),
        )
        app = build_command_app(mock_plugin, cmd_spec)

        with (
            patch("graftpunk.tokens.prepare_session") as mock_prepare,
            patch("graftpunk.tokens.clear_cached_tokens") as mock_clear,
        ):
            runner = TyperCliRunner()
            result = runner.invoke(app, [])

            assert result.exit_code == 0, result.output
            assert call_count == 2
            mock_clear.assert_called_once_with(mock_session)
            # prepare_session called twice: once for initial injection, once for retry
            assert mock_prepare.call_count == 2

    def test_403_without_token_config_propagates(self, isolated_config: Path) -> None:
        """HTTPError 403 without token_config raises normally."""

        mock_session = MagicMock()
        mock_session.driver = MagicMock()
        mock_plugin = MockPlugin()
        mock_plugin.get_session = MagicMock(return_value=mock_session)  # type: ignore[method-assign]
        # No token_config — default MockPlugin has none

        def handler(ctx: Any, **kwargs: Any) -> dict[str, str]:
            raise self._make_403_error()

        cmd_spec = CommandSpec(
            name="test",
            handler=handler,
            click_kwargs={"help": "Test"},
            params=(),
        )
        app = build_command_app(mock_plugin, cmd_spec)

        with patch("graftpunk.cli.plugin_runtime.gp_console"):
            runner = TyperCliRunner()
            result = runner.invoke(app, [])

            # Should exit with error (caught by generic exception handler)
            assert result.exit_code == 1

    def test_403_retry_marks_session_dirty(self, isolated_config: Path) -> None:
        """403 retry sets ctx._session_dirty, triggering update_session_cookies."""

        mock_session = MagicMock()
        mock_session.driver = MagicMock()
        mock_plugin = MockPlugin()
        mock_plugin.get_session = MagicMock(return_value=mock_session)  # type: ignore[method-assign]
        mock_plugin.token_config = MagicMock()  # type: ignore[attr-defined]
        mock_plugin.base_url = "https://example.com"  # type: ignore[attr-defined]

        call_count = 0

        def handler(ctx: Any, **kwargs: Any) -> dict[str, str]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise self._make_403_error()
            return {"ok": True}

        cmd_spec = CommandSpec(
            name="test",
            handler=handler,
            click_kwargs={"help": "Test"},
            params=(),
        )
        app = build_command_app(mock_plugin, cmd_spec)

        with (
            patch("graftpunk.tokens.prepare_session"),
            patch("graftpunk.tokens.clear_cached_tokens"),
            patch("graftpunk.cli.plugin_runtime.update_session_cookies") as mock_update,
        ):
            runner = TyperCliRunner()
            result = runner.invoke(app, [])

            assert result.exit_code == 0, result.output
            # Dirty flag should trigger session persistence
            mock_update.assert_called_once_with(mock_session, mock_plugin.session_name)

    def test_second_403_propagates(self, isolated_config: Path) -> None:
        """If retry also returns 403, error propagates."""

        mock_session = MagicMock()
        mock_session.driver = MagicMock()
        mock_plugin = MockPlugin()
        mock_plugin.get_session = MagicMock(return_value=mock_session)  # type: ignore[method-assign]
        mock_plugin.token_config = MagicMock()  # type: ignore[attr-defined]
        mock_plugin.base_url = "https://example.com"  # type: ignore[attr-defined]

        def handler(ctx: Any, **kwargs: Any) -> dict[str, str]:
            raise self._make_403_error()

        cmd_spec = CommandSpec(
            name="test",
            handler=handler,
            click_kwargs={"help": "Test"},
            params=(),
        )
        app = build_command_app(mock_plugin, cmd_spec)

        with (
            patch("graftpunk.tokens.prepare_session"),
            patch("graftpunk.tokens.clear_cached_tokens"),
            patch("graftpunk.cli.plugin_runtime.gp_console"),
        ):
            runner = TyperCliRunner()
            result = runner.invoke(app, [])

            # Second 403 propagates as error
            assert result.exit_code == 1


class TestSessionPersistence:
    """Tests for session persistence after command execution."""

    def test_saves_session_decorator_persists_after_success(self, isolated_config: Path) -> None:
        """Plugin with @command(saves_session=True) calls update_session_cookies on success."""

        mock_session = MagicMock(spec=requests.Session)
        mock_plugin = MockPlugin()
        mock_plugin.get_session = MagicMock(  # type: ignore[method-assign]
            return_value=mock_session
        )

        def handler(ctx: CommandContext, **kwargs: Any) -> dict[str, str]:
            return {"ok": True}

        cmd_spec = CommandSpec(
            name="test",
            handler=handler,
            click_kwargs={"help": "Test"},
            params=(),
            saves_session=True,
        )
        app = build_command_app(mock_plugin, cmd_spec)

        with patch("graftpunk.cli.plugin_runtime.update_session_cookies") as mock_update:
            runner = TyperCliRunner()
            result = runner.invoke(app, [])

            assert result.exit_code == 0, result.output
            mock_update.assert_called_once_with(mock_session, "mocksession")

    def test_ctx_save_session_persists_after_success(self, isolated_config: Path) -> None:
        """Handler calling ctx.save_session() triggers update_session_cookies."""

        mock_session = MagicMock(spec=requests.Session)
        mock_plugin = MockPlugin()
        mock_plugin.get_session = MagicMock(  # type: ignore[method-assign]
            return_value=mock_session
        )

        def handler(ctx: CommandContext, **kwargs: Any) -> dict[str, str]:
            ctx.save_session()
            return {"ok": True}

        cmd_spec = CommandSpec(
            name="test",
            handler=handler,
            click_kwargs={"help": "Test"},
            params=(),
        )
        app = build_command_app(mock_plugin, cmd_spec)

        with patch("graftpunk.cli.plugin_runtime.update_session_cookies") as mock_update:
            runner = TyperCliRunner()
            result = runner.invoke(app, [])

            assert result.exit_code == 0, result.output
            mock_update.assert_called_once_with(mock_session, "mocksession")

    def test_no_save_when_not_requested(self, isolated_config: Path) -> None:
        """Plain @command() without ctx.save_session() does NOT call update_session_cookies."""

        mock_session = MagicMock(spec=requests.Session)
        mock_plugin = MockPlugin()
        mock_plugin.get_session = MagicMock(  # type: ignore[method-assign]
            return_value=mock_session
        )

        def handler(ctx: CommandContext, **kwargs: Any) -> dict[str, str]:
            return {"ok": True}

        cmd_spec = CommandSpec(
            name="test",
            handler=handler,
            click_kwargs={"help": "Test"},
            params=(),
        )
        app = build_command_app(mock_plugin, cmd_spec)

        with patch("graftpunk.cli.plugin_runtime.update_session_cookies") as mock_update:
            runner = TyperCliRunner()
            result = runner.invoke(app, [])

            assert result.exit_code == 0, result.output
            mock_update.assert_not_called()

    def test_no_save_on_command_failure(self, isolated_config: Path) -> None:
        """Handler exception prevents update_session_cookies from being called."""

        mock_session = MagicMock(spec=requests.Session)
        mock_plugin = MockPlugin()
        mock_plugin.get_session = MagicMock(  # type: ignore[method-assign]
            return_value=mock_session
        )

        def handler(ctx: CommandContext, **kwargs: Any) -> dict[str, str]:
            raise RuntimeError("something went wrong")

        cmd_spec = CommandSpec(
            name="test",
            handler=handler,
            click_kwargs={"help": "Test"},
            params=(),
            saves_session=True,
        )
        app = build_command_app(mock_plugin, cmd_spec)

        with patch("graftpunk.cli.plugin_runtime.update_session_cookies") as mock_update:
            runner = TyperCliRunner()
            result = runner.invoke(app, [])

            assert result.exit_code == 1
            mock_update.assert_not_called()


def _make_minimal_plugin() -> MagicMock:
    """Create a minimal mock plugin for build_command_app-based tests."""
    plugin = MagicMock()
    plugin.site_name = "test-plugin"
    plugin.session_name = "test-plugin"
    plugin.requires_session = True
    plugin.api_version = 1
    plugin.backend = "selenium"
    plugin.base_url = ""
    plugin.help_text = "Test plugin"
    plugin.token_config = None
    return plugin


class TestClickKwargsPassthrough:
    """Verify click_kwargs pass through _build_site_app (production wiring).

    Each test builds a single-command plugin and drives it through
    _build_site_app, not the build_command_app test helper -- registration-
    level behavior (help text, hidden/deprecated/epilog, --help rendering,
    envvar resolution, invalid-kwargs contract enforcement) is owned by
    _build_site_app / synthesize_command_fn, and must be exercised there so
    the suite can't stay green while production drifts.
    """

    def test_option_show_default_passes_through(self) -> None:
        """show_default in click_kwargs renders in --help."""
        from graftpunk.cli.plugin_commands import PluginDiscoveryResult, _build_site_app

        param = PluginParamSpec.option(
            "count",
            type=int,
            default=5,
            click_kwargs={"show_default": True},
        )
        spec = CommandSpec(
            name="test-cmd",
            handler=lambda ctx: None,
            params=(param,),
            requires_session=False,
        )
        plugin = _site_plugin_with_commands("clickkw1", [spec])
        site_app = _build_site_app(plugin, PluginDiscoveryResult())

        root = typer.Typer()
        root.add_typer(site_app, name=plugin.site_name)
        result = TyperCliRunner().invoke(root, [plugin.site_name, "test-cmd", "--help"])
        assert result.exit_code == 0
        assert "default: 5" in result.output

    def test_option_envvar_passes_through(self) -> None:
        """envvar in click_kwargs resolves from the environment."""
        import os

        from graftpunk.cli.plugin_commands import PluginDiscoveryResult, _build_site_app

        captured: dict[str, Any] = {}

        def handler(ctx: Any, **kwargs: Any) -> dict[str, bool]:
            captured.update(kwargs)
            return {"ok": True}

        param = PluginParamSpec.option(
            "token",
            type=str,
            click_kwargs={"envvar": "MY_TOKEN"},
        )
        spec = CommandSpec(
            name="test-cmd",
            handler=handler,
            params=(param,),
            requires_session=False,
        )
        plugin = _site_plugin_with_commands("clickkw2", [spec])
        site_app = _build_site_app(plugin, PluginDiscoveryResult())

        root = typer.Typer()
        root.add_typer(site_app, name=plugin.site_name)

        with patch.dict(os.environ, {"MY_TOKEN": "abc123"}):
            result = TyperCliRunner().invoke(root, [plugin.site_name, "test-cmd"])

        assert result.exit_code == 0, result.output
        assert captured.get("token") == "abc123"

    def test_argument_nargs_passes_through(self) -> None:
        """nargs=-1 in click_kwargs collects multiple positional args into a list."""
        from graftpunk.cli.plugin_commands import PluginDiscoveryResult, _build_site_app

        captured: dict[str, Any] = {}

        def handler(ctx: Any, **kwargs: Any) -> dict[str, bool]:
            captured.update(kwargs)
            return {"ok": True}

        param = PluginParamSpec.argument(
            "files",
            type=str,
            click_kwargs={"nargs": -1},
        )
        spec = CommandSpec(
            name="test-cmd",
            handler=handler,
            params=(param,),
            requires_session=False,
        )
        plugin = _site_plugin_with_commands("clickkw3", [spec])
        site_app = _build_site_app(plugin, PluginDiscoveryResult())

        root = typer.Typer()
        root.add_typer(site_app, name=plugin.site_name)
        result = TyperCliRunner().invoke(root, [plugin.site_name, "test-cmd", "a.txt", "b.txt"])

        assert result.exit_code == 0, result.output
        assert captured.get("files") == ["a.txt", "b.txt"]

    def test_command_help_from_click_kwargs(self) -> None:
        """Command help text comes from CommandSpec.click_kwargs['help']."""
        from graftpunk.cli.plugin_commands import PluginDiscoveryResult, _build_site_app

        spec = CommandSpec(
            name="test-cmd",
            handler=lambda ctx: None,
            click_kwargs={"help": "My custom help"},
            requires_session=False,
        )
        plugin = _site_plugin_with_commands("clickkw4", [spec])
        site_app = _build_site_app(plugin, PluginDiscoveryResult())
        assert site_app.registered_commands[0].help == "My custom help"

    def test_command_default_help_when_no_click_kwargs(self) -> None:
        """When no help in click_kwargs, default 'Run X command' is used."""
        from graftpunk.cli.plugin_commands import PluginDiscoveryResult, _build_site_app

        spec = CommandSpec(
            name="test-cmd",
            handler=lambda ctx: None,
            requires_session=False,
        )
        plugin = _site_plugin_with_commands("clickkw5", [spec])
        site_app = _build_site_app(plugin, PluginDiscoveryResult())
        assert site_app.registered_commands[0].help == "Run test-cmd command"

    def test_invalid_option_click_kwargs_raises_plugin_error(self) -> None:
        """Invalid click_kwargs for an option produce a PluginError."""
        from graftpunk.cli.plugin_commands import PluginDiscoveryResult, _build_site_app

        param = PluginParamSpec(
            name="bad",
            is_option=True,
            click_kwargs={"not_a_real_kwarg": True},
        )
        spec = CommandSpec(
            name="test-cmd",
            handler=lambda ctx: None,
            params=(param,),
            requires_session=False,
        )
        plugin = _site_plugin_with_commands("clickkw6", [spec])
        with pytest.raises(PluginError, match="param 'bad'.*unsupported click_kwargs.*option"):
            _build_site_app(plugin, PluginDiscoveryResult())

    def test_invalid_argument_click_kwargs_raises_plugin_error(self) -> None:
        """Invalid click_kwargs for an argument produce a PluginError."""
        from graftpunk.cli.plugin_commands import PluginDiscoveryResult, _build_site_app

        param = PluginParamSpec(
            name="bad",
            is_option=False,
            click_kwargs={"not_a_real_kwarg": True},
        )
        spec = CommandSpec(
            name="test-cmd",
            handler=lambda ctx: None,
            params=(param,),
            requires_session=False,
        )
        plugin = _site_plugin_with_commands("clickkw7", [spec])
        with pytest.raises(PluginError, match="param 'bad'.*unsupported click_kwargs.*argument"):
            _build_site_app(plugin, PluginDiscoveryResult())

    def test_command_hidden_via_click_kwargs(self) -> None:
        """hidden=True in CommandSpec.click_kwargs lands on the Typer command."""
        from graftpunk.cli.plugin_commands import PluginDiscoveryResult, _build_site_app

        spec = CommandSpec(
            name="secret",
            handler=lambda ctx: None,
            click_kwargs={"help": "Secret command", "hidden": True},
            requires_session=False,
        )
        plugin = _site_plugin_with_commands("clickkw8", [spec])
        site_app = _build_site_app(plugin, PluginDiscoveryResult())
        assert site_app.registered_commands[0].hidden is True

    def test_command_deprecated_via_click_kwargs(self) -> None:
        """deprecated=True in CommandSpec.click_kwargs lands on the Typer command."""
        from graftpunk.cli.plugin_commands import PluginDiscoveryResult, _build_site_app

        spec = CommandSpec(
            name="old-cmd",
            handler=lambda ctx: None,
            click_kwargs={"help": "Old command", "deprecated": True},
            requires_session=False,
        )
        plugin = _site_plugin_with_commands("clickkw9", [spec])
        site_app = _build_site_app(plugin, PluginDiscoveryResult())
        assert site_app.registered_commands[0].deprecated is True

    def test_command_epilog_via_click_kwargs(self) -> None:
        """epilog in CommandSpec.click_kwargs lands on the Typer command."""
        from graftpunk.cli.plugin_commands import PluginDiscoveryResult, _build_site_app

        spec = CommandSpec(
            name="test-cmd",
            handler=lambda ctx: None,
            click_kwargs={"help": "Test", "epilog": "See docs for more info."},
            requires_session=False,
        )
        plugin = _site_plugin_with_commands("clickkw10", [spec])
        site_app = _build_site_app(plugin, PluginDiscoveryResult())
        assert site_app.registered_commands[0].epilog == "See docs for more info."

    def test_unsupported_command_level_key_raises_plugin_error(self) -> None:
        """An unsupported command-level click_kwargs key raises PluginError (COMMAND_KEYS)."""
        from graftpunk.cli.plugin_commands import PluginDiscoveryResult, _build_site_app

        spec = CommandSpec(
            name="test-cmd",
            handler=lambda ctx: None,
            click_kwargs={"help": "Test", "not_a_real_command_kwarg": True},
            requires_session=False,
        )
        plugin = _site_plugin_with_commands("clickkw11", [spec])
        with pytest.raises(PluginError, match="unsupported command-level click_kwargs key"):
            _build_site_app(plugin, PluginDiscoveryResult())


class TestFormatExplicitFlag:
    """Integration tests: callback passes user_explicit correctly to format_output."""

    def test_explicit_format_flag_sets_user_explicit_true(self, isolated_config: Path) -> None:
        """Passing -f on the CLI sets user_explicit=True in format_output."""

        plugin = _make_minimal_plugin()
        plugin.get_session = MagicMock(return_value=requests.Session())
        plugin.requires_session = False

        cmd_spec = CommandSpec(
            name="test",
            handler=lambda ctx, **kw: {"ok": True},
            click_kwargs={"help": "Test"},
            params=(),
        )
        app = build_command_app(plugin, cmd_spec)

        with (
            patch("graftpunk.cli.plugin_runtime.build_observe_context"),
            patch("graftpunk.cli.plugin_runtime.format_output") as mock_fmt,
        ):
            runner = TyperCliRunner()
            result = runner.invoke(app, ["-f", "table"])

            assert result.exit_code == 0
            mock_fmt.assert_called_once()
            assert mock_fmt.call_args[1]["user_explicit"] is True

    def test_default_format_sets_user_explicit_false(self, isolated_config: Path) -> None:
        """Omitting -f on the CLI sets user_explicit=False in format_output."""

        plugin = _make_minimal_plugin()
        plugin.get_session = MagicMock(return_value=requests.Session())
        plugin.requires_session = False

        cmd_spec = CommandSpec(
            name="test",
            handler=lambda ctx, **kw: {"ok": True},
            click_kwargs={"help": "Test"},
            params=(),
        )
        app = build_command_app(plugin, cmd_spec)

        with (
            patch("graftpunk.cli.plugin_runtime.build_observe_context"),
            patch("graftpunk.cli.plugin_runtime.format_output") as mock_fmt,
        ):
            runner = TyperCliRunner()
            result = runner.invoke(app, [])

            assert result.exit_code == 0
            mock_fmt.assert_called_once()
            assert mock_fmt.call_args[1]["user_explicit"] is False
