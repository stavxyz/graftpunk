"""graftpunk helpers the graft skill's fixture-leaks script copies, held equal to
it where the two must agree. The fixtures check itself is tested in
test_graftpunk_testing.py."""

from __future__ import annotations

import importlib.util
import json
import random
import re
from pathlib import Path
from types import ModuleType

import pytest

from graftpunk.har.digest import DigestSource, digest, flagged_values_of
from graftpunk.har.parser import parse_har_file
from graftpunk.testing import decoding, recorded
from graftpunk.testing.decoding import combined_forms, decoded_forms, value_forms
from graftpunk.testing.recorded import RunUnreadableError, recorded_leaks, recorded_values
from tests.unit.guide_harness import REPO_ROOT
from tests.unit.test_graftpunk_testing import _containment_run

SCRIPT = REPO_ROOT / "skills" / "graft" / "scripts" / "fixture-leaks.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fixture_leaks", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "text",
    [
        "plain text",
        '{"url": "/x?a=1\\u0026csrftoken=abc", "s": "\\ud83d\\ude00 \\/"}',
        "var u = '/x?a=1\\x26csrftoken=abc';",
        '{"raw": "Host: x\\r\\nmyshop_session=abc\\tz\\b\\f"}',
        '<a href="/x?a=1&amp;csrftoken=abc&#38;b=&#x27;">',
        "next=a%253D1%2526csrftoken%253Dabc",
        "a%25252525",
        "q=running+shoes&x=%E2%82%AC",
        "",
    ],
)
def test_the_skill_script_reads_a_fixture_in_the_same_forms(text: str) -> None:
    """The script runs on the standard library alone and cannot import graftpunk,
    so the two decoders are held equal here."""
    assert _load_script()._escape_decodings(text) == decoded_forms(text)


def test_percent_decoding_stops_after_three_rounds() -> None:
    assert decoded_forms("a%25252541")[2:] == ["a%252541", "a%2541", "a%41"]


@pytest.mark.parametrize(
    "text",
    [
        "plain text",
        '<div data-x="{&quot;t&quot;:&quot;a&amp;b\\/cdefgh&quot;}">',
        "next=a%253D1%2526csrftoken%253Dabc&amp;x=%5Cu0041",
        '{"u": "/x?a=1\\u0026amp;b=%2522q%2522"}',
        "",
    ],
)
def test_the_skill_script_combines_decodings_as_gp_observe_fixtures_does(text: str) -> None:
    assert _load_script()._combined_decodings(text) == combined_forms(text)


@pytest.mark.parametrize(
    "value",
    ['"Xk3Lp9"', "%22Xk3Lp9%22", "Xk3+Lp9%2F", '%22a+b"', '""', '"', "plain", ""],
)
def test_the_skill_script_looks_for_a_value_in_the_forms_gp_observe_fixtures_does(
    value: str,
) -> None:
    assert _load_script()._value_forms(value) == value_forms(value)


def _page(url: str, headers: list[dict], content: dict) -> dict:
    return {
        "startedDateTime": "2026-09-10T10:00:00.000Z",
        "time": 1,
        "request": {"method": "GET", "url": url, "headers": [], "cookies": []},
        "response": {
            "status": 200,
            "statusText": "OK",
            "headers": headers,
            "cookies": [],
            "content": content,
        },
    }


