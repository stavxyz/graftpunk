"""Tests for header role classification and extraction."""

import pytest

from graftpunk.observe.capture import NodriverCaptureBackend, SeleniumCaptureBackend
from graftpunk.observe.headers import EXCLUDED_HEADERS, classify_request, extract_header_roles

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


def test_classify_a_form_content_type_beats_a_navigate_mode():
    """Chrome submits every HTML form with Sec-Fetch-Mode: navigate, as its wire
    headers show; navigate first would leave no request to take the form role from."""
    headers = {"sec-fetch-mode": "navigate", "Content-Type": "application/x-www-form-urlencoded"}
    assert classify_request(headers) == "form"


def test_classify_navigation_by_accept_html():
    """text/html in Accept triggers navigation when no sec-fetch-mode."""
    headers = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9"}
    assert classify_request(headers) == "navigation"


def test_classify_empty_headers():
    assert classify_request({}) is None


# --- EXCLUDED_HEADERS tests ---


def test_excluded_headers_contains_cookie():
    assert "cookie" in EXCLUDED_HEADERS


def test_a_role_keeps_no_header_the_client_sets_itself():
    """A role recorded from Chrome over HTTP/2 (#370): the pseudo-headers, Priority,
    and the headers the HTTP client sends on its own never reach a role, so the
    session never replays them; Chrome's Accept-Encoding offers zstd, which the
    client cannot decode without zstandard."""
    request_map = {
        "1": {
            "headers": {
                ":authority": "myshop.example",
                ":method": "GET",
                ":path": "/api/orders",
                ":scheme": "https",
                ":protocol": "websocket",
                "priority": "u=1, i",
                "Host": "myshop.example",
                "Connection": "keep-alive",
                "Content-Length": "0",
                "accept-encoding": "gzip, deflate, br, zstd",
                "sec-fetch-mode": "cors",
                "Accept": "application/json",
                "x-api-key": "k-0f9e8d7c6b5a",
            },
        },
    }
    assert extract_header_roles(request_map) == {
        "xhr": {
            "sec-fetch-mode": "cors",
            "Accept": "application/json",
            "x-api-key": "k-0f9e8d7c6b5a",
        }
    }


def test_excluded_headers_contains_content_type():
    assert "content-type" in EXCLUDED_HEADERS


def test_extract_header_roles_excludes_content_type():
    """Content-Type from POST XHR should not be captured in role."""
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
    roles = extract_header_roles(request_map)
    assert "xhr" in roles
    assert "Content-Type" not in roles["xhr"]
    assert roles["xhr"]["Accept"] == "application/json"


def test_excluded_headers_contains_referer_and_origin():
    assert "referer" in EXCLUDED_HEADERS
    assert "origin" in EXCLUDED_HEADERS


def test_excluded_headers_contains_x_csrf_token():
    """x-csrf-token excluded — ephemeral Akamai Bot Manager sensor blob."""
    assert "x-csrf-token" in EXCLUDED_HEADERS


def test_extract_header_roles_excludes_x_csrf_token():
    """X-CSRF-TOKEN from login POST should not leak into role profiles."""
    request_map = {
        "1": {
            "headers": {
                "sec-fetch-mode": "cors",
                "Accept": "application/json",
                "X-CSRF-TOKEN": "base64-akamai-sensor-blob",
                "X-Requested-With": "XMLHttpRequest",
            },
        },
    }
    roles = extract_header_roles(request_map)
    assert "xhr" in roles
    assert "X-CSRF-TOKEN" not in roles["xhr"]


# --- extract_header_roles() tests ---


def test_extract_header_roles_from_request_map():
    """Build roles from a realistic _request_map."""
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
    roles = extract_header_roles(request_map)
    assert "navigation" in roles
    assert "xhr" in roles
    assert "Cookie" not in roles["navigation"]
    assert "Host" not in roles["navigation"]
    assert ":authority" not in roles["navigation"]
    assert roles["navigation"]["User-Agent"] == "Mozilla/5.0 ..."
    assert roles["xhr"]["X-Requested-With"] == "XMLHttpRequest"
    assert "Cookie" not in roles["xhr"]


def test_extract_header_roles_first_match_wins():
    """Only the first matching request per role is stored."""
    request_map = {
        "1": {"headers": {"sec-fetch-mode": "navigate", "User-Agent": "First"}},
        "2": {"headers": {"sec-fetch-mode": "navigate", "User-Agent": "Second"}},
    }
    roles = extract_header_roles(request_map)
    assert roles["navigation"]["User-Agent"] == "First"


def test_extract_header_roles_empty_map():
    assert extract_header_roles({}) == {}


def test_extract_header_roles_skips_empty_headers():
    request_map = {
        "1": {"headers": {}},
        "2": {"headers": {"sec-fetch-mode": "navigate", "User-Agent": "Test"}},
    }
    roles = extract_header_roles(request_map)
    assert "navigation" in roles
    assert roles["navigation"]["User-Agent"] == "Test"


def test_selenium_backend_get_header_roles():
    """SeleniumCaptureBackend exposes header roles from its request map."""
    backend = SeleniumCaptureBackend.__new__(SeleniumCaptureBackend)
    backend._request_map = {
        "1": {"headers": {"sec-fetch-mode": "navigate", "User-Agent": "Test/1.0"}},
    }
    roles = backend.get_header_roles()
    assert "navigation" in roles
    assert roles["navigation"]["User-Agent"] == "Test/1.0"


def test_nodriver_backend_get_header_roles():
    """NodriverCaptureBackend exposes header roles from its request map."""
    backend = NodriverCaptureBackend(None, record_request_bodies=False)
    backend._request_map = {
        "1": {"headers": {"sec-fetch-mode": "cors", "Accept": "application/json"}},
    }
    roles = backend.get_header_roles()
    assert "xhr" in roles


def test_backend_get_header_roles_empty():
    """Backend with empty request map returns empty roles."""
    backend = SeleniumCaptureBackend.__new__(SeleniumCaptureBackend)
    backend._request_map = {}
    assert backend.get_header_roles() == {}


# --- the site's own origin first (#228 review) ---


def _cors(url: str, marker: str) -> dict:
    return {"url": url, "headers": {"sec-fetch-mode": "cors", "X-Marker": marker}}


