"""A suite member binds its own env scrubber and fixtures check (#370).

The suite here is the one the issue reported: an older member, oldshop, whose
conftest predates the fixtures check and whose fixture has no sidecar, joined by
a new member, myshop, through ``gp plugin new`` in suite mode.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from graftpunk.cli.main import app
from graftpunk.devtools.plugin_check import check_project
from graftpunk.devtools.scaffold.project import write_scaffold
from graftpunk.devtools.scaffold.render import ScaffoldSpec
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


def _older_suite(root: Path) -> None:
    write_scaffold(
        root,
        ScaffoldSpec(
            name="oldshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://oldshop.example",
        ),
    )
    (root / "tests" / "conftest.py").write_text(_OLDER_CONFTEST)
    (root / "tests" / "fixtures" / "get_orders.json").write_text('{"orders": []}\n')


def _add_myshop(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``gp plugin new myshop`` into the suite at *root*, from a run with one
    endpoint, and that endpoint's fixture with a declaring sidecar."""
    observe_base = tmp_path / "observe"
    monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
    _write_run(
        observe_base, "myshop", "run-1", url="https://myshop.example/orders/1001", body='{"id": 1}'
    )
    result = runner.invoke(
        app, ["plugin", "new", "myshop", "--from-run", "myshop", "--dir", str(root)]
    )
    assert result.exit_code == 0, result.output
    assert "Add To Suite" in strip_ansi(result.output)
    fixture = root / _MYSHOP_FIXTURE
    fixture.write_text('{"id": 1}')
    Path(f"{fixture}.meta.json").write_text(
        sidecar_text(Sidecar(status=200, content_type="application/json"))
    )


@pytest.fixture
def suite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "suite"
    _older_suite(root)
    _add_myshop(root, tmp_path, monkeypatch)
    return root


def _pytest(root: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [sys.executable, "-m", "pytest", "tests", "-q", "-rA", "-p", "no:cacheprovider"],
        cwd=root,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(root / "src"), **env},
    )


def _unbound(root: Path) -> list[str]:
    return [str(f) for f in check_project(root) if "does not bind" in f.message]


_OWNER_ADVICE = (
    "in a suite gp plugin upgrade adds no fixtures check over the whole tree, which would "
    "fail every plugin's tests on one plugin's fixture."
)
_PLACEHOLDER_MODULE = "def test_placeholder() -> None:\n    pass\n"


def _upgrade(root: Path, *args: str) -> str:
    result = runner.invoke(app, ["plugin", "upgrade", *args, "--dir", str(root)])
    assert result.exit_code == 0, result.output
    return " ".join(strip_ansi(result.output).split())


def _check_output(root: Path) -> str:
    result = runner.invoke(app, ["plugin", "check", "--dir", str(root)])
    return " ".join(strip_ansi(result.output).split())


def test_the_new_member_binds_what_the_check_requires(suite: Path) -> None:
    module = (suite / "tests" / "test_myshop.py").read_text()
    assert 'scrub_site_env = site_env_scrubber("MYSHOP_")' in module
    assert "sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)" in module
    # Only oldshop's conftest lacks the check, and no upgrade is offered for it.
    assert _unbound(suite) == [
        f"tests/conftest.py: does not bind FIXTURES_TREE; {_OWNER_ADVICE}",
        f"tests/conftest.py: does not bind sanitised_fixtures; {_OWNER_ADVICE}",
    ]


def test_the_suite_passes_its_gate_and_checks_the_new_members_fixtures(suite: Path) -> None:
    result = _pytest(suite)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 accepted on declaration" in result.stdout, result.stdout
    assert "get_orders.json: no sidecar" not in result.stdout
    for argv in (["check"], ["format", "--check"]):
        ruff = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [sys.executable, "-m", "ruff", *argv, str(suite)], capture_output=True, text=True
        )
        assert ruff.returncode == 0, ruff.stdout + ruff.stderr


def test_the_new_members_scrubber_serves_its_tests(suite: Path) -> None:
    module = suite / "tests" / "test_myshop.py"
    module.write_text(
        module.read_text()
        + "\n\ndef test_no_site_env() -> None:\n"
        + '    import os\n\n    assert "MYSHOP_USERNAME" not in os.environ\n'
    )
    result = _pytest(suite, MYSHOP_USERNAME="from-the-shell")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASSED tests/test_myshop.py::test_no_site_env" in result.stdout