def test_the_skill_script_reads_every_value_gp_observe_fixtures_reads(tmp_path: Path) -> None:
    """``fixture-leaks.py --har`` reads at least the values the package compares
    with a capture, on a run with a body kept in bodies/, a page source, the
    Content-Type header in lower case, a Cookie header, a Set-Cookie header, and a
    header token sent in two cases. The package knows a cookie's values only;
    every token name stays checked. The script cannot import graftpunk, so the
    two readers are held together here."""
    html = [{"name": "content-type", "value": "text/html"}]
    kept = _page(
        "https://myshop.example.com/account",
        html,
        {"mimeType": "text/html", "_bodyFile": "bodies/1.html"},
    )
    (tmp_path / "bodies").mkdir()
    (tmp_path / "bodies" / "1.html").write_text(
        '<form><input type="hidden" name="csrfmiddlewaretoken" value="Hk4Rt8Wz3Ny6"></form>'
    )
    inline = _page(
        "https://myshop.example.com/cart",
        [*html, {"name": "Set-Cookie", "value": "sessionid=SetByHeader88; HttpOnly"}],
        {"mimeType": "text/html", "text": '<meta name="csrf-token" content="OldTok9Qv2Rt7">'},
    )
    inline["response"]["cookies"] = [{"name": "sessionid", "value": "FromArray66"}]
    inline["request"]["headers"] = [
        {"name": "Cookie", "value": "sessionid=FromHeader77; id=Xk3Lp9Qv2Rt7"},
        {"name": "x-csrf-token", "value": "Wq3Lk6Pd9Xc2"},
    ]
    api = _page("https://myshop.example.com/api/cart", [], {"mimeType": "application/json"})
    api["request"]["headers"] = [{"name": "X-CSRF-Token", "value": "Hd5Ks0Gf2Lq8"}]
    har = tmp_path / "network.har"
    har.write_text(json.dumps({"log": {"version": "1.2", "entries": [kept, inline, api]}}))
    (tmp_path / "page-source.html").write_text('<meta name="csrf-token" content="NewTok3Lp9">')

    result = digest(DigestSource.from_run_dir(tmp_path, session="myshop", run_id="run-1"))
    package = flagged_values_of(result, parse_har_file(har).entries)
    known = {name: values for name, values in package.items() if values is not None}
    assert known == {
        "id": frozenset({"Xk3Lp9Qv2Rt7"}),
        "sessionid": frozenset({"FromArray66", "FromHeader77", "SetByHeader88"}),
    }
    assert {name for name, values in package.items() if values is None} == {
        "X-CSRF-Token",
        "csrf-token",
        "csrfmiddlewaretoken",
        "x-csrf-token",
    }
    script = _load_script().recorded_values(json.loads(har.read_text()), set(package), tmp_path)
    for name, values in known.items():
        assert values <= script.get(name, set()), name
    # The script reports on the token values too, though the suite never drops
    # those names.
    assert script["csrf-token"] == {"OldTok9Qv2Rt7", "NewTok3Lp9"}
    assert script["csrfmiddlewaretoken"] == {"Hk4Rt8Wz3Ny6"}
    assert script["X-CSRF-Token"] == {"Hd5Ks0Gf2Lq8", "Wq3Lk6Pd9Xc2"}


def _run_with_every_source(tmp_path: Path) -> object:
    """A run whose HAR records flagged values in every place ``--har`` reads: a body
    kept in bodies/, a page source, a Content-Type header in lower case, cookie
    arrays, Cookie and Set-Cookie headers, a header token in two cases, a request
    body in postData and one kept in a file, a DOM snapshot, and a snapshot
    reference that climbs out of the run, which is not read."""
    html = [{"name": "content-type", "value": "text/html"}]
    kept = _page(
        "https://myshop.example.com/account",
        html,
        {"mimeType": "text/html", "_bodyFile": "bodies/1.html"},
    )
    (tmp_path / "bodies").mkdir()
    (tmp_path / "bodies" / "1.html").write_text(
        '<form><input type="hidden" name="csrfmiddlewaretoken" value="Hk4Rt8Wz3Ny6"></form>'
    )
    kept["request"]["_domSnapshot"] = "bodies/snap.html"
    kept["request"]["_domAfter"] = "../outside.html"
    (tmp_path / "bodies" / "snap.html").write_text('<meta name="csrf-token" content="Snap5Qv2Rt7">')
    (tmp_path.parent / "outside.html").write_text('<meta name="csrf-token" content="Outside9">')
    inline = _page(
        "https://myshop.example.com/cart",
        [*html, {"name": "Set-Cookie", "value": "sessionid=SetByHeader88; HttpOnly"}],
        {"mimeType": "text/html", "text": '<meta name="csrf-token" content="OldTok9Qv2Rt7">'},
    )
    inline["response"]["cookies"] = [{"name": "sessionid", "value": "FromArray66"}]
    inline["request"]["headers"] = [
        {"name": "Cookie", "value": "sessionid=FromHeader77; id=Xk3Lp9Qv2Rt7"},
        {"name": "x-csrf-token", "value": "Wq3Lk6Pd9Xc2"},
    ]
    api = _page("https://myshop.example.com/api/cart", [], {"mimeType": "application/json"})
    api["request"]["headers"] = [{"name": "X-CSRF-Token", "value": "Hd5Ks0Gf2Lq8"}]
    api["request"]["postData"] = {"text": '{"login": {"password_token": "Pw8Tk2Lm5Qx"}}'}
    form = _page("https://myshop.example.com/api/form", [], {"mimeType": "application/json"})
    form["request"]["_postDataFile"] = "bodies/post.txt"
    (tmp_path / "bodies" / "post.txt").write_text("csrfmiddlewaretoken=Fm3Kp7Lq2Wz&q=shoes")
    (tmp_path / "page-source.html").write_text('<meta name="csrf-token" content="NewTok3Lp9">')
    return {"log": {"version": "1.2", "entries": [kept, inline, api, form]}}


