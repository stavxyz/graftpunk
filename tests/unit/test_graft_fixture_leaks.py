"""The graft skill's fixture-leaks script: every captured value still in a fixture is
reported, so the skill can rewrite it before anything is committed."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.guide_harness import REPO_ROOT

SCRIPT = REPO_ROOT / "skills" / "graft" / "scripts" / "fixture-leaks.py"
PYTHON = shutil.which("python3")

_CAPTURE = {
    "orders": [
        {
            "id": "ORD-4471023",
            "status": "shipped",
            "total": 12345,
            "customer": {"name": "Alice Example", "email": "alice@example.com"},
            "invoice": "/orders/4471023/invoice",
            "gift": False,
        }
    ],
    "page": 1,
}


def _run(tmp_path: Path, capture: str, fixture: str, sidecar: dict | None = None):
    (tmp_path / "capture.json").write_text(capture)
    (tmp_path / "fixture.json").write_text(fixture)
    args = [str(tmp_path / "capture.json"), str(tmp_path / "fixture.json")]
    if sidecar is not None:
        (tmp_path / "fixture.json.meta.json").write_text(json.dumps(sidecar))
        args.append(str(tmp_path / "fixture.json.meta.json"))
    assert PYTHON is not None
    return subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [PYTHON, "-I", str(SCRIPT), *args], capture_output=True, text=True
    )


def test_an_unchanged_copy_reports_every_value(tmp_path: Path) -> None:
    text = json.dumps(_CAPTURE)
    result = _run(tmp_path, text, text)
    assert result.returncode == 1
    for value in ("ORD-4471023", "shipped", "12345", "Alice Example", "alice@example.com"):
        assert repr(value) in result.stdout, value


def test_an_id_inside_another_string_is_still_caught(tmp_path: Path) -> None:
    """Rewriting the order id field is not enough when the same number survives in
    a URL; a substring search finds it where a whole-value comparison would not."""
    fixture = json.dumps(
        {
            "orders": [
                {
                    "id": "ORD-9000001",
                    "status": "shipped",
                    "total": 999,
                    "customer": {"name": "Pat Sample", "email": "pat@example.net"},
                    "invoice": "/orders/4471023/invoice",
                    "gift": False,
                }
            ],
            "page": 1,
        }
    )
    result = _run(tmp_path, json.dumps(_CAPTURE), fixture)
    assert result.returncode == 1
    assert "'4471023'" in result.stdout
    assert "'Alice Example'" not in result.stdout


def test_a_fully_invented_fixture_reports_only_what_was_kept_on_purpose(tmp_path: Path) -> None:
    """A status the command branches on may stay; it is reported for the skill to
    judge, and nothing from the account is."""
    fixture = json.dumps(
        {
            "orders": [
                {
                    "id": "ORD-9000001",
                    "status": "shipped",
                    "total": 999,
                    "customer": {"name": "Pat Sample", "email": "pat@example.net"},
                    "invoice": "/orders/9000001/invoice",
                    "gift": False,
                }
            ],
            "page": 1,
        }
    )
    result = _run(tmp_path, json.dumps(_CAPTURE), fixture)
    assert result.returncode == 1
    lines = [line for line in result.stdout.splitlines() if line.startswith("still in")]
    assert lines == ["still in the fixture: 'shipped'"]


def test_nothing_left_exits_0(tmp_path: Path) -> None:
    result = _run(tmp_path, json.dumps({"name": "Alice Example"}), json.dumps({"name": "Pat"}))
    assert result.returncode == 0
    assert "no captured value is left" in result.stdout


def test_an_html_capture_checks_text_and_attribute_values(tmp_path: Path) -> None:
    capture = '<ul class="orders"><li data-id="4471023">Alice Example</li></ul>'
    fixture = '<ul class="orders"><li data-id="9000001">Alice Example</li></ul>'
    result = _run(tmp_path, capture, fixture)
    assert result.returncode == 1
    assert "'Alice Example'" in result.stdout
    assert "'4471023'" not in result.stdout


def test_the_sidecar_lists_are_printed_for_review(tmp_path: Path) -> None:
    sidecar = {"body_params": ["page"], "flagged_names": ["sessionId"]}
    result = _run(tmp_path, json.dumps({"a": "Alice Example"}), json.dumps({"a": "x"}), sidecar)
    assert "sidecar body_params: page" in result.stdout
    assert "sidecar flagged_names: sessionId" in result.stdout


@pytest.mark.parametrize("argc", [0, 1, 4])
def test_a_wrong_argument_count_exits_2(tmp_path: Path, argc: int) -> None:
    assert PYTHON is not None
    args = [str(tmp_path / f"f{i}") for i in range(argc)]
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [PYTHON, "-I", str(SCRIPT), *args], capture_output=True, text=True
    )
    assert result.returncode == 2


def test_an_unreadable_input_exits_2(tmp_path: Path) -> None:
    assert PYTHON is not None
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [PYTHON, "-I", str(SCRIPT), str(tmp_path / "missing"), str(tmp_path / "missing")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "cannot read" in result.stderr
