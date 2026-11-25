---
type: plan
---

# Sessions stored as plain data: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** graftpunk 1.45.0 reads a saved session as a versioned JSON record or as a legacy pickle, writes pickles by default and records when `GRAFTPUNK_SESSION_FORMAT=record`, and loads either for API use without the browser packages.

**Architecture:** Four browser-free leaf modules own the stored format (`session_riders`, `session_record`, `session_legacy`, `session_view`); `cache.py` keeps storage and the rollout policy in private functions. Every class already reduces itself to a state dict through `__getstate__` and restores through `__setstate__`; the record is that dict as JSON, described by one field table. Committed fixtures written by released versions prove old sessions still load.

**Tech Stack:** Python 3.11 to 3.14, requests, dill (imported as `pickle` in `cache.py`), cryptography (Fernet), pydantic-settings, pytest with xdist, uv.

**Spec:** `docs/superpowers/specs/2026-11-24-session-record-format-design.md`. Read it first; this plan names files, signatures and tests and does not restate the spec's reasoning.

## Global Constraints

- Nothing that works on 1.44.0 may break. A pickle written by this branch under the default setting must load on 1.44.0, tokens included.
- Frozen pickled class paths: `graftpunk.session.BrowserSession`, `graftpunk.graftpunk_session.GraftpunkSession`, `graftpunk.tokens.CachedToken`. The cookie policy pickles as `http.cookiejar.DefaultCookiePolicy`.
- Public signatures unchanged: `cache_session`, `load_session`, `load_session_for_api`, `load_session_for_api_resolved`, `load_session_for_api_from_bytes`, `update_session_cookies`, `copy_session`.
- The four leaf modules import no session class and none of `selenium`, `requestium`, `nodriver`, `httpie`. Allowed graftpunk imports per module are in Task 2 and Task 4 to 6.
- The login engine and `_warn_login_page_unreadable` are not touched.
- Public repo: placeholder hosts only (`myshop.example`, `idp.example.com`), a generated test key, no real site, plugin or account names. No em or en dashes. No attribution lines in commits.
- Gate, green after the last edit of every task: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4` (never `-n auto`), `uvx ruff check .`, `uvx ruff format --check src tests`, `uvx ty@0.0.75 check src/ --python .venv`. Changed test files also under 3.11 and 3.14 in scratch venvs (`UV_PROJECT_ENVIRONMENT=<scratch>/venv3xx`); the worktree `.venv` stays on 3.12.
- Live Chrome only in Task 1 Step 5, headless, with `--use-mock-keychain --password-store=basic`, one launch per fixture version, never in a loop.
- One commit per task, pushed to the PR branch as it lands.

## Review Focus

Inputs the spec is silent on that a user will hit. Each has a test in the task named.

1. A record that decrypts to JSON but is not a valid record (not an object, no `kind`, a `cookies` entry missing `name`): every reader raises `SessionExpiredError` with the session name, as a corrupt pickle does today, never a `KeyError` traceback. (Task 4)
2. A cached token whose `ttl` or `extracted_at` is `inf` or `nan`, or a header whose value is `bytes`: not valid JSON, so the session is unrecordable with a warning naming the key and is written as a pickle; nothing raises. (Task 4)
3. A session with no cookies and no headers, and a cookie with `expires=None`, a non-ASCII value and a `rest` entry: each round-trips state dict to record to state dict unchanged. (Task 4)
4. `GRAFTPUNK_SESSION_FORMAT=json` (a typo): settings validation fails at first use with a message naming `pickle` and `record`; no session is written. (Task 7)
5. A write-back of a record on a full install whose setting is `pickle`: the stored bytes stay a record and its unknown top-level key survives. (Task 8)

---

### Task 1: Compat fixtures from released versions, and a baseline test

The safety net comes first: fixtures written by real releases, and a test that pins what today's loaders return for each, before any loader changes.

**Files:**
- Create: `scripts/session_compat.py`
- Create: `tests/fixtures/session_compat/<version>/<case>.enc`, `<case>.expected.json`, one `key` file, `README.md`
- Create: `tests/unit/test_session_compat.py`
- Modify: `tests/fixtures/README.md`, `CONTRIBUTING.md` (the `gen_browserfree_fixture.py` entry points at the new script)
- Delete: `scripts/gen_browserfree_fixture.py`; move `tests/fixtures/browserfree_session.{enc,key}` to `tests/fixtures/session_compat/legacy/` and update its readers: `scripts/smoke_install.sh`, `tests/unit/test_browserfree_session_load.py`, `tests/unit/test_lite_cli_hints.py`, `tests/unit/test_cookies.py`

**Interfaces:**
- Produces: `scripts/session_compat.py generate --graftpunk VERSION --out DIR [--selenium]` and `read --graftpunk VERSION --store DIR --name NAME` (exit 0 and prints the cookie names when the session loads; exit 3 and prints the exception class when it does not). Both run the released version in a scratch venv made with `uv venv` under a temp directory and never touch the repo `.venv`.
- Produces: fixture cases per version (`1.21.0`, `1.40.1`, `1.43.1`, `1.44.0`): `nodriver`, `requests`, `api`, `tokens` (nodriver with cached tokens and header roles), `account` (with an account label), `adapter` (requests with a custom `HTTPAdapter(max_retries=3)` mounted), `subclass` (a subclass of `GraftpunkSession` defined in the generator; expectation records its class path), `flatfile` (a nodriver pickle placed at `<name>.session.pickle` with no `metadata.json`), and `selenium` where it could be generated.
- Produces: `<case>.expected.json` with keys `cookies` (list of dicts of all 17 `http.cookiejar.Cookie` constructor arguments), `headers` (list of pairs), `header_roles`, `cached_tokens`, `account`, `class` (qualified name), `recordable` (bool, filled in Task 4).
- Produces for later tasks: `tests/unit/session_compat_cases.py` with `iter_cases() -> Iterator[CompatCase]` where `CompatCase` has `version`, `case`, `encrypted: bytes`, `key: bytes`, `expected: dict`.

- [ ] **Step 1: Write `scripts/session_compat.py generate`.** The generator code that runs inside the released version builds each session with placeholder data: cookies `sid` (domain `.myshop.example`, secure, `expires=1790000000`), `pref` (`expires=None`, value `café`, `rest={"HttpOnly": None}`); headers `User-Agent: compat/1`, `Accept: */*`. It calls that version's `cache_session` against a local storage directory under `--out` with `GRAFTPUNK_CONFIG_DIR` set there, then copies out the encrypted payload and writes the expectation. For nodriver it builds the object with `BrowserSession.__new__` and `__setstate__` of a nodriver state dict (no browser). Usage line and a refusal to write outside `--out`.
- [ ] **Step 2: Write the `read` subcommand.** It runs `load_session_for_api(NAME)` in the released version against `--store`.
- [ ] **Step 3: Generate the non-selenium fixtures for all four versions and commit them** with one shared test key (`tests/fixtures/session_compat/key`, generated with `Fernet.generate_key()`; state in the README that it protects only synthetic data).
- [ ] **Step 4: Write the baseline test**

```python
@pytest.mark.parametrize("case", list(iter_cases()), ids=lambda c: f"{c.version}-{c.case}")
def test_a_released_versions_session_loads_for_api(case):
    api = load_session_for_api_from_bytes(case.encrypted, case.key)
    assert cookie_dicts(api.cookies) == case.expected["cookies"]
    assert getattr(api, "_gp_header_roles", {}) == case.expected["header_roles"]
    assert token_dicts(api) == case.expected["cached_tokens"]
```

and a second, skipped without the browser packages, that loads each through `load_session` from a local store laid out as the fixture's version wrote it and asserts `type(obj).__qualname__` ends with `case.expected["class"]`.

- [ ] **Step 5: Generate the selenium fixtures** on this machine, one headless launch per version with `--selenium` (the generator passes `--use-mock-keychain --password-store=basic`). If a version cannot be launched, omit its selenium fixture and record the version and the error in `tests/fixtures/session_compat/README.md`.
- [ ] **Step 6: Run** `uv run -p 3.12 --all-extras pytest -q -rf tests/unit/test_session_compat.py`. Expected: PASS for every case on main's code. A failure here is a finding about today's behaviour: report it before continuing.
- [ ] **Step 7: Commit** `test: session fixtures written by released versions, and the script that makes them`.

### Task 2: `session_riders`, with pickled class paths frozen

**Files:**
- Create: `src/graftpunk/session_riders.py`
- Modify: `src/graftpunk/tokens.py` (definitions removed, names re-exported), and every graftpunk module that imports the moved names from `tokens` (`session.py`, `cache.py`, `graftpunk_session.py`, and others `grep -rn "from graftpunk.tokens import" src` finds)
- Test: `tests/unit/test_session_format_modules.py`

**Interfaces:**
- Produces in `graftpunk.session_riders`: `CachedToken` (same dataclass, `__module__ = "graftpunk.tokens"`), `_CACHE_ATTR`, `_CSRF_TOKENS_ATTR`, `_CSRF_ORIGIN_ATTR`, `_HEADER_ROLES_ATTR`, `_HEADER_ROLE_ORIGINS_ATTR`, `SESSION_RIDER_ATTRS`, `SESSION_RIDER_DEFAULTS`, `_MEMORY_ONLY_SESSION_RIDER_ATTRS`, `PICKLED_SESSION_RIDER_ATTRS`. It imports from graftpunk only `GP_SESSION_NAME_ATTR` and `GP_ACCOUNT_ATTR` from `graftpunk.session_identity`.
- Produces for Task 4 to 6: `assert_leaf_imports(module_name: str, allowed: set[str])` in the test module, which parses the module's source with `ast` and asserts every `graftpunk.*` import is in `allowed`, then imports the module in a subprocess and asserts none of `selenium`, `requestium`, `nodriver`, `httpie` is in `sys.modules`.

- [ ] **Step 1: Write the failing tests**

```python
def test_session_riders_is_a_leaf():
    assert_leaf_imports("graftpunk.session_riders", {"graftpunk.session_identity"})

def test_a_pickle_with_cached_tokens_names_only_the_frozen_paths():
    payload = dill.dumps(session_with_tokens())  # a nodriver BrowserSession built without a browser
    names = pickled_global_names(payload)        # via pickletools.genops
    assert "graftpunk.tokens CachedToken" in names
    assert not any("session_riders" in n for n in names)

def test_tokens_still_exports_every_moved_name():
    for name in MOVED_NAMES:
        assert getattr(graftpunk.tokens, name) is getattr(graftpunk.session_riders, name)
```

- [ ] **Step 2: Run** `pytest tests/unit/test_session_format_modules.py -q`. Expected: FAIL, `No module named 'graftpunk.session_riders'`.
- [ ] **Step 3: Move the definitions**, switch graftpunk's own imports to `session_riders`, and leave in `tokens.py` a re-export block under a comment: "Part of the stored session format: a pickle names `graftpunk.tokens.CachedToken`. Guarded by test_session_format_modules."
- [ ] **Step 4: Run** the new tests and `tests/unit/test_session_compat.py`. Expected: PASS.
- [ ] **Step 5: Gate, then commit** `refactor: session riders and CachedToken in a browser-free module, pickled paths unchanged`.

### Task 3: `session_legacy`: the state dict out of any release's pickle

**Files:**
- Create: `src/graftpunk/session_legacy.py`
- Test: `tests/unit/test_session_legacy.py`

**Interfaces:**
- Produces: `FROZEN_CLASS_PATHS: Mapping[tuple[str, str], object]` (module, qualname to replacement: the three session classes to the capture placeholder, `("graftpunk.tokens", "CachedToken")` to `session_riders.CachedToken`).
- Produces: `SESSION_CLASS_PATHS: Mapping[tuple[str, str], str]` mapping the three session class paths to their kind (`"browser"`, `"api"`, `"requests"`).
- Produces: `state_from_pickle(decrypted: bytes) -> LegacyState`, a frozen dataclass with `kind: str | None` (None when the pickled class is not one of the three, a subclass included), `class_path: str`, `state: dict[str, Any]`, `unresolved: tuple[str, ...]` (class paths that had to be stubbed because they could not be imported).
- Imports from graftpunk only `graftpunk.session_riders`.

- [ ] **Step 1: Write the failing tests**

```python
def test_session_legacy_is_a_leaf():
    assert_leaf_imports("graftpunk.session_legacy", {"graftpunk.session_riders"})

@pytest.mark.parametrize("case", recordable_cases())
def test_the_state_dict_has_the_names_getstate_wrote(case):
    legacy = state_from_pickle(decrypt(case))
    assert legacy.state["session_name"]  # never "_session_name"
    assert cookie_dicts(legacy.state["cookies"]) == case.expected["cookies"]

def test_a_subclass_has_no_kind():
    assert state_from_pickle(decrypt(case_named("1.44.0", "subclass"))).kind is None

def test_reading_tokens_never_imports_graftpunk_tokens():
    # subprocess: sys.modules["graftpunk.tokens"] = None before the read
    assert run_isolated_read("1.44.0", "tokens") == expected_token_dicts
```

- [ ] **Step 2: Run** to see `No module named 'graftpunk.session_legacy'`.
- [ ] **Step 3: Implement** a `dill.Unpickler` subclass whose `find_class` consults `FROZEN_CLASS_PATHS` first, then `super().find_class`, then on `ImportError` returns a stub class and records the path. The capture placeholder's `__setstate__` stores the state dict as given and also accepts the `(dict, slotstate)` tuple `_Stub` handles today.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Gate, then commit** `feat: read a session's state dict from a legacy pickle without importing its classes`.

### Task 4: `session_record`: the field table and the record codec

**Files:**
- Create: `src/graftpunk/session_record.py`
- Test: `tests/unit/test_session_record.py`
- Modify: `tests/fixtures/session_compat/*/*.expected.json` (fill `recordable`)

**Interfaces:**
- Produces: `SessionFormat = Literal["pickle", "record"]`; `RECORD_VERSION = 1`.
- Produces: `classify(decrypted: bytes) -> Record | LegacyBytes`. `Record` is a frozen dataclass: `version: int`, `kind: str`, `fields: dict[str, Any]` (known top-level keys), `extra: dict[str, Any]` (unknown top-level keys, untouched). `LegacyBytes` wraps the bytes. Raises `NewerSessionRecord(version)` (a `GraftpunkError`, message names the version and says to upgrade graftpunk) for `version > RECORD_VERSION`, and `InvalidSessionRecord(reason)` for JSON that is not a valid record.
- Produces: `FIELDS: tuple[Field, ...]`, each with `state_key`, `record_path` (tuple of keys), `kinds: frozenset[str]`, `encode`, `decode`; and `PICKLE_ONLY: tuple[PickleOnly, ...]`, each with `state_key`, `kinds`, `at_default: Callable[[Any], bool]`. The three selenium predicates sit together under a comment that they track requestium internals.
- Produces: `state_to_record(kind: str, state: Mapping[str, Any], extra: Mapping[str, Any] | None = None) -> bytes | Unrecordable`, where `Unrecordable` has `key: str` and `reason: str`.
- Produces: `record_to_state(record: Record) -> dict[str, Any]`.
- Produces: `kind_of(session: object) -> str | None`, comparing `type(session).__module__` and `__qualname__` with `SESSION_CLASS_PATHS` from `session_legacy`. Imports no class.
- Imports from graftpunk only `session_riders`, `session_legacy` (for `SESSION_CLASS_PATHS`), `cookies`, `exceptions`, `logging`. Ruling: the spec's allowlist for `session_record` does not name `session_legacy`. The three session class paths must exist once, the frozen legacy module needs them and may not import the record codec, so the record codec imports them from the legacy module. Cost if wrong: one import line and one allowlist entry.
- Record key names: top level `graftpunk_session`, `kind`, `backend_type`, `use_stealth`, `session_name`, `current_url`, `headers`, `cookies`, `auth`, `riders` (`cached_tokens`, `header_roles`, `header_role_origins`, `account`), `requests` (`params`, `verify`, `proxies`, `cert`, `max_redirects`, `trust_env`, `stream`), `selenium` (`webdriver_path`, `default_timeout`, `last_requests_url`).

- [ ] **Step 1: Write the failing tests**

```python
def test_session_record_is_a_leaf():
    assert_leaf_imports("graftpunk.session_record", {
        "graftpunk.session_riders", "graftpunk.session_legacy",
        "graftpunk.cookies", "graftpunk.exceptions", "graftpunk.logging"})

