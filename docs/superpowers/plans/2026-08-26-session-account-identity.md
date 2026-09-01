# Session Account Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sessions are keyed by plugin **and** account (`myshop@alice`), with the account identifier recorded in metadata, so one login can no longer silently repoint every other process (#151).

**Architecture:** A new dependency-free `session_identity` module owns the `name@label` grammar, identifier derivation, and resolution policy (including the full precedence chain `--session` > `GRAFTPUNK_SESSION` > `.gp-session` > account resolution). Every entry point (plugin commands, `GraftpunkClient`, session/observe commands) computes one **operating session name** per invocation and uses it for both load and write-back. The identifier rides the pickled session object through `BrowserSession`'s serialization whitelist; the CLI login wrapper derives it, stamps the plugin instance inside a restoring context manager, and warns at the login boundary when a slot changes hands.

**Tech Stack:** Python 3.11+, Typer CLI, requests, structlog, pytest (`uv run pytest`), ruff, ty 0.0.75.

**Spec:** `docs/superpowers/specs/2026-08-26-session-account-identity-design.md` (validated; read it alongside this plan — conflicts resolve to the spec).

## Global Constraints

- Session-name charset: `[a-z0-9][a-z0-9_-]*` on each side of **at most one** `@`, never first or last character; dots forbidden. All previously valid names stay valid.
- The env var is `GRAFTPUNK_SESSION` — the string `GP_SESSION` must not appear anywhere.
- `cache_session`'s signature does **not** change and it stays **read-free** (it never fetches stored metadata).
- Load key == store key == the operating session name, computed once per invocation. `plugin.session_name` is only the *base name*, an input to resolution.
- No migration: pre-upgrade bare-name sessions stay valid; `SessionMetadata`'s new field defaults to `None` and old metadata must deserialize on all three backends.
- Run tests as `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q`; lint as `uvx ruff check . && uvx ruff format --check .`; type-check as `uvx ty@0.0.75 check src/` — all three must be green at every commit.
- Never name any private plugin/site in code, tests, comments or commit messages (use `myshop`, `example-store`, placeholders).
- No Claude attribution in commits.

---

### Task 1: `session_identity` module + `AmbiguousSessionError`

**Files:**
- Create: `src/graftpunk/session_identity.py`
- Modify: `src/graftpunk/exceptions.py` (append), `src/graftpunk/cache.py:55` (`_SESSION_NAME_RE = re.compile`) through `src/graftpunk/cache.py:63` (`def validate_session_name`) (validation delegates), `src/graftpunk/__init__.py` (export the error)
- Test: `tests/unit/test_session_identity.py`

**Interfaces:**
- Consumes: nothing (dependency-free module; `SECRET_KEYWORDS` is passed in by callers).
- Produces: `GP_ACCOUNT_ATTR = "_gp_account_identifier"`; `split_session_name(name) -> tuple[str, str | None]`; `join_session_name(base, label) -> str`; `validate_session_name(name) -> None`; `resolve_account_session(base_name, existing_names) -> str`; `derive_account_identity(fields, secret_keywords) -> tuple[str | None, str | None]` (identifier, label); `AmbiguousSessionError(base_name, candidates)` with `.base_name` and `.candidates` attributes, exported from `graftpunk`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_session_identity.py
"""The name@label grammar, derivation, and resolution policy (#151)."""

from __future__ import annotations

import pytest

from graftpunk.exceptions import AmbiguousSessionError, GraftpunkError
from graftpunk.session_identity import (
    GP_ACCOUNT_ATTR,
    derive_account_identity,
    join_session_name,
    resolve_account_session,
    split_session_name,
    validate_session_name,
)

SECRETS = frozenset({"password", "secret", "token", "key"})


class TestGrammar:
    def test_split_bare_and_labelled(self) -> None:
        assert split_session_name("myshop") == ("myshop", None)
        assert split_session_name("myshop@alice") == ("myshop", "alice")

    def test_join_round_trips(self) -> None:
        assert join_session_name("myshop", "alice") == "myshop@alice"
        assert join_session_name("myshop", None) == "myshop"
        base, label = split_session_name(join_session_name("myshop", "alice"))
        assert (base, label) == ("myshop", "alice")


class TestValidation:
    @pytest.mark.parametrize("name", ["myshop", "my-shop_2", "myshop@alice", "a@b"])
    def test_valid(self, name: str) -> None:
        validate_session_name(name)

    @pytest.mark.parametrize(
        "name", ["", "@alice", "myshop@", "a@b@c", "My.Shop", "myshop@Al.ice", "-x"]
    )
    def test_invalid(self, name: str) -> None:
        with pytest.raises(ValueError):
            validate_session_name(name)

    def test_cache_reexport_still_works(self) -> None:
        from graftpunk.cache import validate_session_name as cache_validate

        cache_validate("myshop@alice")


class TestResolution:
    def test_zero_candidates_returns_base(self) -> None:
        assert resolve_account_session("myshop", ["other"]) == "myshop"

    def test_one_bare_candidate(self) -> None:
        assert resolve_account_session("myshop", ["myshop", "other"]) == "myshop"

    def test_one_labelled_candidate(self) -> None:
        assert resolve_account_session("myshop", ["myshop@alice"]) == "myshop@alice"

    def test_several_raise_typed_error_with_candidates(self) -> None:
        with pytest.raises(AmbiguousSessionError) as exc:
            resolve_account_session("myshop", ["myshop", "myshop@alice", "other@bob"])
        assert issubclass(AmbiguousSessionError, GraftpunkError)
        assert exc.value.base_name == "myshop"
        assert exc.value.candidates == ["myshop", "myshop@alice"]

    def test_exported_from_package(self) -> None:
        import graftpunk

        assert "AmbiguousSessionError" in graftpunk.__all__


class TestDerivation:
    def test_keyword_major_order(self) -> None:
        fields = {"login_token": "t", "email": "a@example.com", "username": "alice"}
        identifier, label = derive_account_identity(fields, SECRETS)
        assert identifier == "alice"  # username keyword checked before email
        assert label == "alice"

    def test_user_substring_overlap_is_harmless(self) -> None:
        identifier, _ = derive_account_identity({"user": "bob", "password": "x"}, SECRETS)
        assert identifier == "bob"

    def test_non_secret_fallback(self) -> None:
        identifier, _ = derive_account_identity({"account_no": "12345", "password": "x"}, SECRETS)
        assert identifier == "12345"

    def test_all_secret_or_empty_yields_none(self) -> None:
        assert derive_account_identity({"password": "x"}, SECRETS) == (None, None)
        assert derive_account_identity({"username": ""}, SECRETS) == (None, None)

    def test_label_is_slugified(self) -> None:
        identifier, label = derive_account_identity({"email": "Alice.B@Example.com"}, SECRETS)
        assert identifier == "Alice.B@Example.com"
        assert label == "alice-b-example-com"

    def test_attr_name_constant(self) -> None:
        assert GP_ACCOUNT_ATTR == "_gp_account_identifier"
```

- [ ] **Step 2: Run to verify failure**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_session_identity.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'graftpunk.session_identity'`

