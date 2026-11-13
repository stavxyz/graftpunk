"""A suite's fixtures checks and env scrubbers, per plugin (#370).

The suites here are the ones the issue and its reviews reported: a first plugin,
oldshop, whose conftest predates the fixtures check, joined by members through
``gp plugin new`` in suite mode, some written by an older gp. The shared conftest
checks oldshop's fixtures alone, with every member's directory in
``SUITE_MEMBER_FIXTURES`` and skipped; each member's test modules check its own.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from graftpunk.cli.main import app
from graftpunk.devtools.plugin_check import check_project
from graftpunk.devtools.scaffold.project import write_scaffold
from graftpunk.devtools.scaffold.render import (
    ScaffoldSpec,
    class_name_for,
    graftpunk_version_floor,
)
from graftpunk.testing.sidecar import Sidecar, sidecar_text
from tests.unit.cli_harness import strip_ansi
from tests.unit.test_scaffold_cli import _write_run

runner = CliRunner()

# What an older graftpunk wrote for oldshop: the scrubber, and no fixtures check.
_OLDER_CONFTEST = """\
from graftpunk.testing.plugin import site_env_scrubber

scrub_site_env = site_env_scrubber("OLDSHOP_")
"""
_MYSHOP_FIXTURE = "tests/fixtures/myshop/get_orders_{order_id}.json"
_PLACEHOLDER_MODULE = "def test_placeholder() -> None:\n    pass\n"
_SUITE_CHECK = (
    "sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE, skip=SUITE_MEMBER_FIXTURES)"
)


def _declare(fixture: Path) -> None:
    """The sidecar the fixtures check asks for a hand-made fixture."""
    Path(f"{fixture}.meta.json").write_text(
        sidecar_text(Sidecar(status=200, content_type="application/json"))
    )


def _new(root: Path, name: str, *, mode: str = "new_project") -> None:
    write_scaffold(
        root,
        ScaffoldSpec(
            name=name,
            mode=mode,  # type: ignore[arg-type]
            backend="nodriver",
            base_url=f"https://{name}.example",
        ),
    )


def _older_suite(root: Path, *, declared: bool = True) -> None:
    """oldshop as an older gp left it: a scrubber-only conftest, and a fixture of its
    own, declared by a sidecar unless *declared* is false (the issue's case)."""
    _new(root, "oldshop")
    (root / "tests" / "conftest.py").write_text(_OLDER_CONFTEST)
    fixture = root / "tests" / "fixtures" / "get_orders.json"
    fixture.write_text('{"orders": []}\n')
    if declared:
        _declare(fixture)


def _gp_new(root: Path, *args: str) -> str:
    result = runner.invoke(app, ["plugin", "new", *args, "--dir", str(root)])
    assert result.exit_code == 0, result.output
    output = " ".join(strip_ansi(result.output).split())
    assert "Add To Suite" in output
    return output


def _add_myshop(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """``gp plugin new myshop`` into the suite at *root*, from a run with one
    endpoint, and that endpoint's fixture with a declaring sidecar."""
    observe_base = tmp_path / "observe"
    monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
    _write_run(
        observe_base, "myshop", "run-1", url="https://myshop.example/orders/1001", body='{"id": 1}'
    )
    output = _gp_new(root, "myshop", "--from-run", "myshop")
    fixture = root / _MYSHOP_FIXTURE
    fixture.write_text('{"id": 1}')
    _declare(fixture)
    return output


@pytest.fixture
def suite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """oldshop with its fixture declared, myshop added, and the conftest check over
    oldshop's fixtures added by the upgrade gp names for it."""
    root = tmp_path / "suite"
    _older_suite(root)
    _add_myshop(root, tmp_path, monkeypatch)
    _upgrade(root, "--plugin", "oldshop")
    return root


_UNCHECKED = (
    "tests/conftest.py: oldshop's fixtures are unchecked; gp plugin upgrade --plugin oldshop "
    "adds the check, which fails oldshop's tests on any of its fixtures that lacks a sidecar."
)


def _pytest(root: Path, *paths: str, **env: str) -> str:
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [sys.executable, "-m", "pytest", *(paths or ("tests",)), "-q", "-rA"],
        cwd=root,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": str(root / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            **env,
        },
    )
    return strip_ansi(result.stdout + result.stderr)


