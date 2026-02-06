"""Tests for graftpunk.cli.login_commands resolve functions."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from graftpunk.cli.login_commands import resolve_login_callable, resolve_login_fields
from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep, SitePlugin


class PluginWithLoginMethod(SitePlugin):
    """Plugin with a user-defined login() method (not a CLI command)."""

    site_name = "methlogin"
    session_name = "methlogin"
    help_text = "Method Login"
    base_url = "https://example.com"

    def login(self, credentials: dict[str, str]) -> bool:  # type: ignore[override]
        """Custom login method."""
        return True


class PluginWithDeclarativeLogin(SitePlugin):
    """Plugin with declarative LoginConfig."""

    site_name = "decllogin"
    session_name = "decllogin"
    help_text = "Declarative Login"
    base_url = "https://example.com"
    login_config = LoginConfig(
        steps=[
            LoginStep(
                fields={"username": "#user", "password": "#pass"},
                submit="#submit",
            ),
        ],
        url="/login",
        failure="Bad login.",
    )


class PluginWithNoLogin(SitePlugin):
    """Plugin with no login at all."""

    site_name = "nologin"
    session_name = "nologin"
    help_text = "No Login"
    base_url = "https://example.com"


class PluginWithCustomFields(SitePlugin):
    """Plugin with declarative LoginConfig that has custom fields."""

    site_name = "customfields"
    session_name = "customfields"
    help_text = "Custom Fields"
    base_url = "https://example.com"
    login_config = LoginConfig(
        steps=[
            LoginStep(
                fields={"email": "#email", "token": "#token"},
                submit="#go",
            ),
        ],
        url="/login",
    )


class PluginWithMultiStepLogin(SitePlugin):
    """Plugin with multi-step login (fields across multiple steps)."""

    site_name = "multistep"
    session_name = "multistep"
    help_text = "Multi-Step Login"
    base_url = "https://example.com"
    login_config = LoginConfig(
        steps=[
            LoginStep(
                fields={"username": "#user"},
                submit="#next",
            ),
            LoginStep(
                fields={"password": "#pass"},
                submit="#login",
            ),
            LoginStep(
                fields={"otp": "#otp-input"},
                submit="#verify",
            ),
        ],
        url="/login",
    )


class TestResolveLoginCallable:
    """Tests for resolve_login_callable()."""

    def test_plugin_with_login_method_returns_it(self) -> None:
        """Plugin with a login() method returns that method."""
        plugin = PluginWithLoginMethod()
        result = resolve_login_callable(plugin)
        assert result is not None
        assert callable(result)
        # Should be the actual method on the plugin
        assert result == plugin.login

    @patch("graftpunk.cli.login_commands.generate_login_method")
    def test_plugin_with_declarative_login_returns_generated(
        self, mock_generate: MagicMock
    ) -> None:
        """Plugin with declarative LoginConfig returns a generated login method."""
        mock_func = MagicMock()
        mock_generate.return_value = mock_func

        plugin = PluginWithDeclarativeLogin()
        result = resolve_login_callable(plugin)

        assert result is mock_func
        mock_generate.assert_called_once_with(plugin)

    def test_plugin_with_no_login_returns_none(self) -> None:
        """Plugin with no login method or config returns None."""
        plugin = PluginWithNoLogin()
        result = resolve_login_callable(plugin)
        assert result is None

    def test_plugin_with_command_decorated_login_skipped(self) -> None:
        """Plugin where login has _command_meta is NOT treated as a login method."""

        class CommandLogin(SitePlugin):
            site_name = "cmdlogin"
            session_name = "cmdlogin"
            help_text = "Cmd Login"
            base_url = "https://example.com"

            def login(self) -> None:  # type: ignore[override]
                pass

        # Simulate @command decoration on the underlying function
        CommandLogin.login._command_meta = MagicMock()  # type: ignore[attr-defined]
        plugin = CommandLogin()
        result = resolve_login_callable(plugin)
        assert result is None


class TestResolveLoginFields:
    """Tests for resolve_login_fields()."""

    def test_declarative_login_returns_fields_dict(self) -> None:
        """Plugin with LoginConfig returns its fields dict."""
        plugin = PluginWithCustomFields()
        result = resolve_login_fields(plugin)
        assert result == {"email": "#email", "token": "#token"}

    def test_no_login_config_returns_default(self) -> None:
        """Plugin with no login_config returns default username/password."""
        plugin = PluginWithNoLogin()
        result = resolve_login_fields(plugin)
        assert result == {"username": "", "password": ""}

    def test_login_method_plugin_returns_default(self) -> None:
        """Plugin with login() method but no LoginConfig returns default fields."""
        plugin = PluginWithLoginMethod()
        result = resolve_login_fields(plugin)
        assert result == {"username": "", "password": ""}

    def test_declarative_login_with_standard_fields(self) -> None:
        """Plugin with standard username/password LoginConfig returns those fields."""
        plugin = PluginWithDeclarativeLogin()
        result = resolve_login_fields(plugin)
        assert result == {"username": "#user", "password": "#pass"}

    def test_multi_step_login_aggregates_fields(self) -> None:
        """Plugin with multi-step login returns aggregated fields from all steps."""
        plugin = PluginWithMultiStepLogin()
        result = resolve_login_fields(plugin)
        assert result == {"username": "#user", "password": "#pass", "otp": "#otp-input"}