_FLAGGED = {
    "X-CSRF-Token",
    "csrf-token",
    "csrfmiddlewaretoken",
    "id",
    "password_token",
    "sessionid",
    "x-csrf-token",
}


def test_the_package_reads_every_value_the_skill_script_reads(tmp_path: Path) -> None:
    """graftpunk.testing.recorded is the package's copy of ``fixture-leaks.py
    --har``; the script cannot import graftpunk, so the two readers are held to
    the same output here."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    har = _run_with_every_source(run_dir)
    package = recorded_values(har, _FLAGGED, run_dir)
    assert package == _load_script().recorded_values(har, set(_FLAGGED), run_dir)
    assert package["csrf-token"] == {"OldTok9Qv2Rt7", "NewTok3Lp9", "Snap5Qv2Rt7"}
    assert package["csrfmiddlewaretoken"] == {"Hk4Rt8Wz3Ny6", "Fm3Kp7Lq2Wz"}
    assert package["password_token"] == {"Pw8Tk2Lm5Qx"}
    assert package["sessionid"] == {"FromArray66", "FromHeader77", "SetByHeader88"}


@pytest.mark.parametrize(
    "fixture",
    [
        '{"id": 7, "sessionid": "FromHeader77"}',
        '{\n  "a": 1,\n  "b": "x FromArray66 y",\n  "c": "FromArray66"\n}',
        '{"u": "/x?t=Wq3Lk6Pd9Xc2"}',
        '{"u": "/x?t=Wq%33Lk6Pd9Xc2"}',
        '<div data-x="{&quot;t&quot;:&quot;Hd5Ks0Gf2Lq8&quot;}">',
        '{"s": "\\u0048d5Ks0Gf2Lq8"}',
        "\n".join(f"line {n} OldTok9Qv2Rt7" for n in range(8)),
        '{"id": "Xk3Lp9", "short": "Xk"}',
        '{"nothing": "recorded"}',
        "",
    ],
    ids=[
        "json",
        "lines",
        "url",
        "percent",
        "html-escaped-json",
        "unicode-escape",
        "many-lines",
        "partial",
        "clean",
        "empty",
    ],
)
def test_the_package_reports_a_leak_as_the_skill_script_does(tmp_path: Path, fixture: str) -> None:
    script = _load_script()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    values = recorded_values(_run_with_every_source(run_dir), _FLAGGED, run_dir)
    expected = script.recorded_leaks(script._Fixture(fixture), values)
    assert recorded_leaks(fixture, values) == expected
    for line in expected:
        assert not any(value in line for found in values.values() for value in found)


@pytest.mark.parametrize("loop", ["self", "mutual"])
def test_a_symlink_loop_is_the_same_read_error_in_the_package_and_the_script(
    tmp_path: Path, loop: str
) -> None:
    """Before Python 3.13 ``Path.resolve`` raises RuntimeError on a loop; both
    copies report the file by its path in the run instead of raising it."""
    run_dir = tmp_path / "run"
    (run_dir / "bodies").mkdir(parents=True)
    html = [{"name": "Content-Type", "value": "text/html"}]
    page = _page("https://myshop.example.com/", html, {"_bodyFile": "bodies/a.html"})
    har = {"log": {"entries": [page]}}
    if loop == "self":
        (run_dir / "bodies" / "a.html").symlink_to("a.html")
    else:
        (run_dir / "bodies" / "a.html").symlink_to("b.html")
        (run_dir / "bodies" / "b.html").symlink_to("a.html")
    (run_dir / "page-source.html").symlink_to("page-source.html")
    script = _load_script()
    with pytest.raises(RunUnreadableError) as package:
        recorded_values(har, _FLAGGED, run_dir)
    with pytest.raises(script.RunUnreadableError) as copied:
        script.recorded_values(har, set(_FLAGGED), run_dir)
    assert str(package.value) == str(copied.value)
    assert str(package.value) == (
        "cannot read bodies/a.html (a symlink loop), page-source.html (a symlink loop)"
    )


# Each module-level constant and pattern of the script: the ones copied from the
# package, by the package module that holds the original, and the script's own.
# A constant added to the script must be listed here, so a new copy cannot go
# unguarded.
_COPIED_FROM = {
    "_DIGITS": recorded,
    "_IDENTIFIER": recorded,
    "_SCHEMA_KEY": recorded,
    "_TYPENAME": recorded,
    "_CAMEL_CASE": recorded,
    "_GRAPHQL_META": recorded,
    "_NAME_WORD": recorded,
    "_QUOTED_KEY": recorded,
    "_BEFORE": recorded,
    "_AFTER": recorded,
    "_MOST_PLACES": recorded,
    "_MOST_FILES": recorded,
    "PAGE_SOURCE_NAME": recorded,
    "_MIN_RECORDED_LENGTH": recorded,
    "_MIN_WITH_DIGIT_LENGTH": recorded,
    "_LITERALS": recorded,
    "_UNICODE_ESCAPE": decoding,
    "_SURROGATE_PAIR": decoding,
    "_HEX_ESCAPE": decoding,
    "_CONTROL_ESCAPE": decoding,
    "_CONTROL_CHARACTERS": decoding,
    "_PERCENT_ROUNDS": decoding,
}
_SCRIPTS_OWN = {
    "_WORD",
    "_PROVENANCE",
    "_RUN_LABEL",
    "_DATE_TIME_DIGITS",
    "_NON_ASCII",
    "_CONTEXT",
    "_LONGEST_MATCH",
    "_MOST_VALUES",
    "_MOST_NAMED",
    "_OPEN",
    "_CLOSE",
}


def test_every_constant_the_skill_script_copies_is_graftpunks() -> None:
    script = _load_script()
    constants = {name for name in vars(script) if re.fullmatch(r"_?[A-Z][A-Z0-9_]*", name)}
    assert constants == set(_COPIED_FROM) | _SCRIPTS_OWN
    for name, module in _COPIED_FROM.items():
        ours, theirs = getattr(script, name), getattr(module, name)
        if isinstance(theirs, re.Pattern):
            assert (ours.pattern, ours.flags) == (theirs.pattern, theirs.flags), name
        else:
            assert ours == theirs, name


_PASTED_FIXTURE = (
    '{"user": "alice", "name": "John Smith", "pin": "1234", "id": 48213,'
    ' "password": "hunter12", "card": "4111 1111 1111 1111",'
    ' "t": "Zq9XkLmPwR4t7Bv2Hn8Ks3Jd", "active": true, "gone": null, "tab": "ab",'
    ' "short": ["a1", "USD", "Ann", "482", "A12"]}'
)


@pytest.mark.parametrize(
    ("recorded_value", "reported"),
    [
        ("alice", True),
        ("John Smith", True),
        ("1234", True),
        ("48213", True),
        ("hunter12", True),
        ("4111 1111 1111 1111", True),
        ("Zq9XkLmPwR4t7Bv2Hn8Ks3Jd", True),
        ("true", False),
        ("TRUE", False),
        ("%22null%22", False),
        ("null", False),
        ("", False),
        ("ab", False),
        ("a1", False),
        ("USD", False),
        ("Ann", False),
        ("482", True),
        ("A12", True),
    ],
)
def test_a_recorded_value_is_looked_for_unless_empty_a_literal_or_short(
    recorded_value: str, reported: bool
) -> None:
    """Only an empty value, ``true``, ``false`` or ``null`` in any case, and a
    value shorter than four characters unless it has three that hold a digit, are
    skipped, in both copies: a username, a name, a PIN, a short id, a password
    and a card number pasted into a fixture are each reported."""
    script = _load_script()
    values = {"logged_in_session": {recorded_value}}
    package = recorded_leaks(_PASTED_FIXTURE, values)
    assert package == script.recorded_leaks(script._Fixture(_PASTED_FIXTURE), values)
    assert bool(package) is reported


@pytest.mark.parametrize("missing", ["deleted", "dangling", "no-bodies-dir"])
@pytest.mark.parametrize("ref", ["_bodyFile", "_postDataFile", "_domSnapshot"])
def test_a_file_the_har_names_that_is_missing_is_the_same_read_error_in_both_copies(
    tmp_path: Path, missing: str, ref: str
) -> None:
    """gp names a file in the HAR only after writing it, so a named file that is
    gone (deleted, a dangling symlink, or its directory removed) fails the check
    by its path in the run, in both copies, and is never a quiet pass."""
    run_dir = tmp_path / "run"
    (run_dir / "bodies").mkdir(parents=True)
    html = [{"name": "Content-Type", "value": "text/html"}]
    page = _page("https://myshop.example.com/", html, {"mimeType": "text/html"})
    if ref == "_bodyFile":
        page["response"]["content"]["_bodyFile"] = "bodies/a.html"
    else:
        page["request"][ref] = "bodies/a.html"
    har = {"log": {"entries": [page]}}
    if missing == "dangling":
        (run_dir / "bodies" / "a.html").symlink_to("nothing.html")
    elif missing == "no-bodies-dir":
        (run_dir / "bodies").rmdir()
    script = _load_script()
    with pytest.raises(RunUnreadableError) as package:
        recorded_values(har, _FLAGGED, run_dir)
    with pytest.raises(script.RunUnreadableError) as copied:
        script.recorded_values(har, set(_FLAGGED), run_dir)
    assert str(package.value) == str(copied.value) == "cannot read bodies/a.html (missing)"


def test_a_run_without_a_page_source_is_read_in_both_copies(tmp_path: Path) -> None:
    """gp writes page-source.html only when the browser gave the page at stop, so
    a run without one is read whole."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    har = _run_with_every_source(run_dir)
    (run_dir / "page-source.html").unlink()
    package = recorded_values(har, _FLAGGED, run_dir)
    assert package == _load_script().recorded_values(har, set(_FLAGGED), run_dir)
    assert package["csrf-token"] == {"OldTok9Qv2Rt7", "Snap5Qv2Rt7"}


