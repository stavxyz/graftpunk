---
type: plan
feature: hosted-surety-enrichment
component: A
tracks_issue: stavxyz/backsight#48
validated:
  sha: c4c84407af6fc24ad7ad18530590dac9bdf13b6b
  date: 2026-07-20T05:42:27Z
  reviewers: [fact-check, solid-hygiene]
  findings:
    critical: 0
    important: 2
    medium: 3
    low: 2
    nitpick: 1
  net_negative_remaining: 0
---

# graftpunk Pyodide-Safe Session Load (Component A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `import graftpunk` and a cookies-bearing session load work in a no-browser (Pyodide/Workers) environment, without changing the at-rest format or the `BrowserSession` class hierarchy.

**Architecture:** `load_session_for_api` already extracts cookies/headers/roles/tokens from the unpickled `BrowserSession` into a browser-free `GraftpunkSession`. Two changes unblock Pyodide: (1) lazy the only two eager browser imports in `__init__.py`; (2) add a browser-free deserialize (a `find_class`-stubbing unpickler) plus a bytes-in entry `load_session_for_api_from_bytes`, so a caller holding the encrypted bytes (a Worker via its R2 binding) can build a session with no browser stack, no storage backend, and no filesystem. A committed fixture test is the load-bearing guard.

**Tech Stack:** Python (graftpunk uses `dill as pickle`, `cryptography` Fernet via `graftpunk.encryption`, `requests`), pytest.

## Global Constraints

- **Repo:** all work is in `graftpunk` (`~/src/graftpunk`). Do it in a **dedicated worktree/branch**, NOT the primary checkout (another agent holds `refactor/typer-native-plugin-commands`). Verify `git rev-parse --show-toplevel` ends in the worktree name before any git command.
- **Pickle lib:** `cache.py` aliases `import dill as pickle`. All (un)pickling in `cache.py` goes through that `pickle` name (dill), never stdlib `pickle` directly.
- **No behavior change on the desktop path:** `load_session` and the existing `load_session_for_api` must behave identically for full installs. New code is additive.
- **No browser deps required for the new path:** the browser-free deserialize and `load_session_for_api_from_bytes` must not import `graftpunk.session`, `requestium`, `selenium`, `httpie`, or `webdriver_manager`.
- **No Claude attribution** in any commit (`Co-Authored-By` / `Generated with` forbidden).
- **Spec:** `docs/superpowers/specs/2026-07-19-hosted-surety-enrichment-design.md` (Component A).

---

## File Structure

- **Modify** `src/graftpunk/cache.py`:
  - Add `_Stub` + `_BrowserFreeUnpickler` (a `pickle.Unpickler` subclass; `pickle` = dill) and `_deserialize_browserfree(decrypted: bytes) -> object`.
  - Extract the cookie/header/token copy body of `load_session_for_api` (currently lines ~432-486) into `_api_session_from_session(source) -> "GraftpunkSession"`; have `load_session_for_api` call it (pure refactor, DRY).
  - Add `load_session_for_api_from_bytes(encrypted: bytes) -> requests.Session`.
- **Modify** `src/graftpunk/__init__.py`: remove eager `session`/`stealth` imports (lines 49-50); add module `__getattr__` that lazy-loads `BrowserSession` and `create_stealth_driver`.
- **Create** `tests/unit/test_browserfree_session_load.py`: the A4 guard + refactor-equivalence + lazy-import tests.
- **Create** `tests/fixtures/browserfree_session.enc` + `tests/fixtures/browserfree_session.key`: a committed Fernet-encrypted dill pickle of a real `BrowserSession` carrying **dummy** cookies/headers/roles/tokens, plus its test key (generated once via the script in Task 5).

---

### Task 1: Extract the API-session copy body into a shared helper (`_api_session_from_session`)

Pure refactor so `load_session_for_api` and the new bytes entry share one extraction path (DRY per spec A3). No behavior change.

