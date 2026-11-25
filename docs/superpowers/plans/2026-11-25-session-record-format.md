---
type: plan
---

# Sessions stored as plain data: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** graftpunk 1.45.0 reads a saved session as a versioned JSON record or as a legacy pickle, writes pickles by default and records when `GRAFTPUNK_SESSION_FORMAT=record`, and loads either for API use without the browser packages.

**Architecture:** Four browser-free leaf modules own the stored format (`session_riders`, `session_record`, `session_legacy`, `session_view`); `cache.py` keeps storage and the rollout policy in private functions. Every class already reduces itself to a state dict through `__getstate__` and restores through `__setstate__`; the record is that dict as JSON, described by one field table. Committed fixtures written by released versions prove old sessions still load.

**Tech Stack:** Python 3.11 to 3.14, requests, dill (imported as `pickle` in `cache.py`), cryptography (Fernet), pydantic-settings, pytest with xdist, uv.

**Spec:** `docs/superpowers/specs/2026-11-24-session-record-format-design.md`, including its "Amendments from plan validation" section. Read it first; this plan names files, signatures and tests and does not restate the spec's reasoning.

## Global Constraints

- Nothing that works on 1.44.0 may break. A pickle written by this branch under the default setting must load on 1.44.0, tokens included.
- Frozen pickled class paths: `graftpunk.session.BrowserSession`, `graftpunk.graftpunk_session.GraftpunkSession`, `graftpunk.tokens.CachedToken`. The cookie policy pickles as `http.cookiejar.DefaultCookiePolicy`.
- Public signatures unchanged from 1.44.0: `cache_session(session, session_name=None, *, account_rule=..., key=None)`, `load_session`, `load_session_for_api`, `load_session_for_api_resolved`, `load_session_for_api_from_bytes(encrypted, *, key=None)`, `update_session_cookies(api_session, session_name, *, key=None)`, `copy_session(source, dest, *, account_identifier, key=None)`. Task 7 pins each with `inspect.signature` against the 1.44.0 text.
- `graftpunk.exceptions` keeps importing nothing from graftpunk. New exception types are defined there.
- The four leaf modules import no session class and none of `selenium`, `requestium`, `nodriver`, `httpie`. Allowed graftpunk imports: `session_riders`: `session_identity`. `session_legacy`: `session_riders`. `session_record`: `session_riders`, `cookies`, `exceptions`, `logging`. `session_view`: `session_record`, `session_riders`, `cookies`.
- `src/graftpunk/plugins/login_engine.py`, `login_settle.py` and `_warn_login_page_unreadable` are not edited. `login_engine.py` keeps importing the moved rider names from `graftpunk.tokens`.
- Public repo: placeholder hosts only (`myshop.example`, `idp.example.com`), generated test keys, no real site, plugin or account names. No em or en dashes. No attribution lines in commits.
- Settings are a cached singleton: a test that sets `GRAFTPUNK_SESSION_FORMAT` calls `graftpunk.config.reset_settings()` after `monkeypatch.setenv` and again in teardown.
- Gate, green after the last edit of every task: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4` (never `-n auto`), `uvx ruff check .`, `uvx ruff format --check src tests`, `uvx ty@0.0.75 check src/ --python .venv`. Changed test files also under 3.11 and 3.14 in scratch venvs (`UV_PROJECT_ENVIRONMENT=<scratch>/venv3xx`); the worktree `.venv` stays on 3.12.
- Live Chrome only in Task 1 Step 5: headless, one launch per fixture version, never in a loop.
- One commit per task, pushed to the PR branch as it lands.

## Rulings

Decisions this plan makes where the spec left a choice or where the code forced one. Each is also recorded in the spec's amendments section.

1. **Selenium-backend sessions stay pickles in release 1.** A record of one would have to rebuild `_driver_initializer`, a `functools.partial` of the session's own bound method, on restore. The spec's open-risk clause provides for this ("selenium sessions stay on pickle in release 1 and the design is revisited before release 2"). `state_to_record` returns `Unrecordable(key="_backend_type", reason="selenium sessions are stored as pickles in this release")` for kind `browser` with `_backend_type == "selenium"`. They still load through every loader, which Task 1 and Task 3 prove with a selenium fixture. Cost if wrong: selenium users get no record until a later release; nothing breaks.
2. **The three session class paths live in `session_riders`** (`SESSION_CLASS_PATHS`), which both `session_record` and `session_legacy` may import, so neither imports the other.
3. **`classify` validates the whole record.** The mapping of an invalid record to `SessionExpiredError` then has one owner, `_classified` in `cache.py`.
4. **The cookie jar's policy check is the cookie codec's refusal**, through a new public `graftpunk.cookies.policy_is_default(policy)`, not a separate table row.
5. **`record_to_state` returns the full state `__setstate__` needs**: each pickle-only key the kind has is put back at its default by the row's `default` factory, because `requests.Session.__setstate__` sets only the keys it is given.

## Review Focus

Inputs the spec is silent on that a user will hit. Each has a test in the task named.

1. A record that decrypts to JSON but is not a valid record (not an object, no `kind`, a `cookies` entry missing `name`): every reader raises `SessionExpiredError` naming the session, as a corrupt pickle does today, never a `KeyError` traceback; `update_session_cookies` skips it with a warning. (Tasks 4, 6, 8)
2. A cached token whose `ttl` or `extracted_at` is `inf` or `nan`, or a header whose value is `bytes`: not valid JSON, so the session is unrecordable with a warning naming the key and is written as a pickle; nothing raises. (Task 4)
3. A session with no cookies and no headers, and a cookie with `expires=None`, a non-ASCII value and a `rest` entry: each round-trips state dict to record to state dict unchanged. (Task 4)
4. `GRAFTPUNK_SESSION_FORMAT=json` (a typo): settings validation fails at first use with a message naming `pickle` and `record`; no session is written. (Task 7)
5. A write-back of a record on a full install whose setting is `pickle`: the stored bytes stay a record and its unknown top-level key survives. (Task 8)

---

### Task 1: Compat fixtures from released versions, and a baseline test

The safety net comes first: fixtures written by real releases, and a test that pins what today's loaders return for each, before any loader changes.

**Files:**
- Create: `scripts/session_compat.py`
- Create: `tests/fixtures/session_compat/key`, `tests/fixtures/session_compat/README.md`, `tests/fixtures/session_compat/<version>/<case>.enc` and `<case>.expected.json`
- Create: `tests/unit/session_compat_cases.py` (every helper shared by Tasks 1 to 8), `tests/unit/test_session_compat.py`
- Move: `tests/fixtures/browserfree_session.{enc,key}` to `tests/fixtures/session_compat/legacy/` (it keeps its own key; add `browserfree_session.expected.json`); update its readers `scripts/smoke_install.sh`, `tests/unit/test_browserfree_session_load.py`, `tests/unit/test_lite_cli_hints.py`, `tests/unit/test_cookies.py`
- Delete: `scripts/gen_browserfree_fixture.py`; modify `tests/fixtures/README.md` and `CONTRIBUTING.md` to point at the new script

**Interfaces:**
- Produces: `scripts/session_compat.py generate --graftpunk VERSION --out DIR [--selenium]` and `read --graftpunk VERSION --store DIR --name NAME` (exit 0 and prints the cookie names when `load_session_for_api(NAME)` works in that version; exit 3 and prints the exception class name when it raises). Both run the released version in a scratch venv made with `uv venv` under a temp directory, never the repo `.venv`. Both set `GRAFTPUNK_CONFIG_DIR` to a directory under `--out` or `--store` and write the shared test key to `<config_dir>/.session_key`, because 1.21.0's `cache_session` and `load_session_for_api` take no `key=`.
- Produces: fixture cases per version (`1.21.0`, `1.40.1`, `1.43.1`, `1.44.0`):
  - `nodriver`: a `BrowserSession` built with `__new__` and `__setstate__` of a nodriver state dict (no browser).
  - `requests`: a plain `requests.Session`. `api`: a `GraftpunkSession`.
  - `tokens`: nodriver with two cached tokens and header roles. `account`: nodriver cached under `myshop@alice` with an account identifier.
  - `adapter`: `requests.Session` with `HTTPAdapter(max_retries=3)` mounted on `https://`.
  - `subclass`: a subclass of `GraftpunkSession` defined in the generator's `__main__`, which dill embeds by value; the expectation records `__main__.<name>`.
  - `flatfile`: the `nodriver` pickle placed at `<name>.session.pickle` with no `metadata.json`, the layout `LocalSessionStorage._load_legacy_session` reads. No listed release writes it, so the generator places it.
  - `selenium`: see Step 5.
