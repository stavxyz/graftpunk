"""Tests for GraftpunkSession browser header replay."""

import contextlib
from unittest.mock import MagicMock

import requests

from graftpunk.graftpunk_session import (
    _CANONICAL_REQUEST_HEADERS,
    GraftpunkSession,
    _case_insensitive_get,
)

SAMPLE_PROFILES = {
    "navigation": {
        "User-Agent": "Mozilla/5.0 Test",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "sec-ch-ua": '"Chromium";v="120", "Google Chrome";v="120"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    },
    "xhr": {
        "User-Agent": "Mozilla/5.0 Test",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "X-Requested-With": "XMLHttpRequest",
        "sec-ch-ua": '"Chromium";v="120", "Google Chrome";v="120"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    },
    "form": {
        "User-Agent": "Mozilla/5.0 Test",
        "Accept": "text/html",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Content-Type": "application/x-www-form-urlencoded",
        "sec-ch-ua": '"Chromium";v="120", "Google Chrome";v="120"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    },
}


class TestProfileDetection:
    """Test auto-detection of profile from request characteristics."""

    def test_get_without_json_uses_navigation(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        req = requests.Request("GET", "https://example.com/page")
        prepared = session.prepare_request(req)
        assert prepared.headers["User-Agent"] == "Mozilla/5.0 Test"
        assert "text/html" in prepared.headers["Accept"]

    def test_post_with_json_uses_xhr(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        req = requests.Request("POST", "https://example.com/api", json={"key": "val"})
        prepared = session.prepare_request(req)
        assert prepared.headers.get("X-Requested-With") == "XMLHttpRequest"

    def test_post_with_data_string_uses_form(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        req = requests.Request("POST", "https://example.com/submit", data="key=val")
        prepared = session.prepare_request(req)
        # Form profile: User-Agent from form profile, Accept from form profile
        assert prepared.headers["User-Agent"] == "Mozilla/5.0 Test"

    def test_explicit_accept_json_uses_xhr(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        req = requests.Request(
            "GET",
            "https://example.com/api",
            headers={"Accept": "application/json"},
        )
        prepared = session.prepare_request(req)
        assert prepared.headers.get("X-Requested-With") == "XMLHttpRequest"

    def test_put_with_json_uses_xhr(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        req = requests.Request("PUT", "https://example.com/api/1", json={"key": "val"})
        prepared = session.prepare_request(req)
        assert prepared.headers.get("X-Requested-With") == "XMLHttpRequest"

    def test_patch_with_json_uses_xhr(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        req = requests.Request("PATCH", "https://example.com/api/1", json={"key": "val"})
        prepared = session.prepare_request(req)
        assert prepared.headers.get("X-Requested-With") == "XMLHttpRequest"

    def test_delete_uses_xhr(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        req = requests.Request("DELETE", "https://example.com/api/1")
        prepared = session.prepare_request(req)
        assert prepared.headers.get("X-Requested-With") == "XMLHttpRequest"

    def test_head_uses_xhr(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        req = requests.Request("HEAD", "https://example.com/api/1")
        prepared = session.prepare_request(req)
        assert "application/json" in prepared.headers["Accept"]

    def test_options_uses_xhr(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        req = requests.Request("OPTIONS", "https://example.com/api/1")
        prepared = session.prepare_request(req)
        assert "application/json" in prepared.headers["Accept"]


class TestExplicitOverride:
    """Test explicit profile override mechanisms."""

    def test_headers_for_returns_profile_dict(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        xhr_headers = session.headers_for("xhr")
        assert xhr_headers["X-Requested-With"] == "XMLHttpRequest"

    def test_headers_for_returns_copy(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        xhr_headers = session.headers_for("xhr")
        xhr_headers["New-Header"] = "value"
        # Original should not be modified
        assert "New-Header" not in session._gp_header_profiles["xhr"]

    def test_headers_for_unknown_profile_returns_empty(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        assert session.headers_for("nonexistent") == {}

    def test_gp_default_profile_overrides_detection(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        session.gp_default_profile = "xhr"
        req = requests.Request("GET", "https://example.com/page")
        prepared = session.prepare_request(req)
        assert prepared.headers.get("X-Requested-With") == "XMLHttpRequest"


class TestCallerHeadersPrecedence:
    """Caller-supplied headers must override profile headers."""

    def test_caller_headers_override_profile(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        req = requests.Request(
            "GET",
            "https://example.com/",
            headers={"User-Agent": "CustomAgent"},
        )
        prepared = session.prepare_request(req)
        assert prepared.headers["User-Agent"] == "CustomAgent"

    def test_session_headers_override_profile(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        session.headers["User-Agent"] = "SessionAgent"
        req = requests.Request("GET", "https://example.com/")
        prepared = session.prepare_request(req)
        assert prepared.headers["User-Agent"] == "SessionAgent"


class TestFallbackBehavior:
    """Test fallback when requested profile is missing."""

    def test_missing_form_profile_uses_canonical_and_browser_identity(self):
        profiles = {
            "navigation": {"User-Agent": "Nav/1.0", "Accept": "text/html"},
        }
        session = GraftpunkSession(header_profiles=profiles)
        req = requests.Request("POST", "https://example.com/submit", data="key=val")
        prepared = session.prepare_request(req)
        # Browser identity (User-Agent) comes from session defaults set at init
        assert prepared.headers["User-Agent"] == "Nav/1.0"
        # Canonical form headers are used since form profile is missing
        assert prepared.headers.get("Sec-Fetch-Mode") == "navigate"
        assert prepared.headers.get("Content-Type") == "application/x-www-form-urlencoded"


class TestNoProfiles:
    """Sessions without profiles should work normally."""

    def test_no_profiles_no_injection(self):
        session = GraftpunkSession(header_profiles={})
        req = requests.Request("GET", "https://example.com/")
        prepared = session.prepare_request(req)
        # Should still have requests default User-Agent
        assert "python-requests" in prepared.headers.get("User-Agent", "").lower()

    def test_none_profiles_no_injection(self):
        session = GraftpunkSession(header_profiles=None)
        req = requests.Request("GET", "https://example.com/")
        prepared = session.prepare_request(req)
        assert prepared.headers is not None

    def test_default_profile_none_by_default(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        assert session.gp_default_profile is None


class TestBrowserIdentityGuarantee:
    """Browser identity headers are set at session init from profiles."""

    def test_browser_ua_set_at_init(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        assert session.headers["User-Agent"] == "Mozilla/5.0 Test"

    def test_empty_profiles_keeps_requests_default(self):
        session = GraftpunkSession(header_profiles={})
        assert "python-requests" in session.headers.get("User-Agent", "").lower()

    def test_single_profile_identity_extracted(self):
        profiles = {
            "xhr": {
                "User-Agent": "Mozilla/5.0 XHR Only",
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
        }
        session = GraftpunkSession(header_profiles=profiles)
        assert session.headers["User-Agent"] == "Mozilla/5.0 XHR Only"

    def test_case_insensitive_extraction(self):
        profiles = {
            "navigation": {
                "user-agent": "Mozilla/5.0 Lowercase",
                "Accept": "text/html",
            },
        }
        session = GraftpunkSession(header_profiles=profiles)
        assert session.headers["User-Agent"] == "Mozilla/5.0 Lowercase"

    def test_all_six_identity_headers_extracted(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        assert session.headers["User-Agent"] == "Mozilla/5.0 Test"
        assert session.headers["sec-ch-ua"] == '"Chromium";v="120", "Google Chrome";v="120"'
        assert session.headers["sec-ch-ua-mobile"] == "?0"
        assert session.headers["sec-ch-ua-platform"] == '"macOS"'
        assert session.headers["Accept-Language"] == "en-US,en;q=0.9"
        assert session.headers["Accept-Encoding"] == "gzip, deflate, br"

    def test_no_profile_has_user_agent(self):
        profiles = {
            "navigation": {"Accept": "text/html"},
            "xhr": {"Accept": "application/json"},
        }
        session = GraftpunkSession(header_profiles=profiles)
        # Falls back to python-requests default
        assert "python-requests" in session.headers.get("User-Agent", "").lower()

    def test_mixed_case_identity_headers_extracted(self):
        profiles = {
            "navigation": {
                "user-agent": "Mozilla/5.0 Mixed",
                "ACCEPT-LANGUAGE": "fr-FR,fr;q=0.9",
                "accept-encoding": "gzip, deflate",
                "SEC-CH-UA": '"Chromium";v="121"',
                "Accept": "text/html",
            },
        }
        session = GraftpunkSession(header_profiles=profiles)
        assert session.headers["User-Agent"] == "Mozilla/5.0 Mixed"
        assert session.headers["Accept-Language"] == "fr-FR,fr;q=0.9"
        assert session.headers["Accept-Encoding"] == "gzip, deflate"
        assert session.headers["sec-ch-ua"] == '"Chromium";v="121"'

    def test_partial_identity_headers_only_present_extracted(self):
        profiles = {
            "xhr": {
                "User-Agent": "Mozilla/5.0 Partial",
                "Accept": "application/json",
            },
        }
        session = GraftpunkSession(header_profiles=profiles)
        assert session.headers["User-Agent"] == "Mozilla/5.0 Partial"
        # sec-ch-ua was not in the profile, should not be in session
        assert "sec-ch-ua" not in session.headers

    def test_identity_treated_as_default_not_user_set(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        assert session._is_user_set_header("User-Agent") is False

    def test_user_override_after_init_detected(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        session.headers["User-Agent"] = "Custom"
        assert session._is_user_set_header("User-Agent") is True


class TestCanonicalFallback:
    """When a detected profile is missing, canonical request headers are used."""

    def test_missing_navigation_uses_canonical(self):
        profiles = {
            "xhr": {
                "User-Agent": "Mozilla/5.0 Test",
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
        }
        session = GraftpunkSession(header_profiles=profiles)
        req = requests.Request("GET", "https://example.com/page")
        prepared = session.prepare_request(req)
        assert "text/html" in prepared.headers["Accept"]
        assert prepared.headers["Sec-Fetch-Mode"] == "navigate"

    def test_missing_xhr_uses_canonical(self):
        profiles = {
            "navigation": {
                "User-Agent": "Mozilla/5.0 Test",
                "Accept": "text/html,application/xhtml+xml",
            },
        }
        session = GraftpunkSession(header_profiles=profiles)
        req = requests.Request("POST", "https://example.com/api", json={"key": "val"})
        prepared = session.prepare_request(req)
        assert prepared.headers["X-Requested-With"] == "XMLHttpRequest"
        assert prepared.headers["Sec-Fetch-Mode"] == "cors"

    def test_canonical_navigation_has_correct_headers(self):
        canonical = _CANONICAL_REQUEST_HEADERS["navigation"]
        assert "text/html" in canonical["Accept"]
        assert canonical["Sec-Fetch-Mode"] == "navigate"
        assert canonical["Sec-Fetch-Dest"] == "document"

    def test_canonical_xhr_has_correct_headers(self):
        canonical = _CANONICAL_REQUEST_HEADERS["xhr"]
        assert "application/json" in canonical["Accept"]
        assert canonical["X-Requested-With"] == "XMLHttpRequest"
        assert canonical["Sec-Fetch-Mode"] == "cors"

    def test_canonical_form_has_correct_headers(self):
        canonical = _CANONICAL_REQUEST_HEADERS["form"]
        assert "text/html" in canonical["Accept"]
        assert canonical["Content-Type"] == "application/x-www-form-urlencoded"
        assert canonical["Sec-Fetch-Mode"] == "navigate"
        assert canonical["Sec-Fetch-Dest"] == "document"

    def test_unknown_default_profile_warns(self, capsys):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        session.gp_default_profile = "typo"
        req = requests.Request("GET", "https://example.com/page")
        prepared = session.prepare_request(req)
        captured = capsys.readouterr()
        assert "unknown_profile_no_headers_applied" in captured.out
        # Browser identity still present from session defaults
        assert prepared.headers["User-Agent"] == "Mozilla/5.0 Test"

    def test_captured_profile_preferred_over_canonical(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        req = requests.Request("GET", "https://example.com/page")
        prepared = session.prepare_request(req)
        # Uses captured navigation Accept, not canonical
        assert prepared.headers["Accept"] == "text/html,application/xhtml+xml"

    def test_browser_ua_present_in_canonical_fallback(self):
        profiles = {
            "xhr": {
                "User-Agent": "Mozilla/5.0 XHR Only",
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
        }
        session = GraftpunkSession(header_profiles=profiles)
        req = requests.Request("GET", "https://example.com/page")
        prepared = session.prepare_request(req)
        # Canonical navigation headers used for the request type
        assert prepared.headers.get("Sec-Fetch-Mode") == "navigate"
        # But browser UA still present from session defaults
        assert prepared.headers["User-Agent"] == "Mozilla/5.0 XHR Only"


class TestHeaderPriorityChain:
    """Full priority chain: caller > user-set session > profile > identity defaults."""

    def test_full_priority_chain(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        # User-set session header overrides profile
        session.headers["Accept-Language"] = "de-DE"
        req = requests.Request(
            "GET",
            "https://example.com/page",
            # Caller header overrides everything
            headers={"User-Agent": "CallerAgent"},
        )
        prepared = session.prepare_request(req)
        # Caller wins over profile
        assert prepared.headers["User-Agent"] == "CallerAgent"
        # User-set session wins over profile
        assert prepared.headers["Accept-Language"] == "de-DE"
        # Profile value used when not overridden
        assert prepared.headers["Accept"] == "text/html,application/xhtml+xml"
        # Identity default (sec-ch-ua) still present
        assert prepared.headers["sec-ch-ua"] == '"Chromium";v="120", "Google Chrome";v="120"'


class TestSessionHeaderRestoration:
    """Session headers must be restored after prepare_request, even on error."""

    def test_headers_restored_after_successful_prepare(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        original_headers = dict(session.headers)
        req = requests.Request("GET", "https://example.com/page")
        session.prepare_request(req)
        assert dict(session.headers) == original_headers

    def test_headers_restored_after_exception(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        original_headers = dict(session.headers)
        # Mock super().prepare_request to raise
        original_prepare = requests.Session.prepare_request
        requests.Session.prepare_request = MagicMock(side_effect=ValueError("boom"))
        try:
            req = requests.Request("GET", "https://example.com/page")
            with contextlib.suppress(ValueError):
                session.prepare_request(req)
            assert dict(session.headers) == original_headers
        finally:
            requests.Session.prepare_request = original_prepare


class TestWarnings:
    """Warning logs for misconfiguration scenarios."""

    def test_no_ua_in_any_profile_warns(self, capsys):
        profiles = {
            "navigation": {"Accept": "text/html"},
            "xhr": {"Accept": "application/json"},
        }
        GraftpunkSession(header_profiles=profiles)
        captured = capsys.readouterr()
        assert "no_browser_identity_in_profiles" in captured.out

    def test_empty_profiles_does_not_warn(self, capsys):
        GraftpunkSession(header_profiles={})
        captured = capsys.readouterr()
        assert "no_browser_identity_in_profiles" not in captured.out


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


class TestResolveReferer:
    """Test Referer URL resolution from path or full URL."""

    def test_path_joined_with_base_url(self):
        session = GraftpunkSession(
            header_profiles=SAMPLE_PROFILES, base_url="https://www.example.com"
        )
        assert session._resolve_referer("/invoice/list") == "https://www.example.com/invoice/list"

    def test_full_url_used_as_is(self):
        session = GraftpunkSession(
            header_profiles=SAMPLE_PROFILES, base_url="https://www.example.com"
        )
        assert (
            session._resolve_referer("https://other.example.com/page")
            == "https://other.example.com/page"
        )

    def test_path_without_base_url_warns(self, capsys):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        result = session._resolve_referer("/some/path")
        captured = capsys.readouterr()
        assert "referer_path_without_base_url" in captured.out
        assert result == "/some/path"

    def test_base_url_trailing_slash_handled(self):
        session = GraftpunkSession(
            header_profiles=SAMPLE_PROFILES, base_url="https://www.example.com/"
        )
        assert session._resolve_referer("/invoice/list") == "https://www.example.com/invoice/list"


class TestProfileHeadersFor:
    """Test _profile_headers_for composition of captured + canonical headers."""

    def test_captured_profile_returned(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        headers = session._profile_headers_for("xhr")
        assert headers["X-Requested-With"] == "XMLHttpRequest"
        assert headers["Accept"] == "application/json"

    def test_missing_profile_falls_back_to_canonical(self):
        profiles = {
            "xhr": {
                "User-Agent": "Mozilla/5.0 Test",
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
        }
        session = GraftpunkSession(header_profiles=profiles)
        headers = session._profile_headers_for("navigation")
        assert "text/html" in headers["Accept"]
        assert headers["Sec-Fetch-Mode"] == "navigate"

    def test_returns_copy_not_reference(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        headers = session._profile_headers_for("xhr")
        headers["New-Header"] = "value"
        assert "New-Header" not in session._gp_header_profiles.get("xhr", {})

    def test_unknown_profile_returns_empty(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        headers = session._profile_headers_for("nonexistent")
        assert headers == {}

    def test_excludes_identity_headers(self):
        session = GraftpunkSession(header_profiles=SAMPLE_PROFILES)
        headers = session._profile_headers_for("navigation")
        assert "User-Agent" not in headers
        assert "sec-ch-ua" not in headers
        assert "Accept-Language" not in headers
        assert "Accept-Encoding" not in headers
