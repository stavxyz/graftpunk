# Graft Package Project Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the second of the three pull requests the graft skill design orders: the pieces that need the project reader (`devtools/plugin_project.py`, `gp plugin info --json`, `gp plugin new --command`, `gp plugin add-command`, `gp plugin upgrade`, `gp plugin check`, the in-suite `fixtures_are_sanitised` check and its conftest wiring, and the one project gate quoted into the generated README and the guide).

**Architecture:** One reader, `devtools/plugin_project.py`, resolves the working directory to a plugin project, classifies it (`empty`, `plugin`, `foreign`), and reads every plugin module with `ast` into one structural view; `gp plugin info`, the stub inserter (`scaffold/insert.py`), the migrator (`scaffold/upgrade.py`), and the lint (`devtools/plugin_check.py`) all work from that view and never parse on their own. What a generated project must hold is declared in `scaffold/policy.py` (`PROJECT_REQUIREMENTS`, `PROJECT_GATE`), which the renderer emits, the migrator applies, and the lint reports. The in-suite sanitisation check lives on the pytest side of `graftpunk.testing` and receives the fixtures tree from the generated conftest, so nothing in `graftpunk.testing` imports `graftpunk.devtools`.

**Tech Stack:** Python 3.11+, Typer 0.21+, `ast`, `tomllib`, `hashlib` (stdlib), pytest (and its `pytester` for the in-suite check), ruff (run on generated trees through `sys.executable -m ruff`), ty 0.0.75.

**Spec:** `docs/superpowers/specs/2026-09-21-graft-skill-design.md` (validated 2026-09-22). Read it alongside this plan. This plan starts from the tree `docs/superpowers/plans/2026-09-22-graft-package-foundations.md` leaves and consumes its names exactly as that plan's Interfaces blocks state them. Its own Interfaces blocks are what `docs/superpowers/plans/2026-09-22-graft-skill.md` consumes.

