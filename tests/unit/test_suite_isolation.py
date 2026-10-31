"""The suite never reads the developer's home: tests/conftest.py points HOME at
a directory under each test's tmp_path, and a subprocess a test starts inherits
it, so a generated project's own pytest looks up observe runs there."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_SRC = Path(__file__).resolve().parents[2] / "src"


def _real_home() -> Path:
    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir)


@pytest.mark.skipif(sys.platform == "win32", reason="the account's home comes from pwd")
def test_a_subprocess_sees_the_tests_own_home_and_observe_directory(tmp_path: Path) -> None:
    probe = (
        "import os, pathlib\n"
        "from graftpunk.observe.context import OBSERVE_BASE_DIR\n"
        "print(pathlib.Path.home())\n"
        "print(OBSERVE_BASE_DIR)\n"
        "print(os.environ['GRAFTPUNK_CONFIG_DIR'])\n"
    )
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [sys.executable, "-c", probe],
        env={**os.environ, "PYTHONPATH": str(_REPO_SRC)},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    home, observe, config = (Path(line) for line in result.stdout.splitlines())
    assert Path.home().is_relative_to(tmp_path)
    assert home == Path.home()
    assert observe == home / ".local" / "share" / "graftpunk" / "observe"
    assert observe != _real_home() / ".local" / "share" / "graftpunk" / "observe"
    assert config.is_relative_to(tmp_path)