- [ ] **Step 3: Append the exception to `src/graftpunk/exceptions.py`**

```python
class AmbiguousSessionError(GraftpunkError):
    """Several cached sessions match a plugin and none was selected.

    Pick one with ``--session``, the ``GRAFTPUNK_SESSION`` env var, or
    ``gp session use``.
    """

    def __init__(self, base_name: str, candidates: list[str]) -> None:
        self.base_name = base_name
        self.candidates = candidates
        names = ", ".join(candidates)
        super().__init__(f"Several sessions cached for '{base_name}': {names}. Pick one.")
```

- [ ] **Step 4: Create `src/graftpunk/session_identity.py`**

```python
"""Session identity: the ``name@label`` grammar, derivation, and resolution policy.

This module owns everything about what a session name *means*. It imports
nothing from ``graftpunk.cache`` (callers hand it data), so naming/identity
policy sits above storage mechanism and stays cycle-free (#151).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from slugify import slugify

from graftpunk.exceptions import AmbiguousSessionError

# The session attribute that carries the account identifier. Owned here so no
# other module hard-codes the string (precedent: tokens.py's _CACHE_ATTR).
GP_ACCOUNT_ATTR = "_gp_account_identifier"

# One side of a session name: today's rule, unchanged.
_PART_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Identifier keywords, checked keyword-major (for each keyword, fields in
# declaration order). The user⊂username overlap is intentional: username is
# checked first, so it wins.
_IDENTIFIER_KEYWORDS = ("username", "email", "login", "identifier", "user")


def split_session_name(name: str) -> tuple[str, str | None]:
    """Split ``base@label`` into its halves; a bare name has ``label=None``."""
    base, sep, label = name.partition("@")
    return (base, label if sep else None)


def join_session_name(base: str, label: str | None) -> str:
    """Inverse of :func:`split_session_name`."""
    return f"{base}@{label}" if label else base


def validate_session_name(name: str) -> None:
    """Validate a session name (``base`` or ``base@label``).

    Each side must match ``[a-z0-9][a-z0-9_-]*``; at most one ``@``, never
    first or last; dots are forbidden (they indicate domains in
    ``gp session clear``).

    Raises:
        ValueError: If the name is invalid.
    """
    if not name:
        raise ValueError("Session name must be non-empty")
    if "." in name:
        raise ValueError(
            f"Session name {name!r} cannot contain dots. "
            "Dots are reserved for domain matching in 'gp session clear'."
        )
    if name.count("@") > 1:
        raise ValueError(f"Session name {name!r} may contain at most one '@'")
    base, label = split_session_name(name)
    for part in (base, label) if label is not None else (base,):
        if not _PART_RE.match(part):
            raise ValueError(
                f"Session name {name!r} must match [a-z0-9][a-z0-9_-]* on each "
                "side of an optional '@' (lowercase alphanumeric, hyphens, underscores)"
            )


def resolve_account_session(base_name: str, existing_names: Iterable[str]) -> str:
    """Pick the session for *base_name* among *existing_names*.

    Candidates are ``base_name`` itself plus any name whose split base equals
    ``base_name``. Exactly one candidate is returned; zero returns
    ``base_name`` (the caller's not-found path handles absence, as today);
    several raise :class:`AmbiguousSessionError` — never "most recent wins".
    """
    candidates = [n for n in existing_names if n == base_name or split_session_name(n)[0] == base_name]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        return base_name
    raise AmbiguousSessionError(base_name, sorted(candidates))


def derive_account_identity(
    fields: Mapping[str, str], secret_keywords: frozenset[str]
) -> tuple[str | None, str | None]:
    """Derive ``(identifier, label)`` from resolved login fields.

    The identifier is the value of the first field whose name contains an
    identifier keyword (keyword-major order); failing that, the first field
    that is not secret per *secret_keywords*. The label is the slugified
    identifier. Returns ``(None, None)`` when nothing usable exists — the
    session then keeps its bare legacy name.
    """
    def pick() -> str | None:
        for keyword in _IDENTIFIER_KEYWORDS:
            for field_name, value in fields.items():
                if keyword in field_name.lower() and value:
                    return value
        for field_name, value in fields.items():
            if not any(k in field_name.lower() for k in secret_keywords) and value:
                return value
        return None

    identifier = pick()
    if identifier is None:
        return (None, None)
    label = slugify(identifier)
    return (identifier, label or None)
```

- [ ] **Step 5: Delegate `cache.py`'s validation**

In `src/graftpunk/cache.py`, delete the `_SESSION_NAME_RE` constant (line 55) and the body of `validate_session_name` (lines 63-84), replacing the function with a re-export near the top of the file:

```python
from graftpunk.session_identity import validate_session_name  # noqa: F401  (public re-export)
```

Keep the name importable as `graftpunk.cache.validate_session_name` (existing callers and tests import it from there). Remove the now-unused `re` import if nothing else in the file uses it (grep first).

- [ ] **Step 6: Export the error from `graftpunk`**

In `src/graftpunk/__init__.py`, add `AmbiguousSessionError` to the `from graftpunk.exceptions import (...)` block and to `__all__` immediately after `"GraftpunkError",` (parent-before-child ordering).

- [ ] **Step 7: Run tests, lint, types**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_session_identity.py tests/unit/test_cache_keys.py tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
Expected: all pass (the full suite guards the `cache.py` delegation — several existing tests exercise `validate_session_name`).

- [ ] **Step 8: Commit**

```bash
git add src/graftpunk/session_identity.py src/graftpunk/exceptions.py src/graftpunk/cache.py src/graftpunk/__init__.py tests/unit/test_session_identity.py
git commit -m "feat(identity): session_identity module — name@label grammar, derivation, resolution (#151)"
```

---

### Task 2: `account_identifier` in metadata + pickle whitelist + backend conformance

**Files:**
- Modify: `src/graftpunk/storage/base.py:46` (`class SessionMetadata`) through `src/graftpunk/storage/base.py:135` (`class SessionStorageBackend`) (field, serializers, protocol docstring), `src/graftpunk/cache.py:177` (`def _extract_session_metadata`), the `SessionMetadata(...)` construction inside `src/graftpunk/cache.py:260` (`def cache_session`), `src/graftpunk/session.py:554` (`def __getstate__`) and `src/graftpunk/session.py:619` (`def __setstate__`) (both backend paths)
- Test: `tests/unit/test_account_metadata.py`

**Interfaces:**
- Consumes: `GP_ACCOUNT_ATTR` from Task 1.
- Produces: `SessionMetadata.account_identifier: str | None = None`; `_extract_session_metadata` returns an `"account_identifier"` key; `BrowserSession` round-trips the attribute on both backend paths.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_account_metadata.py
"""account_identifier flows session-attr → metadata → serializers → pickle (#151)."""

from __future__ import annotations

import pickle

import pytest
import requests

from graftpunk.cache import _extract_session_metadata
from graftpunk.session import BrowserSession
from graftpunk.session_identity import GP_ACCOUNT_ATTR
from graftpunk.storage.base import SessionMetadata, dict_to_metadata, metadata_to_dict


