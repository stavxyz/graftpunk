"""Tests for HAR plugin code generator."""

from __future__ import annotations

from graftpunk.har.analyzer import APIEndpoint, AuthFlow, AuthStep
from graftpunk.har.generator import (
    _unique_name,
    generate_plugin_code,
    generate_yaml_plugin,
)


class TestUniqueName:
    """Tests for _unique_name helper function."""

    def test_unique_name_no_conflict(self) -> None:
        """Test that name is returned unchanged when no conflict."""
        seen: set[str] = set()
        result = _unique_name("get_users", seen)
        assert result == "get_users"
        assert "get_users" in seen

    def test_unique_name_single_conflict(self) -> None:
        """Test that _1 suffix is added on first conflict."""
        seen: set[str] = {"get_users"}
        result = _unique_name("get_users", seen)
        assert result == "get_users_1"
        assert "get_users_1" in seen

    def test_unique_name_multiple_conflicts(self) -> None:
        """Test that counter increments for multiple conflicts."""
        seen: set[str] = {"get_users", "get_users_1", "get_users_2"}
        result = _unique_name("get_users", seen)
        assert result == "get_users_3"
        assert "get_users_3" in seen

    def test_unique_name_updates_seen_set(self) -> None:
        """Test that seen set is updated with new name."""
        seen: set[str] = set()
        _unique_name("name1", seen)
        _unique_name("name2", seen)
        _unique_name("name1", seen)  # Should become name1_1
        assert seen == {"name1", "name2", "name1_1"}

    def test_unique_name_sequential_calls(self) -> None:
        """Test sequential calls with same base name."""
        seen: set[str] = set()
        results = [_unique_name("cmd", seen) for _ in range(5)]
        assert results == ["cmd", "cmd_1", "cmd_2", "cmd_3", "cmd_4"]

    def test_unique_name_empty_seen(self) -> None:
        """Test with empty seen set."""
        seen: set[str] = set()
        result = _unique_name("test", seen)
        assert result == "test"
        assert len(seen) == 1

    def test_unique_name_preserves_existing(self) -> None:
        """Test that existing entries in seen are preserved."""
        seen: set[str] = {"other", "entries"}
        _unique_name("new", seen)
        assert "other" in seen
        assert "entries" in seen
        assert "new" in seen


class TestGeneratePluginCode:
    """Tests for generate_plugin_code function."""

    def test_generate_minimal_plugin(self) -> None:
        """Test generating plugin with no endpoints or auth flow."""
        code = generate_plugin_code(
            site_name="test",
            domain="example.com",
            auth_flow=None,
            endpoints=[],
        )

        assert "class TestPlugin(SitePlugin):" in code
        assert 'site_name = "test"' in code
        assert 'session_name = "test"' in code
        assert "example.com" in code
        assert "from graftpunk.plugins import SitePlugin, command" in code

    def test_generate_plugin_with_endpoints(self) -> None:
        """Test generating plugin with API endpoints."""
        endpoints = [
            APIEndpoint(
                method="GET",
                url="https://example.com/api/users",
                path="/api/users",
                params=[],
                description="Get all users",
            ),
            APIEndpoint(
                method="POST",
                url="https://example.com/api/users",
                path="/api/users",
                params=[],
                description="Create user",
            ),
        ]

        code = generate_plugin_code(
            site_name="mysite",
            domain="example.com",
            auth_flow=None,
            endpoints=endpoints,
        )

        assert "class MysitePlugin(SitePlugin):" in code
        assert 'help="Get all users"' in code
        assert 'help="Create user"' in code
        assert "def api_users(" in code or "def users(" in code
        assert "def post_" in code  # POST should have method prefix

    def test_generate_plugin_with_path_params(self) -> None:
        """Test generating plugin with parameterized endpoints."""
        endpoints = [
            APIEndpoint(
                method="GET",
                url="https://example.com/api/users/{user_id}",
                path="/api/users/{user_id}",
                params=["user_id"],
                description="Get user by ID",
            ),
        ]

        code = generate_plugin_code(
            site_name="api",
            domain="example.com",
            auth_flow=None,
            endpoints=endpoints,
        )

        assert "user_id: str" in code
        assert 'url = f"https://example.com/api/users/{user_id}"' in code

    def test_generate_plugin_with_auth_flow(self) -> None:
        """Test generating plugin with auth flow comments."""
        auth_flow = AuthFlow(
            steps=[
                AuthStep(
                    entry=None,  # type: ignore[arg-type]
                    step_type="form_page",
                    description="GET /login (form page)",
                    cookies_set=[],
                ),
                AuthStep(
                    entry=None,  # type: ignore[arg-type]
                    step_type="login_submit",
                    description="POST /login (credentials)",
                    cookies_set=["sessionId"],
                ),
            ],
            session_cookies=["sessionId", "authToken"],
            auth_type="form",
        )

        code = generate_plugin_code(
            site_name="secure",
            domain="example.com",
            auth_flow=auth_flow,
            endpoints=[],
        )

        assert "# Detected authentication flow:" in code
        assert "GET /login" in code
        assert "POST /login" in code
        assert "sessionId, authToken" in code

    def test_generate_plugin_sanitizes_names(self) -> None:
        """Test that invalid Python identifiers are sanitized."""
        code = generate_plugin_code(
            site_name="my-site-123",
            domain="example.com",
            auth_flow=None,
            endpoints=[],
        )

        assert "class MySite123Plugin(SitePlugin):" in code
        assert 'site_name = "my_site_123"' in code

    def test_generate_plugin_unique_command_names(self) -> None:
        """Test that duplicate command names get unique suffixes."""
        endpoints = [
            APIEndpoint(
                method="GET",
                url="https://example.com/api/v1/users",
                path="/api/v1/users",
                params=[],
                description="Get users v1",
            ),
            APIEndpoint(
                method="GET",
                url="https://example.com/api/v2/users",
                path="/api/v2/users",
                params=[],
                description="Get users v2",
            ),
        ]

        code = generate_plugin_code(
            site_name="test",
            domain="example.com",
            auth_flow=None,
            endpoints=endpoints,
        )

        # Extract method definitions
        import re

        method_names = re.findall(r"def (\w+)\(", code)
        # Filter out built-in/inherited methods
        command_methods = [m for m in method_names if not m.startswith("_")]

        # Should have exactly 2 unique command names
        assert len(command_methods) == 2
        assert len(set(command_methods)) == 2  # All names must be unique
        # One should have a suffix (e.g., v1_users and v1_users_1 or similar)
        assert command_methods[0] != command_methods[1]


