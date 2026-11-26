---
type: plan
validated:
  sha: 16e2db7e
  date: 2026-11-25T09:28:47Z
  reviewers: [fact-check, solid-hygiene]
  findings:
    critical: 0
    important: 3
    medium: 5
    low: 6
    nitpick: 0
  net_negative_raised: 0
  net_negative_addressed: 0
  net_negative_remaining: 0
---

# Sessions stored as plain data: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** graftpunk 1.45.0 reads a saved session as a versioned JSON record or as a legacy pickle, writes pickles by default and records when `GRAFTPUNK_SESSION_FORMAT=record`, and loads either for API use without the browser packages.

**Architecture:** Four browser-free leaf modules own the stored format (`session_riders`, `session_record`, `session_legacy`, `session_view`); `cache.py` keeps storage and the rollout policy in private functions. Every class already reduces itself to a state dict through `__getstate__` and restores through `__setstate__`; the record is that dict as JSON, described by one field table. Committed fixtures written by released versions prove old sessions still load.

**Tech Stack:** Python 3.11 to 3.14, requests, dill (imported as `pickle` in `cache.py`), cryptography (Fernet), pydantic-settings, pytest with xdist, uv.

**Spec:** `docs/superpowers/specs/2026-11-24-session-record-format-design.md`, including its "Amendments from plan validation" section, which governs where it differs from the text above it. Read it first; this plan names files, signatures and tests and does not restate the spec's reasoning.

## Global Constraints

