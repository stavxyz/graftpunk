"""Every session reader classifies the stored bytes first, then reads a record
or a legacy pickle; the API loaders read either without the browser stack."""

import copyreg
import json
import os
import re
import subprocess
import sys
import types

import dill
import pytest
import requests

from graftpunk._distribution import missing_browser_module
from graftpunk.cache import (
    _BYTES_LABEL,
    cache_session,
    load_session,
    load_session_for_api,
    load_session_for_api_from_bytes,
    load_session_for_api_resolved,
)
from graftpunk.exceptions import (
    NewerSessionRecord,
    RealSessionClassUnavailable,
    SessionExpiredError,
)
from graftpunk.graftpunk_session import GraftpunkSession
from graftpunk.session_identity import GP_ACCOUNT_ATTR
from graftpunk.session_legacy import state_from_pickle
from graftpunk.session_record import kind_of, state_to_record
from tests.unit.session_compat_cases import (
    TEST_KEY,
    case_id,
    cases,
    cookie_dicts,
    decrypt,
    encrypt,
    nodriver_state,
    record_bytes,
    token_dicts,
)


def as_record(case):
    legacy = state_from_pickle(decrypt(case))
    record = state_to_record(legacy.kind, legacy.state)
    assert isinstance(record, bytes), record
    return record


def api_fingerprint(api):
    return (
        cookie_dicts(api.cookies),
        sorted(api.headers.items()),
        getattr(api, "_gp_header_roles", {}),
        token_dicts(api),
    )


@pytest.mark.parametrize("case", cases(recordable=True), ids=case_id)
def test_a_record_loads_for_api_exactly_as_its_pickle_did(case):
    record = encrypt(as_record(case), case.key)
    assert api_fingerprint(load_session_for_api_from_bytes(record, key=case.key)) == (
        api_fingerprint(load_session_for_api_from_bytes(case.encrypted, key=case.key))
    )


_API_LOAD = """
import json, sys
from graftpunk.cache import load_session_for_api
api = load_session_for_api("myshop")
print(json.dumps({"cookies": len(api.cookies), "modules": sorted(sys.modules)}))
"""


def modules_after_api_load_in_subprocess(session_store):
    env = {**os.environ, "GRAFTPUNK_CONFIG_DIR": str(session_store.root)}
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _API_LOAD], capture_output=True, text=True, timeout=60, env=env
    )
    assert result.returncode == 0, result.stderr
    loaded = json.loads(result.stdout.strip().splitlines()[-1])
    assert loaded["cookies"] == 2, loaded
    return set(loaded["modules"])


@pytest.mark.parametrize("fmt", ["pickle", "record"])
def test_an_api_load_does_not_import_graftpunk_session(fmt, session_store):
    session_store.seed(fmt)
    modules = modules_after_api_load_in_subprocess(session_store)
    assert "graftpunk.session" not in modules
    assert not modules & {"selenium", "requestium", "nodriver", "httpie"}


@pytest.mark.parametrize("cls", [GraftpunkSession, requests.Session])
def test_load_session_of_a_record_returns_the_class_that_was_saved(cls, session_store):
    record = state_to_record(kind_of(cls()), cls().__getstate__())
    assert isinstance(record, bytes), record
    session_store.seed_plain(record)
    assert type(load_session("myshop", key=TEST_KEY)) is cls


def test_load_session_of_a_browser_record_returns_a_browser_session(session_store):
    from graftpunk.session import BrowserSession

    session_store.seed("record")
    loaded = load_session("myshop", key=TEST_KEY)
    assert type(loaded) is BrowserSession
    assert loaded.cookies.get("sid", domain=".myshop.example") == "compat-sid"


def test_load_session_of_a_browser_record_without_the_browser_stack_gives_the_install_hint(
    session_store, no_browser_stack
):
    session_store.seed_plain(record_bytes())
    with no_browser_stack():
        with pytest.raises(RealSessionClassUnavailable) as exc:
            load_session("myshop", key=TEST_KEY)
        # Asked while the stack is absent: it names a module only when that
        # module cannot be imported.
        assert missing_browser_module(exc.value) == "requestium"


def test_load_session_of_a_browser_pickle_without_the_browser_stack_gives_the_install_hint(
    session_store, no_browser_stack
):
    session_store.seed("pickle")
    with no_browser_stack():
        with pytest.raises(RealSessionClassUnavailable) as exc:
            load_session("myshop", key=TEST_KEY)
        # Asked while the stack is absent: it names a module only when that
        # module cannot be imported.
        assert missing_browser_module(exc.value) == "requestium"
    assert "load_session_for_api_from_bytes" in str(exc.value)