class TestGenerateYamlPlugin:
    """Tests for generate_yaml_plugin function."""

    def test_generate_minimal_yaml_plugin(self) -> None:
        """Test generating YAML plugin with no endpoints."""
        yaml = generate_yaml_plugin(
            site_name="test",
            domain="example.com",
            auth_flow=None,
            endpoints=[],
        )

        assert "site_name: test" in yaml
        assert "session_name: test" in yaml
        assert "base_url: https://example.com" in yaml
        assert "commands:" in yaml
        assert "example:" in yaml  # Placeholder command

    def test_generate_yaml_plugin_with_endpoints(self) -> None:
        """Test generating YAML plugin with API endpoints."""
        endpoints = [
            APIEndpoint(
                method="GET",
                url="https://example.com/api/users",
                path="/api/users",
                params=[],
                description="Get all users",
            ),
            APIEndpoint(
                method="DELETE",
                url="https://example.com/api/users/{user_id}",
                path="/api/users/{user_id}",
                params=["user_id"],
                description="Delete user",
            ),
        ]

        yaml = generate_yaml_plugin(
            site_name="mysite",
            domain="example.com",
            auth_flow=None,
            endpoints=endpoints,
        )

        assert "site_name: mysite" in yaml
        assert 'help: "Get all users"' in yaml
        assert "method: GET" in yaml
        assert "method: DELETE" in yaml
        assert 'url: "/api/users"' in yaml
        assert 'url: "/api/users/{user_id}"' in yaml
        assert "- name: user_id" in yaml
        assert "type: str" in yaml
        assert "required: true" in yaml

    def test_generate_yaml_plugin_with_auth_flow(self) -> None:
        """Test generating YAML plugin with auth flow comments."""
        auth_flow = AuthFlow(
            steps=[
                AuthStep(
                    entry=None,  # type: ignore[arg-type]
                    step_type="oauth",
                    description="GET /oauth/authorize",
                    cookies_set=[],
                ),
            ],
            session_cookies=["token"],
            auth_type="oauth",
        )

        yaml = generate_yaml_plugin(
            site_name="api",
            domain="example.com",
            auth_flow=auth_flow,
            endpoints=[],
        )

        assert "# Detected authentication flow:" in yaml
        assert "GET /oauth/authorize" in yaml
        assert "# Session cookies: token" in yaml

    def test_generate_yaml_plugin_sanitizes_names(self) -> None:
        """Test that invalid YAML names are sanitized."""
        yaml = generate_yaml_plugin(
            site_name="My-Site",
            domain="example.com",
            auth_flow=None,
            endpoints=[],
        )

        assert "site_name: my_site" in yaml
        assert "session_name: my_site" in yaml

    def test_generate_yaml_plugin_header_comments(self) -> None:
        """Test that header comments are included."""
        yaml = generate_yaml_plugin(
            site_name="test",
            domain="example.com",
            auth_flow=None,
            endpoints=[],
        )

        assert "# Plugin for example.com" in yaml
        assert "# Generated from HAR file by graftpunk." in yaml
        assert "# Review and customize before use." in yaml
