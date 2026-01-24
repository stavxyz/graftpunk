"""Tests for HAR analysis (auth flow detection and API discovery)."""

from __future__ import annotations

from pathlib import Path

import pytest

from graftpunk.har.analyzer import (
    APIEndpoint,
    AuthFlow,
    detect_auth_flow,
    discover_api_endpoints,
    extract_domain,
)
from graftpunk.har.parser import parse_har_file, parse_har_string


@pytest.fixture
def sample_har_path() -> Path:
    """Path to sample HAR fixture."""
    return Path(__file__).parent.parent / "fixtures" / "sample.har"


@pytest.fixture
def sample_entries(sample_har_path: Path) -> list:
    """Parsed entries from sample HAR."""
    return parse_har_file(sample_har_path)


class TestExtractDomain:
    """Tests for domain extraction."""

    def test_extract_from_sample(self, sample_entries: list) -> None:
        """Extract domain from sample HAR."""
        domain = extract_domain(sample_entries)
        assert domain == "example.com"

    def test_empty_entries(self) -> None:
        """Empty entries returns empty string."""
        assert extract_domain([]) == ""

    def test_most_common_domain(self) -> None:
        """Returns most common domain."""
        import json

        def make_entry(url: str, ts_suffix: str = "00") -> dict:
            return {
                "startedDateTime": f"2024-01-01T00:00:{ts_suffix}Z",
                "request": {"method": "GET", "url": url, "headers": [], "cookies": []},
                "response": {
                    "status": 200,
                    "statusText": "OK",
                    "headers": [],
                    "cookies": [],
                },
            }

        entries_data = [
            make_entry("https://main.com/a", "00"),
            make_entry("https://main.com/b", "01"),
            make_entry("https://cdn.other.com/file", "02"),
        ]
        content = json.dumps({"log": {"entries": entries_data}})
        entries = parse_har_string(content)
        assert extract_domain(entries) == "main.com"


class TestDetectAuthFlow:
    """Tests for auth flow detection."""

    def test_detect_login_flow(self, sample_entries: list) -> None:
        """Detect login flow from sample HAR."""
        auth_flow = detect_auth_flow(sample_entries)

        assert auth_flow is not None
        assert isinstance(auth_flow, AuthFlow)
        assert len(auth_flow.steps) >= 2

    def test_session_cookies_detected(self, sample_entries: list) -> None:
        """Session cookies are captured."""
        auth_flow = detect_auth_flow(sample_entries)

        assert auth_flow is not None
        assert "sessionId" in auth_flow.session_cookies

    def test_auth_type_form(self, sample_entries: list) -> None:
        """Auth type is 'form' for form-based login."""
        auth_flow = detect_auth_flow(sample_entries)

        assert auth_flow is not None
        assert auth_flow.auth_type == "form"

    def test_no_auth_flow(self) -> None:
        """Returns None when no auth flow detected."""
        import json

        entry = {
            "startedDateTime": "2024-01-01T00:00:00Z",
            "request": {
                "method": "GET",
                "url": "https://example.com/public",
                "headers": [],
                "cookies": [],
            },
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": [],
                "cookies": [],
            },
        }
        content = json.dumps({"log": {"entries": [entry]}})
        entries = parse_har_string(content)
        assert detect_auth_flow(entries) is None

    def test_empty_entries(self) -> None:
        """Returns None for empty entries."""
        assert detect_auth_flow([]) is None

    def test_oauth_detection(self) -> None:
        """OAuth flows are detected."""
        import json

        entry = {
            "startedDateTime": "2024-01-01T00:00:00Z",
            "request": {
                "method": "GET",
                "url": "https://example.com/oauth/callback?code=abc",
                "headers": [],
                "cookies": [],
            },
            "response": {
                "status": 302,
                "statusText": "Found",
                "headers": [],
                "cookies": [{"name": "token", "value": "xyz"}],
            },
        }
        content = json.dumps({"log": {"entries": [entry]}})
        entries = parse_har_string(content)
        auth_flow = detect_auth_flow(entries)

        assert auth_flow is not None
        assert auth_flow.auth_type == "oauth"