- Produces: `<case>.expected.json` with keys `cookies` (list of dicts of all 17 `http.cookiejar.Cookie` constructor arguments), `api_headers` (the headers an API load keeps: the saved headers minus any equal to `requests.utils.default_headers()`), `header_roles`, `cached_tokens`, `account`, `class` (qualified name), `recordable` (bool, filled in Task 4).
- Produces in `tests/unit/session_compat_cases.py`: `CompatCase` (`version`, `case`, `encrypted: bytes`, `key: bytes`, `expected: dict`); `iter_cases()`; `case_named(version, case)`; `recordable_cases(kind: str | None = None)`; `decrypt(case) -> bytes`; `cookie_dicts(jar) -> list[dict]`; `token_dicts(session) -> dict`; `local_store_with(case, name="myshop")` (a pytest fixture factory that lays the case out in a temp local store as its version wrote it and points `GRAFTPUNK_CONFIG_DIR` at it); `assert_leaf_imports(module_name: str, allowed: set[str])`; `pickled_global_names(payload: bytes) -> set[str]`.

- [ ] **Step 1: Write `scripts/session_compat.py generate`.** Placeholder data for every case: cookies `sid` (domain `.myshop.example`, secure, `expires=1790000000`) and `pref` (`expires=None`, value `café`, `rest={"HttpOnly": None}`); headers `User-Agent: compat/1`, `Accept: */*`, `X-Compat: 1`. Usage line; refuses to write outside `--out`.
- [ ] **Step 2: Write the `read` subcommand.**
- [ ] **Step 3: Generate the non-selenium fixtures for all four versions and commit them** with the shared key `tests/fixtures/session_compat/key` (`Fernet.generate_key()`; the README says it protects only synthetic data).
- [ ] **Step 4: Write the helpers and the baseline tests**

```python
@pytest.mark.parametrize("case", list(iter_cases()), ids=lambda c: f"{c.version}-{c.case}")
def test_a_released_versions_session_loads_for_api_from_bytes(case):
    api = load_session_for_api_from_bytes(case.encrypted, key=case.key)
    assert cookie_dicts(api.cookies) == case.expected["cookies"]
    assert [[k, v] for k, v in api.headers.items() if [k, v] in case.expected["api_headers"]] == case.expected["api_headers"]
    assert getattr(api, "_gp_header_roles", {}) == case.expected["header_roles"]
    assert token_dicts(api) == case.expected["cached_tokens"]

@pytest.mark.parametrize("loader", [load_session_for_api, load_session_for_api_resolved])
@pytest.mark.parametrize("case", list(iter_cases()), ids=...)
def test_a_released_versions_session_loads_from_a_local_store(case, loader, local_store_with):
    local_store_with(case)
    api = loader("myshop", key=case.key)   # load_session_for_api_resolved returns (session, name); unpack
    # same four assertions, plus:
    assert getattr(api, "_gp_account_identifier", None) == case.expected["account"]

@pytest.mark.parametrize("case", list(iter_cases()), ids=...)
def test_load_session_returns_what_was_saved(case, local_store_with):   # skipped without the browser packages for BrowserSession cases
    local_store_with(case)
    obj = load_session("myshop", key=case.key)
    assert f"{type(obj).__module__}.{type(obj).__qualname__}".endswith(case.expected["class"].split(".")[-1])
    assert cookie_dicts(obj.cookies) == case.expected["cookies"]
```

`pickled_global_names` walks `pickletools.genops`: for each `STACK_GLOBAL` it joins the two preceding string pushes as `"module name"`, and for `GLOBAL` it takes the argument (dill writes protocol 4, where class references are `STACK_GLOBAL`).

