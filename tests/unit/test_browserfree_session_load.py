import types

import requests

from graftpunk import cache
from graftpunk.graftpunk_session import GraftpunkSession
from graftpunk.tokens import _CACHE_ATTR, _CSRF_TOKENS_ATTR


def _fake_source():
    """A minimal stand-in for an unpickled BrowserSession (browser-free)."""
    src = types.SimpleNamespace()
    jar = requests.cookies.RequestsCookieJar()
    jar.set("User", "dummyuser", domain="example.com")
    src.cookies = jar
    src.headers = {"User-Agent": "Chrome/999", "X-CSRF-TOKEN": "ephemeral"}
    src._gp_header_roles = {"api": {"X-Api": "1"}}
    setattr(src, _CACHE_ATTR, {"tok": "cached"})
    setattr(src, _CSRF_TOKENS_ATTR, {"form": "csrf123"})
    return src


def test_api_session_from_source_copies_state():
    api = cache._api_session_from_session(_fake_source())
    assert isinstance(api, GraftpunkSession)
    assert api.cookies.get("User", domain="example.com") == "dummyuser"
    assert api.headers["User-Agent"] == "Chrome/999"
    # ephemeral header is dropped
    assert "X-CSRF-TOKEN" not in api.headers
    assert api._gp_header_roles == {"api": {"X-Api": "1"}}
    assert getattr(api, _CACHE_ATTR) == {"tok": "cached"}
    assert getattr(api, _CSRF_TOKENS_ATTR) == {"form": "csrf123"}