class TestDiscoverAPIEndpoints:
    """Tests for API endpoint discovery."""

    def test_discover_from_sample(self, sample_entries: list) -> None:
        """Discover API endpoints from sample HAR."""
        endpoints = discover_api_endpoints(sample_entries, "example.com")

        assert len(endpoints) >= 2
        assert all(isinstance(e, APIEndpoint) for e in endpoints)

    def test_json_endpoints_included(self, sample_entries: list) -> None:
        """JSON responses are included as API endpoints."""
        endpoints = discover_api_endpoints(sample_entries, "example.com")

        paths = [e.path for e in endpoints]
        assert "/api/users" in paths

    def test_static_assets_excluded(self, sample_entries: list) -> None:
        """Static assets (.js, .css, etc.) are excluded."""
        endpoints = discover_api_endpoints(sample_entries, "example.com")

        paths = [e.path for e in endpoints]
        assert not any(".js" in p for p in paths)

    def test_path_params_extracted(self, sample_entries: list) -> None:
        """Numeric path segments become parameters."""
        endpoints = discover_api_endpoints(sample_entries, "example.com")

        # /api/users/123/posts should have a parameter
        user_posts = [e for e in endpoints if "posts" in e.path]
        assert len(user_posts) >= 1
        assert len(user_posts[0].params) >= 1

    def test_domain_filtering(self, sample_entries: list) -> None:
        """Filter by domain works."""
        endpoints = discover_api_endpoints(sample_entries, "other.com")
        assert len(endpoints) == 0

    def test_empty_entries(self) -> None:
        """Empty entries returns empty list."""
        assert discover_api_endpoints([]) == []

    def test_failed_requests_excluded(self) -> None:
        """4xx/5xx responses are excluded."""
        import json

        entry = {
            "startedDateTime": "2024-01-01T00:00:00Z",
            "request": {
                "method": "GET",
                "url": "https://api.example.com/v1/error",
                "headers": [],
                "cookies": [],
            },
            "response": {
                "status": 500,
                "statusText": "Error",
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "cookies": [],
            },
        }
        content = json.dumps({"log": {"entries": [entry]}})
        entries = parse_har_string(content)
        endpoints = discover_api_endpoints(entries)
        assert len(endpoints) == 0

    def test_post_endpoints(self, sample_entries: list) -> None:
        """POST endpoints are discovered."""
        endpoints = discover_api_endpoints(sample_entries, "example.com")

        methods = [e.method for e in endpoints]
        assert "POST" in methods


class TestAPIEndpoint:
    """Tests for APIEndpoint dataclass."""

    def test_default_values(self) -> None:
        """Default values are set correctly."""
        endpoint = APIEndpoint(
            method="GET",
            url="https://example.com/api/test",
            path="/api/test",
        )
        assert endpoint.params == []
        assert endpoint.description == ""


class TestPathParamExtraction:
    """Tests for path parameter extraction in endpoint discovery."""

    def test_single_param(self) -> None:
        """Single numeric ID is parameterized."""
        import json

        entry = {
            "startedDateTime": "2024-01-01T00:00:00Z",
            "request": {
                "method": "GET",
                "url": "https://api.example.com/users/42",
                "headers": [],
                "cookies": [],
            },
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "cookies": [],
            },
        }
        content = json.dumps({"log": {"entries": [entry]}})
        entries = parse_har_string(content)
        endpoints = discover_api_endpoints(entries)

        assert len(endpoints) == 1
        assert "{user_id}" in endpoints[0].path or "{id}" in endpoints[0].path

    def test_multiple_params(self) -> None:
        """Multiple numeric IDs are parameterized."""
        entry = {
            "startedDateTime": "2024-01-01T00:00:00Z",
            "request": {
                "method": "GET",
                "url": "https://api.example.com/users/1/posts/2",
                "headers": [],
                "cookies": [],
            },
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "cookies": [],
            },
        }
        import json

        content = json.dumps({"log": {"entries": [entry]}})
        entries = parse_har_string(content)
        endpoints = discover_api_endpoints(entries)

        assert len(endpoints) == 1
        assert len(endpoints[0].params) == 2


class TestExcludePatterns:
    """Tests for URL exclusion patterns."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://cdn.example.com/bundle.js",
            "https://example.com/styles.css",
            "https://example.com/logo.png",
            "https://www.google-analytics.com/collect",
            "https://example.com/tracking/pixel.gif",
        ],
    )
    def test_excluded_urls(self, url: str) -> None:
        """Known excluded URLs are filtered out."""
        import json

        entry = {
            "startedDateTime": "2024-01-01T00:00:00Z",
            "request": {"method": "GET", "url": url, "headers": [], "cookies": []},
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "cookies": [],
            },
        }
        content = json.dumps({"log": {"entries": [entry]}})
        entries = parse_har_string(content)
        endpoints = discover_api_endpoints(entries)
        assert len(endpoints) == 0
