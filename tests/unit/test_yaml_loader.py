"""Tests for YAML plugin loader."""

from pathlib import Path

import pytest

from graftpunk.exceptions import PluginError
from graftpunk.plugins.yaml_loader import (
    discover_yaml_plugins,
    expand_env_vars,
    parse_yaml_plugin,
    validate_yaml_schema,
)


class TestExpandEnvVars:
    """Tests for environment variable expansion."""

    def test_expand_single_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test expanding a single environment variable."""
        monkeypatch.setenv("MY_TOKEN", "secret123")
        result = expand_env_vars("Bearer ${MY_TOKEN}")
        assert result == "Bearer secret123"

    def test_expand_multiple_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test expanding multiple environment variables."""
        monkeypatch.setenv("USER_NAME", "alice")
        monkeypatch.setenv("HOST_NAME", "example.com")
        result = expand_env_vars("${USER_NAME}@${HOST_NAME}")
        assert result == "alice@example.com"

    def test_missing_var_raises(self) -> None:
        """Test that missing env var raises PluginError."""
        with pytest.raises(PluginError, match="MISSING_VAR_12345 is not set"):
            expand_env_vars("${MISSING_VAR_12345}")

    def test_no_vars_unchanged(self) -> None:
        """Test that strings without variables are unchanged."""
        result = expand_env_vars("no variables here")
        assert result == "no variables here"

    def test_empty_string(self) -> None:
        """Test that empty string returns empty string."""
        result = expand_env_vars("")
        assert result == ""

    def test_partial_pattern_not_expanded(self) -> None:
        """Test that partial patterns like $VAR are not expanded."""
        result = expand_env_vars("$NOT_A_PATTERN")
        assert result == "$NOT_A_PATTERN"


class TestValidateYamlSchema:
    """Tests for YAML schema validation."""

    def test_missing_site_name_passes_validation(self) -> None:
        """Test that missing site_name passes schema validation.

        site_name inference is handled by build_plugin_config, not validate_yaml_schema.
        """
        data: dict[str, object] = {"commands": {"test": {"url": "/test"}}}
        # Should not raise -- site_name check is deferred to build_plugin_config
        validate_yaml_schema(data, Path("test.yaml"))

    def test_empty_site_name(self) -> None:
        """Test that empty site_name raises error."""
        data: dict[str, object] = {"site_name": "", "commands": {"test": {"url": "/test"}}}
        with pytest.raises(PluginError, match="must be a non-empty string"):
            validate_yaml_schema(data, Path("test.yaml"))

    def test_missing_commands(self) -> None:
        """Test that missing commands raises error."""
        data: dict[str, object] = {"site_name": "test"}
        with pytest.raises(PluginError, match="has no commands defined"):
            validate_yaml_schema(data, Path("test.yaml"))

    def test_empty_commands(self) -> None:
        """Test that empty commands dict raises error."""
        data: dict[str, object] = {"site_name": "test", "commands": {}}
        with pytest.raises(PluginError, match="has no commands defined"):
            validate_yaml_schema(data, Path("test.yaml"))

    def test_missing_url_in_command(self) -> None:
        """Test that command without url raises error."""
        data: dict[str, object] = {
            "site_name": "test",
            "commands": {"test": {"method": "GET"}},
        }
        with pytest.raises(PluginError, match="missing 'url' field"):
            validate_yaml_schema(data, Path("test.yaml"))

    def test_invalid_method(self) -> None:
        """Test that invalid HTTP method raises error."""
        data: dict[str, object] = {
            "site_name": "test",
            "commands": {"test": {"url": "/test", "method": "INVALID"}},
        }
        with pytest.raises(PluginError, match="invalid method"):
            validate_yaml_schema(data, Path("test.yaml"))

    def test_valid_schema_minimal(self) -> None:
        """Test that minimal valid schema passes."""
        data: dict[str, object] = {"site_name": "test", "commands": {"test": {"url": "/test"}}}
        # Should not raise
        validate_yaml_schema(data, Path("test.yaml"))

    def test_valid_schema_full(self) -> None:
        """Test that full valid schema passes."""
        data: dict[str, object] = {
            "site_name": "test",
            "session_name": "test_session",
            "help": "Test plugin",
            "base_url": "https://api.example.com",
            "headers": {"Authorization": "Bearer token"},
            "commands": {
                "list": {
                    "help": "List items",
                    "method": "GET",
                    "url": "/items",
                    "jmespath": "data",
                },
                "get": {
                    "help": "Get item",
                    "method": "GET",
                    "url": "/items/{id}",
                    "params": [{"name": "id", "type": "int", "required": True, "is_option": False}],
                },
            },
        }
        # Should not raise
        validate_yaml_schema(data, Path("test.yaml"))

    def test_param_without_name(self) -> None:
        """Test that param without name raises error."""
        data: dict[str, object] = {
            "site_name": "test",
            "commands": {"test": {"url": "/test", "params": [{"type": "str"}]}},
        }
        with pytest.raises(PluginError, match="missing 'name' field"):
            validate_yaml_schema(data, Path("test.yaml"))

    def test_invalid_param_type(self) -> None:
        """Test that invalid param type raises error."""
        data: dict[str, object] = {
            "site_name": "test",
            "commands": {"test": {"url": "/test", "params": [{"name": "x", "type": "invalid"}]}},
        }
        with pytest.raises(PluginError, match="invalid type"):
            validate_yaml_schema(data, Path("test.yaml"))