- Nothing that works on 1.44.0 may break. A pickle written by this branch under the default setting must load on 1.44.0, tokens included.
- Frozen pickled class paths: `graftpunk.session.BrowserSession`, `graftpunk.graftpunk_session.GraftpunkSession`, `graftpunk.tokens.CachedToken`. The cookie policy pickles as `http.cookiejar.DefaultCookiePolicy`.
- Public signatures unchanged from 1.44.0: `cache_session`, `load_session`, `load_session_for_api`, `load_session_for_api_resolved`, `load_session_for_api_from_bytes(encrypted, *, key=None)`, `update_session_cookies(api_session, session_name, *, key=None)`, `copy_session(source, dest, *, account_identifier, key=None)`. Task 7 pins each by comparing `str(inspect.signature(fn))` with the string v1.44.0 prints for it (in a scratch venv with `graftpunk==1.44.0`: `python -c 'import inspect, graftpunk.cache as c; print(inspect.signature(c.<fn>))'`).
- Existing log event names keep their names and fields; new fields are additive.
- `graftpunk.exceptions` keeps importing nothing from graftpunk. New exception types are defined there.
- The four leaf modules import no session class and none of `selenium`, `requestium`, `nodriver`, `httpie`. Allowed graftpunk imports: `session_riders`: `session_identity`. `session_legacy`: `session_riders`. `session_record`: `session_riders`, `cookies`, `exceptions`, `logging`. `session_view`: `session_record`, `session_riders`, `cookies`.
- `src/graftpunk/plugins/login_engine.py` and `login_settle.py` are not edited; `login_engine.py` keeps importing the moved rider names from `graftpunk.tokens`. Task 10 checks this once with `git diff --name-only d0511e6c -- src/graftpunk/plugins/login_engine.py src/graftpunk/plugins/login_settle.py`, which must print nothing.
- Public repo: placeholder hosts only (`myshop.example`, `idp.example.com`), generated test keys, no real site, plugin or account names. No em or en dashes. No attribution lines in commits.
- No live Chrome anywhere in this plan.
- Gate, green after the last edit of every task: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4` (never `-n auto`), `uvx ruff check .`, `uvx ruff format --check .`, `uvx ty@0.0.75 check src/ --python .venv`. Changed test files also under 3.11 and 3.14 in scratch venvs (`UV_PROJECT_ENVIRONMENT=<scratch>/venv3xx`); the worktree `.venv` stays on 3.12.
- One commit per task, pushed to the PR branch as it lands.

## Rulings

Decisions this plan makes where the spec left a choice or where the code forced one. Each is recorded in the spec's amendments section.

1. **Selenium-backend sessions stay pickles in release 1** (spec open-risk clause). `state_to_record` returns `Unrecordable(key="_backend_type", reason="selenium sessions are stored as pickles in this release")` for kind `browser` with `_backend_type == "selenium"`. They still load through every loader. Cost if wrong: selenium users get no record until a later release; nothing breaks.
2. **Selenium fixtures are built without a browser** (`BrowserSession.__new__`, then `requestium.Session.__init__(obj)`, which starts no driver, then the attributes the selenium `__getstate__` reads). That gives the real pickle shape (requestium's own `_driver_initializer` partial, empty `webdriver_options`), which is what the loaders must read. It is not evidence about a real login's options, which matters only when selenium records are designed. Cost if wrong: a real selenium pickle has a shape the synthetic one lacks, and a loader regression for it would go unseen; the fixtures README says they are synthetic.
3. **The three session class paths live in `session_riders`** (`SESSION_CLASS_PATHS`), which both `session_record` and `session_legacy` may import, so neither imports the other. The module docstring says it holds the stored format's shared constants.
4. **`classify` validates the whole record.** Mapping an invalid record to `SessionExpiredError` then has one owner, `_classified` in `cache.py`.
5. **The cookie jar's policy check is the cookie codec's refusal**, through a new private `graftpunk.cookies._policy_is_default(policy)`, documented as internal to the stored format; its only caller is the cookie codec.
6. **`record_to_state` returns the full state `__setstate__` needs**: each pickle-only key the kind has is put back by the row's `default` factory, because `requests.Session.__setstate__` sets only the keys it is given.
7. **`_load_for_write_back` takes an already classified result**, not raw bytes, because the shared read step classifies first.
8. **Whether a fixture is recordable is asserted in the codec's test**, not stored in the fixture files, which describe frozen releases and never change. For the same reason the fixture files store the headers a release saved, not the headers an API load keeps, which is the loader's policy.
9. **A `GraftpunkSession` restored from its own state can send requests after this work.** Today one rebuilt by `__setstate__` (from a pickle) lacks `_gp_default_session_headers` and raises `AttributeError` on its first request, because requests' `__getstate__` covers only `__attrs__`. `load_session` of kind `api` returns such an object for both formats, so Task 6 gives `GraftpunkSession` a `__setstate__`. One private method owns the attributes `__init__` sets beyond requests' own and their defaults; `__init__` and `__setstate__` both call it, so the list exists once. On restore, `_gp_default_session_headers` is a copy of the restored headers. Cost if wrong: one method on `GraftpunkSession`; pickles and records are unchanged.
10. **The view exports its state as `stored_state()`, the stored keys present in its state.** A real `BrowserSession.__getstate__` adds every pickled rider at its default, so the two are not compared key for key; a 1.21.0 state has no `_gp_header_role_origins` and the view does not invent it. The export is not named `__getstate__`, and the view refuses to be pickled or copied by the pickle protocol, because a pickle naming `graftpunk.session_view.StoredSession` is a class path no older release can read.
11. **The browser-free fixture moves to `tests/fixtures/session_compat/legacy/` and is not a compat case**, because its token cache holds a string, not a `CachedToken`. Its existing tests keep reading it.
12. **What a write-back writes follows the type of the object it loaded.** `_encode_as(session)` takes no format: a `StoredSession` is written as a record, a real object as a pickle. `_load_for_write_back` returns the object alone. A wrong pairing cannot be expressed.

## Shared test helpers

Task 1 creates these; later tasks use them by these names and add no others that a second test file needs. A helper used by one test file only is that file's own.

In `tests/unit/session_compat_cases.py`:

- `TEST_KEY: bytes` (the shared fixture key). `CompatCase`: `version`, `case`, `encrypted: bytes`, `key: bytes`, `metadata: dict | None` (the release's own `metadata.json`), `expected: dict`.
- `iter_cases() -> list[CompatCase]` (every case under the four version directories; it does not include `tests/fixtures/session_compat/legacy/`, which only its existing tests read); `cases(*, kinds: tuple[str, ...] | None = None, recordable: bool | None = None) -> list[CompatCase]` (kind and recordability come from `EXPECTED_KIND` and `RECORDABLE`, two dicts in this module keyed by case name); `case_named(version: str, case: str) -> CompatCase`; `case_id(case) -> str` for `ids=`.
- `decrypt(case: CompatCase) -> bytes`; `encrypt(plain: bytes, key: bytes = TEST_KEY) -> bytes`.
- `cookie_dicts(jar) -> list[dict]` (all 17 `Cookie` constructor arguments, sorted by name); `token_dicts(session) -> dict[str, dict]`.
- `nodriver_state(*, tokens: bool = False) -> dict[str, Any]` (a nodriver `__getstate__`-shaped dict with the fixture cookies and headers); `nodriver_session(*, tokens: bool = False)` (a `BrowserSession` built with `__new__` and `__setstate__(nodriver_state(...))`, no browser).
- `record_bytes(kind: str = "browser", *, version: int = 1, extra: dict | None = None, drop: tuple[str, ...] = (), cookie_value: str | None = None) -> bytes` (a plain JSON record built by hand, independent of the codec; `drop` removes top-level keys).
- `assert_leaf_imports(module_name: str, allowed: set[str]) -> None`: parses the module's source with `ast`, asserts every `graftpunk.*` import is in `allowed`, then imports the module in a subprocess and asserts none of `selenium`, `requestium`, `nodriver`, `httpie` is in `sys.modules`.
- `pickled_global_names(payload: bytes) -> set[str]`: walks `pickletools.genops` keeping a value stack and a memo table (each `MEMOIZE`, `BINPUT`, `LONG_BINPUT` records the top of the stack; each `BINGET`, `LONG_BINGET` pushes the memoized value); at each `STACK_GLOBAL` it joins the two values on top of the stack as `"module name"`; for `GLOBAL` it takes the argument. dill writes `pickle.DEFAULT_PROTOCOL` (4 on 3.11 to 3.13, 5 on 3.14); both use `STACK_GLOBAL`.
- `events(captured_logs, name: str) -> list[dict]`: the entries of the existing `captured_logs` fixture (`tests/conftest.py`) whose `event` is `name`.

Fixtures in `tests/unit/conftest.py`:

- `session_store`: built on the existing `fresh_backend` fixture (which points the config directory at a temp path and resets `cache.py`'s backend singleton), with `TEST_KEY` written to the key file. Methods: `seed(fmt: str, name: str = "myshop", *, extra: dict | None = None, account: str | None = None) -> bytes` (saves a nodriver session with tokens in that format and returns the raw stored bytes); `seed_plain(plain: bytes, name: str = "myshop") -> bytes` (stores `encrypt(plain)` with matching metadata); `put_case(case: CompatCase, name: str = "myshop") -> None` (lays the fixture out as its version wrote it: the encrypted payload and the release's own metadata with only its `name` replaced, or the flat-file layout when the case has no metadata); `raw(name) -> bytes`; `plain(name) -> bytes` (decrypted); `format_of(name) -> str` (`"record"` if `plain(name)` starts with `{`, else `"pickle"`); `metadata(name) -> SessionMetadata`; `cookie_names(name) -> set[str]`; and the attribute `root: Path` (the temp config directory).
- `session_format`: a factory; `session_format("record")` sets `GRAFTPUNK_SESSION_FORMAT`, calls `graftpunk.config.reset_settings()`, and resets again on teardown. Every test that sets the format uses it.
- `no_browser_stack`: a context manager factory, used as `with no_browser_stack():` inside the test after any seeding. Inside the block it removes `graftpunk.session` from `sys.modules` and sets `selenium`, `requestium`, `nodriver` and `httpie` to `None` there, so importing `graftpunk.session` fails on `requestium` with a `ModuleNotFoundError` that `_distribution.missing_browser_module` recognises, as on graftpunk-lite. It restores `sys.modules` on exit.
- `echo_server`: a local HTTP server on a free port that answers 200 with the request's `Cookie` header as the body; `echo_server.url`.

## Review Focus

Inputs the spec is silent on that a user will hit. Each has a test in the task named.

1. A record that decrypts to JSON but is not a valid record (not an object, no `kind`, a `cookies` entry missing `name`): every reader raises `SessionExpiredError` naming the session, as a corrupt pickle does today, never a `KeyError` traceback; `update_session_cookies` skips it with its existing warning. (Tasks 4, 6, 8)
2. A cached token whose `ttl` or `extracted_at` is `inf` or `nan`, or a header whose value is `bytes`: not valid JSON, so the session is unrecordable with a warning naming the key and is written as a pickle; nothing raises. (Tasks 4, 7)
3. A session with no cookies and no headers, and a cookie with `expires=None`, a non-ASCII value and a `rest` entry: each round-trips state dict to record to state dict unchanged. (Task 4)
4. `GRAFTPUNK_SESSION_FORMAT=json` (a typo): settings validation fails at first use with a message naming `pickle` and `record`; no session is written. (Task 7)
5. A write-back of a record on a full install whose setting is `pickle`: the stored bytes stay a record and its unknown top-level key survives. (Task 8)

---

### Task 1: Compat fixtures from released versions, shared helpers, and a baseline test

The safety net comes first: fixtures written by real releases, and a test that pins what today's loaders return for each, before any loader changes.

**Files:**
- Create: `scripts/session_compat.py`
- Create: `tests/fixtures/session_compat/key`, `tests/fixtures/session_compat/README.md`, `tests/fixtures/session_compat/<version>/<case>.enc`, `<case>.metadata.json` (the `metadata.json` that release wrote beside the pickle; absent for `flatfile`) and `<case>.expected.json`
- Create: `tests/unit/session_compat_cases.py`; modify `tests/unit/conftest.py` (the fixtures under "Shared test helpers")
- Create: `tests/unit/test_session_compat.py`
- Move: `tests/fixtures/browserfree_session.{enc,key}` to `tests/fixtures/session_compat/legacy/` (it keeps its own key and its existing tests; it is not a compat case, because its token cache holds a string, not a `CachedToken`); update its readers `scripts/smoke_install.sh`, `tests/unit/test_browserfree_session_load.py`, `tests/unit/test_lite_cli_hints.py`, `tests/unit/test_cookies.py`
- Delete: `scripts/gen_browserfree_fixture.py`; modify `tests/fixtures/README.md` and `CONTRIBUTING.md` to point at the new script

**Interfaces:**
- Produces: `scripts/session_compat.py generate --graftpunk VERSION --out DIR` and `read --graftpunk VERSION --store DIR --name NAME`. `read` exits 0 and prints two lines, `cookies: <sorted names>` and `tokens: <sorted cached token names>`, when `load_session_for_api(NAME)` works in that version; it exits 3 and prints the exception class name when it raises. Both run the released version in a scratch venv made with `uv venv` under a temp directory, never the repo `.venv`. Both set `GRAFTPUNK_CONFIG_DIR` under `--out` or `--store` and write the key to `<config_dir>/.session_key`, because 1.21.0's `cache_session` and `load_session_for_api` take no `key=`.
- Produces: fixture cases per version (`1.21.0`, `1.40.1`, `1.43.1`, `1.44.0`), with kinds as `EXPECTED_KIND` records them:
  - `nodriver` (browser): a `BrowserSession` built with `__new__` and `__setstate__` of a nodriver state dict.
  - `requests` (requests): a plain `requests.Session`. `api` (api): a `GraftpunkSession`.
  - `tokens` (browser): nodriver with two cached tokens and header roles. `account` (browser): nodriver cached under `myshop@alice` with an account identifier.
  - `adapter` (requests): `requests.Session` with `HTTPAdapter(max_retries=3)` mounted on `https://`.
  - `selenium` (browser): built without a browser, per Ruling 2.
  - `subclass` (no kind): a subclass of `GraftpunkSession` defined in the generator's `__main__`, which dill embeds by value; the expectation records `__main__.<name>`.
  - `flatfile` (browser): the `nodriver` pickle placed at `<name>.session.pickle` with no `metadata.json`, the layout `LocalSessionStorage._load_legacy_session` reads. No listed release writes it, so the generator places it.
- Produces: `<case>.expected.json` with keys `cookies` (as `cookie_dicts`), `headers` (the saved headers as pairs), `header_roles`, `cached_tokens`, `account`, `class` (qualified name). These describe a frozen release and are never rewritten.
- Produces: `RECORDABLE` in `session_compat_cases.py`: true for `nodriver`, `requests`, `api`, `tokens`, `account`, `flatfile`; false for `adapter`, `selenium`, `subclass`.

- [ ] **Step 1: Write `scripts/session_compat.py generate`.** Placeholder data for every case: cookies `sid` (domain `.myshop.example`, secure, `expires=1790000000`) and `pref` (`expires=None`, value `café`, `rest={"HttpOnly": None}`); headers `User-Agent: compat/1`, `Accept: */*`, `X-Compat: 1`. A usage line; a refusal to write outside `--out`.
- [ ] **Step 2: Write the `read` subcommand.**
- [ ] **Step 3: Generate the fixtures for all four versions and commit them** with the shared key `tests/fixtures/session_compat/key` (`Fernet.generate_key()`). The README says the key protects only synthetic data and that the selenium cases are built without a browser.
- [ ] **Step 4: Write the shared helpers and fixtures**, then the baseline tests:

```python
ALL = iter_cases()

@pytest.mark.parametrize("case", ALL, ids=case_id)
def test_a_released_versions_session_loads_for_api_from_bytes(case):
    api = load_session_for_api_from_bytes(case.encrypted, key=case.key)
    assert cookie_dicts(api.cookies) == case.expected["cookies"]
    assert api.headers["X-Compat"] == "1"      # a saved header no loader filter drops
    assert all(api.headers[k] == v for k, v in case.expected["headers"] if k in api.headers)
    assert getattr(api, "_gp_header_roles", {}) == case.expected["header_roles"]
    assert token_dicts(api) == case.expected["cached_tokens"]

@pytest.mark.parametrize("case", ALL, ids=case_id)
def test_a_released_versions_session_loads_from_a_local_store(case, session_store):
    session_store.put_case(case)
    for api in (load_session_for_api("myshop", key=case.key), load_session_for_api_resolved("myshop", key=case.key)[0]):
        assert cookie_dicts(api.cookies) == case.expected["cookies"]
        assert token_dicts(api) == case.expected["cached_tokens"]
        assert getattr(api, "_gp_account_identifier", None) == case.expected["account"]

@pytest.mark.parametrize("case", ALL, ids=case_id)
def test_load_session_returns_what_was_saved(case, session_store):
    session_store.put_case(case)
    obj = load_session("myshop", key=case.key)
    assert type(obj).__qualname__ == case.expected["class"].rsplit(".", 1)[-1]
    assert cookie_dicts(obj.cookies) == case.expected["cookies"]
```

Read `load_session_for_api_resolved`'s return shape from the code before writing the second test and unpack it as it is.

- [ ] **Step 5: Run** `uv run -p 3.12 --all-extras pytest -q -rf tests/unit/test_session_compat.py`. Expected: PASS on main's code for every case. A failure is a finding about today's behaviour: report it with the case and the error before continuing, and mark that case `xfail(strict=True)` with the reason, so later tasks can neither regress it further nor silently change it.
- [ ] **Step 6: Gate, then commit** `test: session fixtures written by released versions, and the script that makes them`.

### Task 2: `session_riders`, with pickled class paths frozen

**Files:**
- Create: `src/graftpunk/session_riders.py`
- Modify: `src/graftpunk/tokens.py` (definitions removed, names re-exported); every graftpunk module that imports the moved names from `tokens` (`grep -rn "from graftpunk.tokens import" src`) except `src/graftpunk/plugins/login_engine.py`
- Test: `tests/unit/test_session_format_modules.py`

**Interfaces:**
- Produces in `graftpunk.session_riders`: `CachedToken` (same dataclass, `__module__ = "graftpunk.tokens"`), `_CACHE_ATTR`, `_CSRF_TOKENS_ATTR`, `_CSRF_ORIGIN_ATTR`, `_HEADER_ROLES_ATTR`, `_HEADER_ROLE_ORIGINS_ATTR`, `SESSION_RIDER_ATTRS`, `SESSION_RIDER_DEFAULTS`, `_MEMORY_ONLY_SESSION_RIDER_ATTRS`, `PICKLED_SESSION_RIDER_ATTRS`, and `SESSION_CLASS_PATHS: Mapping[tuple[str, str], str]` = `{("graftpunk.session", "BrowserSession"): "browser", ("graftpunk.graftpunk_session", "GraftpunkSession"): "api", ("requests.sessions", "Session"): "requests"}`. It imports from graftpunk only `GP_SESSION_NAME_ATTR` and `GP_ACCOUNT_ATTR` from `graftpunk.session_identity`.

- [ ] **Step 1: Write the failing tests**

```python
MOVED = ("CachedToken", "_CACHE_ATTR", "_CSRF_TOKENS_ATTR", "_CSRF_ORIGIN_ATTR", "_HEADER_ROLES_ATTR",
         "_HEADER_ROLE_ORIGINS_ATTR", "SESSION_RIDER_ATTRS", "SESSION_RIDER_DEFAULTS",
         "_MEMORY_ONLY_SESSION_RIDER_ATTRS", "PICKLED_SESSION_RIDER_ATTRS")

def test_session_riders_is_a_leaf():
    assert_leaf_imports("graftpunk.session_riders", {"graftpunk.session_identity"})

def test_a_pickle_with_cached_tokens_names_only_the_frozen_paths():
    names = pickled_global_names(dill.dumps(nodriver_session(tokens=True)))
    assert {"graftpunk.tokens CachedToken", "graftpunk.session BrowserSession"} <= names
    assert not any("session_riders" in n for n in names)

def test_tokens_still_exports_every_moved_name():
    for name in MOVED:
        assert getattr(graftpunk.tokens, name) is getattr(graftpunk.session_riders, name)
```

- [ ] **Step 2: Run** `pytest tests/unit/test_session_format_modules.py -q`. Expected: FAIL, `No module named 'graftpunk.session_riders'`.
- [ ] **Step 3: Move the definitions**, switch the other modules' imports to `session_riders`, and leave in `tokens.py` a re-export block under the comment: "Part of the stored session format: a pickle names `graftpunk.tokens.CachedToken`, and `plugins/login_engine.py` imports these names from here. Guarded by test_session_format_modules."
- [ ] **Step 4: Run** the new tests and `tests/unit/test_session_compat.py`. Expected: PASS.
- [ ] **Step 5: Gate, then commit** `refactor: session riders and CachedToken in a browser-free module, pickled paths unchanged`.

### Task 3: `session_legacy`: the state dict out of any release's pickle

**Files:**
- Create: `src/graftpunk/session_legacy.py`
- Test: `tests/unit/test_session_legacy.py`

**Interfaces:**
- Produces: `FROZEN_CLASS_PATHS: Mapping[tuple[str, str], object]`, built with no literals of its own: each key of `session_riders.SESSION_CLASS_PATHS` mapped to the capture placeholder, plus `(CachedToken.__module__, CachedToken.__qualname__)` mapped to `session_riders.CachedToken`.
- Produces: `state_from_pickle(decrypted: bytes) -> LegacyState`, a frozen dataclass: `kind: str | None` (from `SESSION_CLASS_PATHS`; `None` for any other class, a subclass included), `class_path: str`, `state: dict[str, Any]`, `unresolved: tuple[str, ...]` (class paths stubbed because they could not be imported).
- The capture placeholder stores the state dict given to `__setstate__` and accepts the `(dict, slotstate)` tuple `cache._Stub` accepts today. It needs no attribute fallback: a selenium pickle's `_driver_initializer` is a `functools.partial` of `MethodType(requestium.requestium_session.Session._start_chrome_browser, <session>)` with keywords `{"headless": ...}`, whose function resolves through `find_class` (stubbed when requestium is absent) and whose instance is the memoized placeholder.
- `state_from_pickle` lets `pickle.UnpicklingError` propagate for bytes that do not unpickle. For a top-level object the placeholder did not capture (a pickled `dict`, a `types.SimpleNamespace`), it returns `kind=None` and `state=dict(vars(obj))` when the object has a `__dict__`, and raises `TypeError` otherwise.
- Imports from graftpunk only `graftpunk.session_riders`.

- [ ] **Step 1: Write the failing tests**

```python
def test_session_legacy_is_a_leaf():
    assert_leaf_imports("graftpunk.session_legacy", {"graftpunk.session_riders"})

@pytest.mark.parametrize("case", iter_cases(), ids=case_id)
def test_every_fixture_gives_its_state(case):
    assert cookie_dicts(state_from_pickle(decrypt(case)).state["cookies"]) == case.expected["cookies"]

@pytest.mark.parametrize("case", cases(kinds=("browser",)), ids=case_id)
def test_a_browser_state_has_the_name_getstate_wrote(case):
    legacy = state_from_pickle(decrypt(case))
    assert legacy.kind == "browser" and legacy.state["session_name"]      # never "_session_name"

@pytest.mark.parametrize("case", cases(kinds=("api", "requests")), ids=case_id)
def test_api_and_requests_states_hold_no_name(case):
    assert "session_name" not in state_from_pickle(decrypt(case)).state

def test_a_subclass_has_no_kind_and_reports_its_path():
    legacy = state_from_pickle(decrypt(case_named("1.44.0", "subclass")))
    assert legacy.kind is None and legacy.class_path.startswith("__main__.")

def test_a_pickled_namespace_gives_its_attributes_and_a_pickled_list_is_refused():
    assert state_from_pickle(dill.dumps(types.SimpleNamespace(cookies=1, headers=2))).state == {"cookies": 1, "headers": 2}
    with pytest.raises(TypeError): state_from_pickle(dill.dumps([1, 2]))
    with pytest.raises(pickle.UnpicklingError): state_from_pickle(b"this is not a pickle at all")

def test_a_selenium_pickle_gives_its_state():
    legacy = state_from_pickle(decrypt(case_named("1.44.0", "selenium")))
    assert legacy.kind == "browser" and legacy.state["_backend_type"] == "selenium"

def test_reading_tokens_never_imports_graftpunk_tokens():
    # a subprocess that imports graftpunk.session_legacy, then sets sys.modules["graftpunk.tokens"] = None,
    # then reads the 1.44.0 "tokens" fixture and prints token_dicts as JSON
    assert json.loads(run_read_without_tokens_module()) == case_named("1.44.0", "tokens").expected["cached_tokens"]
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

**Interfaces:**
- Produces in `graftpunk.exceptions` (which still imports nothing from graftpunk): `NewerSessionRecord(GraftpunkError)` (takes `version: int`; message names the version and says to upgrade graftpunk), `InvalidSessionRecord(GraftpunkError)` (takes `reason: str`), `RealSessionClassUnavailable(ImportError)` (used from Task 6).
- Produces in `graftpunk.cookies`: `_policy_is_default(policy: object) -> bool`: true when the policy's class is one the existing `_replaceable` recognises (the standard library's, HTTPie's by name, a stub of it) or is `BrowserCookiePolicy`, and `_settings(policy)` equals a fresh `BrowserCookiePolicy`'s. Built on `_replaceable`, so the module has one class rule. Its docstring says it is part of the stored session format.
- Produces in `session_record`: `SessionFormat = Literal["pickle", "record"]`; `RECORD_VERSION = 1`.
- Produces: `classify(decrypted: bytes) -> Record | LegacyBytes`. `Record` (frozen): `format: SessionFormat = "record"`, `version: int`, `kind: str`, `fields: dict[str, Any]`, `extra: dict[str, Any]` (unknown top-level keys, untouched). `LegacyBytes` (frozen): `format: SessionFormat = "pickle"`, `data: bytes`. `classify` validates the whole record: `NewerSessionRecord` for `version > RECORD_VERSION`, `InvalidSessionRecord` for anything else that is not a valid record (not an object, a missing or mistyped field, an unknown `kind`).
- Produces: `FIELDS: tuple[Field, ...]`, each with `state_key`, `record_path` (tuple of keys), `kinds: frozenset[str]`, `encode`, `decode` (an encoder may raise `ValueError` to refuse a value); and `PICKLE_ONLY: tuple[PickleOnly, ...]`, each with `state_key`, `kinds`, `at_default: Callable[[Any], bool]`, `default: Callable[[], Any]`.
  - `PICKLE_ONLY` rows, both for kinds `api` and `requests`: `hooks` (default `{"response": []}`), `adapters` (default: fresh `HTTPAdapter()` on `https://` and `http://`). No selenium rows (Ruling 1).
  - `FIELDS` cover, for kind `browser`: `_backend_type`, `_use_stealth`, `session_name`, `current_url`, `headers`, `cookies`, and the `PICKLED_SESSION_RIDER_ATTRS`; for `api` and `requests`: `headers`, `cookies`, `auth`, `params`, `verify`, `proxies`, `cert`, `max_redirects`, `trust_env`, `stream`.
- Produces: `state_to_record(kind: str, state: Mapping[str, Any], extra: Mapping[str, Any] | None = None) -> bytes | Unrecordable` (`Unrecordable`: `key: str`, `reason: str`). A state key with no row is `Unrecordable(key=that key)`.
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

LIVE = [("browser", nodriver_session(tokens=True).__getstate__()),   # the real BrowserSession.__getstate__, never a hand-built dict
        ("api", GraftpunkSession().__getstate__()), ("requests", requests.Session().__getstate__())]

@pytest.mark.parametrize("kind,state", LIVE, ids=[k for k, _ in LIVE])
def test_every_key_getstate_emits_has_a_row(kind, state):
    known = {f.state_key for f in FIELDS if kind in f.kinds} | {p.state_key for p in PICKLE_ONLY if kind in p.kinds}
    assert set(state) <= known

@pytest.mark.parametrize("case", iter_cases(), ids=case_id)
def test_each_fixture_is_recordable_or_not_as_declared(case):
    legacy = state_from_pickle(decrypt(case))
    recordable = legacy.kind is not None and isinstance(state_to_record(legacy.kind, legacy.state), bytes)
    assert recordable is RECORDABLE[case.case]

@pytest.mark.parametrize("case", cases(recordable=True), ids=case_id)
def test_state_survives_the_record(case):
    legacy = state_from_pickle(decrypt(case))
    back = record_to_state(classify(state_to_record(legacy.kind, legacy.state)))
    stored = {f.state_key for f in FIELDS if legacy.kind in f.kinds}
    assert cookie_dicts(back["cookies"]) == cookie_dicts(legacy.state["cookies"])
    assert {k: back[k] for k in stored - {"cookies"} if k in legacy.state} == {k: legacy.state[k] for k in stored - {"cookies"} if k in legacy.state}
    assert all(p.at_default(back[p.state_key]) for p in PICKLE_ONLY if legacy.kind in p.kinds)

@pytest.mark.parametrize("case,key", [("adapter", "adapters"), ("selenium", "_backend_type")])
def test_what_a_record_cannot_hold_is_refused_by_name(case, key):
    legacy = state_from_pickle(decrypt(case_named("1.44.0", case)))
    assert state_to_record(legacy.kind, legacy.state).key == key

@pytest.mark.parametrize("cls", [requests.Session, GraftpunkSession])
def test_each_default_predicate_holds_for_the_installed_libraries(cls):
    state = cls().__getstate__()
    assert all(p.at_default(state[p.state_key]) for p in PICKLE_ONLY if p.state_key in state)

def test_a_restored_requests_state_makes_a_working_session(echo_server):
    obj = requests.Session.__new__(requests.Session)
    obj.__setstate__(record_to_state(classify(state_to_record("requests", requests.Session().__getstate__()))))
    assert obj.get(echo_server.url).status_code == 200      # needs adapters and hooks back

def test_a_newer_record_is_refused_by_version():
    with pytest.raises(NewerSessionRecord, match="2"):
        classify(record_bytes(version=2))

@pytest.mark.parametrize("payload", [b"[]", record_bytes(drop=("kind",)), record_bytes(kind="nope"), record_bytes(drop=("cookies",))])
def test_json_that_is_not_a_record_is_invalid(payload):
    with pytest.raises(InvalidSessionRecord):
        classify(payload)

UNHOLDABLE = [   # (state mutation, the key the refusal names); each mutation is a small function in this test file
    (set_token_ttl_to_inf, "_gp_cached_tokens"), (set_token_extracted_at_to_nan, "_gp_cached_tokens"),
    (set_a_bytes_header_value, "headers"), (set_a_custom_cookie_policy, "cookies")]

@pytest.mark.parametrize("mutate,key", UNHOLDABLE)
def test_values_a_record_cannot_hold_make_the_session_unrecordable(mutate, key):
    assert state_to_record("browser", mutate(nodriver_state(tokens=True))).key == key

def test_an_auth_object_makes_a_requests_session_unrecordable():
    state = requests.Session().__getstate__() | {"auth": object()}
    assert state_to_record("requests", state).key == "auth"

def test_an_empty_session_round_trips():
    state = nodriver_state() | {"headers": {}, "cookies": requests.cookies.RequestsCookieJar()}
    back = record_to_state(classify(state_to_record("browser", state)))
    assert back["headers"] == {} and cookie_dicts(back["cookies"]) == []

def test_both_header_shapes_give_the_same_record():
    a = nodriver_state() | {"headers": {"A": "1"}}; b = nodriver_state() | {"headers": CaseInsensitiveDict({"A": "1"})}
    assert state_to_record("browser", a) == state_to_record("browser", b)

def test_unknown_top_level_keys_come_back_in_extra_and_go_out_unchanged():
    rec = classify(record_bytes(extra={"future": {"a": 1}}))
    assert rec.extra == {"future": {"a": 1}}
    assert json.loads(state_to_record(rec.kind, record_to_state(rec), rec.extra))["future"] == {"a": 1}

def test_a_pickle_is_never_unpickled_by_classify(tmp_path):
    marker = tmp_path / "ran"
    payload = dill.dumps(RunsOnUnpickle(marker))     # a class in this test file whose __reduce__ touches the marker file
    assert isinstance(classify(payload), LegacyBytes) and not marker.exists()

def test_a_record_holding_pickle_bytes_as_a_string_is_data():
    rec = classify(record_bytes(cookie_value="\x80\x04cos\nsystem\n"))
    assert [c["value"] for c in cookie_dicts(record_to_state(rec)["cookies"])] == ["\x80\x04cos\nsystem\n"]
```

The fixture cookies already cover `expires=None`, a non-ASCII value and a `rest` entry, so `test_state_survives_the_record` exercises them.

- [ ] **Step 2: Run** to see `No module named 'graftpunk.session_record'`.
- [ ] **Step 3: Implement.** `classify` tests the first byte for `{`. `state_to_record` checks the selenium backend (Ruling 1) before it walks the keys, so a selenium state is refused on `_backend_type` and not on its first key without a row. `json.dumps(..., allow_nan=False)`; a `ValueError` or `TypeError` from an encoder becomes `Unrecordable(key=state_key)`. The `at_default` predicates are structural, as the spec lists them: `hooks` is a mapping whose only key is `response` with an empty value; `adapters` has exactly the prefixes `https://` and `http://`, each of type name `HTTPAdapter` from `requests.adapters`, with zero-retry `max_retries` and `requests.adapters.DEFAULT_POOLSIZE` pool sizes. The cookie encoder refuses a jar for which `_policy_is_default` is false.
- [ ] **Step 4: Run** the tests. Expected: PASS. Put the count of unrecordable fixtures per version in the commit body.
- [ ] **Step 5: Gate, then commit** `feat: the session record: one field table, and the codec built from it`.

### Task 5: `session_view`: `StoredSession`

**Files:**
- Create: `src/graftpunk/session_view.py`
- Test: `tests/unit/test_session_view.py`

**Interfaces:**
- Produces: `StoredSessionLike` (Protocol): `cookies`, `headers`, `session_name: str`, `current_url: str`, and each attribute in `SESSION_RIDER_ATTRS`, readable and settable.
- Produces: `StoredSession(kind: str | None, state: dict[str, Any], extra: dict[str, Any] | None = None)` with attributes `kind`, `extra`, and `writable: bool` (`kind is not None`); `stored_state() -> dict[str, Any]` returning the keys of its state that the table lists for its kind, with no defaults filled in; `__reduce_ex__` raising `TypeError` (the view is never pickled or copied); `classmethod from_record(record: Record)`. `cookies` is a `requests.cookies.RequestsCookieJar`. A rider attribute absent from the state reads as its `SESSION_RIDER_DEFAULTS` value. With `kind=None` (a legacy pickle of a subclass or another type) the view can be read, and `stored_state()` raises `TypeError`.
- Imports from graftpunk only `session_record`, `session_riders`, `cookies`.

- [ ] **Step 1: Write the failing tests**

```python
def test_session_view_is_a_leaf():
    assert_leaf_imports("graftpunk.session_view", {"graftpunk.session_record", "graftpunk.session_riders", "graftpunk.cookies"})

@pytest.mark.parametrize("case", cases(recordable=True), ids=case_id)
def test_the_view_and_the_real_object_give_the_same_metadata(case, session_store):
    legacy = state_from_pickle(decrypt(case)); view = StoredSession(legacy.kind, legacy.state)
    session_store.put_case(case); real = load_session("myshop", key=case.key)
    assert _extract_session_metadata(view, "s") == _extract_session_metadata(real, "s")

@pytest.mark.parametrize("case", cases(recordable=True), ids=case_id)
def test_the_view_gives_back_the_stored_keys_of_its_state(case):
    legacy = state_from_pickle(decrypt(case))
    stored = {f.state_key for f in FIELDS if legacy.kind in f.kinds}
    assert set(StoredSession(legacy.kind, legacy.state).stored_state()) == set(legacy.state) & stored

def test_the_protocol_names_every_registered_rider():
    assert set(SESSION_RIDER_ATTRS) <= set(typing.get_type_hints(StoredSessionLike))

def test_setting_a_name_on_a_kind_with_no_name_writes_no_name():
    view = StoredSession("requests", requests.Session().__getstate__()); view.session_name = "x"
    assert "session_name" not in view.stored_state()

def test_a_memory_only_rider_can_be_set_and_is_not_stored():
    view = StoredSession("browser", nodriver_state()); setattr(view, _CSRF_TOKENS_ATTR, {"t": "v"})
    assert getattr(view, _CSRF_TOKENS_ATTR) == {"t": "v"} and _CSRF_TOKENS_ATTR not in view.stored_state()

def test_a_view_with_no_kind_can_be_read_but_not_written():
    view = StoredSession(None, state_from_pickle(decrypt(case_named("1.44.0", "subclass"))).state)
    assert cookie_dicts(view.cookies) and view.writable is False
    with pytest.raises(TypeError): view.stored_state()

def test_a_view_refuses_to_be_pickled():
    with pytest.raises(TypeError): dill.dumps(StoredSession("browser", nodriver_state()))
```

- [ ] **Step 2: Run** to see the import fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Gate, then commit** `feat: StoredSession, a browser-free view of a stored session`.

### Task 6: Readers go through `classify`

**Files:**
- Modify: `src/graftpunk/cache.py` (`load_session`, `_load_session_with_metadata`, the three API loaders; `_Stub`, `_BrowserFreeUnpickler` and `_deserialize_browserfree` removed); `src/graftpunk/graftpunk_session.py` (`GraftpunkSession.__setstate__`, Ruling 9)
- Modify existing tests that reach the removed names or patch `load_session` to feed the API loaders. No assertion is weakened; each test keeps what it asserted.
  - `tests/unit/test_browserfree_session_load.py`: its uses of `cache._BrowserFreeUnpickler`, `cache._Stub` and `cache._deserialize_browserfree` move onto `session_legacy.state_from_pickle` and its placeholder.
  - `tests/unit/test_cookies.py`: the `cache._Stub` base near line 302 becomes the `session_legacy` placeholder.
  - `tests/unit/test_cache.py` (the ten `load_session_for_api` tests that `monkeypatch.setattr("graftpunk.cache.load_session", ...)`) and `tests/unit/test_api_session_resolution.py` (the same patch near line 179): each seeds `session_store` with a session holding the state its mock had and calls the public loader, so it goes through the real read path. The exceptions are `test_load_session_for_api_copies_csrf_tokens` and `test_load_session_for_api_copies_empty_csrf_tokens`: their state is a memory-only rider no stored session holds, so they call `_api_session_from_session` directly on a source object with that rider set, with their assertions unchanged. Where a mock's state cannot be stored (a `MagicMock` attribute the test only needed to exist), the test builds a `requests.Session` or `nodriver_session()` with that state.
- Test: `tests/unit/test_session_readers.py`

**Interfaces (all private, in `cache.py`):**
- `_read_stored(name: str, *, key: bytes | None, backend_override: str | None = None) -> tuple[Record | LegacyBytes, SessionMetadata]`: today's backend load, key resolution, decrypt and checksum check from `_load_session_with_metadata`, then `_classified`.
- `_classified(decrypted: bytes, *, name: str) -> Record | LegacyBytes`: `classify`, mapping `InvalidSessionRecord` to `SessionExpiredError(f"Failed to load session '{name}': ...")`; `NewerSessionRecord` propagates. The only place that mapping exists.
- `_unpickle_whole(legacy: LegacyBytes, *, name: str) -> Any`: today's `pickle.loads` with both of its handlers: `ModuleNotFoundError` becomes `RealSessionClassUnavailable(<today's install-hint text>) from exc`, so `_distribution.missing_browser_module` still finds the `ModuleNotFoundError` in the cause chain; `pickle.UnpicklingError` or `RuntimeError` becomes `SessionExpiredError(f"Failed to load session '{name}': {exc}")`, as today.
- `_restore_record(record: Record, *, name: str) -> Any`: the class for `record.kind` imported by its path from `SESSION_CLASS_PATHS`, then `obj = cls.__new__(cls); obj.__setstate__(record_to_state(record))`. An import failure raises `RealSessionClassUnavailable(...) from exc`.
- `_check_structure(session: Any, name: str) -> None`: today's check that the loaded object has `cookies` and `headers`, raising `SessionExpiredError` with today's text (it contains the words `invalid structure`).
- `_finish_loaded(session: Any, name: str, metadata: SessionMetadata) -> None`: the three things `_load_session_with_metadata` does to a loaded object today, applied alike to a real object and a `StoredSession`: `_check_structure`, `_stamp_session_slot(session, name)`, and the account fallback (metadata under `ACCOUNT_RULE` sets `GP_ACCOUNT_ATTR`). It always takes a name and metadata.
- `_stored_view(stored: Record | LegacyBytes, *, name: str) -> StoredSession`: `StoredSession.from_record` for a record; for legacy bytes, `StoredSession(legacy.kind, legacy.state)` from `state_from_pickle`, with any exception from `state_from_pickle` becoming `SessionExpiredError(f"Failed to deserialize session '{name}': {exc}")`, so the existing error-text tests in `test_browserfree_session_load.py` pass unchanged.
- `_load_session_with_metadata` stays until Task 8 as a thin function (`_read_stored`, `_restore_record` or `_unpickle_whole`, `_finish_loaded`), because `update_session_cookies` and `copy_session` still call it and five existing tests patch it. Task 8 removes it.
- `_api_session_from_session(source)` is unchanged in signature and stays the only builder of a `GraftpunkSession`.
- `load_session_for_api_from_bytes` has no session name and no metadata: it passes `name=_BYTES_LABEL` (a module constant, `"<bytes>"`) to `_classified`, and it calls `_check_structure` directly, not `_finish_loaded`, leaving the session unstamped as today.
- `GraftpunkSession._init_graftpunk_state(self) -> None`: sets every attribute `__init__` sets beyond requests' own, at its default, with `_gp_default_session_headers` a copy of the current headers. `__init__` calls it where it sets those attributes today (after the browser identity is applied, so a fresh session is unchanged), and `__setstate__(state)` calls `super().__setstate__(state)` and then it. The existing lazy accessors (`_declared_roles`, the role-handoff accessor, `_warn_withheld_token`) stay as they are.

- [ ] **Step 1: Write the failing tests**

```python
def as_record(case):
    legacy = state_from_pickle(decrypt(case)); return state_to_record(legacy.kind, legacy.state)

def api_fingerprint(api):
    return cookie_dicts(api.cookies), sorted(api.headers.items()), getattr(api, "_gp_header_roles", {}), token_dicts(api)

@pytest.mark.parametrize("case", cases(recordable=True), ids=case_id)
def test_a_record_loads_for_api_exactly_as_its_pickle_did(case):
    record = encrypt(as_record(case), case.key)
    assert api_fingerprint(load_session_for_api_from_bytes(record, key=case.key)) == \
           api_fingerprint(load_session_for_api_from_bytes(case.encrypted, key=case.key))

@pytest.mark.parametrize("fmt", ["pickle", "record"])
def test_an_api_load_does_not_import_graftpunk_session(fmt, session_store):
    session_store.seed(fmt)
    # a subprocess with the store's environment that calls load_session_for_api("myshop") and prints sorted(sys.modules)
    assert "graftpunk.session" not in modules_after_api_load_in_subprocess(session_store)

@pytest.mark.parametrize("cls", [GraftpunkSession, requests.Session])
def test_load_session_of_a_record_returns_the_class_that_was_saved(cls, session_store):
    kind = kind_of(cls()); session_store.seed_plain(state_to_record(kind, cls().__getstate__()))
    assert type(load_session("myshop", key=TEST_KEY)) is cls

def test_load_session_of_a_browser_record_without_the_browser_stack_gives_the_install_hint(session_store, no_browser_stack):
    session_store.seed_plain(record_bytes())
    with no_browser_stack(), pytest.raises(RealSessionClassUnavailable) as exc:
        load_session("myshop", key=TEST_KEY)
    assert missing_browser_module(exc.value)

def test_a_restored_graftpunk_session_has_every_attribute_a_fresh_one_has():
    fresh = GraftpunkSession(); restored = GraftpunkSession.__new__(GraftpunkSession); restored.__setstate__(fresh.__getstate__())
    assert set(vars(fresh)) <= set(vars(restored))

@pytest.mark.parametrize("fmt", ["pickle", "record"])
def test_a_loaded_graftpunk_session_can_send_a_request(fmt, session_store, session_format, echo_server):
    session_format(fmt); cache_session(GraftpunkSession(), "myshop", key=TEST_KEY)   # the record case passes from Task 7 on; mark it xfail(strict=True) until then
    assert load_session("myshop", key=TEST_KEY).get(echo_server.url).status_code == 200

def test_an_api_load_keeps_the_account_from_the_metadata(session_store):
    session_store.seed("record", "myshop@alice", account="alice")
    assert getattr(load_session_for_api("myshop@alice", key=TEST_KEY), "_gp_account_identifier") == "alice"

def readers(session_store):
    return {"myshop": [lambda: load_session("myshop", key=TEST_KEY),
                       lambda: load_session_for_api("myshop", key=TEST_KEY),
                       lambda: load_session_for_api_resolved("myshop", key=TEST_KEY)],
            _BYTES_LABEL: [lambda: load_session_for_api_from_bytes(session_store.raw("myshop"), key=TEST_KEY)]}

def test_an_invalid_record_reads_as_an_expired_session(session_store):
    session_store.seed_plain(record_bytes(drop=("kind",)))
    for name, fns in readers(session_store).items():
        for fn in fns:
            with pytest.raises(SessionExpiredError, match=re.escape(name)): fn()

def test_a_version_2_record_is_refused_by_every_reader(session_store):
    session_store.seed_plain(record_bytes(version=2))
    for fns in readers(session_store).values():
        for fn in fns:
            with pytest.raises(NewerSessionRecord): fn()
```

- [ ] **Step 2: Run** to see them fail (records are not understood yet).
- [ ] **Step 3: Implement.** `load_session`: `_read_stored`, then `_restore_record` or `_unpickle_whole`, then `_finish_loaded`. `load_session_for_api` and `load_session_for_api_resolved` stop calling `load_session`: `_read_stored`, `_stored_view`, `_finish_loaded`, `_api_session_from_session`.
- [ ] **Step 4: Run** the new tests, `test_session_compat.py`, `test_browserfree_session_load.py`, `test_cookies.py`, `test_cache.py`, `test_api_session_resolution.py`. Expected: PASS.
- [ ] **Step 5: Gate, then commit** `feat: every session reader takes a record or a legacy pickle; API loads need no browser package`.

### Task 7: Fresh saves: the setting and the writer

**Files:**
- Modify: `src/graftpunk/config.py`, `src/graftpunk/cache.py` (`cache_session`), `src/graftpunk/cli/config_commands.py` (show the value)
- Test: `tests/unit/test_session_writers.py`

**Interfaces:**
- Produces: `GraftpunkSettings.session_format: Literal["pickle", "record"] = "pickle"` (`GRAFTPUNK_SESSION_FORMAT`). `config.py` writes the `Literal` itself and does not import `session_record`; a test asserts its values equal `typing.get_args(session_record.SessionFormat)`.
- Produces in `cache.py`: `_encode_fresh(session: Any) -> bytes`; `_encode_as(session: Any) -> bytes` (format-preserving, for write-backs: a writable `StoredSession` is written as a record from `stored_state()` and its `extra`; a `StoredSession` with `writable` false raises `TypeError`; any other object is written as a legacy pickle); `_store(name: str, payload: bytes, session: Any, *, account_rule, key, metadata: SessionMetadata | None = None) -> str`. With `metadata=None`, `_store` builds fresh metadata as `cache_session` does today (new times, `status="active"`); with a `metadata` given it saves that with `checksum` replaced.
- `cache_session` is `_encode_fresh` then `_store`. Under `record`, a fallback to pickle logs one warning, event `session_not_recordable`, with fields `name`, `key` and `reason`. For a class `kind_of` does not know (a subclass or another type), `key` is `"type"` and `reason` names the class path.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_default_still_writes_a_pickle(session_store):
    cache_session(nodriver_session(), "myshop", key=TEST_KEY); assert session_store.format_of("myshop") == "pickle"

def test_record_setting_writes_a_record(session_store, session_format):
    session_format("record"); cache_session(nodriver_session(), "myshop", key=TEST_KEY)
    assert json.loads(session_store.plain("myshop"))["kind"] == "browser"

def test_an_unrecordable_session_is_pickled_with_one_warning(session_store, session_format, captured_logs):
    session_format("record"); s = requests.Session(); s.mount("https://", HTTPAdapter(max_retries=3))
    cache_session(s, "myshop", key=TEST_KEY)
    assert session_store.format_of("myshop") == "pickle"
    assert [e["key"] for e in events(captured_logs, "session_not_recordable")] == ["adapters"]

def test_a_subclass_is_pickled_and_the_warning_names_its_type(session_store, session_format, captured_logs):
    class Mine(GraftpunkSession): pass
    session_format("record"); cache_session(Mine(), "myshop", key=TEST_KEY)
    (event,) = events(captured_logs, "session_not_recordable")
    assert event["key"] == "type" and "Mine" in event["reason"] and session_store.format_of("myshop") == "pickle"

def test_a_mistyped_format_fails_naming_the_choices(session_store, session_format):
    with pytest.raises(ValidationError, match="pickle.*record"):
        session_format("json"); cache_session(nodriver_session(), "myshop", key=TEST_KEY)
    assert not any(session_store.root.rglob("myshop*"))     # checked on disk: reading settings would raise again

def test_the_setting_and_the_codec_name_the_same_formats():
    assert typing.get_args(GraftpunkSettings.model_fields["session_format"].annotation) == typing.get_args(SessionFormat)

def test_encode_as_follows_the_type_of_what_it_is_given(session_format):
    session_format("record")
    assert _encode_as(StoredSession("browser", nodriver_state()))[:1] == b"{"
    assert _encode_as(nodriver_session())[:1] == b"\x80"        # a real object stays a pickle whatever the setting
    with pytest.raises(TypeError): _encode_as(StoredSession(None, nodriver_state()))

@pytest.mark.parametrize("fn,text", SIGNATURES_AT_1_44_0)   # (function, str(inspect.signature) at v1.44.0) for the seven public functions
def test_public_signatures_are_unchanged(fn, text):
    assert str(inspect.signature(fn)) == text
```

`session_format("json")` may raise at the fixture call or at first use; the `with` block covers both.

- [ ] **Step 2: Run** to see them fail.
- [ ] **Step 3: Implement.** Remove the `xfail` from Task 6's record case of `test_a_loaded_graftpunk_session_can_send_a_request`.
- [ ] **Step 4: Run** the new tests plus `test_session_compat.py`, `test_session_readers.py` and `test_cache.py`. Expected: PASS.
- [ ] **Step 5: Gate, then commit** `feat: GRAFTPUNK_SESSION_FORMAT=record writes a session as a record; the default is unchanged`.

### Task 8: Write-backs keep the format they read

**Files:**
- Modify: `src/graftpunk/cache.py` (`update_session_cookies`, `copy_session`)
- Modify: `tests/unit/test_cache.py`: the five `update_session_cookies` tests in `TestUpdateSessionCookies` (lines 1026 to 1134) that patch `graftpunk.cache._load_session_with_metadata`, four of them also `graftpunk.cache.cache_session`. Each seeds `session_store` and asserts on what is stored afterwards, keeping what it asserted: cookies merged, the token cache, the account rule carried, and load and save failures swallowed with their existing warnings. Where a test asserted `cache_session` was called with given arguments, it asserts the stored metadata and bytes those arguments produce. `test_update_session_cookies_persists_csrf_tokens` is the exception: CSRF tokens are memory-only and never stored, so it keeps its assertion on the object the write-back mutated, captured by wrapping `graftpunk.cache._encode_as`, and asserts that object's `_gp_csrf_tokens` equals the API session's and is a copy. `_load_session_with_metadata` is removed in this task.
- Test: `tests/unit/test_session_write_backs.py`; existing `tests/unit/test_account_label_migration.py`, `tests/unit/test_api_session_resolution.py` and `tests/unit/test_encryption.py` stay green unedited

**Interfaces:**
- Produces: `_load_for_write_back(stored: Record | LegacyBytes, *, name: str) -> Any`: a record gives `StoredSession.from_record(stored)`; legacy bytes give `_unpickle_whole(stored, name=name)`. Its input comes from `_read_stored`, so an invalid record is already a `SessionExpiredError` and a newer one a `NewerSessionRecord` before anything is changed.
- `update_session_cookies` stays best-effort and keeps its existing events: a load failure of any kind, the three new ones included (`RealSessionClassUnavailable`, `NewerSessionRecord`, `SessionExpiredError`), logs today's `session_save_skipped_load_failed` warning and writes nothing. The warning gains two fields: `reason` (the exception's class name) and, for `RealSessionClassUnavailable`, `hint` ("the next login, or a write-back from a full install, rewrites the session"). `copy_session` lets all three propagate.
- `copy_session` keeps its contract that created, modified and expiry times and status are kept: it passes `_store(..., metadata=replace(source_metadata, name=dest, account_identifier=account_identifier, account_rule=ACCOUNT_RULE))`. It sets the name with `session.session_name = dest`, dropping the `hasattr(session, "_session_name")` probe, and keeps its read-back verification.

