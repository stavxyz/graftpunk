import io
import types

import dill
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


class _FakeBrowserSessionState:
    """Picklable object whose module/qualname we rewrite to look like the
    real graftpunk.session.BrowserSession, to prove find_class stubbing."""


def test_deserialize_browserfree_stubs_unimportable_class():
    # Build state like BrowserSession.__getstate__ produces (plain data).
    jar = requests.cookies.RequestsCookieJar()
    jar.set("User", "dummyuser", domain="example.com")
    obj = _FakeBrowserSessionState()
    obj.cookies = jar
    obj.headers = {"User-Agent": "Chrome/999"}
    obj._gp_header_roles = {}
    # Rewrite identity so the pickle stream names a module that does NOT exist.
    _FakeBrowserSessionState.__module__ = "graftpunk._nonexistent_browser"
    _FakeBrowserSessionState.__qualname__ = "BrowserSession"

    blob = dill.dumps(obj)

    # _deserialize_browserfree should successfully recover the object state
    # even when the original class cannot be imported.
    recovered = cache._deserialize_browserfree(blob)
    assert recovered.cookies.get("User", domain="example.com") == "dummyuser"
    assert recovered.headers["User-Agent"] == "Chrome/999"
