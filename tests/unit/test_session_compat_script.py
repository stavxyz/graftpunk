"""scripts/session_compat.py, run against the working tree's graftpunk.

The script's real use installs a release from PyPI; these tests pass this
interpreter instead, so they need no network and check that the child half
still runs on today's code.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tests.unit.session_compat_cases import EXPECTED_KIND, TEST_KEY

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "session_compat.py"
_spec = importlib.util.spec_from_file_location("session_compat", SCRIPT)
assert _spec is not None and _spec.loader is not None
session_compat = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(session_compat)


def test_the_script_and_the_cases_agree_on_the_case_list():
    assert set(session_compat.CASES) == set(EXPECTED_KIND)


def test_generate_writes_every_case_and_removes_its_store(tmp_path):
    out = tmp_path / "out"
    session_compat.generate("working-tree", out, TEST_KEY, python=sys.executable)
    written = {p.name for p in out.iterdir()}
    for case in EXPECTED_KIND:
        assert {f"{case}.enc", f"{case}.expected.json"} <= written
        assert (f"{case}.metadata.json" in written) is (case != "flatfile")
    assert session_compat.STORE_DIRNAME not in written
    metadata = json.loads((out / "account.metadata.json").read_text())
    assert metadata["name"] == "myshop@alice"
    assert metadata["storage_location"].startswith(session_compat.PLACEHOLDER_CONFIG_DIR)


def test_generate_refuses_a_directory_that_holds_files(tmp_path):
    out = tmp_path / "1.44.0"
    out.mkdir()
    (out / "nodriver.enc").write_bytes(b"frozen")
    with pytest.raises(session_compat.CompatError, match="not an empty directory"):
        session_compat.generate("working-tree", out, TEST_KEY, python=sys.executable)
    assert [p.name for p in out.iterdir()] == ["nodriver.enc"]
    assert (out / "nodriver.enc").read_bytes() == b"frozen"


def test_read_reports_cookies_and_tokens(session_store, capfd):
    session_store.seed("pickle")
    status = session_compat.read(
        "working-tree", session_store.root, "myshop", TEST_KEY, python=sys.executable
    )
    assert status == 0
    assert capfd.readouterr().out.splitlines() == ["cookies: pref sid", "tokens: bearer csrf"]


def test_read_exits_3_with_the_exception_name(session_store, capfd):
    status = session_compat.read(
        "working-tree", session_store.root, "nothing-here", TEST_KEY, python=sys.executable
    )
    assert status == session_compat.READ_FAILED
    assert capfd.readouterr().out.strip() == "SessionNotFoundError"


def test_read_refuses_to_replace_a_different_key(session_store):
    other = b"A" * 43 + b"="
    with pytest.raises(session_compat.CompatError, match="different key"):
        session_compat.read(
            "working-tree", session_store.root, "myshop", other, python=sys.executable
        )
    assert (session_store.root / ".session_key").read_bytes() == TEST_KEY
