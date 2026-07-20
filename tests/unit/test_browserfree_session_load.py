import io
import pathlib
import subprocess
import sys
import types

import dill
import pytest
import requests

from graftpunk import cache
from graftpunk.encryption import encrypt_data
from graftpunk.graftpunk_session import GraftpunkSession
from graftpunk.tokens import _CACHE_ATTR, _CSRF_TOKENS_ATTR

_FIX = pathlib.Path(__file__).parent.parent / "fixtures"


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


# --- Direct find_class mechanism tests -------------------------------------
#
# NOTE: an earlier version of this test built a "genuinely unimportable
# class" by rewriting a local class's __module__/__qualname__ and
# round-tripping it through dill. That does not exercise the stub path at
# all: dill pickles a class whose module cannot be resolved BY VALUE (it
# embeds the full class definition in the pickle stream), so on load the
# object is reconstructed directly and find_class's except-branch is never
# hit. The test passed without ever touching _Stub — false confidence.
# The tests below exercise find_class and _Stub directly and deterministically,
# plus one true by-reference round-trip (test 4) that does hit the fallback.
# The genuine production case — a real graftpunk.session.BrowserSession
# pickled by reference, then loaded with the browser stack absent — is
# covered end-to-end by Task 5's A4 fixture.


def test_find_class_stubs_genuinely_unimportable_module():
    unpickler = cache._BrowserFreeUnpickler(io.BytesIO(b""))
    stub_cls = unpickler.find_class("graftpunk._nonexistent_pyodide_module", "BrowserSession")
    assert isinstance(stub_cls, type)
    assert issubclass(stub_cls, cache._Stub)


def test_find_class_passes_through_importable_class():
    unpickler = cache._BrowserFreeUnpickler(io.BytesIO(b""))
    resolved = unpickler.find_class("requests.cookies", "RequestsCookieJar")
    assert resolved is requests.cookies.RequestsCookieJar


def test_stub_setstate_restores_dict_state():
    stub_cls = type("StubDictState", (cache._Stub,), {})
    stub = stub_cls()
    jar = requests.cookies.RequestsCookieJar()
    jar.set("User", "dummyuser", domain="example.com")
    stub.__setstate__({"cookies": jar, "headers": {"User-Agent": "Chrome/999"}})
    assert stub.__dict__["cookies"] is jar
    assert stub.__dict__["headers"] == {"User-Agent": "Chrome/999"}


def test_stub_setstate_restores_dict_and_slotstate_tuple():
    stub_cls = type("StubTupleState", (cache._Stub,), {})
    stub = stub_cls()
    stub.__setstate__(({"cookies": "c"}, {"headers": "h"}))
    assert stub.cookies == "c"
    assert stub.headers == "h"


def test_stub_setstate_rejects_unknown_state_shape():
    stub_cls = type("StubBadState", (cache._Stub,), {})
    stub = stub_cls()
    with pytest.raises(TypeError):
        stub.__setstate__("not a dict or tuple")


# --- By-reference round-trip that actually hits the stub fallback ----------


class _ThrowawayBrowserSessionLike:
    """A stand-in for BrowserSession, defined so it can be registered under
    a throwaway module name and pickled BY REFERENCE (module + qualname),
    not by value. Removing the module from sys.modules before deserializing
    makes it genuinely unimportable, forcing find_class's except-branch.
    """

    def __getstate__(self):
        return self.__dict__

    def __setstate__(self, state):
        self.__dict__.update(state)


def test_deserialize_browserfree_stub_roundtrip_by_reference():
    module_name = "graftpunk_test_throwaway_browser_module"
    module = types.ModuleType(module_name)
    _ThrowawayBrowserSessionLike.__module__ = module_name
    _ThrowawayBrowserSessionLike.__qualname__ = "BrowserSessionLike"
    module.BrowserSessionLike = _ThrowawayBrowserSessionLike
    sys.modules[module_name] = module
    try:
        jar = requests.cookies.RequestsCookieJar()
        jar.set("User", "dummyuser", domain="example.com")
        obj = _ThrowawayBrowserSessionLike()
        obj.cookies = jar
        obj.headers = {"User-Agent": "Chrome/999"}

        # dill.dumps here pickles obj's class BY REFERENCE (module.qualname),
        # not by value, because the module is present in sys.modules and the
        # class resolves via getattr(module, qualname) to the same object.
        blob = cache.pickle.dumps(obj)
    finally:
        del sys.modules[module_name]

    # Module is now genuinely unimportable -> find_class must fall back to a stub.
    recovered = cache._deserialize_browserfree(blob)
    assert isinstance(recovered, cache._Stub)
    assert recovered.cookies.get("User", domain="example.com") == "dummyuser"
    assert recovered.headers["User-Agent"] == "Chrome/999"


