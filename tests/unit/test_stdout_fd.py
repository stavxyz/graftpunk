"""httpie is imported under a stdout with a file descriptor (#250)."""

from __future__ import annotations

import io
import json
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
        assert sys.stdout.name == os.devnull
        raise RuntimeError("inside")

    assert sys.stdout is captured


def test_the_process_stdout_is_the_stand_in_when_it_has_a_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    with open(os.devnull, "w") as process_stdout:
        monkeypatch.setattr(sys, "__stdout__", process_stdout)
        with stdout_with_a_file_descriptor():
            assert sys.stdout is process_stdout
    assert sys.stdout is captured


def test_a_none_stderr_is_swapped_and_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stderr", None)
    with stdout_with_a_file_descriptor():
        assert sys.stderr is not None
        assert sys.stderr.name == os.devnull
    assert sys.stderr is None


_EXPORT_UNDER_STRINGIO = """
import io
import sys
from pathlib import Path
from unittest.mock import MagicMock

import requests

mode, target = sys.argv[1], Path(sys.argv[2])
real_stdout, real_stderr = sys.stdout, sys.stderr
if mode in ("none", "pythonw"):
    sys.__stdout__ = None
if mode == "pythonw":
    sys.__stderr__ = None
    sys.stderr = None
    captured = None
else:
    captured = io.StringIO()
sys.stdout = captured
try:
    from graftpunk.session import BrowserSession

    session = BrowserSession.__new__(BrowserSession)
    session._backend_type = "selenium"
    session._backend_instance = None
    requests.Session.__init__(session)
    session._webdriver = MagicMock()
    session._webdriver.current_url = "https://example.com/"
    session.cookies.set("token", "value")
    session.save_httpie_session("myshop", output=target)
    kept = sys.stdout is captured
finally:
    # Restored so a failure's traceback reaches the test's stderr.
    sys.stdout, sys.stderr = real_stdout, real_stderr
print("exported", kept)
"""


@pytest.mark.parametrize("original_stdout", ["pipe", "none", "pythonw"])
def test_a_session_exports_under_a_stringio_stdout(tmp_path: Path, original_stdout: str) -> None:
    """A fresh interpreter, so importing graftpunk.session makes httpie's first
    import, under the StringIO (or ``None``) stdout.

    ``pipe`` leaves ``sys.__stdout__`` as the subprocess pipe, ``none`` sets it
    to ``None`` so devnull stands in, and ``pythonw`` sets stdout, stderr and
    both originals to ``None``, as under ``pythonw`` on Windows.
    """
    home = {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "GRAFTPUNK_CONFIG_DIR": str(tmp_path / "gp"),
        "HTTPIE_CONFIG_DIR": str(tmp_path / "httpie"),
    }
    target = tmp_path / "myshop.json"
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _EXPORT_UNDER_STRINGIO, original_stdout, str(target)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
        env={**os.environ, **home, "PYTHONPATH": str(_REPO / "src")},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "exported True", result.stdout + result.stderr
    cookies = json.loads(target.read_text())["cookies"]
    assert [(c["name"], c["value"]) for c in cookies] == [("token", "value")]