- [ ] **Step 5: Generate the selenium fixtures.** Preferred: one headless launch per version with `--selenium`. Released `create_stealth_driver` takes no extra Chrome arguments and already passes `--password-store=basic`; the generator wraps `undetected_chromedriver.ChromeOptions` inside the scratch venv to add `--use-mock-keychain` before constructing `BrowserSession`, and `GRAFTPUNK_CONFIG_DIR` under `--out` keeps the Chrome profile there. If a version cannot be launched, generate that version's `selenium` case without a browser instead (`BrowserSession.__new__`, then `requestium.Session.__init__(obj)`, which starts no driver, then the selenium attributes `__getstate__` reads) and record in `tests/fixtures/session_compat/README.md` which versions are synthetic and the launch error.
- [ ] **Step 6: Run** `uv run -p 3.12 --all-extras pytest -q -rf tests/unit/test_session_compat.py`. Expected: PASS on main's code for every case. A failure is a finding about today's behaviour: report it with the case and the error before continuing, and mark that case `xfail(strict=True)` with the reason so later tasks cannot regress or silently fix it.
- [ ] **Step 7: Commit** `test: session fixtures written by released versions, and the script that makes them`.

### Task 2: `session_riders`, with pickled class paths frozen

**Files:**
- Create: `src/graftpunk/session_riders.py`
- Modify: `src/graftpunk/tokens.py` (definitions removed, names re-exported); every graftpunk module that imports the moved names from `tokens` (`grep -rn "from graftpunk.tokens import" src`) except `src/graftpunk/plugins/login_engine.py`
- Test: `tests/unit/test_session_format_modules.py`

**Interfaces:**
- Produces in `graftpunk.session_riders`: `CachedToken` (same dataclass, `__module__ = "graftpunk.tokens"`), `_CACHE_ATTR`, `_CSRF_TOKENS_ATTR`, `_CSRF_ORIGIN_ATTR`, `_HEADER_ROLES_ATTR`, `_HEADER_ROLE_ORIGINS_ATTR`, `SESSION_RIDER_ATTRS`, `SESSION_RIDER_DEFAULTS`, `_MEMORY_ONLY_SESSION_RIDER_ATTRS`, `PICKLED_SESSION_RIDER_ATTRS`, and `SESSION_CLASS_PATHS: Mapping[tuple[str, str], str]` = `{("graftpunk.session", "BrowserSession"): "browser", ("graftpunk.graftpunk_session", "GraftpunkSession"): "api", ("requests.sessions", "Session"): "requests"}`. It imports from graftpunk only `GP_SESSION_NAME_ATTR` and `GP_ACCOUNT_ATTR` from `graftpunk.session_identity`.

- [ ] **Step 1: Write the failing tests**

```python
def test_session_riders_is_a_leaf():
    assert_leaf_imports("graftpunk.session_riders", {"graftpunk.session_identity"})

def test_a_pickle_with_cached_tokens_names_only_the_frozen_paths():
    payload = dill.dumps(nodriver_session_with_tokens())
    names = pickled_global_names(payload)
    assert "graftpunk.tokens CachedToken" in names and "graftpunk.session BrowserSession" in names
    assert not any("session_riders" in n for n in names)

def test_tokens_still_exports_every_moved_name():
    for name in MOVED_NAMES:
        assert getattr(graftpunk.tokens, name) is getattr(graftpunk.session_riders, name)

def test_the_login_engine_was_not_edited():
    assert git_diff_names("d0511e6c") .isdisjoint({"src/graftpunk/plugins/login_engine.py", "src/graftpunk/plugins/login_settle.py"})
```

- [ ] **Step 2: Run** `pytest tests/unit/test_session_format_modules.py -q`. Expected: FAIL, `No module named 'graftpunk.session_riders'`.
- [ ] **Step 3: Move the definitions**, switch the other modules' imports to `session_riders`, and leave in `tokens.py` a re-export block under the comment: "Part of the stored session format: a pickle names `graftpunk.tokens.CachedToken`. Guarded by test_session_format_modules."
- [ ] **Step 4: Run** the new tests and `tests/unit/test_session_compat.py`. Expected: PASS.
- [ ] **Step 5: Gate, then commit** `refactor: session riders and CachedToken in a browser-free module, pickled paths unchanged`.

### Task 3: `session_legacy`: the state dict out of any release's pickle

**Files:**
- Create: `src/graftpunk/session_legacy.py`
- Test: `tests/unit/test_session_legacy.py`

**Interfaces:**
- Produces: `FROZEN_CLASS_PATHS: Mapping[tuple[str, str], object]`: each key of `session_riders.SESSION_CLASS_PATHS` mapped to the capture placeholder, plus `("graftpunk.tokens", "CachedToken")` mapped to `session_riders.CachedToken`.
- Produces: `state_from_pickle(decrypted: bytes) -> LegacyState`, a frozen dataclass: `kind: str | None` (from `SESSION_CLASS_PATHS`; `None` for any other class, a subclass included), `class_path: str`, `state: dict[str, Any]`, `unresolved: tuple[str, ...]` (class paths stubbed because they could not be imported).
- The capture placeholder stores the state dict given to `__setstate__` (and accepts the `(dict, slotstate)` tuple `cache._Stub` accepts today), and answers any other attribute lookup during unpickling with an inert marker, because a selenium pickle holds a `functools.partial` of the session's own bound method, which unpickles by `getattr` on the half-built object.
- Imports from graftpunk only `graftpunk.session_riders`.

- [ ] **Step 1: Write the failing tests**

```python
def test_session_legacy_is_a_leaf():
    assert_leaf_imports("graftpunk.session_legacy", {"graftpunk.session_riders"})

@pytest.mark.parametrize("case", list(iter_cases()), ids=...)
def test_every_fixture_gives_its_state(case):
    legacy = state_from_pickle(decrypt(case))
    assert cookie_dicts(legacy.state["cookies"]) == case.expected["cookies"]

@pytest.mark.parametrize("case", cases_of_kind("browser"), ids=...)
def test_a_browser_state_has_the_name_getstate_wrote(case):
    assert state_from_pickle(decrypt(case)).state["session_name"]      # never "_session_name"

@pytest.mark.parametrize("case", cases_of_kind("api", "requests"), ids=...)
def test_api_and_requests_states_hold_no_name(case):
    assert "session_name" not in state_from_pickle(decrypt(case)).state

def test_a_subclass_has_no_kind_and_reports_its_path():
    legacy = state_from_pickle(decrypt(case_named("1.44.0", "subclass")))
    assert legacy.kind is None and legacy.class_path.startswith("__main__.")

def test_a_selenium_pickle_gives_its_state():
    legacy = state_from_pickle(decrypt(case_named("1.44.0", "selenium")))
    assert legacy.kind == "browser" and legacy.state["_backend_type"] == "selenium"

def test_reading_tokens_never_imports_graftpunk_tokens():
    # subprocess: import graftpunk.session_legacy; then sys.modules["graftpunk.tokens"] = None; then the read
    assert run_isolated_read("1.44.0", "tokens") == case_named("1.44.0", "tokens").expected["cached_tokens"]
```

