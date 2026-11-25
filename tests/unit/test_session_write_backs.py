"""Write-backs keep the format they read: a stored record is written back as a
record, with its unknown keys and without the browser stack, and a stored
legacy pickle is written back as a pickle, whatever the format setting."""

import json

import pytest
import requests

from graftpunk import cache
from graftpunk.cache import (
    copy_session,
    load_session,
    load_session_for_api,
    load_session_for_api_resolved,
    update_session_cookies,
)
from graftpunk.exceptions import NewerSessionRecord, SessionExpiredError
from graftpunk.session_legacy import state_from_pickle
from graftpunk.session_record import state_to_record
from graftpunk.session_view import StoredSession
from graftpunk.tokens import CachedToken
from tests.unit.session_compat_cases import (
    TEST_KEY,
    case_id,
    cases,
    cookie_dicts,
    decrypt,
    events,
    nodriver_state,
    record_bytes,
    records_json_could_not_have_written,
    token_dicts,
)


def api_with_new_cookie() -> requests.Session:
    api = load_session_for_api("myshop", key=TEST_KEY)
    api.cookies.set("new", "1", domain=".myshop.example")
    return api


def bare_api_with_new_cookie() -> requests.Session:
    api = requests.Session()
    api.cookies.set("new", "1", domain=".myshop.example")
    return api


@pytest.mark.parametrize("setting", ["pickle", "record"])
@pytest.mark.parametrize("stored", ["pickle", "record"])
def test_a_write_back_keeps_the_stored_format(setting, stored, session_store, session_format):
    session_store.seed(stored)
    session_format(setting)
    update_session_cookies(api_with_new_cookie(), "myshop", key=TEST_KEY)
    assert session_store.format_of("myshop") == stored
    assert "new" in session_store.cookie_names("myshop")
    copy_session("myshop", "myshop@alice", account_identifier="alice", key=TEST_KEY)
    assert session_store.format_of("myshop@alice") == stored


@pytest.mark.parametrize("setting", [None, "pickle"])
def test_unknown_keys_survive_both_write_backs(setting, session_store, session_format):
    extra = {"future": {"nested": {"deeper": [1, 2]}}, "note": "kept"}
    session_store.seed("record", extra=extra)
    if setting is not None:
        session_format(setting)
    update_session_cookies(api_with_new_cookie(), "myshop", key=TEST_KEY)
    copy_session("myshop", "myshop@alice", account_identifier="alice", key=TEST_KEY)
    for name in ("myshop", "myshop@alice"):
        doc = json.loads(session_store.plain(name))
        assert doc["future"] == {"nested": {"deeper": [1, 2]}}
        assert doc["note"] == "kept"
    assert "new" in session_store.cookie_names("myshop")


@pytest.mark.parametrize(
    ("plain", "reason", "error"),
    [
        (record_bytes(version=2), "NewerSessionRecord", NewerSessionRecord),
        (record_bytes(drop=("kind",)), "SessionExpiredError", SessionExpiredError),
        *(
            (plain, "SessionExpiredError", SessionExpiredError)
            for plain in records_json_could_not_have_written().values()
        ),
    ],
    ids=["newer", "invalid", *records_json_could_not_have_written()],
)
def test_a_record_that_cannot_be_read_is_left_byte_for_byte(
    plain, reason, error, session_store, captured_logs
):
    before = session_store.seed_plain(plain)
    update_session_cookies(bare_api_with_new_cookie(), "myshop", key=TEST_KEY)
    skipped = events(captured_logs, "session_save_skipped_load_failed")
    assert [e["reason"] for e in skipped] == [reason]
    assert "hint" not in skipped[0]
    with pytest.raises(error):
        copy_session("myshop", "myshop@alice", account_identifier="alice", key=TEST_KEY)
    assert session_store.raw("myshop") == before
    assert not (session_store.root / "sessions" / "myshop@alice").exists()