def test_the_script_reads_no_file_outside_the_run_as_the_package(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    outside = tmp_path / "outside"
    outside.mkdir()
    har = _containment_run(run_dir, outside)
    assert _load_script().recorded_values(har, {"csrf-token"}, run_dir) == {}
    assert recorded_values(har, {"csrf-token"}, run_dir) == {}


@pytest.mark.parametrize(
    "text",
    [
        '{"data": {"viewer": {"__typename": "User", "primaryEmail": "pat@example.com"}}}',
        '{"recentOrders": [{"orderId": "ORD-9000001"}], "acct_99887766": 1}',
        '{"alice@example.com": {"plan": "pro"}, "Content-Type": "x"}',
        '{"payload": "{\\"orderId\\": \\"ORD-9000001\\"}"}',
        "FIXTURE = {'orderId': 'ORD-9000001', \"customerName\" : \"Pat\"}\n",
        "<p>orderId: Pat</p>",
        'var n = ok ? "aokonkwo" : "guest";',
        'match login:\n    case "aokonkwo":\n        pass',
        '{"msg": "hello \\"aokonkwo\\": welcome", "Okonkwo": 1, "cus_Nffr": 2}',
        '{"__typename": {"email": "a@myshop.example"}, "x": {"__typename": "a b"}}',
        "not json at all",
        "",
    ],
)
def test_the_skill_script_reads_keys_and_values_as_gp_observe_fixtures_does(text: str) -> None:
    """The one rule for keys (a key shaped like a schema name, without a digit,
    in key position, is the site's schema, not a value) and for ``__typename``
    values, held equal."""
    script = _load_script()
    assert script._without_keys(text) == recorded._without_keys(text)
    assert script._schema_keys(text) == recorded._schema_keys(text)
    assert script.body_values(text) == recorded._body_values(text)


def test_both_copies_skip_graphql_names_in_tracing_errors_and_a_sort_order() -> None:
    text = json.dumps(
        {
            "data": {"viewer": {"__typename": "User", "login": "aokonkwo", "sortBy": "createdAt"}},
            "errors": [
                {
                    "message": "No sortBy on User for ann@myshop.example",
                    "extensions": {"requestedFields": ["login", "viewer"], "t": "User"},
                }
            ],
            "extensions": {"tracing": [{"fieldName": "sortBy", "id": "ORD-4471023"}]},
            "createdAt": 1,
        }
    )
    assert recorded._body_values(text) == [
        "aokonkwo",
        "No",
        "on",
        "for ann@myshop.example",
        "ORD-4471023",
        "1",
    ]
    assert _load_script().body_values(text) == recorded._body_values(text)


_FRAGMENTS = (
    "{", "}", "[", "]", ",", ", ", ":", ": ", " ", "\n", '"', "'", '\\"', "\\", "\\\\",
    "\\u00e9", "\\n", "&quot;", "%22", "?", "case ", "== ", "orderId", "created_at",
    "NEXT_DATA", "__typename", "aokonkwo", "Okonkwo", "AliceOkonkwo", "cus_Nffr9",
    "kQ7xZpLmNbVcXwErTyUi", "s3cr\\\"et!", "512", "USD", "A1", "a12", "true", "null",
    "José", "e\u0301", "OKONKWO", "AOKONKWO", "482", "14829", "a1b", "xa1bx", "errors",
    "path", "operationName", "extensions", "FIXTURE = ", "createdAt", "sortBy", "fieldName",
    "requestedFields", "tracing", "User",
)  # fmt: skip


def _random_text(rng: random.Random) -> str:
    """Text of key, quote, escape and value fragments, or a JSON document of
    random keys, values, ``__typename`` entries and GraphQL names, sometimes
    stringified inside another or written into a Python module or a script."""
    if rng.random() < 0.5:
        return "".join(rng.choice(_FRAGMENTS) for _ in range(rng.randint(0, 30)))
    words = [f for f in _FRAGMENTS if f.strip("{}[],: \n\"'\\%&;?=") == f and f]

    def node(depth: int) -> object:
        roll = rng.random()
        if depth > 2 or roll < 0.4:
            return rng.choice([*words, 7, 512, True, None])
        if roll < 0.6:
            return [node(depth + 1) for _ in range(rng.randint(0, 3))]
        return {
            rng.choice([*words, "__typename"]): node(depth + 1) for _ in range(rng.randint(0, 4))
        }

    document = node(0)
    if rng.random() < 0.3:
        # A GraphQL response or request: field and operation names held as values.
        document = {
            "data": document,
            "errors": [
                {
                    "path": [rng.choice([*words, 0]) for _ in range(rng.randint(0, 3))],
                    "message": " ".join(rng.choice([*words, "for", "no"]) for _ in range(4)),
                    "extensions": {"requestedFields": [rng.choice(words) for _ in range(2)]},
                }
            ],
            "extensions": {
                "operationName": rng.choice([*words, "a b", 1]),
                "tracing": [{"fieldName": rng.choice(words), "path": [rng.choice(words), 0]}],
            },
            "operationName": rng.choice([*words, None]),
        }
    text = json.dumps(document, indent=rng.choice([None, 2]), ensure_ascii=rng.random() < 0.5)
    roll = rng.random()
    if roll < 0.2:
        return json.dumps({"payload": text})
    if roll < 0.35:
        # Not JSON as a whole: a Python module or an inline script holding it.
        return rng.choice(["FIXTURE = ", "<script>var d = "]) + text
    return text


def test_the_two_copies_agree_on_random_keys_escapes_and_short_values() -> None:
    """Seeded, so a failure reproduces: 2500 texts, each read by both copies'
    key blanking, key places, body values and length rule, and checked against
    recorded values drawn from the same fragments."""
    script = _load_script()
    rng = random.Random(368)  # noqa: S311 - a fixed sample, not a secret
    words = [f for f in _FRAGMENTS if f.strip()]
    for _ in range(2500):
        text = _random_text(rng)
        assert script._without_keys(text) == recorded._without_keys(text), text
        assert script._schema_keys(text) == recorded._schema_keys(text), text
        assert script.body_values(text) == recorded._body_values(text), text
        values = {"sid": {rng.choice(words) for _ in range(rng.randint(1, 3))}}
        for value in values["sid"]:
            assert script._long_enough(value) == recorded._long_enough(value), value
            assert script._contains(value, text) == recorded._contains(value, text), value
        assert script._fixture_forms(text) == recorded._fixture_forms(text), text
        expected = recorded_leaks(text, values)
        assert script.recorded_leaks(script._Fixture(text), values) == expected, (text, values)


@pytest.mark.parametrize(
    "key",
    ["account", "accountNumber", "created_at", "order__id", "NEXT_DATA", "__typename", "_id"],
)
def test_a_key_shaped_like_a_schema_name_is_schema_in_both_copies(key: str) -> None:
    assert recorded._schema_key(key)
    assert _load_script()._schema_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "Okonkwo",
        "AliceOkonkwo",
        "usrID",
        "orderId2",
        "A1B2C3D4",
        "cus_NffrFeUfNV2Hib",
        "QmFzZTYyVG9rZW5YeXo",
        "Content_Type",
        "_",
    ],
)
def test_any_other_key_is_a_value_in_both_copies(key: str) -> None:
    assert not recorded._schema_key(key)
    assert not _load_script()._schema_key(key)