**Implementation notes (deviations from the spec's wording, for review):**
- `PROJECT_REQUIREMENTS` has two entries, not one, and each carries the imports its statement reads. The spec's single entry ("`tests/conftest.py` binds `fixtures_are_sanitised`, by the import line the renderer writes") cannot work as written: an import alone registers nothing with pytest and hands the check no tree, and an import appended to the end of an existing conftest fails the generated project's own `ruff check` (E402, I001). The check is a factory like `site_env_scrubber`: the conftest binds `FIXTURES_TREE = Path(__file__).parent / "fixtures"` and `sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)`, and those two names are the requirements. The migrator merges each requirement's imports into the file's import block the way isort places them, then appends the statement, so an upgraded conftest is byte-identical to a freshly generated one.
- A plugin is a suite member, for the fixtures-root rule, when its package name differs from the project's `[project].name` (both normalised with `[-_.]` to `_`). That is the fact on disk that matches the generator's own decision: `gp plugin new` names a new project after its package, and every plugin added to it later has a different package.
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
- Within the package, `graftpunk/contracts.py` is the only module that declares a schema number.
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
| `src/graftpunk/testing/plugin.py` (modify, Task 1) | `FixturesTreeReport`, `check_fixtures_tree`, `fixtures_are_sanitised`. |
| `src/graftpunk/devtools/scaffold/policy.py` (modify, Tasks 2, 9) | `PLUGINS_ENTRY_POINT_GROUP`, `ProjectRequirement`, `PROJECT_REQUIREMENTS`, `PROJECT_GATE`. |
| `src/graftpunk/devtools/scaffold/pysrc.py` (modify, Tasks 2, 6) | `_import_block_lines`, `_is_stdlib`, `_isort_name_key`, `with_import`. |
| `src/graftpunk/devtools/scaffold/render.py` (modify, Tasks 2, 5, 9) | Conftest from `PROJECT_REQUIREMENTS`; `CommandSelection`, `PlannedCommand`, `RenderedCommand`, `plan_command`, `render_command`; README checks block from `PROJECT_GATE`. |
| `src/graftpunk/devtools/scaffold/project.py` (modify, Task 2) | Imports `PLUGINS_ENTRY_POINT_GROUP` from policy. |
| `src/graftpunk/devtools/plugin_project.py` (new, Tasks 3, 4) | The reader and its view: `Span`, `CommandView`, `PluginView`, `ProjectView`, `PluginProjectError`, `classify`, `read_project`, `binds_name`, `info_payload`. |
| `src/graftpunk/contracts.py` (modify, Task 4) | `INFO_SCHEMA`; `"info"` in `CLI_SURFACES`. |
| `src/graftpunk/devtools/scaffold/insert.py` (new, Task 6) | `CommandInsertError`, `AddedCommand`, `insertion_line`, `add_command`. |
| `src/graftpunk/devtools/scaffold/upgrade.py` (new, Task 7) | `UpgradeRefusedError`, `missing_requirements`, `with_requirements`, `upgrade_project`. |
| `src/graftpunk/devtools/plugin_check.py` (new, Task 8) | `Finding`, `check_project`. |
| `src/graftpunk/cli/scaffold_commands.py` (modify, Tasks 4 to 8, 10) | `gp plugin info`, `new --command`, `add-command`, `upgrade`, `check`; `plugin_app`'s help. |
| `docs/PLUGIN_DEVELOPMENT.md` (modify, Tasks 2, 9, 10) | The conftest section; "The gate", its CI example, "Before you publish"; the recipe line, the sidecar example, and the check's limits. |
| `README.md` (modify, Task 10) | The `plugin` line of the CLI Reference block. |
| `CHANGELOG.md` (modify, Task 10) | One Added line. |

---

### Task 1: `fixtures_are_sanitised`, the in-suite sanitisation check

**Files:**
- Modify: `src/graftpunk/testing/plugin.py` (module docstring, imports, `__all__`, three new names)
- Test: `tests/unit/test_graftpunk_testing.py`

**Interfaces:**
- Consumes: `graftpunk.testing.sidecar.SidecarError`, `is_sidecar`, `load_sidecar`, `sidecar_path`, and `Sidecar.declared` (foundations Task 7); `graftpunk.contracts.current_schema` (foundations Task 2).
- Produces: `@dataclass(frozen=True) class FixturesTreeReport(problems: tuple[str, ...], verified: int, declared: int)` with property `summary -> str`; `check_fixtures_tree(tree: Path) -> FixturesTreeReport`; `fixtures_are_sanitised(tree: Path | str) -> Any`, a factory returning a session-scoped autouse pytest fixture. Task 2's generated conftest binds `sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_graftpunk_testing.py` (add `import hashlib` and `from graftpunk.testing.plugin import check_fixtures_tree` and `from graftpunk.testing.sidecar import Sidecar, sidecar_text` to its imports):

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

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graftpunk_testing.py -q`
Expected: collection error, `ImportError: cannot import name 'check_fixtures_tree'`.

- [ ] **Step 3: Write the check**

In `src/graftpunk/testing/plugin.py`, replace the module docstring's first sentence with "The pytest half of :mod:`graftpunk.testing`: two fixture factories a generated ``tests/conftest.py`` assigns to module-level names." and append to the docstring: "``fixtures_are_sanitised`` is handed the fixtures tree by the conftest, which declares it as ``FIXTURES_TREE``; nothing here imports ``graftpunk.devtools`` (graft skill spec, 2026-09-21)." Add `import hashlib`, `from dataclasses import dataclass`, `from pathlib import Path`, `from graftpunk.contracts import current_schema`, and `from graftpunk.testing.sidecar import SidecarError, is_sidecar, load_sidecar, sidecar_path`; set `__all__ = ["FixturesTreeReport", "check_fixtures_tree", "fixtures_are_sanitised", "site_env_scrubber"]`; and append:

```python
# The file gp plugin new writes so git keeps an empty fixtures directory.
_PLACEHOLDER_FILE = ".gitkeep"


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
        if p.is_file() and not is_sidecar(p) and p.name != _PLACEHOLDER_FILE
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
        rest = " ".join([str(sidecar.status), sidecar.content_type, *sidecar.body_params])
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

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_graftpunk_testing.py tests/unit/test_contracts.py -q`
Expected: PASS. `test_contracts.py` is in the run because its scan covers `plugin.py`'s new message, which spells the schema through `current_schema`.

- [ ] **Step 5: Commit**

```bash
git add src/graftpunk/testing/plugin.py tests/unit/test_graftpunk_testing.py
git commit -m "feat(testing): fixtures_are_sanitised checks every committed fixture against its sidecar"
```

---

### Task 2: `PROJECT_REQUIREMENTS`, and the conftest that carries them

**Files:**
- Modify: `src/graftpunk/devtools/scaffold/policy.py` (new `PLUGINS_ENTRY_POINT_GROUP`, `ProjectRequirement`, `PROJECT_REQUIREMENTS`; `__all__`)
- Modify: `src/graftpunk/devtools/scaffold/project.py:34` (`PLUGINS_ENTRY_POINT_GROUP` imported from policy)
- Modify: `src/graftpunk/devtools/scaffold/pysrc.py` (new `_is_stdlib`, `_isort_name_key`, `_import_block_lines`)
- Modify: `src/graftpunk/devtools/scaffold/render.py` (`_render_conftest`)
- Modify: `tests/unit/test_scaffold_render.py` (`TestRenderNewProject.test_conftest_is_two_declarations`), `tests/unit/test_scaffold_cli.py` (`test_generated_tests_pass_against_a_generated_fixture_and_sidecar`)
- Modify: `docs/PLUGIN_DEVELOPMENT.md:899-915` ("Keep the developer's own environment out of the tests")
- Test: `tests/unit/test_scaffold_policy.py`, `tests/unit/test_scaffold_render.py`

**Interfaces:**
- Consumes: `fixtures_are_sanitised` (Task 1); `policy.FIXTURES_TREE` (foundations Task 9); `pysrc._import_lines(module, *names)` (foundations Task 11).
- Produces: `policy.PLUGINS_ENTRY_POINT_GROUP: Final = "graftpunk.plugins"`; `@dataclass(frozen=True) class ProjectRequirement(path: str, name: str, statement: str, imports: tuple[tuple[str, str], ...] = ())` with property `key -> str` (`f"{path}:{name}"`); `policy.PROJECT_REQUIREMENTS: Final[tuple[ProjectRequirement, ...]]` (two entries, both `tests/conftest.py`: `FIXTURES_TREE` and `sanitised_fixtures`); `pysrc._import_block_lines(imports: Iterable[tuple[str, str]]) -> list[str]`, `pysrc._is_stdlib(module: str) -> bool`, `pysrc._isort_name_key(name: str) -> tuple[int, str]`. Tasks 3, 7, and 8 read `policy.PROJECT_REQUIREMENTS`.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_scaffold_policy.py`, extend the policy import at the top to `from graftpunk.devtools.scaffold.policy import FIXTURES_TREE, PROJECT_REQUIREMENTS, ProjectRequirement, fixtures_root`, and append:

```python
class TestProjectRequirements:
    def test_the_conftest_binds_the_tree_and_the_check(self) -> None:
        assert [(r.path, r.name) for r in PROJECT_REQUIREMENTS] == [
            ("tests/conftest.py", "FIXTURES_TREE"),
            ("tests/conftest.py", "sanitised_fixtures"),
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

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_policy.py tests/unit/test_scaffold_render.py::TestRenderNewProject -q`
Expected: FAIL (`ImportError: cannot import name 'PROJECT_REQUIREMENTS'`).

- [ ] **Step 3: Declare the requirements**

In `src/graftpunk/devtools/scaffold/policy.py`, add `from dataclasses import dataclass`, set `__all__ = ["FIXTURES_TREE", "PLUGINS_ENTRY_POINT_GROUP", "PROJECT_REQUIREMENTS", "ProjectRequirement", "fixtures_root"]`, and append:

```python
PLUGINS_ENTRY_POINT_GROUP: Final = "graftpunk.plugins"
"""The entry-point group that makes a ``pyproject.toml`` a graftpunk plugin project."""


@dataclass(frozen=True)
class ProjectRequirement:
    """A module-level name a project file must bind, and the statement that binds it.

    Presence is decided structurally by the project reader: the file binds the
    name at module level (a plain import, an aliased import, an assignment, and a
    wrapped line all bind; a star import and a conditional import do not). The
    renderer emits every requirement in a new project, ``gp plugin upgrade``
    applies the ones a project lacks, and ``gp plugin check`` reports them.

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


_CONFTEST = "tests/conftest.py"
# The fixtures tree as the conftest sees it: the conftest lives in tests/.
_TREE_FROM_CONFTEST = FIXTURES_TREE.removeprefix("tests/").strip("/")

PROJECT_REQUIREMENTS: Final[tuple[ProjectRequirement, ...]] = (
    ProjectRequirement(
        path=_CONFTEST,
        name="FIXTURES_TREE",
        statement=f'FIXTURES_TREE = Path(__file__).parent / "{_TREE_FROM_CONFTEST}"',
        imports=(("pathlib", "Path"),),
    ),
    ProjectRequirement(
        path=_CONFTEST,
        name="sanitised_fixtures",
        statement="sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)",
        imports=(("graftpunk.testing.plugin", "fixtures_are_sanitised"),),
    ),
)
"""What every generated project's files must bind, in the order they are applied."""
```

In `src/graftpunk/devtools/scaffold/project.py`, delete the line `PLUGINS_ENTRY_POINT_GROUP = "graftpunk.plugins"` and add `from graftpunk.devtools.scaffold.policy import PLUGINS_ENTRY_POINT_GROUP`.

- [ ] **Step 4: Add the import-block helper**

In `src/graftpunk/devtools/scaffold/pysrc.py`, add `import sys` and `from collections.abc import Iterable`, and append:

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
            lines.extend(_import_lines(module, *sorted(by_module[module], key=_isort_name_key)))
    return lines
```

- [ ] **Step 5: Render the conftest from the requirements**

In `src/graftpunk/devtools/scaffold/render.py`, import `_import_block_lines` from `pysrc` and replace `_render_conftest` with:

```python
_CONFTEST_PATH = "tests/conftest.py"


def _render_conftest(spec: ScaffoldSpec) -> str:
    """The site-environment scrubber, then every ``PROJECT_REQUIREMENTS`` statement for
    this file, with the imports they read.

    The imports alone plus assignments: naming the module in ``pytest_plugins`` as
    well would make pytest try to rewrite assertions in a module the import has
    already loaded, which it reports as a warning on every run (final fix wave,
    2026-09-12). Each assignment is what registers its fixture.
    """
    requirements = [r for r in policy.PROJECT_REQUIREMENTS if r.path == _CONFTEST_PATH]
    imports = [("graftpunk.testing.plugin", "site_env_scrubber")]
    imports.extend(pair for r in requirements for pair in r.imports)
    lines = [
        *_import_block_lines(imports),
        "",
        f'scrub_site_env = site_env_scrubber("{_env_prefix_for(spec.name)}")',
        *(r.statement for r in requirements),
    ]
    return "\n".join(lines) + "\n"
```

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

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_policy.py tests/unit/test_scaffold_render.py tests/unit/test_scaffold_cli.py tests/unit/test_scaffold_project.py tests/unit/test_plugin_development_guide.py -q`
Expected: PASS, including `TestGeneratedProjectPassesItsOwnGate.test_new_project_is_ruff_clean`, which runs the generated project's own ruff over the new conftest.

- [ ] **Step 9: Commit**

```bash
git add src/graftpunk/devtools/scaffold/policy.py src/graftpunk/devtools/scaffold/project.py src/graftpunk/devtools/scaffold/pysrc.py src/graftpunk/devtools/scaffold/render.py tests/unit/test_scaffold_policy.py tests/unit/test_scaffold_render.py tests/unit/test_scaffold_cli.py docs/PLUGIN_DEVELOPMENT.md
git commit -m "feat(scaffold): PROJECT_REQUIREMENTS declares the conftest wiring, and the generated conftest carries it"
```

---

### Task 3: `devtools/plugin_project.py`, the one reader

**Files:**
- Create: `src/graftpunk/devtools/plugin_project.py`
- Test: `tests/unit/test_plugin_project.py`

**Interfaces:**
- Consumes: `policy.PLUGINS_ENTRY_POINT_GROUP`, `policy.PROJECT_REQUIREMENTS`, `policy.fixtures_root` (Task 2, foundations Task 9); `render.module_name_for` (existing).
- Produces: `DirectoryKind = Literal["empty", "plugin", "foreign"]`; `GP_FILL_MARKER = "GP-FILL"`; `@dataclass(frozen=True) class Span(start: int, end: int)` (1-based, inclusive); `@dataclass(frozen=True) class CommandView(method: str, span: Span, keywords: dict[str, str | None])` with property `endpoint -> str | None`; `@dataclass(frozen=True) class PluginView(module_path: str, class_name: str, class_span: Span, site_name: str | None, base_url: str | None, commands: tuple[CommandView, ...], markers: tuple[int, ...], fixtures_root: str)`; `@dataclass(frozen=True) class ProjectView(directory: DirectoryKind, plugins: tuple[PluginView, ...], requirements: dict[str, bool])`; `class PluginProjectError(ValueError)`; `classify(root: Path) -> DirectoryKind`; `read_project(root: Path) -> ProjectView`; `binds_name(tree: ast.Module, name: str) -> bool`. Task 4 adds `info_payload`. Tasks 6, 7, and 8 consume `read_project`.

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

import pytest

from graftpunk.devtools.plugin_project import (
    CommandView,
    PluginProjectError,
    PluginView,
    ProjectView,
    binds_name,
    classify,
    read_project,
)
from graftpunk.devtools.scaffold.policy import fixtures_root
from graftpunk.devtools.scaffold.project import write_scaffold
from graftpunk.devtools.scaffold.render import ScaffoldSpec, _fixtures_root, module_name_for
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
        assert read_project(tmp_path) == ProjectView(directory="empty", plugins=(), requirements={})

    def test_a_pyproject_with_the_group_is_a_plugin(self, tmp_path: Path) -> None:
        _hand_written(tmp_path)
        assert classify(tmp_path) == "plugin"

    def test_a_pyproject_without_the_group_is_foreign(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "other"\n')
        assert classify(tmp_path) == "foreign"
        assert read_project(tmp_path).plugins == ()


class TestTheView:
    def test_the_view_has_exactly_the_fields_its_section_lists(self) -> None:
        """Copied from the spec's list, so a second enumeration cannot drift from it."""
        assert [f.name for f in fields(ProjectView)] == ["directory", "plugins", "requirements"]
        assert [f.name for f in fields(PluginView)] == [
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

    def test_a_hand_written_command_without_a_declaration_reads_none(
        self, tmp_path: Path
    ) -> None:
        _hand_written(tmp_path)
        (plugin,) = read_project(tmp_path).plugins
        (command,) = plugin.commands
        assert (command.method, command.endpoint) == ("orders", None)
        assert command.keywords == {"help": "List orders"}
        assert plugin.markers == ()

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
    def test_a_module_without_exactly_one_plugin_class_is_refused(
        self, tmp_path: Path, classes: int
    ) -> None:
        body = "from graftpunk.plugins import SitePlugin\n\n\n" + "".join(
            f"class P{i}(SitePlugin):\n    site_name = 'p{i}'\n\n\n" for i in range(classes)
        )
        _hand_written(tmp_path, body)
        with pytest.raises(PluginProjectError, match=f"exactly one SitePlugin subclass, found {classes}"):
            read_project(tmp_path)

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
        assert _fixtures_root(spec) == view.fixtures_root == fixtures_root(
            suite_member=second, module_name=module_name_for(name)
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
            'from pathlib import Path\nFIXTURES_TREE = (\n    Path(__file__).parent\n    / "fixtures"\n)\n'
            "from graftpunk.testing.plugin import fixtures_are_sanitised as sanitised_fixtures\n",
            "from somewhere import FIXTURES_TREE, sanitised_fixtures\n",
            "FIXTURES_TREE, sanitised_fixtures = 1, 2\n",
        ],
    )
    def test_any_module_level_binding_satisfies(self, tmp_path: Path, conftest: str) -> None:
        _generate(tmp_path)
        (tmp_path / "tests" / "conftest.py").write_text(conftest)
        assert set(read_project(tmp_path).requirements.values()) == {True}

    @pytest.mark.parametrize(
        "conftest",
        [
            "",
            "from graftpunk.testing.plugin import *\n",
            "if True:\n    FIXTURES_TREE = 1\n    sanitised_fixtures = 2\n",
            "try:\n    from x import FIXTURES_TREE, sanitised_fixtures\nexcept ImportError:\n    pass\n",
        ],
    )
    def test_a_star_import_a_conditional_or_nothing_does_not(
        self, tmp_path: Path, conftest: str
    ) -> None:
        _generate(tmp_path)
        (tmp_path / "tests" / "conftest.py").write_text(conftest)
        assert set(read_project(tmp_path).requirements.values()) == {False}

    def test_a_generated_project_satisfies_every_requirement(self, tmp_path: Path) -> None:
        _generate(tmp_path)
        requirements = read_project(tmp_path).requirements
        assert requirements == {
            "tests/conftest.py:FIXTURES_TREE": True,
            "tests/conftest.py:sanitised_fixtures": True,
        }

    def test_binds_name_reads_module_level_only(self) -> None:
        tree = ast.parse("def f():\n    x = 1\nclass C:\n    y = 2\n")
        assert binds_name(tree, "f") and binds_name(tree, "C")
        assert not binds_name(tree, "x") and not binds_name(tree, "y")


def test_the_reader_never_imports_the_writer() -> None:
    script = (
        "import sys\n"
        "import graftpunk.devtools.plugin_project\n"
        "print('graftpunk.devtools.scaffold.write' in sys.modules)\n"
    )
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=True
    )
    assert result.stdout.strip() == "False"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_plugin_project.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'graftpunk.devtools.plugin_project'`.

- [ ] **Step 3: Write the reader**

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

A reader: never imports ``graftpunk.devtools.scaffold.write``.
"""

from __future__ import annotations

import ast
import re
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from graftpunk.devtools.scaffold import policy
from graftpunk.devtools.scaffold.render import module_name_for

__all__ = [
    "GP_FILL_MARKER",
    "CommandView",
    "DirectoryKind",
    "PluginProjectError",
    "PluginView",
    "ProjectView",
    "Span",
    "binds_name",
    "classify",
    "read_project",
]

DirectoryKind = Literal["empty", "plugin", "foreign"]

GP_FILL_MARKER = "GP-FILL"
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
    the end of its body), and its decorator's keywords, each the string literal it
    was given or ``None`` when it is not one."""

    method: str
    span: Span
    keywords: dict[str, str | None]

    @property
    def endpoint(self) -> str | None:
        """The declared ``endpoint=`` literal, or ``None`` for a command that has none."""
        return self.keywords.get("endpoint")


@dataclass(frozen=True)
class PluginView:
    """One entry point's plugin, as its module reads."""

    module_path: str  # project-relative, forward slashes
    class_name: str
    class_span: Span
    site_name: str | None
    base_url: str | None
    commands: tuple[CommandView, ...]
    markers: tuple[int, ...]  # the line of every GP-FILL marker in the module
    fixtures_root: str  # project-relative, trailing slash


@dataclass(frozen=True)
class ProjectView:
    """A directory's plugin project: its classification, one view per entry point, and
    whether its files bind each ``PROJECT_REQUIREMENTS`` name (keyed by the
    requirement's ``key``)."""

    directory: DirectoryKind
    plugins: tuple[PluginView, ...]
    requirements: dict[str, bool]


class PluginProjectError(ValueError):
    """The project cannot be read: the message names the file and the reason."""


def _load_pyproject(root: Path) -> dict[str, Any] | None:
    path = root / "pyproject.toml"
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise PluginProjectError(f"{path}: not valid TOML ({exc})") from exc


def _entry_points(data: dict[str, Any]) -> dict[str, str] | None:
    group = data.get("project", {}).get("entry-points", {}).get(policy.PLUGINS_ENTRY_POINT_GROUP)
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

    Raises:
        PluginProjectError: ``pyproject.toml`` is not valid TOML; an entry point's
            module is missing or does not parse; a module does not hold exactly
            one ``SitePlugin`` subclass; or a requirement's file does not parse.
    """
    data = _load_pyproject(root)
    if data is None:
        return ProjectView(directory="empty", plugins=(), requirements={})
    entry_points = _entry_points(data)
    if entry_points is None:
        return ProjectView(directory="foreign", plugins=(), requirements={})
    project_name = str(data.get("project", {}).get("name", ""))
    plugins = tuple(
        _read_plugin(root, key, value, project_name) for key, value in entry_points.items()
    )
    requirements = {r.key: _bound(root, r.path, r.name) for r in policy.PROJECT_REQUIREMENTS}
    return ProjectView(directory="plugin", plugins=plugins, requirements=requirements)


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
        raise PluginProjectError(f"{relative}: does not parse ({exc.msg}, line {exc.lineno})") from exc


def _last_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _read_plugin(root: Path, key: str, value: str, project_name: str) -> PluginView:
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
        raise PluginProjectError(
            f"{relative}: expected exactly one SitePlugin subclass, found {len(classes)} "
            f"({found}). A plugin module holds one plugin class."
        )
    (klass,) = classes
    package = module.split(".")[0]
    start = min([klass.lineno, *(d.lineno for d in klass.decorator_list)])
    return PluginView(
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
        yield CommandView(node.name, Span(start, node.end_lineno or node.lineno), keywords)


def _target_names(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return set().union(*(_target_names(e) for e in target.elts))
    return set()


def binds_name(tree: ast.Module, name: str) -> bool:
    """Whether *tree* binds *name* at module level.

    A plain import, an aliased import, an assignment (tuple targets included), an
    annotated assignment with a value, a ``def``, and a ``class`` bind; a line
    wrapped in parentheses binds like any other. A star import does not, and
    neither does anything inside an ``if``, a ``try``, or a function: only the
    module's top-level statements count.
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


def _bound(root: Path, relative: str, name: str) -> bool:
    if not (root / relative).is_file():
        return False
    _text, tree = _parse(root, relative)
    return binds_name(tree, name)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_plugin_project.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/graftpunk/devtools/plugin_project.py tests/unit/test_plugin_project.py
git commit -m "feat(devtools): plugin_project reads a plugin project once into one structural view"
```

---

### Task 4: `gp plugin info --json`, with its schema number

**Files:**
- Modify: `src/graftpunk/contracts.py` (`Surface`, `INFO_SCHEMA`, `_CURRENT`, `CLI_SURFACES`, `__all__`, docstring)
- Modify: `src/graftpunk/devtools/plugin_project.py` (new `info_payload`; `__all__`)
- Modify: `src/graftpunk/cli/scaffold_commands.py` (new `plugin_info`)
- Modify: `tests/unit/test_contracts.py`
- Test: `tests/unit/test_plugin_project_cli.py`

**Interfaces:**
- Consumes: `read_project`, `ProjectView`, `PluginProjectError` (Task 3); `_to_cli_name` (existing, `graftpunk/plugins/cli_plugin.py:775`).
- Produces: `contracts.INFO_SCHEMA: Final = 1`; `Surface = Literal["endpoints", "info", "sidecar"]`; `CLI_SURFACES == ("endpoints", "info")`, so `gp version --json` prints `"contracts": {"endpoints": 1, "info": 1}`; `plugin_project.info_payload(view: ProjectView) -> dict[str, object]` with the pinned field sets `{"schema", "directory", "plugins"}`, each plugin `{"module", "site_name", "base_url", "commands"}`, each command `{"name", "endpoint"}`; `gp plugin info --json [--dir PATH]`. The skill's preflight relays this payload unchanged.

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
_INFO_PLUGIN_V1 = {"module", "site_name", "base_url", "commands"}
_INFO_COMMAND_V1 = {"name", "endpoint"}


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


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
        assert plugin["module"] == "src/graftpunk_myshop/plugin.py"
        assert (plugin["site_name"], plugin["base_url"]) == ("myshop", "https://myshop.example")
        for command in plugin["commands"]:
            assert set(command) == _INFO_COMMAND_V1
        assert {"name": "api-orders", "endpoint": "GET /api/orders"} in plugin["commands"]

    def test_a_suite_lists_every_entry_point(self, recorded: Path) -> None:
        _new(recorded, "myshop")
        _new(recorded, "widgets")
        assert [p["site_name"] for p in _info(recorded)["plugins"]] == ["myshop", "widgets"]

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

    def test_an_unreadable_pyproject_is_a_one_line_refusal(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project\n")
        result = runner.invoke(app, ["plugin", "info", "--json", "--dir", str(tmp_path)])
        assert result.exit_code == 1
        assert "pyproject.toml: not valid TOML" in _plain(result.output)
        assert "Traceback" not in result.output
```

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_plugin_project_cli.py::TestPluginInfo tests/unit/test_contracts.py -q`
Expected: FAIL (`No such command 'info'`, and `ImportError: cannot import name 'INFO_SCHEMA'`).

- [ ] **Step 3: Declare the `info` number**

In `src/graftpunk/contracts.py`: change `Surface` to `Literal["endpoints", "info", "sidecar"]`; add `INFO_SCHEMA: Final = 1` after `ENDPOINTS_SCHEMA`; add `"info": INFO_SCHEMA,` to `_CURRENT`; set `CLI_SURFACES: Final[tuple[Surface, ...]] = ("endpoints", "info")`; add `"INFO_SCHEMA"` to `__all__`; and in the module docstring's first paragraph, add "the ``gp plugin info --json`` payload (``info``)," to the list of payloads.

- [ ] **Step 4: Build the payload and the command**

In `src/graftpunk/devtools/plugin_project.py`, add `from graftpunk.contracts import current_schema` and `from graftpunk.plugins.cli_plugin import _to_cli_name`, add `"info_payload"` to `__all__`, and append:

```python
def info_payload(view: ProjectView) -> dict[str, object]:
    """``gp plugin info --json``: facts about the directory, and none about the install.

    Within a schema version fields are added and never renamed or removed; the
    number comes from :mod:`graftpunk.contracts`. A command's ``name`` is the one
    the CLI registers: its ``name=`` literal, else its method kebab-cased.
    """
    return {
        "schema": current_schema("info"),
        "directory": view.directory,
        "plugins": [
            {
                "module": plugin.module_path,
                "site_name": plugin.site_name,
                "base_url": plugin.base_url,
                "commands": [
                    {
                        "name": command.keywords.get("name") or _to_cli_name(command.method),
                        "endpoint": command.endpoint,
                    }
                    for command in plugin.commands
                ],
            }
            for plugin in view.plugins
        ],
    }
```

In `src/graftpunk/cli/scaffold_commands.py`, add `import json` and `from graftpunk.devtools.plugin_project import PluginProjectError, info_payload, read_project`, and append:

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
        view = read_project(dir_)
    except PluginProjectError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]", soft_wrap=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(info_payload(view), indent=2, sort_keys=True))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_plugin_project_cli.py tests/unit/test_contracts.py tests/unit/test_cli_version.py -q`
Expected: PASS. `test_cli_version.py` now sees `info` under `contracts`.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/contracts.py src/graftpunk/devtools/plugin_project.py src/graftpunk/cli/scaffold_commands.py tests/unit/test_contracts.py tests/unit/test_plugin_project_cli.py
git commit -m "feat(scaffold): gp plugin info --json describes the directory's plugin project"
```

---

### Task 5: The single-command renderer and `gp plugin new --command`

**Files:**
- Modify: `src/graftpunk/devtools/scaffold/render.py` (new `CommandSelection`, `CommandSelectionError`, `PlannedCommand`, `RenderedCommand`, `command_identifier`, `plan_command`, `render_command`, `_planned_commands`; `ScaffoldSpec.commands`; `_decorator_lines`, `_render_command_stub`, `_render_command_stubs`, `_render_test_module`, `fixture_paths`, `_render_plugin_module` read planned commands; `_stub_endpoints` removed)
- Modify: `src/graftpunk/cli/scaffold_commands.py` (`--command` on `plugin_new`; `_command_selections`)
- Test: `tests/unit/test_scaffold_render.py`, `tests/unit/test_scaffold_cli.py`

**Interfaces:**
- Consumes: `parse_command_spec`, `EndpointSpecError` (foundations Task 6); `Endpoint.login_flow` (foundations Task 3); `_decorator_lines`, `_declared_extras`, `_needs_param_specs` (foundations Task 11).
- Produces: `@dataclass(frozen=True) class CommandSelection(name: str, method: str, template: str)`; `class CommandSelectionError(ValueError)`; `@dataclass(frozen=True) class PlannedCommand(identifier: str, cli_name: str | None, method: str, endpoint: Endpoint)`; `@dataclass(frozen=True) class RenderedCommand(lines: tuple[str, ...], needs_param_spec: bool, fixture: str)`; `command_identifier(name: str) -> str`; `plan_command(d: RunDigest, selection: CommandSelection) -> PlannedCommand`; `render_command(command: PlannedCommand, d: RunDigest) -> RenderedCommand`; `ScaffoldSpec.commands: tuple[CommandSelection, ...] = ()`; `_decorator_lines(identifier: str, endpoint_literal: str, param_specs: list[str], *, cli_name: str | None = None)`; in the CLI, `_command_selections(values: list[str]) -> tuple[CommandSelection, ...]`. Task 6 consumes `plan_command`, `render_command`, and `_command_selections`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_scaffold_render.py` (add `CommandSelection`, `CommandSelectionError`, `plan_command`, and `render_command` to its render import):

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
        words = [f"{chr(97 + i // 26)}{chr(97 + i % 26)}route" for i in range(_MAX_SCAFFOLD_ENDPOINTS + 1)]
        endpoints = tuple(dataclasses.replace(_SEARCH_ENDPOINT, template=f"/{w}") for w in words)
        selections = tuple(CommandSelection(w, "GET", f"/{w}") for w in words)
        plugin_code = render(self._spec(*selections, endpoints=endpoints))[
            "src/graftpunk_myshop/plugin.py"
        ]
        assert plugin_code.count("@command(") == _MAX_SCAFFOLD_ENDPOINTS + 1

    def test_a_name_given_twice_is_refused(self) -> None:
        with pytest.raises(CommandSelectionError, match="given twice"):
            self._spec(
                CommandSelection("order", "GET", "/orders/{order_id}"),
                CommandSelection("order", "GET", "/search"),
                endpoints=(_ORDERS_ENDPOINT, _SEARCH_ENDPOINT),
            )

    def test_an_endpoint_the_digest_lacks_is_refused(self) -> None:
        with pytest.raises(CommandSelectionError, match="not an endpoint in this run"):
            self._spec(CommandSelection("x", "GET", "/nowhere"), endpoints=(_ORDERS_ENDPOINT,))

    def test_a_login_flow_endpoint_is_refused(self) -> None:
        flagged = dataclasses.replace(_ORDERS_ENDPOINT, login_flow=True)
        with pytest.raises(CommandSelectionError, match="login flow"):
            self._spec(CommandSelection("x", "GET", "/orders/{order_id}"), endpoints=(flagged,))

    @pytest.mark.parametrize("name", ["class", "2fa", "has space", "a" * 41])
    def test_a_keyword_name_is_refused(self, name: str) -> None:
        with pytest.raises(CommandSelectionError, match=repr(name)):
            self._spec(CommandSelection(name, "GET", "/search"), endpoints=(_SEARCH_ENDPOINT,))

    def test_render_command_matches_the_stub_new_writes(self) -> None:
        d = _digest(endpoints=(_ORDERS_ENDPOINT,))
        command = plan_command(d, CommandSelection("order", "GET", "/orders/{order_id}"))
        rendered = render_command(command, d)
        plugin_code = render(
            self._spec(CommandSelection("order", "GET", "/orders/{order_id}"), endpoints=(_ORDERS_ENDPOINT,))
        )["src/graftpunk_myshop/plugin.py"]
        assert "\n".join(rendered.lines) in plugin_code
        assert rendered.needs_param_spec
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
        (run_dir / "network.har").write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}))

    def test_writes_only_the_selected_stubs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._record(tmp_path, monkeypatch)
        target = tmp_path / "out"
        result = runner.invoke(
            _build_app(),
            ["plugin", "new", "myshop", "--from-run", "myshop", "--dir", str(target),
             "--command", "order=GET /api/orders/{order_id}"],
        )
        assert result.exit_code == 0, result.output
        plugin_code = (target / "src" / "graftpunk_myshop" / "plugin.py").read_text()
        assert "def order(" in plugin_code
        assert "def api_orders(" not in plugin_code
        assert "tests/fixtures/get_api_orders_{order_id}.json" in _plain(result.output)

    @pytest.mark.parametrize("value", ["orders GET /api/orders", "=GET /api/orders", "orders=", "orders=get /api/orders"])
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
            ["plugin", "new", "myshop", "--from-run", "myshop", "--dir", str(target), "--command", value],
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
            ["plugin", "new", "myshop", "--from-run", "myshop", "--dir", str(target),
             "--command", "orders=GET /api/orders", "--command", "orders=GET /api/orders/{order_id}"],
        )
        assert result.exit_code == 1
        assert "given twice" in _plain(result.output)
        assert not target.exists()

    def test_an_endpoint_the_digest_lacks_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._record(tmp_path, monkeypatch)
        result = runner.invoke(
            _build_app(),
            ["plugin", "new", "myshop", "--from-run", "myshop", "--dir", str(tmp_path / "out"),
             "--command", "x=GET /api/nowhere"],
        )
        assert result.exit_code == 1
        assert "not an endpoint in this run" in _plain(result.output)

    def test_command_without_from_run_is_refused(self, tmp_path: Path) -> None:
        result = runner.invoke(
            _build_app(),
            ["plugin", "new", "myshop", "--dir", str(tmp_path), "--command", "orders=GET /api/orders"],
        )
        assert result.exit_code == 1
        assert "--command requires --from-run" in _plain(result.output)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_render.py::TestExplicitSelection tests/unit/test_scaffold_cli.py::TestPluginNewCommand -q`
Expected: FAIL (`ImportError: cannot import name 'CommandSelection'`).

- [ ] **Step 3: Plan commands in the renderer**

In `src/graftpunk/devtools/scaffold/render.py`, add `from graftpunk.plugins.cli_plugin import _to_cli_name`; add `"CommandSelection"`, `"CommandSelectionError"`, `"PlannedCommand"`, `"RenderedCommand"`, `"command_identifier"`, `"plan_command"`, and `"render_command"` to `__all__`; add above `ScaffoldSpec`:

```python
@dataclass(frozen=True)
class CommandSelection:
    """One ``--command "<name>=<METHOD> <template>"``: an endpoint, under a name."""

    name: str
    method: str
    template: str


