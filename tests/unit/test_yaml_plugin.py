"""Tests for YAML site plugin factory (yaml_plugin.py)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from graftpunk.plugins.cli_plugin import (
    CommandContext,
    LoginConfig,
    LoginStep,
    PluginConfig,
    SitePlugin,
    build_plugin_config,
)
from graftpunk.plugins.yaml_loader import YAMLCommandDef, YAMLParamDef
from graftpunk.plugins.yaml_plugin import _convert_params, create_yaml_site_plugin


def _make_config(
    *,
    site_name: str = "testsite",
    session_name: str = "testsession",
    base_url: str = "https://api.example.com",
    requires_session: bool = True,
    login_config: LoginConfig | None = None,
) -> PluginConfig:
    """Helper to create a PluginConfig with sensible defaults."""
    return build_plugin_config(
        site_name=site_name,
        session_name=session_name,
        base_url=base_url,
        requires_session=requires_session,
        login_config=login_config,
    )


def _make_command(
    *,
    name: str = "test",
    url: str = "/test",
    method: str = "GET",
    params: tuple | None = None,
    headers: dict[str, str] | None = None,
    jmespath: str | None = None,
    raise_for_status: bool = True,
    timeout: float | None = None,
    max_retries: int = 0,
    rate_limit: float | None = None,
) -> YAMLCommandDef:
    """Helper to create a YAMLCommandDef."""
    return YAMLCommandDef(
        name=name,
        help_text=f"Help for {name}",
        method=method,
        url=url,
        params=params or (),
        headers=headers or {},
        jmespath=jmespath,
        raise_for_status=raise_for_status,
        timeout=timeout,
        max_retries=max_retries,
        rate_limit=rate_limit,
    )


def _mock_ctx(session: MagicMock | None = None) -> CommandContext:
    """Create a mock CommandContext wrapping a mock session."""
    if session is None:
        session = MagicMock(spec=requests.Session)
    return CommandContext(
        session=session,
        plugin_name="testsite",
        command_name="test",
        api_version=1,
    )


def _mock_response(
    *,
    json_data: object | None = None,
    text: str = "",
    headers: dict[str, str] | None = None,
    json_raises: bool = False,
) -> MagicMock:
    """Create a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.text = text
    resp.headers = headers or {}
    if json_raises:
        resp.json.side_effect = ValueError("No JSON")
    else:
        resp.json.return_value = json_data
    return resp


class TestLoginProperties:
    """Tests for login-related properties on the dynamic SitePlugin."""

    def test_login_properties_with_login_config(self) -> None:
        """When login is set, LoginConfig appears as attribute on the plugin."""
        config = _make_config(
            login_config=LoginConfig(
                steps=[
                    LoginStep(
                        fields={"username": "#user", "password": "#pass"},
                        submit="#submit",
                    )
                ],
                url="/login",
                failure="Bad login",
                success="Welcome",
            ),
        )
        plugin = create_yaml_site_plugin(config, [])

        assert plugin.login_config is not None
        assert plugin.login_config.url == "/login"
        assert len(plugin.login_config.steps) == 1
        assert plugin.login_config.steps[0].fields == {"username": "#user", "password": "#pass"}
        assert plugin.login_config.steps[0].submit == "#submit"
        assert plugin.login_config.failure == "Bad login"
        assert plugin.login_config.success == "Welcome"

    def test_login_properties_without_login_config(self) -> None:
        """When login is not configured, login_config is None."""
        config = _make_config()
        plugin = create_yaml_site_plugin(config, [])

        assert plugin.login_config is None


class TestPluginType:
    """Tests that the factory produces proper SitePlugin instances."""

    def test_plugin_is_site_plugin_instance(self) -> None:
        """The created plugin is an instance of SitePlugin."""
        config = _make_config()
        plugin = create_yaml_site_plugin(config, [])
        assert isinstance(plugin, SitePlugin)

    def test_plugin_has_correct_site_name(self) -> None:
        """The created plugin has the correct site_name."""
        config = _make_config(site_name="mysite")
        plugin = create_yaml_site_plugin(config, [])
        assert plugin.site_name == "mysite"

    def test_plugin_has_correct_session_name(self) -> None:
        """The created plugin has the correct session_name."""
        config = _make_config(session_name="mysession")
        plugin = create_yaml_site_plugin(config, [])
        assert plugin.session_name == "mysession"