def test_each_role_comes_from_the_sites_own_origin_when_the_login_called_it():
    from graftpunk.observe.headers import header_role_origins

    request_map = {
        "1": _cors("https://analytics.other.example/collect", "tracker"),
        "2": _cors("https://api.myshop.example/session", "subdomain"),
        "3": _cors("https://myshop.example/api/me", "own"),
        "4": _cors("https://myshop.example/api/cart", "own-later"),
    }
    roles = extract_header_roles(request_map, "https://myshop.example/login")
    assert roles["xhr"]["X-Marker"] == "own"
    assert header_role_origins(request_map, "https://myshop.example") == {
        "xhr": "https://myshop.example"
    }


def test_another_origin_of_the_site_beats_a_third_party():
    from graftpunk.observe.headers import header_role_origins

    request_map = {
        "1": _cors("https://sso.other.example/token", "sso"),
        "2": _cors("https://api.myshop.example/session", "subdomain"),
    }
    roles = extract_header_roles(request_map, "https://myshop.example")
    assert roles["xhr"]["X-Marker"] == "subdomain"
    assert header_role_origins(request_map, "https://myshop.example") == {
        "xhr": "https://api.myshop.example"
    }


def test_with_nothing_on_the_site_the_first_request_is_kept_with_its_origin():
    from graftpunk.observe.headers import header_role_origins

    request_map = {
        "1": _cors("https://sso.other.example/token", "sso"),
        "2": _cors("https://cdn.other.example/x", "cdn"),
    }
    assert extract_header_roles(request_map, "https://myshop.example")["xhr"]["X-Marker"] == "sso"
    assert header_role_origins(request_map, "https://myshop.example") == {
        "xhr": "https://sso.other.example"
    }
    assert extract_header_roles(request_map)["xhr"]["X-Marker"] == "sso"


def test_the_login_stores_each_roles_origin_on_the_session():
    from types import SimpleNamespace

    from graftpunk.observe.headers import header_role_origins
    from graftpunk.plugins.login_engine import _store_header_roles

    request_map = {
        "1": _cors("https://sso.other.example/token", "sso"),
        "2": _cors("https://myshop.example/api/me", "own"),
    }
    capture = SimpleNamespace(
        get_header_roles=lambda site_url=None: extract_header_roles(request_map, site_url),
        get_header_role_origins=lambda site_url=None: header_role_origins(request_map, site_url),
    )
    session = SimpleNamespace()
    _store_header_roles(session, capture, "https://myshop.example")
    assert session._gp_header_roles["xhr"]["X-Marker"] == "own"
    assert session._gp_header_role_origins == {"xhr": "https://myshop.example"}


def test_a_generated_login_whose_capture_found_no_roles_warns_with_the_requests_seen():
    from types import SimpleNamespace

    from structlog.testing import capture_logs

    from graftpunk.plugins.login_engine import _store_header_roles

    for backend in (
        NodriverCaptureBackend(None, record_request_bodies=False),
        SeleniumCaptureBackend(None, record_request_bodies=False),
    ):
        backend._request_map = {
            "1": {"url": "https://myshop.example/logo.png", "headers": {"Accept": "image/png"}},
            "2": {"url": "https://myshop.example/app.css", "headers": {"Accept": "text/css"}},
        }
        session = SimpleNamespace()
        with capture_logs() as logs:
            _store_header_roles(session, backend, "https://myshop.example")
        empty = [e for e in logs if e["event"] == "login_header_roles_empty"]
        assert len(empty) == 1
        assert empty[0]["log_level"] == "warning"
        assert empty[0]["requests"] == 2
        assert session._gp_header_roles == {}


# --- the headers Chrome sent, not the renderer's partial set ---
#
# Network.requestWillBeSent carries the renderer's request headers, which on a
# navigation lack Accept, Accept-Language and every Sec-Fetch-* header, and on the
# GET after a form POST's redirect still carry the POST's Content-Type and Origin
# (read from Chrome 158 through nodriver on 2026-11-27).
# requestWillBeSentExtraInfo carries the headers sent; the correlator keeps them
# on the entry as _request_extra_headers. Entries below are shaped as both
# backends store them.

CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36"
)
HTML_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
    "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
)
CLIENT_HINTS = {
    "sec-ch-ua": '"Chromium";v="158", "Not.A/Brand";v="24", "Google Chrome";v="158"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}
RENDERER_NAV = {"Upgrade-Insecure-Requests": "1", "User-Agent": CHROME_UA, **CLIENT_HINTS}
RENDERER_POST = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://myshop.example",
    "Referer": "https://myshop.example/login",
    **RENDERER_NAV,
}


def _wire(**extra: str) -> dict[str, str]:
    return {
        "Accept": HTML_ACCEPT,
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Host": "myshop.example",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": CHROME_UA,
        **CLIENT_HINTS,
        **extra,
    }


def _entry(url: str, method: str, headers: dict, wire: dict | None, ts: float) -> dict:
    entry = {
        "url": url,
        "method": method,
        "headers": dict(headers),
        "post_data": None,
        "has_post_data": method == "POST",
        "timestamp": ts,
        "sender_url": None,
        "navigation": True,
        "resource_type": "Document",
        "sender_origin_only": False,
    }
    if wire is not None:
        entry["_request_extra_headers"] = dict(wire)
        entry["_request_extra_seen"] = True
    return entry


BROWSER_NAV_ROLE = {
    "Accept": HTML_ACCEPT,
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": CHROME_UA,
    **CLIENT_HINTS,
}


def test_a_page_load_is_a_navigation_role_from_the_headers_chrome_sent():
    request_map = {
        "1": _entry("https://myshop.example/login", "GET", RENDERER_NAV, _wire(), 1.0),
    }
    assert extract_header_roles(request_map, "https://myshop.example") == {
        "navigation": BROWSER_NAV_ROLE
    }


def test_a_form_post_role_holds_every_header_chrome_sent_but_the_excluded():
    wire = _wire(
        **{
            "Cache-Control": "max-age=0",
            "Content-Length": "38",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://myshop.example",
            "Referer": "https://myshop.example/login",
            "Cookie": "sid=abc",
        }
    )
    request_map = {"1": _entry("https://myshop.example/login", "POST", RENDERER_POST, wire, 1.0)}
    assert extract_header_roles(request_map, "https://myshop.example") == {"form": BROWSER_NAV_ROLE}


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("If-None-Match", '"abc"'),
        ("If-Modified-Since", "Wed, 25 Nov 2026 10:00:00 GMT"),
        ("If-Match", '"abc"'),
        ("If-Unmodified-Since", "Wed, 25 Nov 2026 10:00:00 GMT"),
        ("If-Range", '"abc"'),
        ("Range", "bytes=0-99"),
        ("Cache-Control", "max-age=0"),
        ("Pragma", "no-cache"),
        ("Proxy-Authorization", "Basic cHJveHk6c2VjcmV0"),
        ("Sec-Purpose", "prefetch"),
        ("Purpose", "prefetch"),
    ],
)
@pytest.mark.parametrize("spell", [str, str.lower])
def test_per_request_and_other_party_credential_headers_stay_out_of_roles(
    name: str, value: str, spell
):
    assert name.lower() in EXCLUDED_HEADERS
    request_map = {
        "1": _entry(
            "https://myshop.example/a", "GET", RENDERER_NAV, _wire(**{spell(name): value}), 1.0
        )
    }
    role = extract_header_roles(request_map, "https://myshop.example")["navigation"]
    assert name.lower() not in {k.lower() for k in role}
    assert role == BROWSER_NAV_ROLE