class TestParseYamlPlugin:
    """Tests for parsing YAML plugin files."""

    def test_parse_minimal_plugin(self, tmp_path: Path) -> None:
        """Test parsing a minimal YAML plugin."""
        yaml_content = """
site_name: httpbin
commands:
  ip:
    url: "/ip"
"""
        yaml_file = tmp_path / "httpbin.yaml"
        yaml_file.write_text(yaml_content)

        config, commands, headers = parse_yaml_plugin(yaml_file)

        assert config.site_name == "httpbin"
        assert config.session_name == "httpbin"  # defaults to site_name
        assert config.help_text == "Commands for httpbin"
        assert config.base_url == ""
        assert headers == {}
        assert len(commands) == 1
        assert commands[0].name == "ip"
        assert commands[0].url == "/ip"
        assert commands[0].method == "GET"  # default
        assert commands[0].jmespath is None

    def test_parse_plugin_infers_site_name_from_filename(self, tmp_path: Path) -> None:
        """Test that site_name is inferred from filename when not specified."""
        yaml_content = """
commands:
  ip:
    url: "/ip"
"""
        yaml_file = tmp_path / "httpbin.yaml"
        yaml_file.write_text(yaml_content)

        config, commands, headers = parse_yaml_plugin(yaml_file)

        assert config.site_name == "httpbin"
        assert config.session_name == "httpbin"

    def test_parse_full_plugin(self, tmp_path: Path) -> None:
        """Test parsing a full YAML plugin with all options."""
        yaml_content = """
site_name: myapi
session_name: custom_session
help: "My API commands"
base_url: "https://api.example.com"
headers:
  Authorization: "Bearer token"
  X-Custom: "value"
commands:
  users:
    help: "List users"
    method: GET
    url: "/users"
    jmespath: "data"
  user:
    help: "Get user by ID"
    method: GET
    url: "/users/{id}"
    params:
      - name: id
        type: int
        required: true
        help: "User ID"
        is_option: false
  create:
    help: "Create user"
    method: POST
    url: "/users"
"""
        yaml_file = tmp_path / "myapi.yaml"
        yaml_file.write_text(yaml_content)

        config, commands, headers = parse_yaml_plugin(yaml_file)

        assert config.site_name == "myapi"
        assert config.session_name == "custom_session"
        assert config.help_text == "My API commands"
        assert config.base_url == "https://api.example.com"
        assert headers == {"Authorization": "Bearer token", "X-Custom": "value"}
        assert len(commands) == 3

        # Check user command with params
        user_cmd = next(c for c in commands if c.name == "user")
        assert user_cmd.method == "GET"
        assert user_cmd.url == "/users/{id}"
        assert len(user_cmd.params) == 1
        assert user_cmd.params[0].name == "id"
        assert user_cmd.params[0].type == "int"
        assert user_cmd.params[0].required is True
        assert user_cmd.params[0].is_option is False

        # Check create command
        create_cmd = next(c for c in commands if c.name == "create")
        assert create_cmd.method == "POST"

    def test_parse_empty_session_name_defaults_to_site_name(self, tmp_path: Path) -> None:
        """Test that empty session_name defaults to site_name."""
        yaml_content = """
site_name: httpbin
session_name: ""
commands:
  ip:
    url: "/ip"
"""
        yaml_file = tmp_path / "httpbin.yaml"
        yaml_file.write_text(yaml_content)

        config, commands, headers = parse_yaml_plugin(yaml_file)

        assert config.site_name == "httpbin"
        assert config.session_name == "httpbin"  # defaults to site_name
        assert config.requires_session is True

    def test_parse_requires_session_false(self, tmp_path: Path) -> None:
        """Test that requires_session: false opts out of session loading."""
        yaml_content = """
site_name: httpbin
requires_session: false
commands:
  ip:
    url: "/ip"
"""
        yaml_file = tmp_path / "httpbin.yaml"
        yaml_file.write_text(yaml_content)

        config, commands, headers = parse_yaml_plugin(yaml_file)

        assert config.site_name == "httpbin"
        assert config.session_name == "httpbin"  # still defaults
        assert config.requires_session is False

    def test_parse_invalid_yaml(self, tmp_path: Path) -> None:
        """Test that invalid YAML raises PluginError."""
        yaml_file = tmp_path / "invalid.yaml"
        yaml_file.write_text("this: is: not: valid: yaml:")

        with pytest.raises(PluginError, match="Invalid YAML"):
            parse_yaml_plugin(yaml_file)

    def test_parse_empty_file(self, tmp_path: Path) -> None:
        """Test that empty file raises PluginError."""
        yaml_file = tmp_path / "empty.yaml"
        yaml_file.write_text("")

        with pytest.raises(PluginError, match="is empty"):
            parse_yaml_plugin(yaml_file)

    def test_parse_nonexistent_file(self, tmp_path: Path) -> None:
        """Test that nonexistent file raises PluginError."""
        yaml_file = tmp_path / "nonexistent.yaml"

        with pytest.raises(PluginError, match="Cannot read"):
            parse_yaml_plugin(yaml_file)