class TestGetSession:
    """Tests for the plugin's get_session (inherited from SitePlugin)."""

    def test_get_session_with_session_name(self) -> None:
        """When requires_session is True, load_session_for_api is called."""
        config = _make_config(session_name="my_session")
        plugin = create_yaml_site_plugin(config, [])

        mock_session = MagicMock(spec=requests.Session)
        with patch(
            "graftpunk.plugins.cli_plugin.load_session_for_api",
            return_value=mock_session,
        ) as mock_load:
            result = plugin.get_session()

        mock_load.assert_called_once_with("my_session")
        assert result is mock_session

    def test_get_session_without_requires_session(self) -> None:
        """When requires_session is False, a plain requests.Session is returned."""
        config = _make_config(requires_session=False)
        plugin = create_yaml_site_plugin(config, [])

        session = plugin.get_session()
        assert isinstance(session, requests.Session)


class TestHandlerURLConstruction:
    """Tests for URL building inside the handler closure."""

    def test_base_url_prepended(self) -> None:
        """Handler prepends base_url to the command url."""
        cmd = _make_command(url="/items")
        config = _make_config(base_url="https://api.example.com")
        plugin = create_yaml_site_plugin(config, [cmd])
        commands = {c.name: c for c in plugin.get_commands()}
        handler = commands["test"].handler

        session = MagicMock(spec=requests.Session)
        session.request.return_value = _mock_response(json_data={"ok": True})
        ctx = _mock_ctx(session)

        handler(ctx)

        call_kwargs = session.request.call_args
        assert call_kwargs.kwargs["url"] == "https://api.example.com/items"

    def test_no_base_url(self) -> None:
        """Without base_url, the command url is used as-is."""
        cmd = _make_command(url="https://other.com/data")
        config = _make_config(base_url="")
        plugin = create_yaml_site_plugin(config, [cmd])
        commands = {c.name: c for c in plugin.get_commands()}
        handler = commands["test"].handler

        session = MagicMock(spec=requests.Session)
        session.request.return_value = _mock_response(json_data={})
        ctx = _mock_ctx(session)

        handler(ctx)

        assert session.request.call_args.kwargs["url"] == "https://other.com/data"

    def test_url_parameter_substitution(self) -> None:
        """URL {param} placeholders are substituted with kwarg values."""
        cmd = _make_command(url="/users/{user_id}/posts/{post_id}")
        config = _make_config()
        plugin = create_yaml_site_plugin(config, [cmd])
        handler = {c.name: c for c in plugin.get_commands()}["test"].handler

        session = MagicMock(spec=requests.Session)
        session.request.return_value = _mock_response(json_data=[])
        ctx = _mock_ctx(session)

        handler(ctx, user_id=42, post_id=7)

        assert session.request.call_args.kwargs["url"] == (
            "https://api.example.com/users/42/posts/7"
        )

    def test_missing_url_parameter_raises(self) -> None:
        """Unsubstituted URL parameters raise ValueError."""
        cmd = _make_command(url="/users/{user_id}")
        config = _make_config()
        plugin = create_yaml_site_plugin(config, [cmd])
        handler = {c.name: c for c in plugin.get_commands()}["test"].handler

        session = MagicMock(spec=requests.Session)
        ctx = _mock_ctx(session)

        with pytest.raises(ValueError, match="Missing required URL parameters: user_id"):
            handler(ctx)


class TestHandlerHeaders:
    """Tests for header expansion in the handler closure."""

    def test_plugin_and_command_headers_merged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Plugin-level and command-level headers are merged, command wins."""
        monkeypatch.setenv("MY_TOKEN", "abc123")
        cmd = _make_command(
            url="/test",
            headers={"X-Custom": "cmd-value", "Authorization": "Bearer ${MY_TOKEN}"},
        )
        config = _make_config()
        plugin = create_yaml_site_plugin(
            config,
            [cmd],
            plugin_headers={"X-Custom": "plugin-value", "Accept": "application/json"},
        )
        handler = {c.name: c for c in plugin.get_commands()}["test"].handler

        session = MagicMock(spec=requests.Session)
        session.request.return_value = _mock_response(json_data={})
        ctx = _mock_ctx(session)

        handler(ctx)

        headers_sent = session.request.call_args.kwargs["headers"]
        # Command header overrides plugin header
        assert headers_sent["X-Custom"] == "cmd-value"
        # Plugin header preserved
        assert headers_sent["Accept"] == "application/json"
        # Env var expanded
        assert headers_sent["Authorization"] == "Bearer abc123"


class TestHandlerQueryParams:
    """Tests for query parameter handling."""

    def test_non_url_kwargs_become_query_params(self) -> None:
        """kwargs that aren't URL params become query params."""
        cmd = _make_command(url="/users/{user_id}")
        config = _make_config()
        plugin = create_yaml_site_plugin(config, [cmd])
        handler = {c.name: c for c in plugin.get_commands()}["test"].handler

        session = MagicMock(spec=requests.Session)
        session.request.return_value = _mock_response(json_data=[])
        ctx = _mock_ctx(session)

        handler(ctx, user_id=1, page=2, limit=10)

        call_kwargs = session.request.call_args.kwargs
        assert call_kwargs["params"] == {"page": 2, "limit": 10}

    def test_none_kwargs_excluded_from_query_params(self) -> None:
        """kwargs with None values are excluded from query params."""
        cmd = _make_command(url="/items")
        config = _make_config()
        plugin = create_yaml_site_plugin(config, [cmd])
        handler = {c.name: c for c in plugin.get_commands()}["test"].handler

        session = MagicMock(spec=requests.Session)
        session.request.return_value = _mock_response(json_data=[])
        ctx = _mock_ctx(session)

        handler(ctx, page=1, filter=None)

        call_kwargs = session.request.call_args.kwargs
        assert call_kwargs["params"] == {"page": 1}