- [ ] **Step 1: Write the failing tests**

```python
def api_with_new_cookie():
    api = load_session_for_api("myshop", key=TEST_KEY); api.cookies.set("new", "1", domain=".myshop.example"); return api

@pytest.mark.parametrize("setting", ["pickle", "record"])
@pytest.mark.parametrize("stored", ["pickle", "record"])
def test_a_write_back_keeps_the_stored_format(setting, stored, session_store, session_format):
    session_store.seed(stored); session_format(setting)
    update_session_cookies(api_with_new_cookie(), "myshop", key=TEST_KEY)
    assert session_store.format_of("myshop") == stored and "new" in session_store.cookie_names("myshop")
    copy_session("myshop", "myshop@alice", account_identifier="alice", key=TEST_KEY)
    assert session_store.format_of("myshop@alice") == stored

def test_an_unknown_key_survives_both_write_backs_even_when_the_setting_is_pickle(session_store):
    session_store.seed("record", extra={"future": {"a": 1}})
    update_session_cookies(api_with_new_cookie(), "myshop", key=TEST_KEY)
    copy_session("myshop", "myshop@alice", account_identifier="alice", key=TEST_KEY)
    assert all(json.loads(session_store.plain(n))["future"] == {"a": 1} for n in ("myshop", "myshop@alice"))

@pytest.mark.parametrize("plain,reason,error", [
    (record_bytes(version=2), "NewerSessionRecord", NewerSessionRecord),
    (record_bytes(drop=("kind",)), "SessionExpiredError", SessionExpiredError)])
def test_a_record_that_cannot_be_read_is_left_byte_for_byte(plain, reason, error, session_store, captured_logs):
    before = session_store.seed_plain(plain)
    api = requests.Session(); api.cookies.set("new", "1", domain=".myshop.example")
    update_session_cookies(api, "myshop", key=TEST_KEY)
    assert [e["reason"] for e in events(captured_logs, "session_save_skipped_load_failed")] == [reason]
    with pytest.raises(error): copy_session("myshop", "myshop@alice", account_identifier="alice", key=TEST_KEY)
    assert session_store.raw("myshop") == before

def test_without_the_browser_stack_a_record_write_back_works_and_a_pickle_one_is_skipped(session_store, captured_logs, no_browser_stack):
    session_store.seed("record"); before = session_store.seed("pickle", "other")
    api = requests.Session(); api.cookies.set("new", "1", domain=".myshop.example")
    with no_browser_stack():
        update_session_cookies(api, "myshop", key=TEST_KEY)
        update_session_cookies(api, "other", key=TEST_KEY)
        with pytest.raises(ImportError): copy_session("other", "other@alice", account_identifier="alice", key=TEST_KEY)
    assert "new" in session_store.cookie_names("myshop")
    (event,) = events(captured_logs, "session_save_skipped_load_failed")
    assert session_store.raw("other") == before and event["reason"] == "RealSessionClassUnavailable" and "next login" in event["hint"]

@pytest.mark.parametrize("case", cases(recordable=True), ids=case_id)
def test_a_fixture_stored_as_a_record_reads_and_writes_back_like_its_pickle(case, session_store):
    legacy = state_from_pickle(decrypt(case)); session_store.seed_plain(state_to_record(legacy.kind, legacy.state))
    def check():
        assert cookie_dicts(load_session("myshop", key=TEST_KEY).cookies) == case.expected["cookies"]
        for api in (load_session_for_api("myshop", key=TEST_KEY), load_session_for_api_resolved("myshop", key=TEST_KEY)[0]):
            assert cookie_dicts(api.cookies) == case.expected["cookies"] and token_dicts(api) == case.expected["cached_tokens"]
    check()
    update_session_cookies(load_session_for_api("myshop", key=TEST_KEY), "myshop", key=TEST_KEY); check()
    copy_session("myshop", "myshop@alice", account_identifier="alice", key=TEST_KEY)
    assert session_store.format_of("myshop@alice") == "record"

def test_copy_renames_a_browser_record_and_keeps_its_times(session_store):
    session_store.seed("record"); created = session_store.metadata("myshop").created_at
    copy_session("myshop", "myshop@alice", account_identifier="alice", key=TEST_KEY)
    assert json.loads(session_store.plain("myshop@alice"))["session_name"] == "myshop@alice"
    assert session_store.metadata("myshop@alice").created_at == created
```