class TestDiscoverYamlPlugins:
    """Tests for YAML plugin discovery."""

    def test_discover_no_plugins_dir(self, isolated_config: Path) -> None:
        """Test discovery when plugins directory doesn't exist."""
        result = discover_yaml_plugins()
        assert result.plugins == []
        assert result.errors == []

    def test_discover_empty_plugins_dir(self, isolated_config: Path) -> None:
        """Test discovery with empty plugins directory."""
        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()

        result = discover_yaml_plugins()
        assert result.plugins == []
        assert result.errors == []

    def test_discover_single_plugin(self, isolated_config: Path) -> None:
        """Test discovering a single YAML plugin."""
        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()

        yaml_content = """
site_name: testplugin
commands:
  test:
    url: "/test"
"""
        (plugins_dir / "test.yaml").write_text(yaml_content)

        result = discover_yaml_plugins()
        assert len(result.plugins) == 1
        assert result.plugins[0][0].site_name == "testplugin"
        assert result.errors == []

    def test_discover_multiple_plugins(self, isolated_config: Path) -> None:
        """Test discovering multiple YAML plugins."""
        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()

        (plugins_dir / "plugin1.yaml").write_text(
            "site_name: plugin1\ncommands:\n  cmd:\n    url: /a"
        )
        (plugins_dir / "plugin2.yaml").write_text(
            "site_name: plugin2\ncommands:\n  cmd:\n    url: /b"
        )
        (plugins_dir / "plugin3.yml").write_text(
            "site_name: plugin3\ncommands:\n  cmd:\n    url: /c"
        )

        result = discover_yaml_plugins()
        assert len(result.plugins) == 3
        site_names = {p[0].site_name for p in result.plugins}
        assert site_names == {"plugin1", "plugin2", "plugin3"}
        assert result.errors == []

    def test_discover_skips_invalid_plugins(self, isolated_config: Path) -> None:
        """Test that invalid plugins are skipped and error is recorded."""
        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()

        # Valid plugin
        (plugins_dir / "valid.yaml").write_text("site_name: valid\ncommands:\n  cmd:\n    url: /a")
        # Invalid plugin (missing commands)
        (plugins_dir / "invalid.yaml").write_text("site_name: invalid")

        result = discover_yaml_plugins()
        # Only valid plugin should be loaded
        assert len(result.plugins) == 1
        assert result.plugins[0][0].site_name == "valid"
        # Error should be recorded for invalid plugin
        assert result.has_errors
        assert len(result.errors) == 1
        assert "invalid.yaml" in str(result.errors[0].filepath)


