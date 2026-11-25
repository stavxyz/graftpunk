"""StoredSession: the browser-free stand-in for a saved session."""

from __future__ import annotations

import copy
import typing

import dill
import pytest
import requests

from graftpunk.cache import _extract_session_metadata, load_session
from graftpunk.session_legacy import state_from_pickle
from graftpunk.session_record import FIELDS, Record, classify, state_to_record
from graftpunk.session_riders import (
    _CSRF_TOKENS_ATTR,
    SESSION_RIDER_ATTRS,
    SESSION_RIDER_DEFAULTS,
)
from graftpunk.session_view import StoredSession, StoredSessionLike
from tests.unit.session_compat_cases import (
    assert_leaf_imports,
    case_id,
    case_named,
    cases,
    cookie_dicts,
    decrypt,
    nodriver_state,
    record_bytes,
)


def test_session_view_is_a_leaf():
    assert_leaf_imports(
        "graftpunk.session_view",
        {"graftpunk.session_record", "graftpunk.session_riders", "graftpunk.cookies"},
    )


@pytest.mark.parametrize("case", cases(recordable=True), ids=case_id)
def test_the_view_and_the_real_object_give_the_same_metadata(case, session_store):
    legacy = state_from_pickle(decrypt(case))
    view = StoredSession(legacy.kind, legacy.state)
    session_store.put_case(case)
    real = load_session("myshop", key=case.key)
    assert _extract_session_metadata(view, "s") == _extract_session_metadata(real, "s")


@pytest.mark.parametrize("case", cases(recordable=True), ids=case_id)
def test_the_view_gives_back_the_stored_keys_of_its_state(case):
    legacy = state_from_pickle(decrypt(case))
    stored = {f.state_key for f in FIELDS if legacy.kind in f.kinds}
    view = StoredSession(legacy.kind, legacy.state)
    assert set(view.stored_state()) == set(legacy.state) & stored


def test_the_protocol_names_every_registered_rider():
    assert set(SESSION_RIDER_ATTRS) <= set(typing.get_type_hints(StoredSessionLike))


def test_setting_a_name_on_a_kind_with_no_name_writes_no_name():
    view = StoredSession("requests", requests.Session().__getstate__())
    view.session_name = "x"
    assert view.session_name == "x"
    assert "session_name" not in view.stored_state()


def test_a_memory_only_rider_can_be_set_and_is_not_stored():
    view = StoredSession("browser", nodriver_state())
    setattr(view, _CSRF_TOKENS_ATTR, {"t": "v"})
    assert getattr(view, _CSRF_TOKENS_ATTR) == {"t": "v"}
    assert _CSRF_TOKENS_ATTR not in view.stored_state()


def test_a_view_with_no_kind_can_be_read_but_not_written():
    view = StoredSession(None, state_from_pickle(decrypt(case_named("1.44.0", "subclass"))).state)
    assert cookie_dicts(view.cookies)
    assert view.writable is False
    with pytest.raises(TypeError):
        view.stored_state()


def test_a_view_refuses_to_be_pickled():
    with pytest.raises(TypeError):
        dill.dumps(StoredSession("browser", nodriver_state()))


def test_a_view_refuses_to_be_copied():
    with pytest.raises(TypeError):
        copy.copy(StoredSession("browser", nodriver_state()))


def test_a_rider_absent_from_the_state_reads_as_its_default_and_stays_mutable():
    state = nodriver_state()
    state.pop("_gp_header_role_origins", None)
    view = StoredSession("browser", state)
    for attr in SESSION_RIDER_ATTRS:
        if attr not in state:
            assert getattr(view, attr) == SESSION_RIDER_DEFAULTS[attr]()
    view._gp_header_role_origins["a"] = "https://myshop.example"
    assert view._gp_header_role_origins == {"a": "https://myshop.example"}
    assert "_gp_header_role_origins" not in state


def test_a_stored_rider_set_on_the_view_is_stored():
    view = StoredSession("browser", nodriver_state())
    view._gp_account_identifier = "acct"
    assert view.stored_state()["_gp_account_identifier"] == "acct"


def test_a_view_built_from_a_record_reads_its_cookies_and_extra():
    record = classify(record_bytes("browser", extra={"future_key": 1}))
    assert isinstance(record, Record)
    view = StoredSession.from_record(record)
    assert view.kind == "browser"
    assert view.extra == {"future_key": 1}
    assert view.writable is True
    assert type(view.cookies) is requests.cookies.RequestsCookieJar
    assert cookie_dicts(view.cookies)


def test_what_a_view_stores_is_a_record_the_codec_accepts():
    view = StoredSession("browser", nodriver_state(tokens=True))
    payload = state_to_record("browser", view.stored_state(), view.extra)
    assert isinstance(payload, bytes)
    assert isinstance(classify(payload), Record)
