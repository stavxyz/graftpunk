---
type: plan
---

# Logged-out answers per endpoint (`gp observe probe`): implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `gp observe probe [SESSION] [RUN_ID]` replays the safe requests of a recorded run without the session, writes `logged-out.json` into the run, and the digest and `gp plugin new` report what each endpoint answers logged out.

**Architecture:** The pure parts live in `har/`: `har/model.py` (moved shared names), `har/logged_out.py` (a leaf: vocabulary, classifier, ranking, file format), and `har/probe_select.py` (selection, above the digest). The network and the file write live in `observe/probe.py`. The CLI command only wires them together, and the digest, the report, and the scaffold read the file through `har/logged_out.py`.

**Tech Stack:** Python 3.11 to 3.14, requests (with brotli), Typer, pytest with xdist, uv, ruff, ty 0.0.75.

**Spec:** `docs/superpowers/specs/2026-11-27-logged-out-probe-design.md`. Read it whole first; this plan names files, signatures, and tests and does not restate the spec's reasoning. Paths below are under `src/graftpunk/` unless they start with `tests/`, `docs/`, `skills/`, or a dot.

## Global Constraints

- No module in `har/` holds an `import` or `from` statement naming `graftpunk.observe` (Task 1 adds the AST test).
- `har/logged_out.py` imports from graftpunk only `graftpunk.har.model`, `graftpunk.har.paths`, `graftpunk.har.documents`, and `graftpunk.contracts`. `har/digest.py` imports `har/logged_out.py`, never the reverse. `har/probe_select.py` imports `har/digest.py`, `har/graphql.py`, `har/logged_out.py`, `har/naming.py`, `har/paths.py`, and `har/parser.py`, and nothing from `observe/`.
- `har/digest.py` keeps re-exporting `ShapeNode`, `endpoint_key`, `OMIT_EMPTY`, and `INTERNAL` with a comment saying they are kept for compatibility; no existing `src/` import of those four changes.
- A run without `logged-out.json` renders byte for byte as before in markdown, `--json`, and `--endpoints-json` (Task 1's golden file pins it; every later task keeps it green).
- No schema number is written as a literal outside `contracts.py` (`tests/unit/test_contracts.py::test_contracts_is_the_only_module_that_declares_a_schema_number`). The writer reads `current_schema("logged_out")`.
- `observe/probe.py`'s `send` is the only code that touches the network. Every test that sends mounts the refusing adapter of Task 6 on `http://` and `https://`, so nothing leaves the machine.
- Values from the spec: `--limit` default 50, `--delay` default 1.0 seconds, timeout 10 seconds, at most 5 redirects followed, body read stops at 256 KiB (262144 bytes), `Accept-Encoding: gzip, deflate, br`, `LOGGED_OUT_SCHEMA = 1`, the file name `logged-out.json`, mode 0600 through `write_private_file`.
- Never sent: a recorded `Cookie`, `Authorization`, `Proxy-Authorization`, `Referer`, `Origin`, or any header of the row's custom header set. Sent: the recorded `User-Agent`, `Accept`, `Accept-Language`; `Accept-Encoding: gzip, deflate, br`; `Content-Type: application/json` on a GraphQL POST.
- Public repo: placeholder hosts only (`myshop.example`, `elsewhere.example`), no real site, plugin, company, or account names. No em or en dashes anywhere, code comments and docs included.
- Commit subjects start with `feat:`, `test:`, `docs:`, `refactor:`, or `fix:`; no attribution trailer of any kind. One commit per task.
- Gate, green after the last edit of every task (run from the worktree root):
  - `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4` (the whole suite; per-step runs name paths)
  - `uvx ruff check .` and `uvx ruff format --check .`
  - `uvx ty@0.0.75 check src/`
- Before any git command, `git rev-parse --show-toplevel` must end with `plugin-tooling`.

## Rulings

Decisions this plan makes where the spec left a choice or where the code forced one.

1. **`shape_of` moves to `har/model.py` with `ShapeNode`.** `same_as_logged_in` compares the logged-out body's `ShapeNode` with the recording's, and the leaf `har/logged_out.py` may not import `har/digest.py`, where `_shape_of` lives. `har/digest.py` imports `shape_of` and the two caps from `har/model.py`; `tests/unit/test_har_digest.py` imports `SHAPE_MAX_DEPTH` and `SHAPE_MAX_KEYS` from `har/model.py` in place of the private `_SHAPE_MAX_*` names.
2. **An off-scope redirect is a `ProbeResponse` with `offsite_host` set, not a `NotProbed`.** The spec's `send` bullet calls it `NotProbed`, but the class list keeps `redirect_offsite` with the target host, which only `classify` can produce. `send` stops at that hop without requesting it.
3. **`--limit` is applied by `select`, `--delay` by `send`.** `select(..., *, limit=50)` lists rows past the limit as skips with reason `over_limit`, so `--dry-run` and the prompt print the true count. `send(..., delay=1.0, sleep=time.sleep)`.
4. **The file's top-level facts come from the outcomes.** `to_json(entries, outcomes)` and `write_logged_out(run_dir, entries, outcomes)` take the outcomes too, because the header names sent and whether a proxy was used are facts of the responses, not of the entries.
5. **`classify` takes `ProbeResponse | NotProbed`.** One function builds every entry, skipped and not-probed included; the command passes `NotProbed(skip.reason)` for a plan skip.
6. **`summarize(logged_out, *, unjoined=0)`.** The digest passes the count `join` could not place, and `describe_summary` words it, so that warning has the same owner as every other summary line. `gp plugin new` calls `summarize` without it.
7. **`secret_values(digest, har)` takes `har` as the parsed entries** (`Sequence[HAREntry]`, what `flagged_values_of` reads) and reads the raw HAR itself with `read_run_har(digest.source.har_path.parent)` for `recorded_values`. `recorded_values` is always called, with the token candidate names (possibly none), so an unreadable run always stops the probe.
8. **`empty_data` walks the frontier.** A path is compared only while both sides are objects and depth is under 3; the parent `data` object that is non-empty on both sides is descended into, not counted as "a path still holding a value". The algorithm is in Task 3.
9. **Emptied paths are stored as names only, with `emptied_count` beside them.** The file does not keep whether a path was null, `[]`, `{}`, or missing, so a line reads `empty_data (data.viewer, data.orders)` rather than the spec's `(data.viewer null)`. `emptied_count` counts every emptied path, printable or not, and is what the ranking compares.
10. **Selection lists family rows nowhere** (their recording is a member row's) and lists a row with no fixture recording as a skip with reason `no_recording`. `join` matches family rows to nothing.
11. **A malformed `logged-out.json` is `Ignored` too**, with its own line, as the newer-schema case is. The digest and `gp plugin new` never raise on the file.
12. **A failed request (connection error, timeout, bad encoding) is `NotProbed("request_failed")`** and the run goes on.
13. **Scaffold comments beyond the spec's table.** A `rejected` entry gets the "no check needed" note on a `request_text` stub too (status decides there); `login_page` of either signal on a `request_text` stub, and `html_for_non_html` on a `request_json` stub, get a `GP-FILL`. The `empty_data` example check is `if not body["data"]["viewer"]:` instead of `is None`, because an emptied path can be `[]` or `{}`.
14. **A declined prompt exits 1** (`typer.confirm(..., abort=True)`); a plan with no requests writes the file of skips without asking.
15. **`RunDigest` carries the summary** as `logged_out_summary` (`OMIT_EMPTY`, so `--json` shows it on a probed run) and the `Ignored` as `logged_out_ignored` (`INTERNAL`). The probe command prints `Written: <path>` after the file is written, as `digest --output` prints its own.
16. **The dataclass field is `probe_class`**; the file and the projection spell it `class`.

## Shared test helpers

Created by the task named; later tasks use them by these names.

- `tests/unit/probe_har.py` (Task 5). `SESSION_VALUE = "Zq9XkLmPwR4t7Bv2Hn8Ks3Jd"`; `JWT` (a three-part `eyJ...` token); `CUSTOM_HEADER = "X-Shop-Client"`; index constants `ME = 0, VIEWER = 1, CART = 2, PRINT = 3, DOWNLOAD = 4, ACCOUNT = 5, ORDERS = 6, CATALOG = 7, PROMO = 8, HANGUP = 9, BIG = 10, EXPORT = 11, ADD_TO_CART = 12, MIXED = 13, WISHLIST = 14, DELETE_ITEM = 15, LOGOUT = 16, SEARCH = 17, TOKEN_CHECK = 18, UNSUBSCRIBE = 19, SESSION_API = 20, GET_MUTATION = 21, SLOW_DOWN = 22, AFTER = 23`; `CSRF_VALUE = "c5rfV4lu3Xy9Qw2Er7Ty"` (a hidden input `csrf_token` on `ACCOUNT`'s recorded page, so the digest has a token candidate); `PATHS: dict[int, str]` (index to recorded path and query); `write_probe_run(base: Path, session: str = "myshop", run_id: str = "run-1") -> Path` (writes `<base>/<session>/<run_id>/network.har` and returns that run directory); `probe_digest(run_dir: Path) -> tuple[RunDigest, list[HAREntry]]`. Every entry is `https://myshop.example`, sends `User-Agent: Mozilla/5.0 (probe test)`, `Accept`, `Accept-Language: en-US`, `Cookie: sid=<SESSION_VALUE>; theme=dark`, `Authorization: Bearer <JWT>`, `Referer`, `Origin`, and `X-Shop-Client: web-7`. Recorded answers: `ME` JSON `{"id": "u1", "name": "Ada"}`; `VIEWER` `query Viewer { viewer { id } orders { id } }` answered `{"data": {"viewer": {"id": "u1"}, "orders": [{"id": "o1"}]}}`; `CART` `query Cart { cart { id } }` answered `{"data": {"cart": {"id": "c1"}}}`; `PRINT` JSON `{"invoice": {"id": "i1"}}`; `DOWNLOAD` `application/pdf`; `ACCOUNT` and `ORDERS` HTML with no form; `CATALOG` `{"items": [{"sku": "a1"}]}`; `PROMO`, `HANGUP`, `SLOW_DOWN`, `AFTER` JSON; `BIG` JSON; `EXPORT` `text/csv`. `ME`'s recorded body is the smallest; every other recorded body is at least 100 bytes (`DOWNLOAD` records `body_size` 50000), so the suggested check is `GET /api/me`. `ADD_TO_CART` `mutation AddToCart { add { id } }`; `MIXED` `query A { a } mutation B { b }` with `operationName` `A`; `WISHLIST` `operationName` `Wishlist` with `extensions.persistedQuery.sha256Hash` and no `query`; `DELETE_ITEM` `DELETE /api/cart/items/1`; `LOGOUT` `/logout`; `SEARCH` `/api/search?session=<SESSION_VALUE>`; `TOKEN_CHECK` `/api/token-check?t=<JWT>`; `UNSUBSCRIBE` `/newsletter/unsubscribe`; `SESSION_API` `/api/session`; `GET_MUTATION` `GET /graphql?query=mutation%20Drop%20%7B%20drop%20%7D`.
- `tests/unit/probe_site.py` (Task 6), imported into `tests/unit/conftest.py` beside `site`. `Received(method: str, path: str, headers: list[tuple[str, str]], body: bytes)`; `ProbeSite(base_url: str, received: list[Received], proxies_seen: list[dict[str, str]])` with `session() -> requests.Session` (mounts `_ToLocal` on `https://myshop.example` and `_Refuse` on `http://` and `https://`); fixture `probe_site`. `_ToLocal(HTTPAdapter).send` records `kwargs["proxies"]` into `proxies_seen`, sends a copy of the request with `https://myshop.example` replaced by the local base and `proxies={}`, then sets the response's `url` and `request` back to the original. `_Refuse.send` raises `AssertionError(f"left the machine: {request.url}")`. Routes (logged-out answers): `/api/me` 401 JSON with `Set-Cookie: sid=fresh; Path=/`; `POST /graphql` answering a body naming `Viewer` with `{"data": {"viewer": null, "orders": []}}` and one naming `Cart` with `{"errors": [{"message": "login required"}], "data": null}`; `/print/1` 200 HTML `<html><body>Print preview</body></html>`; `/download/1` 403 HTML; `/account` 302 to `/login`; `/login` 200 HTML with a form holding `<input type="password" name="pw">`; `/orders` 302 to `/sso/start`; `/sso/start` 200 HTML with no form; `/api/catalog` 200 `{"items": [{"sku": "b2"}]}`; `/promo` 302 to `https://elsewhere.example/landing?ref=promo`; `/api/hangup` closes the connection without answering; `/api/big` 200 `application/json` of 300 KiB; `/api/export` 200 HTML `<html><body>Session ended</body></html>`; `/api/slow-down` 429 with `Retry-After: 30`; `/api/after` 200 JSON; `/loop` 302 to `/loop`. Every request is appended to `received` before it is answered.
- `run_probe(run_dir: Path, site: ProbeSite, *, delay: float = 0.0) -> ProbeRun` in `tests/unit/probe_site.py` (Task 6): `ProbeRun(plan: ProbePlan, outcomes: list[ProbeOutcome], entries: tuple[LoggedOutEntry, ...])`, built by `probe_digest`, `secret_values`, `select`, `send(plan, session_factory=site.session, delay=delay)`, and `classify` in the command's order. Its docstring says it mirrors `observe_probe` in `cli/observe_commands.py`.

## Review Focus

Inputs the spec is silent on that a user will hit. Each has a test in the task named.

1. A `logged-out.json` that is not JSON, is cut short, or holds an entry with an unknown class (hand-edited, or a write interrupted): the digest prints one `Ignored` line and renders the run as unprobed; nothing raises. (Tasks 7, 9)
2. A request that fails on the wire (connection closed, timeout, a body that does not decode): its entry is `not_probed: request_failed` and the run continues to the next request. (Task 6)
3. A run where no row is replayable: no prompt, no request, the file is written with every skip, and the summary says no check was found. (Task 8)
4. A logged-out body over 256 KiB: the read stops at 262144 bytes, the class is `other`, and nothing raises on the cut JSON. (Task 6)
5. Probing a run a second time: the new file replaces the old one, stays 0600, and the digest reads the new entries. (Task 8)

---

### Task 1: Pure moves: `har/model.py`, path predicates, `in_primary_scope`, and the unprobed golden

**Files:**
- Create: `src/graftpunk/har/model.py`
- Modify: `src/graftpunk/har/digest.py` (lines 163-170 `INTERNAL`/`OMIT_EMPTY`, 180-181 caps, 238-261 auth and logout patterns, 332-350 `ShapeNode`, 531-537 `endpoint_key`, 1366-1397 `_shape_of`, the two `_shape_of` call sites at 1429 and 1445, 2819 and 4223 regex uses, 4962-4994 `_out_of_scope_cookies`)
- Modify: `src/graftpunk/har/paths.py` (append the predicates)
- Modify: `tests/unit/test_har_digest.py:30-31` (`    _SHAPE_MAX_DEPTH,`), to import the caps from `har/model.py`
- Create: `tests/unit/test_har_model.py`, `tests/unit/test_har_layering.py`, `tests/unit/test_digest_unprobed_golden.py`, `tests/fixtures/digest_without_logged_out.json`
- Modify: `tests/fixtures/README.md` (one entry for the golden)

**Interfaces:**
- Produces in `har/model.py`: `INTERNAL = "internal"`, `OMIT_EMPTY = "omit_empty"`, `SHAPE_MAX_DEPTH: Final = 3`, `SHAPE_MAX_KEYS: Final = 12`, `ShapeNode` (moved unchanged), `shape_of(value: Any, depth: int = 0) -> ShapeNode` (moved `_shape_of` with `_ID_KEY`), `endpoint_key(method: str, template: str, operation: str = "") -> str` (moved; imports `ANONYMOUS_OPERATION` from `har/graphql.py`), and `@dataclass(frozen=True) class RowRef: position: int; fixture_entry_index: int | None; key: str`.
- Produces in `har/paths.py`: `is_auth_path(path: str) -> bool` and `is_logout_path(path: str) -> bool`, with the pattern lists moved as private names.
- Produces in `har/digest.py`: `in_primary_scope(d: RunDigest, host: str) -> bool`, returning `_in_scope(_host_name(host), _scope_root(_host_name(d.primary_host)))`; `_out_of_scope_cookies` calls it. Added to `__all__`.
- Produces: `tests/unit/test_digest_unprobed_golden.py::write_golden() -> None` and `outputs() -> dict[str, dict[str, str]]`.

- [ ] **Step 1: Write the golden generator and test, and generate the golden on the unmodified tree.**

```python
# tests/unit/test_digest_unprobed_golden.py
_GOLDEN = REPO_ROOT / "tests" / "fixtures" / "digest_without_logged_out.json"

def outputs() -> dict[str, dict[str, str]]:
    """The three renderings of tests/fixtures/sample.har as a bare HAR and as a run
    directory, with relative paths so the file holds no machine path."""
    # bare: chdir(REPO_ROOT), DigestSource.from_har(Path("tests/fixtures/sample.har"))
    # run: copy sample.har to <tmp>/myshop/run-1/network.har, chdir(<tmp>),
    #      DigestSource.from_run_dir(Path("myshop/run-1"), session="myshop", run_id="run-1")
    # each: {"markdown": render_markdown(d), "json": render_json(d), "endpoints_json": render_endpoints_json(d)}

def write_golden() -> None:
    _GOLDEN.write_text(json.dumps(outputs(), indent=2, sort_keys=True) + "\n", encoding="utf-8")

def test_a_run_without_logged_out_renders_as_before() -> None:
    assert outputs() == json.loads(_GOLDEN.read_text(encoding="utf-8"))
```

`outputs()` restores the working directory in a `finally` and uses `tempfile.TemporaryDirectory`, since `write_golden` runs outside pytest.

Run: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras python -c "from tests.unit.test_digest_unprobed_golden import write_golden; write_golden()"`
Expected: `tests/fixtures/digest_without_logged_out.json` exists and `git diff --stat -- src` prints nothing.

- [ ] **Step 2: Write the failing tests for the moves.**

```python
# tests/unit/test_har_model.py
def test_the_moved_names_are_the_same_objects_through_the_digest() -> None:
    from graftpunk.har import digest, model
    assert digest.ShapeNode is model.ShapeNode
    assert digest.endpoint_key is model.endpoint_key
    assert digest.OMIT_EMPTY == model.OMIT_EMPTY == "omit_empty"
    assert digest.INTERNAL == model.INTERNAL == "internal"

def test_shape_of_keeps_its_caps() -> None:
    assert shape_of({"a": {"b": {"c": {"d": 1}}}}).children["a"].children["b"].children["c"] == ShapeNode(kind="object", truncated=True)
    assert len(shape_of({f"k{i}": i for i in range(SHAPE_MAX_KEYS + 2)}).children) == SHAPE_MAX_KEYS

@pytest.mark.parametrize(("path", "auth", "logout"), [
    ("/login", True, False), ("/sso/start", True, False), ("/api/session", True, False),
    ("/api/tokens/list", False, False), ("/account", False, False),
    ("/logout", False, True), ("/sign-out/now", False, True), ("/LogOff", False, True),
])
def test_the_auth_and_logout_predicates(path: str, auth: bool, logout: bool) -> None:
    assert is_auth_path(path) is auth
    assert is_logout_path(path) is logout

def test_in_primary_scope_ignores_all_hosts(tmp_path: Path) -> None:
    # a HAR with entries on myshop.example, api.myshop.example and cdn.other.example
    for all_hosts in (False, True):
        d = digest(DigestSource.from_har(har), all_hosts=all_hosts)
        assert in_primary_scope(d, "api.myshop.example") is True
        assert in_primary_scope(d, "api.myshop.example:8443") is True
        assert in_primary_scope(d, "cdn.other.example") is False
```

```python
# tests/unit/test_har_layering.py
_HAR = Path(graftpunk.har.__file__).parent

def _imported(path: Path) -> set[str]:
    """Every module an import or from statement in *path* names (ast)."""

def test_no_har_module_imports_observe() -> None:
    offenders = {
        f"{path.name}: {name}"
        for path in sorted(_HAR.glob("*.py"))
        for name in _imported(path)
        if name == "graftpunk.observe" or name.startswith("graftpunk.observe.")
    }
    assert offenders == set()
```

Run: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4 tests/unit/test_har_model.py tests/unit/test_har_layering.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'graftpunk.har.model'`; the layering test passes.

- [ ] **Step 3: Make the moves.** Create `har/model.py` with the names under Interfaces. In `har/digest.py` replace the moved definitions with `from graftpunk.har.model import INTERNAL, OMIT_EMPTY, ShapeNode, endpoint_key` under a comment `# Moved to graftpunk.har.model; re-exported here for the code that imports them from the digest.`, plus `from graftpunk.har.model import RowRef, SHAPE_MAX_DEPTH, shape_of` as needed, call `shape_of` at the two sites, and call `is_auth_path` and `is_logout_path` at lines 4223 and 2819. Add `in_primary_scope`. Change the test import of `_SHAPE_MAX_DEPTH, _SHAPE_MAX_KEYS` to `SHAPE_MAX_DEPTH, SHAPE_MAX_KEYS` from `graftpunk.har.model` and rename the four uses.

- [ ] **Step 4: Run the moved-code tests, the golden, and the existing digest suites.**

Run: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4 tests/unit/test_har_model.py tests/unit/test_har_layering.py tests/unit/test_digest_unprobed_golden.py tests/unit/test_har_digest.py tests/unit/test_har_report.py tests/unit/test_digest_families.py tests/unit/test_digest_inline_json.py tests/unit/test_digest_after_read.py`
Expected: PASS. Then the full gate.

- [ ] **Step 5: Commit.**

```bash
git add src/graftpunk/har/model.py src/graftpunk/har/digest.py src/graftpunk/har/paths.py tests/unit/test_har_model.py tests/unit/test_har_layering.py tests/unit/test_digest_unprobed_golden.py tests/unit/test_har_digest.py tests/fixtures/digest_without_logged_out.json tests/fixtures/README.md
git commit -m "refactor: move ShapeNode, endpoint_key and the field markers into har/model.py and make the auth, logout and scope predicates public"
```

### Task 2: Every definition of a GraphQL document

**Files:**
- Modify: `src/graftpunk/har/graphql.py:500-524` (`def document_operation(`), and its `__all__`
- Test: `tests/unit/test_graphql_operations.py`

**Interfaces:**
- Produces: `UNPARSED: Final = "unparsed"` and `document_definitions(document: str) -> tuple[tuple[str, str], ...] | Literal["unparsed"]`: `(name, kind)` for every definition in document order, kind one of `query`, `mutation`, `subscription`, `fragment`, name gated as `document_operation` gates it (`ANONYMOUS_OPERATION` when it has none or fails the gate); `UNPARSED` when `_parsed(document)` is None.
- Changes nothing observable: `document_operation(document)` returns the first non-fragment pair of `document_definitions`, or None for `UNPARSED` or a document of fragments only. `operation_kind` is unchanged (it still answers `query` for a document that does not parse).

- [ ] **Step 1: Write the failing tests.**

```python
class TestDocumentDefinitions:
    def test_every_definition_in_order(self) -> None:
        doc = "fragment F on T { a } query A { ...F } mutation B { b }"
        assert document_definitions(doc) == (("F", "fragment"), ("A", "query"), ("B", "mutation"))

    def test_the_shorthand_is_an_anonymous_query(self) -> None:
        assert document_definitions("{ viewer { id } }") == ((ANONYMOUS_OPERATION, "query"),)

    @pytest.mark.parametrize("text", ["red shoes", "{}", "query {"])
    def test_a_document_that_does_not_parse_is_unparsed(self, text: str) -> None:
        assert document_definitions(text) == UNPARSED
        assert operation_kind(text) == "query"

    def test_document_operation_is_the_first_operation_of_the_definitions(self) -> None:
        for doc in (graphql_har.ORDERS_DOCUMENT, graphql_har.ADD_TO_CART_DOCUMENT, "fragment F on T { a } mutation M { ...F }"):
            first = next(d for d in document_definitions(doc) if d[1] != "fragment")
            assert document_operation(doc) == first
```

Run: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4 tests/unit/test_graphql_operations.py`
Expected: FAIL with `ImportError: cannot import name 'document_definitions'`.

- [ ] **Step 2: Implement `document_definitions` and rewrite `document_operation` over it** (one parser read through `_parsed`).

- [ ] **Step 3: Run the GraphQL suites, the har-facts parity test included.**

Run: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4 tests/unit/test_graphql_operations.py tests/unit/test_graft_har_facts_graphql.py tests/unit/test_har_report.py`
Expected: PASS. Then the full gate.

- [ ] **Step 4: Commit.**

```bash
git add src/graftpunk/har/graphql.py tests/unit/test_graphql_operations.py
git commit -m "feat: document_definitions reads the kind of every GraphQL definition, with an unparsed answer"
```

### Task 3: The logged-out vocabulary and the classifier

**Files:**
- Create: `src/graftpunk/har/logged_out.py`
- Test: `tests/unit/test_logged_out_classify.py`; modify `tests/unit/test_har_layering.py`

**Interfaces:**
- Consumes: `shape_of`, `ShapeNode` (Task 1); `is_auth_path`, `printable_name`, `template_path`, `normal_host` (`har/paths.py`); `is_login_document` (`har/documents.py`).
- Produces in `har/logged_out.py` (all frozen dataclasses):
  - `ProbeClass = Literal["rejected", "redirect_offsite", "login_page", "rate_limited", "not_found", "server_error", "graphql_error", "empty_data", "html_for_non_html", "same_as_logged_in", "other"]` and `PROBE_CLASSES: tuple[ProbeClass, ...] = get_args(ProbeClass)`.
  - `LoginSignal = Literal["login_form", "auth_path"]`.
  - `NotProbedReason = Literal["auth_url", "logout_url", "login_flow", "unsafe_segment", "not_a_query", "unparsed_document", "persisted_query", "method", "off_scope_host", "no_recording", "secret_value", "jwt", "over_limit", "rate_limited", "request_failed"]` and `NOT_PROBED_REASONS`.
  - `Location(scheme: str, host: str, path: str)`.
  - `LoggedInRecording(endpoint_key: str, entry_index: int | None, method: str, graphql: bool, media_type: str, body: str | None, body_size: int)`.
  - `ProbeResponse(first_status: int, first_location: str, final_status: int, final_url: str, media_type: str, text: str, body_length: int, offsite_host: str = "", retry_after: bool = False, headers_sent: tuple[str, ...] = (), proxy_used: bool = False)`. `first_location` is absolute or empty; `media_type` is normalised (`normalize_media_type`).
  - `NotProbed(reason: NotProbedReason)`.
  - `LoggedOutEntry(endpoint_key: str, entry_index: int | None, method: str, graphql: bool, recorded_size: int, probe_class: ProbeClass | None = None, login_signal: LoginSignal | None = None, not_probed: NotProbedReason | None = None, first_status: int | None = None, location: Location | None = None, final_status: int | None = None, media_type: str = "", body_length: int | None = None, offsite_host: str = "", emptied_paths: tuple[str, ...] = (), emptied_count: int = 0)`.
  - `classify(response: ProbeResponse | NotProbed, recorded: LoggedInRecording) -> LoggedOutEntry`.
  - `runtime_catches(entry: LoggedOutEntry) -> bool`: `probe_class == "rejected"`, or `probe_class == "login_page" and login_signal == "login_form"`.

- [ ] **Step 1: Write the failing tests.** Helpers in the test file: `_rec(**kw) -> LoggedInRecording` (defaults `endpoint_key="GET /api/me"`, `entry_index=0`, `method="GET"`, `graphql=False`, `media_type="application/json"`, `body='{"id": "u1"}'`, `body_size=12`) and `_resp(**kw) -> ProbeResponse` (defaults `first_status=200`, `first_location=""`, `final_status=200`, `final_url="https://myshop.example/api/me"`, `media_type="application/json"`, `text="{}"`, `body_length=2`).

```python
LOGIN_FORM = '<form><input name="u"><input type="password" name="p"></form>'
FILLED = '{"data": {"viewer": {"id": "u1"}, "orders": [{"id": "o1"}]}}'

@pytest.mark.parametrize("status", [401, 403])
def test_401_and_403_are_rejected(status: int) -> None:
    entry = classify(_resp(first_status=status, final_status=status), _rec())
    assert (entry.probe_class, entry.login_signal, entry.final_status) == ("rejected", None, status)

def test_a_login_page_served_with_403_is_rejected() -> None:
    assert classify(_resp(final_status=403, media_type="text/html", text=LOGIN_FORM), _rec()).probe_class == "rejected"

def test_an_offsite_redirect_keeps_the_target_host() -> None:
    entry = classify(_resp(first_status=302, first_location="https://elsewhere.example/landing?ref=promo", final_status=302, offsite_host="elsewhere.example"), _rec())
    assert entry.probe_class == "redirect_offsite"
    assert entry.offsite_host == "elsewhere.example"
    assert entry.location == Location("https", "elsewhere.example", "/landing")

def test_a_login_form_below_400_is_login_page_with_signal_login_form() -> None:
    entry = classify(_resp(first_status=302, first_location="https://myshop.example/login", final_url="https://myshop.example/login", media_type="text/html", text=LOGIN_FORM), _rec())
    assert (entry.probe_class, entry.login_signal) == ("login_page", "login_form")
    assert entry.location == Location("https", "myshop.example", "/login")

def test_a_page_ending_on_an_auth_path_is_login_page_with_signal_auth_path() -> None:
    entry = classify(_resp(final_url="https://myshop.example/sso/start", media_type="text/html", text="<p>Continue</p>"), _rec())
    assert (entry.probe_class, entry.login_signal) == ("login_page", "auth_path")

def test_json_on_an_auth_path_is_not_a_login_page() -> None:
    assert classify(_resp(final_url="https://myshop.example/login", text='{"id": "u2"}'), _rec()).probe_class == "same_as_logged_in"

@pytest.mark.parametrize(("status", "retry_after", "expected"), [
    (429, False, "rate_limited"), (503, True, "rate_limited"), (503, False, "server_error"),
    (500, False, "server_error"), (404, False, "not_found"),
])
def test_status_classes(status: int, retry_after: bool, expected: str) -> None:
    assert classify(_resp(final_status=status, retry_after=retry_after), _rec()).probe_class == expected

def test_errors_with_null_data_are_graphql_error() -> None:
    rec = _rec(method="POST", graphql=True, body=FILLED)
    assert classify(_resp(text='{"errors": [{"message": "x"}], "data": null}'), rec).probe_class == "graphql_error"
    assert classify(_resp(text='{"errors": [{"message": "x"}]}'), rec).probe_class == "graphql_error"
    assert classify(_resp(text='{"errors": [], "data": null}'), rec).probe_class != "graphql_error"

def test_emptied_paths_are_empty_data_in_recorded_order() -> None:
    entry = classify(_resp(text='{"data": {"viewer": null, "orders": []}}'), _rec(body=FILLED))
    assert entry.probe_class == "empty_data"
    assert entry.emptied_paths == ("data.viewer", "data.orders")
    assert entry.emptied_count == 2

def test_a_missing_path_counts_as_emptied() -> None:
    assert classify(_resp(text='{"data": {"viewer": {}}}'), _rec(body=FILLED)).emptied_paths == ("data.viewer", "data.orders")

def test_a_path_still_holding_a_value_is_not_empty_data() -> None:
    assert classify(_resp(text='{"data": {"viewer": null, "orders": [{"id": "o2"}]}}'), _rec(body=FILLED)).probe_class == "other"

def test_the_walk_stops_at_depth_three() -> None:
    rec = _rec(body='{"a": {"b": {"c": {"d": 1}}}}')
    assert classify(_resp(text='{"a": {"b": {"c": {"d": null}}}}'), rec).probe_class != "empty_data"

def test_an_emptied_name_that_reads_as_a_value_is_counted_not_stored() -> None:
    entry = classify(_resp(text="{}"), _rec(body='{"Zq9XkLmPwR4t7Bv2Hn8Ks3Jd": {"a": 1}, "data": 1}'))
    assert (entry.probe_class, entry.emptied_paths, entry.emptied_count) == ("empty_data", ("data",), 2)

def test_html_where_the_recording_was_not_html() -> None:
    assert classify(_resp(media_type="text/html", text="<p>Print</p>"), _rec()).probe_class == "html_for_non_html"
    assert classify(_resp(media_type="text/html", text="<p>Print</p>"), _rec(media_type="text/html", body="<p>x</p>")).probe_class == "other"

def test_the_same_shape_is_same_as_logged_in() -> None:
    rec = _rec(body='{"items": [{"sku": "a1"}]}')
    assert classify(_resp(text='{"items": [{"sku": "b2"}]}'), rec).probe_class == "same_as_logged_in"

def test_anything_else_is_other_with_its_media_type() -> None:
    entry = classify(_resp(media_type="text/plain", text="hello"), _rec())
    assert (entry.probe_class, entry.media_type) == ("other", "text/plain")

def test_cut_json_is_other() -> None:
    assert classify(_resp(text='{"items": [', body_length=262144), _rec()).probe_class == "other"

def test_not_probed_carries_its_reason_and_no_response_fact() -> None:
    entry = classify(NotProbed("secret_value"), _rec())
    assert (entry.probe_class, entry.not_probed, entry.first_status, entry.final_status, entry.body_length) == (None, "secret_value", None, None, None)

def test_the_entry_keeps_the_recordings_identity() -> None:
    entry = classify(_resp(final_status=401), _rec(endpoint_key="POST /graphql (Viewer)", entry_index=7, method="POST", graphql=True, body_size=99))
    assert (entry.endpoint_key, entry.entry_index, entry.method, entry.graphql, entry.recorded_size) == ("POST /graphql (Viewer)", 7, "POST", True, 99)

def test_the_location_is_templated_and_drops_the_query() -> None:
    entry = classify(_resp(first_status=302, first_location="https://myshop.example/orders/12345678?next=/x", final_url="https://myshop.example/orders/12345678"), _rec())
    assert entry.location == Location("https", "myshop.example", "/orders/{order_id}")

@pytest.mark.parametrize(("probe_class", "signal", "caught"), [
    ("rejected", None, True), ("login_page", "login_form", True), ("login_page", "auth_path", False),
    ("empty_data", None, False), ("html_for_non_html", None, False), ("same_as_logged_in", None, False),
])
def test_runtime_catches(probe_class: str, signal: str | None, caught: bool) -> None:
    entry = LoggedOutEntry("GET /a", 0, "GET", False, 1, probe_class=probe_class, login_signal=signal)
    assert runtime_catches(entry) is caught
```

```python
# tests/unit/test_har_layering.py
def test_logged_out_imports_only_its_leaves() -> None:
    graftpunk_names = {n for n in _imported(_HAR / "logged_out.py") if n.startswith("graftpunk")}
    assert graftpunk_names <= {"graftpunk.har.model", "graftpunk.har.paths", "graftpunk.har.documents", "graftpunk.contracts"}
```

Run: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4 tests/unit/test_logged_out_classify.py tests/unit/test_har_layering.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'graftpunk.har.logged_out'`.

- [ ] **Step 2: Implement the vocabulary, `classify`, and `runtime_catches` in `har/logged_out.py`.** Rules in the spec's order on the final response: `offsite_host` set gives `redirect_offsite` only after the 401/403 check. JSON is read only when `"json" in media_type`, and "non-JSON" for `login_page` is that same test, as `SiteRequests.json` applies it. HTML is `text/html` or `application/xhtml+xml`. `location` is set when `first_location` is non-empty: `urlsplit`, `normal_host(scheme, netloc)`, `template_path(path)[0]`. `same_as_logged_in` compares `shape_of(json.loads(text)) == shape_of(json.loads(recorded.body))`; a recorded body that is None or does not parse makes the endpoint `other`. The `empty_data` walk:

```python
_MISSING = object()

def _is_filled(value: object) -> bool:
    # logged in: a value counts when it is not null, "", [] or {}
    return value is not None and value != "" and value != [] and value != {}

def _is_emptied(value: object) -> bool:
    # logged out: the spec's list exactly
    return value is _MISSING or value is None or value == [] or value == {}

def _walk(recorded: object, out: object, path: tuple[str, ...], depth: int,
          emptied: list[tuple[str, ...]]) -> bool:
    """Append each emptied path under *path*; return True when a path under it
    still holds a value logged out."""
    if not isinstance(recorded, dict) or depth >= 3:
        return False
    kept = False
    for key, value in recorded.items():
        if not _is_filled(value):
            continue
        child = out.get(key, _MISSING) if isinstance(out, dict) else _MISSING
        here = (*path, str(key))
        if _is_emptied(child):
            emptied.append(here)
        elif isinstance(value, dict) and isinstance(child, dict) and depth + 1 < 3:
            kept = _walk(value, child, here, depth + 1, emptied) or kept
        else:
            kept = True
    return kept
```

`empty_data` holds when `_walk(recorded_json, out_json, (), 0, emptied)` is False and `emptied` is non-empty. `emptied_count = len(emptied)`; `emptied_paths` keeps, in order, the `".".join(path)` of each path whose every part passes `printable_name`.

- [ ] **Step 3: Run the tests.**

Run: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4 tests/unit/test_logged_out_classify.py tests/unit/test_har_layering.py`
Expected: PASS. Then the full gate.

- [ ] **Step 4: Commit.**

```bash
git add src/graftpunk/har/logged_out.py tests/unit/test_logged_out_classify.py tests/unit/test_har_layering.py
git commit -m "feat: har/logged_out.py classifies a logged-out answer against the logged-in recording"
```

### Task 4: The ranking and every display line

**Files:**
- Modify: `src/graftpunk/har/logged_out.py`
- Test: `tests/unit/test_logged_out_summary.py`

**Interfaces:**
- Consumes: Task 3's types and `runtime_catches`.
- Produces: `LoggedOut(schema: int, probed_at: str, headers_sent: tuple[str, ...], proxy_used: bool, entries: tuple[LoggedOutEntry, ...])`; `LoggedOutSummary(counts: dict[ProbeClass, int], not_probed: dict[NotProbedReason, int], suggested: LoggedOutEntry | None, client_refused: bool, unjoined: int = 0)` (counts hold non-zero values only, in `PROBE_CLASSES` and `NOT_PROBED_REASONS` order); `summarize(logged_out: LoggedOut, *, unjoined: int = 0) -> LoggedOutSummary`; `describe(entry: LoggedOutEntry) -> str`; `describe_summary(summary: LoggedOutSummary) -> list[str]`.
- Ranking: the suggested check is the entry with `method == "GET"`, a `probe_class`, and `runtime_catches` true, with the smallest `recorded_size`, a tie going to the smaller `entry_index`; else the entry with `graphql` and `probe_class == "empty_data"` and the smallest `emptied_count`, the same tie rule; else None. `client_refused` is true when at least one entry has a `probe_class` and every such entry is `rejected`.

- [ ] **Step 1: Write the failing tests.** Helper `_e(probe_class=None, **kw) -> LoggedOutEntry` (defaults `endpoint_key="GET /api/me"`, `entry_index=0`, `method="GET"`, `graphql=False`, `recorded_size=100`) and `_out(*entries) -> LoggedOut` (schema 1, fixed `probed_at`, no headers, no proxy).

```python
def test_counts_per_class_and_reason() -> None:
    s = summarize(_out(_e("rejected"), _e("rejected", entry_index=1), _e("empty_data", entry_index=2), _e(not_probed="not_a_query", entry_index=3)))
    assert s.counts == {"rejected": 2, "empty_data": 1}
    assert s.not_probed == {"not_a_query": 1}

def test_the_suggested_check_is_the_smallest_caught_get() -> None:
    small = _e("login_page", login_signal="login_form", endpoint_key="GET /account", entry_index=5, recorded_size=300)
    s = summarize(_out(_e("rejected", recorded_size=900), small, _e("rejected", method="POST", entry_index=6, recorded_size=10), _e("empty_data", graphql=True, entry_index=7)))
    assert s.suggested == small

def test_a_size_tie_goes_to_the_earlier_entry() -> None:
    first = _e("rejected", entry_index=1)
    assert summarize(_out(_e("rejected", entry_index=4), first)).suggested == first

def test_without_a_caught_get_the_graphql_empty_data_with_fewest_paths_wins() -> None:
    fewest = _e("empty_data", method="POST", graphql=True, entry_index=9, emptied_count=1)
    s = summarize(_out(_e("empty_data", method="POST", graphql=True, entry_index=2, emptied_count=3), fewest, _e("login_page", login_signal="auth_path", entry_index=1)))
    assert s.suggested == fewest

def test_no_candidate_means_no_suggestion() -> None:
    s = summarize(_out(_e("same_as_logged_in"), _e("login_page", login_signal="auth_path", entry_index=1)))
    assert s.suggested is None
    assert describe_summary(s)[1] == "suggested session check: none: no probed endpoint answers a dead session in a way request_json or a GraphQL data check reads"

def test_every_probed_endpoint_rejected_warns_of_bot_defence() -> None:
    assert summarize(_out(_e("rejected"), _e(not_probed="method", entry_index=1))).client_refused is True
    assert summarize(_out(_e("rejected"), _e("other", entry_index=1))).client_refused is False
    assert summarize(_out(_e(not_probed="method"))).client_refused is False

@pytest.mark.parametrize(("entry", "line"), [
    (_e("rejected", first_status=401, final_status=401, media_type="application/json"), "logged out: 401 application/json, rejected"),
    (_e("login_page", login_signal="login_form", first_status=302, location=Location("https", "myshop.example", "/login"), final_status=200, media_type="text/html"), "logged out: 302 to /login, then 200 text/html, login_page (login_form)"),
    (_e("redirect_offsite", first_status=302, final_status=302, offsite_host="elsewhere.example", location=Location("https", "elsewhere.example", "/landing")), "logged out: 302 to elsewhere.example, redirect_offsite"),
    (_e("empty_data", first_status=200, final_status=200, media_type="application/json", emptied_paths=("data.viewer", "data.orders"), emptied_count=2), "logged out: 200 application/json, empty_data (data.viewer, data.orders)"),
    (_e("empty_data", first_status=200, final_status=200, media_type="application/json", emptied_paths=("data",), emptied_count=2), "logged out: 200 application/json, empty_data (data, 1 more not shown)"),
    (_e(not_probed="secret_value"), "logged out: not probed (secret_value)"),
])
def test_describe(entry: LoggedOutEntry, line: str) -> None:
    assert describe(entry) == line

def test_describe_summary_lines() -> None:
    me = _e("rejected", first_status=401, final_status=401)
    s = summarize(_out(me, _e("rejected", entry_index=1, recorded_size=500), _e("empty_data", entry_index=2), _e(not_probed="not_a_query", entry_index=3), _e(not_probed="not_a_query", entry_index=4)))
    assert describe_summary(s) == [
        "probed logged out: 3 (rejected 2, empty_data 1); not probed: 2 (not_a_query 2)",
        "suggested session check: GET /api/me, rejected: request_json raises SessionRejectedError on a dead session there",
    ]

def test_an_empty_data_suggestion_names_its_first_path() -> None:
    s = summarize(_out(_e("empty_data", endpoint_key="POST /graphql (Viewer)", method="POST", graphql=True, emptied_paths=("data.viewer",), emptied_count=1)))
    assert describe_summary(s)[1] == "suggested session check: POST /graphql (Viewer), empty_data: check that data.viewer is not empty"

def test_the_warning_lines() -> None:
    lines = describe_summary(summarize(_out(_e("rejected")), unjoined=2))
    assert "warning: every probed endpoint answered 401 or 403, so the site may be refusing this client rather than the missing session (bot defence); a plugin's requests may be answered differently" in lines
    assert lines[-1] == "warning: 2 logged-out entries match no endpoint of this digest; run gp observe probe again"
    assert describe_summary(summarize(_out(_e("other")), unjoined=1))[-1] == "warning: 1 logged-out entry matches no endpoint of this digest; run gp observe probe again"
```

Run: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4 tests/unit/test_logged_out_summary.py`
Expected: FAIL with `ImportError: cannot import name 'summarize'`.

- [ ] **Step 2: Implement `LoggedOut`, `LoggedOutSummary`, `summarize`, `describe`, and `describe_summary`** with the exact copy the tests pin. The count line drops its `; not probed: ...` clause when nothing was skipped and reads `probed logged out: 0` when nothing was probed. A login-form suggestion reads `suggested session check: <key>, login_page (login_form): request_json raises SessionRejectedError on a dead session there`; an empty-data suggestion whose paths were all held back reads `..., empty_data: check the field it empties`.

- [ ] **Step 3: Run the tests.**

Run: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4 tests/unit/test_logged_out_summary.py tests/unit/test_logged_out_classify.py`
Expected: PASS. Then the full gate.

- [ ] **Step 4: Commit.**

```bash
git add src/graftpunk/har/logged_out.py tests/unit/test_logged_out_summary.py
git commit -m "feat: summarize ranks the suggested session check and words every logged-out line"
```

### Task 5: Which requests are replayed

**Files:**
- Create: `src/graftpunk/har/probe_select.py`, `src/graftpunk/devtools/probe_secrets.py`
- Create: `tests/unit/probe_har.py`, `tests/unit/test_probe_select.py`

**Interfaces:**
- Consumes: `in_primary_scope`, `flagged_values_of`, `entry_operations`, `RunDigest`, `Endpoint` (`har/digest.py`); `document_definitions`, `UNPARSED` (Task 2); `is_auth_path`, `is_logout_path` (Task 1); `LoggedInRecording`, `NotProbedReason` (Task 3); `normalize_media_type` (`har/naming.py`); `HAREntry` (`har/parser.py`); `recorded_values`, `read_run_har`, `RunUnreadableError` (`testing/recorded.py`).
- Produces in `har/probe_select.py`:
  - `UNSAFE_SEGMENTS: Final = frozenset({"unsubscribe", "confirm", "verify", "activate", "delete", "remove", "cancel", "approve", "reset"})`, compared with each path segment casefolded.
  - `ProbeItem(recording: LoggedInRecording, url: str, headers: tuple[tuple[str, str], ...], body: str | None)`; `Skip(recording: LoggedInRecording, reason: NotProbedReason)`; `ProbePlan(host: str, items: tuple[ProbeItem, ...], skipped: tuple[Skip, ...], digest: RunDigest = field(repr=False, compare=False))` with `in_scope(self, host: str) -> bool` (`in_primary_scope(self.digest, host)`).
  - `class MissingUserAgentError(ValueError)`, message `"<endpoint key>: the recording holds no User-Agent header for it, and the probe sends none of its own; record the run again"`.
  - `select(digest: RunDigest, har: Sequence[HAREntry], secret_values: AbstractSet[str], *, limit: int = 50) -> ProbePlan`.
  - `describe_plan(plan: ProbePlan) -> list[str]`.
  - `looks_like_jwt(text: str) -> bool`: `re.fullmatch(r"eyJ[A-Za-z0-9_-]{2,}\.eyJ[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]*", text)`.
- Produces in `devtools/probe_secrets.py`: `secret_values(digest: RunDigest, har: Sequence[HAREntry]) -> set[str]`: the non-empty values of every non-None set `flagged_values_of(digest, har)` returns, plus every value `recorded_values(read_run_har(run_dir), {t.name for t in digest.tokens}, run_dir)` returns, where `run_dir = digest.source.har_path.parent`. Raises `RunUnreadableError`.
- Selection order: rows are the digest's endpoints without `family_members`, ordered by `fixture_entry_index` (None last). For each row, the first rule that applies decides: `fixture_entry_index` None gives `no_recording`; `not plan.in_scope(row.host)` gives `off_scope_host`; a GraphQL POST row (`operation` set) gives `persisted_query` when an operation of `entry_operations(entry)` has `document` None, `unparsed_document` when `document_definitions` is `UNPARSED`, `not_a_query` when a non-fragment kind is not `query`, else replayed; any other non-GET gives `method`; a GET gives `auth_url` (`is_auth_path`), `logout_url`, `login_flow`, `unsafe_segment`, or `not_a_query` (a `query` parameter whose definitions hold a `mutation`), in that order. Then a replayable row gives `secret_value` when an unquoted path segment or a query value is in `secret_values`, and `jwt` when one passes `looks_like_jwt`. A replayable row with no recorded `User-Agent` raises `MissingUserAgentError`. Replayable rows past `limit` become `over_limit` skips. An item's headers are the recorded `User-Agent`, `Accept`, and `Accept-Language` (looked up without regard to case, each only when recorded), plus `("Content-Type", "application/json")` on a GraphQL POST; its body is the recorded `post_data` on a GraphQL POST and None otherwise. `LoggedInRecording.body_size` is `response.body_size` when positive, else the UTF-8 length of `response.body`.

- [ ] **Step 1: Write `tests/unit/probe_har.py`** (see Shared test helpers) and the failing tests.

```python
@pytest.fixture
def run(tmp_path: Path) -> tuple[RunDigest, list[HAREntry]]:
    return probe_digest(write_probe_run(tmp_path))

def test_every_recorded_request_is_a_row(run) -> None:
    d, _ = run
    indices = {e.fixture_entry_index for e in d.endpoints if not e.family_members}
    assert set(PATHS) <= indices  # if this fails, change the entry in probe_har.py, not this assertion

def test_the_session_cookie_value_is_known(run) -> None:
    d, entries = run
    assert flagged_values_of(d, entries)["sid"] == frozenset({SESSION_VALUE})
    assert {SESSION_VALUE, CSRF_VALUE} <= secret_values(d, entries)  # CSRF_VALUE through recorded_values

def test_the_plan_replays_exactly_the_safe_rows(run) -> None:
    d, entries = run
    plan = select(d, entries, secret_values(d, entries))
    assert [i.recording.entry_index for i in plan.items] == [ME, VIEWER, CART, PRINT, DOWNLOAD, ACCOUNT, ORDERS, CATALOG, PROMO, HANGUP, BIG, EXPORT, SLOW_DOWN, AFTER]

def test_each_skip_has_its_reason(run) -> None:
    d, entries = run
    plan = select(d, entries, secret_values(d, entries))
    assert {s.recording.entry_index: s.reason for s in plan.skipped} == {
        ADD_TO_CART: "not_a_query", MIXED: "not_a_query", WISHLIST: "persisted_query",
        DELETE_ITEM: "method", LOGOUT: "logout_url", SEARCH: "secret_value", TOKEN_CHECK: "jwt",
        UNSUBSCRIBE: "unsafe_segment", SESSION_API: "auth_url", GET_MUTATION: "not_a_query",
    }

def test_a_login_flow_row_is_skipped(run) -> None:
    d, entries = run
    d = replace(d, endpoints=tuple(replace(e, login_flow=True) if e.fixture_entry_index == CATALOG else e for e in d.endpoints))
    assert {s.recording.entry_index: s.reason for s in select(d, entries, set()).skipped}[CATALOG] == "login_flow"

def test_an_unparsed_document_is_never_replayed(run, monkeypatch) -> None:
    d, entries = run
    monkeypatch.setattr("graftpunk.har.probe_select.document_definitions", lambda _doc: UNPARSED)
    assert {s.recording.entry_index: s.reason for s in select(d, entries, set()).skipped}[VIEWER] == "unparsed_document"

def test_a_host_off_the_primary_scope_is_skipped(run) -> None:
    d, entries = run
    plan = select(replace(d, primary_host="elsewhere.example"), entries, set())
    assert plan.items == () and {s.reason for s in plan.skipped} == {"off_scope_host"}

def test_rows_past_the_limit_are_over_limit(run) -> None:
    d, entries = run
    plan = select(d, entries, set(), limit=3)
    assert [i.recording.entry_index for i in plan.items] == [ME, VIEWER, CART]
    assert [s.recording.entry_index for s in plan.skipped if s.reason == "over_limit"][:2] == [PRINT, DOWNLOAD]

def test_an_item_carries_only_the_three_recorded_headers(run) -> None:
    d, entries = run
    plan = select(d, entries, set())
    get = next(i for i in plan.items if i.recording.entry_index == ME)
    post = next(i for i in plan.items if i.recording.entry_index == VIEWER)
    assert [n for n, _ in get.headers] == ["User-Agent", "Accept", "Accept-Language"]
    assert dict(post.headers)["Content-Type"] == "application/json"
    assert post.body is not None and "Viewer" in post.body and get.body is None
    assert dict(get.headers)["User-Agent"] == "Mozilla/5.0 (probe test)"

def test_a_recording_without_a_user_agent_refuses_the_run(tmp_path: Path) -> None:
    # write_probe_run, then delete the User-Agent header of entry ME in network.har
    with pytest.raises(MissingUserAgentError, match="GET /api/me"):
        select(d, entries, set())

def test_an_unreadable_run_raises_before_any_plan(tmp_path: Path) -> None:
    # write_probe_run, then give entry CATALOG's response content {"_bodyFile": "bodies/gone.json"} and no text
    with pytest.raises(RunUnreadableError, match="bodies/gone.json"):
        secret_values(d, entries)

@pytest.mark.parametrize(("text", "jwt"), [(JWT, True), ("eyJhbGciOiJIUzI1NiJ9", False), ("abc.def.ghi", False)])
def test_looks_like_jwt(text: str, jwt: bool) -> None:
    assert looks_like_jwt(text) is jwt

def test_describe_plan(run) -> None:
    d, entries = run
    lines = describe_plan(select(d, entries, secret_values(d, entries)))
    assert lines[0] == "host: myshop.example"
    assert lines[1] == "requests: 14, one at a time"
    assert "  GET /api/me" in lines
    assert any(line.endswith(": secret_value") for line in lines)
    assert lines[-4:] == [
        "These requests go to the live site from this machine.",
        "No cookies or credentials are sent.",
        "URLs and GraphQL variables are sent as recorded and may hold account ids.",
        "The site may log them or rate-limit this address.",
    ]
    assert SESSION_VALUE not in "\n".join(lines) and JWT not in "\n".join(lines)
```

Run: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4 tests/unit/test_probe_select.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'graftpunk.har.probe_select'`.

- [ ] **Step 2: Implement `har/probe_select.py` and `devtools/probe_secrets.py`.** `describe_plan` prints `host: <plan.host>`, `requests: <n>, one at a time`, `  <endpoint key>` per item, `skipped: <n>`, `  <endpoint key>: <reason>` per skip, then the four warning lines.

- [ ] **Step 3: Run the tests.**

Run: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4 tests/unit/test_probe_select.py tests/unit/test_har_layering.py`
Expected: PASS. Then the full gate.

- [ ] **Step 4: Commit.**

```bash
git add src/graftpunk/har/probe_select.py src/graftpunk/devtools/probe_secrets.py tests/unit/probe_har.py tests/unit/test_probe_select.py
git commit -m "feat: select the requests a logged-out probe replays, with every skip and its reason"
```

### Task 6: Sending

**Files:**
- Create: `src/graftpunk/observe/probe.py`
- Create: `tests/unit/probe_site.py`, `tests/unit/test_observe_probe_send.py`; modify `tests/unit/conftest.py:32` (`observe_http_site`), to import `probe_site` beside it

**Interfaces:**
- Consumes: `ProbePlan`, `ProbeItem` (Task 5); `ProbeResponse`, `NotProbed`, `classify`, `runtime_catches` (Task 3); `normalize_media_type`, `normal_host`.
- Produces in `observe/probe.py`: `ACCEPT_ENCODING: Final = "gzip, deflate, br"`, `MAX_REDIRECTS: Final = 5`, `TIMEOUT_SECONDS: Final = 10`, `MAX_BODY_BYTES: Final = 256 * 1024`; `ProbeOutcome = ProbeResponse | NotProbed`; `default_session() -> requests.Session`; `send(plan: ProbePlan, *, session_factory: Callable[[], requests.Session] | None = None, delay: float = 1.0, sleep: Callable[[float], None] = time.sleep) -> list[ProbeOutcome]` (one outcome per `plan.items`, in order).
- Behaviour: the session from `(session_factory or default_session)()` gets `trust_env = False`, `headers.clear()`, and `cookies.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))`. Each request: `session.request(method, url, headers={**dict(item.headers), "Accept-Encoding": ACCEPT_ENCODING}, data=body, proxies=requests.utils.get_environ_proxies(url), verify=os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE") or True, timeout=TIMEOUT_SECONDS, allow_redirects=False, stream=True)`. A 301, 302, 303, 307, or 308 with a `Location` is followed by hand up to `MAX_REDIRECTS` times: the target is `urljoin(current, location)`; a target whose `normal_host` fails `plan.in_scope` (or whose scheme is not http or https) is not requested and ends the chain with `offsite_host` set (the host, or the scheme when there is none); 303, and 301 or 302 after a POST, continue as a GET with no body; 307 and 308 keep the method and body. The final body is read with `iter_content` until `MAX_BODY_BYTES` and decoded with the response's encoding (UTF-8 when it names none or an unknown one), `errors="replace"`. `headers_sent` is the case-insensitive union over the hops of `list(response.request.headers)` plus `"Host"`; `proxy_used` is whether `requests.utils.select_proxy(url, proxies)` chose one on any hop; `retry_after` is whether the final response has a `Retry-After`. A `requests.RequestException` gives `NotProbed("request_failed")`. After a final status of 429, or 503 with `Retry-After`, every later item is `NotProbed("rate_limited")` and is not sent. `sleep(delay)` runs before every request but the first.

- [ ] **Step 1: Write `tests/unit/probe_site.py`** (see Shared test helpers, `run_probe` included) **and the failing tests.**

```python
@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)

def _by_index(result: ProbeRun) -> dict[int, LoggedOutEntry]:
    return {e.entry_index: e for e in result.entries}

def test_each_probed_endpoint_gets_its_class(tmp_path, probe_site) -> None:
    got = _by_index(run_probe(write_probe_run(tmp_path), probe_site))
    assert {i: (got[i].probe_class, got[i].login_signal, got[i].not_probed) for i in (ME, VIEWER, CART, PRINT, DOWNLOAD, ACCOUNT, ORDERS, CATALOG, PROMO, HANGUP, BIG, EXPORT, SLOW_DOWN, AFTER)} == {
        ME: ("rejected", None, None), VIEWER: ("empty_data", None, None), CART: ("graphql_error", None, None),
        PRINT: ("html_for_non_html", None, None), DOWNLOAD: ("rejected", None, None),
        ACCOUNT: ("login_page", "login_form", None), ORDERS: ("login_page", "auth_path", None),
        CATALOG: ("same_as_logged_in", None, None), PROMO: ("redirect_offsite", None, None),
        HANGUP: (None, None, "request_failed"), BIG: ("other", None, None),
        EXPORT: ("html_for_non_html", None, None), SLOW_DOWN: ("rate_limited", None, None),
        AFTER: (None, None, "rate_limited"),
    }
    assert got[VIEWER].emptied_paths == ("data.viewer", "data.orders")
    assert got[PROMO].offsite_host == "elsewhere.example"
    assert got[BIG].body_length == MAX_BODY_BYTES

def test_the_server_never_sees_a_skipped_request(tmp_path, probe_site) -> None:
    run_probe(write_probe_run(tmp_path), probe_site)
    paths = [r.path.split("?")[0] for r in probe_site.received]
    assert not {"/logout", "/api/search", "/api/token-check", "/newsletter/unsubscribe", "/api/session", "/api/after"} & set(paths)
    assert all(r.method in ("GET", "POST") for r in probe_site.received)
    assert not any(b"mutation" in r.body or b"persistedQuery" in r.body for r in probe_site.received)
    assert not any(r.path.startswith("/graphql?") for r in probe_site.received)

def test_no_credential_or_custom_header_reaches_the_server(tmp_path, probe_site) -> None:
    run_probe(write_probe_run(tmp_path), probe_site)
    sent = {name.lower() for r in probe_site.received for name, _ in r.headers}
    assert not sent & {"cookie", "authorization", "proxy-authorization", "referer", "origin", CUSTOM_HEADER.lower()}

def test_a_set_cookie_is_never_sent_back(tmp_path, probe_site) -> None:
    run_probe(write_probe_run(tmp_path), probe_site)
    assert probe_site.received[0].path == "/api/me"
    assert all("cookie" not in {n.lower() for n, _ in r.headers} for r in probe_site.received[1:])

def test_the_headers_on_the_wire(tmp_path, probe_site) -> None:
    result = run_probe(write_probe_run(tmp_path), probe_site)
    for r in probe_site.received:
        headers = {n.lower(): v for n, v in r.headers}
        assert headers["accept-encoding"] == "gzip, deflate, br"
        assert "connection" not in headers
        assert headers["user-agent"] == "Mozilla/5.0 (probe test)"
    on_wire = {n.lower() for r in probe_site.received for n, _ in r.headers}
    listed = {n.lower() for o in result.outcomes if isinstance(o, ProbeResponse) for n in o.headers_sent}
    assert on_wire == listed

def test_a_429_stops_the_run(tmp_path, probe_site) -> None:
    run_probe(write_probe_run(tmp_path), probe_site)
    assert probe_site.received[-1].path == "/api/slow-down"

def test_an_off_scope_redirect_is_not_requested(tmp_path, probe_site) -> None:
    run_probe(write_probe_run(tmp_path), probe_site)  # _Refuse would raise AssertionError on elsewhere.example
    assert [r.path for r in probe_site.received].count("/promo") == 1

def test_a_failed_request_does_not_stop_the_run(tmp_path, probe_site) -> None:
    run_probe(write_probe_run(tmp_path), probe_site)
    paths = [r.path for r in probe_site.received]
    assert paths.index("/api/hangup") < paths.index("/api/big")

def test_https_proxy_is_handed_to_the_transport(tmp_path, probe_site, monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.myshop.example:3128")
    result = run_probe(write_probe_run(tmp_path), probe_site)
    assert requests.utils.select_proxy("https://myshop.example/api/me", probe_site.proxies_seen[0]) == "http://proxy.myshop.example:3128"
    assert all(o.proxy_used for o in result.outcomes if isinstance(o, ProbeResponse))

def test_a_host_in_no_proxy_goes_direct(tmp_path, probe_site, monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.myshop.example:3128")
    monkeypatch.setenv("NO_PROXY", "myshop.example")
    result = run_probe(write_probe_run(tmp_path), probe_site)
    assert requests.utils.select_proxy("https://myshop.example/api/me", probe_site.proxies_seen[0]) is None
    assert not any(o.proxy_used for o in result.outcomes if isinstance(o, ProbeResponse))

def test_a_netrc_adds_no_authorization(tmp_path, probe_site, monkeypatch) -> None:
    netrc = tmp_path / "home" / ".netrc"
    netrc.parent.mkdir()
    netrc.write_text("machine myshop.example login ada password secret\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("HOME", str(netrc.parent))
    monkeypatch.setenv("NETRC", str(netrc))
    run_probe(write_probe_run(tmp_path), probe_site)
    assert all("authorization" not in {n.lower() for n, _ in r.headers} for r in probe_site.received)

def test_at_most_five_redirects_are_followed(tmp_path, probe_site) -> None:
    d, entries = probe_digest(write_probe_run(tmp_path))
    me = select(d, entries, set(), limit=1).items[0]
    loop = ProbeItem(recording=me.recording, url="https://myshop.example/loop", headers=me.headers, body=None)
    (outcome,) = send(ProbePlan(host="myshop.example", items=(loop,), skipped=(), digest=d), session_factory=probe_site.session, delay=0)
    assert [r.path for r in probe_site.received] == ["/loop"] * 6
    assert isinstance(outcome, ProbeResponse) and outcome.final_status == 302

def test_requests_are_a_delay_apart(tmp_path, probe_site) -> None:
    d, entries = probe_digest(write_probe_run(tmp_path))
    waits: list[float] = []
    send(select(d, entries, secret_values(d, entries), limit=3), session_factory=probe_site.session, delay=0.25, sleep=waits.append)
    assert waits == [0.25, 0.25]

def test_request_json_agrees_with_runtime_catches(tmp_path, probe_site) -> None:
    """The stub comments rest on this: request_json raises SessionRejectedError exactly
    where runtime_catches says it does, on the same server answers."""
    result = run_probe(write_probe_run(tmp_path), probe_site)
    site_requests = SiteRequests(probe_site.session(), "myshop", "https://myshop.example")
    checked = set()
    for entry in result.entries:
        if entry.method != "GET" or entry.probe_class not in ("rejected", "login_page"):
            continue
        expected = SessionRejectedError if runtime_catches(entry) else UnexpectedResponseError
        with pytest.raises(expected):
            site_requests.json("GET", PATHS[entry.entry_index])
        checked.add((entry.probe_class, entry.login_signal))
    assert checked == {("rejected", None), ("login_page", "login_form"), ("login_page", "auth_path")}
```

Run: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4 tests/unit/test_observe_probe_send.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'graftpunk.observe.probe'`.

- [ ] **Step 2: Implement `send` and `default_session` in `observe/probe.py`.** Close each response after its body is read or its redirect taken.

- [ ] **Step 3: Run the tests.**

Run: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4 tests/unit/test_observe_probe_send.py tests/unit/test_har_layering.py`
Expected: PASS. Then the full gate.

- [ ] **Step 4: Commit.**

```bash
git add src/graftpunk/observe/probe.py tests/unit/probe_site.py tests/unit/test_observe_probe_send.py tests/unit/conftest.py
git commit -m "feat: send replays a probe plan without cookies or credentials, one request at a time"
```

### Task 7: The file

**Files:**
- Modify: `src/graftpunk/contracts.py` (`Surface`, `LOGGED_OUT_SCHEMA`, `_CURRENT`, `__all__`, module docstring)
- Modify: `src/graftpunk/har/logged_out.py`, `src/graftpunk/observe/probe.py`
- Test: `tests/unit/test_logged_out_file.py`; modify `tests/unit/test_contracts.py`

**Interfaces:**
- Produces in `contracts.py`: `Surface = Literal["endpoints", "info", "logged_out", "sidecar"]`, `LOGGED_OUT_SCHEMA: Final = 1`, `_CURRENT["logged_out"] = LOGGED_OUT_SCHEMA`; not in `CLI_SURFACES`.
- Produces in `har/logged_out.py`: `LOGGED_OUT_FILE: Final = "logged-out.json"`; `Ignored(line: str)`; `to_json(entries: Sequence[LoggedOutEntry], outcomes: Sequence[ProbeResponse | NotProbed]) -> dict[str, Any]` returning `{"headers_sent": [...], "proxy_used": bool, "entries": [...]}` (headers sorted, deduplicated without regard to case; each entry with every key always present: `endpoint_key`, `entry_index`, `method`, `graphql`, `recorded_size`, `class`, `login_signal`, `not_probed`, `first_status`, `location` (`{"scheme", "host", "path"}` or null), `final_status`, `media_type`, `body_length`, `offsite_host`, `emptied_paths`, `emptied_count`); `read_logged_out(run_dir: Path) -> LoggedOut | Ignored | None`; `join(entries: Iterable[LoggedOutEntry], rows: Iterable[RowRef]) -> tuple[dict[int, LoggedOutEntry], int]` (keyed by `RowRef.position`; an entry goes to every row with its `entry_index`, else to every row whose `key` equals its `endpoint_key`, else counts as unjoined; an `entry_index` of None never matches by index).
- Produces in `observe/probe.py`: `write_logged_out(run_dir: Path, entries: Sequence[LoggedOutEntry], outcomes: Sequence[ProbeOutcome], *, now: datetime | None = None) -> Path`, writing `json.dumps({**to_json(entries, outcomes), "schema": current_schema("logged_out"), "probed_at": (now or datetime.now(UTC)).isoformat(timespec="seconds")}, indent=2, sort_keys=True) + "\n"` with `write_private_file` and returning the path.
- `Ignored` lines, exact: a missing or unknown schema gives `logged-out.json is ignored: its schema is missing or not one this graftpunk reads (a newer graftpunk wrote it, or it was edited); run gp observe probe again to replace it.`; anything else that does not read gives `logged-out.json is ignored: it cannot be read as a logged-out probe (<exception class name>); run gp observe probe again to replace it.`

- [ ] **Step 1: Write the failing tests.**

```python
# tests/unit/test_contracts.py additions
def test_the_logged_out_file_is_versioned_and_not_read_through_the_cli() -> None:
    assert current_schema("logged_out") == LOGGED_OUT_SCHEMA == 1
    assert "logged_out" not in cli_contracts()
    assert written_schemas("logged_out") == (1,)
    assert contract_mismatch("logged_out", 1) == ("logged_out: graftpunk versions it, but it is not read through the gp CLI, so there is no contract to check.")
```

```python
# tests/unit/test_logged_out_file.py
_ENTRY = LoggedOutEntry("GET /api/me", 0, "GET", False, 30, probe_class="rejected", first_status=401, final_status=401, media_type="application/json", body_length=24)
_SKIP = LoggedOutEntry("GET /logout", 16, "GET", False, 0, not_probed="logout_url")
_RESP = ProbeResponse(401, "", 401, "https://myshop.example/api/me", "application/json", '{"error": "x"}', 14, headers_sent=("User-Agent", "Host", "accept-encoding"), proxy_used=False)

def test_the_file_round_trips(tmp_path: Path) -> None:
    path = write_logged_out(tmp_path, (_ENTRY, _SKIP), (_RESP,), now=datetime(2026, 11, 27, tzinfo=UTC))
    assert path == tmp_path / LOGGED_OUT_FILE
    read = read_logged_out(tmp_path)
    assert isinstance(read, LoggedOut)
    assert read.entries == (_ENTRY, _SKIP)
    assert read.schema == current_schema("logged_out")
    assert read.probed_at == "2026-11-27T00:00:00+00:00"
    assert {n.lower() for n in read.headers_sent} == {"user-agent", "host", "accept-encoding"}
    assert json.loads(path.read_text())["entries"][0]["class"] == "rejected"

def test_the_file_is_private(tmp_path: Path) -> None:
    assert stat.S_IMODE(write_logged_out(tmp_path, (_ENTRY,), (_RESP,)).stat().st_mode) == 0o600

def test_no_file_reads_as_none(tmp_path: Path) -> None:
    assert read_logged_out(tmp_path) is None

@pytest.mark.parametrize("schema", [{}, {"schema": 0}, {"schema": 2}, {"schema": "1"}], ids=["missing", "zero", "newer", "text"])
def test_an_unknown_schema_is_ignored_with_one_line(tmp_path: Path, schema: dict[str, object]) -> None:
    doc = json.loads(write_logged_out(tmp_path, (_ENTRY,), (_RESP,)).read_text())
    del doc["schema"]
    (tmp_path / LOGGED_OUT_FILE).write_text(json.dumps({**doc, **schema}))
    read = read_logged_out(tmp_path)
    assert isinstance(read, Ignored)
    assert read.line.startswith("logged-out.json is ignored: its schema is missing or not one this graftpunk reads")
    assert "\n" not in read.line

@pytest.mark.parametrize("text", ["{not json", '{"schema": 1, "entries": [{"class": "sideways"}]}', '{"schema": 1}', "[]"])
def test_a_file_that_does_not_read_is_ignored(tmp_path: Path, text: str) -> None:
    (tmp_path / LOGGED_OUT_FILE).write_text(text)
    read = read_logged_out(tmp_path)
    assert isinstance(read, Ignored) and read.line.startswith("logged-out.json is ignored: it cannot be read as a logged-out probe (")

def test_join_by_har_index_then_key() -> None:
    rows = [RowRef(0, 0, "GET /api/me"), RowRef(1, 7, "GET /api/catalog"), RowRef(2, None, "GET /api/x")]
    moved = replace(_ENTRY, endpoint_key="GET /api/me/v2")          # key changed, index still 0
    by_key = replace(_ENTRY, entry_index=99, endpoint_key="GET /api/catalog")
    lost = replace(_ENTRY, entry_index=98, endpoint_key="GET /gone")
    joined, unjoined = join([moved, by_key, lost], rows)
    assert joined == {0: moved, 1: by_key}
    assert unjoined == 1

def test_the_file_holds_no_body_cookie_header_value_or_proxy_url(tmp_path: Path, probe_site, monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://ada:hunter2@proxy.myshop.example:3128")
    run_dir = write_probe_run(tmp_path)
    result = run_probe(run_dir, probe_site)
    text = write_logged_out(run_dir, result.entries, result.outcomes).read_text()
    for needle in (SESSION_VALUE, JWT, "web-7", "login required", "Print preview", "Ada", "proxy.myshop.example", "hunter2", "ref=promo", "Mozilla/5.0"):
        assert needle not in text, needle
    assert json.loads(text)["proxy_used"] is True

def test_the_listed_headers_are_the_ones_the_server_saw(tmp_path: Path, probe_site) -> None:
    run_dir = write_probe_run(tmp_path)
    result = run_probe(run_dir, probe_site)
    listed = json.loads(write_logged_out(run_dir, result.entries, result.outcomes).read_text())["headers_sent"]
    assert {n.lower() for n in listed} == {n.lower() for r in probe_site.received for n, _ in r.headers}
```

The schema parametrisation builds four files by hand: no `schema` key, then `0`, `2`, and `"1"`; write it as four explicit dictionaries if the one-liner above reads badly.

Run: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4 tests/unit/test_logged_out_file.py tests/unit/test_contracts.py`
Expected: FAIL with `ImportError: cannot import name 'LOGGED_OUT_SCHEMA'`.

- [ ] **Step 2: Implement the contracts surface, `to_json`, `read_logged_out` (with `refuse_unknown_schema("logged_out", doc.get("schema"))` first, then a parse that checks each Literal field against its tuple and each type, raising on anything else), `join`, and `write_logged_out`.** The reader catches `UnknownSchemaError` for the schema line and `(OSError, ValueError, TypeError, KeyError, RecursionError)` for the other; it never echoes file content.

- [ ] **Step 3: Run the tests.**

Run: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4 tests/unit/test_logged_out_file.py tests/unit/test_contracts.py tests/unit/test_har_layering.py`
Expected: PASS. Then the full gate.

- [ ] **Step 4: Commit.**

```bash
git add src/graftpunk/contracts.py src/graftpunk/har/logged_out.py src/graftpunk/observe/probe.py tests/unit/test_logged_out_file.py tests/unit/test_contracts.py
git commit -m "feat: logged-out.json, its versioned reader, the row join, and the private writer"
```

### Task 8: `gp observe probe`

**Files:**
- Modify: `src/graftpunk/cli/observe_commands.py` (module docstring "all seven commands" becomes eight and names `probe`; the new command after `observe_show`)
- Test: `tests/unit/test_observe_probe_command.py`

**Interfaces:**
- Consumes: `resolve_run`, `_refuse_recording`; `DigestSource`, `digest`; `parse_har_file`, `HARParseError`; `secret_values` (Task 5); `select`, `describe_plan`, `MissingUserAgentError` (Task 5); `send` and `write_logged_out` through `from graftpunk.observe import probe as observe_probe` (so a test's patch of `graftpunk.observe.probe.default_session` is seen); `classify`, `NotProbed`, `read_logged_out`, `LoggedOut`, `summarize`, `describe_summary`; `RunUnreadableError`.
- Produces: `observe_probe(ctx: typer.Context, session_name: str | None = None, run_id: str | None = None, *, yes: bool = False, limit: int = 50, delay: float = 1.0, dry_run: bool = False) -> None`, registered as `@observe_app.command("probe")`, options `--yes`, `--limit` (`min=1`), `--delay` (`min=0.0`), `--dry-run`. Help: `Replay the run's safe requests without the session and record what each endpoint answers logged out.`
- Flow, in this order and with no other logic: session from the argument or `ctx.obj["observe_session"]` (refused as `observe_show` refuses); `resolve_run`; `DigestSource.from_run_dir`; `parse_har_file(...).entries` and `digest(source)` (`FileNotFoundError` and `HARParseError` go to `_refuse_recording`); `secret_values` (`RunUnreadableError` prints `Could not read the run: <message>` in red and exits 1); `select(..., limit=limit)` (`MissingUserAgentError` prints its message in red and exits 1); print `describe_plan` lines; `--dry-run` exits 0; when `plan.items` is non-empty and `--yes` is absent, `typer.confirm("Send these requests?", default=False, abort=True)`; `observe_probe.send(plan, delay=delay)`; entries are `classify(NotProbed(s.reason), s.recording)` for each skip followed by `classify(o, i.recording)` for each item and outcome; `write_logged_out`; print `Written: <path>`; print `describe_summary(summarize(read))` for `read = read_logged_out(run_dir)`. Every printed line goes through `escape` with `soft_wrap=True` and `highlight=False`.

- [ ] **Step 1: Write the failing tests.** Fixture `probe_env(tmp_path, monkeypatch, probe_site)`: patches `graftpunk.cli.observe_commands.OBSERVE_BASE_DIR` to `tmp_path`, `graftpunk.observe.probe.default_session` to `probe_site.session`, clears the proxy variables, and returns `write_probe_run(tmp_path)`. App built as `test_observe_commands._build_app` builds it.

```python
def _invoke(*args: str, input: str | None = None):
    return runner.invoke(_build_app(), ["observe", "probe", "myshop", *args], input=input)

def test_dry_run_prints_the_plan_and_sends_nothing(probe_env, probe_site) -> None:
    result = _invoke("--dry-run")
    assert result.exit_code == 0, result.output
    assert "host: myshop.example" in result.output and "requests: 14, one at a time" in result.output
    assert "No cookies or credentials are sent." in result.output
    assert probe_site.received == []
    assert not (probe_env / "logged-out.json").exists()

def test_yes_probes_writes_the_file_and_prints_the_summary(probe_env, probe_site) -> None:
    result = _invoke("--yes", "--delay", "0")
    assert result.exit_code == 0, result.output
    assert (probe_env / "logged-out.json").is_file()
    assert "suggested session check: GET /api/me, rejected" in result.output
    assert "probed logged out: " in result.output
    assert SESSION_VALUE not in result.output and JWT not in result.output

def test_n_at_the_prompt_sends_nothing(probe_env, probe_site) -> None:
    result = _invoke("--delay", "0", input="n\n")
    assert result.exit_code == 1
    assert probe_site.received == [] and not (probe_env / "logged-out.json").exists()

def test_y_at_the_prompt_probes(probe_env, probe_site) -> None:
    assert _invoke("--delay", "0", input="y\n").exit_code == 0
    assert probe_site.received

def test_limit_caps_the_requests(probe_env, probe_site) -> None:
    assert _invoke("--yes", "--delay", "0", "--limit", "2").exit_code == 0
    assert [r.path for r in probe_site.received] == ["/api/me", "/graphql"]
    entries = json.loads((probe_env / "logged-out.json").read_text())["entries"]
    assert sum(e["not_probed"] == "over_limit" for e in entries) == 12

def test_the_group_session_option_names_the_run(probe_env, probe_site) -> None:
    result = runner.invoke(_build_app(), ["observe", "--session", "myshop", "--no-session", "probe", "--dry-run"])
    assert result.exit_code == 0, result.output

def test_an_unreadable_run_is_a_one_line_refusal(probe_env, probe_site) -> None:
    # give entry CATALOG's response {"_bodyFile": "bodies/gone.json"} in network.har
    result = _invoke("--yes")
    assert result.exit_code == 1 and "Could not read the run: cannot read bodies/gone.json" in result.output
    assert probe_site.received == []

def test_a_missing_user_agent_is_a_refusal(probe_env, probe_site) -> None:
    # delete entry ME's User-Agent in network.har
    result = _invoke("--yes")
    assert result.exit_code == 1 and "holds no User-Agent" in result.output
    assert probe_site.received == []

def test_a_run_with_nothing_to_replay_writes_the_skips_without_asking(tmp_path, monkeypatch, probe_site) -> None:
    # run_dir = write_probe_run(tmp_path), then keep only the LOGOUT and DELETE_ITEM entries
    # of its network.har; patch OBSERVE_BASE_DIR and default_session as probe_env does
    result = _invoke()  # no --yes and no input: a prompt would fail the run
    assert result.exit_code == 0, result.output
    assert probe_site.received == []
    assert {e["not_probed"] for e in json.loads((run_dir / "logged-out.json").read_text())["entries"]} == {"logout_url", "method"}
    assert "suggested session check: none" in result.output

def test_probing_again_replaces_the_file(probe_env, probe_site) -> None:
    assert _invoke("--yes", "--delay", "0", "--limit", "1").exit_code == 0
    assert _invoke("--yes", "--delay", "0").exit_code == 0
    path = probe_env / "logged-out.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not any(e["not_probed"] == "over_limit" for e in json.loads(path.read_text())["entries"])
```

Run: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4 tests/unit/test_observe_probe_command.py`
Expected: FAIL with exit code 2 and `No such command 'probe'`.

- [ ] **Step 2: Implement `observe_probe`.**

- [ ] **Step 3: Run the tests and the CLI suites.**

Run: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4 tests/unit/test_observe_probe_command.py tests/unit/test_observe_commands.py tests/unit/test_cli.py tests/unit/test_cli_no_external_click.py`
Expected: PASS. Then the full gate.

- [ ] **Step 4: Commit.**

```bash
git add src/graftpunk/cli/observe_commands.py tests/unit/test_observe_probe_command.py
git commit -m "feat: gp observe probe records what each endpoint of a run answers without the session"
```

### Task 9: How the digest states it

**Files:**
- Modify: `src/graftpunk/har/digest.py` (`DigestSource` at 300-330, `Endpoint` at 375-530, `RunDigest` at 641-756, the end of `digest()` where `RunDigest(...)` is built)
- Modify: `src/graftpunk/har/report.py` (`_endpoint_block`, `render_markdown`, `_projected_endpoint`, the `endpoints_projection` docstring)
- Test: `tests/unit/test_digest_logged_out.py`

**Interfaces:**
- Consumes: `LOGGED_OUT_FILE`, `read_logged_out`, `join`, `summarize`, `describe`, `describe_summary`, `Ignored`, `LoggedOutEntry`, `LoggedOutSummary` (Tasks 3, 4, 7); `RowRef` (Task 1).
- Produces: `DigestSource.logged_out: Path | None = field(default=None, metadata={OMIT_EMPTY: True})`, set by `from_run_dir` to `run_dir / LOGGED_OUT_FILE` when that is a file; `Endpoint.logged_out: LoggedOutEntry | None = field(default=None, metadata={OMIT_EMPTY: True})`; `RunDigest.logged_out_summary: LoggedOutSummary | None = field(default=None, metadata={OMIT_EMPTY: True})`; `RunDigest.logged_out_ignored: Ignored | None = field(default=None, repr=False, metadata={INTERNAL: True})`; private `_with_logged_out(source: DigestSource, endpoints: tuple[Endpoint, ...]) -> tuple[tuple[Endpoint, ...], LoggedOutSummary | None, Ignored | None]`, called on the final endpoints immediately before `RunDigest(...)` is built: no file gives the input unchanged; `Ignored` passes through; otherwise `join(read.entries, [RowRef(i, e.fixture_entry_index, e.key) for i, e in enumerate(endpoints) if not e.family_members])`, each joined row gets `replace(e, logged_out=entry)`, and the summary is `summarize(read, unjoined=unjoined)`.
- Report: `_endpoint_block` appends `f"- {describe(endpoint.logged_out)}"` just before the `- examples:` line. `render_markdown` adds, after the Endpoints block and before `## Shared values`, `## Logged out` followed by `- <line>` for each `describe_summary` line, or `- <ignored.line>`, then a blank line; nothing when the run was not probed. `_projected_endpoint` adds `logged_out` only when the entry has a `probe_class`: `{"class", "login_signal", "final_status", "media_type", "emptied_paths"}` built field by field. The projection's schema rule is unchanged.

- [ ] **Step 1: Write the failing tests.** Fixture `probed(tmp_path, probe_site)`: `run_dir = write_probe_run(tmp_path)`, `result = run_probe(run_dir, probe_site)`, `write_logged_out(run_dir, result.entries, result.outcomes)`, returns `run_dir`. `_digest(run_dir)` is `digest(DigestSource.from_run_dir(run_dir, session="myshop", run_id="run-1"))`.

```python
def test_the_source_finds_the_file(probed) -> None:
    assert DigestSource.from_run_dir(probed, session="myshop", run_id="run-1").logged_out == probed / "logged-out.json"

def test_a_probed_endpoint_block_carries_its_line(probed) -> None:
    text = render_markdown(_digest(probed))
    assert "- logged out: 401 application/json, rejected" in text
    assert "- logged out: 302 to /login, then 200 text/html, login_page (login_form)" in text
    assert "- logged out: 200 application/json, empty_data (data.viewer, data.orders)" in text
    assert "- logged out: not probed (secret_value)" in text

def test_the_logged_out_section_prints_the_summary(probed) -> None:
    d = _digest(probed)
    text = render_markdown(d)
    section = text[text.index("## Logged out"):]
    for line in describe_summary(d.logged_out_summary):
        assert f"- {line}" in section

def test_json_carries_the_entry_and_the_summary(probed) -> None:
    rendered = json.loads(render_json(_digest(probed)))
    me = next(e for e in rendered["endpoints"] if e["template"] == "/api/me")
    assert me["logged_out"]["probe_class"] == "rejected"
    assert rendered["logged_out_summary"]["suggested"]["endpoint_key"] == "GET /api/me"
    assert "logged_out_ignored" not in rendered

def test_endpoints_json_carries_logged_out_on_probed_entries_only(probed) -> None:
    projected = endpoints_projection(_digest(probed))
    me = next(e for e in projected["endpoints"] if e["template"] == "/api/me")
    assert me["logged_out"] == {"class": "rejected", "login_signal": None, "final_status": 401, "media_type": "application/json", "emptied_paths": []}
    logout = next(e for e in projected["endpoints"] if e["template"] == "/logout")
    assert "logged_out" not in logout

def test_a_schema_1_run_stays_schema_1(tmp_path, probe_site) -> None:
    # write_probe_run, drop every POST /graphql and GET /graphql entry, probe, write the file
    assert endpoints_projection(_digest(run_dir))["schema"] == 1
    assert any("logged_out" in e for e in endpoints_projection(_digest(run_dir))["endpoints"])

def test_a_newer_schema_is_ignored_with_one_line(probed) -> None:
    path = probed / "logged-out.json"
    doc = json.loads(path.read_text())
    doc["schema"] = doc["schema"] + 1
    path.write_text(json.dumps(doc))
    d = _digest(probed)
    text = render_markdown(d)
    assert text.count("logged-out.json is ignored") == 1
    assert "- logged out:" not in text
    assert all(e.logged_out is None for e in d.endpoints)

def test_a_file_that_does_not_read_is_ignored(probed) -> None:
    (probed / "logged-out.json").write_text("{cut")
    assert "logged-out.json is ignored: it cannot be read" in render_markdown(_digest(probed))

def test_an_entry_whose_key_changed_still_joins_by_index(probed) -> None:
    path = probed / "logged-out.json"
    doc = json.loads(path.read_text())
    for entry in doc["entries"]:
        if entry["entry_index"] == ME:
            entry["endpoint_key"] = "GET /api/me/old"
    path.write_text(json.dumps(doc))
    me = next(e for e in _digest(probed).endpoints if e.template == "/api/me")
    assert me.logged_out is not None and me.logged_out.endpoint_key == "GET /api/me/old"

def test_unjoined_entries_print_one_warning(probed) -> None:
    path = probed / "logged-out.json"
    doc = json.loads(path.read_text())
    doc["entries"].append({**doc["entries"][0], "entry_index": 999, "endpoint_key": "GET /gone"})
    path.write_text(json.dumps(doc))
    text = render_markdown(_digest(probed))
    assert text.count("warning: 1 logged-out entry matches no endpoint of this digest") == 1

def test_removing_the_file_renders_the_run_as_before(tmp_path, probe_site) -> None:
    run_dir = write_probe_run(tmp_path)
    before = {f.__name__: f(_digest(run_dir)) for f in (render_markdown, render_json, render_endpoints_json)}
    result = run_probe(run_dir, probe_site)
    write_logged_out(run_dir, result.entries, result.outcomes).unlink()
    assert {f.__name__: f(_digest(run_dir)) for f in (render_markdown, render_json, render_endpoints_json)} == before
```

Run: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4 tests/unit/test_digest_logged_out.py`
Expected: FAIL with `AttributeError: 'DigestSource' object has no attribute 'logged_out'`.

- [ ] **Step 2: Implement the fields, `_with_logged_out`, and the three report changes.** Add `logged_out` to the `endpoints_projection` docstring: added within schemas 1 and 2, on probed entries only.

- [ ] **Step 3: Run the tests, the golden, and the digest suites.**

Run: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4 tests/unit/test_digest_logged_out.py tests/unit/test_digest_unprobed_golden.py tests/unit/test_har_report.py tests/unit/test_har_digest.py tests/unit/test_digest_families.py tests/unit/test_digest_inline_json.py tests/unit/test_digest_after_read.py tests/unit/test_graft_references.py`
Expected: PASS. Then the full gate.

- [ ] **Step 4: Commit.**

```bash
git add src/graftpunk/har/digest.py src/graftpunk/har/report.py tests/unit/test_digest_logged_out.py
git commit -m "feat: the digest prints each endpoint's logged-out answer and the run's suggested session check"
```

### Task 10: What the scaffold suggests

**Files:**
- Modify: `src/graftpunk/devtools/scaffold/render.py` (`ScaffoldSpec` at 233-249; `_render_command_stub` notes loop near 1940; `_render_graphql_stub` notes at 1771-1775)
- Modify: `src/graftpunk/cli/scaffold_commands.py` (`plugin_new` after the digest; `_print_next_steps`)
- Test: `tests/unit/test_scaffold_logged_out.py`

**Interfaces:**
- Consumes: `Endpoint.logged_out`, `RunDigest` (Task 9); `runtime_catches`, `read_logged_out`, `summarize`, `describe_summary`, `LoggedOut`, `Ignored`, `LoggedOutSummary`; `GP_FILL_MARKER` (`devtools/scaffold/policy.py`).
- Produces: `ScaffoldSpec.logged_out_summary: LoggedOutSummary | None = None`; private `_logged_out_notes(endpoint: Endpoint, call: Literal["request_json", "request_text"]) -> list[str]`, added to each stub's notes (the GraphQL stub passes `"request_json"`), so `gp plugin add-command` stubs carry them too. `_print_next_steps` prints `describe_summary(spec.logged_out_summary)` lines before the fixture list, and before its early return. `plugin_new` reads `read_logged_out(run_dir)` after the digest, prints `ignored.line` for an `Ignored`, and passes `summarize(read)` for a `LoggedOut`.
- Notes, exact copy, by the entry's class (no note for `redirect_offsite`, `rate_limited`, `not_found`, `server_error`, `other`, or a not-probed entry):
  - `rejected` (either call), and `login_page` with `login_form` on `request_json`: `Logged out, this endpoint answered <HTTP <status> | a login form>: <call> raises SessionRejectedError then, so it needs no session check of its own (gp observe probe).`
  - `login_page` with `auth_path` on `request_json`: `GP-FILL: logged out, this endpoint ends on an auth path<, <location path>,> with a page that holds no password form, so request_json raises UnexpectedResponseError there, not SessionRejectedError; catch it and raise SessionRejectedError(...) for a dead session.`
  - `login_page` (either signal) on `request_text`: `GP-FILL: logged out, this endpoint answers a login page, and request_text decides by status alone, so it returns that page on a dead session; check the text and raise SessionRejectedError(...).`
  - `empty_data`: `GP-FILL: logged out, this endpoint answers <status> with <paths> empty, which no status check sees; check it, for example: if not body["data"]["viewer"]: raise SessionRejectedError(...)` (the example from the first stored path; with none stored, `check the field that is empty logged out` in its place).
  - `graphql_error`: `GP-FILL: logged out, this operation answers with GraphQL errors and no data, which request_json returns as JSON; check it, for example: if body.get("errors") and body.get("data") is None: raise SessionRejectedError(...)`
  - `html_for_non_html` on `request_text`: `GP-FILL: logged out, this endpoint answers an HTML page where the recording held another type, and status alone will not catch a dead session here; check the text and raise SessionRejectedError(...).`
  - `html_for_non_html` on `request_json`: `GP-FILL: logged out, this endpoint answers an HTML page that is not a login form, so request_json raises UnexpectedResponseError, not SessionRejectedError, on a dead session; catch it and raise SessionRejectedError(...) for a dead session.`
  - `same_as_logged_in`: `Logged out, this endpoint answers the same shape as logged in: it is public and cannot tell a dead session (gp observe probe).`

- [ ] **Step 1: Write the failing tests.** Fixture `probed` as in Task 9; `_module(run_dir) -> str` renders `ScaffoldSpec(name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example", digest=d)` and returns the plugin module's text; `_stub(text, name) -> str` cuts the text of one command's method out of it.

```python
def test_a_caught_endpoint_needs_no_check(probed) -> None:
    stub = _stub(_module(probed), "api_me")
    assert "request_json raises SessionRejectedError then, so it needs no session check of its own" in " ".join(stub.split())
    assert "GP-FILL: logged out" not in stub

def test_an_auth_path_login_page_names_unexpected_response_error(probed) -> None:
    stub = " ".join(_stub(_module(probed), "orders").split())
    assert f"{GP_FILL_MARKER}: logged out, this endpoint ends on an auth path" in stub
    assert "UnexpectedResponseError" in stub

def test_empty_data_writes_the_check_out(probed) -> None:
    stub = " ".join(_stub(_module(probed), "viewer").split())
    assert 'if not body["data"]["viewer"]: raise SessionRejectedError(...)' in stub

def test_graphql_errors_get_their_check(probed) -> None:
    assert "answers with GraphQL errors and no data" in " ".join(_stub(_module(probed), "cart").split())

def test_html_on_a_text_stub_says_status_will_not_catch_it(probed) -> None:
    stub = " ".join(_stub(_module(probed), "api_export").split())
    assert "return ctx.request_text(" in _stub(_module(probed), "api_export")
    assert "status alone will not catch a dead session here" in stub

def test_html_on_a_json_stub_names_unexpected_response_error(probed) -> None:
    assert "is not a login form, so request_json raises UnexpectedResponseError" in " ".join(_stub(_module(probed), "print_1").split())

def test_a_public_endpoint_says_so(probed) -> None:
    assert "it is public and cannot tell a dead session" in " ".join(_stub(_module(probed), "api_catalog").split())

def test_an_unprobed_run_renders_as_before(tmp_path) -> None:
    run_dir = write_probe_run(tmp_path)
    assert "gp observe probe" not in _module(run_dir)

def test_the_module_still_compiles(probed) -> None:
    compile(_module(probed), "plugin.py", "exec")

def test_plugin_new_prints_the_summary_lines(probed, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", probed.parent.parent)
    result = runner.invoke(_build_app(), ["plugin", "new", "myshop", "--from-run", "myshop", "--dir", str(tmp_path / "out")])
    assert result.exit_code == 0, result.output
    read = read_logged_out(probed)
    for line in describe_summary(summarize(read)):
        assert line in " ".join(result.output.split())

def test_the_digest_and_plugin_new_print_the_same_suggested_check(probed, monkeypatch, tmp_path) -> None:
    # gp observe digest myshop and gp plugin new myshop --from-run myshop on the same run
    def suggested(text: str) -> str:
        return next(line.strip(" -") for line in text.splitlines() if "suggested session check:" in line)
    assert suggested(digest_output) == suggested(plugin_new_output)

def test_an_ignored_file_prints_its_line_on_plugin_new(probed, monkeypatch, tmp_path) -> None:
    (probed / "logged-out.json").write_text("{cut")
    result = runner.invoke(...)  # as above
    assert result.exit_code == 0 and "logged-out.json is ignored" in result.output
```

The CLI tests build the app as `tests/unit/test_scaffold_cli.py::_build_app` does, and the digest output comes from `gp observe digest myshop` on the same patched `OBSERVE_BASE_DIR`. The command names in `_stub` calls are the ones `_default_name` derives; confirm each in the rendered module on the first run and correct the literal in the test, never the renderer.

Run: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4 tests/unit/test_scaffold_logged_out.py`
Expected: FAIL on the first assertion of each stub test (no logged-out note rendered) and with `TypeError` on `logged_out_summary` once the CLI tests reach it.

- [ ] **Step 2: Implement `_logged_out_notes`, the `ScaffoldSpec` field, and the two `scaffold_commands.py` changes.** Notes go through `wrapped_comment_lines(note, indent=len(L2))` like the other notes.

- [ ] **Step 3: Run the tests and the scaffold suites, the frozen goldens included.**

Run: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4 tests/unit/test_scaffold_logged_out.py tests/unit/test_scaffold_cli.py tests/unit/test_digest_families.py tests/unit/test_digest_inline_json.py tests/unit/test_digest_after_read.py`
Expected: PASS. Then the full gate.

- [ ] **Step 4: Commit.**

```bash
git add src/graftpunk/devtools/scaffold/render.py src/graftpunk/cli/scaffold_commands.py tests/unit/test_scaffold_logged_out.py
git commit -m "feat: gp plugin new writes a session check note per stub from the logged-out probe"
```

### Task 11: Documentation, the skill, and the changelog

**Files:**
- Modify: `docs/PLUGIN_DEVELOPMENT.md` (a new `### What a dead session answers` under `## Understand`, after the digest options paragraph at about line 400)
- Modify: `skills/graft/SKILL.md` (step 3 and the sentence near line 103 naming where live calls happen), `skills/graft/references/commands.md` (Understand block; the allow-rules paragraph), `skills/graft/references/digest.md`
- Modify: `.claude-plugin/plugin.json` (`0.1.41` to `0.1.42`)
- Modify: `README.md` (the "From recording to plugin" block, lines 432-436)
- Modify: `CHANGELOG.md` (a `## [Unreleased]` section above `## [1.46.1]`)

**Interfaces:**
- Consumes: the command and options of Task 8; the digest lines of Task 9; the stub notes of Task 10.

- [ ] **Step 1: The guide.** The new subsection says, in this order: what the probe is for (learn what each endpoint answers without the session, so the plugin can tell a dead session from a live one); `gp observe probe myshop --dry-run` and `gp observe probe myshop`, with `--yes`, `--limit N` (50), `--delay SECONDS` (1.0); which requests are replayed (one per row; GETs off auth, logout and the listed action segments; GraphQL POSTs whose every operation is a query; never a mutation, a mixed document, or a persisted query) and which are skipped and why; what is sent and never sent (the header lists from Global Constraints, no cookies, proxies from the environment, `~/.netrc` unused); the stops (429, or 503 with `Retry-After`); that `logged-out.json` (0600) holds no body, header value, or query value; the eleven classes in one list with a clause each; the digest's `- logged out:` line and `## Logged out` section; the stub notes; and the bot-defence warning. Every `gp` invocation in it must pass `tests/unit/test_plugin_development_guide.py`.
- [ ] **Step 2: The skill.** `commands.md` Understand block gains `gp observe probe <session> <run> --dry-run` and `gp observe probe <session> <run> --yes`; the allow-rules paragraph says none is offered for `gp observe probe`, which sends requests to the live site. `SKILL.md` step 3, after the user's keep, rename, or drop answer and before scaffolding: ask in words "send each kept endpoint's recorded request to the live site once, without cookies or credentials, to learn what a dead session gets?"; on yes run the probe's `--dry-run` line, show its plan, then run its `--yes` line; on no, skip it. The sentence near line 103 that names the kick-the-tires step as the only live calls names the probe too. `digest.md`: after the paragraph ending "`content_type` and `shape`, never the call.", one sentence: after a probe, a row's `logged_out.class` says what a dead session gets; show it as a column. `digest.md` is at 119 lines and `test_each_reference_stays_short` requires fewer than 120: tighten the worked example's three-line opening sentence to two lines without changing what it says, so `wc -l skills/graft/references/digest.md` prints at most 119. No run of eight words may repeat the guide (`test_no_run_of_eight_words_from_the_guide`). Bump `.claude-plugin/plugin.json` to `0.1.42`.
- [ ] **Step 3: README and changelog.** README: add `gp observe probe mybank --dry-run` between the digest and `gp plugin new` lines of the block, and one sentence before the block naming it as the optional logged-out check that feeds the stubs' session-check notes, linking the guide's new subsection. CHANGELOG `## [Unreleased]`: an `### Added` list with `gp observe probe` (what it sends, the file, the defaults), the digest's `logged out` line and section and the `logged_out` field of `--json` and `--endpoints-json` (optional, within the current endpoints schema), the stub notes, `graftpunk.har.logged_out`, `graftpunk.har.probe_select`, `graftpunk.har.model`, `is_auth_path`, `is_logout_path`, `in_primary_scope`, `document_definitions`, and the `logged_out` contracts surface; and an `### Upgrading` note that a run without `logged-out.json` digests and scaffolds byte for byte as before, and that the graft skill is 0.1.42.
- [ ] **Step 4: Run the documentation suites, the skill version check, and the dash check.**

Run: `NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -p 3.12 --all-extras pytest -q -rf -n 4 tests/unit/test_plugin_development_guide.py tests/unit/test_guide_harness.py tests/unit/test_graft_references.py tests/unit/test_graft_consent.py tests/unit/test_skill_version_script.py`
Expected: PASS.

Run: `scripts/check-skill-version.sh main`
Expected: exit 0, naming the bump to 0.1.42.

Run: `grep -nP '[\x{2013}\x{2014}]' docs/PLUGIN_DEVELOPMENT.md README.md CHANGELOG.md skills/graft/SKILL.md skills/graft/references/*.md docs/superpowers/plans/2026-11-27-logged-out-probe.md`
Expected: no output. Then the full gate.

- [ ] **Step 5: Commit.**

```bash
git add docs/PLUGIN_DEVELOPMENT.md skills/graft/SKILL.md skills/graft/references/commands.md skills/graft/references/digest.md .claude-plugin/plugin.json README.md CHANGELOG.md
git commit -m "docs: gp observe probe in the guide, the graft skill, the README and the changelog"
```