def _outcomes(output: str) -> dict[str, str]:
    """Each test id in a ``-rA`` summary and its outcome."""
    return {
        test: outcome
        for outcome, test in re.findall(r"^(PASSED|ERROR|FAILED) (\S+)", output, re.MULTILINE)
    }


def _unbound(root: Path) -> list[str]:
    return [str(f) for f in check_project(root) if "does not bind" in f.message]


def _wiring(root: Path) -> list[str]:
    """Every finding but a GP-FILL marker left: the project's wiring."""
    return [str(f) for f in check_project(root) if "GP-FILL" not in f.message]


def _upgrade(root: Path, *args: str) -> str:
    result = runner.invoke(app, ["plugin", "upgrade", *args, "--dir", str(root)])
    assert result.exit_code == 0, result.output
    return " ".join(strip_ansi(result.output).split())


def _fill_markers(root: Path, *names: str) -> None:
    """The author's part: each plugin module filled in (a class with no command left
    to fill) and each test module's GP-FILL line gone, so ``gp plugin check`` judges
    the wiring alone."""
    for name in names:
        module = name.replace("-", "_")
        (root / "src" / f"graftpunk_{module}" / "plugin.py").write_text(
            "from graftpunk.plugins import SitePlugin\n\n\n"
            f"class {class_name_for(name)}(SitePlugin):\n"
            f'    site_name = "{name}"\n'
            f'    base_url = "https://{name}.example"\n'
        )
    for test_module in (root / "tests").glob("*.py"):
        lines = test_module.read_text().splitlines(keepends=True)
        test_module.write_text("".join(line for line in lines if "GP-FILL" not in line))


def test_gp_plugin_new_adds_the_member_to_the_conftest_check(suite: Path) -> None:
    conftest = (suite / "tests" / "conftest.py").read_text()
    assert 'SUITE_MEMBER_FIXTURES = ("myshop",)' in conftest
    assert _SUITE_CHECK in conftest
    module = (suite / "tests" / "test_myshop.py").read_text()
    assert 'scrub_site_env = site_env_scrubber("MYSHOP_")' in module
    assert "sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)" in module
    assert _wiring(suite) == []


def test_gp_plugin_new_raises_the_floor_when_it_scopes_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first plugin gp wrote has the standalone check: adding a member scopes it,
    which needs skip=, so the floor goes up."""
    root = tmp_path / "suite"
    _new(root, "oldshop")
    pyproject = root / "pyproject.toml"
    pyproject.write_text(
        re.sub(
            r"graftpunk\[browser\]>=[0-9.]+", "graftpunk[browser]>=1.30.0", pyproject.read_text()
        )
    )
    output = _add_myshop(root, tmp_path, monkeypatch)
    assert "tests/conftest.py: added SUITE_MEMBER_FIXTURES" in output
    assert "tests/conftest.py: sanitised_fixtures: skips SUITE_MEMBER_FIXTURES" in output
    floor = graftpunk_version_floor()
    assert f"graftpunk[browser]>={floor}" in pyproject.read_text()
    assert f"pyproject.toml: graftpunk>={floor} (was >=1.30.0)" in output
    assert "unchecked" not in output


def test_adding_or_upgrading_a_member_never_turns_on_the_first_plugins_check(
    tmp_path: Path,
) -> None:
    """The reviewer's oldshop suite (#370's): a scrubber-only conftest and a fixture
    with no sidecar. gp plugin new myshop and upgrade --plugin myshop leave oldshop's
    tests passing and say its fixtures are unchecked; upgrade --plugin oldshop adds
    the check, and the failure names the sidecar fix."""
    root = tmp_path / "suite"
    _older_suite(root, declared=False)
    conftest = root / "tests" / "conftest.py"

    output = _gp_new(root, "myshop", "--url", "https://myshop.example")

    assert _UNCHECKED in output
    assert conftest.read_text() == _OLDER_CONFTEST
    assert set(_outcomes(_pytest(root)).values()) == {"PASSED"}
    assert _unbound(root) == [
        f"tests/conftest.py: does not bind {name}; gp plugin upgrade --plugin oldshop adds it."
        for name in ("FIXTURES_TREE", "SUITE_MEMBER_FIXTURES", "sanitised_fixtures")
    ]
    (root / "tests" / "test_myshop.py").write_text(_PLACEHOLDER_MODULE)

    output = _upgrade(root, "--plugin", "myshop")

    assert "tests/test_myshop.py: added sanitised_fixtures" in output
    assert _UNCHECKED in output
    assert conftest.read_text() == _OLDER_CONFTEST
    assert set(_outcomes(_pytest(root)).values()) == {"PASSED"}

    output = _upgrade(root, "--plugin", "oldshop")

    assert "tests/conftest.py: added sanitised_fixtures" in output
    assert "unchecked" not in output
    assert 'SUITE_MEMBER_FIXTURES = ("myshop",)' in conftest.read_text()
    run = _pytest(root)
    assert _outcomes(run)["tests/test_plugin.py::test_plugin_instantiates"] == "ERROR", run
    assert "write get_orders.json.meta.json" in " ".join(run.split())


def test_the_issues_suite_passes_after_gps_advice_and_checks_every_plugin(
    tmp_path: Path,
) -> None:
    """#370's suite, following each piece of gp's advice: the upgrade the check names
    for oldshop, then the sidecar its fixtures check names. gp plugin check then has
    nothing on the wiring, pytest passes, and oldshop's fixtures are checked."""
    root = tmp_path / "suite"
    _older_suite(root, declared=False)
    _gp_new(root, "myshop", "--url", "https://myshop.example")
    _upgrade(root, "--plugin", "oldshop")

    first = _pytest(root)
    assert _outcomes(first) == {
        "tests/test_myshop.py::test_plugin_instantiates": "PASSED",
        "tests/test_plugin.py::test_plugin_instantiates": "ERROR",
    }, first
    assert "get_orders.json: no sidecar" in first

    _declare(root / "tests" / "fixtures" / "get_orders.json")
    _fill_markers(root, "oldshop", "myshop")
    assert [str(f) for f in check_project(root)] == []
    final = _pytest(root)
    assert set(_outcomes(final).values()) == {"PASSED"}, final
    assert "1 accepted on declaration" in final