class TestYAMLLoginBlock:
    """Tests for YAML login block parsing."""

    def test_parse_login_block(self, isolated_config: Path) -> None:
        """Test parsing a YAML plugin with login block."""
        yaml_content = """
site_name: hn
session_name: hackernews
base_url: "https://news.ycombinator.com"
backend: nodriver

login:
  url: /login
  fields:
    username: "input[name='acct']"
    password: "input[name='pw']"
  submit: "input[value='login']"
  failure: "Bad login."

commands:
  front:
    help: "Get front page"
    url: "/news"
"""
        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir(exist_ok=True)
        yaml_file = plugins_dir / "hn.yaml"
        yaml_file.write_text(yaml_content)

        config, commands, headers = parse_yaml_plugin(yaml_file)

        assert config.login_config is not None
        assert config.login_config.url == "/login"
        assert config.login_config.fields == {
            "username": "input[name='acct']",
            "password": "input[name='pw']",
        }
        assert config.login_config.submit == "input[value='login']"
        assert config.login_config.failure == "Bad login."
        assert config.login_config.success == ""
        assert config.backend == "nodriver"

    def test_parse_no_login_block(self, isolated_config: Path) -> None:
        """Test parsing YAML without login block."""
        yaml_content = """
site_name: simple
commands:
  ping:
    url: "/ping"
"""
        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir(exist_ok=True)
        yaml_file = plugins_dir / "simple.yaml"
        yaml_file.write_text(yaml_content)

        config, commands, headers = parse_yaml_plugin(yaml_file)
        assert config.login_config is None
        assert config.backend == "selenium"  # default

    def test_parse_nested_login_block(self, tmp_path: Path) -> None:
        """Nested login: block is flattened to login_url/login_fields/login_submit."""
        yaml_content = """
site_name: mysite
base_url: "https://example.com"
login:
  url: "/login"
  fields:
    username: "#user"
    password: "#pass"
  submit: "#submit"
  failure: "Bad login"
commands:
  cmd:
    url: "/api"
"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(yaml_content)
        config, commands, headers = parse_yaml_plugin(yaml_file)
        assert config.login_config is not None
        assert config.login_config.url == "/login"
        assert config.login_config.fields == {"username": "#user", "password": "#pass"}
        assert config.login_config.submit == "#submit"
        assert config.login_config.failure == "Bad login"

    def test_login_with_success_selector(self, tmp_path: Path) -> None:
        """Login block with success selector is parsed correctly."""
        yaml_content = """
