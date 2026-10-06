---
type: plan
validated:
  sha: 3aecda67831e34d92a88266b76f65d382b16e7b2
  date: 2026-10-06T15:34:57Z
  reviewers: [fact-check, solid-hygiene]
  findings:
    critical: 0
    important: 0
    medium: 0
    low: 3
    nitpick: 1
  net_negative_raised: 0
  net_negative_addressed: 0
  net_negative_remaining: 0
---

# Per-Endpoint Hosts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A command `gp plugin new` or `gp plugin add-command` generates calls the host its endpoint was recorded on, the writers say when that host is not `base_url`'s, and `gp observe digest --endpoints-json` carries each endpoint's host (#214, #225).

**Architecture:** One function, `request_target(endpoint_host, base_url)` in `src/graftpunk/devtools/scaffold/render.py`, decides whether a stub requests a path (joined to `base_url` by `SiteRequests`) or an absolute URL on the endpoint's host, and returns that decision as a `RequestTarget` record: the origin to prefix (or `None`), the endpoint's host, and `base_url`'s host (or `None`). One function, `_read_base_url`, is the only place `base_url` is parsed, and one predicate, `_on_base_host`, answers "is this `base_url`'s host" for both `request_target` and the login page's `url`, so the two cannot disagree. One helper, `_target_for`, is the only caller of `request_target` for a planned command, so `render_command` (`add-command`) and `gp plugin new`'s stubs and lines get their target from one place. The stub renderer prefixes `RequestTarget.origin` onto the templated path, and `OtherHostCommand`, a data-only record of a command that calls another host, is a projection of the same `RequestTarget`. Both writers get the rule through the one renderer: `gp plugin new` passes the spec's `base_url`, and `gp plugin add-command` passes the target plugin's `PluginView.base_url`. One CLI helper, `print_other_host` in `src/graftpunk/cli/scaffold_shared.py`, words the line both writers print. `gp plugin new` refuses a `--url` with no `http`/`https` scheme or no host, so only `add-command` meets a plugin with no readable `base_url`. The projection gains `"host"` per endpoint, additive within schema `endpoints` 1.

**Tech Stack:** Python 3.11+, Typer, `urllib.parse.urlsplit`, `graftpunk.har.paths.normal_host`, pytest, ruff (run on generated trees through `sys.executable -m ruff`), ty 0.0.75.

**Spec:** `docs/superpowers/specs/2026-10-06-endpoint-hosts-design.md`. Read it alongside this plan; it is the authority where the two disagree. Its "Amended 2026-10-06" sentence records where this plan departs from its first text (where `request_target` lives, what it takes and returns, the `--url` refusal, and where the line is worded).

**Implementation notes (decisions the spec leaves open, for review):**
- `request_target` lives in `render.py`, not `policy.py`. The rule for `policy.py` (stated in `docs/superpowers/plans/2026-09-22-graft-package-project-tools.md`, Global Constraints) is that a computation with its own tests gets a module other than `policy.py`, and `render.py` already holds the one other host comparison the renderer makes, `_login_page_url` (`src/graftpunk/devtools/scaffold/render.py:584` (`def _login_page_url`)). Task 1 moves that comparison into `_on_base_host`, which both functions call.
- Its signature is `request_target(endpoint_host: str, base_url: str | None) -> RequestTarget`, wider than the spec's first text (`base_url: str`, returning `str | None`), because `PluginView.base_url` is `str | None` (`src/graftpunk/devtools/plugin_project.py:272` (`base_url: str | None`)) and because the stub and the other-host record must read one result rather than recompute it. `base_host(base_url)` is the host-less rule: `None` when `base_url` is `None`, empty, or not an `http`/`https` URL with a host (a bare `myshop.example`), since no path resolves against it; `request_target` then gives every endpoint an `https://` origin.
- `gp plugin new` refuses a `--url` for which `base_host` is `None`, with `--url must be an http:// or https:// URL with a host, got '<value>'.` and exit 1, before reading the recording. It refuses rather than normalising: a missing scheme could mean `http` or `https`, and a guessed one written into `base_url` is a guess the developer then has to find, while a refusal costs one retype. Every `gp plugin new` that has endpoints therefore has a `base_url` with a host, and `base_host` is `None` only when `add-command`'s project reader finds no string literal for `base_url`, or a literal that is not an `http`/`https` URL with a host.
- The information line names the command by its registered CLI name (`PlannedCommand.registered_name`), the name `Added <name> to ...` already prints. With a base host it is the spec's `<name> calls <host>, not <base host>; its request is an absolute URL`. The spec gives no wording for the no-base-host case, which only `add-command` reaches; this plan uses `<name> calls <host>; the plugin sets no base_url gp can read as a URL, so its request is an absolute URL`.
- `gp plugin new` prints the lines after the written-files listing and before `Next:`; `gp plugin add-command` prints its line right after `Added ...`. Neither is a `Next:` step.
- Under this change, 34 existing tests fail, all in `tests/unit/test_scaffold_render.py` (measured on a scratch copy of this tree with the Task 1 and Task 2 code applied to the unmodified test module): 31 render endpoints recorded on `api.myshop.example.com` against `base_url="https://myshop.example.com"`, so their stubs turn absolute and their path assertions fail; `test_maximal_name_deep_paths_and_wide_parameter_names_project` renders endpoints that are not on its own `base_url`'s host; and the two `TestExplicitSelection` tests call `render_command` without `base_url`. Task 2 Step 1 moves those fixtures' endpoints onto the host their `base_url` names, so each test keeps testing what it tested, and Task 2 adds new tests for the other-host case.
- The guide sentence on `--all-hosts` gets the same correction as the help text.
- Out of scope: the graft skill changes in the spec's "The skill, after the release" section (they ship after the release, in the skill), and both "Known limits" items.

## Global Constraints

- Placeholders only, in code, tests, docs, and commit messages: `myshop`, `myshop.example`, `api.myshop.example`, `alice@example.com`, and `example.com` hosts. Never a real site, vendor, account, `op://` path, or a named secret manager.
- No em dashes or en dashes, and no spaced double hyphen standing in for one, in any file, docstring, comment, commit message, or plan text.
- Every commit subject is in the repository's `type(scope): subject` form.
- No Claude attribution in commits: no `Co-Authored-By`, no "Generated with" footer, no session trailer.
- Tests assert behaviour, never that a mock was called. The recording session in Task 2 records the URL the request was sent to; the assertion is on that URL.
- The placement rule in `src/graftpunk/devtools/__init__.py:1-7` (`CLI-only developer tooling`): `base_host`, `request_target`, `RequestTarget`, and `OtherHostCommand` are generator tooling and stay under `graftpunk.devtools`; nothing in `graftpunk.testing` or `graftpunk.plugins` changes.
- No test imports another module's private names (`tests/unit/test_scaffold_pysrc.py:140` (`def test_no_test_imports_another_modules_private_names`)): `test_scaffold_render.py` may import `render.py`'s private names (its own subject, here `_on_base_host`), and every other test module uses `request_target`, `RequestTarget`, `OtherHostCommand`, `other_host_commands`, `make_context`, and `FixtureSession` by their public names. No test module imports from another `test_*.py` module.
- `src/graftpunk/cli/scaffold_commands.py` and `src/graftpunk/cli/scaffold_project_commands.py` stay argument handling and printing only. The decision lives in `render.py`, and `OtherHostCommand` carries data only. The wording of the line lives in one helper, `print_other_host` in `src/graftpunk/cli/scaffold_shared.py`, which both modules import, as `_print_floor` in `scaffold_project_commands.py` words its line from `pyproject_edit`'s data-only floor records.
- No new filesystem access in `src/`: the one-probe rule that `tests/unit/test_graftpunk_testing.py:850` (`def test_only_stat_kind_calls_exists_is_dir_is_file_is_symlink_lstat_or_stat`) guards in `graftpunk.testing` is untouched, and `policy.py` keeps importing nothing that touches the filesystem (`tests/unit/test_scaffold_policy.py:56` (`def test_policy_imports_nothing_that_touches_the_filesystem`)).
- Within the package, `graftpunk/contracts.py` is the only module that declares the current schema number (`tests/unit/test_contracts.py:120` (`def test_contracts_is_the_only_module_that_declares_a_schema_number`)); `ENDPOINTS_SCHEMA` stays 1.
- A single test runs as `NO_COLOR=1 FORCE_COLOR= uv run --all-extras pytest tests/unit/<file>.py::<test> -q`. The venv must be Python 3.12 (`uv sync -p 3.12 --all-extras` once if `.venv` is 3.14: a 3.14 venv fails the nodriver tests on import, unrelated to this change).
- The full gate, green at the end of every task and run in full by the last task: `NO_COLOR=1 FORCE_COLOR= uv run --all-extras pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`

> **Design note (2026-10-06):** The constraint on the two CLI modules used to say the line's wording lived in `render.py` (as an `OtherHostCommand.notice` property). Wording now lives in `graftpunk.cli`, in one helper both writers import, and the devtools record is data only, matching how `_print_floor` words `RaisedFloor` and its siblings.

## Review Focus

1. A template already past the generated width with an origin in front of it (a long API host): the wrapped literal must rejoin to the whole URL and the generated tree must pass its own `ruff check` and `ruff format --check`. Pinned in Task 2 (`test_a_long_absolute_url_wraps_and_stays_ruff_clean`).
2. `gp plugin new --from-run myshop --url https://api.myshop.example`, the documented override: the API endpoints then request paths and the page endpoint requests `https://myshop.example/...`, with a line saying so. Pinned in Task 3 (`test_url_on_the_api_host_turns_the_page_command_absolute`).
3. A `base_url` spelled `https://MyShop.example:443`: a person expects every stub on `myshop.example` to stay a path, not to turn absolute over case or a default port, and the login page's `url` to agree. Pinned in Task 1 (`test_a_default_port_or_a_case_difference_is_the_same_host`) and Task 2 (`test_a_login_page_and_an_endpoint_on_one_host_agree`).
4. A `base_url` no path resolves against. In `gp plugin new` it is refused (`--url myshop.example`); in `gp plugin add-command` (a hand-written `base_url` built from an expression, or a literal with no scheme) every stub must request an absolute `https://` URL rather than a path that goes nowhere. Pinned in Task 1 (`test_no_base_url_host_is_an_https_origin_for_every_endpoint`) and Task 3 (`test_a_url_without_an_http_scheme_and_a_host_is_refused`, `test_a_plugin_with_no_readable_base_url_gets_an_absolute_url`).
5. An API host on a non-default port (port 8443 on `api.myshop.example`): the origin must keep the port, and a `base_url` on a non-default port must not match the same name without it. Pinned in Task 1 (`test_a_non_default_port_is_kept`).

