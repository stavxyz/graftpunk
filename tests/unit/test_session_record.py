"""The session record: one field table, and the codec built from it."""

from __future__ import annotations

import json
import math
import sys
from collections import Counter
from collections.abc import Callable
from http import cookiejar
from pathlib import Path
from typing import Any

import dill
import pytest
import requests
from requests.structures import CaseInsensitiveDict

from graftpunk.exceptions import InvalidSessionRecord, NewerSessionRecord
from graftpunk.graftpunk_session import GraftpunkSession
from graftpunk.session_legacy import state_from_pickle
from graftpunk.session_record import (
    FIELDS,
    PICKLE_ONLY,
    RECORD_VERSION,
    LegacyBytes,
    Record,
    Unrecordable,
    classify,
    kind_of,
    record_to_state,
    state_to_record,
)
from graftpunk.session_riders import CachedToken
from tests.unit.session_compat_cases import (
    RECORDABLE,
    VERSIONS,
    assert_leaf_imports,
    case_id,
    case_named,
    cases,
    cookie_dicts,
    decrypt,
    iter_cases,
    nodriver_session,
    nodriver_state,
    record_bytes,
)


def test_session_record_is_a_leaf():
    assert_leaf_imports(
        "graftpunk.session_record",
        {
            "graftpunk.session_riders",
            "graftpunk.cookies",
            "graftpunk.exceptions",
            "graftpunk.logging",
        },
    )


def test_exceptions_still_imports_nothing_from_graftpunk():
    assert_leaf_imports("graftpunk.exceptions", set())


def test_record_version_is_one():
    assert RECORD_VERSION == 1


# The real __getstate__ of each class, never a hand-built dict.
LIVE = [
    ("browser", nodriver_session(tokens=True).__getstate__()),
    ("api", GraftpunkSession().__getstate__()),
    ("requests", requests.Session().__getstate__()),
]


@pytest.mark.parametrize(("kind", "state"), LIVE, ids=[k for k, _ in LIVE])
def test_every_key_getstate_emits_has_a_row(kind, state):
    known = {f.state_key for f in FIELDS if kind in f.kinds} | {
        p.state_key for p in PICKLE_ONLY if kind in p.kinds
    }
    assert set(state) <= known


@pytest.mark.parametrize(("kind", "state"), LIVE, ids=[k for k, _ in LIVE])
def test_a_live_state_is_recordable(kind, state):
    assert isinstance(state_to_record(kind, state), bytes)


def test_no_state_key_has_two_rows_for_one_kind():
    rows = [(f.state_key, k) for f in FIELDS for k in f.kinds]
    rows += [(p.state_key, k) for p in PICKLE_ONLY for k in p.kinds]
    assert len(rows) == len(set(rows))


@pytest.mark.parametrize("case", iter_cases(), ids=case_id)
def test_each_fixture_is_recordable_or_not_as_declared(case):
    legacy = state_from_pickle(decrypt(case))
    recordable = legacy.kind is not None and isinstance(
        state_to_record(legacy.kind, legacy.state), bytes
    )
    assert recordable is RECORDABLE[case.case]


def test_unrecordable_fixtures_per_version():
    # The count the commit body reports: adapter, selenium and subclass in each.
    def recordable(case: Any) -> bool:
        legacy = state_from_pickle(decrypt(case))
        return legacy.kind is not None and isinstance(
            state_to_record(legacy.kind, legacy.state), bytes
        )

    counts = Counter(c.version for c in iter_cases() if not recordable(c))
    assert counts == dict.fromkeys(VERSIONS, 3)


@pytest.mark.parametrize("case", ["nodriver", "requests", "api"])
def test_a_jar_with_httpies_policy_is_recordable_without_httpie(case, no_browser_stack):
    # 1.21.0 pickled HTTPie's policy class; without HTTPie the legacy reader
    # stands a placeholder in for it.
    fixture = case_named("1.21.0", case)
    with no_browser_stack():
        for name in [m for m in sys.modules if m.startswith("httpie.")]:
            del sys.modules[name]
        legacy = state_from_pickle(decrypt(fixture))
        assert legacy.unresolved == ("httpie.cookies.HTTPieCookiePolicy",)
        assert isinstance(state_to_record(legacy.kind, legacy.state), bytes)


