---
type: plan
validated:
  sha: 7bd960466f6e1484ad4e5b85e5d943b83e3948e2
  date: 2026-09-23T06:14:41Z
  reviewers: [fact-check, solid-hygiene]
  findings:
    critical: 0
    important: 0
    medium: 3
    low: 9
    nitpick: 0
  net_negative_raised: 0
  net_negative_addressed: 0
  net_negative_remaining: 0
---

# Graft Package Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the first of the three pull requests the graft skill design orders: the package pieces that stand on their own (schema numbers, the committable sidecar, the digest's `login_flow` flag and its versioned `--endpoints-json` projection, `gp version --json --at-least`, `gp plugin new --check-name`, the fixtures-root rule, the endpoint and command-spec parsers, the one write discipline, the `render.py` split, and the declared endpoint and typed parameters on every generated stub).

**Architecture:** Every versioned payload takes its number from one new module, `graftpunk/contracts.py`. The sidecar format gets one owner on the pytest-free side of `graftpunk.testing` (`testing/sidecar.py`), with its writer in `devtools/captures.py`. The digest gains a `login_flow` flag that the generator reads instead of recomputing, and `har/report.py` gains an explicit, versioned projection. `har/naming.py` becomes the one parser of `"<METHOD> <template>"` and `"<name>=<METHOD> <template>"`. Under `devtools/scaffold/`, the formatting helpers move to `pysrc.py`, the fixtures-root rule moves to a new declarative `policy.py`, and every scaffold write goes through a new `write.py`.

**Tech Stack:** Python 3.11+, Typer 0.21+, `packaging` (new runtime dependency), `ast`, `tomllib`, `hashlib` (stdlib), pytest via `uv run`, ruff, ty 0.0.75.

**Spec:** `docs/superpowers/specs/2026-09-21-graft-skill-design.md` (validated 2026-09-22). Read it alongside this plan. This plan implements the first pull request of the three the spec orders at the end of "Package changes, delivered first". The second is `docs/superpowers/plans/2026-09-22-graft-package-project-tools.md` and the third is `docs/superpowers/plans/2026-09-22-graft-skill.md`; both consume the names this plan produces exactly as its Interfaces blocks state them.

## Global Constraints

- The placement rule in `src/graftpunk/devtools/__init__.py`: public API a plugin imports at runtime or in its own tests lives at the top level of the package; CLI-only tooling that a plugin never imports lives under `graftpunk.devtools`. `graftpunk.testing` imports nothing from `graftpunk.devtools`, and a test asserts it on the import graph. The one recorded exception is the `endpoint` field on `CommandMetadata`, tooling provenance on a runtime object, documented as such in the decorator's docstring.
- Placeholders only, in code, tests, docs, and commit messages: `myshop`, `myshop.example`, `alice@example.com`, and `example.com` and `example.net` hosts. Never a real site, vendor, account, `op://` path, or a named secret manager.
- No em dashes or en dashes, and no spaced double hyphen standing in for one, in any file, docstring, comment, commit message, or plan text.
- Every commit subject is in the repository's `type(scope): subject` form.
- No Claude attribution in commits: no `Co-Authored-By`, no "Generated with" footer, no `Claude-Session` trailer.
- Python `>=3.11` typing: `X | None`, `Literal[...]` for closed string sets, `from __future__ import annotations` at the top of every new module.
- Tests assert behaviour, never that a mock was called. Fault injection is allowed; the assertions stay on what is on disk or printed.
- Within the package, `graftpunk/contracts.py` is the only module that declares the current schema number, and the only module that compares one: `refuse_unknown_schema` for a payload a reader loads, and `contract_mismatch` for the `gp version --contract` handshake.
- The full gate, green at the end of every task and run in full by the last task: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
- A single test runs as `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/<file>.py::<test> -q`. Use `uv sync -p 3.12` if the virtualenv was built on 3.14 (the suite fakes failures there).

## Review Focus

1. A capture whose flagged name is a common word (a cookie called `sid`) makes `flagged_names` match ordinary text: the writer must record names exactly as the digest holds them and never widen them, so the in-suite check (next pull request) sees only real names. Pinned in Task 7 (`test_flagged_names_are_exactly_the_digest_cookie_and_token_names`).
2. A `--match` or `--command` value with surrounding whitespace or a tab between method and template (`" GET  /orders "`, `"GET\t/orders"`): a person expects the digest's own printed form with stray spaces to work, and a tab to be refused rather than matched as nothing. Pinned in Task 6 (`test_surrounding_whitespace_is_accepted_and_a_tab_is_refused`).
3. `gp version --json` run where graftpunk is installed from a local checkout (`0.0.0+unknown` or `1.17.0.dev3+g1234abc`): `--at-least` must order a local or dev version by `packaging` rules rather than crash. Pinned in Task 5 (`test_a_dev_or_local_version_is_ordered_not_rejected`).
4. A sidecar that is valid JSON but not an object (a list, a string), or whose `status` is a string: `FixtureSession` must refuse it with the file named, never answer a test with a half-read sidecar. Pinned in Task 7 (`test_a_sidecar_that_is_not_an_object_or_has_a_wrong_type_is_refused`).
5. A planned write whose target directory cannot be created (a file sits where a parent directory should be): `write.py` must refuse and restore, leaving no partial file or `.gp-partial` temp behind. Pinned in Task 10 (`test_a_parent_that_is_a_file_restores_and_leaves_no_temp`).

## File Structure

| File | Responsibility |
| --- | --- |
| `src/graftpunk/devtools/scaffold/pysrc.py` (new, Task 1) | The Python-source formatting helpers moved out of `render.py`: width, indentation, quoting, escaping, wrapping, literal, dict, call, URL, and import line builders. The ones another module imports get public names and an `__all__` in a second commit. |
| `src/graftpunk/contracts.py` (new, Tasks 2, 5) | `ENDPOINTS_SCHEMA`, `SIDECAR_SCHEMA`, `CLI_SURFACES`, `current_schema`, `cli_contracts`, `refuse_unknown_schema`, `UnknownSchemaError`, `contract_mismatch`, and `installation_facts`, the payload `gp version --json` prints, beside `cli_contracts`. |
| `src/graftpunk/har/digest.py` (modify, Tasks 3, 7) | `Endpoint.login_flow`; `_login_flow_pairs` and `_with_login_flow` moved in from the generator (Task 3); `flagged_names_of`, the names a fixture must not contain, beside the digest that records them (Task 7). |
| `src/graftpunk/har/report.py` (modify, Task 4) | `endpoints_projection`, `render_endpoints_json`. |
| `src/graftpunk/cli/observe_commands.py` (modify, Tasks 4, 6, 7) | `--endpoints-json`; the matcher consumes `parse_endpoint`'s pair; fixtures write their sidecar through the devtools writer. |
| `src/graftpunk/cli/main.py` (modify, Task 5) | `gp version --json --at-least --contract`, which prints `contracts.installation_facts()` and builds nothing itself. |
| `pyproject.toml`, `uv.lock` (modify, Task 5) | `packaging` joins the runtime dependencies. |
| `src/graftpunk/har/naming.py` (modify, Task 6) | `HTTP_METHODS`, `EndpointSpecError`, `parse_endpoint`, `parse_command_spec`. |
| `src/graftpunk/testing/sidecar.py` (new, Task 7) | `SIDECAR_SUFFIX`, `SIDECAR_FIELDS`, `Sidecar`, `SidecarError`, `sidecar_path`, `is_sidecar`, `sidecar_payload`, `sidecar_text`, `load_sidecar`. |
| `src/graftpunk/testing/__init__.py` (modify, Task 7) | `FixtureSession` reads sidecars through the owner. |
| `src/graftpunk/devtools/captures.py` (modify, Tasks 7, 10) | The writing side of the captures rule: `write_sidecar`, which takes the flagged names as plain strings (Task 7); `ensure_ignored` applies `captures_rule.with_ignored` to disk, and `CAPTURES_DIR` moves out (Task 10). |
| `src/graftpunk/devtools/captures_rule.py` (new, Task 10) | The pure side of the captures rule: `CAPTURES_DIR` and `with_ignored`, the text edit. Imports nothing that touches the filesystem, so scaffold modules import the rule from here. |
| `docs/PLUGIN_DEVELOPMENT.md`, `docs/HOW_IT_WORKS.md` (modify, Task 7) | The sidecar example and the sidecar field list describe the schema 1 format. |
| `src/graftpunk/cli/scaffold_commands.py` (modify, Tasks 8, 10) | `gp plugin new --check-name`, `_name_refusal` (Task 8); `plugin_new`'s `ScaffoldWriteError` arm (Task 10). |
| `src/graftpunk/devtools/scaffold/policy.py` (new, Task 9) | `TESTS_DIR`, `FIXTURES_TREE`, `fixtures_root`. |
| `src/graftpunk/devtools/scaffold/render.py` (modify, Tasks 1, 3, 9, 11) | Loses the formatting helpers, the login-flow computation, and the fixtures-root rule; gains the declared endpoint and typed parameters on every stub. |
| `src/graftpunk/devtools/errors.py` (new, Task 10) | `DevtoolsRefusal`, the one base class of every devtools refusal a CLI entry point catches; `ScaffoldWriteError`, a write that failed, naming any path the writer's restore could not put back, and carrying the wrapped error's `errno`, `strerror`, and `filename`. |
| `src/graftpunk/devtools/scaffold/write.py` (new, Task 10) | `PlannedChange`, `apply_changes`, `find_conflicts`, `validate_python`, `validate_toml`, `ChangeConflictError`, `InvalidChangeError`; an `OSError` during a write reaches the caller as `ScaffoldWriteError`. |
| `src/graftpunk/devtools/scaffold/pyproject_edit.py` (modify, Task 10) | Pure `with_entry_point` and `with_wheel_package`; the file-writing `add_entry_point` and `add_wheel_package` are removed. |
| `src/graftpunk/devtools/scaffold/project.py` (modify, Task 10) | `write_scaffold` plans its changes and applies them through `write.py`. |
| `src/graftpunk/plugins/cli_plugin.py` (modify, Task 11) | `CommandMetadata.endpoint`; `@command(endpoint=...)`. |
| `docs/PLUGIN_DEVELOPMENT.md` (modify, Task 11) | "What gets filled in" and "CLI parameter types" describe the new stub. |
| `CHANGELOG.md` (modify, Task 12) | One Added line; the unreleased `gp observe fixtures` line corrected for the new sidecar. |

---

### Task 1: Split the formatting helpers out of `render.py` into `pysrc.py`

A pure move, first, so every later task edits the post-split `render.py` and a bisect can tell a move from a behaviour change. The move keeps every name as it is; a second commit (Steps 7 to 9) then gives the helpers another module imports public names and gives `pysrc.py` an `__all__`, so no module imports a private name from it. This closes item 2 of issue #201.

**Files:**
- Create: `src/graftpunk/devtools/scaffold/pysrc.py`
- Modify: `src/graftpunk/devtools/scaffold/render.py:49-62`, `:371-608`, `:611-625`, `:674`, `:695-770`, `:1029-1038` (moved out)
- Modify: `tests/unit/test_scaffold_render.py:14-36` (import block)
- Test: `tests/unit/test_scaffold_pysrc.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `graftpunk.devtools.scaffold.pysrc` defining the 28 helpers the move carries. After Step 8, the sixteen that `render.py` imports are public and listed in `pysrc.__all__`: `GENERATED_LINE_LENGTH`, `L1`, `L2`, `L3`, `INDENT_STEP`, `URL_PLACEHOLDER_RE`, `wrapped_docstring_lines`, `wrapped_comment_lines`, `wrapped_docstring_block`, `quoted_literal` (was `_quoted`), `exploded_dict_lines`, `call_expression_lines` (was `_call_lines`), `literal_lines`, `literal_dict_entry_lines`, `url_expr_lines`, and `import_lines(module: str, name: str) -> list[str]`. The other twelve stay private to `pysrc.py` (`_L4`, `_DOCSTRING_WRAP_WIDTH`, `_escaped_for_docstring`, `_repaired_escape_splits`, `_escaped_docstring_wrap`, `_quote_char`, `_escaped_body`, `_split_key_lines`, `_dict_entry_lines`, `_quoted_fstring`, `_url_atoms`, `_url_chunks`). Task 11 generalises `import_lines` to `(module: str, *names: str)`. Every later task, in this plan and the next, uses the public names, and any name another module imports from `pysrc` joins `__all__` (a test in `tests/unit/test_scaffold_pysrc.py` holds that).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_scaffold_pysrc.py`:

```python
"""pysrc.py owns the Python-source formatting helpers and render.py keeps the
per-artifact renderers (graft skill spec, 2026-09-21; issue #201, item 2)."""

from __future__ import annotations

import ast
from pathlib import Path
from types import ModuleType

import graftpunk.devtools.scaffold.pysrc as pysrc
import graftpunk.devtools.scaffold.render as render

_FORMATTING_HELPERS = frozenset(
    {
        "_GENERATED_LINE_LENGTH",
        "_L1",
        "_L2",
        "_L3",
        "_L4",
        "_INDENT_STEP",
        "_DOCSTRING_WRAP_WIDTH",
        "_escaped_for_docstring",
        "_repaired_escape_splits",
        "_escaped_docstring_wrap",
        "_wrapped_docstring_lines",
        "_wrapped_comment_lines",
        "_wrapped_docstring_block",
        "_quoted",
        "_quote_char",
        "_escaped_body",
        "_split_key_lines",
        "_dict_entry_lines",
        "_exploded_dict_lines",
        "_call_lines",
        "_literal_lines",
        "_literal_dict_entry_lines",
        "_URL_PLACEHOLDER_RE",
        "_quoted_fstring",
        "_url_atoms",
        "_url_chunks",
        "_url_expr_lines",
        "_import_lines",
    }
)


def _defined_at_module_level(module: ModuleType) -> set[str]:
    assert module.__file__ is not None
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_pysrc_defines_every_formatting_helper() -> None:
    assert _FORMATTING_HELPERS <= _defined_at_module_level(pysrc)


def test_render_defines_none_of_the_formatting_helpers() -> None:
    assert _FORMATTING_HELPERS & _defined_at_module_level(render) == set()


def test_pysrc_imports_nothing_from_graftpunk() -> None:
    """The helpers format text; they know nothing about digests or specs."""
    assert pysrc.__file__ is not None
    tree = ast.parse(Path(pysrc.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert {name for name in imported if name.startswith("graftpunk")} == set()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_pysrc.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'graftpunk.devtools.scaffold.pysrc'`.

- [ ] **Step 3: Move the helpers with a checked script**

The ranges below were verified at `eeb6e73`; the script refuses to run if any first line has moved. Run from the worktree root:

```bash
uv run python - <<'PY'
import re
from pathlib import Path

render = Path("src/graftpunk/devtools/scaffold/render.py")
pysrc = Path("src/graftpunk/devtools/scaffold/pysrc.py")
ranges = [(49, 62), (371, 608), (611, 625), (674, 674), (695, 770), (1029, 1038)]
first_lines = {
    49: "# The line length a generated project's own [tool.ruff] declares",
    371: "def _escaped_for_docstring(",
    611: "def _literal_dict_entry_lines(",
    674: "_URL_PLACEHOLDER_RE = ",
    695: "def _quoted_fstring(",
    1029: "def _import_lines(",
}
lines = render.read_text(encoding="utf-8").splitlines(keepends=True)
for number, prefix in first_lines.items():
    assert lines[number - 1].startswith(prefix), (number, lines[number - 1])
moved_blocks = ["".join(lines[start - 1 : end]) for start, end in ranges]
dropped = {n for start, end in ranges for n in range(start, end + 1)}
kept = "".join(line for n, line in enumerate(lines, start=1) if n not in dropped)
moved_text = "\n\n".join(block.rstrip("\n") + "\n" for block in moved_blocks)
names = [
    a or b
    for a, b in re.findall(r"^(_[A-Za-z0-9_]+)\s*[:=]|^def (_[A-Za-z0-9_]+)\(", moved_text, re.M)
]
used = [name for name in dict.fromkeys(names) if re.search(rf"\b{re.escape(name)}\b", kept)]
header = '''"""Python-source formatting for generated files: width, indentation, quoting,
escaping, and wrapping.

Every helper here turns a captured site fact into Python source that parses
and that ``ruff format`` leaves unchanged at the generated project's line
length. ``render.py`` decides what a generated file says; this module decides
how each line of it is spelled (issue #201, item 2). The names keep their
leading underscore because they are internal to ``graftpunk.devtools.scaffold``;
moving them here changed no behaviour.
"""

from __future__ import annotations

import json
import re
import textwrap


'''
pysrc.write_text(header + moved_text, encoding="utf-8")
anchor = "from graftpunk.devtools.captures import CAPTURES_DIR\n"
assert anchor in kept
block = "from graftpunk.devtools.scaffold.pysrc import (\n" + "".join(
    f"    {name},\n" for name in used
) + ")\n"
render.write_text(kept.replace(anchor, anchor + block, 1), encoding="utf-8")
print("moved", len(names), "names; render imports", len(used))
PY
uvx ruff check --fix src/graftpunk/devtools/scaffold/
uvx ruff format src/graftpunk/devtools/scaffold/
```

Expected: `moved 28 names; render imports ...` and ruff removes the imports `render.py` no longer uses (`json`, `textwrap`, and any other it names as F401).

- [ ] **Step 4: Point the render tests at the new owner**

In `tests/unit/test_scaffold_render.py`, replace the import block at lines 14-36 with:

```python
from graftpunk.devtools.scaffold.pysrc import (
    _DOCSTRING_WRAP_WIDTH,
    _GENERATED_LINE_LENGTH,
    _dict_entry_lines,
    _literal_dict_entry_lines,
    _literal_lines,
    _url_chunks,
    _wrapped_comment_lines,
    _wrapped_docstring_block,
    _wrapped_docstring_lines,
)
from graftpunk.devtools.scaffold.render import (
    _MAX_COMMAND_NAME,
    _MAX_PARAM_NAME,
    _MAX_PLUGIN_NAME,
    _MAX_SCAFFOLD_ENDPOINTS,
    PLUGIN_NAME_RE,
    ScaffoldSpec,
    _command_name,
    _param_identifier,
    class_name_for,
    module_name_for,
    render,
    validate_plugin_name,
)
```

- [ ] **Step 5: Run the new test and the render suite**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_pysrc.py tests/unit/test_scaffold_render.py tests/unit/test_scaffold_cli.py tests/unit/test_scaffold_project.py -q`
Expected: all pass. The width property test and the real-ruff tree tests in `test_scaffold_render.py` are the safety net for a pure move.

- [ ] **Step 6: Run the gate, then commit**

Run the full gate from Global Constraints. Expected: green.

```bash
git add src/graftpunk/devtools/scaffold/pysrc.py src/graftpunk/devtools/scaffold/render.py tests/unit/test_scaffold_pysrc.py tests/unit/test_scaffold_render.py
git commit -m "refactor(scaffold): move the formatting helpers out of render.py into pysrc.py"
```

- [ ] **Step 7: Write the failing export test**

Append to `tests/unit/test_scaffold_pysrc.py`:

```python
def test_every_name_another_module_imports_is_public_and_exported() -> None:
    """No module imports a private name from pysrc: what it shares is its __all__."""
    package = Path(pysrc.__file__).parents[2]
    imported: set[str] = set()
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported |= {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "graftpunk.devtools.scaffold.pysrc"
            for alias in node.names
        }
    assert imported
    assert imported <= set(pysrc.__all__)
    assert not [name for name in pysrc.__all__ if name.startswith("_")]
```

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_pysrc.py -q`
Expected: FAIL (`AttributeError: module 'graftpunk.devtools.scaffold.pysrc' has no attribute '__all__'`).

- [ ] **Step 8: Rename the shared helpers and declare `__all__`**

Run from the worktree root. The script refuses to run if a new name is already bound anywhere it edits (`_quoted` and `_call_lines` get longer names because `quoted` and `call_lines` are local variables in those modules):

```bash
uv run python - <<'PY'
import re
from pathlib import Path

RENAMES = {
    "_GENERATED_LINE_LENGTH": "GENERATED_LINE_LENGTH",
    "_L1": "L1",
    "_L2": "L2",
    "_L3": "L3",
    "_INDENT_STEP": "INDENT_STEP",
    "_URL_PLACEHOLDER_RE": "URL_PLACEHOLDER_RE",
    "_wrapped_docstring_lines": "wrapped_docstring_lines",
    "_wrapped_comment_lines": "wrapped_comment_lines",
    "_wrapped_docstring_block": "wrapped_docstring_block",
    "_quoted": "quoted_literal",
    "_exploded_dict_lines": "exploded_dict_lines",
    "_call_lines": "call_expression_lines",
    "_literal_lines": "literal_lines",
    "_literal_dict_entry_lines": "literal_dict_entry_lines",
    "_url_expr_lines": "url_expr_lines",
    "_import_lines": "import_lines",
}
files = [
    Path("src/graftpunk/devtools/scaffold/pysrc.py"),
    Path("src/graftpunk/devtools/scaffold/render.py"),
    Path("tests/unit/test_scaffold_pysrc.py"),
    Path("tests/unit/test_scaffold_render.py"),
]
texts = {path: path.read_text(encoding="utf-8") for path in files}
for path, text in texts.items():
    for new in RENAMES.values():
        assert not re.search(rf"\b{new}\b", text), (path, new)
for path, text in texts.items():
    for old, new in RENAMES.items():
        text = re.sub(rf"\b{re.escape(old)}\b", new, text)
    path.write_text(text, encoding="utf-8")
PY
```

Then, in `src/graftpunk/devtools/scaffold/pysrc.py`, replace the module docstring's last two sentences ("The names keep their leading underscore ... changed no behaviour.") with "A name another module imports is public and listed in ``__all__``; the rest are private to this module.", and add after the imports:

```python
__all__ = [
    "GENERATED_LINE_LENGTH",
    "INDENT_STEP",
    "L1",
    "L2",
    "L3",
    "URL_PLACEHOLDER_RE",
    "call_expression_lines",
    "exploded_dict_lines",
    "import_lines",
    "literal_dict_entry_lines",
    "literal_lines",
    "quoted_literal",
    "url_expr_lines",
    "wrapped_comment_lines",
    "wrapped_docstring_block",
    "wrapped_docstring_lines",
]
```

Run `uvx ruff check --fix src/graftpunk/devtools/scaffold/ tests/unit/test_scaffold_render.py tests/unit/test_scaffold_pysrc.py` and `uvx ruff format src/graftpunk/devtools/scaffold/ tests/unit/`, which re-sort the renamed import blocks.