- [ ] **Step 2: Run** to see them fail.
- [ ] **Step 3: Implement** each write-back as: `_read_stored`, `_load_for_write_back`, `_finish_loaded`, its existing mutation, `_encode_as(session)`, `_store`. Neither calls the public `cache_session`.
- [ ] **Step 4: Run** the new tests, `test_cache.py`, `test_account_label_migration.py`, `test_api_session_resolution.py` and `test_encryption.py`. Expected: PASS.
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
  - a command that refreshes a cookie in a stored record, with `GRAFTPUNK_SESSION_FORMAT` unset, writes it back as a record (the decrypted bytes start with `{`), and the next run sends it;
  - the same refresh on a stored pickle prints the `session_save_skipped_load_failed` warning and exits 0;
  - `copy_session` on a stored pickle raises `ImportError` and the source slot's bytes are unchanged;
  - `gp <plugin> login` prints the install hint and exits 1.
- [ ] **Step 2: Update `tests/unit/test_lite_cli_hints.py`**: a plugin command and `gp http` with a stored session no longer print the install hint on lite; `login`, `gp observe` and `gp session export` still do.
- [ ] **Step 3: Run** `python scripts/build_lite.py --out-dir <scratch>/lite`, `uv build --out-dir <scratch>/full`, then `scripts/smoke_install.sh <scratch>/lite/graftpunk_lite-*.whl <scratch>/full/graftpunk-*.whl`. Expected: every new line prints `OK`.
- [ ] **Step 4: Add the CI job `old-reader`** to `python-quality.yml`: with the branch, cache a nodriver session holding two cached tokens and header roles into a local store under the default setting; `python scripts/session_compat.py read --graftpunk 1.44.0 --store $STORE --name myshop` must exit 0 and print the cookie names and both token names. Then cache it with `GRAFTPUNK_SESSION_FORMAT=record`; the same command must exit 3 printing `SessionExpiredError`, and the store's bytes must be unchanged afterwards.
- [ ] **Step 5: Run the job's commands locally** and put both outputs in the commit body.
- [ ] **Step 6: Update the README passages**: on lite, a plugin command or `gp http` with a stored session runs; `load_session_for_api` no longer needs `[browser]`; `login`, `gp observe` and `gp session export` still do; a write-back from lite works for a session stored as a record.
- [ ] **Step 7: Gate, then commit** `feat: plugin commands use a stored session on graftpunk-lite; CI proves 1.44.0 reads what the default writes`.