class TestHandlerResponseParsing:
    """Tests for response parsing in the handler."""

    def test_json_response_returned(self) -> None:
        """Valid JSON response is returned as parsed data."""
        cmd = _make_command(url="/data")
        config = _make_config()
        plugin = create_yaml_site_plugin(config, [cmd])
        handler = {c.name: c for c in plugin.get_commands()}["test"].handler

        session = MagicMock(spec=requests.Session)
        session.request.return_value = _mock_response(json_data={"items": [1, 2, 3]})
        ctx = _mock_ctx(session)

        result = handler(ctx)
        assert result == {"items": [1, 2, 3]}

    def test_non_json_response_returns_text(self) -> None:
        """When response is not JSON, returns response.text."""
        cmd = _make_command(url="/data")
        config = _make_config()
        plugin = create_yaml_site_plugin(config, [cmd])
        handler = {c.name: c for c in plugin.get_commands()}["test"].handler

        session = MagicMock(spec=requests.Session)
        session.request.return_value = _mock_response(
            json_raises=True,
            text="plain text content",
            headers={"Content-Type": "text/plain"},
        )
        ctx = _mock_ctx(session)

        result = handler(ctx)
        assert result == "plain text content"

    def test_html_response_warns_user(self) -> None:
        """HTML response triggers a warning via gp_console."""
        cmd = _make_command(url="/data")
        config = _make_config()
        plugin = create_yaml_site_plugin(config, [cmd])
        handler = {c.name: c for c in plugin.get_commands()}["test"].handler

        session = MagicMock(spec=requests.Session)
        session.request.return_value = _mock_response(
            json_raises=True,
            text="<!DOCTYPE html><html><body>Error</body></html>",
            headers={"Content-Type": "text/html"},
        )

        ctx = _mock_ctx(session)

        with patch("graftpunk.plugins.yaml_plugin.gp_console") as mock_console:
            result = handler(ctx)

        assert result == "<!DOCTYPE html><html><body>Error</body></html>"
        mock_console.warn.assert_called_once()
        call_arg = mock_console.warn.call_args[0][0]
        assert "Expected JSON but received HTML" in call_arg

    def test_raise_for_status_called(self) -> None:
        """When raise_for_status is True, response.raise_for_status is called."""
        cmd = _make_command(url="/data", raise_for_status=True)
        config = _make_config()
        plugin = create_yaml_site_plugin(config, [cmd])
        handler = {c.name: c for c in plugin.get_commands()}["test"].handler

        session = MagicMock(spec=requests.Session)
        resp = _mock_response(json_data={})
        session.request.return_value = resp
        ctx = _mock_ctx(session)

        handler(ctx)
        resp.raise_for_status.assert_called_once()

    def test_raise_for_status_not_called(self) -> None:
        """When raise_for_status is False, response.raise_for_status is NOT called."""
        cmd = _make_command(url="/data", raise_for_status=False)
        config = _make_config()
        plugin = create_yaml_site_plugin(config, [cmd])
        handler = {c.name: c for c in plugin.get_commands()}["test"].handler

        session = MagicMock(spec=requests.Session)
        resp = _mock_response(json_data={})
        session.request.return_value = resp
        ctx = _mock_ctx(session)

        handler(ctx)
        resp.raise_for_status.assert_not_called()


