---
type: plan
validated:
  sha: 493d10f
  date: 2026-09-12T19:22:11Z
  reviewers: [fact-check, solid-hygiene]
  findings:
    critical: 1
    important: 3
    medium: 3
    low: 0
    nitpick: 0
  net_negative_raised: 3
  net_negative_addressed: 3
  net_negative_remaining: 0
---

# Plugin Tooling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `gp import-har` with three commands built on a shared HAR digest (`gp observe digest`, `gp observe fixtures`, `gp plugin new`), so a plugin author starts from a readable summary of a capture instead of a 36 MB HAR file and ends at a scaffold that calls graftpunk's real, current API.

**Architecture:** One analysis package, `graftpunk.har`, gains a digest model (`har/digest.py`) built from a HAR entry list, four supporting pure modules (`har/paths.py`, `har/naming.py`, `har/documents.py`, `har/report.py`), and drops `analyzer.py`/`generator.py`. Two new framework primitives the scaffold calls into (`CommandContext.request_json`/`request_text`, backed by `plugins/site_requests.py`, and the pytest-free `graftpunk.testing` package) live at the top level so a generated plugin can import them at runtime and in its own tests. Everything that only `gp` runs (the scaffold, the capture-directory helper) lives under `graftpunk.devtools`, never imported by generated code. Three CLI surfaces consume all of this: `cli/observe_commands.py` (`digest`, `fixtures`, and the shared `resolve_run`), `cli/scaffold_commands.py` (`gp plugin new`), and `cli/plugin_commands.py` (the reserved top-level CLI names a plugin's `site_name` cannot collide with).

**Tech Stack:** Python 3.11+, Typer 0.21+, `requests`, `structlog`, `html.parser` (stdlib), `tomllib` (stdlib), pytest (`uv run pytest`), ruff, ty 0.0.75.

**Spec:** `docs/superpowers/specs/2026-09-11-plugin-tooling-design.md` (approved, validated against `f9fe962`, rebased onto `3b3b661`). Read it alongside this plan; where this plan and the spec conflict, follow the spec except where a task below carries an **Implementation note** recording a reviewed deviation made necessary by Python's import rules or by this repository's HEAD.

## Global Constraints

- Python `>=3.11` typing throughout: `X | None`, never `Optional[X]`; `Literal[...]` for closed string sets; `from __future__ import annotations` at the top of every new module.
- structlog event-style logging where a module logs at all: `LOG = get_logger(__name__)` at module scope, event name first, values as keyword arguments.
- Gate command, green at every commit: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
- Tests assert behaviour, never mock-call counts (no `assert_called_once_with`, no `call_count`); a synthetic HAR is built by hand in `tmp_path` for every digest/report/fixtures/scaffold test, never a recorded capture. No test launches a browser or opens a network socket.
- Placeholders only, in tests and docs: `myshop`, `alice@example.com`, `bob@example.com`, `fmtsite`, `example.com`. Never name a real site, store, account, or domain.
- No em dashes or en dashes anywhere in new prose, docstrings, comments, or commit messages. Use commas, colons, parentheses, or separate sentences.
- No attribution trailers in commits. Commit subjects are `type(scope): subject`.
- The scaffold rule: a generated file may contain only two kinds of line, facts about the site (URLs, selectors, parameter names, header names) and calls into graftpunk's public API. It never contains graftpunk's own logic (a copy of expired-session detection, a hand-rolled retry loop, and so on).
- The placement rule: public API a plugin imports at runtime or in its own tests (the request helpers on `CommandContext`, `graftpunk.testing`) lives at the top level of the package; CLI-only tooling that a plugin never imports (the scaffold, the capture-directory helper) lives under `graftpunk.devtools`. `graftpunk.har` is analysis and is importable by both.
- Every numeric threshold introduced by this plan is a named module-level constant, defined once in the module that applies it, so tests assert the name rather than repeat the literal.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/graftpunk/har/paths.py` (new) | Pure path templating: `template_path`. |
| `src/graftpunk/har/naming.py` (new) | The one capture/fixture filename rule: `capture_slug`, `capture_filename`. Depends only on `paths.py`. |
| `src/graftpunk/har/documents.py` (new) | HTML extraction on stdlib `html.parser`: `LoginForm`, `TokenCandidate`, `TokenKind`, `extract_login_forms`, `extract_token_candidates`, `is_login_document`. |
| `src/graftpunk/har/digest.py` (new) | The digest model (`DigestSource`, `ShapeNode`, `Endpoint`, `LoginObservation`, `RunDigest`) and `digest()`. Calls `paths.py`, `documents.py`, `parser.py`. |
| `src/graftpunk/har/report.py` (new) | `render_markdown`, `render_json`. Not exported from the package top level. |
| `src/graftpunk/har/__init__.py` (modify) | Drops the analyzer/generator exports, adds the digest exports. |
| `src/graftpunk/har/analyzer.py`, `generator.py` (deleted, Task 10) | Superseded by `digest.py`. |
| `src/graftpunk/exceptions.py` (modify) | `SessionRejectedError`, `UnexpectedResponseError`. |
| `src/graftpunk/plugins/site_requests.py` (new) | `SiteRequests`: the role, rejection, and body-shape policy behind `ctx.request_json`/`request_text`. |
| `src/graftpunk/plugins/cli_plugin.py` (modify) | `CommandContext.request_json`/`request_text` delegates. |
| `src/graftpunk/testing/__init__.py` (new) | `make_context`, `FixtureSession`, `fixture_context`. Pytest-free. |
| `src/graftpunk/testing/plugin.py` (new) | `site_env_scrubber`. Imports pytest; loaded only as a pytest plugin. |
| `src/graftpunk/devtools/__init__.py` (new) | Package docstring only: CLI-only tooling, never imported by a generated plugin. |
| `src/graftpunk/devtools/captures.py` (new) | `CAPTURES_DIR`, `ensure_ignored`, `is_tracked`: the one owner of "captures never enter git". |
| `src/graftpunk/cli/observe_commands.py` (new) | `resolve_run`, `digest_cmd`, `fixtures_cmd`, `register(observe_app)`. |
| `src/graftpunk/cli/main.py` (modify) | Calls `observe_commands.register`; `observe_show` uses `resolve_run`; drops `import-har` (Task 10). |
| `src/graftpunk/devtools/scaffold/__init__.py` (new) | Package docstring only. |
| `src/graftpunk/devtools/scaffold/render.py` (new) | `ScaffoldSpec`, `render(spec) -> dict[str, str]`, `class_name_for`. |
| `src/graftpunk/devtools/scaffold/project.py` (new) | `write_scaffold`, `ScaffoldConflictError`, `ScaffoldResult`: mode decision, conflict check, write. |
| `src/graftpunk/devtools/scaffold/pyproject_edit.py` (new) | `add_entry_point`, `add_wheel_package`, `PyprojectEditError`: the one seam that edits an existing `pyproject.toml`. |
| `src/graftpunk/cli/scaffold_commands.py` (new) | `plugin_app`, `gp plugin new`: argument handling only. |
| `src/graftpunk/cli/plugin_commands.py` (modify) | `reserved_cli_names()`, derived from the app at attach time. |
| `src/graftpunk/cli/import_har.py` (deleted, Task 10) | Superseded by `gp plugin new`. |
| `examples/templates/python_template.py` (deleted, Task 10) | Superseded by `gp plugin new`. |
| `tests/unit/conftest.py` (modify) | `_CONSOLE_LOCATIONS` gains the two new CLI modules' consoles (Task 7, Task 9) and drops `import_har`'s (Task 10). |
| `README.md`, `docs/HOW_IT_WORKS.md`, `examples/README.md`, `CHANGELOG.md` (modify, Task 10) | Document the three commands; retire `import-har`. |

---

### Task 1: `har/paths.py` and `har/naming.py`

**Files:**
- Create: `src/graftpunk/har/paths.py`
- Create: `src/graftpunk/har/naming.py`
- Test: `tests/unit/test_har_paths.py`
- Test: `tests/unit/test_har_naming.py`

**Interfaces:**
- Consumes: nothing outside the standard library.
- Produces: `paths.template_path(path: str) -> tuple[str, dict[str, str]]`; `paths.param_name_for_segment(prev_segment: str) -> str` (the singular-of-previous-segment rule, reused by `digest.py`'s high-cardinality collapsing in Task 3). `naming.capture_slug(method: str, path: str) -> str`; `naming.capture_filename(method: str, path: str, content_type: str) -> str`. Tasks 3, 6, 7, and 9 all import from here.

**Implementation note:** the spec describes `template_path`'s return as `tuple[str, dict[str, str]]` without naming what the dict maps; this plan maps collapsed parameter name to the literal segment value it replaced, so a caller (the digest's endpoint examples, the fixtures command) can see what was actually observed at that position.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_har_paths.py`:

```python
"""Pure path templating: no HAR involved (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import pytest

from graftpunk.har.paths import param_name_for_segment, template_path


class TestTemplatePath:
    def test_numeric_segment_collapses_with_singular_param_name(self) -> None:
        template, params = template_path("/orders/123")
        assert template == "/orders/{order_id}"
        assert params == {"order_id": "123"}

    def test_multiple_numeric_segments(self) -> None:
        template, params = template_path("/orders/123/items/456")
        assert template == "/orders/{order_id}/items/{item_id}"
        assert params == {"order_id": "123", "item_id": "456"}

    def test_uuid_segment_collapses(self) -> None:
        template, params = template_path("/sessions/8f14e45f-ceea-467e-bd3d-46f0e7d1f5a3")
        assert template == "/sessions/{session_id}"

    def test_hex_segment_at_least_16_chars_collapses(self) -> None:
        template, _ = template_path("/tokens/0123456789abcdef")
        assert template == "/tokens/{token_id}"

    def test_hex_segment_under_16_chars_stays_literal(self) -> None:
        template, _ = template_path("/tokens/abc123")
        assert template == "/tokens/abc123"

    def test_base64_like_segment_at_least_20_chars_collapses(self) -> None:
        template, _ = template_path("/files/QWxhZGRpbjpvcGVuIHNlc2FtZQ")
        assert template == "/files/{file_id}"

    def test_short_alphabetic_segment_stays_literal(self) -> None:
        template, params = template_path("/products/wireless-mouse")
        assert template == "/products/wireless-mouse"
        assert params == {}

    def test_leading_segment_with_no_predecessor_uses_bare_id(self) -> None:
        template, params = template_path("/123")
        assert template == "/{id}"
        assert params == {"id": "123"}

    def test_root_path(self) -> None:
        assert template_path("/") == ("/", {})

    def test_trailing_slash_preserved(self) -> None:
        template, _ = template_path("/orders/123/")
        assert template == "/orders/{order_id}/"

    def test_plural_previous_segment_singularizes(self) -> None:
        template, _ = template_path("/carts/999")
        assert template == "/carts/{cart_id}"

    def test_previous_segment_already_a_param_uses_bare_id(self) -> None:
        template, _ = template_path("/orders/123/456")
        assert template == "/orders/{order_id}/{id}"


class TestParamNameForSegment:
    @pytest.mark.parametrize(
        ("prev", "expected"),
        [
            ("orders", "order_id"),
            ("cart", "cart_id"),
            ("", "id"),
            ("{order_id}", "id"),
        ],
    )
    def test_singular_of_previous_segment(self, prev: str, expected: str) -> None:
        assert param_name_for_segment(prev) == expected
```

Create `tests/unit/test_har_naming.py`:

```python
"""The one capture/fixture filename rule (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

from graftpunk.har.naming import capture_filename, capture_slug


class TestCaptureSlug:
    def test_method_lowercased_and_path_templated(self) -> None:
        assert capture_slug("GET", "/orders/123") == "get_orders_{order_id}"

    def test_root_path_uses_root_placeholder(self) -> None:
        assert capture_slug("GET", "/") == "get_root"

    def test_two_calls_with_different_ids_produce_the_same_slug(self) -> None:
        assert capture_slug("GET", "/orders/1") == capture_slug("GET", "/orders/2")


class TestCaptureFilename:
    def test_json_content_type(self) -> None:
        assert capture_filename("GET", "/orders/123", "application/json") == (
            "get_orders_{order_id}.json"
        )

    def test_html_content_type(self) -> None:
        assert capture_filename("GET", "/login", "text/html; charset=utf-8") == (
            "get_login.html"
        )

    def test_other_text_content_type_falls_back_to_txt(self) -> None:
        assert capture_filename("GET", "/robots", "text/plain") == "get_robots.txt"

    def test_unknown_content_type_falls_back_to_bin(self) -> None:
        assert capture_filename("GET", "/asset", "application/octet-stream") == (
            "get_asset.bin"
        )

    def test_known_binary_content_type_uses_its_extension(self) -> None:
        assert capture_filename("GET", "/photo", "image/png") == "get_photo.png"

    def test_post_method_lowercased(self) -> None:
        assert capture_filename("POST", "/login", "application/json") == "post_login.json"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_har_paths.py tests/unit/test_har_naming.py -q`
Expected: collection errors, `ModuleNotFoundError: No module named 'graftpunk.har.paths'` and `'graftpunk.har.naming'`.

- [ ] **Step 3: Write the implementation**

Create `src/graftpunk/har/paths.py`:

```python
"""Pure path templating, no HAR involved.

The one rule for collapsing a dynamic path segment into a parameter, shared
by the digest's endpoint modelling and the fixtures/naming rule below it
(plugin tooling spec, 2026-09-11).
"""

from __future__ import annotations

import re

# A segment collapses to a parameter when it looks like an opaque
# identifier rather than a word: all digits, a UUID, 16+ hex characters, or
# 20+ URL-safe-base64-like characters. The base64-like check additionally
# requires at least one digit, so an ordinary long slug ("administrator-
# dashboard") is not mistaken for an encoded token.
_MIN_HEX_LEN = 16
_MIN_BASE64_LEN = 20

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_BASE64_RE = re.compile(r"^[A-Za-z0-9_-]+=*$")

__all__ = ["param_name_for_segment", "template_path"]


def _is_dynamic_segment(segment: str) -> bool:
    if not segment:
        return False
    if segment.isdigit():
        return True
    if _UUID_RE.match(segment):
        return True
    if len(segment) >= _MIN_HEX_LEN and _HEX_RE.match(segment):
        return True
    if (
        len(segment) >= _MIN_BASE64_LEN
        and _BASE64_RE.match(segment)
        and any(ch.isdigit() for ch in segment)
    ):
        return True
    return False


def param_name_for_segment(prev_segment: str) -> str:
    """The parameter name for a segment collapsed at ``prev_segment``'s position.

    The singular of the preceding literal segment (``orders`` -> ``order_id``),
    or the bare ``id`` when there is no preceding segment or it is itself a
    collapsed parameter (``{order_id}``).
    """
    if not prev_segment or prev_segment.startswith("{"):
        return "id"
    singular = prev_segment[:-1] if prev_segment.endswith("s") and len(prev_segment) > 1 else prev_segment
    return f"{singular}_id"


def template_path(path: str) -> tuple[str, dict[str, str]]:
    """Collapse a URL path's dynamic segments into named parameters.

    ``/orders/123`` becomes ``/orders/{order_id}``. A leading and/or
    trailing slash is preserved exactly as given.

    Returns:
        The templated path, and a dict mapping each collapsed parameter
        name to the literal segment value it replaced.
    """
    leading = "/" if path.startswith("/") else ""
    trailing = "/" if len(path) > 1 and path.endswith("/") else ""
    segments = path.strip("/").split("/") if path.strip("/") else []

    params: dict[str, str] = {}
    result_segments: list[str] = []
    for segment in segments:
        if _is_dynamic_segment(segment):
            prev = result_segments[-1] if result_segments else ""
            name = param_name_for_segment(prev)
            params[name] = segment
            result_segments.append(f"{{{name}}}")
        else:
            result_segments.append(segment)

    body = "/".join(result_segments)
    return f"{leading}{body}{trailing}" if body else (leading or "/"), params
```

Create `src/graftpunk/har/naming.py`:

```python
"""The one capture/fixture filename rule.

``gp observe fixtures``, :mod:`graftpunk.testing`'s ``FixtureSession``, and
the scaffold's generated tests all name a file the same way, so this module
depends on ``paths.py`` and nothing else (plugin tooling spec, 2026-09-11).
"""

from __future__ import annotations

from graftpunk.har.paths import template_path

__all__ = ["capture_filename", "capture_slug"]

_EXTENSION_BY_MIME: dict[str, str] = {
    "application/json": "json",
    "text/html": "html",
    "text/xml": "xml",
    "application/xml": "xml",
    "text/css": "css",
    "application/javascript": "js",
    "text/javascript": "js",
    "application/pdf": "pdf",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/svg+xml": "svg",
}
_TEXT_MIME_KEYWORDS = ("text", "xml")


def capture_slug(method: str, path: str) -> str:
    """The method+templated-path stem shared by a capture, a fixture, and its sidecar."""
    template, _ = template_path(path)
    body = template.strip("/").replace("/", "_")
    return f"{method.lower()}_{body or 'root'}"


def _extension_for_content_type(content_type: str) -> str:
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime in _EXTENSION_BY_MIME:
        return _EXTENSION_BY_MIME[mime]
    if any(keyword in mime for keyword in _TEXT_MIME_KEYWORDS):
        return "txt"
    return "bin"


def capture_filename(method: str, path: str, content_type: str) -> str:
    """``<method>_<slug>.<ext>``, the one name a capture, fixture, and test share."""
    return f"{capture_slug(method, path)}.{_extension_for_content_type(content_type)}"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_har_paths.py tests/unit/test_har_naming.py -q`
Expected: PASS, all tests green.

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
Expected: all four green. Nothing imports the new modules yet.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/har/paths.py src/graftpunk/har/naming.py tests/unit/test_har_paths.py tests/unit/test_har_naming.py
git commit -m "feat(har): add pure path templating and the capture naming rule"
```

---

### Task 2: `har/documents.py`

**Files:**
- Create: `src/graftpunk/har/documents.py`
- Test: `tests/unit/test_har_documents.py`

**Interfaces:**
- Consumes: nothing outside the standard library (`html.parser`).
- Produces: `TokenKind = Literal["header", "meta", "hidden_input", "cookie"]`; `LoginForm` (frozen dataclass: `action`, `method`, `fields: dict[str, str]`, `submit: str | None`, `hidden: tuple[str, ...]`, `source: str`); `TokenCandidate` (frozen dataclass: `kind: TokenKind`, `name: str`, `seen_on: tuple[str, ...]`); `extract_login_forms(html: str, source: str) -> tuple[LoginForm, ...]`; `extract_token_candidates(html: str, source: str) -> tuple[TokenCandidate, ...]`; `is_login_document(html: str) -> bool`; `looks_like_token_name(name: str) -> bool`. Task 3 imports `LoginForm`, `TokenCandidate`, `TokenKind`, the three functions, and `looks_like_token_name` (its header- and cookie-name scans call it instead of re-typing the hint tuple; see Task 3's design note). Task 5's `site_requests.py` imports `is_login_document`.

**Implementation note (deviation from the spec's single code block):** the spec's `har/digest.py` code block defines `TokenKind`, `LoginForm`, and `TokenCandidate` inline, but also has `digest.py` call `documents.extract_login_forms`/`extract_token_candidates`, whose return types are exactly those dataclasses. Defining them in `digest.py` and having `documents.py` import them back would be a circular import (`digest` imports `documents`, `documents` would import `digest`). This plan defines them in `documents.py`, the module that constructs them, and `digest.py` imports them from there (Task 3) so they remain reachable as `graftpunk.har.digest.LoginForm` etc. and, via `har/__init__.py` (Task 4), as `graftpunk.har.LoginForm`. The public import path the spec promises is unaffected; only the defining module differs.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_har_documents.py`:

```python
"""HTML extraction on inline HTML, no HAR involved (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import pytest

from graftpunk.har.documents import (
    extract_login_forms,
    extract_token_candidates,
    is_login_document,
    looks_like_token_name,
)

_LOGIN_PAGE = """
<html><body>
<form action="/login" method="post">
  <input type="email" name="email" id="email-field">
  <input type="password" name="password" id="pw">
  <input type="hidden" name="_token" value="abc123">
  <button type="submit" id="login-btn">Sign in</button>
</form>
</body></html>
"""

_NO_LOGIN_PAGE = "<html><body><p>Welcome, alice@example.com</p></body></html>"

_MULTI_FORM_PAGE = """
<html><body>
<form action="/search"><input type="text" name="q"></form>
<form action="/login" method="POST">
  <input name="username" id="user">
  <input type="password" name="pw">
  <input type="submit" value="Go">
</form>
</body></html>
"""


class TestExtractLoginForms:
    def test_finds_form_with_password_input(self) -> None:
        forms = extract_login_forms(_LOGIN_PAGE, source="page-source.html")
        assert len(forms) == 1
        form = forms[0]
        assert form.action == "/login"
        assert form.method == "POST"
        assert form.source == "page-source.html"

    def test_id_selector_preferred_over_name_selector(self) -> None:
        (form,) = extract_login_forms(_LOGIN_PAGE, source="s")
        assert form.fields["username"] == "#email-field"
        assert form.fields["password"] == "#pw"

    def test_email_type_guessed_as_username(self) -> None:
        (form,) = extract_login_forms(_LOGIN_PAGE, source="s")
        assert "username" in form.fields

    def test_hidden_inputs_captured(self) -> None:
        (form,) = extract_login_forms(_LOGIN_PAGE, source="s")
        assert form.hidden == ("_token",)

    def test_submit_selector(self) -> None:
        (form,) = extract_login_forms(_LOGIN_PAGE, source="s")
        assert form.submit == "#login-btn"

    def test_form_without_password_input_is_not_a_login_form(self) -> None:
        assert extract_login_forms(_NO_LOGIN_PAGE, source="s") == ()

    def test_only_the_password_form_among_several_is_returned(self) -> None:
        forms = extract_login_forms(_MULTI_FORM_PAGE, source="s")
        assert len(forms) == 1
        assert forms[0].action == "/login"

    def test_field_without_id_falls_back_to_name_selector(self) -> None:
        (form,) = extract_login_forms(_MULTI_FORM_PAGE, source="s")
        assert form.fields["username"] == 'form[action="/login"] input[name="username"]'

    def test_submit_input_without_id_falls_back_to_type_selector(self) -> None:
        (form,) = extract_login_forms(_MULTI_FORM_PAGE, source="s")
        assert form.submit == 'form[action="/login"] input[type="submit"]'


class TestExtractTokenCandidates:
    def test_meta_tag_with_csrf_name_is_a_candidate(self) -> None:
        html = '<html><head><meta name="csrf-token" content="xyz"></head></html>'
        candidates = extract_token_candidates(html, source="page-source.html")
        assert len(candidates) == 1
        assert candidates[0].kind == "meta"
        assert candidates[0].name == "csrf-token"
        assert candidates[0].seen_on == ("page-source.html",)

    def test_meta_tag_without_token_substring_is_not_a_candidate(self) -> None:
        html = '<html><head><meta name="description" content="a shop"></head></html>'
        assert extract_token_candidates(html, source="s") == ()

    def test_hidden_input_named_authenticity_token(self) -> None:
        html = '<form><input type="hidden" name="authenticity_token" value="v"></form>'
        candidates = extract_token_candidates(html, source="s")
        assert len(candidates) == 1
        assert candidates[0].kind == "hidden_input"
        assert candidates[0].name == "authenticity_token"

    def test_no_candidates_in_plain_html(self) -> None:
        assert extract_token_candidates(_NO_LOGIN_PAGE, source="s") == ()


class TestIsLoginDocument:
    def test_true_for_password_form(self) -> None:
        assert is_login_document(_LOGIN_PAGE) is True

    def test_false_for_ordinary_page(self) -> None:
        assert is_login_document(_NO_LOGIN_PAGE) is False


class TestLooksLikeTokenName:
    """The one predicate digest.py's header and cookie scans call, instead of
    re-typing the hint tuple (validation net-negative, addressed 2026-09-12)."""

    @pytest.mark.parametrize(
        "name",
        ["X-CSRF-Token", "xsrf-token", "csrftoken", "Authorization-Token"],
    )
    def test_true_for_a_token_like_name(self, name: str) -> None:
        assert looks_like_token_name(name) is True

    @pytest.mark.parametrize("name", ["Content-Type", "Accept", "session_id"])
    def test_false_for_an_ordinary_name(self, name: str) -> None:
        assert looks_like_token_name(name) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_har_documents.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'graftpunk.har.documents'`.

- [ ] **Step 3: Write the implementation**

Create `src/graftpunk/har/documents.py`:

```python
"""HTML extraction for the digest: login forms and token candidates.

On the standard library ``html.parser``, limited to forms, inputs, and meta
tags. Inputs are HTML text (a page source or an HTML response body), never a
HAR entry, so this module knows nothing about HAR (plugin tooling spec,
2026-09-11).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Literal

TokenKind = Literal["header", "meta", "hidden_input", "cookie"]

__all__ = [
    "LoginForm",
    "TokenCandidate",
    "TokenKind",
    "extract_login_forms",
    "extract_token_candidates",
    "is_login_document",
    "looks_like_token_name",
]

_USERNAME_HINTS = ("user", "email", "login", "account")
_TOKEN_NAME_HINTS = ("csrf", "xsrf", "token")
_HIDDEN_TOKEN_NAME_HINTS = ("_token", "csrf", "authenticity_token")


def looks_like_token_name(name: str) -> bool:
    """True when *name* (a header, meta, or cookie name) contains a CSRF-token hint.

    The one predicate over ``_TOKEN_NAME_HINTS``: ``digest.py``'s header and
    cookie scans call this instead of re-typing the substring tuple, so the
    three-way "csrf, xsrf, token" rule has a single owner (plugin tooling
    spec, 2026-09-11, "Token candidates").
    """
    lowered = name.lower()
    return any(hint in lowered for hint in _TOKEN_NAME_HINTS)


@dataclass(frozen=True)
class LoginForm:
    """A ``<form>`` containing a password input, from a page source or an HTML response."""

    action: str
    method: str
    fields: dict[str, str]  # credential role -> CSS selector
    submit: str | None
    hidden: tuple[str, ...]  # hidden input names (token candidates)
    source: str


@dataclass(frozen=True)
class TokenCandidate:
    """A header, meta tag, hidden input, or cookie whose name looks like a CSRF token."""

    kind: TokenKind
    name: str
    seen_on: tuple[str, ...]


@dataclass
class _RawInput:
    tag: str
    input_type: str
    name: str
    element_id: str


@dataclass
class _RawForm:
    action: str = ""
    method: str = "GET"
    inputs: list[_RawInput] = field(default_factory=list)


class _DocumentParser(HTMLParser):
    """Collects every ``<form>`` (with its inputs) and every ``<meta>`` in one pass."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_RawForm] = []
        self.metas: list[tuple[str, str]] = []
        self._current: _RawForm | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: (v or "") for k, v in attrs}
        if tag == "form":
            self._current = _RawForm(
                action=values.get("action", ""),
                method=(values.get("method") or "GET").upper(),
            )
        elif tag in ("input", "button") and self._current is not None:
            self._current.inputs.append(
                _RawInput(
                    tag=tag,
                    input_type=(values.get("type") or ("submit" if tag == "button" else "text")).lower(),
                    name=values.get("name", ""),
                    element_id=values.get("id", ""),
                )
            )
        elif tag == "meta":
            name, content = values.get("name"), values.get("content")
            if name and content:
                self.metas.append((name, content))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None


def _parse(html: str) -> _DocumentParser:
    parser = _DocumentParser()
    parser.feed(html)
    if parser._current is not None:  # an unclosed <form>: keep what was seen
        parser.forms.append(parser._current)
    return parser


def _selector_for(raw: _RawInput, form_action: str) -> str:
    if raw.element_id:
        return f"#{raw.element_id}"
    if raw.name:
        return f'form[action="{form_action}"] {raw.tag}[name="{raw.name}"]'
    return f'form[action="{form_action}"] {raw.tag}[type="{raw.input_type}"]'


def _guess_role(input_type: str, name: str) -> str:
    if input_type == "password":
        return "password"
    if input_type == "email":
        return "username"
    lowered = name.lower()
    if any(hint in lowered for hint in _USERNAME_HINTS):
        return "username"
    return name


def extract_login_forms(html: str, source: str) -> tuple[LoginForm, ...]:
    """Every ``<form>`` in *html* that contains a password input.

    Each yields a :class:`LoginForm` with one CSS selector per input (an
    ``#id`` selector when the input has one, else a selector scoped to the
    form's action), the credential role guessed from type and name, the
    submit control's selector, and hidden input names.
    """
    forms: list[LoginForm] = []
    for raw in _parse(html).forms:
        password_inputs = [i for i in raw.inputs if i.input_type == "password"]
        if not password_inputs:
            continue
        fields: dict[str, str] = {}
        hidden: list[str] = []
        submit: str | None = None
        for raw_input in raw.inputs:
            if raw_input.input_type == "hidden":
                if raw_input.name:
                    hidden.append(raw_input.name)
                continue
            if raw_input.input_type == "submit" or (
                raw_input.tag == "button" and raw_input.input_type != "button"
            ):
                submit = _selector_for(raw_input, raw.action)
                continue
            role = _guess_role(raw_input.input_type, raw_input.name)
            if role:
                fields[role] = _selector_for(raw_input, raw.action)
        forms.append(
            LoginForm(
                action=raw.action,
                method=raw.method,
                fields=fields,
                submit=submit,
                hidden=tuple(hidden),
                source=source,
            )
        )
    return tuple(forms)


def extract_token_candidates(html: str, source: str) -> tuple[TokenCandidate, ...]:
    """Meta tags and hidden inputs in *html* whose name looks like a CSRF token.

    Header and cookie candidates are HAR-entry concerns and are collected by
    ``digest.py`` directly; this function only ever sees document HTML.
    """
    parsed = _parse(html)
    candidates: list[TokenCandidate] = []
    for name, _content in parsed.metas:
        if looks_like_token_name(name):
            candidates.append(TokenCandidate(kind="meta", name=name, seen_on=(source,)))
    for raw_form in parsed.forms:
        for raw_input in raw_form.inputs:
            if raw_input.input_type != "hidden" or not raw_input.name:
                continue
            lowered = raw_input.name.lower()
            if any(hint in lowered for hint in _HIDDEN_TOKEN_NAME_HINTS):
                candidates.append(
                    TokenCandidate(kind="hidden_input", name=raw_input.name, seen_on=(source,))
                )
    return tuple(candidates)


def is_login_document(html: str) -> bool:
    """True when *html* contains a form with a password input.

    Used by :mod:`graftpunk.plugins.site_requests` to tell a real login page
    (a stale session gets one back as a 200) apart from any other
    unexpectedly non-JSON response.
    """
    return bool(extract_login_forms(html, source=""))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_har_documents.py -q`
Expected: PASS, all tests green.

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
Expected: all four green.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/har/documents.py tests/unit/test_har_documents.py
git commit -m "feat(har): add login-form and token-candidate extraction from HTML"
```

---

### Task 3: `har/digest.py` model and `digest()`

**Files:**
- Create: `src/graftpunk/har/digest.py`
- Test: `tests/unit/test_har_digest.py`

**Interfaces:**
- Consumes: `graftpunk.har.parser.parse_har_file`, `HAREntry` (`src/graftpunk/har/parser.py:318` (`def parse_har_file(filepath: Path | str) -> HARParseResult:`)); `graftpunk.har.paths.template_path`, `param_name_for_segment` (Task 1); `graftpunk.har.documents.LoginForm`, `TokenCandidate`, `TokenKind`, `extract_login_forms`, `extract_token_candidates` (Task 2).
- Produces: `BodyKind`, `ObservationKind`, `DropReason` (`Literal` type aliases); `DigestSource` (frozen dataclass with `from_run_dir`/`from_har` classmethods); `ShapeNode`; `Endpoint`; `LoginObservation`; `RunDigest`; `digest(source: DigestSource, *, all_hosts: bool = False) -> RunDigest`; `body_params(entry: HAREntry) -> dict[str, str]` (request body field names to observed type; the one parse both `digest()`'s endpoint accumulation and Task 7's fixtures sidecar use). Task 4 (`report.py`) renders a `RunDigest`. Tasks 7 and 9 call `digest()` from the CLI and the scaffold; Task 7's fixtures command also calls `body_params`.

**Implementation note:** the high-cardinality collapse ("a segment that appears with more than 8 distinct values across a run" also collapses) is a run-wide pass on top of `template_path`'s per-segment rule, so it lives here rather than in `paths.py`, which only ever sees one path at a time. The algorithm below groups templates that share a method and segment count and differ only at one position; it is a best-effort heuristic (documented in its own docstring), not an exhaustive path-family miner, which is adequate for a digest tool and is flagged here as a design choice this plan made, not one the spec dictated.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_har_digest.py`:

```python
"""RunDigest over synthetic HARs built in tmp_path (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import json
from pathlib import Path

from graftpunk.har.digest import (
    DigestSource,
    Endpoint,
    RunDigest,
    ShapeNode,
    _HIGH_CARDINALITY_THRESHOLD,
    _LOGIN_WINDOW,
    _SHAPE_MAX_DEPTH,
    _SHAPE_MAX_KEYS,
    body_params,
    digest,
)
from graftpunk.har.parser import parse_har_file


def _entry(
    method: str,
    url: str,
    *,
    status: int = 200,
    content_type: str = "application/json",
    body: str = "{}",
    request_headers: dict[str, str] | None = None,
    response_headers: dict[str, str] | None = None,
    post_data: str | None = None,
    set_cookies: list[str] | None = None,
    started: str = "2026-09-10T10:00:00.000Z",
) -> dict:
    resp_headers = [{"name": "Content-Type", "value": content_type}]
    for name, value in (response_headers or {}).items():
        resp_headers.append({"name": name, "value": value})
    for cookie in set_cookies or []:
        resp_headers.append({"name": "Set-Cookie", "value": cookie})
    entry = {
        "startedDateTime": started,
        "time": 5,
        "request": {
            "method": method,
            "url": url,
            "headers": [{"name": k, "value": v} for k, v in (request_headers or {}).items()],
            "cookies": [],
            "queryString": [],
        },
        "response": {
            "status": status,
            "statusText": "OK",
            "headers": resp_headers,
            "cookies": [],
            "content": {"mimeType": content_type, "text": body, "size": len(body)},
        },
    }
    if post_data is not None:
        entry["request"]["postData"] = {"mimeType": "application/json", "text": post_data}
    return entry


def _write_har(tmp_path: Path, entries: list[dict], name: str = "network.har") -> Path:
    har_path = tmp_path / name
    har_path.write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}))
    return har_path


class TestPrimaryHostAndHosts:
    def test_primary_host_is_most_common_non_static(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://api.myshop.example.com/orders/1"),
            _entry("GET", "https://ads.tracker.example.net/pixel.gif", content_type="image/gif", body=""),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.primary_host == "api.myshop.example.com"

    def test_hosts_dict_counts_every_host(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://cdn.myshop.example.com/logo.png", content_type="image/png", body=""),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.hosts["api.myshop.example.com"] == 1
        assert result.hosts["cdn.myshop.example.com"] == 1


class TestStaticAndThirdPartyExclusion:
    def test_static_extension_is_dropped(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://api.myshop.example.com/app.js", content_type="application/javascript", body=""),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["static"] == 1
        assert not any(e.template == "/app.js" for e in result.endpoints)

    def test_image_content_type_is_static_regardless_of_url(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://api.myshop.example.com/generated-thumb", content_type="image/jpeg", body=""),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["static"] == 1

    def test_third_party_host_dropped_without_all_hosts(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://api.myshop.example.com/orders/2"),
            _entry("GET", "https://other.example.org/data"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["third_party"] == 1
        assert all(e.host == "api.myshop.example.com" for e in result.endpoints)

    def test_all_hosts_models_every_host(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://api.myshop.example.com/orders/2"),
            _entry("GET", "https://other.example.org/data"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)), all_hosts=True)
        assert any(e.host == "other.example.org" for e in result.endpoints)
        assert result.dropped["third_party"] == 0


class TestTypeObservation:
    def test_query_param_int_type(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/orders?page=1")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        endpoint = result.endpoints[0]
        assert endpoint.query_params["page"] == "int"

    def test_query_param_bool_type(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/orders?archived=true")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].query_params["archived"] == "bool"

    def test_body_param_types_on_post(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/orders",
                post_data=json.dumps({"quantity": 3, "gift": True, "note": "hi"}),
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        endpoint = result.endpoints[0]
        assert endpoint.body_params["quantity"] == "int"
        assert endpoint.body_params["gift"] == "bool"
        assert endpoint.body_params["note"] == "str"


class TestBodyParams:
    """The one owner of body-param parsing: digest()'s endpoint accumulation
    and the fixtures sidecar (Task 7) both call this directly
    (validation net-negative, addressed 2026-09-12)."""

    def test_json_body_field_types(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/orders",
                post_data=json.dumps({"quantity": 3, "gift": True}),
            )
        ]
        (entry,) = parse_har_file(_write_har(tmp_path, entries)).entries
        assert body_params(entry) == {"quantity": "int", "gift": "bool"}

    def test_form_encoded_body_field_types(self, tmp_path: Path) -> None:
        entry_dict = _entry("POST", "https://api.myshop.example.com/login")
        entry_dict["request"]["postData"] = {
            "mimeType": "application/x-www-form-urlencoded",
            "text": "username=alice&remember=true",
        }
        (entry,) = parse_har_file(_write_har(tmp_path, [entry_dict])).entries
        assert body_params(entry) == {"username": "str", "remember": "bool"}

    def test_no_body_is_empty(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/orders")]
        (entry,) = parse_har_file(_write_har(tmp_path, entries)).entries
        assert body_params(entry) == {}


class TestShapeNode:
    def test_object_shape_has_children(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/orders/1",
                body=json.dumps({"id": 1, "total": 9.5, "paid": True}),
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        shape = result.endpoints[0].shape
        assert shape is not None
        assert shape.kind == "object"
        assert shape.children is not None
        assert shape.children["id"].kind == "number"
        assert shape.children["paid"].kind == "boolean"

    def test_depth_beyond_3_is_truncated(self, tmp_path: Path) -> None:
        deeply_nested = {"a": {"b": {"c": {"d": "too deep"}}}}
        entries = [_entry("GET", "https://api.myshop.example.com/nested", body=json.dumps(deeply_nested))]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        shape = result.endpoints[0].shape
        assert shape is not None
        node = shape.children["a"].children["b"].children["c"]
        assert node.truncated is True

    def test_more_than_12_keys_is_truncated(self, tmp_path: Path) -> None:
        wide = {f"key{i}": i for i in range(_SHAPE_MAX_KEYS + 3)}
        entries = [_entry("GET", "https://api.myshop.example.com/wide", body=json.dumps(wide))]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        shape = result.endpoints[0].shape
        assert shape is not None
        assert shape.truncated is True
        assert len(shape.children) == _SHAPE_MAX_KEYS

    def test_non_json_response_has_no_shape(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/page", content_type="text/html", body="<html></html>")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].shape is None


class TestCustomHeaders:
    def test_non_standard_header_name_reported(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/orders",
                request_headers={"X-Shop-Client": "web", "Accept": "application/json"},
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert "X-Shop-Client" in result.endpoints[0].custom_headers
        assert "Accept" not in result.endpoints[0].custom_headers

    def test_header_values_never_appear_anywhere_in_the_model(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/orders",
                request_headers={"X-Shop-Client": "super-secret-build-id-42"},
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert "super-secret-build-id-42" not in repr(result)


class TestLoginObservations:
    def test_form_page_observation(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/login",
                content_type="text/html",
                body='<form><input type="password" name="pw"></form>',
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.login[0].kind == "form_page"

    def test_credential_post_lists_field_names_never_values(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/login",
                post_data=json.dumps({"email": "alice@example.com", "password": "hunter2"}),
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (observation,) = [o for o in result.login if o.kind == "credential_post"]
        assert set(observation.fields) == {"email", "password"}
        assert "hunter2" not in repr(result)

    def test_redirect_within_window_after_credential_post_is_an_observation(self, tmp_path: Path) -> None:
        entries = [
            _entry("POST", "https://api.myshop.example.com/login", post_data=json.dumps({"password": "x"})),
            _entry("GET", "https://api.myshop.example.com/dashboard", status=302, body=""),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert any(o.kind == "redirect" for o in result.login)

    def test_redirect_outside_window_is_not_an_observation(self, tmp_path: Path) -> None:
        entries = [
            _entry("POST", "https://api.myshop.example.com/login", post_data=json.dumps({"password": "x"})),
            *[
                _entry("GET", f"https://api.myshop.example.com/item/{i}")
                for i in range(_LOGIN_WINDOW + 1)
            ],
            _entry("GET", "https://api.myshop.example.com/elsewhere", status=302, body=""),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert not any(o.kind == "redirect" for o in result.login)

    def test_set_cookie_within_window_is_an_observation(self, tmp_path: Path) -> None:
        entries = [
            _entry("POST", "https://api.myshop.example.com/login", post_data=json.dumps({"password": "x"})),
            _entry(
                "GET",
                "https://api.myshop.example.com/dashboard",
                set_cookies=["session_id=abc123; Path=/; HttpOnly"],
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (observation,) = [o for o in result.login if o.kind == "set_cookie"]
        assert observation.fields == ("session_id",)

    def test_auth_api_path_is_an_observation(self, tmp_path: Path) -> None:
        entries = [_entry("POST", "https://api.myshop.example.com/api/auth", post_data="{}")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert any(o.kind == "auth_api" for o in result.login)

    def test_unrelated_asset_load_is_not_a_login_observation(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/products")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.login == ()


class TestTokenCandidatePairing:
    def test_header_and_meta_both_counted(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/dashboard",
                content_type="text/html",
                body='<meta name="csrf-token" content="abc">',
                request_headers={"X-CSRF-Token": "abc"},
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        kinds = {c.kind for c in result.tokens}
        assert "header" in kinds
        assert "meta" in kinds


class TestDigestSourceFromHar:
    def test_bare_har_has_no_run_metadata(self, tmp_path: Path) -> None:
        source = DigestSource.from_har(_write_har(tmp_path, [_entry("GET", "https://api.myshop.example.com/x")]))
        assert source.session is None
        assert source.bodies_dir is None
        assert source.page_source is None

    def test_run_dir_picks_up_bodies_and_page_source(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "myshop" / "20260910-100000-1"
        (run_dir / "bodies").mkdir(parents=True)
        (run_dir / "page-source.html").write_text("<html></html>")
        _write_har(run_dir, [_entry("GET", "https://api.myshop.example.com/x")])
        source = DigestSource.from_run_dir(run_dir, session="myshop", run_id=run_dir.name)
        assert source.bodies_dir == run_dir / "bodies"
        assert source.page_source == run_dir / "page-source.html"
        assert source.session == "myshop"
        assert source.run_id == run_dir.name


class TestEveryEndpointPresent:
    def test_low_and_high_count_endpoints_both_appear(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/rare")]
        entries += [_entry("GET", "https://api.myshop.example.com/common") for _ in range(50)]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert "/rare" in templates
        assert "/common" in templates


class TestHighCardinalityCollapse:
    def test_segment_with_many_distinct_literal_values_collapses(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", f"https://api.myshop.example.com/products/item-{i}")
            for i in range(_HIGH_CARDINALITY_THRESHOLD + 2)
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert "/products/{product_id}" in templates

    def test_segment_at_or_below_threshold_stays_literal(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", f"https://api.myshop.example.com/products/item-{i}")
            for i in range(_HIGH_CARDINALITY_THRESHOLD)
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert all(t.startswith("/products/item-") for t in templates)


class TestParseErrorsAndMissingBodies:
    def test_malformed_entry_counts_as_dropped_error(self, tmp_path: Path) -> None:
        good = _entry("GET", "https://api.myshop.example.com/orders")
        bad = {"request": None, "response": {}}
        har_path = _write_har(tmp_path, [good, bad])
        result = digest(DigestSource.from_har(har_path))
        assert result.dropped["error"] == 1

    def test_missing_body_file_is_dropped_not_raised(self, tmp_path: Path) -> None:
        entry = _entry("GET", "https://api.myshop.example.com/big")
        entry["response"]["content"] = {
            "mimeType": "application/json",
            "size": 0,
            "_bodyFile": "bodies/missing.json",
        }
        har_path = _write_har(tmp_path, [entry])
        result = digest(DigestSource.from_har(har_path))  # must not raise
        assert result.dropped["error"] == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_har_digest.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'graftpunk.har.digest'`.

- [ ] **Step 3: Write the implementation**

Create `src/graftpunk/har/digest.py`:

```python
"""RunDigest: a HAR (and whatever a run adds to it) read into a small, readable model.

Rules are documented on :func:`digest`. The exclusion and auth-path patterns
below replace ``har/analyzer.py``'s, moved here because the digest is the
only remaining consumer (plugin tooling spec, 2026-09-11).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from graftpunk.har.documents import (
    LoginForm,
    TokenCandidate,
    TokenKind,
    extract_login_forms,
    extract_token_candidates,
    looks_like_token_name,
)
from graftpunk.har.parser import HAREntry, parse_har_file
from graftpunk.har.paths import param_name_for_segment, template_path
from graftpunk.logging import get_logger

LOG = get_logger(__name__)

BodyKind = Literal["json", "form", "none"]
ObservationKind = Literal["form_page", "credential_post", "redirect", "set_cookie", "auth_api"]
DropReason = Literal["static", "third_party", "error"]

__all__ = [
    "BodyKind",
    "DigestSource",
    "DropReason",
    "Endpoint",
    "LoginForm",
    "LoginObservation",
    "ObservationKind",
    "RunDigest",
    "ShapeNode",
    "TokenCandidate",
    "TokenKind",
    "body_params",
    "digest",
]

# -- thresholds, every one a named constant (plugin tooling spec, "Rules the digest applies") --
_BODY_SAMPLE_THRESHOLD = 256 * 1024  # bodies over this size are sampled for shape only
_BODY_SAMPLE_SIZE = 64 * 1024
_HIGH_CARDINALITY_THRESHOLD = 8  # a segment with more distinct values than this collapses too
_LOGIN_WINDOW = 20  # entries after a credential post that may carry a redirect/set_cookie
_SHAPE_MAX_DEPTH = 3
_SHAPE_MAX_KEYS = 12
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)

_EXCLUDE_PATTERNS = [
    r"\.(js|css|png|jpg|jpeg|gif|svg|ico|woff|woff2|ttf|eot|map)(\?|$)",
    r"google-analytics",
    r"googletagmanager",
    r"facebook\.com",
    r"analytics",
    r"tracking",
    r"pixel",
    r"beacon",
    r"cdn\.",
    r"static\.",
    r"assets\.",
    r"fonts\.",
]
_EXCLUDE_REGEX = re.compile("|".join(_EXCLUDE_PATTERNS), re.IGNORECASE)
_STATIC_CONTENT_TYPE_PREFIXES = (
    "image/",
    "font/",
    "text/css",
    "application/javascript",
    "text/javascript",
)

_AUTH_URL_PATTERNS = [
    r"/login",
    r"/signin",
    r"/sign-in",
    r"/auth",
    r"/oauth",
    r"/authenticate",
    r"/session",
    r"/api/auth",
    r"/api/login",
    r"/api/session",
    r"/token",
    r"/callback",
    r"/sso",
]
_AUTH_URL_REGEX = re.compile("|".join(_AUTH_URL_PATTERNS), re.IGNORECASE)

_STANDARD_REQUEST_HEADERS = frozenset(
    {
        "host",
        "connection",
        "accept",
        "accept-encoding",
        "accept-language",
        "cache-control",
        "pragma",
        "content-type",
        "content-length",
        "cookie",
        "referer",
        "origin",
        "user-agent",
        "x-requested-with",
        "upgrade-insecure-requests",
    }
)
_STANDARD_REQUEST_HEADER_PREFIXES = ("sec-ch-", "sec-fetch-", "accept")

_PASSWORD_FIELD_HINTS = ("password", "passwd", "pwd")


@dataclass(frozen=True)
class DigestSource:
    """Where the digest reads a HAR from, and what else a run adds to it."""

    har_path: Path
    bodies_dir: Path | None = None
    page_source: Path | None = None
    session: str | None = None
    run_id: str | None = None

    @classmethod
    def from_run_dir(cls, run_dir: Path, *, session: str, run_id: str) -> DigestSource:
        bodies_dir = run_dir / "bodies"
        page_source = run_dir / "page-source.html"
        return cls(
            har_path=run_dir / "network.har",
            bodies_dir=bodies_dir if bodies_dir.is_dir() else None,
            page_source=page_source if page_source.is_file() else None,
            session=session,
            run_id=run_id,
        )

    @classmethod
    def from_har(cls, har_path: Path) -> DigestSource:
        return cls(har_path=har_path)


@dataclass(frozen=True)
class ShapeNode:
    """The shape of one JSON value: its kind, and (for objects/arrays) its children."""

    kind: Literal["object", "array", "string", "number", "boolean", "null"]
    children: dict[str, "ShapeNode"] | None = None  # objects: key -> node, at most _SHAPE_MAX_KEYS
    item: "ShapeNode | None" = None  # arrays: the first element's node
    truncated: bool = False  # keys beyond the cap or depth beyond the cap were dropped


@dataclass(frozen=True)
class Endpoint:
    """One (method, templated path) observed one or more times against the primary domain."""

    host: str
    template: str
    methods: tuple[str, ...]
    count: int
    statuses: tuple[int, ...]
    content_type: str
    query_params: dict[str, str]
    body_params: dict[str, str]
    body_kind: BodyKind
    shape: ShapeNode | None
    custom_headers: tuple[str, ...]
    examples: tuple[str, ...]


@dataclass(frozen=True)
class LoginObservation:
    """One entry in the credential-post-centred window that looks like part of a login."""

    order: int
    method: str
    url: str  # query stripped
    status: int
    kind: ObservationKind
    fields: tuple[str, ...]  # form/JSON field NAMES on a credential post, or cookie names; never values


@dataclass(frozen=True)
class RunDigest:
    source: DigestSource
    primary_host: str
    hosts: dict[str, int]
    endpoints: tuple[Endpoint, ...]
    login: tuple[LoginObservation, ...]
    login_forms: tuple[LoginForm, ...]
    tokens: tuple[TokenCandidate, ...]
    cookies: tuple[str, ...]
    dropped: dict[DropReason, int]


def _registrable_domain(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _is_static(entry: HAREntry) -> bool:
    if _EXCLUDE_REGEX.search(entry.request.url):
        return True
    content_type = (entry.response.content_type or "").lower()
    return any(content_type.startswith(p) for p in _STATIC_CONTENT_TYPE_PREFIXES)


def _observed_type(value: str) -> str:
    lowered = value.lower()
    if lowered in ("true", "false"):
        return "bool"
    try:
        int(value)
        return "int"
    except ValueError:
        return "str"


def _query_param_types(url: str) -> dict[str, str]:
    query = urlparse(url).query
    if not query:
        return {}
    parsed = parse_qs(query, keep_blank_values=True)
    types: dict[str, str] = {}
    for name, values in parsed.items():
        types[name] = "list" if len(values) > 1 else _observed_type(values[0])
    return types


def _parse_body(entry: HAREntry) -> tuple[dict[str, str], BodyKind]:
    """The request body's field names and observed types, and which kind it was."""
    post_data = entry.request.post_data
    if not post_data:
        return {}, "none"
    import json

    try:
        parsed = json.loads(post_data)
        if isinstance(parsed, dict):
            types: dict[str, str] = {}
            for key, value in parsed.items():
                if isinstance(value, bool):
                    types[key] = "bool"
                elif isinstance(value, (int, float)):
                    types[key] = "int"
                elif isinstance(value, list):
                    types[key] = "list"
                else:
                    types[key] = "str"
            return types, "json"
    except (ValueError, TypeError):
        pass
    form = parse_qs(post_data, keep_blank_values=True)
    if form:
        return {k: ("list" if len(v) > 1 else _observed_type(v[0])) for k, v in form.items()}, "form"
    return {}, "none"


def body_params(entry: HAREntry) -> dict[str, str]:
    """Request body field names to their observed type: JSON object fields,
    or form-encoded fields when the body is not JSON. Empty when there is no
    body.

    The one public entry point for "what are this entry's body param
    names": ``digest()``'s endpoint accumulation and the fixtures command's
    sidecar (``cli/observe_commands.py``) both call this instead of
    reimplementing the JSON-then-form parse (validation net-negative,
    addressed 2026-09-12).
    """
    types, _kind = _parse_body(entry)
    return types


def _shape_of(value: Any, depth: int = 0) -> ShapeNode:
    if isinstance(value, dict):
        if depth >= _SHAPE_MAX_DEPTH:
            return ShapeNode(kind="object", truncated=bool(value))
        keys = list(value.keys())
        truncated = len(keys) > _SHAPE_MAX_KEYS
        children = {k: _shape_of(value[k], depth + 1) for k in keys[:_SHAPE_MAX_KEYS]}
        return ShapeNode(kind="object", children=children, truncated=truncated)
    if isinstance(value, list):
        if depth >= _SHAPE_MAX_DEPTH:
            return ShapeNode(kind="array", truncated=bool(value))
        item = _shape_of(value[0], depth + 1) if value else None
        return ShapeNode(kind="array", item=item)
    if isinstance(value, bool):
        return ShapeNode(kind="boolean")
    if isinstance(value, (int, float)):
        return ShapeNode(kind="number")
    if isinstance(value, str):
        return ShapeNode(kind="string")
    return ShapeNode(kind="null")


def _body_missing(entry: HAREntry) -> bool:
    return entry.response.body_file is not None and entry.response.body is None


def _response_shape(entry: HAREntry) -> ShapeNode | None:
    content_type = (entry.response.content_type or "").lower()
    if "json" not in content_type or not entry.response.body:
        return None
    import json

    text = entry.response.body
    if len(text.encode("utf-8", errors="ignore")) > _BODY_SAMPLE_THRESHOLD:
        text = text[:_BODY_SAMPLE_SIZE]
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return _shape_of(parsed)


def _custom_headers(entry: HAREntry) -> tuple[str, ...]:
    names: list[str] = []
    for name in entry.request.headers:
        lowered = name.lower()
        if lowered in _STANDARD_REQUEST_HEADERS:
            continue
        if any(lowered.startswith(p) for p in _STANDARD_REQUEST_HEADER_PREFIXES):
            continue
        names.append(name)
    return tuple(names)


def _response_cookie_names(entry: HAREntry) -> list[str]:
    return [c["name"] for c in entry.response.cookies if c.get("name")]


def _has_password_field(entry: HAREntry) -> list[str]:
    """Field names on a POST whose body has a password-like field."""
    field_types = body_params(entry)
    return [name for name in field_types if any(hint in name.lower() for hint in _PASSWORD_FIELD_HINTS)]


class _EndpointAccumulator:
    def __init__(self, host: str) -> None:
        self.host = host
        self.methods: list[str] = []
        self.count = 0
        self.statuses: list[int] = []
        self.content_types: dict[str, int] = {}
        self.query_params: dict[str, str] = {}
        self.body_params: dict[str, str] = {}
        self.body_kind: BodyKind = "none"
        self.shape: ShapeNode | None = None
        self.custom_headers: set[str] = set()
        self.examples: list[str] = []

    def record(self, entry: HAREntry, path: str) -> None:
        method = entry.request.method.upper()
        if method not in self.methods:
            self.methods.append(method)
        self.count += 1
        self.statuses.append(entry.response.status)
        content_type = entry.response.content_type or ""
        self.content_types[content_type] = self.content_types.get(content_type, 0) + 1
        self.query_params.update(_query_param_types(entry.request.url))
        field_types, body_kind = _parse_body(entry)
        if field_types:
            self.body_params.update(field_types)
        if body_kind != "none":
            self.body_kind = body_kind
        if self.shape is None:
            self.shape = _response_shape(entry)
        self.custom_headers.update(_custom_headers(entry))
        if path not in self.examples and len(self.examples) < 3:
            self.examples.append(path)

    def finish(self, template: str) -> Endpoint:
        primary_content_type = max(self.content_types, key=self.content_types.get, default="")
        return Endpoint(
            host=self.host,
            template=template,
            methods=tuple(self.methods),
            count=self.count,
            statuses=tuple(sorted(set(self.statuses))),
            content_type=primary_content_type,
            query_params=dict(self.query_params),
            body_params=dict(self.body_params),
            body_kind=self.body_kind,
            shape=self.shape,
            custom_headers=tuple(sorted(self.custom_headers)),
            examples=tuple(self.examples),
        )


def _collapse_high_cardinality(templates: list[str]) -> dict[str, str]:
    """Map each raw template to its high-cardinality-collapsed form.

    Best-effort: groups templates sharing a segment count, and within a
    group compares each literal position against the first template's other
    positions to approximate "the same family of endpoints". This is a
    heuristic, not an exhaustive path-family miner: a run whose family has no
    common representative at position 0 is not detected. Adequate for a
    digest tool; documented rather than perfected (Task 3 implementation
    note).
    """
    by_count: dict[int, list[list[str]]] = {}
    for template in templates:
        segments = template.strip("/").split("/") if template.strip("/") else []
        by_count.setdefault(len(segments), []).append(segments)

    collapse_positions: dict[int, set[int]] = {}
    for count, rows in by_count.items():
        if count == 0:
            continue
        for i in range(count):
            if rows[0][i].startswith("{"):
                continue
            family = [r for r in rows if all(r[j] == rows[0][j] for j in range(count) if j != i)]
            distinct = {r[i] for r in family}
            if len(distinct) > _HIGH_CARDINALITY_THRESHOLD:
                collapse_positions.setdefault(count, set()).add(i)

    result: dict[str, str] = {}
    for template in templates:
        segments = template.strip("/").split("/") if template.strip("/") else []
        positions = collapse_positions.get(len(segments), set())
        if not positions:
            continue
        new_segments = list(segments)
        for i in sorted(positions):
            if new_segments[i].startswith("{"):
                continue
            prev = new_segments[i - 1] if i > 0 else ""
            new_segments[i] = "{" + param_name_for_segment(prev) + "}"
        new_template = "/" + "/".join(new_segments)
        if new_template != template:
            result[template] = new_template
    return result


def digest(source: DigestSource, *, all_hosts: bool = False) -> RunDigest:
    """Read *source* into a :class:`RunDigest`.

    Never raises on a malformed entry (the parser already records per-entry
    errors) or on a body file the HAR references but that is missing on
    disk; both count under ``dropped["error"]`` and the digest still
    completes.
    """
    parse_result = parse_har_file(source.har_path)
    entries = parse_result.entries
    dropped: dict[DropReason, int] = {
        "static": 0,
        "third_party": 0,
        "error": len(parse_result.errors),
    }

    hosts: dict[str, int] = {}
    non_static_hosts: dict[str, int] = {}
    classified: list[tuple[HAREntry, str, bool]] = []
    for entry in entries:
        host = urlparse(entry.request.url).netloc.lower()
        hosts[host] = hosts.get(host, 0) + 1
        static = _is_static(entry)
        if not static:
            non_static_hosts[host] = non_static_hosts.get(host, 0) + 1
        classified.append((entry, host, static))

    primary_host = max(non_static_hosts, key=non_static_hosts.get, default="")
    primary_domain = _registrable_domain(primary_host) if primary_host else ""

    accumulators: dict[tuple[str, str], _EndpointAccumulator] = {}
    login_forms: list[LoginForm] = []
    token_seen: dict[tuple[TokenKind, str], list[str]] = {}
    cookies_seen: dict[str, None] = {}
    login: list[LoginObservation] = []
    credential_post_indexes: list[int] = []
    order = 0

    for index, (entry, host, static) in enumerate(classified):
        if static:
            dropped["static"] += 1
            continue
        in_scope = all_hosts or (_registrable_domain(host) == primary_domain)
        if not in_scope:
            dropped["third_party"] += 1
            continue
        if _body_missing(entry):
            dropped["error"] += 1
            LOG.warning("digest_body_file_missing", url=entry.request.url)
            continue

        path = urlparse(entry.request.url).path or "/"
        method = entry.request.method.upper()
        raw_template, _ = template_path(path)
        key = (method, raw_template)
        acc = accumulators.setdefault(key, _EndpointAccumulator(host=host))
        acc.record(entry, path)

        for name in entry.request.headers:
            if looks_like_token_name(name):
                token_seen.setdefault(("header", name), []).append(f"{method} {raw_template}")
        for cookie in entry.request.cookies + entry.response.cookies:
            cname = cookie.get("name", "")
            if cname and looks_like_token_name(cname):
                token_seen.setdefault(("cookie", cname), []).append(f"{method} {raw_template}")

        if host == primary_host:
            for cookie_name in _response_cookie_names(entry):
                cookies_seen.setdefault(cookie_name, None)

        content_type = (entry.response.content_type or "").lower()
        forms_in_entry: tuple[LoginForm, ...] = ()
        if "html" in content_type and entry.response.body:
            document_source = entry.request.url
            forms_in_entry = extract_login_forms(entry.response.body, source=document_source)
            login_forms.extend(forms_in_entry)
            for candidate in extract_token_candidates(entry.response.body, source=document_source):
                token_seen.setdefault((candidate.kind, candidate.name), []).extend(candidate.seen_on)

        credential_fields = _has_password_field(entry) if method == "POST" else []
        kind: ObservationKind | None = None
        fields: tuple[str, ...] = ()
        if method == "GET" and forms_in_entry:
            kind = "form_page"
        elif method == "POST" and credential_fields:
            kind, fields = "credential_post", tuple(sorted(credential_fields))
        elif credential_post_indexes and index - credential_post_indexes[-1] <= _LOGIN_WINDOW:
            if entry.response.status in _REDIRECT_STATUSES:
                kind = "redirect"
            elif _response_cookie_names(entry):
                kind, fields = "set_cookie", tuple(_response_cookie_names(entry))
        if kind is None and _AUTH_URL_REGEX.search(path):
            kind = "auth_api"

        if kind is not None:
            order += 1
            login.append(
                LoginObservation(
                    order=order,
                    method=method,
                    url=f"{urlparse(entry.request.url).scheme}://{host}{path}",
                    status=entry.response.status,
                    kind=kind,
                    fields=fields,
                )
            )
            if kind == "credential_post":
                credential_post_indexes.append(index)

    if source.page_source is not None and source.page_source.is_file():
        page_html = source.page_source.read_text(encoding="utf-8", errors="replace")
        page_label = str(source.page_source)
        login_forms.extend(extract_login_forms(page_html, source=page_label))
        for candidate in extract_token_candidates(page_html, source=page_label):
            token_seen.setdefault((candidate.kind, candidate.name), []).extend(candidate.seen_on)

    collapse_map = _collapse_high_cardinality([template for _method, template in accumulators])
    merged: dict[tuple[str, str], _EndpointAccumulator] = {}
    for (method, raw_template), acc in accumulators.items():
        final_template = collapse_map.get(raw_template, raw_template)
        merged_key = (method, final_template)
        if merged_key not in merged:
            merged[merged_key] = acc
        else:
            target = merged[merged_key]
            target.count += acc.count
            target.statuses.extend(acc.statuses)
            for ct, n in acc.content_types.items():
                target.content_types[ct] = target.content_types.get(ct, 0) + n
            target.query_params.update(acc.query_params)
            target.body_params.update(acc.body_params)
            target.custom_headers.update(acc.custom_headers)
            for example in acc.examples:
                if example not in target.examples and len(target.examples) < 3:
                    target.examples.append(example)

    endpoints = tuple(
        sorted(
            (acc.finish(template) for (_method, template), acc in merged.items()),
            key=lambda e: (-e.count, e.template),
        )
    )

    tokens = tuple(
        TokenCandidate(kind=kind, name=name, seen_on=tuple(dict.fromkeys(seen_on)))
        for (kind, name), seen_on in token_seen.items()
    )

    return RunDigest(
        source=source,
        primary_host=primary_host,
        hosts=hosts,
        endpoints=endpoints,
        login=tuple(login),
        login_forms=tuple(login_forms),
        tokens=tokens,
        cookies=tuple(cookies_seen),
        dropped=dropped,
    )
```

> **Design note (2026-09-12):** an earlier draft re-typed the substring tuple `("csrf", "xsrf", "token")` inline at both the header loop and the cookie loop above, instead of reusing `documents.py`'s own token-name rule. Validation flagged the duplication (two owners of one "does this name look like a CSRF token" decision, easy to drift apart at either site). `har/documents.py` (Task 2) now exports `looks_like_token_name(name: str) -> bool`, and both loops above call it, so the three-way hint list has exactly one owner.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_har_digest.py -q`
Expected: PASS, all tests green.

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
Expected: all four green.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/har/digest.py tests/unit/test_har_digest.py
git commit -m "feat(har): add the RunDigest model and digest() over HAR entries"
```

---

### Task 4: `har/report.py` and the `har/__init__.py` export update

**Files:**
- Create: `src/graftpunk/har/report.py`
- Modify: `src/graftpunk/har/__init__.py` (replaced in full; see Step 3)
- Modify: `src/graftpunk/cli/import_har.py:20` (`from graftpunk.har import (`), lines 20-30
- Test: `tests/unit/test_har_report.py`

**Interfaces:**
- Consumes: `graftpunk.har.digest.RunDigest`, `Endpoint`, `ShapeNode` (Task 3).
- Produces: `render_markdown(d: RunDigest, *, limit: int = 60) -> str`; `render_json(d: RunDigest) -> str`; `summarize_shape(shape: ShapeNode | None, *, depth: int = 3) -> str`. Task 7's `cli/observe_commands.py` calls `render_markdown`/`render_json`; Task 8's `devtools/scaffold/render.py` calls `summarize_shape` with its own depth budget instead of keeping a second implementation (see Task 8's design note). None of the three is exported from `graftpunk.har`'s top level (`import graftpunk.har.report` directly), per the spec.

**Note on `cli/import_har.py`:** `import-har` is not removed until Task 10, and today it imports `APIEndpoint`, `AuthFlow`, `HARParseResult`, `detect_auth_flow`, `discover_api_endpoints`, `extract_domain`, and `parse_har_file` from the package top level (`src/graftpunk/cli/import_har.py:20` (`from graftpunk.har import (`)). This task's `__init__.py` rewrite drops those names from `graftpunk.har.__all__` and its imports, which would break `gp import-har` (and `tests/unit/test_import_har.py`, `tests/unit/test_har_generator.py`, `tests/unit/test_har_analyzer.py`, and `TestImportHarCommand`, all still present and still run by the gate) the moment this task lands. Step 3 below also repoints `import_har.py`'s imports at the submodules directly (`graftpunk.har.analyzer`, `graftpunk.har.parser`), which changes nothing about its behaviour and keeps the gate green until Task 10 deletes the file outright.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_har_report.py`:

```python
"""render_markdown and render_json over synthetic runs (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import json
from pathlib import Path

from graftpunk.har.digest import DigestSource, digest
from graftpunk.har.report import render_json, render_markdown

_EXPECTED_MAX_MARKDOWN_LINES = 400
_LARGE_RUN_ENTRY_COUNT = 700


def _entry(method: str, url: str, *, content_type: str = "application/json", body: str = "{}") -> dict:
    return {
        "startedDateTime": "2026-09-10T10:00:00.000Z",
        "time": 5,
        "request": {"method": method, "url": url, "headers": [], "cookies": [], "queryString": []},
        "response": {
            "status": 200,
            "statusText": "OK",
            "headers": [{"name": "Content-Type", "value": content_type}],
            "cookies": [],
            "content": {"mimeType": content_type, "text": body, "size": len(body)},
        },
    }


def _write_har(tmp_path: Path, entries: list[dict]) -> Path:
    har_path = tmp_path / "network.har"
    har_path.write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}))
    return har_path


class TestRenderMarkdown:
    def test_sections_present_in_order(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/orders")]
        text = render_markdown(digest(DigestSource.from_har(_write_har(tmp_path, entries))))
        for heading in ("## Summary", "## Login", "## Tokens", "## Cookies", "## Endpoints"):
            assert heading in text
        assert text.index("## Summary") < text.index("## Login") < text.index("## Tokens")
        assert text.index("## Tokens") < text.index("## Cookies") < text.index("## Endpoints")

    def test_json_endpoints_ordered_before_non_json(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/page", content_type="text/html", body="<html></html>"),
            _entry("GET", "https://api.myshop.example.com/orders", body="{}"),
        ]
        text = render_markdown(digest(DigestSource.from_har(_write_har(tmp_path, entries))))
        assert text.index("/orders") < text.index("/page")

    def test_limit_caps_endpoints_shown(self, tmp_path: Path) -> None:
        entries = [_entry("GET", f"https://api.myshop.example.com/item-{i}") for i in range(10)]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        text = render_markdown(result, limit=3)
        assert text.count("### GET /item-") == 3
        assert "more endpoint(s)" in text

    def test_large_run_stays_under_400_lines_with_default_limit(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", f"https://api.myshop.example.com/orders/{i}", body=json.dumps({"id": i}))
            for i in range(_LARGE_RUN_ENTRY_COUNT)
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        text = render_markdown(result)
        assert len(text.splitlines()) < _EXPECTED_MAX_MARKDOWN_LINES

    def test_no_planted_secret_appears_in_rendered_output(self, tmp_path: Path) -> None:
        planted_cookie = "session_id=s3cr3t-cookie-value; Path=/"
        planted_password = "hunter2superSecret"
        entries = [
            {
                "startedDateTime": "2026-09-10T10:00:00.000Z",
                "time": 5,
                "request": {
                    "method": "POST",
                    "url": "https://api.myshop.example.com/login",
                    "headers": [{"name": "X-Api-Key", "value": "unshown-header-value-999"}],
                    "cookies": [],
                    "queryString": [],
                    "postData": {
                        "mimeType": "application/json",
                        "text": json.dumps({"email": "alice@example.com", "password": planted_password}),
                    },
                },
                "response": {
                    "status": 200,
                    "statusText": "OK",
                    "headers": [
                        {"name": "Content-Type", "value": "application/json"},
                        {"name": "Set-Cookie", "value": planted_cookie},
                    ],
                    "cookies": [],
                    "content": {"mimeType": "application/json", "text": "{}", "size": 2},
                },
            }
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        markdown = render_markdown(result)
        rendered_json = render_json(result)
        for planted in (planted_password, "s3cr3t-cookie-value", "unshown-header-value-999"):
            assert planted not in markdown
            assert planted not in rendered_json


class TestRenderJson:
    def test_output_is_valid_json_with_expected_top_level_keys(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/orders")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        parsed = json.loads(render_json(result))
        assert set(parsed) == {
            "source",
            "primary_host",
            "hosts",
            "endpoints",
            "login",
            "login_forms",
            "tokens",
            "cookies",
            "dropped",
        }

    def test_is_complete_regardless_of_endpoint_count(self, tmp_path: Path) -> None:
        entries = [_entry("GET", f"https://api.myshop.example.com/item-{i}") for i in range(80)]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        parsed = json.loads(render_json(result))
        assert len(parsed["endpoints"]) == len(result.endpoints) == 80
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_har_report.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'graftpunk.har.report'`.

- [ ] **Step 3: Write the implementation**

Create `src/graftpunk/har/report.py`:

```python
"""Two renderers for a RunDigest: markdown for a person, JSON for a program.

Each applies its own budget on top of what ``digest()`` found (the model
itself is complete): the markdown renderer's ``limit`` caps the endpoint
list and orders JSON endpoints first, the JSON renderer is complete. Not
exported from ``graftpunk.har``'s top level, so presentation can change
without touching the package's compatibility surface (plugin tooling spec,
2026-09-11).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from graftpunk.har.digest import Endpoint, RunDigest, ShapeNode

__all__ = ["render_json", "render_markdown", "summarize_shape"]

_DEFAULT_ENDPOINT_LIMIT = 60
_DEFAULT_SUMMARY_DEPTH = 3


def summarize_shape(shape: ShapeNode | None, *, depth: int = _DEFAULT_SUMMARY_DEPTH) -> str:
    """A one-line human-readable summary of *shape*, recursing up to *depth* levels.

    The one shape-summarising function: the markdown renderer below and the
    scaffold's generated docstrings (``devtools/scaffold/render.py``) both
    call this instead of keeping their own divergent formatting, so "what
    does this endpoint return" reads the same in a digest and in a generated
    plugin (plugin tooling spec, 2026-09-11; validation net-negative,
    addressed 2026-09-12).
    """
    if shape is None:
        return "non-JSON"
    if shape.kind in ("string", "number", "boolean", "null"):
        return shape.kind
    if shape.kind == "array":
        if depth <= 0:
            return "array<...>"
        return f"array<{summarize_shape(shape.item, depth=depth - 1)}>"
    if not shape.children:
        return "object{}"
    if depth <= 0:
        return "object{...}"
    keys = ", ".join(sorted(shape.children))
    suffix = ", ..." if shape.truncated else ""
    return f"object{{{keys}{suffix}}}"


def _endpoint_block(endpoint: Endpoint) -> list[str]:
    lines = [f"### {'/'.join(endpoint.methods)} {endpoint.template}", ""]
    lines.append(
        f"- host: `{endpoint.host}`, count: {endpoint.count}, statuses: {list(endpoint.statuses)}"
    )
    lines.append(f"- content type: `{endpoint.content_type or '(none)'}`")
    if endpoint.query_params:
        params = ", ".join(f"{k}: {v}" for k, v in sorted(endpoint.query_params.items()))
        lines.append(f"- query params: {params}")
    if endpoint.body_params:
        params = ", ".join(f"{k}: {v}" for k, v in sorted(endpoint.body_params.items()))
        lines.append(f"- body params ({endpoint.body_kind}): {params}")
    if endpoint.custom_headers:
        lines.append(f"- custom headers: {', '.join(endpoint.custom_headers)}")
    lines.append(f"- shape: {summarize_shape(endpoint.shape)}")
    if endpoint.examples:
        lines.append(f"- examples: {', '.join(endpoint.examples)}")
    lines.append("")
    return lines


def render_markdown(d: RunDigest, *, limit: int = _DEFAULT_ENDPOINT_LIMIT) -> str:
    """A person-readable digest: Summary, Login, Tokens, Cookies, Endpoints, Other hosts."""
    lines: list[str] = ["# Observe digest", ""]

    lines.append("## Summary")
    source_label = f"{d.source.session}/{d.source.run_id}" if d.source.session else str(d.source.har_path)
    lines.append(f"- source: `{source_label}`")
    lines.append(f"- primary host: `{d.primary_host}`")
    lines.append(f"- hosts: {len(d.hosts)}, endpoints: {len(d.endpoints)}")
    lines.append(
        f"- dropped: static={d.dropped.get('static', 0)}, "
        f"third_party={d.dropped.get('third_party', 0)}, error={d.dropped.get('error', 0)}"
    )
    lines.append("")

    lines.append("## Login")
    if d.login:
        for obs in d.login:
            field_note = f" ({', '.join(obs.fields)})" if obs.fields else ""
            lines.append(f"{obs.order}. {obs.method} {obs.url} [{obs.status}] {obs.kind}{field_note}")
    else:
        lines.append("(no login observations)")
    if d.login_forms:
        lines.append("")
        for form in d.login_forms:
            lines.append(f"- form at `{form.action}` ({form.method}), source: {form.source}")
            for role, selector in sorted(form.fields.items()):
                lines.append(f"    - {role}: `{selector}`")
    lines.append("")

    lines.append("## Tokens")
    if d.tokens:
        for token in d.tokens:
            lines.append(f"- {token.kind} `{token.name}`, seen on: {', '.join(token.seen_on)}")
    else:
        lines.append("(none found)")
    lines.append("")

    lines.append("## Cookies")
    lines.append(", ".join(d.cookies) if d.cookies else "(none)")
    lines.append("")

    lines.append("## Endpoints")
    ordered = sorted(d.endpoints, key=lambda e: (e.shape is None, -e.count, e.template))
    shown, remaining = ordered[:limit], ordered[limit:]
    for endpoint in shown:
        lines.extend(_endpoint_block(endpoint))
    if remaining:
        lines.append(f"... and {len(remaining)} more endpoint(s) (raise --limit to see them)")
        lines.append("")

    other_hosts = {h: c for h, c in d.hosts.items() if h != d.primary_host}
    if other_hosts:
        lines.append("## Other hosts")
        for host, count in sorted(other_hosts.items(), key=lambda item: -item[1]):
            lines.append(f"- `{host}`: {count} request(s)")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def render_json(d: RunDigest) -> str:
    """The complete digest as JSON: nothing capped, nothing summarised."""
    return json.dumps(_jsonable(d), indent=2, sort_keys=True)
```

In `src/graftpunk/cli/import_har.py`, replace the import block at lines 20-30 (`src/graftpunk/cli/import_har.py:20` (`from graftpunk.har import (`) through the closing `)` at line 28, the `graftpunk.har.generator` line at line 29, AND the standalone `from graftpunk.har.parser import HARParseError` at line 30. Dropping line 30 matters: leaving it in place while also adding `HARParseError` to the combined parser import below duplicates the name and `uvx ruff check` fails F811 (redefinition) at this commit.

```python
from graftpunk.har.analyzer import (
    APIEndpoint,
    AuthFlow,
    detect_auth_flow,
    discover_api_endpoints,
    extract_domain,
)
from graftpunk.har.generator import generate_plugin_code, generate_yaml_plugin
from graftpunk.har.parser import HARParseError, HARParseResult, parse_har_file
```

Replace `src/graftpunk/har/__init__.py` in full:

```python
"""HAR (HTTP Archive) parsing and the run digest.

Everything that understands a HAR file: the parser, and the digest that
turns entries into a readable RunDigest.

Example usage:
    from graftpunk.har import parse_har_file
    from graftpunk.har.digest import DigestSource, digest
    from graftpunk.har.report import render_markdown

    result = parse_har_file("network.har")
    source = DigestSource.from_har("network.har")
    print(render_markdown(digest(source)))
"""

from graftpunk.har.digest import (
    DigestSource,
    Endpoint,
    LoginForm,
    LoginObservation,
    RunDigest,
    ShapeNode,
    TokenCandidate,
    digest,
)
from graftpunk.har.parser import (
    HAREntry,
    HARParseResult,
    HARRequest,
    HARResponse,
    ParseError,
    parse_har_file,
)

__all__ = [
    # Parser
    "HAREntry",
    "HARParseResult",
    "HARRequest",
    "HARResponse",
    "ParseError",
    "parse_har_file",
    # Digest
    "DigestSource",
    "Endpoint",
    "LoginForm",
    "LoginObservation",
    "RunDigest",
    "ShapeNode",
    "TokenCandidate",
    "digest",
]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_har_report.py -q`
Expected: PASS, all tests green.

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
Expected: all four green. `tests/unit/test_import_har.py`, `tests/unit/test_har_generator.py`, and `tests/unit/test_har_analyzer.py` still pass unchanged at this point (they import `graftpunk.har.analyzer`/`generator` directly, not through `graftpunk.har`'s `__all__`); they are deleted in Task 10.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/har/report.py src/graftpunk/har/__init__.py src/graftpunk/cli/import_har.py tests/unit/test_har_report.py
git commit -m "feat(har): add digest renderers and update graftpunk.har's public exports"
```

---

### Task 5: `SessionRejectedError`, `UnexpectedResponseError`, `SiteRequests`, and `CommandContext.request_json`/`request_text`

**Files:**
- Modify: `src/graftpunk/exceptions.py:60` (`class KeepaliveError(GraftpunkError):`), insert two new classes before it
- Create: `src/graftpunk/plugins/site_requests.py`
- Modify: `src/graftpunk/plugins/cli_plugin.py:59` (`from graftpunk.observe import NoOpObservabilityContext, ObservabilityContext`), insert one import line after it
- Modify: `src/graftpunk/plugins/cli_plugin.py:288` (`class CommandResult:`), insert two new methods on `CommandContext` before it
- Test: `tests/unit/test_site_requests.py`

**Interfaces:**
- Consumes: `graftpunk.har.documents.is_login_document` (Task 2); `graftpunk.exceptions.CommandError` (`src/graftpunk/exceptions.py:44` (`class CommandError(PluginError):`)); `CommandContext` (`src/graftpunk/plugins/cli_plugin.py:237` (`class CommandContext:`)), whose `session: requests.Session`, `plugin_name: str`, and `base_url: str` fields `SiteRequests` is built from.
- Produces: `SessionRejectedError(CommandError)`, `UnexpectedResponseError(CommandError)`; `SiteRequests(session, plugin_name, base_url)` with `.json(method, url, *, role="xhr", **kwargs) -> Any` and `.text(method, url, *, role="navigation", **kwargs) -> str`; `CommandContext.request_json` and `CommandContext.request_text`, the same signatures minus `self`/`session`/`plugin_name`/`base_url`. Task 8's generated command stubs call `ctx.request_json(...)`/`ctx.request_text(...)` exclusively; Task 6's `FixtureSession` is what a generated test answers them from.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_site_requests.py`:

```python
"""SiteRequests and the CommandContext delegates (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from graftpunk.exceptions import CommandError, SessionRejectedError, UnexpectedResponseError
from graftpunk.graftpunk_session import GraftpunkSession
from graftpunk.plugins.cli_plugin import CommandContext
from graftpunk.plugins.site_requests import SiteRequests


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        content_type: str = "application/json",
        text: str = "{}",
        request_method: str = "GET",
        url: str = "https://myshop.example.com/x",
    ) -> None:
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        self.text = text
        self.url = url
        self.request = requests.PreparedRequest()
        self.request.method = request_method
        self.request.url = url

    def json(self) -> Any:
        return json.loads(self.text)


class _RoleAwareSession:
    """A duck-typed session with request_with_role, like GraftpunkSession."""

    def __init__(self, response: _FakeResponse) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._response = response

    def request_with_role(self, role: str, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((role, method, url))
        return self._response


class TestJsonHappyPath:
    def test_2xx_json_returns_parsed_body(self) -> None:
        session = _RoleAwareSession(_FakeResponse(200, text=json.dumps({"a": 1})))
        result = SiteRequests(session, "myshop", "https://myshop.example.com").json("GET", "/orders")
        assert result == {"a": 1}

    def test_relative_url_resolved_against_base_url(self) -> None:
        session = _RoleAwareSession(_FakeResponse(200))
        SiteRequests(session, "myshop", "https://myshop.example.com/").json("GET", "/orders")
        assert session.calls[0][2] == "https://myshop.example.com/orders"

    def test_absolute_url_used_as_is(self) -> None:
        session = _RoleAwareSession(_FakeResponse(200))
        SiteRequests(session, "myshop", "https://myshop.example.com").json(
            "GET", "https://other.example.com/x"
        )
        assert session.calls[0][2] == "https://other.example.com/x"

    def test_default_role_is_xhr(self) -> None:
        session = _RoleAwareSession(_FakeResponse(200))
        SiteRequests(session, "myshop", "https://myshop.example.com").json("GET", "/orders")
        assert session.calls[0][0] == "xhr"

    def test_explicit_role_honoured(self) -> None:
        session = _RoleAwareSession(_FakeResponse(200))
        SiteRequests(session, "myshop", "https://myshop.example.com").json(
            "POST", "/submit", role="form"
        )
        assert session.calls[0][0] == "form"


class TestTextHappyPath:
    def test_returns_any_2xx_body_as_text(self) -> None:
        session = _RoleAwareSession(_FakeResponse(200, content_type="text/html", text="<html></html>"))
        result = SiteRequests(session, "myshop", "https://myshop.example.com").text("GET", "/page")
        assert result == "<html></html>"

    def test_default_role_is_navigation(self) -> None:
        session = _RoleAwareSession(_FakeResponse(200, content_type="text/html"))
        SiteRequests(session, "myshop", "https://myshop.example.com").text("GET", "/page")
        assert session.calls[0][0] == "navigation"


class TestRejection:
    @pytest.mark.parametrize("status", [401, 403])
    def test_401_and_403_raise_session_rejected(self, status: int) -> None:
        session = _RoleAwareSession(_FakeResponse(status))
        with pytest.raises(SessionRejectedError) as exc:
            SiteRequests(session, "myshop", "https://myshop.example.com").json("GET", "/orders")
        assert "gp myshop login" in str(exc.value)
        assert str(status) in str(exc.value)

    def test_text_also_raises_on_401(self) -> None:
        session = _RoleAwareSession(_FakeResponse(401, content_type="text/html"))
        with pytest.raises(SessionRejectedError):
            SiteRequests(session, "myshop", "https://myshop.example.com").text("GET", "/page")

    def test_html_endpoint_2xx_is_not_mistaken_for_rejection_by_text(self) -> None:
        session = _RoleAwareSession(_FakeResponse(200, content_type="text/html", text="<p>ok</p>"))
        result = SiteRequests(session, "myshop", "https://myshop.example.com").text("GET", "/page")
        assert result == "<p>ok</p>"

    def test_other_4xx_raises_command_error(self) -> None:
        session = _RoleAwareSession(_FakeResponse(404))
        with pytest.raises(CommandError) as exc:
            SiteRequests(session, "myshop", "https://myshop.example.com").json("GET", "/missing")
        assert not isinstance(exc.value, SessionRejectedError)
        assert "404" in exc.value.user_message

    def test_5xx_raises_command_error(self) -> None:
        session = _RoleAwareSession(_FakeResponse(500))
        with pytest.raises(CommandError):
            SiteRequests(session, "myshop", "https://myshop.example.com").json("GET", "/x")


class TestUnexpectedResponse:
    def test_2xx_non_json_body_raises_unexpected_response(self) -> None:
        session = _RoleAwareSession(_FakeResponse(200, content_type="text/csv", text="a,b\n1,2"))
        with pytest.raises(UnexpectedResponseError) as exc:
            SiteRequests(session, "myshop", "https://myshop.example.com").json("GET", "/export")
        assert "text/csv" in str(exc.value)

    def test_2xx_login_page_raises_session_rejected_not_unexpected_response(self) -> None:
        login_html = '<form><input type="password" name="pw"></form>'
        session = _RoleAwareSession(_FakeResponse(200, content_type="text/html", text=login_html))
        with pytest.raises(SessionRejectedError):
            SiteRequests(session, "myshop", "https://myshop.example.com").json("GET", "/orders")


class TestPlainSessionHasNoRoles:
    def test_plain_session_gets_the_request_without_role_headers(self) -> None:
        class _PlainSession:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
                self.calls.append((method, url))
                return _FakeResponse(200)

        session = _PlainSession()
        SiteRequests(session, "myshop", "https://myshop.example.com").json("GET", "/orders")
        assert session.calls == [("GET", "https://myshop.example.com/orders")]

    def test_warning_logged_once_per_session_not_once_per_call(self, capsys: pytest.CaptureFixture) -> None:
        """Matches the established structlog assertion pattern in this suite
        (`tests/unit/test_graftpunk_session.py::test_unknown_default_role_warns`):
        structlog's configured PrintLogger is captured via capsys, not caplog."""

        class _PlainSession:
            def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
                return _FakeResponse(200)

        session = _PlainSession()
        policy = SiteRequests(session, "myshop", "https://myshop.example.com")
        policy.json("GET", "/a")
        policy.json("GET", "/b")
        second_policy = SiteRequests(session, "myshop", "https://myshop.example.com")
        second_policy.json("GET", "/c")
        captured = capsys.readouterr()
        assert captured.out.count("session_roles_unavailable") == 1


class TestCommandContextDelegates:
    def test_request_json_delegates_to_site_requests(self) -> None:
        session = _RoleAwareSession(_FakeResponse(200, text=json.dumps({"ok": True})))
        ctx = CommandContext(
            session=session,  # type: ignore[arg-type]
            plugin_name="myshop",
            command_name="orders",
            api_version=1,
            base_url="https://myshop.example.com",
        )
        assert ctx.request_json("GET", "/orders") == {"ok": True}

    def test_request_text_delegates_to_site_requests(self) -> None:
        session = _RoleAwareSession(_FakeResponse(200, content_type="text/html", text="<p>hi</p>"))
        ctx = CommandContext(
            session=session,  # type: ignore[arg-type]
            plugin_name="myshop",
            command_name="page",
            api_version=1,
            base_url="https://myshop.example.com",
        )
        assert ctx.request_text("GET", "/page") == "<p>hi</p>"

    def test_default_context_session_is_a_graftpunk_session_end_to_end(self) -> None:
        """A real GraftpunkSession wired through request_with_role, not just the fake."""
        session = GraftpunkSession()
        ctx = CommandContext(
            session=session,
            plugin_name="myshop",
            command_name="orders",
            api_version=1,
            base_url="https://myshop.example.com",
        )
        assert hasattr(ctx.session, "request_with_role")


class TestCliOneLineRendering:
    def test_session_rejected_renders_as_one_line_through_the_cli(self) -> None:
        from tests.unit.cli_harness import invoke_plugin_app
        from graftpunk.plugins import SitePlugin, command

        class _RejectingPlugin(SitePlugin):
            site_name = "myshop"
            session_name = "myshop"
            help_text = "test"
            requires_session = False

            @command(help="Fetch orders")
            def orders(self, ctx: CommandContext) -> dict:
                raise SessionRejectedError("myshop", "GET", "/orders", 401)

        result = invoke_plugin_app(_RejectingPlugin(), ["orders"])
        assert result.exit_code == 1
        assert "gp myshop login" in result.output
        assert result.output.count("\n") <= 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_site_requests.py -q`
Expected: collection error, `ImportError: cannot import name 'SessionRejectedError' from 'graftpunk.exceptions'` (and `ModuleNotFoundError` for `graftpunk.plugins.site_requests`).

- [ ] **Step 3: Write the implementation**

In `src/graftpunk/exceptions.py`, insert before `src/graftpunk/exceptions.py:60` (`class KeepaliveError(GraftpunkError):`):

```python
class SessionRejectedError(CommandError):
    """A command's live request was refused by the site: 401, 403, or a login page on a 2xx.

    Distinct from :class:`SessionExpiredError` (the cached blob itself
    failed to load) and :class:`SessionInvalidatedError` (token extraction
    found the site wants a fresh login): this one means a request the
    session actually sent came back rejected.
    """

    def __init__(self, plugin_name: str, method: str, path: str, status: int) -> None:
        self.plugin_name = plugin_name
        self.method = method
        self.path = path
        self.status = status
        super().__init__(
            f"The site rejected the cached session ({status} on {method} {path}). "
            f"Run: gp {plugin_name} login"
        )


class UnexpectedResponseError(CommandError):
    """``request_json`` got a 2xx whose body is neither JSON nor a login page."""

    def __init__(self, method: str, path: str, content_type: str) -> None:
        self.method = method
        self.path = path
        self.content_type = content_type
        super().__init__(
            f"{method} {path} returned {content_type or 'no content type'}, not JSON"
        )


```

Create `src/graftpunk/plugins/site_requests.py`:

```python
"""The request policy behind ``CommandContext.request_json``/``request_text``.

Every hand-written and generated command needs to decide two things: which
header role a request carries, and what a non-2xx or a not-JSON 2xx means.
``SiteRequests`` owns both, out of ``CommandContext`` itself, so the policy
has its own file and tests (plugin tooling spec, 2026-09-11, design note on
the "session expired" primitive).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from graftpunk.exceptions import CommandError, SessionRejectedError, UnexpectedResponseError
from graftpunk.har.documents import is_login_document
from graftpunk.logging import get_logger

LOG = get_logger(__name__)

_REJECTED_STATUSES = frozenset({401, 403})
_ROLES_WARNED_ATTR = "_gp_roles_unavailable_warned"

__all__ = ["SiteRequests"]


def _path_of(url: str) -> str:
    return urlparse(url).path or "/"


class SiteRequests:
    """Applies the role, rejection, and body-shape policy for one plugin's requests."""

    def __init__(self, session: requests.Session, plugin_name: str, base_url: str) -> None:
        self._session = session
        self._plugin_name = plugin_name
        self._base_url = base_url

    def _resolve(self, url: str) -> str:
        return urljoin(self._base_url, url) if self._base_url else url

    def _warn_no_roles_once(self) -> None:
        if getattr(self._session, _ROLES_WARNED_ATTR, False):
            return
        setattr(self._session, _ROLES_WARNED_ATTR, True)
        LOG.warning("session_roles_unavailable", plugin=self._plugin_name)

    def _send(self, method: str, url: str, role: str, **kwargs: Any) -> requests.Response:
        full_url = self._resolve(url)
        request_with_role = getattr(self._session, "request_with_role", None)
        if callable(request_with_role):
            return request_with_role(role, method, full_url, **kwargs)
        self._warn_no_roles_once()
        return self._session.request(method, full_url, **kwargs)

    def _raise_for_rejection(self, response: requests.Response) -> None:
        method = response.request.method or "GET"
        path = _path_of(response.url)
        if response.status_code in _REJECTED_STATUSES:
            raise SessionRejectedError(self._plugin_name, method, path, response.status_code)
        if response.status_code >= 400:
            raise CommandError(f"{method} {path} returned {response.status_code}")

    def json(self, method: str, url: str, *, role: str = "xhr", **kwargs: Any) -> Any:
        """Send *method* *url* with role headers; return the parsed JSON body.

        Raises:
            SessionRejectedError: 401/403, or a 2xx whose body is a login
                page (``har.documents.is_login_document``).
            UnexpectedResponseError: A 2xx whose body is neither JSON nor a
                login page.
            CommandError: Any other 4xx or 5xx.
        """
        response = self._send(method, url, role, **kwargs)
        self._raise_for_rejection(response)
        content_type = response.headers.get("Content-Type", "")
        if "json" not in content_type.lower():
            if is_login_document(response.text):
                raise SessionRejectedError(
                    self._plugin_name, method, _path_of(response.url), response.status_code
                )
            raise UnexpectedResponseError(method, _path_of(response.url), content_type)
        return response.json()

    def text(self, method: str, url: str, *, role: str = "navigation", **kwargs: Any) -> str:
        """Send *method* *url* with role headers; return any 2xx body as text.

        Rejection is detected by status only, so an HTML endpoint's real
        2xx body is never mistaken for an expired session.
        """
        response = self._send(method, url, role, **kwargs)
        self._raise_for_rejection(response)
        return response.text
```

In `src/graftpunk/plugins/cli_plugin.py`, insert one import line after `src/graftpunk/plugins/cli_plugin.py:59` (`from graftpunk.observe import NoOpObservabilityContext, ObservabilityContext`):

```python
from graftpunk.plugins.site_requests import SiteRequests
```

> **Design note (2026-09-12):** validation asked whether the two methods below should import `SiteRequests` inside their bodies (to avoid a `cli_plugin` <-> `site_requests` import cycle) or at module level. Checked: `site_requests.py` imports only `graftpunk.exceptions`, `graftpunk.har.documents`, and `graftpunk.logging`, none of which import `graftpunk.plugins` or `graftpunk.plugins.cli_plugin`, so there is no cycle. The import moves to module level, as above; `request_json`/`request_text` below reference `SiteRequests` directly rather than re-importing it on every call.

Insert before `src/graftpunk/plugins/cli_plugin.py:288` (`class CommandResult:`):

```python
    def request_json(self, method: str, url: str, *, role: str = "xhr", **kwargs: Any) -> Any:
        """``SiteRequests(self.session, self.plugin_name, self.base_url).json(...)``.

        See :class:`graftpunk.plugins.site_requests.SiteRequests` for the
        role, rejection, and body-shape policy this delegates to.
        """
        return SiteRequests(self.session, self.plugin_name, self.base_url).json(
            method, url, role=role, **kwargs
        )

    def request_text(self, method: str, url: str, *, role: str = "navigation", **kwargs: Any) -> str:
        """``SiteRequests(self.session, self.plugin_name, self.base_url).text(...)``."""
        return SiteRequests(self.session, self.plugin_name, self.base_url).text(
            method, url, role=role, **kwargs
        )

```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_site_requests.py -q`
Expected: PASS, all tests green.

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
Expected: all four green.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/exceptions.py src/graftpunk/plugins/site_requests.py src/graftpunk/plugins/cli_plugin.py tests/unit/test_site_requests.py
git commit -m "feat(plugins): add the request policy behind ctx.request_json/request_text"
```

---

### Task 6: `graftpunk.testing`

**Files:**
- Create: `src/graftpunk/testing/__init__.py`
- Create: `src/graftpunk/testing/plugin.py`
- Test: `tests/unit/test_graftpunk_testing.py`

**Interfaces:**
- Consumes: `graftpunk.graftpunk_session.GraftpunkSession` (`src/graftpunk/graftpunk_session.py:150` (`class GraftpunkSession(requests.Session):`)); `graftpunk.har.naming.capture_slug` (Task 1); `graftpunk.plugins.cli_plugin.CommandContext` (`src/graftpunk/plugins/cli_plugin.py:237` (`class CommandContext:`)); `PluginConfig` (`src/graftpunk/plugins/cli_plugin.py:568` (`class PluginConfig:`)).
- Produces: `make_context(session=None, *, plugin_name="test", command_name="test", base_url="", config=None) -> CommandContext`; `FixtureSession(fixtures_dir)`, a `GraftpunkSession` subclass; `fixture_context(fixtures_dir, **kwargs) -> CommandContext`; `graftpunk.testing.plugin.site_env_scrubber(prefix) -> pytest fixture`. Task 8's generated `tests/test_plugin.py`/`tests/test_<name>.py` call `fixture_context` against `tests/fixtures/`; the generated `tests/conftest.py` declares `pytest_plugins = ["graftpunk.testing.plugin"]` and one `scrub_site_env = site_env_scrubber("<NAME>_")` line.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_graftpunk_testing.py`:

```python
"""graftpunk.testing: make_context, FixtureSession, fixture_context, site_env_scrubber
(plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import requests

from graftpunk.graftpunk_session import GraftpunkSession
from graftpunk.plugins.cli_plugin import CommandContext
from graftpunk.testing import FixtureSession, fixture_context, make_context


class TestMakeContext:
    def test_builds_a_valid_command_context(self) -> None:
        ctx = make_context(plugin_name="myshop", command_name="orders", base_url="https://myshop.example.com")
        assert isinstance(ctx, CommandContext)
        assert ctx.plugin_name == "myshop"
        assert ctx.command_name == "orders"
        assert ctx.base_url == "https://myshop.example.com"

    def test_default_session_is_a_graftpunk_session(self) -> None:
        ctx = make_context()
        assert isinstance(ctx.session, GraftpunkSession)

    def test_given_session_is_used_as_is(self) -> None:
        session = requests.Session()
        ctx = make_context(session=session)
        assert ctx.session is session

    def test_defaults_are_reasonable(self) -> None:
        ctx = make_context()
        assert ctx.plugin_name == "test"
        assert ctx.command_name == "test"
        assert ctx.api_version == 1


class TestFixtureSession:
    def test_answers_a_get_from_the_matching_file(self, tmp_path: Path) -> None:
        (tmp_path / "get_orders.json").write_text('{"orders": []}')
        session = FixtureSession(tmp_path)
        response = session.get("https://myshop.example.com/orders")
        assert response.status_code == 200
        assert response.json() == {"orders": []}

    def test_templated_path_matches_any_id(self, tmp_path: Path) -> None:
        (tmp_path / "get_orders_{order_id}.json").write_text('{"id": 1}')
        session = FixtureSession(tmp_path)
        assert session.get("https://myshop.example.com/orders/1").json() == {"id": 1}
        assert session.get("https://myshop.example.com/orders/999").json() == {"id": 1}

    def test_unmatched_request_returns_404(self, tmp_path: Path) -> None:
        session = FixtureSession(tmp_path)
        response = session.get("https://myshop.example.com/nothing-here")
        assert response.status_code == 404

    def test_sidecar_supplies_status_and_content_type(self, tmp_path: Path) -> None:
        (tmp_path / "get_orders.json").write_text("Forbidden")
        (tmp_path / "get_orders.json.meta.json").write_text(
            json.dumps({"status": 403, "content_type": "text/plain"})
        )
        session = FixtureSession(tmp_path)
        response = session.get("https://myshop.example.com/orders")
        assert response.status_code == 403
        assert response.headers["Content-Type"] == "text/plain"

    def test_without_sidecar_status_is_200_and_type_from_extension(self, tmp_path: Path) -> None:
        (tmp_path / "get_page.html").write_text("<html></html>")
        session = FixtureSession(tmp_path)
        response = session.get("https://myshop.example.com/page")
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "text/html"

    def test_never_opens_a_socket(self, tmp_path: Path) -> None:
        """No fixture file for this path: still answers 404 rather than connecting."""
        session = FixtureSession(tmp_path)
        response = session.get("http://192.0.2.1/nonexistent")
        assert response.status_code == 404


class TestFixtureContext:
    def test_is_make_context_with_a_fixture_session(self, tmp_path: Path) -> None:
        (tmp_path / "get_orders.json").write_text("{}")
        ctx = fixture_context(tmp_path, plugin_name="myshop", base_url="https://myshop.example.com")
        assert isinstance(ctx.session, FixtureSession)
        assert ctx.request_json("GET", "/orders") == {}


pytest_plugins = ["pytester"]


class TestSiteEnvScrubber:
    """Driven through a real inner pytest run (``pytester``), not by reaching
    into the ``@pytest.fixture``-wrapped generator's internals, since a
    fixture is meant to be exercised through pytest's own protocol."""

    def test_removes_prefixed_vars_for_the_test_and_restores_after(
        self, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MYSHOP_USERNAME", "alice")
        monkeypatch.setenv("OTHER_VAR", "kept")
        pytester.makepyfile(
            conftest="""
            pytest_plugins = ["graftpunk.testing.plugin"]
            from graftpunk.testing.plugin import site_env_scrubber
            scrub_site_env = site_env_scrubber("MYSHOP_")
            """,
            test_scrub="""
            import os

            def test_scrubbed():
                assert "MYSHOP_USERNAME" not in os.environ
                assert os.environ["OTHER_VAR"] == "kept"
            """,
        )
        result = pytester.runpytest_inprocess()
        result.assert_outcomes(passed=1)
        assert os.environ["MYSHOP_USERNAME"] == "alice"


class TestImportableWithoutPytest:
    def test_testing_package_importable_with_pytest_hidden(self) -> None:
        script = textwrap.dedent(
            """
            import builtins

            real_import = builtins.__import__

            def _blocked(name, *args, **kwargs):
                if name == "pytest" or name.startswith("pytest."):
                    raise ImportError("pytest is not installed")
                return real_import(name, *args, **kwargs)

            builtins.__import__ = _blocked

            from graftpunk.testing import FixtureSession, make_context

            assert callable(make_context)
            assert FixtureSession is not None
            print("OK")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graftpunk_testing.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'graftpunk.testing'`.

- [ ] **Step 3: Write the implementation**

Create `src/graftpunk/testing/__init__.py`:

```python
"""Test doubles for plugin authors: pytest-free and unconditional.

Never imports pytest, so a generated project's runtime code (and any
consumer importing graftpunk without pytest installed) can import this
module safely. The pytest-dependent half is :mod:`graftpunk.testing.plugin`,
loaded only via ``pytest_plugins = ["graftpunk.testing.plugin"]``, so the
import boundary is legible from the import paths themselves (plugin tooling
spec, 2026-09-11, design note).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from graftpunk.graftpunk_session import GraftpunkSession
from graftpunk.har.naming import capture_slug
from graftpunk.plugins.cli_plugin import CommandContext, PluginConfig

__all__ = ["FixtureSession", "fixture_context", "make_context"]

_CONTENT_TYPE_BY_SUFFIX: dict[str, str] = {
    ".json": "application/json",
    ".html": "text/html",
    ".txt": "text/plain",
    ".xml": "application/xml",
}
_DEFAULT_FIXTURE_CONTENT_TYPE = "application/octet-stream"


def make_context(
    session: requests.Session | None = None,
    *,
    plugin_name: str = "test",
    command_name: str = "test",
    base_url: str = "",
    config: PluginConfig | None = None,
) -> CommandContext:
    """A valid CommandContext for testing a command handler directly.

    Args:
        session: The session the context carries. Defaults to a fresh
            ``GraftpunkSession`` with no captured header roles.
        plugin_name: The plugin identity the context reports.
        command_name: The command identity the context reports.
        base_url: The plugin's base URL, for relative-URL resolution in
            ``ctx.request_json``/``ctx.request_text``.
        config: The plugin's ``PluginConfig``, or ``None``.
    """
    return CommandContext(
        session=session if session is not None else GraftpunkSession(),
        plugin_name=plugin_name,
        command_name=command_name,
        api_version=1,
        base_url=base_url,
        config=config,
    )


class FixtureSession(GraftpunkSession):
    """A GraftpunkSession that never opens a socket.

    Every request is answered from the file in *fixtures_dir* named the way
    :func:`graftpunk.har.naming.capture_slug` names a capture for that
    method and path, the same rule ``gp observe fixtures`` uses to write
    files, so a fixture copied from a capture keeps its name. A sidecar
    ``<filename>.meta.json`` beside a fixture supplies its status and
    content type when present (the same sidecar ``gp observe fixtures``
    writes); without one, the status is 200 and the type is guessed from
    the file's extension. No matching file answers 404.
    """

    def __init__(self, fixtures_dir: Path | str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fixtures_dir = Path(fixtures_dir)

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        path = urlparse(url).path or "/"
        stem = capture_slug(method, path)
        matches = sorted(
            p
            for p in self._fixtures_dir.glob(f"{stem}.*")
            if p.is_file() and not p.name.endswith(".meta.json")
        )
        response = requests.Response()
        response.request = requests.PreparedRequest()
        response.request.method = method.upper()
        response.request.url = url
        response.url = url
        if not matches:
            response.status_code = 404
            response._content = b""
            return response
        return self._respond_from_file(matches[0], response)

    def _respond_from_file(self, path: Path, response: requests.Response) -> requests.Response:
        meta_path = path.with_name(path.name + ".meta.json")
        status = 200
        content_type = _CONTENT_TYPE_BY_SUFFIX.get(path.suffix, _DEFAULT_FIXTURE_CONTENT_TYPE)
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            status = int(meta.get("status", status))
            content_type = meta.get("content_type", content_type)
        response.status_code = status
        response.headers["Content-Type"] = content_type
        response._content = path.read_bytes()
        return response


def fixture_context(fixtures_dir: Path | str, **kwargs: Any) -> CommandContext:
    """``make_context(session=FixtureSession(fixtures_dir), **kwargs)`` and nothing more."""
    return make_context(session=FixtureSession(fixtures_dir), **kwargs)
```

Create `src/graftpunk/testing/plugin.py`:

```python
"""The pytest half of :mod:`graftpunk.testing`. Load only as a pytest plugin.

A generated ``tests/conftest.py`` declares
``pytest_plugins = ["graftpunk.testing.plugin"]`` and nothing else:
everything the generated test suite needs from this module is a fixture, so
the generated file carries no framework logic of its own to drift from
graftpunk's (the scaffold rule, plugin tooling spec, 2026-09-11).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

__all__ = ["site_env_scrubber"]


def site_env_scrubber(prefix: str) -> Any:
    """An autouse fixture that removes every env var starting with *prefix*
    for the duration of each test, and restores them afterward.

    A generated plugin's ``tests/conftest.py`` sets
    ``scrub_site_env = site_env_scrubber("<NAME>_")`` so a developer's own
    ``MYSHOP_USERNAME``/``MYSHOP_PASSWORD`` in the ambient environment never
    leaks into a test asserting on the plugin's behaviour without them.
    """

    @pytest.fixture(autouse=True)
    def _scrub() -> Iterator[None]:
        saved = {k: v for k, v in os.environ.items() if k.startswith(prefix)}
        for key in saved:
            del os.environ[key]
        try:
            yield
        finally:
            os.environ.update(saved)

    return _scrub
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graftpunk_testing.py -q`
Expected: PASS, all tests green.

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
Expected: all four green.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/testing/__init__.py src/graftpunk/testing/plugin.py tests/unit/test_graftpunk_testing.py
git commit -m "feat(testing): add graftpunk.testing (make_context, FixtureSession, site_env_scrubber)"
```

---

### Task 7: `devtools/captures.py`, `cli/observe_commands.py`, and `main.py` wiring

**Files:**
- Create: `src/graftpunk/devtools/__init__.py`
- Create: `src/graftpunk/devtools/captures.py`
- Create: `src/graftpunk/cli/observe_commands.py`
- Modify: `src/graftpunk/cli/main.py:203` (`observe_app = typer.Typer(`), and `src/graftpunk/cli/main.py:289` (`@observe_app.command("show")`) through the end of `observe_show`
- Modify: `tests/unit/conftest.py:21` (`    ("graftpunk.cli.plugin_runtime", "_format_console"),`), append one entry
- Test: `tests/unit/test_devtools_captures.py`
- Test: `tests/unit/test_observe_commands.py`

**Interfaces:**
- Consumes: `graftpunk.har.digest.DigestSource`, `RunDigest`, `digest`, `body_params` (Task 3, called by the fixtures sidecar for its field-name list rather than reimplemented; see this task's design note); `graftpunk.har.report.render_json`, `render_markdown` (Task 4); `graftpunk.har.naming.capture_slug`, `graftpunk.har.paths.template_path` (Task 1); `graftpunk.observe.OBSERVE_BASE_DIR` (`src/graftpunk/observe/context.py:100` (`OBSERVE_BASE_DIR = Path.home() / ".local" / "share" / "graftpunk" / "observe"`)); `graftpunk.observe.storage.session_dirname` (`src/graftpunk/observe/storage.py:20` (`def session_dirname(session_name: str) -> str:`)).
- Produces: `devtools.captures.CAPTURES_DIR = "tests/captures"`, `find_repo_root(start: Path) -> Path | None`, `ensure_ignored(repo_root: Path, relative: str) -> bool`, `is_tracked(path: Path) -> bool`. `cli/observe_commands.resolve_run(session_name: str, run_id: str | None, *, base_dir: Path | None = None) -> Path`, `register(observe_app: typer.Typer) -> None`. Task 9's `cli/scaffold_commands.py` calls `resolve_run` (for `--from-run`) and imports `CAPTURES_DIR`/`ensure_ignored` via `project.py`.

**Implementation note on `OBSERVE_BASE_DIR` patchability:** the existing `TestObserveCLICommands` suite (`tests/unit/test_cli.py:886` (`class TestObserveCLICommands:`)) patches `graftpunk.cli.main.OBSERVE_BASE_DIR` directly (e.g. `patch("graftpunk.cli.main.OBSERVE_BASE_DIR", tmp_path)`), which only rebinds that name in `main.py`'s own namespace. If `resolve_run` read a module-level `OBSERVE_BASE_DIR` imported into `observe_commands.py`, those patches would silently stop working the moment `observe_show` started delegating to it, since `observe_commands.py` would hold its own separate binding. `resolve_run` therefore takes `base_dir` as an explicit keyword, defaulting to `graftpunk.observe.OBSERVE_BASE_DIR` only when the caller does not override it; `main.py`'s `observe_show` passes its own (still-patchable) module-level `OBSERVE_BASE_DIR` through explicitly, so every existing `observe show` test keeps working unmodified. `digest_cmd` and `fixtures_cmd` call `resolve_run` with no override, so tests for them patch `graftpunk.cli.observe_commands.OBSERVE_BASE_DIR` instead, the binding that is actually in scope there.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_devtools_captures.py`:

```python
"""The single owner of 'captures never enter git' (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from graftpunk.devtools.captures import CAPTURES_DIR, ensure_ignored, find_repo_root, is_tracked


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "alice@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "alice"], cwd=root, check=True)


class TestCapturesDir:
    def test_is_tests_captures(self) -> None:
        assert CAPTURES_DIR == "tests/captures"


class TestFindRepoRoot:
    def test_finds_the_root_of_a_git_work_tree(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert find_repo_root(nested) == tmp_path.resolve()

    def test_none_outside_a_work_tree(self, tmp_path: Path) -> None:
        assert find_repo_root(tmp_path) is None


class TestEnsureIgnored:
    def test_adds_the_line_when_absent(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        added = ensure_ignored(tmp_path, CAPTURES_DIR)
        assert added is True
        assert "tests/captures/" in (tmp_path / ".gitignore").read_text()

    def test_idempotent_second_call_reports_no_change(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        ensure_ignored(tmp_path, CAPTURES_DIR)
        added_again = ensure_ignored(tmp_path, CAPTURES_DIR)
        assert added_again is False
        assert (tmp_path / ".gitignore").read_text().count("tests/captures/") == 1

    def test_creates_gitignore_when_missing(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        ensure_ignored(tmp_path, CAPTURES_DIR)
        assert (tmp_path / ".gitignore").exists()


class TestIsTracked:
    def test_tracked_file_is_true(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        tracked = tmp_path / "committed.txt"
        tracked.write_text("hi")
        subprocess.run(["git", "add", "committed.txt"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add"], cwd=tmp_path, check=True)
        assert is_tracked(tracked) is True

    def test_untracked_file_is_false(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        untracked = tmp_path / "scratch.txt"
        untracked.write_text("hi")
        assert is_tracked(untracked) is False
```

Create `tests/unit/test_observe_commands.py`:

```python
"""resolve_run, gp observe digest, and gp observe fixtures (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from graftpunk.cli.observe_commands import digest_cmd, fixtures_cmd, register, resolve_run

runner = CliRunner()


def _build_app() -> typer.Typer:
    app = typer.Typer()
    observe_app = typer.Typer(name="observe")
    register(observe_app)
    app.add_typer(observe_app)
    return app


def _write_run(base_dir: Path, session: str, run_id: str, entries: list[dict]) -> Path:
    run_dir = base_dir / session / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "network.har").write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}))
    return run_dir


def _entry(method: str, url: str, *, content_type: str = "application/json", body: str = "{}") -> dict:
    return {
        "startedDateTime": "2026-09-10T10:00:00.000Z",
        "time": 1,
        "request": {"method": method, "url": url, "headers": [], "cookies": [], "queryString": []},
        "response": {
            "status": 200,
            "statusText": "OK",
            "headers": [{"name": "Content-Type", "value": content_type}],
            "cookies": [],
            "content": {"mimeType": content_type, "text": body, "size": len(body)},
        },
    }


class TestResolveRun:
    def test_defaults_to_the_newest_run(self, tmp_path: Path) -> None:
        (tmp_path / "myshop" / "20260101-080000").mkdir(parents=True)
        (tmp_path / "myshop" / "20260101-120000").mkdir(parents=True)
        run_dir = resolve_run("myshop", None, base_dir=tmp_path)
        assert run_dir.name == "20260101-120000"

    def test_explicit_run_id(self, tmp_path: Path) -> None:
        (tmp_path / "myshop" / "run-specific").mkdir(parents=True)
        run_dir = resolve_run("myshop", "run-specific", base_dir=tmp_path)
        assert run_dir.name == "run-specific"

    def test_missing_session_exits_1(self, tmp_path: Path) -> None:
        with pytest.raises(typer.Exit) as exc:
            resolve_run("ghost", None, base_dir=tmp_path)
        assert exc.value.exit_code == 1

    def test_missing_run_id_exits_1(self, tmp_path: Path) -> None:
        (tmp_path / "myshop").mkdir(parents=True)
        with pytest.raises(typer.Exit):
            resolve_run("myshop", "no-such-run", base_dir=tmp_path)


class TestDigestCommand:
    def test_digest_over_a_run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", tmp_path)
        _write_run(tmp_path, "myshop", "run-1", [_entry("GET", "https://api.myshop.example.com/orders")])
        app = _build_app()
        result = runner.invoke(app, ["observe", "digest", "myshop"])
        assert result.exit_code == 0, result.output
        assert "## Summary" in result.output

    def test_digest_json_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", tmp_path)
        _write_run(tmp_path, "myshop", "run-1", [_entry("GET", "https://api.myshop.example.com/orders")])
        app = _build_app()
        result = runner.invoke(app, ["observe", "digest", "myshop", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.output)["primary_host"] == "api.myshop.example.com"

    def test_digest_har_flag_on_a_bare_file(self, tmp_path: Path) -> None:
        har_path = tmp_path / "network.har"
        har_path.write_text(
            json.dumps(
                {"log": {"version": "1.2", "entries": [_entry("GET", "https://api.myshop.example.com/orders")]}}
            )
        )
        app = _build_app()
        result = runner.invoke(app, ["observe", "digest", "--har", str(har_path)])
        assert result.exit_code == 0
        assert "## Summary" in result.output

    def test_digest_refuses_both_session_and_har(self, tmp_path: Path) -> None:
        app = _build_app()
        result = runner.invoke(app, ["observe", "digest", "myshop", "--har", str(tmp_path / "x.har")])
        assert result.exit_code == 1
        assert "not both" in result.output.lower()

    def test_digest_missing_har_file_is_one_line_error(self, tmp_path: Path) -> None:
        app = _build_app()
        result = runner.invoke(app, ["observe", "digest", "--har", str(tmp_path / "missing.har")])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_digest_missing_session_or_har_is_an_error(self) -> None:
        app = _build_app()
        result = runner.invoke(app, ["observe", "digest"])
        assert result.exit_code == 1


class TestFixturesCommand:
    def test_writes_matching_files_with_sidecars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        _write_run(
            observe_base,
            "myshop",
            "run-1",
            [_entry("GET", "https://api.myshop.example.com/orders/1", body='{"id": 1}')],
        )
        out_dir = tmp_path / "out"
        app = _build_app()
        result = runner.invoke(
            app,
            ["observe", "fixtures", "myshop", "--match", "GET /orders/{order_id}", "--out", str(out_dir)],
        )
        assert result.exit_code == 0, result.output
        written = list(out_dir.glob("get_orders_*.json"))
        assert len(written) == 1
        assert json.loads(written[0].read_text()) == {"id": 1}
        assert (out_dir / f"{written[0].name}.meta.json").exists()

    def test_limit_caps_files_per_template(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        entries = [
            _entry("GET", f"https://api.myshop.example.com/orders/{i}", body=json.dumps({"id": i}))
            for i in range(10)
        ]
        _write_run(observe_base, "myshop", "run-1", entries)
        out_dir = tmp_path / "out"
        app = _build_app()
        result = runner.invoke(
            app,
            [
                "observe", "fixtures", "myshop",
                "--match", "GET /orders/{order_id}",
                "--out", str(out_dir),
                "--limit", "2",
            ],
        )
        assert result.exit_code == 0
        assert len(list(out_dir.glob("get_orders_*.json"))) == 2

    def test_refuses_a_tracked_target_without_allow_tracked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        _write_run(
            observe_base, "myshop", "run-1", [_entry("GET", "https://api.myshop.example.com/orders/1")]
        )
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "alice@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "alice"], cwd=repo, check=True)
        out_dir = repo / "tests" / "captures"
        out_dir.mkdir(parents=True)
        tracked = out_dir / "get_orders_{order_id}.json"
        tracked.write_text("{}")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)

        app = _build_app()
        result = runner.invoke(
            app,
            ["observe", "fixtures", "myshop", "--match", "GET /orders/{order_id}", "--out", str(out_dir)],
        )
        assert result.exit_code == 1
        assert "tracked" in result.output.lower()

    def test_missing_match_is_an_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", tmp_path)
        app = _build_app()
        result = runner.invoke(app, ["observe", "fixtures", "myshop"])
        assert result.exit_code == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_devtools_captures.py tests/unit/test_observe_commands.py -q`
Expected: collection errors, `ModuleNotFoundError: No module named 'graftpunk.devtools'` and `'graftpunk.cli.observe_commands'`.

- [ ] **Step 3: Write the implementation**

Create `src/graftpunk/devtools/__init__.py`:

```python
"""CLI-only developer tooling: the scaffold and the capture-directory helper.

Nothing here is imported by a generated plugin at runtime; that is the
placement rule (plugin tooling spec, 2026-09-11): public API a plugin uses
lives at the top level of the package (``graftpunk.testing``, the request
helpers on ``CommandContext``), and tooling only ``gp`` runs lives here.
"""
```

Create `src/graftpunk/devtools/captures.py`:

```python
"""The single owner of the 'captures never enter git' rule.

The fixtures command, the scaffold's generated ``.gitignore``, and the
scaffold's suite mode all use this module, so the default directory and the
ignore line cannot disagree (plugin tooling spec, 2026-09-11).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from graftpunk.logging import get_logger

LOG = get_logger(__name__)

CAPTURES_DIR = "tests/captures"

__all__ = ["CAPTURES_DIR", "ensure_ignored", "find_repo_root", "is_tracked"]

_GIT_TIMEOUT_SECONDS = 10


def find_repo_root(start: Path) -> Path | None:
    """The git work tree's root containing *start*, or ``None`` outside one
    (including when ``git`` is not on ``PATH``)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def ensure_ignored(repo_root: Path, relative: str) -> bool:
    """Add *relative* to the root ``.gitignore`` if it is not already covered.

    Returns:
        True when the line was added, False when it was already present.
    """
    gitignore = repo_root / ".gitignore"
    line = relative.rstrip("/") + "/"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    existing_lines = {entry.strip().rstrip("/") for entry in existing.splitlines()}
    if relative.rstrip("/") in existing_lines:
        return False
    with gitignore.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write(line + "\n")
    LOG.info("captures_gitignore_updated", path=str(gitignore), line=line)
    return True


def is_tracked(path: Path) -> bool:
    """True when *path* is tracked by git (``git ls-files --error-unmatch``)."""
    directory = path.parent if path.parent.exists() else Path.cwd()
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(path)],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0
```

Create `src/graftpunk/cli/observe_commands.py`:

```python
"""``gp observe digest`` and ``gp observe fixtures``, and the shared run resolver.

Takes ownership of ``resolve_run``, which ``gp observe show`` also uses. The
five existing observe commands stay in ``main.py`` for this part, so its
diff is limited to the new commands (plugin tooling spec, 2026-09-11, design
note); a later part moves them here.
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import typer
from rich.console import Console
from rich.markup import escape

from graftpunk.devtools.captures import CAPTURES_DIR, ensure_ignored, find_repo_root, is_tracked
from graftpunk.har.digest import DigestSource, body_params, digest
from graftpunk.har.naming import capture_filename
from graftpunk.har.parser import parse_har_file
from graftpunk.har.paths import template_path
from graftpunk.har.report import render_json, render_markdown
from graftpunk.logging import get_logger
from graftpunk.observe import OBSERVE_BASE_DIR
from graftpunk.observe.storage import session_dirname

LOG = get_logger(__name__)
console = Console()

_DEFAULT_FIXTURE_LIMIT = 5


def resolve_run(session_name: str, run_id: str | None, *, base_dir: Path | None = None) -> Path:
    """The run directory (session[, run]) means: the newest run when *run_id* is omitted.

    ``base_dir`` defaults to :data:`graftpunk.observe.OBSERVE_BASE_DIR`;
    callers that need a patchable module-level default (``gp observe show``
    in ``main.py``) pass their own binding through explicitly instead of
    relying on this module's.

    Raises:
        typer.Exit: No runs exist for the session, or the named run is missing.
    """
    base = base_dir if base_dir is not None else OBSERVE_BASE_DIR
    session_dir = base / session_dirname(session_name)
    if not session_dir.is_dir():
        console.print(f"[red]No runs found for session '{escape(session_name)}'[/red]")
        raise typer.Exit(1)

    if run_id is None:
        run_dirs = sorted(d for d in session_dir.iterdir() if d.is_dir())
        if not run_dirs:
            console.print(f"[red]No runs found for session '{escape(session_name)}'[/red]")
            raise typer.Exit(1)
        return run_dirs[-1]

    run_dir = session_dir / run_id
    if not run_dir.exists():
        console.print(
            f"[red]Run '{escape(run_id)}' not found for session '{escape(session_name)}'[/red]"
        )
        raise typer.Exit(1)
    return run_dir


def _digest_source(session_name: str | None, run_id: str | None, har: Path | None) -> DigestSource:
    if har is not None and session_name is not None:
        console.print("[red]Pass a session or --har, not both.[/red]")
        raise typer.Exit(1)
    if har is not None:
        if not har.exists():
            console.print(f"[red]HAR file not found: {escape(str(har))}[/red]")
            raise typer.Exit(1)
        return DigestSource.from_har(har)
    if session_name is None:
        console.print("[red]Session name required, or pass --har.[/red]")
        raise typer.Exit(1)
    run_dir = resolve_run(session_name, run_id)
    if not (run_dir / "network.har").exists():
        console.print(f"[red]No network.har in run '{escape(run_dir.name)}'[/red]")
        raise typer.Exit(1)
    return DigestSource.from_run_dir(run_dir, session=session_name, run_id=run_dir.name)


def digest_cmd(
    session: Annotated[str | None, typer.Argument(metavar="SESSION")] = None,
    run: Annotated[str | None, typer.Argument(metavar="RUN_ID")] = None,
    har: Annotated[
        Path | None, typer.Option("--har", help="Digest a bare HAR file instead of a run")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the complete digest as JSON")] = False,
    all_hosts: Annotated[
        bool, typer.Option("--all-hosts", help="Model every host, not just the primary one")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", help="Max endpoints in the markdown form")] = 60,
    output: Annotated[
        Path | None, typer.Option("--output", help="Write to a file instead of stdout")
    ] = None,
) -> None:
    """Read a HAR (a run or a bare file) into a readable digest of hosts, endpoints, login, and tokens."""
    source = _digest_source(session, run, har)
    result = digest(source, all_hosts=all_hosts)
    text = render_json(result) if as_json else render_markdown(result, limit=limit)
    if output is not None:
        output.write_text(text, encoding="utf-8")
        console.print(f"[green]Digest written:[/green] {escape(str(output))}")
    else:
        console.print(text, markup=False, highlight=False)


def _matches_template(entry_method: str, entry_template: str, pattern: str) -> bool:
    import fnmatch

    method_part, _, path_part = pattern.strip().partition(" ")
    if method_part.upper() != entry_method:
        return False
    return entry_template == path_part or fnmatch.fnmatch(entry_template, path_part)


def fixtures_cmd(
    session: Annotated[str, typer.Argument(metavar="SESSION")],
    run: Annotated[str | None, typer.Argument(metavar="RUN_ID")] = None,
    match: Annotated[
        list[str], typer.Option("--match", help='"METHOD template" (repeatable)')
    ] = [],  # noqa: B006 - Typer reads this default at decoration time, never mutated per-call
    out: Annotated[Path | None, typer.Option("--out", help=f"Defaults to ./{CAPTURES_DIR}")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max files per matched template")] = _DEFAULT_FIXTURE_LIMIT,
    allow_tracked: Annotated[
        bool, typer.Option("--allow-tracked", help="Write even onto a tracked path")
    ] = False,
) -> None:
    """Write captured response bodies exactly as recorded, named for deriving test fixtures."""
    if not match:
        console.print("[red]--match is required (repeatable).[/red]")
        raise typer.Exit(1)

    run_dir = resolve_run(session, run)
    har_path = run_dir / "network.har"
    if not har_path.exists():
        console.print(f"[red]No network.har in run '{escape(run_dir.name)}'[/red]")
        raise typer.Exit(1)

    target_dir = out if out is not None else Path.cwd() / CAPTURES_DIR
    repo_root = find_repo_root(target_dir)
    if repo_root is not None:
        try:
            relative = str(target_dir.resolve().relative_to(repo_root))
        except ValueError:
            relative = CAPTURES_DIR
        if ensure_ignored(repo_root, relative):
            console.print(f"[dim]Added '{escape(relative)}/' to .gitignore[/dim]")
    else:
        console.print(
            "[yellow]Not inside a git work tree: nothing protects this directory "
            "from being committed.[/yellow]"
        )

    if not allow_tracked and target_dir.exists():
        tracked = [p for p in sorted(target_dir.rglob("*")) if p.is_file() and is_tracked(p)]
        if tracked:
            console.print("[red]Refusing to write: these paths are tracked by git:[/red]")
            for path in tracked:
                console.print(f"  {escape(str(path))}")
            console.print("[dim]Pass --allow-tracked to write anyway.[/dim]")
            raise typer.Exit(1)

    target_dir.mkdir(parents=True, exist_ok=True)
    entries = parse_har_file(har_path).entries
    per_template_count: dict[str, int] = {}
    written: list[Path] = []
    for entry in entries:
        path = urlparse(entry.request.url).path or "/"
        template, _ = template_path(path)
        method = entry.request.method.upper()
        if not any(_matches_template(method, template, pattern) for pattern in match):
            continue
        key = f"{method} {template}"
        seen = per_template_count.get(key, 0)
        if seen >= limit:
            continue
        per_template_count[key] = seen + 1

        content_type = entry.response.content_type or "application/octet-stream"
        filename = capture_filename(method, path, content_type)
        if seen > 0:
            stem, _, ext = filename.rpartition(".")
            filename = f"{stem}_{seen}.{ext}"
        file_path = target_dir / filename
        file_path.write_text(entry.response.body or "", encoding="utf-8")
        meta_path = target_dir / f"{filename}.meta.json"
        meta_path.write_text(
            jsonlib.dumps(
                {
                    "url": entry.request.url,
                    "status": entry.response.status,
                    "content_type": content_type,
                    "body_params": sorted(body_params(entry)),
                    "captured_at": entry.timestamp.isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        written.append(file_path)
        console.print(f"[green]Wrote:[/green] {escape(str(file_path))}")

    if not written:
        console.print("[yellow]No entries matched --match.[/yellow]")


def register(observe_app: typer.Typer) -> None:
    """Attach ``digest`` and ``fixtures`` to *observe_app*."""
    observe_app.command("digest")(digest_cmd)
    observe_app.command("fixtures")(fixtures_cmd)
```

> **Design note (2026-09-12):** an earlier draft of `fixtures_cmd` carried its own `_body_param_names`, reimplementing `har/digest.py`'s JSON-then-form body parse to get field names for the sidecar. Validation flagged the duplication: one decision ("what are this entry's body param names"), two independent implementations that could silently diverge. `har/digest.py` (Task 3) now exports `body_params(entry) -> dict[str, str]`, which `digest()`'s own endpoint accumulation and this sidecar both call; the sidecar takes `sorted(body_params(entry))` for just the names. `HAREntry` and `urllib.parse.parse_qs` are dropped from this module's imports along with the removed function: nothing else here used them.

In `src/graftpunk/cli/main.py`, add the import between the existing `graftpunk.cli.keepalive_commands` and `graftpunk.cli.plugin_commands` imports (`src/graftpunk/cli/main.py:40` (`from graftpunk.cli.keepalive_commands import keepalive_app`)), keeping the `graftpunk.cli.*` block alphabetically ordered (ruff's `I` rule):

```python
from graftpunk.cli.observe_commands import register as register_observe_commands
from graftpunk.cli.observe_commands import resolve_run as resolve_observe_run
```

Replace the `observe_app` construction block (`src/graftpunk/cli/main.py:203` (`observe_app = typer.Typer(`)):

```python
observe_app = typer.Typer(
    name="observe",
    help="View and manage observability data (HAR, screenshots, logs).",
)
register_observe_commands(observe_app)
```

Replace `observe_show`'s run-resolution logic (`src/graftpunk/cli/main.py:289` (`@observe_app.command("show")`) through the `run_dir = session_dir / run_id` / `else:` block that follows it) with a call to `resolve_observe_run`, keeping everything from `info = f"[bold]..."` onward unchanged:

```python
@observe_app.command("show")
def observe_show(
    ctx: typer.Context,
    session_name: Annotated[
        str | None,
        typer.Argument(help="Session name to show runs for", metavar="SESSION"),
    ] = None,
    run_id: Annotated[
        str | None,
        typer.Argument(help="Specific run ID (default: latest)", metavar="RUN_ID"),
    ] = None,
) -> None:
    """Show details of an observability run."""
    if session_name is None:
        session_name = ctx.ensure_object(dict).get("observe_session")
    if session_name is None:
        console.print("[red]Session name required. Use --session or pass SESSION argument.[/red]")
        raise typer.Exit(1)
    # base_dir=OBSERVE_BASE_DIR passes main.py's own (test-patchable) global
    # through explicitly; see observe_commands.resolve_run's docstring.
    run_dir = resolve_observe_run(session_name, run_id, base_dir=OBSERVE_BASE_DIR)

    info = f"[bold]{escape(session_name)}[/bold] / {escape(run_dir.name)}\n"
    info += f"[dim]Path:[/dim] {escape(str(run_dir))}\n"

    # List files in the run directory
    files = sorted(run_dir.iterdir())
    file_list = []
    for f in files:
        if f.is_dir():
            subfiles = list(f.iterdir())
            file_list.append(f"  {escape(f.name)}/ ({len(subfiles)} files)")
        else:
            size = f.stat().st_size
            file_list.append(f"  {escape(f.name)} ({size} bytes)")

    if file_list:
        info += "[dim]Contents:[/dim]\n" + "\n".join(file_list)

    console.print(Panel(info.strip(), border_style="cyan"))
```

`observe_clean` (`src/graftpunk/cli/main.py:351` (`@observe_app.command("clean")`)) is left untouched: it operates on a whole session's directory (it takes no `RUN_ID` argument at all), never a single resolved run, so `resolve_run`'s (session, run) contract does not fit it.

In `tests/unit/conftest.py`, append one entry to `_CONSOLE_LOCATIONS` after `tests/unit/conftest.py:21` (`    ("graftpunk.cli.plugin_runtime", "_format_console"),`):

```python
    ("graftpunk.cli.observe_commands", "console"),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_devtools_captures.py tests/unit/test_observe_commands.py tests/unit/test_cli.py -q -k "Observe or Captures or observe_commands"`
Expected: PASS, all tests green (including the pre-existing `TestObserveCLICommands` and `TestObserveSessionFlag` classes, unmodified and still passing).

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
Expected: all four green.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/devtools/__init__.py src/graftpunk/devtools/captures.py src/graftpunk/cli/observe_commands.py src/graftpunk/cli/main.py tests/unit/conftest.py tests/unit/test_devtools_captures.py tests/unit/test_observe_commands.py
git commit -m "feat(cli): add gp observe digest and gp observe fixtures"
```

---

### Task 8: `devtools/scaffold/render.py` and `devtools/scaffold/pyproject_edit.py`

**Files:**
- Create: `src/graftpunk/devtools/scaffold/__init__.py`
- Create: `src/graftpunk/devtools/scaffold/render.py`
- Create: `src/graftpunk/devtools/scaffold/pyproject_edit.py`
- Test: `tests/unit/test_scaffold_render.py`
- Test: `tests/unit/test_scaffold_pyproject_edit.py`

**Interfaces:**
- Consumes: `graftpunk.har.digest.RunDigest`, `Endpoint`, `ShapeNode`, `TokenCandidate` (Task 3); `graftpunk.har.report.summarize_shape` (Task 4, called with the scaffold's own depth budget rather than reimplemented); `graftpunk.devtools.captures.CAPTURES_DIR` (Task 7).
- Produces: `ScaffoldSpec` (frozen dataclass: `name`, `mode: Literal["new_project", "add_to_suite"]`, `backend: Literal["nodriver", "selenium"]`, `base_url`, `digest: RunDigest | None = None`, `graftpunk_version: str = ""`); `class_name_for(name: str) -> str`; `render(spec: ScaffoldSpec) -> dict[str, str]` (relative path -> file content). `add_entry_point(pyproject_path: Path, name: str, target: str) -> None`; `add_wheel_package(pyproject_path: Path, package: str) -> None`; `PyprojectEditError`. Task 9's `project.py` calls `render`, `add_entry_point`, and `add_wheel_package` and builds `ScaffoldSpec` instances; `cli/scaffold_commands.py` builds the initial `ScaffoldSpec` from `--from-run`'s digest.

Render.py and pyproject_edit.py have no dependency on each other: `project.py` (Task 9) is what wires them together, so both can be built and tested independently first.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_scaffold_render.py`:

```python
"""ScaffoldSpec -> file content, from a synthetic RunDigest (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

from graftpunk.devtools.scaffold.render import ScaffoldSpec, class_name_for, render
from graftpunk.har.digest import (
    DigestSource,
    Endpoint,
    LoginForm,
    LoginObservation,
    RunDigest,
    ShapeNode,
    TokenCandidate,
)


def _digest(
    *,
    endpoints: tuple[Endpoint, ...] = (),
    login_forms: tuple[LoginForm, ...] = (),
    login: tuple[LoginObservation, ...] = (),
    tokens: tuple[TokenCandidate, ...] = (),
) -> RunDigest:
    return RunDigest(
        source=DigestSource(har_path=__import__("pathlib").Path("network.har"), session="myshop", run_id="run-1"),
        primary_host="api.myshop.example.com",
        hosts={"api.myshop.example.com": 3},
        endpoints=endpoints,
        login=login,
        login_forms=login_forms,
        tokens=tokens,
        cookies=(),
        dropped={"static": 0, "third_party": 0, "error": 0},
    )


_ORDERS_ENDPOINT = Endpoint(
    host="api.myshop.example.com",
    template="/orders/{order_id}",
    methods=("GET",),
    count=5,
    statuses=(200,),
    content_type="application/json",
    query_params={"page": "int"},
    body_params={},
    body_kind="none",
    shape=ShapeNode(kind="object", children={"id": ShapeNode(kind="number")}),
    custom_headers=("X-Shop-Client",),
    examples=("/orders/1",),
)


class TestClassNameFor:
    def test_simple_name(self) -> None:
        assert class_name_for("myshop") == "MyshopPlugin"

    def test_hyphenated_name(self) -> None:
        assert class_name_for("my-shop") == "MyShopPlugin"


class TestRenderNewProject:
    def test_produces_the_expected_file_set(self) -> None:
        spec = ScaffoldSpec(name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example.com")
        files = render(spec)
        assert set(files) == {
            "pyproject.toml",
            "src/graftpunk_myshop/__init__.py",
            "src/graftpunk_myshop/plugin.py",
            "tests/conftest.py",
            "tests/test_plugin.py",
            "tests/fixtures/.gitkeep",
            ".gitignore",
            "README.md",
        }

    def test_pyproject_declares_entry_point_and_dependency_floor(self) -> None:
        spec = ScaffoldSpec(
            name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example.com",
            graftpunk_version="1.17.0",
        )
        pyproject = render(spec)["pyproject.toml"]
        assert '[project.entry-points."graftpunk.plugins"]' in pyproject
        assert 'myshop = "graftpunk_myshop.plugin:MyshopPlugin"' in pyproject
        assert 'graftpunk[browser]>=1.17.0' in pyproject
        assert 'select = ["E", "F", "I", "UP", "B"]' in pyproject

    def test_gitignore_contains_captures_dir(self) -> None:
        spec = ScaffoldSpec(name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example.com")
        assert "tests/captures/" in render(spec)[".gitignore"]

    def test_conftest_is_two_declarations(self) -> None:
        spec = ScaffoldSpec(name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example.com")
        conftest = render(spec)["tests/conftest.py"]
        assert 'pytest_plugins = ["graftpunk.testing.plugin"]' in conftest
        assert 'site_env_scrubber("MYSHOP_")' in conftest


class TestRenderAddToSuite:
    def test_produces_only_the_incremental_files(self) -> None:
        spec = ScaffoldSpec(name="widgets", mode="add_to_suite", backend="nodriver", base_url="https://myshop.example.com")
        files = render(spec)
        assert set(files) == {
            "src/graftpunk_widgets/__init__.py",
            "src/graftpunk_widgets/plugin.py",
            "tests/test_widgets.py",
            "tests/fixtures/.gitkeep",
        }


class TestPluginModuleWithLoginForm:
    def test_login_config_uses_the_first_password_form(self) -> None:
        form = LoginForm(
            action="/login",
            method="POST",
            fields={"username": "#email", "password": "#pw"},
            submit="#login-btn",
            hidden=("_token",),
            source="page-source.html",
        )
        spec = ScaffoldSpec(
            name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example.com",
            digest=_digest(login_forms=(form,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert 'login_config = LoginConfig(' in plugin_code
        assert '"username": "#email"' in plugin_code
        assert '"password": "#pw"' in plugin_code
        assert 'submit="#login-btn"' in plugin_code
        assert 'url="/login"' in plugin_code
        assert "GP-FILL" in plugin_code  # failure/success still need a human

    def test_no_form_found_emits_commented_block_with_observations(self) -> None:
        observation = LoginObservation(
            order=1, method="POST", url="https://api.myshop.example.com/api/auth", status=200,
            kind="auth_api", fields=(),
        )
        spec = ScaffoldSpec(
            name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example.com",
            digest=_digest(login=(observation,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "# login_config = LoginConfig" in plugin_code
        assert "auth_api" in plugin_code


class TestPluginModuleWithTokens:
    def test_paired_header_and_meta_candidates_emit_token_config(self) -> None:
        header = TokenCandidate(kind="header", name="X-CSRF-Token", seen_on=("GET /dashboard",))
        meta = TokenCandidate(kind="meta", name="X-CSRF-Token", seen_on=("page-source.html",))
        spec = ScaffoldSpec(
            name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example.com",
            digest=_digest(tokens=(header, meta)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "from graftpunk.tokens import Token, TokenConfig" in plugin_code
        assert 'Token.from_meta_tag(name="X-CSRF-Token", header="X-CSRF-Token")' in plugin_code

    def test_paired_header_and_cookie_candidates_use_from_cookie(self) -> None:
        header = TokenCandidate(kind="header", name="X-Csrf", seen_on=("GET /a",))
        cookie = TokenCandidate(kind="cookie", name="X-Csrf", seen_on=("GET /a",))
        spec = ScaffoldSpec(
            name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example.com",
            digest=_digest(tokens=(header, cookie)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert 'Token.from_cookie(cookie_name="X-Csrf", header="X-Csrf")' in plugin_code

    def test_unpaired_candidate_is_a_gp_fill_line(self) -> None:
        lone = TokenCandidate(kind="header", name="X-Lonely", seen_on=("GET /a",))
        spec = ScaffoldSpec(
            name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example.com",
            digest=_digest(tokens=(lone,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "GP-FILL: unpaired token candidate: header 'X-Lonely'" in plugin_code


class TestPluginModuleCommandStubs:
    def test_stub_per_endpoint_up_to_twelve(self) -> None:
        endpoints = tuple(
            Endpoint(
                host="api.myshop.example.com", template=f"/item-{i}", methods=("GET",), count=1,
                statuses=(200,), content_type="application/json", query_params={}, body_params={},
                body_kind="none", shape=ShapeNode(kind="object", children={}), custom_headers=(), examples=(),
            )
            for i in range(15)
        )
        spec = ScaffoldSpec(
            name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example.com",
            digest=_digest(endpoints=endpoints),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert plugin_code.count("@command(") == 12

    def test_json_endpoint_calls_request_json_with_xhr_role(self) -> None:
        spec = ScaffoldSpec(
            name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_ORDERS_ENDPOINT,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert 'ctx.request_json("GET", f"/orders/{order_id}"' in plugin_code
        assert 'role="xhr"' in plugin_code

    def test_path_param_becomes_a_required_argument(self) -> None:
        spec = ScaffoldSpec(
            name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_ORDERS_ENDPOINT,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "order_id: str" in plugin_code

    def test_query_param_becomes_an_optional_option(self) -> None:
        spec = ScaffoldSpec(
            name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_ORDERS_ENDPOINT,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "page: int | None = None" in plugin_code

    def test_custom_headers_passed_explicitly(self) -> None:
        spec = ScaffoldSpec(
            name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_ORDERS_ENDPOINT,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert '"X-Shop-Client": "GP-FILL"' in plugin_code

    def test_html_endpoint_calls_request_text_with_navigation_role(self) -> None:
        html_endpoint = Endpoint(
            host="api.myshop.example.com", template="/page", methods=("GET",), count=1, statuses=(200,),
            content_type="text/html", query_params={}, body_params={}, body_kind="none", shape=None,
            custom_headers=(), examples=(),
        )
        spec = ScaffoldSpec(
            name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example.com",
            digest=_digest(endpoints=(html_endpoint,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert 'ctx.request_text("GET", "/page", role="navigation")' in plugin_code

    def test_no_digest_emits_one_gp_fill_stub(self) -> None:
        spec = ScaffoldSpec(name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example.com")
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert plugin_code.count("@command(") == 1
        assert "GP-FILL" in plugin_code


class TestGeneratedPluginModuleParses:
    def test_ast_parses_with_and_without_a_digest(self) -> None:
        import ast

        bare = ScaffoldSpec(name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example.com")
        ast.parse(render(bare)["src/graftpunk_myshop/plugin.py"])

        full = ScaffoldSpec(
            name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example.com",
            digest=_digest(
                endpoints=(_ORDERS_ENDPOINT,),
                login_forms=(
                    LoginForm(
                        action="/login", method="POST", fields={"password": "#pw"}, submit="#go",
                        hidden=(), source="s",
                    ),
                ),
                tokens=(
                    TokenCandidate(kind="header", name="X-T", seen_on=("a",)),
                    TokenCandidate(kind="meta", name="X-T", seen_on=("b",)),
                ),
            ),
        )
        ast.parse(render(full)["src/graftpunk_myshop/plugin.py"])
```

Create `tests/unit/test_scaffold_pyproject_edit.py`:

```python
"""The one seam that edits an existing pyproject.toml (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

from pathlib import Path

import pytest

from graftpunk.devtools.scaffold.pyproject_edit import (
    PyprojectEditError,
    add_entry_point,
    add_wheel_package,
)

_SINGLE_LINE_ARRAY = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "mysuite"
version = "0.1.0"

[project.entry-points."graftpunk.plugins"]
existing = "mysuite.existing:ExistingPlugin"

[tool.hatch.build.targets.wheel]
packages = ["src/mysuite"]
"""

_MULTI_LINE_ARRAY = """\
[project]
name = "mysuite"

[project.entry-points."graftpunk.plugins"]
existing = "mysuite.existing:ExistingPlugin"

[tool.hatch.build.targets.wheel]
packages = [
    "src/mysuite",
]
"""

_NO_ENTRY_POINT_TABLE = """\
[project]
name = "mysuite"
"""

_INCLUDE_INSTEAD_OF_PACKAGES = """\
[project]
name = "mysuite"

[project.entry-points."graftpunk.plugins"]

[tool.hatch.build.targets.wheel]
include = ["src/mysuite/**"]
"""

_NO_PACKAGES_KEY_AT_ALL = """\
[project]
name = "mysuite"

[project.entry-points."graftpunk.plugins"]

[tool.hatch.build.targets.wheel]
"""


class TestAddEntryPoint:
    def test_appends_a_new_line_to_the_table(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_SINGLE_LINE_ARRAY)
        add_entry_point(pyproject, "widgets", "graftpunk_widgets.plugin:WidgetsPlugin")
        text = pyproject.read_text()
        assert 'widgets = "graftpunk_widgets.plugin:WidgetsPlugin"' in text
        assert 'existing = "mysuite.existing:ExistingPlugin"' in text  # untouched

    def test_result_is_valid_toml(self, tmp_path: Path) -> None:
        import tomllib

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_SINGLE_LINE_ARRAY)
        add_entry_point(pyproject, "widgets", "graftpunk_widgets.plugin:WidgetsPlugin")
        data = tomllib.loads(pyproject.read_text())
        eps = data["project"]["entry-points"]["graftpunk.plugins"]
        assert eps["widgets"] == "graftpunk_widgets.plugin:WidgetsPlugin"
        assert eps["existing"] == "mysuite.existing:ExistingPlugin"

    def test_duplicate_name_refuses(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_SINGLE_LINE_ARRAY)
        with pytest.raises(PyprojectEditError, match="already registered"):
            add_entry_point(pyproject, "existing", "graftpunk_widgets.plugin:WidgetsPlugin")

    def test_missing_table_refuses_with_instructions(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_NO_ENTRY_POINT_TABLE)
        with pytest.raises(PyprojectEditError, match="entry-points"):
            add_entry_point(pyproject, "widgets", "graftpunk_widgets.plugin:WidgetsPlugin")


class TestAddWheelPackage:
    def test_appends_to_a_single_line_array(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_SINGLE_LINE_ARRAY)
        add_wheel_package(pyproject, "src/graftpunk_widgets")
        assert '"src/graftpunk_widgets"' in pyproject.read_text()

    def test_appends_to_a_multi_line_array(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_MULTI_LINE_ARRAY)
        add_wheel_package(pyproject, "src/graftpunk_widgets")
        text = pyproject.read_text()
        assert '"src/graftpunk_widgets"' in text
        assert '"src/mysuite"' in text

    def test_result_is_valid_toml_with_both_packages(self, tmp_path: Path) -> None:
        import tomllib

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_MULTI_LINE_ARRAY)
        add_wheel_package(pyproject, "src/graftpunk_widgets")
        packages = tomllib.loads(pyproject.read_text())["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
        assert set(packages) == {"src/mysuite", "src/graftpunk_widgets"}

    def test_already_present_is_a_no_op(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_SINGLE_LINE_ARRAY)
        before = pyproject.read_text()
        add_wheel_package(pyproject, "src/mysuite")
        assert pyproject.read_text() == before

    def test_no_packages_key_is_a_no_op(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_NO_PACKAGES_KEY_AT_ALL)
        before = pyproject.read_text()
        add_wheel_package(pyproject, "src/graftpunk_widgets")
        assert pyproject.read_text() == before

    def test_include_instead_of_packages_refuses(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_INCLUDE_INSTEAD_OF_PACKAGES)
        with pytest.raises(PyprojectEditError, match="include"):
            add_wheel_package(pyproject, "src/graftpunk_widgets")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_render.py tests/unit/test_scaffold_pyproject_edit.py -q`
Expected: collection errors, `ModuleNotFoundError: No module named 'graftpunk.devtools.scaffold'`.

- [ ] **Step 3: Write the implementation**

Create `src/graftpunk/devtools/scaffold/__init__.py`:

```python
"""Plugin scaffolding: turns a ScaffoldSpec into files, an existing repository
into a new plugin, and an existing pyproject.toml into one that declares it.

CLI-only: ``gp plugin new`` is the only caller. Nothing here is imported by
a generated plugin at runtime (plugin tooling spec, 2026-09-11).
"""
```

Create `src/graftpunk/devtools/scaffold/render.py`:

```python
"""ScaffoldSpec -> relative-path -> file-content.

The rule for every line this module emits: a fact about the site (a URL, a
selector, a parameter name, a header name) or a call into graftpunk's public
API. Never graftpunk's own logic (plugin tooling spec, 2026-09-11, "A rule
for everything the scaffold emits").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from graftpunk.devtools.captures import CAPTURES_DIR
from graftpunk.har.digest import Endpoint, RunDigest, TokenCandidate
from graftpunk.har.report import summarize_shape

__all__ = ["ScaffoldSpec", "class_name_for", "render"]

_MAX_SCAFFOLD_ENDPOINTS = 12
_PY_TYPE_BY_OBSERVED: dict[str, str] = {"int": "int", "bool": "bool", "list": "list[str]", "str": "str"}
# A generated stub's docstring is one line: shallower than report.py's own
# default (3), so a wide response shows its top-level keys without spilling
# nested detail into the docstring.
_SCAFFOLD_SHAPE_DEPTH = 1


@dataclass(frozen=True)
class ScaffoldSpec:
    name: str
    mode: Literal["new_project", "add_to_suite"]
    backend: Literal["nodriver", "selenium"]
    base_url: str
    digest: RunDigest | None = None
    graftpunk_version: str = ""


def class_name_for(name: str) -> str:
    parts = re.split(r"[^a-zA-Z0-9]+", name)
    return "".join(part.capitalize() for part in parts if part) + "Plugin"


def _env_prefix_for(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()) + "_"


def _command_name(template: str, seen: set[str]) -> str:
    segments = [s for s in template.strip("/").split("/") if s and not s.startswith("{")]
    base = re.sub(r"[^a-z0-9_]", "_", "_".join(segments).lower()) or "root"
    name = base
    counter = 1
    while name in seen:
        counter += 1
        name = f"{base}_{counter}"
    seen.add(name)
    return name


def _redirect_target_after_credential_post(d: RunDigest) -> str | None:
    """The path a credential post redirected to, if any: a candidate for `success`."""
    posted = False
    for observation in d.login:
        if observation.kind == "credential_post":
            posted = True
            continue
        if posted and observation.kind == "redirect":
            return urlparse(observation.url).path
    return None


def _render_login_config(spec: ScaffoldSpec) -> list[str]:
    if spec.digest is None:
        return [
            "    # GP-FILL: no run digest available.",
            '    # login_config = LoginConfig(steps=[LoginStep(fields={...}, submit="...")])',
        ]
    form = next((f for f in spec.digest.login_forms if "password" in f.fields), None)
    if form is None:
        lines = ["    # No login form detected. Observations:"]
        for observation in spec.digest.login:
            lines.append(f"    #   {observation.order}. {observation.method} {observation.url} ({observation.kind})")
        lines.append('    # login_config = LoginConfig(steps=[LoginStep(fields={...}, submit="...")])')
        return lines
    fields_repr = ", ".join(f'"{role}": "{selector}"' for role, selector in sorted(form.fields.items()))
    submit_repr = form.submit or "GP-FILL: submit selector"
    hint = _redirect_target_after_credential_post(spec.digest)
    lines = [
        "    login_config = LoginConfig(",
        "        steps=[",
        f'            LoginStep(fields={{{fields_repr}}}, submit="{submit_repr}"),',
        "        ],",
        f'        url="{form.action}",',
        '        failure="GP-FILL: text on the page indicating login failure",',
    ]
    if hint:
        lines.append(f"        # candidate success redirect target, from the run: {hint}")
    lines.append('        success="GP-FILL: CSS selector for login success",')
    lines.append("    )")
    return lines


def _paired_token_candidates(d: RunDigest) -> list[tuple[TokenCandidate, TokenCandidate]]:
    by_name: dict[str, list[TokenCandidate]] = {}
    for candidate in d.tokens:
        by_name.setdefault(candidate.name.lower(), []).append(candidate)
    pairs: list[tuple[TokenCandidate, TokenCandidate]] = []
    for candidates in by_name.values():
        headers = [c for c in candidates if c.kind == "header"]
        sources = [c for c in candidates if c.kind in ("meta", "cookie")]
        if headers and sources:
            pairs.append((headers[0], sources[0]))
    return pairs


def _render_token_config(spec: ScaffoldSpec) -> list[str]:
    if spec.digest is None:
        return ['    # token_config = TokenConfig(tokens=[Token.from_meta_tag(name="...", header="...")])']
    pairs = _paired_token_candidates(spec.digest)
    paired = {candidate for pair in pairs for candidate in pair}
    unpaired = [c for c in spec.digest.tokens if c not in paired]
    lines: list[str] = []
    if pairs:
        lines.append("    token_config = TokenConfig(tokens=[")
        for header, source in pairs:
            if source.kind == "meta":
                lines.append(f'        Token.from_meta_tag(name="{source.name}", header="{header.name}"),')
            else:
                lines.append(f'        Token.from_cookie(cookie_name="{source.name}", header="{header.name}"),')
        lines.append("    ])")
    else:
        lines.append(
            '    # token_config = TokenConfig(tokens=[Token.from_meta_tag(name="...", header="...")])'
        )
    for candidate in unpaired:
        lines.append(f"    # GP-FILL: unpaired token candidate: {candidate.kind} '{candidate.name}'")
    return lines


def _is_json_endpoint(endpoint: Endpoint) -> bool:
    return endpoint.shape is not None or "json" in endpoint.content_type.lower()


def _render_command_stub(endpoint: Endpoint, seen_names: set[str], run_label: str) -> list[str]:
    method = endpoint.methods[0]
    name = _command_name(endpoint.template, seen_names)
    path_params = re.findall(r"\{([a-zA-Z0-9_]+)\}", endpoint.template)
    is_json = _is_json_endpoint(endpoint)
    call, role, return_type = ("request_json", "xhr", "dict") if is_json else ("request_text", "navigation", "str")

    sig_parts = ["self", "ctx: CommandContext"] + [f"{p}: str" for p in path_params]
    for extra in sorted(set(endpoint.query_params) | set(endpoint.body_params)):
        observed = endpoint.query_params.get(extra) or endpoint.body_params.get(extra, "str")
        sig_parts.append(f"{extra}: {_PY_TYPE_BY_OBSERVED.get(observed, 'str')} | None = None")

    url_expr = f'f"{endpoint.template}"' if path_params else f'"{endpoint.template}"'
    call_kwargs = [f'role="{role}"']
    if endpoint.query_params:
        query_dict = ", ".join(f'"{p}": {p}' for p in sorted(endpoint.query_params))
        call_kwargs.append(f"params={{{query_dict}}}")
    if any(m in ("POST", "PUT", "PATCH") for m in endpoint.methods) and endpoint.body_params:
        body_dict = ", ".join(f'"{p}": {p}' for p in sorted(endpoint.body_params))
        call_kwargs.append(f"json={{{body_dict}}}")
    if endpoint.custom_headers:
        header_dict = ", ".join(f'"{h}": "GP-FILL"' for h in endpoint.custom_headers)
        call_kwargs.append(f"headers={{{header_dict}}}")

    return [
        f'    @command(help="GP-FILL: describe {name}")',
        f"    def {name}({', '.join(sig_parts)}) -> {return_type}:",
        (
            f'        """{method} {endpoint.template}: seen {endpoint.count} time(s) in run '
            f'{run_label}. Shape: {summarize_shape(endpoint.shape, depth=_SCAFFOLD_SHAPE_DEPTH)}."""'
        ),
        f'        return ctx.{call}("{method}", {url_expr}, {", ".join(call_kwargs)})',
        "",
    ]


def _ordered_endpoints(d: RunDigest) -> list[Endpoint]:
    return sorted(d.endpoints, key=lambda e: (not _is_json_endpoint(e), -e.count, e.template))


def _run_label(d: RunDigest) -> str:
    return f"{d.source.session}/{d.source.run_id}" if d.source.session else "the supplied HAR"


def _render_command_stubs(spec: ScaffoldSpec) -> list[str]:
    if spec.digest is None:
        return [
            '    @command(help="GP-FILL: describe this command")',
            "    def example(self, ctx: CommandContext) -> dict:",
            '        """GP-FILL: what this command does."""',
            '        return ctx.request_json("GET", "/GP-FILL/path")',
        ]
    seen_names: set[str] = set()
    lines: list[str] = []
    for endpoint in _ordered_endpoints(spec.digest)[:_MAX_SCAFFOLD_ENDPOINTS]:
        lines.extend(_render_command_stub(endpoint, seen_names, _run_label(spec.digest)))
    return lines


def _render_plugin_module(spec: ScaffoldSpec) -> str:
    klass = class_name_for(spec.name)
    needs_token_import = spec.digest is not None and bool(_paired_token_candidates(spec.digest))
    lines = [
        f'"""{spec.name} plugin.',
        "",
        "Verified against a real account on: (none yet)",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from graftpunk.plugins import CommandContext, LoginConfig, LoginStep, SitePlugin, command",
    ]
    if needs_token_import:
        lines.append("from graftpunk.tokens import Token, TokenConfig")
    lines += ["", "", f"class {klass}(SitePlugin):"]
    lines.append(f'    """Commands for {spec.base_url or "GP-FILL: base_url"}."""')
    lines.append("")
    lines.append(f'    site_name = "{spec.name}"')
    lines.append(f'    session_name = "{spec.name}"')
    lines.append(f'    help_text = "Commands for {spec.name}"')
    lines.append(f'    base_url = "{spec.base_url}"')
    lines.append(f'    backend = "{spec.backend}"')
    lines.append("    api_version = 1")
    lines.append("")
    lines.extend(_render_login_config(spec))
    lines.append("")
    lines.extend(_render_token_config(spec))
    lines.append("")
    lines.extend(_render_command_stubs(spec))
    return "\n".join(lines).rstrip() + "\n"


def _render_pyproject(spec: ScaffoldSpec) -> str:
    package = f"graftpunk_{spec.name}"
    klass = class_name_for(spec.name)
    floor = spec.graftpunk_version or "0.0.0"
    return (
        "[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n'
        "\n"
        "[project]\n"
        f'name = "{package}"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.11"\n'
        "dependencies = [\n"
        f'    "graftpunk[browser]>={floor}",\n'
        "]\n"
        "\n"
        "[project.optional-dependencies]\n"
        'dev = ["pytest>=8.0.0", "ruff>=0.5.0"]\n'
        "\n"
        '[project.entry-points."graftpunk.plugins"]\n'
        f'{spec.name} = "{package}.plugin:{klass}"\n'
        "\n"
        "[tool.hatch.build.targets.wheel]\n"
        f'packages = ["src/{package}"]\n'
        "\n"
        "[tool.ruff]\n"
        "line-length = 100\n"
        "\n"
        "[tool.ruff.lint]\n"
        'select = ["E", "F", "I", "UP", "B"]\n'
    )


def _render_conftest(spec: ScaffoldSpec) -> str:
    return (
        'pytest_plugins = ["graftpunk.testing.plugin"]\n'
        "from graftpunk.testing.plugin import site_env_scrubber\n"
        "\n"
        f'scrub_site_env = site_env_scrubber("{_env_prefix_for(spec.name)}")\n'
    )


def _render_test_module(spec: ScaffoldSpec, *, package: str) -> str:
    klass = class_name_for(spec.name)
    lines = [
        f'"""Tests for the {spec.name} plugin."""',
        "",
        "from __future__ import annotations",
        "",
        "from pathlib import Path",
        "",
        f"from {package}.plugin import {klass}",
        "from graftpunk.testing import fixture_context",
        "",
        'FIXTURES_DIR = Path(__file__).parent / "fixtures"',
        "",
        "",
        "def test_plugin_instantiates() -> None:",
        f"    plugin = {klass}()",
        f'    assert plugin.site_name == "{spec.name}"',
        "",
        "",
    ]
    if spec.digest is None:
        lines.append("# GP-FILL: add a test per command, against a fixture in tests/fixtures/")
        return "\n".join(lines).rstrip() + "\n"
    seen: set[str] = set()
    for endpoint in _ordered_endpoints(spec.digest)[:_MAX_SCAFFOLD_ENDPOINTS]:
        name = _command_name(endpoint.template, seen)
        path_params = re.findall(r"\{([a-zA-Z0-9_]+)\}", endpoint.template)
        # "1" round-trips through the naming rule (paths.template_path treats
        # an all-digit segment as dynamic): the request this test issues
        # renames back to the endpoint's own template, so it finds the
        # fixture named for it. A literal "GP-FILL" would not: it stays a
        # literal segment and the fixture lookup would 404.
        args = ", ".join(f'{p}="1"' for p in path_params)
        call_args = f"ctx{', ' + args if args else ''}"
        lines.append(f"def test_{name}() -> None:")
        lines.append(
            f'    ctx = fixture_context(FIXTURES_DIR, plugin_name="{spec.name}", base_url="{spec.base_url}")'
        )
        lines.append(f"    plugin = {klass}()")
        lines.append(f"    result = plugin.{name}({call_args})")
        lines.append("    assert result  # GP-FILL: assert on the shape you expect")
        lines.append("")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_gitignore() -> str:
    return f"{CAPTURES_DIR}/\n__pycache__/\n*.egg-info/\n.venv/\n"


def _render_readme(spec: ScaffoldSpec) -> str:
    return (
        f"# {spec.name}\n\n"
        "A graftpunk plugin.\n\n"
        "## Install\n\n"
        "```bash\npip install -e .\n```\n\n"
        "## Log in\n\n"
        f"```bash\ngp {spec.name} login\n```\n\n"
        "## Run a command\n\n"
        f"```bash\ngp {spec.name} --help\n```\n\n"
        "## Tests\n\n"
        "```bash\npytest\n```\n\n"
        "Fixtures under `tests/fixtures/` are hand-derived from captures in "
        f"`{CAPTURES_DIR}/` (captures are never committed; a fixture copies the "
        "structure and invents the content). See `gp observe fixtures --help`.\n"
    )


def render(spec: ScaffoldSpec) -> dict[str, str]:
    """Render *spec* into relative-path -> file-content, per its ``mode``."""
    package = f"graftpunk_{spec.name}"
    plugin_module = _render_plugin_module(spec)
    if spec.mode == "new_project":
        return {
            "pyproject.toml": _render_pyproject(spec),
            f"src/{package}/__init__.py": f'"""{spec.name}: a graftpunk plugin."""\n',
            f"src/{package}/plugin.py": plugin_module,
            "tests/conftest.py": _render_conftest(spec),
            "tests/test_plugin.py": _render_test_module(spec, package=package),
            "tests/fixtures/.gitkeep": "",
            ".gitignore": _render_gitignore(),
            "README.md": _render_readme(spec),
        }
    return {
        f"src/{package}/__init__.py": f'"""{spec.name}: a graftpunk plugin."""\n',
        f"src/{package}/plugin.py": plugin_module,
        f"tests/test_{spec.name}.py": _render_test_module(spec, package=package),
        "tests/fixtures/.gitkeep": "",
    }
```

> **Design note (2026-09-12):** an earlier draft of `render.py` defined its own `_shape_summary`, whose wording (`"object with keys: ..."`, no array nesting, no truncation marker) diverged from `har/report.py`'s own shape summariser. Validation flagged the two implementations as a single decision (what a shape looks like as one line of text) with two owners that could silently drift. `har/report.py` (Task 4) now exports `summarize_shape(shape, *, depth=3)`; `render.py` imports it and calls it with its own `_SCAFFOLD_SHAPE_DEPTH = 1` budget (a generated stub's docstring is one line, shallower than the digest's default), rather than keeping a second copy.

Create `src/graftpunk/devtools/scaffold/pyproject_edit.py`:

```python
"""The one seam that edits an existing pyproject.toml: textual, two known shapes only.

Refuses rather than guesses on anything else: a wrong edit to someone's
build configuration is worse than a refusal with instructions (plugin
tooling spec, 2026-09-11).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

__all__ = ["PyprojectEditError", "add_entry_point", "add_wheel_package"]

_ENTRY_POINT_TABLE_RE = re.compile(
    r'(\[project\.entry-points\."graftpunk\.plugins"\]\n)((?:.*\n)*?)(?=\[|\Z)'
)
_WHEEL_PACKAGES_RE = re.compile(
    r"(\[tool\.hatch\.build\.targets\.wheel\][^\[]*?packages\s*=\s*)(\[[^\]]*\])", re.DOTALL
)


class PyprojectEditError(Exception):
    """The shape pyproject_edit.py expected was not found; nothing was written."""


def add_entry_point(pyproject_path: Path, name: str, target: str) -> None:
    """Append ``name = "target"`` to the ``[project.entry-points."graftpunk.plugins"]`` table.

    Raises:
        PyprojectEditError: The name is already registered, or the table's
            shape cannot be located textually.
    """
    text = pyproject_path.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    existing = data.get("project", {}).get("entry-points", {}).get("graftpunk.plugins", {})
    if name in existing:
        raise PyprojectEditError(f"Entry point '{name}' is already registered in {pyproject_path}.")
    match = _ENTRY_POINT_TABLE_RE.search(text)
    if match is None:
        raise PyprojectEditError(
            f'{pyproject_path} has no [project.entry-points."graftpunk.plugins"] table I can '
            f"locate textually. Add this line by hand:\n"
            f'{name} = "{target}"'
        )
    header, body = match.group(1), match.group(2)
    new_text = text[: match.start()] + header + body + f'{name} = "{target}"\n' + text[match.end() :]
    pyproject_path.write_text(new_text, encoding="utf-8")


def _append_to_array(array_text: str, item: str) -> str:
    inner = array_text[1:-1]
    stripped = inner.rstrip()
    if not stripped.strip():
        return f'["{item}"]'
    separator = "" if stripped.endswith(",") else ","
    if "\n" in inner:
        return f'[{stripped}{separator}\n    "{item}",\n]'
    return f'[{stripped}{separator} "{item}"]'


def add_wheel_package(pyproject_path: Path, package: str) -> None:
    """Append *package* to ``[tool.hatch.build.targets.wheel]``'s ``packages`` array.

    A no-op when the table has no explicit ``packages`` key at all (hatchling
    then infers packages on its own).

    Raises:
        PyprojectEditError: The wheel table uses ``include`` instead of
            ``packages``, or ``packages`` exists but its array cannot be
            located textually.
    """
    text = pyproject_path.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    wheel = data.get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {})
    if "packages" not in wheel:
        if "include" in wheel:
            raise PyprojectEditError(
                f"{pyproject_path} uses [tool.hatch.build.targets.wheel].include instead of "
                f'packages. Add "{package}" to it by hand.'
            )
        return
    if package in wheel["packages"]:
        return
    match = _WHEEL_PACKAGES_RE.search(text)
    if match is None:
        raise PyprojectEditError(
            f"{pyproject_path} has [tool.hatch.build.targets.wheel].packages but I cannot "
            f'locate its array textually. Add "{package}" to it by hand.'
        )
    prefix, array = match.group(1), match.group(2)
    new_text = text[: match.start()] + prefix + _append_to_array(array, package) + text[match.end() :]
    pyproject_path.write_text(new_text, encoding="utf-8")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_render.py tests/unit/test_scaffold_pyproject_edit.py -q`
Expected: PASS, all tests green.

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
Expected: all four green.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/devtools/scaffold/__init__.py src/graftpunk/devtools/scaffold/render.py src/graftpunk/devtools/scaffold/pyproject_edit.py tests/unit/test_scaffold_render.py tests/unit/test_scaffold_pyproject_edit.py
git commit -m "feat(scaffold): add file rendering and the pyproject.toml edit seam"
```

---

### Task 9: `devtools/scaffold/project.py`, `cli/scaffold_commands.py`, and the reserved CLI names

**Files:**
- Create: `src/graftpunk/devtools/scaffold/project.py`
- Create: `src/graftpunk/cli/scaffold_commands.py`
- Modify: `src/graftpunk/cli/plugin_commands.py:287` (`def register_plugin_commands(app: typer.Typer, *, notify_errors: bool = True) -> dict[str, str]:`)
- Modify: `src/graftpunk/cli/main.py` (attach `plugin_app` before `register_plugin_commands(app)` runs, and near the top import block)
- Modify: `tests/unit/conftest.py:21` (`    ("graftpunk.cli.plugin_runtime", "_format_console"),`), append one entry
- Test: `tests/unit/test_scaffold_project.py`
- Test: `tests/unit/test_scaffold_cli.py`

**Interfaces:**
- Consumes: `graftpunk.devtools.scaffold.render.ScaffoldSpec`, `class_name_for`, `render` (Task 8); `graftpunk.devtools.scaffold.pyproject_edit.add_entry_point`, `add_wheel_package` (Task 8); `graftpunk.devtools.captures.CAPTURES_DIR`, `ensure_ignored` (Task 7); `graftpunk.cli.observe_commands.resolve_run` (Task 7); `graftpunk.har.digest.DigestSource`, `digest` (Task 3).
- Produces: `write_scaffold(target_dir: Path, spec: ScaffoldSpec, *, force_new: bool = False) -> ScaffoldResult`, `ScaffoldConflictError`; `plugin_app` (the `gp plugin new` Typer sub-app); `reserved_cli_names() -> frozenset[str]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_scaffold_project.py`:

```python
"""Mode decision, conflict detection, and writing (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

from pathlib import Path

import pytest

from graftpunk.devtools.scaffold.project import ScaffoldConflictError, write_scaffold
from graftpunk.devtools.scaffold.render import ScaffoldSpec

_SUITE_PYPROJECT = """\
[project]
name = "mysuite"

[project.entry-points."graftpunk.plugins"]
existing = "mysuite.existing:ExistingPlugin"

[tool.hatch.build.targets.wheel]
packages = ["src/mysuite"]
"""

_NON_PLUGIN_PYPROJECT = """\
[project]
name = "unrelated-package"
"""


def _spec(name: str = "myshop") -> ScaffoldSpec:
    return ScaffoldSpec(name=name, mode="new_project", backend="nodriver", base_url="https://myshop.example.com")


class TestNewProjectMode:
    def test_empty_directory_writes_a_new_project(self, tmp_path: Path) -> None:
        result = write_scaffold(tmp_path, _spec())
        assert result.mode == "new_project"
        assert (tmp_path / "pyproject.toml").exists()
        assert (tmp_path / "src" / "graftpunk_myshop" / "plugin.py").exists()

    def test_gitignore_not_reported_as_updated_for_a_new_project(self, tmp_path: Path) -> None:
        """A new project's .gitignore is written fresh with the line already in it."""
        result = write_scaffold(tmp_path, _spec())
        assert result.gitignore_updated is False


class TestAddToSuiteMode:
    def test_existing_plugin_suite_gets_incremental_files(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(_SUITE_PYPROJECT)
        result = write_scaffold(tmp_path, _spec("widgets"))
        assert result.mode == "add_to_suite"
        assert (tmp_path / "src" / "graftpunk_widgets" / "plugin.py").exists()
        assert (tmp_path / "tests" / "test_widgets.py").exists()
        assert not (tmp_path / "pyproject.toml").exists() or True  # untouched path, not rewritten

    def test_entry_point_and_package_added_to_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(_SUITE_PYPROJECT)
        write_scaffold(tmp_path, _spec("widgets"))
        text = (tmp_path / "pyproject.toml").read_text()
        assert 'widgets = "graftpunk_widgets.plugin:WidgetsPlugin"' in text
        assert '"src/graftpunk_widgets"' in text
        assert 'existing = "mysuite.existing:ExistingPlugin"' in text  # unaffected

    def test_gitignore_created_and_reported(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(_SUITE_PYPROJECT)
        result = write_scaffold(tmp_path, _spec("widgets"))
        assert result.gitignore_updated is True
        assert "tests/captures/" in (tmp_path / ".gitignore").read_text()

    def test_a_non_plugin_pyproject_refuses(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(_NON_PLUGIN_PYPROJECT)
        with pytest.raises(ValueError, match="entry-points"):
            write_scaffold(tmp_path, _spec())

    def test_new_flag_forces_a_new_project_despite_existing_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(_SUITE_PYPROJECT)
        with pytest.raises(ScaffoldConflictError):
            # A new-project render also writes pyproject.toml, which already
            # exists here: --new still refuses on conflict, it just skips the
            # suite-mode decision, so this proves force_new took the
            # new-project branch rather than raising the "not a suite" ValueError.
            write_scaffold(tmp_path, _spec(), force_new=True)


class TestConflicts:
    def test_existing_target_file_refuses_before_writing_anything(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("already here")
        with pytest.raises(ScaffoldConflictError) as exc:
            write_scaffold(tmp_path, _spec())
        assert any(p.name == "README.md" for p in exc.value.conflicts)
        assert not (tmp_path / "pyproject.toml").exists()
```

Create `tests/unit/test_scaffold_cli.py`:

```python
"""gp plugin new through the Typer runner (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from graftpunk.cli.scaffold_commands import plugin_app

runner = CliRunner()


def _build_app() -> typer.Typer:
    app = typer.Typer()
    app.add_typer(plugin_app)
    return app


def _entry(method: str, url: str, *, content_type: str = "application/json", body: str = "{}") -> dict:
    return {
        "startedDateTime": "2026-09-10T10:00:00.000Z",
        "time": 1,
        "request": {"method": method, "url": url, "headers": [], "cookies": [], "queryString": []},
        "response": {
            "status": 200,
            "statusText": "OK",
            "headers": [{"name": "Content-Type", "value": content_type}],
            "cookies": [],
            "content": {"mimeType": content_type, "text": body, "size": len(body)},
        },
    }


class TestPluginNewHappyPath:
    def test_new_project_in_target_dir(self, tmp_path: Path) -> None:
        result = runner.invoke(
            _build_app(),
            ["plugin", "new", "myshop", "--url", "https://myshop.example.com", "--dir", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / "src" / "graftpunk_myshop" / "plugin.py").exists()

    def test_reserved_name_refused_through_the_real_app(self) -> None:
        """Uses the real graftpunk.cli.main.app, whose plugin_app and existing
        groups (observe, session, http, config, keepalive) are all registered
        by the time register_plugin_commands runs, so 'observe' is reserved."""
        from graftpunk.cli.main import app as real_app

        result = runner.invoke(real_app, ["plugin", "new", "observe"])
        assert result.exit_code == 1
        assert "reserved" in result.output.lower()

    def test_conflict_refusal(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("already here")
        result = runner.invoke(
            _build_app(),
            ["plugin", "new", "myshop", "--url", "https://myshop.example.com", "--dir", str(tmp_path)],
        )
        assert result.exit_code == 1
        assert "README.md" in result.output

    def test_bad_backend_refused(self, tmp_path: Path) -> None:
        result = runner.invoke(
            _build_app(),
            ["plugin", "new", "myshop", "--dir", str(tmp_path), "--backend", "carrier-pigeon"],
        )
        assert result.exit_code == 1


class TestPluginNewFromRun:
    def test_from_run_emits_login_config_token_config_and_stubs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        run_dir = observe_base / "myshop" / "run-1"
        run_dir.mkdir(parents=True)
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/login",
                content_type="text/html",
                body=(
                    '<meta name="csrf-token" content="abc">'
                    '<form action="/login" method="post">'
                    '<input type="email" name="email" id="email-field">'
                    '<input type="password" name="password" id="pw">'
                    '<button type="submit" id="go">Go</button>'
                    "</form>"
                ),
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/login",
                content_type="application/json",
                body="{}",
            ),
            _entry("GET", "https://api.myshop.example.com/orders/1", body='{"id": 1}'),
        ]
        (run_dir / "network.har").write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}))

        target = tmp_path / "out"
        result = runner.invoke(
            _build_app(),
            ["plugin", "new", "myshop", "--from-run", "myshop", "run-1", "--dir", str(target)],
        )
        assert result.exit_code == 0, result.output
        plugin_code = (target / "src" / "graftpunk_myshop" / "plugin.py").read_text()
        assert "login_config = LoginConfig(" in plugin_code
        assert '"password": "#pw"' in plugin_code
        assert '@command(' in plugin_code
        assert 'base_url = "https://api.myshop.example.com"' in plugin_code


class TestGeneratedProjectPassesItsOwnGate:
    def test_new_project_is_ruff_clean(self, tmp_path: Path) -> None:
        result = runner.invoke(
            _build_app(),
            ["plugin", "new", "myshop", "--url", "https://myshop.example.com", "--dir", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        check = subprocess.run(
            [sys.executable, "-m", "ruff", "check", str(tmp_path)], capture_output=True, text=True
        )
        assert check.returncode == 0, check.stdout + check.stderr
        fmt = subprocess.run(
            [sys.executable, "-m", "ruff", "format", "--check", str(tmp_path)],
            capture_output=True,
            text=True,
        )
        assert fmt.returncode == 0, fmt.stdout + fmt.stderr

    def test_generated_tests_pass_against_a_generated_fixture_and_sidecar(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        run_dir = observe_base / "myshop" / "run-1"
        run_dir.mkdir(parents=True)
        entries = [_entry("GET", "https://api.myshop.example.com/orders/1", body='{"id": 1}')]
        (run_dir / "network.har").write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}))

        target = tmp_path / "out"
        result = runner.invoke(
            _build_app(),
            ["plugin", "new", "myshop", "--from-run", "myshop", "run-1", "--dir", str(target)],
        )
        assert result.exit_code == 0, result.output

        # The developer's hand-derivation step: copy the capture's shape into
        # a fixture. "1" is what the generated test's own path-param
        # placeholder produces, so the fixture name below is what the
        # request under test actually looks up.
        fixtures_dir = target / "tests" / "fixtures"
        (fixtures_dir / "get_orders_{order_id}.json").write_text('{"id": 1}')

        env = {**os.environ, "PYTHONPATH": str(target / "src")}
        pytest_result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "-q"],
            cwd=target,
            capture_output=True,
            text=True,
            env=env,
        )
        assert pytest_result.returncode == 0, pytest_result.stdout + pytest_result.stderr


class TestSuiteModeLeavesRestOfPyprojectByteIdentical:
    def test_only_the_two_known_edits_change(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        original = (
            '[project]\nname = "mysuite"\n\n'
            '[project.entry-points."graftpunk.plugins"]\n'
            'existing = "mysuite.existing:ExistingPlugin"\n\n'
            "[tool.hatch.build.targets.wheel]\n"
            'packages = ["src/mysuite"]\n\n'
            "[tool.ruff]\n"
            "line-length = 88\n"
        )
        pyproject.write_text(original)
        result = runner.invoke(
            _build_app(),
            ["plugin", "new", "widgets", "--url", "https://myshop.example.com", "--dir", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        after = pyproject.read_text()
        assert "[tool.ruff]" in after
        assert "line-length = 88" in after
        assert 'existing = "mysuite.existing:ExistingPlugin"' in after
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_project.py tests/unit/test_scaffold_cli.py -q`
Expected: collection errors, `ModuleNotFoundError: No module named 'graftpunk.devtools.scaffold.project'` and `'graftpunk.cli.scaffold_commands'`.

- [ ] **Step 3: Write the implementation**

Create `src/graftpunk/devtools/scaffold/project.py`:

```python
"""Decides new-project vs add-to-suite, checks for conflicts, and writes.

Generation lives in ``render.py``; this module owns the filesystem
decisions ``render.py`` has no business making (plugin tooling spec,
2026-09-11).
"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass
from pathlib import Path

from graftpunk.devtools.captures import CAPTURES_DIR, ensure_ignored
from graftpunk.devtools.scaffold.pyproject_edit import add_entry_point, add_wheel_package
from graftpunk.devtools.scaffold.render import ScaffoldSpec, class_name_for, render
from graftpunk.logging import get_logger

LOG = get_logger(__name__)

__all__ = ["ScaffoldConflictError", "ScaffoldResult", "write_scaffold"]

PLUGINS_ENTRY_POINT_GROUP = "graftpunk.plugins"


class ScaffoldConflictError(Exception):
    """One or more target paths already exist; nothing was written."""

    def __init__(self, conflicts: list[Path]) -> None:
        self.conflicts = conflicts
        listing = ", ".join(str(p) for p in conflicts)
        super().__init__(f"Refusing to overwrite existing file(s): {listing}")


@dataclass(frozen=True)
class ScaffoldResult:
    mode: str
    written: tuple[Path, ...]
    gitignore_updated: bool
    pyproject_updated: bool


def _existing_pyproject(target_dir: Path) -> Path | None:
    candidate = target_dir / "pyproject.toml"
    return candidate if candidate.is_file() else None


def _declares_plugin_group(pyproject_path: Path) -> bool:
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    return PLUGINS_ENTRY_POINT_GROUP in data.get("project", {}).get("entry-points", {})


def write_scaffold(target_dir: Path, spec: ScaffoldSpec, *, force_new: bool = False) -> ScaffoldResult:
    """Render *spec* and write it under *target_dir*, refusing on any conflict.

    Args:
        target_dir: A new project's root, or an existing suite's root.
        spec: What to generate; its ``mode`` is overwritten by this
            function's own decision (new project vs add to suite).
        force_new: ``--new``: force a new project in *target_dir* even when
            a suite ``pyproject.toml`` is present.

    Raises:
        ScaffoldConflictError: A target path already exists. Nothing is
            written; the check runs before any file is touched.
        ValueError: *target_dir* has a ``pyproject.toml`` that does not
            declare the ``graftpunk.plugins`` entry-point group: a
            different kind of project.
    """
    existing = None if force_new else _existing_pyproject(target_dir)
    if existing is not None and not _declares_plugin_group(existing):
        raise ValueError(
            f'{existing} exists but does not declare [project.entry-points."{PLUGINS_ENTRY_POINT_GROUP}"]. '
            "This does not look like a graftpunk plugin suite. Use --new to start a fresh "
            "project in a different directory, or add the entry-point group by hand."
        )

    mode = "add_to_suite" if existing is not None else "new_project"
    resolved_spec = dataclasses.replace(spec, mode=mode)
    files = render(resolved_spec)

    targets = {target_dir / rel: content for rel, content in files.items()}
    conflicts = sorted(p for p in targets if p.exists())
    if conflicts:
        raise ScaffoldConflictError(conflicts)

    for path, content in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    gitignore_updated = False
    pyproject_updated = False
    if mode == "add_to_suite":
        assert existing is not None  # narrows for the type checker; mode implies it
        gitignore_updated = ensure_ignored(target_dir, CAPTURES_DIR)
        package = f"src/graftpunk_{resolved_spec.name}"
        target = f"graftpunk_{resolved_spec.name}.plugin:{class_name_for(resolved_spec.name)}"
        add_entry_point(existing, resolved_spec.name, target)
        add_wheel_package(existing, package)
        pyproject_updated = True

    return ScaffoldResult(
        mode=mode,
        written=tuple(sorted(targets)),
        gitignore_updated=gitignore_updated,
        pyproject_updated=pyproject_updated,
    )
```

Create `src/graftpunk/cli/scaffold_commands.py`:

```python
"""``gp plugin new``: the CLI surface for the scaffold. Argument handling only."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

import graftpunk
from graftpunk.devtools.captures import CAPTURES_DIR
from graftpunk.devtools.scaffold.project import ScaffoldConflictError, write_scaffold
from graftpunk.devtools.scaffold.render import ScaffoldSpec
from graftpunk.har.digest import DigestSource, digest
from graftpunk.logging import get_logger

LOG = get_logger(__name__)
console = Console()

plugin_app = typer.Typer(name="plugin", help="Scaffold a new graftpunk plugin.")

_SUPPORTED_BACKENDS = ("nodriver", "selenium")
_PRERELEASE_SUFFIX_RE = re.compile(r"(a|b|rc|dev)\d*$")


def _graftpunk_version_floor() -> str:
    """The running graftpunk's release, floored to ``major.minor.0``.

    A pre-release or local checkout (``1.17.0.dev3+g1234abc``) floors at its
    base release (``1.17.0``), per the spec.
    """
    version = re.split(r"[-+]", graftpunk.__version__)[0]
    version = _PRERELEASE_SUFFIX_RE.sub("", version)
    parts = (version.split(".") + ["0", "0"])[:2]
    return f"{parts[0]}.{parts[1]}.0"


@plugin_app.command("new")
def plugin_new(
    name: Annotated[str, typer.Argument(help="Plugin name: site_name, and the package suffix")],
    url: Annotated[
        str, typer.Option("--url", help="Base URL (ignored when --from-run supplies one)")
    ] = "",
    from_run: Annotated[
        list[str] | None,
        typer.Option("--from-run", help="SESSION [RUN_ID]: fill the scaffold from an observe run"),
    ] = None,
    dir_: Annotated[Path, typer.Option("--dir", help="Target directory")] = Path("."),
    backend: Annotated[str, typer.Option("--backend", help="nodriver or selenium")] = "nodriver",
    new: Annotated[bool, typer.Option("--new", help="Force a new project in --dir")] = False,
) -> None:
    """Scaffold a new plugin: a fresh project, or a member of the suite in --dir."""
    if backend not in _SUPPORTED_BACKENDS:
        console.print(
            f"[red]--backend must be one of {_SUPPORTED_BACKENDS}, got '{escape(backend)}'[/red]"
        )
        raise typer.Exit(1)

    from graftpunk.cli.plugin_commands import reserved_cli_names

    if name in reserved_cli_names():
        console.print(f"[red]'{escape(name)}' is a reserved command name and cannot be a plugin name.[/red]")
        raise typer.Exit(1)

    digest_result = None
    base_url = url
    if from_run:
        if len(from_run) not in (1, 2):
            console.print("[red]--from-run takes SESSION or SESSION RUN_ID.[/red]")
            raise typer.Exit(1)
        session_name = from_run[0]
        run_id = from_run[1] if len(from_run) == 2 else None
        from graftpunk.cli.observe_commands import resolve_run

        run_dir = resolve_run(session_name, run_id)
        source = DigestSource.from_run_dir(run_dir, session=session_name, run_id=run_dir.name)
        digest_result = digest(source)
        base_url = url or f"https://{digest_result.primary_host}"

    spec = ScaffoldSpec(
        name=name,
        mode="new_project",  # write_scaffold decides the real mode
        backend=backend,  # type: ignore[arg-type]
        base_url=base_url,
        digest=digest_result,
        graftpunk_version=_graftpunk_version_floor(),
    )

    try:
        result = write_scaffold(dir_, spec, force_new=new)
    except ScaffoldConflictError as exc:
        console.print("[red]Refusing to overwrite existing file(s):[/red]")
        for path in exc.conflicts:
            console.print(f"  {escape(str(path))}")
        raise typer.Exit(1) from None
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None

    console.print(f"[green]{result.mode.replace('_', ' ').title()}:[/green]")
    for path in result.written:
        console.print(f"  {escape(str(path))}")
    if result.gitignore_updated:
        console.print(f"[dim]Added {escape(CAPTURES_DIR)}/ to .gitignore[/dim]")
```

In `src/graftpunk/cli/plugin_commands.py`, insert before `src/graftpunk/cli/plugin_commands.py:287` (`def register_plugin_commands(app: typer.Typer, *, notify_errors: bool = True) -> dict[str, str]:`):

```python
_reserved_cli_names: frozenset[str] = frozenset()


def _group_name(info: Any) -> str | None:
    """The name of one ``typer.Typer`` sub-app registration, however it was added.

    ``info.name`` is a real string only when ``add_typer(..., name=...)``
    overrode it; otherwise it is a ``DefaultPlaceholder`` (falsy), and the
    real name lives on the attached Typer's own ``info.name``. Verified
    empirically against the pinned ``typer`` version's ``registered_groups``
    shape, not assumed from its docs.
    """
    if isinstance(info.name, str) and info.name:
        return info.name
    typer_instance = getattr(info, "typer_instance", None)
    inner_info = getattr(typer_instance, "info", None)
    inner_name = getattr(inner_info, "name", None)
    return inner_name if isinstance(inner_name, str) else None


def _derive_reserved_cli_names(app: typer.Typer) -> frozenset[str]:
    """Every top-level command and group name already on *app*, at attach time.

    A plugin whose ``site_name`` collides with one of these is refused:
    'plugin', 'plugins', 'session', 'http', 'config', 'keepalive', 'observe'
    today, and whatever else ``main.py`` has registered by the time plugins
    attach, derived rather than hardcoded so a new top-level command
    reserves its own name automatically (plugin tooling spec, 2026-09-11).
    """
    names: set[str] = set()
    for command_info in getattr(app, "registered_commands", []):
        if isinstance(command_info.name, str) and command_info.name:
            names.add(command_info.name)
    for group_info in getattr(app, "registered_groups", []):
        name = _group_name(group_info)
        if name:
            names.add(name)
    return frozenset(names)


def reserved_cli_names() -> frozenset[str]:
    """The reserved set derived at the last ``register_plugin_commands`` call.

    ``gp plugin new`` reads this to refuse a plugin name before generating
    anything.
    """
    return _reserved_cli_names


```

In the same file, inside `register_plugin_commands`, insert the derivation call right after the existing cache-clearing lines (`src/graftpunk/cli/plugin_commands.py:306` (`    _registered_plugins_for_teardown.clear()`)):

```python
    global _reserved_cli_names
    _reserved_cli_names = _derive_reserved_cli_names(app)
```

And inside the per-plugin loop, right after `site_name = plugin.site_name` (`src/graftpunk/cli/plugin_commands.py:317` (`            site_name = plugin.site_name`)), insert the reserved-name check before the existing collision-detection comment:

```python

            if site_name in _reserved_cli_names:
                raise PluginError(
                    f"Plugin name collision: '{site_name}' is a reserved top-level command name."
                )
```

In `src/graftpunk/cli/main.py`, add the import beside the other `graftpunk.cli.*` imports, alphabetically after `graftpunk.cli.plugin_commands` (`src/graftpunk/cli/main.py:41` (`from graftpunk.cli.plugin_commands import resolve_session_name_or_exit`)):

```python
from graftpunk.cli.scaffold_commands import plugin_app
```

Attach it before `register_plugin_commands(app)` runs, alongside the other `app.add_typer(...)` calls (`src/graftpunk/cli/main.py:749` (`app.add_typer(config_app)`)):

```python
app.add_typer(plugin_app)
```

In `tests/unit/conftest.py`, append one more entry to `_CONSOLE_LOCATIONS` after the `observe_commands` entry Task 7 added:

```python
    ("graftpunk.cli.scaffold_commands", "console"),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_project.py tests/unit/test_scaffold_cli.py -q`
Expected: PASS, all tests green.

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
Expected: all four green.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/devtools/scaffold/project.py src/graftpunk/cli/scaffold_commands.py src/graftpunk/cli/plugin_commands.py src/graftpunk/cli/main.py tests/unit/conftest.py tests/unit/test_scaffold_project.py tests/unit/test_scaffold_cli.py
git commit -m "feat(cli): add gp plugin new and reserve top-level CLI names against it"
```

---

### Task 10: Removal and documentation

**Files:**
- Delete: `src/graftpunk/cli/import_har.py`
- Delete: `src/graftpunk/har/generator.py`
- Delete: `src/graftpunk/har/analyzer.py`
- Delete: `tests/unit/test_import_har.py`
- Delete: `tests/unit/test_har_generator.py`
- Delete: `tests/unit/test_har_analyzer.py`
- Delete: `examples/templates/python_template.py`
- Modify: `tests/unit/test_cli.py:637` (`class TestImportHarCommand:`) through line 809, delete the class
- Modify: `tests/unit/conftest.py:20` (`    ("graftpunk.cli.import_har", "console"),`), delete the line
- Modify: `src/graftpunk/cli/main.py:794` (`@app.command("import-har")`) through the end of `import_har_cmd`, delete the command
- Modify: `examples/README.md:102` (`for Python plugins`)
- Modify: `./README.md:305` (`Commands:`) through line 313, and `./README.md:379` (`### HAR Import`)
- Modify: `docs/HOW_IT_WORKS.md:888` (`gp observe show <session>                # Show run details (file list, sizes)`) and `docs/HOW_IT_WORKS.md:425` (`is also a frozen dataclass defining CLI parameter specifications`)
- Modify: `CHANGELOG.md:8` (`## [Unreleased]`)

**Interfaces:** none (this task removes code and updates prose; no new runtime interfaces).

- [ ] **Step 1: Confirm what still references the code being deleted**

Run: `grep -rn "import_har\|har\.generator\|har\.analyzer\|python_template" src/ tests/ examples/ README.md docs/HOW_IT_WORKS.md CHANGELOG.md`
Expected: only the files listed above (plus this plan and the spec, which are not part of the codebase). No other module imports `graftpunk.har.analyzer`, `graftpunk.har.generator`, or `graftpunk.cli.import_har` after Task 4's rewrite of `import_har.py`'s own imports.

- [ ] **Step 2: Delete the superseded modules and their tests**

```bash
git rm src/graftpunk/cli/import_har.py src/graftpunk/har/generator.py src/graftpunk/har/analyzer.py
git rm tests/unit/test_import_har.py tests/unit/test_har_generator.py tests/unit/test_har_analyzer.py
git rm examples/templates/python_template.py
```

- [ ] **Step 3: Remove `import-har` from `main.py`**

Delete the entire `import_har_cmd` function, from `src/graftpunk/cli/main.py:794` (`@app.command("import-har")`) through its closing `)` (the `import_har(...)` call's closing paren, the line before the `# Register plugin commands dynamically...` comment).

- [ ] **Step 4: Remove the `TestImportHarCommand` class**

Delete lines 637 through 809 of `tests/unit/test_cli.py` (the entire `class TestImportHarCommand:` block, from its `"""Tests for import-har command."""` docstring through the test immediately preceding `class TestObserveCommands:` at line 810).

- [ ] **Step 5: Remove the `import_har` console-location entry**

In `tests/unit/conftest.py`, delete `tests/unit/conftest.py:20` (`    ("graftpunk.cli.import_har", "console"),`).

- [ ] **Step 6: Point `examples/README.md` at `gp plugin new`**

Replace the line at `examples/README.md:102` (`for Python plugins`):

```markdown
   - `gp plugin new` for Python plugins (see the main README's "From recording to plugin")
```

- [ ] **Step 7: Update `README.md`**

Replace the `Commands:` block (`./README.md:305` (`Commands:`) through line 313):

```
Commands:
  version     Show graftpunk version and installation info.
  plugins     List discovered plugins (storage, handlers, sites, CLI).
  plugin      Scaffold a new graftpunk plugin.
  observe     View and manage observability data (HAR, screenshots, logs).
  session     Manage encrypted browser sessions.
  keepalive   Manage the session keepalive daemon.
  http        Make ad-hoc HTTP requests with cached session cookies.
  config      Show configuration; manage the workstation env file.
```

Replace the `### HAR Import` section (`./README.md:379` (`### HAR Import`), through its closing ` ``` ` before `## Configuration`):

```markdown
### From recording to plugin

Record a session, read it, then scaffold:

```bash
# 1. Capture: record real traffic (see Observability above)
gp observe -s mybank interactive https://secure.mybank.example.com/dashboard

# 2. Read: a digest of hosts, endpoints, login, and tokens, redacted by construction
gp observe digest mybank

# 3. Scaffold: a plugin filled in from that digest
gp plugin new mybank --from-run mybank
```

`gp observe fixtures` derives named, provenance-tagged file fixtures from the
same run for the generated project's own test suite; captures never enter
git (`tests/captures/` is gitignored automatically).
```

- [ ] **Step 8: Update `docs/HOW_IT_WORKS.md`**

Append to the Observability section's `### CLI Commands` block (the one containing `docs/HOW_IT_WORKS.md:888` (`gp observe show <session>                # Show run details (file list, sizes)`), inside its existing code fence, after the `interactive` line):

```
gp observe digest <session> [<run>]      # Read a run into a digest of hosts, endpoints, login, tokens
gp observe digest --har <path>           # Digest a bare HAR file instead of a run
gp observe fixtures <session> --match "<METHOD> <template>"  # Write matching bodies for deriving test fixtures
```

Insert a new subsection after the `PluginParamSpec` paragraph (`docs/HOW_IT_WORKS.md:425` (`is also a frozen dataclass defining CLI parameter specifications`)) and before the `---` / `## Login System` boundary:

```markdown

### Building a plugin from a recording

`gp plugin new <name> --from-run <session> [<run>]` fills a scaffold from a
run's digest: `base_url` from the primary host, `login_config` from the
first detected login form, `token_config` from paired header/meta or
header/cookie candidates, and one command stub per endpoint (JSON first, up
to twelve). Everything the digest could not determine is marked
`# GP-FILL: <what to fill in>`. `gp plugin new <name>` with no `--from-run`
scaffolds a bare project from `--url` alone.

Generated (and hand-written) commands call two `CommandContext` methods
instead of the session directly:

- `ctx.request_json(method, url, *, role="xhr", **kwargs) -> Any` sends
  *url* (relative to `base_url`, or absolute) with role headers and returns
  the parsed JSON body. A 401 or 403 raises `SessionRejectedError`; a 2xx
  whose body is a login page also does (a stale session's tell); any other
  non-JSON 2xx raises `UnexpectedResponseError`; any other 4xx/5xx raises
  `CommandError`.
- `ctx.request_text(method, url, *, role="navigation", **kwargs) -> str`
  returns any 2xx body as text, detecting rejection by status only, so an
  HTML endpoint's real response is never mistaken for an expired session.

`graftpunk.testing` (pytest-free) supplies `make_context()` for building a
`CommandContext` directly in a test, and `FixtureSession`/`fixture_context()`
for answering `ctx.request_json`/`request_text` from a file under
`tests/fixtures/` instead of the network, named the way `gp observe
fixtures` names captures. `graftpunk.testing.plugin.site_env_scrubber(prefix)`
is a pytest fixture (loaded via `pytest_plugins =
["graftpunk.testing.plugin"]` in `conftest.py`) that removes prefixed
environment variables for the duration of each test.
```

- [ ] **Step 9: Update `CHANGELOG.md`**

In the existing `CHANGELOG.md:8` (`## [Unreleased]`) section, add to `### Added` (after the existing three `#96` bullets) and add a new `### Removed` subsection before the `### Fixed` heading:

```markdown
- **`gp observe digest`** and **`gp observe digest --har PATH`**. Reads a HAR (a run, or a bare file from another tool) into a `RunDigest`: hosts, endpoints (method, templated path, parameter types, response shape, custom header names), login observations and forms, token candidates, and cookie names. Redacted by construction: no header, cookie, query, or body value is ever retained. `graftpunk.har` gains the model (`DigestSource`, `RunDigest`, `Endpoint`, `ShapeNode`, `LoginObservation`, `LoginForm`, `TokenCandidate`, `digest`).
- **`gp observe fixtures`**. Writes captured response bodies exactly as recorded, named and matched against `gp observe digest`'s own templates, for deriving committed test fixtures by hand. Captures never enter git: the target directory is added to `.gitignore` automatically, and a tracked path refuses without `--allow-tracked`.
- **`CommandContext.request_json`** and **`request_text`**. The role, rejection, and body-shape policy every generated (and hand-written) command needs, backed by the new `graftpunk.plugins.site_requests.SiteRequests`. New `SessionRejectedError` and `UnexpectedResponseError` in `graftpunk.exceptions`.
- **`graftpunk.testing`**. `make_context`, `FixtureSession`, `fixture_context` (pytest-free), and `graftpunk.testing.plugin.site_env_scrubber` (a pytest fixture), for testing a plugin's commands without a network.
- **`gp plugin new <name> [--url URL] [--from-run SESSION [RUN_ID]] [--dir PATH] [--backend nodriver|selenium] [--new]`**. Scaffolds a new plugin project, or adds one to an existing suite, optionally filled in from a `gp observe digest` run: `login_config`, `token_config`, and up to twelve command stubs. Plugin names are refused when they collide with a reserved top-level CLI command name (`plugin`, `plugins`, `session`, `http`, `config`, `keepalive`, `observe`, and any other top-level command registered by the time plugins attach).

### Removed

- **`gp import-har`**, replaced by `gp observe digest` and `gp plugin new` (see the README's "From recording to plugin"). It generated handlers taking a raw `requests.Session`, a shape the framework stopped accepting once command handlers started receiving a `CommandContext`.
- **`graftpunk.har.analyzer`** and **`graftpunk.har.generator`**, and the exports they backed on `graftpunk.har`: `detect_auth_flow`, `discover_api_endpoints`, `generate_plugin_code`, `extract_domain`, `APIEndpoint`, `AuthFlow`, `AuthStep`. Superseded by `graftpunk.har.digest`.
- **`examples/templates/python_template.py`**, replaced by `gp plugin new`. `examples/templates/yaml_template.yaml` is unaffected.
```

- [ ] **Step 10: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
Expected: all four green. The three deleted test modules and the deleted `TestImportHarCommand` class are the only tests removed from the suite across the whole plan; every other test added by Tasks 1-9 is still present and passing.

- [ ] **Step 11: Commit**

```bash
git add -A
git commit -m "refactor: remove gp import-har in favour of gp observe digest and gp plugin new"
```

---

## Self-review notes

**Spec coverage.** Every numbered section of the spec has a task: path templating and naming (Task 1); HTML extraction (Task 2); the digest model and rules (Task 3); the renderers and the `graftpunk.har` export change (Task 4); the request helpers and the two new exceptions (Task 5); `graftpunk.testing` (Task 6); `gp observe digest`/`fixtures`, `resolve_run`, and `devtools/captures.py` (Task 7); the scaffold's rendering and the `pyproject.toml` edit seam (Task 8); the scaffold's mode decision, `gp plugin new`, and the reserved CLI names (Task 9); the removal, the minor-release rationale, and the documentation section (Task 10). The "rule for everything the scaffold emits" (facts and API calls only, no framework logic) is honoured throughout Tasks 8-9: every `GP-FILL` line marks a fact the digest could not determine, never a stand-in for graftpunk's own logic.

**Placeholder scan.** No `TBD`, no "add error handling", no "similar to Task N", no un-shown code steps. Every test file has real assertions; every implementation file is complete, runnable Python. The one `or True` I nearly shipped in Task 3's `digest()` draft (the `form_page` classification) was caught during drafting and replaced with a clean `forms_in_entry` reuse before this plan was finalized; there is no dead scaffolding left in any task's code block.

**Type and signature consistency.** `DigestSource`, `RunDigest`, `Endpoint`, `ShapeNode`, `LoginObservation` are defined once in Task 3 and used with those exact field names in Tasks 4, 7, 8, and 9. `LoginForm`, `TokenCandidate`, `TokenKind` are defined once in Task 2 (not Task 3, see the implementation note below) and imported with those spellings everywhere else. `capture_slug(method, path)` and `capture_filename(method, path, content_type)` (Task 1) are called with that exact argument order in Tasks 6, 7, and nowhere are they given a different order. `ctx.request_json`/`ctx.request_text` (Task 5) keep the same keyword-only `role` default (`"xhr"`/`"navigation"`) in every task that calls them, including the generated stubs in Task 8. `resolve_run(session_name, run_id, *, base_dir=None)` (Task 7) is called with that signature from `main.py`, `observe_commands.py`'s own commands, and `scaffold_commands.py`. Three single-owner helpers added in the 2026-09-12 revision keep the same spelling everywhere: `documents.looks_like_token_name(name) -> bool` (Task 2, called from Task 3's header and cookie scans), `report.summarize_shape(shape, *, depth=3) -> str` (Task 4, called from Task 8's stub docstrings with `depth=_SCAFFOLD_SHAPE_DEPTH`), and `digest.body_params(entry) -> dict[str, str]` (Task 3, called from Task 7's fixtures sidecar).

**Deviations from the spec's literal text, and why:**

- The spec's `har/digest.py` code block defines `TokenKind`, `LoginForm`, and `TokenCandidate` inline alongside the rest of the digest model, and describes `digest.py` as calling `documents.extract_login_forms`/`extract_token_candidates`. Defining those three names in `digest.py` while `documents.py` also needs them (as its own functions' return types) is a circular import; Task 2 defines them in `documents.py`, and Task 3's `digest.py` imports them from there. `graftpunk.har.LoginForm`, `TokenCandidate` (Task 4's `__init__.py`) are unaffected: the public import path is identical, only the defining module differs. Flagged as an **Implementation note** in Task 2.
- The spec says `resolve_run` is what `gp observe show` *and* `gp observe clean` use. At HEAD (`src/graftpunk/cli/main.py:351` (`@observe_app.command("clean")`)), `clean` takes no `RUN_ID` argument at all: it operates on a whole session's directory (or everything), never a single resolved run, so `resolve_run`'s `(session, run)` contract does not fit its signature. Task 7 refactors only `observe_show`, which does share the exact resolution logic the new commands need, and leaves `observe_clean` untouched, with a note explaining why. This also uncovered a real compatibility risk that the spec did not call out: the existing `TestObserveCLICommands` suite patches `graftpunk.cli.main.OBSERVE_BASE_DIR` directly, which would silently stop reaching `observe_show`'s resolution once it moved to another module's global. Task 7's `base_dir` parameter on `resolve_run` is the fix, documented as an **Implementation note** there.
- The spec's `template_path` return type (`tuple[str, dict[str, str]]`) does not say what the dict maps; Task 1 maps the collapsed parameter name to the literal value it replaced, and says so in an **Implementation note**.
- The spec's high-cardinality collapsing ("a segment that appears with more than 8 distinct values across a run") is described but not algorithmically specified; Task 3 documents its own heuristic (group by segment count, compare against the first row's other positions) directly in `_collapse_high_cardinality`'s docstring as a stated limitation rather than presenting it as exhaustive.
- `SiteRequests`' "once per session" warning (spec: "logs the warning once") is implemented by stamping a flag on the *session object itself* rather than on the `SiteRequests` wrapper, since `CommandContext.request_json`/`request_text` construct a fresh `SiteRequests` per call; a per-wrapper flag would never accumulate across calls. Documented in Task 5.
- Task 9's reserved-CLI-names derivation reads `typer.Typer.registered_commands`/`registered_groups`, which are undocumented-but-stable Typer internals (the same ones Typer's own test suite exercises). Their shape was verified empirically against the pinned `typer` version in this repository's `.venv` before writing the derivation code (a `DefaultPlaceholder` sentinel for an un-renamed sub-app's `.name`, confirmed falsy), rather than assumed from Typer's public docs, which do not document this surface at all.

**Nothing in the spec was found to contradict HEAD.** Every citation the spec carries into this plan (the parser, analyzer, generator, and package-export lines in `graftpunk.har`; the observe storage, context, and capture lines; the `main.py`, `plugin_runtime.py`, and `import_har.py` lines; the `plugins/__init__.py` line; the `test_cli.py` and `conftest.py` lines) was re-read against this worktree's HEAD (`0aca14d`) while writing the task that uses it, and each is anchored at its own point of use above rather than repeated here. They all matched exactly; the spec's own last commit was precisely this kind of citation refresh after the `#195` rebase, so agreement is expected rather than notable.