- [ ] **Step 2: Run** to see `No module named 'graftpunk.session_legacy'`.
- [ ] **Step 3: Implement** a `dill.Unpickler` subclass whose `find_class` consults `FROZEN_CLASS_PATHS` first, then `super().find_class`, then on `ImportError` returns a stub class and records the path.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Gate, then commit** `feat: read a session's state dict from a legacy pickle without importing its classes`.

### Task 4: `session_record`: the field table and the record codec

**Files:**
- Create: `src/graftpunk/session_record.py`
- Modify: `src/graftpunk/exceptions.py`, `src/graftpunk/cookies.py`
- Test: `tests/unit/test_session_record.py`
- Modify: `tests/fixtures/session_compat/*/*.expected.json` (fill `recordable`)

**Interfaces:**
- Produces in `graftpunk.exceptions` (which still imports nothing from graftpunk): `NewerSessionRecord(GraftpunkError)` (takes `version: int`; message names the version and says to upgrade graftpunk), `InvalidSessionRecord(GraftpunkError)` (takes `reason: str`), `RealSessionClassUnavailable(ImportError)` (used from Task 6).
- Produces in `graftpunk.cookies`: `policy_is_default(policy: object) -> bool`: true for the standard library's `DefaultCookiePolicy`, graftpunk's `BrowserCookiePolicy`, or the legacy HTTPie policy class (or a stub of it), when `_settings(policy)` equals a fresh `BrowserCookiePolicy`'s. Its docstring says it is part of the stored session format.
- Produces in `session_record`: `SessionFormat = Literal["pickle", "record"]`; `RECORD_VERSION = 1`.
- Produces: `classify(decrypted: bytes) -> Record | LegacyBytes`. `Record` (frozen): `format: SessionFormat = "record"`, `version: int`, `kind: str`, `fields: dict[str, Any]`, `extra: dict[str, Any]` (unknown top-level keys, untouched). `LegacyBytes` (frozen): `format: SessionFormat = "pickle"`, `data: bytes`. `classify` validates the whole record: it raises `NewerSessionRecord` for `version > RECORD_VERSION` and `InvalidSessionRecord` for anything else that is not a valid record (not an object, a missing or mistyped field, an unknown `kind`).
- Produces: `FIELDS: tuple[Field, ...]`, each with `state_key`, `record_path` (tuple of keys), `kinds: frozenset[str]`, `encode`, `decode` (an encoder may raise `ValueError` to refuse a value); and `PICKLE_ONLY: tuple[PickleOnly, ...]`, each with `state_key`, `kinds`, `at_default: Callable[[Any], bool]`, `default: Callable[[], Any]`.
  - `PICKLE_ONLY` rows: `hooks` (default `{"response": []}`), `adapters` (default: fresh `HTTPAdapter()` on `https://` and `http://`), both for kinds `api` and `requests`. No selenium rows (Ruling 1).
  - `FIELDS` cover, for kind `browser`: `_backend_type`, `_use_stealth`, `session_name`, `current_url`, `headers`, `cookies`, and the `PICKLED_SESSION_RIDER_ATTRS`; for `api` and `requests`: `headers`, `cookies`, `auth`, `params`, `verify`, `proxies`, `cert`, `max_redirects`, `trust_env`, `stream`.
- Produces: `state_to_record(kind: str, state: Mapping[str, Any], extra: Mapping[str, Any] | None = None) -> bytes | Unrecordable` (`Unrecordable`: `key: str`, `reason: str`). Any state key with no row is `Unrecordable(key=that key)`.
- Produces: `record_to_state(record: Record) -> dict[str, Any]`: the stored keys decoded, plus each `PICKLE_ONLY` row of the kind at `default()`.
- Produces: `kind_of(session: object) -> str | None`, comparing `(type(session).__module__, type(session).__qualname__)` with `SESSION_CLASS_PATHS`. Imports no class.
- Record key names: top level `graftpunk_session`, `kind`, `backend_type`, `use_stealth`, `session_name`, `current_url`, `headers`, `cookies`, `auth`, `riders` (`cached_tokens`, `header_roles`, `header_role_origins`, `account`), `requests` (`params`, `verify`, `proxies`, `cert`, `max_redirects`, `trust_env`, `stream`).

- [ ] **Step 1: Write the failing tests**