def test_a_bad_member_fixture_fails_that_member_alone(suite: Path) -> None:
    Path(f"{suite / _MYSHOP_FIXTURE}.meta.json").unlink()
    output = _pytest(suite)
    outcomes = _outcomes(output)
    assert outcomes["tests/test_plugin.py::test_plugin_instantiates"] == "PASSED", output
    assert outcomes["tests/test_myshop.py::test_plugin_instantiates"] == "ERROR", output


def test_the_new_members_scrubber_serves_its_tests(suite: Path) -> None:
    module = suite / "tests" / "test_myshop.py"
    module.write_text(
        module.read_text()
        + "\n\ndef test_no_site_env() -> None:\n"
        + '    import os\n\n    assert "MYSHOP_USERNAME" not in os.environ\n'
    )
    output = _pytest(suite, MYSHOP_USERNAME="from-the-shell")
    assert _outcomes(output)["tests/test_myshop.py::test_no_site_env"] == "PASSED", output


def test_the_reviewers_suite_passes_after_every_piece_of_advice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review probe A: oldshop first, midshop a member an older gp wrote (no
    bindings, a sidecar-less fixture), then myshop. Every upgrade gp names leaves
    each plugin failing only on its own fixture; the fixes the messages name then
    make gp plugin check and pytest pass."""
    root = tmp_path / "suite"
    _older_suite(root, declared=False)
    _new(root, "midshop", mode="add_to_suite")
    (root / "tests" / "test_midshop.py").write_text(_PLACEHOLDER_MODULE)
    midshop_fixture = root / "tests" / "fixtures" / "midshop" / "get_x.json"
    midshop_fixture.write_text('{"a": 1}\n')
    _add_myshop(root, tmp_path, monkeypatch)
    assert sorted(_unbound(root)) == sorted(
        [
            *(
                f"tests/conftest.py: does not bind {name}; gp plugin upgrade --plugin oldshop "
                "adds it."
                for name in ("FIXTURES_TREE", "SUITE_MEMBER_FIXTURES", "sanitised_fixtures")
            ),
            *(
                f"tests/test_midshop.py: does not bind {name}; gp plugin upgrade --plugin "
                "midshop adds it."
                for name in ("FIXTURES_TREE", "sanitised_fixtures", "scrub_site_env")
            ),
        ]
    )

    output = _upgrade(root, "--plugin", "midshop")
    assert "tests/test_midshop.py: added scrub_site_env" in output
    _upgrade(root, "--plugin", "oldshop")
    assert (
        'SUITE_MEMBER_FIXTURES = ("midshop", "myshop")'
        in (root / "tests" / "conftest.py").read_text()
    )
    assert _wiring(root) == []
    run = _pytest(root)
    assert _outcomes(run) == {
        "tests/test_midshop.py::test_placeholder": "ERROR",
        "tests/test_myshop.py::test_orders_by_order_id": "PASSED",
        "tests/test_myshop.py::test_plugin_instantiates": "PASSED",
        "tests/test_plugin.py::test_plugin_instantiates": "ERROR",
    }, run
    assert "get_x.json: no sidecar" in run
    assert "get_orders.json: no sidecar" in run

    _declare(midshop_fixture)
    _declare(root / "tests" / "fixtures" / "get_orders.json")
    _fill_markers(root, "oldshop", "midshop")
    assert _wiring(root) == []
    final = _pytest(root)
    assert set(_outcomes(final).values()) == {"PASSED"}, final


def test_a_suite_conftest_that_checks_the_whole_tree_is_scoped(tmp_path: Path) -> None:
    """Two gp plugin new runs before 1.41.0 left the standalone check, which walks
    every member's directory: the check names it, and the upgrade it names scopes it."""
    root = tmp_path / "suite"
    _new(root, "oldshop")
    _new(root, "myshop", mode="add_to_suite")
    conftest = root / "tests" / "conftest.py"
    standalone = (
        conftest.read_text()
        .replace('SUITE_MEMBER_FIXTURES = ("myshop",)\n', "")
        .replace(_SUITE_CHECK, "sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)")
    )
    conftest.write_text(standalone)
    (root / "tests" / "fixtures" / "myshop" / "get_x.json").write_text("{}")
    assert _wiring(root) == [
        "tests/conftest.py: sanitised_fixtures also checks every member's fixtures; gp "
        "plugin upgrade --plugin oldshop scopes it to oldshop's with skip=SUITE_MEMBER_FIXTURES."
    ]
    before = _outcomes(_pytest(root))
    assert before["tests/test_plugin.py::test_plugin_instantiates"] == "ERROR"

    output = _upgrade(root, "--plugin", "oldshop")

    assert "tests/conftest.py: sanitised_fixtures: skips SUITE_MEMBER_FIXTURES" in output
    assert _wiring(root) == []
    after = _outcomes(_pytest(root))
    assert after["tests/test_plugin.py::test_plugin_instantiates"] == "PASSED"
    assert after["tests/test_myshop.py::test_plugin_instantiates"] == "ERROR"


