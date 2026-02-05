"""Tests for CommandMetadata dataclass and @command decorator."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from graftpunk.plugins.cli_plugin import (
    SUPPORTED_API_VERSIONS,
    CommandContext,
    CommandGroupMeta,
    CommandMetadata,
    CommandResult,
    CommandSpec,
    LoginConfig,
    PluginConfig,
    PluginParamSpec,
    SitePlugin,
    _to_cli_name,
    command,
)


class TestLoginConfig:
    """Tests for the LoginConfig frozen dataclass."""

    def test_create_valid(self) -> None:
        """LoginConfig can be created with required fields and defaults."""
        cfg = LoginConfig(url="/login", fields={"user": "#u"}, submit="#btn")
        assert cfg.url == "/login"
        assert cfg.fields == {"user": "#u"}
        assert cfg.submit == "#btn"
        assert cfg.failure == ""
        assert cfg.success == ""

    def test_frozen(self) -> None:
        """LoginConfig is frozen (immutable)."""
        from dataclasses import FrozenInstanceError

        cfg = LoginConfig(url="/login", fields={"u": "#u"}, submit="#b")
        with pytest.raises(FrozenInstanceError):
            cfg.url = "/other"  # type: ignore[misc]

    def test_empty_url_raises(self) -> None:
        """LoginConfig rejects empty url."""
        with pytest.raises(ValueError, match="url must be non-empty"):
            LoginConfig(url="", fields={"u": "#u"}, submit="#b")

    def test_empty_fields_raises(self) -> None:
        """LoginConfig rejects empty fields."""
        with pytest.raises(ValueError, match="fields must be non-empty"):
            LoginConfig(url="/login", fields={}, submit="#b")

    def test_empty_submit_raises(self) -> None:
        """LoginConfig rejects empty submit."""
        with pytest.raises(ValueError, match="submit must be non-empty"):
            LoginConfig(url="/login", fields={"u": "#u"}, submit="")

    def test_whitespace_url_raises(self) -> None:
        """LoginConfig rejects whitespace-only url."""
        with pytest.raises(ValueError, match="url must be non-empty"):
            LoginConfig(url="   ", fields={"u": "#u"}, submit="#b")

    def test_whitespace_submit_raises(self) -> None:
        """LoginConfig rejects whitespace-only submit."""
        with pytest.raises(ValueError, match="submit must be non-empty"):
            LoginConfig(url="/login", fields={"u": "#u"}, submit="  \t  ")

    def test_whitespace_wait_for_raises(self) -> None:
        """LoginConfig rejects whitespace-only wait_for."""
        with pytest.raises(ValueError, match="wait_for must not be whitespace-only"):
            LoginConfig(url="/login", fields={"u": "#u"}, submit="#b", wait_for="   ")

    def test_whitespace_field_selector_raises(self) -> None:
        """LoginConfig rejects whitespace-only field selectors."""
        with pytest.raises(ValueError, match="fields\\['u'\\] selector must be non-empty"):
            LoginConfig(url="/login", fields={"u": "  "}, submit="#b")

    def test_empty_field_selector_raises(self) -> None:
        """LoginConfig rejects empty string field selectors."""
        with pytest.raises(ValueError, match="fields\\['u'\\] selector must be non-empty"):
            LoginConfig(url="/login", fields={"u": ""}, submit="#b")

    def test_wait_for_default_empty(self) -> None:
        """LoginConfig.wait_for defaults to empty string."""
        cfg = LoginConfig(url="/login", fields={"u": "#u"}, submit="#b")
        assert cfg.wait_for == ""

    def test_wait_for_stores_value(self) -> None:
        """LoginConfig stores wait_for selector."""
        cfg = LoginConfig(
            url="/login",
            fields={"u": "#u"},
            submit="#b",
            wait_for="input#signInName",
        )
        assert cfg.wait_for == "input#signInName"

    def test_with_optional_fields(self) -> None:
        """LoginConfig stores optional failure and success fields."""
        cfg = LoginConfig(url="/l", fields={"u": "#u"}, submit="#b", failure="Bad", success=".ok")
        assert cfg.failure == "Bad"
        assert cfg.success == ".ok"


class TestCommandMetadata:
    """Tests for the CommandMetadata frozen dataclass."""

    def test_create_minimal(self) -> None:
        """CommandMetadata can be created with just name and help_text."""
        meta = CommandMetadata(name="accounts", help_text="List accounts")
        assert meta.name == "accounts"
        assert meta.help_text == "List accounts"
        assert meta.params == ()

    def test_create_with_params(self) -> None:
        """CommandMetadata stores params tuple."""
        params = (PluginParamSpec(name="id", param_type=int, required=True),)
        meta = CommandMetadata(name="get", help_text="Get item", params=params)
        assert len(meta.params) == 1
        assert meta.params[0].name == "id"
        assert meta.params[0].param_type is int

    def test_frozen(self) -> None:
        """CommandMetadata is frozen (immutable)."""
        import dataclasses

        meta = CommandMetadata(name="test", help_text="Test")
        with pytest.raises(dataclasses.FrozenInstanceError):
            meta.name = "changed"  # type: ignore[misc]

    def test_default_params_is_empty_tuple(self) -> None:
        """Each CommandMetadata instance defaults to empty tuple."""
        meta1 = CommandMetadata(name="a", help_text="A")
        meta2 = CommandMetadata(name="b", help_text="B")
        assert meta1.params == ()
        assert meta2.params == ()

    def test_empty_name_raises(self) -> None:
        """CommandMetadata rejects empty name."""
        with pytest.raises(ValueError, match="CommandMetadata.name must be non-empty"):
            CommandMetadata(name="", help_text="Test")


class TestCommandDecorator:
    """Tests for the @command decorator storing CommandMetadata."""

    def test_decorator_stores_command_meta(self) -> None:
        """@command stores a CommandMetadata on the wrapper function."""

        @command(help="List items")
        def items(self: Any, ctx: Any) -> dict[str, list[int]]:
            return {"items": [1, 2, 3]}

        assert hasattr(items, "_command_meta")
        meta = items._command_meta  # type: ignore[attr-defined]
        assert isinstance(meta, CommandMetadata)
        assert meta.name == "items"
        assert meta.help_text == "List items"
        assert meta.params == ()

    def test_decorator_with_params(self) -> None:
        """@command stores explicit params in CommandMetadata."""
        params = [PluginParamSpec(name="item_id", param_type=int, required=True)]

        @command(help="Get item", params=params)
        def get_item(self: Any, ctx: Any, item_id: int) -> dict[str, int]:
            return {"id": item_id}

        meta = get_item._command_meta  # type: ignore[attr-defined]
        assert len(meta.params) == 1
        assert meta.params[0].name == "item_id"

    def test_decorator_preserves_function_name(self) -> None:
        """@command preserves the original function name via functools.wraps."""

        @command(help="Test")
        def my_function(self: Any, ctx: Any) -> None:
            pass

        assert my_function.__name__ == "my_function"

    def test_decorator_no_old_attributes(self) -> None:
        """@command does NOT set _is_cli_command, _help_text, or _params."""

        @command(help="Test")
        def test_cmd(self: Any, ctx: Any) -> None:
            pass

        assert not hasattr(test_cmd, "_is_cli_command")
        assert not hasattr(test_cmd, "_help_text")
        assert not hasattr(test_cmd, "_params")

    def test_decorator_calls_through(self) -> None:
        """Decorated function still executes correctly."""

        @command(help="Add")
        def add(a: int, b: int) -> int:
            return a + b

        assert add(1, 2) == 3

    def test_decorator_empty_help(self) -> None:
        """@command with no help text stores empty string."""

        @command()
        def no_help(self: Any, ctx: Any) -> None:
            pass

        meta = no_help._command_meta  # type: ignore[attr-defined]
        assert meta.help_text == ""


class TestGetCommandsWithMetadata:
    """Tests for SitePlugin.get_commands() using _command_meta."""

    def test_discovers_decorated_methods(self) -> None:
        """get_commands() discovers methods with _command_meta."""

        class MyPlugin(SitePlugin):
            site_name = "test_discover"
            session_name = "test"
            help_text = "Test"

            @command(help="List items")
            def items(self, ctx: Any) -> dict[str, list[int]]:
                return {"items": [1, 2, 3]}

            @command(help="Get item")
            def item(self, ctx: Any, item_id: int) -> dict[str, int]:
                return {"id": item_id}

        plugin = MyPlugin()
        commands = {c.name: c for c in plugin.get_commands()}

        assert "items" in commands
        assert "item" in commands
        assert isinstance(commands["items"], CommandSpec)
        assert commands["items"].help_text == "List items"
        assert commands["item"].help_text == "Get item"

    def test_skips_non_decorated_methods(self) -> None:
        """get_commands() ignores methods without _command_meta."""

        class MyPlugin(SitePlugin):
            site_name = "test_skip"
            session_name = "test"
            help_text = "Test"

            @command(help="Listed")
            def items(self, ctx: Any) -> dict[str, list[int]]:
                return {"items": []}

            def not_a_command(self) -> None:
                pass

        plugin = MyPlugin()
        commands = {c.name: c for c in plugin.get_commands()}

        assert "items" in commands
        assert "not_a_command" not in commands

    def test_explicit_params_used_over_introspection(self) -> None:
        """get_commands() uses explicit params from CommandMetadata."""
        explicit_params = [
            PluginParamSpec(name="item_id", param_type=int, required=True, is_option=False)
        ]

        class MyPlugin(SitePlugin):
            site_name = "test_explicit"
            session_name = "test"
            help_text = "Test"

            @command(help="Get item", params=explicit_params)
            def item(self, ctx: Any, item_id: int) -> dict[str, int]:
                return {"id": item_id}

        plugin = MyPlugin()
        commands = {c.name: c for c in plugin.get_commands()}
        assert commands["item"].params[0].name == "item_id"
        assert commands["item"].params[0].is_option is False

    def test_introspection_used_when_no_explicit_params(self) -> None:
        """get_commands() falls back to introspection when no params in metadata."""

        class MyPlugin(SitePlugin):
            site_name = "test_introspect"
            session_name = "test"
            help_text = "Test"

            @command(help="Search")
            def search(self, ctx: Any, query: str, limit: int = 10) -> list[str]:
                return []

        plugin = MyPlugin()
        commands = {c.name: c for c in plugin.get_commands()}
        params = commands["search"].params

        assert len(params) == 2
        assert params[0].name == "query"
        assert params[0].required is True
        assert params[1].name == "limit"
        assert params[1].required is False
        assert params[1].default == 10


class TestCommandMetadataExport:
    """Tests for CommandMetadata export from plugins package."""

    def test_importable_from_plugins_package(self) -> None:
        """CommandMetadata is importable from graftpunk.plugins."""
        from graftpunk.plugins import CommandMetadata as PluginsCommandMetadata

        assert PluginsCommandMetadata is CommandMetadata

    def test_in_all(self) -> None:
        """CommandMetadata is listed in __all__."""
        import graftpunk.plugins

        assert "CommandMetadata" in graftpunk.plugins.__all__


class TestApiVersion:
    """Tests for api_version field on PluginConfig and SitePlugin."""

    def test_plugin_config_default_api_version(self) -> None:
        """PluginConfig defaults api_version to 1."""
        config = PluginConfig(site_name="test", session_name="test", help_text="Test")
        assert config.api_version == 1

    def test_plugin_config_rejects_unsupported_api_version(self) -> None:
        """PluginConfig rejects api_version not in SUPPORTED_API_VERSIONS."""
        with pytest.raises(ValueError, match="not supported"):
            PluginConfig(site_name="test", session_name="test", help_text="Test", api_version=2)

    def test_site_plugin_default_api_version(self) -> None:
        """SitePlugin subclass defaults api_version to 1."""

        class MyPlugin(SitePlugin):
            site_name = "test_api_ver"
            session_name = "test_api_ver"
            help_text = "Test"

        plugin = MyPlugin()
        assert plugin.api_version == 1

    def test_site_plugin_unsupported_api_version_rejected(self) -> None:
        """SitePlugin with unsupported api_version raises during config build."""
        with pytest.raises(ValueError, match="not supported"):

            class MyPlugin(SitePlugin):
                site_name = "test_api_ver_custom"
                session_name = "test_api_ver_custom"
                help_text = "Test"
                api_version = 2


class TestCommandContext:
    """Tests for the CommandContext frozen dataclass."""

    def test_fields(self) -> None:
        """CommandContext stores all fields correctly."""
        import requests

        session = requests.Session()
        ctx = CommandContext(
            session=session,
            plugin_name="test",
            command_name="search",
            api_version=1,
        )
        assert ctx.session is session
        assert ctx.plugin_name == "test"
        assert ctx.command_name == "search"
        assert ctx.api_version == 1

    def test_mutable(self) -> None:
        """CommandContext is mutable (not frozen) to support save_session()."""
        import requests

        ctx = CommandContext(
            session=requests.Session(),
            plugin_name="test",
            command_name="cmd",
            api_version=1,
        )
        # CommandContext is no longer frozen -- mutation is allowed
        ctx.plugin_name = "other"
        assert ctx.plugin_name == "other"

    def test_importable_from_plugins_package(self) -> None:
        """CommandContext is importable from graftpunk.plugins."""
        from graftpunk.plugins import CommandContext as PluginsCommandContext

        assert PluginsCommandContext is CommandContext

    def test_in_all(self) -> None:
        """CommandContext is listed in __all__."""
        import graftpunk.plugins

        assert "CommandContext" in graftpunk.plugins.__all__


class TestCommandContextValidation:
    """Tests for CommandContext __post_init__ validation."""

    def test_api_version_zero_raises(self) -> None:
        """CommandContext rejects api_version not in SUPPORTED_API_VERSIONS."""
        import requests

        with pytest.raises(ValueError, match="not supported"):
            CommandContext(
                session=requests.Session(),
                plugin_name="test",
                command_name="cmd",
                api_version=0,
            )

    def test_negative_api_version_raises(self) -> None:
        """CommandContext rejects negative api_version."""
        import requests

        with pytest.raises(ValueError, match="not supported"):
            CommandContext(
                session=requests.Session(),
                plugin_name="test",
                command_name="cmd",
                api_version=-1,
            )

    def test_empty_plugin_name_raises(self) -> None:
        """CommandContext rejects empty plugin_name."""
        import requests

        with pytest.raises(ValueError, match="plugin_name must be non-empty"):
            CommandContext(
                session=requests.Session(),
                plugin_name="",
                command_name="cmd",
                api_version=1,
            )

    def test_empty_command_name_raises(self) -> None:
        """CommandContext rejects empty command_name."""
        import requests

        with pytest.raises(ValueError, match="command_name must be non-empty"):
            CommandContext(
                session=requests.Session(),
                plugin_name="test",
                command_name="",
                api_version=1,
            )


class TestCommandResult:
    """Tests for the CommandResult dataclass."""

    def test_create_with_data_only(self) -> None:
        """CommandResult can be created with just data."""
        result = CommandResult(data={"items": [1, 2, 3]})
        assert result.data == {"items": [1, 2, 3]}
        assert result.metadata == {}
        assert result.format_hint is None

    def test_create_with_all_fields(self) -> None:
        """CommandResult stores all fields correctly."""
        result = CommandResult(
            data=[1, 2, 3],
            metadata={"page": 1, "total": 100},
            format_hint="table",
        )
        assert result.data == [1, 2, 3]
        assert result.metadata == {"page": 1, "total": 100}
        assert result.format_hint == "table"

    def test_frozen(self) -> None:
        """CommandResult is frozen (immutable)."""
        import dataclasses

        result = CommandResult(data="hello")
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.data = "updated"  # type: ignore[misc]

    def test_default_metadata_not_shared(self) -> None:
        """Each CommandResult instance gets its own metadata dict."""
        r1 = CommandResult(data=1)
        r2 = CommandResult(data=2)
        # Metadata dicts are separate instances even though frozen
        assert r1.metadata is not r2.metadata

    def test_importable_from_plugins_package(self) -> None:
        """CommandResult is importable from graftpunk.plugins."""
        from graftpunk.plugins import CommandResult as PluginsCommandResult

        assert PluginsCommandResult is CommandResult

    def test_in_all(self) -> None:
        """CommandResult is listed in __all__."""
        import graftpunk.plugins

        assert "CommandResult" in graftpunk.plugins.__all__


class TestCommandSpecResourceLimits:
    """Tests for timeout, max_retries, and rate_limit fields on CommandSpec."""

    def test_default_no_limits(self) -> None:
        """CommandSpec defaults to no resource limits."""
        spec = CommandSpec(name="test", handler=lambda: None, help_text="Test")
        assert spec.timeout is None
        assert spec.max_retries == 0
        assert spec.rate_limit is None

    def test_custom_limits(self) -> None:
        """CommandSpec accepts custom resource limit values."""
        spec = CommandSpec(
            name="test",
            handler=lambda: None,
            help_text="Test",
            timeout=30.0,
            max_retries=3,
            rate_limit=1.0,
        )
        assert spec.timeout == 30.0
        assert spec.max_retries == 3
        assert spec.rate_limit == 1.0

    def test_partial_limits(self) -> None:
        """CommandSpec allows setting only some resource limits."""
        spec = CommandSpec(
            name="test",
            handler=lambda: None,
            help_text="Test",
            timeout=60.0,
        )
        assert spec.timeout == 60.0
        assert spec.max_retries == 0
        assert spec.rate_limit is None


class TestCommandSpecValidation:
    """Tests for CommandSpec __post_init__ validation."""

    def test_empty_name_raises(self) -> None:
        """CommandSpec rejects empty name."""
        with pytest.raises(ValueError, match="CommandSpec.name must be non-empty"):
            CommandSpec(name="", handler=lambda: None)

    def test_negative_max_retries_raises(self) -> None:
        """CommandSpec rejects negative max_retries."""
        with pytest.raises(ValueError, match="max_retries must be >= 0"):
            CommandSpec(name="test", handler=lambda: None, max_retries=-1)

    def test_zero_timeout_raises(self) -> None:
        """CommandSpec rejects zero timeout."""
        with pytest.raises(ValueError, match="timeout must be positive when set"):
            CommandSpec(name="test", handler=lambda: None, timeout=0.0)

    def test_negative_timeout_raises(self) -> None:
        """CommandSpec rejects negative timeout."""
        with pytest.raises(ValueError, match="timeout must be positive when set"):
            CommandSpec(name="test", handler=lambda: None, timeout=-1.0)

    def test_zero_rate_limit_raises(self) -> None:
        """CommandSpec rejects zero rate_limit."""
        with pytest.raises(ValueError, match="rate_limit must be positive when set"):
            CommandSpec(name="test", handler=lambda: None, rate_limit=0.0)

    def test_negative_rate_limit_raises(self) -> None:
        """CommandSpec rejects negative rate_limit."""
        with pytest.raises(ValueError, match="rate_limit must be positive when set"):
            CommandSpec(name="test", handler=lambda: None, rate_limit=-1.0)

    def test_none_timeout_ok(self) -> None:
        """CommandSpec allows None timeout (no limit)."""
        spec = CommandSpec(name="test", handler=lambda: None, timeout=None)
        assert spec.timeout is None

    def test_none_rate_limit_ok(self) -> None:
        """CommandSpec allows None rate_limit (no limit)."""
        spec = CommandSpec(name="test", handler=lambda: None, rate_limit=None)
        assert spec.rate_limit is None


class TestPluginParamSpecValidation:
    """Tests for PluginParamSpec __post_init__ validation."""

    def test_empty_name_raises(self) -> None:
        """PluginParamSpec rejects empty name."""
        with pytest.raises(ValueError, match="PluginParamSpec.name must be non-empty"):
            PluginParamSpec(name="")

    def test_unsupported_param_type_raises(self) -> None:
        """PluginParamSpec rejects unsupported param_type."""
        with pytest.raises(ValueError, match="Unsupported param_type"):
            PluginParamSpec(name="x", param_type=list)

    def test_valid_param_types(self) -> None:
        """PluginParamSpec accepts str, int, float, and bool."""
        for t in (str, int, float, bool):
            spec = PluginParamSpec(name="x", param_type=t)
            assert spec.param_type is t

    def test_default_param_type_is_str(self) -> None:
        """PluginParamSpec defaults param_type to str."""
        spec = PluginParamSpec(name="x")
        assert spec.param_type is str


class TestCommandDecoratorSimplified:
    """Tests verifying the simplified @command decorator (no wrapper)."""

    def test_returns_original_function(self) -> None:
        """Simplified @command returns the original function, not a wrapper."""

        def my_func(self: Any, ctx: Any) -> None:
            pass

        decorated = command(help="Test")(my_func)
        assert decorated is my_func

    def test_sets_command_meta_on_original(self) -> None:
        """@command sets _command_meta directly on the original function."""

        @command(help="Do stuff")
        def do_stuff(self: Any, ctx: Any) -> None:
            pass

        assert hasattr(do_stuff, "_command_meta")
        meta = do_stuff._command_meta  # type: ignore[attr-defined]
        assert meta.name == "do_stuff"
        assert meta.help_text == "Do stuff"

    def test_preserves_function_identity(self) -> None:
        """Decorated function preserves __name__ without needing wraps."""

        @command(help="Test")
        def original(self: Any, ctx: Any) -> None:
            pass

        assert original.__name__ == "original"

    def test_decorated_function_callable(self) -> None:
        """Decorated function remains callable."""

        @command(help="Add numbers")
        def add(a: int, b: int) -> int:
            return a + b

        assert add(3, 4) == 7


class TestSupportedApiVersions:
    """Tests for SUPPORTED_API_VERSIONS constant."""

    def test_is_frozenset(self) -> None:
        """SUPPORTED_API_VERSIONS is a frozenset."""
        assert isinstance(SUPPORTED_API_VERSIONS, frozenset)

    def test_contains_version_1(self) -> None:
        """SUPPORTED_API_VERSIONS contains version 1."""
        assert 1 in SUPPORTED_API_VERSIONS

    def test_importable_from_plugins_package(self) -> None:
        """SUPPORTED_API_VERSIONS is importable from graftpunk.plugins."""
        from graftpunk.plugins import (
            SUPPORTED_API_VERSIONS as PLUGINS_SUPPORTED_API_VERSIONS,
        )

        assert PLUGINS_SUPPORTED_API_VERSIONS is SUPPORTED_API_VERSIONS

    def test_in_plugins_all(self) -> None:
        """SUPPORTED_API_VERSIONS is listed in __all__."""
        import graftpunk.plugins

        assert "SUPPORTED_API_VERSIONS" in graftpunk.plugins.__all__


class TestCommandContextExpansion:
    """Tests for new base_url and config fields on CommandContext."""

    def test_base_url_default_empty(self) -> None:
        """CommandContext defaults base_url to empty string."""
        import requests

        ctx = CommandContext(
            session=requests.Session(),
            plugin_name="test",
            command_name="cmd",
            api_version=1,
        )
        assert ctx.base_url == ""

    def test_base_url_set(self) -> None:
        """CommandContext stores base_url."""
        import requests

        ctx = CommandContext(
            session=requests.Session(),
            plugin_name="test",
            command_name="cmd",
            api_version=1,
            base_url="https://example.com",
        )
        assert ctx.base_url == "https://example.com"

    def test_config_default_none(self) -> None:
        """CommandContext defaults config to None."""
        import requests

        ctx = CommandContext(
            session=requests.Session(),
            plugin_name="test",
            command_name="cmd",
            api_version=1,
        )
        assert ctx.config is None

    def test_config_set(self) -> None:
        """CommandContext stores a PluginConfig."""
        import requests

        config = PluginConfig(site_name="test", session_name="test", help_text="Test")
        ctx = CommandContext(
            session=requests.Session(),
            plugin_name="test",
            command_name="cmd",
            api_version=1,
            config=config,
        )
        assert ctx.config is config
        assert ctx.config.site_name == "test"


class TestCommandSpecRequiresSession:
    """Tests for requires_session field on CommandSpec."""

    def test_default_none(self) -> None:
        """CommandSpec defaults requires_session to None."""
        spec = CommandSpec(name="test", handler=lambda: None)
        assert spec.requires_session is None

    def test_set_true(self) -> None:
        """CommandSpec accepts requires_session=True."""
        spec = CommandSpec(name="test", handler=lambda: None, requires_session=True)
        assert spec.requires_session is True

    def test_set_false(self) -> None:
        """CommandSpec accepts requires_session=False."""
        spec = CommandSpec(name="test", handler=lambda: None, requires_session=False)
        assert spec.requires_session is False


class TestCommandMetadataRequiresSession:
    """Tests for requires_session field on CommandMetadata."""

    def test_default_none(self) -> None:
        """CommandMetadata defaults requires_session to None."""
        meta = CommandMetadata(name="test", help_text="Test")
        assert meta.requires_session is None

    def test_set_true(self) -> None:
        """CommandMetadata accepts requires_session=True."""
        meta = CommandMetadata(name="test", help_text="Test", requires_session=True)
        assert meta.requires_session is True

    def test_set_false(self) -> None:
        """CommandMetadata accepts requires_session=False."""
        meta = CommandMetadata(name="test", help_text="Test", requires_session=False)
        assert meta.requires_session is False


class TestCommandDecoratorRequiresSession:
    """Tests for requires_session parameter on @command decorator."""

    def test_default_none(self) -> None:
        """@command defaults requires_session to None in metadata."""

        @command(help="Test")
        def test_cmd(self: Any, ctx: Any) -> None:
            pass

        meta = test_cmd._command_meta  # type: ignore[attr-defined]
        assert meta.requires_session is None

    def test_set_false(self) -> None:
        """@command passes requires_session=False to metadata."""

        @command(help="Test", requires_session=False)
        def test_cmd(self: Any, ctx: Any) -> None:
            pass

        meta = test_cmd._command_meta  # type: ignore[attr-defined]
        assert meta.requires_session is False

    def test_set_true(self) -> None:
        """@command passes requires_session=True to metadata."""

        @command(help="Test", requires_session=True)
        def test_cmd(self: Any, ctx: Any) -> None:
            pass

        meta = test_cmd._command_meta  # type: ignore[attr-defined]
        assert meta.requires_session is True

    def test_get_commands_passes_requires_session(self) -> None:
        """get_commands() passes requires_session from metadata to CommandSpec."""

        class MyPlugin(SitePlugin):
            site_name = "test_rs"
            session_name = "test_rs"
            help_text = "Test"

            @command(help="No session needed", requires_session=False)
            def public_cmd(self, ctx: Any) -> None:
                pass

            @command(help="Session needed", requires_session=True)
            def private_cmd(self, ctx: Any) -> None:
                pass

            @command(help="Inherit from plugin")
            def default_cmd(self, ctx: Any) -> None:
                pass

        plugin = MyPlugin()
        commands = {c.name: c for c in plugin.get_commands()}

        assert commands["public_cmd"].requires_session is False
        assert commands["private_cmd"].requires_session is True
        assert commands["default_cmd"].requires_session is None


class TestPluginConfigMetadata:
    """Tests for plugin metadata fields on PluginConfig."""

    def test_defaults_empty(self) -> None:
        """PluginConfig defaults metadata fields to empty strings."""
        config = PluginConfig(site_name="test", session_name="test", help_text="Test")
        assert config.plugin_version == ""
        assert config.plugin_author == ""
        assert config.plugin_url == ""

    def test_custom_values(self) -> None:
        """PluginConfig accepts custom metadata values."""
        config = PluginConfig(
            site_name="test",
            session_name="test",
            help_text="Test",
            plugin_version="1.0.0",
            plugin_author="Test Author",
            plugin_url="https://example.com",
        )
        assert config.plugin_version == "1.0.0"
        assert config.plugin_author == "Test Author"
        assert config.plugin_url == "https://example.com"

    def test_frozen(self) -> None:
        """PluginConfig metadata fields are frozen."""
        import dataclasses

        config = PluginConfig(
            site_name="test",
            session_name="test",
            help_text="Test",
            plugin_version="1.0.0",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            config.plugin_version = "2.0.0"  # type: ignore[misc]


class TestToCliName:
    """Tests for _to_cli_name helper converting Python names to CLI names."""

    def test_camel_case(self) -> None:
        """CamelCase becomes kebab-case."""
        assert _to_cli_name("AccountStatements") == "account-statements"

    def test_underscores(self) -> None:
        """Underscores become hyphens."""
        assert _to_cli_name("account_statements") == "account-statements"

    def test_single_word_lower(self) -> None:
        """Single lowercase word is unchanged."""
        assert _to_cli_name("accounts") == "accounts"

    def test_single_word_upper(self) -> None:
        """Single uppercase word becomes lowercase."""
        assert _to_cli_name("Accounts") == "accounts"

    def test_mixed_case_with_numbers(self) -> None:
        """Numbers followed by uppercase get a hyphen."""
        assert _to_cli_name("v2Accounts") == "v2-accounts"

    def test_already_kebab(self) -> None:
        """Already kebab-case is unchanged."""
        assert _to_cli_name("account-statements") == "account-statements"


class TestCommandGroupMeta:
    """Tests for the CommandGroupMeta frozen dataclass."""

    def test_create_minimal(self) -> None:
        """CommandGroupMeta can be created with name and help_text."""
        meta = CommandGroupMeta(name="accounts", help_text="Account management")
        assert meta.name == "accounts"
        assert meta.help_text == "Account management"
        assert meta.parent is None

    def test_with_parent(self) -> None:
        """CommandGroupMeta stores a parent class reference."""

        class ParentGroup:
            pass

        meta = CommandGroupMeta(name="sub", help_text="Sub group", parent=ParentGroup)
        assert meta.parent is ParentGroup

    def test_frozen(self) -> None:
        """CommandGroupMeta is frozen (immutable)."""
        import dataclasses

        meta = CommandGroupMeta(name="test", help_text="Test")
        with pytest.raises(dataclasses.FrozenInstanceError):
            meta.name = "changed"  # type: ignore[misc]

    def test_empty_name_rejected(self) -> None:
        """CommandGroupMeta rejects empty name."""
        with pytest.raises(ValueError, match="name must be non-empty"):
            CommandGroupMeta(name="", help_text="Test")


class TestCommandDecoratorOnClass:
    """Tests for @command decorator applied to classes (command groups)."""

    def test_class_gets_command_group_meta(self) -> None:
        """@command on a class stores _command_group_meta."""

        @command(help="Account management")
        class Accounts:
            def list_all(self) -> None:
                pass

        assert hasattr(Accounts, "_command_group_meta")
        meta = Accounts._command_group_meta
        assert isinstance(meta, CommandGroupMeta)
        assert meta.name == "accounts"
        assert meta.help_text == "Account management"

    def test_class_methods_auto_discovered(self) -> None:
        """Non-underscore methods on a @command class get _command_meta."""

        @command(help="Group")
        class MyGroup:
            def action(self) -> None:
                pass

            def _private(self) -> None:
                pass

        # Public method gets metadata
        assert hasattr(MyGroup.action, "_command_meta")
        # Private method does not
        assert not hasattr(MyGroup._private, "_command_meta")

    def test_explicitly_decorated_methods_preserved(self) -> None:
        """Methods already decorated with @command keep their metadata."""

        @command(help="Group")
        class MyGroup:
            @command(help="Custom help")
            def action(self) -> None:
                pass

        meta = MyGroup.action._command_meta
        assert meta.help_text == "Custom help"

    def test_class_with_parent(self) -> None:
        """@command on a class with parent= stores parent reference."""

        @command(help="Parent")
        class Parent:
            pass

        @command(help="Child", parent=Parent)
        class Child:
            pass

        assert Child._command_group_meta.parent is Parent

    def test_function_still_works(self) -> None:
        """@command on a function still stores _command_meta (backward compat)."""

        @command(help="Test function")
        def my_func(self: Any, ctx: Any) -> None:
            pass

        assert hasattr(my_func, "_command_meta")
        assert isinstance(my_func._command_meta, CommandMetadata)
        assert not hasattr(my_func, "_command_group_meta")

    def test_function_with_parent(self) -> None:
        """@command on a function with parent= stores parent in metadata."""

        @command(help="Parent")
        class Parent:
            pass

        @command(help="Standalone", parent=Parent)
        def standalone(ctx: Any) -> None:
            pass

        assert standalone._command_meta.parent is Parent  # type: ignore[attr-defined]


class TestCommandMetadataParentField:
    """Tests for the parent field on CommandMetadata."""

    def test_default_none(self) -> None:
        """CommandMetadata defaults parent to None."""
        meta = CommandMetadata(name="test", help_text="Test")
        assert meta.parent is None

    def test_set_parent(self) -> None:
        """CommandMetadata stores a parent class reference."""

        class MyGroup:
            pass

        meta = CommandMetadata(name="test", help_text="Test", parent=MyGroup)
        assert meta.parent is MyGroup


class TestCommandSpecGroupField:
    """Tests for the group field on CommandSpec."""

    def test_default_none(self) -> None:
        """CommandSpec defaults group to None."""
        spec = CommandSpec(name="test", handler=lambda: None)
        assert spec.group is None

    def test_set_group(self) -> None:
        """CommandSpec stores a group dotted path."""
        spec = CommandSpec(name="test", handler=lambda: None, group="accounts.statements")
        assert spec.group == "accounts.statements"


class TestCommandGroupMetaExport:
    """Tests for CommandGroupMeta export from plugins package."""

    def test_importable_from_plugins_package(self) -> None:
        """CommandGroupMeta is importable from graftpunk.plugins."""
        from graftpunk.plugins import CommandGroupMeta as PluginsCommandGroupMeta

        assert PluginsCommandGroupMeta is CommandGroupMeta

    def test_in_all(self) -> None:
        """CommandGroupMeta is listed in __all__."""
        import graftpunk.plugins

        assert "CommandGroupMeta" in graftpunk.plugins.__all__


class TestSavesSessionField:
    """Tests for saves_session field on CommandMetadata and CommandSpec."""

    def test_command_metadata_default_false(self) -> None:
        meta = CommandMetadata(name="test", help_text="help")
        assert meta.saves_session is False

    def test_command_metadata_explicit_true(self) -> None:
        meta = CommandMetadata(name="test", help_text="help", saves_session=True)
        assert meta.saves_session is True

    def test_command_spec_default_false(self) -> None:
        spec = CommandSpec(name="test", handler=lambda ctx: None)
        assert spec.saves_session is False

    def test_command_spec_explicit_true(self) -> None:
        spec = CommandSpec(name="test", handler=lambda ctx: None, saves_session=True)
        assert spec.saves_session is True


class TestCommandDecoratorSavesSession:
    """Tests for saves_session parameter on @command decorator."""

    def test_default_false(self) -> None:
        @command(help="test")
        def my_cmd(self: Any, ctx: Any) -> None:
            pass

        assert my_cmd._command_meta.saves_session is False  # type: ignore[attr-defined]

    def test_explicit_true(self) -> None:
        @command(help="test", saves_session=True)
        def my_cmd(self: Any, ctx: Any) -> None:
            pass

        assert my_cmd._command_meta.saves_session is True  # type: ignore[attr-defined]

    def test_saves_session_flows_to_command_spec(self) -> None:
        class TestPlugin(SitePlugin):
            site_name = "test_ss"
            session_name = "test_ss"
            help_text = "test"

            @command(help="test", saves_session=True)
            def my_cmd(self, ctx: Any) -> dict[str, str]:
                return {}

        plugin = TestPlugin()
        specs = plugin.get_commands()
        my_spec = next(s for s in specs if s.name == "my_cmd")
        assert my_spec.saves_session is True


class TestCommandContextSaveSession:
    """Tests for CommandContext.save_session() method."""

    def test_session_not_dirty_by_default(self) -> None:
        ctx = CommandContext(
            session=MagicMock(),
            plugin_name="test",
            command_name="cmd",
            api_version=1,
            _session_name="testsession",
        )
        assert ctx._session_dirty is False

    def test_save_session_sets_dirty_flag(self) -> None:
        ctx = CommandContext(
            session=MagicMock(),
            plugin_name="test",
            command_name="cmd",
            api_version=1,
            _session_name="testsession",
        )
        ctx.save_session()
        assert ctx._session_dirty is True

    def test_save_session_requires_session_name(self) -> None:
        ctx = CommandContext(
            session=MagicMock(),
            plugin_name="test",
            command_name="cmd",
            api_version=1,
        )
        with pytest.raises(ValueError, match="No session name"):
            ctx.save_session()

    def test_session_name_default_empty(self) -> None:
        ctx = CommandContext(
            session=MagicMock(),
            plugin_name="test",
            command_name="cmd",
            api_version=1,
        )
        assert ctx._session_name == ""


class TestPluginParamSpecClickKwargs:
    """Tests for PluginParamSpec with click_kwargs passthrough design."""

    def test_option_constructor_basic(self) -> None:
        """option() creates an option with help, type, default, and auto-detects is_flag."""
        # Bool option with default=False should auto-set is_flag=True
        spec = PluginParamSpec.option("verbose", type=bool, default=False, help="Enable verbose")
        assert spec.name == "verbose"
        assert spec.is_option is True
        assert spec.click_kwargs["type"] is bool
        assert spec.click_kwargs["default"] is False
        assert spec.click_kwargs["help"] == "Enable verbose"
        assert spec.click_kwargs["is_flag"] is True

    def test_option_constructor_string(self) -> None:
        """option() creates a string option with required=True."""
        spec = PluginParamSpec.option("username", type=str, required=True, help="User name")
        assert spec.name == "username"
        assert spec.is_option is True
        assert spec.click_kwargs["type"] is str
        assert spec.click_kwargs["required"] is True
        assert spec.click_kwargs["help"] == "User name"
        # No is_flag for string types
        assert "is_flag" not in spec.click_kwargs

    def test_argument_constructor(self) -> None:
        """argument() creates a positional argument with is_option=False."""
        spec = PluginParamSpec.argument("filename", type=str)
        assert spec.name == "filename"
        assert spec.is_option is False
        assert spec.click_kwargs["type"] is str
        assert spec.click_kwargs["required"] is True  # arguments default to required

    def test_raw_click_kwargs_passthrough(self) -> None:
        """Extra kwargs like show_default, envvar pass through to click_kwargs."""
        spec = PluginParamSpec.option(
            "port",
            type=int,
            default=8080,
            help="Port number",
            click_kwargs={"show_default": True, "envvar": "APP_PORT"},
        )
        assert spec.click_kwargs["show_default"] is True
        assert spec.click_kwargs["envvar"] == "APP_PORT"
        assert spec.click_kwargs["type"] is int
        assert spec.click_kwargs["default"] == 8080

    def test_direct_construction_with_click_kwargs(self) -> None:
        """Direct PluginParamSpec(...) construction with click_kwargs dict."""
        spec = PluginParamSpec(
            name="output",
            is_option=True,
            click_kwargs={"type": str, "default": "json", "help": "Output format"},
        )
        assert spec.name == "output"
        assert spec.is_option is True
        assert spec.click_kwargs["type"] is str
        assert spec.click_kwargs["default"] == "json"
        assert spec.click_kwargs["help"] == "Output format"

    def test_empty_name_raises(self) -> None:
        """PluginParamSpec still rejects empty name."""
        with pytest.raises(ValueError, match="PluginParamSpec.name must be non-empty"):
            PluginParamSpec(name="")

    def test_frozen(self) -> None:
        """PluginParamSpec is frozen (immutable)."""
        import dataclasses

        spec = PluginParamSpec.option("test", type=str, help="Test")
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.name = "changed"  # type: ignore[misc]

    def test_defensive_copy_click_kwargs(self) -> None:
        """click_kwargs dict is defensively copied to prevent external mutation."""
        original = {"type": str, "default": "hello"}
        spec = PluginParamSpec(name="test", click_kwargs=original)
        # Mutating the original should not affect the spec
        original["type"] = int
        assert spec.click_kwargs["type"] is str

    def test_option_bool_non_false_default_no_flag(self) -> None:
        """option() with type=bool but default=True should NOT auto-set is_flag."""
        spec = PluginParamSpec.option("flag", type=bool, default=True)
        assert "is_flag" not in spec.click_kwargs

    def test_option_default_values(self) -> None:
        """option() defaults: type=str, required=False, default=None, help=empty."""
        spec = PluginParamSpec.option("simple")
        assert spec.click_kwargs["type"] is str
        assert spec.click_kwargs["required"] is False
        assert spec.click_kwargs["default"] is None

    def test_argument_default_values(self) -> None:
        """argument() defaults: type=str, required=True, default=None."""
        spec = PluginParamSpec.argument("arg")
        assert spec.click_kwargs["type"] is str
        assert spec.click_kwargs["required"] is True
