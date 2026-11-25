import io
import pathlib
import subprocess
import sys
import types

import dill
import pytest
import requests

from graftpunk import cache, session_legacy
from graftpunk.encryption import encrypt_data
from graftpunk.exceptions import EncryptionError, SessionExpiredError
from graftpunk.graftpunk_session import GraftpunkSession
from graftpunk.tokens import _CACHE_ATTR, _CSRF_TOKENS_ATTR

_FIX = pathlib.Path(__file__).parent.parent / "fixtures" / "session_compat" / "legacy"


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


# --- A class pickled by reference whose module is gone ----------------------
#
# dill pickles a class whose module cannot be resolved BY VALUE (it embeds the
# class definition in the pickle), so a class must be pickled while its module
# is importable and read after the module is gone for the read to stub it.
# The placeholder's own state handling is tested in test_session_legacy.py.


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


def test_state_from_pickle_stubs_a_class_pickled_by_reference():
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
    recovered = session_legacy.state_from_pickle(blob)
    assert recovered.unresolved == ("graftpunk_test_throwaway_browser_module.BrowserSessionLike",)
    assert recovered.state["cookies"].get("User", domain="example.com") == "dummyuser"
    assert recovered.state["headers"]["User-Agent"] == "Chrome/999"


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
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_cli_import_stays_browser_free():
    """The CLI entry point must import without the browser stack.

    Regression guard for a shipped-breakage class the `import graftpunk` test
    above cannot see: `[project.scripts]` declares `gp` unconditionally, and
    cli/main.py -> cli/plugin_commands.py -> cli/login_commands.py ->
    plugins/login_engine.py used to do a MODULE-LEVEL
    `from graftpunk import BrowserSession`. The lazy __getattr__ doesn't help
    there (it only moves where the eager chain fires), so on graftpunk-lite
    EVERY `gp` invocation (even `gp --version`) died with
    "ModuleNotFoundError: No module named 'httpie'".
    """
    code = (
        "import graftpunk.cli.main, sys; "
        "assert 'graftpunk.session' not in sys.modules, 'session eagerly imported via CLI'; "
        "assert 'httpie' not in sys.modules, 'httpie eagerly imported via CLI'; "
        "assert 'selenium' not in sys.modules, 'selenium eagerly imported via CLI'; "
        "print('ok')"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_lazy_browser_symbols_still_resolve():
    import graftpunk

    # Accessing the lazy attributes still works on a full install.
    assert graftpunk.BrowserSession is not None
    assert callable(graftpunk.create_stealth_driver)


def test_dir_lists_lazy_browser_symbols():
    # PEP 562 __getattr__ doesn't feed dir(); the __dir__ companion restores
    # tab-completion/introspection for the lazily-loaded symbols.
    import graftpunk

    listing = dir(graftpunk)
    assert "BrowserSession" in listing
    assert "create_stealth_driver" in listing


def test_missing_browser_stack_error_names_the_extra(monkeypatch):
    """A missing browser stack must say what to install.

    The raw failure is `No module named 'httpie'` — a package the caller never
    asked for, with no hint that the browser stack is the fix.
    """
    import graftpunk

    monkeypatch.setitem(sys.modules, "graftpunk.session", None)
    with pytest.raises(ImportError, match=r"graftpunk\[browser\]"):
        getattr(graftpunk, "BrowserSession")  # noqa: B009 - must go through __getattr__


def test_unknown_attribute_still_raises_attribute_error():
    # The lazy __getattr__ must not swallow genuine typos into an ImportError.
    import graftpunk

    with pytest.raises(AttributeError):
        getattr(graftpunk, "definitely_not_a_real_symbol")  # noqa: B009 - via __getattr__


# --- A4: committed fixture guard --------------------------------------------


def test_a4_fixture_decodes_browser_free(monkeypatch):
    """Whack-once guard: a real committed BrowserSession pickle (pickled BY
    REFERENCE to graftpunk.session.BrowserSession, see
    tests/fixtures/session_compat/README.md) must keep decoding through the
    browser-free path (cookies/headers/roles/tokens) as graftpunk/deps evolve.

    The CI/dev env used to run this suite has the full browser stack
    installed, so graftpunk.session IS importable here. If we just loaded the
    fixture as-is, dill would reconstruct the REAL BrowserSession via its
    normal __setstate__ and the test would never show that
    session_legacy's placeholder takes its place -- it would pass
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

    # Verify the stub path is genuinely taken under forced browser-stack absence.
    from graftpunk.encryption import decrypt_data

    decrypted = decrypt_data(enc, key=key)
    source = session_legacy._Unpickler(io.BytesIO(decrypted)).load()
    assert isinstance(source, session_legacy._Capture), (
        "A4 must exercise the stub fallback, not the real BrowserSession"
    )

    api = cache.load_session_for_api_from_bytes(enc, key=key)
    assert api.cookies.get("User", domain="example.com") == "dummyuser"
    assert api.cookies.get("Password", domain="example.com") == "dummypass"
    assert "Mozilla/5.0 (dummy)" in api.headers["User-Agent"]
    assert api._gp_header_roles == {"api": {"X-Api-Key": "dummy"}}
    # The docstring claims this guards TOKENS too, so assert them — otherwise a
    # regression in token carry-over would slip through a test that says it looks.
    assert getattr(api, _CACHE_ATTR) == {"cached": "dummy"}


# --- load_session_for_api_from_bytes: documented error paths -----------------
# The Worker's error handling depends on this contract, and none of it was
# pinned before — which is how a structural failure escaping as a raw
# AttributeError went unnoticed.


def test_from_bytes_malformed_key_raises_encryption_error():
    """A malformed key secret (wrong length / stray newline) is the likeliest
    Worker misconfiguration. It must say "bad key", not "session expired" —
    otherwise the operator re-runs a login instead of fixing the secret."""
    with pytest.raises(EncryptionError, match="Invalid encryption key"):
        cache.load_session_for_api_from_bytes(b"whatever", key=b"not-a-fernet-key")


def test_from_bytes_wrong_key_raises_encryption_error():
    from cryptography.fernet import Fernet

    good, other = Fernet.generate_key(), Fernet.generate_key()
    blob = Fernet(good).encrypt(dill.dumps(_fake_source()))
    # Decryptable-looking bytes under the WRONG key: an operator/config problem.
    with pytest.raises(EncryptionError, match="Decryption failed"):
        cache.load_session_for_api_from_bytes(blob, key=other)


def test_from_bytes_undeserializable_payload_raises_session_expired():
    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    blob = Fernet(key).encrypt(b"this is not a pickle at all")
    with pytest.raises(SessionExpiredError, match="deserialize"):
        cache.load_session_for_api_from_bytes(blob, key=key)


def test_from_bytes_object_without_session_shape_raises_session_expired():
    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    blob = Fernet(key).encrypt(dill.dumps({"not": "a session"}))
    with pytest.raises(SessionExpiredError, match="invalid structure"):
        cache.load_session_for_api_from_bytes(blob, key=key)


@pytest.mark.parametrize("cookies", ["not-a-jar", 5, None])
def test_from_bytes_malformed_cookies_raises_session_expired(cookies):
    """Structurally present but unusable contents must still honour the
    documented SessionExpiredError contract rather than leaking an
    AttributeError from deep inside session construction."""
    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    broken = types.SimpleNamespace(cookies=cookies, headers="not-a-mapping")
    blob = Fernet(key).encrypt(dill.dumps(broken))
    with pytest.raises(SessionExpiredError):
        cache.load_session_for_api_from_bytes(blob, key=key)


def test_a_session_saved_before_1_41_0_replays_no_header_the_client_sets_itself():
    """A BrowserSession pickled the way 1.40.1 saves it (by reference, through
    __getstate__'s nodriver branch, as the legacy fixture in
    tests/fixtures/session_compat/legacy/ was built), holding the role an older
    capture kept and the Accept-Encoding older identity extraction put on its
    headers (#370). The restored session sends the client's own
    Accept-Encoding, no Priority, and keeps the rest."""
    from cryptography.fernet import Fernet

    from graftpunk.session import BrowserSession

    chrome_ae = "gzip, deflate, br, zstd"
    sess = BrowserSession.__new__(BrowserSession)
    sess._backend_type = "nodriver"
    sess._use_stealth = False
    sess.current_url = "https://myshop.example"
    sess._session_name = "fixture"
    sess.cookies = requests.cookies.RequestsCookieJar()
    sess.headers = {"User-Agent": "Mozilla/5.0 (dummy)", "Accept-Encoding": chrome_ae}
    sess._gp_header_roles = {
        "xhr": {
            "User-Agent": "Mozilla/5.0 (dummy)",
            "accept-encoding": chrome_ae,
            "priority": "u=1, i",
            "connection": "keep-alive",
            "sec-fetch-mode": "cors",
            "Accept": "application/json",
            "X-Api-Key": "dummy",
        }
    }
    key = Fernet.generate_key()
    blob = Fernet(key).encrypt(dill.dumps(sess))

    api = cache.load_session_for_api_from_bytes(blob, key=key)

    assert api._gp_header_roles == {
        "xhr": {
            "User-Agent": "Mozilla/5.0 (dummy)",
            "sec-fetch-mode": "cors",
            "Accept": "application/json",
            "X-Api-Key": "dummy",
        }
    }
    prepared = api.prepare_request(
        requests.Request(
            "GET", "https://myshop.example/api/orders", headers={"Accept": "application/json"}
        )
    )
    defaults = requests.utils.default_headers()
    assert prepared.headers["Accept-Encoding"] == defaults["Accept-Encoding"]
    assert "priority" not in prepared.headers
    assert prepared.headers["X-Api-Key"] == "dummy"
    assert prepared.headers["User-Agent"] == "Mozilla/5.0 (dummy)"