## File Structure

| File | Responsibility |
| --- | --- |
| `src/graftpunk/devtools/scaffold/render.py` (modify, Tasks 1, 2) | `_BaseUrl`, `_read_base_url`, `base_host`, `_on_base_host`, `RequestTarget`, `request_target`, and `_login_page_url` calling `_on_base_host` (Task 1); `OtherHostCommand`, `_other_host`, `_target_for`, `other_host_commands`, `RenderedCommand.other_host`, the `target` parameter on `_render_command_stub`, the `base_url` parameter on `render_command`, and the login `url` `GP-FILL` wording (Task 2). |
| `src/graftpunk/devtools/scaffold/insert.py` (modify, Task 2) | Passes `plugin.base_url` to `render_command`; `AddedCommand.other_host`. |
| `src/graftpunk/cli/scaffold_shared.py` (modify, Task 3) | `print_other_host`, the one wording of the line. |
| `src/graftpunk/cli/scaffold_commands.py` (modify, Task 3) | `gp plugin new` refuses a `--url` with no host, and prints one line per other-host command. |
| `src/graftpunk/cli/scaffold_project_commands.py` (modify, Task 3) | `gp plugin add-command` prints the line for `added.other_host` when set. |
| `src/graftpunk/har/report.py` (modify, Task 4) | `"host"` on each projected endpoint. |
| `src/graftpunk/cli/observe_commands.py` (modify, Task 4) | The `--all-hosts` help text. |
| `tests/unit/test_scaffold_render.py` (modify, Tasks 1, 2) | `TestRequestTarget`; fixtures moved onto their `base_url`'s host; `TestAnEndpointOnAnotherHost`; `TestTheLoginPageAndTheRequestsShareOneHostRule`; the long-URL ruff test; the login `GP-FILL` wording. |
| `tests/unit/test_scaffold_cli.py` (modify, Tasks 2, 3) | `TestATwoHostRecording`: #214's acceptance end to end, then the `gp plugin new` information lines; `TestUrlNeedsAHost`. |
| `tests/unit/test_plugin_project_cli.py` (modify, Task 3) | `TestAddCommandTargetsTheEndpointsHost`. |
| `tests/unit/test_har_report.py` (modify, Task 4) | `"host"` in `_ENDPOINT_V1`; a two-host projection test. |
| `tests/unit/test_observe_commands.py` (modify, Task 4) | The `--all-hosts` help text, pinned. |
| `docs/PLUGIN_DEVELOPMENT.md` (modify, Task 5) | `--all-hosts` and `--endpoints-json` sentence; `--url` bullet; "What came from the digest"; a two-host paragraph; the add-command section. |
| `CHANGELOG.md` (modify, Task 5) | `## [Unreleased]` above `## [1.17.0] - 2026-10-06`. |

---

### Task 1: `request_target`, the one rule, and the one host comparison

**Files:**
- Modify: `src/graftpunk/devtools/scaffold/render.py:69-80` (`__all__`), `:584-600` (`def _login_page_url`), and insert above `src/graftpunk/devtools/scaffold/render.py:924` (`def _render_command_stub(command`)
- Test: `tests/unit/test_scaffold_render.py:26-39` (`from graftpunk.devtools.scaffold.render import (`), and a new class at the end of the module

**Interfaces:**
- Consumes: `normal_host(scheme: str, netloc: str) -> str` (`src/graftpunk/har/paths.py:71` (`def normal_host`)), already imported by `render.py`; `urlsplit`, already imported.
- Produces, all exported in `__all__` except `_BaseUrl`, `_read_base_url`, and `_on_base_host`:
  - `render._BaseUrl`, `@dataclass(frozen=True)` with `scheme: str` and `host: str` (spelled by `normal_host`).
  - `render._read_base_url(base_url: str | None) -> _BaseUrl | None`: one `urlsplit` of `base_url`, or `None` when its scheme is not `http`/`https` or its netloc is empty. The only place `base_url` is parsed.
  - `render.base_host(base_url: str | None) -> str | None`: `_read_base_url(base_url).host`, or `None`.
  - `render._on_base_host(scheme: str, netloc: str, base: _BaseUrl | None) -> bool`: `base is not None and normal_host(scheme, netloc) == base.host`.
  - `render.RequestTarget`, `@dataclass(frozen=True)` with `origin: str | None`, `host: str`, `base_host: str | None`.
  - `render.request_target(endpoint_host: str, base_url: str | None) -> RequestTarget`. It reads `base_url` once (`_read_base_url`). The scheme is `base_url`'s when it has a host, else `"https"`; `host` is `normal_host(scheme, endpoint_host)`; `origin` is `None` when `_on_base_host(scheme, endpoint_host, base)`, else `f"{scheme}://{host}"`.

- [ ] **Step 1: Write the failing test**

In `tests/unit/test_scaffold_render.py`, add `RequestTarget,` after `PLUGIN_NAME_RE,`, `base_host,` after `_param_identifier,`, and `request_target,` after `render_command,` in the `from graftpunk.devtools.scaffold.render import (...)` block, which then reads:

```python
from graftpunk.devtools.scaffold.render import (
    _MAX_PARAM_NAME,
    _MAX_PLUGIN_NAME,
    _MAX_SCAFFOLD_ENDPOINTS,
    PLUGIN_NAME_RE,
    RequestTarget,
    ScaffoldSpec,
    _command_name,
    _param_identifier,
    base_host,
    class_name_for,
    fixture_paths,
    render,
    render_command,
    request_target,
    validate_plugin_name,
)
```

Append to the end of the module:

```python
class TestRequestTarget:
    """request_target: the one rule for where a generated command's request goes."""

    def test_the_base_urls_own_host_is_a_path(self) -> None:
        assert request_target("myshop.example", "https://myshop.example") == RequestTarget(
            origin=None, host="myshop.example", base_host="myshop.example"
        )

    def test_a_default_port_or_a_case_difference_is_the_same_host(self) -> None:
        assert request_target("myshop.example", "https://MyShop.example:443").origin is None
        assert request_target("myshop.example:443", "https://myshop.example").origin is None
        assert request_target("myshop.example:80", "http://myshop.example").origin is None

    def test_another_host_is_its_origin_under_the_base_urls_scheme(self) -> None:
        assert request_target("api.myshop.example", "https://myshop.example") == RequestTarget(
            origin="https://api.myshop.example",
            host="api.myshop.example",
            base_host="myshop.example",
        )
        assert (
            request_target("api.myshop.example", "http://myshop.example").origin
            == "http://api.myshop.example"
        )

    def test_a_non_default_port_is_kept(self) -> None:
        assert (
            request_target("api.myshop.example:8443", "https://myshop.example").origin
            == "https://api.myshop.example:8443"
        )
        assert (
            request_target("myshop.example", "https://myshop.example:8443").origin
            == "https://myshop.example"
        )

    def test_a_base_url_with_a_path_compares_only_its_host(self) -> None:
        assert request_target("myshop.example", "https://myshop.example/shop/").origin is None
        assert (
            request_target("api.myshop.example", "https://myshop.example/shop/").origin
            == "https://api.myshop.example"
        )

    @pytest.mark.parametrize("base_url", [None, "", "myshop.example", "ftp://myshop.example"])
    def test_no_base_url_host_is_an_https_origin_for_every_endpoint(
        self, base_url: str | None
    ) -> None:
        assert base_host(base_url) is None
        assert request_target("myshop.example", base_url) == RequestTarget(
            origin="https://myshop.example", host="myshop.example", base_host=None
        )
        assert request_target("api.myshop.example", base_url).origin == "https://api.myshop.example"

    def test_base_host_is_spelled_by_normal_host(self) -> None:
        assert base_host("https://MyShop.example:443/shop/") == "myshop.example"
        assert base_host("http://myshop.example:8080") == "myshop.example:8080"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `NO_COLOR=1 FORCE_COLOR= uv run --all-extras pytest tests/unit/test_scaffold_render.py -q`
Expected: collection ERROR, `ImportError: cannot import name 'RequestTarget' from 'graftpunk.devtools.scaffold.render'`.

- [ ] **Step 3: Write the implementation**

In `src/graftpunk/devtools/scaffold/render.py`, add `"RequestTarget",` to `__all__` after `"RenderedCommand",`, `"base_host",` after `"ScaffoldSpec",`, and `"request_target",` after `"render_command",`.

In `_login_page_url`, replace the end of its docstring and its body's comparison:

```python
    host (compared through ``normal_host``, so case and a default port do not
    matter) and absolute otherwise. None when there is no such page to name (the form
    came from a saved page source) or when its path holds an id or a token
    (``templates_a_segment``)."""
    page = form.opened_from or form.source
    parts = urlsplit(page)
    if parts.scheme not in ("http", "https") or templates_a_segment(page):
        return None
    base = urlsplit(base_url)
    if normal_host(parts.scheme, parts.netloc) == normal_host(base.scheme, base.netloc):
        return parts.path or "/"
    return page
```

with:

```python
    host (``_on_base_host``, the comparison ``request_target`` makes for a stub's
    request) and absolute otherwise. None when there is no such page to name (the form
    came from a saved page source) or when its path holds an id or a token
    (``templates_a_segment``)."""
    page = form.opened_from or form.source
    parts = urlsplit(page)
    if parts.scheme not in ("http", "https") or templates_a_segment(page):
        return None
    if _on_base_host(parts.scheme, parts.netloc, _read_base_url(base_url)):
        return parts.path or "/"
    return page
