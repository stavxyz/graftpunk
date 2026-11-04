"""Test files that once leaked state into a later file in the same process.

The xdist gate spreads files over workers, so a leak from one file into
another passes there unless the two land on one worker. Each test here runs a
leaking file and its victim in one pytest process, in the order that failed.
"""

import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def _run_in_one_process(tmp_path: Path, *node_ids: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-n0",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            f"--basetemp={tmp_path / 'basetemp'}",
            *node_ids,
        ],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    )


def test_observe_start_hardening_passes_after_the_headless_observe_tests(tmp_path: Path) -> None:
    """test_login_headless_observe.py's capture mock was bound into
    graftpunk.session by a first import inside its patch, and three
    TestObserveStartHardening tests then recorded nothing (#352)."""
    result = _run_in_one_process(
        tmp_path,
        "tests/unit/test_login_headless_observe.py",
        "tests/unit/test_session.py::TestObserveStartHardening",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    summary = result.stdout.strip().splitlines()[-1]
    assert " passed" in summary, result.stdout
    assert "failed" not in summary, result.stdout
