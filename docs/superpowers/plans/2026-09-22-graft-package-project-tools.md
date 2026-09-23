---
type: plan
validated:
  sha: 80a738066bb7d3d8adbcc65437249edcb47cd2a8
  date: 2026-09-23T07:07:51Z
  reviewers: [fact-check, solid-hygiene]
  findings:
    critical: 0
    important: 1
    medium: 2
    low: 9
    nitpick: 0
  net_negative_raised: 1
  net_negative_addressed: 1
  net_negative_remaining: 0
---

# Graft Package Project Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the second of the three pull requests the graft skill design orders: the pieces that need the project reader (`devtools/plugin_project.py`, `gp plugin info --json`, `gp plugin new --command`, `gp plugin add-command`, `gp plugin upgrade`, `gp plugin check`, the in-suite `fixtures_are_sanitised` check and its conftest wiring, and the one project gate quoted into the generated README and the guide).

**Architecture:** One reader, `devtools/plugin_project.py`, resolves the working directory to a plugin project, classifies it (`empty`, `plugin`, `foreign`), and reads every plugin module with `ast` into one structural view; `gp plugin info`, the stub inserter (`scaffold/insert.py`), the migrator (`scaffold/upgrade.py`), and the lint (`devtools/plugin_check.py`) all work from that view and never parse on their own. What a generated project must hold is declared in `scaffold/policy.py` (`PROJECT_REQUIREMENTS`, `PROJECT_GATE`), which the renderer emits, the migrator applies, and the lint reports. The in-suite sanitisation check lives on the pytest side of `graftpunk.testing` and receives the fixtures tree from the generated conftest, so nothing in `graftpunk.testing` imports `graftpunk.devtools`.

**Tech Stack:** Python 3.11+, Typer 0.21+, `ast`, `tomllib`, `hashlib` (stdlib), pytest (and its `pytester` for the in-suite check), ruff (run on generated trees through `sys.executable -m ruff`), ty 0.0.75.

**Spec:** `docs/superpowers/specs/2026-09-21-graft-skill-design.md` (validated 2026-09-22). Read it alongside this plan. This plan starts from the tree `docs/superpowers/plans/2026-09-22-graft-package-foundations.md` leaves and consumes its names exactly as that plan's Interfaces blocks state them. Its own Interfaces blocks are what `docs/superpowers/plans/2026-09-22-graft-skill.md` consumes.