# --- load_session_for_api_from_bytes ---------------------------------------


class _FakeBrowserSessionState:
    """A minimal stand-in for a pickled BrowserSession's plain state, used to
    exercise the encrypt -> load_session_for_api_from_bytes round trip below."""


def test_load_session_for_api_from_bytes_roundtrip():
    # Build a plain-state object that looks like a pickled BrowserSession.
    jar = requests.cookies.RequestsCookieJar()
    jar.set("User", "dummyuser", domain="example.com")
    obj = _FakeBrowserSessionState()
    obj.cookies = jar
    obj.headers = {"User-Agent": "Chrome/999"}
    obj._gp_header_roles = {}
    _FakeBrowserSessionState.__module__ = "graftpunk._nonexistent_browser"
    _FakeBrowserSessionState.__qualname__ = "BrowserSession"

    encrypted = encrypt_data(dill.dumps(obj))
    api = cache.load_session_for_api_from_bytes(encrypted)
    assert isinstance(api, requests.Session)
    assert api.cookies.get("User", domain="example.com") == "dummyuser"
    assert api.headers["User-Agent"] == "Chrome/999"


def test_load_session_for_api_from_bytes_with_explicit_key():
    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    jar = requests.cookies.RequestsCookieJar()
    jar.set("User", "dummyuser", domain="example.com")
    obj = _FakeBrowserSessionState()
    obj.cookies = jar
    obj.headers = {"User-Agent": "Chrome/999"}
    obj._gp_header_roles = {}
    _FakeBrowserSessionState.__module__ = "graftpunk._nonexistent_browser"
    _FakeBrowserSessionState.__qualname__ = "BrowserSession"

    encrypted = Fernet(key).encrypt(dill.dumps(obj))
    api = cache.load_session_for_api_from_bytes(encrypted, key=key)
    assert isinstance(api, requests.Session)
    assert api.cookies.get("User", domain="example.com") == "dummyuser"
    assert api.headers["User-Agent"] == "Chrome/999"


def test_import_graftpunk_does_not_eagerly_import_browser_stack():
    # Fresh interpreter: importing graftpunk must not pull graftpunk.session.
    code = (
        "import graftpunk, sys; "
        "assert 'graftpunk.session' not in sys.modules, 'session eagerly imported'; "
        "assert 'graftpunk.stealth' not in sys.modules, 'stealth eagerly imported'; "
        "print('ok')"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_lazy_browser_symbols_still_resolve():
    import graftpunk

    # Accessing the lazy attributes still works on a full install.
    assert graftpunk.BrowserSession is not None
    assert callable(graftpunk.create_stealth_driver)


# --- A4: committed fixture guard --------------------------------------------


def test_a4_fixture_decodes_browser_free(monkeypatch):
    """Whack-once guard: a real committed BrowserSession pickle (pickled BY
    REFERENCE to graftpunk.session.BrowserSession, see
    scripts/gen_browserfree_fixture.py) must keep decoding through the
    browser-free path (cookies/headers/roles/tokens) as graftpunk/deps evolve.

    The CI/dev env used to run this suite has the full browser stack
    installed, so graftpunk.session IS importable here. If we just loaded the
    fixture as-is, dill would reconstruct the REAL BrowserSession via its
    normal __setstate__ and the test would never touch cache._Stub or
    cache._BrowserFreeUnpickler's except-branch at all -- it would pass
    without exercising the browser-free path it's meant to guard. So we force
    the browser stack (and its transitive deps) out of sys.modules for the
    duration of this test, simulating a Pyodide/lite environment where those
    imports genuinely fail, which is what makes find_class fall back to a
    stub and is the only way this guard can catch a real regression.
    """
    enc = (_FIX / "browserfree_session.enc").read_bytes()
    key = (_FIX / "browserfree_session.key").read_bytes()

    for mod in (
        "graftpunk.session",
        "graftpunk.stealth",
        "requestium",
        "selenium",
        "webdriver_manager",
        "httpie",
        "httpie.cookies",
    ):
        monkeypatch.setitem(sys.modules, mod, None)  # None -> import raises ImportError

    api = cache.load_session_for_api_from_bytes(enc, key=key)
    assert api.cookies.get("User", domain="example.com") == "dummyuser"
    assert api.cookies.get("Password", domain="example.com") == "dummypass"
    assert "Mozilla/5.0 (dummy)" in api.headers["User-Agent"]
    assert api._gp_header_roles == {"api": {"X-Api-Key": "dummy"}}