```python
def test_session_record_is_a_leaf():
    assert_leaf_imports("graftpunk.session_record", {
        "graftpunk.session_riders", "graftpunk.cookies", "graftpunk.exceptions", "graftpunk.logging"})

def test_exceptions_still_imports_nothing_from_graftpunk():
    assert_leaf_imports("graftpunk.exceptions", set())

@pytest.mark.parametrize("kind,state", [("browser", nodriver_state()), ("api", GraftpunkSession().__getstate__()), ("requests", requests.Session().__getstate__())])
def test_every_key_getstate_emits_has_a_row(kind, state):
    known = {f.state_key for f in FIELDS if kind in f.kinds} | {p.state_key for p in PICKLE_ONLY if kind in p.kinds}
    assert set(state) <= known

@pytest.mark.parametrize("case", recordable_cases(), ids=...)
def test_state_survives_the_record(case):
    legacy = state_from_pickle(decrypt(case))
    rec = classify(state_to_record(legacy.kind, legacy.state))
    assert comparable(record_to_state(rec)) == comparable(legacy.state)   # comparable(): cookie dicts, header pairs, token dicts; pickle-only keys compared by at_default

@pytest.mark.parametrize("case,key", [("adapter", "adapters"), ("selenium", "_backend_type")])
def test_what_a_record_cannot_hold_is_refused_by_name(case, key):
    legacy = state_from_pickle(decrypt(case_named("1.44.0", case)))
    assert state_to_record(legacy.kind, legacy.state).key == key

def test_each_default_predicate_holds_for_the_installed_libraries():
    for obj in (requests.Session(), GraftpunkSession()):
        state = obj.__getstate__()
        assert all(p.at_default(state[p.state_key]) for p in PICKLE_ONLY if p.state_key in state)

@pytest.mark.parametrize("kind,cls", [("api", GraftpunkSession), ("requests", requests.Session)])
def test_a_restored_state_makes_a_working_session(kind, cls, echo_server):
    obj = cls.__new__(cls); obj.__setstate__(record_to_state(classify(state_to_record(kind, cls().__getstate__()))))
    assert obj.get(echo_server.url).status_code == 200      # needs adapters and hooks back

def test_a_newer_record_is_refused_by_version():
    with pytest.raises(NewerSessionRecord, match="2"):
        classify(json.dumps({"graftpunk_session": 2, "kind": "browser"}).encode())

@pytest.mark.parametrize("payload", [b"[]", b'{"graftpunk_session": 1}', b'{"graftpunk_session": 1, "kind": "nope"}', record_with_cookie_missing_name()])
def test_json_that_is_not_a_record_is_invalid(payload):
    with pytest.raises(InvalidSessionRecord):
        classify(payload)

@pytest.mark.parametrize("mutate,key", [(token_ttl_inf, "_gp_cached_tokens"), (token_extracted_at_nan, "_gp_cached_tokens"), (bytes_header_value, "headers"), (custom_cookie_policy, "cookies"), (auth_object, "auth")])
def test_values_a_record_cannot_hold_make_the_session_unrecordable(mutate, key): ...

def test_an_empty_session_and_an_awkward_cookie_round_trip(): ...
    # no cookies, no headers; and expires=None, value "café", rest={"HttpOnly": None}

def test_both_header_shapes_encode_the_same():
    assert encode_headers({"A": "1"}) == encode_headers(CaseInsensitiveDict({"A": "1"}))

def test_unknown_top_level_keys_come_back_in_extra_and_go_out_unchanged():
    rec = classify(record_with(extra={"future": {"a": 1}}))
    assert rec.extra == {"future": {"a": 1}}
    assert json.loads(state_to_record(rec.kind, record_to_state(rec), rec.extra))["future"] == {"a": 1}

def test_a_pickle_is_never_unpickled_by_classify():
    assert isinstance(classify(pickle_that_calls_a_marker()), LegacyBytes) and not MARKER.called

def test_a_record_holding_pickle_bytes_as_a_string_is_data():
    rec = classify(record_with(cookie_value="\x80\x04cos\nsystem\n"))
    assert record_to_state(rec)["cookies"] and not MARKER.called
```

- [ ] **Step 2: Run** to see `No module named 'graftpunk.session_record'`.
- [ ] **Step 3: Implement.** `classify` tests the first byte for `{`. `json.dumps(..., allow_nan=False)`; a `ValueError` or `TypeError` from an encoder becomes `Unrecordable(key=state_key)`. The `at_default` predicates are structural, as the spec lists them: `hooks` is a mapping whose only key is `response` with an empty value; `adapters` has exactly the prefixes `https://` and `http://`, each of type name `HTTPAdapter` from `requests.adapters`, with zero-retry `max_retries` and `requests.adapters.DEFAULT_POOLSIZE` pool sizes. The cookie encoder calls `policy_is_default`.
- [ ] **Step 4: Run** the tests; fill `recordable` in each `expected.json` (a `--update-expected` mode of `scripts/session_compat.py`) and print the unrecordable count per version. Put the counts in the commit body.
- [ ] **Step 5: Gate, then commit** `feat: the session record: one field table, and the codec built from it`.

### Task 5: `session_view`: `StoredSession`

**Files:**
- Create: `src/graftpunk/session_view.py`
- Test: `tests/unit/test_session_view.py`

**Interfaces:**
- Produces: `StoredSessionLike` (Protocol): `cookies`, `headers`, `session_name: str`, `current_url: str`, and each attribute in `SESSION_RIDER_ATTRS`, readable and settable.
- Produces: `StoredSession(kind: str | None, state: dict[str, Any], extra: dict[str, Any] | None = None)` with attributes `kind`, `extra`; `__getstate__() -> dict[str, Any]` returning only the table's keys for its kind; `classmethod from_record(record: Record)`. `cookies` is a `requests.cookies.RequestsCookieJar`. `kind=None` (a legacy pickle of a subclass or another type) is for reading only: `__getstate__()` raises `TypeError`.
- Imports from graftpunk only `session_record`, `session_riders`, `cookies`.

- [ ] **Step 1: Write the failing tests**

```python
def test_session_view_is_a_leaf():
    assert_leaf_imports("graftpunk.session_view", {"graftpunk.session_record", "graftpunk.session_riders", "graftpunk.cookies"})

@pytest.mark.parametrize("case", recordable_cases(), ids=...)
def test_the_view_and_the_real_object_give_the_same_metadata(case, local_store_with):
    legacy = state_from_pickle(decrypt(case)); view = StoredSession(legacy.kind, legacy.state)
    local_store_with(case); real = load_session("myshop", key=case.key)   # skipped without the browser packages for browser kinds
    assert _extract_session_metadata(view, "s") == _extract_session_metadata(real, "s")
    assert set(view.__getstate__()) == {k for k in real.__getstate__() if k not in PICKLE_ONLY_KEYS}

def test_setting_a_name_on_a_kind_with_no_name_writes_no_name():
    view = StoredSession("requests", requests.Session().__getstate__()); view.session_name = "x"
    assert "session_name" not in view.__getstate__()

def test_a_memory_only_rider_can_be_set_and_is_not_stored():
    view = StoredSession("browser", nodriver_state()); setattr(view, _CSRF_TOKENS_ATTR, {"t": "v"})
    assert getattr(view, _CSRF_TOKENS_ATTR) == {"t": "v"} and _CSRF_TOKENS_ATTR not in view.__getstate__()

def test_a_view_with_no_kind_can_be_read_but_not_written():
    view = StoredSession(None, state_from_pickle(decrypt(case_named("1.44.0", "subclass"))).state)
    assert cookie_dicts(view.cookies)
    with pytest.raises(TypeError): view.__getstate__()
```

- [ ] **Step 2: Run** to see the import fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Gate, then commit** `feat: StoredSession, a browser-free view of a stored session`.

### Task 6: Readers go through `classify`