@pytest.mark.parametrize("kind,session", live_sessions_of_each_kind())
def test_every_key_getstate_emits_has_a_row(kind, session):
    known = {f.state_key for f in FIELDS if kind in f.kinds} | {p.state_key for p in PICKLE_ONLY if kind in p.kinds}
    assert set(session.__getstate__()) <= known

@pytest.mark.parametrize("case", recordable_cases())
def test_state_survives_the_record(case):
    legacy = state_from_pickle(decrypt(case))
    payload = state_to_record(legacy.kind, legacy.state)
    assert comparable(record_to_state(classify(payload))) == comparable(legacy.state)

@pytest.mark.parametrize("version,case", [("1.44.0", "adapter"), ("1.44.0", "subclass")])
def test_what_a_record_cannot_hold_is_refused_by_name(version, case): ...
    # adapter -> Unrecordable(key="adapters"); subclass has kind None and is never offered

def test_each_default_predicate_holds_for_the_installed_libraries():
    for obj in (requests.Session(), GraftpunkSession()):
        assert all(p.at_default(obj.__getstate__()[p.state_key]) for p in PICKLE_ONLY if p.state_key in obj.__getstate__())

@pytest.mark.skipif(not HAVE_REQUESTIUM, reason="needs requestium")
def test_the_selenium_predicates_track_installed_requestium():
    s = requestium.Session()
    assert hasattr(requestium.Session, "_start_chrome_browser")
    assert predicate("_driver_initializer")(s._driver_initializer)
    assert predicate("webdriver_options")(s.webdriver_options)