def test_a_member_the_tuple_does_not_list_is_reported_and_added(suite: Path) -> None:
    conftest = suite / "tests" / "conftest.py"
    conftest.write_text(conftest.read_text().replace('("myshop",)', '("othershop",)'))
    assert _wiring(suite) == [
        "tests/conftest.py: SUITE_MEMBER_FIXTURES does not list myshop; gp plugin upgrade "
        "--plugin myshop adds it."
    ]
    output = _upgrade(suite, "--plugin", "myshop")
    assert "tests/conftest.py: SUITE_MEMBER_FIXTURES: added myshop" in output
    assert 'SUITE_MEMBER_FIXTURES = ("othershop", "myshop")' in conftest.read_text()
    assert _wiring(suite) == []


def test_upgrade_scoped_to_a_member_touches_only_its_files(suite: Path) -> None:
    """A member written before #370 binds none of the names: the check names it, and
    the upgrade it names adds them to its test module and leaves the conftest alone."""
    module = suite / "tests" / "test_myshop.py"
    module.write_text(_PLACEHOLDER_MODULE)
    conftest = (suite / "tests" / "conftest.py").read_bytes()
    assert (
        "tests/test_myshop.py: does not bind sanitised_fixtures; gp plugin upgrade --plugin "
        "myshop adds it."
    ) in _unbound(suite)

    output = _upgrade(suite, "--plugin", "myshop")

    assert "tests/test_myshop.py: added scrub_site_env" in output
    assert "tests/test_myshop.py: added FIXTURES_TREE" in output
    assert "other plugins" not in output
    assert (suite / "tests" / "conftest.py").read_bytes() == conftest
    assert 'FIXTURES_TREE = Path(__file__).parent / "fixtures" / "myshop"' in module.read_text()
    assert _wiring(suite) == []
    assert "1 accepted on declaration" in _pytest(suite)
    again = _upgrade(suite, "--plugin", "myshop")
    assert "Nothing to upgrade: myshop already has every requirement." in again


