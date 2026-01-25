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

    def test_missing_site_name(self) -> None:
        """Test that missing site_name raises error."""
        data = {"commands": {"test": {"url": "/test"}}}
        with pytest.raises(PluginError, match="missing required field 'site_name'"):
            validate_yaml_schema(data, Path("test.yaml"))

    def test_empty_site_name(self) -> None:
        """Test that empty site_name raises error."""
        data = {"site_name": "", "commands": {"test": {"url": "/test"}}}
        with pytest.raises(PluginError, match="must be a non-empty string"):
            validate_yaml_schema(data, Path("test.yaml"))

    def test_missing_commands(self) -> None:
        """Test that missing commands raises error."""
        data = {"site_name": "test"}
        with pytest.raises(PluginError, match="has no commands defined"):
            validate_yaml_schema(data, Path("test.yaml"))

    def test_empty_commands(self) -> None:
        """Test that empty commands dict raises error."""
        data = {"site_name": "test", "commands": {}}
        with pytest.raises(PluginError, match="has no commands defined"):
            validate_yaml_schema(data, Path("test.yaml"))

    def test_missing_url_in_command(self) -> None:
        """Test that command without url raises error."""
        data = {"site_name": "test", "commands": {"test": {"method": "GET"}}}
        with pytest.raises(PluginError, match="missing 'url' field"):
            validate_yaml_schema(data, Path("test.yaml"))

    def test_invalid_method(self) -> None:
        """Test that invalid HTTP method raises error."""
        data = {"site_name": "test", "commands": {"test": {"url": "/test", "method": "INVALID"}}}
        with pytest.raises(PluginError, match="invalid method"):
            validate_yaml_schema(data, Path("test.yaml"))

    def test_valid_schema_minimal(self) -> None:
        """Test that minimal valid schema passes."""
        data = {"site_name": "test", "commands": {"test": {"url": "/test"}}}
        # Should not raise
        validate_yaml_schema(data, Path("test.yaml"))

    def test_valid_schema_full(self) -> None:
        """Test that full valid schema passes."""
        data = {
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
        data = {
            "site_name": "test",
            "commands": {"test": {"url": "/test", "params": [{"type": "str"}]}},
        }
        with pytest.raises(PluginError, match="missing 'name' field"):
            validate_yaml_schema(data, Path("test.yaml"))

    def test_invalid_param_type(self) -> None:
        """Test that invalid param type raises error."""
        data = {
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

        plugin = parse_yaml_plugin(yaml_file)

        assert plugin.site_name == "httpbin"
        assert plugin.session_name == "httpbin"  # defaults to site_name
        assert plugin.help_text == "Commands for httpbin"
        assert plugin.base_url == ""
        assert plugin.headers == {}
        assert len(plugin.commands) == 1
        assert plugin.commands[0].name == "ip"
        assert plugin.commands[0].url == "/ip"
        assert plugin.commands[0].method == "GET"  # default
        assert plugin.commands[0].jmespath is None

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

        plugin = parse_yaml_plugin(yaml_file)

        assert plugin.site_name == "myapi"
        assert plugin.session_name == "custom_session"
        assert plugin.help_text == "My API commands"
        assert plugin.base_url == "https://api.example.com"
        assert plugin.headers == {"Authorization": "Bearer token", "X-Custom": "value"}
        assert len(plugin.commands) == 3

        # Check user command with params
        user_cmd = next(c for c in plugin.commands if c.name == "user")
        assert user_cmd.method == "GET"
        assert user_cmd.url == "/users/{id}"
        assert len(user_cmd.params) == 1
        assert user_cmd.params[0]["name"] == "id"
        assert user_cmd.params[0]["type"] == "int"
        assert user_cmd.params[0]["required"] is True
        assert user_cmd.params[0]["is_option"] is False

        # Check create command
        create_cmd = next(c for c in plugin.commands if c.name == "create")
        assert create_cmd.method == "POST"

    def test_parse_empty_session_name(self, tmp_path: Path) -> None:
        """Test parsing plugin with empty session_name."""
        yaml_content = """
site_name: httpbin
session_name: ""
commands:
  ip:
    url: "/ip"
"""
        yaml_file = tmp_path / "httpbin.yaml"
        yaml_file.write_text(yaml_content)

        plugin = parse_yaml_plugin(yaml_file)

        assert plugin.site_name == "httpbin"
        assert plugin.session_name == ""  # empty string means no session

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
        assert result.plugins[0].site_name == "testplugin"
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
        site_names = {p.site_name for p in result.plugins}
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
        assert result.plugins[0].site_name == "valid"
        # Error should be recorded for invalid plugin
        assert result.has_errors
        assert len(result.errors) == 1
        assert "invalid.yaml" in str(result.errors[0].filepath)