def test_a_newer_record_is_refused_by_version():
    with pytest.raises(NewerSessionRecord, match="2"):
        classify(json.dumps({"graftpunk_session": 2, "kind": "browser"}).encode())

@pytest.mark.parametrize("payload", [b"[]", b'{"graftpunk_session": 1}', b'{"graftpunk_session": 1, "kind": "browser", "cookies": [{}]}'])
def test_json_that_is_not_a_record_is_invalid(payload):
    with pytest.raises(InvalidSessionRecord):
        record_to_state(classify(payload))

@pytest.mark.parametrize("mutate,key", [(token_ttl_inf, "_gp_cached_tokens"), (bytes_header_value, "headers")])
def test_values_json_cannot_hold_make_the_session_unrecordable(mutate, key): ...

def test_an_empty_session_and_an_awkward_cookie_round_trip(): ...
    # no cookies, no headers; and expires=None, value "café", rest={"HttpOnly": None}

def test_unknown_top_level_keys_come_back_in_extra_and_go_out_unchanged():
    rec = classify(record_with(extra={"future": {"a": 1}}))
    assert rec.extra == {"future": {"a": 1}}
    assert json.loads(state_to_record(rec.kind, record_to_state(rec), rec.extra))["future"] == {"a": 1}

def test_a_pickle_is_never_unpickled_by_classify():
    assert isinstance(classify(pickle_that_calls_a_marker()), LegacyBytes) and not MARKER.called