class CommandSelectionError(ValueError):
    """An explicit selection the generator cannot render; nothing is written."""


_COMMAND_NAME_RE = re.compile(rf"[A-Za-z][A-Za-z0-9_-]{{0,{_MAX_COMMAND_NAME - 1}}}")


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
    if keyword.iskeyword(identifier) or keyword.issoftkeyword(identifier):
        raise CommandSelectionError(f"Command name {name!r} is a Python keyword; choose another.")
    return identifier
```

add to `ScaffoldSpec` the field `commands: tuple[CommandSelection, ...] = ()` after `graftpunk_version`, and extend its `__post_init__`:

```python
    def __post_init__(self) -> None:
        validate_plugin_name(self.name)
        if self.commands:
            if self.digest is None:
                raise CommandSelectionError(
                    "An explicit command selection needs a digest to select from."
                )
            _planned_commands(self)
```

and add below `_run_label`:

```python
@dataclass(frozen=True)
class PlannedCommand:
    """One stub to render: its method name, the CLI name to pin when kebab-casing the
    method would not produce the agreed one, the HTTP method, and the endpoint."""

    identifier: str
    cli_name: str | None
    method: str
    endpoint: Endpoint


@dataclass(frozen=True)
class RenderedCommand:
    """One stub, at class-body indentation with no trailing blank line; whether it needs
    ``PluginParamSpec`` imported; and the fixture filename its test looks for."""

    lines: tuple[str, ...]
    needs_param_spec: bool
    fixture: str


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
    cli_name = None if _to_cli_name(identifier) == selection.name else selection.name
    return PlannedCommand(identifier, cli_name, selection.method, endpoint)