### Task 10: Docs and release 1.45.0

**Files:**
- Modify: `CHANGELOG.md`, `pyproject.toml`, `uv.lock`, `docs/HOW_IT_WORKS.md` (`### Caching`, `### Loading` and `### Storage Backends` under `## Session Management`), `CONTRIBUTING.md` (what `scripts/session_compat.py` checks, and to run `generate` before any change to the stored format or `BrowserSession.__getstate__`), `.claude-plugin/plugin.json` only if `scripts/check-skill-version.sh d0511e6c` asks for it
- Comment on issue #245: the session half shipped; the plugin-packaging half stays open and needs its own short design.

- [ ] **Step 1: Write the 1.45.0 CHANGELOG section.** Added: `GRAFTPUNK_SESSION_FORMAT`, the record format, API loads and plugin commands on graftpunk-lite, `scripts/session_compat.py`. Changed: `load_session_for_api` no longer needs the browser packages. Upgrading, in this order: (1) nothing changes unless you set `GRAFTPUNK_SESSION_FORMAT=record`; (2) before any host writes records, every graftpunk that reads the same store must be 1.45.0 or later, because an older one reports a record as an expired session (it does not delete it); (3) a session becomes a record at its next login on a host that sets `record`, and a write-back never converts one; (4) selenium-backend sessions, and sessions with a custom adapter, hooks, a custom cookie policy or a subclass, stay pickles, with the warning `session_not_recordable`; (5) the new names: the warning `session_not_recordable`, the `reason` and `hint` fields on `session_save_skipped_load_failed`, and the errors `NewerSessionRecord` and `RealSessionClassUnavailable` (an `ImportError`). `InvalidSessionRecord` is internal to the codec: public loaders report an invalid record as `SessionExpiredError`, so the notes do not list it. Fixed: a `GraftpunkSession` loaded with `load_session` can send requests.
- [ ] **Step 2: Run** `git diff --name-only d0511e6c -- src/graftpunk/plugins/login_engine.py src/graftpunk/plugins/login_settle.py` (expected: no output), `scripts/check-skill-version.sh d0511e6c`, the full gate once more with `--cov=src/graftpunk`, and the changed test files under 3.11 and 3.14.
- [ ] **Step 3: Commit** `release: 1.45.0`, push, and open the PR with the verification commands and their output, the per-version unrecordable counts, the note that selenium fixtures are synthetic, and "Refs #245". Do not merge.
