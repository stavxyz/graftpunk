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


def test_the_new_member_binds_what_the_check_requires(suite: Path) -> None:
    module = (suite / "tests" / "test_myshop.py").read_text()
    assert 'scrub_site_env = site_env_scrubber("MYSHOP_")' in module
    assert "sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)" in module
    # Only oldshop's conftest lacks the check, and the advice upgrades oldshop alone.
    assert _unbound(suite) == [
        "tests/conftest.py: does not bind FIXTURES_TREE; gp plugin upgrade --plugin oldshop "
        "adds it.",
        "tests/conftest.py: does not bind sanitised_fixtures; gp plugin upgrade --plugin "
        "oldshop adds it.",
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
    """A member written before #370 binds neither name: the check names it, and the
    upgrade it names adds them to its test module and leaves the conftest, and so
    oldshop's sidecar-less fixture, alone."""
    module = suite / "tests" / "test_myshop.py"
    module.write_text("def test_placeholder() -> None:\n    pass\n")
    conftest = (suite / "tests" / "conftest.py").read_bytes()
    assert (
        "tests/test_myshop.py: does not bind sanitised_fixtures; gp plugin upgrade --plugin "
        "myshop adds it."
    ) in _unbound(suite)

    result = runner.invoke(app, ["plugin", "upgrade", "--plugin", "myshop", "--dir", str(suite)])

    assert result.exit_code == 0, result.output
    assert "tests/test_myshop.py: added FIXTURES_TREE" in strip_ansi(result.output)
    assert (suite / "tests" / "conftest.py").read_bytes() == conftest
    assert 'FIXTURES_TREE = Path(__file__).parent / "fixtures" / "myshop"' in module.read_text()
    assert all("test_myshop.py" not in line for line in _unbound(suite))
    run = _pytest(suite)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "1 accepted on declaration" in run.stdout, run.stdout


def test_upgrade_scoped_to_a_plugin_the_project_lacks_refuses(suite: Path) -> None:
    result = runner.invoke(app, ["plugin", "upgrade", "--plugin", "nosuch", "--dir", str(suite)])
    assert result.exit_code == 1
    assert "no plugin named 'nosuch' in this project; its plugins: oldshop, myshop." in (
        strip_ansi(result.output).replace("\n", "")
    )


def test_a_conftest_that_checks_the_whole_tree_serves_every_member(suite: Path) -> None:
    """A suite gp plugin new wrote before #370: the conftest's check walks the whole
    tree, so a member whose test module binds neither name has nothing to add."""
    (suite / "tests" / "fixtures" / "get_orders.json").unlink()
    result = runner.invoke(app, ["plugin", "upgrade", "--plugin", "oldshop", "--dir", str(suite)])
    assert result.exit_code == 0, result.output
    (suite / "tests" / "test_myshop.py").write_text("def test_placeholder() -> None:\n    pass\n")
    assert _unbound(suite) == []