def test_the_other_plugins_line_counts_what_an_upgrade_adds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "suite"
    _older_suite(root)
    _new(root, "midshop", mode="add_to_suite")
    (root / "tests" / "test_midshop.py").write_text(_PLACEHOLDER_MODULE)
    _add_myshop(root, tmp_path, monkeypatch)
    (root / "tests" / "test_myshop.py").write_text(_PLACEHOLDER_MODULE)

    output = _upgrade(root, "--plugin", "myshop")

    assert (
        "3 requirement(s) or fixtures root(s) of other plugins are still missing; gp plugin "
        "upgrade adds them all."
    ) in output
    assert "other plugins" not in _upgrade(root)
    assert _wiring(root) == []


def test_upgrade_scoped_to_a_plugin_the_project_lacks_refuses(suite: Path) -> None:
    result = runner.invoke(app, ["plugin", "upgrade", "--plugin", "nosuch", "--dir", str(suite)])
    assert result.exit_code == 1
    assert "no plugin named 'nosuch' in this project; its plugins: oldshop, myshop." in (
        strip_ansi(result.output).replace("\n", "")
    )


def test_every_test_file_of_a_member_is_scrubbed_and_checked(suite: Path) -> None:
    """The second-file probe: tests/test_myshop_orders.py is myshop's, so the check
    holds it to the three names and the upgrade it names binds them; then a bad
    myshop fixture fails it too, run on its own."""
    extra = suite / "tests" / "test_myshop_orders.py"
    extra.write_text(
        'import os\n\n\ndef test_env() -> None:\n    assert "MYSHOP_USERNAME" not in os.environ\n'
    )
    assert (
        "tests/test_myshop_orders.py: does not bind scrub_site_env; gp plugin upgrade "
        "--plugin myshop adds it."
    ) in _unbound(suite)

    _upgrade(suite, "--plugin", "myshop")

    assert _wiring(suite) == []
    output = _pytest(suite, MYSHOP_USERNAME="from-the-shell")
    assert _outcomes(output)["tests/test_myshop_orders.py::test_env"] == "PASSED", output
    Path(f"{suite / _MYSHOP_FIXTURE}.meta.json").unlink()
    alone = _pytest(suite, "tests/test_myshop_orders.py")
    assert _outcomes(alone) == {"tests/test_myshop_orders.py::test_env": "ERROR"}, alone


@pytest.mark.parametrize(
    ("first", "member", "first_file"),
    [
        ("shop-eu", "shop", "test_shop_eu_orders.py"),
        ("shop-pro", "shop", "test_shop_pro_extra.py"),
    ],
)
def test_a_first_plugins_test_file_is_never_a_members(
    tmp_path: Path, first: str, member: str, first_file: str
) -> None:
    """The prefix probes: the first plugin's module name wins its own files, so a
    member whose name is a prefix of it claims none of them."""
    root = tmp_path / "suite"
    _new(root, first)
    _new(root, member, mode="add_to_suite")
    owned = root / "tests" / first_file
    owned.write_text(_PLACEHOLDER_MODULE)
    member_file = root / "tests" / f"test_{member}_x.py"
    member_file.write_text(_PLACEHOLDER_MODULE)
    assert all(first_file not in finding for finding in _wiring(root))
    assert f"tests/test_{member}_x.py: does not bind scrub_site_env" in " ".join(_wiring(root))

    _upgrade(root, "--plugin", member)

    assert owned.read_text() == _PLACEHOLDER_MODULE
    assert f'site_env_scrubber("{member.upper()}_")' in member_file.read_text()


def test_a_member_module_the_upgrade_creates_runs_the_members_check(tmp_path: Path) -> None:
    """A member with no test module of its own: the upgrade creates one holding a test
    that requests the check, so a bad member fixture fails it."""
    root = tmp_path / "suite"
    _older_suite(root)
    _new(root, "midshop", mode="add_to_suite")
    (root / "tests" / "test_midshop.py").unlink()
    (root / "tests" / "fixtures" / "midshop" / "get_x.json").write_text("{}")

    output = _upgrade(root, "--plugin", "midshop")

    assert "tests/test_midshop.py: added sanitised_fixtures" in output
    run = _pytest(root)
    assert _outcomes(run)["tests/test_midshop.py::test_fixtures_are_sanitised"] == "ERROR", run
    assert "get_x.json: no sidecar" in run