```

- [ ] **Step 2: Run** to see `No module named 'graftpunk.session_record'`.
- [ ] **Step 3: Implement.** `classify` tests the first byte for `{`, parses, and validates `graftpunk_session` and `kind` only; field validation happens in `record_to_state`, which raises `InvalidSessionRecord` on a missing or mistyped field. `json.dumps(..., allow_nan=False)`; a `ValueError` or `TypeError` from a field's encoder becomes `Unrecordable(key=state_key)`. The predicates are the ones the spec lists under "What the record cannot hold"; the cookie jar's policy is a `PICKLE_ONLY` row keyed `cookies` whose predicate reads the jar's policy and compares `graftpunk.cookies._settings(policy)` with a fresh policy's.
- [ ] **Step 4: Run** the tests; then run the compat generator's expectation update so each `expected.json` records `recordable`, and print the unrecordable count per fixture. Report the counts in the commit body.
- [ ] **Step 5: Gate, then commit** `feat: the session record: one field table, and the codec built from it`.

### Task 5: `session_view`: `StoredSession`

**Files:**
- Create: `src/graftpunk/session_view.py`
- Test: `tests/unit/test_session_view.py`

**Interfaces:**
- Produces: `StoredSessionLike` (Protocol): `cookies`, `headers`, `session_name: str`, `current_url: str`, and each attribute in `SESSION_RIDER_ATTRS`, readable and settable.
- Produces: `StoredSession(kind: str, state: dict[str, Any], extra: dict[str, Any] | None = None)` with `kind`, `extra`, `__getstate__() -> dict[str, Any]` returning only the table's keys for its kind, and `classmethod from_record(record: Record)` and `from_legacy(legacy: LegacyState)`. `cookies` is a `requests.cookies.RequestsCookieJar`.
- Imports from graftpunk only `session_record`, `session_riders`, `cookies`.

- [ ] **Step 1: Write the failing tests**

```python
def test_session_view_is_a_leaf(): ...

@pytest.mark.parametrize("case", recordable_cases())
def test_the_view_and_the_real_object_give_the_same_metadata(case):
    view = StoredSession.from_legacy(state_from_pickle(decrypt(case)))
    real = unpickle_real(case)   # skipped without the browser packages for browser kinds
    assert _extract_session_metadata(view, "s") == _extract_session_metadata(real, "s")
    assert set(view.__getstate__()) == set(real.__getstate__()) - PICKLE_ONLY_KEYS

def test_setting_a_name_on_a_kind_with_no_name_writes_no_name():
    view = StoredSession("requests", requests_state()); view.session_name = "x"
    assert "session_name" not in view.__getstate__()

def test_a_memory_only_rider_can_be_set_and_is_not_stored():
    view.__setattr__(_CSRF_TOKENS_ATTR, {"t": "v"}); assert _CSRF_TOKENS_ATTR not in view.__getstate__()
```

- [ ] **Step 2: Run** to see the import fail. **Step 3: Implement.** **Step 4: Run**, expected PASS.
- [ ] **Step 5: Gate, then commit** `feat: StoredSession, a browser-free view of a stored session`.

### Task 6: Readers go through `classify`

**Files:**
- Modify: `src/graftpunk/cache.py` (`load_session`, `_load_session_with_metadata`, `_api_session_from_session`, the three API loaders; `_Stub`, `_BrowserFreeUnpickler` and `_deserialize_browserfree` removed), `src/graftpunk/exceptions.py`
- Test: `tests/unit/test_session_readers.py`; existing `tests/unit/test_browserfree_session_load.py` and `tests/unit/test_cache.py` stay green

**Interfaces:**
- Produces in `graftpunk.exceptions`: `RealSessionClassUnavailable(ImportError)` carrying today's install-hint text; `NewerSessionRecord` and `InvalidSessionRecord` are re-exported from here.
- Produces in `cache.py`: `_unpickle_whole(legacy: LegacyBytes, *, name: str) -> Any` (today's `pickle.loads` and its `ModuleNotFoundError` handling, raising `RealSessionClassUnavailable`); `_restore_record(record: Record) -> Any` (`cls.__new__(cls)` then `__setstate__(record_to_state(record))`, class imported by kind; kind `browser` raises `RealSessionClassUnavailable` when `graftpunk.session` cannot be imported).
- `_api_session_from_session(source)` is unchanged in signature and is the only builder; the API loaders hand it a `StoredSession`.
- Error mapping in every reader: `InvalidSessionRecord` becomes `SessionExpiredError` naming the session (as a corrupt pickle does today); `NewerSessionRecord` propagates.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("case", recordable_cases())
def test_a_record_loads_for_api_exactly_as_its_pickle_did(case):
    record_bytes = encrypt(as_record(case), case.key)
    assert api_fingerprint(load_session_for_api_from_bytes(record_bytes, case.key)) == \
           api_fingerprint(load_session_for_api_from_bytes(case.encrypted, case.key))

def test_api_loaders_do_not_import_graftpunk_session():   # subprocess, both formats
    assert "graftpunk.session" not in loaded_modules_after_api_load()

@pytest.mark.parametrize("kind,cls", [("api", GraftpunkSession), ("requests", requests.Session)])
def test_load_session_returns_the_class_that_was_saved(kind, cls, local_store): ...

def test_load_session_of_a_browser_record_without_the_browser_stack_gives_the_install_hint(no_browser_stack, local_store):
    with pytest.raises(ImportError, match="browser"): load_session("myshop")

def test_an_invalid_record_reads_as_an_expired_session(local_store):
    with pytest.raises(SessionExpiredError, match="myshop"): load_session_for_api("myshop")

def test_a_version_2_record_is_refused_by_every_reader(local_store):
    for reader in (load_session, load_session_for_api): 
        with pytest.raises(NewerSessionRecord): reader("myshop")
```

- [ ] **Step 2: Run** to see them fail (records are not understood yet).
- [ ] **Step 3: Implement.** `load_session_for_api` and `load_session_for_api_resolved` stop calling `load_session`: decrypt and checksum as today (factor today's code into `_decrypt_verified(name, key) -> tuple[bytes, SessionMetadata]`), `classify`, build a `StoredSession`, `_api_session_from_session`. The metadata account fallback they apply today stays where it is.
- [ ] **Step 4: Run** the new tests, `test_session_compat.py`, `test_browserfree_session_load.py`, `test_cache.py`. Expected: PASS.
- [ ] **Step 5: Gate, then commit** `feat: every session reader takes a record or a legacy pickle; API loads need no browser package`.

### Task 7: Fresh saves: the setting and the writer

**Files:**
- Modify: `src/graftpunk/config.py`, `src/graftpunk/cache.py` (`cache_session`), `src/graftpunk/cli/config_commands.py` (show the value)
- Test: `tests/unit/test_session_writers.py`

**Interfaces:**
- Produces: `GraftpunkSettings.session_format: SessionFormat = "pickle"` (`GRAFTPUNK_SESSION_FORMAT`).
- Produces in `cache.py`: `_encode_fresh(session: Any) -> bytes`; `_encode_as(session: Any, fmt: SessionFormat) -> bytes` (`StoredSession` with `record`, a real object with `pickle`; anything else `TypeError`); `_store(name: str, payload: bytes, session: Any, *, account_rule, key) -> str` (checksum, encrypt, metadata, `backend.save_session`, slot stamp; today's lines 319 to 352 of `cache_session`).
- `cache_session` is `_encode_fresh` then `_store`. Under `record`, a fallback to pickle logs one warning `session_not_recordable` with `key` and `reason`.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_default_still_writes_a_pickle(local_store):
    cache_session(nodriver_session(), "myshop"); assert stored_bytes("myshop")[:1] == b"\x80"

def test_record_setting_writes_a_record(local_store, monkeypatch):
    monkeypatch.setenv("GRAFTPUNK_SESSION_FORMAT", "record")
    cache_session(nodriver_session(), "myshop"); assert json.loads(stored_bytes("myshop"))["kind"] == "browser"

def test_an_unrecordable_session_is_pickled_with_one_warning(local_store, record_format, caplog):
    cache_session(session_with_custom_adapter(), "myshop")
    assert stored_bytes("myshop")[:1] == b"\x80" and warnings_named("session_not_recordable") == [{"key": "adapters"}]

def test_a_mistyped_format_fails_naming_the_choices(monkeypatch):
    monkeypatch.setenv("GRAFTPUNK_SESSION_FORMAT", "json")
    with pytest.raises(ValidationError, match="pickle.*record"): reset_and_get_settings()

def test_encode_as_refuses_a_view_as_a_pickle():
    with pytest.raises(TypeError): _encode_as(stored_view(), "pickle")

def test_cache_session_signature_is_unchanged():
    assert str(inspect.signature(cache_session)) == SIGNATURE_AT_1_44_0
```

- [ ] **Step 2: Run** to see them fail. **Step 3: Implement.** **Step 4: Run**, plus `test_session_compat.py` and `test_cache.py`. Expected: PASS.
- [ ] **Step 5: Gate, then commit** `feat: GRAFTPUNK_SESSION_FORMAT=record writes a session as a record; the default is unchanged`.

### Task 8: Write-backs keep the format they read

**Files:**
- Modify: `src/graftpunk/cache.py` (`update_session_cookies`, `copy_session`; `_load_session_with_metadata` callers)
- Test: `tests/unit/test_session_write_backs.py`; existing `tests/unit/test_session_migration*.py` stay green

**Interfaces:**
- Produces: `load_for_write_back(decrypted: bytes, *, name: str) -> tuple[Any, SessionFormat]`: `classify` first; a record gives `(StoredSession.from_record(...), "record")`; legacy bytes give `(_unpickle_whole(...), "pickle")`.
- `update_session_cookies` catches `RealSessionClassUnavailable` and logs warning `session_write_back_skipped` with `reason` and the hint "the next login, or a write-back from a full install, rewrites the session"; it lets `NewerSessionRecord` skip the same way with its own reason and writes nothing. `copy_session` lets both propagate. `copy_session` sets the name with `session.session_name = name` and drops the `hasattr(session, "_session_name")` probe.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("setting", ["pickle", "record"])
@pytest.mark.parametrize("stored", ["pickle", "record"])
def test_a_write_back_keeps_the_stored_format(setting, stored, local_store, monkeypatch):
    seed(stored); monkeypatch.setenv("GRAFTPUNK_SESSION_FORMAT", setting)
    update_session_cookies(api_with_new_cookie(), "myshop")
    assert format_of(stored_bytes("myshop")) == stored and "new" in cookie_names("myshop")
    copy_session("myshop", "myshop@alice"); assert format_of(stored_bytes("myshop@alice")) == stored

def test_an_unknown_key_survives_both_write_backs_even_when_the_setting_is_pickle(local_store):
    seed_record(extra={"future": {"a": 1}})
    update_session_cookies(api_with_new_cookie(), "myshop"); copy_session("myshop", "myshop@alice")
    assert all(json.loads(stored_bytes(n))["future"] == {"a": 1} for n in ("myshop", "myshop@alice"))

def test_a_version_2_record_is_left_byte_for_byte(local_store):
    before = seed_raw(version_2_record())
    update_session_cookies(api_with_new_cookie(), "myshop")
    with pytest.raises(NewerSessionRecord): copy_session("myshop", "myshop@alice")
    assert stored_bytes("myshop") == before

def test_without_the_browser_stack_a_record_write_back_works_and_a_pickle_one_is_skipped(no_browser_stack, local_store): ...
    # record: cookie written back; pickle: warning session_write_back_skipped, bytes unchanged; copy_session raises ImportError

def test_copy_renames_a_browser_record(local_store):
    seed("record"); copy_session("myshop", "myshop@alice")
    assert json.loads(stored_bytes("myshop@alice"))["session_name"] == "myshop@alice"
```

- [ ] **Step 2: Run** to see them fail. **Step 3: Implement** each write-back as: `load_for_write_back`, its existing mutation, `_encode_as(session, fmt)`, `_store`. `copy_session`'s read-back verification stays.
- [ ] **Step 4: Run** the new tests and the session-migration and cache suites. Expected: PASS.
- [ ] **Step 5: Gate, then commit** `feat: a session write-back keeps the format it read and never drops a record's unknown keys`.

### Task 9: graftpunk-lite and the old-reader check

**Files:**
- Modify: `src/graftpunk/cli/plugin_runtime.py`, `src/graftpunk/cli/http_commands.py` (the missing-browser-module handlers no longer need to fire on a session load; keep them for any other `ImportError` path they cover), `scripts/smoke_install.sh`, `.github/workflows/python-quality.yml`, `README.md` (the lite paragraph and the `load_session_for_api` note)
- Test: `tests/unit/test_lite_cli_hints.py` (update expectations)

**Interfaces:**
- Consumes: `scripts/session_compat.py read` from Task 1.

- [ ] **Step 1: Extend `scripts/smoke_install.sh`** (lite venv, no browser packages, a local `python -m http.server`-style fake that echoes the `Cookie` header): a YAML plugin command with a stored session returns the fake's response for a pickle fixture and for a record; a command that refreshes a cookie in a stored record writes it back and the next run sends it; the same refresh on a stored pickle prints the `session_write_back_skipped` warning and still exits 0; `gp <plugin> login` prints the install hint and exits 1.
- [ ] **Step 2: Run** `scripts/smoke_install.sh` against wheels built from the branch (`python scripts/build_lite.py --out-dir <scratch>/lite`, `uv build --out-dir <scratch>/full`). Expected: the new lines print `OK`; before Task 6 to 8 they would not.
- [ ] **Step 3: Add the CI job `old-reader`** to `python-quality.yml`: with the branch, cache a `tokens` session into a local store under the default setting, then `python scripts/session_compat.py read --graftpunk 1.44.0 --store $STORE --name myshop` must exit 0 and print the cookie names; then cache the same session with `GRAFTPUNK_SESSION_FORMAT=record` and the same command must exit 3 printing `SessionExpiredError`, and the store's bytes must be unchanged afterwards.
- [ ] **Step 4: Run the job's commands locally** and paste both outputs into the commit body.
- [ ] **Step 5: Update the README passages**: on lite, an API command or `gp http` with a stored session runs; `load_session_for_api` no longer needs `[browser]`; `login`, `gp observe` and `gp session export` still do; a write-back from lite works for a session stored as a record.
- [ ] **Step 6: Gate, then commit** `feat: plugin commands use a stored session on graftpunk-lite; CI proves 1.44.0 reads what the default writes`.

### Task 10: Docs and release 1.45.0

**Files:**
- Modify: `CHANGELOG.md`, `pyproject.toml`, `uv.lock`, `docs/HOW_IT_WORKS.md` (the session storage section), `CONTRIBUTING.md` (what `scripts/session_compat.py` checks, and to run `generate` before any change to the stored format or `BrowserSession.__getstate__`), `.claude-plugin/plugin.json` only if `scripts/check-skill-version.sh d0511e6c` asks for it
- Modify: issue #245 (comment the session half as shipped; the plugin-packaging half stays open with one line saying it needs its own short design)

- [ ] **Step 1: Write the 1.45.0 CHANGELOG section.** Added: `GRAFTPUNK_SESSION_FORMAT`, the record format, API loads and plugin commands on graftpunk-lite, `scripts/session_compat.py`. Changed: `load_session_for_api` no longer needs the browser packages. Upgrading, in this order: (1) nothing changes unless you set `GRAFTPUNK_SESSION_FORMAT=record`; (2) before any host writes records, every graftpunk that reads the same store must be 1.45.0 or later, because an older one reports a record as an expired session (it does not delete it); (3) a session becomes a record at its next login on a host that sets `record`, and a write-back never converts one; (4) sessions with a custom adapter, hooks, a subclass, or non-default selenium options stay pickles, with the warning `session_not_recordable`; (5) the new warnings and errors by name: `session_not_recordable`, `session_write_back_skipped`, `NewerSessionRecord`.
- [ ] **Step 2: Run** `scripts/check-skill-version.sh d0511e6c`, the full gate once more with `--cov=src/graftpunk`, and the changed test files under 3.11 and 3.14.
- [ ] **Step 3: Commit** `release: 1.45.0`, push, and open the PR with the verification commands and their output, the per-fixture unrecordable counts, and "Refs #245".