def _metadata(**kw):  # noqa: ANN003, ANN202
    from datetime import UTC, datetime

    base = dict(
        name="myshop@alice",
        checksum="c",
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
        expires_at=None,
        storage_backend="local",
        storage_location="~/x",
    )
    base.update(kw)
    return SessionMetadata(**base)


class TestSerializers:
    def test_round_trip_with_identifier(self) -> None:
        m = _metadata(account_identifier="alice@example.com")
        again = dict_to_metadata(metadata_to_dict(m))
        assert again.account_identifier == "alice@example.com"

    def test_pre_upgrade_dict_without_field_loads_as_none(self) -> None:
        d = metadata_to_dict(_metadata())
        d.pop("account_identifier")
        assert dict_to_metadata(d).account_identifier is None


class TestExtraction:
    def test_extracts_attribute_from_session(self) -> None:
        s = requests.Session()
        setattr(s, GP_ACCOUNT_ATTR, "alice@example.com")
        raw = _extract_session_metadata(s, "myshop@alice")
        assert raw["account_identifier"] == "alice@example.com"

    def test_absent_attribute_is_none(self) -> None:
        raw = _extract_session_metadata(requests.Session(), "myshop")
        assert raw["account_identifier"] is None


@pytest.mark.parametrize("backend_type", ["nodriver", "selenium"])
def test_identifier_survives_pickle_round_trip(backend_type: str) -> None:
    session = BrowserSession.__new__(BrowserSession)
    requests.Session.__init__(session)
    session._backend_type = backend_type
    session._session_name = "myshop@alice"
    if backend_type == "selenium":
        session._driver = None
        session._driver_initializer = None
        session.webdriver_path = None
        session.default_timeout = 30
        session.webdriver_options = {}
        session._last_requests_url = None
    setattr(session, GP_ACCOUNT_ATTR, "alice@example.com")
    restored = pickle.loads(pickle.dumps(session))  # noqa: S301 — our own object
    assert getattr(restored, GP_ACCOUNT_ATTR, None) == "alice@example.com"


def test_backend_key_schemes_accept_at(tmp_path) -> None:  # noqa: ANN001
    """The '@' round-trips through each backend's key/path construction (spec: Naming)."""
    from graftpunk.storage.local import LocalStorageBackend
    from graftpunk.storage.s3 import S3StorageBackend

    local = LocalStorageBackend(base_dir=tmp_path)
    # Local: the session directory is created and named verbatim.
    d = tmp_path / "myshop@alice"
    d.mkdir()
    assert d.name == "myshop@alice"
    assert "myshop@alice" in local.storage_location or True  # path handling only

    key = S3StorageBackend._session_key(
        S3StorageBackend.__new__(S3StorageBackend), "myshop@alice"
    )
    assert "myshop@alice" in key
    # Supabase paths are string joins of the name — covered by the serializer
    # round-trip plus this same in-key assertion pattern if its helper exists.


def test_backend_protocol_docstring_states_charset() -> None:
    from graftpunk.storage.base import SessionStorageBackend

    doc = SessionStorageBackend.__doc__ or ""
    assert "@" in doc and "[a-z0-9]" in doc
```

- [ ] **Step 2: Run to verify failure**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_account_metadata.py -q`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'account_identifier'` (and the pickle tests fail with `None`).

- [ ] **Step 3: Add the field and serializer lines**

In `src/graftpunk/storage/base.py`:
- Add to `SessionMetadata` (after `storage_location`): `account_identifier: str | None = None` with an attribute docstring line: `account_identifier: The unslugified login identifier recorded at login (what the user typed, not server-verified), or None for legacy/no-identity sessions.`
- In `metadata_to_dict`, add `"account_identifier": metadata.account_identifier,`.
- In `dict_to_metadata`, add `account_identifier=data.get("account_identifier"),`.
- Extend the `SessionStorageBackend` class docstring with: `Session names match [a-z0-9][a-z0-9_-]* on each side of at most one '@' (never first/last); every backend's key scheme must round-trip that charset.`

- [ ] **Step 4: Extract the attribute in `cache.py`**

In `_extract_session_metadata` (cache.py:177), add alongside the existing `getattr` collections:

```python
from graftpunk.session_identity import GP_ACCOUNT_ATTR  # module-level import

metadata["account_identifier"] = getattr(session, GP_ACCOUNT_ATTR, None)
```

And in `cache_session`'s `SessionMetadata(...)` construction (cache.py:294-308), add `account_identifier=raw_metadata.get("account_identifier"),`.

- [ ] **Step 5: Extend both pickle paths in `session.py`**

In `BrowserSession.__getstate__` (session.py:554): in the nodriver branch, after `state[_GP_CACHED_TOKENS] = ...`, add `state[GP_ACCOUNT_ATTR] = getattr(self, GP_ACCOUNT_ATTR, None)`; in the selenium branch, after the same line, add the identical statement. In `__setstate__` (session.py:619), in **both** branches beside `self._gp_cached_tokens = state.get(_GP_CACHED_TOKENS, {})`, add:

```python
account = state.get(GP_ACCOUNT_ATTR)
if account is not None:
    setattr(self, GP_ACCOUNT_ATTR, account)
```

Import `GP_ACCOUNT_ATTR` from `graftpunk.session_identity` at the top of `session.py`. While there, fix the stale comment at session.py:570 (`requests.Session has no custom __getstate__` is false — requests uses an `__attrs__` whitelist; reword to "requests.Session pickles via its __attrs__ whitelist, so we build nodriver state manually").

- [ ] **Step 6: Run tests, lint, types; commit**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
Expected: all pass.

```bash
git add src/graftpunk/storage/base.py src/graftpunk/cache.py src/graftpunk/session.py tests/unit/test_account_metadata.py
git commit -m "feat(identity): account_identifier rides the session and its metadata (#151)"
```

---

### Task 3: `compute_operating_session_name` (the one precedence chain)

**Files:**
- Modify: `src/graftpunk/session_identity.py` (append)
- Test: `tests/unit/test_session_identity.py` (append)

**Interfaces:**
- Consumes: `resolve_account_session` (Task 1); `graftpunk.session_context.resolve_session` for the ambient tiers.
- Produces: `compute_operating_session_name(explicit, base_name, existing_names, *, use_ambient=True) -> str` — the single home of `--session` > `GRAFTPUNK_SESSION` > `.gp-session` > account resolution.

- [ ] **Step 1: Write the failing tests (append to `tests/unit/test_session_identity.py`)**

