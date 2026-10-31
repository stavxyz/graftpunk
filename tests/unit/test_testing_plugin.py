"""graftpunk helpers the graft skill's fixture-leaks script copies, held equal to
it where the two must agree. The fixtures check itself is tested in
test_graftpunk_testing.py."""

from __future__ import annotations

import importlib.util
import json
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
    "_MOST_PLACES": recorded,
    "_MOST_FILES": recorded,
    "PAGE_SOURCE_NAME": recorded,
    "_MIN_RECORDED_LENGTH": recorded,
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
    "_MIN_LENGTH",
    "_PROVENANCE",
    "_RUN_LABEL",
    "_DATE_TIME_DIGITS",
    "_BEFORE",
    "_AFTER",
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
    ' "t": "Zq9XkLmPwR4t7Bv2Hn8Ks3Jd", "active": true, "gone": null, "tab": "ab"}'
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
    ],
)
def test_a_recorded_value_is_looked_for_unless_empty_a_literal_or_short(
    recorded_value: str, reported: bool
) -> None:
    """Only an empty value, ``true``, ``false`` or ``null`` in any case, and a
    value shorter than four characters are skipped, in both copies: a username,
    a name, a PIN, a short id, a password and a card number pasted into a
    fixture are each reported."""
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