def test_a_new_members_fixture_problem_fails_only_its_own_tests(suite: Path) -> None:
    Path(f"{suite / _MYSHOP_FIXTURE}.meta.json").unlink()
    result = _pytest(suite)
    assert "PASSED tests/test_plugin.py::test_plugin_instantiates" in result.stdout, result.stdout
    assert "ERROR tests/test_myshop.py::test_plugin_instantiates" in result.stdout, result.stdout


def test_upgrade_scoped_to_a_member_touches_only_its_test_module(suite: Path) -> None:
    """A member written before #370 binds none of the names: the check names it, and
    the upgrade it names adds them to its test module and leaves the conftest, and so
    oldshop's sidecar-less fixture, alone."""
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
    assert (
        "2 requirement(s) or fixtures root(s) of other plugins are still missing; gp plugin "
        "check names the gp plugin upgrade --plugin <name> for each."
    ) in output
    assert (suite / "tests" / "conftest.py").read_bytes() == conftest
    assert 'FIXTURES_TREE = Path(__file__).parent / "fixtures" / "myshop"' in module.read_text()
    assert all("test_myshop.py" not in line for line in _unbound(suite))
    run = _pytest(suite)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "1 accepted on declaration" in run.stdout, run.stdout
    again = _upgrade(suite, "--plugin", "myshop")
    assert "Nothing to upgrade: myshop already has every requirement." in again


def test_upgrade_scoped_to_a_plugin_the_project_lacks_refuses(suite: Path) -> None:
    result = runner.invoke(app, ["plugin", "upgrade", "--plugin", "nosuch", "--dir", str(suite)])
    assert result.exit_code == 1
    assert "no plugin named 'nosuch' in this project; its plugins: oldshop, myshop." in (
        strip_ansi(result.output).replace("\n", "")
    )


def test_following_the_advice_never_fails_a_plugin_on_another_plugins_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's suite: oldshop first, with a scrubber-only conftest and a
    sidecar-less fixture; midshop, a member an older gp wrote, with none of the names
    and a sidecar-less fixture of its own; then myshop. Every upgrade the check
    names, and a bare upgrade, leave oldshop's tests passing, never add a check over
    the whole tree, and leave oldshop's findings reported."""
    root = tmp_path / "suite"
    _older_suite(root)
    write_scaffold(
        root,
        ScaffoldSpec(
            name="midshop",
            mode="add_to_suite",
            backend="nodriver",
            base_url="https://midshop.example",
        ),
    )
    (root / "tests" / "test_midshop.py").write_text(_PLACEHOLDER_MODULE)
    (root / "tests" / "fixtures" / "midshop" / "get_x.json").write_text('{"a": 1}\n')
    _add_myshop(root, tmp_path, monkeypatch)
    conftest = (root / "tests" / "conftest.py").read_bytes()
    assert sorted(_unbound(root)) == sorted(
        [
            f"tests/conftest.py: does not bind FIXTURES_TREE; {_OWNER_ADVICE}",
            f"tests/conftest.py: does not bind sanitised_fixtures; {_OWNER_ADVICE}",
            *(
                f"tests/test_midshop.py: does not bind {name}; gp plugin upgrade --plugin "
                "midshop adds it."
                for name in ("scrub_site_env", "FIXTURES_TREE", "sanitised_fixtures")
            ),
        ]
    )

    output = _upgrade(root, "--plugin", "oldshop")
    assert (
        "2 requirement(s) left as they are: in a suite gp plugin upgrade adds no fixtures "
        "check over the whole tree; gp plugin check says which."
    ) in output
    _upgrade(root, "--plugin", "midshop")
    _upgrade(root)

    assert (root / "tests" / "conftest.py").read_bytes() == conftest
    assert _unbound(root) == [
        f"tests/conftest.py: does not bind FIXTURES_TREE; {_OWNER_ADVICE}",
        f"tests/conftest.py: does not bind sanitised_fixtures; {_OWNER_ADVICE}",
    ]
    run = _pytest(root)
    assert "PASSED tests/test_plugin.py::test_plugin_instantiates" in run.stdout, run.stdout
    assert "PASSED tests/test_myshop.py::test_plugin_instantiates" in run.stdout, run.stdout
    # midshop's own fixture fails midshop alone.
    assert "ERROR tests/test_midshop.py::test_placeholder" in run.stdout, run.stdout
    assert "get_x.json: no sidecar" in run.stdout