- [ ] **Step 9: Run the suite and the gate, then commit**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_pysrc.py tests/unit/test_scaffold_render.py tests/unit/test_scaffold_cli.py tests/unit/test_scaffold_project.py -q`
Expected: PASS. Then run the full gate from Global Constraints. Expected: green.

```bash
git add src/graftpunk/devtools/scaffold/pysrc.py src/graftpunk/devtools/scaffold/render.py tests/unit/test_scaffold_pysrc.py tests/unit/test_scaffold_render.py
git commit -m "refactor(scaffold): pysrc.py exports the helpers other modules use under public names"
```

---

### Task 2: `graftpunk/contracts.py`

**Files:**
- Create: `src/graftpunk/contracts.py`
- Test: `tests/unit/test_contracts.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Surface = Literal["endpoints", "sidecar"]`; `ENDPOINTS_SCHEMA: Final = 1`; `SIDECAR_SCHEMA: Final = 1`; `CLI_SURFACES: Final[tuple[Surface, ...]] = ("endpoints",)`; `current_schema(surface: Surface) -> int`; `cli_contracts() -> dict[str, int]`; `class UnknownSchemaError(ValueError)`; `refuse_unknown_schema(surface: Surface, value: object) -> int`. Task 4 reads `current_schema("endpoints")`, Task 5 reads `cli_contracts()` and adds `contract_mismatch`, Task 7 reads `current_schema("sidecar")` and `refuse_unknown_schema("sidecar", ...)`. The project-tools plan adds `"info"` to `Surface`, `INFO_SCHEMA`, `_CURRENT`, and `CLI_SURFACES`, following the edit list in the module docstring; the test that holds `_CURRENT` to `Surface` fails if it misses one.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_contracts.py`:

```python
"""One owner for every versioned payload's schema number (graft skill spec, 2026-09-21,
"Versioned payloads have one owner")."""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

import graftpunk
from graftpunk.contracts import (
    _CURRENT,
    CLI_SURFACES,
    ENDPOINTS_SCHEMA,
    SIDECAR_SCHEMA,
    Surface,
    UnknownSchemaError,
    cli_contracts,
    current_schema,
    refuse_unknown_schema,
)

_PACKAGE = Path(graftpunk.__file__).parent
# A schema number written as a literal anywhere but contracts.py: a "schema" key
# with a digit, a schema= keyword with a digit, or a *_SCHEMA constant with a digit.
_LITERAL_SCHEMA_RE = re.compile(
    r"""["']schema["']\s*:\s*\d|\bschema\s*=\s*\d|_SCHEMA\s*(?::[^=\n]*)?=\s*\d"""
)


class TestCurrentNumbers:
    def test_one_number_per_surface(self) -> None:
        assert current_schema("endpoints") == ENDPOINTS_SCHEMA == 1
        assert current_schema("sidecar") == SIDECAR_SCHEMA == 1

    def test_cli_contracts_lists_every_surface_a_caller_reads_through_the_cli(self) -> None:
        assert cli_contracts() == {surface: current_schema(surface) for surface in CLI_SURFACES}
        assert "sidecar" not in cli_contracts()

    def test_every_surface_has_a_number_and_the_cli_ones_are_among_them(self) -> None:
        """A surface added to Surface without its number, or listed for the CLI
        without being declared, fails here rather than at a KeyError in a reader."""
        assert set(_CURRENT) == set(get_args(Surface))
        assert set(CLI_SURFACES) <= set(_CURRENT)


class TestRefuseUnknownSchema:
    def test_the_current_number_is_accepted(self) -> None:
        assert refuse_unknown_schema("sidecar", 1) == 1

    def test_a_missing_number_is_refused_with_the_surface_named(self) -> None:
        with pytest.raises(UnknownSchemaError, match="sidecar"):
            refuse_unknown_schema("sidecar", None)

    @pytest.mark.parametrize("value", [0, 2, "1", True, 1.0])
    def test_an_unknown_number_is_refused_with_the_surface_named(self, value: object) -> None:
        with pytest.raises(UnknownSchemaError, match="endpoints"):
            refuse_unknown_schema("endpoints", value)

    def test_the_error_is_a_value_error(self) -> None:
        assert issubclass(UnknownSchemaError, ValueError)


def test_contracts_is_the_only_module_that_declares_a_schema_number() -> None:
    offenders = [
        f"{path.relative_to(_PACKAGE)}:{number}: {line.strip()}"
        for path in sorted(_PACKAGE.rglob("*.py"))
        if path.name != "contracts.py" or path.parent != _PACKAGE
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _LITERAL_SCHEMA_RE.search(line)
    ]
    assert offenders == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_contracts.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'graftpunk.contracts'`.

- [ ] **Step 3: Write the module**

Create `src/graftpunk/contracts.py`:

```python
"""The schema number of every versioned machine payload, and the one check a reader applies.

Payloads a program reads carry a ``schema`` number: the ``gp observe digest
--endpoints-json`` projection (``endpoints``) and the fixture sidecar
(``sidecar``). Within one schema version, fields are added and never renamed or
removed; a rename or a removal is a new version, and a reader accepts every
version from 1 to the current one.

``gp version --json`` is the bootstrap of every other contract, so it carries no
number of its own: its two fields, ``graftpunk`` and ``contracts``, are
permanent, and fields are added to it and never renamed or removed.

This module is the only place in the package the current schema number of a
surface is declared, and the only place one is compared:
:func:`refuse_unknown_schema` for a payload a reader loads (graft skill spec,
2026-09-21).

Adding a surface is these edits, all here: its name in ``Surface``; its
``<NAME>_SCHEMA`` constant; its entry in ``_CURRENT``; and, when a caller reads
it through a ``gp`` command, its name in ``CLI_SURFACES``. The payload's writer
then reads its number through :func:`current_schema` and never writes a
literal. A test holds ``_CURRENT`` to ``Surface`` and ``CLI_SURFACES`` to
``_CURRENT``, so a missed edit fails the suite.
"""

from __future__ import annotations

from typing import Final, Literal

__all__ = [
    "CLI_SURFACES",
    "ENDPOINTS_SCHEMA",
    "SIDECAR_SCHEMA",
    "Surface",
    "UnknownSchemaError",
    "cli_contracts",
    "current_schema",
    "refuse_unknown_schema",
]

Surface = Literal["endpoints", "sidecar"]

ENDPOINTS_SCHEMA: Final = 1
SIDECAR_SCHEMA: Final = 1

_CURRENT: Final[dict[Surface, int]] = {
    "endpoints": ENDPOINTS_SCHEMA,
    "sidecar": SIDECAR_SCHEMA,
}

CLI_SURFACES: Final[tuple[Surface, ...]] = ("endpoints",)
"""The surfaces a caller reads through the CLI, which ``gp version --json`` lists.

The sidecar is not one: it is read by ``graftpunk.testing`` from a plugin's
committed fixtures, never through a ``gp`` command."""


class UnknownSchemaError(ValueError):
    """A payload's ``schema`` is missing or is not a version this graftpunk reads."""


def current_schema(surface: Surface) -> int:
    """The schema number *surface* is written at by this graftpunk."""
    return _CURRENT[surface]


def cli_contracts() -> dict[str, int]:
    """Each CLI-read surface and its current number, as ``gp version --json`` prints them."""
    return {surface: _CURRENT[surface] for surface in CLI_SURFACES}


def refuse_unknown_schema(surface: Surface, value: object) -> int:
    """*value* as a schema number this graftpunk reads for *surface*, or raise.

    Accepts an integer from 1 to the current number. ``True`` is refused although
    it is an ``int`` subclass, and so is ``1.0``: a schema number is written as an
    integer and nothing else.

    Raises:
        UnknownSchemaError: *value* is missing (``None``) or unknown; the message
            names *surface*.
    """
    current = _CURRENT[surface]
    if value is None:
        raise UnknownSchemaError(
            f"{surface}: no schema number. This payload predates its versioned format "
            f"or was written by hand; regenerate it with this graftpunk."
        )
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= current:
        raise UnknownSchemaError(
            f"{surface}: schema {value!r} is not one this graftpunk reads "
            f"(it reads 1 to {current}). Upgrade graftpunk, or regenerate the payload."
        )
    return value
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_contracts.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/graftpunk/contracts.py tests/unit/test_contracts.py
git commit -m "feat(contracts): one owner for every versioned payload's schema number"
```

---

### Task 3: `login_flow` on every digest endpoint

**Files:**
- Modify: `src/graftpunk/har/digest.py:224-239` (`Endpoint`), `src/graftpunk/har/digest.py:712-905` (`def digest(`), new private functions above `digest()`
- Modify: `src/graftpunk/devtools/scaffold/render.py` (`_LOGIN_FLOW_KINDS`, `_template_covers_path`, `_login_flow_endpoints` removed; `_ordered_endpoints` reads the flag)
- Modify: `tests/unit/test_scaffold_render.py` (`TestLoginFlowEndpointsAreNotCommandStubs`)
- Test: `tests/unit/test_har_digest.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `Endpoint.login_flow: bool = False` (the last field); `graftpunk.har.digest._with_login_flow(endpoints: tuple[Endpoint, ...], login: tuple[LoginObservation, ...]) -> tuple[Endpoint, ...]`; `digest()` returns endpoints with the flag set. Task 4's projection and the project-tools plan's `plan_command` read `Endpoint.login_flow`.

- [ ] **Step 1: Write the failing digest tests**

Append to `tests/unit/test_har_digest.py` (add `Endpoint`, `LoginObservation`, `ObservationKind`, and `_with_login_flow` to its `from graftpunk.har.digest import (...)` block):

```python
def _flow_endpoint(template: str, method: str = "GET", examples: tuple[str, ...] = ()) -> Endpoint:
    return Endpoint(
        host="api.myshop.example.com",
        template=template,
        methods=(method,),
        count=1,
        statuses=(200,),
        content_type="text/html",
        query_params={},
        body_params={},
        body_kind="none",
        shape=None,
        custom_headers=(),
        examples=examples,
    )


def _flow_observation(kind: ObservationKind, method: str, path: str) -> LoginObservation:
    return LoginObservation(
        order=1,
        method=method,
        url=f"https://api.myshop.example.com{path}",
        status=200,
        kind=kind,
        fields=(),
    )