@pytest.mark.parametrize(
    ("fixture", "value", "where"),
    [
        ('{"sessions": {"kQ7xZpLmNbVcXwErTyUi": {"a": 1}}}', "kQ7xZpLmNbVcXwErTyUi", "line 1"),
        ('{"accounts": {\n  "aokonkwo": 1}}', "aokonkwo", "line 2"),
        ('{"balances": {"Okonkwo": 1}}', "Okonkwo", "line 1"),
        ("PREFS = {'theme': 'dark', 'aokonkwo': 1}", "aokonkwo", "line 1"),
        ('var t = x ? "kQ7xZpLmNbVcXwErTyUi" : null;', "kQ7xZpLmNbVcXwErTyUi", "line 1"),
        ('match u:\n    case "aokonkwo":\n        pass', "aokonkwo", "line 2"),
        ('{"m": "hi \\"aokonkwo\\": ok"}', "aokonkwo", "line 1"),
        ("<p>Customer 'aokonkwo': balance due</p>", "aokonkwo", "line 1"),
        ('{"pw": "s3cr\\"et!", "id": "x"}', 's3cr"et!', "found decoded"),
        ('{"pw": "s3cr\\\\et!", "id": "x"}', "s3cr\\et!", "found decoded"),
        ('{"aa": "Ren\\u00e9e Okonkwo", "bb": "x"}', "Renée Okonkwo", "found decoded"),
        ('{"pin": "482", "id": 1}', "482", "line 1"),
    ],
    ids=[
        "token-as-key",
        "username-as-key",
        "name-as-key",
        "username-as-python-key",
        "ternary",
        "case-label",
        "escaped-quotes-in-value",
        "quoted-prose",
        "escaped-quote-beside-same-length-key",
        "escaped-backslash-beside-same-length-key",
        "unicode-escape-beside-same-length-key",
        "three-digit-pin",
    ],
)
def test_a_recorded_value_used_as_a_key_or_in_key_like_text_is_reported(
    fixture: str, value: str, where: str
) -> None:
    """A value used whole as a key, a quoted value before a colon outside key
    position, and a value beside a blanked key of its length are each reported,
    in both copies."""
    script = _load_script()
    values = {"session": {value}}
    package = recorded_leaks(fixture, values)
    assert package == script.recorded_leaks(script._Fixture(fixture), values)
    assert package == [f"the recorded value of flagged 'session' is still in the fixture ({where})"]