**Files:**
- Modify: `src/graftpunk/cache.py` (`load_session`, `_load_session_with_metadata`, the three API loaders; `_Stub`, `_BrowserFreeUnpickler` and `_deserialize_browserfree` removed)
- Modify tests that reach the removed names or patch `load_session` to feed the API loaders: `tests/unit/test_browserfree_session_load.py` (its uses of `cache._BrowserFreeUnpickler`, `cache._Stub`, `cache._deserialize_browserfree` move onto `session_legacy.state_from_pickle` and its placeholder), `tests/unit/test_cookies.py` (the `cache._Stub` base near line 302), `tests/unit/test_cache.py` (the ten `load_session_for_api` tests that `monkeypatch.setattr("graftpunk.cache.load_session", ...)`), `tests/unit/test_api_session_resolution.py` (the same patch near line 179). The last two patch `graftpunk.cache._read_stored` instead, returning a `LegacyBytes` or `Record` built from the mock session's state. No assertion is weakened; each test keeps what it asserted.
- Test: `tests/unit/test_session_readers.py`

**Interfaces (all private, in `cache.py`):**
- `_read_stored(name: str, *, key: bytes | None, backend_override: str | None = None) -> tuple[Record | LegacyBytes, SessionMetadata]`: today's backend load, key resolution, decrypt and checksum check from `_load_session_with_metadata`, then `_classified`.
- `_classified(decrypted: bytes, *, name: str) -> Record | LegacyBytes`: `classify`, mapping `InvalidSessionRecord` to `SessionExpiredError` naming the session (the wording a corrupt pickle gets today); `NewerSessionRecord` propagates. The only place that mapping exists.
- `_unpickle_whole(legacy: LegacyBytes, *, name: str) -> Any`: today's `pickle.loads` and its `ModuleNotFoundError` handling, raising `RealSessionClassUnavailable(...) from exc` so `_distribution.missing_browser_module` still finds the `ModuleNotFoundError` in the cause chain and the CLI prints its install hint.
- `_restore_record(record: Record, *, name: str) -> Any`: the class for `record.kind` imported by its path from `SESSION_CLASS_PATHS`, then `obj = cls.__new__(cls); obj.__setstate__(record_to_state(record))`. An import failure for kind `browser` raises `RealSessionClassUnavailable(...) from exc`.
- `_finish_loaded(session: Any, name: str, metadata: SessionMetadata) -> None`: the three things `_load_session_with_metadata` does to a loaded object today, applied alike to a real object and a `StoredSession`: the `cookies` and `headers` structure check, `_stamp_session_slot(session, name)`, and the account fallback (metadata under `ACCOUNT_RULE` sets `GP_ACCOUNT_ATTR`).
- `_stored_view(stored: Record | LegacyBytes) -> StoredSession`: `StoredSession.from_record` for a record; for legacy bytes, `StoredSession(legacy.kind, legacy.state)` from `state_from_pickle`.
- `_api_session_from_session(source)` is unchanged in signature and stays the only builder of a `GraftpunkSession`.
- `load_session_for_api_from_bytes` has no session name: it passes `name="session"` to `_classified`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("case", recordable_cases(), ids=...)
def test_a_record_loads_for_api_exactly_as_its_pickle_did(case):
    record_bytes = encrypt(as_record(case), case.key)
    assert api_fingerprint(load_session_for_api_from_bytes(record_bytes, key=case.key)) == \
           api_fingerprint(load_session_for_api_from_bytes(case.encrypted, key=case.key))

@pytest.mark.parametrize("fmt", ["pickle", "record"])
def test_api_loaders_do_not_import_graftpunk_session(fmt):      # subprocess
    assert "graftpunk.session" not in modules_loaded_by_an_api_load(fmt)

@pytest.mark.parametrize("kind,cls", [("api", GraftpunkSession), ("requests", requests.Session)])
def test_load_session_of_a_record_returns_the_class_that_was_saved(kind, cls, seeded_record_store):
    assert type(load_session("myshop", key=KEY)) is cls

def test_load_session_of_a_browser_record_without_the_browser_stack_gives_the_install_hint(no_browser_stack, seeded_record_store):
    with pytest.raises(RealSessionClassUnavailable) as exc: load_session("myshop", key=KEY)
    assert missing_browser_module(exc.value)

def test_an_api_load_keeps_the_account_from_the_metadata(seeded_record_store_with_account):
    assert getattr(load_session_for_api("myshop@alice", key=KEY), "_gp_account_identifier") == "alice"

def test_an_invalid_record_reads_as_an_expired_session(store_holding(b'{"graftpunk_session": 1}')):
    for reader in READERS:
        with pytest.raises(SessionExpiredError, match="myshop"): reader("myshop")

def test_a_version_2_record_is_refused_by_every_reader(store_holding(version_2_record())):
    for reader in READERS:
        with pytest.raises(NewerSessionRecord): reader("myshop")