**Implementation notes (deviations from the spec's wording, for review):**
- `PROJECT_REQUIREMENTS` has two entries, not one, and each carries the imports its statement reads. The spec's single entry ("`tests/conftest.py` binds `fixtures_are_sanitised`, by the import line the renderer writes") cannot work as written: an import alone registers nothing with pytest and hands the check no tree, and an import appended to the end of an existing conftest fails the generated project's own `ruff check` (E402, I001). The check is a factory like `site_env_scrubber`: the conftest binds `FIXTURES_TREE = Path(__file__).parent / "fixtures"` and `sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)`, and those two names are the requirements. The migrator merges each requirement's imports into the file's import block the way isort places them, then appends the statement, so an upgraded conftest is byte-identical to a freshly generated one.
- A plugin is a suite member, for the fixtures-root rule, when its package name differs from the project's `[project].name` (both normalised with `[-_.]` to `_`). That is the fact on disk that matches the generator's own decision: `gp plugin new` names a new project after its package, and every plugin added to it later has a different package.
- The reader records a plugin module that parses but does not hold exactly one `SitePlugin` subclass as a `PluginDefect` in the view rather than raising, so one bad module does not blind every consumer. `gp plugin check` lists it as a finding, `gp plugin add-command` refuses only when it targets that plugin, and `gp plugin upgrade` proceeds. `gp plugin info --json` refuses (exit 1, one line per defect) rather than print a payload that silently leaves the plugin out, so preflight still stops with exit 4 and the reader's reason; the refusal is `info_payload`'s own (`PluginDefectRefusal`), so no caller can build a payload that omits a plugin, and the `info` payload's field set does not change. A requirement's file that does not parse (`tests/conftest.py`) is recorded the same way, as the requirement state `unreadable` with its reason: `gp plugin upgrade` refuses on it and `gp plugin check` reports it, while `info` and `add-command`, which never read that file, proceed. Only invalid TOML and a plugin module that is missing or does not parse make the whole read fail.
- `selection.py` owns only the explicit selection (`plan_command`, and `planned_commands` for a list of them). The renderer decides between the explicit selection and its default choice (every eligible endpoint under generated names, up to the cap), so `selection.py` never imports `render.py`, takes no callback, and the generated-name rules stay beside the code that spells a stub.
- `pysrc.with_import` places an import only into the shapes generated files have and refuses anything else with a message naming `ruff check --fix`; `add-command` and `upgrade` report that as their own refusal.
- `gp plugin new` keeps its existing per-error `except` arms, each of which logs its own refusal reason (foundations Task 10 gave it the `ScaffoldWriteError` arm); the entry points this plan adds (`info`, `add-command`, `upgrade`) catch `DevtoolsRefusal` alone, which covers a failed write too, because `apply_changes` raises `ScaffoldWriteError` and never a bare `OSError`.
- A plugin's one identity is its entry-point name, the key of its line in the `graftpunk.plugins` table. The reader keys each `PluginView` and `PluginDefect` by it, `gp plugin info --json` reports it as `entry_point`, `gp plugin add-command <plugin>` takes it, and the skill matches `$0` against it. `site_name` is a fact about the plugin, not its address: a hand-written project may name the two differently.
- `gp plugin new --command` refuses an endpoint the digest marks `login_flow`, with the same text `gp plugin add-command` uses. The spec lists that refusal for `add-command` only; one rule for both explicit-selection paths is the reading that keeps them from disagreeing.

## Global Constraints

- The placement rule in `src/graftpunk/devtools/__init__.py`: public API a plugin imports at runtime or in its own tests lives at the top level of the package; CLI-only tooling that a plugin never imports lives under `graftpunk.devtools`. `graftpunk.testing` imports nothing from `graftpunk.devtools`, and a test asserts it on the import graph. The import direction is: devtools reads policy; `graftpunk.testing` imports neither.
- `policy.py` is imported by readers and writers alike and imports nothing that touches the filesystem; the reader (`plugin_project.py`) and the lint (`plugin_check.py`) never import `write.py`; the writers (`project.py`, `insert.py`, `upgrade.py`) write only through it.
- Consumers read `policy.PROJECT_REQUIREMENTS` and `policy.PROJECT_GATE` as module attributes (`from graftpunk.devtools.scaffold import policy`), never by `from ... import PROJECT_REQUIREMENTS`, so one entry added in a test reaches every consumer.
- `src/graftpunk/cli/scaffold_commands.py` stays "argument handling only": each entry point parses its options and calls into `devtools`.
- Placeholders only, in code, tests, docs, and commit messages: `myshop`, `myshop.example`, `alice@example.com`, and `example.com` and `example.net` hosts. Never a real site, vendor, account, `op://` path, or a named secret manager.
- No em dashes or en dashes, and no spaced double hyphen standing in for one, in any file, docstring, comment, commit message, or plan text.
- Every commit subject is in the repository's `type(scope): subject` form.
- No Claude attribution in commits: no `Co-Authored-By`, no "Generated with" footer, no `Claude-Session` trailer.
- Tests assert behaviour, never that a mock was called.
- Within the package, `graftpunk/contracts.py` is the only module that declares the current schema number, and the only one that compares one.
- `GP-FILL`, the tests directory, the fixtures tree, the conftest path, and the module-name rule are each spelled once, in `policy.py`; the renderer, the reader, the migrator, and the lint take them from there. The fixtures placeholder name (`.gitkeep`) is spelled once, in `graftpunk.testing.sidecar`, which policy re-exports. The entry-point group is the runtime's: `graftpunk.plugins.PLUGINS_GROUP` is its one spelling, policy does not import it, and a test holds the group's two uses (a TOML table header and an entry-point lookup) to that constant. The same text as an import path (the package a generated plugin imports from) is a different fact, and the renderer keeps it as its own plain constant.
- Every devtools refusal a CLI entry point reports subclasses `graftpunk.devtools.errors.DevtoolsRefusal` (foundations Task 10), and the entry points this plan adds catch that one type.
- No test module imports from another `test_*.py` module. Helpers two test modules share live in a non-test module (`tests/unit/guide_harness.py`, Task 9).
- The full gate, green at the end of every task and run in full by the last task: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
- A single test runs as `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/<file>.py::<test> -q`.

## Review Focus

1. `gp plugin add-command` on a module whose plugin class is followed by a decorated helper and an `if __name__ == "__main__":` block: the stub must land inside the class, and the module must still pass `ruff check` and `ruff format --check`. Pinned in Task 6 (`test_a_decorated_helper_and_a_main_block_below_the_class_stay_below_it`).
2. `gp plugin info --json` in a directory whose `pyproject.toml` is not valid TOML: a person expects exit 1 and a line naming the file, not a traceback. Pinned in Task 4 (`test_an_unreadable_pyproject_is_a_one_line_refusal`).
3. A binary fixture under the fixtures tree (a PDF export): the sanitisation check must read it as bytes and never crash on decoding. Pinned in Task 1 (`test_a_binary_fixture_is_checked_without_decoding_errors`).
4. `gp plugin upgrade` on a conftest whose last statement is a fixture function a developer added: the appended statements must sit two blank lines below it so the project's `ruff check` still passes. Pinned in Task 7 (`test_statements_after_a_function_keep_two_blank_lines`).
5. A `--command` name that is a Python keyword (`class`) or that collides with an existing command only by hyphen versus underscore (`order-list` against `order_list`): both must be refused before anything is written. Pinned in Task 5 (`test_a_keyword_name_is_refused`) and Task 6 (`test_a_hyphen_underscore_collision_is_refused`).

## File Structure

| File | Responsibility |
| --- | --- |
| `src/graftpunk/testing/sidecar.py` (modify, Task 1) | `FIXTURES_PLACEHOLDER`, the `.gitkeep` name; `sidecar_scannable_text`, the text of the fields copied from the capture (every field but `flagged_names` and `capture_sha256`). |
| `src/graftpunk/testing/plugin.py` (modify, Task 1) | `FixturesTreeReport`, `check_fixtures_tree`, `fixtures_are_sanitised`; the module docstring rewritten. |
| `src/graftpunk/devtools/scaffold/policy.py` (modify, Tasks 2, 3, 9) | `CONFTEST_PATH`, `FIXTURES_PLACEHOLDER` (imported from `graftpunk.testing.sidecar`), `ProjectRequirement`, `PROJECT_REQUIREMENTS` (Task 2); `GP_FILL_MARKER`, `module_name_for` (Task 3); `PROJECT_GATE` (Task 9). |
| `src/graftpunk/devtools/scaffold/pysrc.py` (modify, Task 2) | `binds_name`, `with_import`, `Binding`, `with_bindings`, and their private helpers: the one binding predicate and the one assembler for statements added to a module. |
| `src/graftpunk/devtools/scaffold/render.py` (modify, Tasks 2, 3, 5, 9) | Conftest through `with_bindings`; `PLUGINS_GROUP` in the rendered `pyproject.toml`; `GP-FILL` from policy, and `module_name_for` called as `policy.module_name_for` and no longer exported; `RenderedCommand`, `render_command`, commands planned once per render through `selection.py`; README checks block from `PROJECT_GATE`. |
| `src/graftpunk/devtools/scaffold/project.py` (modify, Tasks 2, 3) | Imports `PLUGINS_GROUP` from `graftpunk.plugins`, the group's one spelling, and `FIXTURES_PLACEHOLDER` from policy (Task 2); imports `module_name_for` from policy directly (Task 3). |
| `src/graftpunk/devtools/scaffold/pyproject_edit.py` (modify, Task 2) | Reads `PLUGINS_GROUP` instead of spelling the group. |
| `src/graftpunk/devtools/scaffold/selection.py` (new, Task 5) | `CommandSelection`, `CommandSelectionError`, `command_identifier`, `PlannedCommand`, `plan_command`, `planned_commands`: which commands a render or an insert produces, under which names. |
| `src/graftpunk/har/naming.py` (modify, Task 3) | `to_cli_name`, the kebab-case rule a command's CLI name follows, moved here from `cli_plugin.py` and made public; `registered_name`, the one owner of "the `name=` pin, else the kebab-cased identifier". |
| `src/graftpunk/plugins/cli_plugin.py`, `src/graftpunk/client.py` (modify, Task 3) | Import `to_cli_name` from `har/naming.py`, and `cli_plugin.py` also `registered_name`, which the `command` decorator's two pinnable sites call; `_to_cli_name` is gone, with no alias kept. |
| `src/graftpunk/devtools/plugin_project.py` (new, Task 3) | The reader and its structural view: `Span`, `CommandView` (with `cli_name`), `PluginView`, `PluginDefect`, `RequirementStatus`, `ProjectView` (with `missing_requirements()` and `unreadable_files()`), `PluginProjectError`, `NotAPluginProjectError`, `classify`, `read_project`, `require_plugin_project`. |
| `src/graftpunk/devtools/plugin_info.py` (new, Task 4) | `info_payload`, the `gp plugin info --json` payload built from the view, and `PluginDefectRefusal`, which it raises instead of building a payload that omits a defective plugin. |
| `src/graftpunk/contracts.py` (modify, Task 4) | `INFO_SCHEMA`; `"info"` in `CLI_SURFACES`. |
| `src/graftpunk/devtools/scaffold/insert.py` (new, Task 6) | `CommandInsertError`, `AddedCommand`, `insertion_line`, `add_command`. |
| `src/graftpunk/devtools/scaffold/__init__.py` (modify, Task 6) | Its docstring: the generators and mutators of a plugin project that the `gp plugin` commands call. |
| `src/graftpunk/devtools/scaffold/upgrade.py` (new, Task 7) | `UpgradeRefusedError`, `upgrade_project`. |
| `src/graftpunk/devtools/plugin_check.py` (new, Task 8) | `Finding`, `check_project`. |
| `src/graftpunk/cli/scaffold_commands.py` (modify, Tasks 4 to 8, 10) | `gp plugin info`, `new --command`, `add-command`, `upgrade`, `check`; `plugin_app`'s help. |
| `tests/unit/guide_harness.py` (new, Task 9) | The guide helpers two test modules share: `REPO_ROOT`, `GUIDE`, `GUIDE_TEXT`, `blocks`, `gp_invocations`, `option_names`, `check_invocation`, `slug`, `slugs_of`, `outside_fences`, and `section`, the one rule for where a markdown section ends. |
| `docs/PLUGIN_DEVELOPMENT.md` (modify, Tasks 2, 9, 10) | The conftest section; "The gate", its CI example, "Before you publish"; the recipe, the sentence on a fixture without a sidecar, and the check's limits. |
| `README.md` (modify, Task 10) | The `plugin` line of the CLI Reference block. |
| `CHANGELOG.md` (modify, Task 10) | One Added line. |

---

### Task 1: `fixtures_are_sanitised`, the in-suite sanitisation check

**Files:**
- Modify: `src/graftpunk/testing/sidecar.py` (new `FIXTURES_PLACEHOLDER`, `sidecar_scannable_text`; `__all__`)
- Modify: `src/graftpunk/testing/plugin.py` (module docstring, imports, `__all__`, three new names)
- Test: `tests/unit/test_testing_sidecar.py`
- Test: `tests/unit/test_graftpunk_testing.py`

**Interfaces:**
- Consumes: `graftpunk.testing.sidecar.SidecarError`, `is_sidecar`, `load_sidecar`, `sidecar_path`, and `Sidecar.declared` (foundations Task 7); `graftpunk.contracts.current_schema` (foundations Task 2).
- Produces: in `graftpunk.testing.sidecar`, `FIXTURES_PLACEHOLDER = ".gitkeep"` (the one file under a fixtures tree that is neither a fixture nor a sidecar; Task 2's policy re-exports it) and `sidecar_scannable_text(sidecar: Sidecar) -> str` (the value of every field but `flagged_names`, the text the check scans for a flagged name); `@dataclass(frozen=True) class FixturesTreeReport(problems: tuple[str, ...], verified: int, declared: int)` with property `summary -> str`; `check_fixtures_tree(tree: Path) -> FixturesTreeReport`; `fixtures_are_sanitised(tree: Path | str) -> Any`, a factory returning a session-scoped autouse pytest fixture. Task 2's generated conftest binds `sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_testing_sidecar.py` (add `FIXTURES_PLACEHOLDER` and `sidecar_scannable_text` to its `graftpunk.testing.sidecar` import):

```python
class TestScannableText:
    def test_every_copied_field_is_scanned(self) -> None:
        sidecar = Sidecar(
            status=403,
            content_type="text/plain",
            body_params=("page",),
            capture_sha256="ab" * 32,
            flagged_names=("shop_session",),
        )
        text = sidecar_scannable_text(sidecar)
        for value in ("403", "text/plain", "page"):
            assert value in text
        assert "shop_session" not in text
        assert "ab" * 32 not in text


def test_the_placeholder_is_the_gitkeep() -> None:
    assert FIXTURES_PLACEHOLDER == ".gitkeep"
```

Append to `tests/unit/test_graftpunk_testing.py` (add only `import hashlib` and `from graftpunk.testing.plugin import check_fixtures_tree` to its imports; foundations Task 7 already imports `Sidecar` and `sidecar_text` there):

```python
_GENERATED_CONFTEST = """
from pathlib import Path

from graftpunk.testing.plugin import fixtures_are_sanitised

FIXTURES_TREE = Path(__file__).parent / "fixtures"

sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)
"""


def _fixture(tree: Path, relative: str, body: bytes, sidecar: Sidecar | None) -> Path:
    path = tree / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    if sidecar is not None:
        path.with_name(path.name + ".meta.json").write_text(sidecar_text(sidecar))
    return path


def _captured(body: bytes, *flagged: str) -> Sidecar:
    return Sidecar(
        status=200,
        content_type="application/json",
        capture_sha256=hashlib.sha256(body).hexdigest(),
        flagged_names=flagged,
    )


class TestCheckFixturesTree:
    """The one enforcer of "a committed fixture came off no account unchanged"
    (graft skill spec, 2026-09-21)."""

    def test_a_missing_tree_fails(self, tmp_path: Path) -> None:
        report = check_fixtures_tree(tmp_path / "fixtures")
        assert len(report.problems) == 1
        assert "does not exist" in report.problems[0]

    def test_an_empty_tree_with_its_placeholder_passes(self, tmp_path: Path) -> None:
        (tmp_path / ".gitkeep").write_text("")
        assert check_fixtures_tree(tmp_path).problems == ()

    def test_a_fixture_with_no_sidecar_fails_and_says_how_to_make_one(
        self, tmp_path: Path
    ) -> None:
        _fixture(tmp_path, "get_orders.json", b"{}", None)
        (problem,) = check_fixtures_tree(tmp_path).problems
        assert "get_orders.json: no sidecar" in problem
        assert "gp observe fixtures" in problem
        assert '"capture_sha256": null' in problem

    def test_a_suite_member_added_after_the_conftest_is_covered(self, tmp_path: Path) -> None:
        """The walk covers the whole tree, so a second member's root needs no
        per-plugin fact in the conftest."""
        _fixture(tmp_path, "myshop/get_orders.json", b"{}", None)
        _fixture(tmp_path, "widgets/get_widgets.json", b"{}", None)
        problems = check_fixtures_tree(tmp_path).problems
        assert any(p.startswith("myshop/get_orders.json") for p in problems)
        assert any(p.startswith("widgets/get_widgets.json") for p in problems)

    def test_an_unchanged_copy_of_the_capture_fails(self, tmp_path: Path) -> None:
        body = b'{"orders": [{"id": "1001"}]}'
        _fixture(tmp_path, "get_orders.json", body, _captured(body))
        (problem,) = check_fixtures_tree(tmp_path).problems
        assert "unchanged copy" in problem

    def test_a_flagged_name_in_the_body_fails(self, tmp_path: Path) -> None:
        _fixture(
            tmp_path,
            "get_orders.json",
            b'{"myshop_session": "invented"}',
            _captured(b"captured", "myshop_session"),
        )
        (problem,) = check_fixtures_tree(tmp_path).problems
        assert "'myshop_session'" in problem

    def test_a_flagged_name_in_the_sidecar_fails(self, tmp_path: Path) -> None:
        sidecar = Sidecar(
            status=200,
            content_type="application/json",
            body_params=("myshop_session",),
            capture_sha256=hashlib.sha256(b"captured").hexdigest(),
            flagged_names=("myshop_session",),
        )
        _fixture(tmp_path, "get_orders.json", b'{"orders": []}', sidecar)
        (problem,) = check_fixtures_tree(tmp_path).problems
        assert problem.startswith("get_orders.json.meta.json")

    def test_a_sidecar_of_unknown_schema_fails(self, tmp_path: Path) -> None:
        path = _fixture(tmp_path, "get_orders.json", b"{}", None)
        payload = json.loads(sidecar_text(Sidecar(status=200, content_type="x")))
        payload["schema"] = 99
        path.with_name(path.name + ".meta.json").write_text(json.dumps(payload))
        (problem,) = check_fixtures_tree(tmp_path).problems
        assert "schema 99" in problem

    def test_an_invented_fixture_passes_and_counts_as_verified(self, tmp_path: Path) -> None:
        _fixture(
            tmp_path,
            "get_orders.json",
            b'{"orders": [{"id": "9001"}]}',
            _captured(b'{"orders": [{"id": "1001"}]}', "myshop_session"),
        )
        report = check_fixtures_tree(tmp_path)
        assert (report.problems, report.verified, report.declared) == ((), 1, 0)

    def test_a_hand_made_fixture_with_no_capture_hash_passes_on_declaration(
        self, tmp_path: Path
    ) -> None:
        _fixture(tmp_path, "get_orders.json", b"{}", Sidecar(status=200, content_type="x"))
        report = check_fixtures_tree(tmp_path)
        assert (report.problems, report.verified, report.declared) == ((), 0, 1)
        assert "1 accepted on declaration" in report.summary

    def test_a_binary_fixture_is_checked_without_decoding_errors(self, tmp_path: Path) -> None:
        body = b"%PDF-1.7\n\xff\xfe\x00invented"
        _fixture(tmp_path, "get_invoice.pdf", body, _captured(b"%PDF captured", "myshop_session"))
        assert check_fixtures_tree(tmp_path).problems == ()


class TestFixturesAreSanitisedInASuite:
    """Driven through a real inner pytest run, given FIXTURES_TREE the way the
    generated conftest supplies it."""

    def test_a_clean_tree_passes_and_the_declared_count_is_reported(
        self, pytester: pytest.Pytester
    ) -> None:
        fixtures = pytester.path / "fixtures"
        fixtures.mkdir()
        (fixtures / "get_orders.json").write_text("{}")
        (fixtures / "get_orders.json.meta.json").write_text(
            sidecar_text(Sidecar(status=200, content_type="application/json"))
        )
        pytester.makepyfile(conftest=_GENERATED_CONFTEST, test_one="def test_one():\n    pass\n")
        result = pytester.runpytest_inprocess("-o", "asyncio_default_fixture_loop_scope=function")
        result.assert_outcomes(passed=1)
        result.stdout.fnmatch_lines(["*0 fixture(s) verified*1 accepted on declaration*"])

    def test_a_violation_fails_the_run_with_its_message(self, pytester: pytest.Pytester) -> None:
        fixtures = pytester.path / "fixtures"
        fixtures.mkdir()
        (fixtures / "get_orders.json").write_text("{}")
        pytester.makepyfile(conftest=_GENERATED_CONFTEST, test_one="def test_one():\n    pass\n")
        result = pytester.runpytest_inprocess("-o", "asyncio_default_fixture_loop_scope=function")
        result.assert_outcomes(errors=1)
        result.stdout.fnmatch_lines(["*get_orders.json: no sidecar*"])


def test_graftpunk_testing_plugin_imports_nothing_from_devtools() -> None:
    script = (
        "import sys\n"
        "import graftpunk.testing.plugin\n"
        "print(sorted(m for m in sys.modules if m.startswith('graftpunk.devtools')))\n"
    )
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=True
    )
    assert result.stdout.strip() == "[]"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graftpunk_testing.py tests/unit/test_testing_sidecar.py -q`
Expected: collection errors, `ImportError: cannot import name 'check_fixtures_tree'` and `cannot import name 'FIXTURES_PLACEHOLDER'`.

- [ ] **Step 3: Write the check**

In `src/graftpunk/testing/sidecar.py`, add `"FIXTURES_PLACEHOLDER"` and `"sidecar_scannable_text"` to `__all__`, add below `SIDECAR_SUFFIX`:

```python
FIXTURES_PLACEHOLDER = ".gitkeep"
"""The file gp plugin new writes so git keeps an empty fixtures directory: the one
file under a fixtures tree that is neither a fixture nor a sidecar. Spelled here,
in the plugin-facing layer the fixtures check lives in, and re-exported by the
devtools policy that renders it."""
```

and append:

```python
def sidecar_scannable_text(sidecar: Sidecar) -> str:
    """The values of the fields of *sidecar* that were copied from the capture,
    space-separated: the text a flagged name must not appear in. Two fields are
    exempt. ``flagged_names`` lists the names themselves, and ``capture_sha256``
    is a digest the writer computed, never text copied from the capture, so
    scanning it could only produce a false match on a name that happens to be a
    run of hex digits. ``schema`` is the file's number, not a field of the loaded
    sidecar."""
    return " ".join([str(sidecar.status), sidecar.content_type, *sidecar.body_params])
```

In `src/graftpunk/testing/plugin.py`, replace the whole module docstring (its "Load only as a pytest plugin" sentence and its "and nothing else" paragraph are both false once a second factory exists) with:

```python
"""The pytest half of :mod:`graftpunk.testing`: fixture factories a generated
``tests/conftest.py`` calls.

A generated conftest imports the factories here and assigns each one's result
to a module-level name, which is what registers that fixture with pytest.
:func:`site_env_scrubber` keeps the developer's own site variables out of the
tests; :func:`fixtures_are_sanitised` checks every committed fixture against
its sidecar, and :func:`check_fixtures_tree` is that check without pytest, for
a caller that wants the report itself. The generated file carries no framework
logic of its own to drift from graftpunk's (the scaffold rule, plugin tooling
spec, 2026-09-11).

It deliberately does not name this module in ``pytest_plugins``: that would ask
pytest to rewrite assertions in a module the import has already loaded, which
it warns about, and this module has no hooks of its own to register that way.
``fixtures_are_sanitised`` is handed the fixtures tree by the conftest, which
declares it as ``FIXTURES_TREE``; nothing here imports ``graftpunk.devtools``
(graft skill spec, 2026-09-21).
"""
```

Add `import hashlib`, `from dataclasses import dataclass`, `from pathlib import Path`, `from graftpunk.contracts import current_schema`, and `from graftpunk.testing.sidecar import FIXTURES_PLACEHOLDER, SidecarError, is_sidecar, load_sidecar, sidecar_path, sidecar_scannable_text` (parenthesised, one name per line, as ruff format writes an import past 100 columns); set `__all__ = ["FixturesTreeReport", "check_fixtures_tree", "fixtures_are_sanitised", "site_env_scrubber"]`; and append:

```python
@dataclass(frozen=True)
class FixturesTreeReport:
    """What :func:`check_fixtures_tree` found: every problem, and the two counts."""

    problems: tuple[str, ...]
    verified: int
    declared: int

    @property
    def summary(self) -> str:
        """The line the check prints on every run, so the declared count shows in review."""
        return (
            f"fixtures_are_sanitised: {self.verified} fixture(s) verified against their "
            f"capture, {self.declared} accepted on declaration (no capture hash: "
            f"trusted, not checked)"
        )


def _missing_sidecar(relative: Path, sidecar_name: str) -> str:
    schema = current_schema("sidecar")
    return (
        f"{relative}: no sidecar ({sidecar_name}). For a fixture derived from a capture, "
        f"copy the sidecar gp observe fixtures wrote beside the capture. For a hand-made "
        f'fixture, write {sidecar_name} with "schema": {schema}, a status, a content type, '
        f'"body_params": [], "capture_sha256": null, and "flagged_names": [], which '
        f"declares that the file came off no account."
    )


def check_fixtures_tree(tree: Path) -> FixturesTreeReport:
    """Check every fixture anywhere under *tree* against its sidecar.

    Catches a missing tree, a fixture with no sidecar, a sidecar outside its
    declared format (through the owner, :mod:`graftpunk.testing.sidecar`), a
    fixture that is byte for byte its capture, and a flagged cookie or token name
    in a fixture or in its sidecar's other fields. It does not judge whether
    invented content is invented well. A sidecar with no capture hash is the
    author's declaration that the file came off no account; it is trusted, not
    checked, and counted.
    """
    if not tree.is_dir():
        return FixturesTreeReport(
            problems=(
                f"{tree}: the fixtures tree does not exist. The generated conftest names "
                f"it as FIXTURES_TREE and gp plugin new creates it with a .gitkeep, so a "
                f"missing tree means the two disagree. Recreate the directory, or "
                f"correct FIXTURES_TREE.",
            ),
            verified=0,
            declared=0,
        )
    problems: list[str] = []
    verified = declared = 0
    fixtures = sorted(
        p
        for p in tree.rglob("*")
        if p.is_file() and not is_sidecar(p) and p.name != FIXTURES_PLACEHOLDER
    )
    for fixture in fixtures:
        relative = fixture.relative_to(tree)
        meta = sidecar_path(fixture)
        if not meta.is_file():
            problems.append(_missing_sidecar(relative, meta.name))
            continue
        try:
            sidecar = load_sidecar(meta)
        except SidecarError as exc:
            problems.append(f"{relative}: {exc}")
            continue
        body = fixture.read_bytes()
        if sidecar.declared:
            declared += 1
        else:
            verified += 1
            if hashlib.sha256(body).hexdigest() == sidecar.capture_sha256:
                problems.append(
                    f"{relative}: an unchanged copy of its capture. A fixture keeps the "
                    f"capture's structure and invents every value."
                )
        text = body.decode("utf-8", errors="replace")
        rest = sidecar_scannable_text(sidecar)
        for name in sidecar.flagged_names:
            if name in text:
                problems.append(
                    f"{relative}: contains the flagged name {name!r}, a cookie or token "
                    f"name the capture's digest recorded."
                )
            if name in rest:
                problems.append(
                    f"{relative.parent / meta.name}: carries the flagged name {name!r} "
                    f"outside flagged_names."
                )
    return FixturesTreeReport(problems=tuple(problems), verified=verified, declared=declared)


def fixtures_are_sanitised(tree: Path | str) -> Any:
    """An autouse, session-scoped fixture that fails the run unless every fixture under
    *tree* passes :func:`check_fixtures_tree`.

    A generated ``tests/conftest.py`` sets
    ``sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)``; the assignment is
    what registers it. It prints :attr:`FixturesTreeReport.summary` once per run,
    and on a problem it fails every test with the list of problems.
    """
    root = Path(tree)

    @pytest.fixture(scope="session", autouse=True)
    def _sanitised(request: pytest.FixtureRequest) -> None:
        report = check_fixtures_tree(root)
        reporter = request.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(report.summary)
        if report.problems:
            listing = "\n".join(f"  {problem}" for problem in report.problems)
            pytest.fail(f"fixtures_are_sanitised:\n{listing}", pytrace=False)

    return _sanitised
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graftpunk_testing.py tests/unit/test_testing_sidecar.py tests/unit/test_contracts.py -q`
Expected: PASS. `test_contracts.py` is in the run because its scan covers `plugin.py`'s new message, which spells the schema through `current_schema`.

- [ ] **Step 5: Commit**

```bash
git add src/graftpunk/testing/sidecar.py src/graftpunk/testing/plugin.py tests/unit/test_graftpunk_testing.py tests/unit/test_testing_sidecar.py
git commit -m "feat(testing): fixtures_are_sanitised checks every committed fixture against its sidecar"
```

---

### Task 2: `PROJECT_REQUIREMENTS`, the one binding predicate and assembler, and the conftest that carries them

**Files:**
- Modify: `src/graftpunk/devtools/scaffold/policy.py` (new `CONFTEST_PATH`, `ProjectRequirement`, `PROJECT_REQUIREMENTS`; `FIXTURES_PLACEHOLDER` re-exported from `graftpunk.testing.sidecar`; `__all__`)
- Modify: `src/graftpunk/devtools/scaffold/project.py:34` (the `PLUGINS_ENTRY_POINT_GROUP = "graftpunk.plugins"` assignment, replaced by an import of `PLUGINS_GROUP` from `graftpunk.plugins`; the `.gitkeep` literal of its conflict exemption read from policy)
- Modify: `src/graftpunk/devtools/scaffold/pyproject_edit.py` (`with_entry_point`'s table lookup and refusal message read `PLUGINS_GROUP`; at `7bd9604` the lookup literal is `pyproject_edit.py:53`, inside the function foundations Task 10 rewrote)
- Modify: `src/graftpunk/devtools/scaffold/pysrc.py` (new `binds_name`, `ImportPlacementError`, `with_import`, `Binding`, `with_bindings`, and the private `_is_stdlib`, `_isort_name_key`, `_import_block_lines`, `_target_names`, `_imported_module`, `_joined`; `__all__`)
- Modify: `src/graftpunk/devtools/scaffold/render.py` (`_render_conftest`; the rendered `pyproject.toml`'s entry-point table, `render.py:1000` at `7bd9604`, reads `PLUGINS_GROUP`; `_PLUGINS_MODULE`; the conftest and `.gitkeep` keys of `render()`)
- Modify: `tests/unit/test_scaffold_render.py` (`TestRenderNewProject.test_conftest_is_two_declarations`), `tests/unit/test_scaffold_cli.py` (`test_generated_tests_pass_against_a_generated_fixture_and_sidecar`)
- Modify: `docs/PLUGIN_DEVELOPMENT.md` ("Keep the developer's own environment out of the tests")
- Test: `tests/unit/test_scaffold_policy.py`, `tests/unit/test_scaffold_pysrc.py`, `tests/unit/test_scaffold_render.py`

**Interfaces:**
- Consumes: `fixtures_are_sanitised` (Task 1); `policy.TESTS_DIR`, `policy.FIXTURES_TREE` (foundations Task 9); `pysrc.import_lines(module, *names)` (foundations Tasks 1 and 11).
- Produces: `policy.CONFTEST_PATH: Final = f"{TESTS_DIR}conftest.py"`; `policy.FIXTURES_PLACEHOLDER` (the name `graftpunk.testing.sidecar` owns, re-exported); `@dataclass(frozen=True) class ProjectRequirement(path: str, name: str, statement: str, imports: tuple[tuple[str, str], ...] = ())` with property `key -> str` (`f"{path}:{name}"`); `policy.PROJECT_REQUIREMENTS: Final[tuple[ProjectRequirement, ...]]` (two entries, both `CONFTEST_PATH`: `FIXTURES_TREE` and `sanitised_fixtures`). In `pysrc`: `binds_name(tree: ast.Module, name: str) -> bool`, the one binding predicate; `class ImportPlacementError(ValueError)`; `with_import(text: str, module: str, name: str) -> str`, which merges into an existing same-module from-import or places a new line into the shapes generated files have (imports contiguous at the top, after a docstring and any `__future__` import, in at most two isort sections: the standard library, then everything else) and raises `ImportPlacementError`, whose message says to add the import by hand and run `ruff check --fix`, for anything else; `class Binding(Protocol)` with read-only `statement: str` and `imports: tuple[tuple[str, str], ...]` (a `ProjectRequirement` is one); `with_bindings(text: str, bindings: Sequence[Binding]) -> str`, the one assembler and the one blank-line rule for statements added to a module. The entry-point group has one owner, the runtime's `graftpunk.plugins.PLUGINS_GROUP`: `project.py`, `pyproject_edit.py`, and `render.py` import it here, Task 3's reader imports it, and policy never does. Task 3's reader decides presence with `binds_name`; Task 6 merges imports with `with_import`; Task 7 applies requirements with `with_bindings`; Tasks 3, 7, and 8 read `policy.PROJECT_REQUIREMENTS`.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_scaffold_policy.py`, extend the policy import at the top to `CONFTEST_PATH, FIXTURES_PLACEHOLDER, FIXTURES_TREE, PROJECT_REQUIREMENTS, TESTS_DIR, ProjectRequirement, fixtures_root` (parenthesised, one name per line), and append the tests below. `test_policy_imports_nothing_that_touches_the_filesystem` needs no change: it is a denylist of filesystem-capable modules (foundations Task 9), and the sidecar owner, from which policy takes one constant, is not on it.

```python
class TestProjectRequirements:
    def test_the_conftest_binds_the_tree_and_the_check(self) -> None:
        assert CONFTEST_PATH == f"{TESTS_DIR}conftest.py"
        assert [(r.path, r.name) for r in PROJECT_REQUIREMENTS] == [
            (CONFTEST_PATH, "FIXTURES_TREE"),
            (CONFTEST_PATH, "sanitised_fixtures"),
        ]

    def test_each_statement_binds_its_own_name(self) -> None:
        for requirement in PROJECT_REQUIREMENTS:
            assert requirement.statement.startswith(f"{requirement.name} = ")

    def test_the_tree_statement_names_the_policy_tree(self) -> None:
        tree = next(r for r in PROJECT_REQUIREMENTS if r.name == "FIXTURES_TREE")
        assert tree.statement == 'FIXTURES_TREE = Path(__file__).parent / "fixtures"'
        assert FIXTURES_TREE == "tests/fixtures/"

    def test_the_key(self) -> None:
        assert ProjectRequirement(path="a.py", name="x", statement="x = 1").key == "a.py:x"


def test_the_placeholder_is_the_testing_layers() -> None:
    from graftpunk.testing import sidecar

    assert FIXTURES_PLACEHOLDER is sidecar.FIXTURES_PLACEHOLDER


def _group_literals(tree: ast.Module, group: str) -> list[int]:
    """The lines where *tree* spells *group* in one of the group's two uses, outside
    docstrings: inside a TOML table header (a literal holding the group in double
    quotes), or as the key of an entry-point lookup (``.get(group)`` or
    ``group=group``). The same text as an import path is not counted."""
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
    }
    lines: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and f'"{group}"' in node.value
        ):
            lines.append(node.lineno)
        elif isinstance(node, ast.Call):
            lookup = isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            keys = [*(node.args[:1] if lookup else [])]
            keys += [k.value for k in node.keywords if k.arg == "group"]
            lines += [k.lineno for k in keys if isinstance(k, ast.Constant) and k.value == group]
    return lines


def test_the_entry_point_group_is_spelled_in_one_module() -> None:
    """The group is the runtime's (graftpunk.plugins.PLUGINS_GROUP), and its two uses,
    a TOML table header and an entry-point lookup, read that constant everywhere.
    The renderer's _PLUGINS_MODULE, the package a generated plugin imports from,
    is the same text as an import path, a different fact, and is not counted."""
    import graftpunk
    from graftpunk.plugins import PLUGINS_GROUP

    assert PLUGINS_GROUP == "graftpunk.plugins"
    package = Path(graftpunk.__file__).parent
    spelled = [
        f"{path.relative_to(package).as_posix()}:{line}"
        for path in sorted(package.rglob("*.py"))
        for line in _group_literals(ast.parse(path.read_text(encoding="utf-8")), PLUGINS_GROUP)
    ]
    assert spelled == []
```

In `tests/unit/test_scaffold_pysrc.py`, add `import subprocess`, `import sys`, `from dataclasses import dataclass`, `import pytest`, and `from graftpunk.devtools.scaffold.pysrc import ImportPlacementError, binds_name, with_bindings, with_import` to the imports, and append:

```python
class TestBindsName:
    """The one binding predicate: the project reader decides a requirement's presence
    with it, and with_import decides whether an import is needed."""

    @pytest.mark.parametrize(
        "source",
        [
            "from pathlib import Path\n",
            "from elsewhere import thing as Path\n",
            "import Path\n",
            "Path = 1\n",
            "Path, other = 1, 2\n",
            "Path: type = object\n",
            "Path = (\n    object\n)\n",
            "def Path() -> None:\n    pass\n",
            "class Path:\n    pass\n",
        ],
    )
    def test_a_module_level_binding_binds(self, source: str) -> None:
        assert binds_name(ast.parse(source), "Path")

    @pytest.mark.parametrize(
        "source",
        [
            "",
            "from pathlib import *\n",
            "import pathlib.Path\n",
            "Path: type\n",
            "if True:\n    from pathlib import Path\n",
            "try:\n    from pathlib import Path\nexcept ImportError:\n    pass\n",
            "def f() -> None:\n    Path = 1\n",
            "class C:\n    Path = 1\n",
        ],
    )
    def test_a_star_a_conditional_or_a_nested_binding_does_not(self, source: str) -> None:
        assert not binds_name(ast.parse(source), "Path")


_OLD_CONFTEST = (
    "from graftpunk.testing.plugin import site_env_scrubber\n"
    "\n"
    'scrub_site_env = site_env_scrubber("MYSHOP_")\n'
)


class TestWithImport:
    def test_a_name_merges_into_the_same_modules_import(self) -> None:
        result = with_import(_OLD_CONFTEST, "graftpunk.testing.plugin", "fixtures_are_sanitised")
        assert result.splitlines()[0] == (
            "from graftpunk.testing.plugin import fixtures_are_sanitised, site_env_scrubber"
        )

    def test_a_stdlib_import_goes_first_with_a_blank_line_after(self) -> None:
        result = with_import(_OLD_CONFTEST, "pathlib", "Path")
        assert result.splitlines()[:3] == [
            "from pathlib import Path",
            "",
            "from graftpunk.testing.plugin import site_env_scrubber",
        ]

    def test_a_name_already_bound_leaves_the_text_alone(self) -> None:
        assert (
            with_import(_OLD_CONFTEST, "graftpunk.testing.plugin", "site_env_scrubber")
            == _OLD_CONFTEST
        )
        aliased = "from elsewhere import thing as Path\n"
        assert with_import(aliased, "pathlib", "Path") == aliased

    def test_a_module_with_no_imports_gets_one_after_its_docstring(self) -> None:
        result = with_import('"""Doc."""\n\nx = 1\n', "pathlib", "Path")
        assert result == '"""Doc."""\n\nfrom pathlib import Path\n\nx = 1\n'

    @pytest.mark.parametrize(
        "text",
        [
            "x = 1\nfrom os import sep\n",
            "from . import sibling\n",
        ],
    )
    def test_a_shape_it_does_not_place_into_is_refused_with_the_ruff_hint(self, text: str) -> None:
        with pytest.raises(ImportPlacementError, match="ruff check --fix"):
            with_import(text, "pathlib", "Path")

    def test_a_merge_needs_no_known_shape(self) -> None:
        """Merging into an existing same-module import is always safe, so an odd
        layout elsewhere does not stop it."""
        text = "from pathlib import PurePath\nx = 1\nfrom os import sep\n"
        assert with_import(text, "pathlib", "Path").splitlines()[0] == (
            "from pathlib import Path, PurePath"
        )


_HAND_WRITTEN_PLUGIN = '''\
"""myshop plugin, written by hand."""

from __future__ import annotations

from graftpunk.plugins import SitePlugin, command


class MyshopPlugin(SitePlugin):
    site_name = "myshop"

    @command(help="List orders", params=[PluginParamSpec.option("page", type=int)])
    def orders(self, ctx: CommandContext, page: int | None = None) -> dict:
        return ctx.request_json("GET", "/api/orders", params={"page": page})
'''

# The lint a generated project's own pyproject.toml declares.
_PROJECT_RUFF = (
    '[tool.ruff]\nline-length = 100\n\n[tool.ruff.lint]\nselect = ["E", "F", "I", "UP", "B"]\n'
)


def test_a_merge_into_a_hand_written_module_passes_the_projects_ruff(tmp_path: Path) -> None:
    """The merge path re-renders an existing from-import in isort's order, and a
    hand-written module reaches it through gp plugin add-command; the project's
    own ruff has to agree with the result."""
    text = with_import(_HAND_WRITTEN_PLUGIN, "graftpunk.plugins", "CommandContext")
    text = with_import(text, "graftpunk.plugins", "PluginParamSpec")
    (tmp_path / "pyproject.toml").write_text(_PROJECT_RUFF)
    (tmp_path / "plugin.py").write_text(text)
    for argv in (["check", "plugin.py"], ["format", "--check", "plugin.py"]):
        result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [sys.executable, "-m", "ruff", *argv], cwd=tmp_path, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stdout + result.stderr


@dataclass(frozen=True)
class _Statement:
    statement: str
    imports: tuple[tuple[str, str], ...] = ()


class TestWithBindings:
    """The one assembler: the renderer and gp plugin upgrade both add statements
    through it, so there is one blank-line rule."""

    def test_empty_text_gets_one_import_block_then_the_statements(self) -> None:
        result = with_bindings(
            "", [_Statement("x = Path()", (("pathlib", "Path"),)), _Statement("y = 1")]
        )
        assert result == "from pathlib import Path\n\nx = Path()\ny = 1\n"

    def test_empty_text_and_no_imports_is_just_the_statements(self) -> None:
        assert with_bindings("", [_Statement("y = 1")]) == "y = 1\n"

    def test_a_statement_after_a_definition_gets_two_blank_lines(self) -> None:
        result = with_bindings(
            "def f() -> int:\n    return 1\n", [_Statement("y = 1"), _Statement("z = 2")]
        )
        assert result == "def f() -> int:\n    return 1\n\n\ny = 1\nz = 2\n"

    def test_imports_merge_into_the_existing_block(self) -> None:
        pair = ("graftpunk.testing.plugin", "fixtures_are_sanitised")
        result = with_bindings(_OLD_CONFTEST, [_Statement("x = fixtures_are_sanitised", (pair,))])
        assert result.splitlines()[0] == (
            "from graftpunk.testing.plugin import fixtures_are_sanitised, site_env_scrubber"
        )
        assert result.endswith(
            'scrub_site_env = site_env_scrubber("MYSHOP_")\nx = fixtures_are_sanitised\n'
        )
```

In `tests/unit/test_scaffold_render.py`, replace `test_conftest_is_two_declarations` with:

```python
    def test_the_conftest_carries_the_scrubber_and_every_requirement(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
        )
        assert render(spec)["tests/conftest.py"] == (
            "from pathlib import Path\n"
            "\n"
            "from graftpunk.testing.plugin import fixtures_are_sanitised, site_env_scrubber\n"
            "\n"
            'scrub_site_env = site_env_scrubber("MYSHOP_")\n'
            'FIXTURES_TREE = Path(__file__).parent / "fixtures"\n'
            "sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)\n"
        )
```

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_policy.py tests/unit/test_scaffold_pysrc.py tests/unit/test_scaffold_render.py::TestRenderNewProject -q`
Expected: FAIL (`ImportError: cannot import name 'CONFTEST_PATH'` and `cannot import name 'binds_name'`).

- [ ] **Step 3: Declare the requirements**

In `src/graftpunk/devtools/scaffold/policy.py`, add `from dataclasses import dataclass` and `from graftpunk.testing.sidecar import FIXTURES_PLACEHOLDER`, set `__all__ = ["CONFTEST_PATH", "FIXTURES_PLACEHOLDER", "FIXTURES_TREE", "PROJECT_REQUIREMENTS", "TESTS_DIR", "ProjectRequirement", "fixtures_root"]`, and append:

```python
CONFTEST_PATH: Final = f"{TESTS_DIR}conftest.py"
"""The generated project's shared conftest, project-relative. Written once, for the
first plugin of a project; a suite member added later shares it."""

@dataclass(frozen=True)
class ProjectRequirement:
    """A module-level name a project file must bind, and the statement that binds it.

    Presence is decided structurally by the project reader, with
    ``pysrc.binds_name``: the file binds the name at module level. The renderer
    emits every requirement in a new project, ``gp plugin upgrade`` applies the
    ones a project lacks, and ``gp plugin check`` reports them; the renderer and
    the migrator both add the statement through ``pysrc.with_bindings``.

    Only a Python file and a module-level binding. A ``pyproject.toml`` key is not
    this format's business (it needs a TOML edit through ``pyproject_edit.py``),
    and neither is a gate entry (that is the README regenerated from the gate).
    """

    path: str
    name: str
    statement: str
    # (module, name) pairs the statement reads, merged into the file's imports.
    imports: tuple[tuple[str, str], ...] = ()

    @property
    def key(self) -> str:
        """``"<path>:<name>"``: how the project view reports this requirement."""
        return f"{self.path}:{self.name}"


# The fixtures tree as the conftest sees it: the conftest lives in TESTS_DIR.
_TREE_FROM_CONFTEST = FIXTURES_TREE.removeprefix(TESTS_DIR).strip("/")

PROJECT_REQUIREMENTS: Final[tuple[ProjectRequirement, ...]] = (
    ProjectRequirement(
        path=CONFTEST_PATH,
        name="FIXTURES_TREE",
        statement=f'FIXTURES_TREE = Path(__file__).parent / "{_TREE_FROM_CONFTEST}"',
        imports=(("pathlib", "Path"),),
    ),
    ProjectRequirement(
        path=CONFTEST_PATH,
        name="sanitised_fixtures",
        statement="sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)",
        imports=(("graftpunk.testing.plugin", "fixtures_are_sanitised"),),
    ),
)
"""What every generated project's files must bind, in the order they are applied."""
```

The entry-point group keeps its one owner, the runtime: `graftpunk.plugins.PLUGINS_GROUP` (`src/graftpunk/plugins/__init__.py:176`). In `src/graftpunk/devtools/scaffold/project.py`, delete the line `PLUGINS_ENTRY_POINT_GROUP = "graftpunk.plugins"`, add `from graftpunk.plugins import PLUGINS_GROUP` and `from graftpunk.devtools.scaffold.policy import FIXTURES_PLACEHOLDER`, rename its two uses of `PLUGINS_ENTRY_POINT_GROUP` to `PLUGINS_GROUP`, and in the conflict exemption for an existing `.gitkeep` change `relative.endswith("/.gitkeep")` to `relative.endswith(f"/{FIXTURES_PLACEHOLDER}")`. In `src/graftpunk/devtools/scaffold/pyproject_edit.py`, add `from graftpunk.plugins import PLUGINS_GROUP`, and in `with_entry_point` change `.get("graftpunk.plugins", {})` to `.get(PLUGINS_GROUP, {})` and the refusal's `f'{pyproject_path} has no [project.entry-points."graftpunk.plugins"] table I can '` to `f'{pyproject_path} has no [project.entry-points."{PLUGINS_GROUP}"] table I can '`. Docstrings keep the group's name as prose.

- [ ] **Step 4: Add the binding predicate, the import merger, and the assembler to `pysrc.py`**

In `src/graftpunk/devtools/scaffold/pysrc.py`, add `import ast`, `import sys`, `from collections.abc import Iterable, Sequence`, and `from typing import Protocol`; add `"Binding"`, `"ImportPlacementError"`, `"binds_name"`, `"with_bindings"`, and `"with_import"` to `__all__`; and append:

```python
def _is_stdlib(module: str) -> bool:
    """Whether *module* is in the standard library, the first isort section."""
    return module.split(".")[0] in sys.stdlib_module_names


def _isort_name_key(name: str) -> tuple[int, str]:
    """isort's order-by-type within one import: CONSTANTS, then Classes, then the rest.
    An aliased name (``a as b``) sorts by the imported name."""
    bare = name.split(" as ")[0]
    if bare.isupper():
        return (0, bare)
    if bare[:1].isupper():
        return (1, bare)
    return (2, bare)


def _import_block_lines(imports: Iterable[tuple[str, str]]) -> list[str]:
    """``from module import names`` lines for (module, name) pairs, grouped the way isort
    groups them: the standard library first, then the rest, a blank line between
    the two, modules sorted and each module's names in isort's order."""
    by_module: dict[str, set[str]] = {}
    for module, name in imports:
        by_module.setdefault(module, set()).add(name)
    sections = (
        sorted(m for m in by_module if _is_stdlib(m)),
        sorted(m for m in by_module if not _is_stdlib(m)),
    )
    lines: list[str] = []
    for section in sections:
        if not section:
            continue
        if lines:
            lines.append("")
        for module in section:
            lines.extend(import_lines(module, *sorted(by_module[module], key=_isort_name_key)))
    return lines


def _target_names(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return set().union(*(_target_names(e) for e in target.elts))
    return set()


def binds_name(tree: ast.Module, name: str) -> bool:
    """Whether *tree* binds *name* at module level: the one binding predicate.

    A plain import, an aliased import, an assignment (tuple targets included), an
    annotated assignment with a value, a ``def``, and a ``class`` bind; a line
    wrapped in parentheses binds like any other. A star import does not, and
    neither does a conditional import or anything inside an ``if``, a ``try``,
    or a function: only the module's top-level statements count.
    """
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name != "*" and (alias.asname or alias.name.split(".")[0]) == name:
                    return True
        elif isinstance(node, ast.Assign):
            if any(name in _target_names(t) for t in node.targets):
                return True
        elif isinstance(node, ast.AnnAssign):
            if node.value is not None and name in _target_names(node.target):
                return True
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return True
    return False


def _imported_module(node: ast.Import | ast.ImportFrom) -> str:
    if isinstance(node, ast.ImportFrom):
        return node.module or ""
    return node.names[0].name


def _joined(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


class ImportPlacementError(ValueError):
    """An import :func:`with_import` will not place: the module's imports are not in a
    shape it knows. Nothing is changed; the message says what to do instead."""


def with_import(text: str, module: str, name: str) -> str:
    """*text* with ``from {module} import {name}`` in its module-level imports, placed
    the way isort places a from-import.

    Its contract is the shapes generated files have and nothing wider: the merge
    below, which a hand-written module reaches too (a test runs the project's own
    ruff over one), and a new line in the layout ``gp plugin new`` writes. It is
    not a general import sorter. Widening the shapes it places into is the point
    to stop placing imports here and run ``ruff check --fix --select I`` over the
    edited text instead.

    Unchanged when :func:`binds_name` says the module already binds *name*.
    Otherwise merged into an existing ``from {module} import ...``, re-rendered in
    isort's name order, whatever the rest of the module looks like. Failing that,
    placed only into the shapes generated files have: module-level imports
    contiguous at the top (after a docstring and any ``__future__`` import) and in
    at most two isort sections, the standard library and then everything else.
    The line goes among its section's imports in module order, or, for a module
    with no imports, after its docstring. A project whose own code forms a third
    section (a first-party package isort sorts apart) gets the line in the second
    section, which its ``ruff check --fix`` then moves.

    Raises:
        ImportPlacementError: An import follows other code, or a relative import
            is present; the message says to add the import by hand and run
            ``ruff check --fix``.
    """
    tree = ast.parse(text)
    if binds_name(tree, name):
        return text
    lines = text.splitlines()
    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    for node in imports:
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module == module
            and all(alias.name != "*" for alias in node.names)
        ):
            names = [
                alias.name if alias.asname is None else f"{alias.name} as {alias.asname}"
                for alias in node.names
            ]
            merged = import_lines(module, *sorted([*names, name], key=_isort_name_key))
            return _joined(
                lines[: node.lineno - 1] + merged + lines[node.end_lineno or node.lineno :]
            )
    new_line = f"from {module} import {name}"
    first = next(
        (i for i, n in enumerate(tree.body) if isinstance(n, (ast.Import, ast.ImportFrom))), 0
    )
    # At the top: nothing but a docstring before the first import, and no other
    # statement between the imports.
    at_top = first <= 1 and all(
        isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) for n in tree.body[:first]
    )
    contiguous = tree.body[first : first + len(imports)] == imports
    relative = any(isinstance(n, ast.ImportFrom) and n.level for n in imports)
    if not (at_top and contiguous) or relative:
        raise ImportPlacementError(
            f"cannot place {new_line!r}: the module's imports are not all at its top in "
            f"the shape generated files have. Add the line by hand, then run "
            f"`ruff check --fix` to sort it."
        )
    body = [n for n in imports if _imported_module(n) != "__future__"]
    same_section = [n for n in body if _is_stdlib(_imported_module(n)) == _is_stdlib(module)]
    later = [n for n in same_section if _imported_module(n) > module]
    if later:
        at, insert = later[0].lineno - 1, [new_line]
    elif same_section:
        at, insert = same_section[-1].end_lineno or same_section[-1].lineno, [new_line]
    elif body and _is_stdlib(module):
        at, insert = body[0].lineno - 1, [new_line, ""]
    elif body:
        at, insert = body[-1].end_lineno or body[-1].lineno, ["", new_line]
    else:
        leading = [
            n
            for n in tree.body[:2]
            if (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
            or (isinstance(n, ast.ImportFrom) and n.module == "__future__")
        ]
        at = (leading[-1].end_lineno or leading[-1].lineno) if leading else 0
        insert = ["", new_line] if at else [new_line, ""]
    return _joined(lines[:at] + insert + lines[at:])


class Binding(Protocol):
    """A module-level statement and the ``(module, name)`` imports it reads.

    ``policy.ProjectRequirement`` is one; this module imports nothing from
    graftpunk, so it names the shape rather than the class."""

    @property
    def statement(self) -> str: ...

    @property
    def imports(self) -> tuple[tuple[str, str], ...]: ...


def with_bindings(text: str, bindings: Sequence[Binding]) -> str:
    """*text* with each binding's imports merged and its statement appended, in order.

    The one assembler for statements a generator or a migrator adds to a module,
    so the two cannot disagree about layout. The blank-line rule, stated once: a
    statement follows the one before it on the next line, and follows a ``def``
    or a ``class`` after two blank lines, which is what ``ruff format`` keeps. Text
    with nothing in it gets the imports as one isort block, a blank line, and then
    the statements.
    """
    if not text.strip():
        imports = [pair for binding in bindings for pair in binding.imports]
        head = [*_import_block_lines(imports), ""] if imports else []
        return _joined([*head, *(binding.statement for binding in bindings)])
    for binding in bindings:
        for module, name in binding.imports:
            text = with_import(text, module, name)
        body = ast.parse(text).body
        after_definition = bool(body) and isinstance(
            body[-1], (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        )
        separator = "\n\n\n" if after_definition else "\n"
        text = text.rstrip("\n") + separator + binding.statement + "\n"
    return text
```

- [ ] **Step 5: Render the conftest through the assembler**

In `src/graftpunk/devtools/scaffold/render.py`, import `with_bindings` from `pysrc` (beside `import_lines`), and replace `_render_conftest` with:

```python
def _render_conftest(spec: ScaffoldSpec) -> str:
    """The site-environment scrubber, then every ``PROJECT_REQUIREMENTS`` statement for
    the conftest, added by ``pysrc.with_bindings`` exactly as ``gp plugin upgrade``
    adds them to an existing conftest, so the two outputs are byte-identical.

    The imports alone plus assignments: naming the module in ``pytest_plugins`` as
    well would make pytest try to rewrite assertions in a module the import has
    already loaded, which it reports as a warning on every run (final fix wave,
    2026-09-12). Each assignment is what registers its fixture.
    """
    scrubber = [
        *import_lines("graftpunk.testing.plugin", "site_env_scrubber"),
        "",
        f'scrub_site_env = site_env_scrubber("{_env_prefix_for(spec.name)}")',
    ]
    requirements = [r for r in policy.PROJECT_REQUIREMENTS if r.path == policy.CONFTEST_PATH]
    return with_bindings("\n".join(scrubber) + "\n", requirements)
```

The conftest path and the placeholder have one spelling each: in `render()`'s new-project dict, the key `f"{policy.TESTS_DIR}conftest.py"` (foundations Task 9) becomes `policy.CONFTEST_PATH`, and in both dicts `f"{fixtures_root_for(spec)}.gitkeep"` becomes `f"{fixtures_root_for(spec)}{policy.FIXTURES_PLACEHOLDER}"`.

The entry-point group and the package a generated plugin imports from are two facts that happen to share a spelling. The group is read from its owner; the import path is the renderer's own plain constant. Add `from graftpunk.plugins import PLUGINS_GROUP` to `render.py`'s imports; in `_render_pyproject` change the line `'[project.entry-points."graftpunk.plugins"]\n'` to `f'[project.entry-points."{PLUGINS_GROUP}"]\n'`; and add beside `_MUTATING_METHODS`:

```python
# The package a generated plugin imports its API from: an import path. It is the
# same text as the entry-point group (graftpunk.plugins.PLUGINS_GROUP), but a
# different fact, and the group test counts only the group's own uses.
_PLUGINS_MODULE = "graftpunk.plugins"
```

and change `import_lines("graftpunk.plugins", *plugins_names)` in `_render_plugin_module` (foundations Task 11) to `import_lines(_PLUGINS_MODULE, *plugins_names)`.

- [ ] **Step 6: Give the generated-suite test a sidecar**

A fixture without a sidecar now fails the generated suite by design. In `tests/unit/test_scaffold_cli.py`, add `from graftpunk.testing.sidecar import Sidecar, sidecar_text`, and in `test_generated_tests_pass_against_a_generated_fixture_and_sidecar`, after the line that writes `get_orders_{order_id}.json`, add:

```python
        # A hand-derived fixture with no capture behind it: its sidecar declares so.
        (fixtures_dir / "get_orders_{order_id}.json.meta.json").write_text(
            sidecar_text(Sidecar(status=200, content_type="application/json"))
        )
```

and after the `warnings summary` assertion, add:

```python
        assert "1 accepted on declaration" in pytest_result.stdout, pytest_result.stdout
```

- [ ] **Step 7: Describe the conftest in the guide**

In `docs/PLUGIN_DEVELOPMENT.md`, "Keep the developer's own environment out of the tests", replace the sentence "A generated `tests/conftest.py` is an import and an assignment:" and the Python block after it with:

````markdown
A generated `tests/conftest.py` is two imports and three assignments:

```python
from pathlib import Path

from graftpunk.testing.plugin import fixtures_are_sanitised, site_env_scrubber

scrub_site_env = site_env_scrubber("MYSHOP_")
FIXTURES_TREE = Path(__file__).parent / "fixtures"
sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)
```
````

and append to the end of that section: "The last two lines wire in the fixtures check described in [Deriving a fixture from a capture](#deriving-a-fixture-from-a-capture). `FIXTURES_TREE` is the whole fixtures directory, so a plugin added to the suite later is covered without editing this file."

- [ ] **Step 8: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_policy.py tests/unit/test_scaffold_pysrc.py tests/unit/test_scaffold_render.py tests/unit/test_scaffold_cli.py tests/unit/test_scaffold_project.py tests/unit/test_scaffold_pyproject_edit.py tests/unit/test_plugin_development_guide.py -q`
Expected: PASS, including `TestGeneratedProjectPassesItsOwnGate.test_new_project_is_ruff_clean`, which runs the generated project's own ruff over the new conftest, and foundations Task 1's export test, which now finds `with_bindings` imported by `render.py` and listed in `pysrc.__all__`.

- [ ] **Step 9: Commit**

```bash
git add src/graftpunk/devtools/scaffold/policy.py src/graftpunk/devtools/scaffold/project.py src/graftpunk/devtools/scaffold/pyproject_edit.py src/graftpunk/devtools/scaffold/pysrc.py src/graftpunk/devtools/scaffold/render.py tests/unit/test_scaffold_policy.py tests/unit/test_scaffold_pysrc.py tests/unit/test_scaffold_render.py tests/unit/test_scaffold_cli.py docs/PLUGIN_DEVELOPMENT.md
git commit -m "feat(scaffold): PROJECT_REQUIREMENTS declares the conftest wiring, and the generated conftest carries it"
```

---

### Task 3: `devtools/plugin_project.py`, the one reader

**Files:**
- Create: `src/graftpunk/devtools/plugin_project.py`
- Modify: `src/graftpunk/har/naming.py` (`to_cli_name`, moved in; new `registered_name`; `import re`; `__all__`; docstring)
- Modify: `src/graftpunk/plugins/cli_plugin.py` (`def _to_cli_name(`, line 775 at `7bd9604`, about 780 after foundations Task 11: moved to `har/naming.py` as the public `to_cli_name`, its three calls in `command` renamed and served by an import from there, and the two that honour a `name=` pin, lines 846 and 869 at `7bd9604`, rewritten to call `registered_name`), `src/graftpunk/client.py` (its import and five calls), `tests/unit/test_cli_plugin.py` (its import and `TestToCliName`); no alias is kept
- Modify: `src/graftpunk/devtools/scaffold/project.py` (`module_name_for` imported from policy)
- Modify: `src/graftpunk/devtools/scaffold/policy.py` (new `GP_FILL_MARKER`; `module_name_for` moved in from `render.py`; `__all__`)
- Modify: `src/graftpunk/devtools/scaffold/render.py` (`module_name_for` deleted and dropped from `__all__`, every call spelled `policy.module_name_for`; every string literal holding `GP-FILL` built from `GP_FILL_MARKER`)
- Test: `tests/unit/test_plugin_project.py`, `tests/unit/test_devtools_errors.py`, `tests/unit/test_scaffold_policy.py`, `tests/unit/test_scaffold_render.py`, `tests/unit/test_cli_plugin.py`, `tests/unit/test_har_naming.py`

**Interfaces:**
- Consumes: `graftpunk.plugins.PLUGINS_GROUP` (existing, `src/graftpunk/plugins/__init__.py:176`); `policy.PROJECT_REQUIREMENTS`, `ProjectRequirement`, `pysrc.binds_name` (Task 2); `policy.fixtures_root` (foundations Task 9); `graftpunk.devtools.errors.DevtoolsRefusal` and `ScaffoldWriteError`, with `write.ChangeConflictError` and `write.InvalidChangeError` already subclassing `DevtoolsRefusal` (foundations Task 10).
- Produces: `policy.GP_FILL_MARKER: Final = "GP-FILL"`; `policy.module_name_for(name: str) -> str` (moved; `render` no longer has the name, and every importer, `project.py` and `tests/unit/test_scaffold_render.py` included, takes it from policy); `graftpunk.har.naming.to_cli_name(name: str) -> str`, in the dependency-free naming module that already owns the naming stems, which `cli_plugin`, `client.py`, the reader, and `selection.py` (Task 5) import from there; `graftpunk.har.naming.registered_name(name_pin: str | None, identifier: str) -> str`, the one owner of "the `name=` pin, else `to_cli_name(identifier)`", which the `command` decorator's two pinnable sites, `CommandView.cli_name`, and `PlannedCommand.registered_name` (Task 5) call; no `_to_cli_name` left in `src/` or `tests/` (historical design docs under `docs/plans/` keep the old name); `PluginProjectError` and `NotAPluginProjectError` join the refusals under `DevtoolsRefusal` (`CommandSelectionError`, `CommandInsertError`, and `UpgradeRefusedError` join in Tasks 5 to 7), each keeping its existing second base. In `plugin_project`: `DirectoryKind = Literal["empty", "plugin", "foreign"]`; `@dataclass(frozen=True) class Span(start: int, end: int)` (1-based, inclusive); `@dataclass(frozen=True) class CommandView(method: str, span: Span, keywords: Mapping[str, str | None])` (a read-only `MappingProxyType`) with properties `endpoint -> str | None` and `cli_name -> str` (`registered_name(keywords.get("name"), method)`: the name the CLI registers); `@dataclass(frozen=True) class PluginView(entry_point: str, module_path: str, class_name: str, class_span: Span, site_name: str | None, base_url: str | None, commands: tuple[CommandView, ...], markers: tuple[int, ...], fixtures_root: str)`, where `entry_point` is the entry-point name the reader keys the plugin by (and derives `fixtures_root` from, through `module_name_for`): the one identity every consumer addresses a plugin by; `@dataclass(frozen=True) class PluginDefect(entry_point: str, module_path: str, message: str)`, a plugin whose module the reader parsed but that does not hold exactly one `SitePlugin` subclass; `RequirementState = Literal["bound", "unbound", "unreadable"]`; `@dataclass(frozen=True) class RequirementStatus(state: RequirementState, reason: str | None = None)`, where `unreadable` means the requirement's file does not parse and `reason` says why (the reader parses each distinct requirement file once, so every requirement in one file carries that file's one reason); `@dataclass(frozen=True) class ProjectView(directory: DirectoryKind, plugins: tuple[PluginView, ...], defects: tuple[PluginDefect, ...], requirements: Mapping[str, RequirementStatus])` (read-only; `requirements` is computed at read time) with methods `missing_requirements() -> tuple[ProjectRequirement, ...]`, the one owner of "which requirements does this project lack" (the `unbound` ones, reading `policy.PROJECT_REQUIREMENTS` at call time), and `unreadable_files() -> tuple[tuple[str, str], ...]`, the one owner of "which requirement files do not parse": one `(path, reason)` entry per file, in the order `PROJECT_REQUIREMENTS` first names it, however many requirements the file holds; `class PluginProjectError(DevtoolsRefusal, ValueError)`, raised only when the project cannot be read at all (invalid TOML, or a plugin module missing or not parsing), while a per-plugin structural defect is recorded in `defects` and an unparseable requirement file as `unreadable` instead; `class NotAPluginProjectError(DevtoolsRefusal, ValueError)`; `classify(root: Path) -> DirectoryKind`; `read_project(root: Path) -> ProjectView`; `require_plugin_project(root: Path) -> ProjectView`, the one owner of "this directory must be a plugin project", which reads *root* and raises `NotAPluginProjectError` naming the classification for anything but `plugin`. The reader imports neither `render.py` nor `write.py`. Task 4's `plugin_info.info_payload` reads the view and reports `entry_point`; Task 6 addresses the target plugin and its defect by `entry_point`; Tasks 6, 7, and 8 consume `require_plugin_project` (Task 8 turns its refusal into a finding); Tasks 7 and 8 call `missing_requirements()` and `unreadable_files()` and neither groups requirements by path (Task 7 refuses on the unreadable files, naming each once, Task 8 reports one finding per file, and Tasks 4 and 6 never look); Tasks 4 and 6 read `CommandView.cli_name`; Task 4 refuses on any defect, Task 6 refuses only when the plugin it targets is defective, Task 7 applies requirements whatever the plugins hold, and Task 8 lists each defect as a finding.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_plugin_project.py`:

```python
"""The one reader of a plugin project and its structural view (graft skill spec,
2026-09-21, "One owner for what plugin project is this directory")."""

from __future__ import annotations

import ast
import subprocess
import sys
from dataclasses import fields
from pathlib import Path
from types import MappingProxyType

import pytest

from graftpunk.devtools.plugin_project import (
    CommandView,
    NotAPluginProjectError,
    PluginDefect,
    PluginProjectError,
    PluginView,
    ProjectView,
    Span,
    classify,
    read_project,
    require_plugin_project,
)
from graftpunk.devtools.scaffold.policy import fixtures_root, module_name_for
from graftpunk.devtools.scaffold.project import write_scaffold
from graftpunk.devtools.scaffold.render import ScaffoldSpec, fixtures_root_for
from graftpunk.har.digest import DigestSource, Endpoint, RunDigest, ShapeNode


def _endpoint(template: str) -> Endpoint:
    return Endpoint(
        host="myshop.example",
        template=template,
        methods=("GET",),
        count=1,
        statuses=(200,),
        content_type="application/json",
        query_params={},
        body_params={},
        body_kind="none",
        shape=ShapeNode(kind="object", children={"id": ShapeNode(kind="number")}),
        custom_headers=(),
        examples=(),
    )


def _digest(*templates: str) -> RunDigest:
    return RunDigest(
        source=DigestSource(har_path=Path("network.har"), session="myshop", run_id="run-1"),
        primary_host="myshop.example",
        hosts={"myshop.example": 1},
        endpoints=tuple(_endpoint(t) for t in templates),
        login=(),
        login_forms=(),
        tokens=(),
        cookies=(),
        dropped={"static": 0, "third_party": 0, "error": 0, "other_scheme": 0},
    )


def _generate(root: Path, name: str = "myshop", *templates: str) -> None:
    write_scaffold(
        root,
        ScaffoldSpec(
            name=name,
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example",
            digest=_digest(*(templates or ("/api/orders", "/api/orders/{order_id}"))),
        ),
    )


_HAND_WRITTEN_PYPROJECT = """\
[project]
name = "graftpunk-myshop"

[project.entry-points."graftpunk.plugins"]
myshop = "graftpunk_myshop.plugin:MyshopPlugin"
"""

_HAND_WRITTEN_MODULE = """\
from graftpunk.plugins import CommandContext, SitePlugin, command


class MyshopPlugin(SitePlugin):
    site_name = "myshop"
    base_url = "https://myshop.example"

    @command(help="List orders")
    def orders(self, ctx: CommandContext) -> dict:
        return ctx.request_json("GET", "/api/orders")
"""


def _hand_written(root: Path, module: str = _HAND_WRITTEN_MODULE) -> Path:
    (root / "pyproject.toml").write_text(_HAND_WRITTEN_PYPROJECT)
    package = root / "src" / "graftpunk_myshop"
    package.mkdir(parents=True)
    (package / "plugin.py").write_text(module)
    return package / "plugin.py"


class TestClassify:
    def test_no_pyproject_is_empty(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("not a project")
        assert classify(tmp_path) == "empty"
        assert read_project(tmp_path) == ProjectView(
            directory="empty", plugins=(), defects=(), requirements={}
        )

    def test_a_pyproject_with_the_group_is_a_plugin(self, tmp_path: Path) -> None:
        _hand_written(tmp_path)
        assert classify(tmp_path) == "plugin"

    def test_a_pyproject_without_the_group_is_foreign(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "other"\n')
        assert classify(tmp_path) == "foreign"
        assert read_project(tmp_path).plugins == ()


class TestRequirePluginProject:
    """The one owner of "this directory must be a plugin project": insert, upgrade,
    and check all ask it, so they refuse with one type and one message."""

    def test_a_plugin_project_is_returned(self, tmp_path: Path) -> None:
        _hand_written(tmp_path)
        assert require_plugin_project(tmp_path) == read_project(tmp_path)

    @pytest.mark.parametrize("kind", ["empty", "foreign"])
    def test_anything_else_is_refused_naming_its_classification(
        self, tmp_path: Path, kind: str
    ) -> None:
        if kind == "foreign":
            (tmp_path / "pyproject.toml").write_text('[project]\nname = "other"\n')
        expected = rf"not a graftpunk plugin project \({kind}\)"
        with pytest.raises(NotAPluginProjectError, match=expected):
            require_plugin_project(tmp_path)


class TestTheView:
    def test_the_view_has_exactly_the_fields_its_section_lists(self) -> None:
        """Copied from the spec's list, so a second enumeration cannot drift from it."""
        assert [f.name for f in fields(ProjectView)] == [
            "directory",
            "plugins",
            "defects",
            "requirements",
        ]
        assert [f.name for f in fields(PluginDefect)] == ["entry_point", "module_path", "message"]
        assert [f.name for f in fields(PluginView)] == [
            "entry_point",
            "module_path",
            "class_name",
            "class_span",
            "site_name",
            "base_url",
            "commands",
            "markers",
            "fixtures_root",
        ]
        assert [f.name for f in fields(CommandView)] == ["method", "span", "keywords"]

    def test_a_generated_plugin_declares_every_endpoint(self, tmp_path: Path) -> None:
        _generate(tmp_path)
        (plugin,) = read_project(tmp_path).plugins
        assert plugin.entry_point == "myshop"
        assert plugin.module_path == "src/graftpunk_myshop/plugin.py"
        assert plugin.class_name == "MyshopPlugin"
        assert (plugin.site_name, plugin.base_url) == ("myshop", "https://myshop.example")
        assert [c.endpoint for c in plugin.commands] == [
            "GET /api/orders",
            "GET /api/orders/{order_id}",
        ]
        text = (tmp_path / plugin.module_path).read_text().splitlines()
        assert plugin.markers
        assert all("GP-FILL" in text[line - 1] for line in plugin.markers)
        assert plugin.fixtures_root == "tests/fixtures/"

    def test_a_hand_written_command_without_a_declaration_reads_none(self, tmp_path: Path) -> None:
        _hand_written(tmp_path)
        (plugin,) = read_project(tmp_path).plugins
        (command,) = plugin.commands
        assert (command.method, command.endpoint) == ("orders", None)
        assert command.keywords == {"help": "List orders"}
        assert plugin.markers == ()

    def test_cli_name_is_the_pinned_name_or_the_kebab_cased_method(self) -> None:
        span = Span(1, 2)
        assert CommandView("api_orders", span, MappingProxyType({})).cli_name == "api-orders"
        pinned = CommandView("by_id", span, MappingProxyType({"name": "order"}))
        assert pinned.cli_name == "order"

    def test_the_view_is_read_only(self, tmp_path: Path) -> None:
        _hand_written(tmp_path)
        view = read_project(tmp_path)
        (command,) = view.plugins[0].commands
        with pytest.raises(TypeError):
            command.keywords["endpoint"] = "GET /api/orders"
        with pytest.raises(TypeError):
            view.requirements["tests/conftest.py:FIXTURES_TREE"] = True

    def test_spans_cover_the_decorator_through_the_body(self, tmp_path: Path) -> None:
        _hand_written(tmp_path)
        (plugin,) = read_project(tmp_path).plugins
        lines = _HAND_WRITTEN_MODULE.splitlines()
        (command,) = plugin.commands
        assert lines[command.span.start - 1].strip().startswith("@command(")
        assert lines[command.span.end - 1].strip().startswith("return ctx.request_json")
        assert lines[plugin.class_span.start - 1].startswith("class MyshopPlugin")
        assert plugin.class_span.end == command.span.end

    @pytest.mark.parametrize("classes", [0, 2])
    def test_a_module_without_exactly_one_plugin_class_is_recorded_not_raised(
        self, tmp_path: Path, classes: int
    ) -> None:
        """A per-plugin defect: the rest of the project still reads, and each
        consumer decides whether the defect stops it."""
        body = "from graftpunk.plugins import SitePlugin\n\n\n" + "".join(
            f"class P{i}(SitePlugin):\n    site_name = 'p{i}'\n\n\n" for i in range(classes)
        )
        _hand_written(tmp_path, body)
        view = read_project(tmp_path)
        assert view.plugins == ()
        (defect,) = view.defects
        assert (defect.entry_point, defect.module_path) == (
            "myshop",
            "src/graftpunk_myshop/plugin.py",
        )
        assert f"exactly one SitePlugin subclass, found {classes}" in defect.message
        assert view.requirements, "the requirements still read"

    def test_a_module_that_does_not_parse_is_refused(self, tmp_path: Path) -> None:
        _hand_written(tmp_path, "class (:\n")
        with pytest.raises(PluginProjectError, match="does not parse"):
            read_project(tmp_path)

    def test_a_missing_module_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(_HAND_WRITTEN_PYPROJECT)
        with pytest.raises(PluginProjectError, match="graftpunk_myshop/plugin.py"):
            read_project(tmp_path)


class TestFixturesRoots:
    def test_a_suite_gives_its_first_member_the_tree_and_the_next_its_own_root(
        self, tmp_path: Path
    ) -> None:
        _generate(tmp_path, "myshop")
        write_scaffold(
            tmp_path,
            ScaffoldSpec(
                name="widgets",
                mode="new_project",
                backend="nodriver",
                base_url="https://myshop.example",
                digest=_digest("/api/widgets"),
            ),
        )
        roots = {p.site_name: p.fixtures_root for p in read_project(tmp_path).plugins}
        assert roots == {"myshop": "tests/fixtures/", "widgets": "tests/fixtures/widgets/"}

    @pytest.mark.parametrize("second", [False, True])
    def test_the_rule_gives_the_same_answer_from_the_spec_and_from_the_project(
        self, tmp_path: Path, second: bool
    ) -> None:
        _generate(tmp_path, "myshop")
        name, mode = ("widgets", "add_to_suite") if second else ("myshop", "new_project")
        if second:
            write_scaffold(
                tmp_path,
                ScaffoldSpec(
                    name=name,
                    mode="new_project",
                    backend="nodriver",
                    base_url="https://myshop.example",
                    digest=_digest("/api/widgets"),
                ),
            )
        spec = ScaffoldSpec(
            name=name, mode=mode, backend="nodriver", base_url="https://myshop.example"
        )
        view = next(p for p in read_project(tmp_path).plugins if p.site_name == name)
        assert (
            fixtures_root_for(spec)
            == view.fixtures_root
            == fixtures_root(suite_member=second, module_name=module_name_for(name))
        )

    def test_fixtures_dir_is_the_root_and_the_conftest_tree_contains_it(
        self, tmp_path: Path
    ) -> None:
        _generate(tmp_path, "myshop")
        write_scaffold(
            tmp_path,
            ScaffoldSpec(
                name="widgets",
                mode="new_project",
                backend="nodriver",
                base_url="https://myshop.example",
                digest=_digest("/api/widgets"),
            ),
        )
        tree = _assigned(tmp_path / "tests" / "conftest.py", "FIXTURES_TREE")
        for plugin, test_file in (("myshop", "test_plugin.py"), ("widgets", "test_widgets.py")):
            view = next(p for p in read_project(tmp_path).plugins if p.site_name == plugin)
            fixtures_dir = _assigned(tmp_path / "tests" / test_file, "FIXTURES_DIR")
            assert fixtures_dir == tmp_path / view.fixtures_root.rstrip("/")
            assert fixtures_dir.is_relative_to(tree)


def _assigned(module: Path, name: str) -> Path:
    """The value of *name*'s module-level Path assignment in *module*, evaluated."""
    for node in ast.parse(module.read_text()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return eval(  # noqa: S307 - evaluates the generator's own Path expression
                ast.unparse(node.value), {"Path": Path, "__file__": str(module)}
            )
    raise AssertionError(f"{module} does not assign {name}")


class TestRequirementsAreDecidedStructurally:
    @pytest.mark.parametrize(
        "conftest",
        [
            'from pathlib import Path\nFIXTURES_TREE = Path(__file__).parent / "fixtures"\n'
            "sanitised_fixtures = 1\n",
            "from pathlib import Path\n"
            'FIXTURES_TREE = (\n    Path(__file__).parent\n    / "fixtures"\n)\n'
            "from graftpunk.testing.plugin import fixtures_are_sanitised as sanitised_fixtures\n",
            "from somewhere import FIXTURES_TREE, sanitised_fixtures\n",
            "FIXTURES_TREE, sanitised_fixtures = 1, 2\n",
        ],
    )
    def test_any_module_level_binding_satisfies(self, tmp_path: Path, conftest: str) -> None:
        _generate(tmp_path)
        (tmp_path / "tests" / "conftest.py").write_text(conftest)
        assert _states(tmp_path) == {"bound"}

    @pytest.mark.parametrize(
        "conftest",
        [
            "",
            "from graftpunk.testing.plugin import *\n",
            "if True:\n    FIXTURES_TREE = 1\n    sanitised_fixtures = 2\n",
            "try:\n    from x import FIXTURES_TREE, sanitised_fixtures\n"
            "except ImportError:\n    pass\n",
        ],
    )
    def test_a_star_import_a_conditional_or_nothing_does_not(
        self, tmp_path: Path, conftest: str
    ) -> None:
        _generate(tmp_path)
        (tmp_path / "tests" / "conftest.py").write_text(conftest)
        assert _states(tmp_path) == {"unbound"}

    def test_a_generated_project_satisfies_every_requirement(self, tmp_path: Path) -> None:
        _generate(tmp_path)
        requirements = read_project(tmp_path).requirements
        assert {key: status.state for key, status in requirements.items()} == {
            "tests/conftest.py:FIXTURES_TREE": "bound",
            "tests/conftest.py:sanitised_fixtures": "bound",
        }

    def test_a_requirement_file_that_does_not_parse_is_unreadable_not_raised(
        self, tmp_path: Path
    ) -> None:
        """One broken conftest must not blind info and add-command, which never read
        it: the view records it, upgrade refuses on it, and check reports it."""
        _generate(tmp_path)
        (tmp_path / "tests" / "conftest.py").write_text("def (:\n")
        view = read_project(tmp_path)
        assert view.plugins, "the plugins still read"
        for status in view.requirements.values():
            assert status.state == "unreadable"
            assert status.reason is not None and "does not parse" in status.reason
        assert view.missing_requirements() == ()
        ((path, reason),) = view.unreadable_files()
        assert path == "tests/conftest.py"
        assert reason.startswith("does not parse (")

    def test_each_requirement_file_is_parsed_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two requirements live in tests/conftest.py; the reader parses it once and
        both statuses come from that one tree."""
        _generate(tmp_path)
        conftest = (tmp_path / "tests" / "conftest.py").read_text(encoding="utf-8")
        parsed: list[str] = []
        real_parse = ast.parse

        def counting_parse(source: str) -> ast.Module:
            parsed.append(source)
            return real_parse(source)

        monkeypatch.setattr(ast, "parse", counting_parse)
        view = read_project(tmp_path)
        assert parsed.count(conftest) == 1
        assert {status.state for status in view.requirements.values()} == {"bound"}

    def test_missing_requirements_are_the_unbound_entries_in_declared_order(
        self, tmp_path: Path
    ) -> None:
        _generate(tmp_path)
        (tmp_path / "tests" / "conftest.py").write_text(
            'from pathlib import Path\nFIXTURES_TREE = Path(__file__).parent / "fixtures"\n'
        )
        missing = read_project(tmp_path).missing_requirements()
        assert [r.name for r in missing] == ["sanitised_fixtures"]

    def test_a_directory_that_is_not_a_plugin_project_lacks_nothing(self, tmp_path: Path) -> None:
        assert read_project(tmp_path).missing_requirements() == ()


def _states(root: Path) -> set[str]:
    return {status.state for status in read_project(root).requirements.values()}


def test_the_reader_imports_neither_the_writer_nor_the_renderer() -> None:
    """A structural view: it reads policy, never the module that writes the disk or
    the one that renders a project."""
    script = (
        "import sys\n"
        "import graftpunk.devtools.plugin_project\n"
        "print(sorted(m for m in ('graftpunk.devtools.scaffold.write',"
        " 'graftpunk.devtools.scaffold.render') if m in sys.modules))\n"
    )
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=True
    )
    assert result.stdout.strip() == "[]"
```

In `tests/unit/test_scaffold_policy.py`, add `GP_FILL_MARKER` and `module_name_for` to the policy import (the filesystem denylist needs no change for the `re` import), and append:

```python
def test_the_marker_and_the_module_name_rule_live_here() -> None:
    """Policy is the rule's one home: the renderer calls it through policy and
    re-exports nothing, so no importer can reach it by a second route."""
    import graftpunk.devtools.scaffold.render as render_module

    assert GP_FILL_MARKER == "GP-FILL"
    assert module_name_for("My-Shop.v2") == "my_shop_v2"
    assert "module_name_for" not in render_module.__all__
    assert not hasattr(render_module, "module_name_for")
```

In `tests/unit/test_scaffold_render.py`, remove `module_name_for` from the `graftpunk.devtools.scaffold.render` import block and add `from graftpunk.devtools.scaffold.policy import module_name_for`, so `TestModuleNameFor` tests the rule where it lives. Then append:

```python
def test_the_renderer_spells_the_marker_only_through_the_policy() -> None:
    """One spelling of GP-FILL: the reader and the lint find what the renderer
    wrote because all three take it from policy."""
    import graftpunk.devtools.scaffold.render as render_module
    from graftpunk.devtools.scaffold.policy import GP_FILL_MARKER

    assert render_module.__file__ is not None
    tree = ast.parse(Path(render_module.__file__).read_text(encoding="utf-8"))
    docstrings = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Expr)}
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and GP_FILL_MARKER in node.value
    ]
    assert literals == []
```

In `tests/unit/test_devtools_errors.py` (foundations Task 10), add `from graftpunk.devtools.plugin_project import NotAPluginProjectError, PluginProjectError` to its imports, add both to the loop's tuple, and append `assert issubclass(PluginProjectError, ValueError)` and `assert issubclass(NotAPluginProjectError, ValueError)` to the test. Tasks 5 to 7 add their own refusals to the tuple in the loop.

Append to `tests/unit/test_cli_plugin.py`:

```python
class TestToCliNameLivesInNaming:
    def test_the_rule_has_one_public_home_and_no_alias(self) -> None:
        from graftpunk.har import naming
        from graftpunk.plugins import cli_plugin

        assert naming.to_cli_name("AccountStatements") == "account-statements"
        assert cli_plugin.to_cli_name is naming.to_cli_name
        assert cli_plugin.registered_name is naming.registered_name
        assert not hasattr(cli_plugin, "_to_cli_name")
```

In `tests/unit/test_har_naming.py`, add `registered_name` and `to_cli_name` to its `graftpunk.har.naming` import and append:

```python
def test_naming_imports_nothing_from_the_plugin_runtime() -> None:
    """The kebab-case rule lives in the naming module so the runtime, the reader,
    and the selection all read one rule without the devtools reaching into
    cli_plugin; the naming module stays free of that dependency."""
    import graftpunk.har.naming as naming

    assert naming.__file__ is not None
    tree = ast.parse(Path(naming.__file__).read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not any(m and m.startswith("graftpunk.plugins") for m in imported)
    assert to_cli_name("account_statements") == "account-statements"


def test_the_registered_name_is_the_pin_else_the_kebab_cased_identifier() -> None:
    assert registered_name(None, "order_detail") == "order-detail"
    assert registered_name("Orders", "orders") == "Orders"
```

Also add `import ast` and `from pathlib import Path` to that module's imports (at `7bd9604` it imports neither, and foundations Task 6 adds only `import pytest`).

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_plugin_project.py tests/unit/test_devtools_errors.py tests/unit/test_scaffold_policy.py tests/unit/test_scaffold_render.py tests/unit/test_cli_plugin.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'graftpunk.devtools.plugin_project'`, and `ImportError: cannot import name 'GP_FILL_MARKER'`.

- [ ] **Step 3: Give the marker, the module-name rule, and the kebab-case rule one public home each**

Move `_to_cli_name` out of `cli_plugin.py` into `har/naming.py` as the public `to_cli_name`, and rename every call, with no alias left behind. The naming module already owns the naming stems and depends on `har/paths.py` alone, so the plugin runtime, the reader, and `selection.py` all read the rule there. The command refuses to run if the new name is already bound in any of the four files, or if the function is not where it was verified:

```bash
uv run python - <<'PY'
import re
from pathlib import Path

cli_plugin = Path("src/graftpunk/plugins/cli_plugin.py")
naming = Path("src/graftpunk/har/naming.py")
callers = [cli_plugin, Path("src/graftpunk/client.py"), Path("tests/unit/test_cli_plugin.py")]
for path in [naming, *callers]:
    assert not re.search(r"(?<![\w])to_cli_name\b", path.read_text(encoding="utf-8")), path
text = cli_plugin.read_text(encoding="utf-8")
# The def, its indented body, and the blank lines after it, up to the next top-level line.
found = re.search(r"^def _to_cli_name\(name: str\) -> str:\n(?:(?:    .*)?\n)+", text, re.M)
assert found, "def _to_cli_name( not found"
function = found.group(0).rstrip("\n").replace("def _to_cli_name(", "def to_cli_name(")
cli_plugin.write_text(text[: found.start()] + text[found.end() :], encoding="utf-8")
naming.write_text(
    naming.read_text(encoding="utf-8").rstrip("\n") + "\n\n\n" + function + "\n",
    encoding="utf-8",
)
for path in callers:
    renamed = re.sub(r"\b_to_cli_name\b", "to_cli_name", path.read_text(encoding="utf-8"))
    path.write_text(renamed, encoding="utf-8")
PY
```

Then fix the imports by hand. In `src/graftpunk/har/naming.py`, add `import re`, add `"registered_name"` and `"to_cli_name"` to `__all__`, append to the module docstring: "It also owns the kebab-case rule a command's CLI name follows (``to_cli_name``) and the name a command registers (``registered_name``), which the plugin runtime and the devtools read from here.", and append after `to_cli_name`:

```python
def registered_name(name_pin: str | None, identifier: str) -> str:
    """The name the CLI registers for a command: its ``name=`` pin, else
    *identifier* kebab-cased. The ``command`` decorator, the project reader's
    ``CommandView.cli_name``, and the scaffold's ``PlannedCommand`` all ask this,
    so a stub's name and a hand-written command's name cannot be decided twice."""
    return name_pin or to_cli_name(identifier)
```

In `src/graftpunk/plugins/cli_plugin.py`, add `from graftpunk.har.naming import registered_name, to_cli_name`, and in `command`'s `decorator` rewrite the two lines that read `name=name or to_cli_name(target.__name__),` (the command group's `CommandGroupMeta` and the function's `CommandMetadata`) as `name=registered_name(name, target.__name__),`. The auto-discovered method's `name=to_cli_name(attr_name),` has no pin and keeps its call. `registered_name` spells the same `or` the two lines did, so no registered name changes. In `src/graftpunk/client.py` and `tests/unit/test_cli_plugin.py`, take `to_cli_name` out of the `graftpunk.plugins.cli_plugin` import and add `from graftpunk.har.naming import to_cli_name`. Run `uvx ruff check --fix` and `uvx ruff format` over the four files, which re-sort the import blocks and drop `re` from `cli_plugin.py` if nothing else there uses it. `TestToCliName`'s docstring now names `to_cli_name`, which is right. `grep -rn "_to_cli_name" src tests` prints nothing; `docs/plans/` keeps the old name in its historical design docs.

Importing `graftpunk.har.naming` from `cli_plugin.py` adds no new edge: `graftpunk.plugins.site_requests` already imports `graftpunk.har.documents` (`src/graftpunk/plugins/site_requests.py:19` at `7bd9604`), so the `graftpunk.har` package is loaded with the plugin runtime today.

In `src/graftpunk/devtools/scaffold/project.py`, import `module_name_for` from `graftpunk.devtools.scaffold.policy`, and import only `ScaffoldSpec`, `class_name_for`, and `render` from `render`, which no longer has the name.

In `src/graftpunk/devtools/scaffold/policy.py`, add `import re`, add `"GP_FILL_MARKER"` and `"module_name_for"` to `__all__`, move `module_name_for` here from `render.py` unchanged (its body and docstring), and append:

```python
GP_FILL_MARKER: Final = "GP-FILL"
"""The marker the generator writes wherever the digest could not decide a value.
The renderer writes it, the project reader finds it, and ``gp plugin check``
reports every one left; all three take it from here."""
```

In `src/graftpunk/devtools/scaffold/render.py`, delete `module_name_for` and its `__all__` entry, and rewrite every call to it as `policy.module_name_for(...)`, through the `from graftpunk.devtools.scaffold import policy` import foundations Task 9 added (find the calls with `grep -n "module_name_for" src/graftpunk/devtools/scaffold/render.py`, then run `uvx ruff format src/graftpunk/devtools/scaffold/render.py`). The module then has no `module_name_for` attribute, so nothing can import the rule from `render`. Add `from graftpunk.devtools.scaffold.policy import GP_FILL_MARKER`, and rewrite every string literal that holds `GP-FILL` to build it from the constant, keeping the generated text byte for byte. For example, `'        failure="GP-FILL: text on the page indicating login failure",'` becomes `f'        failure="{GP_FILL_MARKER}: text on the page indicating login failure",'`, and `'"GP-FILL"'` becomes `f'"{GP_FILL_MARKER}"'`. Find them with `grep -n "GP-FILL" src/graftpunk/devtools/scaffold/render.py`; comments may keep the word. The render tests that compare generated text are the check that nothing changed.

- [ ] **Step 4: Write the reader**

Create `src/graftpunk/devtools/plugin_project.py`:

```python
"""The one reader of a plugin project: what a directory holds, read once.

Resolves a directory to its plugin project (``pyproject.toml``, the
``graftpunk.plugins`` entry-point table, each entry point's module), classifies
it in its own terms (``empty``, ``plugin``, ``foreign``), and reads every plugin
module with ``ast``, never importing it, so a plugin whose code does not run
still gets an answer. :func:`read_project` returns one structural view that
``gp plugin info``, the stub inserter, the migrator, and ``gp plugin check`` all
work from; none of them parses a project on its own. The view grows here when a
consumer needs more, but a need for body-level facts beyond marker locations is
the point to reconsider it rather than extend it (graft skill spec, 2026-09-21).

A reader: it imports neither ``graftpunk.devtools.scaffold.write`` nor
``graftpunk.devtools.scaffold.render``, and knows no output format (the
``gp plugin info`` payload is built in ``graftpunk.devtools.plugin_info``).
"""

from __future__ import annotations

import ast
import re
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from graftpunk.devtools.errors import DevtoolsRefusal
from graftpunk.devtools.scaffold import policy
from graftpunk.devtools.scaffold.policy import GP_FILL_MARKER, ProjectRequirement, module_name_for
from graftpunk.devtools.scaffold.pysrc import binds_name
from graftpunk.har.naming import registered_name
from graftpunk.plugins import PLUGINS_GROUP

__all__ = [
    "CommandView",
    "DirectoryKind",
    "NotAPluginProjectError",
    "PluginDefect",
    "PluginProjectError",
    "PluginView",
    "ProjectView",
    "RequirementState",
    "RequirementStatus",
    "Span",
    "classify",
    "read_project",
    "require_plugin_project",
]

DirectoryKind = Literal["empty", "plugin", "foreign"]
RequirementState = Literal["bound", "unbound", "unreadable"]

_PLUGIN_BASE = "SitePlugin"
_COMMAND_DECORATOR = "command"
# Where an entry point's module may live, relative to the project root: the src
# layout gp plugin new writes, then a flat layout.
_SOURCE_ROOTS = ("src", "")


@dataclass(frozen=True)
class Span:
    """A 1-based, inclusive line range in a module."""

    start: int
    end: int


@dataclass(frozen=True)
class CommandView:
    """One ``@command``-decorated method: its name, its span (first decorator through
    the end of its body), and its decorator's keywords, read-only, each the string
    literal it was given or ``None`` when it is not one."""

    method: str
    span: Span
    keywords: Mapping[str, str | None]

    @property
    def endpoint(self) -> str | None:
        """The declared ``endpoint=`` literal, or ``None`` for a command that has none."""
        return self.keywords.get("endpoint")

    @property
    def cli_name(self) -> str:
        """The name the CLI registers, by the rule the ``command`` decorator applies."""
        return registered_name(self.keywords.get("name"), self.method)


@dataclass(frozen=True)
class PluginView:
    """One entry point's plugin, as its module reads. ``entry_point`` is the name of
    its line in the ``graftpunk.plugins`` table: the one identity every consumer
    addresses a plugin by, which ``site_name`` need not match."""

    entry_point: str
    module_path: str  # project-relative, forward slashes
    class_name: str
    class_span: Span
    site_name: str | None
    base_url: str | None
    commands: tuple[CommandView, ...]
    markers: tuple[int, ...]  # the line of every GP-FILL marker in the module
    fixtures_root: str  # project-relative, trailing slash


@dataclass(frozen=True)
class PluginDefect:
    """An entry point whose module parsed but does not hold exactly one ``SitePlugin``
    subclass. Recorded rather than raised, so the rest of the project still reads
    and each consumer decides whether the defect stops it."""

    entry_point: str
    module_path: str  # project-relative, forward slashes
    message: str


@dataclass(frozen=True)
class RequirementStatus:
    """Whether a project's file binds one ``PROJECT_REQUIREMENTS`` name: ``bound``;
    ``unbound`` (the file is missing, or does not bind it); or ``unreadable`` (the
    file does not parse), with ``reason`` saying why."""

    state: RequirementState
    reason: str | None = None


@dataclass(frozen=True)
class ProjectView:
    """A directory's plugin project: its classification, one view per readable entry
    point, one defect per entry point whose module is structurally wrong, and the
    status of each ``PROJECT_REQUIREMENTS`` name in its files (read-only, keyed by
    the requirement's ``key``).

    ``requirements`` is computed when the project is read, from the requirements
    declared then; :meth:`missing_requirements` and :meth:`unreadable_files` read
    ``policy.PROJECT_REQUIREMENTS`` when they are called, and treat a
    requirement the view has no status for as unbound. A view is short-lived: read
    it, act on it, and read the project again rather than keep one."""

    directory: DirectoryKind
    plugins: tuple[PluginView, ...]
    defects: tuple[PluginDefect, ...]
    requirements: Mapping[str, RequirementStatus]

    def _state(self, requirement: ProjectRequirement) -> RequirementState:
        status = self.requirements.get(requirement.key)
        return "unbound" if status is None else status.state

    def missing_requirements(self) -> tuple[ProjectRequirement, ...]:
        """The ``PROJECT_REQUIREMENTS`` entries this project's files do not bind, in
        declared order: the one answer ``gp plugin upgrade`` applies and
        ``gp plugin check`` reports. An entry whose file does not parse is not
        here; its file is in :meth:`unreadable_files`. Empty for a directory that
        is not a plugin project, which has no requirements to lack."""
        if self.directory != "plugin":
            return ()
        return tuple(r for r in policy.PROJECT_REQUIREMENTS if self._state(r) == "unbound")

    def unreadable_files(self) -> tuple[tuple[str, str], ...]:
        """Each requirement file that does not parse, once, as ``(path, reason)``, in
        the order ``PROJECT_REQUIREMENTS`` first names it, however many requirements
        it holds: ``gp plugin upgrade`` refuses on them and ``gp plugin check``
        reports one finding each, while ``gp plugin info`` and
        ``gp plugin add-command``, which never read those files, proceed."""
        if self.directory != "plugin":
            return ()
        files: dict[str, str] = {}
        for r in policy.PROJECT_REQUIREMENTS:
            status = self.requirements.get(r.key)
            if status is not None and status.state == "unreadable":
                files.setdefault(r.path, status.reason or "does not parse")
        return tuple(files.items())


class PluginProjectError(DevtoolsRefusal, ValueError):
    """The project cannot be read at all: the message names the file and the reason.
    A plugin module that parses but holds the wrong number of plugin classes is a
    :class:`PluginDefect` in the view, and a requirement's file that does not parse
    is an ``unreadable`` :class:`RequirementStatus`, not this."""


class NotAPluginProjectError(DevtoolsRefusal, ValueError):
    """An operation that needs a plugin project was pointed at a directory that is
    not one; the message names the directory and its classification."""


def _load_pyproject(root: Path) -> dict[str, Any] | None:
    path = root / "pyproject.toml"
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise PluginProjectError(f"{path}: not valid TOML ({exc})") from exc


def _entry_points(data: dict[str, Any]) -> dict[str, str] | None:
    group = data.get("project", {}).get("entry-points", {}).get(PLUGINS_GROUP)
    return None if group is None else {str(k): str(v) for k, v in group.items()}


def classify(root: Path) -> DirectoryKind:
    """*root* in its own terms: no ``pyproject.toml`` is ``empty``; one declaring the
    ``graftpunk.plugins`` entry-point group is ``plugin``; any other is ``foreign``."""
    data = _load_pyproject(root)
    if data is None:
        return "empty"
    return "plugin" if _entry_points(data) is not None else "foreign"


def read_project(root: Path) -> ProjectView:
    """Read *root* into its structural view.

    A module that parses but does not hold exactly one ``SitePlugin`` subclass is
    recorded in ``defects`` and read no further; every other plugin still reads.
    A requirement's file that does not parse is recorded as ``unreadable``; each
    distinct requirement file is parsed once.

    Raises:
        PluginProjectError: The project cannot be read at all: ``pyproject.toml``
            is not valid TOML, or an entry point's module is missing or does not
            parse.
    """
    data = _load_pyproject(root)
    if data is None:
        return ProjectView(
            directory="empty", plugins=(), defects=(), requirements=MappingProxyType({})
        )
    entry_points = _entry_points(data)
    if entry_points is None:
        return ProjectView(
            directory="foreign", plugins=(), defects=(), requirements=MappingProxyType({})
        )
    project_name = str(data.get("project", {}).get("name", ""))
    read = [_read_plugin(root, key, value, project_name) for key, value in entry_points.items()]
    requirements = _requirement_statuses(root)
    return ProjectView(
        directory="plugin",
        plugins=tuple(r for r in read if isinstance(r, PluginView)),
        defects=tuple(r for r in read if isinstance(r, PluginDefect)),
        requirements=MappingProxyType(requirements),
    )


def require_plugin_project(root: Path) -> ProjectView:
    """*root*'s view, when *root* is a plugin project: the one owner of "this
    directory must be a plugin project", which the stub inserter, the migrator,
    and the lint all ask.

    Raises:
        NotAPluginProjectError: *root* is ``empty`` or ``foreign``.
        PluginProjectError: See :func:`read_project`.
    """
    view = read_project(root)
    if view.directory != "plugin":
        raise NotAPluginProjectError(
            f"{root} is not a graftpunk plugin project ({view.directory})."
        )
    return view


def _normalised(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def _module_file(root: Path, module: str) -> str | None:
    relative = module.replace(".", "/")
    for base in _SOURCE_ROOTS:
        for candidate in (f"{relative}.py", f"{relative}/__init__.py"):
            path = Path(base) / candidate
            if (root / path).is_file():
                return path.as_posix()
    return None


def _parse(root: Path, relative: str) -> tuple[str, ast.Module]:
    text = (root / relative).read_text(encoding="utf-8")
    try:
        return text, ast.parse(text)
    except SyntaxError as exc:
        raise PluginProjectError(
            f"{relative}: does not parse ({exc.msg}, line {exc.lineno})"
        ) from exc


def _last_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _read_plugin(root: Path, key: str, value: str, project_name: str) -> PluginView | PluginDefect:
    module = value.partition(":")[0]
    relative = _module_file(root, module)
    if relative is None:
        path = module.replace(".", "/") + ".py"
        raise PluginProjectError(
            f"entry point {key!r} names {value!r}, but neither src/{path} nor {path} exists."
        )
    text, tree = _parse(root, relative)
    classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and any(_last_name(b) == _PLUGIN_BASE for b in node.bases)
    ]
    if len(classes) != 1:
        found = ", ".join(c.name for c in classes) or "none"
        return PluginDefect(
            entry_point=key,
            module_path=relative,
            message=(
                f"{relative}: expected exactly one SitePlugin subclass, found {len(classes)} "
                f"({found}). A plugin module holds one plugin class."
            ),
        )
    (klass,) = classes
    package = module.split(".")[0]
    start = min([klass.lineno, *(d.lineno for d in klass.decorator_list)])
    return PluginView(
        entry_point=key,
        module_path=relative,
        class_name=klass.name,
        class_span=Span(start, klass.end_lineno or klass.lineno),
        site_name=_class_string(klass, "site_name"),
        base_url=_class_string(klass, "base_url"),
        commands=tuple(_commands(klass)),
        markers=tuple(
            number
            for number, line in enumerate(text.splitlines(), start=1)
            if GP_FILL_MARKER in line
        ),
        fixtures_root=policy.fixtures_root(
            suite_member=_normalised(project_name) != _normalised(package),
            module_name=module_name_for(key),
        ),
    )


def _class_string(klass: ast.ClassDef, name: str) -> str | None:
    """The string literal *klass*'s body assigns to *name*, or ``None``."""
    for node in klass.body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets, value = [node.target.id], node.value
        else:
            continue
        if name in targets and isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return None


def _commands(klass: ast.ClassDef) -> Iterator[CommandView]:
    for node in klass.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorator = next(
            (
                d
                for d in node.decorator_list
                if _last_name(d.func if isinstance(d, ast.Call) else d) == _COMMAND_DECORATOR
            ),
            None,
        )
        if decorator is None:
            continue
        keywords: dict[str, str | None] = {}
        if isinstance(decorator, ast.Call):
            for keyword in decorator.keywords:
                if keyword.arg is None:
                    continue
                value = keyword.value
                keywords[keyword.arg] = (
                    value.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str)
                    else None
                )
        start = min(d.lineno for d in node.decorator_list)
        yield CommandView(
            node.name, Span(start, node.end_lineno or node.lineno), MappingProxyType(keywords)
        )


def _requirement_statuses(root: Path) -> dict[str, RequirementStatus]:
    """Each ``PROJECT_REQUIREMENTS`` entry's status, keyed by its ``key``: whether its
    file binds its name, by the one predicate, ``pysrc.binds_name``. Each distinct
    file is parsed once. A file that does not parse is ``unreadable`` for every entry
    it holds, with its one reason, rather than raised: one broken conftest must not
    blind the consumers that never read it."""
    paths = dict.fromkeys(r.path for r in policy.PROJECT_REQUIREMENTS)
    parsed = {relative: _parse_requirement_file(root / relative) for relative in paths}
    statuses: dict[str, RequirementStatus] = {}
    for r in policy.PROJECT_REQUIREMENTS:
        tree = parsed[r.path]
        if isinstance(tree, RequirementStatus):
            statuses[r.key] = tree
        else:
            statuses[r.key] = RequirementStatus("bound" if binds_name(tree, r.name) else "unbound")
    return statuses


def _parse_requirement_file(path: Path) -> ast.Module | RequirementStatus:
    """*path*'s tree, or the status every requirement in it takes when there is no
    tree: ``unbound`` for a missing file, ``unreadable`` with the reason for one that
    does not parse."""
    if not path.is_file():
        return RequirementStatus("unbound")
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return RequirementStatus("unreadable", f"does not parse ({exc.msg}, line {exc.lineno})")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_plugin_project.py tests/unit/test_devtools_errors.py tests/unit/test_scaffold_policy.py tests/unit/test_scaffold_render.py tests/unit/test_scaffold_cli.py tests/unit/test_scaffold_write.py tests/unit/test_cli_plugin.py tests/unit/test_client.py tests/unit/test_scaffold_pysrc.py tests/unit/test_har_naming.py -q`
Expected: PASS, including foundations Task 1's export test, which now finds `binds_name` imported from `pysrc` by the reader.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/devtools/plugin_project.py src/graftpunk/har/naming.py src/graftpunk/plugins/cli_plugin.py src/graftpunk/client.py src/graftpunk/devtools/scaffold/project.py src/graftpunk/devtools/scaffold/policy.py src/graftpunk/devtools/scaffold/render.py tests/unit/test_plugin_project.py tests/unit/test_devtools_errors.py tests/unit/test_scaffold_policy.py tests/unit/test_scaffold_render.py tests/unit/test_cli_plugin.py tests/unit/test_har_naming.py
git commit -m "feat(devtools): plugin_project reads a plugin project once into one structural view"
```

---

### Task 4: `gp plugin info --json`, with its schema number

**Files:**
- Modify: `src/graftpunk/contracts.py` (`Surface`, `INFO_SCHEMA`, `_CURRENT`, `CLI_SURFACES`, `__all__`, docstring)
- Create: `src/graftpunk/devtools/plugin_info.py` (`info_payload` and `PluginDefectRefusal`: the payload lives beside the entry point's needs, and the reader stays a structural view)
- Modify: `src/graftpunk/cli/scaffold_commands.py` (new `plugin_info`)
- Modify: `tests/unit/test_contracts.py`, `tests/unit/test_devtools_errors.py`
- Test: `tests/unit/test_plugin_project_cli.py`

**Interfaces:**
- Consumes: `DevtoolsRefusal` (foundations Task 10); `read_project`, `ProjectView`, `ProjectView.defects`, `PluginDefect`, `PluginView.entry_point`, and `CommandView.cli_name` (Task 3; `cli_name` calls `registered_name`, which Task 3 added to `src/graftpunk/har/naming.py` beside `to_cli_name`).
- Produces: `contracts.INFO_SCHEMA: Final = 1`; `Surface = Literal["endpoints", "info", "sidecar"]`; `CLI_SURFACES == ("endpoints", "info")`, so `gp version --json` prints `"contracts": {"endpoints": 1, "info": 1}` and `gp version --contract info=1` exits 0; `graftpunk.devtools.plugin_info.info_payload(view: ProjectView) -> dict[str, object]` with the pinned field sets `{"schema", "directory", "plugins"}`, each plugin `{"entry_point", "module", "site_name", "base_url", "commands"}`, each command `{"name", "endpoint"}`; `entry_point` is the name `gp plugin add-command` takes and the skill matches `$0` against. `info_payload` owns "info refuses while any plugin has a defect": for a view with any defect it raises `class PluginDefectRefusal(DevtoolsRefusal)` (with `.defects: tuple[PluginDefect, ...]`), whose message is one line per defect, `entry point '<name>': <message>`, and it never builds a payload that omits a plugin. `gp plugin info --json [--dir PATH]` catches `DevtoolsRefusal` alone, like `add-command` and `upgrade`, and exits 1 with that message. A requirement's file that does not parse does not stop it: the payload carries no requirement. The skill's preflight relays this payload unchanged.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_contracts.py`, add `INFO_SCHEMA` to the import, and append to `TestCurrentNumbers`:

```python
    def test_the_info_surface_is_read_through_the_cli(self) -> None:
        assert current_schema("info") == INFO_SCHEMA == 1
        assert cli_contracts() == {"endpoints": 1, "info": 1}
```

Create `tests/unit/test_plugin_project_cli.py`:

```python
"""gp plugin info, add-command, upgrade, and check through the Typer runner
(graft skill spec, 2026-09-21)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from graftpunk.cli.main import app

runner = CliRunner()

# The info payload's field sets at schema 1, frozen here so a rename fails the suite.
_INFO_V1 = {"schema", "directory", "plugins"}
_INFO_PLUGIN_V1 = {"entry_point", "module", "site_name", "base_url", "commands"}
_INFO_COMMAND_V1 = {"name", "endpoint"}


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _entry(
    method: str, url: str, *, content_type: str = "application/json", body: str = "{}"
) -> dict:
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


_LOGIN_PAGE = (
    '<form action="/session" method="post"><input type="email" id="email" name="email">'
    '<input type="password" id="password" name="password"></form>'
)


def _credential_post() -> dict:
    entry = _entry("POST", "https://myshop.example/session")
    entry["request"]["postData"] = {
        "mimeType": "application/json",
        "text": json.dumps({"email": "alice@example.com", "password": "invented"}),
    }
    return entry


def _run_entries() -> list[dict]:
    return [
        _entry("GET", "https://myshop.example/login", content_type="text/html", body=_LOGIN_PAGE),
        _credential_post(),
        _entry("GET", "https://myshop.example/api/orders?page=2", body='{"orders": []}'),
        _entry("GET", "https://myshop.example/api/orders/1001", body='{"id": "1001"}'),
        _entry("GET", "https://myshop.example/api/invoices", body='{"invoices": []}'),
    ]


@pytest.fixture()
def recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A recording named myshop, and the directory a project is generated into."""
    observe_base = tmp_path / "observe"
    monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
    run_dir = observe_base / "myshop" / "run-1"
    run_dir.mkdir(parents=True)
    har = {"log": {"version": "1.2", "entries": _run_entries()}}
    (run_dir / "network.har").write_text(json.dumps(har))
    project = tmp_path / "project"
    project.mkdir()
    return project


def _new(project: Path, name: str = "myshop", *commands: str) -> None:
    argv = ["plugin", "new", name, "--from-run", "myshop", "--dir", str(project)]
    for command in commands:
        argv += ["--command", command]
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output


def _info(project: Path) -> dict:
    result = runner.invoke(app, ["plugin", "info", "--json", "--dir", str(project)])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


class TestPluginInfo:
    def test_an_empty_directory(self, tmp_path: Path) -> None:
        assert _info(tmp_path) == {"schema": 1, "directory": "empty", "plugins": []}

    def test_a_foreign_directory(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "other"\n')
        assert _info(tmp_path)["directory"] == "foreign"

    def test_a_standalone_project_lists_one_plugin_with_the_pinned_fields(
        self, recorded: Path
    ) -> None:
        _new(recorded)
        payload = _info(recorded)
        assert set(payload) == _INFO_V1
        assert payload["schema"] == 1
        assert payload["directory"] == "plugin"
        (plugin,) = payload["plugins"]
        assert set(plugin) == _INFO_PLUGIN_V1
        assert plugin["entry_point"] == "myshop"
        assert plugin["module"] == "src/graftpunk_myshop/plugin.py"
        assert (plugin["site_name"], plugin["base_url"]) == ("myshop", "https://myshop.example")
        for command in plugin["commands"]:
            assert set(command) == _INFO_COMMAND_V1
        assert {"name": "api-orders", "endpoint": "GET /api/orders"} in plugin["commands"]

    def test_a_suite_lists_every_entry_point(self, recorded: Path) -> None:
        _new(recorded, "myshop")
        _new(recorded, "widgets")
        plugins = _info(recorded)["plugins"]
        assert [p["entry_point"] for p in plugins] == ["myshop", "widgets"]
        assert [p["site_name"] for p in plugins] == ["myshop", "widgets"]

    def test_an_undeclared_command_reports_null(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "graftpunk-myshop"\n\n'
            '[project.entry-points."graftpunk.plugins"]\n'
            'myshop = "graftpunk_myshop.plugin:MyshopPlugin"\n'
        )
        package = tmp_path / "src" / "graftpunk_myshop"
        package.mkdir(parents=True)
        (package / "plugin.py").write_text(
            "from graftpunk.plugins import SitePlugin, command\n\n\n"
            "class MyshopPlugin(SitePlugin):\n"
            '    site_name = "myshop"\n\n'
            '    @command(help="One order", name="order")\n'
            "    def by_id(self, ctx, order_id: str) -> dict:\n"
            "        return {}\n"
        )
        (plugin,) = _info(tmp_path)["plugins"]
        assert plugin["commands"] == [{"name": "order", "endpoint": None}]
        assert plugin["base_url"] is None

    def test_the_payload_carries_no_installation_facts(self, recorded: Path) -> None:
        _new(recorded)
        payload = _info(recorded)
        assert "contracts" not in payload and "graftpunk" not in payload

    def test_json_is_required(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["plugin", "info", "--dir", str(tmp_path)])
        assert result.exit_code == 1
        assert "--json" in _plain(result.output)

    def test_a_defective_plugin_module_is_refused_not_left_out(self, recorded: Path) -> None:
        _new(recorded)
        module = recorded / "src" / "graftpunk_myshop" / "plugin.py"
        module.write_text(module.read_text() + "\n\nclass Other(SitePlugin):\n    pass\n")
        result = runner.invoke(app, ["plugin", "info", "--json", "--dir", str(recorded)])
        assert result.exit_code == 1
        output = " ".join(_plain(result.output).split())
        assert "entry point 'myshop':" in output
        assert "exactly one SitePlugin subclass, found 2" in output

    def test_an_unreadable_pyproject_is_a_one_line_refusal(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project\n")
        result = runner.invoke(app, ["plugin", "info", "--json", "--dir", str(tmp_path)])
        assert result.exit_code == 1
        assert "pyproject.toml: not valid TOML" in _plain(result.output)
        assert "Traceback" not in result.output

    def test_a_conftest_that_does_not_parse_does_not_stop_it(self, recorded: Path) -> None:
        """info never reads the conftest: an unreadable requirement file is upgrade's
        and check's business."""
        _new(recorded)
        (recorded / "tests" / "conftest.py").write_text("def (:\n")
        (plugin,) = _info(recorded)["plugins"]
        assert plugin["entry_point"] == "myshop"


def test_info_payload_refuses_a_view_with_a_defect_and_names_every_one() -> None:
    """The refusal is info_payload's own, so no caller can build a payload that
    leaves a defective plugin out."""
    defects = tuple(
        PluginDefect(
            entry_point=name,
            module_path=f"src/graftpunk_{name}/plugin.py",
            message=f"{name} is broken",
        )
        for name in ("myshop", "widgets")
    )
    view = ProjectView(
        directory="plugin", plugins=(), defects=defects, requirements=MappingProxyType({})
    )
    with pytest.raises(PluginDefectRefusal) as caught:
        info_payload(view)
    assert str(caught.value).splitlines() == [
        "entry point 'myshop': myshop is broken",
        "entry point 'widgets': widgets is broken",
    ]
    assert caught.value.defects == defects
```

Add `from types import MappingProxyType`, `from graftpunk.devtools.plugin_info import PluginDefectRefusal, info_payload`, and `from graftpunk.devtools.plugin_project import PluginDefect, ProjectView` to its imports. In `tests/unit/test_devtools_errors.py`, add `PluginDefectRefusal` (from `graftpunk.devtools.plugin_info`) to the loop's tuple.

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_plugin_project_cli.py::TestPluginInfo tests/unit/test_contracts.py -q`
Expected: FAIL (`No such command 'info'`, and `ImportError: cannot import name 'INFO_SCHEMA'`).

- [ ] **Step 3: Declare the `info` number**

In `src/graftpunk/contracts.py`: change `Surface` to `Literal["endpoints", "info", "sidecar"]`; add `INFO_SCHEMA: Final = 1` after `ENDPOINTS_SCHEMA`; add `"info": INFO_SCHEMA,` to `_CURRENT`; set `CLI_SURFACES: Final[tuple[Surface, ...]] = ("endpoints", "info")`; add `"INFO_SCHEMA"` to `__all__`; and in the module docstring's second paragraph (the one beginning "Payloads a program reads carry a ``schema`` number"), add "the ``gp plugin info --json`` payload (``info``)," to its list of payloads.

- [ ] **Step 4: Build the payload and the command**

Create `src/graftpunk/devtools/plugin_info.py`:

```python
"""``gp plugin info --json``: the payload, built from the project reader's view.

Kept apart from :mod:`graftpunk.devtools.plugin_project`, which stays a
structural view that knows no output format (graft skill spec, 2026-09-21).
"""

from __future__ import annotations

from collections.abc import Sequence

from graftpunk.contracts import current_schema
from graftpunk.devtools.errors import DevtoolsRefusal
from graftpunk.devtools.plugin_project import PluginDefect, ProjectView

__all__ = ["PluginDefectRefusal", "info_payload"]


class PluginDefectRefusal(DevtoolsRefusal):
    """``gp plugin info`` refuses while any plugin module is defective, rather than
    describe a project with that plugin left out. The message is one line per
    defect: ``entry point '<name>': <the reader's message>``."""

    def __init__(self, defects: Sequence[PluginDefect]) -> None:
        self.defects = tuple(defects)
        super().__init__(
            "\n".join(f"entry point {d.entry_point!r}: {d.message}" for d in self.defects)
        )


def info_payload(view: ProjectView) -> dict[str, object]:
    """``gp plugin info --json``: facts about the directory, and none about the install.

    Within a schema version fields are added and never renamed or removed; the
    number comes from :mod:`graftpunk.contracts`. A plugin's ``entry_point`` is its
    one identity, the name ``gp plugin add-command`` takes; a command's ``name`` is
    the one the CLI registers (:attr:`CommandView.cli_name`).

    Raises:
        PluginDefectRefusal: A plugin module is defective. The payload is never
            built without that plugin, so every caller gets the refusal.
    """
    if view.defects:
        raise PluginDefectRefusal(view.defects)
    return {
        "schema": current_schema("info"),
        "directory": view.directory,
        "plugins": [
            {
                "entry_point": plugin.entry_point,
                "module": plugin.module_path,
                "site_name": plugin.site_name,
                "base_url": plugin.base_url,
                "commands": [
                    {
                        "name": command.cli_name,
                        "endpoint": command.endpoint,
                    }
                    for command in plugin.commands
                ],
            }
            for plugin in view.plugins
        ],
    }
```

In `src/graftpunk/cli/scaffold_commands.py`, add `import json`, add `DevtoolsRefusal` to the `graftpunk.devtools.errors` import foundations Task 10 added, add `from graftpunk.devtools.plugin_info import info_payload`, and `from graftpunk.devtools.plugin_project import read_project`, and append:

```python
@plugin_app.command("info")
def plugin_info(
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON (the only form)")] = False,
    dir_: Annotated[Path, typer.Option("--dir", help="Project directory")] = Path("."),
) -> None:
    """Describe the plugin project in --dir: its classification, plugins, and commands."""
    if not as_json:
        console.print("[red]gp plugin info prints JSON only: pass --json.[/red]")
        raise typer.Exit(1)
    try:
        payload = info_payload(read_project(dir_))
    except DevtoolsRefusal as exc:
        LOG.debug("plugin_info_refused", reason=type(exc).__name__)
        console.print(f"[red]{escape(str(exc))}[/red]", soft_wrap=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_plugin_project_cli.py tests/unit/test_contracts.py tests/unit/test_cli_version.py tests/unit/test_devtools_errors.py -q`
Expected: PASS. `test_cli_version.py` now sees `info` under `contracts`.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/contracts.py src/graftpunk/devtools/plugin_info.py src/graftpunk/cli/scaffold_commands.py tests/unit/test_contracts.py tests/unit/test_plugin_project_cli.py tests/unit/test_devtools_errors.py
git commit -m "feat(scaffold): gp plugin info --json describes the directory's plugin project"
```

---

### Task 5: The single-command renderer and `gp plugin new --command`

**Files:**
- Create: `src/graftpunk/devtools/scaffold/selection.py` (`CommandSelection`, `CommandSelectionError`, `command_identifier`, `PlannedCommand`, `plan_command`, `planned_commands`; `_MAX_COMMAND_NAME` moves here from `render.py`)
- Modify: `src/graftpunk/devtools/scaffold/render.py` (imports the selection names; new `RenderedCommand`, `render_command`, `_default_commands`; `ScaffoldSpec.commands`; `render` plans the commands once and passes them to `_render_plugin_module`, `_render_command_stubs`, and `_render_test_module`; `_decorator_lines` and `_render_command_stub` take a planned command; `fixture_paths` reads planned commands; `_stub_endpoints` removed)
- Modify: `src/graftpunk/cli/scaffold_commands.py` (`--command` on `plugin_new`; `_command_selections`)
- Modify: `docs/PLUGIN_DEVELOPMENT.md` ("What gets filled in": the stub's help placeholder names the registered command)
- Test: `tests/unit/test_scaffold_selection.py`, `tests/unit/test_scaffold_render.py`, `tests/unit/test_scaffold_cli.py`, `tests/unit/test_devtools_errors.py`

**Interfaces:**
- Consumes: `parse_command_spec`, `EndpointSpecError` (foundations Task 6); `Endpoint.login_flow` (foundations Task 3); `_declared_extras` and `_needs_param_specs` (foundations Task 11), and `_decorator_lines` (foundations Task 11), whose signature this task changes; `policy.GP_FILL_MARKER`, `graftpunk.har.naming.to_cli_name`, and `graftpunk.har.naming.registered_name` (Task 3); `DevtoolsRefusal` (foundations Task 10); `_PLUGINS_MODULE` (Task 2).
- Produces, in `graftpunk.devtools.scaffold.selection` (which commands a render or an insert produces, under which names; it imports nothing from `render.py`, which imports it): `@dataclass(frozen=True) class CommandSelection(name: str, method: str, template: str)`; `class CommandSelectionError(DevtoolsRefusal, ValueError)`; `command_identifier(name: str) -> str`; `@dataclass(frozen=True) class PlannedCommand(identifier: str, name_pin: str | None, method: str, endpoint: Endpoint)` with property `registered_name -> str` (`naming.registered_name(name_pin, identifier)`: the name the CLI registers, by the rule the `command` decorator and `CommandView.cli_name` also call); `plan_command(d: RunDigest, selection: CommandSelection) -> PlannedCommand`; `planned_commands(d: RunDigest, selections: Sequence[CommandSelection]) -> list[PlannedCommand]`, the explicit selection planned in order with a name given twice refused. `selection.py` owns only the explicit selection and takes no callback; the renderer's `_planned(spec)` decides between the spec's selections and its own default choice. In `render`: `@dataclass(frozen=True) class RenderedCommand(lines: tuple[str, ...], imports: tuple[tuple[str, str], ...], fixture: str)`, where `imports` is every `(module, name)` pair the stub references (always `(_PLUGINS_MODULE, "CommandContext")` and `(_PLUGINS_MODULE, "command")`, plus `(_PLUGINS_MODULE, "PluginParamSpec")` for a stub with explicit parameter specs, `_PLUGINS_MODULE` being `graftpunk.plugins`); `render_command(command: PlannedCommand, d: RunDigest) -> RenderedCommand`; `ScaffoldSpec.commands: tuple[CommandSelection, ...] = ()`, checked for shape only when the spec is built (a selection needs a digest) and planned, with every refusal, when it is rendered; `_decorator_lines(command: PlannedCommand, param_specs: list[str])`, whose help placeholder names `command.registered_name`, the kebab form the CLI registers; in the CLI, `_command_selections(values: list[str]) -> tuple[CommandSelection, ...]`. `render.py` re-imports `CommandSelection` for `ScaffoldSpec` and nothing re-exports the selection names. Task 6 consumes `plan_command`, `PlannedCommand.registered_name`, `render_command`, `RenderedCommand.imports`, and `_command_selections`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_scaffold_selection.py`:

```python
"""Which commands a render or an insert produces, under which names (graft skill
spec, 2026-09-21)."""

from __future__ import annotations

import ast
from pathlib import Path
from types import MappingProxyType

import pytest

import graftpunk.devtools.scaffold.selection as selection
from graftpunk.devtools.plugin_project import CommandView, Span
from graftpunk.devtools.scaffold.selection import CommandSelection, PlannedCommand
from graftpunk.har.digest import Endpoint
from graftpunk.har.naming import registered_name
from graftpunk.plugins import command


def _endpoint() -> Endpoint:
    return Endpoint(
        host="myshop.example.com",
        template="/orders/{order_id}",
        methods=("GET",),
        count=1,
        statuses=(200,),
        content_type="application/json",
        query_params={},
        body_params={},
        body_kind="none",
        shape=None,
        custom_headers=(),
        examples=(),
    )


def test_the_registered_name_is_the_pin_or_the_kebab_cased_identifier() -> None:
    assert PlannedCommand("order_detail", None, "GET", _endpoint()).registered_name == (
        "order-detail"
    )
    assert PlannedCommand("Orders", "Orders", "GET", _endpoint()).registered_name == "Orders"


@pytest.mark.parametrize(("pin", "expected"), [(None, "order-detail"), ("order", "order")])
def test_the_decorator_the_reader_and_the_plan_register_one_name(
    pin: str | None, expected: str
) -> None:
    """The runtime registers, the reader reports, and the scaffold plans the same
    name for a command, pinned or not: all three call naming.registered_name."""

    def order_detail(self: object, ctx: object) -> None:
        return None

    runtime = command(name=pin)(order_detail)._command_meta.name
    keywords = MappingProxyType({} if pin is None else {"name": pin})
    reader = CommandView("order_detail", Span(1, 2), keywords).cli_name
    planned = PlannedCommand("order_detail", pin, "GET", _endpoint()).registered_name
    assert runtime == reader == planned == registered_name(pin, "order_detail") == expected


def test_a_selection_is_a_plain_triple() -> None:
    assert CommandSelection("order", "GET", "/orders/{order_id}").name == "order"


def test_selection_does_not_import_the_renderer() -> None:
    """render.py imports selection.py, never the reverse."""
    assert selection.__file__ is not None
    tree = ast.parse(Path(selection.__file__).read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "graftpunk.devtools.scaffold.render" not in imported
```

In `tests/unit/test_devtools_errors.py`, add `CommandSelectionError` (from `graftpunk.devtools.scaffold.selection`) to the loop's tuple.

Append to `tests/unit/test_scaffold_render.py` (add `from graftpunk.devtools.scaffold.selection import CommandSelection, CommandSelectionError, plan_command`, and add `render_command` to its render import):

```python
class TestExplicitSelection:
    @staticmethod
    def _spec(*selections: CommandSelection, endpoints: tuple[Endpoint, ...]) -> ScaffoldSpec:
        return ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=endpoints),
            commands=selections,
        )

    def test_only_the_selected_stubs_under_the_given_names(self) -> None:
        files = render(
            self._spec(
                CommandSelection("order", "GET", "/orders/{order_id}"),
                endpoints=(_ORDERS_ENDPOINT, _SEARCH_ENDPOINT),
            )
        )
        plugin_code = files["src/graftpunk_myshop/plugin.py"]
        assert "def order(" in plugin_code
        assert "def search(" not in plugin_code
        assert "def test_order(" in files["tests/test_plugin.py"]

    def test_a_hyphenated_name_becomes_an_identifier_with_no_pin(self) -> None:
        plugin_code = render(
            self._spec(
                CommandSelection("order-detail", "GET", "/orders/{order_id}"),
                endpoints=(_ORDERS_ENDPOINT,),
            )
        )["src/graftpunk_myshop/plugin.py"]
        assert "def order_detail(" in plugin_code
        assert "name=" not in plugin_code
        assert 'help="GP-FILL: describe order-detail",' in plugin_code

    def test_a_name_kebab_case_cannot_reach_is_pinned(self) -> None:
        plugin_code = render(
            self._spec(
                CommandSelection("Orders", "GET", "/orders/{order_id}"),
                endpoints=(_ORDERS_ENDPOINT,),
            )
        )["src/graftpunk_myshop/plugin.py"]
        assert "def Orders(" in plugin_code
        assert '        name="Orders",' in plugin_code.splitlines()

    def test_explicit_selection_ignores_the_stub_cap(self) -> None:
        words = [
            f"{chr(97 + i // 26)}{chr(97 + i % 26)}route"
            for i in range(_MAX_SCAFFOLD_ENDPOINTS + 1)
        ]
        endpoints = tuple(dataclasses.replace(_SEARCH_ENDPOINT, template=f"/{w}") for w in words)
        selections = tuple(CommandSelection(w, "GET", f"/{w}") for w in words)
        plugin_code = render(self._spec(*selections, endpoints=endpoints))[
            "src/graftpunk_myshop/plugin.py"
        ]
        assert plugin_code.count("@command(") == _MAX_SCAFFOLD_ENDPOINTS + 1

    def test_a_name_given_twice_is_refused(self) -> None:
        spec = self._spec(
            CommandSelection("order", "GET", "/orders/{order_id}"),
            CommandSelection("order", "GET", "/search"),
            endpoints=(_ORDERS_ENDPOINT, _SEARCH_ENDPOINT),
        )
        with pytest.raises(CommandSelectionError, match="given twice"):
            render(spec)

    def test_an_endpoint_the_digest_lacks_is_refused(self) -> None:
        spec = self._spec(CommandSelection("x", "GET", "/nowhere"), endpoints=(_ORDERS_ENDPOINT,))
        with pytest.raises(CommandSelectionError, match="not an endpoint in this run"):
            render(spec)

    def test_a_login_flow_endpoint_is_refused(self) -> None:
        flagged = dataclasses.replace(_ORDERS_ENDPOINT, login_flow=True)
        spec = self._spec(CommandSelection("x", "GET", "/orders/{order_id}"), endpoints=(flagged,))
        with pytest.raises(CommandSelectionError, match="login flow"):
            render(spec)

    @pytest.mark.parametrize("name", ["class", "type", "match", "2fa", "has space", "a" * 41])
    def test_a_keyword_name_is_refused(self, name: str) -> None:
        """Soft keywords included, on every interpreter: "type" is refused on 3.11
        too, whose keyword module does not list it."""
        spec = self._spec(CommandSelection(name, "GET", "/search"), endpoints=(_SEARCH_ENDPOINT,))
        with pytest.raises(CommandSelectionError, match=repr(name)):
            render(spec)

    def test_building_the_spec_checks_only_its_shape(self) -> None:
        """Planning happens once, at render; building a spec does not plan."""
        with pytest.raises(CommandSelectionError, match="needs a digest"):
            ScaffoldSpec(
                name="myshop",
                mode="new_project",
                backend="nodriver",
                base_url="https://myshop.example.com",
                commands=(CommandSelection("x", "GET", "/nowhere"),),
            )
        self._spec(CommandSelection("x", "GET", "/nowhere"), endpoints=(_ORDERS_ENDPOINT,))

    def test_render_command_matches_the_stub_new_writes(self) -> None:
        d = _digest(endpoints=(_ORDERS_ENDPOINT,))
        command = plan_command(d, CommandSelection("order", "GET", "/orders/{order_id}"))
        rendered = render_command(command, d)
        plugin_code = render(
            self._spec(
                CommandSelection("order", "GET", "/orders/{order_id}"),
                endpoints=(_ORDERS_ENDPOINT,),
            )
        )["src/graftpunk_myshop/plugin.py"]
        assert "\n".join(rendered.lines) in plugin_code
        assert ("graftpunk.plugins", "PluginParamSpec") in rendered.imports
        assert {("graftpunk.plugins", "CommandContext"), ("graftpunk.plugins", "command")} <= set(
            rendered.imports
        )
        assert rendered.fixture == "get_orders_{order_id}.json"
```

Append to `tests/unit/test_scaffold_cli.py`:

```python
class TestPluginNewCommand:
    """--command selects and names the stubs (graft skill spec, 2026-09-21)."""

    @staticmethod
    def _record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        run_dir = observe_base / "myshop" / "run-1"
        run_dir.mkdir(parents=True)
        entries = [
            _entry("GET", "https://myshop.example/api/orders", body='{"orders": []}'),
            _entry("GET", "https://myshop.example/api/orders/1001", body='{"id": 1}'),
        ]
        (run_dir / "network.har").write_text(
            json.dumps({"log": {"version": "1.2", "entries": entries}})
        )

    def test_writes_only_the_selected_stubs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._record(tmp_path, monkeypatch)
        target = tmp_path / "out"
        result = runner.invoke(
            _build_app(),
            [
                "plugin",
                "new",
                "myshop",
                "--from-run",
                "myshop",
                "--dir",
                str(target),
                "--command",
                "order=GET /api/orders/{order_id}",
            ],
        )
        assert result.exit_code == 0, result.output
        plugin_code = (target / "src" / "graftpunk_myshop" / "plugin.py").read_text()
        assert "def order(" in plugin_code
        assert "def api_orders(" not in plugin_code
        assert "tests/fixtures/get_api_orders_{order_id}.json" in _plain(result.output)

    @pytest.mark.parametrize(
        "value", ["orders GET /api/orders", "=GET /api/orders", "orders=", "orders=get /api/orders"]
    )
    def test_a_malformed_value_is_refused_with_the_parsers_own_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        from graftpunk.har.naming import EndpointSpecError, parse_command_spec

        self._record(tmp_path, monkeypatch)
        with pytest.raises(EndpointSpecError) as caught:
            parse_command_spec(value)
        target = tmp_path / "out"
        result = runner.invoke(
            _build_app(),
            [
                "plugin",
                "new",
                "myshop",
                "--from-run",
                "myshop",
                "--dir",
                str(target),
                "--command",
                value,
            ],
        )
        assert result.exit_code == 1
        assert f"--command: {caught.value}" in " ".join(_plain(result.output).split())
        assert not target.exists()

    def test_a_colliding_name_is_refused_and_nothing_is_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._record(tmp_path, monkeypatch)
        target = tmp_path / "out"
        result = runner.invoke(
            _build_app(),
            [
                "plugin",
                "new",
                "myshop",
                "--from-run",
                "myshop",
                "--dir",
                str(target),
                "--command",
                "orders=GET /api/orders",
                "--command",
                "orders=GET /api/orders/{order_id}",
            ],
        )
        assert result.exit_code == 1
        assert "given twice" in _plain(result.output)
        assert not target.exists()

    def test_an_endpoint_the_digest_lacks_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._record(tmp_path, monkeypatch)
        result = runner.invoke(
            _build_app(),
            [
                "plugin",
                "new",
                "myshop",
                "--from-run",
                "myshop",
                "--dir",
                str(tmp_path / "out"),
                "--command",
                "x=GET /api/nowhere",
            ],
        )
        assert result.exit_code == 1
        assert "not an endpoint in this run" in _plain(result.output)

    def test_command_without_from_run_is_refused(self, tmp_path: Path) -> None:
        result = runner.invoke(
            _build_app(),
            [
                "plugin",
                "new",
                "myshop",
                "--dir",
                str(tmp_path),
                "--command",
                "orders=GET /api/orders",
            ],
        )
        assert result.exit_code == 1
        assert "--command requires --from-run" in _plain(result.output)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_selection.py tests/unit/test_scaffold_render.py::TestExplicitSelection tests/unit/test_scaffold_cli.py::TestPluginNewCommand -q`
Expected: collection errors, `ModuleNotFoundError: No module named 'graftpunk.devtools.scaffold.selection'`.

- [ ] **Step 3: Plan commands in `selection.py`**

Create `src/graftpunk/devtools/scaffold/selection.py`:

```python
"""Which commands a scaffold render or a stub insert produces, and under which names.

An explicit selection (``--command "<name>=<METHOD> <template>"``) is checked
against the digest here, once, with every refusal; the renderer is handed the
planned commands and decides only how each stub is spelled (graft skill spec,
2026-09-21). ``render.py`` and ``insert.py`` import this module; it imports
neither.
"""

from __future__ import annotations

import keyword
import re
from collections.abc import Sequence
from dataclasses import dataclass

from graftpunk.devtools.errors import DevtoolsRefusal
from graftpunk.har.digest import Endpoint, RunDigest
from graftpunk.har import naming
from graftpunk.har.naming import to_cli_name

__all__ = [
    "CommandSelection",
    "CommandSelectionError",
    "PlannedCommand",
    "command_identifier",
    "plan_command",
    "planned_commands",
]

_MAX_COMMAND_NAME = 40
# keyword.softkwlist on Python 3.13, the newest version CI runs
# (.github/workflows/python-quality.yml). Fixed here rather than read from the
# running interpreter, so a name refused on one Python is refused on every one:
# 3.11 lacks "type", which 3.12 added.
_SOFT_KEYWORDS = ("_", "case", "match", "type")
_COMMAND_NAME_RE = re.compile(rf"[A-Za-z][A-Za-z0-9_-]{{0,{_MAX_COMMAND_NAME - 1}}}")


@dataclass(frozen=True)
class CommandSelection:
    """One ``--command "<name>=<METHOD> <template>"``: an endpoint, under a name."""

    name: str
    method: str
    template: str


class CommandSelectionError(DevtoolsRefusal, ValueError):
    """An explicit selection the generator cannot render; nothing is written."""


def command_identifier(name: str) -> str:
    """The Python method name for the command a person named *name*.

    Raises:
        CommandSelectionError: *name* is not a letter followed by letters, digits,
            hyphens, and underscores within ``_MAX_COMMAND_NAME``, or it maps to
            a Python keyword.
    """
    if not _COMMAND_NAME_RE.fullmatch(name):
        raise CommandSelectionError(
            f"Command name {name!r} must start with a letter and contain only letters, "
            f"digits, hyphens, and underscores, and be at most {_MAX_COMMAND_NAME} characters."
        )
    identifier = name.replace("-", "_")
    if keyword.iskeyword(identifier) or identifier in _SOFT_KEYWORDS:
        raise CommandSelectionError(f"Command name {name!r} is a Python keyword; choose another.")
    return identifier


@dataclass(frozen=True)
class PlannedCommand:
    """One stub to render: its method name, the ``name=`` to pin when kebab-casing the
    method would not produce the agreed name, the HTTP method, and the endpoint."""

    identifier: str
    name_pin: str | None
    method: str
    endpoint: Endpoint

    @property
    def registered_name(self) -> str:
        """The name the CLI registers, by the rule the ``command`` decorator applies.
        The stub's help placeholder and the inserter's collision check both read
        it."""
        return naming.registered_name(self.name_pin, self.identifier)


def plan_command(d: RunDigest, selection: CommandSelection) -> PlannedCommand:
    """*selection* checked against *d*.

    Raises:
        CommandSelectionError: A name ``command_identifier`` refuses, an endpoint
            the digest does not hold, or one the digest marks ``login_flow``.
    """
    identifier = command_identifier(selection.name)
    endpoint = next(
        (
            e
            for e in d.endpoints
            if e.template == selection.template and selection.method in e.methods
        ),
        None,
    )
    shown = f"{selection.method} {selection.template}"
    if endpoint is None:
        raise CommandSelectionError(
            f"{shown} is not an endpoint in this run's digest; gp observe digest lists the "
            f"ones it holds."
        )
    if endpoint.login_flow:
        raise CommandSelectionError(
            f"{shown} is part of the login flow, which login_config drives; it cannot be a "
            f"command."
        )
    name_pin = None if to_cli_name(identifier) == selection.name else selection.name
    return PlannedCommand(identifier, name_pin, selection.method, endpoint)


def planned_commands(d: RunDigest, selections: Sequence[CommandSelection]) -> list[PlannedCommand]:
    """The explicit *selections*, each checked against *d*, uncapped and in the order
    given. Only the explicit case: whether a render uses a selection or its own
    default choice is the renderer's decision.

    Raises:
        CommandSelectionError: See :func:`plan_command`, or a name given twice.
    """
    planned: list[PlannedCommand] = []
    seen: set[str] = set()
    for selection in selections:
        command = plan_command(d, selection)
        if command.identifier in seen:
            raise CommandSelectionError(f"Command name {selection.name!r} is given twice.")
        seen.add(command.identifier)
        planned.append(command)
    return planned
```

In `src/graftpunk/devtools/scaffold/render.py`, delete `_MAX_COMMAND_NAME = 40` and import it, with `CommandSelection`, `CommandSelectionError`, `PlannedCommand`, and `planned_commands`, from `graftpunk.devtools.scaffold.selection` (the render tests keep importing `_MAX_COMMAND_NAME` from `render`, which still uses it in `_command_name`); add `"RenderedCommand"` and `"render_command"` to `__all__`; add to `ScaffoldSpec` the field `commands: tuple[CommandSelection, ...] = ()` after `graftpunk_version`, and extend its `__post_init__`:

```python
    def __post_init__(self) -> None:
        # Cheap shape checks only. Planning the selections (and every refusal it
        # makes) happens once, when render() plans the commands.
        validate_plugin_name(self.name)
        if self.commands and self.digest is None:
            raise CommandSelectionError(
                "An explicit command selection needs a digest to select from."
            )
```

and add below `_run_label`:

```python
@dataclass(frozen=True)
class RenderedCommand:
    """One stub, at class-body indentation with no trailing blank line; every
    ``(module, name)`` import the stub references, which an inserter merges as it is
    handed them; and the fixture filename its test looks for."""

    lines: tuple[str, ...]
    imports: tuple[tuple[str, str], ...]
    fixture: str


def _default_commands(d: RunDigest) -> list[PlannedCommand]:
    """Every eligible endpoint up to ``_MAX_SCAFFOLD_ENDPOINTS``, under generated names:
    what a render produces when the spec selects nothing."""
    seen_names: set[str] = set()
    return [
        PlannedCommand(_command_name(e.template, seen_names), None, e.methods[0], e)
        for e in _ordered_endpoints(d)[:_MAX_SCAFFOLD_ENDPOINTS]
    ]


def _planned(spec: ScaffoldSpec) -> list[PlannedCommand]:
    """The stubs a render produces: the spec's explicit selections, planned by
    ``selection.py``, or else the default choice. The renderer makes that choice;
    ``selection.py`` owns only the explicit case."""
    if spec.digest is None:
        return []
    if spec.commands:
        return planned_commands(spec.digest, spec.commands)
    return _default_commands(spec.digest)


def render_command(command: PlannedCommand, d: RunDigest) -> RenderedCommand:
    """The single-command entry point: the stub ``gp plugin new`` writes for *command*,
    which ``gp plugin add-command`` inserts on its own."""
    lines = _render_command_stub(command, _run_label(d))
    imports = [(_PLUGINS_MODULE, "CommandContext"), (_PLUGINS_MODULE, "command")]
    if _needs_param_specs(command.endpoint):
        imports.append((_PLUGINS_MODULE, "PluginParamSpec"))
    return RenderedCommand(
        lines=tuple(lines[:-1] if lines[-1] == "" else lines),
        imports=tuple(imports),
        fixture=capture_filename(
            command.method, command.endpoint.template, command.endpoint.content_type
        ),
    )
```

- [ ] **Step 4: Render every stub, test, and fixture path from the plan**

Still in `render.py`:

1. Change `_decorator_lines` to take the planned command, so the help placeholder, the name pin, and the endpoint all come from one value, and the placeholder names the command the way the CLI registers it (`describe api-orders`, not the method's `api_orders`):

```python
def _decorator_lines(command: PlannedCommand, param_specs: list[str]) -> list[str]:
    """A stub's ``@command(...)``, always exploded one keyword per line so the
    ``endpoint=`` declaration sits on a line of its own."""
    lines = [f"{L1}@command("]
    lines.extend(
        literal_lines(
            f"{GP_FILL_MARKER}: describe {command.registered_name}",
            indent=len(L2),
            prefix="help=",
        )
    )
    if command.name_pin is not None:
        lines.extend(literal_lines(command.name_pin, indent=len(L2), prefix="name="))
    if param_specs:
        lines.append(f"{L2}params=[")
        lines.extend(f"{L3}{spec}," for spec in param_specs)
        lines.append(f"{L2}],")
    endpoint_literal = f"{command.method} {command.endpoint.template}"
    lines.extend(literal_lines(endpoint_literal, indent=len(L2), prefix="endpoint="))
    lines.append(f"{L1})")
    return lines
```

In `docs/PLUGIN_DEVELOPMENT.md`, "What gets filled in", change `help="GP-FILL: describe api_orders",` in the generated example to `help="GP-FILL: describe api-orders",`.

2. Change `_render_command_stub`'s signature to `(command: PlannedCommand, run_label: str) -> list[str]`, and replace its first two lines with:

```python
    endpoint = command.endpoint
    method = command.method
    name = command.identifier
```

and its `_decorator_lines(...)` call with `_decorator_lines(command, param_specs if _needs_param_specs(endpoint) else [])`.

3. Replace `_stub_endpoints` and `_render_command_stubs` with:

```python
def _render_command_stubs(spec: ScaffoldSpec, planned: list[PlannedCommand]) -> list[str]:
    if spec.digest is None or not planned:
        return [
            f'    @command(help="{GP_FILL_MARKER}: describe this command")',
            "    def example(self, ctx: CommandContext) -> dict:",
            f'        """{GP_FILL_MARKER}: what this command does."""',
            f'        return ctx.request_json("GET", "/{GP_FILL_MARKER}/path")',
        ]
    lines: list[str] = []
    for command in planned:
        lines.extend(_render_command_stub(command, _run_label(spec.digest)))
    return lines
```

4. Give `_render_plugin_module` a second parameter, `planned: list[PlannedCommand]`, replace `any(_needs_param_specs(e) for e in _stub_endpoints(spec))` with `any(_needs_param_specs(c.endpoint) for c in planned)`, and pass `planned` on to `_render_command_stubs(spec, planned)`.

5. Replace the body of `fixture_paths` with:

```python
    return [
        f"{fixtures_root_for(spec)}"
        f"{capture_filename(c.method, c.endpoint.template, c.endpoint.content_type)}"
        for c in _planned(spec)
    ]
```

6. Give `_render_test_module` the parameter `planned: list[PlannedCommand]` after `spec`, replace `endpoints = _stub_endpoints(spec)` and `has_endpoint_tests = bool(endpoints)` with `has_endpoint_tests = bool(planned)`, and replace the loop head through `_, path_params = ...` with:

```python
    for command in planned:
        name = command.identifier
        # The same seeding as _render_command_stub, so the identifiers here are
        # the ones the stub actually declares.
        _, path_params = _templated_url(command.endpoint.template, {"self", "ctx"})
```

(delete the now-unused `seen: set[str] = set()` line above the loop).

7. Plan once per render. In `render`, add `planned = _planned(spec)` as its first line, and pass it on: `plugin_module = _render_plugin_module(spec, planned)`, and `_render_test_module(spec, planned, package=package)` in both branches. `fixture_paths` is a separate public entry point (the CLI's `Next:` lines), so it plans on its own, once per call.

- [ ] **Step 5: Add `--command` to `gp plugin new`**

In `src/graftpunk/cli/scaffold_commands.py`, import `CommandSelection` and `CommandSelectionError` from `graftpunk.devtools.scaffold.selection`, and `EndpointSpecError`, `parse_command_spec` from `graftpunk.har.naming`; add above `plugin_new`:

```python
def _command_selections(values: list[str]) -> tuple[CommandSelection, ...]:
    """Every ``--command`` value as a selection, refusing the first that does not parse
    with :func:`parse_command_spec`'s own text. Both scaffold commands call this, and
    neither splits a value itself."""
    selections: list[CommandSelection] = []
    for value in values:
        try:
            name, method, template = parse_command_spec(value)
        except EndpointSpecError as exc:
            LOG.debug("scaffold_refused", reason="bad_command")
            console.print(f"[red]--command: {escape(str(exc))}[/red]")
            raise typer.Exit(1) from None
        selections.append(CommandSelection(name=name, method=method, template=template))
    return tuple(selections)
```

add this parameter to `plugin_new` after `check_name`:

```python
    command: Annotated[
        list[str],
        typer.Option(
            "--command",
            help='"NAME=METHOD template": stub only these endpoints, under these names '
            "(repeatable; needs --from-run)",
        ),
    ] = [],  # noqa: B006 - Typer reads this default at decoration time, never mutated per-call
```

after the `--run requires --from-run` block add:

```python
    selections = _command_selections(command)
    if selections and from_run is None:
        LOG.debug("scaffold_refused", reason="command_without_from_run")
        console.print("[red]--command requires --from-run.[/red]")
        raise typer.Exit(1)
```

pass `commands=selections,` to the `ScaffoldSpec(...)` call, and add this arm directly above `except NotAPluginSuiteError as exc:` (the refusal comes from `render`, which `write_scaffold` calls before it checks for conflicts or writes anything, inside the same `try`):

```python
    except CommandSelectionError as exc:
        LOG.debug("scaffold_refused", reason="bad_command")
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_selection.py tests/unit/test_scaffold_render.py tests/unit/test_scaffold_cli.py tests/unit/test_scaffold_project.py tests/unit/test_plugin_project.py tests/unit/test_devtools_errors.py tests/unit/test_plugin_development_guide.py -q`
Expected: PASS, including the ruff-clean tree tests.

- [ ] **Step 7: Commit**

```bash
git add src/graftpunk/devtools/scaffold/selection.py src/graftpunk/devtools/scaffold/render.py src/graftpunk/cli/scaffold_commands.py docs/PLUGIN_DEVELOPMENT.md tests/unit/test_scaffold_selection.py tests/unit/test_scaffold_render.py tests/unit/test_scaffold_cli.py tests/unit/test_devtools_errors.py
git commit -m "feat(scaffold): gp plugin new --command selects and names the stubs through one single-command renderer"
```

---

### Task 6: `insert.py` and `gp plugin add-command`

**Files:**
- Create: `src/graftpunk/devtools/scaffold/insert.py`
- Modify: `src/graftpunk/devtools/scaffold/__init__.py:1-6` (module docstring: this task adds the second `gp plugin` command that calls into the package, so "`gp plugin new` is the only caller" stops being true here)
- Modify: `src/graftpunk/cli/scaffold_commands.py` (new `plugin_add_command`)
- Test: `tests/unit/test_plugin_project_cli.py`

**Interfaces:**
- Consumes: `require_plugin_project`, `PluginView` (with `entry_point`), `ProjectView.defects`, `CommandView.cli_name` (Task 3); `DevtoolsRefusal` and `ScaffoldWriteError` (foundations Task 10); `pysrc.with_import` and `ImportPlacementError` (Task 2); `plan_command`, `PlannedCommand.registered_name`, `CommandSelection` (Task 5, `selection.py`); `render_command`, `RenderedCommand.imports` (Task 5, `render.py`); `_command_selections` (Task 5); `apply_changes`, `PlannedChange`, `validate_python` (foundations Task 10); `resolve_run` (existing, `src/graftpunk/cli/observe_commands.py`).
- Produces: `insert.CommandInsertError(DevtoolsRefusal, ValueError)`, raised when the target plugin is defective (and only then: another plugin's defect does not stop it), when no entry point has the given name (the message lists the entry-point names that exist), and when the module's imports are in a shape `with_import` will not place into; `@dataclass(frozen=True) class AddedCommand(module: Path, cli_name: str, fixture: str)`; `insertion_line(plugin: PluginView) -> int`; `add_command(root: Path, entry_point: str, d: RunDigest, selection: CommandSelection) -> AddedCommand`, which looks up both the defect and the plugin by `entry_point`; `gp plugin add-command PLUGIN --from-run SESSION --command "NAME=METHOD template" [--run RUN_ID] [--dir PATH]`, where `PLUGIN` is the entry-point name `gp plugin info --json` reports. A write that fails reaches the entry point as `ScaffoldWriteError`, a `DevtoolsRefusal`, so `except DevtoolsRefusal` is its only arm. `tests/unit/test_scaffold_write.py` finds `insert.py` among the modules that apply changes without being told (foundations Task 10).

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_devtools_errors.py`, add `CommandInsertError` (from `graftpunk.devtools.scaffold.insert`) to the loop's tuple.

Append to `tests/unit/test_plugin_project_cli.py` (add `import ast`, `import subprocess`, `import sys`, and `from graftpunk.devtools.plugin_project import read_project` to its imports):

```python
def _add(project: Path, plugin: str, command: str) -> object:
    return runner.invoke(
        app,
        [
            "plugin",
            "add-command",
            plugin,
            "--from-run",
            "myshop",
            "--command",
            command,
            "--dir",
            str(project),
        ],
    )


def _snapshot(root: Path, *, skip: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and p != skip
    }


def _ruff_clean(project: Path) -> None:
    for argv in (["check", "."], ["format", "--check", "."]):
        result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [sys.executable, "-m", "ruff", *argv], cwd=project, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stdout + result.stderr


_HAND_WRITTEN = """\
\"\"\"myshop plugin.\"\"\"

from __future__ import annotations

import functools

from graftpunk.plugins import SitePlugin


class MyshopPlugin(SitePlugin):
    site_name = "myshop"
    base_url = "https://myshop.example"


@functools.cache
def _helper() -> int:
    return 1


if __name__ == "__main__":
    print(_helper())
"""


def _hand_written_project(root: Path, entry_point: str = "myshop") -> Path:
    (root / "pyproject.toml").write_text(
        '[project]\nname = "graftpunk-myshop"\n\n'
        '[project.entry-points."graftpunk.plugins"]\n'
        f'{entry_point} = "graftpunk_myshop.plugin:MyshopPlugin"\n\n'
        "[tool.ruff]\nline-length = 100\n\n"
        '[tool.ruff.lint]\nselect = ["E", "F", "I", "UP", "B"]\n'
    )
    package = root / "src" / "graftpunk_myshop"
    package.mkdir(parents=True)
    module = package / "plugin.py"
    module.write_text(_HAND_WRITTEN)
    return module


class TestAddCommand:
    def test_the_stub_goes_after_the_last_command(self, recorded: Path) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        result = _add(recorded, "myshop", "order=GET /api/orders/{order_id}")
        assert result.exit_code == 0, result.output
        (plugin,) = read_project(recorded).plugins
        assert [c.method for c in plugin.commands] == ["orders", "order"]
        assert plugin.commands[-1].endpoint == "GET /api/orders/{order_id}"
        _ruff_clean(recorded)

    def test_a_first_command_goes_after_the_class_bodys_last_statement(
        self, recorded: Path
    ) -> None:
        module = _hand_written_project(recorded)
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 0, result.output
        (plugin,) = read_project(recorded).plugins
        (command,) = plugin.commands
        assert command.span.start > module.read_text().splitlines().index(
            '    base_url = "https://myshop.example"'
        )

    def test_a_decorated_helper_and_a_main_block_below_the_class_stay_below_it(
        self, recorded: Path
    ) -> None:
        module = _hand_written_project(recorded)
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 0, result.output
        tree = ast.parse(module.read_text())
        (klass,) = [n for n in tree.body if isinstance(n, ast.ClassDef)]
        helper = next(n for n in tree.body if isinstance(n, ast.FunctionDef))
        assert any(isinstance(n, ast.FunctionDef) and n.name == "orders" for n in klass.body)
        assert klass.end_lineno is not None and klass.end_lineno < helper.lineno
        _ruff_clean(recorded)

    def test_a_typed_stub_imports_what_it_uses(self, recorded: Path) -> None:
        module = _hand_written_project(recorded)
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 0, result.output
        imported = {
            alias.name
            for node in ast.parse(module.read_text()).body
            if isinstance(node, ast.ImportFrom) and node.module == "graftpunk.plugins"
            for alias in node.names
        }
        assert {"CommandContext", "PluginParamSpec", "SitePlugin", "command"} <= imported

    def test_a_duplicate_name_is_refused_and_nothing_changes(self, recorded: Path) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        before = _snapshot(recorded, skip=recorded / "nothing")
        result = _add(recorded, "myshop", "orders=GET /api/orders/{order_id}")
        assert result.exit_code == 1
        assert "already has a command named 'orders'" in _plain(result.output)
        assert _snapshot(recorded, skip=recorded / "nothing") == before

    def test_a_hyphen_underscore_collision_is_refused(self, recorded: Path) -> None:
        _new(recorded, "myshop", "order_list=GET /api/orders")
        result = _add(recorded, "myshop", "order-list=GET /api/orders/{order_id}")
        assert result.exit_code == 1
        assert "already has a command" in _plain(result.output)

    def test_a_login_flow_endpoint_is_refused(self, recorded: Path) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        result = _add(recorded, "myshop", "session=POST /session")
        assert result.exit_code == 1
        assert "login flow" in _plain(result.output)

    def test_nothing_but_the_module_is_touched(self, recorded: Path) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        module = recorded / "src" / "graftpunk_myshop" / "plugin.py"
        before = _snapshot(recorded, skip=module)
        assert _add(recorded, "myshop", "invoices=GET /api/invoices").exit_code == 0
        assert _snapshot(recorded, skip=module) == before

    def test_the_fixture_path_for_a_standalone_project(self, recorded: Path) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        result = _add(recorded, "myshop", "order=GET /api/orders/{order_id}")
        assert "tests/fixtures/get_api_orders_{order_id}.json" in _plain(result.output)

    def test_the_fixture_path_for_a_suite_member(self, recorded: Path) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        _new(recorded, "widgets", "orders=GET /api/orders")
        result = _add(recorded, "widgets", "order=GET /api/orders/{order_id}")
        assert result.exit_code == 0, result.output
        assert "tests/fixtures/widgets/get_api_orders_{order_id}.json" in _plain(result.output)

    def test_a_defective_target_is_refused_and_another_plugins_defect_is_not(
        self, recorded: Path
    ) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        _new(recorded, "widgets", "orders=GET /api/orders")
        widgets = recorded / "src" / "graftpunk_widgets" / "plugin.py"
        widgets.write_text(widgets.read_text() + "\n\nclass Other(SitePlugin):\n    pass\n")
        refused = _add(recorded, "widgets", "order=GET /api/orders/{order_id}")
        assert refused.exit_code == 1
        assert "exactly one SitePlugin subclass" in " ".join(_plain(refused.output).split())
        assert _add(recorded, "myshop", "order=GET /api/orders/{order_id}").exit_code == 0

    def test_an_unknown_plugin_is_refused(self, recorded: Path) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        result = _add(recorded, "elsewhere", "order=GET /api/orders/{order_id}")
        assert result.exit_code == 1
        assert "myshop" in _plain(result.output)

    def test_the_plugin_is_addressed_by_its_entry_point_name(self, recorded: Path) -> None:
        """A hand-written project whose entry-point name is not its site_name: the
        entry point is the identity gp plugin info reports and add-command takes."""
        module = _hand_written_project(recorded, entry_point="shop")
        by_site_name = _add(recorded, "myshop", "orders=GET /api/orders")
        assert by_site_name.exit_code == 1
        assert "its entry points are: shop." in " ".join(_plain(by_site_name.output).split())
        assert module.read_text() == _HAND_WRITTEN
        added = _add(recorded, "shop", "orders=GET /api/orders")
        assert added.exit_code == 0, added.output
        (plugin,) = read_project(recorded).plugins
        assert (plugin.entry_point, plugin.site_name) == ("shop", "myshop")
        assert [c.method for c in plugin.commands] == ["orders"]

    def test_a_defect_on_a_plugin_addressed_by_its_entry_point_is_refused(
        self, recorded: Path
    ) -> None:
        module = _hand_written_project(recorded, entry_point="shop")
        module.write_text(_HAND_WRITTEN + "\n\nclass Other(SitePlugin):\n    pass\n")
        before = module.read_bytes()
        refused = _add(recorded, "shop", "orders=GET /api/orders")
        assert refused.exit_code == 1
        assert "exactly one SitePlugin subclass" in " ".join(_plain(refused.output).split())
        assert module.read_bytes() == before

    def test_a_failed_write_is_one_line_exit_1_and_the_original_bytes(
        self, recorded: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk.devtools.scaffold import write

        _new(recorded, "myshop", "orders=GET /api/orders")
        before = _snapshot(recorded, skip=recorded / "nothing")

        def failing(path: Path, text: str) -> None:
            raise OSError(28, "No space left on device", str(path))

        monkeypatch.setattr(write, "_write_atomically", failing)
        result = _add(recorded, "myshop", "invoices=GET /api/invoices")
        assert result.exit_code == 1, result.output
        (line,) = _plain(result.output).strip().splitlines()
        assert line.startswith("Could not write ")
        assert "No space left on device" in line
        assert _snapshot(recorded, skip=recorded / "nothing") == before

    @pytest.mark.parametrize("value", ["orders GET /api/orders", "=GET /api/orders", "orders="])
    def test_both_entry_points_refuse_a_malformed_value_with_the_same_text(
        self, recorded: Path, value: str
    ) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        added = _add(recorded, "myshop", value)
        created = runner.invoke(
            app,
            [
                "plugin",
                "new",
                "other",
                "--from-run",
                "myshop",
                "--dir",
                str(recorded / "other"),
                "--command",
                value,
            ],
        )
        assert added.exit_code == created.exit_code == 1
        assert _plain(added.output) == _plain(created.output)

    def test_a_conftest_that_does_not_parse_does_not_stop_it(self, recorded: Path) -> None:
        """add-command edits the plugin module and never reads the conftest."""
        _new(recorded, "myshop", "orders=GET /api/orders")
        (recorded / "tests" / "conftest.py").write_text("def (:\n")
        result = _add(recorded, "myshop", "invoices=GET /api/invoices")
        assert result.exit_code == 0, result.output

    def test_a_directory_that_is_not_a_plugin_project_is_refused(self, recorded: Path) -> None:
        """The recording exists and the project directory is empty."""
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 1
        assert "not a graftpunk plugin project (empty)" in " ".join(_plain(result.output).split())
```

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_plugin_project_cli.py::TestAddCommand -q`
Expected: FAIL (`No such command 'add-command'`).

- [ ] **Step 3: Write the inserter**

Create `src/graftpunk/devtools/scaffold/insert.py`:

```python
"""Places one rendered command stub inside a plugin class, through ``write.py``.

The placement rule is code, and total over any class the project reader
accepts: immediately after the class's last ``@command``-decorated method, or,
for a class with no command yet, immediately after the class body's last
statement, wherever the class sits in the module. A module a developer has
extended with helpers above or below the class is handled rather than refused
(graft skill spec, 2026-09-21). It inserts a stub, the imports the rendered stub says it
references, and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from graftpunk.devtools.errors import DevtoolsRefusal
from graftpunk.devtools.plugin_project import PluginView, require_plugin_project
from graftpunk.devtools.scaffold.pysrc import ImportPlacementError, with_import
from graftpunk.devtools.scaffold.render import render_command
from graftpunk.devtools.scaffold.selection import CommandSelection, plan_command
from graftpunk.devtools.scaffold.write import PlannedChange, apply_changes, validate_python
from graftpunk.har.digest import RunDigest

__all__ = ["AddedCommand", "CommandInsertError", "add_command", "insertion_line"]


class CommandInsertError(DevtoolsRefusal, ValueError):
    """The stub cannot be added; nothing was written."""


@dataclass(frozen=True)
class AddedCommand:
    """What ``add_command`` did: the module it edited, the command's CLI name, and the
    project-relative fixture its test will look for."""

    module: Path
    cli_name: str
    fixture: str


def insertion_line(plugin: PluginView) -> int:
    """The line after which a new command goes."""
    return plugin.commands[-1].span.end if plugin.commands else plugin.class_span.end


def _taken_names(plugin: PluginView) -> set[str]:
    taken: set[str] = set()
    for command in plugin.commands:
        taken.add(command.method)
        taken.add(command.cli_name)
    return taken


def add_command(
    root: Path, entry_point: str, d: RunDigest, selection: CommandSelection
) -> AddedCommand:
    """Add *selection*'s stub to the plugin whose entry-point name is *entry_point*,
    the one identity ``gp plugin info --json`` reports for it.

    Raises:
        CommandInsertError: The target plugin's module is defective, no entry
            point has that name, the command name is taken (as a method name or a
            CLI name), or the module's imports are in a shape ``with_import`` does
            not place into.
        CommandSelectionError: See :func:`plan_command`.
        NotAPluginProjectError: *root* is not a plugin project.
        PluginProjectError: See :func:`read_project`.
        ScaffoldWriteError: The write failed; the module was restored first, or
            the error names it as left changed.
    """
    view = require_plugin_project(root)
    defect = next((d for d in view.defects if d.entry_point == entry_point), None)
    if defect is not None:
        raise CommandInsertError(defect.message)
    plugin = next((p for p in view.plugins if p.entry_point == entry_point), None)
    if plugin is None:
        names = ", ".join(
            sorted({p.entry_point for p in view.plugins} | {d.entry_point for d in view.defects})
        )
        raise CommandInsertError(
            f"No plugin has the entry-point name {entry_point!r} in {root}; "
            f"its entry points are: {names}."
        )
    command = plan_command(d, selection)
    if {command.identifier, command.registered_name} & _taken_names(plugin):
        raise CommandInsertError(
            f"{plugin.module_path} already has a command named {selection.name!r}."
        )
    rendered = render_command(command, d)
    module = root / plugin.module_path
    original = module.read_text(encoding="utf-8")
    lines = original.splitlines()
    at = insertion_line(plugin)
    text = "\n".join([*lines[:at], "", *rendered.lines, *lines[at:]]) + "\n"
    try:
        for imported_from, name in rendered.imports:
            text = with_import(text, imported_from, name)
    except ImportPlacementError as exc:
        raise CommandInsertError(f"{plugin.module_path}: {exc}") from exc
    apply_changes([PlannedChange(module, text, original=original, validate=validate_python)])
    return AddedCommand(
        module=module,
        cli_name=command.registered_name,
        fixture=f"{plugin.fixtures_root}{rendered.fixture}",
    )
```

Then replace the module docstring of `src/graftpunk/devtools/scaffold/__init__.py`, whose "`gp plugin new` is the only caller" this task makes false, with:

```python
"""Plugin scaffolding: the generators and mutators of a plugin project that the
``gp plugin`` commands call.

Nothing here is imported by a generated plugin at runtime (plugin tooling spec,
2026-09-11).
"""
```

- [ ] **Step 4: Add the command**

In `src/graftpunk/cli/scaffold_commands.py`, add `from graftpunk.devtools.scaffold.insert import add_command`, and append:

```python
@plugin_app.command("add-command")
def plugin_add_command(
    plugin: Annotated[
        str,
        typer.Argument(help="The plugin's entry-point name, as gp plugin info --json reports it"),
    ],
    from_run: Annotated[
        str, typer.Option("--from-run", help="SESSION: take the endpoint from its newest run")
    ],
    command: Annotated[
        str, typer.Option("--command", help='"NAME=METHOD template": the command to add')
    ],
    run: Annotated[
        str | None, typer.Option("--run", help="RUN_ID: use this run instead of the newest one")
    ] = None,
    dir_: Annotated[Path, typer.Option("--dir", help="Project directory")] = Path("."),
) -> None:
    """Add one command stub to a plugin, in the shape gp plugin new writes."""
    (selection,) = _command_selections([command])
    run_dir = resolve_run(from_run, run)
    run_digest = digest(DigestSource.from_run_dir(run_dir, session=from_run, run_id=run_dir.name))
    try:
        added = add_command(dir_, plugin, run_digest, selection)
    except DevtoolsRefusal as exc:
        LOG.debug("add_command_refused", reason=type(exc).__name__)
        console.print(f"[red]{escape(str(exc))}[/red]", soft_wrap=True)
        raise typer.Exit(1) from None
    console.print(
        f"[green]Added[/green] {escape(added.cli_name)} to {escape(str(added.module))}",
        soft_wrap=True,
    )
    console.print(
        f"[bold]Next:[/bold] its test looks for {escape(added.fixture)}", soft_wrap=True
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_plugin_project_cli.py tests/unit/test_scaffold_write.py tests/unit/test_scaffold_pysrc.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/devtools/scaffold/insert.py src/graftpunk/devtools/scaffold/__init__.py src/graftpunk/cli/scaffold_commands.py tests/unit/test_plugin_project_cli.py tests/unit/test_devtools_errors.py
git commit -m "feat(scaffold): gp plugin add-command inserts one generated stub into an existing plugin"
```

---

### Task 7: `upgrade.py` and `gp plugin upgrade`

**Files:**
- Create: `src/graftpunk/devtools/scaffold/upgrade.py`
- Modify: `src/graftpunk/cli/scaffold_commands.py` (new `plugin_upgrade`)
- Test: `tests/unit/test_scaffold_upgrade.py`, `tests/unit/test_plugin_project_cli.py`

**Interfaces:**
- Consumes: `require_plugin_project`, `NotAPluginProjectError`, `ProjectView.missing_requirements()`, and `ProjectView.unreadable_files()` (Task 3); `ProjectRequirement` (Task 2); `pysrc.with_bindings` (Task 2), the same assembler the renderer uses for a new conftest; `apply_changes`, `PlannedChange`, `validate_python`, `DevtoolsRefusal`, and `ScaffoldWriteError` (foundations Task 10).
- Produces: `upgrade.UpgradeRefusedError(DevtoolsRefusal, ValueError)`, raised for a requirement's file that does not parse (each file in `ProjectView.unreadable_files()`, named once with its reason) and for a file whose imports `pysrc.with_import` will not place (its message then tells the user to run `ruff check --fix` after adding the import by hand); a directory that is not a plugin project is refused by `require_plugin_project` with `NotAPluginProjectError`; a plugin defect does not stop it, since it edits no plugin module; `upgrade_project(root: Path) -> tuple[ProjectRequirement, ...]` (the requirements it applied); `gp plugin upgrade [--dir PATH]`, whose only arm is `except DevtoolsRefusal`, which covers a failed write (`ScaffoldWriteError`). `tests/unit/test_scaffold_write.py` finds `upgrade.py` among the modules that apply changes without being told (foundations Task 10).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_scaffold_upgrade.py`:

```python
"""gp plugin upgrade's migrator (graft skill spec, 2026-09-21)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from graftpunk.devtools.plugin_project import NotAPluginProjectError, read_project
from graftpunk.devtools.scaffold.project import write_scaffold
from graftpunk.devtools.scaffold.render import ScaffoldSpec, render
from graftpunk.devtools.scaffold.upgrade import UpgradeRefusedError, upgrade_project

_SPEC = ScaffoldSpec(
    name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example"
)
_OLD_CONFTEST = (
    "from graftpunk.testing.plugin import site_env_scrubber\n"
    "\n"
    'scrub_site_env = site_env_scrubber("MYSHOP_")\n'
)


def _project(tmp_path: Path, conftest: str | None) -> Path:
    write_scaffold(tmp_path, _SPEC)
    path = tmp_path / "tests" / "conftest.py"
    if conftest is None:
        path.unlink()
    else:
        path.write_text(conftest)
    return path


def _ruff_check(project: Path) -> None:
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [sys.executable, "-m", "ruff", "check", "tests/conftest.py"],
        cwd=project,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


class TestUpgrade:
    def test_a_project_lacking_the_wiring_gets_exactly_the_generated_conftest(
        self, tmp_path: Path
    ) -> None:
        conftest = _project(tmp_path, _OLD_CONFTEST)
        applied = upgrade_project(tmp_path)
        assert [r.name for r in applied] == ["FIXTURES_TREE", "sanitised_fixtures"]
        assert conftest.read_text() == render(_SPEC)["tests/conftest.py"]
        states = {s.state for s in read_project(tmp_path).requirements.values()}
        assert states == {"bound"}
        _ruff_check(tmp_path)

    def test_a_project_that_has_it_is_left_byte_identical(self, tmp_path: Path) -> None:
        write_scaffold(tmp_path, _SPEC)
        conftest = tmp_path / "tests" / "conftest.py"
        before = conftest.read_bytes()
        assert upgrade_project(tmp_path) == ()
        assert conftest.read_bytes() == before

    def test_twice_in_a_row_writes_each_statement_once(self, tmp_path: Path) -> None:
        conftest = _project(tmp_path, _OLD_CONFTEST)
        upgrade_project(tmp_path)
        assert upgrade_project(tmp_path) == ()
        text = conftest.read_text()
        assert text.count("FIXTURES_TREE = ") == 1
        assert text.count("sanitised_fixtures = ") == 1

    def test_a_wiring_in_another_spelling_is_left_alone(self, tmp_path: Path) -> None:
        wired = (
            "from pathlib import Path\n\n"
            "from graftpunk.testing.plugin import fixtures_are_sanitised as check_tree\n\n"
            "FIXTURES_TREE = (\n    Path(__file__).parent\n    / \"fixtures\"\n)\n"
            "sanitised_fixtures = check_tree(FIXTURES_TREE)\n"
        )
        conftest = _project(tmp_path, wired)
        assert upgrade_project(tmp_path) == ()
        assert conftest.read_text() == wired

    def test_a_missing_conftest_is_created(self, tmp_path: Path) -> None:
        conftest = _project(tmp_path, None)
        upgrade_project(tmp_path)
        assert conftest.read_text() == (
            "from pathlib import Path\n"
            "\n"
            "from graftpunk.testing.plugin import fixtures_are_sanitised\n"
            "\n"
            'FIXTURES_TREE = Path(__file__).parent / "fixtures"\n'
            "sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)\n"
        )
        _ruff_check(tmp_path)

    def test_statements_after_a_function_keep_two_blank_lines(self, tmp_path: Path) -> None:
        conftest = _project(
            tmp_path,
            _OLD_CONFTEST
            + "\n\ndef helper_fixture() -> int:\n    return 1\n",
        )
        upgrade_project(tmp_path)
        assert "    return 1\n\n\nFIXTURES_TREE = " in conftest.read_text()
        _ruff_check(tmp_path)

    def test_a_directory_that_is_not_a_plugin_project_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(NotAPluginProjectError, match="empty"):
            upgrade_project(tmp_path)

    def test_a_conftest_that_does_not_parse_is_refused_and_left_alone(
        self, tmp_path: Path
    ) -> None:
        broken = "def (:\n"
        conftest = _project(tmp_path, broken)
        with pytest.raises(UpgradeRefusedError, match="does not parse") as refused:
            upgrade_project(tmp_path)
        assert str(refused.value).count("tests/conftest.py") == 1, "one line per file"
        assert conftest.read_text() == broken

    def test_a_conftest_whose_imports_follow_code_is_refused_and_left_alone(
        self, tmp_path: Path
    ) -> None:
        odd = 'X = 1\nfrom graftpunk.testing.plugin import site_env_scrubber\n'
        conftest = _project(tmp_path, odd)
        with pytest.raises(UpgradeRefusedError, match="ruff check --fix"):
            upgrade_project(tmp_path)
        assert conftest.read_text() == odd
```

In `tests/unit/test_devtools_errors.py`, add `UpgradeRefusedError` (from `graftpunk.devtools.scaffold.upgrade`) to the loop's tuple.

Append to `tests/unit/test_plugin_project_cli.py`:

```python
def _project_lacking_the_wiring(root: Path) -> Path:
    """A generated project whose conftest predates the sanitisation wiring."""
    from graftpunk.devtools.scaffold.project import write_scaffold
    from graftpunk.devtools.scaffold.render import ScaffoldSpec

    write_scaffold(
        root,
        ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example",
        ),
    )
    conftest = root / "tests" / "conftest.py"
    conftest.write_text(
        "from graftpunk.testing.plugin import site_env_scrubber\n\n"
        'scrub_site_env = site_env_scrubber("MYSHOP_")\n'
    )
    return conftest


class TestPluginUpgrade:
    def test_a_failed_write_is_one_line_exit_1_and_the_original_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk.devtools.scaffold import write

        conftest = _project_lacking_the_wiring(tmp_path)
        before = conftest.read_bytes()

        def failing(path: Path, text: str) -> None:
            raise OSError(28, "No space left on device", str(path))

        monkeypatch.setattr(write, "_write_atomically", failing)
        result = runner.invoke(app, ["plugin", "upgrade", "--dir", str(tmp_path)])
        assert result.exit_code == 1, result.output
        (line,) = _plain(result.output).strip().splitlines()
        assert line.startswith("Could not write ")
        assert "No space left on device" in line
        assert conftest.read_bytes() == before

    def test_prints_what_it_added_and_then_nothing(self, tmp_path: Path) -> None:
        _project_lacking_the_wiring(tmp_path)
        first = runner.invoke(app, ["plugin", "upgrade", "--dir", str(tmp_path)])
        assert first.exit_code == 0, first.output
        assert "tests/conftest.py: added FIXTURES_TREE" in _plain(first.output)
        assert "tests/conftest.py: added sanitised_fixtures" in _plain(first.output)
        second = runner.invoke(app, ["plugin", "upgrade", "--dir", str(tmp_path)])
        assert second.exit_code == 0
        assert "Nothing to upgrade" in _plain(second.output)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_upgrade.py tests/unit/test_plugin_project_cli.py::TestPluginUpgrade -q`
Expected: collection error, `ModuleNotFoundError: No module named 'graftpunk.devtools.scaffold.upgrade'`.

- [ ] **Step 3: Write the migrator**

Create `src/graftpunk/devtools/scaffold/upgrade.py`:

```python
"""Brings an existing plugin project up to ``PROJECT_REQUIREMENTS``, through ``write.py``.

On purpose and by name: ``gp plugin upgrade`` runs it, and nothing else does.
It applies only what the project reader's view says is missing, so it is
idempotent under the reader's own definition, and it changes nothing a project
already has (graft skill spec, 2026-09-21). The statements are added by
``pysrc.with_bindings``, the assembler the renderer uses for a new conftest, so
an upgraded conftest is byte-identical to a generated one.
"""

from __future__ import annotations

from pathlib import Path

from graftpunk.devtools.errors import DevtoolsRefusal
from graftpunk.devtools.plugin_project import require_plugin_project
from graftpunk.devtools.scaffold.policy import ProjectRequirement
from graftpunk.devtools.scaffold.pysrc import ImportPlacementError, with_bindings
from graftpunk.devtools.scaffold.write import PlannedChange, apply_changes, validate_python

__all__ = ["UpgradeRefusedError", "upgrade_project"]


class UpgradeRefusedError(DevtoolsRefusal, ValueError):
    """A requirement's file does not parse, or its imports are not in a shape the
    migrator places into; nothing was written."""


def upgrade_project(root: Path) -> tuple[ProjectRequirement, ...]:
    """Apply every requirement *root*'s project lacks, and return them.

    Raises:
        UpgradeRefusedError: A requirement's file does not parse, or its imports
            are not in a shape ``pysrc.with_import`` places into.
        NotAPluginProjectError: *root* is not a plugin project.
        PluginProjectError: See :func:`read_project`.
        ScaffoldWriteError: A write failed; every file was restored first, or the
            error names what was left changed.
    """
    view = require_plugin_project(root)
    unreadable = view.unreadable_files()
    if unreadable:
        listing = "; ".join(f"{path}: {reason}" for path, reason in unreadable)
        raise UpgradeRefusedError(
            f"{listing}. gp plugin upgrade edits only a file it can parse; fix it by "
            f"hand, then run it again."
        )
    missing = view.missing_requirements()
    by_path: dict[str, list[ProjectRequirement]] = {}
    for requirement in missing:
        by_path.setdefault(requirement.path, []).append(requirement)
    changes: list[PlannedChange] = []
    for relative, requirements in by_path.items():
        path = root / relative
        original = path.read_text(encoding="utf-8") if path.is_file() else None
        try:
            content = with_bindings(original or "", requirements)
        except ImportPlacementError as exc:
            raise UpgradeRefusedError(f"{relative}: {exc}") from exc
        changes.append(PlannedChange(path, content, original=original, validate=validate_python))
    apply_changes(changes)
    return missing
```

- [ ] **Step 4: Add the command**

In `src/graftpunk/cli/scaffold_commands.py`, add `from graftpunk.devtools.scaffold.upgrade import upgrade_project`, and append:

```python
@plugin_app.command("upgrade")
def plugin_upgrade(
    dir_: Annotated[Path, typer.Option("--dir", help="Project directory")] = Path("."),
) -> None:
    """Bring a plugin project up to the current generated shape, changing nothing it has."""
    try:
        applied = upgrade_project(dir_)
    except DevtoolsRefusal as exc:
        LOG.debug("upgrade_refused", reason=type(exc).__name__)
        console.print(f"[red]{escape(str(exc))}[/red]", soft_wrap=True)
        raise typer.Exit(1) from None
    if not applied:
        console.print("Nothing to upgrade: the project already has every requirement.")
        return
    for requirement in applied:
        console.print(
            f"{escape(requirement.path)}: added {escape(requirement.name)}", soft_wrap=True
        )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_upgrade.py tests/unit/test_plugin_project_cli.py tests/unit/test_scaffold_write.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/devtools/scaffold/upgrade.py src/graftpunk/cli/scaffold_commands.py tests/unit/test_scaffold_upgrade.py tests/unit/test_plugin_project_cli.py tests/unit/test_devtools_errors.py
git commit -m "feat(scaffold): gp plugin upgrade applies the project requirements a plugin lacks"
```

---

### Task 8: `plugin_check.py`, `gp plugin check`, and the three consumers pinned to the declaration

**Files:**
- Create: `src/graftpunk/devtools/plugin_check.py`
- Modify: `src/graftpunk/cli/scaffold_commands.py` (new `plugin_check`)
- Test: `tests/unit/test_plugin_check.py`

**Interfaces:**
- Consumes: `require_plugin_project`, `NotAPluginProjectError`, `PluginProjectError`, `ProjectView.defects`, `ProjectView.missing_requirements()`, the same answer `gp plugin upgrade` applies, and `ProjectView.unreadable_files()` (Task 3); `policy.GP_FILL_MARKER` (Task 3).
- Produces: `@dataclass(frozen=True) class Finding(path: str, line: int | None, message: str)` with `__str__`; `check_project(root: Path) -> list[Finding]`; `gp plugin check [--dir PATH]`, exit 0 with no findings, exit 1 listing each. Task 9 puts `gp plugin check` in `PROJECT_GATE`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_plugin_check.py`:

```python
"""gp plugin check, a lint over the project reader's view (graft skill spec, 2026-09-21)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from graftpunk.cli.main import app
from graftpunk.devtools.plugin_check import check_project
from graftpunk.devtools.scaffold import policy
from graftpunk.devtools.scaffold.policy import ProjectRequirement
from graftpunk.devtools.scaffold.project import write_scaffold
from graftpunk.devtools.scaffold.render import ScaffoldSpec, render
from graftpunk.devtools.scaffold.upgrade import upgrade_project

runner = CliRunner()
_SPEC = ScaffoldSpec(
    name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example"
)
_CLEAN_MODULE = """\
from graftpunk.plugins import CommandContext, SitePlugin, command


class MyshopPlugin(SitePlugin):
    site_name = "myshop"
    base_url = "https://myshop.example"

    @command(help="List orders", endpoint="GET /api/orders")
    def orders(self, ctx: CommandContext) -> dict:
        return ctx.request_json("GET", "/api/orders", role="xhr")
"""


def _clean_project(root: Path) -> Path:
    write_scaffold(root, _SPEC)
    module = root / "src" / "graftpunk_myshop" / "plugin.py"
    module.write_text(_CLEAN_MODULE)
    return module


class TestFindings:
    def test_a_clean_project_has_none(self, tmp_path: Path) -> None:
        _clean_project(tmp_path)
        assert check_project(tmp_path) == []
        result = runner.invoke(app, ["plugin", "check", "--dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "no findings" in result.output

    def test_a_remaining_marker_is_reported_with_its_line(self, tmp_path: Path) -> None:
        write_scaffold(tmp_path, _SPEC)
        findings = check_project(tmp_path)
        assert findings
        module = (tmp_path / "src" / "graftpunk_myshop" / "plugin.py").read_text().splitlines()
        for finding in findings:
            assert finding.path == "src/graftpunk_myshop/plugin.py"
            assert finding.line is not None and "GP-FILL" in module[finding.line - 1]
        result = runner.invoke(app, ["plugin", "check", "--dir", str(tmp_path)])
        assert result.exit_code == 1
        assert "src/graftpunk_myshop/plugin.py:" in result.output

    def test_a_module_without_exactly_one_plugin_class_is_reported(self, tmp_path: Path) -> None:
        module = _clean_project(tmp_path)
        module.write_text(_CLEAN_MODULE + "\n\nclass Other(SitePlugin):\n    site_name = 'o'\n")
        (finding,) = check_project(tmp_path)
        assert finding.path == "src/graftpunk_myshop/plugin.py"
        assert "exactly one SitePlugin subclass, found 2" in finding.message

    def test_a_missing_requirement_is_reported_and_names_the_fix(self, tmp_path: Path) -> None:
        _clean_project(tmp_path)
        (tmp_path / "tests" / "conftest.py").write_text("")
        messages = [f.message for f in check_project(tmp_path)]
        assert len(messages) == 2
        assert all("gp plugin upgrade" in m for m in messages)

    def test_a_directory_that_is_not_a_plugin_project_is_a_finding(self, tmp_path: Path) -> None:
        (finding,) = check_project(tmp_path)
        assert "not a graftpunk plugin project" in finding.message

    def test_a_conftest_that_does_not_parse_is_one_finding(self, tmp_path: Path) -> None:
        """One finding per unreadable file, however many requirements it holds."""
        _clean_project(tmp_path)
        (tmp_path / "tests" / "conftest.py").write_text("def (:\n")
        (finding,) = check_project(tmp_path)
        assert finding.path == "tests/conftest.py"
        assert "does not parse" in finding.message


def test_the_three_consumers_follow_the_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One entry added to PROJECT_REQUIREMENTS: the renderer emits it, check reports
    it on a project that lacks it, and upgrade applies it."""
    _clean_project(tmp_path)
    probe = ProjectRequirement(
        path="tests/conftest.py", name="extra_probe", statement="extra_probe = 1"
    )
    monkeypatch.setattr(policy, "PROJECT_REQUIREMENTS", (*policy.PROJECT_REQUIREMENTS, probe))
    assert "extra_probe = 1" in render(_SPEC)["tests/conftest.py"]
    (finding,) = check_project(tmp_path)
    assert "extra_probe" in finding.message
    assert [r.name for r in upgrade_project(tmp_path)] == ["extra_probe"]
    assert check_project(tmp_path) == []


def test_the_lint_never_imports_the_writer() -> None:
    script = (
        "import sys\n"
        "import graftpunk.devtools.plugin_check\n"
        "print('graftpunk.devtools.scaffold.write' in sys.modules)\n"
    )
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=True
    )
    assert result.stdout.strip() == "False"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_plugin_check.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'graftpunk.devtools.plugin_check'`.

- [ ] **Step 3: Write the lint**

The lint and the migrator take "what does this project lack" from one place, the view's `missing_requirements()`, so they cannot disagree, and the lint still never imports the writer. Create `src/graftpunk/devtools/plugin_check.py`:

```python
"""``gp plugin check``: a lint over the project reader's view. It never edits.

Reports a remaining ``GP-FILL`` marker, a module without exactly one
``SitePlugin`` subclass (the reader's per-plugin defect, listed with the other
findings), a requirement's file that does not parse, and a
``PROJECT_REQUIREMENTS`` entry the project lacks, which ``gp plugin upgrade``
fixes. It does not compare a declared endpoint
against the request call: the declaration is authoritative by design, and a
check that could only ever be weak would give an author a reason to drop the
keyword. It does not restate the fixtures check, which the generated suite runs
(graft skill spec, 2026-09-21). A reader: never imports ``write.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from graftpunk.devtools.plugin_project import (
    NotAPluginProjectError,
    PluginProjectError,
    require_plugin_project,
)
from graftpunk.devtools.scaffold.policy import GP_FILL_MARKER

__all__ = ["Finding", "check_project"]


@dataclass(frozen=True)
class Finding:
    """One thing the lint found, at a project-relative path and, when it has one, a line."""

    path: str
    line: int | None
    message: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line is not None else self.path
        return f"{where}: {self.message}"


def check_project(root: Path) -> list[Finding]:
    """Every finding in *root*'s plugin project: markers in file order, then plugin
    defects, then requirement files that do not parse, then missing requirements.
    A refusal from the reader (not a plugin project, or not readable at all) is
    the one finding."""
    try:
        view = require_plugin_project(root)
    except (NotAPluginProjectError, PluginProjectError) as exc:
        return [Finding(path=".", line=None, message=str(exc))]
    findings = [
        Finding(
            path=plugin.module_path, line=line, message=f"{GP_FILL_MARKER} marker left to fill in."
        )
        for plugin in view.plugins
        for line in plugin.markers
    ]
    findings.extend(
        Finding(path=defect.module_path, line=None, message=defect.message)
        for defect in view.defects
    )
    findings.extend(
        Finding(
            path=path,
            line=None,
            message=f"{reason}; gp plugin upgrade can add its wiring once it parses.",
        )
        for path, reason in view.unreadable_files()
    )
    findings.extend(
        Finding(
            path=requirement.path,
            line=None,
            message=f"does not bind {requirement.name}; gp plugin upgrade adds it.",
        )
        for requirement in view.missing_requirements()
    )
    return findings
```

- [ ] **Step 4: Add the command**

In `src/graftpunk/cli/scaffold_commands.py`, add `from graftpunk.devtools.plugin_check import check_project`, and append:

```python
@plugin_app.command("check")
def plugin_check(
    dir_: Annotated[Path, typer.Option("--dir", help="Project directory")] = Path("."),
) -> None:
    """Lint a plugin project: markers left, one plugin class per module, project wiring."""
    findings = check_project(dir_)
    for finding in findings:
        console.print(escape(str(finding)), soft_wrap=True, highlight=False)
    if findings:
        console.print(f"[red]gp plugin check: {len(findings)} finding(s).[/red]")
        raise typer.Exit(1)
    console.print("[green]gp plugin check: no findings.[/green]")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_plugin_check.py tests/unit/test_plugin_project_cli.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/devtools/plugin_check.py src/graftpunk/cli/scaffold_commands.py tests/unit/test_plugin_check.py
git commit -m "feat(devtools): gp plugin check lints markers, the one-class rule, and project wiring"
```

---

### Task 9: `PROJECT_GATE`, quoted into the generated README and the guide, and pinned

**Files:**
- Modify: `src/graftpunk/devtools/scaffold/policy.py` (new `PROJECT_GATE`; `__all__`)
- Modify: `src/graftpunk/devtools/scaffold/render.py` (`_render_readme`)
- Modify: `docs/PLUGIN_DEVELOPMENT.md` ("The gate", the section under `### The gate`, with its CI example; and "Before you publish", the checklist under `### Before you publish`)
- Create: `tests/unit/guide_harness.py` (the guide helpers moved out of the guide's test module)
- Modify: `tests/unit/test_plugin_development_guide.py` (imports the moved helpers; no test changes)
- Test: `tests/unit/test_project_gate.py`

**Interfaces:**
- Consumes: `gp plugin check` (Task 8); the CLI walker, moved in Step 1 from `tests/unit/test_plugin_development_guide.py` (`_check_invocation` and the helpers it calls) into `tests/unit/guide_harness.py`.
- Produces: `policy.PROJECT_GATE: Final[tuple[str, ...]] = ("pytest", "ruff check .", "ruff format --check .", "gp plugin check")`, reproduced once in the generated README's `## Checks` block. `tests/unit/guide_harness.py`, a non-test module (pytest does not collect it) that the guide's test, this task's gate test, and the skill plan's tests import: `REPO_ROOT: Path`, `GUIDE: Path`, `GUIDE_TEXT: str`, `blocks(text: str, language: str) -> list[tuple[int, str]]`, `gp_invocations(text: str) -> list[tuple[int, str]]`, `option_names(command: click.Command) -> set[str]`, `check_invocation(invocation: str, where: str) -> None`, `slug(title: str) -> str`, `slugs_of(path: Path) -> set[str]`, `outside_fences(text: str) -> str`, and `section(text: str, heading: str) -> str`, the one rule for where a markdown section ends (at the next heading of the same or a higher level, never at a heading-like line inside a fenced block). This task's gate test and the skill plan's tests take their sections from `section`; fence handling has one owner, the moved `_fenced_line_numbers`. No test module imports from another `test_*.py` module. The skill's `harden.md` points at the guide's "The gate" and never enumerates it.

- [ ] **Step 1: Move the guide helpers into `tests/unit/guide_harness.py`**

A pure move with public names, so this task's gate test and the skill plan's tests share the helpers without importing a test module. The guide's own tests keep passing unchanged. The ranges below were verified at `27326b5`, and nothing earlier in these plans edits this file; the script refuses to run if any first line has moved. Run from the worktree root:

```bash
uv run python - <<'PY'
import re
from pathlib import Path

guide_test = Path("tests/unit/test_plugin_development_guide.py")
harness = Path("tests/unit/guide_harness.py")
ranges = [(28, 38), (47, 48), (50, 54), (62, 198), (205, 232), (317, 337)]
first_lines = {
    28: "def _repo_root(",
    47: "_FENCE_RE = ",
    50: "# A code span may wrap",
    62: "def _blocks(",
    205: "def _check_invocation(",
    317: "def _slugs_of(",
}
renames = {
    "_blocks": "blocks",
    "_gp_invocations": "gp_invocations",
    "_option_names": "option_names",
    "_check_invocation": "check_invocation",
    "_slugs_of": "slugs_of",
}
lines = guide_test.read_text(encoding="utf-8").splitlines(keepends=True)
for number, prefix in first_lines.items():
    assert lines[number - 1].startswith(prefix), (number, lines[number - 1])
moved = "\n\n\n".join("".join(lines[s - 1 : e]).rstrip("\n") + "\n" for s, e in ranges)
dropped = {n for s, e in ranges for n in range(s, e + 1)}
kept = "".join(line for n, line in enumerate(lines, start=1) if n not in dropped)


def renamed(text: str) -> str:
    for old, new in renames.items():
        text = re.sub(rf"(?<![\w]){old}\b", new, text)
    return text


header = '''"""The plugin guide, read, and gp invocations checked against the real CLI.

Shared by the guide's own test (tests/unit/test_plugin_development_guide.py),
the project gate test (tests/unit/test_project_gate.py), and the graft skill's
tests, so no test module imports another. Nothing here is a test and nothing
here runs a gp command: the CLI is inspected through typer.main.get_command.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import click
import typer.main

from graftpunk.cli.main import app


'''
harness.write_text(header + renamed(moved), encoding="utf-8")
anchor = "from graftpunk.cli.main import app\n"
assert anchor in kept
imports = (
    "from tests.unit.guide_harness import (\n"
    "    GUIDE,\n    GUIDE_TEXT,\n    REPO_ROOT,\n    blocks,\n    check_invocation,\n"
    "    gp_invocations,\n    option_names,\n    slugs_of,\n)\n"
)
guide_test.write_text(renamed(kept.replace(anchor, anchor + imports, 1)), encoding="utf-8")
print("moved", len(dropped), "lines")
PY
```

Then, in `tests/unit/guide_harness.py`, add above `slugs_of`:

```python
def slug(title: str) -> str:
    """The GitHub anchor for a heading whose text is *title*."""
    title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", title).replace("`", "")
    return re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", title.strip().lower()))


def outside_fences(text: str) -> str:
    """*text* without its fenced blocks, the fence lines included: its prose."""
    fenced = _fenced_line_numbers(text)
    return "\n".join(
        line for line_no, line in enumerate(text.splitlines(), start=1) if line_no not in fenced
    )


def section(text: str, heading: str) -> str:
    """*text* from the line *heading* up to the next heading of the same or a higher
    level, or to its end: the one rule for where a markdown section ends. A line
    inside a fenced block is never a heading (the guide quotes a digest whose body
    has ``##`` lines)."""
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
    fenced = _fenced_line_numbers(text)
    start = lines.index(heading)
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if i + 1 not in fenced and re.match(rf"#{{1,{level}}}\s", lines[i])
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])
```

and replace the body of `slugs_of` after its docstring with:

```python
    slugs: set[str] = set()
    for line in outside_fences(path.read_text(encoding="utf-8")).splitlines():
        heading = _HEADING_RE.match(line)
        if heading is not None:
            slugs.add(slug(heading.group(2)))
    return slugs
```

so its fence loop and its inline slug rule each give way to the one helper that owns them. Run `uvx ruff check --fix tests/unit/guide_harness.py tests/unit/test_plugin_development_guide.py` (it drops the imports the guide's test no longer uses and sorts the new block) and `uvx ruff format tests/unit/`.

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_plugin_development_guide.py -q`
Expected: PASS, with the same test count as before the move.

- [ ] **Step 2: Write the failing tests**

Create `tests/unit/test_project_gate.py`:

```python
"""One gate, reproduced once and pinned everywhere it is quoted
(graft skill spec, 2026-09-21, "The project gate and the project's requirements")."""

from __future__ import annotations

import re

from graftpunk.devtools.scaffold import policy
from graftpunk.devtools.scaffold.render import ScaffoldSpec, render
from tests.unit.guide_harness import GUIDE_TEXT, check_invocation, section


def _block_lines(text: str, language: str) -> list[str]:
    """The lines of the first fenced block of *language* in *text*: the one extractor."""
    match = re.search(rf"^```{language}\n(.*?)^```", text, re.M | re.S)
    assert match, f"no {language} block"
    return [line.strip() for line in match.group(1).splitlines() if line.strip()]


def _run_step_lines(yaml_lines: list[str]) -> list[str]:
    """The lines of the one multi-line ``run: |`` step in a workflow block."""
    start = yaml_lines.index("- run: |")
    step: list[str] = []
    for line in yaml_lines[start + 1 :]:
        if line.startswith("- "):
            break
        step.append(line)
    return step


def test_the_gate() -> None:
    assert policy.PROJECT_GATE == (
        "pytest",
        "ruff check .",
        "ruff format --check .",
        "gp plugin check",
    )


def test_the_generated_readme_quotes_the_gate() -> None:
    spec = ScaffoldSpec(
        name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example"
    )
    readme = render(spec)["README.md"]
    assert _block_lines(section(readme, "## Checks"), "bash") == list(policy.PROJECT_GATE)


def test_the_guides_gate_section_quotes_the_gate() -> None:
    gate = section(GUIDE_TEXT, "### The gate")
    assert _block_lines(gate, "bash") == list(policy.PROJECT_GATE)


def test_the_guides_ci_example_runs_the_gate_as_one_step() -> None:
    gate = section(GUIDE_TEXT, "### The gate")
    assert _run_step_lines(_block_lines(gate, "yaml")) == list(policy.PROJECT_GATE)


def test_the_publish_checklist_names_the_gate_and_reproduces_none_of_it() -> None:
    checklist = section(GUIDE_TEXT, "### Before you publish")
    items = [line for line in checklist.splitlines() if line.startswith("- [ ]")]
    assert items[0].startswith("- [ ] The gate is green")
    rest = "\n".join(items[1:])
    assert "GP-FILL" not in rest
    for entry in policy.PROJECT_GATE:
        assert f"`{entry}`" not in checklist


def test_every_gp_entry_in_the_gate_resolves_through_the_cli() -> None:
    for entry in policy.PROJECT_GATE:
        if entry.startswith("gp "):
            check_invocation(entry, "PROJECT_GATE")
```

- [ ] **Step 3: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_project_gate.py -q`
Expected: FAIL (`AttributeError: module 'graftpunk.devtools.scaffold.policy' has no attribute 'PROJECT_GATE'`).

- [ ] **Step 4: Declare the gate and render it into the README**

In `src/graftpunk/devtools/scaffold/policy.py`, add `"PROJECT_GATE"` to `__all__` and append:

```python
PROJECT_GATE: Final[tuple[str, ...]] = (
    "pytest",
    "ruff check .",
    "ruff format --check .",
    "gp plugin check",
)
"""The commands a generated project's gate runs, in order.

Reproduced in exactly one rendered form, the checks block of the generated
README; the guide's "The gate" section and its CI example quote the same
lines, and a test pins all three to this constant."""
```

In `src/graftpunk/devtools/scaffold/render.py`, replace `_render_readme` with:

```python
def _render_readme(spec: ScaffoldSpec) -> str:
    checks = "\n".join(policy.PROJECT_GATE)
    return (
        f"# {spec.name}\n\n"
        "A graftpunk plugin.\n\n"
        "## Install\n\n"
        "```bash\npip install -e .\n```\n\n"
        "## Log in\n\n"
        f"```bash\ngp {spec.name} login\n```\n\n"
        "## Run a command\n\n"
        f"```bash\ngp {spec.name} --help\n```\n\n"
        "## Checks\n\n"
        "Run all of these before every commit:\n\n"
        f"```bash\n{checks}\n```\n\n"
        "Fixtures under `tests/fixtures/` are hand-derived from captures in "
        f"`{CAPTURES_DIR}/` (captures are never committed; a fixture copies the "
        "structure and invents the content). See `gp observe fixtures --help`.\n"
    )
```

- [ ] **Step 5: Quote the gate in the guide**

In `docs/PLUGIN_DEVELOPMENT.md`, "The gate": replace the bash block with

```bash
pytest
ruff check .
ruff format --check .
gp plugin check
```

replace the paragraph that begins "Add a type checker." with:

```markdown
`gp plugin check` lists every `GP-FILL` marker left in a plugin module, any
plugin module that does not hold exactly one plugin class, and any project
wiring the project lacks, which `gp plugin upgrade` adds. A fresh scaffold fails
it until its markers are filled in. It passes `ruff check` and `ruff format
--check` as written, so a red ruff run on a fresh scaffold is something you
introduced. Add a type checker.
```

and in the CI example replace the three lines `      - run: pytest`, `      - run: ruff check .`, `      - run: ruff format --check .` with:

```yaml
      - run: |
          pytest
          ruff check .
          ruff format --check .
          gp plugin check
```

In "Before you publish", replace the first item, ``- [ ] No `GP-FILL` marker is left anywhere in the project.`` (the marker is in backticks in the file), with `- [ ] The gate is green: every command in [The gate](#the-gate) passes.` and delete the item `` - [ ] `pytest`, `ruff check .`, and `ruff format --check .` are green. ``.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_project_gate.py tests/unit/test_plugin_development_guide.py tests/unit/test_scaffold_render.py tests/unit/test_scaffold_cli.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/graftpunk/devtools/scaffold/policy.py src/graftpunk/devtools/scaffold/render.py docs/PLUGIN_DEVELOPMENT.md tests/unit/guide_harness.py tests/unit/test_plugin_development_guide.py tests/unit/test_project_gate.py
git commit -m "feat(scaffold): PROJECT_GATE is the one gate, quoted in the generated README and the guide"
```

---

### Task 10: The guide's fixture recipe, the help string, the CHANGELOG, and the full gate

**Files:**
- Modify: `docs/PLUGIN_DEVELOPMENT.md` ("Test against fixtures, not against the site": the sentence that begins "Without a sidecar", in the paragraph after the sidecar's JSON block; and "Deriving a fixture from a capture": the paragraph beginning "Then do the work by hand."). The sidecar's JSON example and the error-path sentence are the foundations plan's (its Task 7), and this task does not touch them.
- Modify: `src/graftpunk/cli/scaffold_commands.py:1` (module docstring) and the `plugin_app = typer.Typer(...)` assignment (its `help=`)
- Modify: `README.md:311` (the `plugin` line of the CLI Reference block)
- Modify: `CHANGELOG.md` (`[Unreleased]` Added)

**Interfaces:**
- Consumes: everything above.
- Produces: the guide sections the skill's `harden.md` cites ("Deriving a fixture from a capture", "The gate", "Before you publish").

- [ ] **Step 1: Update the guide's recipe and the sentence on a fixture without a sidecar**

In `docs/PLUGIN_DEVELOPMENT.md`, "Test against fixtures, not against the site", the paragraph after the sidecar's JSON block begins "Without a sidecar the status is 200 and the content type is guessed from the extension." (`docs/PLUGIN_DEVELOPMENT.md:963-964` at `27326b5`; the foundations plan rewrote only the paragraph's second sentence). That first sentence is now half true: `FixtureSession` still answers that way, but the generated suite fails on a fixture with no sidecar. Replace it with:

```markdown
Without a sidecar, `FixtureSession` answers with status 200 and guesses the
content type from the extension, but the generated suite's fixtures check fails
on any fixture that has none (see
[Deriving a fixture from a capture](#deriving-a-fixture-from-a-capture)). A
fixture you make by hand gets a sidecar of its own, with `"capture_sha256": null`
and `"flagged_names": []`.
```

In "Deriving a fixture from a capture", replace the paragraph that begins "Then do the work by hand." with:

```markdown
Then do the work by hand. Copy the capture and its `.meta.json` sidecar into the
plugin's fixtures directory together, under the same names, and edit only the
copy of the capture. **A fixture copies the real structure and invents the
content. No captured page is committed.** Keep the shape of the response and
replace every real value: order ids, names, addresses, amounts, tokens, ids in
URLs. `tests/captures/` is gitignored and stays that way; `tests/fixtures/` is
committed and contains nothing that came off a real account.

The generated suite holds you to part of that. Its `tests/conftest.py` wires in
`fixtures_are_sanitised`, which walks `tests/fixtures/` on every run and fails
when a fixture has no sidecar, when a fixture is still byte for byte its
capture, when a name the sidecar flags turns up in the fixture, or when a
sidecar is outside its declared format. It cannot tell whether invented content
was invented well; that part stays yours. A fixture you wrote from nothing needs
a sidecar too, with `"capture_sha256": null` and `"flagged_names": []`: that
declares the file came off no account. The check trusts that declaration rather
than verifying it, and prints on every run how many fixtures it accepted that
way, so the number shows up in review.
```

- [ ] **Step 2: Rename the command group's purpose**

In `src/graftpunk/cli/scaffold_commands.py`, change the module docstring to `"""``gp plugin``: new, add-command, info, upgrade, and check. Argument handling only; each entry point calls into ``graftpunk.devtools``."""` and the `plugin_app` help to `help="Scaffold, extend, inspect, and check a graftpunk plugin project."`. In `README.md`, change the CLI Reference line `  plugin      Scaffold a new graftpunk plugin.` to `  plugin      Scaffold, extend, inspect, and check a graftpunk plugin project.`

- [ ] **Step 3: Check the CLI module against the split trigger**

Run: `wc -l src/graftpunk/cli/scaffold_commands.py`
Expected: under 400. The spec defers splitting this module until "the next command added to `plugin_app` after these, or the module passing 400 lines". If the count is 400 or more, stop and report it to the controller: the trigger has fired inside this pull request, and the split (`plugin_app` and the reserved-name snapshot into a small module both halves import, and `main.py` importing both halves) needs its own task.

- [ ] **Step 4: Add the CHANGELOG line**

Append to `CHANGELOG.md` under `[Unreleased]` / `### Added`:

```markdown
- **`gp plugin info`, `add-command`, `upgrade`, and `check`, and the in-suite fixtures check.** `gp plugin info --json` describes the working directory as `empty`, `plugin`, or `foreign`, and for a plugin project lists each entry point's name (`entry_point`, the name `add-command` takes), module, `site_name`, `base_url`, and commands with their declared endpoints. `gp plugin new --command "NAME=METHOD template"` (repeatable) makes only the stubs you name, under your names, with no twelve-stub cap; `gp plugin add-command PLUGIN --from-run SESSION --command ...` adds one stub in the same shape to an existing plugin and prints the fixture its test reads. A generated `tests/conftest.py` now also carries `FIXTURES_TREE` and `fixtures_are_sanitised`, a check that fails the suite when a committed fixture has no sidecar, is an unchanged copy of its capture, or contains a flagged cookie or token name; on an existing project, `gp plugin upgrade` adds the two lines. `gp plugin check` reports `GP-FILL` markers left, a module without exactly one plugin class, and missing wiring, and joins `pytest` and ruff in the project gate the generated README and the guide list.
```

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
Expected: every test passes, ruff reports no findings and no files to reformat, ty reports no errors.

- [ ] **Step 6: Scan the diff for banned punctuation**

Run: `git diff main | perl -CSD -ne 'print "$.: $_" if /^\+.*([\x{2013}\x{2014}]| \x2d\x2d )/'`
Expected: no output.

- [ ] **Step 7: Commit**

```bash
git add docs/PLUGIN_DEVELOPMENT.md src/graftpunk/cli/scaffold_commands.py README.md CHANGELOG.md
git commit -m "docs(guide): the fixture recipe copies the sidecar, and the check's limits are stated"
```