@pytest.mark.parametrize("case", cases(recordable=True), ids=case_id)
def test_state_survives_the_record(case):
    legacy = state_from_pickle(decrypt(case))
    back = record_to_state(classify(state_to_record(legacy.kind, legacy.state)))
    stored = {f.state_key for f in FIELDS if legacy.kind in f.kinds}
    assert cookie_dicts(back["cookies"]) == cookie_dicts(legacy.state["cookies"])
    assert {k: back[k] for k in stored - {"cookies"} if k in legacy.state} == {
        k: legacy.state[k] for k in stored - {"cookies"} if k in legacy.state
    }
    assert all(p.at_default(back[p.state_key]) for p in PICKLE_ONLY if legacy.kind in p.kinds)


@pytest.mark.parametrize("case", cases(recordable=True), ids=case_id)
def test_a_decoded_state_has_the_shapes_getstate_writes(case):
    legacy = state_from_pickle(decrypt(case))
    back = record_to_state(classify(state_to_record(legacy.kind, legacy.state)))
    assert type(back["headers"]) is type(legacy.state["headers"])
    assert type(back["cookies"]) is requests.cookies.RequestsCookieJar
    if "auth" in legacy.state:
        assert back["auth"] == legacy.state["auth"]


@pytest.mark.parametrize(("case", "key"), [("adapter", "adapters"), ("selenium", "_backend_type")])
def test_what_a_record_cannot_hold_is_refused_by_name(case, key):
    legacy = state_from_pickle(decrypt(case_named("1.44.0", case)))
    refused = state_to_record(legacy.kind, legacy.state)
    assert isinstance(refused, Unrecordable)
    assert refused.key == key


@pytest.mark.parametrize("cls", [requests.Session, GraftpunkSession])
def test_each_default_predicate_holds_for_the_installed_libraries(cls):
    state = cls().__getstate__()
    assert all(p.at_default(state[p.state_key]) for p in PICKLE_ONLY if p.state_key in state)


def test_a_retry_without_other_is_at_default():
    # urllib3 before 1.26 (requests 2.28.0 allows 1.21.1 and later) has no
    # Retry.other; checked against urllib3 1.25.11 on 2026-11-25.
    state = requests.Session().__getstate__()
    for adapter in state["adapters"].values():
        fields = {k: v for k, v in vars(adapter.max_retries).items() if k != "other"}
        adapter.max_retries = type("Retry", (), fields)()
    assert isinstance(state_to_record("requests", state), bytes)


@pytest.mark.parametrize("row", PICKLE_ONLY, ids=lambda p: p.state_key)
def test_each_pickle_only_default_passes_its_own_predicate(row):
    assert row.at_default(row.default())


def _hooked(state: dict[str, Any]) -> dict[str, Any]:
    return state | {"hooks": {"response": [print]}}


def _retrying(state: dict[str, Any]) -> dict[str, Any]:
    adapters = dict(state["adapters"])
    adapters["https://"] = requests.adapters.HTTPAdapter(max_retries=3)
    return state | {"adapters": adapters}


def _big_pool(state: dict[str, Any]) -> dict[str, Any]:
    adapters = dict(state["adapters"])
    adapters["https://"] = requests.adapters.HTTPAdapter(pool_maxsize=50)
    return state | {"adapters": adapters}


def _extra_mount(state: dict[str, Any]) -> dict[str, Any]:
    return state | {"adapters": {**state["adapters"], "https://idp.example.com": object()}}


class _AdapterSubclass(requests.adapters.HTTPAdapter):
    pass


def _subclassed_adapter(state: dict[str, Any]) -> dict[str, Any]:
    return state | {"adapters": {**state["adapters"], "http://": _AdapterSubclass()}}