class TestLoginFlowFlag:
    """login_flow marks the endpoints login_config drives: the login form's GET and
    the credential POST. It moved here from the generator so the proposal table and
    the scaffold read one flag (graft skill spec, 2026-09-21)."""

    def test_the_form_page_and_the_credential_post_are_flagged(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/login",
                content_type="text/html",
                body=(
                    '<form action="/login" method="post">'
                    '<input type="email" name="email"><input type="password" name="password">'
                    "</form>"
                ),
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/login",
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
            _entry("GET", "https://api.myshop.example.com/api/orders", body='{"orders": []}'),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags == {
            ("GET", "/login"): True,
            ("POST", "/login"): True,
            ("GET", "/api/orders"): False,
        }

    def test_the_same_path_under_another_method_is_not_flagged(self) -> None:
        (endpoint,) = _with_login_flow(
            (_flow_endpoint("/login", "DELETE"),),
            (_flow_observation("form_page", "GET", "/login"),),
        )
        assert endpoint.login_flow is False

    def test_a_login_path_inside_a_collapsed_family_is_flagged(self) -> None:
        """The high-cardinality collapse can re-template the endpoint the observation
        belongs to, so templating the observation's path alone would miss it."""
        family = _flow_endpoint("/account/{account_id}", examples=("/account/login",))
        (endpoint,) = _with_login_flow(
            (family,), (_flow_observation("form_page", "GET", "/account/login"),)
        )
        assert endpoint.login_flow is True

    def test_a_redirect_or_auth_api_observation_flags_nothing(self) -> None:
        endpoints = (_flow_endpoint("/auth/callback"), _flow_endpoint("/api/session"))
        observations = (
            _flow_observation("redirect", "GET", "/auth/callback"),
            _flow_observation("auth_api", "GET", "/api/session"),
        )
        assert [e.login_flow for e in _with_login_flow(endpoints, observations)] == [False, False]

    def test_the_flag_defaults_to_false(self) -> None:
        assert _flow_endpoint("/anything").login_flow is False
```

- [ ] **Step 2: Write the failing render test**

In `tests/unit/test_scaffold_render.py`, add `_with_login_flow` to the `from graftpunk.har.digest import (...)` block and append this method to `TestLoginFlowEndpointsAreNotCommandStubs`:

```python
    def test_the_generator_reads_the_digest_flag(self) -> None:
        """With no login observation at all, an endpoint the digest flagged is still
        skipped: the generator reads the flag and never recomputes it."""
        flagged = dataclasses.replace(_ORDERS_ENDPOINT, login_flow=True)
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(flagged, _SEARCH_ENDPOINT)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "def orders_by_order_id(" not in plugin_code
        assert "def search(" in plugin_code
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_har_digest.py::TestLoginFlowFlag tests/unit/test_scaffold_render.py::TestLoginFlowEndpointsAreNotCommandStubs -q`
Expected: FAIL (`ImportError: cannot import name '_with_login_flow'`).

- [ ] **Step 4: Add the flag and move the computation into `digest.py`**

In `src/graftpunk/har/digest.py`, change `from dataclasses import dataclass` to `from dataclasses import dataclass, replace`, and add the last field of `Endpoint` after `examples: tuple[str, ...]`:

```python
    # Part of the login flow login_config drives (the login form's GET or the
    # credential POST): the generator renders no stub for it and a command
    # proposal drops it, both from this one flag (graft skill spec, 2026-09-21).
    login_flow: bool = False
```

Add above `def digest(`:

```python
_LOGIN_FLOW_KINDS: tuple[ObservationKind, ...] = ("form_page", "credential_post")


def _template_covers_path(template: str, path: str) -> bool:
    """True when *path* is one of the paths *template* stands for: the same number
    of segments, each of the template's either a ``{placeholder}`` or that segment
    spelled exactly."""
    template_segments = template.strip("/").split("/") if template.strip("/") else []
    path_segments = path.strip("/").split("/") if path.strip("/") else []
    if len(template_segments) != len(path_segments):
        return False
    return all(
        (segment.startswith("{") and segment.endswith("}")) or segment == observed
        for segment, observed in zip(template_segments, path_segments, strict=True)
    )


def _login_flow_pairs(
    login: tuple[LoginObservation, ...], endpoints: tuple[Endpoint, ...]
) -> set[tuple[str, str]]:
    """The ``(method, template)`` pairs ``login_config`` owns, as endpoints are keyed.

    The login form's own GET and the credential POST are the login flow. An
    observation carries the raw path, and the endpoint it belongs to may have
    been re-templated by the high-cardinality collapse, so each observation
    claims every endpoint of its method whose final template covers its path,
    plus its own templated path for a run whose login flow produced no endpoint
    (moved from ``devtools/scaffold/render.py``; polish round 2, 2026-09-12).
    """
    owned: set[tuple[str, str]] = set()
    for observation in login:
        if observation.kind not in _LOGIN_FLOW_KINDS:
            continue
        method = observation.method.upper()
        path = urlparse(observation.url).path or "/"
        template, _ = template_path(path)
        owned.add((method, template))
        for endpoint in endpoints:
            if method in endpoint.methods and _template_covers_path(endpoint.template, path):
                owned.add((method, endpoint.template))
    return owned


def _with_login_flow(
    endpoints: tuple[Endpoint, ...], login: tuple[LoginObservation, ...]
) -> tuple[Endpoint, ...]:
    """*endpoints* with ``login_flow`` set on each one every method of which the
    login flow owns."""
    owned = _login_flow_pairs(login, endpoints)
    return tuple(
        replace(endpoint, login_flow=all((m, endpoint.template) in owned for m in endpoint.methods))
        for endpoint in endpoints
    )
```

In `digest()`, change the `return RunDigest(...)` argument `endpoints=endpoints,` to `endpoints=_with_login_flow(endpoints, tuple(login)),`.

- [ ] **Step 5: Make the generator read the flag**

In `src/graftpunk/devtools/scaffold/render.py`, delete `_LOGIN_FLOW_KINDS`, `_template_covers_path`, and `_login_flow_endpoints`, and replace `_ordered_endpoints` with:

```python
def _ordered_endpoints(d: RunDigest) -> list[Endpoint]:
    """The endpoints the scaffold renders a stub for, most useful first. The login
    flow's own endpoints are skipped by the digest's ``login_flow`` flag."""
    kept = [e for e in d.endpoints if not e.login_flow]
    return sorted(kept, key=lambda e: (not _is_json_endpoint(e), -e.count, e.template))
```

Run `uvx ruff check --fix src/graftpunk/devtools/scaffold/render.py` to drop the `urlparse` and `template_path` imports it no longer uses.

- [ ] **Step 6: Feed the existing render tests a flagged digest**

The render tests in `TestLoginFlowEndpointsAreNotCommandStubs` built digests by hand with observations and relied on the generator to compute ownership. They now compose the digest's own function with the generator. In `_spec`, change `endpoints=endpoints,` to `endpoints=_with_login_flow(endpoints, (_FORM_PAGE_OBSERVATION, _CREDENTIAL_POST_OBSERVATION)),`, and in `test_a_login_path_inside_a_collapsed_family_is_still_owned` change `endpoints=(_COLLAPSED_ACCOUNT_FAMILY, _ORDERS_ENDPOINT),` to:

```python
                endpoints=_with_login_flow(
                    (_COLLAPSED_ACCOUNT_FAMILY, _ORDERS_ENDPOINT),
                    (_COLLAPSED_FORM_PAGE_OBSERVATION,),
                ),
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_har_digest.py tests/unit/test_scaffold_render.py tests/unit/test_scaffold_cli.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/graftpunk/har/digest.py src/graftpunk/devtools/scaffold/render.py tests/unit/test_har_digest.py tests/unit/test_scaffold_render.py
git commit -m "feat(har): the digest flags the login flow's endpoints and the generator reads the flag"
```

---

### Task 4: The versioned `--endpoints-json` projection

**Files:**
- Modify: `src/graftpunk/har/report.py` (new `endpoints_projection`, `render_endpoints_json`; `__all__`)
- Modify: `src/graftpunk/cli/observe_commands.py:117-153` (`digest_cmd`)
- Test: `tests/unit/test_har_report.py`, `tests/unit/test_observe_commands.py`

**Interfaces:**
- Consumes: `current_schema("endpoints")` (Task 2); `Endpoint.login_flow` (Task 3); `summarize_shape` (existing, `report.py:35`).
- Produces: `graftpunk.har.report.endpoints_projection(d: RunDigest) -> dict[str, Any]` with the field set `{"schema", "source", "primary_host", "endpoints", "login"}`; `source` keys `{"session", "run_id", "har"}`; each endpoint entry `{"method", "template", "login_flow", "content_type", "shape", "query_params", "body_params", "custom_headers"}`; `login` keys `{"auth_urls", "forms"}` with `auth_urls` entries `{"method", "url", "kind"}` and `forms` entries `{"action", "fields"}`. `render_endpoints_json(d: RunDigest) -> str`. `gp observe digest --endpoints-json`. The skill's `digest.md` reads exactly these names.

**Stated deferral:** `digest_cmd` ends this task with two machine-format flags, `--json` and `--endpoints-json`, refused together. One output-format option (`--format markdown|json|endpoints-json`) would be cleaner, and it is deferred with a trigger: the first proposal of a third machine format replaces the flags with that option (keeping the two flags as aliases for one release) instead of adding a third flag.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_har_report.py` (add `dataclasses` to its imports, `Endpoint` to the `graftpunk.har.digest` import, and `endpoints_projection`, `render_endpoints_json` to the `graftpunk.har.report` import):

```python
_SAMPLE_HAR = Path(__file__).resolve().parents[1] / "fixtures" / "sample.har"

# The projection's field set at schema 1, frozen here so a rename fails the suite.
_PROJECTION_V1 = {"schema", "source", "primary_host", "endpoints", "login"}
_SOURCE_V1 = {"session", "run_id", "har"}
_ENDPOINT_V1 = {
    "method",
    "template",
    "login_flow",
    "content_type",
    "shape",
    "query_params",
    "body_params",
    "custom_headers",
}
_LOGIN_V1 = {"auth_urls", "forms"}
_AUTH_URL_V1 = {"method", "url", "kind"}
_FORM_V1 = {"action", "fields"}


def _planted_run(tmp_path: Path) -> Path:
    """A run holding a cookie name, a token candidate, an example path, and a body."""
    account = _entry(
        "GET",
        "https://myshop.example.com/account",
        content_type="text/html",
        body=(
            '<html><head><meta name="csrf-token" content="planted-token-value"></head>'
            '<form action="/session" method="post"><input type="email" id="email" name="email">'
            '<input type="password" id="password" name="password"></form></html>'
        ),
    )
    order = _entry(
        "GET",
        "https://myshop.example.com/api/orders/12345",
        body='{"id": "planted-body-value"}',
    )
    order["response"]["cookies"] = [{"name": "shop_session_cookie", "value": "planted-cookie"}]
    return _write_har(tmp_path, [account, order])


class TestEndpointsProjection:
    def test_schema_one_and_its_field_set(self, tmp_path: Path) -> None:
        payload = endpoints_projection(digest(DigestSource.from_har(_planted_run(tmp_path))))
        assert payload["schema"] == 1
        assert set(payload) == _PROJECTION_V1
        assert set(payload["source"]) == _SOURCE_V1
        assert payload["endpoints"]
        for entry in payload["endpoints"]:
            assert set(entry) == _ENDPOINT_V1
        assert set(payload["login"]) == _LOGIN_V1
        for url in payload["login"]["auth_urls"]:
            assert set(url) == _AUTH_URL_V1
        assert payload["login"]["forms"]
        for form in payload["login"]["forms"]:
            assert set(form) == _FORM_V1

    def test_no_cookie_name_token_candidate_example_path_or_body(self, tmp_path: Path) -> None:
        result = digest(DigestSource.from_har(_planted_run(tmp_path)))
        # The digest itself holds all four, so the assertions below are not vacuous.
        assert "shop_session_cookie" in result.cookies
        assert any(token.name == "csrf-token" for token in result.tokens)
        assert any("/api/orders/12345" in e.examples for e in result.endpoints)
        text = render_endpoints_json(result)
        for planted in (
            "shop_session_cookie",
            "planted-cookie",
            "csrf-token",
            "planted-token-value",
            "/api/orders/12345",
            "planted-body-value",
        ):
            assert planted not in text, planted

    def test_the_sample_har_leaks_no_cookie_name_or_example_path(self) -> None:
        result = digest(DigestSource.from_har(_SAMPLE_HAR))
        assert "sessionId" in result.cookies
        text = render_endpoints_json(result)
        assert "sessionId" not in text
        assert "/api/users/123/posts" not in text

    def test_the_projection_is_uncapped(self, tmp_path: Path) -> None:
        # Letters only: a segment with a digit is eligible for the high-cardinality
        # collapse, which would fold these seventy routes into one.
        words = [f"{chr(97 + i // 26)}{chr(97 + i % 26)}route" for i in range(70)]
        entries = [
            _entry("GET", f"https://myshop.example.com/api/{word}", body='{"id": 1}')
            for word in words
        ]
        payload = endpoints_projection(digest(DigestSource.from_har(_write_har(tmp_path, entries))))
        assert len(payload["endpoints"]) == 70

    def test_login_flow_and_shape_come_through(self, tmp_path: Path) -> None:
        payload = endpoints_projection(digest(DigestSource.from_har(_planted_run(tmp_path))))
        order = next(e for e in payload["endpoints"] if e["template"] == "/api/orders/{order_id}")
        assert order["method"] == "GET"
        assert order["login_flow"] is False
        assert order["shape"] == "object{id}"

    def test_the_projection_survives_a_rename_of_an_internal_endpoint_field(
        self, tmp_path: Path
    ) -> None:
        """Built by an explicit function, not by reflection: renaming a field the
        projection does not carry changes render_json and leaves this unchanged."""
        result = digest(DigestSource.from_har(_planted_run(tmp_path)))
        renames = {"examples": "example_paths", "statuses": "status_codes"}
        fields = dataclasses.fields(Endpoint)
        renamed_cls = dataclasses.make_dataclass(
            "Endpoint", [(renames.get(f.name, f.name), f.type) for f in fields], frozen=True
        )
        renamed = tuple(
            renamed_cls(**{renames.get(f.name, f.name): getattr(e, f.name) for f in fields})
            for e in result.endpoints
        )
        patched = dataclasses.replace(result, endpoints=renamed)
        assert endpoints_projection(patched) == endpoints_projection(result)
        assert render_json(patched) != render_json(result)
```

Append to `tests/unit/test_observe_commands.py`, inside `class TestDigestCommand`:

```python
    def test_endpoints_json_prints_the_projection(
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
        result = runner.invoke(
            _build_app(), ["observe", "digest", "myshop", "--endpoints-json"]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["schema"] == 1
        assert payload["source"] == {"session": "myshop", "run_id": "run-1", "har": None}

    def test_json_and_endpoints_json_together_is_an_error(self, tmp_path: Path) -> None:
        har = tmp_path / "network.har"
        har.write_text(json.dumps({"log": {"version": "1.2", "entries": []}}))
        result = runner.invoke(
            _build_app(),
            ["observe", "digest", "--har", str(har), "--json", "--endpoints-json"],
        )
        assert result.exit_code == 1
        assert "not both" in _plain(result.output)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_har_report.py::TestEndpointsProjection tests/unit/test_observe_commands.py::TestDigestCommand -q`
Expected: FAIL (`ImportError: cannot import name 'endpoints_projection'`).

- [ ] **Step 3: Write the projection**

In `src/graftpunk/har/report.py`, add `from graftpunk.contracts import current_schema` to the imports, add `"endpoints_projection"` and `"render_endpoints_json"` to `__all__`, and append:

```python
def endpoints_projection(d: RunDigest) -> dict[str, Any]:
    """The declared projection ``gp observe digest --endpoints-json`` prints.

    An explicit field list, never a reflection over the digest's dataclasses, so
    renaming an internal field touches this function and no contract. It carries
    only what a command proposal consumes, and by construction no cookie name, no
    token candidate, no example path, and no body. Its field set is pinned per
    schema version; fields are added and never renamed or removed within one
    (:mod:`graftpunk.contracts`). ``render_json`` stays an unversioned dump.
    """
    source = d.source
    return {
        "schema": current_schema("endpoints"),
        "source": {
            "session": source.session,
            "run_id": source.run_id,
            "har": None if source.session else str(source.har_path),
        },
        "primary_host": d.primary_host,
        "endpoints": [
            {
                "method": method,
                "template": endpoint.template,
                "login_flow": endpoint.login_flow,
                "content_type": endpoint.content_type,
                "shape": summarize_shape(endpoint.shape),
                "query_params": dict(sorted(endpoint.query_params.items())),
                "body_params": dict(sorted(endpoint.body_params.items())),
                "custom_headers": list(endpoint.custom_headers),
            }
            for endpoint in sorted(d.endpoints, key=_endpoint_sort_key)
            for method in endpoint.methods
        ],
        "login": {
            "auth_urls": [
                {"method": o.method, "url": o.url, "kind": o.kind} for o in d.login
            ],
            "forms": [
                {"action": form.action, "fields": dict(sorted(form.fields.items()))}
                for form in d.login_forms
            ],
        },
    }


def render_endpoints_json(d: RunDigest) -> str:
    """:func:`endpoints_projection` as indented JSON with sorted keys."""
    return json.dumps(endpoints_projection(d), indent=2, sort_keys=True)
```

- [ ] **Step 4: Add the CLI flag**

In `src/graftpunk/cli/observe_commands.py`, import `render_endpoints_json` beside `render_json`, add this parameter to `digest_cmd` after `as_json`:

```python
    endpoints_json: Annotated[
        bool,
        typer.Option(
            "--endpoints-json",
            help="Print the versioned endpoint projection a program reads (uncapped)",
        ),
    ] = False,
```

and replace the three lines of its body after the docstring (`source = _digest_source(...)`, `result = digest(...)`, and `text = render_json(result) if as_json else render_markdown(...)`, at `src/graftpunk/cli/observe_commands.py:138-140`) with:

```python
    if as_json and endpoints_json:
        console.print("[red]Pass --json or --endpoints-json, not both.[/red]")
        raise typer.Exit(1)
    source = _digest_source(session, run, har)
    result = digest(source, all_hosts=all_hosts)
    if endpoints_json:
        text = render_endpoints_json(result)
    elif as_json:
        text = render_json(result)
    else:
        text = render_markdown(result, limit=limit)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_har_report.py tests/unit/test_observe_commands.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/har/report.py src/graftpunk/cli/observe_commands.py tests/unit/test_har_report.py tests/unit/test_observe_commands.py
git commit -m "feat(observe): gp observe digest --endpoints-json, a versioned projection with a pinned field set"
```

---

### Task 5: `gp version --json`, `--at-least`, and `--contract`, with the `packaging` dependency

**Files:**
- Modify: `pyproject.toml:33-47` (`dependencies`), `uv.lock`
- Modify: `src/graftpunk/cli/main.py:6-14` (standard-library and third-party imports), `:32` (`from graftpunk.console import err_console`, the import the new one follows), `:169-182` (`version`)
- Modify: `src/graftpunk/contracts.py` (new `contract_mismatch` and `installation_facts`; `__all__`; docstring)
- Test: `tests/unit/test_cli_version.py`, `tests/unit/test_contracts.py`

**Interfaces:**
- Consumes: `cli_contracts()` (Task 2).
- Produces: `graftpunk.contracts.contract_mismatch(surface: str, reads: int) -> str | None`; `graftpunk.contracts.installation_facts() -> dict[str, object]` with the permanent field set `{"graftpunk", "contracts"}`, beside `cli_contracts`, reading `graftpunk.__version__` at call time; `gp version --json` prints `json.dumps(installation_facts(), sort_keys=True)` on one line; `gp version --at-least VERSION --contract SURFACE=N ...` prints that line first when `--json` is given, then exits 0 when the installed version is at least VERSION and every named surface is written at exactly N; 1 when the installed version is below VERSION; 3 when a named surface differs, with one line per mismatch on stderr naming the older side; 1 with a message on stderr for an unreadable VERSION or a malformed `--contract` value; and 2 only for Typer's own usage errors (an unknown option). The skill plan's preflight passes its floor and its two contract numbers in this one call and reads only the exit status, never the JSON.

**Implementation note:** the spec's first wording had preflight read the `contracts` numbers out of the JSON and compare them itself. The comparison moves into the package (`--contract`), so preflight parses no JSON and compares nothing, and the spec's Preflight section says so. An unreadable `--at-least` value exits 1 with a message rather than 2, so exit 2 means exactly "an option gp does not know", which is what preflight's exit 5 reports.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_cli_version.py`:

```python
"""gp version --json and --at-least (graft skill spec, 2026-09-21)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

import graftpunk
from graftpunk import contracts
from graftpunk.cli.main import app
from graftpunk.contracts import cli_contracts, installation_facts

runner = CliRunner()

# gp version --json is the bootstrap surface: these fields are permanent.
_VERSION_JSON_FIELDS = {"graftpunk", "contracts"}


def test_json_prints_the_installation_facts_with_exactly_the_pinned_fields() -> None:
    result = runner.invoke(app, ["version", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == _VERSION_JSON_FIELDS
    assert payload == {"graftpunk": graftpunk.__version__, "contracts": cli_contracts()}
    assert payload == installation_facts()


def test_json_is_one_line_with_sorted_keys() -> None:
    result = runner.invoke(app, ["version", "--json"])
    assert result.stdout.strip() == json.dumps(installation_facts(), sort_keys=True)


@pytest.mark.parametrize(
    ("installed", "floor", "exit_code"),
    [
        ("1.17.0", "1.17.0", 0),
        ("1.17.1", "1.17.0", 0),
        ("1.16.9", "1.17.0", 1),
        ("1.10.0", "1.9.0", 0),
        ("1.9.0", "1.10.0", 1),
        ("1.17.0rc1", "1.17.0", 1),
        ("1.17.0", "1.17.0rc1", 0),
    ],
)
def test_at_least_orders_by_version_rules(
    monkeypatch: pytest.MonkeyPatch, installed: str, floor: str, exit_code: int
) -> None:
    monkeypatch.setattr(graftpunk, "__version__", installed)
    result = runner.invoke(app, ["version", "--json", "--at-least", floor])
    assert result.exit_code == exit_code, result.output
    assert json.loads(result.stdout)["graftpunk"] == installed


def test_at_least_without_json_prints_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graftpunk, "__version__", "1.17.0")
    result = runner.invoke(app, ["version", "--at-least", "1.17.0"])
    assert result.exit_code == 0
    assert result.stdout == ""


def test_an_unreadable_floor_exits_1_with_a_message() -> None:
    """Exit 2 is kept for an option gp does not know, so a caller can tell the two apart."""
    result = runner.invoke(app, ["version", "--at-least", "not-a-version"])
    assert result.exit_code == 1
    assert "'not-a-version' is not a version" in result.output


def _contract_args(contracts: dict[str, int]) -> list[str]:
    return [
        arg
        for surface, number in contracts.items()
        for arg in ("--contract", f"{surface}={number}")
    ]


def test_matching_contracts_exit_0_and_still_print_the_facts() -> None:
    result = runner.invoke(app, ["version", "--json", *_contract_args(cli_contracts())])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == installation_facts()


def test_a_caller_reading_an_older_schema_is_named_the_older_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(contracts._CURRENT, "endpoints", 2)
    result = runner.invoke(app, ["version", "--json", "--contract", "endpoints=1"])
    assert result.exit_code == 3
    assert "endpoints" in result.output
    assert "the caller is older than graftpunk" in result.output
    assert json.loads(result.stdout)["contracts"]["endpoints"] == 2


def test_a_caller_reading_a_newer_schema_names_graftpunk_the_older_side() -> None:
    result = runner.invoke(app, ["version", "--contract", "endpoints=2"])
    assert result.exit_code == 3
    assert "graftpunk is older than the caller" in result.output


def test_a_surface_graftpunk_does_not_serve_is_a_mismatch() -> None:
    result = runner.invoke(app, ["version", "--contract", "nosuch=1"])
    assert result.exit_code == 3
    assert "nosuch" in result.output


@pytest.mark.parametrize("value", ["endpoints", "endpoints=", "endpoints=one", "=1"])
def test_a_malformed_contract_exits_1_with_a_message(value: str) -> None:
    result = runner.invoke(app, ["version", "--contract", value])
    assert result.exit_code == 1
    assert "SURFACE=N" in result.output


def test_a_floor_not_met_exits_1_before_the_contracts_are_compared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(graftpunk, "__version__", "1.16.0")
    result = runner.invoke(
        app, ["version", "--json", "--at-least", "1.17.0", "--contract", "endpoints=2"]
    )
    assert result.exit_code == 1


@pytest.mark.parametrize(
    ("installed", "exit_code"), [("0.0.0+unknown", 1), ("1.17.0.dev3+g1234abc", 1)]
)
def test_a_dev_or_local_version_is_ordered_not_rejected(
    monkeypatch: pytest.MonkeyPatch, installed: str, exit_code: int
) -> None:
    """A local checkout reports a local or dev version; it orders below the release."""
    monkeypatch.setattr(graftpunk, "__version__", installed)
    result = runner.invoke(app, ["version", "--json", "--at-least", "1.17.0"])
    assert result.exit_code == exit_code, result.output
```

In `tests/unit/test_contracts.py`, add `contract_mismatch` to the `graftpunk.contracts` import and append:

```python
class TestContractMismatch:
    """The gp version --contract handshake: equality, with the older side named."""

    def test_the_current_number_matches(self) -> None:
        assert contract_mismatch("endpoints", current_schema("endpoints")) is None

    def test_an_older_caller_is_named(self) -> None:
        message = contract_mismatch("endpoints", current_schema("endpoints") - 1)
        assert message is not None and "the caller is older than graftpunk" in message

    def test_a_newer_caller_is_named(self) -> None:
        message = contract_mismatch("endpoints", current_schema("endpoints") + 1)
        assert message is not None and "graftpunk is older than the caller" in message

    def test_a_surface_no_caller_reads_through_the_cli_is_a_mismatch(self) -> None:
        message = contract_mismatch("sidecar", 1)
        assert message is not None and "sidecar" in message
```

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_cli_version.py tests/unit/test_contracts.py -q`
Expected: FAIL (`ImportError: cannot import name 'installation_facts'`, and `cannot import name 'contract_mismatch'`).

- [ ] **Step 3: Add the dependency**

In `pyproject.toml`, add `"packaging>=23.0",` as the last entry of `[project].dependencies` (after `"fpdf2>=2.8.0",`), then run:

```bash
uv lock
```

Expected: `uv.lock` records `packaging` as a direct dependency of `graftpunk`.

- [ ] **Step 4: Write the comparison in `contracts.py`**

In `src/graftpunk/contracts.py`, add `import graftpunk` after the `typing` import (its own isort section), add `"contract_mismatch"` and `"installation_facts"` to `__all__`, change the docstring's comparison sentence to "the only place one is compared: :func:`refuse_unknown_schema` for a payload a reader loads, and :func:`contract_mismatch` for the ``gp version --contract`` handshake (graft skill spec, 2026-09-21).", append to the docstring's paragraph on ``gp version --json`` the sentence ":func:`installation_facts` builds it, and the command only prints it.", and append:

```python
def installation_facts() -> dict[str, object]:
    """What ``gp version --json`` prints: facts about this installation as data.

    The installed version, and the schema number of every payload a caller reads
    through the CLI. The bootstrap of every other contract, so it carries no
    number of its own; its two fields are permanent (see the module docstring).
    The version is read at call time, so nothing here freezes it at import.
    """
    return {"graftpunk": graftpunk.__version__, "contracts": cli_contracts()}
```

and:

```python
def contract_mismatch(surface: str, reads: int) -> str | None:
    """Why a caller that reads *surface* at schema *reads* cannot use this graftpunk, or None.

    The ``gp version --contract`` handshake. Equality, not a range: a caller is
    written for one schema of a surface and reads that one. The message names the
    older side, so the caller can say which of the two to update.
    """
    current = cli_contracts().get(surface)
    if current is None:
        return (
            f"{surface}: this graftpunk serves no surface by that name to a caller; "
            f"graftpunk is older than the caller, or the caller misspelled the surface."
        )
    if reads == current:
        return None
    if reads < current:
        return (
            f"{surface}: this graftpunk writes schema {current} and the caller reads "
            f"{reads}; the caller is older than graftpunk."
        )
    return (
        f"{surface}: the caller reads schema {reads} and this graftpunk writes "
        f"{current}; graftpunk is older than the caller."
    )
```

- [ ] **Step 5: Write the command**

In `src/graftpunk/cli/main.py`, add `import json` to the standard-library imports, `from packaging.version import InvalidVersion, Version` after `import typer` and before `from rich.console import Console` (isort's order; anywhere else ruff reports I001), and `from graftpunk.contracts import contract_mismatch, installation_facts` after `from graftpunk.console import err_console` (`main.py:32`). Replace the `version` command with:

```python
@app.command("version")
def version(
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the installation facts as JSON")
    ] = False,
    at_least: Annotated[
        str | None,
        typer.Option(
            "--at-least",
            metavar="VERSION",
            help="Exit 0 when the installed graftpunk is at least VERSION, 1 otherwise",
        ),
    ] = None,
    contract: Annotated[
        list[str],
        typer.Option(
            "--contract",
            metavar="SURFACE=N",
            help="Exit 3 unless this graftpunk writes SURFACE at schema N (repeatable)",
        ),
    ] = [],  # noqa: B006 - Typer reads this default at decoration time, never mutated per-call
) -> None:
    """Show graftpunk version and installation info."""
    floor: Version | None = None
    if at_least is not None:
        try:
            floor = Version(at_least)
        except InvalidVersion:
            typer.echo(f"--at-least: {at_least!r} is not a version graftpunk can order.", err=True)
            raise typer.Exit(1) from None
    expected: list[tuple[str, int]] = []
    for value in contract:
        surface, separator, number = value.partition("=")
        if not surface or not separator or not (number.isascii() and number.isdigit()):
            typer.echo(f"--contract: {value!r} is not SURFACE=N, as in endpoints=1.", err=True)
            raise typer.Exit(1)
        expected.append((surface, int(number)))
    if as_json:
        typer.echo(json.dumps(installation_facts(), sort_keys=True))
    elif floor is None and not expected:
        # A caller passing --at-least or --contract without --json wants the
        # exit status, not the panel.
        settings = get_settings()
        console.print(
            Panel(
                f"[bold cyan]graftpunk[/bold cyan] v{graftpunk.__version__}\n"
                f"{escape(graftpunk.DESCRIPTION)}\n\n"
                f"[dim]Config:[/dim]  {escape(str(settings.config_dir))}\n"
                f"[dim]Storage:[/dim] {escape(str(settings.storage_backend))}",
                title="graftpunk",
                border_style="cyan",
            )
        )
    if floor is not None and Version(graftpunk.__version__) < floor:
        raise typer.Exit(1)
    mismatches = [
        message
        for surface, reads in expected
        if (message := contract_mismatch(surface, reads)) is not None
    ]
    for message in mismatches:
        typer.echo(message, err=True)
    if mismatches:
        raise typer.Exit(3)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_cli_version.py tests/unit/test_contracts.py tests/unit/test_cli.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/graftpunk/cli/main.py src/graftpunk/contracts.py tests/unit/test_cli_version.py tests/unit/test_contracts.py
git commit -m "feat(cli): gp version --json, --at-least ordered by packaging, and the --contract handshake"
```

---

### Task 6: `parse_endpoint` and `parse_command_spec`, and the matcher routed through them

**Files:**
- Modify: `src/graftpunk/har/naming.py` (new constants, error, two parsers; `__all__`)
- Modify: `src/graftpunk/cli/observe_commands.py:43-45` (`_HTTP_METHODS` removed), `:156-179` (`_matches_template`, `_validate_match_patterns` replaced), `:201-251` (`fixtures_cmd` body, from `if not match:` through the `_matches_template` call)
- Modify: `tests/unit/test_observe_commands.py:563-611` (`TestMatchPatternValidation`)
- Test: `tests/unit/test_har_naming.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `graftpunk.har.naming.HTTP_METHODS: frozenset[str]`; `class EndpointSpecError(ValueError)`; `parse_endpoint(value: str) -> tuple[str, str]` returning `(method, template)`; `parse_command_spec(value: str) -> tuple[str, str, str]` returning `(name, method, template)`. The project-tools plan's `gp plugin new --command` and `gp plugin add-command --command` call `parse_command_spec` and print `f"--command: {exc}"`; `gp observe fixtures --match` prints `f"--match: {exc}"`.

**Implementation note:** the current validator accepts a lowercase method (`observe_commands.py:173` upper-cases before checking) and `TestMatchPatternValidation.test_a_well_formed_pattern_is_accepted` pins `"get /orders/{order_id}"` as accepted. The spec's testing bullet for `parse_endpoint` requires refusing a lowercase method, so that test changes to `"GET"` and the lowercase value joins the refused list.

- [ ] **Step 1: Write the failing parser tests**

Append to `tests/unit/test_har_naming.py` (extend its import to `from graftpunk.har.naming import EndpointSpecError, capture_filename, capture_slug, parse_command_spec, parse_endpoint`, and add `import pytest`):

```python
class TestParseEndpoint:
    def test_the_digest_printed_form_parses_to_the_pair(self) -> None:
        assert parse_endpoint("GET /api/orders/{order_id}") == ("GET", "/api/orders/{order_id}")

    def test_a_glob_template_is_kept_as_written(self) -> None:
        assert parse_endpoint("GET /api/orders/*") == ("GET", "/api/orders/*")

    @pytest.mark.parametrize(
        "value", ["/orders", "get /orders", "Get /orders", "GET", "GET   ", "ORDERS /orders"]
    )
    def test_a_value_that_is_not_method_space_template_is_refused(self, value: str) -> None:
        with pytest.raises(EndpointSpecError, match="METHOD template"):
            parse_endpoint(value)

    def test_surrounding_whitespace_is_accepted_and_a_tab_is_refused(self) -> None:
        assert parse_endpoint("  GET   /orders  ") == ("GET", "/orders")
        with pytest.raises(EndpointSpecError):
            parse_endpoint("GET\t/orders")


class TestParseCommandSpec:
    def test_the_triple(self) -> None:
        assert parse_command_spec("order=GET /api/orders/{order_id}") == (
            "order",
            "GET",
            "/api/orders/{order_id}",
        )

    def test_the_split_is_on_the_first_equals_sign(self) -> None:
        assert parse_command_spec("search=GET /api/search?q=a") == (
            "search",
            "GET",
            "/api/search?q=a",
        )

    def test_a_missing_equals_sign_is_refused(self) -> None:
        with pytest.raises(EndpointSpecError, match="no '='"):
            parse_command_spec("orders GET /api/orders")

    def test_an_empty_name_is_refused(self) -> None:
        with pytest.raises(EndpointSpecError, match="no command name"):
            parse_command_spec("=GET /api/orders")

    def test_a_trailing_equals_sign_is_refused(self) -> None:
        with pytest.raises(EndpointSpecError, match="nothing after '='"):
            parse_command_spec("orders=")

    def test_a_malformed_endpoint_half_is_refused_by_parse_endpoint(self) -> None:
        with pytest.raises(EndpointSpecError, match="METHOD template"):
            parse_command_spec("orders=get /api/orders")
```

- [ ] **Step 2: Write the failing CLI tests**

In `tests/unit/test_observe_commands.py`, change the parametrize list of `test_a_pattern_that_is_not_method_plus_template_is_refused` to `["/orders", "ORDERS /orders", "GET", "GET   ", "get /orders/{order_id}"]`, change `"get /orders/{order_id}"` to `"GET /orders/{order_id}"` in `test_a_well_formed_pattern_is_accepted`, and append to `TestMatchPatternValidation`:

```python
    def test_the_matcher_takes_its_halves_from_parse_endpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A grammar change made in parse_endpoint alone reaches the matcher: the
        matcher never splits a pattern itself."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        _write_run(
            observe_base,
            "myshop",
            "run-1",
            [_entry("GET", "https://api.myshop.example.com/synthetic/1", body='{"id": 1}')],
        )
        monkeypatch.setattr(
            "graftpunk.cli.observe_commands.parse_endpoint",
            lambda value: ("GET", "/synthetic/*"),
        )
        out_dir = tmp_path / "out"
        result = runner.invoke(
            _build_app(),
            ["observe", "fixtures", "myshop", "--match", "PUT /whatever", "--out", str(out_dir)],
        )
        assert result.exit_code == 0, result.output
        written = [p for p in out_dir.glob("get_synthetic_*") if not p.name.endswith(".meta.json")]
        assert len(written) == 1

    def test_the_refusal_is_parse_endpoints_own_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk.har.naming import EndpointSpecError, parse_endpoint

        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        _write_run(
            observe_base, "myshop", "run-1", [_entry("GET", "https://api.myshop.example.com/a")]
        )
        with pytest.raises(EndpointSpecError) as caught:
            parse_endpoint("get /a")
        result = runner.invoke(_build_app(), ["observe", "fixtures", "myshop", "--match", "get /a"])
        assert result.exit_code == 1
        assert f"--match: {caught.value}" in " ".join(_plain(result.output).split())
```

- [ ] **Step 3: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_har_naming.py tests/unit/test_observe_commands.py::TestMatchPatternValidation -q`
Expected: FAIL (`ImportError: cannot import name 'EndpointSpecError'`).

- [ ] **Step 4: Write the parsers**

In `src/graftpunk/har/naming.py`, extend `__all__` to `["EndpointSpecError", "HTTP_METHODS", "capture_filename", "capture_slug", "parse_command_spec", "parse_endpoint"]` and append:

```python
HTTP_METHODS: frozenset[str] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"}
)