```

The page keeps its own scheme for the comparison, as before; an endpoint has none recorded and is read under `base_url`'s.

Insert directly above `def _render_command_stub(` (line 924):

```python
@dataclass(frozen=True)
class _BaseUrl:
    """``base_url`` read once: its scheme, and its host spelled by ``normal_host``
    (lower case, no default port)."""

    scheme: str
    host: str


def _read_base_url(base_url: str | None) -> _BaseUrl | None:
    """*base_url*'s scheme and host from one ``urlsplit``, or ``None`` when it is not
    an ``http`` or ``https`` URL with a host: no path resolves against it (``None``,
    empty, or a bare ``myshop.example``). The one place *base_url* is parsed."""
    base = urlsplit(base_url or "")
    if base.scheme not in ("http", "https") or not base.netloc:
        return None
    return _BaseUrl(scheme=base.scheme, host=normal_host(base.scheme, base.netloc))


def base_host(base_url: str | None) -> str | None:
    """*base_url*'s host, spelled by ``normal_host``, or ``None`` when it has none
    (``_read_base_url``)."""
    base = _read_base_url(base_url)
    return None if base is None else base.host


def _on_base_host(scheme: str, netloc: str, base: _BaseUrl | None) -> bool:
    """Whether *netloc*, read under *scheme*, is *base*'s host: both spelled by
    ``normal_host``, and never when ``base_url`` has no host (*base* is ``None``). The
    one comparison behind a stub's request (``request_target``) and the login page's
    ``url`` (``_login_page_url``), so the two cannot disagree."""
    return base is not None and normal_host(scheme, netloc) == base.host


@dataclass(frozen=True)
class RequestTarget:
    """Where a generated command's request goes. ``origin`` is what the stub puts in
    front of its templated path, or ``None`` when the stub requests the path and
    ``SiteRequests`` joins it to ``base_url``; ``host`` is the endpoint's host and
    ``base_host`` is ``base_url``'s (``None`` when it has none), both spelled by
    ``normal_host``."""

    origin: str | None
    host: str
    base_host: str | None


def request_target(endpoint_host: str, base_url: str | None) -> RequestTarget:
    """The one rule for where a generated command's request goes: a path when
    *endpoint_host* is *base_url*'s host (``_on_base_host``), and otherwise an origin
    of *base_url*'s scheme (the digest records none) and the endpoint's host. When
    *base_url* has no host, no path resolves, so every endpoint gets an ``https://``
    origin."""
    base = _read_base_url(base_url)
    scheme = "https" if base is None else base.scheme
    host = normal_host(scheme, endpoint_host)
    origin = None if _on_base_host(scheme, endpoint_host, base) else f"{scheme}://{host}"
    return RequestTarget(origin=origin, host=host, base_host=None if base is None else base.host)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run --all-extras pytest tests/unit/test_scaffold_render.py -q`
Expected: all pass (`TestRequestTarget` contributes 10 passing tests; the parametrized one runs four times). The existing `TestGeneratedLoginHoldsNoAccountValue` tests pass unchanged, since `_on_base_host` makes the comparison `_login_page_url` made.

Run: `uvx ruff check src tests && uvx ruff format --check src tests && uvx ty@0.0.75 check src/`
Expected: `All checks passed!`, every file already formatted, and `All checks passed!`.

- [ ] **Step 5: Commit**

```bash
git add src/graftpunk/devtools/scaffold/render.py tests/unit/test_scaffold_render.py
git commit -m "feat(scaffold): request_target, the one rule for where a generated request goes"
```

> **Design note (2026-10-06):** `request_target` returned `str | None` and `_login_page_url` compared hosts on its own. It now returns `RequestTarget` (origin, endpoint host, base host), so Task 2's stub prefix and other-host record read one result, and one predicate, `_on_base_host`, owns the comparison and the host-less rule (`base_host`) for both the requests and the login page's `url`. Task 2 pins the shared behaviour (`TestTheLoginPageAndTheRequestsShareOneHostRule`).

> **Design note (2026-10-06, round 2):** `request_target` parsed `base_url` three times (through `base_host`, again for its scheme, and again inside `_on_base_host`). `_read_base_url` now parses it once and returns its scheme and host together as a private `_BaseUrl`, or `None`; `base_host` and `request_target` each call it once, and `_on_base_host` takes the `_BaseUrl` rather than the string, so it parses nothing. `_login_page_url` reads `base_url` through the same function.

---

### Task 2: A stub for an endpoint on another host requests its absolute URL

**Files:**
- Modify: `src/graftpunk/devtools/scaffold/render.py:69-80` (`__all__`), `:458` (`f"{GP_FILL_MARKER}: url, the path of the login page. "`), `:924-932` (`def _render_command_stub(command`), `:1042-1051` (`class RenderedCommand`), `:1075-1098` (`def render_command`), `:1129-1140` (`def _render_command_stubs`), and below `:1302` (`def fixture_paths`)
- Modify: `src/graftpunk/devtools/scaffold/insert.py:44` (`from graftpunk.devtools.scaffold.render import render_command`), `:61-73` (`class AddedCommand`), `:236` (`rendered = render_command(command, d)`), `:255-260` (`return AddedCommand(`)
- Test: `tests/unit/test_scaffold_render.py`, `tests/unit/test_scaffold_cli.py`

**Interfaces:**
- Consumes: `request_target`, `RequestTarget`, `_read_base_url`, `_on_base_host` (Task 1); `PlannedCommand.registered_name` and `PlannedCommand.endpoint` (`src/graftpunk/devtools/scaffold/selection.py:75` (`class PlannedCommand`)); `Endpoint.host` (`src/graftpunk/har/digest.py:252` (`class Endpoint:`)); `_planned(spec)` (`src/graftpunk/devtools/scaffold/render.py:1064` (`def _planned(spec: ScaffoldSpec)`)); `PluginView.base_url` (`src/graftpunk/devtools/plugin_project.py:272` (`base_url: str | None`)).
- Produces:
  - `render.OtherHostCommand`, `@dataclass(frozen=True)` with `name: str`, `host: str`, `base_host: str | None`, and no behaviour. Exported.
  - `render._other_host(name: str, target: RequestTarget) -> OtherHostCommand | None`: `None` when `target.origin is None`, else `OtherHostCommand(name, target.host, target.base_host)`.
  - `render._target_for(command: PlannedCommand, base_url: str | None) -> RequestTarget`: `request_target(command.endpoint.host, base_url)`. The one answer to "which target does this planned command get under this `base_url`", called by `render_command`, `_render_command_stubs`, and `other_host_commands`; none of them calls `request_target` directly.
  - `render.other_host_commands(spec: ScaffoldSpec) -> list[OtherHostCommand]`, in render order. Exported.
  - `render.render_command(command: PlannedCommand, d: RunDigest, base_url: str | None) -> RenderedCommand` (the third parameter is new and required). It calls `_target_for` once and hands that result to both the stub and `_other_host`.
  - `RenderedCommand.other_host: OtherHostCommand | None = None`.
  - `insert.AddedCommand.other_host: OtherHostCommand | None = None`.

- [ ] **Step 1: Move the existing render fixtures onto their `base_url`'s host**

These tests render against `base_url="https://myshop.example.com"` and assert path requests; their endpoints say `api.myshop.example.com` only because nothing read the host until now. Run:

```bash
perl -pi -e 's/host="api\.myshop\.example\.com"/host="myshop.example.com"/g; s/hosts=\{"api\.myshop\.example\.com": 3\}/hosts={"myshop.example.com": 3}/' tests/unit/test_scaffold_render.py
grep -c 'host="api.myshop.example.com"' tests/unit/test_scaffold_render.py
```

Expected: the `grep -c` prints `0` (the first pattern also rewrites `_digest`'s `primary_host="api.myshop.example.com"`, which is intended).

In `test_maximal_name_deep_paths_and_wide_parameter_names_project` (`tests/unit/test_scaffold_render.py:2833` (`def test_maximal_name_deep_paths_and_wide_parameter_names_project`)), the two endpoints must sit on that test's own 120-character `base_url` host, or they turn absolute and the test's `f"{deep_template}",` assertion fails. Move the `base_url` lines above `deep = Endpoint(` and use its host. Replace:

```python
        deep = Endpoint(
            host="myshop.example.com",
            template=deep_template,
```

with:

```python
        base_url = "https://" + "x" * 100 + ".example.com"
        assert len(base_url) == 120
        deep = Endpoint(
            host=base_url.removeprefix("https://"),
            template=deep_template,
```

replace:

```python
        wide = Endpoint(
            host="myshop.example.com",
            template=wide_template,
```

with:

```python
        wide = Endpoint(
            host=base_url.removeprefix("https://"),
            template=wide_template,
```

and delete the two original `base_url` lines, the two lines directly above `spec = ScaffoldSpec(` in that test (the copies you just inserted above `deep = Endpoint(` stay):

```python
        base_url = "https://" + "x" * 100 + ".example.com"
        assert len(base_url) == 120
```

In `TestExplicitSelection` (lines 3495 and 3515 of the unmodified module), replace both occurrences of:

```python
        rendered = render_command(command, d)
```

with:

```python
        rendered = render_command(command, d, "https://myshop.example.com")
```

- [ ] **Step 2: Write the failing render tests**

In the `from graftpunk.devtools.scaffold.render import (...)` block of `tests/unit/test_scaffold_render.py`, add `OtherHostCommand,` after `PLUGIN_NAME_RE,`, `_on_base_host,` after `_command_name,`, `_read_base_url,` after `_param_identifier,`, and `other_host_commands,` after `fixture_paths,`, so the block reads:

```python
from graftpunk.devtools.scaffold.render import (
    _MAX_PARAM_NAME,
    _MAX_PLUGIN_NAME,
    _MAX_SCAFFOLD_ENDPOINTS,
    PLUGIN_NAME_RE,
    OtherHostCommand,
    RequestTarget,
    ScaffoldSpec,
    _command_name,
    _on_base_host,
    _param_identifier,
    _read_base_url,
    base_host,
    class_name_for,
    fixture_paths,
    other_host_commands,
    render,
    render_command,
    request_target,
    validate_plugin_name,
)
```

In `TestGeneratedLoginHoldsNoAccountValue`, directly after `test_a_page_path_holding_an_id_is_a_gp_fill` (it ends at line 725 of the unmodified module, `assert "The recorded login page path holds an account value." in comments`), add:

```python
    def test_the_url_gp_fill_names_a_full_url_for_another_hosts_page(self) -> None:
        segment = "7f3a9c2e8b1d4f60a9e2c3b4d5f6a7b8"
        code = self._plugin_code(
            self._form("/session", f"https://myshop.example.com/signin/{segment}")
        )
        comments = " ".join(
            line.strip().lstrip("#").strip()
            for line in code.splitlines()
            if line.strip().startswith("#")
        )
        assert (
            "GP-FILL: url, the path of the login page, or its full URL when it is not on "
            "base_url's host."
        ) in comments
```

In `TestRenderedTreeIsRuffClean`, directly after `test_maximal_name_deep_paths_and_wide_parameter_names_project` (before `class TestFixturesDirFollowsThePolicy:`, line 2916 of the unmodified module), add:

```python
    def test_a_long_absolute_url_wraps_and_stays_ruff_clean(self, tmp_path: Path) -> None:
        # An endpoint on another host puts its origin in front of a template that is
        # already past the width on its own: the wrapped literal must still rejoin to
        # the whole URL, and the tree must still pass its own gate.
        origin = "https://regional-api-gateway.customer-services.myshop.example.com"
        template = (
            "/api/v2/customer-accounts/{account_id}/payment-methods/{payment_method_id}"
            "/scheduled-deliveries/recurring-orders/preferences-list/default-billing-addresses"
        )
        endpoint = dataclasses.replace(
            _ORDERS_ENDPOINT,
            host=origin.removeprefix("https://"),
            template=template,
            query_params={},
            custom_headers=(),
            examples=(),
        )
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(endpoint,)),
        )
        files = render(spec)
        (url,) = [
            node.args[1]
            for node in ast.walk(ast.parse(files["src/graftpunk_myshop/plugin.py"]))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "request_json"
        ]
        assert isinstance(url, ast.JoinedStr)
        rejoined = "".join(
            part.value if isinstance(part, ast.Constant) else "{" + ast.unparse(part.value) + "}"
            for part in url.values
            if isinstance(part, (ast.Constant, ast.FormattedValue))
        )
        assert rejoined == origin + template
        tree = self._write_tree(tmp_path / "long_origin", files)
        self._assert_tree_is_clean(tree)
```

Append to the end of the module, after `TestRequestTarget`:

```python
_API_ORDERS_ENDPOINT = dataclasses.replace(_ORDERS_ENDPOINT, host="api.myshop.example.com")


class TestAnEndpointOnAnotherHost:
    """A stub for an endpoint recorded on a host other than base_url's requests an
    absolute URL; one on base_url's host requests a path."""

    @staticmethod
    def _spec(*endpoints: Endpoint, base_url: str = "https://myshop.example.com") -> ScaffoldSpec:
        return ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url=base_url,
            digest=_digest(endpoints=endpoints),
        )

    def test_the_stub_requests_the_absolute_url(self) -> None:
        plugin_code = render(self._spec(_API_ORDERS_ENDPOINT))["src/graftpunk_myshop/plugin.py"]
        assert 'f"https://api.myshop.example.com/orders/{order_id}",' in plugin_code
        assert 'f"/orders/{order_id}",' not in plugin_code

    def test_a_stub_on_the_base_host_still_requests_a_path(self) -> None:
        plugin_code = render(self._spec(_ORDERS_ENDPOINT))["src/graftpunk_myshop/plugin.py"]
        assert 'f"/orders/{order_id}",' in plugin_code
        assert "https://myshop.example.com/orders" not in plugin_code

    def test_the_endpoint_declaration_stays_a_path(self) -> None:
        plugin_code = render(self._spec(_API_ORDERS_ENDPOINT))["src/graftpunk_myshop/plugin.py"]
        assert 'endpoint="GET /orders/{order_id}",' in plugin_code

    def test_the_generated_test_is_unchanged(self) -> None:
        same = render(self._spec(_ORDERS_ENDPOINT))["tests/test_plugin.py"]
        other = render(self._spec(_API_ORDERS_ENDPOINT))["tests/test_plugin.py"]
        assert other == same

    def test_other_host_commands_names_each_one_and_both_hosts(self) -> None:
        spec = self._spec(_API_ORDERS_ENDPOINT, _SEARCH_ENDPOINT)
        assert other_host_commands(spec) == [
            OtherHostCommand(
                name="orders-by-order-id",
                host="api.myshop.example.com",
                base_host="myshop.example.com",
            )
        ]

    def test_other_host_commands_is_empty_when_every_endpoint_is_on_the_base_host(self) -> None:
        assert other_host_commands(self._spec(_ORDERS_ENDPOINT, _SEARCH_ENDPOINT)) == []

    def test_render_command_reports_the_other_host(self) -> None:
        d = _digest(endpoints=(_API_ORDERS_ENDPOINT,))
        command = plan_command(d, CommandSelection("order", "GET", "/orders/{order_id}"))
        rendered = render_command(command, d, "https://myshop.example.com")
        assert rendered.other_host == OtherHostCommand(
            name="order", host="api.myshop.example.com", base_host="myshop.example.com"
        )
        assert 'f"https://api.myshop.example.com/orders/{order_id}",' in "\n".join(rendered.lines)

    def test_render_command_without_a_base_url_requests_every_endpoint_absolutely(self) -> None:
        d = _digest(endpoints=(_ORDERS_ENDPOINT,))
        command = plan_command(d, CommandSelection("order", "GET", "/orders/{order_id}"))
        rendered = render_command(command, d, None)
        assert rendered.other_host == OtherHostCommand(
            name="order", host="myshop.example.com", base_host=None
        )
        assert 'f"https://myshop.example.com/orders/{order_id}",' in "\n".join(rendered.lines)


class TestTheLoginPageAndTheRequestsShareOneHostRule:
    """The login page's url and each stub's request come from one comparison, so a
    login page and an endpoint on the same host are both a path or both absolute,
    whatever base_url spells."""

    @pytest.mark.parametrize(
        ("base_url", "same_host"),
        [
            ("https://myshop.example.com", True),
            ("https://MyShop.example.com:443/shop/", True),
            ("https://api.myshop.example.com", False),
            ("https://myshop.example.com:8443", False),
        ],
    )
    def test_a_login_page_and_an_endpoint_on_one_host_agree(
        self, base_url: str, same_host: bool
    ) -> None:
        form = LoginForm(
            action="/session",
            method="POST",
            fields={"username": 'input[name="username"]', "password": 'input[name="password"]'},
            submit='button[type="submit"]',
            hidden=(),
            source="https://myshop.example.com/signin",
        )
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url=base_url,
            digest=_digest(endpoints=(_ORDERS_ENDPOINT,), login_forms=(form,)),
        )
        code = render(spec)["src/graftpunk_myshop/plugin.py"]
        origin = "" if same_host else "https://myshop.example.com"
        assert f'url="{origin}/signin",' in code
        assert f'f"{origin}/orders/{{order_id}}",' in code
        assert _on_base_host("https", "myshop.example.com", _read_base_url(base_url)) is same_host
```

- [ ] **Step 3: Write the failing end-to-end test (#214's acceptance)**

In `tests/unit/test_scaffold_cli.py`, replace the import block's first lines (lines 5-17) so the standard library and third-party imports read:

```python
import importlib.util
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import requests
import structlog
import typer
from structlog.testing import capture_logs
from typer.testing import CliRunner
```

and add the `graftpunk.testing` import between `from graftpunk.logging import configure_logging` and `from graftpunk.testing.sidecar import ...`:

```python
from graftpunk.logging import configure_logging
from graftpunk.testing import FixtureSession, make_context
from graftpunk.testing.sidecar import Sidecar, load_sidecar, sidecar_text
```

Insert directly above `class TestPathListingsDoNotWrapMidWord:` (line 411):

```python
class _RecordingSession(FixtureSession):
    """A FixtureSession that keeps every URL it is asked for: the URL on the wire."""

    def __init__(self, fixtures_dir: Path) -> None:
        super().__init__(fixtures_dir)
        self.urls: list[str] = []

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.urls.append(url)
        return super().request(method, url, **kwargs)


def _write_two_host_run(observe_base: Path) -> None:
    """Pages on myshop.example, JSON on api.myshop.example (#214)."""
    run_dir = observe_base / "myshop" / "run-1"
    run_dir.mkdir(parents=True)
    entries = [
        _entry(
            "GET",
            "https://myshop.example/account",
            content_type="text/html",
            body="<html><body>Account</body></html>",
        ),
        _entry("GET", "https://api.myshop.example/api/orders/1001", body='{"id": "1001"}'),
    ]
    (run_dir / "network.har").write_text(
        json.dumps({"log": {"version": "1.2", "entries": entries}})
    )


def _write_fixture(fixtures_dir: Path, name: str, body: str, content_type: str) -> None:
    (fixtures_dir / name).write_text(body)
    (fixtures_dir / f"{name}.meta.json").write_text(
        sidecar_text(Sidecar(status=200, content_type=content_type))
    )


class TestATwoHostRecording:
    """#214's acceptance: a generated command calls the host its endpoint was recorded on."""

    @staticmethod
    def _generate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        _write_two_host_run(observe_base)
        target = tmp_path / "out"
        result = runner.invoke(
            _build_app(),
            ["plugin", "new", "myshop", "--from-run", "myshop", "--dir", str(target)],
        )
        assert result.exit_code == 0, result.output
        return target, strip_ansi(result.output)

    def test_the_generated_command_calls_the_api_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target, _ = self._generate(tmp_path, monkeypatch)
        fixtures_dir = target / "tests" / "fixtures"
        _write_fixture(
            fixtures_dir, "get_api_orders_{order_id}.json", '{"id": "1001"}', "application/json"
        )
        spec = importlib.util.spec_from_file_location(
            "two_host_generated_plugin", target / "src" / "graftpunk_myshop" / "plugin.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        plugin = module.MyshopPlugin()
        assert plugin.base_url == "https://myshop.example"
        session = _RecordingSession(fixtures_dir)
        ctx = make_context(session, plugin_name="myshop", base_url=plugin.base_url)
        assert plugin.api_orders_by_order_id(ctx, order_id="1001") == {"id": "1001"}
        assert session.urls == ["https://api.myshop.example/api/orders/1001"]

    def test_the_generated_tests_pass_against_their_fixtures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target, _ = self._generate(tmp_path, monkeypatch)
        fixtures_dir = target / "tests" / "fixtures"
        _write_fixture(
            fixtures_dir, "get_api_orders_{order_id}.json", '{"id": "1001"}', "application/json"
        )
        _write_fixture(
            fixtures_dir, "get_account.html", "<html><body>Account</body></html>", "text/html"
        )
        env = {**os.environ, "PYTHONPATH": str(target / "src")}
        pytest_result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [sys.executable, "-m", "pytest", "tests", "-q"],
            cwd=target,
            capture_output=True,
            text=True,
            env=env,
        )
        assert pytest_result.returncode == 0, pytest_result.stdout + pytest_result.stderr
        summary = pytest_result.stdout.strip().splitlines()[-1]
        assert summary.startswith("3 passed"), pytest_result.stdout
```

The digest of this recording picks `myshop.example` as `primary_host` (the host that served the HTML document) and keeps both endpoints, `GET /account` on `myshop.example` and `GET /api/orders/{order_id}` on `api.myshop.example`; the default stub for the second is `api_orders_by_order_id`. All three facts were checked on a scratch copy before this plan was written.

- [ ] **Step 4: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run --all-extras pytest tests/unit/test_scaffold_render.py -q`
Expected: collection ERROR, `ImportError: cannot import name 'OtherHostCommand' from 'graftpunk.devtools.scaffold.render'`.

Run: `NO_COLOR=1 FORCE_COLOR= uv run --all-extras pytest tests/unit/test_scaffold_cli.py -q -k TwoHost`
Expected: `1 failed, 1 passed`. `test_the_generated_command_calls_the_api_host` FAILS on its last assertion, `'https://myshop.example/api/orders/1001' != 'https://api.myshop.example/api/orders/1001'` (the bug as reported); `test_the_generated_tests_pass_against_their_fixtures` PASSES already, since `FixtureSession` matches by path. It is here to show the change keeps it passing.

- [ ] **Step 5: Write the implementation in `render.py`**

Add `"OtherHostCommand",` and `"other_host_commands",` to `__all__`, which then reads:

```python
__all__ = [
    "PLUGIN_NAME_RE",
    "OtherHostCommand",
    "RenderedCommand",
    "RequestTarget",
    "ScaffoldSpec",
    "base_host",
    "class_name_for",
    "fixture_paths",
    "fixtures_root_for",
    "graftpunk_version_floor",
    "other_host_commands",
    "render",
    "render_command",
    "request_target",
    "validate_plugin_name",
]
```

At line 458, inside `_render_login_config`, replace:

```python
                f"{GP_FILL_MARKER}: url, the path of the login page. "
```

with:

```python
                f"{GP_FILL_MARKER}: url, the path of the login page, or its full URL when it is "
                "not on base_url's host. "
```

Directly below `request_target` (Task 1) and above `def _render_command_stub(`, add `OtherHostCommand`, `_other_host`, and `_target_for`:

```python
@dataclass(frozen=True)
class OtherHostCommand:
    """A generated command whose request is an absolute URL: its registered name, the
    host it calls, and ``base_url``'s host (``None`` when the plugin has no
    ``base_url`` with a host to read). A record only: ``graftpunk.cli`` words the
    line a writer prints for it."""

    name: str
    host: str
    base_host: str | None


def _other_host(name: str, target: RequestTarget) -> OtherHostCommand | None:
    """The command registered as *name*, as an ``OtherHostCommand`` when *target*
    gives its request an origin, else ``None``: a projection of the one
    ``request_target`` result its stub was rendered from."""
    if target.origin is None:
        return None
    return OtherHostCommand(name=name, host=target.host, base_host=target.base_host)


def _target_for(command: PlannedCommand, base_url: str | None) -> RequestTarget:
    """Where *command*'s request goes under *base_url*: the one call to
    ``request_target`` behind ``render_command`` (``gp plugin add-command``),
    ``_render_command_stubs``, and ``other_host_commands`` (``gp plugin new``), so
    the two writers agree by construction."""
    return request_target(command.endpoint.host, base_url)
```

Change `_render_command_stub`'s signature to take the `RequestTarget` and prefix its origin onto the templated path. Replace:

```python
def _render_command_stub(command: PlannedCommand, run_label: str) -> list[str]:
```

with:

```python
def _render_command_stub(
    command: PlannedCommand, run_label: str, target: RequestTarget
) -> list[str]:
```

and, in its body, replace:

```python
    url_text, path_params = _templated_url(endpoint.template, seen_params)
    is_json = _is_json_endpoint(endpoint)
```

with:

```python
    url_text, path_params = _templated_url(endpoint.template, seen_params)
    url_text = (target.origin or "") + url_text
    is_json = _is_json_endpoint(endpoint)
```

`url_expr_lines` (`src/graftpunk/devtools/scaffold/pysrc.py:397` (`def url_expr_lines`)) renders the result unchanged: an f-string when the path has placeholders, wrapped at `/` boundaries when past the width. `SiteRequests._resolve` (`src/graftpunk/plugins/site_requests.py:75` (`def _resolve`)) passes an absolute URL through `urljoin` unchanged, so nothing at runtime changes.

In `class RenderedCommand` (line 1042), replace its docstring and fields:

```python
    """One stub, at class-body indentation with no trailing blank line; every
    ``(module, name)`` import the stub references, which an inserter merges as it is
    handed them; and the fixture filename its test looks for, or ``None`` when
    ``_no_fixture_is_written`` says ``gp observe fixtures`` writes no fixture for
    this endpoint (its test needs a fixture of its own instead)."""

    lines: tuple[str, ...]
    imports: tuple[tuple[str, str], ...]
    fixture: str | None
```

with:

```python
    """One stub, at class-body indentation with no trailing blank line; every
    ``(module, name)`` import the stub references, which an inserter merges as it is
    handed them; the fixture filename its test looks for, or ``None`` when
    ``_no_fixture_is_written`` says ``gp observe fixtures`` writes no fixture for
    this endpoint (its test needs a fixture of its own instead); and, when its
    request is an absolute URL on a host other than ``base_url``'s, which one."""

    lines: tuple[str, ...]
    imports: tuple[tuple[str, str], ...]
    fixture: str | None
    other_host: OtherHostCommand | None = None
```

Replace `render_command`'s signature, docstring, and first line:

```python
def render_command(command: PlannedCommand, d: RunDigest) -> RenderedCommand:
    """The single-command entry point: the stub ``gp plugin new`` writes for *command*,
    which ``gp plugin add-command`` inserts on its own."""
    lines = _render_command_stub(command, _run_label(d))
```

with:

```python
def render_command(command: PlannedCommand, d: RunDigest, base_url: str | None) -> RenderedCommand:
    """The single-command entry point: the stub ``gp plugin new`` writes for *command*,
    which ``gp plugin add-command`` inserts on its own. *base_url* is the plugin's, or
    ``None`` when it has none to read; ``_target_for`` is evaluated once, and both
    the stub's request and ``other_host`` read that one result."""
    target = _target_for(command, base_url)
    lines = _render_command_stub(command, _run_label(d), target)
```

and the end of its `return`:

```python
        imports=tuple(imports),
        fixture=fixture,
    )
```

with:

```python
        imports=tuple(imports),
        fixture=fixture,
        other_host=_other_host(command.registered_name, target),
    )
```

In `_render_command_stubs` (line 1129), replace:

```python
        lines.extend(_render_command_stub(command, _run_label(spec.digest)))
```

with:

```python
        target = _target_for(command, spec.base_url)
        lines.extend(_render_command_stub(command, _run_label(spec.digest), target))
```

Directly after `fixture_paths` (it ends at the `return paths` that follows line 1302), add:

```python
def other_host_commands(spec: ScaffoldSpec) -> list[OtherHostCommand]:
    """Every stub *spec* renders whose request is an absolute URL, in render order:
    what ``gp plugin new`` reports after it writes, from the same ``_target_for``
    the render used, as ``fixture_paths`` plans from the same commands."""
    found = (
        _other_host(command.registered_name, _target_for(command, spec.base_url))
        for command in _planned(spec)
    )
    return [other for other in found if other is not None]
```

- [ ] **Step 6: Write the implementation in `insert.py`**

Replace line 44:

```python
from graftpunk.devtools.scaffold.render import render_command
```

with:

```python
from graftpunk.devtools.scaffold.render import OtherHostCommand, render_command
```

Replace the end of `class AddedCommand`'s docstring and its fields (lines 61-73):

```python
    ``render.RenderedCommand.fixture``), and what the write did to the
    project's ``graftpunk`` requirement (see
    ``project.planned_graftpunk_floor``)."""

    module: Path
    cli_name: str
    fixture: str | None
    floor: RaisedFloor | CannotRaiseFloor | DynamicDependencies | None = None
```

with:

```python
    ``render.RenderedCommand.fixture``), what the write did to the
    project's ``graftpunk`` requirement (see
    ``project.planned_graftpunk_floor``), and, when the stub requests an absolute
    URL on a host other than the plugin's ``base_url``'s, which one
    (``render.OtherHostCommand``)."""

    module: Path
    cli_name: str
    fixture: str | None
    floor: RaisedFloor | CannotRaiseFloor | DynamicDependencies | None = None
    other_host: OtherHostCommand | None = None
```

Replace line 236:

```python
    rendered = render_command(command, d)
```

with:

```python
    rendered = render_command(command, d, plugin.base_url)
```

and the `return AddedCommand(` at the end of `add_command` with:

```python
    return AddedCommand(
        module=module,
        cli_name=command.registered_name,
        fixture=fixture,
        floor=floor,
        other_host=rendered.other_host,
    )
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run --all-extras pytest tests/unit/test_scaffold_render.py tests/unit/test_scaffold_cli.py tests/unit/test_plugin_project_cli.py -q`
Expected: all pass, with no failures and no errors.

Run: `uvx ruff check src tests && uvx ruff format --check src tests && uvx ty@0.0.75 check src/`
Expected: `All checks passed!`, every file already formatted, and `All checks passed!`.

- [ ] **Step 8: Commit**

```bash
git add src/graftpunk/devtools/scaffold/render.py src/graftpunk/devtools/scaffold/insert.py tests/unit/test_scaffold_render.py tests/unit/test_scaffold_cli.py
git commit -m "fix(scaffold): a stub for an endpoint on another host requests its absolute URL"
```

> **Design note (2026-10-06):** `_other_host` used to call `request_target` again and re-parse the origin and `base_url` to build its record, and `OtherHostCommand` carried a `notice` property with the user-facing wording. `render_command` now evaluates `request_target` once and hands the one `RequestTarget` to both the stub (`target.origin`) and `_other_host`, which only projects it; `OtherHostCommand` is data only (Task 3 words the line in `graftpunk.cli`). `TestTheLoginPageAndTheRequestsShareOneHostRule` pins that the login page's `url` and a stub's request agree for each `base_url` spelling.

> **Design note (2026-10-06, round 2):** `_render_command_stubs` and `other_host_commands` each called `request_target(command.endpoint.host, spec.base_url)` inline, and `render_command` made the same call, so `gp plugin new` agreed with `add-command` by convention only. `_target_for(command, base_url)` is now the one place a planned command gets its `RequestTarget`, and all three call it.

---

### Task 3: The writers name each command that calls another host, and `gp plugin new` refuses a `--url` with no host

**Files:**
- Modify: `src/graftpunk/cli/scaffold_shared.py:1-8` (`Shared state for`), `:16` (`from graftpunk.devtools.scaffold.selection import CommandSelection`), and the end of the module after `:41` (`return tuple(selections)`)
- Modify: `src/graftpunk/cli/scaffold_commands.py:25` (`from graftpunk.cli.scaffold_shared import LOG, command_selections, console, plugin_app`), `:34-39` (`from graftpunk.devtools.scaffold.render import (`), `:169-170` (`console.print("[red]--command requires --from-run.[/red]", soft_wrap=True)`), `:256-258` (`if result.gitignore_updated:`)
- Modify: `src/graftpunk/cli/scaffold_project_commands.py:21` (`from graftpunk.cli.scaffold_shared import LOG, command_selections, console, plugin_app`), `:130` (`f"[green]Added[/green] {escape(added.cli_name)} to {escape(str(added.module))}",`)
- Test: `tests/unit/test_scaffold_cli.py` (`TestATwoHostRecording`, Task 2), `tests/unit/test_plugin_project_cli.py`

**Interfaces:**
- Consumes: `other_host_commands(spec) -> list[OtherHostCommand]`, `AddedCommand.other_host` (Task 2); `base_host(base_url) -> str | None` (Task 1).
- Produces: `scaffold_shared.print_other_host(other: OtherHostCommand) -> None`, which prints `f"{name} calls {host}, not {base_host}; its request is an absolute URL"`, or `f"{name} calls {host}; the plugin sets no base_url gp can read as a URL, so its request is an absolute URL"` when `base_host` is `None`. One line per other-host command on `gp plugin new`'s output, after the written-files listing and the `.gitignore` line and before `Next:`; one line on `gp plugin add-command`'s output, right after `Added ...`. `gp plugin new` exits 1 with `--url must be an http:// or https:// URL with a host, got '<value>'.` when `--url` is set and `base_host(url)` is `None`.

- [ ] **Step 1: Write the failing `gp plugin new` tests**

Append these methods to `TestATwoHostRecording` in `tests/unit/test_scaffold_cli.py`, and the class after them:

```python
    def test_the_output_names_the_command_on_the_other_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, output = self._generate(tmp_path, monkeypatch)
        notices = [line for line in output.splitlines() if " calls " in line]
        assert notices == [
            "api-orders-by-order-id calls api.myshop.example, not myshop.example; "
            "its request is an absolute URL"
        ]

    def test_a_one_host_recording_prints_no_host_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        _write_run(
            observe_base, "myshop", "run-1", url="https://myshop.example/api/orders", body="{}"
        )
        result = runner.invoke(
            _build_app(),
            ["plugin", "new", "myshop", "--from-run", "myshop", "--dir", str(tmp_path / "out")],
        )
        assert result.exit_code == 0, result.output
        assert " calls " not in strip_ansi(result.output)

    def test_url_on_the_api_host_turns_the_page_command_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        _write_two_host_run(observe_base)
        target = tmp_path / "out"
        result = runner.invoke(
            _build_app(),
            [
                "plugin",
                "new",
                "myshop",
                "--from-run",
                "myshop",
                "--url",
                "https://api.myshop.example",
                "--dir",
                str(target),
            ],
        )
        assert result.exit_code == 0, result.output
        plugin_code = (target / "src" / "graftpunk_myshop" / "plugin.py").read_text()
        assert '"https://myshop.example/account",' in plugin_code
        assert 'f"/api/orders/{order_id}",' in plugin_code
        assert (
            "account calls myshop.example, not api.myshop.example; its request is an absolute URL"
            in strip_ansi(result.output).splitlines()
        )


class TestUrlNeedsAHost:
    """gp plugin new refuses a --url no path resolves against, so a generated plugin's
    base_url always has a host and only add-command meets a plugin without one."""

    @pytest.mark.parametrize("url", ["myshop.example", "https://", "ftp://myshop.example"])
    def test_a_url_without_an_http_scheme_and_a_host_is_refused(
        self, tmp_path: Path, url: str
    ) -> None:
        target = tmp_path / "out"
        result = runner.invoke(
            _build_app(), ["plugin", "new", "myshop", "--url", url, "--dir", str(target)]
        )
        assert result.exit_code == 1
        output = strip_ansi(result.output)
        assert f"--url must be an http:// or https:// URL with a host, got '{url}'." in output
        assert not target.exists()
```

- [ ] **Step 2: Write the failing `gp plugin add-command` tests**

In `tests/unit/test_plugin_project_cli.py`, insert directly above `def _project_lacking_the_wiring(root: Path) -> Path:` (line 1352):

```python
def _record_api_run(project: Path) -> None:
    """A second recording, myshop-api, whose page is on myshop.example and whose JSON
    is on api.myshop.example, beside the one the recorded fixture writes."""
    run_dir = project.parent / "observe" / "myshop-api" / "run-1"
    run_dir.mkdir(parents=True)
    entries = [
        _entry(
            "GET",
            "https://myshop.example/account",
            content_type="text/html",
            body="<html><body>Account</body></html>",
        ),
        _entry("GET", "https://api.myshop.example/api/orders/1001", body='{"id": "1001"}'),
    ]
    (run_dir / "network.har").write_text(
        json.dumps({"log": {"version": "1.2", "entries": entries}})
    )


def _add_from(project: Path, session: str, command: str) -> object:
    return runner.invoke(
        app,
        [
            "plugin",
            "add-command",
            "myshop",
            "--from-run",
            session,
            "--command",
            command,
            "--dir",
            str(project),
        ],
    )


@pytest.mark.usefixtures("gp_logging")
class TestAddCommandTargetsTheEndpointsHost:
    def test_an_endpoint_on_another_host_is_requested_absolutely(self, recorded: Path) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        _record_api_run(recorded)
        result = _add_from(recorded, "myshop-api", "order=GET /api/orders/{order_id}")
        assert result.exit_code == 0, result.output
        module = (recorded / "src" / "graftpunk_myshop" / "plugin.py").read_text()
        assert 'f"https://api.myshop.example/api/orders/{order_id}",' in module
        assert 'endpoint="GET /api/orders/{order_id}",' in module
        assert (
            "order calls api.myshop.example, not myshop.example; its request is an absolute URL"
            in _plain(result.output).splitlines()
        )
        _ruff_clean(recorded)

    def test_an_endpoint_on_the_base_host_prints_no_host_line(self, recorded: Path) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        result = _add(recorded, "myshop", "order=GET /api/orders/{order_id}")
        assert result.exit_code == 0, result.output
        module = (recorded / "src" / "graftpunk_myshop" / "plugin.py").read_text()
        assert 'f"/api/orders/{order_id}",' in module
        assert " calls " not in _plain(result.output)

    @pytest.mark.parametrize(
        "base_url_line",
        ['base_url = "https://" + "myshop.example"', 'base_url = "myshop.example"'],
    )
    def test_a_plugin_with_no_readable_base_url_gets_an_absolute_url(
        self, recorded: Path, base_url_line: str
    ) -> None:
        module = _hand_written_project(
            recorded,
            module_text=_HAND_WRITTEN.replace('base_url = "https://myshop.example"', base_url_line),
        )
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 0, result.output
        assert '"https://myshop.example/api/orders",' in module.read_text()
        assert (
            "orders calls myshop.example; the plugin sets no base_url gp can read as a URL, "
            "so its request is an absolute URL"
        ) in " ".join(_plain(result.output).split())
        _ruff_clean(recorded)
```

`recorded` (line 80) writes the `myshop` recording, all on `myshop.example`, and generated plugins get `base_url = "https://myshop.example"`. `_hand_written_project` (line 640) with a `base_url` built from `+` gives `PluginView.base_url is None`, since the reader reads only a string literal (`src/graftpunk/devtools/plugin_project.py:892` (`def _class_string`)); with the scheme-less literal it gives `"myshop.example"`, for which `base_host` is `None`. These are the two ways a plugin reaches `add-command` with no base host, now that `gp plugin new` refuses a `--url` without one.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run --all-extras pytest tests/unit/test_scaffold_cli.py tests/unit/test_plugin_project_cli.py -q -k "TwoHost or TargetsTheEndpointsHost or UrlNeedsAHost"`
Expected: `8 failed, 4 passed`. The three `test_a_url_without_an_http_scheme_and_a_host_is_refused` cases fail on `assert result.exit_code == 1` (the value is accepted today). `test_the_output_names_the_command_on_the_other_host` fails with `notices == []`; `test_url_on_the_api_host_turns_the_page_command_absolute`, `test_an_endpoint_on_another_host_is_requested_absolutely`, and both `test_a_plugin_with_no_readable_base_url_gets_an_absolute_url` cases fail with an `AssertionError` on the information line, after the module assertions (Task 2's) pass.

- [ ] **Step 4: Write the implementation**

In `src/graftpunk/cli/scaffold_shared.py`, replace the module docstring:

```python
"""Shared state for ``gp plugin``'s scaffold commands.

``scaffold_commands.py`` (``new``) and ``scaffold_project_commands.py``
(``info``, ``add-command``, ``upgrade``, ``check``) both attach their
commands to ``plugin_app`` here, and both call :func:`command_selections` to
parse a ``--command`` value; splitting either of those two per module would
give the CLI two Typer sub-apps or two parsers instead of one.
"""
```

with:

```python
"""Shared state for ``gp plugin``'s scaffold commands.

``scaffold_commands.py`` (``new``) and ``scaffold_project_commands.py``
(``info``, ``add-command``, ``upgrade``, ``check``) both attach their
commands to ``plugin_app`` here, both call :func:`command_selections` to
parse a ``--command`` value, and both call :func:`print_other_host` to word
a command that calls another host; splitting any of those three per module
would give the CLI two Typer sub-apps, two parsers, or two wordings instead
of one.
"""
```

add the import above the `selection` import:

```python
from graftpunk.devtools.scaffold.render import OtherHostCommand
from graftpunk.devtools.scaffold.selection import CommandSelection
```

and append to the end of the module:

```python


def print_other_host(other: OtherHostCommand) -> None:
    """The line ``gp plugin new`` and ``gp plugin add-command`` print for a command
    whose request is an absolute URL. ``soft_wrap`` keeps it whole for a reader
    that matches it; ``highlight=False`` keeps Rich from colouring the hosts."""
    if other.base_host is None:
        line = (
            f"{other.name} calls {other.host}; the plugin sets no base_url gp can read "
            "as a URL, so its request is an absolute URL"
        )
    else:
        line = (
            f"{other.name} calls {other.host}, not {other.base_host}; "
            "its request is an absolute URL"
        )
    console.print(escape(line), soft_wrap=True, highlight=False)
```

In `src/graftpunk/cli/scaffold_commands.py`, replace line 25:

```python
from graftpunk.cli.scaffold_shared import LOG, command_selections, console, plugin_app
```

with:

```python
from graftpunk.cli.scaffold_shared import (
    LOG,
    command_selections,
    console,
    plugin_app,
    print_other_host,
)
```

replace the render import:

```python
from graftpunk.devtools.scaffold.render import (
    ScaffoldSpec,
    fixture_paths,
    graftpunk_version_floor,
    validate_plugin_name,
)
```

with:

```python
from graftpunk.devtools.scaffold.render import (
    ScaffoldSpec,
    base_host,
    fixture_paths,
    graftpunk_version_floor,
    other_host_commands,
    validate_plugin_name,
)
```

directly after the `--command requires --from-run` refusal (its `raise typer.Exit(1)` follows line 169) and before `digest_result = None`, add:

```python
    if url and base_host(url) is None:
        LOG.debug("scaffold_refused", reason="url_without_host")
        console.print(
            "[red]--url must be an http:// or https:// URL with a host, "
            f"got '{escape(url)}'.[/red]",
            soft_wrap=True,
        )
        raise typer.Exit(1)
```

and replace lines 256-258:

```python
    if result.gitignore_updated:
        console.print(f"[dim]Added {escape(CAPTURES_DIR)}/ to .gitignore[/dim]")
    _print_next_steps(dataclasses.replace(spec, mode=result.mode))
```

with:

```python
    if result.gitignore_updated:
        console.print(f"[dim]Added {escape(CAPTURES_DIR)}/ to .gitignore[/dim]")
    for other in other_host_commands(spec):
        print_other_host(other)
    _print_next_steps(dataclasses.replace(spec, mode=result.mode))
```

In `src/graftpunk/cli/scaffold_project_commands.py`, replace line 21 with the same multi-line `scaffold_shared` import as `scaffold_commands.py` above, and directly after the `console.print(` call that prints `[green]Added[/green] ...` (lines 129-132), add:

```python
    if added.other_host is not None:
        print_other_host(added.other_host)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run --all-extras pytest tests/unit/test_scaffold_cli.py tests/unit/test_plugin_project_cli.py -q`
Expected: all pass.

Run: `uvx ruff check src tests && uvx ruff format --check src tests && uvx ty@0.0.75 check src/`
Expected: `All checks passed!`, every file already formatted, and `All checks passed!`.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/cli/scaffold_shared.py src/graftpunk/cli/scaffold_commands.py src/graftpunk/cli/scaffold_project_commands.py tests/unit/test_scaffold_cli.py tests/unit/test_plugin_project_cli.py
git commit -m "feat(cli): gp plugin new and add-command name each command that calls another host"
```

> **Design note (2026-10-06):** Each CLI module used to print `OtherHostCommand.notice`, wording that lived in `render.py`; both now call `print_other_host` in `scaffold_shared.py`, the one place the line is worded. `base_host=None` used to cover both a `gp plugin new --url` with no scheme and an `add-command` plugin whose `base_url` the reader could not read; `gp plugin new` now refuses the first (`TestUrlNeedsAHost`), so the no-base-host wording only has to fit `add-command`, and it says the plugin sets no `base_url` gp can read as a URL.

---

### Task 4: `host` in the endpoints projection, and the `--all-hosts` help

**Files:**
- Modify: `src/graftpunk/har/report.py:249` (`"method": method,`)
- Modify: `src/graftpunk/cli/observe_commands.py:157-159` (`all_hosts: Annotated[`)
- Test: `tests/unit/test_har_report.py:257` (`_ENDPOINT_V1 = {`), `:325` (`class TestEndpointsProjection:`); `tests/unit/test_observe_commands.py` (end of module)

**Interfaces:**
- Consumes: `Endpoint.host` (`src/graftpunk/har/digest.py:252` (`class Endpoint:`)), spelled by `normal_host` when the digest builds it.
- Produces: `endpoints_projection(d)["endpoints"][i]["host"]: str`, within schema `endpoints` 1 (`src/graftpunk/contracts.py:53` (`ENDPOINTS_SCHEMA: Final = 1`) unchanged). The graft skill's understand step consumes it after the release.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_har_report.py`, add `"host",` to `_ENDPOINT_V1` after `"method",`:

```python
_ENDPOINT_V1 = {
    "method",
    "host",
    "template",
    "login_flow",
    "content_type",
    "shape",
    "query_params",
    "body_params",
    "custom_headers",
}
```

and add as the first method of `TestEndpointsProjection` (line 325):

```python
    def test_each_endpoint_carries_the_host_it_was_recorded_on(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://myshop.example/account",
                content_type="text/html",
                body="<html><body>Account</body></html>",
            ),
            _entry("GET", "https://api.myshop.example/api/orders/1001", body='{"id": 1}'),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        payload = endpoints_projection(result)
        assert payload["schema"] == 1
        assert payload["primary_host"] == "myshop.example"
        hosts = {(e["method"], e["template"]): e["host"] for e in payload["endpoints"]}
        assert hosts == {
            ("GET", "/account"): "myshop.example",
            ("GET", "/api/orders/{order_id}"): "api.myshop.example",
        }
        assert hosts == {
            (method, e.template): e.host for e in result.endpoints for method in e.methods
        }
```

Append to the end of `tests/unit/test_observe_commands.py` (it already imports `typer` and `observe_app`):

```python
def test_the_all_hosts_help_says_the_default_keeps_the_primary_hosts_domain() -> None:
    """The default digest keeps every host under the primary host's domain
    (api.myshop.example beside myshop.example), not the primary host alone."""
    digest_command = typer.main.get_command(observe_app).commands["digest"]
    (option,) = [p for p in digest_command.params if "--all-hosts" in p.opts]
    assert option.help == "Model every host, not just the primary host's domain"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run --all-extras pytest tests/unit/test_har_report.py tests/unit/test_observe_commands.py -q -k "host or field_set or all_hosts_help"`
Expected: `test_each_endpoint_carries_the_host_it_was_recorded_on` FAILS with `KeyError: 'host'`; `test_schema_one_and_its_field_set` FAILS on `set(entry) == _ENDPOINT_V1`; `test_the_all_hosts_help_says_the_default_keeps_the_primary_hosts_domain` FAILS, the help reading `'Model every host, not just the primary one'`.

- [ ] **Step 3: Write the implementation**

In `src/graftpunk/har/report.py`, in `endpoints_projection`, replace:

```python
            {
                "method": method,
                "template": endpoint.template,
```

with:

```python
            {
                "method": method,
                "host": endpoint.host,
                "template": endpoint.template,
```

In `src/graftpunk/cli/observe_commands.py`, replace lines 157-159:

```python
    all_hosts: Annotated[
        bool, typer.Option("--all-hosts", help="Model every host, not just the primary one")
    ] = False,
```

with:

```python
    all_hosts: Annotated[
        bool,
        typer.Option("--all-hosts", help="Model every host, not just the primary host's domain"),
    ] = False,
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run --all-extras pytest tests/unit/test_har_report.py tests/unit/test_observe_commands.py tests/unit/test_contracts.py -q`
Expected: all pass (`test_contracts.py` confirms `current_schema("endpoints") == 1` still).

Run: `uvx ruff check src tests && uvx ruff format --check src tests && uvx ty@0.0.75 check src/`
Expected: `All checks passed!`, every file already formatted, and `All checks passed!`.

- [ ] **Step 5: Commit**

```bash
git add src/graftpunk/har/report.py src/graftpunk/cli/observe_commands.py tests/unit/test_har_report.py tests/unit/test_observe_commands.py
git commit -m "feat(har): each endpoint in --endpoints-json carries the host it was recorded on"
```

---

### Task 5: The guide, the changelog, and the full gate

**Files:**
- Modify: `docs/PLUGIN_DEVELOPMENT.md:217-219` (`just the primary one`), `:541-544` (`it is the only source of`), `:709-710` (`the observed custom headers, and the endpoint it`), `:743` (`never read as JSON, so a`), `:873-876` (`Added export to src/graftpunk_myshop/plugin.py`)
- Modify: `CHANGELOG.md:8` (`## [1.17.0] - 2026-10-06`)
- Test: `tests/unit/test_plugin_development_guide.py` (unchanged; it must stay green)

**Interfaces:**
- Consumes: the CLI output of Task 3 (`api-orders-by-order-id calls api.myshop.example, not myshop.example; its request is an absolute URL`, the line `TestATwoHostRecording` pins), the `--url` refusal of Task 3, and the projection field of Task 4.
- Produces: documentation only.

The guide's generated-module example (`test_the_generated_plugin_example_matches_the_generator_output`, `tests/unit/test_plugin_development_guide.py:88` (`def test_the_generated_plugin_example_matches_the_generator_output`)) renders a one-host digest (`host="myshop.example"`, `base_url="https://myshop.example"`), so its block does not change. No guide test pins the wording edited here. `test_every_gp_invocation_names_a_real_command_and_options` checks every `gp` invocation in a `bash` block or an inline code span, and the spans this task adds (`gp plugin info --json` and `gp plugin new`) name real commands and options.

- [ ] **Step 1: Edit the guide's digest options paragraph**

In `docs/PLUGIN_DEVELOPMENT.md`, replace:

```markdown
own observations, never a logout or a cart redirect recorded beside it), `--all-hosts` models every host instead of
just the primary one, `--limit N` raises the cap on how many endpoints the
markdown form lists (60 by default), and `--output PATH` writes to a file.
```

with:

```markdown
own observations, never a logout or a cart redirect recorded beside it), `--all-hosts` models every host instead of
just the primary host's domain, `--limit N` raises the cap on how many endpoints the
markdown form lists (60 by default), and `--output PATH` writes to a file.
Each endpoint in `--endpoints-json` carries the `host` it was recorded on,
beside the run's one `primary_host`, so a program can see a site whose pages
and JSON come from different hosts.
```

- [ ] **Step 2: Edit the `--url` option**

Replace:

```markdown
- `--url URL` sets `base_url`. Without `--from-run` it is the only source of
  one. With `--from-run`, `base_url` comes from the digest's primary host, and
  an explicit `--url` overrides it (use that when the recording's busiest host
  is a CDN or an API subdomain you do not want as the base).
```

with:

```markdown
- `--url URL` sets `base_url`. Without `--from-run` it is the only source of
  one. With `--from-run`, `base_url` comes from the digest's primary host, and
  an explicit `--url` overrides it (use that when the recording's busiest host
  is a CDN or an API subdomain you do not want as the base). It must be an
  `http://` or `https://` URL with a host; `--url myshop.example` is refused,
  since no path resolves against it. A site whose pages and JSON come from
  different hosts needs no `--url`: each stub requests the host its endpoint
  was recorded on (see [What gets filled in](#what-gets-filled-in)).
```

- [ ] **Step 3: Edit "What came from the digest" and add the two-host paragraph**

Replace:

```markdown
types](#cli-parameter-types)), the observed custom headers, and the endpoint it
calls declared as `endpoint=` on its decorator; a docstring recording the
```

with:

```markdown
types](#cli-parameter-types)), the observed custom headers, the endpoint it
calls declared as `endpoint=` on its decorator (method and path only), and a
request for a path, which `ctx.request_json` joins to `base_url`, or, for an
endpoint recorded on a host other than `base_url`'s, for the absolute URL on
that host; a docstring recording the
```

Then replace the paragraph boundary after the fixture paragraph:

````markdown
never read as JSON, so a `text/plain` `0` keeps `assert result`.

````

with:

````markdown
never read as JSON, so a `text/plain` `0` keeps `assert result`.

A site whose pages are on `myshop.example` and whose JSON is on
`api.myshop.example` gets `base_url = "https://myshop.example"` and, for each
JSON endpoint, a request for `f"https://api.myshop.example/api/orders/{order_id}"`
in place of the path. Its generated test is unchanged, since `FixtureSession`
finds a fixture by method and path. `gp plugin new` prints one line for each
such command:

```text
api-orders-by-order-id calls api.myshop.example, not myshop.example; its request is an absolute URL
```

````

- [ ] **Step 4: Edit the add-command section**

Replace:

````markdown
```text
Added export to src/graftpunk_myshop/plugin.py
Next: gp observe fixtures writes no fixture for this endpoint; write its test against a fixture of your own.
```
````

with:

````markdown
```text
Added export to src/graftpunk_myshop/plugin.py
Next: gp observe fixtures writes no fixture for this endpoint; write its test against a fixture of your own.
```

The stub requests the host its endpoint was recorded on, by the rule
`gp plugin new` follows: a path on the host of the plugin's `base_url`, and an absolute
URL on any other host, with a line after `Added` naming the command and both
hosts. When the plugin sets no `base_url` that `gp plugin info --json` can
report as a URL (none, one built from an expression rather than a string, or
a string with no `http://` or `https://`), every stub it adds requests an
absolute `https://` URL, and the line says the plugin sets no `base_url` gp
can read as a URL.
````

- [ ] **Step 5: Add the changelog entry**

`CHANGELOG.md` has no `## [Unreleased]` section (checked: `grep -n Unreleased CHANGELOG.md` prints nothing). Insert directly above `## [1.17.0] - 2026-10-06` (line 8):

```markdown
## [Unreleased]

### Upgrading

1. **A plugin generated by 1.17.0 from a recording whose pages and JSON are on different hosts still calls `base_url`'s host for its JSON commands.** Nothing rewrites an existing module. Put the API host's origin in front of each such request by hand (`f"https://api.myshop.example/api/orders/{order_id}"` for `f"/api/orders/{order_id}"`), or regenerate the command with `gp plugin add-command`.
2. **`gp plugin new --url` refuses a value that is not an `http://` or `https://` URL with a host.** `--url myshop.example` used to write `base_url = "myshop.example"`, which no request path resolves against; pass `--url https://myshop.example`.

### Added

- **`host` on each endpoint of `gp observe digest --endpoints-json`** ([#225](https://github.com/stavxyz/graftpunk/issues/225)). The host the endpoint was recorded on, beside the run's one `primary_host`. It is added within schema `endpoints` 1, so a caller checking `gp version --contract endpoints=1` still matches.
- **`gp plugin new` and `gp plugin add-command` name each command that calls another host** ([#214](https://github.com/stavxyz/graftpunk/issues/214)), one line per command: `<name> calls <host>, not <base host>; its request is an absolute URL`.

### Fixed

- **A generated command calls the host its endpoint was recorded on** ([#214](https://github.com/stavxyz/graftpunk/issues/214)). Every stub requested a path, which `SiteRequests` joins to `base_url`, and `base_url` comes from the host that served the pages, so on a site whose JSON is on `api.myshop.example` every JSON command called `myshop.example` and failed on its first live call while its fixture test passed. A stub for an endpoint on a host other than `base_url`'s now requests the absolute URL on that host, under `base_url`'s scheme. `gp plugin add-command` into a plugin whose `base_url` it cannot read as a URL writes an absolute `https://` URL for every endpoint. The `endpoint=` declaration and the generated test are unchanged.
- **`gp observe digest --all-hosts` help** says the default keeps the primary host's domain, as it always has, not the primary host alone ([#225](https://github.com/stavxyz/graftpunk/issues/225)). The login `url` `GP-FILL` comment says the field takes the login page's full URL when it is not on `base_url`'s host.

```

- [ ] **Step 6: Check the prose rules**

Older `CHANGELOG.md` entries use em dashes, so the check covers the guide and only the new changelog section.

Run: `grep -cP '\x{2014}|\x{2013}' docs/PLUGIN_DEVELOPMENT.md; sed -n '/^## \[Unreleased\]/,/^## \[1.17.0\]/p' CHANGELOG.md | grep -cP '\x{2014}|\x{2013}'`
Expected: `0`, then `0`.

- [ ] **Step 7: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run --all-extras pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
Expected: pytest reports no failures and no errors, then `All checks passed!`, every file already formatted, and `All checks passed!`. The gate checks only those outcomes, not a test count. For reference, a scratch run of this plan's code, tests, and doc edits on a copy of this tree on 2026-10-06 recorded `5780 passed, 13 skipped` (against `5738 passed, 13 skipped` on the unmodified tree); that figure is a record of one run, not something this step verifies.

- [ ] **Step 8: Commit**

```bash
git add docs/PLUGIN_DEVELOPMENT.md CHANGELOG.md
git commit -m "docs: per-endpoint hosts in the plugin guide and the changelog"
```

---

## Self-Review

**Spec coverage.** The one rule and its home: Task 1. Both writers calling it, `gp plugin new` with `--url` or `https://{primary_host}` and `add-command` with `PluginView.base_url`: Task 2 (render and insert), exercised through both CLIs in Tasks 2 and 3. No readable `base_url`: Tasks 1 and 3, and its one remaining source after the `--url` refusal (Task 3). Absolute URL built by `_templated_url` with the origin in front, `endpoint=` unchanged, generated test unchanged: Task 2. The projection's `host`, schema unchanged: Task 4. The information line, for one other-host command and for none: Task 3. The `--all-hosts` help and the login `url` `GP-FILL` comment: Tasks 4 and 2. Every bullet of the spec's Testing section: `request_target` cases (Task 1), the two-host recording end to end (Task 2), `add-command` into a page-host plugin and into one with no readable `base_url` (Task 3), the projection (Task 4), the writers' line (Task 3). The skill section and the Known limits are out of scope, stated in the implementation notes. Where the plan departs from the spec's first text, the spec's "Amended 2026-10-06" sentence says so.

**Placeholder scan.** Every code step carries its code; every run step carries its command and expected result. Hosts used: `myshop.example`, `api.myshop.example`, `myshop.example.com`, `api.myshop.example.com`, `regional-api-gateway.customer-services.myshop.example.com`, and the existing test's `xxx...x.example.com`.

**Type consistency.** `_read_base_url(base_url: str | None) -> _BaseUrl | None`, `base_host(base_url: str | None) -> str | None`, `_on_base_host(scheme, netloc, base: _BaseUrl | None) -> bool`, and `request_target(endpoint_host: str, base_url: str | None) -> RequestTarget` in Tasks 1, 2, and 3; `RequestTarget(origin, host, base_host)` in Tasks 1 and 2; `OtherHostCommand(name, host, base_host)` in Tasks 2 and 3, worded by `print_other_host` in Task 3; `_target_for(command, base_url) -> RequestTarget`, `_render_command_stub(command, run_label, target)`, and `render_command(command, d, base_url)` in Task 2's `render.py`, `insert.py`, and tests; `RenderedCommand.other_host` and `AddedCommand.other_host` typed `OtherHostCommand | None` in Tasks 2 and 3.

**Review Focus.** Each of the five lines has its pinned test in the named task.
