"""scripts/session_compat.py, run against the working tree's graftpunk.

The script's real use installs a release from PyPI; these tests pass this
interpreter instead, so they need no network and check that the child half
still runs on today's code.
"""

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

from graftpunk.encryption import decrypt_data
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


def _failing_child() -> str:
    false = shutil.which("false")
    assert false is not None
    return false  # ignores its arguments and exits 1, as a crashed child would


@pytest.mark.parametrize("existing", [False, True], ids=["new", "empty"])
def test_a_failed_generate_leaves_nothing_and_a_rerun_works(tmp_path, existing):
    parent = tmp_path / "fixtures"
    parent.mkdir()
    out = parent / "1.44.0"
    if existing:
        out.mkdir()
    with pytest.raises(session_compat.CompatError, match="failed"):
        session_compat.generate("working-tree", out, TEST_KEY, python=_failing_child())
    assert [p.name for p in parent.iterdir()] == (["1.44.0"] if existing else [])
    assert not existing or list(out.iterdir()) == []
    session_compat.generate("working-tree", out, TEST_KEY, python=sys.executable)
    assert [p.name for p in parent.iterdir()] == ["1.44.0"]
    assert (out / "nodriver.enc").exists()


def test_read_removes_the_key_it_wrote(session_store, capfd):
    session_store.seed("pickle")
    (session_store.root / ".session_key").unlink()
    status = session_compat.read(
        "working-tree", session_store.root, "myshop", TEST_KEY, python=sys.executable
    )
    assert status == 0
    assert capfd.readouterr().out.splitlines()[0] == "cookies: pref sid"
    assert not (session_store.root / ".session_key").exists()


def test_read_keeps_a_key_it_did_not_write(session_store, capfd):
    session_store.seed("pickle")
    session_compat.read(
        "working-tree", session_store.root, "myshop", TEST_KEY, python=sys.executable
    )
    capfd.readouterr()
    assert (session_store.root / ".session_key").read_bytes() == TEST_KEY


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


@pytest.mark.parametrize("fmt", ["pickle", "record"])
def test_write_stores_the_tokens_session_in_the_requested_format(tmp_path, capfd, fmt):
    store = tmp_path / "store"
    session_compat.write(store, "myshop", fmt, TEST_KEY, python=sys.executable)
    status = session_compat.read("working-tree", store, "myshop", TEST_KEY, python=sys.executable)
    assert status == 0
    assert capfd.readouterr().out.splitlines() == ["cookies: pref sid", "tokens: bearer csrf"]
    plain = decrypt_data(
        (store / "sessions" / "myshop" / "session.pickle").read_bytes(), key=TEST_KEY
    )
    assert plain.startswith(b"{") is (fmt == "record")


def test_write_refuses_a_slot_that_exists(session_store):
    before = session_store.seed("pickle")
    with pytest.raises(session_compat.CompatError, match="refusing to replace"):
        session_compat.write(
            session_store.root, "myshop", "record", TEST_KEY, python=sys.executable
        )
    assert session_store.raw("myshop") == before


def test_write_without_a_format_leaves_the_setting_unset_whatever_the_caller_has(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GRAFTPUNK_SESSION_FORMAT", "record")
    store = tmp_path / "store"
    session_compat.write(store, "myshop", None, TEST_KEY, python=sys.executable)
    plain = decrypt_data(
        (store / "sessions" / "myshop" / "session.pickle").read_bytes(), key=TEST_KEY
    )
    assert not plain.startswith(b"{")