_ENDPOINT_EXAMPLE = '"GET /orders/{order_id}"'
_COMMAND_EXAMPLE = '"order=GET /orders/{order_id}"'


class EndpointSpecError(ValueError):
    """A value that does not read as an endpoint or a command spec. The message says
    how to write one, and every consumer prints it unchanged."""


def parse_endpoint(value: str) -> tuple[str, str]:
    """``"<METHOD> <template>"``, the way the digest prints an endpoint, as its pair.

    The one reader of the grammar: ``gp observe fixtures --match``, both
    ``--command`` options, and the skill's own composition all take the halves
    from here. The method is one of :data:`HTTP_METHODS`, in capitals; the
    template is everything after the first space, stripped, and may be a glob
    where the consumer accepts one. A value that splits wrong would match nothing
    and look like an empty result, so it is refused instead (final fix wave,
    2026-09-12).

    Raises:
        EndpointSpecError: No space, a method that is not a capitalised HTTP
            method, or an empty template.
    """
    method, separator, template = value.strip().partition(" ")
    template = template.strip()
    if not separator or not template or method not in HTTP_METHODS:
        raise EndpointSpecError(
            f'{value!r} is not a "METHOD template" pair. Write the method in capitals, '
            f"a space, then the template, as in {_ENDPOINT_EXAMPLE}."
        )
    return method, template


def parse_command_spec(value: str) -> tuple[str, str, str]:
    """``"<name>=<METHOD> <template>"`` as its (name, method, template) triple.

    A command name cannot contain ``=``, so the split on the first one is
    unambiguous; the endpoint half goes through :func:`parse_endpoint`. Whether
    the name is a usable command name is the generator's rule, not this one's.

    Raises:
        EndpointSpecError: No ``=``, an empty name, nothing after the ``=``, or
            an endpoint half :func:`parse_endpoint` refuses.
    """
    name, separator, endpoint = value.partition("=")
    name = name.strip()
    if not separator:
        raise EndpointSpecError(
            f"{value!r} has no '='. Write the command name, '=', then the endpoint, "
            f"as in {_COMMAND_EXAMPLE}."
        )
    if not name:
        raise EndpointSpecError(
            f"{value!r} has no command name before '='. Write one, as in {_COMMAND_EXAMPLE}."
        )
    if not endpoint.strip():
        raise EndpointSpecError(
            f"{value!r} has nothing after '='. Write the endpoint the way the digest "
            f"prints it, as in {_COMMAND_EXAMPLE}."
        )
    method, template = parse_endpoint(endpoint)
    return name, method, template
```

- [ ] **Step 5: Route the matcher through the parser**

In `src/graftpunk/cli/observe_commands.py`: delete `_HTTP_METHODS` (lines 43-45) and `_validate_match_patterns`; change the import `from graftpunk.har.naming import capture_filename` to `from graftpunk.har.naming import EndpointSpecError, capture_filename, parse_endpoint`; replace `_matches_template` with:

```python
def _matches_template(entry_method: str, entry_template: str, endpoint: tuple[str, str]) -> bool:
    """True when the entry is *endpoint*: the same method, and a template equal to the
    pattern's or matching it as a glob. *endpoint* is :func:`parse_endpoint`'s pair;
    this function never splits a pattern itself."""
    method, template = endpoint
    if method != entry_method:
        return False
    return entry_template == template or fnmatch.fnmatch(entry_template, template)


def _parsed_matches(patterns: list[str]) -> list[tuple[str, str]]:
    """Every ``--match`` value as its pair, refusing the first that does not parse."""
    parsed: list[tuple[str, str]] = []
    for pattern in patterns:
        try:
            parsed.append(parse_endpoint(pattern))
        except EndpointSpecError as exc:
            console.print(f"[red]--match: {escape(str(exc))}[/red]")
            raise typer.Exit(1) from None
    return parsed
```

In `fixtures_cmd`, replace `_validate_match_patterns(match)` with `endpoints = _parsed_matches(match)`, and replace `if not any(_matches_template(method, template, pattern) for pattern in match):` with `if not any(_matches_template(method, template, e) for e in endpoints):`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_har_naming.py tests/unit/test_observe_commands.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/graftpunk/har/naming.py src/graftpunk/cli/observe_commands.py tests/unit/test_har_naming.py tests/unit/test_observe_commands.py
git commit -m "feat(har): parse_endpoint and parse_command_spec own the endpoint grammar, and the matcher takes its halves from them"
```

---

### Task 7: The sidecar owner, its writer, and `FixtureSession` reading through it