site_name: mysite
base_url: "https://example.com"
login:
  url: "/login"
  fields:
    username: "input#email"
    password: "input#pass"
  submit: "button[type=submit]"
  failure: "Invalid credentials"
  success: ".dashboard"
commands:
  search:
    url: "/api/search"
"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(yaml_content)
        config, commands, headers = parse_yaml_plugin(yaml_file)
        assert config.login_config is not None
        assert config.login_config.url == "/login"
        assert config.login_config.fields == {"username": "input#email", "password": "input#pass"}
        assert config.login_config.submit == "button[type=submit]"
        assert config.login_config.failure == "Invalid credentials"
        assert config.login_config.success == ".dashboard"

    def test_parse_flat_login_fields(self, tmp_path: Path) -> None:
        """Flat login_url/login_fields work directly."""
        yaml_content = """
site_name: mysite
base_url: "https://example.com"
login_url: "/login"
login_fields:
  username: "#user"
  password: "#pass"
login_submit: "#submit"
commands:
  cmd:
    url: "/api"
"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(yaml_content)
        config, commands, headers = parse_yaml_plugin(yaml_file)
        assert config.login_config is not None
        assert config.login_config.url == "/login"
        assert config.login_config.fields == {"username": "#user", "password": "#pass"}
        assert config.login_config.submit == "#submit"


class TestYAMLResourceLimits:
    """Tests for resource limit fields on YAML commands."""

    def test_command_with_timeout(self, tmp_path: Path) -> None:
        """Test that timeout is parsed from YAML command definition."""
        yaml_content = """
site_name: test-site
base_url: "https://example.com"
commands:
  slow:
    url: "/api/slow"
    timeout: 30
"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(yaml_content)
        config, commands, headers = parse_yaml_plugin(yaml_file)
        assert commands[0].timeout == 30

    def test_command_with_all_limits(self, tmp_path: Path) -> None:
        """Test that all resource limit fields are parsed."""
        yaml_content = """
site_name: test-site
base_url: "https://example.com"
commands:
  limited:
    url: "/api/limited"
    timeout: 60
    max_retries: 3
    rate_limit: 2.0
"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(yaml_content)
        config, commands, headers = parse_yaml_plugin(yaml_file)
        assert commands[0].timeout == 60
        assert commands[0].max_retries == 3
        assert commands[0].rate_limit == 2.0

    def test_command_defaults_no_limits(self, tmp_path: Path) -> None:
        """Test that resource limits default correctly when not specified."""
        yaml_content = """
site_name: test-site
base_url: "https://example.com"
commands:
  basic:
    url: "/api/basic"
"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(yaml_content)
        config, commands, headers = parse_yaml_plugin(yaml_file)
        assert commands[0].timeout is None
        assert commands[0].max_retries == 0
        assert commands[0].rate_limit is None

    def test_mixed_commands_some_with_limits(self, tmp_path: Path) -> None:
        """Test that commands with and without limits coexist correctly."""
        yaml_content = """
site_name: test-site
base_url: "https://example.com"
commands:
  fast:
    url: "/api/fast"
  slow:
    url: "/api/slow"
    timeout: 120
    max_retries: 5
    rate_limit: 0.5
"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(yaml_content)
        config, commands, headers = parse_yaml_plugin(yaml_file)

        fast_cmd = next(c for c in commands if c.name == "fast")
        assert fast_cmd.timeout is None
        assert fast_cmd.max_retries == 0
        assert fast_cmd.rate_limit is None

        slow_cmd = next(c for c in commands if c.name == "slow")
        assert slow_cmd.timeout == 120
        assert slow_cmd.max_retries == 5
        assert slow_cmd.rate_limit == 0.5


class TestYAMLTokenConfig:
    """Tests for YAML token config parsing."""

    def test_parse_tokens_from_yaml(self, tmp_path: Path) -> None:
        """tokens: block in YAML produces TokenConfig on PluginConfig."""
        yaml_content = """