def test_without_the_browser_stack_a_record_write_back_works_and_a_pickle_one_is_skipped(
    session_store, captured_logs, no_browser_stack
):
    session_store.seed("record")
    before = session_store.seed("pickle", "other")
    api = bare_api_with_new_cookie()
    with no_browser_stack():
        update_session_cookies(api, "myshop", key=TEST_KEY)
        update_session_cookies(api, "other", key=TEST_KEY)
        copy_session("myshop", "myshop@alice", account_identifier="alice", key=TEST_KEY)
        with pytest.raises(ImportError):
            copy_session("other", "other@alice", account_identifier="alice", key=TEST_KEY)
    assert "new" in session_store.cookie_names("myshop")
    assert session_store.format_of("myshop") == "record"
    assert session_store.format_of("myshop@alice") == "record"
    (event,) = events(captured_logs, "session_save_skipped_load_failed")
    assert session_store.raw("other") == before
    assert event["reason"] == "RealSessionClassUnavailable"
    assert (
        event["hint"] == "the next login, or a write-back from a full install, rewrites the session"
    )
    assert not (session_store.root / "sessions" / "other@alice").exists()


@pytest.mark.parametrize("case", cases(recordable=True), ids=case_id)
def test_a_fixture_stored_as_a_record_reads_and_writes_back_like_its_pickle(case, session_store):
    legacy = state_from_pickle(decrypt(case))
    plain = state_to_record(legacy.kind, legacy.state)
    assert isinstance(plain, bytes)
    session_store.seed_plain(plain)

    def check() -> None:
        assert (
            cookie_dicts(load_session("myshop", key=TEST_KEY).cookies) == case.expected["cookies"]
        )
        for api in (
            load_session_for_api("myshop", key=TEST_KEY),
            load_session_for_api_resolved("myshop", key=TEST_KEY)[0],
        ):
            assert cookie_dicts(api.cookies) == case.expected["cookies"]
            assert token_dicts(api) == case.expected["cached_tokens"]

    check()
    update_session_cookies(load_session_for_api("myshop", key=TEST_KEY), "myshop", key=TEST_KEY)
    assert session_store.format_of("myshop") == "record"
    check()
    copy_session("myshop", "myshop@alice", account_identifier="alice", key=TEST_KEY)
    assert session_store.format_of("myshop@alice") == "record"


def test_copy_renames_a_browser_record_and_keeps_its_times(session_store):
    session_store.seed("record")
    source = session_store.metadata("myshop")
    copy_session("myshop", "myshop@alice", account_identifier="alice", key=TEST_KEY)
    doc = json.loads(session_store.plain("myshop@alice"))
    assert doc["session_name"] == "myshop@alice"
    assert doc["riders"]["account"] == "alice"
    copied = session_store.metadata("myshop@alice")
    assert (copied.created_at, copied.modified_at, copied.expires_at, copied.status) == (
        source.created_at,
        source.modified_at,
        source.expires_at,
        source.status,
    )
    assert copied.account_identifier == "alice"


def test_copy_of_an_api_record_gives_it_no_session_name(session_store):
    session_store.seed_plain(record_bytes("api"))
    copy_session("myshop", "myshop@alice", account_identifier="alice", key=TEST_KEY)
    doc = json.loads(session_store.plain("myshop@alice"))
    assert "session_name" not in doc
    assert "riders" not in doc
    assert session_store.metadata("myshop@alice").account_identifier == "alice"


def test_a_record_write_back_keeps_cookies_requests_builds(session_store):
    """A response or a plugin adds cookies to the API session through requests'
    own builders; the record written back carries each of them unchanged."""
    from tests.unit.session_compat_cases import cookies_requests_builds

    session_store.seed("record")
    api = load_session_for_api("myshop", key=TEST_KEY)
    added = cookies_requests_builds()
    for cookie in added:
        api.cookies.set_cookie(cookie)

    update_session_cookies(api, "myshop", key=TEST_KEY)

    assert session_store.format_of("myshop") == "record"
    names = {cookie.name for cookie in added}
    reread = load_session_for_api("myshop", key=TEST_KEY).cookies
    assert [c for c in cookie_dicts(reread) if c["name"] in names] == cookie_dicts(added)