def test_a_schema_key_and_a_typename_are_not_values() -> None:
    text = (
        '{"__typename": "User", "orderId": "ORD-9000001", "created_at": "2026",'
        ' "acct_99887766": "x", "alice@example.com": "y", "Content-Type": "z"}'
    )
    assert recorded._body_values(text) == [
        "acct_99887766",
        "alice@example.com",
        "Content-Type",
        "ORD-9000001",
        "2026",
        "x",
        "y",
        "z",
    ]
    assert recorded._without_keys(text) == (
        '{"          ": "User", "       ": "ORD-9000001", "          ": "2026",'
        ' "acct_99887766": "x", "alice@example.com": "y", "Content-Type": "z"}'
    )


_GRAPHQL_FIXTURE = (
    '{"data": {"viewer": {"__typename": "User",\n'
    '  "sessionToken": "tok_example0000",\n'
    '  "recentOrders": {"nodes": [{"__typename": "Order", "orderId": "ORD-9000001"}]}}}}'
)


@pytest.mark.parametrize(
    ("recorded_value", "where"),
    [
        ("sessionToken", "line 2"),
        ("recentOrders", "line 3"),
        ("Orders", None),
        ("Token", None),
        ("orderId", "line 3"),
        ("tok_example0000", "line 2"),
        ("ORD-9000001", "line 3"),
    ],
)
def test_a_recorded_value_is_found_as_a_value_or_a_whole_key_never_inside_a_key(
    recorded_value: str, where: str | None
) -> None:
    """A recorded value that the fixture holds only inside a key the site named is
    not reported, in both copies; one it holds as a value or as a whole key is."""
    script = _load_script()
    values = {"session": {recorded_value}}
    package = recorded_leaks(_GRAPHQL_FIXTURE, values)
    assert package == script.recorded_leaks(script._Fixture(_GRAPHQL_FIXTURE), values)
    expected = f"the recorded value of flagged 'session' is still in the fixture ({where})"
    assert package == ([expected] if where else [])


