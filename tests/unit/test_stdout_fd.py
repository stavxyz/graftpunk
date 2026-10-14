"""graftpunk.session imports httpie under a stdout with a file descriptor (#250)."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from graftpunk._stdout_fd import stdout_with_a_file_descriptor

_REPO = Path(__file__).parent.parent.parent


def test_a_stdout_with_a_descriptor_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    with open(os.devnull, "w") as stream:
        monkeypatch.setattr(sys, "stdout", stream)
        with stdout_with_a_file_descriptor():
            assert sys.stdout is stream
        assert sys.stdout is stream


@pytest.mark.parametrize("original_stdout", ["fd-less", "none"])
def test_a_stdout_without_one_is_swapped_and_restored_on_error(
    monkeypatch: pytest.MonkeyPatch, original_stdout: str
) -> None:
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    if original_stdout == "none":
        monkeypatch.setattr(sys, "__stdout__", None)
    else:
        monkeypatch.setattr(sys, "__stdout__", io.StringIO())

    with pytest.raises(RuntimeError, match="inside"), stdout_with_a_file_descriptor():
        assert sys.stdout.fileno() >= 0
        raise RuntimeError("inside")

    assert sys.stdout is captured


_IMPORT_UNDER_STRINGIO = """
import io
import sys

if sys.argv[1] == "none":
    sys.__stdout__ = None
real_stdout = sys.stdout
captured = io.StringIO()
sys.stdout = captured
try:
    import graftpunk.session
    kept = sys.stdout is captured
finally:
    sys.stdout = real_stdout
print("imported", kept)
"""


@pytest.mark.parametrize("original_stdout", ["pipe", "none"])
def test_graftpunk_session_imports_under_a_stringio_stdout(
    tmp_path: Path, original_stdout: str
) -> None:
    """A fresh interpreter, so this import is httpie's first."""
    home = {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "GRAFTPUNK_CONFIG_DIR": str(tmp_path / "gp"),
        "HTTPIE_CONFIG_DIR": str(tmp_path / "httpie"),
    }
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _IMPORT_UNDER_STRINGIO, original_stdout],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
        env={**os.environ, **home, "PYTHONPATH": str(_REPO)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "imported True", result.stdout + result.stderr