def _planned_commands(spec: ScaffoldSpec) -> list[PlannedCommand]:
    """The stubs *spec* renders: its explicit selections, uncapped and in the order
    given, or else every eligible endpoint up to ``_MAX_SCAFFOLD_ENDPOINTS`` under
    generated names."""
    if spec.digest is None:
        return []
    if spec.commands:
        planned: list[PlannedCommand] = []
        seen: set[str] = set()
        for selection in spec.commands:
            command = plan_command(spec.digest, selection)
            if command.identifier in seen:
                raise CommandSelectionError(f"Command name {selection.name!r} is given twice.")
            seen.add(command.identifier)
            planned.append(command)
        return planned
    seen_names: set[str] = set()
    return [
        PlannedCommand(_command_name(e.template, seen_names), None, e.methods[0], e)
        for e in _ordered_endpoints(spec.digest)[:_MAX_SCAFFOLD_ENDPOINTS]
    ]


def render_command(command: PlannedCommand, d: RunDigest) -> RenderedCommand:
    """The single-command entry point: the stub ``gp plugin new`` writes for *command*,
    which ``gp plugin add-command`` inserts on its own."""
    lines = _render_command_stub(command, _run_label(d))
    return RenderedCommand(
        lines=tuple(lines[:-1] if lines[-1] == "" else lines),
        needs_param_spec=_needs_param_specs(command.endpoint),
        fixture=capture_filename(
            command.method, command.endpoint.template, command.endpoint.content_type
        ),
    )
