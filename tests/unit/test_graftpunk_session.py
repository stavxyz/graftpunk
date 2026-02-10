"""Tests for GraftpunkSession browser header replay."""

import contextlib
import re
from unittest.mock import patch

import pytest
import requests

from graftpunk.graftpunk_session import (
    _ROLE_REGISTRY,
    GraftpunkSession,
    _case_insensitive_get,
    get_role_headers,
    list_roles,
    register_role,
)

# Shared browser identity headers — identical across all roles,
# which is the invariant that browser identity separation guarantees.
_IDENTITY_HEADERS = {
    "User-Agent": "Mozilla/5.0 Test",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Chromium";v="120", "Google Chrome";v="120"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}

SAMPLE_ROLES = {
    "navigation": {
        **_IDENTITY_HEADERS,
        "Accept": "text/html,application/xhtml+xml",
    },
    "xhr": {
        **_IDENTITY_HEADERS,
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    },
    "form": {
        **_IDENTITY_HEADERS,
        "Accept": "text/html",
        "Content-Type": "application/x-www-form-urlencoded",
    },
}


class TestRoleDetection:
    """Test auto-detection of role from request characteristics."""

    def test_get_without_json_uses_navigation(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        req = requests.Request("GET", "https://example.com/page")
        prepared = session.prepare_request(req)
        assert prepared.headers["User-Agent"] == "Mozilla/5.0 Test"
        assert "text/html" in prepared.headers["Accept"]

    def test_post_with_json_uses_xhr(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        req = requests.Request("POST", "https://example.com/api", json={"key": "val"})
        prepared = session.prepare_request(req)
        assert prepared.headers.get("X-Requested-With") == "XMLHttpRequest"

    def test_post_with_data_string_uses_form(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        req = requests.Request("POST", "https://example.com/submit", data="key=val")
        prepared = session.prepare_request(req)
        # Form role: User-Agent from form role, Accept from form role
        assert prepared.headers["User-Agent"] == "Mozilla/5.0 Test"

    def test_explicit_accept_json_uses_xhr(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        req = requests.Request(
            "GET",
            "https://example.com/api",
            headers={"Accept": "application/json"},
        )
        prepared = session.prepare_request(req)
        assert prepared.headers.get("X-Requested-With") == "XMLHttpRequest"

    def test_put_with_json_uses_xhr(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        req = requests.Request("PUT", "https://example.com/api/1", json={"key": "val"})
        prepared = session.prepare_request(req)
        assert prepared.headers.get("X-Requested-With") == "XMLHttpRequest"

    def test_patch_with_json_uses_xhr(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        req = requests.Request("PATCH", "https://example.com/api/1", json={"key": "val"})
        prepared = session.prepare_request(req)
        assert prepared.headers.get("X-Requested-With") == "XMLHttpRequest"

    def test_delete_uses_xhr(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        req = requests.Request("DELETE", "https://example.com/api/1")
        prepared = session.prepare_request(req)
        assert prepared.headers.get("X-Requested-With") == "XMLHttpRequest"

    def test_head_uses_xhr(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        req = requests.Request("HEAD", "https://example.com/api/1")
        prepared = session.prepare_request(req)
        assert "application/json" in prepared.headers["Accept"]

    def test_options_uses_xhr(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        req = requests.Request("OPTIONS", "https://example.com/api/1")
        prepared = session.prepare_request(req)
        assert "application/json" in prepared.headers["Accept"]


class TestExplicitOverride:
    """Test explicit role override mechanisms."""

    def test_headers_for_returns_role_dict(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        xhr_headers = session.headers_for("xhr")
        assert xhr_headers["X-Requested-With"] == "XMLHttpRequest"

    def test_headers_for_returns_copy(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        xhr_headers = session.headers_for("xhr")
        xhr_headers["New-Header"] = "value"
        # Original should not be modified
        assert "New-Header" not in session._gp_header_roles["xhr"]

    def test_headers_for_unknown_role_returns_empty(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        assert session.headers_for("nonexistent") == {}

    def test_gp_default_role_overrides_detection(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        session.gp_default_role = "xhr"
        req = requests.Request("GET", "https://example.com/page")
        prepared = session.prepare_request(req)
        assert prepared.headers.get("X-Requested-With") == "XMLHttpRequest"


class TestCallerHeadersPrecedence:
    """Caller-supplied headers must override role headers."""

    def test_caller_headers_override_role(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        req = requests.Request(
            "GET",
            "https://example.com/",
            headers={"User-Agent": "CustomAgent"},
        )
        prepared = session.prepare_request(req)
        assert prepared.headers["User-Agent"] == "CustomAgent"

    def test_session_headers_override_role(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        session.headers["User-Agent"] = "SessionAgent"
        req = requests.Request("GET", "https://example.com/")
        prepared = session.prepare_request(req)
        assert prepared.headers["User-Agent"] == "SessionAgent"


class TestFallbackBehavior:
    """Test fallback when requested role is missing."""

    def test_missing_form_role_uses_canonical_and_browser_identity(self):
        roles = {
            "navigation": {"User-Agent": "Nav/1.0", "Accept": "text/html"},
        }
        session = GraftpunkSession(header_roles=roles)
        req = requests.Request("POST", "https://example.com/submit", data="key=val")
        prepared = session.prepare_request(req)
        # Browser identity (User-Agent) comes from session defaults set at init
        assert prepared.headers["User-Agent"] == "Nav/1.0"
        # Canonical form headers are used since form role is missing
        assert prepared.headers.get("Sec-Fetch-Mode") == "navigate"
        assert prepared.headers.get("Content-Type") == "application/x-www-form-urlencoded"


class TestNoRoles:
    """Sessions without roles should work normally."""

    def test_no_roles_no_injection(self):
        session = GraftpunkSession(header_roles={})
        req = requests.Request("GET", "https://example.com/")
        prepared = session.prepare_request(req)
        # Should still have requests default User-Agent
        assert "python-requests" in prepared.headers.get("User-Agent", "").lower()

    def test_none_roles_no_injection(self):
        session = GraftpunkSession(header_roles=None)
        req = requests.Request("GET", "https://example.com/")
        prepared = session.prepare_request(req)
        assert prepared.headers is not None

    def test_default_role_none_by_default(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        assert session.gp_default_role is None


class TestBrowserIdentityGuarantee:
    """Browser identity headers are set at session init from roles."""

    def test_browser_ua_set_at_init(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        assert session.headers["User-Agent"] == "Mozilla/5.0 Test"

    def test_empty_roles_keeps_requests_default(self):
        session = GraftpunkSession(header_roles={})
        assert "python-requests" in session.headers.get("User-Agent", "").lower()

    def test_single_role_identity_extracted(self):
        roles = {
            "xhr": {
                "User-Agent": "Mozilla/5.0 XHR Only",
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
        }
        session = GraftpunkSession(header_roles=roles)
        assert session.headers["User-Agent"] == "Mozilla/5.0 XHR Only"

    def test_case_insensitive_extraction(self):
        roles = {
            "navigation": {
                "user-agent": "Mozilla/5.0 Lowercase",
                "Accept": "text/html",
            },
        }
        session = GraftpunkSession(header_roles=roles)
        assert session.headers["User-Agent"] == "Mozilla/5.0 Lowercase"

    def test_all_six_identity_headers_extracted(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        assert session.headers["User-Agent"] == "Mozilla/5.0 Test"
        assert session.headers["sec-ch-ua"] == '"Chromium";v="120", "Google Chrome";v="120"'
        assert session.headers["sec-ch-ua-mobile"] == "?0"
        assert session.headers["sec-ch-ua-platform"] == '"macOS"'
        assert session.headers["Accept-Language"] == "en-US,en;q=0.9"
        assert session.headers["Accept-Encoding"] == "gzip, deflate, br"

    def test_no_role_has_user_agent(self):
        roles = {
            "navigation": {"Accept": "text/html"},
            "xhr": {"Accept": "application/json"},
        }
        session = GraftpunkSession(header_roles=roles)
        # Falls back to python-requests default
        assert "python-requests" in session.headers.get("User-Agent", "").lower()

    def test_mixed_case_identity_headers_extracted(self):
        roles = {
            "navigation": {
                "user-agent": "Mozilla/5.0 Mixed",
                "ACCEPT-LANGUAGE": "fr-FR,fr;q=0.9",
                "accept-encoding": "gzip, deflate",
                "SEC-CH-UA": '"Chromium";v="121"',
                "Accept": "text/html",
            },
        }
        session = GraftpunkSession(header_roles=roles)
        assert session.headers["User-Agent"] == "Mozilla/5.0 Mixed"
        assert session.headers["Accept-Language"] == "fr-FR,fr;q=0.9"
        assert session.headers["Accept-Encoding"] == "gzip, deflate"
        assert session.headers["sec-ch-ua"] == '"Chromium";v="121"'

    def test_partial_identity_headers_only_present_extracted(self):
        roles = {
            "xhr": {
                "User-Agent": "Mozilla/5.0 Partial",
                "Accept": "application/json",
            },
        }
        session = GraftpunkSession(header_roles=roles)
        assert session.headers["User-Agent"] == "Mozilla/5.0 Partial"
        # sec-ch-ua was not in the role, should not be in session
        assert "sec-ch-ua" not in session.headers

    def test_identity_treated_as_default_not_user_set(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        assert session._is_user_set_header("User-Agent") is False

    def test_user_override_after_init_detected(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        session.headers["User-Agent"] = "Custom"
        assert session._is_user_set_header("User-Agent") is True


class TestCanonicalFallback:
    """When a detected role is missing, canonical request headers are used."""

    def test_missing_navigation_uses_canonical(self):
        roles = {
            "xhr": {
                "User-Agent": "Mozilla/5.0 Test",
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
        }
        session = GraftpunkSession(header_roles=roles)
        req = requests.Request("GET", "https://example.com/page")
        prepared = session.prepare_request(req)
        assert "text/html" in prepared.headers["Accept"]
        assert prepared.headers["Sec-Fetch-Mode"] == "navigate"

    def test_missing_xhr_uses_canonical(self):
        roles = {
            "navigation": {
                "User-Agent": "Mozilla/5.0 Test",
                "Accept": "text/html,application/xhtml+xml",
            },
        }
        session = GraftpunkSession(header_roles=roles)
        req = requests.Request("POST", "https://example.com/api", json={"key": "val"})
        prepared = session.prepare_request(req)
        assert prepared.headers["X-Requested-With"] == "XMLHttpRequest"
        assert prepared.headers["Sec-Fetch-Mode"] == "cors"

    def test_canonical_navigation_has_correct_headers(self):
        canonical = get_role_headers("navigation")
        assert canonical is not None
        assert "text/html" in canonical["Accept"]
        assert canonical["Sec-Fetch-Mode"] == "navigate"
        assert canonical["Sec-Fetch-Dest"] == "document"

    def test_canonical_xhr_has_correct_headers(self):
        canonical = get_role_headers("xhr")
        assert canonical is not None
        assert "application/json" in canonical["Accept"]
        assert canonical["X-Requested-With"] == "XMLHttpRequest"
        assert canonical["Sec-Fetch-Mode"] == "cors"

    def test_canonical_form_has_correct_headers(self):
        canonical = get_role_headers("form")
        assert canonical is not None
        assert "text/html" in canonical["Accept"]
        assert canonical["Content-Type"] == "application/x-www-form-urlencoded"
        assert canonical["Sec-Fetch-Mode"] == "navigate"
        assert canonical["Sec-Fetch-Dest"] == "document"

    def test_unknown_default_role_warns(self, capsys):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        session.gp_default_role = "typo"
        req = requests.Request("GET", "https://example.com/page")
        prepared = session.prepare_request(req)
        captured = capsys.readouterr()
        assert "unknown_role_no_headers_applied" in captured.out
        # Browser identity still present from session defaults
        assert prepared.headers["User-Agent"] == "Mozilla/5.0 Test"

    def test_captured_role_preferred_over_canonical(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        req = requests.Request("GET", "https://example.com/page")
        prepared = session.prepare_request(req)
        # Uses captured navigation Accept, not canonical
        assert prepared.headers["Accept"] == "text/html,application/xhtml+xml"

    def test_browser_ua_present_in_canonical_fallback(self):
        roles = {
            "xhr": {
                "User-Agent": "Mozilla/5.0 XHR Only",
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
        }
        session = GraftpunkSession(header_roles=roles)
        req = requests.Request("GET", "https://example.com/page")
        prepared = session.prepare_request(req)
        # Canonical navigation headers used for the request type
        assert prepared.headers.get("Sec-Fetch-Mode") == "navigate"
        # But browser UA still present from session defaults
        assert prepared.headers["User-Agent"] == "Mozilla/5.0 XHR Only"


class TestHeaderPriorityChain:
    """Full priority chain: caller > user-set session > role > identity defaults."""

    def test_full_priority_chain(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        # User-set session header overrides role
        session.headers["Accept-Language"] = "de-DE"
        req = requests.Request(
            "GET",
            "https://example.com/page",
            # Caller header overrides everything
            headers={"User-Agent": "CallerAgent"},
        )
        prepared = session.prepare_request(req)
        # Caller wins over role
        assert prepared.headers["User-Agent"] == "CallerAgent"
        # User-set session wins over role
        assert prepared.headers["Accept-Language"] == "de-DE"
        # Role value used when not overridden
        assert prepared.headers["Accept"] == "text/html,application/xhtml+xml"
        # Identity default (sec-ch-ua) still present
        assert prepared.headers["sec-ch-ua"] == '"Chromium";v="120", "Google Chrome";v="120"'


class TestSessionHeaderRestoration:
    """Session headers must be restored after prepare_request, even on error."""

    def test_headers_restored_after_successful_prepare(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        original_headers = dict(session.headers)
        req = requests.Request("GET", "https://example.com/page")
        session.prepare_request(req)
        assert dict(session.headers) == original_headers

    def test_headers_restored_after_exception(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        original_headers = dict(session.headers)
        with patch.object(requests.Session, "prepare_request", side_effect=ValueError("boom")):
            req = requests.Request("GET", "https://example.com/page")
            with contextlib.suppress(ValueError):
                session.prepare_request(req)
        assert dict(session.headers) == original_headers


class TestWarnings:
    """Warning logs for misconfiguration scenarios."""

    def test_no_ua_in_any_role_warns(self, capsys):
        roles = {
            "navigation": {"Accept": "text/html"},
            "xhr": {"Accept": "application/json"},
        }
        GraftpunkSession(header_roles=roles)
        captured = capsys.readouterr()
        assert "no_browser_identity_in_roles" in captured.out

    def test_empty_roles_does_not_warn(self, capsys):
        GraftpunkSession(header_roles={})
        captured = capsys.readouterr()
        assert "no_browser_identity_in_roles" not in captured.out


class TestCaseInsensitiveGet:
    """Test the _case_insensitive_get helper function."""

    def test_exact_match(self):
        assert _case_insensitive_get({"User-Agent": "test"}, "User-Agent") == "test"

    def test_lowercase_match(self):
        assert _case_insensitive_get({"user-agent": "test"}, "User-Agent") == "test"

    def test_uppercase_match(self):
        assert _case_insensitive_get({"ACCEPT": "text/html"}, "Accept") == "text/html"

    def test_missing_key(self):
        assert _case_insensitive_get({"Accept": "text/html"}, "User-Agent") is None

    def test_empty_mapping(self):
        assert _case_insensitive_get({}, "User-Agent") is None


class TestConstructor:
    """Test GraftpunkSession constructor wiring."""

    def test_gp_base_url_set_from_constructor(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES, base_url="https://www.example.com")
        assert session.gp_base_url == "https://www.example.com"

    def test_gp_base_url_defaults_to_empty(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        assert session.gp_base_url == ""


class TestResolveReferer:
    """Test Referer URL resolution from path or full URL."""

    def test_path_joined_with_base_url(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES, base_url="https://www.example.com")
        assert session._resolve_referer("/invoice/list") == "https://www.example.com/invoice/list"

    def test_full_https_url_used_as_is(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES, base_url="https://www.example.com")
        assert (
            session._resolve_referer("https://other.example.com/page")
            == "https://other.example.com/page"
        )

    def test_full_http_url_used_as_is(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES, base_url="https://www.example.com")
        assert (
            session._resolve_referer("http://other.example.com/page")
            == "http://other.example.com/page"
        )

    def test_path_without_base_url_raises(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        with pytest.raises(ValueError, match=re.escape("gp_base_url")):
            session._resolve_referer("/some/path")

    def test_base_url_trailing_slash_handled(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES, base_url="https://www.example.com/")
        assert session._resolve_referer("/invoice/list") == "https://www.example.com/invoice/list"

    def test_path_without_leading_slash_normalized(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES, base_url="https://www.example.com")
        assert session._resolve_referer("invoice/list") == "https://www.example.com/invoice/list"


class TestRoleHeadersFor:
    """Test _role_headers_for composition of captured + canonical headers."""

    def test_captured_role_returned(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        headers = session._role_headers_for("xhr")
        assert headers["X-Requested-With"] == "XMLHttpRequest"
        assert headers["Accept"] == "application/json"

    def test_missing_role_falls_back_to_canonical(self):
        roles = {
            "xhr": {
                "User-Agent": "Mozilla/5.0 Test",
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
        }
        session = GraftpunkSession(header_roles=roles)
        headers = session._role_headers_for("navigation")
        assert "text/html" in headers["Accept"]
        assert headers["Sec-Fetch-Mode"] == "navigate"

    def test_returns_copy_not_reference(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        headers = session._role_headers_for("xhr")
        headers["New-Header"] = "value"
        assert "New-Header" not in session._gp_header_roles.get("xhr", {})

    def test_unknown_role_returns_empty(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        headers = session._role_headers_for("nonexistent")
        assert headers == {}

    def test_unknown_role_warns(self, capsys):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        session._role_headers_for("nonexistent")
        captured = capsys.readouterr()
        assert "unknown_role_no_headers_applied" in captured.out

    def test_excludes_identity_headers(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        headers = session._role_headers_for("navigation")
        assert "User-Agent" not in headers
        assert "sec-ch-ua" not in headers
        assert "Accept-Language" not in headers
        assert "Accept-Encoding" not in headers


class TestXhr:
    """Test xhr() method for XHR-style requests."""

    def test_xhr_get_applies_xhr_headers(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        with patch.object(session, "request") as mock_request:
            session.xhr("GET", "https://example.com/api/data")
        headers = mock_request.call_args.kwargs.get("headers", {})
        assert headers["X-Requested-With"] == "XMLHttpRequest"
        assert "application/json" in headers["Accept"]

    def test_xhr_post_with_json(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        with patch.object(session, "request") as mock_request:
            session.xhr("POST", "https://example.com/api", json={"key": "val"})
        assert mock_request.call_args[0] == ("POST", "https://example.com/api")
        assert mock_request.call_args.kwargs["json"] == {"key": "val"}

    def test_xhr_with_referer_path(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES, base_url="https://example.com")
        with patch.object(session, "request") as mock_request:
            session.xhr("GET", "https://example.com/api", referer="/invoice/list")
        headers = mock_request.call_args.kwargs.get("headers", {})
        assert headers["Referer"] == "https://example.com/invoice/list"

    def test_xhr_without_referer(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        with patch.object(session, "request") as mock_request:
            session.xhr("GET", "https://example.com/api")
        headers = mock_request.call_args.kwargs.get("headers", {})
        assert "Referer" not in headers

    def test_xhr_caller_headers_override_role(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        with patch.object(session, "request") as mock_request:
            session.xhr("GET", "https://example.com/api", headers={"Accept": "text/plain"})
        headers = mock_request.call_args.kwargs.get("headers", {})
        assert headers["Accept"] == "text/plain"
        assert headers["X-Requested-With"] == "XMLHttpRequest"

    def test_xhr_passes_kwargs_through(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        with patch.object(session, "request") as mock_request:
            session.xhr("GET", "https://example.com/api", params={"q": "test"}, timeout=10)
        assert mock_request.call_args.kwargs["params"] == {"q": "test"}
        assert mock_request.call_args.kwargs["timeout"] == 10

    def test_xhr_uses_canonical_when_role_missing(self):
        roles = {
            "navigation": {
                "User-Agent": "Mozilla/5.0 Test",
                "Accept": "text/html,application/xhtml+xml",
            },
        }
        session = GraftpunkSession(header_roles=roles)
        with patch.object(session, "request") as mock_request:
            session.xhr("GET", "https://example.com/api")
        headers = mock_request.call_args.kwargs.get("headers", {})
        assert "application/json" in headers["Accept"]
        assert headers["X-Requested-With"] == "XMLHttpRequest"
        assert headers["Sec-Fetch-Mode"] == "cors"


class TestNavigate:
    """Test navigate() method for navigation-style requests."""

    def test_navigate_applies_navigation_headers(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        with patch.object(session, "request") as mock_request:
            session.navigate("GET", "https://example.com/page")
        headers = mock_request.call_args.kwargs.get("headers", {})
        assert "text/html" in headers["Accept"]

    def test_navigate_with_referer(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES, base_url="https://example.com")
        with patch.object(session, "request") as mock_request:
            session.navigate("GET", "https://example.com/page2", referer="/page1")
        headers = mock_request.call_args.kwargs.get("headers", {})
        assert headers["Referer"] == "https://example.com/page1"

    def test_navigate_caller_headers_override_role(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        with patch.object(session, "request") as mock_request:
            session.navigate("GET", "https://example.com/page", headers={"Accept": "text/plain"})
        headers = mock_request.call_args.kwargs.get("headers", {})
        assert headers["Accept"] == "text/plain"

    def test_navigate_passes_kwargs_through(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        with patch.object(session, "request") as mock_request:
            session.navigate("GET", "https://example.com/page", params={"q": "test"}, timeout=10)
        assert mock_request.call_args.kwargs["params"] == {"q": "test"}
        assert mock_request.call_args.kwargs["timeout"] == 10

    def test_navigate_uses_canonical_when_role_missing(self):
        roles = {
            "xhr": {
                "User-Agent": "Mozilla/5.0 Test",
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
        }
        session = GraftpunkSession(header_roles=roles)
        with patch.object(session, "request") as mock_request:
            session.navigate("GET", "https://example.com/page")
        headers = mock_request.call_args.kwargs.get("headers", {})
        assert "text/html" in headers["Accept"]
        assert headers["Sec-Fetch-Mode"] == "navigate"


class TestFormSubmit:
    """Test form_submit() method for form submission requests."""

    def test_form_submit_applies_form_headers(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        with patch.object(session, "request") as mock_request:
            session.form_submit("POST", "https://example.com/login", data={"user": "me"})
        headers = mock_request.call_args.kwargs.get("headers", {})
        assert "text/html" in headers["Accept"]

    def test_form_submit_with_referer(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES, base_url="https://example.com")
        with patch.object(session, "request") as mock_request:
            session.form_submit(
                "POST",
                "https://example.com/login",
                referer="/login",
                data={"u": "me"},
            )
        headers = mock_request.call_args.kwargs.get("headers", {})
        assert headers["Referer"] == "https://example.com/login"

    def test_form_submit_passes_data_through(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        with patch.object(session, "request") as mock_request:
            session.form_submit("POST", "https://example.com/submit", data="key=val")
        assert mock_request.call_args.kwargs["data"] == "key=val"

    def test_form_submit_caller_headers_override_role(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        with patch.object(session, "request") as mock_request:
            session.form_submit(
                "POST",
                "https://example.com/submit",
                headers={"Accept": "text/plain"},
                data="key=val",
            )
        headers = mock_request.call_args.kwargs.get("headers", {})
        assert headers["Accept"] == "text/plain"

    def test_form_submit_passes_kwargs_through(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        with patch.object(session, "request") as mock_request:
            session.form_submit("POST", "https://example.com/submit", data="key=val", timeout=10)
        assert mock_request.call_args.kwargs["timeout"] == 10

    def test_form_submit_uses_canonical_when_role_missing(self):
        roles = {
            "xhr": {
                "User-Agent": "Mozilla/5.0 Test",
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
        }
        session = GraftpunkSession(header_roles=roles)
        with patch.object(session, "request") as mock_request:
            session.form_submit("POST", "https://example.com/submit", data="key=val")
        headers = mock_request.call_args.kwargs.get("headers", {})
        assert headers["Content-Type"] == "application/x-www-form-urlencoded"
        assert headers["Sec-Fetch-Mode"] == "navigate"


class TestExplicitMethodIntegration:
    """Integration tests: explicit methods through full prepare_request path.

    These tests do NOT mock session.request, so the headers flow through
    both request_with_role and prepare_request's auto-detection layer.
    This verifies that explicit role headers survive when _detect_role
    would choose a different role.
    """

    def test_xhr_get_survives_navigation_autodetect(self):
        """xhr("GET", ...) should use XHR headers even though
        _detect_role would classify a plain GET as navigation."""
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        req = requests.Request("GET", "https://example.com/api")
        # Simulate what xhr() does: compose headers then pass to request()
        role_headers = session._role_headers_for("xhr")
        req.headers = role_headers
        prepared = session.prepare_request(req)
        assert prepared.headers.get("X-Requested-With") == "XMLHttpRequest"
        assert "application/json" in prepared.headers["Accept"]

    def test_navigate_post_with_data_survives_form_autodetect(self):
        """navigate("POST", ..., data=...) should use navigation headers
        even though _detect_role would classify POST+data as form."""
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        role_headers = session._role_headers_for("navigation")
        req = requests.Request("POST", "https://example.com/page", data="x", headers=role_headers)
        prepared = session.prepare_request(req)
        assert "text/html" in prepared.headers["Accept"]

    def test_form_submit_get_survives_navigation_autodetect(self):
        """form_submit("GET", ...) should use form headers even though
        _detect_role would classify a plain GET as navigation."""
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        role_headers = session._role_headers_for("form")
        req = requests.Request("GET", "https://example.com/page", headers=role_headers)
        prepared = session.prepare_request(req)
        assert prepared.headers.get("Content-Type") == "application/x-www-form-urlencoded"


class TestCsrfTokenInjection:
    """Test that CSRF tokens are only injected on mutation methods."""

    def _session_with_csrf(self) -> GraftpunkSession:
        """Create a GraftpunkSession with CSRF tokens stored."""
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        session._gp_csrf_tokens = {"X-CSRF-Token": "secret123"}
        return session

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_csrf_injected_on_mutation_methods(self, method):
        session = self._session_with_csrf()
        req = requests.Request(method, "https://example.com/api", json={"key": "val"})
        prepared = session.prepare_request(req)
        assert prepared.headers["X-CSRF-Token"] == "secret123"

    @pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
    def test_csrf_not_injected_on_safe_methods(self, method):
        session = self._session_with_csrf()
        req = requests.Request(method, "https://example.com/page")
        prepared = session.prepare_request(req)
        assert "X-CSRF-Token" not in prepared.headers

    def test_no_csrf_tokens_no_error(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        req = requests.Request("POST", "https://example.com/api", json={"key": "val"})
        prepared = session.prepare_request(req)
        assert "X-CSRF-Token" not in prepared.headers

    def test_csrf_does_not_override_caller_header(self):
        session = self._session_with_csrf()
        req = requests.Request(
            "POST",
            "https://example.com/api",
            json={"key": "val"},
            headers={"X-CSRF-Token": "caller_value"},
        )
        prepared = session.prepare_request(req)
        assert prepared.headers["X-CSRF-Token"] == "caller_value"

    def test_csrf_injected_without_roles(self):
        """CSRF injection works even with no header roles."""
        session = GraftpunkSession(header_roles={})
        session._gp_csrf_tokens = {"X-CSRF-Token": "secret123"}
        req = requests.Request("POST", "https://example.com/api", json={"key": "val"})
        prepared = session.prepare_request(req)
        assert prepared.headers["X-CSRF-Token"] == "secret123"

    def test_csrf_not_injected_on_get_without_roles(self):
        """CSRF skipped on GET even with no header roles."""
        session = GraftpunkSession(header_roles={})
        session._gp_csrf_tokens = {"X-CSRF-Token": "secret123"}
        req = requests.Request("GET", "https://example.com/page")
        prepared = session.prepare_request(req)
        assert "X-CSRF-Token" not in prepared.headers

    def test_csrf_injected_through_role_matched_path(self):
        """CSRF tokens appear alongside role headers on mutation requests."""
        session = self._session_with_csrf()
        req = requests.Request("POST", "https://example.com/api", json={"key": "val"})
        prepared = session.prepare_request(req)
        # CSRF token injected
        assert prepared.headers["X-CSRF-Token"] == "secret123"
        # XHR role headers also present (POST+json -> xhr role)
        assert prepared.headers.get("X-Requested-With") == "XMLHttpRequest"

    @pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
    def test_csrf_injected_on_lowercase_mutation_methods(self, method):
        """CSRF injection works when PreparedRequest.method is lowercase."""
        session = self._session_with_csrf()
        req = requests.Request(method, "https://example.com/api", json={"key": "val"})
        prepared = session.prepare_request(req)
        assert prepared.headers["X-CSRF-Token"] == "secret123"

    def test_multiple_csrf_tokens_injected(self):
        """Multiple CSRF tokens are all injected on mutation requests."""
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        session._gp_csrf_tokens = {
            "X-CSRF-Token": "csrf123",
            "X-Custom-Auth": "auth456",
        }
        req = requests.Request("POST", "https://example.com/api", json={"key": "val"})
        prepared = session.prepare_request(req)
        assert prepared.headers["X-CSRF-Token"] == "csrf123"
        assert prepared.headers["X-Custom-Auth"] == "auth456"


# ---------------------------------------------------------------------------
# Role Registry Tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def _clean_registry():
    """Save and restore _ROLE_REGISTRY around tests that mutate it."""
    saved = dict(_ROLE_REGISTRY)
    yield
    _ROLE_REGISTRY.clear()
    _ROLE_REGISTRY.update(saved)


class TestRoleRegistry:
    """Tests for register_role(), list_roles(), get_role_headers()."""

    def test_get_role_headers_returns_copy(self):
        headers = get_role_headers("xhr")
        assert headers is not None
        headers["MUTATED"] = "yes"
        assert "MUTATED" not in get_role_headers("xhr")

    def test_get_role_headers_unknown_returns_none(self):
        assert get_role_headers("nonexistent") is None

    def test_list_roles_returns_sorted(self):
        names = list_roles()
        assert names == sorted(names)

    def test_list_roles_contains_builtins(self):
        names = list_roles()
        assert "form" in names
        assert "navigation" in names
        assert "xhr" in names

    @pytest.mark.usefixtures("_clean_registry")
    def test_register_role_stores_copy(self):
        original = {"Accept": "text/plain"}
        register_role("test-copy", original)
        original["MUTATED"] = "yes"
        stored = get_role_headers("test-copy")
        assert stored is not None
        assert "MUTATED" not in stored

    @pytest.mark.usefixtures("_clean_registry")
    def test_register_role_overwrites_existing(self, capsys):
        register_role("test-overwrite", {"Accept": "text/html"})
        register_role("test-overwrite", {"Accept": "application/json"})
        result = get_role_headers("test-overwrite")
        assert result is not None
        assert result["Accept"] == "application/json"
        captured = capsys.readouterr()
        assert "role_overwritten" in captured.out

    @pytest.mark.usefixtures("_clean_registry")
    def test_register_role_appears_in_list(self):
        register_role("custom-api", {"Accept": "application/json"})
        assert "custom-api" in list_roles()

    def test_register_role_empty_name_raises(self):
        with pytest.raises(ValueError, match="non-empty string"):
            register_role("", {"Accept": "text/html"})

    def test_register_role_whitespace_name_raises(self):
        with pytest.raises(ValueError, match="non-empty string"):
            register_role("   ", {"Accept": "text/html"})

    def test_register_role_empty_headers_raises(self):
        with pytest.raises(ValueError, match="at least one header"):
            register_role("bad-role", {})


class TestRequestWithRole:
    """Tests for request_with_role() with custom role names."""

    @pytest.mark.usefixtures("_clean_registry")
    def test_custom_role_from_captured(self):
        roles = {
            **SAMPLE_ROLES,
            "api": {"Accept": "application/json", "X-API-Version": "2"},
        }
        session = GraftpunkSession(header_roles=roles)
        with patch.object(session, "request") as mock_request:
            session.request_with_role("api", "GET", "https://example.com/api")
        headers = mock_request.call_args.kwargs.get("headers", {})
        assert headers["X-API-Version"] == "2"

    @pytest.mark.usefixtures("_clean_registry")
    def test_custom_role_from_registry(self):
        register_role("custom-api", {"Accept": "application/json", "X-Custom": "1"})
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        with patch.object(session, "request") as mock_request:
            session.request_with_role("custom-api", "GET", "https://example.com/api")
        headers = mock_request.call_args.kwargs.get("headers", {})
        assert headers["X-Custom"] == "1"


class TestResolveRoleTruthiness:
    """Test that _resolve_role uses 'is not None' check, not truthiness."""

    def test_empty_captured_role_is_honoured(self):
        """An explicitly empty captured role should not fall through to registry."""
        session = GraftpunkSession(header_roles={"xhr": {}})
        result = session._resolve_role("xhr")
        assert result == {}


class TestSessionEncapsulation:
    """Tests for clear_header_roles() and merge_header_roles()."""

    def test_clear_header_roles(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        assert session._gp_header_roles
        session.clear_header_roles()
        assert session._gp_header_roles == {}

    def test_merge_header_roles(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        session.merge_header_roles({"api": {"Accept": "application/json"}})
        assert "api" in session._gp_header_roles
        assert session._gp_header_roles["api"]["Accept"] == "application/json"

    def test_merge_header_roles_preserves_existing(self):
        session = GraftpunkSession(header_roles=SAMPLE_ROLES)
        session.merge_header_roles({"api": {"Accept": "application/json"}})
        assert "xhr" in session._gp_header_roles
        assert "navigation" in session._gp_header_roles

    def test_constructor_copies_roles_dict(self):
        """Clearing session roles must not mutate the original dict."""
        original = {"xhr": {"Accept": "*/*"}}
        session = GraftpunkSession(header_roles=original)
        session.clear_header_roles()
        assert original == {"xhr": {"Accept": "*/*"}}