def _uninspectable_adapter(state: dict[str, Any]) -> dict[str, Any]:
    stub = type("HTTPAdapter", (), {"__module__": "requests.adapters"})()
    return state | {"adapters": {**state["adapters"], "http://": stub}}


@pytest.mark.parametrize(
    ("mutate", "key"),
    [
        (_hooked, "hooks"),
        (_retrying, "adapters"),
        (_big_pool, "adapters"),
        (_extra_mount, "adapters"),
        (_subclassed_adapter, "adapters"),
        (_uninspectable_adapter, "adapters"),
    ],
)
def test_a_pickle_only_value_away_from_its_default_is_refused_by_name(mutate, key):
    refused = state_to_record("requests", mutate(requests.Session().__getstate__()))
    assert isinstance(refused, Unrecordable)
    assert refused.key == key


def test_a_restored_requests_state_makes_a_working_session(echo_server):
    obj = requests.Session.__new__(requests.Session)
    state = requests.Session().__getstate__()
    obj.__setstate__(record_to_state(classify(state_to_record("requests", state))))
    # needs adapters and hooks back
    assert obj.get(echo_server.url).status_code == 200


def test_a_newer_record_is_refused_by_version():
    with pytest.raises(NewerSessionRecord, match="2") as exc:
        classify(record_bytes(version=2))
    assert exc.value.version == 2
    assert "upgrade graftpunk" in str(exc.value).lower()


def _record_dict(**changes: Any) -> bytes:
    record = json.loads(record_bytes())
    record.update(changes)
    return json.dumps(record).encode()


def _with_huge_int(place: Callable[[dict[str, Any]], dict[str, Any]], key: str) -> bytes:
    # An integer too large for a float (10**400), which json.loads reads as an int.
    record = json.loads(record_bytes())
    place(record)[key] = "HUGE"
    payload = json.dumps(record).encode()
    assert payload.count(b'"HUGE"') == 1
    return payload.replace(b'"HUGE"', b"1" + b"0" * 400)


def _huge_cookie_expires() -> bytes:
    return _with_huge_int(lambda r: r["cookies"][0], "expires")


def _huge_token_ttl() -> bytes:
    return _with_huge_int(lambda r: r["riders"]["cached_tokens"]["csrf"], "ttl")


def _huge_token_extracted_at() -> bytes:
    return _with_huge_int(lambda r: r["riders"]["cached_tokens"]["csrf"], "extracted_at")


def _cookie_without_name() -> bytes:
    record = json.loads(record_bytes())
    del record["cookies"][0]["name"]
    return json.dumps(record).encode()


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b"{not json",
        b'{"graftpunk_session": 1, "kind": "browser"',
        record_bytes(drop=("kind",)),
        record_bytes(kind="nope"),
        record_bytes(drop=("cookies",)),
        record_bytes(drop=("graftpunk_session",)),
        record_bytes(version=0),
        _record_dict(graftpunk_session=True),
        _record_dict(graftpunk_session="1"),
        _cookie_without_name(),
        _record_dict(headers={"A": "1"}),
        _record_dict(backend_type="selenium"),
        _record_dict(riders={"cached_tokens": {"x": {"name": "x", "value": "v"}}}),
        _record_dict(riders={"unknown_rider": 1}),
        _record_dict(auth=None),
        record_bytes("requests", drop=("requests",)),
        _record_dict(kind=["browser"]),
        _record_dict(kind={"browser": 1}),
        _huge_cookie_expires(),
        _huge_token_ttl(),
        _huge_token_extracted_at(),
    ],
    ids=[
        "array",
        "not-json",
        "truncated",
        "no-kind",
        "unknown-kind",
        "no-cookies",
        "no-version",
        "version-0",
        "version-bool",
        "version-str",
        "cookie-without-name",
        "headers-not-pairs",
        "selenium-backend",
        "token-missing-fields",
        "unknown-rider",
        "key-of-another-kind",
        "requests-without-requests-block",
        "kind-array",
        "kind-object",
        "huge-cookie-expires",
        "huge-token-ttl",
        "huge-token-extracted-at",
    ],
)
def test_json_that_is_not_a_record_is_invalid(payload):
    with pytest.raises(InvalidSessionRecord):
        classify(payload)