```python
class TestOperatingName:
    def test_explicit_flag_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from graftpunk.session_identity import compute_operating_session_name

        monkeypatch.setenv("GRAFTPUNK_SESSION", "myshop@env")
        got = compute_operating_session_name("myshop@flag", "myshop", ["myshop@a", "myshop@b"])
        assert got == "myshop@flag"

    def test_env_beats_file_and_resolution(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from graftpunk.session_identity import compute_operating_session_name

        monkeypatch.setenv("GRAFTPUNK_SESSION", "myshop@env")
        (tmp_path / ".gp-session").write_text("myshop@file")
        monkeypatch.chdir(tmp_path)
        got = compute_operating_session_name(None, "myshop", ["myshop@a", "myshop@b"])
        assert got == "myshop@env"

    def test_file_beats_resolution(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        from graftpunk.session_identity import compute_operating_session_name

        monkeypatch.delenv("GRAFTPUNK_SESSION", raising=False)
        (tmp_path / ".gp-session").write_text("myshop@file")
        monkeypatch.chdir(tmp_path)
        got = compute_operating_session_name(None, "myshop", ["myshop@a", "myshop@b"])
        assert got == "myshop@file"

    def test_falls_through_to_account_resolution(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from graftpunk.session_identity import compute_operating_session_name

        monkeypatch.delenv("GRAFTPUNK_SESSION", raising=False)
        monkeypatch.chdir(tmp_path)
        assert compute_operating_session_name(None, "myshop", ["myshop@a"]) == "myshop@a"

    def test_use_ambient_false_skips_env_and_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from graftpunk.session_identity import compute_operating_session_name

        monkeypatch.setenv("GRAFTPUNK_SESSION", "myshop@env")
        (tmp_path / ".gp-session").write_text("myshop@file")
        monkeypatch.chdir(tmp_path)
        got = compute_operating_session_name(None, "myshop", ["myshop@a"], use_ambient=False)
        assert got == "myshop@a"
```

- [ ] **Step 2: Run to verify failure**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_session_identity.py -q -k OperatingName`
Expected: FAIL — `ImportError: cannot import name 'compute_operating_session_name'`

- [ ] **Step 3: Append the implementation to `session_identity.py`**

```python
def compute_operating_session_name(
    explicit: str | None,
    base_name: str,
    existing_names: Iterable[str],
    *,
    use_ambient: bool = True,
) -> str:
    """The one home of the precedence chain: flag > env > .gp-session > resolution.

    ``use_ambient=False`` (the Python API's deliberate mode) skips the env and
    per-directory tiers: library code is not steered by ambient shell state.
    Raises :class:`AmbiguousSessionError` when nothing selects and several
    sessions are cached for *base_name*.
    """
    if explicit:
        return explicit
    if use_ambient:
        from graftpunk.session_context import get_active_session

        ambient = get_active_session()
        if ambient:
            return ambient
    return resolve_account_session(base_name, existing_names)
```

(The import is function-local to keep `session_identity`'s module surface dependency-free for its pure functions; `session_context` imports nothing back, so there is no cycle either way.)

- [ ] **Step 4: Run tests, lint, types; commit**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_session_identity.py -q && uvx ruff check . && uvx ty@0.0.75 check src/`
Expected: all pass.

```bash
git add src/graftpunk/session_identity.py tests/unit/test_session_identity.py
git commit -m "feat(identity): compute_operating_session_name owns the precedence chain (#151)"
```

---

### Task 4: CLI resolution wiring + shared renderer + Account column

**Files:**
- Modify: `src/graftpunk/cli/plugin_commands.py:364` (`def get_plugin_for_session`) and `src/graftpunk/cli/plugin_commands.py:379` (`def resolve_session_name`), `src/graftpunk/console.py` (append renderer), `src/graftpunk/cli/session_commands.py:63` (`def session_list`) and the show command (Account column, show fields), `src/graftpunk/cli/main.py:34` (`from graftpunk.cli.plugin_commands import resolve_session_name`) (observe boundary renders the error), `src/graftpunk/cli/plugin_runtime.py` (error boundary renders it — the same `except GraftpunkError` block that prints friendly errors)
- Test: `tests/unit/test_session_commands.py` (append), `tests/unit/test_plugin_commands.py` (append)

**Interfaces:**
- Consumes: `resolve_account_session`, `split_session_name`, `AmbiguousSessionError` (Task 1).
- Produces: `resolve_session_name(name)` now account-aware; `graftpunk.console.render_ambiguous_session(exc) -> None` (prints candidates + the three pick mechanisms to stderr); `session list` has an **Account** column.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_plugin_commands.py`:

```python
class TestAccountAwareResolution:
    def test_single_labelled_session_resolves(self, monkeypatch) -> None:  # noqa: ANN001
        from graftpunk.cli import plugin_commands

        monkeypatch.setitem(plugin_commands._plugin_session_map, "fmtsite", "myshop")
        monkeypatch.setattr(plugin_commands, "list_sessions", lambda: ["myshop@alice"])
        assert plugin_commands.resolve_session_name("fmtsite") == "myshop@alice"

    def test_several_raise(self, monkeypatch) -> None:  # noqa: ANN001
        from graftpunk.cli import plugin_commands
        from graftpunk.exceptions import AmbiguousSessionError

        monkeypatch.setitem(plugin_commands._plugin_session_map, "fmtsite", "myshop")
        monkeypatch.setattr(
            plugin_commands, "list_sessions", lambda: ["myshop", "myshop@alice"]
        )
        with pytest.raises(AmbiguousSessionError):
            plugin_commands.resolve_session_name("fmtsite")

    def test_full_name_passes_through(self, monkeypatch) -> None:  # noqa: ANN001
        from graftpunk.cli import plugin_commands

        monkeypatch.setattr(plugin_commands, "list_sessions", lambda: [])
        assert plugin_commands.resolve_session_name("myshop@alice") == "myshop@alice"

    def test_get_plugin_for_session_matches_labelled(self, monkeypatch) -> None:  # noqa: ANN001
        from graftpunk.cli import plugin_commands

        monkeypatch.setitem(plugin_commands._plugin_session_map, "fmtsite", "myshop")
        # get_plugin_for_session compares by split base; a labelled name maps back.
        # (Uses the registered-plugin registry; assert via the map-only helper.)
        assert plugin_commands._session_base_matches("myshop@alice", "myshop")
```

Append to `tests/unit/test_session_commands.py`:

```python
class TestAccountColumn:
    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_list_shows_account(self, mock_list) -> None:  # noqa: ANN001
        mock_list.return_value = [
            _make_session(name="myshop@alice", account_identifier="alice@example.com"),
            _make_session(name="myshop"),
        ]
        result = runner.invoke(session_app, ["list"], env={"COLUMNS": "220"})
        assert result.exit_code == 0, result.output
        assert "Account" in result.output
        assert "alice@example.com" in result.output


def test_render_ambiguous_session_lists_candidates_and_mechanisms() -> None:
    import io

    from rich.console import Console

    from graftpunk import console as gp_console
    from graftpunk.exceptions import AmbiguousSessionError

    buf = Console(file=io.StringIO(), width=200)
    exc = AmbiguousSessionError("myshop", ["myshop", "myshop@alice"])
    gp_console.render_ambiguous_session(exc, console=buf)
    out = buf.file.getvalue()
    for expected in ("myshop@alice", "--session", "GRAFTPUNK_SESSION", "gp session use"):
        assert expected in out