def test_a_missing_member_fixtures_directory_is_reported_and_created(suite: Path) -> None:
    root = suite / "tests" / "fixtures" / "myshop"
    for path in root.iterdir():
        path.unlink()
    root.rmdir()
    assert _wiring(suite) == [
        "tests/fixtures/myshop/: missing; gp plugin upgrade --plugin myshop creates it."
    ]
    run = _pytest(suite)
    # The pytest message names the command that creates this tree.
    assert "Run gp plugin upgrade --plugin myshop, or recreate the directory" in " ".join(
        run.split()
    ), run

    output = _upgrade(suite, "--plugin", "myshop")

    assert "tests/fixtures/myshop/: created" in output
    assert (root / ".gitkeep").is_file()
    assert _wiring(suite) == []
    assert _outcomes(_pytest(suite))["tests/test_myshop.py::test_plugin_instantiates"] == "PASSED"


def test_a_single_plugin_project_keeps_the_whole_tree_check(tmp_path: Path) -> None:
    root = tmp_path / "solo"
    _new(root, "myshop")
    conftest = root / "tests" / "conftest.py"
    assert "sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)\n" in conftest.read_text()
    assert "SUITE_MEMBER_FIXTURES" not in conftest.read_text()
    conftest.write_text(_OLDER_CONFTEST.replace("OLDSHOP_", "MYSHOP_"))
    _upgrade(root)
    assert "sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)\n" in conftest.read_text()
    assert "SUITE_MEMBER_FIXTURES" not in conftest.read_text()


def test_a_tuple_that_is_not_a_literal_is_reported(suite: Path) -> None:
    """A SUITE_MEMBER_FIXTURES gp cannot read could skip less than every member; the
    check says so, and skip itself refuses a string."""
    conftest = suite / "tests" / "conftest.py"
    conftest.write_text(conftest.read_text().replace('("myshop",)', '"myshop"'))
    assert _wiring(suite) == [
        "tests/conftest.py: SUITE_MEMBER_FIXTURES is not a literal tuple, so gp cannot list "
        'members in it; write it as a literal tuple of directory names, such as ("myshop",).'
    ]
    assert "TypeError" in _pytest(suite)


@pytest.mark.parametrize("entry", ['"myshop/x"', '""'])
def test_a_tuple_entry_skip_refuses_is_reported_and_never_appended_to(
    suite: Path, entry: str
) -> None:
    """An entry skip refuses fails the conftest's import. The check names it; an
    upgrade leaves the tuple as it is and says why; following the fix the finding
    names gives a suite that imports and passes."""
    conftest = suite / "tests" / "conftest.py"
    conftest.write_text(conftest.read_text().replace('("myshop",)', f"({entry},)"))
    finding = (
        f"tests/conftest.py: SUITE_MEMBER_FIXTURES holds {entry.replace(chr(34), chr(39))}, "
        "which skip refuses, since it is not one directory name; write it as a literal "
        'tuple of directory names, such as ("myshop",).'
    )
    assert _wiring(suite) == [finding]
    assert "ValueError" in _pytest(suite)
    before = conftest.read_text()

    output = _upgrade(suite, "--plugin", "myshop")

    assert conftest.read_text() == before
    assert finding in output
    assert "Nothing upgraded: tests/conftest.py needs the edit below first." in output
    assert "every requirement" not in output

    conftest.write_text(before.replace(f"({entry},)", '("myshop",)'))
    assert _wiring(suite) == []
    assert set(_outcomes(_pytest(suite)).values()) == {"PASSED"}


def test_gp_plugin_new_says_when_it_cannot_list_the_new_member(
    suite: Path,
) -> None:
    conftest = suite / "tests" / "conftest.py"
    conftest.write_text(conftest.read_text().replace('("myshop",)', 'tuple(["myshop"])'))

    output = _gp_new(suite, "thirdshop", "--url", "https://thirdshop.example")

    assert (
        "tests/conftest.py: SUITE_MEMBER_FIXTURES is not a literal tuple, so gp cannot list "
        "members in it; write it as a literal tuple of directory names, such as "
        '("myshop", "thirdshop").'
    ) in output
    assert 'tuple(["myshop"])' in conftest.read_text()