def _with_token(state: dict[str, Any], **changes: Any) -> dict[str, Any]:
    tokens = dict(state["_gp_cached_tokens"])
    old = tokens["csrf"]
    fields = {"name": old.name, "value": old.value, "extracted_at": old.extracted_at}
    tokens["csrf"] = CachedToken(**(fields | {"ttl": old.ttl} | changes))
    return state | {"_gp_cached_tokens": tokens}


def set_token_ttl_to_inf(state: dict[str, Any]) -> dict[str, Any]:
    return _with_token(state, ttl=math.inf)


def set_token_extracted_at_to_nan(state: dict[str, Any]) -> dict[str, Any]:
    return _with_token(state, extracted_at=math.nan)


def set_a_bytes_header_value(state: dict[str, Any]) -> dict[str, Any]:
    return state | {"headers": {**state["headers"], "X-Raw": b"\x00"}}


class _ProgramPolicy(cookiejar.DefaultCookiePolicy):
    pass


def set_a_custom_cookie_policy(state: dict[str, Any]) -> dict[str, Any]:
    state["cookies"].set_policy(_ProgramPolicy())
    return state


def set_a_cookie_policy_with_blocked_domains(state: dict[str, Any]) -> dict[str, Any]:
    state["cookies"].set_policy(cookiejar.DefaultCookiePolicy(blocked_domains=["idp.example.com"]))
    return state


def set_a_non_string_header_role(state: dict[str, Any]) -> dict[str, Any]:
    return state | {"_gp_header_roles": {"xhr": {"X-Count": 1}}}


UNHOLDABLE: list[tuple[Callable[[dict[str, Any]], dict[str, Any]], str]] = [
    (set_token_ttl_to_inf, "_gp_cached_tokens"),
    (set_token_extracted_at_to_nan, "_gp_cached_tokens"),
    (set_a_bytes_header_value, "headers"),
    (set_a_custom_cookie_policy, "cookies"),
    (set_a_cookie_policy_with_blocked_domains, "cookies"),
    (set_a_non_string_header_role, "_gp_header_roles"),
]


@pytest.mark.parametrize(("mutate", "key"), UNHOLDABLE, ids=[m.__name__ for m, _ in UNHOLDABLE])
def test_values_a_record_cannot_hold_make_the_session_unrecordable(mutate, key):
    refused = state_to_record("browser", mutate(nodriver_state(tokens=True)))
    assert isinstance(refused, Unrecordable)
    assert refused.key == key


def test_an_auth_object_makes_a_requests_session_unrecordable():
    state = requests.Session().__getstate__() | {"auth": object()}
    refused = state_to_record("requests", state)
    assert isinstance(refused, Unrecordable)
    assert refused.key == "auth"


def test_an_auth_pair_comes_back_a_tuple():
    state = requests.Session().__getstate__() | {"auth": ("alice", "secret")}
    assert record_to_state(classify(state_to_record("requests", state)))["auth"] == (
        "alice",
        "secret",
    )


def test_a_params_value_json_would_change_makes_the_session_unrecordable():
    state = requests.Session().__getstate__() | {"params": {"q": ("a", "b")}}
    refused = state_to_record("requests", state)
    assert isinstance(refused, Unrecordable)
    assert refused.key == "params"


def test_params_nested_to_the_limit_round_trip_and_one_deeper_are_unrecordable():
    from graftpunk.session_record import MAX_DEPTH
    from tests.unit.session_compat_cases import nested

    # The params object sits two levels below the record's top.
    deepest = requests.Session().__getstate__() | {"params": nested(MAX_DEPTH - 2)}
    restored = record_to_state(classify(state_to_record("requests", deepest)))
    assert restored["params"] == deepest["params"]
    for depth in (MAX_DEPTH - 1, 5000):  # 5000 is past what json.dumps can recurse
        state = requests.Session().__getstate__() | {"params": nested(depth)}
        refused = state_to_record("requests", state)
        assert isinstance(refused, Unrecordable), depth
        assert refused.key == "params"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "deep"], ids=["nan", "inf", "deep"])