**Files:**
- Modify: `src/graftpunk/cache.py` (the body of `load_session_for_api`, ~432-486)
- Test: `tests/unit/test_browserfree_session_load.py`

**Interfaces:**
- Consumes: `GraftpunkSession` (from `graftpunk.graftpunk_session`), `_CACHE_ATTR`/`_CSRF_TOKENS_ATTR` (from `graftpunk.tokens`), `_EPHEMERAL_HEADERS` and the `SessionLike` Protocol (both module-level in `cache.py`, lines 36-58), `requests`.
- Produces: `_api_session_from_session(source: "SessionLike") -> "GraftpunkSession"` — builds a `GraftpunkSession` from a session-like object (cookies + headers), copying `_gp_header_roles` / token attrs when present. Typed against the module's own `SessionLike` Protocol rather than bare `object`.

> **Design note (2026-07-19):** in response to SOLID review — (1) type the shared helper's `source` as the module's existing `SessionLike` Protocol (not `object`), keeping that abstraction load-bearing; (2) carry the existing per-branch DEBUG logs (cookie count, `skipped_ephemeral_header`, header/token/csrf copies) into the helper so the extraction is observably behavior-neutral on the desktop path.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_browserfree_session_load.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_browserfree_session_load.py::test_api_session_from_source_copies_state -v`
Expected: FAIL — `AttributeError: module 'graftpunk.cache' has no attribute '_api_session_from_session'`.

- [ ] **Step 3: Extract the helper**

In `src/graftpunk/cache.py`, add the helper (lift the existing copy logic verbatim; keep the same header-skip rules and token copy):

```python
def _api_session_from_session(source: "SessionLike") -> "GraftpunkSession":
    """Build a browser-free GraftpunkSession from a session-like object
    (the module's SessionLike Protocol — cookies + headers), copying header
    roles and token caches when present.

    Shared by load_session_for_api (from a cached BrowserSession) and
    load_session_for_api_from_bytes (from a browser-free deserialize).
    """
    from graftpunk.graftpunk_session import GraftpunkSession
    from graftpunk.tokens import _CACHE_ATTR, _CSRF_TOKENS_ATTR

    header_roles = getattr(source, "_gp_header_roles", {})
    api_session = GraftpunkSession(header_roles=header_roles)

    if hasattr(source, "cookies"):
        api_session.cookies = source.cookies
        LOG.debug("copied_cookies_from_session", cookie_count=len(source.cookies))

    if hasattr(source, "headers"):
        _requests_defaults = requests.utils.default_headers()
        for key, value in source.headers.items():
            if key in _requests_defaults and _requests_defaults[key] == value:
                continue
            if key.lower() in _EPHEMERAL_HEADERS:
                LOG.debug("skipped_ephemeral_header", header=key)
                continue
            api_session.headers[key] = value
        LOG.debug("copied_headers_from_session")

    token_cache = getattr(source, _CACHE_ATTR, None)
    if token_cache:
        setattr(api_session, _CACHE_ATTR, token_cache)
        LOG.debug("copied_cached_tokens_from_session", count=len(token_cache))

    csrf_tokens = getattr(source, _CSRF_TOKENS_ATTR, None)
    if csrf_tokens is not None:
        setattr(api_session, _CSRF_TOKENS_ATTR, dict(csrf_tokens))
        LOG.debug("copied_csrf_tokens_from_session", count=len(csrf_tokens))

    return api_session
```

Then replace the body of `load_session_for_api` (after `browser_session = load_session(name)` and the `SessionNotFoundError` guard) so it delegates:

```python
    api_session = _api_session_from_session(browser_session)
    LOG.info(
        "created_api_session_from_cached_session",
        name=name,
        has_header_roles=bool(getattr(browser_session, "_gp_header_roles", {})),
    )
    return api_session