```

- [ ] **Step 4: Render every stub, test, and fixture path from the plan**

Still in `render.py`:

1. Give `_decorator_lines` a keyword-only `cli_name: str | None = None`, make its help literal `f"GP-FILL: describe {cli_name or identifier}"` (rename its first parameter `name` to `identifier`), and after the `help=` lines add:

```python
    if cli_name is not None:
        lines.extend(_literal_lines(cli_name, indent=len(_L2), prefix="name="))
```

2. Change `_render_command_stub`'s signature to `(command: PlannedCommand, run_label: str) -> list[str]`, and replace its first two lines with:

```python
    endpoint = command.endpoint
    method = command.method
    name = command.identifier
```

and its `_decorator_lines(...)` call with `_decorator_lines(name, f"{method} {endpoint.template}", param_specs if _needs_param_specs(endpoint) else [], cli_name=command.cli_name)`.

3. Replace `_stub_endpoints` and `_render_command_stubs` with:

```python
def _render_command_stubs(spec: ScaffoldSpec) -> list[str]:
    planned = _planned_commands(spec)
    if spec.digest is None or not planned:
        return [
            '    @command(help="GP-FILL: describe this command")',
            "    def example(self, ctx: CommandContext) -> dict:",
            '        """GP-FILL: what this command does."""',
            '        return ctx.request_json("GET", "/GP-FILL/path")',
        ]
    lines: list[str] = []
    for command in planned:
        lines.extend(_render_command_stub(command, _run_label(spec.digest)))
    return lines
```

4. In `_render_plugin_module`, replace `any(_needs_param_specs(e) for e in _stub_endpoints(spec))` with `any(_needs_param_specs(c.endpoint) for c in _planned_commands(spec))`.

5. Replace the body of `fixture_paths` with:

```python
    return [
        f"{_fixtures_root(spec)}"
        f"{capture_filename(c.method, c.endpoint.template, c.endpoint.content_type)}"
        for c in _planned_commands(spec)
    ]
```

6. In `_render_test_module`, replace `endpoints = _stub_endpoints(spec)` and `has_endpoint_tests = bool(endpoints)` with `planned = _planned_commands(spec)` and `has_endpoint_tests = bool(planned)`, and replace the loop head through `_, path_params = ...` with:

```python
    for command in planned:
        name = command.identifier
        # The same seeding as _render_command_stub, so the identifiers here are
        # the ones the stub actually declares.
        _, path_params = _templated_url(command.endpoint.template, {"self", "ctx"})