@pytest.mark.parametrize(
    ("fixture", "value", "where"),
    [
        ('{"OKONKWO": {"total": 1}}', "Okonkwo", "line 1"),
        ('{"byLogin": {\n  "AOKONKWO": 1}}', "aokonkwo", "line 2"),
        ('{"pin": "482"}', "482", "line 1"),
        ('{"total": 14829}', "482", None),
        ('{"at": "2026-11-04T10:48:22Z"}', "482", None),
        ('{"id": "xa1bx"}', "a1b", None),
        ('{"id": "x-a1b-y"}', "a1b", "line 1"),
        ("FIXTURE = {'primaryAccount': 1}\n", "Account", "line 1"),
        ('<script>var d = {"recentOrders": 2};</script>', "Orders", "line 1"),
        ('{"primaryAccount": 1}', "Account", None),
    ],
    ids=[
        "name-as-capitals-key",
        "username-as-capitals-key",
        "three-digits-alone",
        "three-digits-inside-a-number",
        "three-digits-inside-a-timestamp",
        "three-characters-inside-a-word",
        "three-characters-between-hyphens",
        "python-module-is-plain-text",
        "inline-script-is-plain-text",
        "json-key-is-blanked",
    ],
)
def test_case_boundaries_and_non_json_fixtures_are_read_alike_in_both_copies(
    fixture: str, value: str, where: str | None
) -> None:
    """A whole key matches as written or in capitals; a three-character value only
    with no letter or digit on either side; and a fixture that does not parse as
    JSON is searched as plain text, keys included."""
    script = _load_script()
    values = {"session": {value}}
    package = recorded_leaks(fixture, values)
    assert package == script.recorded_leaks(script._Fixture(fixture), values)
    expected = f"the recorded value of flagged 'session' is still in the fixture ({where})"
    assert package == ([expected] if where else [])


def test_graphql_field_and_operation_names_are_not_body_values() -> None:
    text = json.dumps(
        {
            "data": {"viewer": None},
            "errors": [{"message": "Denied", "path": ["viewer", "loyaltyAccount", 0]}],
            "extensions": {"operationName": "viewerAccount", "code": "x"},
            "operationName": "ViewerAccount",
        }
    )
    assert recorded._body_values(text) == ["Denied", "0", "x"]
    assert _load_script().body_values(text) == ["Denied", "0", "x"]
    batch = json.dumps([{"errors": [{"path": ["viewer", "not a name"]}]}])
    assert recorded._body_values(batch) == ["not a name"]
    assert _load_script().body_values(batch) == ["not a name"]