def test_an_extra_key_a_reader_would_refuse_is_unrecordable(value):
    from graftpunk.session_record import MAX_DEPTH
    from tests.unit.session_compat_cases import nested

    extra = {"x": nested(MAX_DEPTH) if value == "deep" else value}
    refused = state_to_record("browser", nodriver_state(), extra)
    assert isinstance(refused, Unrecordable)
    assert refused.key == "extra"


def test_classify_takes_a_record_nested_to_the_limit_and_refuses_one_deeper():
    from graftpunk.session_record import MAX_DEPTH
    from tests.unit.session_compat_cases import nested

    assert classify(record_bytes(extra={"x": nested(MAX_DEPTH - 1)})).extra
    with pytest.raises(InvalidSessionRecord, match="nested more than"):
        classify(record_bytes(extra={"x": nested(MAX_DEPTH)}))


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e400", "-1e400"])
def test_classify_refuses_a_non_finite_number(literal):
    payload = record_bytes()[:-1] + b', "x": ' + literal.encode() + b"}"
    with pytest.raises(InvalidSessionRecord, match=literal):
        classify(payload)


def test_cookies_requests_builds_round_trip_with_all_17_arguments():
    from tests.unit.session_compat_cases import cookies_requests_builds

    jar = requests.cookies.RequestsCookieJar()
    for cookie in cookies_requests_builds():
        jar.set_cookie(cookie)
    state = requests.Session().__getstate__() | {"cookies": jar}
    record = state_to_record("requests", state)
    assert isinstance(record, bytes), record
    assert cookie_dicts(record_to_state(classify(record))["cookies"]) == cookie_dicts(jar)


def test_a_state_key_with_no_row_is_unrecordable_by_name():
    refused = state_to_record("browser", nodriver_state() | {"_gp_csrf_tokens": {}})
    assert isinstance(refused, Unrecordable)
    assert refused.key == "_gp_csrf_tokens"


def test_a_state_missing_a_stored_key_is_unrecordable_by_name():
    state = nodriver_state()
    del state["cookies"]
    refused = state_to_record("browser", state)
    assert isinstance(refused, Unrecordable)
    assert refused.key == "cookies"


def test_a_state_missing_a_rider_gives_a_record_without_it():
    state = nodriver_state()
    del state["_gp_header_role_origins"]
    assert "_gp_header_role_origins" not in record_to_state(
        classify(state_to_record("browser", state))
    )


def test_an_unknown_kind_is_a_programming_error():
    with pytest.raises(ValueError, match="nope"):
        state_to_record("nope", {})


def test_an_extra_key_that_the_table_owns_is_a_programming_error():
    with pytest.raises(ValueError, match="cookies"):
        state_to_record("browser", nodriver_state(), {"cookies": []})


def test_an_empty_session_round_trips():
    state = nodriver_state() | {"headers": {}, "cookies": requests.cookies.RequestsCookieJar()}
    back = record_to_state(classify(state_to_record("browser", state)))
    assert back["headers"] == {}
    assert cookie_dicts(back["cookies"]) == []


def test_both_header_shapes_give_the_same_record():
    a = nodriver_state() | {"headers": {"A": "1"}}
    b = nodriver_state() | {"headers": CaseInsensitiveDict({"A": "1"})}
    assert state_to_record("browser", a) == state_to_record("browser", b)


def test_headers_keep_their_order_and_case():
    headers = {"X-B": "2", "x-a": "1"}
    back = record_to_state(
        classify(state_to_record("browser", nodriver_state() | {"headers": headers}))
    )
    assert list(back["headers"].items()) == [("X-B", "2"), ("x-a", "1")]


def test_a_record_built_by_hand_decodes_for_each_kind():
    for kind in ("browser", "api", "requests"):
        rec = classify(record_bytes(kind))
        assert isinstance(rec, Record)
        assert rec.version == 1
        assert rec.kind == kind
        assert rec.extra == {}
        assert [c["name"] for c in cookie_dicts(record_to_state(rec)["cookies"])] == ["pref", "sid"]