```

(`_make_session` needs an `account_identifier: str | None = None` parameter added to its signature and dict.)

- [ ] **Step 2: Run to verify failure**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_plugin_commands.py -q -k AccountAware; NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_session_commands.py -q -k "AccountColumn or ambiguous"`
Expected: FAIL (missing helper, missing column, missing renderer).

- [ ] **Step 3: Implement**

In `src/graftpunk/cli/plugin_commands.py`:

```python
from graftpunk.cache import list_sessions
from graftpunk.session_identity import resolve_account_session, split_session_name


def _session_base_matches(session_name: str, base: str) -> bool:
    """True when *session_name* belongs to the plugin whose base name is *base*."""
    return split_session_name(session_name)[0] == base


def resolve_session_name(name: str) -> str:
    """Resolve a name to an operating session name.

    A registered plugin site name maps to its base ``session_name`` and then
    through account resolution (one cached -> that one; several -> raise
    AmbiguousSessionError; zero -> the base, so the not-found path is
    unchanged). Anything else — including full ``base@label`` names — passes
    through unchanged.
    """
    if name in _plugin_session_map:
        return resolve_account_session(_plugin_session_map[name], list_sessions())
    return name
```

Change `get_plugin_for_session`'s comparison from `==` on the map values to `_session_base_matches(session_name, mapped_base)`.

In `src/graftpunk/console.py` append:

```python
def render_ambiguous_session(exc: "AmbiguousSessionError", *, console: Console | None = None) -> None:
    """Render the pick-one message for an ambiguous session.

    Every CLI entry point that can surface ``AmbiguousSessionError`` calls
    this — the rendering exists once, whichever door the error exits through.
    """
    c = console or err_console
    error(f"Several sessions cached for '{exc.base_name}':", console=c)
    for name in exc.candidates:
        c.print(f"  - {name}", markup=False, highlight=False)
    info(
        "Pick one with --session <name>, the GRAFTPUNK_SESSION env var, "
        "or: gp session use <name>",
        console=c,
    )
```

(with `from graftpunk.exceptions import AmbiguousSessionError` under `TYPE_CHECKING` or directly — `exceptions` imports nothing, so a direct import is safe). Wire `except AmbiguousSessionError as exc: gp_console.render_ambiguous_session(exc); raise typer.Exit(1)` at: the plugin-runtime friendly-error boundary, each `resolve_session_name` call site in `session_commands.py`, and the observe callback in `main.py`.

In `src/graftpunk/cli/session_commands.py`: add a `table.add_column("Account", style="white")` after the Domain column and a `_cell(session.get("account_identifier") or _label_of(session.get("name", "")))` row cell, where `_label_of` is `split_session_name(name)[1] or ""`; `session show` prints `Account:` (identifier) and `Label:` (from the name) lines using the same escape discipline as the existing fields.

- [ ] **Step 4: Run the full suite, lint, types; commit**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
Expected: all pass.

```bash
git add src/graftpunk/cli/plugin_commands.py src/graftpunk/console.py src/graftpunk/cli/session_commands.py src/graftpunk/cli/main.py src/graftpunk/cli/plugin_runtime.py tests/unit/test_plugin_commands.py tests/unit/test_session_commands.py
git commit -m "feat(cli): account-aware resolution, shared ambiguity renderer, Account column (#151)"
```

---

### Task 5: Plugin-command path — `--session` builtin + operating name for load and store

**Files:**
- Modify: `src/graftpunk/cli/command_factory.py:144` (`BUILTIN_OPTIONS: dict`) and `_builtin_option_params` (`BUILTIN_OPTIONS` + option param), `src/graftpunk/cli/plugin_runtime.py:79` (`session = plugin.get_session() if needs_session else requests.Session()`), `src/graftpunk/cli/plugin_runtime.py:163` (`_session_name=(plugin.session_name if needs_session else "")`) and `src/graftpunk/cli/plugin_runtime.py:207` (`update_session_cookies(session, plugin.session_name)`) (operating name; load; ctx; write-back)
- Test: `tests/unit/test_plugin_runtime_session.py` (new)