site_name: testsite
base_url: https://example.com
commands:
  test:
    url: /api/test
    help: "Test command"
tokens:
  - name: X-CSRF-Token
    source: page
    pattern: 'csrf_token\\s*=\\s*"([^"]+)"'
    page_url: /
    cache_duration: 600
  - name: X-CSRFToken
    source: cookie
    cookie_name: csrftoken
"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(yaml_content)

        config, commands, headers = parse_yaml_plugin(yaml_file)

        assert config.token_config is not None
        assert len(config.token_config.tokens) == 2

        csrf_token = config.token_config.tokens[0]
        assert csrf_token.name == "X-CSRF-Token"
        assert csrf_token.source == "page"
        assert csrf_token.pattern == 'csrf_token\\s*=\\s*"([^"]+)"'
        assert csrf_token.page_url == "/"
        assert csrf_token.cache_duration == 600

        cookie_token = config.token_config.tokens[1]
        assert cookie_token.name == "X-CSRFToken"
        assert cookie_token.source == "cookie"
        assert cookie_token.cookie_name == "csrftoken"

    def test_parse_tokens_page_source(self, tmp_path: Path) -> None:
        """Token with source=page and pattern is parsed correctly."""
        yaml_content = """
site_name: testsite
base_url: https://example.com
commands:
  test:
    url: /api/test
tokens:
  - name: X-Token
    source: page
    pattern: 'token="([^"]+)"'
    page_url: /dashboard
    cache_duration: 120
"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(yaml_content)

        config, commands, headers = parse_yaml_plugin(yaml_file)

        assert config.token_config is not None
        token = config.token_config.tokens[0]
        assert token.name == "X-Token"
        assert token.source == "page"
        assert token.pattern == 'token="([^"]+)"'
        assert token.page_url == "/dashboard"
        assert token.cache_duration == 120

    def test_parse_tokens_cookie_source(self, tmp_path: Path) -> None:
        """Token with source=cookie and cookie_name is parsed correctly."""
        yaml_content = """
site_name: testsite
base_url: https://example.com
commands:
  test:
    url: /api/test
tokens:
  - name: X-CSRFToken
    source: cookie
    cookie_name: csrftoken
"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(yaml_content)

        config, commands, headers = parse_yaml_plugin(yaml_file)

        assert config.token_config is not None
        token = config.token_config.tokens[0]
        assert token.name == "X-CSRFToken"
        assert token.source == "cookie"
        assert token.cookie_name == "csrftoken"
        assert token.cache_duration == 300  # default

    def test_parse_tokens_invalid_not_list(self, tmp_path: Path) -> None:
        """tokens: as non-list raises PluginError."""
        yaml_content = """
site_name: testsite
base_url: https://example.com
commands:
  test:
    url: /api/test
tokens:
  name: X-Token
  source: page
"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(yaml_content)

        with pytest.raises(PluginError, match="'tokens' must be a list"):
            parse_yaml_plugin(yaml_file)

    def test_parse_tokens_missing_name(self, tmp_path: Path) -> None:
        """Token without name field raises PluginError."""
        yaml_content = """
site_name: testsite
base_url: https://example.com
commands:
  test:
    url: /api/test
tokens:
  - source: cookie
    cookie_name: csrftoken
"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(yaml_content)

        with pytest.raises(PluginError, match="missing required field"):
            parse_yaml_plugin(yaml_file)

    def test_no_tokens_block_is_none(self, tmp_path: Path) -> None:
        """No tokens: block means token_config is None."""
        yaml_content = """
site_name: testsite
base_url: https://example.com
commands:
  test:
    url: /api/test
"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(yaml_content)

        config, commands, headers = parse_yaml_plugin(yaml_file)
        assert config.token_config is None