```

(Delete the now-duplicated inline copy block from `load_session_for_api`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_browserfree_session_load.py -v` and the existing cache suite `pytest tests/unit -k session -v`
Expected: PASS; no regressions in existing `load_session_for_api` tests.

- [ ] **Step 5: Commit**

```bash
git add src/graftpunk/cache.py tests/unit/test_browserfree_session_load.py
git commit -m "refactor(cache): extract _api_session_from_session shared helper"
```

---

### Task 2: Browser-free deserialize (`_BrowserFreeUnpickler` + `_deserialize_browserfree`)

**Files:**
- Modify: `src/graftpunk/cache.py`
- Test: `tests/unit/test_browserfree_session_load.py`

**Interfaces:**
- Consumes: `pickle` (dill alias) already imported in `cache.py`.
- Produces: `_deserialize_browserfree(decrypted: bytes) -> object` — unpickles bytes, stubbing any class it cannot import so the browser stack is never required.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_browserfree_session_load.py`:

```python
import io

import dill


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
    # Sanity: a normal load raises because the module can't be imported.
    import pytest

    with pytest.raises(Exception):
        dill.loads(blob)

    recovered = cache._deserialize_browserfree(blob)
    assert recovered.cookies.get("User", domain="example.com") == "dummyuser"
    assert recovered.headers["User-Agent"] == "Chrome/999"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_browserfree_session_load.py::test_deserialize_browserfree_stubs_unimportable_class -v`
Expected: FAIL — `AttributeError: module 'graftpunk.cache' has no attribute '_deserialize_browserfree'`.

- [ ] **Step 3: Implement the browser-free unpickler**

In `src/graftpunk/cache.py`, add (near the other private helpers):

```python
class _Stub:
    """Placeholder for a class the browser-free path cannot import (the browser
    stack is absent). Unpickling restores state via __setstate__ below.

    cookies/headers/_gp_header_roles and the cached-token dict are plain data in
    BrowserSession.__getstate__ (session.py:581-582), so they land directly on
    the stub. NOTE: __getstate__ does NOT serialize `_gp_csrf_tokens`, so csrf
    tokens are absent from the pickle entirely — the stub cannot and does not
    surface them.
    """

    def __setstate__(self, state) -> None:
        # pickle/dill deliver either a dict, or a (dict, slotstate) 2-tuple for
        # __slots__-bearing classes. Handle both; fail loud on any other shape
        # rather than silently discarding state.
        if isinstance(state, tuple) and len(state) == 2:
            dict_state, slot_state = state
            if dict_state:
                self.__dict__.update(dict_state)
            for key, value in (slot_state or {}).items():
                setattr(self, key, value)
        elif isinstance(state, dict):
            self.__dict__.update(state)
        else:
            raise TypeError(f"_Stub cannot restore pickle state of type {type(state)!r}")


class _BrowserFreeUnpickler(pickle.Unpickler):  # pickle is dill (see top import)
    """Unpickler that stubs classes it cannot import BECAUSE the browser stack is
    absent (graftpunk.session.BrowserSession -> requestium/selenium/httpie), so a
    cached session's plain state deserializes browser-free.

    Only import-family errors are converted to stubs. Any OTHER resolution error
    (a genuinely broken module, a renamed-but-present symbol) propagates — it
    must not be silently masked into a stub, which the A4 fixture could never
    catch (stubbing still lands the plain __dict__ and the test keeps passing).
    """

    def find_class(self, module: str, name: str):
        try:
            return super().find_class(module, name)
        except (ImportError, ModuleNotFoundError, AttributeError):
            return type(name, (_Stub,), {})


def _deserialize_browserfree(decrypted: bytes) -> object:
    """Deserialize decrypted session bytes without importing the browser stack."""
    return _BrowserFreeUnpickler(io.BytesIO(decrypted)).load()