**Files:**
- Create: `src/graftpunk/testing/sidecar.py`
- Modify: `src/graftpunk/testing/__init__.py:13` (`import json`, deleted), `:76-80` (the class docstring's sidecar sentence), `:96-131` (`FixtureSession.request`, `_respond_from_file`)
- Modify: `src/graftpunk/har/digest.py` (new `flagged_names_of`; `__all__`)
- Modify: `src/graftpunk/devtools/captures.py` (new `write_sidecar`; `__all__`)
- Modify: `src/graftpunk/cli/observe_commands.py:13` (`import json as jsonlib`, deleted), `:27-28` (the captures and digest imports), `:201-296` (`fixtures_cmd` body: it writes the sidecar through the writer)
- Modify: `tests/unit/test_graftpunk_testing.py:66-74` (the sidecar test writes a schema 1 sidecar)
- Modify: `docs/PLUGIN_DEVELOPMENT.md` ("Test against fixtures, not against the site": the sentence that introduces the sidecar, the JSON block after it, and the error-path sentence of the paragraph after that), `docs/HOW_IT_WORKS.md:483-486` (the sidecar's field list)
- Test: `tests/unit/test_testing_sidecar.py`, `tests/unit/test_har_digest.py`, `tests/unit/test_devtools_captures.py`, `tests/unit/test_observe_commands.py`, `tests/unit/test_graftpunk_testing.py`

**Interfaces:**
- Consumes: `current_schema("sidecar")`, `refuse_unknown_schema`, `UnknownSchemaError` (Task 2); `digest`, `DigestSource`, `RunDigest` (existing).
- Produces, in `graftpunk.testing.sidecar`: `SIDECAR_SUFFIX = ".meta.json"`; `SIDECAR_FIELDS: dict[int, frozenset[str]]` (version 1: `{"status", "content_type", "body_params", "capture_sha256", "flagged_names"}`); `@dataclass(frozen=True) class Sidecar(status: int, content_type: str, body_params: tuple[str, ...] = (), capture_sha256: str | None = None, flagged_names: tuple[str, ...] = ())` with property `declared -> bool`; `class SidecarError(ValueError)`; `sidecar_path(fixture: Path) -> Path`; `is_sidecar(path: Path) -> bool`; `sidecar_payload(sidecar: Sidecar) -> dict[str, object]`; `sidecar_text(sidecar: Sidecar) -> str`; `load_sidecar(path: Path) -> Sidecar`. In `graftpunk.har.digest`: `flagged_names_of(d: RunDigest) -> tuple[str, ...]`. In `graftpunk.devtools.captures`, which imports nothing from the digest and takes the names as plain strings: `write_sidecar(fixture: Path, *, status: int, content_type: str, body_params: Iterable[str], flagged_names: Iterable[str]) -> Path`. The project-tools plan's `fixtures_are_sanitised` uses `is_sidecar`, `sidecar_path`, `load_sidecar`, `SidecarError`, and `Sidecar`.

**Accepted cost, with a trigger:** `gp observe fixtures` runs the full digest over the run to learn two name sets (cookie names and token candidate names) for `flagged_names`, as the spec has it. The trigger for a narrower entry point (a `har.digest` function that computes only those two sets) is a second caller that needs only the names, or a recording on which the digest dominates the command's run time.

- [ ] **Step 1: Write the failing owner tests**

Create `tests/unit/test_testing_sidecar.py`:

```python
"""The fixture sidecar's one owner: keys per schema version and the loader
(graft skill spec, 2026-09-21, "A sidecar with one owner")."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from graftpunk.contracts import current_schema
from graftpunk.testing.sidecar import (
    SIDECAR_FIELDS,
    Sidecar,
    SidecarError,
    is_sidecar,
    load_sidecar,
    sidecar_path,
    sidecar_payload,
    sidecar_text,
)

_V1_FIELDS = {"status", "content_type", "body_params", "capture_sha256", "flagged_names"}


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _v1(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": 1,
        "status": 200,
        "content_type": "application/json",
        "body_params": [],
        "capture_sha256": None,
        "flagged_names": [],
    }
    payload.update(overrides)
    return payload


class TestTheFormat:
    def test_version_one_is_the_committable_key_set(self) -> None:
        assert SIDECAR_FIELDS[1] == _V1_FIELDS

    def test_every_version_up_to_the_current_one_has_a_key_set(self) -> None:
        assert set(SIDECAR_FIELDS) == set(range(1, current_schema("sidecar") + 1))

    def test_the_payload_writes_exactly_the_current_schemas_keys(self) -> None:
        payload = sidecar_payload(Sidecar(status=200, content_type="text/html"))
        assert set(payload) == SIDECAR_FIELDS[current_schema("sidecar")] | {"schema"}
        assert payload["schema"] == current_schema("sidecar")

    def test_the_path_rule(self, tmp_path: Path) -> None:
        fixture = tmp_path / "get_orders.json"
        assert sidecar_path(fixture) == tmp_path / "get_orders.json.meta.json"
        assert is_sidecar(sidecar_path(fixture))
        assert not is_sidecar(fixture)


class TestLoad:
    def test_round_trip(self, tmp_path: Path) -> None:
        sidecar = Sidecar(
            status=403,
            content_type="text/plain",
            body_params=("page",),
            capture_sha256="ab" * 32,
            flagged_names=("shop_session",),
        )
        path = tmp_path / "x.json.meta.json"
        path.write_text(sidecar_text(sidecar), encoding="utf-8")
        assert load_sidecar(path) == sidecar

    def test_a_missing_schema_is_refused(self, tmp_path: Path) -> None:
        payload = _v1()
        del payload["schema"]
        with pytest.raises(SidecarError, match="sidecar: no schema number"):
            load_sidecar(_write(tmp_path / "a.meta.json", payload))

    def test_an_unknown_schema_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(SidecarError, match="schema 99"):
            load_sidecar(_write(tmp_path / "a.meta.json", _v1(schema=99)))

    def test_a_key_outside_the_version_is_refused(self, tmp_path: Path) -> None:
        """An old-format or hand-edited sidecar cannot smuggle a URL in."""
        with pytest.raises(SidecarError, match="url"):
            load_sidecar(_write(tmp_path / "a.meta.json", _v1(url="https://myshop.example/")))

    def test_a_missing_key_is_refused(self, tmp_path: Path) -> None:
        payload = _v1()
        del payload["flagged_names"]
        with pytest.raises(SidecarError, match="flagged_names"):
            load_sidecar(_write(tmp_path / "a.meta.json", payload))

    @pytest.mark.parametrize(
        "payload",
        [["not", "an", "object"], "text", _v1(status="200"), _v1(body_params="page")],
    )
    def test_a_sidecar_that_is_not_an_object_or_has_a_wrong_type_is_refused(
        self, tmp_path: Path, payload: object
    ) -> None:
        path = _write(tmp_path / "a.meta.json", payload)
        with pytest.raises(SidecarError, match="a.meta.json"):
            load_sidecar(path)

    def test_declared_means_no_capture_hash(self) -> None:
        assert Sidecar(status=200, content_type="x").declared
        assert not Sidecar(status=200, content_type="x", capture_sha256="ab" * 32).declared


def test_graftpunk_testing_imports_nothing_from_devtools() -> None:
    """The placement rule, asserted on the import graph rather than a docstring."""
    script = (
        "import sys\n"
        "import graftpunk.testing, graftpunk.testing.sidecar\n"
        "print(sorted(m for m in sys.modules if m.startswith('graftpunk.devtools')))\n"
    )
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=True
    )
    assert result.stdout.strip() == "[]"
```

- [ ] **Step 2: Write the failing writer, session, and CLI tests**

In `tests/unit/test_devtools_captures.py`, add `import hashlib`, `import json`, and `from graftpunk.testing.sidecar import load_sidecar, sidecar_path` to the imports at the top, add `write_sidecar` to its `graftpunk.devtools.captures` import, and append:

```python
class TestWriteSidecar:
    def test_writes_the_schema_keys_and_the_hash_of_the_file_on_disk(self, tmp_path: Path) -> None:
        fixture = tmp_path / "get_orders.json"
        fixture.write_text('{"orders": []}', encoding="utf-8")
        path = write_sidecar(
            fixture,
            status=200,
            content_type="application/json",
            body_params=["page", "archived"],
            flagged_names=["shop_session", "csrf-token", "shop_session"],
        )
        assert path == sidecar_path(fixture)
        sidecar = load_sidecar(path)
        assert sidecar.capture_sha256 == hashlib.sha256(fixture.read_bytes()).hexdigest()
        assert sidecar.body_params == ("archived", "page")
        assert sidecar.flagged_names == ("csrf-token", "shop_session")
        assert "url" not in json.loads(path.read_text())
        assert "captured_at" not in json.loads(path.read_text())
```

The captures import then passes 100 columns; write it in the parenthesised form, one name per line.

Append to `tests/unit/test_har_digest.py` (add `flagged_names_of` to its `from graftpunk.har.digest import (...)` block; `json` and `Path` are already imported):

```python
class TestFlaggedNamesOf:
    def test_flagged_names_are_exactly_the_digest_cookie_and_token_names(
        self, tmp_path: Path
    ) -> None:
        entry = {
            "startedDateTime": "2026-09-10T10:00:00.000Z",
            "time": 1,
            "request": {
                "method": "GET",
                "url": "https://myshop.example.com/account",
                "headers": [],
                "cookies": [],
                "queryString": [],
            },
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": [{"name": "Content-Type", "value": "text/html"}],
                "cookies": [{"name": "sid", "value": "planted"}],
                "content": {
                    "mimeType": "text/html",
                    "text": '<meta name="csrf-token" content="planted">',
                    "size": 42,
                },
            },
        }
        har = tmp_path / "network.har"
        har.write_text(json.dumps({"log": {"version": "1.2", "entries": [entry]}}))
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result) == tuple(
            sorted(set(result.cookies) | {t.name for t in result.tokens})
        )
        assert flagged_names_of(result) == ("csrf-token", "sid")
```

In `tests/unit/test_graftpunk_testing.py`, add `from graftpunk.testing.sidecar import Sidecar, SidecarError, sidecar_text`, replace the body of `test_sidecar_supplies_status_and_content_type` with:

```python
        (tmp_path / "get_orders.json").write_text("Forbidden")
        (tmp_path / "get_orders.json.meta.json").write_text(
            sidecar_text(Sidecar(status=403, content_type="text/plain"))
        )
        session = FixtureSession(tmp_path)
        response = session.get("https://myshop.example.com/orders")
        assert response.status_code == 403
        assert response.headers["Content-Type"] == "text/plain"
```

and append to `class TestFixtureSession`:

```python
    def test_a_sidecar_with_no_schema_is_refused_not_half_read(self, tmp_path: Path) -> None:
        (tmp_path / "get_orders.json").write_text("{}")
        (tmp_path / "get_orders.json.meta.json").write_text(json.dumps({"status": 403}))
        session = FixtureSession(tmp_path)
        with pytest.raises(SidecarError, match="get_orders.json.meta.json"):
            session.get("https://myshop.example.com/orders")
```

In `tests/unit/test_observe_commands.py`, add `import hashlib` and append to `class TestFixturesCommand`:

```python
    def test_the_sidecar_is_committable_and_records_the_capture(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        order = _entry("GET", "https://api.myshop.example.com/orders/1?page=2", body='{"id": 1}')
        order["response"]["cookies"] = [{"name": "shop_session", "value": "planted"}]
        _write_run(observe_base, "myshop", "run-1", [order])
        out_dir = tmp_path / "out"
        result = _invoke_fixtures(out_dir)
        assert result.exit_code == 0, result.output
        (fixture,) = [
            p for p in out_dir.glob("get_orders_*.json") if not p.name.endswith(".meta.json")
        ]
        sidecar = json.loads((out_dir / f"{fixture.name}.meta.json").read_text())
        assert set(sidecar) == {
            "schema",
            "status",
            "content_type",
            "body_params",
            "capture_sha256",
            "flagged_names",
        }
        assert sidecar["schema"] == 1
        assert sidecar["capture_sha256"] == hashlib.sha256(fixture.read_bytes()).hexdigest()
        assert sidecar["flagged_names"] == ["shop_session"]
        assert "page=2" not in json.dumps(sidecar)
```

- [ ] **Step 3: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_testing_sidecar.py tests/unit/test_har_digest.py tests/unit/test_devtools_captures.py tests/unit/test_graftpunk_testing.py tests/unit/test_observe_commands.py::TestFixturesCommand -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'graftpunk.testing.sidecar'`).

- [ ] **Step 4: Write the owner**

Create `src/graftpunk/testing/sidecar.py`:

```python
"""The fixture sidecar's one owner: the keys of each schema version, and the loader.

A ``<fixture>.meta.json`` sidecar sits beside every file ``gp observe fixtures``
writes and travels with the fixture into a plugin's committed fixtures tree.
Every field in it may be committed: the capture's URL and time are not in it
(the HAR and the digest hold them), ``capture_sha256`` is the hash of the
captured body, and ``flagged_names`` is the cookie and token names the digest
recorded, never a value.

This module is the only place a key set is spelled. It takes the current
version number from :mod:`graftpunk.contracts`, and a later version adds its
own key set to :data:`SIDECAR_FIELDS` so sidecars committed under an earlier
one still load. Pytest-free, because ``FixtureSession`` reads through it; the
writer, which only ``gp observe fixtures`` runs, lives in
``graftpunk.devtools.captures`` and builds its text here (graft skill spec,
2026-09-21).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from graftpunk.contracts import UnknownSchemaError, current_schema, refuse_unknown_schema

__all__ = [
    "SIDECAR_FIELDS",
    "SIDECAR_SUFFIX",
    "Sidecar",
    "SidecarError",
    "is_sidecar",
    "load_sidecar",
    "sidecar_path",
    "sidecar_payload",
    "sidecar_text",
]

SIDECAR_SUFFIX = ".meta.json"

SIDECAR_FIELDS: dict[int, frozenset[str]] = {
    1: frozenset({"status", "content_type", "body_params", "capture_sha256", "flagged_names"}),
}
"""The keys each schema version holds besides ``schema`` itself."""


@dataclass(frozen=True)
class Sidecar:
    """One sidecar's facts, independent of the version it was read from."""

    status: int
    content_type: str
    body_params: tuple[str, ...] = ()
    # None: the fixture came off no capture, which the author declares by
    # writing null; the in-suite sanitisation check counts it as declared.
    capture_sha256: str | None = None
    flagged_names: tuple[str, ...] = ()

    @property
    def declared(self) -> bool:
        """True for a hand-made fixture's sidecar: trusted, not checked against a capture."""
        return self.capture_sha256 is None


class SidecarError(ValueError):
    """A sidecar that cannot be read, or that is outside its declared format."""


def sidecar_path(fixture: Path) -> Path:
    """Where *fixture*'s sidecar lives: beside it, named ``<fixture name>.meta.json``."""
    return fixture.with_name(fixture.name + SIDECAR_SUFFIX)


def is_sidecar(path: Path) -> bool:
    """True when *path* is a sidecar rather than a fixture."""
    return path.name.endswith(SIDECAR_SUFFIX)


def sidecar_payload(sidecar: Sidecar) -> dict[str, object]:
    """*sidecar* as the current schema's JSON object."""
    return {
        "schema": current_schema("sidecar"),
        "status": sidecar.status,
        "content_type": sidecar.content_type,
        "body_params": list(sidecar.body_params),
        "capture_sha256": sidecar.capture_sha256,
        "flagged_names": list(sidecar.flagged_names),
    }


def sidecar_text(sidecar: Sidecar) -> str:
    """*sidecar* as the text a sidecar file holds: indented JSON, sorted keys."""
    return json.dumps(sidecar_payload(sidecar), indent=2, sort_keys=True) + "\n"


def load_sidecar(path: Path) -> Sidecar:
    """Read the sidecar at *path*, holding it to its version's key set.

    Raises:
        SidecarError: The file is unreadable or not a JSON object; its ``schema``
            is missing or unknown; it has a key outside its version's set or
            lacks one; or a value has the wrong type. The message names *path*.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SidecarError(f"{path}: not a readable JSON sidecar ({exc})") from exc
    if not isinstance(data, dict):
        raise SidecarError(f"{path}: a sidecar is a JSON object")
    try:
        version = refuse_unknown_schema("sidecar", data.get("schema"))
    except UnknownSchemaError as exc:
        raise SidecarError(f"{path}: {exc}") from exc
    expected = SIDECAR_FIELDS[version] | {"schema"}
    extra = sorted(set(data) - expected)
    missing = sorted(expected - set(data))
    if extra:
        raise SidecarError(f"{path}: keys outside sidecar schema {version}: {', '.join(extra)}")
    if missing:
        raise SidecarError(f"{path}: missing keys: {', '.join(missing)}")
    return Sidecar(
        status=_integer(data, "status", path),
        content_type=_text(data, "content_type", path),
        body_params=_names(data, "body_params", path),
        capture_sha256=_optional_text(data, "capture_sha256", path),
        flagged_names=_names(data, "flagged_names", path),
    )


def _integer(data: dict[str, object], key: str, path: Path) -> int:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise SidecarError(f"{path}: {key} must be an integer, got {value!r}")
    return value


def _text(data: dict[str, object], key: str, path: Path) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise SidecarError(f"{path}: {key} must be a string, got {value!r}")
    return value


def _optional_text(data: dict[str, object], key: str, path: Path) -> str | None:
    value = data[key]
    if value is not None and not isinstance(value, str):
        raise SidecarError(f"{path}: {key} must be a string or null, got {value!r}")
    return value


def _names(data: dict[str, object], key: str, path: Path) -> tuple[str, ...]:
    value = data[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SidecarError(f"{path}: {key} must be a list of strings, got {value!r}")
    return tuple(value)
```

- [ ] **Step 5: Read sidecars through the owner in `FixtureSession`**

In `src/graftpunk/testing/__init__.py`: delete `import json`, add `from graftpunk.testing.sidecar import is_sidecar, load_sidecar, sidecar_path`, change the glob filter `and not p.name.endswith(".meta.json")` to `and not is_sidecar(p)`, and replace `_respond_from_file` with:

```python
    def _respond_from_file(self, path: Path, response: requests.Response) -> requests.Response:
        """Answer from *path*; its sidecar, when present, is read through the owner,
        which refuses a sidecar outside its declared format rather than half-reading it."""
        meta_path = sidecar_path(path)
        status = 200
        content_type = _CONTENT_TYPE_BY_SUFFIX.get(path.suffix, _DEFAULT_FIXTURE_CONTENT_TYPE)
        if meta_path.exists():
            sidecar = load_sidecar(meta_path)
            status = sidecar.status
            content_type = sidecar.content_type
        response.status_code = status
        response.headers["Content-Type"] = content_type
        response._content = path.read_bytes()
        return response
```

Update the class docstring's sidecar sentence to: "A sidecar ``<filename>.meta.json`` beside a fixture supplies its status and content type when present, read through :mod:`graftpunk.testing.sidecar`; without one, the status is 200 and the type is guessed from the file's extension."

- [ ] **Step 6: Write the writer**

In `src/graftpunk/har/digest.py`, add `"flagged_names_of"` to `__all__` and append after `digest()`:

```python
def flagged_names_of(d: RunDigest) -> tuple[str, ...]:
    """The names a committed fixture must never contain: every cookie name the digest
    recorded on the primary host and every token candidate's name, exactly as the
    digest holds them, sorted and deduplicated. It lives beside the digest that
    records the names, so the sidecar writer takes plain strings and imports
    nothing from the digest."""
    return tuple(sorted(set(d.cookies) | {token.name for token in d.tokens}))
```

In `src/graftpunk/devtools/captures.py`, add `import hashlib` and `from collections.abc import Iterable`, add `from graftpunk.testing.sidecar import Sidecar, sidecar_path, sidecar_text`, extend `__all__` with `"write_sidecar"`, and append:

```python
def write_sidecar(
    fixture: Path,
    *,
    status: int,
    content_type: str,
    body_params: Iterable[str],
    flagged_names: Iterable[str],
) -> Path:
    """Write *fixture*'s committable sidecar beside it and return its path.

    *fixture* must already be on disk: ``capture_sha256`` is the hash of its bytes
    as written, which is what the in-suite check compares a derived fixture to.
    """
    sidecar = Sidecar(
        status=status,
        content_type=content_type,
        body_params=tuple(sorted(set(body_params))),
        capture_sha256=hashlib.sha256(fixture.read_bytes()).hexdigest(),
        flagged_names=tuple(sorted(set(flagged_names))),
    )
    path = sidecar_path(fixture)
    path.write_text(sidecar_text(sidecar), encoding="utf-8")
    return path
```

Update the module docstring's first paragraph to add: "It also writes the committable sidecar beside each capture, through the format :mod:`graftpunk.testing.sidecar` owns."

- [ ] **Step 7: Write sidecars through the writer in `gp observe fixtures`**

In `src/graftpunk/cli/observe_commands.py`: delete `import json as jsonlib`; change the captures import to the parenthesised form, one name per line, that ruff format gives an import past 100 columns:

```python
from graftpunk.devtools.captures import (
    CAPTURES_DIR,
    ensure_ignored,
    find_repo_root,
    is_tracked,
    write_sidecar,
)
```

change the digest import to `from graftpunk.har.digest import DigestSource, body_params, digest, flagged_names_of`; after `entries = parse_har_file(har_path).entries` add:

```python
    run_digest = digest(DigestSource.from_run_dir(run_dir, session=session, run_id=run_dir.name))
    flagged = flagged_names_of(run_digest)
```

and replace the `meta_path = ...` line and the `try:` block that writes both files with:

```python
        try:
            file_path.write_text(entry.response.body, encoding="utf-8")
            write_sidecar(
                file_path,
                status=entry.response.status,
                content_type=content_type,
                body_params=body_params(entry),
                flagged_names=flagged,
            )
        except OSError as exc:
            _refuse_write(file_path, exc)
```

- [ ] **Step 8: Describe the schema 1 sidecar in the docs**

In `docs/PLUGIN_DEVELOPMENT.md`, "Test against fixtures, not against the site", replace the sentence "A `<filename>.meta.json` sidecar beside a fixture supplies its status and content type. `gp observe fixtures` writes one for every capture:" and the JSON block after it with:

````markdown
A `<filename>.meta.json` sidecar beside a fixture supplies its status and
content type. `gp observe fixtures` writes one for every capture, and it is safe
to commit: it holds no URL, no time, and no value, only the hash of the captured
body and the cookie and token names the recording's digest listed.

```json
{
  "body_params": [],
  "capture_sha256": "4f6c1e0a9d2b7c3e8f5a1d6b0c9e2f7a3b8d4c1e6f0a5b9c2d7e3f8a1b6c0d4e",
  "content_type": "application/json",
  "flagged_names": ["X-Csrf-Token", "myshop_session"],
  "schema": 1,
  "status": 200
}
```
````

In the paragraph after it, replace "A sidecar is how you test an error path: copy a fixture, set the sidecar's status to 403, and assert that the command raises `SessionRejectedError`." with "A sidecar is how you test an error path: copy a fixture together with its sidecar, set the copied sidecar's `status` to 403, and assert that the command raises `SessionRejectedError`." Leave that paragraph's first sentence ("Without a sidecar ...") as it is; the project-tools plan's last task rewrites it once the generated suite requires a sidecar.

In `docs/HOW_IT_WORKS.md`, replace "writes a `<file>.meta.json` sidecar beside every capture (url, status, content type, body parameter names, capture time); `FixtureSession` reads the same sidecar for status and content type" (lines 483 to 485) with "writes a `<file>.meta.json` sidecar beside every capture (a `schema` number, the status, the content type, the body parameter names, the hash of the captured body, and the cookie and token names the run's digest recorded, but never the URL or the capture time), safe to commit beside the fixture derived from it; `FixtureSession` reads the sidecar through `graftpunk.testing.sidecar` for status and content type". The phrase wraps across lines 483 to 485, so match it with the line breaks as they are in the file. The rest of line 485 (", so a fixture copied from") and line 486 ("a capture keeps its recorded status.") stay as they are.

- [ ] **Step 9: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_testing_sidecar.py tests/unit/test_har_digest.py tests/unit/test_devtools_captures.py tests/unit/test_graftpunk_testing.py tests/unit/test_observe_commands.py tests/unit/test_scaffold_cli.py tests/unit/test_plugin_development_guide.py -q`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/graftpunk/testing/sidecar.py src/graftpunk/testing/__init__.py src/graftpunk/har/digest.py src/graftpunk/devtools/captures.py src/graftpunk/cli/observe_commands.py tests/unit/test_testing_sidecar.py tests/unit/test_har_digest.py tests/unit/test_devtools_captures.py tests/unit/test_graftpunk_testing.py tests/unit/test_observe_commands.py docs/PLUGIN_DEVELOPMENT.md docs/HOW_IT_WORKS.md
git commit -m "feat(testing): the fixture sidecar gets one owner and a committable, versioned format"
```

---

### Task 8: `gp plugin new <name> --check-name`

**Files:**
- Modify: `src/graftpunk/cli/scaffold_commands.py:24` (import), `:82-124` (`plugin_new`'s signature and its early refusals; the function runs to line 184), new `_name_refusal`
- Test: `tests/unit/test_scaffold_cli.py`

**Interfaces:**
- Consumes: `validate_plugin_name` (existing, `render.py`), `reserved_cli_names()` (existing).
- Produces: `gp plugin new NAME --check-name`: writes nothing; exit 0 printing `'<name>' is an acceptable plugin name.`; exit 1 printing exactly the refusal `gp plugin new NAME` prints. `_name_refusal(name: str) -> tuple[str, str] | None` returning `(log_reason, message)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_scaffold_cli.py`:

```python
class TestCheckName:
    """--check-name answers "is this name acceptable" with gp plugin new's own
    validation, writing nothing (graft skill spec, 2026-09-21)."""

    def test_a_valid_name_is_accepted_and_nothing_is_written(self, tmp_path: Path) -> None:
        from graftpunk.cli.main import app as real_app

        before = set(tmp_path.iterdir())
        result = runner.invoke(
            real_app, ["plugin", "new", "myshop", "--check-name", "--dir", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output
        assert "acceptable" in _plain(result.output)
        assert set(tmp_path.iterdir()) == before

    @pytest.mark.parametrize("name", ["observe", "2fa-site", "a" * 41])
    def test_a_refusal_is_the_same_text_gp_plugin_new_prints(
        self, tmp_path: Path, name: str
    ) -> None:
        from graftpunk.cli.main import app as real_app

        checked = runner.invoke(real_app, ["plugin", "new", name, "--check-name"])
        created = runner.invoke(real_app, ["plugin", "new", name, "--dir", str(tmp_path)])
        assert checked.exit_code == created.exit_code == 1
        assert _plain(checked.output) == _plain(created.output)
        assert list(tmp_path.iterdir()) == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_cli.py::TestCheckName -q`
Expected: FAIL (`No such option: --check-name`).

- [ ] **Step 3: Write the option**

In `src/graftpunk/cli/scaffold_commands.py`, change the render import to `from graftpunk.devtools.scaffold.render import ScaffoldSpec, fixture_paths, validate_plugin_name`, add above `plugin_new`:

```python
def _name_refusal(name: str) -> tuple[str, str] | None:
    """The refusal ``gp plugin new`` gives for *name*, as (log reason, message), or None.

    One owner for the two name checks: the reserved top-level names snapshotted
    at attach time, then the name rule ``ScaffoldSpec`` enforces. ``--check-name``
    and the real run both call this, so their refusals cannot differ.
    """
    if name in reserved_cli_names():
        return "reserved_name", f"'{name}' is a reserved command name and cannot be a plugin name."
    try:
        validate_plugin_name(name)
    except ValueError as exc:
        return "invalid_name", str(exc)
    return None
```

add this parameter to `plugin_new` after `new`:

```python
    check_name: Annotated[
        bool,
        typer.Option("--check-name", help="Check NAME the way this command would, write nothing"),
    ] = False,
```

and replace the start of the body, from `if backend not in _SUPPORTED_BACKENDS:` through the reserved-name block, with:

```python
    refusal = _name_refusal(name)
    if check_name:
        if refusal is not None:
            console.print(f"[red]{escape(refusal[1])}[/red]")
            raise typer.Exit(1)
        console.print(f"'{escape(name)}' is an acceptable plugin name.")
        return
    if backend not in _SUPPORTED_BACKENDS:
        LOG.debug("scaffold_refused", reason="bad_backend", backend=backend)
        console.print(
            f"[red]--backend must be one of {_SUPPORTED_BACKENDS}, got '{escape(backend)}'[/red]"
        )
        raise typer.Exit(1)
    # ty narrows `backend: str` to `_BackendName` from the membership check
    # above (against a tuple typed `tuple[_BackendName, ...]`): no cast needed.

    if refusal is not None:
        LOG.debug("scaffold_refused", reason=refusal[0], name=name)
        console.print(f"[red]{escape(refusal[1])}[/red]")
        raise typer.Exit(1)
```

The `except ValueError` arm further down stays: `ScaffoldSpec` still validates, and that arm now only catches what the early check did not.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_cli.py -q`
Expected: PASS, including `TestRefusalReasons.test_an_invalid_name_still_logs_invalid_name`.

- [ ] **Step 5: Commit**

```bash
git add src/graftpunk/cli/scaffold_commands.py tests/unit/test_scaffold_cli.py
git commit -m "feat(scaffold): gp plugin new --check-name refuses a name with the same text and writes nothing"
```

---

### Task 9: `policy.py` and the fixtures-root rule

**Files:**
- Create: `src/graftpunk/devtools/scaffold/policy.py`
- Modify: `src/graftpunk/devtools/scaffold/render.py` (the public `fixtures_root(spec)` is renamed `fixtures_root_for(spec)`, in `__all__` and at its three callers, and reads policy; `_fixtures_dir_expression` derived from it; `render()` builds the test-module, conftest, and `.gitkeep` paths from `policy.TESTS_DIR` and the fixtures root)
- Test: `tests/unit/test_scaffold_policy.py`, `tests/unit/test_scaffold_render.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `graftpunk.devtools.scaffold.policy.TESTS_DIR: Final = "tests/"`; `FIXTURES_TREE: Final = f"{TESTS_DIR}fixtures/"` (`"tests/fixtures/"`); `fixtures_root(*, suite_member: bool, module_name: str) -> str`; `render.fixtures_root_for(spec: ScaffoldSpec) -> str`, the spec-taking wrapper, public (it replaces the public `render.fixtures_root`, whose name now belongs to the policy's rule alone), so the project-tools plan's tests import it by that name rather than reaching for a private one. The tests directory is spelled once, here; `render.py` derives the generated `FIXTURES_DIR` expression and every `tests/` path in `render()` from `TESTS_DIR`. The project-tools plan adds `CONFTEST_PATH`, `FIXTURES_PLACEHOLDER` (imported from `graftpunk.testing.sidecar`), `GP_FILL_MARKER`, `module_name_for`, `PROJECT_GATE`, `ProjectRequirement`, and `PROJECT_REQUIREMENTS` to this module, and its reader calls `policy.fixtures_root` from the project on disk. The entry-point group is not policy's: it stays `graftpunk.plugins.PLUGINS_GROUP`, and policy does not import it.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_scaffold_policy.py`:

```python
"""Declarative project policy: nothing here touches the filesystem
(graft skill spec, 2026-09-21, "Project policy is declarative")."""

from __future__ import annotations

import ast
from pathlib import Path

import graftpunk.devtools.scaffold.policy as policy
from graftpunk.devtools.scaffold.policy import FIXTURES_TREE, TESTS_DIR, fixtures_root


def test_the_tree_lies_under_the_tests_directory() -> None:
    assert (TESTS_DIR, FIXTURES_TREE) == ("tests/", "tests/fixtures/")
    assert FIXTURES_TREE.startswith(TESTS_DIR)


def test_a_standalone_project_uses_the_tree_itself() -> None:
    assert fixtures_root(suite_member=False, module_name="myshop") == "tests/fixtures/"


def test_a_suite_member_owns_a_directory_under_the_tree() -> None:
    assert fixtures_root(suite_member=True, module_name="my_shop") == "tests/fixtures/my_shop/"


def test_every_root_lies_under_the_tree() -> None:
    for suite_member in (False, True):
        assert fixtures_root(suite_member=suite_member, module_name="x").startswith(FIXTURES_TREE)


# Modules that can touch the filesystem. Policy is data, so it imports none of
# them. A denylist rather than an allowlist: a later addition to policy (a
# dataclass, a regex, a constant owned elsewhere) needs no edit here, and the
# check does not loosen as policy grows.
_FILESYSTEM_MODULES = (
    "os",
    "shutil",
    "pathlib",
    "io",
    "tempfile",
    "subprocess",
    "graftpunk.devtools.captures",
)


def test_policy_imports_nothing_that_touches_the_filesystem() -> None:
    """Checks policy's own imports. graftpunk.devtools.captures is the writing side
    of the captures rule; its pure side, captures_rule, is not on the list."""
    assert policy.__file__ is not None
    tree = ast.parse(Path(policy.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported |= {f"{node.module}.{alias.name}" for alias in node.names}
    touching = sorted(
        name
        for name in imported
        for module in _FILESYSTEM_MODULES
        if name == module or name.startswith(f"{module}.")
    )
    assert touching == []
```

Append to `tests/unit/test_scaffold_render.py`:

```python
class TestFixturesDirFollowsThePolicy:
    """The generated test module's FIXTURES_DIR is the policy's root for that plugin."""

    @staticmethod
    def _fixtures_dir(test_module: str, project: Path, test_file: str) -> Path:
        for node in ast.parse(test_module).body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "FIXTURES_DIR" for t in node.targets
            ):
                expression = ast.unparse(node.value)
                return eval(  # noqa: S307 - evaluates the generator's own Path expression
                    expression, {"Path": Path, "__file__": str(project / test_file)}
                )
        raise AssertionError("no FIXTURES_DIR in the generated test module")

    def test_a_new_project(self, tmp_path: Path) -> None:
        from graftpunk.devtools.scaffold.policy import fixtures_root

        spec = ScaffoldSpec(
            name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example"
        )
        files = render(spec)
        found = self._fixtures_dir(files["tests/test_plugin.py"], tmp_path, "tests/test_plugin.py")
        expected = fixtures_root(suite_member=False, module_name="myshop")
        assert found == tmp_path / expected.rstrip("/")

    def test_a_suite_member(self, tmp_path: Path) -> None:
        from graftpunk.devtools.scaffold.policy import fixtures_root

        spec = ScaffoldSpec(
            name="my-shop",
            mode="add_to_suite",
            backend="nodriver",
            base_url="https://myshop.example",
        )
        files = render(spec)
        found = self._fixtures_dir(
            files["tests/test_my_shop.py"], tmp_path, "tests/test_my_shop.py"
        )
        expected = fixtures_root(suite_member=True, module_name="my_shop")
        assert found == tmp_path / expected.rstrip("/")
        assert f"{expected}.gitkeep" in files
```

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_policy.py tests/unit/test_scaffold_render.py::TestFixturesDirFollowsThePolicy -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'graftpunk.devtools.scaffold.policy'`).

- [ ] **Step 3: Write the policy module**

Create `src/graftpunk/devtools/scaffold/policy.py`:

```python
"""What a generated plugin project must look like, as data.

Knows nothing about the filesystem and imports nothing that touches it, so a
reader, a lint, and a writer can all import it without importing each other
(graft skill spec, 2026-09-21, "Project policy is declarative and lives apart
from the writer").
"""

from __future__ import annotations

from typing import Final

__all__ = ["FIXTURES_TREE", "TESTS_DIR", "fixtures_root"]

TESTS_DIR: Final = "tests/"
"""The directory a generated project's tests live in, project-relative: the one
spelling every other tests-relative path here is built from."""

FIXTURES_TREE: Final = f"{TESTS_DIR}fixtures/"
"""The one directory every plugin's fixtures live under, project-relative.

The same in a standalone project and in a suite, and true for every member a
suite ever gains, which is why a generated conftest can carry it although it
is written once."""


def fixtures_root(*, suite_member: bool, module_name: str) -> str:
    """Where one plugin's fixtures live, project-relative, with a trailing slash.

    A standalone project uses the tree itself. A suite member owns a directory
    under it named for its module, because fixture names come from endpoint paths
    and two plugins in one suite can share a path (polish round 1, 2026-09-12).
    The generator supplies the two facts from its spec; a reader supplies them
    from the project on disk.
    """
    if suite_member:
        return f"{FIXTURES_TREE}{module_name}/"
    return FIXTURES_TREE
```

- [ ] **Step 4: Make the renderer read the policy**

In `src/graftpunk/devtools/scaffold/render.py`, add `from graftpunk.devtools.scaffold import policy`, and replace `fixtures_root` and `_fixtures_dir_expression` with the two functions below. The wrapper is renamed `fixtures_root_for`, and its `__all__` entry with it, so one name means one rule: `fixtures_root` is the policy's, always spelled `policy.fixtures_root` inside `render.py`, and `fixtures_root_for` applies it to a spec.

```python
def fixtures_root_for(spec: ScaffoldSpec) -> str:
    """Where *spec*'s generated tests look for fixtures: the policy's rule, from the
    spec's two facts. Also the directory ``gp plugin new``'s ``Next:`` line names."""
    return policy.fixtures_root(
        suite_member=spec.mode == "add_to_suite", module_name=module_name_for(spec.name)
    )


def _fixtures_dir_expression(spec: ScaffoldSpec) -> str:
    """The generated ``FIXTURES_DIR`` assignment's right-hand side: the fixtures root,
    relative to the test module, which lives in ``policy.TESTS_DIR``. Every root
    lies under that directory by the policy's own rule, which its tests pin."""
    parts = fixtures_root_for(spec).removeprefix(policy.TESTS_DIR).strip("/").split("/")
    return "Path(__file__).parent" + "".join(f' / "{part}"' for part in parts)
```

Rename the calls `fixtures_root(spec)` in `fixture_paths`, `_render_test_module`, and `render` to `fixtures_root_for(spec)` (`grep -n "fixtures_root(spec)" src/graftpunk/devtools/scaffold/render.py` finds them); nothing outside `render.py` calls it at `7bd9604`.

Then build every `tests/` path in `render()` from the policy, so the tests directory and the fixtures tree are spelled once. Replace its two return dicts' test-side keys: in the new-project dict, `"tests/conftest.py"` becomes `f"{policy.TESTS_DIR}conftest.py"`, `"tests/test_plugin.py"` becomes `f"{policy.TESTS_DIR}test_plugin.py"`, and `"tests/fixtures/.gitkeep"` becomes `f"{fixtures_root_for(spec)}.gitkeep"` (a new project's root is the tree itself); in the suite dict, `f"tests/test_{module_name_for(spec.name)}.py"` becomes `f"{policy.TESTS_DIR}test_{module_name_for(spec.name)}.py"`, and the `.gitkeep` key already reads `fixtures_root_for(spec)` after the rename. The project-tools plan's Task 2 replaces the conftest key with `policy.CONFTEST_PATH` and the `.gitkeep` name with `policy.FIXTURES_PLACEHOLDER`. The render tests that index `render(spec)` by literal path are the check that nothing moved.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_policy.py tests/unit/test_scaffold_render.py tests/unit/test_scaffold_cli.py tests/unit/test_scaffold_project.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/devtools/scaffold/policy.py src/graftpunk/devtools/scaffold/render.py tests/unit/test_scaffold_policy.py tests/unit/test_scaffold_render.py
git commit -m "feat(scaffold): the fixtures-root rule moves to a declarative policy module"
```

---

### Task 10: `write.py`, the one write discipline, with `write_scaffold` routed through it

**Files:**
- Create: `src/graftpunk/devtools/errors.py` (`DevtoolsRefusal`, `ScaffoldWriteError`)
- Create: `src/graftpunk/devtools/scaffold/write.py`
- Modify: `src/graftpunk/cli/scaffold_commands.py` (`plugin_new` reports a `ScaffoldWriteError` as one line, in a new arm above its `except OSError as exc:` arm)
- Modify: `src/graftpunk/devtools/scaffold/pyproject_edit.py:28-133` (pure `with_entry_point` and `with_wheel_package` replace `add_entry_point`, `add_wheel_package`, and `_read_normalised`)
- Modify: `src/graftpunk/devtools/scaffold/project.py:10-219` (`_missing_parents`, `_undo_writes`, `ScaffoldConflictError` class removed; `write_scaffold` plans and applies, the `.gitignore` edit included)
- Create: `src/graftpunk/devtools/captures_rule.py` (`CAPTURES_DIR`, moved from `captures.py:17`, and the new pure `with_ignored`)
- Modify: `src/graftpunk/devtools/captures.py:1-19` (docstring, `CAPTURES_DIR` removed, `__all__`), `:63-85` (`ensure_ignored` applies `with_ignored`)
- Modify: the importers of `CAPTURES_DIR`, which take it from `captures_rule`: `src/graftpunk/cli/scaffold_commands.py:17`, `src/graftpunk/cli/observe_commands.py` (the captures import Task 7 wrote), `src/graftpunk/devtools/scaffold/render.py:20`, and `tests/unit/test_devtools_captures.py:8`
- Modify: `tests/unit/test_scaffold_project.py:184-300` (fault injection moves to `write._write_atomically`)
- Modify: `tests/unit/test_scaffold_pyproject_edit.py` (the tests call the pure functions)
- Test: `tests/unit/test_scaffold_write.py`, `tests/unit/test_devtools_captures.py`, `tests/unit/test_devtools_errors.py`, `tests/unit/test_scaffold_cli.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `graftpunk.devtools.errors`: `class DevtoolsRefusal(Exception)`, the base of every devtools refusal a CLI entry point reports (a refusal means the operation left the disk as it found it, except for any path a `ScaffoldWriteError` names as left changed, and the message says why); `class ScaffoldWriteError(DevtoolsRefusal, OSError)` with `.path: Path`, `.error: OSError`, and `.unrestored: tuple[Path, ...]`, raised by `apply_changes` after it tried to restore every change it had applied, with a one-line message naming the path and the OS error, and then either "Every file this operation had changed was restored." when `unrestored` is empty or the paths left changed. Its `OSError` base carries the wrapped error's `errno`, `strerror`, and `filename`, so a caller that caught the writer's `OSError` keeps working and reads the same fields. `write._restore` returns the paths it could not put back. `graftpunk.devtools.scaffold.write`: `Validator = Callable[[str], None]`; `validate_python(text: str) -> None`; `validate_toml(text: str) -> None` (both raise `ValueError`); `@dataclass(frozen=True) class PlannedChange(path: Path, content: str, original: str | None = None, validate: Validator | None = None)`; `class ChangeConflictError(DevtoolsRefusal)` with `.conflicts: list[Path]`; `class InvalidChangeError(DevtoolsRefusal, ValueError)` with `.path`, `.reason`; `find_conflicts(changes: Sequence[PlannedChange]) -> list[Path]`; `apply_changes(changes: Sequence[PlannedChange]) -> tuple[Path, ...]`, which raises `ChangeConflictError`, `InvalidChangeError`, or `ScaffoldWriteError` and no bare `OSError`. `gp plugin new` reports a `ScaffoldWriteError` as one red line and exit 1. The project-tools plan consumes `DevtoolsRefusal` and `ScaffoldWriteError` from `graftpunk.devtools.errors`, and its writing entry points (`add-command`, `upgrade`) catch `DevtoolsRefusal` alone. `project.ScaffoldConflictError` is `write.ChangeConflictError`. `pyproject_edit.with_entry_point(text: str, pyproject_path: Path, name: str, target: str) -> str`; `pyproject_edit.with_wheel_package(text: str, pyproject_path: Path, package: str) -> str`, which returns *text* itself, byte for byte, in its two no-op branches. `pyproject_edit` no longer writes files and has no file-level wrappers: `write_scaffold` is the one production caller and applies the result through `write.py`. `graftpunk.devtools.captures_rule`, the pure side of the captures rule, importing nothing but `__future__`: `CAPTURES_DIR` (moved from `captures.py`, not re-exported there) and `with_ignored(text: str, relative: str) -> str`, the text edit `ensure_ignored` makes, so `write_scaffold` plans the `.gitignore` edit as the last `PlannedChange` of its batch. `graftpunk.devtools.captures` keeps the writing side (`ensure_ignored`, with its signature, for `gp observe fixtures`, which is not a scaffold writer; `write_sidecar`; and the git queries), and no scaffold module imports it. Every scaffold write, the `.gitignore` edit included, goes through `apply_changes`, so "the one way scaffold writes" has no exception. The project-tools plan's `insert.py` and `upgrade.py` write only through `apply_changes`, and `tests/unit/test_scaffold_write.py` finds them without a list (it scans every module in the package).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_scaffold_write.py`:

```python
"""The one write discipline for every scaffold mutator (graft skill spec, 2026-09-21,
"One write discipline for every mutator")."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import graftpunk.devtools.scaffold as scaffold_package
from graftpunk.devtools.errors import ScaffoldWriteError
from graftpunk.devtools.scaffold import write
from graftpunk.devtools.scaffold.write import (
    ChangeConflictError,
    InvalidChangeError,
    PlannedChange,
    apply_changes,
    validate_python,
    validate_toml,
)

_SCAFFOLD_DIR = Path(scaffold_package.__file__).parent


def _files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class TestRefusesBeforeTouching:
    def test_a_create_over_an_existing_file_writes_nothing(self, tmp_path: Path) -> None:
        (tmp_path / "taken.py").write_text("x = 1\n")
        before = _files(tmp_path)
        with pytest.raises(ChangeConflictError) as caught:
            apply_changes(
                [
                    PlannedChange(tmp_path / "new" / "a.py", "a = 1\n", validate=validate_python),
                    PlannedChange(tmp_path / "taken.py", "x = 2\n", validate=validate_python),
                ]
            )
        assert caught.value.conflicts == [tmp_path / "taken.py"]
        assert _files(tmp_path) == before
        assert not (tmp_path / "new").exists()

    def test_an_edit_whose_file_changed_since_it_was_planned_is_refused(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "plugin.py"
        target.write_text("x = 2\n")
        with pytest.raises(ChangeConflictError):
            apply_changes([PlannedChange(target, "x = 3\n", original="x = 1\n")])
        assert target.read_text() == "x = 2\n"

    def test_a_module_that_does_not_parse_is_refused_and_nothing_is_written(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(InvalidChangeError, match="does not parse"):
            apply_changes(
                [
                    PlannedChange(tmp_path / "ok.py", "ok = 1\n", validate=validate_python),
                    PlannedChange(tmp_path / "bad.py", "def (:\n", validate=validate_python),
                ]
            )
        assert _files(tmp_path) == {}

    def test_a_pyproject_that_does_not_load_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidChangeError, match="does not load"):
            apply_changes(
                [PlannedChange(tmp_path / "pyproject.toml", "[project\n", validate=validate_toml)]
            )
        assert _files(tmp_path) == {}


class TestTheValidatorIsTheChanges:
    def test_the_writer_runs_the_carried_validator_whatever_the_suffix(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(InvalidChangeError):
            apply_changes(
                [PlannedChange(tmp_path / "notes.txt", "def (:\n", validate=validate_python)]
            )

    def test_a_change_with_no_validator_is_written_as_given(self, tmp_path: Path) -> None:
        apply_changes([PlannedChange(tmp_path / "raw.py", "def (:\n")])
        assert (tmp_path / "raw.py").read_text() == "def (:\n"


class TestRestoresOnFailure:
    def test_an_edit_is_restored_and_a_create_removed_when_a_later_write_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        edited = tmp_path / "pyproject.toml"
        edited.write_text('[project]\nname = "x"\n')
        real = write._write_atomically

        def failing(path: Path, text: str) -> None:
            if path.name == "plugin.py":
                raise OSError(28, "No space left on device", str(path))
            real(path, text)

        monkeypatch.setattr(write, "_write_atomically", failing)
        with pytest.raises(ScaffoldWriteError) as caught:
            apply_changes(
                [
                    PlannedChange(
                        edited,
                        '[project]\nname = "y"\n',
                        original='[project]\nname = "x"\n',
                        validate=validate_toml,
                    ),
                    PlannedChange(tmp_path / "src" / "a.py", "a = 1\n"),
                    PlannedChange(tmp_path / "src" / "plugin.py", "p = 1\n"),
                ]
            )
        assert _files(tmp_path) == {"pyproject.toml": b'[project]\nname = "x"\n'}
        assert not (tmp_path / "src").exists()
        message = str(caught.value)
        assert message.startswith(f"Could not write {tmp_path / 'src' / 'plugin.py'}: ")
        assert "No space left on device" in message
        assert "\n" not in message
        assert message.endswith("Every file this operation had changed was restored.")
        assert caught.value.path == tmp_path / "src" / "plugin.py"
        assert caught.value.unrestored == ()
        assert caught.value.error.errno == 28
        assert caught.value.errno == 28

    def test_a_path_the_restore_cannot_put_back_is_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The message promises a full restore only when there was one."""
        created = tmp_path / "src" / "a.py"
        real_write = write._write_atomically
        real_unlink = Path.unlink

        def failing_write(path: Path, text: str) -> None:
            if path.name == "plugin.py":
                raise OSError(28, "No space left on device", str(path))
            real_write(path, text)

        def failing_unlink(self: Path, missing_ok: bool = False) -> None:
            if self == created:
                raise OSError(13, "Permission denied", str(self))
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(write, "_write_atomically", failing_write)
        monkeypatch.setattr(Path, "unlink", failing_unlink)
        with pytest.raises(ScaffoldWriteError) as caught:
            apply_changes(
                [
                    PlannedChange(created, "a = 1\n"),
                    PlannedChange(tmp_path / "src" / "plugin.py", "p = 1\n"),
                ]
            )
        assert created in caught.value.unrestored
        message = str(caught.value)
        assert str(created) in message
        assert "Every file this operation had changed was restored" not in message
        assert "\n" not in message

    def test_a_parent_that_is_a_file_restores_and_leaves_no_temp(self, tmp_path: Path) -> None:
        (tmp_path / "blocker").write_text("a file where a directory should be")
        with pytest.raises(ScaffoldWriteError):
            apply_changes(
                [
                    PlannedChange(tmp_path / "first.py", "a = 1\n"),
                    PlannedChange(tmp_path / "blocker" / "second.py", "b = 1\n"),
                ]
            )
        assert sorted(p.name for p in tmp_path.iterdir()) == ["blocker"]

    def test_no_partial_file_is_left_when_the_replace_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def failing_replace(src: object, dst: object) -> None:
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(write.os, "replace", failing_replace)
        with pytest.raises(ScaffoldWriteError):
            apply_changes([PlannedChange(tmp_path / "a.py", "a = 1\n")])
        assert list(tmp_path.iterdir()) == []


# Every module in the package but the writer itself, found rather than listed, so
# a mutator added later is covered without editing this test.
_NOT_THE_WRITER = sorted(p.name for p in _SCAFFOLD_DIR.glob("*.py") if p.name != "write.py")
_DISK_CALLS = {"write_text", "write_bytes", "touch", "unlink", "rmdir", "mkdir"}
_WRITE_MODE = re.compile(r"[wax+]")
# Modules whose import is itself a way to write: the filesystem calls of os and
# shutil, and the writing side of the captures rule, devtools/captures.py. The
# rule's pure side, devtools/captures_rule.py, is the one a scaffold module
# imports, so no list of writer names has to be kept in step with captures.py.
_WRITING_MODULES = {"os", "shutil"}
_CAPTURES_WRITING_SIDE = "graftpunk.devtools.captures"


def _tree(module: str) -> ast.Module:
    return ast.parse((_SCAFFOLD_DIR / module).read_text(encoding="utf-8"))


def _opens_for_writing(node: ast.Call) -> bool:
    """``open(path, mode)`` or ``path.open(mode)`` with a mode that writes. A mode
    that is not a string literal counts as writing."""
    if isinstance(node.func, ast.Name) and node.func.id == "open":
        mode_index = 1
    elif isinstance(node.func, ast.Attribute) and node.func.attr == "open":
        mode_index = 0
    else:
        return False
    modes = [k.value for k in node.keywords if k.arg == "mode"]
    if len(node.args) > mode_index:
        modes.append(node.args[mode_index])
    return any(
        not (isinstance(m, ast.Constant) and isinstance(m.value, str))
        or bool(_WRITE_MODE.search(m.value))
        for m in modes
    )


@pytest.mark.parametrize("module", _NOT_THE_WRITER)
def test_no_module_but_write_py_touches_the_disk(module: str) -> None:
    """No scaffold module but write.py calls a Path writer, opens a file to write,
    or imports os, shutil, or the writing side of the captures rule.

    What these checks cannot see: a writer reached through getattr or a string
    name, a Path method passed as a value and called elsewhere, ``open`` bound to
    another name, a subprocess that writes, and a call into any other package's
    function that writes. Review covers those."""
    writes_directly = [
        node.func.attr
        for node in ast.walk(_tree(module))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _DISK_CALLS
    ]
    assert writes_directly == []
    opened = [
        node.lineno
        for node in ast.walk(_tree(module))
        if isinstance(node, ast.Call) and _opens_for_writing(node)
    ]
    assert opened == []


def _writes(module: str) -> bool:
    return module.split(".")[0] in _WRITING_MODULES or module == _CAPTURES_WRITING_SIDE


@pytest.mark.parametrize("module", _NOT_THE_WRITER)
def test_no_module_but_write_py_imports_a_way_to_write(module: str) -> None:
    """A scaffold module takes the captures rule from captures_rule.py, whose names
    are pure, and nothing at all from captures.py."""
    found: list[str] = []
    for node in ast.walk(_tree(module)):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if _writes(a.name)]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if _writes(node.module):
                found.append(node.module)
            elif node.module == "graftpunk.devtools":
                found += [
                    f"graftpunk.devtools.{a.name}" for a in node.names if a.name == "captures"
                ]
    assert found == []


def test_every_module_that_applies_changes_takes_them_from_write_py() -> None:
    """The routing, on the import graph: a module that applies changes imports
    apply_changes from write.py, and write_scaffold's module is one of them."""
    appliers = []
    for module in _NOT_THE_WRITER:
        tree = _tree(module)
        calls = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "apply_changes"
            for node in ast.walk(tree)
        )
        if calls:
            appliers.append(module)
            assert any(
                isinstance(node, ast.ImportFrom)
                and node.module == "graftpunk.devtools.scaffold.write"
                and any(alias.name == "apply_changes" for alias in node.names)
                for node in ast.walk(tree)
            ), module
    assert "project.py" in appliers
```

Create `tests/unit/test_devtools_errors.py`:

```python
"""One base class for every devtools refusal a CLI entry point reports."""

from __future__ import annotations

from pathlib import Path

from graftpunk.devtools.errors import DevtoolsRefusal, ScaffoldWriteError
from graftpunk.devtools.scaffold.write import ChangeConflictError, InvalidChangeError


def test_the_refusals_share_one_base_and_keep_their_own() -> None:
    for error in (ChangeConflictError, InvalidChangeError, ScaffoldWriteError):
        assert issubclass(error, DevtoolsRefusal), error
    assert issubclass(InvalidChangeError, ValueError)
    assert issubclass(ScaffoldWriteError, OSError)


def test_a_write_error_keeps_the_os_errors_fields_and_its_own_message() -> None:
    """A caller that caught the writer's OSError before this class existed reads the
    same errno, strerror, and filename it did then."""
    error = OSError(28, "No space left on device", "src/plugin.py")
    raised = ScaffoldWriteError(Path("src/plugin.py"), error)
    assert (raised.errno, raised.strerror, raised.filename) == (
        28,
        "No space left on device",
        "src/plugin.py",
    )
    assert str(raised) == (
        "Could not write src/plugin.py: No space left on device. "
        "Every file this operation had changed was restored."
    )
```

The project-tools plan adds its own refusals to the tuple in the loop.

Append to `tests/unit/test_scaffold_cli.py`:

```python
class TestAWriteFailureIsOneRefusal:
    def test_a_failed_write_is_one_line_exit_1_and_the_original_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A suite member: the pyproject.toml edit is applied first, so the
        restore is what puts its original bytes back."""
        from graftpunk.devtools.scaffold import write

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "mysuite"\n\n'
            '[project.entry-points."graftpunk.plugins"]\n'
            'existing = "mysuite.existing:ExistingPlugin"\n'
        )
        before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
        real_write = write._write_atomically

        def write_failing_on_the_plugin_module(path: Path, text: str) -> None:
            if path.name == "plugin.py":
                raise OSError(28, "No space left on device", str(path))
            real_write(path, text)

        monkeypatch.setattr(write, "_write_atomically", write_failing_on_the_plugin_module)
        result = runner.invoke(
            _build_app(),
            ["plugin", "new", "widgets", "--url", "https://myshop.example", "--dir", str(tmp_path)],
        )
        assert result.exit_code == 1, result.output
        (line,) = _plain(result.output).strip().splitlines()
        assert line.startswith("Could not write ")
        assert "No space left on device" in line
        assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
```

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_write.py tests/unit/test_devtools_errors.py tests/unit/test_scaffold_cli.py::TestAWriteFailureIsOneRefusal -q`
Expected: FAIL (`ImportError: cannot import name 'write'`, and `ModuleNotFoundError: No module named 'graftpunk.devtools.errors'`).

The pyproject edit tests are rewritten in Step 4, beside the code they test.

- [ ] **Step 3: Write `errors.py` and `write.py`**

Create `src/graftpunk/devtools/errors.py`:

```python
"""The one base class of every refusal ``graftpunk.devtools`` reports to a person.

A refusal means the operation left the disk as it found it, except for any path
a :class:`ScaffoldWriteError` names as left changed, and the message says why.
Each refusal keeps its own class, and its own second base where it has one, for
callers that want it; a ``gp plugin`` entry point catches :class:`DevtoolsRefusal`
alone.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

__all__ = ["DevtoolsRefusal", "ScaffoldWriteError"]


class DevtoolsRefusal(Exception):
    """A devtools operation refused, and the message says why. The disk is as it was,
    except for any path a :class:`ScaffoldWriteError` names as left changed."""


class ScaffoldWriteError(DevtoolsRefusal, OSError):
    """A write failed partway through an operation. The writer then put back every
    change it had already applied that it could; ``unrestored`` lists the paths it
    could not, and the message names them. Also an ``OSError`` carrying the wrapped
    error's ``errno``, ``strerror``, and ``filename``, so a caller that caught the
    writer's ``OSError`` before this class existed still catches it and reads the
    same fields."""

    def __init__(self, path: Path, error: OSError, unrestored: Sequence[Path] = ()) -> None:
        # OSError's own constructor sets errno, strerror, and filename from these
        # three arguments; __str__ keeps the one-line refusal as the message.
        super().__init__(error.errno, error.strerror, error.filename)
        self.path = path
        self.error = error
        self.unrestored = tuple(unrestored)
        reason = error.strerror or str(error)
        if self.unrestored:
            listing = ", ".join(str(p) for p in self.unrestored)
            aftermath = f"These paths could not be restored and are left changed: {listing}."
        else:
            aftermath = "Every file this operation had changed was restored."
        self._message = f"Could not write {path}: {reason}. {aftermath}"

    def __str__(self) -> str:
        return self._message
```

Create `src/graftpunk/devtools/scaffold/write.py`:

```python
"""The one way ``graftpunk.devtools.scaffold`` writes to a developer's project.

A change is planned against the text on disk, refused with a reason if it
conflicts, rendered in full, validated before anything is written by the
validator the change carries, written atomically, and restored from the
original text if any later write of the same operation fails. The writer never
switches on file type: a Python module's change carries :func:`validate_python`,
a ``pyproject.toml``'s carries :func:`validate_toml`, and a file with no grammar
(a README, a ``.gitkeep``) carries none (graft skill spec, 2026-09-21).

Imported by the writers only (the scaffold, the stub inserter, the migrator);
the project reader and the lint never import it.
"""

from __future__ import annotations

import ast
import contextlib
import os
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from graftpunk.devtools.errors import DevtoolsRefusal, ScaffoldWriteError

__all__ = [
    "ChangeConflictError",
    "InvalidChangeError",
    "PlannedChange",
    "Validator",
    "apply_changes",
    "find_conflicts",
    "validate_python",
    "validate_toml",
]

Validator = Callable[[str], None]

_PARTIAL_SUFFIX = ".gp-partial"


def validate_python(text: str) -> None:
    """Raise ``ValueError`` unless *text* parses as a Python module."""
    try:
        ast.parse(text)
    except SyntaxError as exc:
        raise ValueError(f"does not parse as Python: {exc.msg} (line {exc.lineno})") from exc


def validate_toml(text: str) -> None:
    """Raise ``ValueError`` unless *text* loads as TOML."""
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"does not load as TOML: {exc}") from exc


@dataclass(frozen=True)
class PlannedChange:
    """One file's full new content, planned against what the file held.

    ``original`` is ``None`` for a create, which conflicts with any file already
    at ``path``; for an edit it is the text the plan was made from, and the
    change conflicts if the file no longer holds exactly that.
    """

    path: Path
    content: str
    original: str | None = None
    validate: Validator | None = None


class ChangeConflictError(DevtoolsRefusal):
    """One or more planned changes conflict with the disk; nothing was written."""

    def __init__(self, conflicts: list[Path]) -> None:
        self.conflicts = conflicts
        listing = ", ".join(str(p) for p in conflicts)
        super().__init__(f"Refusing to overwrite existing file(s): {listing}")


class InvalidChangeError(DevtoolsRefusal, ValueError):
    """A planned change's content fails its own validator; nothing was written."""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"Refusing to write {path}: the result {reason}")


def _conflicts(change: PlannedChange) -> bool:
    if change.original is None:
        return change.path.exists()
    try:
        return change.path.read_text(encoding="utf-8") != change.original
    except OSError:
        return True


def find_conflicts(changes: Sequence[PlannedChange]) -> list[Path]:
    """The paths among *changes* that conflict with the disk, sorted."""
    return sorted(change.path for change in changes if _conflicts(change))


def _missing_parents(directory: Path) -> list[Path]:
    """The ancestors of *directory*, *directory* included, that do not exist yet,
    deepest last: the order ``mkdir(parents=True)`` creates them."""
    missing: list[Path] = []
    current = directory
    while not current.exists() and current != current.parent:
        missing.append(current)
        current = current.parent
    return list(reversed(missing))


def _write_atomically(path: Path, text: str) -> None:
    """Write *text* to a sibling temp file and move it over *path*, so a reader never
    sees a half-written module. The temp file is removed if either step fails."""
    partial = path.with_name(f".{path.name}{_PARTIAL_SUFFIX}")
    try:
        partial.write_text(text, encoding="utf-8")
        os.replace(partial, path)
    except OSError:
        with contextlib.suppress(OSError):
            partial.unlink()
        raise


def _restore(started: list[PlannedChange], created_dirs: list[Path]) -> list[Path]:
    """Put back what this operation changed, best effort, and return every path it
    could not put back: each edited file's original text, each created file
    removed, then each directory this operation created, deepest first and only
    while empty. Every step is guarded, because unwinding runs in the conditions
    that caused the failure and the original error is the one the caller must
    see; a step that fails is reported in the return value instead."""
    unrestored: list[Path] = []
    for change in reversed(started):
        try:
            if change.original is None:
                change.path.unlink(missing_ok=True)
            else:
                change.path.write_text(change.original, encoding="utf-8")
        except OSError:
            unrestored.append(change.path)
    for directory in reversed(created_dirs):
        try:
            directory.rmdir()
        except OSError:
            if directory.exists():
                unrestored.append(directory)
    return unrestored


def apply_changes(changes: Sequence[PlannedChange]) -> tuple[Path, ...]:
    """Apply *changes* in order, all or nothing.

    Raises:
        ChangeConflictError: A create's path exists or an edit's file changed
            since it was planned. Raised before anything is touched.
        InvalidChangeError: A change's content fails its validator. Raised
            before anything is touched.
        ScaffoldWriteError: A write failed. Every change already applied is
            undone first where it can be; the error names the path, carries the
            ``OSError``, and lists in ``unrestored`` anything left changed.
    """
    conflicts = find_conflicts(changes)
    if conflicts:
        raise ChangeConflictError(conflicts)
    for change in changes:
        if change.validate is not None:
            try:
                change.validate(change.content)
            except ValueError as exc:
                raise InvalidChangeError(change.path, str(exc)) from exc
    started: list[PlannedChange] = []
    created_dirs: list[Path] = []
    for change in changes:
        try:
            created_dirs.extend(_missing_parents(change.path.parent))
            change.path.parent.mkdir(parents=True, exist_ok=True)
            started.append(change)
            _write_atomically(change.path, change.content)
        except OSError as exc:
            unrestored = _restore(started, created_dirs)
            raise ScaffoldWriteError(change.path, exc, unrestored) from exc
    return tuple(change.path for change in changes)
```

- [ ] **Step 4: Make the pyproject edits pure text functions**

In `src/graftpunk/devtools/scaffold/pyproject_edit.py`, set `__all__ = ["PyprojectEditError", "with_entry_point", "with_wheel_package"]`, change the module docstring's first line to "The one seam that edits an existing pyproject.toml's text: two known shapes only, and no file access.", add to its body "The caller writes the result; ``write_scaffold`` does, through ``write.py``.", and replace `_read_normalised`, `add_entry_point`, and `add_wheel_package` with:

```python
def _normalised(text: str) -> str:
    """*text* ending in a newline. The entry-point table's own pattern needs its last
    line terminated, so a file whose table is last and that ends without a newline
    was refused as an unknown shape (polish round 1, 2026-09-12)."""
    return text if text.endswith("\n") or not text else text + "\n"


def with_entry_point(text: str, pyproject_path: Path, name: str, target: str) -> str:
    """*text* with ``name = "target"`` appended to its
    ``[project.entry-points."graftpunk.plugins"]`` table. *pyproject_path* names the
    file in a refusal and is never opened.

    Raises:
        PyprojectEditError: The name is already registered, or the table's
            shape cannot be located textually.
    """
    text = _normalised(text)
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
    new_text = text[: match.start()] + header + _body_with_entry(body, name, target)
    return new_text + text[match.end() :]


def with_wheel_package(text: str, pyproject_path: Path, package: str) -> str:
    """*text* with *package* appended to ``[tool.hatch.build.targets.wheel]``'s
    ``packages`` array. *pyproject_path* names the file in a refusal and is never
    opened.

    Returns *text* itself, byte for byte, when there is nothing to add: the table
    has no explicit ``packages`` key (hatchling then infers packages on its own),
    or *package* is already listed. Only an edit normalises the trailing newline.

    Raises:
        PyprojectEditError: The wheel table uses ``include`` instead of
            ``packages``, or ``packages`` exists but its array cannot be
            located textually.
    """
    data = tomllib.loads(text)
    wheel = (
        data.get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {})
    )
    if "packages" not in wheel:
        if "include" in wheel:
            raise PyprojectEditError(
                f"{pyproject_path} uses [tool.hatch.build.targets.wheel].include instead of "
                f'packages. Add "{package}" to it by hand.'
            )
        return text
    if package in wheel["packages"]:
        return text
    text = _normalised(text)
    match = _WHEEL_PACKAGES_RE.search(text)
    if match is None:
        raise PyprojectEditError(
            f"{pyproject_path} has [tool.hatch.build.targets.wheel].packages but I cannot "
            f'locate its array textually. Add "{package}" to it by hand.'
        )
    prefix, array = match.group(1), match.group(2)
    return text[: match.start()] + prefix + _append_to_array(array, package) + text[match.end() :]
```

`_body_with_entry` and `_append_to_array` stay as they are. No wrapper that reads or writes the file is kept: after Step 5, `write_scaffold` is the only production caller, and it applies the result through `write.py`.

In `tests/unit/test_scaffold_pyproject_edit.py`, keep the module-level text constants (`_SINGLE_LINE_ARRAY` through `_ENTRY_POINT_TABLE_LAST_NO_TRAILING_NEWLINE`) as they are, and replace the module docstring, the imports, and the two test classes with:

```python
"""The one seam that edits an existing pyproject.toml's text (plugin tooling spec,
2026-09-11). The functions are pure; the file on disk is write_scaffold's, and
tests/unit/test_scaffold_project.py covers it (TestAddToSuiteMode,
TestPyprojectEditFailureLeavesSuiteUntouched)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from graftpunk.devtools.scaffold.pyproject_edit import (
    PyprojectEditError,
    with_entry_point,
    with_wheel_package,
)

_PATH = Path("pyproject.toml")
_WIDGETS = "graftpunk_widgets.plugin:WidgetsPlugin"
_GADGETS = "graftpunk_gadgets.plugin:GadgetsPlugin"


def _entry_points(text: str) -> dict[str, str]:
    return tomllib.loads(text)["project"]["entry-points"]["graftpunk.plugins"]


def _wheel(text: str) -> dict[str, object]:
    return tomllib.loads(text)["tool"]["hatch"]["build"]["targets"]["wheel"]


class TestWithEntryPoint:
    def test_appends_a_new_line_to_the_table(self) -> None:
        text = with_entry_point(_SINGLE_LINE_ARRAY, _PATH, "widgets", _WIDGETS)
        assert f'widgets = "{_WIDGETS}"' in text
        assert 'existing = "mysuite.existing:ExistingPlugin"' in text

    def test_result_is_valid_toml(self) -> None:
        eps = _entry_points(with_entry_point(_SINGLE_LINE_ARRAY, _PATH, "widgets", _WIDGETS))
        assert eps["widgets"] == _WIDGETS
        assert eps["existing"] == "mysuite.existing:ExistingPlugin"

    def test_duplicate_name_refuses(self) -> None:
        with pytest.raises(PyprojectEditError, match="already registered"):
            with_entry_point(_SINGLE_LINE_ARRAY, _PATH, "existing", _WIDGETS)

    def test_missing_table_refuses_with_instructions(self) -> None:
        with pytest.raises(PyprojectEditError, match="entry-points"):
            with_entry_point(_NO_ENTRY_POINT_TABLE, _PATH, "widgets", _WIDGETS)

    def test_appends_to_an_empty_table(self) -> None:
        text = with_entry_point(_EMPTY_ENTRY_POINT_TABLE, _PATH, "widgets", _WIDGETS)
        assert f'widgets = "{_WIDGETS}"' in text
        assert _entry_points(text)["widgets"] == _WIDGETS

    def test_the_blank_line_before_the_next_table_survives(self) -> None:
        lines = with_entry_point(_SINGLE_LINE_ARRAY, _PATH, "widgets", _WIDGETS).splitlines()
        header_index = lines.index("[tool.hatch.build.targets.wheel]")
        assert lines[header_index - 1] == ""
        assert lines[header_index - 2] == f'widgets = "{_WIDGETS}"'

    def test_two_adds_leave_both_entries_in_the_same_table(self) -> None:
        once = with_entry_point(_SINGLE_LINE_ARRAY, _PATH, "widgets", _WIDGETS)
        text = with_entry_point(once, _PATH, "gadgets", _GADGETS)
        assert _entry_points(text) == {
            "existing": "mysuite.existing:ExistingPlugin",
            "widgets": _WIDGETS,
            "gadgets": _GADGETS,
        }
        lines = text.splitlines()
        assert lines[lines.index("[tool.hatch.build.targets.wheel]") - 1] == ""
        assert _wheel(text)["packages"] == ["src/mysuite"]

    def test_an_empty_table_keeps_its_separator_too(self) -> None:
        lines = with_entry_point(_EMPTY_ENTRY_POINT_TABLE, _PATH, "widgets", _WIDGETS).splitlines()
        header_index = lines.index("[tool.hatch.build.targets.wheel]")
        assert lines[header_index - 1] == ""
        assert lines[header_index - 2] == f'widgets = "{_WIDGETS}"'

    def test_a_table_that_is_last_and_has_no_trailing_newline(self) -> None:
        """The table's pattern needs its last line terminated, so this shape was
        refused as unlocatable."""
        text = with_entry_point(
            _ENTRY_POINT_TABLE_LAST_NO_TRAILING_NEWLINE, _PATH, "widgets", _WIDGETS
        )
        assert text.endswith("\n")
        assert _entry_points(text) == {
            "existing": "mysuite.existing:ExistingPlugin",
            "widgets": _WIDGETS,
        }

    def test_the_refusal_names_the_path_it_was_given(self) -> None:
        with pytest.raises(PyprojectEditError, match="custom/pyproject.toml"):
            with_entry_point(_NO_ENTRY_POINT_TABLE, Path("custom/pyproject.toml"), "w", _WIDGETS)


class TestWithWheelPackage:
    def test_an_array_before_packages_is_still_located(self) -> None:
        """The span between the header and the packages key stopped at any "[",
        so an earlier array's own bracket made a known shape unlocatable."""
        wheel = _wheel(with_wheel_package(_ARRAY_BEFORE_PACKAGES, _PATH, "src/graftpunk_widgets"))
        assert set(wheel["packages"]) == {"src/mysuite", "src/graftpunk_widgets"}
        assert wheel["exclude"] == ["docs"]

    def test_appends_to_a_single_line_array(self) -> None:
        text = with_wheel_package(_SINGLE_LINE_ARRAY, _PATH, "src/graftpunk_widgets")
        assert '"src/graftpunk_widgets"' in text

    def test_appends_to_a_multi_line_array(self) -> None:
        text = with_wheel_package(_MULTI_LINE_ARRAY, _PATH, "src/graftpunk_widgets")
        assert '"src/graftpunk_widgets"' in text
        assert '"src/mysuite"' in text

    def test_result_is_valid_toml_with_both_packages(self) -> None:
        text = with_wheel_package(_MULTI_LINE_ARRAY, _PATH, "src/graftpunk_widgets")
        assert set(_wheel(text)["packages"]) == {"src/mysuite", "src/graftpunk_widgets"}

    @pytest.mark.parametrize(
        ("text", "package"),
        [
            (_SINGLE_LINE_ARRAY, "src/mysuite"),
            (_SINGLE_LINE_ARRAY.rstrip("\n"), "src/mysuite"),
            (_NO_PACKAGES_KEY_AT_ALL, "src/graftpunk_widgets"),
            (_NO_PACKAGES_KEY_AT_ALL.rstrip("\n"), "src/graftpunk_widgets"),
        ],
    )
    def test_a_no_op_returns_the_input_byte_for_byte(self, text: str, package: str) -> None:
        """Nothing is rewritten when nothing changes, not even a missing final newline."""
        assert with_wheel_package(text, _PATH, package) == text

    def test_include_instead_of_packages_refuses(self) -> None:
        with pytest.raises(PyprojectEditError, match="include"):
            with_wheel_package(_INCLUDE_INSTEAD_OF_PACKAGES, _PATH, "src/graftpunk_widgets")
```

Split the captures rule into its pure side and its writing side, so a scaffold module can import the rule without importing a writer. Create `src/graftpunk/devtools/captures_rule.py`:

```python
"""The single owner of the "captures never enter git" rule, as text.

The directory captures go to, and the ``.gitignore`` edit that keeps them out
of git. Nothing here touches a file, so the scaffold modules, which write only
through ``write.py``, import the rule from here. The fixtures command, the
scaffold's generated ``.gitignore``, and the scaffold's suite mode all use it,
so the default directory and the ignore line cannot disagree (plugin tooling
spec, 2026-09-11). ``graftpunk.devtools.captures`` applies the edit to disk for
``gp observe fixtures`` (graft skill spec, 2026-09-21).
"""

from __future__ import annotations

__all__ = ["CAPTURES_DIR", "with_ignored"]

CAPTURES_DIR = "tests/captures"


def with_ignored(text: str, relative: str) -> str:
    """*text*, a ``.gitignore``'s content, with ``<relative>/`` appended unless that
    exact line is there, in which case *text* itself. The rule ``ensure_ignored``
    applies, as a text function a scaffold writer can plan as a change."""
    if relative.rstrip("/") in {entry.strip().rstrip("/") for entry in text.splitlines()}:
        return text
    separator = "\n" if text and not text.endswith("\n") else ""
    return f"{text}{separator}{relative.rstrip('/')}/\n"
```

In `src/graftpunk/devtools/captures.py`: delete `CAPTURES_DIR = "tests/captures"` and its `__all__` entry (it is not re-exported), add `from graftpunk.devtools.captures_rule import with_ignored`, and replace the module docstring's first paragraph (Task 7's sentence about the sidecar included) with: "The writing side of the \"captures never enter git\" rule: the ``.gitignore`` edit applied to disk, the git queries, and the committable sidecar written beside each capture through the format :mod:`graftpunk.testing.sidecar` owns. The rule itself, the directory and the text edit, lives in :mod:`graftpunk.devtools.captures_rule`, which touches no file." Then replace the body of `ensure_ignored` after its docstring with:

```python
    gitignore = repo_root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    updated = with_ignored(existing, relative)
    if updated == existing:
        return False
    with gitignore.open("a", encoding="utf-8") as handle:
        handle.write(updated[len(existing) :])
    LOG.info("captures_gitignore_updated", path=str(gitignore), line=relative.rstrip("/") + "/")
    return True
```

Every other importer takes `CAPTURES_DIR` from the pure side: in `src/graftpunk/cli/scaffold_commands.py` and `src/graftpunk/devtools/scaffold/render.py`, `from graftpunk.devtools.captures import CAPTURES_DIR` becomes `from graftpunk.devtools.captures_rule import CAPTURES_DIR`; in `src/graftpunk/cli/observe_commands.py`, drop `CAPTURES_DIR` from the captures import Task 7 wrote and add `from graftpunk.devtools.captures_rule import CAPTURES_DIR`. Run `uvx ruff check --fix` and `uvx ruff format` over the edited files to sort the imports.

In `tests/unit/test_devtools_captures.py`, add `import ast` and `import graftpunk.devtools.captures_rule as captures_rule`, move `CAPTURES_DIR` out of the captures import into `from graftpunk.devtools.captures_rule import CAPTURES_DIR, with_ignored`, and append:

```python
class TestWithIgnored:
    def test_the_line_is_added_once(self) -> None:
        assert with_ignored("", "tests/captures") == "tests/captures/\n"
        assert with_ignored("dist/", "tests/captures") == "dist/\ntests/captures/\n"
        assert with_ignored("tests/captures\n", "tests/captures/") == "tests/captures\n"


def test_the_rule_module_imports_nothing_that_touches_the_filesystem() -> None:
    """Scaffold modules import the rule from here, so it stays pure text."""
    assert captures_rule.__file__ is not None
    tree = ast.parse(Path(captures_rule.__file__).read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert imported <= {"__future__"}
```

The existing `ensure_ignored` tests in that file are the check that its behaviour did not change.

- [ ] **Step 5: Route `write_scaffold` through `write.py`**

In `src/graftpunk/devtools/scaffold/project.py`: remove `import contextlib`, `_missing_parents`, `_undo_writes`, and the `ScaffoldConflictError` class; change the captures import to `from graftpunk.devtools.captures_rule import CAPTURES_DIR, with_ignored` (drop `ensure_ignored`: `project.py` imports nothing from `captures.py`); import `PyprojectEditError, with_entry_point, with_wheel_package` from `pyproject_edit` (drop `add_entry_point`, `add_wheel_package`), `ChangeConflictError, PlannedChange, Validator, apply_changes, find_conflicts, validate_python, validate_toml` from `write`, and `ScaffoldWriteError` from `graftpunk.devtools.errors`; add after `PLUGINS_ENTRY_POINT_GROUP`:

```python
ScaffoldConflictError = ChangeConflictError
"""A target path already exists; nothing was written. The writer's own error, under
the name the CLI and callers of ``write_scaffold`` already catch."""


def _validator_for(relative: str) -> Validator | None:
    """The grammar a rendered file must satisfy before it is written."""
    if relative.endswith(".py"):
        return validate_python
    if relative.endswith(".toml"):
        return validate_toml
    return None
```

and replace everything in `write_scaffold` from `# Conflict detection runs before anything else is touched` through the `except OSError:` block with:

```python
    rendered = [
        PlannedChange(target_dir / rel, content, validate=_validator_for(rel))
        for rel, content in files.items()
    ]
    # Deliberately before apply_changes runs its own check: a refusal here never
    # computes the pyproject.toml edit.
    conflicts = find_conflicts(rendered)
    if conflicts:
        LOG.debug("scaffold_write_refused", conflicts=len(conflicts))
        raise ScaffoldConflictError(conflicts)

    changes: list[PlannedChange] = []
    pyproject_updated = False
    if mode == "add_to_suite":
        assert existing is not None  # narrows for the type checker; mode implies it
        module = module_name_for(resolved_spec.name)
        package = f"src/graftpunk_{module}"
        target = f"graftpunk_{module}.plugin:{class_name_for(resolved_spec.name)}"
        original = existing.read_text(encoding="utf-8")
        try:
            edited = with_wheel_package(
                with_entry_point(original, existing, resolved_spec.name, target), existing, package
            )
        except PyprojectEditError:
            LOG.debug("scaffold_write_refused", reason="pyproject_edit_error")
            raise
        # The pyproject.toml edit is applied first and restored with everything
        # else if any rendered file then fails to write.
        changes.append(PlannedChange(existing, edited, original=original, validate=validate_toml))
        pyproject_updated = True
    changes.extend(rendered)

    gitignore_updated = False
    if mode == "add_to_suite":
        # Last in the batch: the ignore line protects files this call writes, so
        # a failed write restores it with everything else (polish round 1,
        # 2026-09-12).
        gitignore = target_dir / ".gitignore"
        before = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else None
        after = with_ignored(before or "", CAPTURES_DIR)
        if after != (before or ""):
            changes.append(PlannedChange(gitignore, after, original=before))
            gitignore_updated = True

    try:
        apply_changes(changes)
    except ScaffoldWriteError:
        LOG.debug("scaffold_write_refused", reason="os_error")
        raise
```

Delete the old `.gitignore` block after the write (its comment and the `ensure_ignored` call): the edit is now the batch's last planned change. `targets` no longer exists, so change the two lines after it that read it: in `LOG.info("scaffold_written", ...)`, `files=len(targets)` becomes `files=len(rendered)`, and in `return ScaffoldResult(...)`, `written=tuple(sorted(targets))` becomes `written=tuple(sorted(c.path for c in rendered))`. Update the docstring's `PyprojectEditError` paragraph to say `pyproject.toml` is never written when the edit cannot be computed, and add a `ScaffoldWriteError` paragraph: "a write failed; every change already applied, the `pyproject.toml` edit included, is undone first where it can be, and the error names the file, the OS error, and any path left changed (see `write.py`). A bare `OSError` now means a read before anything was written failed."

In `src/graftpunk/cli/scaffold_commands.py`, add `from graftpunk.devtools.errors import ScaffoldWriteError`, add this arm directly above `except OSError as exc:` in `plugin_new` (a `ScaffoldWriteError` is also an `OSError`, so the order matters):

```python
    except ScaffoldWriteError as exc:
        # The writer restored what it could before raising, and its message is
        # the whole refusal on one line: the path, the OS error, and any path
        # it could not put back.
        LOG.debug("scaffold_refused", reason="os_error", error=str(exc.error))
        console.print(f"[red]{escape(str(exc))}[/red]", soft_wrap=True)
        raise typer.Exit(1) from None
```

and replace the `except OSError as exc:` arm's three comment lines (from `# A CLI refusal is a red line and exit 1` through `# write_scaffold has already removed whatever it wrote before failing.`) with:

```python
        # A read before anything was written failed (the suite's pyproject.toml
        # or .gitignore): a red line and exit 1, never a Rich traceback. A failed
        # write is the ScaffoldWriteError arm above.
```

`TestUnwritableTargetDirIsARefusal` in `tests/unit/test_scaffold_cli.py` now reaches the new arm (the unwritable `--dir` fails in `apply_changes`); its assertions (`could not write`, no traceback, nothing left in the directory) hold unchanged.

- [ ] **Step 6: Move the project tests' fault injection to the writer**

In `tests/unit/test_scaffold_project.py`, add `from graftpunk.devtools.scaffold import write`, and in each of the four tests of `TestAWriteFailureLeavesNoPartialTree` and `TestPyprojectRestoredAfterRenderedFileFailure`, replace the `real_write_text = Path.write_text` line, the `failing_name = "plugin.py"` line where the test has one (three of the four do; ruff reports it as F841 once the fault function below stops reading it), the old fault function, and its `monkeypatch.setattr(Path, "write_text", ...)` with:

```python
        real_write = write._write_atomically

        def write_failing_on_the_plugin_module(path: Path, text: str) -> None:
            if path.name == "plugin.py":
                raise OSError(28, "No space left on device", str(path))
            real_write(path, text)

        monkeypatch.setattr(write, "_write_atomically", write_failing_on_the_plugin_module)
```

For `test_a_short_write_that_touches_the_file_is_still_cleaned_up`, use this fault function instead, which leaves the target on disk before failing:

```python
        real_write = write._write_atomically

        def write_touching_then_failing(path: Path, text: str) -> None:
            if path.name == "plugin.py":
                path.touch()
                raise OSError(28, "No space left on device", str(path))
            real_write(path, text)

        monkeypatch.setattr(write, "_write_atomically", write_touching_then_failing)
```

Every assertion in those tests stays as it is: `pytest.raises(OSError, match="No space left on device")` still matches, because `ScaffoldWriteError` is an `OSError` whose message carries the OS error. The docstring of `test_files_written_before_the_failure_are_removed` (`tests/unit/test_scaffold_project.py:188-195`) still says the fault is injected at `Path.write_text`. Keep its first two sentences (the second says ``plugin.py`` is the third file the renderer emits, which is still true and is why two files are on disk when it fails), and replace only its last sentence, the one that begins "Fault injection at ``Path.write_text``", with: "Fault injection at ``write._write_atomically``, the one place a planned change reaches the disk, is how one file fails and the rest do not; the assertions are all on the tree the call leaves on disk."

- [ ] **Step 7: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_write.py tests/unit/test_devtools_errors.py tests/unit/test_scaffold_project.py tests/unit/test_scaffold_pyproject_edit.py tests/unit/test_devtools_captures.py tests/unit/test_scaffold_cli.py tests/unit/test_observe_commands.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/graftpunk/devtools/errors.py src/graftpunk/devtools/scaffold/write.py src/graftpunk/devtools/scaffold/pyproject_edit.py src/graftpunk/devtools/scaffold/project.py src/graftpunk/devtools/scaffold/render.py src/graftpunk/devtools/captures.py src/graftpunk/devtools/captures_rule.py src/graftpunk/cli/scaffold_commands.py src/graftpunk/cli/observe_commands.py tests/unit/test_scaffold_write.py tests/unit/test_devtools_errors.py tests/unit/test_scaffold_project.py tests/unit/test_scaffold_pyproject_edit.py tests/unit/test_devtools_captures.py tests/unit/test_scaffold_cli.py
git commit -m "feat(scaffold): write.py is the one write discipline, and write_scaffold goes through it"
```

---

### Task 11: `@command(endpoint=...)`, and the declared endpoint and typed parameters on every stub

**Files:**
- Modify: `src/graftpunk/plugins/cli_plugin.py:215-234` (`CommandMetadata`), `:788-877` (`command`)
- Modify: `src/graftpunk/devtools/scaffold/pysrc.py` (`import_lines` takes several names)
- Modify: `src/graftpunk/devtools/scaffold/render.py` (`_render_command_stub`, `_plugins_import_names`, `_render_plugin_module`; new `_SPEC_TYPE_BY_OBSERVED`, `_emits_body`, `_declared_extras`, `_needs_param_specs`, `_decorator_lines`, `_ENDPOINT_COMMENT`)
- Modify: `docs/PLUGIN_DEVELOPMENT.md:395-485` ("What gets filled in": from its heading through the generated `plugin.py` example, the paragraph that begins "What came from the digest", and the paragraph that begins "Everything the digest could not decide", after which the new paragraph goes), `:569-601` ("CLI parameter types")
- Test: `tests/unit/test_cli_plugin.py`, `tests/unit/test_scaffold_render.py`

**Interfaces:**
- Consumes: `literal_lines`, `quoted_literal`, `L1`, `L2`, `L3`, `import_lines` from `pysrc` (Task 1).
- Produces: `CommandMetadata.endpoint: str | None = None`; `command(..., endpoint: str | None = None)`; `pysrc.import_lines(module: str, *names: str) -> list[str]`; every generated stub's decorator carries `endpoint="<METHOD> <template>"` on its own line, and a stub with an `int` or `bool` parameter carries `params=[PluginParamSpec.option(...), ...]` for every parameter. The project-tools plan's reader reads the `endpoint` keyword from source, and its `render_command` reuses `_declared_extras` and `_needs_param_specs` as they are and `_decorator_lines` with a changed signature: that plan's Task 5 makes it `_decorator_lines(command: PlannedCommand, param_specs: list[str]) -> list[str]`, so the help placeholder, the name pin, and the endpoint all come from the planned command.

- [ ] **Step 1: Write the failing decorator tests**

Append to `tests/unit/test_cli_plugin.py`:

```python
class TestCommandEndpoint:
    """@command(endpoint=...) is tooling provenance: stored, never consulted at
    runtime, never shown in help (graft skill spec, 2026-09-21)."""

    def test_the_endpoint_is_stored_on_the_metadata(self) -> None:
        from graftpunk.plugins import command

        @command(help="List orders", endpoint="GET /api/orders")
        def orders(self: object, ctx: object) -> dict:
            return {}

        assert orders._command_meta.endpoint == "GET /api/orders"

    def test_a_command_without_one_stores_none(self) -> None:
        from graftpunk.plugins import command

        @command(help="List orders")
        def orders(self: object, ctx: object) -> dict:
            return {}

        assert orders._command_meta.endpoint is None

    def test_the_endpoint_is_absent_from_help(self) -> None:
        from graftpunk.plugins import CommandContext, SitePlugin, command
        from tests.unit.cli_harness import invoke_plugin_app

        class _EndpointPlugin(SitePlugin):
            site_name = "myshop"
            session_name = "myshop"
            base_url = "https://myshop.example.com"
            requires_session = False

            @command(help="List orders", endpoint="GET /api/orders")
            def orders(self, ctx: CommandContext) -> dict:
                return {}

        result = invoke_plugin_app(_EndpointPlugin(), ["myshop", "orders", "--help"])
        assert result.exit_code == 0, result.output
        assert "List orders" in result.output
        assert "/api/orders" not in result.output
```

- [ ] **Step 2: Write the failing generator tests**

Append to `class TestPluginModuleCommandStubs` in `tests/unit/test_scaffold_render.py` (add `from typing import Any` to its imports):

```python
    def test_every_stub_declares_its_endpoint_on_a_line_of_its_own(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_ORDERS_ENDPOINT,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert '        endpoint="GET /orders/{order_id}",' in plugin_code.splitlines()
        tree = ast.parse(plugin_code)
        (stub,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        (decorator,) = stub.decorator_list
        assert isinstance(decorator, ast.Call)
        keywords = {k.arg: k.value for k in decorator.keywords}
        endpoint = keywords["endpoint"]
        assert isinstance(endpoint, ast.Constant) and endpoint.value == "GET /orders/{order_id}"

    def test_a_comment_above_the_request_ties_it_to_the_declaration(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_ORDERS_ENDPOINT,)),
        )
        lines = render(spec)["src/graftpunk_myshop/plugin.py"].splitlines()
        request = lines.index("        return ctx.request_json(")
        assert "endpoint=" in lines[request - 1] and "change both together" in lines[request - 1]

    def test_typed_parameters_get_explicit_param_specs(self) -> None:
        """#208: introspected options arrive as strings under the future import, so a
        stub with an int or bool parameter declares every parameter explicitly."""
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_ORDERS_ENDPOINT,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert 'PluginParamSpec.option("order_id", required=True),' in plugin_code
        assert 'PluginParamSpec.option("page", type=int),' in plugin_code
        assert "PluginParamSpec" in plugin_code.split("class ")[0]

    def test_the_param_specs_register_the_types_at_runtime(self, tmp_path: Path) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_SEARCH_ENDPOINT,)),
        )
        namespace: dict[str, Any] = {"__name__": "generated_plugin"}
        exec(render(spec)["src/graftpunk_myshop/plugin.py"], namespace)  # noqa: S102
        meta = namespace["MyshopPlugin"].search._command_meta
        types = {p.name: p.click_kwargs["type"] for p in meta.params}
        assert types["page"] is int
        assert types["include_meta"] is bool
        assert types["q"] is str

    def test_an_untyped_stub_carries_no_params_list(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_CAMEL_PATH_ENDPOINT,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "params=[" not in plugin_code
        assert "PluginParamSpec" not in plugin_code

    def test_a_long_plugins_import_is_exploded(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_ORDERS_ENDPOINT,), login_forms=(_PASSWORD_LOGIN_FORM,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "from graftpunk.plugins import (" in plugin_code
        assert all(len(line) <= GENERATED_LINE_LENGTH for line in plugin_code.splitlines())
```

- [ ] **Step 3: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_cli_plugin.py::TestCommandEndpoint tests/unit/test_scaffold_render.py::TestPluginModuleCommandStubs -q`
Expected: FAIL (`TypeError: command() got an unexpected keyword argument 'endpoint'`).

- [ ] **Step 4: Store the endpoint on the metadata**

In `src/graftpunk/plugins/cli_plugin.py`, add to `CommandMetadata` after `click_kwargs`:

```python
    # Tooling provenance, not behaviour: the "<METHOD> <template>" this command
    # implements, as gp plugin new declares it. Never consulted at runtime and
    # never shown in help; gp plugin info reads it from source. A recorded
    # exception to the devtools placement rule (graft skill spec, 2026-09-21).
    endpoint: str | None = None
```

In `command(...)`, add the parameter `endpoint: str | None = None,` after `name`, add to its docstring's Args:

```
        endpoint: Tooling provenance (functions only): the ``"<METHOD> <template>"``
            this command implements, as ``gp plugin new`` writes it. Stored on the
            metadata, never consulted at runtime, and never shown in help. It is a
            claim the author keeps true by hand: change it with the request.
```

and pass `endpoint=endpoint,` in the function branch's `CommandMetadata(...)` call.

- [ ] **Step 5: Let `import_lines` take several names**

In `src/graftpunk/devtools/scaffold/pysrc.py`, replace `import_lines` with:

```python
def import_lines(module: str, *names: str) -> list[str]:
    """``from {module} import {names}`` on one line when it fits the generated width,
    otherwise the parenthesised form, one name per line with a magic trailing comma,
    which is the shape ``ruff format`` would give it."""
    single_line = f"from {module} import {', '.join(names)}"
    if len(single_line) <= GENERATED_LINE_LENGTH:
        return [single_line]
    return [f"from {module} import (", *(f"{L1}{name}," for name in names), ")"]
```

- [ ] **Step 6: Render the declaration and the typed parameters**

In `src/graftpunk/devtools/scaffold/render.py`, add below `_MUTATING_METHODS`:

```python
# The observed types an explicit PluginParamSpec entry carries. A stub with a
# parameter of one of these gets an explicit params= list, the one route that
# keeps the type under the generated module's future-annotations import. This
# is the compensation for #208: remove it, and the params= emission, when #208
# lands.
_SPEC_TYPE_BY_OBSERVED: dict[str, str] = {"int": "int", "bool": "bool"}

_ENDPOINT_COMMENT = (
    f"{L2}# This request is the endpoint= declared on @command above: change both together."
)
```

and add above `_render_command_stub`:

```python
def _emits_body(endpoint: Endpoint) -> bool:
    """Whether the stub carries a JSON body dict: only for a mutating method, so a GET
    that happened to record a body declares no body arguments (polish round 1,
    2026-09-12)."""
    return bool(endpoint.body_params) and any(m in _MUTATING_METHODS for m in endpoint.methods)


def _declared_extras(endpoint: Endpoint) -> dict[str, str]:
    """The query and body parameters a stub declares as keyword arguments, each with
    its observed type; a query parameter's type wins over a body parameter's."""
    declared = dict(endpoint.body_params) if _emits_body(endpoint) else {}
    declared.update(endpoint.query_params)
    return declared


def _needs_param_specs(endpoint: Endpoint) -> bool:
    """Whether the stub declares its parameters explicitly (see _SPEC_TYPE_BY_OBSERVED)."""
    return any(t in _SPEC_TYPE_BY_OBSERVED for t in _declared_extras(endpoint).values())


def _decorator_lines(name: str, endpoint_literal: str, param_specs: list[str]) -> list[str]:
    """A stub's ``@command(...)``, always exploded one keyword per line so the
    ``endpoint=`` declaration sits on a line of its own."""
    lines = [f"{L1}@command("]
    lines.extend(literal_lines(f"GP-FILL: describe {name}", indent=len(L2), prefix="help="))
    if param_specs:
        lines.append(f"{L2}params=[")
        lines.extend(f"{L3}{spec}," for spec in param_specs)
        lines.append(f"{L2}],")
    lines.extend(literal_lines(endpoint_literal, indent=len(L2), prefix="endpoint="))
    lines.append(f"{L1})")
    return lines
```

Replace `_render_command_stub` with:

```python
def _render_command_stub(endpoint: Endpoint, seen_names: set[str], run_label: str) -> list[str]:
    method = endpoint.methods[0]
    name = _command_name(endpoint.template, seen_names)
    # "self" and "ctx" are taken before any site parameter is named, so a site
    # parameter called either cannot shadow the stub's own arguments.
    seen_params = {"self", "ctx"}
    url_text, path_params = _templated_url(endpoint.template, seen_params)
    is_json = _is_json_endpoint(endpoint)
    call, role, return_type = (
        ("request_json", "xhr", "dict") if is_json else ("request_text", "navigation", "str")
    )
    emits_body = _emits_body(endpoint)
    extras = _declared_extras(endpoint)

    params = ["self", "ctx: CommandContext"] + [f"{p}: str" for p in path_params]
    param_specs = [
        f"PluginParamSpec.option({quoted_literal(p)}, required=True)" for p in path_params
    ]
    identifier_for: dict[str, str] = {}
    for extra in sorted(extras):
        observed = extras[extra]
        identifier_for[extra] = _param_identifier(extra, seen_params)
        annotation = _PY_TYPE_BY_OBSERVED.get(observed, "str")
        params.append(f"{identifier_for[extra]}: {annotation} | None = None")
        spec_type = _SPEC_TYPE_BY_OBSERVED.get(observed)
        type_keyword = f", type={spec_type}" if spec_type else ""
        param_specs.append(
            f"PluginParamSpec.option({quoted_literal(identifier_for[extra])}{type_keyword})"
        )

    # Through quoted_literal like every other captured value: the method comes from
    # the capture, so it is not this module's to assume is quote-free.
    call_lines = [f"{L3}{quoted_literal(method)},"]
    call_lines.extend(url_expr_lines(url_text, is_fstring=bool(path_params), indent=len(L3)))
    call_lines.append(f"{L3}role={quoted_literal(role)},")
    if endpoint.query_params:
        entries = [(p, identifier_for[p]) for p in sorted(endpoint.query_params)]
        call_lines.extend(exploded_dict_lines("params", entries))
    if emits_body:
        entries = [(p, identifier_for[p]) for p in sorted(endpoint.body_params)]
        call_lines.extend(exploded_dict_lines("json", entries))
    if endpoint.custom_headers:
        entries = [(h, '"GP-FILL"') for h in endpoint.custom_headers]
        call_lines.extend(exploded_dict_lines("headers", entries))

    summary = f"{method} {endpoint.template}: seen {endpoint.count} time(s) in run {run_label}."
    # An unavailable shape is not a fact about the site, so the docstring says
    # nothing rather than guessing (polish round 1, 2026-09-12).
    shape_known = endpoint.shape is None or endpoint.shape != SHAPE_UNAVAILABLE

    lines = _decorator_lines(
        name,
        f"{method} {endpoint.template}",
        param_specs if _needs_param_specs(endpoint) else [],
    )
    lines.append(f"{L1}def {name}(")
    lines.extend(f"{L2}{p}," for p in params)
    lines.append(f"{L1}) -> {return_type}:")
    lines.append(f'{L2}"""')
    lines.extend(wrapped_docstring_lines(summary))
    if shape_known:
        shape_line = f"Shape: {summarize_shape(endpoint.shape, depth=_SCAFFOLD_SHAPE_DEPTH)}."
        lines.append("")
        lines.extend(wrapped_docstring_lines(shape_line))
    lines.append(f'{L2}"""')
    lines.append(_ENDPOINT_COMMENT)
    lines.append(f"{L2}return ctx.{call}(")
    lines.extend(call_lines)
    lines.append(f"{L2})")
    lines.append("")
    return lines
```

Replace `_plugins_import_names` with:

```python
def _plugins_import_names(*, needs_login_import: bool, needs_param_spec: bool) -> list[str]:
    """The names to import from ``graftpunk.plugins``, ordered the way this project's
    own isort setting (classes, then functions, each alphabetical) expects, so the
    generated line never needs a second reformatting pass."""
    classes = ["CommandContext", "SitePlugin"]
    if needs_login_import:
        classes += ["LoginConfig", "LoginStep"]
    if needs_param_spec:
        classes.append("PluginParamSpec")
    return sorted(classes) + ["command"]
```

In `_render_plugin_module`, replace the `plugins_import = ...` assignment and the `f"from graftpunk.plugins import {plugins_import}",` list entry with:

```python
    needs_param_spec = any(_needs_param_specs(e) for e in _stub_endpoints(spec))
    plugins_names = _plugins_import_names(
        needs_login_import=needs_login_import, needs_param_spec=needs_param_spec
    )
```

and `*import_lines("graftpunk.plugins", *plugins_names),` in the `lines = [...]` list.

- [ ] **Step 7: Bring the guide's generated example up to date**

In `docs/PLUGIN_DEVELOPMENT.md`, "What gets filled in": replace the import line `from graftpunk.plugins import CommandContext, LoginConfig, LoginStep, SitePlugin, command` with:

```python
from graftpunk.plugins import (
    CommandContext,
    LoginConfig,
    LoginStep,
    PluginParamSpec,
    SitePlugin,
    command,
)
```

replace the line `    @command(help="GP-FILL: describe api_orders")` with:

```python
    @command(
        help="GP-FILL: describe api_orders",
        params=[
            PluginParamSpec.option("archived", type=bool),
            PluginParamSpec.option("page", type=int),
            PluginParamSpec.option("per_page", type=int),
        ],
        endpoint="GET /api/orders",
    )
```

and insert `        # This request is the endpoint= declared on @command above: change both together.` directly above `        return ctx.request_json(` in that block. In the paragraph that begins "What came from the digest", replace "each with the observed query parameters as typed keyword arguments and the observed custom headers;" (the phrase wraps across `docs/PLUGIN_DEVELOPMENT.md:474-475`, breaking after "arguments and", so an exact-match edit has to carry that line break) with "each with the observed query parameters as typed keyword arguments, an explicit `params=` list whenever one of them is an `int` or a `bool` (see [CLI parameter types](#cli-parameter-types)), the observed custom headers, and the endpoint it calls declared as `endpoint=` on its decorator;". Append this paragraph after the one that begins "Everything the digest could not decide":

```markdown
The `endpoint=` keyword is a declaration, not a check: nothing compares it with
the request below it, and it is never used when the command runs. Tooling that
reads the plugin's source takes the endpoint from it, so when you change the
request, change the declaration with it.
```

In "CLI parameter types", append to the paragraph that ends "so it works in a generated module as written.": " `gp plugin new` writes that explicit list itself for every stub with an `int` or `bool` parameter."

- [ ] **Step 8: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_cli_plugin.py tests/unit/test_scaffold_render.py tests/unit/test_scaffold_cli.py tests/unit/test_plugin_development_guide.py -q`
Expected: PASS, including `TestRenderedTreeIsRuffClean` and `TestGeneratedProjectPassesItsOwnGate`, which run real ruff over generated trees, and the guide's executed Python blocks.

- [ ] **Step 9: Commit**

```bash
git add src/graftpunk/plugins/cli_plugin.py src/graftpunk/devtools/scaffold/pysrc.py src/graftpunk/devtools/scaffold/render.py docs/PLUGIN_DEVELOPMENT.md tests/unit/test_cli_plugin.py tests/unit/test_scaffold_render.py
git commit -m "feat(scaffold): every stub declares its endpoint, and typed parameters get explicit PluginParamSpec entries"
```

---

### Task 12: CHANGELOG and the full gate

**Files:**
- Modify: `CHANGELOG.md:10-22` (`[Unreleased]` Added)

**Interfaces:**
- Consumes: everything above.
- Produces: the pull request's release note.

- [ ] **Step 1: Correct the unreleased fixtures line and add this pull request's line**

In `CHANGELOG.md` under `[Unreleased]` / `### Added`, in the `**gp observe fixtures**` entry, replace "Each fixture gets a `<file>.meta.json` sidecar (url, status, content type, body parameter names, capture time); `graftpunk.testing.FixtureSession` reads the same sidecar so a fixture copied from a capture keeps its recorded status." with "Each capture gets a `<file>.meta.json` sidecar that is safe to commit beside the fixture derived from it: a `schema` number, the status, the content type, the body parameter names, the hash of the captured body, and the cookie and token names the run's digest recorded (never the URL or the capture time). `graftpunk.testing.FixtureSession` reads it through `graftpunk.testing.sidecar`, which refuses a sidecar with a missing or unknown `schema` or a key outside its version." Then append this line as the last bullet of `### Added`:

```markdown
- **Machine surfaces for tools that drive `gp`** (the groundwork for the `/graftpunk:graft` Claude Code skill). `gp observe digest --endpoints-json` prints a versioned projection of the digest (method, template, `login_flow`, content type, shape summary, parameter names and types, custom header names, and the login's auth URLs and form selectors; no cookie name, token candidate, example path, or body). `gp version --json` prints the installed version and the schema number of each payload a caller reads; `--at-least VERSION` exits 0 or 1 by version order (`packaging` is now a dependency), and `--contract SURFACE=N` (repeatable) exits 3, naming the older side, unless graftpunk writes that payload at that schema. `gp plugin new NAME --check-name` checks a name and writes nothing. Every stub `gp plugin new` writes declares the endpoint it calls as `@command(endpoint="GET /api/orders")`, a new keyword that is stored on the command and never used at runtime, and a stub with an `int` or `bool` parameter carries explicit `PluginParamSpec` entries so the type survives ([#208](https://github.com/stavxyz/graftpunk/issues/208)). `--match` values now need the method in capitals, as the digest prints it.
```

- [ ] **Step 2: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
Expected: every test passes, ruff reports no findings and no files to reformat, ty reports no errors.

- [ ] **Step 3: Scan the diff for banned punctuation and names**

Run: `git diff main | perl -CSD -ne 'print "$.: $_" if /^\+.*([\x{2013}\x{2014}]| \x2d\x2d )/'`
Expected: no output (no added line carries an em dash, an en dash, or a spaced double hyphen).

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): machine surfaces for tools that drive gp"
```