```

`READERS` is `load_session`, `load_session_for_api`, `load_session_for_api_resolved`, and a wrapper calling `load_session_for_api_from_bytes(stored_encrypted(name), key=KEY)` (which names the session `"session"`, so its `match` is `"session"`).

- [ ] **Step 2: Run** to see them fail (records are not understood yet).
- [ ] **Step 3: Implement.** `load_session`: `_read_stored`, then `_restore_record` or `_unpickle_whole`, then `_finish_loaded`. `load_session_for_api` and `load_session_for_api_resolved` stop calling `load_session`: `_read_stored`, `_stored_view`, `_finish_loaded`, `_api_session_from_session`.
- [ ] **Step 4: Run** the new tests, `test_session_compat.py`, `test_browserfree_session_load.py`, `test_cookies.py`, `test_cache.py`, `test_api_session_resolution.py`. Expected: PASS.
- [ ] **Step 5: Gate, then commit** `feat: every session reader takes a record or a legacy pickle; API loads need no browser package`.

### Task 7: Fresh saves: the setting and the writer

**Files:**
- Modify: `src/graftpunk/config.py`, `src/graftpunk/cache.py` (`cache_session`), `src/graftpunk/cli/config_commands.py` (show the value)
- Test: `tests/unit/test_session_writers.py`

**Interfaces:**
- Produces: `GraftpunkSettings.session_format: Literal["pickle", "record"] = "pickle"` (`GRAFTPUNK_SESSION_FORMAT`). `config.py` writes the `Literal` itself and does not import `session_record`; a test asserts its two values equal `typing.get_args(session_record.SessionFormat)`.
- Produces in `cache.py`: `_encode_fresh(session: Any) -> bytes`; `_encode_as(session: Any, fmt: SessionFormat) -> bytes` (a `StoredSession` with `"record"`, a real object with `"pickle"`; anything else raises `TypeError`); `_store(name: str, payload: bytes, session: Any, *, account_rule, key, metadata: SessionMetadata | None = None) -> str`. With `metadata=None`, `_store` builds fresh metadata as `cache_session` does today (new times, `status="active"`); with a `metadata` given it saves that, with `checksum` replaced.
- `cache_session` is `_encode_fresh` then `_store`. Under `record`, a fallback to pickle logs one warning, event `session_not_recordable`, with fields `name`, `key` and `reason`.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_default_still_writes_a_pickle(local_store):
    cache_session(nodriver_session(), "myshop"); assert stored_plain("myshop")[:1] == b"\x80"

def test_record_setting_writes_a_record(local_store, record_format):
    cache_session(nodriver_session(), "myshop"); assert json.loads(stored_plain("myshop"))["kind"] == "browser"

def test_an_unrecordable_session_is_pickled_with_one_warning(local_store, record_format, log):
    cache_session(session_with_custom_adapter(), "myshop")
    assert stored_plain("myshop")[:1] == b"\x80"
    assert [e["key"] for e in log.named("session_not_recordable")] == ["adapters"]

def test_a_mistyped_format_fails_naming_the_choices(monkeypatch):
    monkeypatch.setenv("GRAFTPUNK_SESSION_FORMAT", "json"); reset_settings()
    with pytest.raises(ValidationError, match="pickle.*record"): get_settings().session_format

def test_the_setting_and_the_codec_name_the_same_formats():
    assert typing.get_args(GraftpunkSettings.model_fields["session_format"].annotation) == typing.get_args(SessionFormat)

def test_encode_as_refuses_a_view_as_a_pickle():
    with pytest.raises(TypeError): _encode_as(StoredSession("browser", nodriver_state()), "pickle")

@pytest.mark.parametrize("fn,text", SIGNATURES_AT_1_44_0.items())   # the seven public functions under Global Constraints
def test_public_signatures_are_unchanged(fn, text):
    assert str(inspect.signature(fn)) == text
```

`record_format` is a fixture: `monkeypatch.setenv("GRAFTPUNK_SESSION_FORMAT", "record")`, `reset_settings()`, and `reset_settings()` again on teardown.

- [ ] **Step 2: Run** to see them fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new tests plus `test_session_compat.py` and `test_cache.py`. Expected: PASS.
- [ ] **Step 5: Gate, then commit** `feat: GRAFTPUNK_SESSION_FORMAT=record writes a session as a record; the default is unchanged`.

### Task 8: Write-backs keep the format they read

**Files:**
- Modify: `src/graftpunk/cache.py` (`update_session_cookies`, `copy_session`)
- Test: `tests/unit/test_session_write_backs.py`; existing `tests/unit/test_account_label_migration.py`, `tests/unit/test_api_session_resolution.py`, `tests/unit/test_encryption.py` and `tests/unit/test_cache.py` stay green