@pytest.mark.parametrize("fmt", ["pickle", "record"])
def test_an_api_load_needs_no_browser_stack(fmt, session_store, no_browser_stack):
    session_store.seed(fmt)
    with no_browser_stack():
        api = load_session_for_api("myshop", key=TEST_KEY)
    assert api.cookies.get("sid", domain=".myshop.example") == "compat-sid"


def test_a_restored_graftpunk_session_has_every_attribute_a_fresh_one_has():
    fresh = GraftpunkSession()
    restored = GraftpunkSession.__new__(GraftpunkSession)
    restored.__setstate__(fresh.__getstate__())
    assert set(vars(fresh)) <= set(vars(restored))


def test_a_restored_graftpunk_session_snapshots_its_restored_headers():
    saved = GraftpunkSession()
    saved.headers["X-Saved"] = "1"
    restored = GraftpunkSession.__new__(GraftpunkSession)
    restored.__setstate__(saved.__getstate__())
    assert restored._gp_default_session_headers == dict(restored.headers)
    assert restored._gp_default_session_headers is not restored.headers


@pytest.mark.parametrize(
    "fmt",
    ["pickle", "record"],
)
def test_a_loaded_graftpunk_session_can_send_a_request(
    fmt, session_store, session_format, echo_server
):
    session_format(fmt)
    cache_session(GraftpunkSession(), "myshop", key=TEST_KEY)
    assert session_store.format_of("myshop") == fmt
    assert load_session("myshop", key=TEST_KEY).get(echo_server.url).status_code == 200


def test_an_api_load_keeps_the_account_from_the_metadata(session_store):
    session_store.seed("record", "myshop@alice", account="alice")
    assert getattr(load_session_for_api("myshop@alice", key=TEST_KEY), GP_ACCOUNT_ATTR) == "alice"


@pytest.mark.parametrize("fmt", ["pickle", "record"])
def test_an_api_load_takes_the_account_the_metadata_alone_holds(fmt, session_store):
    session_store.seed(fmt, "myshop@alice")
    metadata_file = session_store.root / "sessions" / "myshop@alice" / "metadata.json"
    metadata = json.loads(metadata_file.read_text())
    assert metadata["account_identifier"] is None
    metadata_file.write_text(json.dumps({**metadata, "account_identifier": "alice"}))

    api = load_session_for_api("myshop@alice", key=TEST_KEY)

    assert getattr(api, GP_ACCOUNT_ATTR) == "alice"


@pytest.mark.parametrize("fmt", ["pickle", "record"])
def test_an_api_load_stamps_the_slot_it_read(fmt, session_store):
    session_store.seed(fmt, "myshop@alice")
    api, loaded = load_session_for_api_resolved("myshop", key=TEST_KEY)
    assert loaded == "myshop@alice"
    assert api._gp_session_name == "myshop@alice"


def test_a_bytes_load_leaves_the_session_unstamped(session_store):
    session_store.seed("record")
    api = load_session_for_api_from_bytes(session_store.raw("myshop"), key=TEST_KEY)
    assert getattr(api, "_gp_session_name", None) is None


def store_payload(session_store, plain, name="myshop"):
    """Store *plain* under *name*, which ``seed_plain`` cannot when *plain* is
    not a readable session: seed a valid one, then replace its payload and
    the checksum its metadata keeps."""
    import hashlib

    session_store.seed("pickle", name)
    slot = session_store.root / "sessions" / name
    (slot / "session.pickle").write_bytes(encrypt(plain))
    metadata = json.loads((slot / "metadata.json").read_text())
    metadata["checksum"] = hashlib.sha256(plain).hexdigest()
    (slot / "metadata.json").write_text(json.dumps(metadata))


def readers(session_store):
    return {
        "myshop": [
            lambda: load_session("myshop", key=TEST_KEY),
            lambda: load_session_for_api("myshop", key=TEST_KEY),
            lambda: load_session_for_api_resolved("myshop", key=TEST_KEY),
        ],
        _BYTES_LABEL: [
            lambda: load_session_for_api_from_bytes(session_store.raw("myshop"), key=TEST_KEY)
        ],
    }


@pytest.mark.parametrize(
    "plain",
    [
        record_bytes(drop=("kind",)),
        b"[1, 2]",
        b"{not json",
        json.dumps({**json.loads(record_bytes()), "cookies": [{"value": "no-name"}]}).encode(),
    ],
    ids=["no-kind", "array", "not-json", "cookie-without-name"],
)
def test_an_invalid_record_reads_as_an_expired_session(plain, session_store):
    store_payload(session_store, plain)
    for name, fns in readers(session_store).items():
        for fn in fns:
            with pytest.raises(SessionExpiredError, match=re.escape(f"'{name}'")):
                fn()