```

(delete the now-unused `seen: set[str] = set()` line above the loop).

- [ ] **Step 5: Add `--command` to `gp plugin new`**

In `src/graftpunk/cli/scaffold_commands.py`, import `CommandSelection` and `CommandSelectionError` from `render`, and `EndpointSpecError`, `parse_command_spec` from `graftpunk.har.naming`; add above `plugin_new`:

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

pass `commands=selections,` to the `ScaffoldSpec(...)` call, and add this arm directly above `except NotAPluginSuiteError as exc:`:

```python
    except CommandSelectionError as exc:
        LOG.debug("scaffold_refused", reason="bad_command")
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_render.py tests/unit/test_scaffold_cli.py tests/unit/test_scaffold_project.py tests/unit/test_plugin_project.py -q`
Expected: PASS, including the ruff-clean tree tests.

- [ ] **Step 7: Commit**

```bash
git add src/graftpunk/devtools/scaffold/render.py src/graftpunk/cli/scaffold_commands.py tests/unit/test_scaffold_render.py tests/unit/test_scaffold_cli.py
git commit -m "feat(scaffold): gp plugin new --command selects and names the stubs through one single-command renderer"
```

---

### Task 6: `insert.py` and `gp plugin add-command`

**Files:**
- Create: `src/graftpunk/devtools/scaffold/insert.py`
- Modify: `src/graftpunk/devtools/scaffold/pysrc.py` (new `with_import` and its helpers)
- Modify: `src/graftpunk/cli/scaffold_commands.py` (new `plugin_add_command`)
- Modify: `tests/unit/test_scaffold_write.py` (`test_the_mutators_write_only_through_write_py` covers `insert.py`)
- Test: `tests/unit/test_plugin_project_cli.py`, `tests/unit/test_scaffold_pysrc.py`

**Interfaces:**
- Consumes: `read_project`, `PluginView`, `PluginProjectError` (Task 3); `plan_command`, `render_command`, `CommandSelection`, `CommandSelectionError` (Task 5); `_command_selections` (Task 5); `apply_changes`, `PlannedChange`, `validate_python`, `ChangeConflictError`, `InvalidChangeError` (foundations Task 10); `resolve_run` (existing, `observe_commands.py:53`).
- Produces: `pysrc.with_import(text: str, module: str, name: str) -> str`; `insert.CommandInsertError(ValueError)`; `@dataclass(frozen=True) class AddedCommand(module: Path, cli_name: str, fixture: str)`; `insertion_line(plugin: PluginView) -> int`; `add_command(root: Path, plugin_name: str, d: RunDigest, selection: CommandSelection) -> AddedCommand`; `gp plugin add-command PLUGIN --from-run SESSION --command "NAME=METHOD template" [--run RUN_ID] [--dir PATH]`. Task 7 consumes `with_import`.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_scaffold_pysrc.py`, add `from graftpunk.devtools.scaffold.pysrc import with_import` to the imports at the top, and append:

```python
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
        assert with_import(_OLD_CONFTEST, "graftpunk.testing.plugin", "site_env_scrubber") == _OLD_CONFTEST
        aliased = "from elsewhere import thing as Path\n"
        assert with_import(aliased, "pathlib", "Path") == aliased

    def test_a_module_with_no_imports_gets_one_after_its_docstring(self) -> None:
        result = with_import('"""Doc."""\n\nx = 1\n', "pathlib", "Path")
        assert result == '"""Doc."""\n\nfrom pathlib import Path\n\nx = 1\n'
```

Append to `tests/unit/test_plugin_project_cli.py` (add `import ast`, `import subprocess`, `import sys`, and `from graftpunk.devtools.plugin_project import read_project` to its imports):

```python
def _add(project: Path, plugin: str, command: str) -> object:
    return runner.invoke(
        app,
        ["plugin", "add-command", plugin, "--from-run", "myshop", "--command", command,
         "--dir", str(project)],
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


def _hand_written_project(root: Path) -> Path:
    (root / "pyproject.toml").write_text(
        '[project]\nname = "graftpunk-myshop"\n\n'
        '[project.entry-points."graftpunk.plugins"]\n'
        'myshop = "graftpunk_myshop.plugin:MyshopPlugin"\n\n'
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

    def test_a_first_command_goes_after_the_class_bodys_last_statement(self, recorded: Path) -> None:
        module = _hand_written_project(recorded)
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 0, result.output
        (plugin,) = read_project(recorded).plugins
        (command,) = plugin.commands
        assert command.span.start > module.read_text().splitlines().index('    base_url = "https://myshop.example"')

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

    def test_an_unknown_plugin_is_refused(self, recorded: Path) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        result = _add(recorded, "elsewhere", "order=GET /api/orders/{order_id}")
        assert result.exit_code == 1
        assert "myshop" in _plain(result.output)

    @pytest.mark.parametrize("value", ["orders GET /api/orders", "=GET /api/orders", "orders="])
    def test_both_entry_points_refuse_a_malformed_value_with_the_same_text(
        self, recorded: Path, value: str
    ) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        added = _add(recorded, "myshop", value)
        created = runner.invoke(
            app,
            ["plugin", "new", "other", "--from-run", "myshop", "--dir", str(recorded / "other"),
             "--command", value],
        )
        assert added.exit_code == created.exit_code == 1
        assert _plain(added.output) == _plain(created.output)
```

In `tests/unit/test_scaffold_write.py`, change the parametrize list of `test_the_mutators_write_only_through_write_py` to `["project.py", "pyproject_edit.py", "insert.py"]`.

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_pysrc.py tests/unit/test_plugin_project_cli.py::TestAddCommand tests/unit/test_scaffold_write.py -q`
Expected: FAIL (`ImportError: cannot import name 'with_import'`).

- [ ] **Step 3: Write `with_import`**

In `src/graftpunk/devtools/scaffold/pysrc.py`, add `import ast` and append:

```python
def _imported_module(node: ast.Import | ast.ImportFrom) -> str:
    if isinstance(node, ast.ImportFrom):
        return node.module or ""
    return node.names[0].name


def _binds(node: ast.Import | ast.ImportFrom, name: str) -> bool:
    return any(
        alias.name != "*" and (alias.asname or alias.name.split(".")[0]) == name
        for alias in node.names
    )