class TestHandlerJmespath:
    """Tests for jmespath filtering in the handler."""

    def test_jmespath_filter_applied(self) -> None:
        """When jmespath is set and installed, data is filtered."""
        cmd = _make_command(url="/data", jmespath="items[0]")
        config = _make_config()
        plugin = create_yaml_site_plugin(config, [cmd])
        handler = {c.name: c for c in plugin.get_commands()}["test"].handler

        session = MagicMock(spec=requests.Session)
        session.request.return_value = _mock_response(json_data={"items": ["first", "second"]})
        ctx = _mock_ctx(session)

        mock_jmespath = MagicMock()
        mock_jmespath.search.return_value = "first"

        with (
            patch("graftpunk.plugins.yaml_plugin.HAS_JMESPATH", True),
            patch("graftpunk.plugins.yaml_plugin._jmespath", mock_jmespath),
        ):
            result = handler(ctx)

        assert result == "first"
        mock_jmespath.search.assert_called_once_with("items[0]", {"items": ["first", "second"]})

    def test_jmespath_not_installed_warns(self) -> None:
        """When jmespath is not installed, a warning is printed and raw data returned."""
        cmd = _make_command(url="/data", jmespath="items[0]")
        config = _make_config()
        plugin = create_yaml_site_plugin(config, [cmd])
        handler = {c.name: c for c in plugin.get_commands()}["test"].handler

        session = MagicMock(spec=requests.Session)
        session.request.return_value = _mock_response(json_data={"items": ["first", "second"]})
        ctx = _mock_ctx(session)

        with (
            patch("graftpunk.plugins.yaml_plugin.HAS_JMESPATH", False),
            patch("graftpunk.plugins.yaml_plugin.gp_console") as mock_console,
        ):
            result = handler(ctx)

        # Returns unfiltered data
        assert result == {"items": ["first", "second"]}
        # Warning was printed
        mock_console.warn.assert_called_once()
        call_arg = mock_console.warn.call_args[0][0]
        assert "jmespath filter ignored" in call_arg

    def test_no_jmespath_filter_returns_data_as_is(self) -> None:
        """When no jmespath is specified, data is returned unfiltered."""
        cmd = _make_command(url="/data", jmespath=None)
        config = _make_config()
        plugin = create_yaml_site_plugin(config, [cmd])
        handler = {c.name: c for c in plugin.get_commands()}["test"].handler

        session = MagicMock(spec=requests.Session)
        session.request.return_value = _mock_response(json_data={"items": [1, 2, 3]})
        ctx = _mock_ctx(session)

        result = handler(ctx)
        assert result == {"items": [1, 2, 3]}


class TestResourceLimitsPassthrough:
    """Tests that resource limits are passed from YAMLCommandDef to CommandSpec."""

    def test_limits_passed_to_command_spec(self) -> None:
        """Resource limit fields are forwarded to CommandSpec."""
        cmd = _make_command(
            name="limited",
            url="/api/limited",
            timeout=30.0,
            max_retries=3,
            rate_limit=1.5,
        )
        config = _make_config()
        plugin = create_yaml_site_plugin(config, [cmd])
        commands = {c.name: c for c in plugin.get_commands()}

        spec = commands["limited"]
        assert spec.timeout == 30.0
        assert spec.max_retries == 3
        assert spec.rate_limit == 1.5

    def test_default_limits_passed_to_command_spec(self) -> None:
        """Default resource limit values are forwarded to CommandSpec."""
        cmd = _make_command(name="basic", url="/api/basic")
        config = _make_config()
        plugin = create_yaml_site_plugin(config, [cmd])
        commands = {c.name: c for c in plugin.get_commands()}

        spec = commands["basic"]
        assert spec.timeout is None
        assert spec.max_retries == 0
        assert spec.rate_limit is None

    def test_mixed_limits_across_commands(self) -> None:
        """Multiple commands have independent resource limits."""
        cmd_fast = _make_command(name="fast", url="/api/fast")
        cmd_slow = _make_command(
            name="slow",
            url="/api/slow",
            timeout=60.0,
            max_retries=5,
            rate_limit=0.5,
        )
        config = _make_config()
        plugin = create_yaml_site_plugin(config, [cmd_fast, cmd_slow])
        commands = {c.name: c for c in plugin.get_commands()}

        assert commands["fast"].timeout is None
        assert commands["fast"].max_retries == 0
        assert commands["fast"].rate_limit is None

        assert commands["slow"].timeout == 60.0
        assert commands["slow"].max_retries == 5
        assert commands["slow"].rate_limit == 0.5