def test_the_get_after_a_post_redirect_is_classified_from_its_own_headers():
    """Chrome sends no Sec-Fetch-* to a plain-http site, so only Content-Type and
    Accept tell the two apart; the renderer's stale Content-Type on the redirect
    GET would make it a second form."""
    from graftpunk.observe.headers import header_role_origins

    no_fetch = {k: v for k, v in _wire().items() if not k.startswith("Sec-Fetch")}
    request_map = {
        "2:redirect:1": {
            **_entry(
                "http://myshop.example/login",
                "POST",
                RENDERER_POST,
                {**no_fetch, "Content-Type": "application/x-www-form-urlencoded"},
                1.0,
            ),
            "response": {"status": 302, "redirectURL": "http://myshop.example/home"},
            "_redirect_hop": True,
        },
        "2": _entry("http://myshop.example/home", "GET", RENDERER_POST, no_fetch, 2.0),
    }
    roles = extract_header_roles(request_map, "http://myshop.example")
    assert sorted(roles) == ["form", "navigation"]
    assert roles["navigation"]["Accept"] == HTML_ACCEPT
    assert header_role_origins(request_map, "http://myshop.example") == {
        "form": "http://myshop.example",
        "navigation": "http://myshop.example",
    }


def test_a_get_with_a_stale_form_content_type_and_no_wire_headers_is_no_form():
    request_map = {"1": _entry("https://myshop.example/home", "GET", RENDERER_POST, None, 1.0)}
    assert "form" not in extract_header_roles(request_map)


def test_without_wire_headers_the_renderer_headers_are_used():
    renderer = {**RENDERER_NAV, "Sec-Fetch-Mode": "navigate"}
    request_map = {"1": _entry("https://myshop.example/a", "GET", renderer, None, 1.0)}
    assert extract_header_roles(request_map)["navigation"]["User-Agent"] == CHROME_UA


def test_excluded_and_client_set_wire_headers_stay_out_of_every_role():
    wire_h2 = {
        ":authority": "myshop.example",
        ":method": "GET",
        ":path": "/login",
        ":scheme": "https",
        "accept": HTML_ACCEPT,
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "en-US,en;q=0.9",
        "content-length": "0",
        "cookie": "sid=abc",
        "priority": "u=0, i",
        "sec-fetch-mode": "navigate",
        "user-agent": CHROME_UA,
        "x-csrf-token": "t0k3n",
        "referer": "https://myshop.example/",
        "origin": "https://myshop.example",
        "content-type": "text/plain",
        "host": "myshop.example",
        "connection": "keep-alive",
        **CLIENT_HINTS,
    }
    request_map = {"1": _entry("https://myshop.example/login", "GET", RENDERER_NAV, wire_h2, 1.0)}
    (role,) = extract_header_roles(request_map).values()
    assert {name.lower() for name in role} == {
        "accept",
        "accept-language",
        "sec-fetch-mode",
        "user-agent",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
    }


def test_a_name_in_both_sets_in_another_case_is_kept_once_in_the_renderers_case():
    wire = {"user-agent": CHROME_UA, "sec-fetch-mode": "navigate", "upgrade-insecure-requests": "1"}
    request_map = {"1": _entry("https://myshop.example/", "GET", RENDERER_NAV, wire, 1.0)}
    assert extract_header_roles(request_map)["navigation"] == {
        "User-Agent": CHROME_UA,
        "sec-fetch-mode": "navigate",
        "Upgrade-Insecure-Requests": "1",
    }


def test_classify_a_navigating_form_post_is_a_form():
    headers = {"Sec-Fetch-Mode": "navigate", "Content-Type": "multipart/form-data; boundary=x"}
    assert classify_request(headers, method="POST") == "form"


@pytest.mark.parametrize(
    "content_type", ["application/x-www-form-urlencoded", "multipart/form-data; boundary=x"]
)
@pytest.mark.parametrize("mode", ["cors", "navigate", "same-origin", "no-cors", None])
def test_classify_a_form_body_post_is_a_form_in_every_mode(content_type: str, mode: str | None):
    """1.46.0 classified a form-bodied POST as a form from the renderer headers,
    which carry no Sec-Fetch-Mode; a fetch() of FormData stays a form in 1.46.1."""
    headers = {"Content-Type": content_type}
    if mode is not None:
        headers["Sec-Fetch-Mode"] = mode
    assert classify_request(headers, method="POST") == "form"