def test_a_version_2_record_is_refused_by_every_reader(session_store):
    session_store.seed_plain(record_bytes(version=2))
    for fns in readers(session_store).values():
        for fn in fns:
            with pytest.raises(NewerSessionRecord):
                fn()


@pytest.mark.parametrize("plain", [b"not a pickle at all", b"\x80\x04K"], ids=["text", "truncated"])
def test_a_corrupt_pickle_reads_as_an_expired_session(plain, session_store):
    store_payload(session_store, plain)
    for name, fns in readers(session_store).items():
        for fn in fns:
            with pytest.raises(SessionExpiredError, match=re.escape(f"'{name}'")):
                fn()


def test_a_pickle_of_something_without_session_state_has_invalid_structure(session_store):
    import dill

    store_payload(session_store, dill.dumps({"not": "a session"}))
    for fn in readers(session_store)["myshop"]:
        with pytest.raises(SessionExpiredError, match=re.escape("'myshop' has invalid structure")):
            fn()
    with pytest.raises(SessionExpiredError, match="^Session bytes have invalid structure") as exc:
        readers(session_store)[_BYTES_LABEL][0]()
    # A caller holding bytes has no store to clear.
    assert "gp clear" not in str(exc.value)


class _PickledAs:
    """Pickles as *cls*, with *state* handed to ``cls.__setstate__`` on load,
    so a test can store a real session class holding a value no session sets."""

    def __init__(self, cls, state):
        self.cls, self.state = cls, state

    def __reduce_ex__(self, protocol):
        return (copyreg._reconstructor, (self.cls, object, None), self.state)


def _pickled_as(shape, over):
    if shape == "namespace":
        state = {"cookies": requests.cookies.RequestsCookieJar(), "headers": {}, **over}
        return dill.dumps(types.SimpleNamespace(**state))
    if shape == "BrowserSession":
        from graftpunk.session import BrowserSession

        return dill.dumps(_PickledAs(BrowserSession, {**nodriver_state(), **over}))
    cls = {"requests.Session": requests.Session, "GraftpunkSession": GraftpunkSession}[shape]
    return dill.dumps(_PickledAs(cls, {**requests.Session().__getstate__(), **over}))


_BAD_VALUES = {
    "cookies-int": {"cookies": 5},
    "cookies-none": {"cookies": None},
    "cookies-str": {"cookies": "not-a-jar"},
    "cookies-list-of-int": {"cookies": [1]},
    "headers-int": {"headers": 5},
    "headers-none": {"headers": None},
    "headers-list": {"headers": [1]},
    "roles-int": {"_gp_header_roles": 5},
    "roles-of-int": {"_gp_header_roles": {"xhr": 5}},
}
_SHAPES = ("namespace", "BrowserSession", "requests.Session", "GraftpunkSession")


@pytest.mark.parametrize("bad", sorted(_BAD_VALUES))
@pytest.mark.parametrize("shape", _SHAPES)
def test_a_pickle_holding_a_value_no_session_has_reads_as_an_expired_session(
    shape, bad, session_store
):
    """No reader leaks a TypeError or AttributeError from inside a session it
    could not use: the API loaders report it as expired, and ``load_session``
    either returns what was saved or reports it as expired."""
    store_payload(session_store, _pickled_as(shape, _BAD_VALUES[bad]))
    load_session_reader, *api_readers = readers(session_store)["myshop"]
    for fn in [*api_readers, *readers(session_store)[_BYTES_LABEL]]:
        with pytest.raises(SessionExpiredError):
            fn()
    try:
        load_session_reader()
    except SessionExpiredError as exc:
        assert "'myshop'" in str(exc)


@pytest.mark.parametrize(
    "over",
    [{"cookies": 5}, {"cookies": None}, {"headers": 5}, {"headers": None}, {"headers": {"a": 1}}],
    ids=["cookies-int", "cookies-none", "headers-int", "headers-none", "headers-object"],
)
def test_a_record_holding_a_value_no_session_has_reads_as_an_expired_session(over, session_store):
    store_payload(session_store, json.dumps({**json.loads(record_bytes()), **over}).encode())
    for name, fns in readers(session_store).items():
        for fn in fns:
            with pytest.raises(SessionExpiredError, match=re.escape(f"'{name}'")):
                fn()


def test_the_install_hint_names_both_browser_free_loaders(session_store, no_browser_stack):
    session_store.seed("pickle")
    with no_browser_stack(), pytest.raises(RealSessionClassUnavailable) as exc:
        load_session("myshop", key=TEST_KEY)
    assert "load_session_for_api or load_session_for_api_from_bytes" in str(exc.value)