def test_the_codec_writes_what_a_record_built_by_hand_holds():
    rec = classify(record_bytes())
    assert json.loads(state_to_record(rec.kind, record_to_state(rec))) == json.loads(record_bytes())


def test_unknown_top_level_keys_come_back_in_extra_and_go_out_unchanged():
    rec = classify(record_bytes(extra={"future": {"a": 1}}))
    assert rec.extra == {"future": {"a": 1}}
    assert json.loads(state_to_record(rec.kind, record_to_state(rec), rec.extra))["future"] == {
        "a": 1
    }


class RunsOnUnpickle:
    def __init__(self, marker: Path) -> None:
        self.marker = marker

    def __reduce__(self) -> tuple[Any, ...]:
        return (Path.touch, (self.marker,))


def test_a_pickle_is_never_unpickled_by_classify(tmp_path):
    marker = tmp_path / "ran"
    payload = dill.dumps(RunsOnUnpickle(marker))
    result = classify(payload)
    assert isinstance(result, LegacyBytes)
    assert result.data == payload
    assert not marker.exists()


def test_a_record_holding_pickle_bytes_as_a_string_is_data():
    rec = classify(record_bytes(cookie_value="\x80\x04cos\nsystem\n"))
    cookies = cookie_dicts(record_to_state(rec)["cookies"])
    assert [c["value"] for c in cookies if c["name"] == "sid"] == ["\x80\x04cos\nsystem\n"]


def test_record_to_state_gives_fresh_objects_each_call():
    rec = classify(record_bytes("requests"))
    first, second = record_to_state(rec), record_to_state(rec)
    assert first["cookies"] is not second["cookies"]
    assert first["adapters"]["https://"] is not second["adapters"]["https://"]


@pytest.mark.parametrize(
    ("session", "kind"),
    [
        (requests.Session(), "requests"),
        (GraftpunkSession(), "api"),
        (nodriver_session(), "browser"),
        (type("Session", (requests.Session,), {})(), None),
        (object(), None),
    ],
    ids=["requests", "api", "browser", "subclass-named-session", "object"],
)
def test_kind_of_matches_the_exact_class(session, kind):
    assert kind_of(session) == kind


# Values of every JSON type, plus an integer too large for a float.
_WRONG_VALUES: tuple[Any, ...] = (
    None,
    True,
    0,
    -1,
    10**400,
    1.5,
    "",
    "x",
    [],
    ["a"],
    [["a", "b", "c"]],
    {},
    {"a": 1},
    {"a": {"b": []}},
)


def _paths(node: Any, path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    found = [path] if path else []
    if isinstance(node, dict):
        children = list(node.items())
    elif isinstance(node, list):
        children = list(enumerate(node))
    else:
        children = []
    for key, child in children:
        found += _paths(child, (*path, key))
    return found


def _replaced(record: Any, path: tuple[Any, ...], value: Any) -> Any:
    changed = json.loads(json.dumps(record))
    node = changed
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return changed


@pytest.mark.parametrize("kind", ["browser", "api", "requests"])
def test_every_malformed_value_is_an_invalid_record_or_decodes(kind):
    # Each node of a hand-built record, replaced by each value above: classify
    # raises InvalidSessionRecord or gives a record that record_to_state decodes.
    record = json.loads(record_bytes(kind))
    escaped = []
    for path in _paths(record):
        for value in _WRONG_VALUES:
            payload = json.dumps(_replaced(record, path, value)).encode()
            try:
                record_to_state(classify(payload))
            except InvalidSessionRecord:
                pass
            except NewerSessionRecord:
                if path != ("graftpunk_session",):
                    escaped.append(f"{path} = {value!r:.40}: NewerSessionRecord")
            except Exception as exc:  # noqa: BLE001 - collected and reported below
                escaped.append(f"{path} = {value!r:.40}: {type(exc).__name__}: {exc}")
    assert escaped == []