def test_classify_a_json_xhr_is_still_xhr():
    headers = {
        "Sec-Fetch-Mode": "cors",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    assert classify_request(headers, method="POST") == "xhr"
    request_map = {
        "1": _entry("https://myshop.example/api", "POST", {}, {**_wire(), **headers}, 1.0)
    }
    assert list(extract_header_roles(request_map)) == ["xhr"]


def test_classify_a_get_is_never_a_form():
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    assert classify_request(headers, method="GET") is None


# --- Sec-Fetch-Site: none says the user typed the address ---
#
# A page a login opens with Tab.get is sent as a typed address is. A command
# replaying that would claim the same of every request; 1.46.0 sent the
# registered navigation role's same-origin, and 1.46.1 keeps that.


def _nav(
    url: str, site: str, ts: float, *, spelling: str = "Sec-Fetch-Site", dest: str = "document"
) -> dict:
    wire = {k: v for k, v in _wire().items() if k != "Sec-Fetch-Site"}
    wire[spelling] = site
    wire["Sec-Fetch-Dest"] = dest
    return _entry(url, "GET", RENDERER_NAV, {**wire, "X-Marker": f"{site}@{ts}"}, ts)


def test_a_navigation_role_from_only_typed_page_loads_says_same_origin():
    request_map = {
        "1": _nav("https://myshop.example/login", "none", 1.0),
        "2": _nav("https://myshop.example/home", "none", 2.0),
    }
    role = extract_header_roles(request_map, SITE)["navigation"]
    assert role["Sec-Fetch-Site"] == "same-origin"
    assert role["X-Marker"] == "none@1.0"


def test_the_rewrite_keeps_the_captured_spelling():
    request_map = {"1": _nav("https://myshop.example/", "none", 1.0, spelling="sec-fetch-site")}
    role = extract_header_roles(request_map, SITE)["navigation"]
    assert role["sec-fetch-site"] == "same-origin"
    assert "Sec-Fetch-Site" not in role


def test_a_page_load_the_site_sent_is_preferred_to_a_typed_one():
    from graftpunk.observe.headers import header_role_origins

    request_map = {
        "1": _nav("https://myshop.example/login", "none", 1.0),
        "2": _nav("https://sso.other.example/authorize", "cross-site", 2.0),
        "3": _nav("https://api.myshop.example/start", "same-site", 3.0),
        "4": _nav("https://myshop.example/home", "same-origin", 4.0),
        "5": _nav("https://myshop.example/later", "same-origin", 5.0),
    }
    role = extract_header_roles(request_map, SITE)["navigation"]
    assert role["X-Marker"] == "same-origin@4.0"
    assert header_role_origins(request_map, SITE) == {"navigation": SITE}


def test_a_typed_load_of_the_site_beats_a_sent_load_of_another_origin():
    """The origin rank comes before typed or sent: the site's own page load is the
    one whose headers its commands should look like."""
    request_map = {
        "1": _nav("https://myshop.example/login", "none", 1.0),
        "2": _nav("https://api.myshop.example/start", "same-site", 2.0),
    }
    role = extract_header_roles(request_map, SITE)["navigation"]
    assert role["X-Marker"] == "none@1.0"
    assert role["Sec-Fetch-Site"] == "same-origin"


def test_a_typed_site_load_beats_a_cross_site_frame_navigation():
    """A challenge widget's frame (a reCAPTCHA-style anchor) is a navigation Chrome
    sent, to another site, inside the page."""
    from graftpunk.observe.headers import header_role_origins

    request_map = {
        "1": _nav("https://myshop.example/login", "none", 1.0),
        "2": _nav("https://captcha.other.example/anchor", "cross-site", 2.0, dest="iframe"),
    }
    assert extract_header_roles(request_map, SITE)["navigation"]["X-Marker"] == "none@1.0"
    assert header_role_origins(request_map, SITE) == {"navigation": SITE}


def test_a_typed_document_beats_a_same_origin_iframe():
    request_map = {
        "1": _nav("https://myshop.example/login", "none", 1.0),
        "2": _nav("https://myshop.example/widget", "same-origin", 2.0, dest="iframe"),
    }
    role = extract_header_roles(request_map, SITE)["navigation"]
    assert role["X-Marker"] == "none@1.0"
    assert role["Sec-Fetch-Dest"] == "document"


@pytest.mark.parametrize("dest", ["frame", "iframe", "embed", "object"])
def test_a_framed_load_is_never_the_navigation_role(dest: str):
    """A frame's headers are not the page's: a login whose only page loads were
    framed caches no navigation role, as 1.46.0 cached none for it."""
    framed = {"1": _nav("https://myshop.example/inner", "same-origin", 1.0, dest=dest)}
    assert extract_header_roles(framed, SITE) == {}
    with_document = {**framed, "2": _nav("https://other.example/page", "cross-site", 2.0)}
    assert extract_header_roles(with_document, SITE)["navigation"]["X-Marker"] == "cross-site@2.0"


@pytest.mark.parametrize("captured", ["none", "same-site", "cross-site", "same-origin"])
def test_the_navigation_role_always_stores_same_origin(captured: str):
    request_map = {"1": _nav("https://myshop.example/a", captured, 1.0, spelling="sec-fetch-site")}
    role = extract_header_roles(request_map, SITE)["navigation"]
    assert role["sec-fetch-site"] == "same-origin"
    assert "Sec-Fetch-Site" not in role


def test_an_sso_callback_role_says_same_origin_on_the_site_and_cross_site_elsewhere():
    """The site's page load after an identity provider sends the browser back is
    cross-site as captured; replayed on the site it is a same-origin load, and a
    request to the provider still gets the computed relation."""
    from graftpunk.graftpunk_session import GraftpunkSession
    from graftpunk.observe.headers import header_role_origins

    request_map = {
        "1": _nav("https://myshop.example/login", "none", 1.0),
        "2": _nav("https://sso.other.example/authorize", "cross-site", 2.0),
        "3": _nav("https://myshop.example/callback", "cross-site", 3.0),
    }
    roles = extract_header_roles(request_map, SITE)
    assert roles["navigation"]["X-Marker"] == "cross-site@3.0"
    assert roles["navigation"]["Sec-Fetch-Site"] == "same-origin"

    import requests

    sent: list[requests.PreparedRequest] = []

    class Wire(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):  # noqa: ANN001, ANN003, ANN201
            sent.append(request)
            response = requests.Response()
            response.status_code = 200
            response.request = request
            response.url = request.url
            response._content = b""
            return response

        def close(self) -> None:
            pass

    session = GraftpunkSession(header_roles=roles, base_url=SITE)
    session._gp_header_role_origins = header_role_origins(request_map, SITE)
    session.mount("https://", Wire())
    session.navigate("GET", f"{SITE}/orders")
    session.navigate("GET", "https://sso.other.example/account")
    on_site, elsewhere = sent
    assert on_site.headers["Sec-Fetch-Site"] == "same-origin"
    assert on_site.headers["X-Marker"] == "cross-site@3.0"
    assert elsewhere.headers["Sec-Fetch-Site"] == "cross-site"
    assert "X-Marker" not in elsewhere.headers


@pytest.mark.parametrize(
    ("captured", "stored"),
    [
        ("none", "same-origin"),
        ("cross-site", "same-origin"),
        ("same-origin", "same-origin"),
        ("same-site", "same-site"),
    ],
)
def test_form_and_xhr_roles_store_a_relation_their_own_origin_can_have(captured: str, stored: str):
    """A role is sent only to the origin it came from. A form posted there from
    another site (an SSO form_post callback, a SAML POST) or typed has no such
    relation to it; a same-site one (a sibling origin's page posting) can."""
    form_wire = _wire(**{"Content-Type": "application/x-www-form-urlencoded"})
    form_wire["sec-fetch-site"] = captured
    del form_wire["Sec-Fetch-Site"]
    xhr_wire = {**_wire(), "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": captured}
    request_map = {
        "1": _nav("https://myshop.example/login", "none", 1.0),
        "2": _entry("https://myshop.example/login", "POST", RENDERER_POST, form_wire, 2.0),
        "3": _entry("https://myshop.example/api", "GET", RENDERER_NAV, xhr_wire, 3.0),
    }
    roles = extract_header_roles(request_map, SITE)
    assert roles["navigation"]["Sec-Fetch-Site"] == "same-origin"
    assert roles["form"]["sec-fetch-site"] == stored
    assert "Sec-Fetch-Site" not in roles["form"]
    assert roles["xhr"]["Sec-Fetch-Site"] == stored


def test_an_oidc_form_post_logins_form_role_is_replayed_as_same_origin():
    """The identity provider's page auto-posts to the site's callback: the site's
    own form POST is cross-site as captured, and a command's form POST to the
    site must not say so."""
    from graftpunk.graftpunk_session import GraftpunkSession
    from graftpunk.observe.headers import header_role_origins

    form = "application/x-www-form-urlencoded"

    def posted(url: str, site: str, ts: float) -> dict:
        wire = _wire(**{"Content-Type": form, "X-Marker": f"{url}@{ts}"})
        wire["Sec-Fetch-Site"] = site
        return _entry(url, "POST", RENDERER_POST, wire, ts)

    request_map = {
        "1": _nav(f"{SITE}/login", "none", 1.0),
        "2": _nav("https://idp.other.example/authorize", "cross-site", 2.0),
        "3": posted("https://idp.other.example/login", "same-origin", 3.0),
        "4": posted(f"{SITE}/oidc/callback", "cross-site", 4.0),
        "5": _nav(f"{SITE}/home", "cross-site", 5.0),
    }
    roles = extract_header_roles(request_map, SITE)
    assert roles["form"]["X-Marker"] == f"{SITE}/oidc/callback@4.0"
    assert roles["form"]["Sec-Fetch-Site"] == "same-origin"

    import requests

    sent: list[requests.PreparedRequest] = []

    class Wire(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):  # noqa: ANN001, ANN003, ANN201
            sent.append(request)
            response = requests.Response()
            response.status_code = 200
            response.request = request
            response.url = request.url
            response._content = b""
            return response

        def close(self) -> None:
            pass

    session = GraftpunkSession(header_roles=roles, base_url=SITE)
    session._gp_header_role_origins = header_role_origins(request_map, SITE)
    session.mount("https://", Wire())
    session.post(f"{SITE}/cart/add", data={"sku": "1"})
    [command] = sent
    assert command.headers["Sec-Fetch-Site"] == "same-origin"
    assert command.headers["Sec-Fetch-Mode"] == "navigate"


# --- both backends: the same Chrome events give the same roles ---

SITE = "https://myshop.example"
POST_WIRE = _wire(
    **{
        "Cache-Control": "max-age=0",
        "Content-Length": "38",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": SITE,
        "Referer": f"{SITE}/login",
    }
)
# A form login as Chrome reports it: the login page, the POST, its 302, the GET.
LOGIN_EVENTS: list[tuple[str, dict]] = [
    (
        "Network.requestWillBeSent",
        {
            "requestId": "1",
            "wallTime": 1_700_000_001.0,
            "type": "Document",
            "documentURL": f"{SITE}/login",
            "request": {"url": f"{SITE}/login", "method": "GET", "headers": RENDERER_NAV},
        },
    ),
    ("Network.requestWillBeSentExtraInfo", {"requestId": "1", "headers": _wire()}),
    (
        "Network.responseReceived",
        {
            "requestId": "1",
            "response": {
                "url": f"{SITE}/login",
                "status": 200,
                "statusText": "OK",
                "headers": {"Content-Type": "text/html"},
                "mimeType": "text/html",
            },
        },
    ),
    (
        "Network.requestWillBeSent",
        {
            "requestId": "2",
            "wallTime": 1_700_000_002.0,
            "type": "Document",
            "documentURL": f"{SITE}/login",
            "request": {
                "url": f"{SITE}/login",
                "method": "POST",
                "headers": RENDERER_POST,
                "hasPostData": True,
            },
        },
    ),
    ("Network.requestWillBeSentExtraInfo", {"requestId": "2", "headers": POST_WIRE}),
    (
        "Network.responseReceivedExtraInfo",
        {"requestId": "2", "statusCode": 302, "headers": {"Location": "/home"}},
    ),
    (
        "Network.requestWillBeSent",
        {
            "requestId": "2",
            "wallTime": 1_700_000_003.0,
            "type": "Document",
            "documentURL": f"{SITE}/home",
            "redirectHasExtraInfo": True,
            "redirectResponse": {
                "status": 302,
                "statusText": "Found",
                "headers": {"Location": "/home"},
                "mimeType": "",
            },
            "request": {"url": f"{SITE}/home", "method": "GET", "headers": RENDERER_POST},
        },
    ),
    (
        "Network.requestWillBeSentExtraInfo",
        {"requestId": "2", "headers": _wire(Referer=f"{SITE}/login", Cookie="sid=abc")},
    ),
]


def _nodriver_event(params: dict):
    """A CDP event's params as nodriver's dataclasses expose them."""
    from types import SimpleNamespace

    request = params.get("request")
    redirect = params.get("redirectResponse")
    return SimpleNamespace(
        request_id=params["requestId"],
        headers=params.get("headers"),
        status_code=params.get("statusCode"),
        wall_time=params.get("wallTime"),
        type_=params.get("type"),
        document_url=params.get("documentURL"),
        initiator=None,
        redirect_has_extra_info=params.get("redirectHasExtraInfo", False),
        request=request
        and SimpleNamespace(
            url=request["url"],
            method=request["method"],
            headers=request["headers"],
            has_post_data=request.get("hasPostData", False),
            post_data=None,
            post_data_entries=None,
        ),
        redirect_response=redirect
        and SimpleNamespace(
            status=redirect["status"],
            status_text=redirect["statusText"],
            headers=redirect["headers"],
            mime_type=redirect["mimeType"],
        ),
        response=params.get("response")
        and SimpleNamespace(
            url=params["response"]["url"],
            status=params["response"]["status"],
            status_text=params["response"]["statusText"],
            headers=params["response"]["headers"],
            mime_type=params["response"]["mimeType"],
        ),
    )


def _nodriver_roles() -> tuple[dict, dict]:
    backend = NodriverCaptureBackend(None, record_request_bodies=False, record_console=False)
    handlers = {
        "Network.requestWillBeSent": backend._on_request,
        "Network.requestWillBeSentExtraInfo": backend._on_request_extra_info,
        "Network.responseReceived": backend._on_response,
        "Network.responseReceivedExtraInfo": backend._on_response_extra_info,
    }
    for method, params in LOGIN_EVENTS:
        handlers[method](_nodriver_event(params))
    return backend.get_header_roles(SITE), backend.get_header_role_origins(SITE)


def _selenium_roles() -> tuple[dict, dict]:
    import json
    from types import SimpleNamespace

    log = [
        {"message": json.dumps({"message": {"method": method, "params": params}})}
        for method, params in LOGIN_EVENTS
    ]
    driver = SimpleNamespace(get_log=lambda kind: log)
    backend = SeleniumCaptureBackend(driver, record_request_bodies=False, record_console=False)
    backend.start_capture()
    backend.stop_capture()
    return backend.get_header_roles(SITE), backend.get_header_role_origins(SITE)


def test_both_backends_take_a_logins_roles_from_the_headers_chrome_sent():
    expected_roles = {
        "navigation": BROWSER_NAV_ROLE,
        "form": BROWSER_NAV_ROLE,
    }
    expected_origins = {"navigation": SITE, "form": SITE}
    assert _nodriver_roles() == (expected_roles, expected_origins)
    assert _selenium_roles() == (expected_roles, expected_origins)


def test_roles_read_mid_capture_see_a_buffered_event_its_entry_could_have():
    """Sent headers still in the correlator's buffer when a nodriver login reads
    its roles (the capture has not stopped) are applied as the drain would apply
    them: here headers that name no method, path or host, which the drain's last
    resort gives the one entry still lacking them. The capture itself keeps the
    event buffered."""
    url = f"{SITE}/home"
    backend = NodriverCaptureBackend(None, record_request_bodies=False, record_console=False)
    backend._on_request(
        _nodriver_event(
            {
                "requestId": "7",
                "wallTime": 1_700_000_001.0,
                "type": "Document",
                "documentURL": url,
                "request": {"url": url, "method": "GET", "headers": RENDERER_NAV},
            }
        )
    )
    backend._request_map["7"]["_request_extra_seen"] = False
    sent = {k: v for k, v in _wire().items() if k.lower() != "host"}
    backend._extra_info._pending_request["7"] = [sent]

    roles = backend.get_header_roles(SITE)
    assert roles["navigation"]["Accept-Language"] == "en-US,en;q=0.9"
    assert backend.get_header_role_origins(SITE) == {"navigation": SITE}
    assert "_request_extra_headers" not in backend._request_map["7"]
    assert backend._extra_info._pending_request == {"7": [sent]}


def test_the_drains_last_resort_never_gives_a_request_another_requests_headers():
    """Sent headers whose :path names another request (here only by its
    percent-encoding) are left unattached at drain: a guess that could give one
    request another's headers is worse than none."""
    url = f"{SITE}/search?q=a b"
    wire = {
        ":authority": "myshop.example",
        ":method": "GET",
        ":path": "/search?q=a%20b",
        ":scheme": "https",
        **{k.lower(): v for k, v in _wire().items()},
    }
    backend = NodriverCaptureBackend(None, record_request_bodies=False, record_console=False)
    backend._on_request_extra_info(_nodriver_event({"requestId": "7", "headers": wire}))
    backend._on_request(
        _nodriver_event(
            {
                "requestId": "7",
                "wallTime": 1_700_000_001.0,
                "type": "Document",
                "documentURL": url,
                "request": {"url": url, "method": "GET", "headers": RENDERER_NAV},
            }
        )
    )
    backend._extra_info.log_unmatched(backend._request_map)
    assert "_request_extra_headers" not in backend._request_map["7"]


def test_reading_roles_mid_capture_changes_nothing_and_never_attaches_a_mismatch():
    """A form POST whose own sent headers Chrome dropped, and the redirect GET's
    sent headers arriving before that GET is announced: they name GET /home, so
    they are not the POST's. Reading roles mid-capture keeps the POST's form
    role and leaves the capture (its request map, its HAR, its buffer) as it was,
    and the drain's last resort does not attach them to the POST either."""
    import copy

    backend = NodriverCaptureBackend(None, record_request_bodies=False, record_console=False)
    post = (
        "Network.requestWillBeSent",
        {
            "requestId": "r1",
            "wallTime": 1_700_000_001.0,
            "type": "Document",
            "documentURL": f"{SITE}/login",
            "request": {
                "url": f"{SITE}/login",
                "method": "POST",
                "headers": RENDERER_POST,
                "hasPostData": True,
            },
        },
    )
    get_wire = {
        ":method": "GET",
        ":path": "/home",
        ":authority": "myshop.example",
        ":scheme": "https",
        "sec-fetch-mode": "navigate",
        "sec-fetch-dest": "document",
        "accept": HTML_ACCEPT,
        "user-agent": CHROME_UA,
    }
    backend._on_request(_nodriver_event(post[1]))
    backend._on_request_extra_info(_nodriver_event({"requestId": "r1", "headers": get_wire}))
    before_map = copy.deepcopy(backend._request_map)
    before_har = backend.get_har_entries()
    before_pending = copy.deepcopy(backend._extra_info._pending_request)

    roles = backend.get_header_roles(SITE)
    origins = backend.get_header_role_origins(SITE)

    assert "form" in roles
    assert roles["form"]["User-Agent"] == CHROME_UA
    assert origins["form"] == SITE
    assert backend._request_map == before_map
    assert backend._extra_info._pending_request == before_pending
    after_har = backend.get_har_entries()
    for entry in (*before_har, *after_har):
        entry.pop("startedDateTime", None)
        entry.pop("time", None)
    assert after_har == before_har

    backend._extra_info.log_unmatched(backend._request_map)
    assert "_request_extra_headers" not in backend._request_map["r1"]


# --- HTTP/1.1: the POST's sent headers after the redirect GET is announced ---
#
# The order a Selenium login's performance log delivered on 2026-11-27 (Chrome
# 158, a local http site, values replaced): the POST and the redirect GET are
# both announced before the POST's requestWillBeSentExtraInfo. On HTTP/1.1 the
# sent headers name only the Host, so the body headers are what tell them apart.

_H1 = "http://127.0.0.1:8080"
_POST_RENDERER = {
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": _H1,
    "Referer": f"{_H1}/login",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": CHROME_UA,
}


def _h1_sent(**extra: str) -> dict[str, str]:
    return {
        "Accept": HTML_ACCEPT,
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "max-age=0",
        "Connection": "keep-alive",
        "Host": "127.0.0.1:8080",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": CHROME_UA,
        **extra,
    }


_POST_SENT = _h1_sent(
    **{
        "Content-Length": "41",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": _H1,
        "Referer": f"{_H1}/login",
        "X-Marker": "post",
    }
)
_GET_SENT = _h1_sent(**{"Cookie": "sid=placeholder", "Referer": f"{_H1}/login", "X-Marker": "get"})
_LATE_POST_EVENTS: list[tuple[str, dict]] = [
    (
        "Network.requestWillBeSent",
        {
            "requestId": "R",
            "wallTime": 1_700_000_001.0,
            "type": "Document",
            "documentURL": f"{_H1}/login",
            "request": {
                "url": f"{_H1}/login",
                "method": "POST",
                "headers": _POST_RENDERER,
                "hasPostData": True,
            },
        },
    ),
    (
        "Network.requestWillBeSent",
        {
            "requestId": "R",
            "wallTime": 1_700_000_002.0,
            "type": "Document",
            "documentURL": f"{_H1}/home",
            "redirectHasExtraInfo": True,
            "redirectResponse": {
                "status": 302,
                "statusText": "Found",
                "headers": {"Location": "/home"},
                "mimeType": "text/html",
            },
            "request": {"url": f"{_H1}/home", "method": "GET", "headers": _POST_RENDERER},
        },
    ),
    ("Network.requestWillBeSentExtraInfo", {"requestId": "R", "headers": _POST_SENT}),
    (
        "Network.responseReceivedExtraInfo",
        {"requestId": "R", "statusCode": 302, "headers": {"Location": "/home"}},
    ),
    ("Network.requestWillBeSentExtraInfo", {"requestId": "R", "headers": _GET_SENT}),
    (
        "Network.responseReceivedExtraInfo",
        {"requestId": "R", "statusCode": 200, "headers": {"Content-Type": "text/html"}},
    ),
    (
        "Network.responseReceived",
        {
            "requestId": "R",
            "response": {
                "url": f"{_H1}/home",
                "status": 200,
                "statusText": "OK",
                "headers": {"Content-Type": "text/html"},
                "mimeType": "text/html",
            },
        },
    ),
]


def _late_post_backend(kind: str, events: list[tuple[str, dict]] | None = None):
    import json
    from types import SimpleNamespace

    if kind == "selenium":
        log = [
            {"message": json.dumps({"message": {"method": method, "params": params}})}
            for method, params in events or _LATE_POST_EVENTS
        ]
        backend = SeleniumCaptureBackend(
            SimpleNamespace(get_log=lambda _kind: log),
            record_request_bodies=False,
            record_console=False,
        )
        backend.start_capture()
        backend.stop_capture()
        return backend
    backend = NodriverCaptureBackend(None, record_request_bodies=False, record_console=False)
    handlers = {
        "Network.requestWillBeSent": backend._on_request,
        "Network.requestWillBeSentExtraInfo": backend._on_request_extra_info,
        "Network.responseReceived": backend._on_response,
        "Network.responseReceivedExtraInfo": backend._on_response_extra_info,
    }
    for method, params in events or _LATE_POST_EVENTS:
        handlers[method](_nodriver_event(params))
    return backend


@pytest.mark.parametrize("kind", ["selenium", "nodriver"])
def test_a_posts_late_sent_headers_stay_on_the_post(kind: str):
    backend = _late_post_backend(kind)

    roles = backend.get_header_roles(_H1)
    assert roles["form"]["X-Marker"] == "post"
    assert roles["navigation"]["X-Marker"] == "get"

    by_method = {e["request"]["method"]: e["request"] for e in backend.get_har_entries()}
    post_headers = {h["name"].lower(): h["value"] for h in by_method["POST"]["headers"]}
    get_headers = {h["name"].lower(): h["value"] for h in by_method["GET"]["headers"]}
    assert post_headers["content-length"] == "41"
    assert post_headers["x-marker"] == "post"
    assert get_headers["x-marker"] == "get"
    assert "content-length" not in get_headers
    assert get_headers["cookie"] == "sid=placeholder"


def test_sent_headers_with_a_body_never_match_a_get_or_head():
    from graftpunk.observe.capture import _request_headers_match

    for method in ("GET", "HEAD"):
        entry = {"url": f"{_H1}/home", "method": method}
        assert _request_headers_match(_POST_SENT, entry) is False
        # A Content-Length of 0 is a POST's too: Chrome sends none on a GET or
        # HEAD, so its presence, not its value, is the body evidence.
        assert (
            _request_headers_match({"Host": "127.0.0.1:8080", "Content-Length": "0"}, entry)
            is False
        )
        assert _request_headers_match({"Host": "127.0.0.1:8080"}, entry) is True
    assert _request_headers_match(_POST_SENT, {"url": f"{_H1}/login", "method": "POST"})


@pytest.mark.parametrize("kind", ["selenium", "nodriver"])
def test_a_post_with_an_empty_body_keeps_its_own_late_sent_headers(kind: str):
    """A POST whose body is empty still sends Content-Length: 0, which the
    redirect GET does not, so the two stay apart in the late order too."""
    empty = {**_POST_SENT, "Content-Length": "0"}
    events = [
        *_LATE_POST_EVENTS[:2],
        ("Network.requestWillBeSentExtraInfo", {"requestId": "R", "headers": empty}),
        *_LATE_POST_EVENTS[3:],
    ]
    backend = _late_post_backend(kind, events)
    roles = backend.get_header_roles(_H1)
    assert roles["form"]["X-Marker"] == "post"
    assert roles["navigation"]["X-Marker"] == "get"


# --- CORS preflight: Chrome's own OPTIONS before a fetch it guards ---


@pytest.mark.parametrize(
    ("method", "resource_type"),
    [("OPTIONS", "Preflight"), ("OPTIONS", "Other"), ("GET", "Preflight")],
)
def test_a_preflight_is_never_a_role(method: str, resource_type: str):
    preflight = {
        "Accept": "*/*",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Site": "same-site",
        "User-Agent": CHROME_UA,
        "X-Marker": "preflight",
    }
    fetch = {
        "Accept": "application/json",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "User-Agent": CHROME_UA,
        "X-Marker": "fetch",
    }
    request_map = {
        "1": {
            **_entry("https://api.myshop.example/v1/cart", method, {}, preflight, 1.0),
            "resource_type": resource_type,
        },
        "2": {
            **_entry("https://api.myshop.example/v1/cart", "POST", {}, fetch, 2.0),
            "resource_type": "Fetch",
        },
    }
    assert extract_header_roles(request_map, SITE)["xhr"]["X-Marker"] == "fetch"
    only_preflight = {"1": request_map["1"]}
    assert extract_header_roles(only_preflight, SITE) == {}


@pytest.mark.parametrize(
    "name", ["Access-Control-Request-Method", "access-control-request-headers"]
)
def test_preflight_request_headers_stay_out_of_roles(name: str):
    assert name.lower() in EXCLUDED_HEADERS
    request_map = {
        "1": _entry("https://myshop.example/a", "GET", RENDERER_NAV, _wire(**{name: "POST"}), 1.0)
    }
    assert extract_header_roles(request_map, SITE)["navigation"] == BROWSER_NAV_ROLE


# --- a GET that names a Content-Type and sends no body ---
#
# Single-page apps set Content-Type: application/json on every fetch, GETs
# included. That is not a body: only a Content-Length above 0 is, and on HTTP/2
# the :method pseudo-header decides.

_XHR_SENT = {
    "Host": "127.0.0.1:8080",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Site": "same-origin",
    "User-Agent": CHROME_UA,
    "X-Marker": "api",
}
_XHR_SENT_H2 = {
    **{k.lower(): v for k, v in _XHR_SENT.items() if k != "Host"},
    ":method": "GET",
    ":path": "/api",
    ":authority": "127.0.0.1:8080",
    ":scheme": "http",
}


@pytest.mark.parametrize("sent", [_XHR_SENT, _XHR_SENT_H2], ids=["http1", "http2"])
def test_a_get_naming_a_content_type_without_a_body_keeps_its_sent_headers(sent: dict):
    import json
    from types import SimpleNamespace

    from graftpunk.observe.capture import _request_headers_match

    assert _request_headers_match(sent, {"url": f"{_H1}/api", "method": "GET"}) is not False
    events = [
        (
            "Network.requestWillBeSent",
            {
                "requestId": "4",
                "wallTime": 1_700_000_001.0,
                "type": "XHR",
                "documentURL": f"{_H1}/app",
                "request": {"url": f"{_H1}/api", "method": "GET", "headers": {}},
            },
        ),
        ("Network.requestWillBeSentExtraInfo", {"requestId": "4", "headers": sent}),
        ("Network.responseReceivedExtraInfo", {"requestId": "4", "headers": {}, "statusCode": 200}),
        (
            "Network.responseReceived",
            {
                "requestId": "4",
                "response": {
                    "url": f"{_H1}/api",
                    "status": 200,
                    "statusText": "OK",
                    "headers": {},
                    "mimeType": "application/json",
                },
            },
        ),
    ]
    log = [{"message": json.dumps({"message": {"method": m, "params": p}})} for m, p in events]
    backend = SeleniumCaptureBackend(
        SimpleNamespace(get_log=lambda _kind: log),
        record_request_bodies=False,
        record_console=False,
    )
    backend.start_capture()
    backend.stop_capture()
    roles = backend.get_header_roles(_H1)
    assert {k.lower(): v for k, v in roles["xhr"].items()}["x-marker"] == "api"
    (entry,) = backend.get_har_entries()
    assert "x-marker" in {h["name"].lower() for h in entry["request"]["headers"]}


def test_on_http2_the_pseudo_headers_decide_not_the_body_evidence():
    from graftpunk.observe.capture import _request_headers_match

    sent = {**_XHR_SENT_H2, "content-length": "12", ":method": "GET"}
    assert _request_headers_match(sent, {"url": f"{_H1}/api", "method": "GET"}) is True


# --- a redirect GET's sent headers before the GET is announced (HTTP/1.1) ---


def test_a_continuations_early_sent_headers_wait_for_it_rather_than_land_on_the_post():
    """The redirect GET's sent headers arrive while only the POST is announced,
    then the GET is announced, then the POST's own: bodiless headers on HTTP/1.1
    are not a body-carrying POST's, so they wait for the GET."""
    events = [
        _LATE_POST_EVENTS[0],
        ("Network.requestWillBeSentExtraInfo", {"requestId": "R", "headers": _GET_SENT}),
        _LATE_POST_EVENTS[1],
        ("Network.requestWillBeSentExtraInfo", {"requestId": "R", "headers": _POST_SENT}),
        *_LATE_POST_EVENTS[3:4],
        *_LATE_POST_EVENTS[5:],
    ]
    for kind in ("selenium", "nodriver"):
        backend = _late_post_backend(kind, events)
        roles = backend.get_header_roles(_H1)
        assert roles["form"]["X-Marker"] == "post", kind
        assert roles["navigation"]["X-Marker"] == "get", kind
        by_method = {e["request"]["method"]: e["request"] for e in backend.get_har_entries()}
        post = {h["name"].lower(): h["value"] for h in by_method["POST"]["headers"]}
        get = {h["name"].lower(): h["value"] for h in by_method["GET"]["headers"]}
        assert post["x-marker"] == "post" and get["x-marker"] == "get", kind


def test_the_measured_selenium_late_post_log_keeps_post_and_get_apart():
    """The performance log of a live Selenium login whose POST's sent headers came
    after the redirect GET was announced (tests/fixtures/README.md says how it
    was trimmed): the POST keeps its body headers, the GET gets its own, and the
    login has both roles."""
    import json
    from pathlib import Path
    from types import SimpleNamespace

    fixture = Path(__file__).parents[1] / "fixtures" / "selenium_late_post_perflog.json"
    events = json.loads(fixture.read_text())["events"]
    log = [{"message": json.dumps({"message": e})} for e in events]
    backend = SeleniumCaptureBackend(
        SimpleNamespace(get_log=lambda _kind: log),
        record_request_bodies=False,
        record_console=False,
    )
    backend.start_capture()
    backend.stop_capture()

    roles = backend.get_header_roles(_H1)
    assert sorted(roles) == ["form", "navigation"]
    entries = {
        (e["request"]["method"], e["request"]["url"]): {
            h["name"].lower(): h["value"] for h in e["request"]["headers"]
        }
        for e in backend.get_har_entries()
    }
    post = entries[("POST", f"{_H1}/login")]
    home = entries[("GET", f"{_H1}/home")]
    assert post["content-type"] == "application/x-www-form-urlencoded"
    assert int(post["content-length"]) > 0
    assert "content-length" not in home
    assert "cookie" in home