```

Add `import io` to the stdlib imports at the top of `cache.py` if not already present.

> **Design note (2026-07-19):** in response to SOLID review — (1) `find_class`
> catches only the import family (`ImportError`/`ModuleNotFoundError`/
> `AttributeError`), so "browser stack absent" stubs quietly while real
> resolution bugs fail loud; (2) `_Stub.__setstate__` handles the `(dict,
> slotstate)` tuple form and raises on any unexpected shape, so a future slotted
> class in the graph is never silently dropped. Both harden the deliberately
> broad stub mechanism; the A4 fixture (Task 5) remains its load-bearing guard.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_browserfree_session_load.py::test_deserialize_browserfree_stubs_unimportable_class -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/graftpunk/cache.py tests/unit/test_browserfree_session_load.py
git commit -m "feat(cache): browser-free unpickler that stubs unimportable classes"
```

---

### Task 3: Bytes-in entry (`load_session_for_api_from_bytes`)

**Files:**
- Modify: `src/graftpunk/cache.py`
- Test: `tests/unit/test_browserfree_session_load.py`

**Interfaces:**
- Consumes: `decrypt_data` (from `graftpunk.encryption`, **extended here with an optional `key` param**), `_deserialize_browserfree` (Task 2), `_api_session_from_session` (Task 1), `SessionExpiredError`. `cache.py` must NOT import `cryptography.fernet` — all Fernet handling stays in `encryption.py`.
- Produces: `load_session_for_api_from_bytes(encrypted: bytes, *, key: bytes | None = None) -> requests.Session`. The optional `key` lets a caller that holds the Fernet key directly (e.g. a Cloudflare Worker with the key as a secret, where graftpunk's `.session_key` file does not exist) decrypt without going through `decrypt_data`'s key sources. **backsight Plan 2 Task 4's cookie-provider Worker depends on this `key` param.**

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_browserfree_session_load.py`:

```python
from graftpunk.encryption import encrypt_data


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
```

(`tests/conftest.py` isolates the config dir per test via `GRAFTPUNK_CONFIG_DIR` + `reset_settings` — it does NOT set an encryption key. The file-based Fernet key is auto-generated on the first `get_encryption_key()` call and cached in-process, so this in-process `encrypt_data` → `load_session_for_api_from_bytes` round-trip shares one key and works with no extra fixture.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_browserfree_session_load.py::test_load_session_for_api_from_bytes_roundtrip -v`
Expected: FAIL — `AttributeError: ... has no attribute 'load_session_for_api_from_bytes'`.

- [ ] **Step 3a: Add an optional `key` to `encryption.decrypt_data`**

Keep all Fernet handling in the module that owns it. In `src/graftpunk/encryption.py`, extend `decrypt_data` so a caller can supply the key, preserving the existing `InvalidToken → EncryptionError` mapping for both paths:

```python
def decrypt_data(data: bytes, *, key: bytes | None = None) -> bytes:
    """Decrypt Fernet-encrypted bytes. Uses the configured key by default, or a
    caller-supplied `key` (for environments without graftpunk's key file/vault,
    e.g. a Worker holding the key as a secret)."""
    fernet_key = key if key is not None else get_encryption_key()
    try:
        return Fernet(fernet_key).decrypt(data)
    except InvalidToken as exc:
        raise EncryptionError("failed to decrypt session data") from exc
```

(Match the existing body/error text at `encryption.py:224`; only the key source becomes conditional — do not change the default behavior. Add a test: `decrypt_data(encrypt_data(b"x"), key=None)` round-trips, `decrypt_data(Fernet(k).encrypt(b"x"), key=k)` round-trips with an explicit key, and a wrong key raises `EncryptionError`.)

- [ ] **Step 3b: Implement the bytes entry**

In `src/graftpunk/cache.py`, add after `load_session_for_api` (no `cryptography.fernet` import here — decryption routes through `encryption.decrypt_data`):

```python
def load_session_for_api_from_bytes(
    encrypted: bytes, *, key: bytes | None = None
) -> requests.Session:
    """Build a browser-free API session directly from encrypted session bytes.

    For callers that already hold the encrypted blob (e.g. a Cloudflare Worker
    that read it through an R2 binding) and cannot/should not go through a
    storage backend or the browser stack. Decrypts, deserializes browser-free,
    and extracts cookies/headers/roles/tokens into a GraftpunkSession.

    Args:
        encrypted: the Fernet(pickle(...)) blob.
        key: optional raw Fernet key. When given, decrypt with it directly —
            for environments where graftpunk's key file/vault does not exist
            (a Worker holding the key as a secret). When None, use the normal
            `decrypt_data` key sources.

    Raises:
        SessionExpiredError: if decryption or deserialization fails, or the
            recovered object lacks the expected structure.
    """
    try:
        decrypted = decrypt_data(encrypted, key=key)
        source = _deserialize_browserfree(decrypted)
    except SessionExpiredError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SessionExpiredError(f"Failed to load session from bytes: {exc}") from exc

    if not hasattr(source, "cookies") or not hasattr(source, "headers"):
        raise SessionExpiredError("Session bytes have invalid structure.")

    return _api_session_from_session(source)
```

> **Design note (2026-07-19):** in response to SOLID review (net-negative) — key
> injection lives in `encryption.py` (the module that owns Fernet), not inline in
> `cache.py`. `load_session_for_api_from_bytes` calls one decryption entry point
> (`decrypt_data(..., key=)`) regardless of key source, so future Fernet changes
> (rotation / TTL / error semantics) have a single site, and both paths yield
> `EncryptionError` on a bad key. `cache.py` never imports `cryptography.fernet`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_browserfree_session_load.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Commit**

```bash
git add src/graftpunk/cache.py tests/unit/test_browserfree_session_load.py
git commit -m "feat(cache): load_session_for_api_from_bytes browser-free entry"
```

---

### Task 4: Lazy browser imports in `__init__.py`

**Files:**
- Modify: `src/graftpunk/__init__.py` (remove eager imports at lines 49-50; add `__getattr__`)
- Test: `tests/unit/test_browserfree_session_load.py`

**Interfaces:**
- Produces: `import graftpunk` no longer imports `graftpunk.session` / `graftpunk.stealth` eagerly; `graftpunk.BrowserSession` and `graftpunk.create_stealth_driver` still resolve on access.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_browserfree_session_load.py`:

```python
import subprocess
import sys


def test_import_graftpunk_does_not_eagerly_import_browser_stack():
    # Fresh interpreter: importing graftpunk must not pull graftpunk.session.
    code = (
        "import graftpunk, sys; "
        "assert 'graftpunk.session' not in sys.modules, 'session eagerly imported'; "
        "assert 'graftpunk.stealth' not in sys.modules, 'stealth eagerly imported'; "
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_lazy_browser_symbols_still_resolve():
    import graftpunk

    # Accessing the lazy attributes still works on a full install.
    assert graftpunk.BrowserSession is not None
    assert callable(graftpunk.create_stealth_driver)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_browserfree_session_load.py::test_import_graftpunk_does_not_eagerly_import_browser_stack -v`
Expected: FAIL — `graftpunk.session` is in `sys.modules` (eagerly imported by the current `__init__.py:49`).

- [ ] **Step 3: Make the imports lazy**

In `src/graftpunk/__init__.py`, delete lines 49-50:

```python
from graftpunk.session import BrowserSession
from graftpunk.stealth import create_stealth_driver
```

Add, immediately after the remaining imports (before `__version__` handling), a module `__getattr__`:

```python
def __getattr__(name):  # PEP 562 lazy attribute loading
    """Lazy-load browser-only symbols so `import graftpunk` works without the
    browser stack (selenium/requestium/etc.) installed — e.g. under Pyodide."""
    if name == "BrowserSession":
        from graftpunk.session import BrowserSession

        return BrowserSession
    if name == "create_stealth_driver":
        from graftpunk.stealth import create_stealth_driver

        return create_stealth_driver
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

Leave `BrowserSession` and `create_stealth_driver` in `__all__` (lazy access satisfies `from graftpunk import BrowserSession`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_browserfree_session_load.py -v`
Expected: PASS. Also spot-check `python -c "from graftpunk import BrowserSession; print(BrowserSession)"` still works.

- [ ] **Step 5: Commit**

```bash
git add src/graftpunk/__init__.py tests/unit/test_browserfree_session_load.py
git commit -m "feat(init): lazy-load browser symbols so import works without browser deps"
```

---

### Task 5: A4 committed fixture guard (real BrowserSession pickle, dummy creds)

The load-bearing whack-once guard: a committed encrypted pickle of a **real** `BrowserSession` (with dummy cookies) that the browser-free path must keep decoding as graftpunk/deps evolve.

**Files:**
- Create: `tests/fixtures/browserfree_session.enc`, `tests/fixtures/browserfree_session.key`
- Create: `scripts/gen_browserfree_fixture.py` (one-time generator, committed for reproducibility)
- Test: `tests/unit/test_browserfree_session_load.py`

**Interfaces:**
- Consumes: `load_session_for_api_from_bytes` (Task 3), `graftpunk.encryption`.

- [ ] **Step 1: Write the generator script (run once, on a full install)**

Create `scripts/gen_browserfree_fixture.py`:

```python
"""One-time generator for the browser-free deserialize fixture.

Run on a FULL graftpunk install (browser deps present). Produces a Fernet-
encrypted dill pickle of a real BrowserSession carrying DUMMY credentials,
plus the test key used to encrypt it. Commit both outputs.
"""
import pathlib

from cryptography.fernet import Fernet

import dill
from graftpunk.session import BrowserSession
from graftpunk.tokens import _CACHE_ATTR

FIX = pathlib.Path(__file__).parent.parent / "tests" / "fixtures"


def main() -> None:
    key = Fernet.generate_key()
    # BrowserSession() with the default backend=selenium + use_stealth=True LAUNCHES
    # a real browser via create_stealth_driver (session.py:146-162) — do NOT
    # construct it bare. Build it under the same mocks the existing session tests
    # use (tests/unit/test_session.py patches graftpunk.stealth.create_stealth_driver
    # + requestium.Session.__init__), and pin the nodriver backend whose
    # __getstate__ builds state manually without touching a live driver.
    from unittest.mock import patch

    with patch("graftpunk.stealth.create_stealth_driver"), patch(
        "requestium.Session.__init__", return_value=None
    ):
        sess = BrowserSession(headless=True, backend="nodriver")

    import requests

    requests.Session.__init__(sess)  # init the cookie jar / headers the pickle needs
    sess._backend_type = "nodriver"  # force __getstate__ down the browser-free branch
    sess.cookies.set("User", "dummyuser", domain="example.com")
    sess.cookies.set("Password", "dummypass", domain="example.com")
    sess.headers["User-Agent"] = "Mozilla/5.0 (dummy)"
    sess._gp_header_roles = {"api": {"X-Api-Key": "dummy"}}
    setattr(sess, _CACHE_ATTR, {"cached": "dummy"})
    # NOTE: BrowserSession.__getstate__ adds only _gp_header_roles + _gp_cached_tokens
    # (session.py:581-582); it does NOT serialize _gp_csrf_tokens, so a csrf attr
    # would be dropped on pickling — intentionally omitted here and not asserted in
    # the A4 guard.

    blob = dill.dumps(sess)
    (FIX / "browserfree_session.enc").write_bytes(Fernet(key).encrypt(blob))
    (FIX / "browserfree_session.key").write_bytes(key)
    print("wrote fixture + key to", FIX)


if __name__ == "__main__":
    main()
```

Run it once on a full install: `python scripts/gen_browserfree_fixture.py`, then commit the two output files. The mocks above avoid a live Chrome; if `BrowserSession`'s constructor signature has drifted, mirror the current `tests/unit/test_session.py` construction pattern.

- [ ] **Step 2: Write the failing guard test**

Append to `tests/unit/test_browserfree_session_load.py`:

```python
import pathlib

from cryptography.fernet import Fernet

_FIX = pathlib.Path(__file__).parent.parent / "fixtures"


def test_a4_fixture_decodes_browser_free(monkeypatch):
    """Whack-once guard: a real committed BrowserSession pickle must keep
    decoding through the browser-free path (cookies/headers/roles/tokens)."""
    enc = (_FIX / "browserfree_session.enc").read_bytes()
    key = (_FIX / "browserfree_session.key").read_bytes()

    # Point graftpunk.encryption at the fixture key for this test.
    from graftpunk import encryption

    monkeypatch.setattr(encryption, "_load_encryption_key", lambda: key)
    encryption.reset_encryption_key_cache()

    api = cache.load_session_for_api_from_bytes(enc)
    assert api.cookies.get("User", domain="example.com") == "dummyuser"
    assert api.cookies.get("Password", domain="example.com") == "dummypass"
    assert "Mozilla/5.0 (dummy)" in api.headers["User-Agent"]
    assert api._gp_header_roles == {"api": {"X-Api-Key": "dummy"}}

    encryption.reset_encryption_key_cache()
```

(Confirm the exact monkeypatch target against `encryption.py`: `get_encryption_key` caches the result of `_load_encryption_key`; patch whichever the codebase reads and call `reset_encryption_key_cache()` before/after. Adjust to the real symbol names verified in `encryption.py:34/59/201`.)

- [ ] **Step 3: Run test to verify it fails, then passes**

Run: `pytest tests/unit/test_browserfree_session_load.py::test_a4_fixture_decodes_browser_free -v`
Expected: FAIL until the fixtures exist (Step 1 run) and the key monkeypatch matches; then PASS.

- [ ] **Step 4: Full suite green**

Run: `pytest tests/unit -v`
Expected: PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
git add scripts/gen_browserfree_fixture.py tests/fixtures/browserfree_session.enc tests/fixtures/browserfree_session.key tests/unit/test_browserfree_session_load.py
git commit -m "test(cache): committed A4 fixture guarding browser-free session load"
```

---

## Self-Review

**Spec coverage (Component A):** A1 lazy imports → Task 4. A2 browser-free deserialize (default-stub-any-unimportable) → Task 2. A3 `load_session_for_api_from_bytes` + DRY shared extraction → Tasks 1 + 3. A4 fixture guard → Task 5. Interfaces produced (`load_session_for_api_from_bytes`, importable-under-Pyodide) → Tasks 3 + 4. "Explicitly NOT changed" (format/class/httpx) — respected: no task touches the pickle format, `BrowserSession`'s bases, or the HTTP transport.

**Placeholder scan:** every code step shows complete code. Two steps flag a verify-against-real-symbol check (the encryption key fixture in Tasks 3/5, the `BrowserSession` constructor in Task 5) — these are genuine "confirm the exact local symbol" notes, not deferred logic; the surrounding code is complete.

**Type consistency:** `_api_session_from_session(source) -> GraftpunkSession` (Task 1) is consumed by `load_session_for_api_from_bytes` (Task 3) and `load_session_for_api` (Task 1). `_deserialize_browserfree(bytes) -> object` (Task 2) feeds Task 3. `_Stub`/`_BrowserFreeUnpickler` (Task 2) used only in Task 2. Names consistent across tasks.

## Notes for the implementer

- Run in a graftpunk worktree; `pip install -e .` it into `backsight-dev` and the cookie-provider Worker deps (per spec Dev & Rollout).
- Do NOT merge or release graftpunk until backsight Plan 2's e2e prod validation passes (spec sequencing step 3).