class TestConvertParamsClickKwargs:
    """Tests for _convert_params producing click_kwargs."""

    def test_string_param_converts(self) -> None:
        cmd_def = YAMLCommandDef(
            name="test",
            help_text="",
            method="GET",
            url="/test",
            params=(YAMLParamDef(name="query", type="str", required=True),),
        )
        params = _convert_params(cmd_def)
        assert len(params) == 1
        assert params[0].name == "query"
        assert params[0].is_option is True
        assert params[0].click_kwargs["type"] is str
        assert params[0].click_kwargs["required"] is True

    def test_int_param_converts(self) -> None:
        cmd_def = YAMLCommandDef(
            name="test",
            help_text="",
            method="GET",
            url="/test",
            params=(YAMLParamDef(name="limit", type="int", default=10),),
        )
        params = _convert_params(cmd_def)
        assert params[0].click_kwargs["type"] is int
        assert params[0].click_kwargs["default"] == 10

    def test_bool_param_flag_detection(self) -> None:
        cmd_def = YAMLCommandDef(
            name="test",
            help_text="",
            method="GET",
            url="/test",
            params=(YAMLParamDef(name="verbose", type="bool", default=False),),
        )
        params = _convert_params(cmd_def)
        assert params[0].click_kwargs.get("is_flag") is True

    def test_float_param_converts(self) -> None:
        """YAML type 'float' maps to Python float."""
        cmd_def = YAMLCommandDef(
            name="test",
            help_text="",
            method="GET",
            url="/test",
            params=(YAMLParamDef(name="threshold", type="float", default=0.5),),
        )
        params = _convert_params(cmd_def)
        assert params[0].click_kwargs["type"] is float
        assert params[0].click_kwargs["default"] == 0.5

    def test_help_in_click_kwargs(self) -> None:
        cmd_def = YAMLCommandDef(
            name="test",
            help_text="",
            method="GET",
            url="/test",
            params=(YAMLParamDef(name="q", help="Search query"),),
        )
        params = _convert_params(cmd_def)
        assert params[0].click_kwargs["help"] == "Search query"

    def test_positional_argument(self) -> None:
        cmd_def = YAMLCommandDef(
            name="test",
            help_text="",
            method="GET",
            url="/test/{id}",
            params=(YAMLParamDef(name="id", is_option=False, required=True),),
        )
        params = _convert_params(cmd_def)
        assert params[0].is_option is False

    def test_help_not_in_click_kwargs_when_empty(self) -> None:
        cmd_def = YAMLCommandDef(
            name="test",
            help_text="",
            method="GET",
            url="/test",
            params=(YAMLParamDef(name="q"),),
        )
        params = _convert_params(cmd_def)
        assert "help" not in params[0].click_kwargs

    def test_unknown_type_defaults_to_str(self) -> None:
        """Unknown type strings in YAML params default to str and log a warning."""
        cmd_def = YAMLCommandDef(
            name="test",
            help_text="",
            method="GET",
            url="/test",
            params=(YAMLParamDef(name="x", type="custom_type"),),
        )
        with patch("graftpunk.plugins.yaml_plugin.LOG") as mock_log:
            params = _convert_params(cmd_def)
        assert params[0].click_kwargs["type"] is str
        mock_log.warning.assert_called_once_with(
            "unknown_yaml_param_type",
            param="x",
            type="custom_type",
            fallback="str",
        )

    def test_yaml_help_text_in_command_spec_click_kwargs(self) -> None:
        """YAML command help_text appears in CommandSpec.click_kwargs."""
        cmd = YAMLCommandDef(name="get", help_text="Get items", method="GET", url="/items")
        config = PluginConfig(site_name="test-help", session_name="test-help", help_text="Test")
        plugin = create_yaml_site_plugin(config, [cmd])
        spec = plugin.get_commands()[0]
        assert spec.click_kwargs["help"] == "Get items"
        assert spec.help_text == "Get items"

    def test_yaml_empty_help_text_empty_click_kwargs(self) -> None:
        """YAML command with empty help_text has empty click_kwargs."""
        cmd = YAMLCommandDef(name="t", help_text="", method="GET", url="/t")
        config = PluginConfig(site_name="test-empty", session_name="test-empty", help_text="Test")
        plugin = create_yaml_site_plugin(config, [cmd])
        spec = plugin.get_commands()[0]
        assert spec.click_kwargs == {}
