"""Tests for GraftpunkSession browser header replay."""

import requests

from graftpunk.graftpunk_session import GraftpunkSession

SAMPLE_PROFILES = {
    "navigation": {
        "User-Agent": "Mozilla/5.0 Test",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    },
    "xhr": {
        "User-Agent": "Mozilla/5.0 Test",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    },
    "form": {
        "User-Agent": "Mozilla/5.0 Test",
        "Accept": "text/html",
        "Content-Type": "application/x-www-form-urlencoded",
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

    def test_missing_form_profile_falls_back_to_navigation(self):
        profiles = {
            "navigation": {"User-Agent": "Nav/1.0", "Accept": "text/html"},
        }
        session = GraftpunkSession(header_profiles=profiles)
        req = requests.Request("POST", "https://example.com/submit", data="key=val")
        prepared = session.prepare_request(req)
        assert prepared.headers["User-Agent"] == "Nav/1.0"


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