**Interfaces:**
- Produces: `_load_for_write_back(stored: Record | LegacyBytes, *, name: str) -> tuple[Any, SessionFormat]`: a record gives `(StoredSession.from_record(stored), "record")`; legacy bytes give `(_unpickle_whole(stored, name=name), "pickle")`. Its input comes from `_read_stored`, so an invalid record is already a `SessionExpiredError` and a newer one a `NewerSessionRecord` before anything is changed.
- `update_session_cookies` is best-effort: for `RealSessionClassUnavailable`, `NewerSessionRecord` and `SessionExpiredError` it writes nothing and logs one warning, event `session_write_back_skipped`, with `name`, `reason` (the exception's class name) and the hint "the next login, or a write-back from a full install, rewrites the session". `copy_session` lets all three propagate.
- `copy_session` keeps its contract that created, modified and expiry times and status are kept: it passes `_store(..., metadata=replace(source_metadata, name=dest, account_identifier=account_identifier, account_rule=ACCOUNT_RULE))`. It sets the name with `session.session_name = dest`, dropping the `hasattr(session, "_session_name")` probe, and keeps its read-back verification.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("setting", ["pickle", "record"])
@pytest.mark.parametrize("stored", ["pickle", "record"])
def test_a_write_back_keeps_the_stored_format(setting, stored, local_store, monkeypatch):
    seed(stored); monkeypatch.setenv("GRAFTPUNK_SESSION_FORMAT", setting); reset_settings()
    update_session_cookies(api_with_new_cookie(), "myshop")
    assert format_of(stored_plain("myshop")) == stored and "new" in cookie_names("myshop")
    copy_session("myshop", "myshop@alice", account_identifier="alice")
    assert format_of(stored_plain("myshop@alice")) == stored

def test_an_unknown_key_survives_both_write_backs_even_when_the_setting_is_pickle(local_store):
    seed_record(extra={"future": {"a": 1}})
    update_session_cookies(api_with_new_cookie(), "myshop")
    copy_session("myshop", "myshop@alice", account_identifier="alice")
    assert all(json.loads(stored_plain(n))["future"] == {"a": 1} for n in ("myshop", "myshop@alice"))

def test_a_version_2_record_is_left_byte_for_byte(local_store, log):
    before = seed_raw(version_2_record())
    update_session_cookies(api_with_new_cookie(), "myshop")
    assert [e["reason"] for e in log.named("session_write_back_skipped")] == ["NewerSessionRecord"]
    with pytest.raises(NewerSessionRecord): copy_session("myshop", "myshop@alice", account_identifier="alice")
    assert stored_raw("myshop") == before

def test_an_invalid_record_is_skipped_by_the_cookie_write_back_and_raises_from_copy(local_store, log):
    before = seed_raw(encrypt(b'{"graftpunk_session": 1}'))
    update_session_cookies(api_with_new_cookie(), "myshop")
    assert [e["reason"] for e in log.named("session_write_back_skipped")] == ["SessionExpiredError"]
    with pytest.raises(SessionExpiredError): copy_session("myshop", "myshop@alice", account_identifier="alice")
    assert stored_raw("myshop") == before

def test_without_the_browser_stack_a_record_write_back_works_and_a_pickle_one_is_skipped(no_browser_stack, local_store, log):
    seed("record"); update_session_cookies(api_with_new_cookie(), "myshop"); assert "new" in cookie_names("myshop")
    before = seed("pickle", name="other"); update_session_cookies(api_with_new_cookie(), "other")
    assert stored_raw("other") == before and log.named("session_write_back_skipped")
    with pytest.raises(ImportError): copy_session("other", "other@alice", account_identifier="alice")

def test_copy_renames_a_browser_record_and_keeps_its_times(local_store):
    seed("record"); created = metadata("myshop").created_at
    copy_session("myshop", "myshop@alice", account_identifier="alice")
    assert json.loads(stored_plain("myshop@alice"))["session_name"] == "myshop@alice"
    assert metadata("myshop@alice").created_at == created
```

- [ ] **Step 2: Run** to see them fail.
- [ ] **Step 3: Implement** each write-back as: `_read_stored`, `_load_for_write_back`, its existing mutation, `_encode_as(session, fmt)`, `_store`. Neither calls the public `cache_session`.
- [ ] **Step 4: Run** the new tests, `test_account_label_migration.py`, `test_api_session_resolution.py`, `test_encryption.py` and `test_cache.py`. Expected: PASS.
- [ ] **Step 5: Gate, then commit** `feat: a session write-back keeps the format it read and never drops a record's unknown keys`.

### Task 9: graftpunk-lite and the old-reader check

**Files:**
- Modify: `scripts/smoke_install.sh`, `.github/workflows/python-quality.yml`, `README.md` (the lite paragraph and the `load_session_for_api` note), `tests/unit/test_lite_cli_hints.py`
- Not modified: the missing-browser-module handlers in `src/graftpunk/cli/plugin_runtime.py` and `src/graftpunk/cli/http_commands.py`. They stay because a plugin command's own code can import a browser module; they no longer fire on a session load, which Step 1 proves.

**Interfaces:**
- Consumes: `scripts/session_compat.py read` from Task 1.

- [ ] **Step 1: Extend `scripts/smoke_install.sh`** (lite venv, no browser packages, a local server that echoes the `Cookie` header). Each line prints `OK` or fails the script:
  - a YAML plugin command with a stored session returns the server's response for a stored pickle and for a stored record;
  - `gp http` with a stored session does the same;
  - a command that refreshes a cookie in a stored record writes it back, and the next run sends it;
  - the same refresh on a stored pickle prints the `session_write_back_skipped` warning and exits 0;
  - `copy_session` on a stored pickle raises `ImportError` and the source slot's bytes are unchanged;
  - `gp <plugin> login` prints the install hint and exits 1.
- [ ] **Step 2: Update `tests/unit/test_lite_cli_hints.py`**: a plugin command and `gp http` with a stored session no longer print the install hint on lite; `login`, `gp observe` and `gp session export` still do.
- [ ] **Step 3: Run** `python scripts/build_lite.py --out-dir <scratch>/lite`, `uv build --out-dir <scratch>/full`, then `scripts/smoke_install.sh <scratch>/lite/graftpunk_lite-*.whl <scratch>/full/graftpunk-*.whl`. Expected: every new line prints `OK`.
- [ ] **Step 4: Add the CI job `old-reader`** to `python-quality.yml`: with the branch, cache the `tokens` session into a local store under the default setting; `python scripts/session_compat.py read --graftpunk 1.44.0 --store $STORE --name myshop` must exit 0 and print the cookie names. Then cache it with `GRAFTPUNK_SESSION_FORMAT=record`; the same command must exit 3 printing `SessionExpiredError`, and the store's bytes must be unchanged afterwards.
- [ ] **Step 5: Run the job's commands locally** and put both outputs in the commit body.
- [ ] **Step 6: Update the README passages**: on lite, a plugin command or `gp http` with a stored session runs; `load_session_for_api` no longer needs `[browser]`; `login`, `gp observe` and `gp session export` still do; a write-back from lite works for a session stored as a record.
- [ ] **Step 7: Gate, then commit** `feat: plugin commands use a stored session on graftpunk-lite; CI proves 1.44.0 reads what the default writes`.

### Task 10: Docs and release 1.45.0

**Files:**
- Modify: `CHANGELOG.md`, `pyproject.toml`, `uv.lock`, `docs/HOW_IT_WORKS.md` (`### Caching`, `### Loading` and `### Storage Backends` under `## Session Management`), `CONTRIBUTING.md` (what `scripts/session_compat.py` checks, and to run `generate` before any change to the stored format or `BrowserSession.__getstate__`), `.claude-plugin/plugin.json` only if `scripts/check-skill-version.sh d0511e6c` asks for it
- Comment on issue #245: the session half shipped; the plugin-packaging half stays open and needs its own short design.

- [ ] **Step 1: Write the 1.45.0 CHANGELOG section.** Added: `GRAFTPUNK_SESSION_FORMAT`, the record format, API loads and plugin commands on graftpunk-lite, `scripts/session_compat.py`. Changed: `load_session_for_api` no longer needs the browser packages. Upgrading, in this order: (1) nothing changes unless you set `GRAFTPUNK_SESSION_FORMAT=record`; (2) before any host writes records, every graftpunk that reads the same store must be 1.45.0 or later, because an older one reports a record as an expired session (it does not delete it); (3) a session becomes a record at its next login on a host that sets `record`, and a write-back never converts one; (4) selenium-backend sessions, and sessions with a custom adapter, hooks, a custom cookie policy or a subclass, stay pickles, with the warning `session_not_recordable`; (5) the new warnings and errors by name: `session_not_recordable`, `session_write_back_skipped`, `NewerSessionRecord`, `InvalidSessionRecord`, `RealSessionClassUnavailable` (an `ImportError`).
- [ ] **Step 2: Run** `scripts/check-skill-version.sh d0511e6c`, the full gate once more with `--cov=src/graftpunk`, and the changed test files under 3.11 and 3.14.
- [ ] **Step 3: Commit** `release: 1.45.0`, push, and open the PR with the verification commands and their output, the per-version unrecordable counts, which selenium fixtures are synthetic, and "Refs #245". Do not merge.
