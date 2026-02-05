"""Tests for header profile classification and extraction."""

from graftpunk.observe.capture import NodriverCaptureBackend, SeleniumCaptureBackend
from graftpunk.observe.headers import EXCLUDED_HEADERS, classify_request, extract_header_profiles

# --- classify_request() tests ---


def test_classify_navigation_by_sec_fetch_mode():
    headers = {"sec-fetch-mode": "navigate", "Accept": "text/html"}
    assert classify_request(headers) == "navigation"


def test_classify_xhr_by_sec_fetch_mode():
    headers = {"sec-fetch-mode": "cors", "Accept": "application/json"}
    assert classify_request(headers) == "xhr"


def test_classify_xhr_by_x_requested_with():
    headers = {"X-Requested-With": "XMLHttpRequest", "Accept": "*/*"}
    assert classify_request(headers) == "xhr"


def test_classify_xhr_by_accept_json():
    headers = {"Accept": "application/json, text/javascript, */*; q=0.01"}
    assert classify_request(headers) == "xhr"


def test_classify_form_by_content_type_urlencoded():
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    assert classify_request(headers) == "form"


def test_classify_form_by_content_type_multipart():
    headers = {"Content-Type": "multipart/form-data; boundary=----"}
    assert classify_request(headers) == "form"


def test_classify_returns_none_for_image():
    headers = {"Accept": "image/webp,image/png,*/*;q=0.8"}
    assert classify_request(headers) is None


def test_classify_returns_none_for_css():
    headers = {"Accept": "text/css,*/*;q=0.1"}
    assert classify_request(headers) is None


def test_classify_sec_fetch_mode_takes_priority():
    """sec-fetch-mode: navigate overrides Content-Type: form."""
    headers = {"sec-fetch-mode": "navigate", "Content-Type": "application/x-www-form-urlencoded"}
    assert classify_request(headers) == "navigation"


def test_classify_navigation_by_accept_html():
    """text/html in Accept triggers navigation when no sec-fetch-mode."""
    headers = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9"}
    assert classify_request(headers) == "navigation"


def test_classify_empty_headers():
    assert classify_request({}) is None


# --- EXCLUDED_HEADERS tests ---


def test_excluded_headers_contains_cookie():
    assert "cookie" in EXCLUDED_HEADERS


def test_excluded_headers_contains_host():
    assert "host" in EXCLUDED_HEADERS


def test_excluded_headers_contains_pseudo_headers():
    assert ":authority" in EXCLUDED_HEADERS
    assert ":method" in EXCLUDED_HEADERS
    assert ":path" in EXCLUDED_HEADERS
    assert ":scheme" in EXCLUDED_HEADERS


def test_excluded_headers_contains_content_length():
    assert "content-length" in EXCLUDED_HEADERS


def test_excluded_headers_contains_content_type():
    assert "content-type" in EXCLUDED_HEADERS


def test_extract_header_profiles_excludes_content_type():
    """Content-Type from POST XHR should not be captured in profile."""
    request_map = {
        "1": {
            "headers": {
                "sec-fetch-mode": "cors",
                "Accept": "application/json",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            },
        },
    }
    profiles = extract_header_profiles(request_map)
    assert "xhr" in profiles
    assert "Content-Type" not in profiles["xhr"]
    assert profiles["xhr"]["Accept"] == "application/json"


def test_excluded_headers_contains_referer_and_origin():
    assert "referer" in EXCLUDED_HEADERS
    assert "origin" in EXCLUDED_HEADERS


# --- extract_header_profiles() tests ---


def test_extract_header_profiles_from_request_map():
    """Build profiles from a realistic _request_map."""
    request_map = {
        "1": {
            "url": "https://example.com/",
            "method": "GET",
            "headers": {
                "sec-fetch-mode": "navigate",
                "User-Agent": "Mozilla/5.0 ...",
                "Accept": "text/html,application/xhtml+xml",
                "Cookie": "session=abc",
                "Host": "example.com",
                ":authority": "example.com",
            },
        },
        "2": {
            "url": "https://example.com/api/data",
            "method": "GET",
            "headers": {
                "sec-fetch-mode": "cors",
                "User-Agent": "Mozilla/5.0 ...",
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Cookie": "session=abc",
            },
        },
        "3": {
            "url": "https://example.com/logo.png",
            "method": "GET",
            "headers": {"Accept": "image/webp,image/png"},
        },
    }
    profiles = extract_header_profiles(request_map)
    assert "navigation" in profiles
    assert "xhr" in profiles
    assert "Cookie" not in profiles["navigation"]
    assert "Host" not in profiles["navigation"]
    assert ":authority" not in profiles["navigation"]
    assert profiles["navigation"]["User-Agent"] == "Mozilla/5.0 ..."
    assert profiles["xhr"]["X-Requested-With"] == "XMLHttpRequest"
    assert "Cookie" not in profiles["xhr"]


def test_extract_header_profiles_first_match_wins():
    """Only the first matching request per profile is stored."""
    request_map = {
        "1": {"headers": {"sec-fetch-mode": "navigate", "User-Agent": "First"}},
        "2": {"headers": {"sec-fetch-mode": "navigate", "User-Agent": "Second"}},
    }
    profiles = extract_header_profiles(request_map)
    assert profiles["navigation"]["User-Agent"] == "First"


def test_extract_header_profiles_empty_map():
    assert extract_header_profiles({}) == {}


def test_extract_header_profiles_skips_empty_headers():
    request_map = {
        "1": {"headers": {}},
        "2": {"headers": {"sec-fetch-mode": "navigate", "User-Agent": "Test"}},
    }
    profiles = extract_header_profiles(request_map)
    assert "navigation" in profiles
    assert profiles["navigation"]["User-Agent"] == "Test"


def test_selenium_backend_get_header_profiles():
    """SeleniumCaptureBackend exposes header profiles from its request map."""
    backend = SeleniumCaptureBackend.__new__(SeleniumCaptureBackend)
    backend._request_map = {
        "1": {"headers": {"sec-fetch-mode": "navigate", "User-Agent": "Test/1.0"}},
    }
    profiles = backend.get_header_profiles()
    assert "navigation" in profiles
    assert profiles["navigation"]["User-Agent"] == "Test/1.0"


def test_nodriver_backend_get_header_profiles():
    """NodriverCaptureBackend exposes header profiles from its request map."""
    backend = NodriverCaptureBackend.__new__(NodriverCaptureBackend)
    backend._request_map = {
        "1": {"headers": {"sec-fetch-mode": "cors", "Accept": "application/json"}},
    }
    profiles = backend.get_header_profiles()
    assert "xhr" in profiles


def test_backend_get_header_profiles_empty():
    """Backend with empty request map returns empty profiles."""
    backend = SeleniumCaptureBackend.__new__(SeleniumCaptureBackend)
    backend._request_map = {}
    assert backend.get_header_profiles() == {}