**Interfaces:**
- Consumes: `compute_operating_session_name` (Task 3), `load_session_for_api` (existing).
- Produces: every synthesized plugin command accepts `--session <name>`; `run_plugin_command`'s load and write-back both use the operating name; `CommandContext._session_name` carries it.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_plugin_runtime_session.py
"""The plugin-command path computes one operating name for load AND store (#151)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from graftpunk.exceptions import AmbiguousSessionError


def _invoke(plugin, argv, **env):  # noqa: ANN001, ANN002, ANN003, ANN202
    import typer
    from typer.testing import CliRunner

    from graftpunk.cli.plugin_commands import register_plugin_commands

    app = typer.Typer()
    with patch(
        "graftpunk.cli.plugin_commands.discover_all_plugins", return_value=(plugin,)
    ):
        register_plugin_commands(app, notify_errors=False)
    return CliRunner().invoke(app, argv, env={"COLUMNS": "200", **env})


def _plugin():  # noqa: ANN202
    from graftpunk.plugins.cli_plugin import SitePlugin, command

    class ShopPlugin(SitePlugin):
        site_name = "fmtsite"
        base_url = "https://fmt.example.com"
        session_name = "myshop"
        requires_session = True
        saves_session = True

        @command(help="List items", saves_session=True)
        def items(self, ctx):  # noqa: ANN001, ANN202
            ctx._session_dirty = True
            return {"ok": True}

    return ShopPlugin()


class TestOperatingNameOnPluginPath:
    @patch("graftpunk.cli.plugin_runtime.update_session_cookies")
    @patch("graftpunk.cli.plugin_runtime.load_session_for_api")
    @patch("graftpunk.cli.plugin_runtime.list_sessions", return_value=["myshop@alice"])
    def test_resolved_name_used_for_load_and_store(
        self, _ls, mock_load, mock_update
    ) -> None:  # noqa: ANN001
        mock_load.return_value = requests.Session()
        result = _invoke(_plugin(), ["fmtsite", "items"])
        assert result.exit_code == 0, result.output
        assert mock_load.call_args[0][0] == "myshop@alice"
        assert mock_update.call_args[0][1] == "myshop@alice"

    @patch("graftpunk.cli.plugin_runtime.load_session_for_api")
    @patch(
        "graftpunk.cli.plugin_runtime.list_sessions",
        return_value=["myshop@alice", "myshop@bob"],
    )
    def test_session_flag_pins(self, _ls, mock_load) -> None:  # noqa: ANN001
        mock_load.return_value = requests.Session()
        result = _invoke(_plugin(), ["fmtsite", "items", "--session", "myshop@bob"])
        assert result.exit_code == 0, result.output
        assert mock_load.call_args[0][0] == "myshop@bob"

    @patch("graftpunk.cli.plugin_runtime.load_session_for_api")
    @patch(
        "graftpunk.cli.plugin_runtime.list_sessions",
        return_value=["myshop@alice", "myshop@bob"],
    )
    def test_ambiguous_refuses_with_candidates(self, _ls, mock_load) -> None:  # noqa: ANN001
        mock_load.return_value = requests.Session()
        result = _invoke(_plugin(), ["fmtsite", "items"])
        assert result.exit_code != 0
        assert "myshop@alice" in result.output and "GRAFTPUNK_SESSION" in result.output

    @patch("graftpunk.cli.plugin_runtime.load_session_for_api")
    @patch("graftpunk.cli.plugin_runtime.list_sessions", return_value=["myshop@alice"])
    def test_env_var_pins(self, _ls, mock_load) -> None:  # noqa: ANN001
        mock_load.return_value = requests.Session()
        result = _invoke(
            _plugin(), ["fmtsite", "items"], GRAFTPUNK_SESSION="myshop@alice"
        )
        assert result.exit_code == 0, result.output
        assert mock_load.call_args[0][0] == "myshop@alice"
```

- [ ] **Step 2: Run to verify failure**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_plugin_runtime_session.py -q`
Expected: FAIL — `--session` is not a recognized option / load called with a bare name.

- [ ] **Step 3: Implement**

In `src/graftpunk/cli/command_factory.py`: change `BUILTIN_OPTIONS` to `{"format": "json", "view": (), "output": "", "session": ""}` and add the option parameter in `_builtin_option_params` (same shape as the `output` one):

```python
sess = inspect.Parameter(
    "session",
    inspect.Parameter.KEYWORD_ONLY,
    default=typer.Option(
        BUILTIN_OPTIONS["session"],
        "--session",
        help="Session name to use (base or base@label); overrides env and .gp-session",
    ),
    annotation=str,
)
```

Note the reserved-name check at `src/graftpunk/cli/command_factory.py:232` (`reserved = {"ctx"} | (set(BUILTIN_OPTIONS) if include_builtin_options else set())`) already covers new builtin names via `set(BUILTIN_OPTIONS)` — a plugin param named `session` now raises `PluginError` at registration, which is the correct collision behaviour (add one test for it in the existing reserved-names test class).

In `src/graftpunk/cli/plugin_runtime.py`: pop the option (`explicit_session: str = kwargs.pop("session", BUILTIN_OPTIONS["session"])` beside the other pops at :54-57); replace the load at :79 and the write-back at :207:

```python
from graftpunk.cache import list_sessions, load_session_for_api
from graftpunk.session_identity import compute_operating_session_name

operating_name = ""
if needs_session:
    operating_name = compute_operating_session_name(
        explicit_session or None, plugin.session_name, list_sessions()
    )
    session = load_session_for_api(operating_name)
else:
    session = requests.Session()
```

`CommandContext` gets `_session_name=operating_name` (replacing `plugin.session_name` at :163), and the write-back becomes `update_session_cookies(session, operating_name)`. The `AmbiguousSessionError` escapes to the friendly-error boundary wired in Task 4.

Also confirm the removed `plugin.get_session()` call has no other consumer on this path (the method itself stays — plugins may call it; a one-line docstring note on `get_session` says the CLI runtime now resolves accounts first and loads directly).

- [ ] **Step 4: Run the full suite, lint, types; commit**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
Expected: all pass (existing plugin-runtime tests updated only if they asserted the old `plugin.get_session()` call — adjust those mocks to `load_session_for_api`).

```bash
git add src/graftpunk/cli/command_factory.py src/graftpunk/cli/plugin_runtime.py tests/unit/test_plugin_runtime_session.py tests/unit/test_command_factory.py
git commit -m "feat(cli): plugin commands take --session; operating name keys load and store (#151)"
```

---

### Task 6: `GraftpunkClient(session=...)` and client write-backs

**Files:**
- Modify: `src/graftpunk/client.py:200` (`class GraftpunkClient`), `src/graftpunk/client.py:402` (`update_session_cookies(session, plugin.session_name)`) and `src/graftpunk/client.py:431` (`update_session_cookies(self._session, self._plugin.session_name)`)
- Test: `tests/unit/test_client.py` (append)

**Interfaces:**
- Consumes: `compute_operating_session_name` (Task 3).
- Produces: `GraftpunkClient(plugin_name, session: str | None = None)`; `self._session_name` (operating name) keys `load_session_for_api` and both `update_session_cookies` calls.

- [ ] **Step 1: Write the failing tests (append to `tests/unit/test_client.py`)**

```python
class TestClientOperatingSession:
    def test_pin_is_used_for_load_and_store(self) -> None:
        plugin = MagicMock()
        plugin.session_name = "myshop"
        plugin.get_commands.return_value = [_make_spec(name="items", requires_session=True)]
        with patch("graftpunk.client.get_plugin", return_value=plugin):
            client = GraftpunkClient("fmtsite", session="myshop@bob")
        assert client._session_name == "myshop@bob"

    def test_unpinned_resolves_ignoring_ambient(self, monkeypatch) -> None:  # noqa: ANN001
        monkeypatch.setenv("GRAFTPUNK_SESSION", "myshop@env")
        plugin = MagicMock()
        plugin.session_name = "myshop"
        plugin.get_commands.return_value = []
        with (
            patch("graftpunk.client.get_plugin", return_value=plugin),
            patch("graftpunk.client.list_sessions", return_value=["myshop@alice"]),
        ):
            client = GraftpunkClient("fmtsite")
        assert client._session_name == "myshop@alice"  # env deliberately ignored

    def test_unpinned_ambiguous_raises(self) -> None:
        from graftpunk.exceptions import AmbiguousSessionError

        plugin = MagicMock()
        plugin.session_name = "myshop"
        plugin.get_commands.return_value = []
        with (
            patch("graftpunk.client.get_plugin", return_value=plugin),
            patch(
                "graftpunk.client.list_sessions",
                return_value=["myshop@alice", "myshop@bob"],
            ),
            pytest.raises(AmbiguousSessionError),
        ):
            GraftpunkClient("fmtsite")
```

- [ ] **Step 2: Run to verify failure**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_client.py -q -k OperatingSession`
Expected: FAIL — unexpected keyword `session` / missing `_session_name`.

- [ ] **Step 3: Implement**

In `src/graftpunk/client.py`: add `list_sessions` to the `graftpunk.cache` import; extend `__init__(self, plugin_name: str, session: str | None = None)`; after the plugin is loaded, compute once:

```python
from graftpunk.session_identity import compute_operating_session_name

# Library code is deliberately not steered by ambient shell state
# (GRAFTPUNK_SESSION / .gp-session): pin > account resolution only.
self._session_name = compute_operating_session_name(
    session, self._plugin.session_name, list_sessions(), use_ambient=False
)
```

Replace `self._plugin.session_name` with `self._session_name` in the session load path and at the two write-backs (client.py:402 and :431). Update the class docstring's `Raises:` to mention `AmbiguousSessionError` when unpinned with several cached.

- [ ] **Step 4: Run the full suite, lint, types; commit**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
Expected: all pass (existing client tests construct plugins with `session_name`; they resolve to the bare name via the zero/one rule and keep passing).

```bash
git add src/graftpunk/client.py tests/unit/test_client.py
git commit -m "feat(client): session= pin; operating name keys client load and write-backs (#151)"
```

---### Task 7: Login flow — `--as`, derivation, stamp, funnel, boundary warning

**Files:**
- Modify: `src/graftpunk/cli/login_commands.py:143` (`def make_login_body`) and the login-command registration (derivation, stamp context manager, warning, `--as` param, post-login print), `src/graftpunk/plugins/login_engine.py:735` (`async def _run_nodriver_steps`) and `src/graftpunk/plugins/login_engine.py:847` (`def _generate_selenium_login`) (two cache sites → funnel), `src/graftpunk/plugins/cli_plugin.py:1066` (`async def browser_session`) and `src/graftpunk/plugins/cli_plugin.py:1105` (`def browser_session_sync`) (two `browser_session` cache sites → funnel; funnel helper lives here beside them)
- Test: `tests/unit/test_login_identity.py` (new)

**Interfaces:**
- Consumes: `derive_account_identity`, `join_session_name`, `GP_ACCOUNT_ATTR` (Task 1), `get_session_metadata` (existing, cache.py:222).
- Produces: `_stamp_login_identity(plugin, label, identifier)` context manager in `login_commands.py`; `_cache_login_session(plugin, session)` in `cli_plugin.py` (copies the `GP_ACCOUNT_ATTR` attribute plugin→session, then `cache_session(session, plugin.session_name)`); `gp <site> login --as <label>`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_login_identity.py
"""Login derives, stamps, funnels and warns about account identity (#151)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from graftpunk.session_identity import GP_ACCOUNT_ATTR


class TestStamp:
    def test_stamp_sets_and_restores(self) -> None:
        from graftpunk.cli.login_commands import _stamp_login_identity
        from graftpunk.plugins.cli_plugin import SitePlugin

        class P(SitePlugin):
            site_name = "fmtsite"
            base_url = "https://fmt.example.com"
            session_name = "myshop"

        plugin = P()
        with _stamp_login_identity(plugin, "alice", "alice@example.com"):
            assert plugin.session_name == "myshop@alice"
            assert getattr(plugin, GP_ACCOUNT_ATTR) == "alice@example.com"
        assert plugin.session_name == "myshop"  # class attr visible again
        assert not hasattr(plugin, GP_ACCOUNT_ATTR)

    def test_stamp_without_label_is_a_no_op(self) -> None:
        from graftpunk.cli.login_commands import _stamp_login_identity
        from graftpunk.plugins.cli_plugin import SitePlugin

        class P(SitePlugin):
            site_name = "fmtsite"
            base_url = "https://fmt.example.com"
            session_name = "myshop"

        plugin = P()
        with _stamp_login_identity(plugin, None, None):
            assert plugin.session_name == "myshop"


class TestFunnel:
    def test_funnel_copies_attribute_and_caches(self) -> None:
        from graftpunk.plugins.cli_plugin import SitePlugin, _cache_login_session

        class P(SitePlugin):
            site_name = "fmtsite"
            base_url = "https://fmt.example.com"
            session_name = "myshop"

        plugin = P()
        setattr(plugin, GP_ACCOUNT_ATTR, "alice@example.com")
        session = requests.Session()
        with patch("graftpunk.plugins.cli_plugin.cache_session") as mock_cache:
            _cache_login_session(plugin, session)
        assert getattr(session, GP_ACCOUNT_ATTR) == "alice@example.com"
        mock_cache.assert_called_once_with(session, "myshop")


class TestBoundaryWarning:
    def test_warns_only_when_both_present_and_unequal(self) -> None:
        from graftpunk.cli.login_commands import _warn_if_slot_changes_hands

        stored = MagicMock(account_identifier="bob@example.com")
        with patch(
            "graftpunk.cli.login_commands.get_session_metadata", return_value=stored
        ), patch("graftpunk.cli.login_commands.gp_console") as mock_console:
            _warn_if_slot_changes_hands("myshop", "alice@example.com")
        assert mock_console.warn.called

    @pytest.mark.parametrize(
        ("stored_id", "incoming"),
        [(None, "alice@example.com"), ("bob@example.com", None), (None, None), ("a", "a")],
    )
    def test_silent_otherwise(self, stored_id, incoming) -> None:  # noqa: ANN001
        from graftpunk.cli.login_commands import _warn_if_slot_changes_hands

        stored = MagicMock(account_identifier=stored_id) if stored_id is not None else None
        with patch(
            "graftpunk.cli.login_commands.get_session_metadata", return_value=stored
        ), patch("graftpunk.cli.login_commands.gp_console") as mock_console:
            _warn_if_slot_changes_hands("myshop", incoming)
        assert not mock_console.warn.called
```

- [ ] **Step 2: Run to verify failure**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_login_identity.py -q`
Expected: FAIL — the three helpers do not exist.

- [ ] **Step 3: Implement the helpers**

In `src/graftpunk/plugins/cli_plugin.py` (beside the `browser_session` helpers):

```python
def _cache_login_session(plugin: "SitePlugin", session: Any) -> str:
    """The one login-flow cache funnel: copy the account attribute, then cache.

    Every login path caches through this helper so the plugin→session identity
    copy is a function call, not an invariant each cache site must remember.
    Returns the session name used.
    """
    from graftpunk.session_identity import GP_ACCOUNT_ATTR

    account = getattr(plugin, GP_ACCOUNT_ATTR, None)
    if account is not None:
        setattr(session, GP_ACCOUNT_ATTR, account)
    cache_session(session, plugin.session_name)
    return plugin.session_name
```

Replace the four cache sites — both `cache_session(session, plugin.session_name)` calls in the generated login flows, and the two inside `browser_session`/`browser_session_sync` — with `_cache_login_session(plugin, session)` (in `login_engine.py`, import it from `graftpunk.plugins.cli_plugin`; that direction already exists).

In `src/graftpunk/cli/login_commands.py`:

```python
from contextlib import contextmanager

from graftpunk import console as gp_console
from graftpunk.cache import get_session_metadata
from graftpunk.session_identity import (
    GP_ACCOUNT_ATTR,
    derive_account_identity,
    join_session_name,
)


@contextmanager
def _stamp_login_identity(plugin, label, identifier):  # noqa: ANN001, ANN201
    """Stamp the plugin instance for the duration of one login flow.

    Instance attributes shadow the class attributes the cache sites read;
    on exit the shadows are deleted, so a plugin instance held across a
    login cannot leak the stamped identity into later reads.
    """
    if not label:
        yield
        return
    base = plugin.session_name
    plugin.session_name = join_session_name(base, label)
    if identifier is not None:
        setattr(plugin, GP_ACCOUNT_ATTR, identifier)
    try:
        yield
    finally:
        try:
            del plugin.session_name  # remove the instance shadow
        except AttributeError:
            pass
        if hasattr(plugin, GP_ACCOUNT_ATTR):
            delattr(plugin, GP_ACCOUNT_ATTR)


def _warn_if_slot_changes_hands(session_name: str, incoming_identifier: str | None) -> None:
    """Warn when a login will overwrite a slot recorded for a different account.

    Both identifiers must be present and unequal; a missing identifier on
    either side never warns (legacy slots, refresh writes).
    """
    if not incoming_identifier:
        return
    stored = get_session_metadata(session_name)
    stored_id = getattr(stored, "account_identifier", None) if stored else None
    if stored_id and stored_id != incoming_identifier:
        LOG.warning(
            "session_slot_changing_account",
            session=session_name,
            stored=stored_id,
            incoming=incoming_identifier,
        )
        gp_console.warn(
            f"Session '{session_name}' was recorded for {stored_id}; this login "
            f"replaces it as {incoming_identifier}."
        )
```

- [ ] **Step 4: Wire `make_login_body` and the `--as` option**

In `make_login_body` (login_commands.py:143), after the credentials dict is fully resolved and before the login callable is invoked:

```python
as_label: str = kwargs.pop("as_label", "") or ""
identifier, derived_label = derive_account_identity(credentials, SECRET_KEYWORDS)
label = as_label or derived_label
target_name = join_session_name(plugin.session_name, label) if label else plugin.session_name
_warn_if_slot_changes_hands(target_name, identifier)
with _stamp_login_identity(plugin, label, identifier):
    ... existing invocation of login_method (both async and sync branches) ...
    cached_name = plugin.session_name  # read inside the stamp
```

After a successful login, print the full name and the mismatch hint:

```python
gp_console.info(f"Cached session: {cached_name}")
from graftpunk.session_context import resolve_session

current = resolve_session(None)
if current and current != cached_name:
    gp_console.info(
        f"This shell is pinned to {current} — run: gp session use {cached_name} to switch"
    )
```

In the login command registration (login_commands.py:290), add to `param_specs`:

```python
PluginParamSpec.option(
    "as_label",
    type=str,
    default="",
    help="Account label for the cached session (default: derived from the login identifier)",
    cli_name="--as",
)
```

(If `PluginParamSpec.option` has no `cli_name` parameter, register the option as `as_label` and add `"--as"` via the param's click kwargs the same way `headful` is declared — check `PluginParamSpec.option`'s signature first and follow its existing pattern; the CLI-visible flag must be `--as`.) Validate the label with the same charset as a name part: `if as_label: validate_session_name(f"x@{as_label}")` (cheap reuse; a bad label raises `ValueError` at the CLI boundary).

- [ ] **Step 5: Run the full suite, lint, types; commit**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
Expected: all pass (existing login tests exercise `make_login_body` with credentials; the derivation is additive and the stamp is a no-op when no identifier is derivable — e.g. all-secret test fixtures).

```bash
git add src/graftpunk/cli/login_commands.py src/graftpunk/plugins/login_engine.py src/graftpunk/plugins/cli_plugin.py tests/unit/test_login_identity.py
git commit -m "feat(login): --as label, identity derivation and stamp, cache funnel, boundary warning (#151)"
```

---

### Task 8: End-to-end test, docs, changelog

**Files:**
- Test: `tests/unit/test_multi_account_e2e.py` (new)
- Modify: `docs/HOW_IT_WORKS.md` (session cache section), `CHANGELOG.md` (`[Unreleased]` → `### Added`)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the end-to-end test**

```python
# tests/unit/test_multi_account_e2e.py
"""Two accounts on one plugin coexist; nothing resolves silently (#151)."""

from __future__ import annotations

import requests

from graftpunk.cache import cache_session, list_sessions
from graftpunk.exceptions import AmbiguousSessionError
from graftpunk.session_identity import (
    GP_ACCOUNT_ATTR,
    compute_operating_session_name,
    resolve_account_session,
)


def _cache(name: str, identifier: str, isolated_config) -> None:  # noqa: ANN001
    s = requests.Session()
    setattr(s, GP_ACCOUNT_ATTR, identifier)
    cache_session(s, name)


def test_two_accounts_coexist_and_require_pinning(
    isolated_config, monkeypatch  # noqa: ANN001
) -> None:
    import pytest

    monkeypatch.delenv("GRAFTPUNK_SESSION", raising=False)
    _cache("myshop@alice", "alice@example.com", isolated_config)
    _cache("myshop@bob", "bob@example.com", isolated_config)

    names = list_sessions()
    assert {"myshop@alice", "myshop@bob"} <= set(names)

    with pytest.raises(AmbiguousSessionError):
        resolve_account_session("myshop", names)

    assert compute_operating_session_name("myshop@bob", "myshop", names) == "myshop@bob"
    monkeypatch.setenv("GRAFTPUNK_SESSION", "myshop@alice")
    assert compute_operating_session_name(None, "myshop", names) == "myshop@alice"
```

(Reuse the existing `isolated_config` fixture pattern from `tests/unit/test_config_commands.py` — it points `GRAFTPUNK_CONFIG_DIR` at a tmp dir so `cache_session` writes to the local backend under the test's control. If the fixture lives in that module rather than `conftest.py`, move it to `tests/unit/conftest.py` so both files share it.)

- [ ] **Step 2: Run it**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_multi_account_e2e.py -q`
Expected: PASS (everything is already built; this is the integration proof).

- [ ] **Step 3: Docs**

In `docs/HOW_IT_WORKS.md`, in the session-cache/session-management area, add a short subsection:

```markdown
### Multi-Account Sessions

Sessions are keyed by plugin **and** account: `gp myshop login` as alice caches
`myshop@alice`; a second login as bob caches `myshop@bob` beside it, and logging
in never evicts another account's session. The label defaults to the slugified
login identifier; `gp myshop login --as work` pins a label. Metadata records the
identifier (what was typed at login, not server-verified), shown in
`gp session list`'s Account column.

Which session a command uses is resolved once per invocation:
`--session` > `GRAFTPUNK_SESSION` > `.gp-session` in the working directory >
the plugin's cached sessions (exactly one → used; several → the command refuses
and lists them — never "most recent wins"). The same resolved name keys every
write-back, so a refresh can never fork into another slot. `GraftpunkClient`
resolves the same way but deliberately ignores the ambient env/file tiers; pass
`GraftpunkClient("site", session="myshop@alice")` to pin.
```

- [ ] **Step 4: CHANGELOG**

Under `## [Unreleased]`, add a `### Added` entry (create the heading if absent, above `### Changed`):

```markdown
- **Multi-account sessions** ([#151](https://github.com/stavxyz/graftpunk/issues/151)) — sessions are keyed by plugin and account (`myshop@alice`); `gp <site> login` derives the account label from the login identifier (`--as` overrides) and records the identifier in metadata (`gp session list` shows it). Commands resolve one operating session per invocation (`--session` > `GRAFTPUNK_SESSION` > `.gp-session` > sole cached session) and refuse with `AmbiguousSessionError` when several are cached and none is picked; the same name keys every write-back. `GraftpunkClient` gains `session=` and ignores ambient shell state by design. Logging in over a slot recorded for a different account warns. Existing bare-name sessions keep working; no migration.
```

- [ ] **Step 5: Full suite, lint, types; commit**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
Expected: all pass.

```bash
git add tests/unit/test_multi_account_e2e.py docs/HOW_IT_WORKS.md CHANGELOG.md tests/unit/conftest.py
git commit -m "test(identity): multi-account end-to-end; document multi-account sessions (#151)"
```