@pytest.mark.parametrize("value", ['"\\"', '"a"', '"', 'a"b"c', '"a\\"b"'])
def test_a_quoted_cookie_value_reads_and_writes_back_as_from_a_pickle(
    value, session_store, session_format
):
    """requests' jar deletes each \\" from a value that starts and ends with a
    quote whenever a cookie is added through it. A record reads every value as
    the pickle of the same session does, and a write-back stores what a pickle
    write-back stores."""
    from http import cookiejar

    from tests.unit.session_compat_cases import nodriver_session

    def sid(jar) -> list[str]:
        return [c.value for c in jar if c.name == "sid"]

    seen = {}
    for fmt in ("pickle", "record"):
        session_format(fmt)
        session = nodriver_session(tokens=True)
        stored = next(c for c in session.cookies if c.name == "sid")
        session.cookies.clear(stored.domain, stored.path, stored.name)
        stored.value = value
        cookiejar.CookieJar.set_cookie(session.cookies, stored)
        cache.cache_session(session, fmt, key=TEST_KEY)
        assert session_store.format_of(fmt) == fmt
        api = load_session_for_api(fmt, key=TEST_KEY)
        read = {
            "load_session": sid(load_session(fmt, key=TEST_KEY).cookies),
            "for_api": sid(api.cookies),
            "from_bytes": sid(
                cache.load_session_for_api_from_bytes(session_store.raw(fmt), key=TEST_KEY).cookies
            ),
        }
        update_session_cookies(api, fmt, key=TEST_KEY)
        assert session_store.format_of(fmt) == fmt
        written = sid(load_session(fmt, key=TEST_KEY).cookies)
        seen[fmt] = (read, written)
    assert seen["pickle"][0] == dict.fromkeys(seen["pickle"][0], [value])
    assert seen["record"] == seen["pickle"]


def test_a_value_a_record_cannot_hold_skips_the_write_back(session_store, captured_logs):
    before = session_store.seed("record")
    api = bare_api_with_new_cookie()
    api._gp_cached_tokens = {  # type: ignore[attr-defined]
        "X-CSRF": CachedToken(name="X-CSRF", value="t", extracted_at=1.0, ttl=float("inf"))
    }
    update_session_cookies(api, "myshop", key=TEST_KEY)
    (failed,) = events(captured_logs, "session_save_failed")
    assert failed["reason"] == "ValueError"
    assert "_gp_cached_tokens" in failed["error"]
    assert session_store.raw("myshop") == before


def _view_that_cannot_be_written(*args, **kwargs):
    return StoredSession(None, nodriver_state())


def _view_holding_a_value_a_record_cannot(*args, **kwargs):
    state = nodriver_state()
    state["_gp_cached_tokens"] = {
        "X-CSRF": CachedToken(name="X-CSRF", value="t", extracted_at=1.0, ttl=float("nan"))
    }
    return StoredSession("browser", state)


@pytest.mark.parametrize(
    ("loaded", "reason"),
    [
        (_view_that_cannot_be_written, "TypeError"),
        (_view_holding_a_value_a_record_cannot, "ValueError"),
    ],
    ids=["not-writable", "unrecordable"],
)
def test_an_encode_refusal_leaves_the_store_untouched(
    loaded, reason, session_store, captured_logs, monkeypatch
):
    before = session_store.seed("record")
    monkeypatch.setattr(cache, "_load_for_write_back", loaded)
    update_session_cookies(bare_api_with_new_cookie(), "myshop", key=TEST_KEY)
    (failed,) = events(captured_logs, "session_save_failed")
    assert failed["reason"] == reason
    with pytest.raises((TypeError, ValueError)) as raised:
        copy_session("myshop", "myshop@alice", account_identifier="alice", key=TEST_KEY)
    assert type(raised.value).__name__ == reason
    assert session_store.raw("myshop") == before
    assert not (session_store.root / "sessions" / "myshop@alice").exists()
