"""Fresh saves: GRAFTPUNK_SESSION_FORMAT picks the format, and a session the
codec refuses is still saved, as a pickle."""

import dataclasses
import inspect
import json
import typing

import pytest
import requests
from pydantic import ValidationError
from requests.adapters import HTTPAdapter

from graftpunk import cache
from graftpunk.cache import (
    _encode_as,
    _encode_fresh,
    cache_session,
    copy_session,
    load_session,
    load_session_for_api,
    load_session_for_api_from_bytes,
    load_session_for_api_resolved,
    update_session_cookies,
)
from graftpunk.config import GraftpunkSettings
from graftpunk.graftpunk_session import GraftpunkSession
from graftpunk.session_record import SessionFormat
from graftpunk.session_view import StoredSession
from tests.unit.session_compat_cases import (
    TEST_KEY,
    events,
    nodriver_session,
    nodriver_state,
)

# str(inspect.signature(fn)) as printed by graftpunk 1.44.0.
SIGNATURES_AT_1_44_0 = [
    (
        cache_session,
        "(session: ~T, session_name: str | None = None, *, "
        "account_rule: int | None = 2, key: bytes | None = None) -> str",
    ),
    (
        load_session,
        "(name: str, backend_override: str | None = None, *, key: bytes | None = None)"
        " -> graftpunk.cache.SessionLike",
    ),
    (
        load_session_for_api,
        "(name: str, *, resolve: bool = True, key: bytes | None = None)"
        " -> requests.sessions.Session",
    ),
    (
        load_session_for_api_resolved,
        "(name: str, *, resolve: bool = True, key: bytes | None = None)"
        " -> tuple[requests.sessions.Session, str]",
    ),
    (
        load_session_for_api_from_bytes,
        "(encrypted: bytes, *, key: bytes | None = None) -> requests.sessions.Session",
    ),
    (
        update_session_cookies,
        "(api_session: requests.sessions.Session, session_name: str, *, "
        "key: bytes | None = None) -> None",
    ),
    (
        copy_session,
        "(source: str, dest: str, *, account_identifier: str | None, "
        "key: bytes | None = None) -> None",
    ),
]


def test_the_default_still_writes_a_pickle(session_store):
    cache_session(nodriver_session(), "myshop", key=TEST_KEY)
    assert session_store.format_of("myshop") == "pickle"


def test_an_explicit_pickle_setting_writes_a_pickle(session_store, session_format):
    session_format("pickle")
    cache_session(nodriver_session(), "myshop", key=TEST_KEY)
    assert session_store.format_of("myshop") == "pickle"


def test_record_setting_writes_a_record(session_store, session_format):
    session_format("record")
    cache_session(nodriver_session(), "myshop", key=TEST_KEY)
    assert json.loads(session_store.plain("myshop"))["kind"] == "browser"


def test_a_record_save_keeps_the_metadata_a_pickle_save_has(session_store, session_format):
    cache_session(nodriver_session(tokens=True), "plain", key=TEST_KEY)
    session_format("record")
    cache_session(nodriver_session(tokens=True), "rec", key=TEST_KEY)
    a, b = session_store.metadata("plain"), session_store.metadata("rec")
    assert (a.domain, a.cookie_count, a.cookie_domains, a.status, a.account_rule) == (
        b.domain,
        b.cookie_count,
        b.cookie_domains,
        b.status,
        b.account_rule,
    )
    assert b.checksum != a.checksum


def test_a_saved_record_loads_back_with_its_cookies(session_store, session_format):
    session_format("record")
    cache_session(nodriver_session(), "myshop", key=TEST_KEY)
    expected = {c.name for c in nodriver_session().cookies}
    assert session_store.cookie_names("myshop") == expected
    api = load_session_for_api("myshop", key=TEST_KEY)
    assert {c.name for c in api.cookies} == expected


def test_an_unrecordable_session_is_pickled_with_one_warning(
    session_store, session_format, captured_logs
):
    session_format("record")
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=3))
    cache_session(s, "myshop", key=TEST_KEY)
    assert session_store.format_of("myshop") == "pickle"
    (event,) = events(captured_logs, "session_not_recordable")
    assert event["key"] == "adapters" and event["name"] == "myshop" and event["reason"]


def test_a_subclass_is_pickled_and_the_warning_names_its_type(
    session_store, session_format, captured_logs
):
    class Mine(GraftpunkSession):
        pass

    session_format("record")
    cache_session(Mine(), "myshop", key=TEST_KEY)
    (event,) = events(captured_logs, "session_not_recordable")
    assert event["key"] == "type" and "Mine" in event["reason"]
    assert session_store.format_of("myshop") == "pickle"


def test_a_non_json_token_is_pickled_with_a_warning_naming_the_key(
    session_store, session_format, captured_logs
):
    session_format("record")
    session = nodriver_session(tokens=True)
    tokens = session._gp_cached_tokens
    first = next(iter(tokens))
    tokens[first] = dataclasses.replace(tokens[first], ttl=float("inf"))
    cache_session(session, "myshop", key=TEST_KEY)
    (event,) = events(captured_logs, "session_not_recordable")
    assert event["key"] == "_gp_cached_tokens"
    assert session_store.format_of("myshop") == "pickle"


def test_a_recordable_save_logs_no_warning(session_store, session_format, captured_logs):
    session_format("record")
    cache_session(nodriver_session(), "myshop", key=TEST_KEY)
    assert events(captured_logs, "session_not_recordable") == []


def test_a_mistyped_format_fails_naming_the_choices(session_store, session_format):
    with pytest.raises(ValidationError, match="pickle.*record"):
        session_format("json")
        cache_session(nodriver_session(), "myshop", key=TEST_KEY)
    # Checked on disk: reading the settings back would raise again.
    assert not any(session_store.root.rglob("myshop*"))


def test_the_setting_and_the_codec_name_the_same_formats():
    assert typing.get_args(GraftpunkSettings.model_fields["session_format"].annotation) == (
        typing.get_args(SessionFormat)
    )


def test_encode_as_follows_the_type_of_what_it_is_given(session_format):
    session_format("record")
    assert _encode_as(StoredSession("browser", nodriver_state()))[:1] == b"{"
    assert _encode_as(nodriver_session())[:1] == b"\x80"  # a real object stays a pickle
    with pytest.raises(TypeError):
        _encode_as(StoredSession(None, nodriver_state()))


def test_encode_as_keeps_a_views_extra_keys():
    payload = _encode_as(StoredSession("browser", nodriver_state(), {"future_key": {"a": 1}}))
    assert json.loads(payload)["future_key"] == {"a": 1}


def test_encode_fresh_writes_a_pickle_when_the_setting_is_unset():
    assert _encode_fresh(nodriver_session())[:1] == b"\x80"


def test_store_with_given_metadata_keeps_its_times_and_replaces_the_checksum(session_store):
    cache_session(nodriver_session(), "src", key=TEST_KEY)
    original = session_store.metadata("src")
    payload = b"{}"
    cache._store(
        "dst",
        payload,
        nodriver_session(),
        account_rule=original.account_rule,
        key=TEST_KEY,
        metadata=original,
    )
    saved = session_store.metadata("dst")
    assert saved.created_at == original.created_at
    assert saved.checksum != original.checksum
    assert session_store.plain("dst") == payload


@pytest.mark.parametrize(
    ("fn", "text"), SIGNATURES_AT_1_44_0, ids=lambda v: v if isinstance(v, str) else v.__name__
)
def test_public_signatures_are_unchanged(fn, text):
    assert str(inspect.signature(fn)) == text