def test_a_whole_tree_conftest_is_left_alone_and_noted_once(suite: Path) -> None:
    """A conftest that already checks the whole tree is the author's: no finding, no
    upgrade touches it, and the check notes once what it does to a suite. A member
    is still held to its own names."""
    (suite / "tests" / "fixtures" / "get_orders.json").unlink()
    conftest = suite / "tests" / "conftest.py"
    conftest.write_text(
        _OLDER_CONFTEST.replace(
            "import site_env_scrubber", "import fixtures_are_sanitised, site_env_scrubber"
        )
        + 'FIXTURES_TREE = Path(__file__).parent / "fixtures"\n'
        + "sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)\n"
    )
    conftest.write_text("from pathlib import Path\n\n" + conftest.read_text())
    (suite / "tests" / "test_myshop.py").write_text(_PLACEHOLDER_MODULE)
    before = conftest.read_bytes()

    output = _check_output(suite)

    note = (
        "note: tests/conftest.py checks the whole fixtures tree, so one plugin's bad fixture "
        "fails every plugin's tests in this suite."
    )
    assert output.count(note) == 1
    assert "tests/conftest.py: does not bind" not in output
    assert "tests/test_myshop.py: does not bind scrub_site_env" in output
    _upgrade(suite)
    assert conftest.read_bytes() == before
    assert "tests/test_myshop.py: does not bind" not in _check_output(suite)


def test_every_test_file_of_a_member_is_scrubbed_and_checked(suite: Path) -> None:
    """The reviewer's second-file probe: tests/test_myshop_orders.py is myshop's, so
    the check holds it to the three names and the upgrade it names binds them; then
    a bad myshop fixture fails it too, with test_myshop.py deselected."""
    extra = suite / "tests" / "test_myshop_orders.py"
    extra.write_text(
        'import os\n\n\ndef test_env() -> None:\n    assert "MYSHOP_USERNAME" not in os.environ\n'
    )
    assert (
        "tests/test_myshop_orders.py: does not bind scrub_site_env; gp plugin upgrade "
        "--plugin myshop adds it."
    ) in _unbound(suite)

    _upgrade(suite, "--plugin", "myshop")

    assert all("test_myshop_orders.py" not in line for line in _unbound(suite))
    run = _pytest(suite, MYSHOP_USERNAME="from-the-shell")
    assert "PASSED tests/test_myshop_orders.py::test_env" in run.stdout, run.stdout
    Path(f"{suite / _MYSHOP_FIXTURE}.meta.json").unlink()
    only = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [sys.executable, "-m", "pytest", "tests/test_myshop_orders.py", "-q", "-rA"],
        cwd=suite,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(suite / "src")},
    )
    assert "ERROR tests/test_myshop_orders.py::test_env" in only.stdout, only.stdout


def test_a_missing_member_fixtures_directory_is_reported_and_created(suite: Path) -> None:
    root = suite / "tests" / "fixtures" / "myshop"
    for path in root.iterdir():
        path.unlink()
    root.rmdir()
    assert ("tests/fixtures/myshop/: missing; gp plugin upgrade --plugin myshop creates it.") in [
        str(f) for f in check_project(suite)
    ]
    run = _pytest(suite)
    # The pytest message names the command that creates this tree.
    assert "Run gp plugin upgrade --plugin myshop, or recreate the directory" in " ".join(
        run.stdout.split()
    ), run.stdout

    output = _upgrade(suite, "--plugin", "myshop")

    assert "tests/fixtures/myshop/: created" in output
    assert (root / ".gitkeep").is_file()
    assert all("tests/fixtures/myshop" not in str(f) for f in check_project(suite))
    assert "PASSED tests/test_myshop.py::test_plugin_instantiates" in _pytest(suite).stdout