def _joined(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def with_import(text: str, module: str, name: str) -> str:
    """*text* with ``from {module} import {name}`` in its module-level imports, placed
    the way isort places a from-import.

    Unchanged when a module-level import already binds *name*. Otherwise merged
    into an existing ``from {module} import ...``, re-rendered in isort's name
    order; failing that, a line of its own among the imports of its section (the
    standard library before everything else), in module order; for a module with
    no imports, after its docstring and any ``__future__`` import. Written for the
    from-import shape generated files use; the project's own ruff run reports
    anything else.
    """
    tree = ast.parse(text)
    lines = text.splitlines()
    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    if any(_binds(node, name) for node in imports):
        return text
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
            merged = _import_lines(module, *sorted([*names, name], key=_isort_name_key))
            return _joined(lines[: node.lineno - 1] + merged + lines[node.end_lineno or node.lineno :])
    new_line = f"from {module} import {name}"
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
```

- [ ] **Step 4: Write the inserter**

Create `src/graftpunk/devtools/scaffold/insert.py`:

```python
"""Places one rendered command stub inside a plugin class, through ``write.py``.

The placement rule is code, and total over any class the project reader
accepts: immediately after the class's last ``@command``-decorated method, or,
for a class with no command yet, immediately after the class body's last
statement, wherever the class sits in the module. A module a developer has
extended with helpers above or below the class is handled rather than refused
(graft skill spec, 2026-09-21). It inserts a stub, the imports the stub uses,
and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from graftpunk.devtools.plugin_project import PluginView, read_project
from graftpunk.devtools.scaffold.pysrc import with_import
from graftpunk.devtools.scaffold.render import CommandSelection, plan_command, render_command
from graftpunk.devtools.scaffold.write import PlannedChange, apply_changes, validate_python
from graftpunk.har.digest import RunDigest
from graftpunk.plugins.cli_plugin import _to_cli_name

__all__ = ["AddedCommand", "CommandInsertError", "add_command", "insertion_line"]

_PLUGINS_MODULE = "graftpunk.plugins"
# What every rendered stub reads from graftpunk.plugins; PluginParamSpec joins
# them for a stub with explicit parameter specs.
_STUB_IMPORTS = ("CommandContext", "command")


class CommandInsertError(ValueError):
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
        taken.add(command.keywords.get("name") or _to_cli_name(command.method))
    return taken


def add_command(
    root: Path, plugin_name: str, d: RunDigest, selection: CommandSelection
) -> AddedCommand:
    """Add *selection*'s stub to the plugin whose ``site_name`` is *plugin_name*.

    Raises:
        CommandInsertError: *root* is not a plugin project, no plugin has that
            name, or the name is taken (as a method name or a CLI name).
        CommandSelectionError: See :func:`plan_command`.
        PluginProjectError: See :func:`read_project`.
    """
    view = read_project(root)
    if view.directory != "plugin":
        raise CommandInsertError(f"{root} is not a graftpunk plugin project ({view.directory}).")
    plugin = next((p for p in view.plugins if p.site_name == plugin_name), None)
    if plugin is None:
        names = ", ".join(sorted(p.site_name or p.class_name for p in view.plugins))
        raise CommandInsertError(f"No plugin named {plugin_name!r} in {root}; it holds: {names}.")
    command = plan_command(d, selection)
    cli_name = command.cli_name or _to_cli_name(command.identifier)
    if {command.identifier, cli_name} & _taken_names(plugin):
        raise CommandInsertError(
            f"{plugin.module_path} already has a command named {selection.name!r}."
        )
    rendered = render_command(command, d)
    module = root / plugin.module_path
    original = module.read_text(encoding="utf-8")
    lines = original.splitlines()
    at = insertion_line(plugin)
    text = "\n".join([*lines[:at], "", *rendered.lines, *lines[at:]]) + "\n"
    for name in (*_STUB_IMPORTS, *(("PluginParamSpec",) if rendered.needs_param_spec else ())):
        text = with_import(text, _PLUGINS_MODULE, name)
    apply_changes([PlannedChange(module, text, original=original, validate=validate_python)])
    return AddedCommand(
        module=module, cli_name=cli_name, fixture=f"{plugin.fixtures_root}{rendered.fixture}"
    )
```

- [ ] **Step 5: Add the command**

In `src/graftpunk/cli/scaffold_commands.py`, add `from graftpunk.devtools.scaffold.insert import CommandInsertError, add_command` and `from graftpunk.devtools.scaffold.write import ChangeConflictError, InvalidChangeError`, and append:

```python
@plugin_app.command("add-command")
def plugin_add_command(
    plugin: Annotated[str, typer.Argument(help="The plugin's site_name")],
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
    except (
        CommandInsertError,
        CommandSelectionError,
        PluginProjectError,
        InvalidChangeError,
        ChangeConflictError,
    ) as exc:
        LOG.debug("add_command_refused", reason=type(exc).__name__)
        console.print(f"[red]{escape(str(exc))}[/red]", soft_wrap=True)
        raise typer.Exit(1) from None
    except OSError as exc:
        console.print(f"[red]Could not write: {escape(exc.strerror or str(exc))}[/red]")
        raise typer.Exit(1) from None
    console.print(
        f"[green]Added[/green] {escape(added.cli_name)} to {escape(str(added.module))}",
        soft_wrap=True,
    )
    console.print(
        f"[bold]Next:[/bold] its test looks for {escape(added.fixture)}", soft_wrap=True
    )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_pysrc.py tests/unit/test_plugin_project_cli.py tests/unit/test_scaffold_write.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/graftpunk/devtools/scaffold/pysrc.py src/graftpunk/devtools/scaffold/insert.py src/graftpunk/cli/scaffold_commands.py tests/unit/test_scaffold_pysrc.py tests/unit/test_plugin_project_cli.py tests/unit/test_scaffold_write.py
git commit -m "feat(scaffold): gp plugin add-command inserts one generated stub into an existing plugin"
```

---

### Task 7: `upgrade.py` and `gp plugin upgrade`

**Files:**
- Create: `src/graftpunk/devtools/scaffold/upgrade.py`
- Modify: `src/graftpunk/cli/scaffold_commands.py` (new `plugin_upgrade`)
- Modify: `tests/unit/test_scaffold_write.py` (the routing test covers `upgrade.py`)
- Test: `tests/unit/test_scaffold_upgrade.py`, `tests/unit/test_plugin_project_cli.py`

**Interfaces:**
- Consumes: `read_project`, `ProjectView` (Task 3); `policy.PROJECT_REQUIREMENTS`, `ProjectRequirement` (Task 2); `pysrc.with_import` (Task 6), `pysrc._import_block_lines` (Task 2); `apply_changes`, `PlannedChange`, `validate_python` (foundations Task 10).
- Produces: `upgrade.UpgradeRefusedError(ValueError)`; `missing_requirements(view: ProjectView) -> tuple[ProjectRequirement, ...]`; `with_requirements(text: str, requirements: Sequence[ProjectRequirement]) -> str`; `upgrade_project(root: Path) -> tuple[ProjectRequirement, ...]` (the requirements it applied); `gp plugin upgrade [--dir PATH]`. Task 8 consumes `missing_requirements`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_scaffold_upgrade.py`:

```python
"""gp plugin upgrade's migrator (graft skill spec, 2026-09-21)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from graftpunk.devtools.plugin_project import read_project
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
        assert set(read_project(tmp_path).requirements.values()) == {True}
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
        with pytest.raises(UpgradeRefusedError, match="empty"):
            upgrade_project(tmp_path)
```

Append to `tests/unit/test_plugin_project_cli.py`:

```python
class TestPluginUpgrade:
    def test_prints_what_it_added_and_then_nothing(self, tmp_path: Path) -> None:
        from graftpunk.devtools.scaffold.project import write_scaffold
        from graftpunk.devtools.scaffold.render import ScaffoldSpec

        write_scaffold(
            tmp_path,
            ScaffoldSpec(name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example"),
        )
        (tmp_path / "tests" / "conftest.py").write_text(
            'from graftpunk.testing.plugin import site_env_scrubber\n\nscrub_site_env = site_env_scrubber("MYSHOP_")\n'
        )
        first = runner.invoke(app, ["plugin", "upgrade", "--dir", str(tmp_path)])
        assert first.exit_code == 0, first.output
        assert "tests/conftest.py: added FIXTURES_TREE" in _plain(first.output)
        assert "tests/conftest.py: added sanitised_fixtures" in _plain(first.output)
        second = runner.invoke(app, ["plugin", "upgrade", "--dir", str(tmp_path)])
        assert second.exit_code == 0
        assert "Nothing to upgrade" in _plain(second.output)
```

In `tests/unit/test_scaffold_write.py`, change the routing test's parametrize list to `["project.py", "pyproject_edit.py", "insert.py", "upgrade.py"]`.

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_upgrade.py tests/unit/test_plugin_project_cli.py::TestPluginUpgrade -q`
Expected: collection error, `ModuleNotFoundError: No module named 'graftpunk.devtools.scaffold.upgrade'`.

- [ ] **Step 3: Write the migrator**

Create `src/graftpunk/devtools/scaffold/upgrade.py`:

```python
"""Brings an existing plugin project up to ``PROJECT_REQUIREMENTS``, through ``write.py``.

On purpose and by name: ``gp plugin upgrade`` runs it, and nothing else does.
It applies only what the project reader says is unbound, so it is idempotent
under the reader's own definition, and it changes nothing a project already has
(graft skill spec, 2026-09-21). Each requirement's imports are merged the way
isort places them and its statement is appended, so an upgraded conftest reads
exactly like a generated one.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from pathlib import Path

from graftpunk.devtools.plugin_project import ProjectView, read_project
from graftpunk.devtools.scaffold import policy
from graftpunk.devtools.scaffold.policy import ProjectRequirement
from graftpunk.devtools.scaffold.pysrc import _import_block_lines, with_import
from graftpunk.devtools.scaffold.write import PlannedChange, apply_changes, validate_python

__all__ = ["UpgradeRefusedError", "missing_requirements", "upgrade_project", "with_requirements"]


class UpgradeRefusedError(ValueError):
    """The directory is not a plugin project; nothing was written."""


def missing_requirements(view: ProjectView) -> tuple[ProjectRequirement, ...]:
    """The requirements *view* says the project's files do not bind, in declared order."""
    return tuple(r for r in policy.PROJECT_REQUIREMENTS if not view.requirements.get(r.key, False))


def with_requirements(text: str, requirements: Sequence[ProjectRequirement]) -> str:
    """*text* with each requirement's imports merged and its statement appended."""
    if not text.strip():
        imports = [pair for r in requirements for pair in r.imports]
        return "\n".join([*_import_block_lines(imports), "", *(r.statement for r in requirements)]) + "\n"
    for requirement in requirements:
        for module, name in requirement.imports:
            text = with_import(text, module, name)
        body = ast.parse(text).body
        after_definition = bool(body) and isinstance(
            body[-1], (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        )
        text = text.rstrip("\n") + ("\n\n\n" if after_definition else "\n") + requirement.statement + "\n"
    return text


def upgrade_project(root: Path) -> tuple[ProjectRequirement, ...]:
    """Apply every requirement *root*'s project lacks, and return them.

    Raises:
        UpgradeRefusedError: *root* is not a plugin project.
        PluginProjectError: See :func:`read_project`.
    """
    view = read_project(root)
    if view.directory != "plugin":
        raise UpgradeRefusedError(f"{root} is not a graftpunk plugin project ({view.directory}).")
    missing = missing_requirements(view)
    by_path: dict[str, list[ProjectRequirement]] = {}
    for requirement in missing:
        by_path.setdefault(requirement.path, []).append(requirement)
    changes: list[PlannedChange] = []
    for relative, requirements in by_path.items():
        path = root / relative
        original = path.read_text(encoding="utf-8") if path.is_file() else None
        content = with_requirements(original or "", requirements)
        changes.append(PlannedChange(path, content, original=original, validate=validate_python))
    apply_changes(changes)
    return missing
```

- [ ] **Step 4: Add the command**

In `src/graftpunk/cli/scaffold_commands.py`, add `from graftpunk.devtools.scaffold.upgrade import UpgradeRefusedError, upgrade_project`, and append:

```python
@plugin_app.command("upgrade")
def plugin_upgrade(
    dir_: Annotated[Path, typer.Option("--dir", help="Project directory")] = Path("."),
) -> None:
    """Bring a plugin project up to the current generated shape, changing nothing it has."""
    try:
        applied = upgrade_project(dir_)
    except (UpgradeRefusedError, PluginProjectError, InvalidChangeError, ChangeConflictError) as exc:
        console.print(f"[red]{escape(str(exc))}[/red]", soft_wrap=True)
        raise typer.Exit(1) from None
    if not applied:
        console.print("Nothing to upgrade: the project already has every requirement.")
        return
    for requirement in applied:
        console.print(f"{escape(requirement.path)}: added {escape(requirement.name)}", soft_wrap=True)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_scaffold_upgrade.py tests/unit/test_plugin_project_cli.py tests/unit/test_scaffold_write.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/devtools/scaffold/upgrade.py src/graftpunk/cli/scaffold_commands.py tests/unit/test_scaffold_upgrade.py tests/unit/test_plugin_project_cli.py tests/unit/test_scaffold_write.py
git commit -m "feat(scaffold): gp plugin upgrade applies the project requirements a plugin lacks"
```

---

### Task 8: `plugin_check.py`, `gp plugin check`, and the three consumers pinned to the declaration

**Files:**
- Create: `src/graftpunk/devtools/plugin_check.py`
- Modify: `src/graftpunk/cli/scaffold_commands.py` (new `plugin_check`)
- Test: `tests/unit/test_plugin_check.py`

**Interfaces:**
- Consumes: `read_project`, `PluginProjectError` (Task 3); `missing_requirements` (Task 7); `policy.PROJECT_REQUIREMENTS` (Task 2).
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


def test_the_three_consumers_follow_the_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One entry added to PROJECT_REQUIREMENTS: the renderer emits it, check reports
    it on a project that lacks it, and upgrade applies it."""
    _clean_project(tmp_path)
    probe = ProjectRequirement(path="tests/conftest.py", name="extra_probe", statement="extra_probe = 1")
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

`missing_requirements` lives in `upgrade.py`, which imports `write.py`, and the lint must not import the writer; so the lint applies the same one-line rule to the view itself, reading the same declaration. Create `src/graftpunk/devtools/plugin_check.py`:

```python
"""``gp plugin check``: a lint over the project reader's view. It never edits.

Reports a remaining ``GP-FILL`` marker, a module without exactly one
``SitePlugin`` subclass, and a ``PROJECT_REQUIREMENTS`` entry the project lacks,
which ``gp plugin upgrade`` fixes. It does not compare a declared endpoint
against the request call: the declaration is authoritative by design, and a
check that could only ever be weak would give an author a reason to drop the
keyword. It does not restate the fixtures check, which the generated suite runs
(graft skill spec, 2026-09-21). A reader: never imports ``write.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from graftpunk.devtools.plugin_project import GP_FILL_MARKER, PluginProjectError, read_project
from graftpunk.devtools.scaffold import policy

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
    """Every finding in *root*'s plugin project, markers first, in file order."""
    try:
        view = read_project(root)
    except PluginProjectError as exc:
        return [Finding(path=".", line=None, message=str(exc))]
    if view.directory != "plugin":
        return [
            Finding(
                path=".",
                line=None,
                message=f"not a graftpunk plugin project ({view.directory}).",
            )
        ]
    findings = [
        Finding(path=plugin.module_path, line=line, message=f"{GP_FILL_MARKER} marker left to fill in.")
        for plugin in view.plugins
        for line in plugin.markers
    ]
    findings.extend(
        Finding(
            path=requirement.path,
            line=None,
            message=f"does not bind {requirement.name}; gp plugin upgrade adds it.",
        )
        for requirement in policy.PROJECT_REQUIREMENTS
        if not view.requirements.get(requirement.key, False)
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
- Modify: `docs/PLUGIN_DEVELOPMENT.md` ("The gate" at `:1020-1056`, "Before you publish" at `:1084-1097`)
- Test: `tests/unit/test_project_gate.py`

**Interfaces:**
- Consumes: `gp plugin check` (Task 8); the CLI walker `_check_invocation` (existing, `tests/unit/test_plugin_development_guide.py:205`).
- Produces: `policy.PROJECT_GATE: Final[tuple[str, ...]] = ("pytest", "ruff check .", "ruff format --check .", "gp plugin check")`, reproduced once in the generated README's `## Checks` block. The skill's `harden.md` points at the guide's "The gate" and never enumerates it.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_project_gate.py`:

```python
"""One gate, reproduced once and pinned everywhere it is quoted
(graft skill spec, 2026-09-21, "The project gate and the project's requirements")."""

from __future__ import annotations

import re

from graftpunk.devtools.scaffold import policy
from graftpunk.devtools.scaffold.render import ScaffoldSpec, render
from tests.unit.test_plugin_development_guide import GUIDE_TEXT, _check_invocation


def _section(text: str, heading: str) -> str:
    """*text* from the line ``heading`` to the next heading of the same or higher level."""
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
    start = lines.index(heading)
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if re.match(rf"^#{{1,{level}}}\s", lines[i])
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


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
    assert policy.PROJECT_GATE == ("pytest", "ruff check .", "ruff format --check .", "gp plugin check")


def test_the_generated_readme_quotes_the_gate() -> None:
    spec = ScaffoldSpec(
        name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example"
    )
    readme = render(spec)["README.md"]
    assert _block_lines(_section(readme, "## Checks"), "bash") == list(policy.PROJECT_GATE)


def test_the_guides_gate_section_quotes_the_gate() -> None:
    gate = _section(GUIDE_TEXT, "### The gate")
    assert _block_lines(gate, "bash") == list(policy.PROJECT_GATE)


def test_the_guides_ci_example_runs_the_gate_as_one_step() -> None:
    gate = _section(GUIDE_TEXT, "### The gate")
    assert _run_step_lines(_block_lines(gate, "yaml")) == list(policy.PROJECT_GATE)


def test_the_publish_checklist_names_the_gate_and_reproduces_none_of_it() -> None:
    checklist = _section(GUIDE_TEXT, "### Before you publish")
    items = [line for line in checklist.splitlines() if line.startswith("- [ ]")]
    assert items[0].startswith("- [ ] The gate is green")
    rest = "\n".join(items[1:])
    assert "GP-FILL" not in rest
    for entry in policy.PROJECT_GATE:
        assert f"`{entry}`" not in checklist


def test_every_gp_entry_in_the_gate_resolves_through_the_cli() -> None:
    for entry in policy.PROJECT_GATE:
        if entry.startswith("gp "):
            _check_invocation(entry, "PROJECT_GATE")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_project_gate.py -q`
Expected: FAIL (`AttributeError: module 'graftpunk.devtools.scaffold.policy' has no attribute 'PROJECT_GATE'`).

- [ ] **Step 3: Declare the gate and render it into the README**

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

- [ ] **Step 4: Quote the gate in the guide**

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

In "Before you publish", replace the first item (`No GP-FILL marker is left anywhere in the project.`) with `- [ ] The gate is green: every command in [The gate](#the-gate) passes.` and delete the item `` - [ ] `pytest`, `ruff check .`, and `ruff format --check .` are green. ``.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_project_gate.py tests/unit/test_plugin_development_guide.py tests/unit/test_scaffold_render.py tests/unit/test_scaffold_cli.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/devtools/scaffold/policy.py src/graftpunk/devtools/scaffold/render.py docs/PLUGIN_DEVELOPMENT.md tests/unit/test_project_gate.py
git commit -m "feat(scaffold): PROJECT_GATE is the one gate, quoted in the generated README and the guide"
```

---

### Task 10: The guide's fixture recipe and sidecar, the help string, the CHANGELOG, and the full gate

**Files:**
- Modify: `docs/PLUGIN_DEVELOPMENT.md` ("Test against fixtures, not against the site" sidecar example at `:944-957`; "Deriving a fixture from a capture" at `:968-988`)
- Modify: `src/graftpunk/cli/scaffold_commands.py:1` (module docstring), `:31` (`plugin_app` help)
- Modify: `README.md:311` (the `plugin` line of the CLI Reference block)
- Modify: `CHANGELOG.md` (`[Unreleased]` Added)

**Interfaces:**
- Consumes: everything above.
- Produces: the guide sections the skill's `harden.md` cites ("Deriving a fixture from a capture", "The gate", "Before you publish").

- [ ] **Step 1: Update the guide's sidecar example and recipe**

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
- **`gp plugin info`, `add-command`, `upgrade`, and `check`, and the in-suite fixtures check.** `gp plugin info --json` describes the working directory as `empty`, `plugin`, or `foreign`, and for a plugin project lists each entry point's module, `site_name`, `base_url`, and commands with their declared endpoints. `gp plugin new --command "NAME=METHOD template"` (repeatable) makes only the stubs you name, under your names, with no twelve-stub cap; `gp plugin add-command PLUGIN --from-run SESSION --command ...` adds one stub in the same shape to an existing plugin and prints the fixture its test reads. A generated `tests/conftest.py` now also carries `FIXTURES_TREE` and `fixtures_are_sanitised`, a check that fails the suite when a committed fixture has no sidecar, is an unchanged copy of its capture, or contains a flagged cookie or token name; on an existing project, `gp plugin upgrade` adds the two lines. `gp plugin check` reports `GP-FILL` markers left, a module without exactly one plugin class, and missing wiring, and joins `pytest` and ruff in the project gate the generated README and the guide list.
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
