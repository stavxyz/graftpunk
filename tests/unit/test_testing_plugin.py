"""graftpunk helpers the graft skill's fixture-leaks script copies, held equal to
it where the two must agree. The fixtures check itself is tested in
test_graftpunk_testing.py."""

from __future__ import annotations

import importlib.util
import json
import random
import string
from pathlib import Path
from types import ModuleType

import pytest

from graftpunk.har import paths as paths_module
from graftpunk.har.digest import DigestSource, digest, flagged_values_of, form_state
from graftpunk.har.parser import parse_har_file
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


def _value_corpus() -> list[str]:
    """Recorded values of every shape the form-state rule tells apart: random
    tokens, words in each casing, names, dates, quantities, punctuation, quoted
    and percent-encoded values."""
    rng = random.Random(345)  # noqa: S311 - a fixed sample, not a secret
    alphabets = [
        string.ascii_letters + string.digits,
        string.ascii_lowercase,
        string.ascii_uppercase + " ",
        string.ascii_letters,
        string.ascii_letters + string.digits + "+/=",
        string.digits + "-/ .",
        string.ascii_lowercase + " ,.'-",
        "abcdefghijklmnop%2C0123456789",
        string.punctuation + " ",
    ]
    words = ["shoes", "Smith", "JOHN", "current", "Tab", "view", "PDF", "list", "Results"]
    words += ["café", "iPhone", "15", "512gb", "en-US", "true", "of", "By", "Mary", "O'Brien"]
    corpus = [
        "".join(rng.choices(rng.choice(alphabets), k=rng.randint(1, 30))) for _ in range(4_000)
    ]
    corpus += [
        rng.choice(["", " ", ", ", "-"]).join(rng.choices(words, k=rng.randint(1, 4)))
        for _ in range(2_000)
    ]
    corpus += [f'"{value}"' for value in corpus[:300]] + [f"%22{v}%22" for v in corpus[:300]]
    corpus += [
        "true",
        "false",
        "USD",
        "en-US",
        "2026-10-01",
        "running+shoes",
        "currentTab",
        "viewPDF",
        "Smith, John",
        "LANDOWNER",
        "HTMLParserView",
        "LISTviewResults",
        "tabXQZresults",
        "****",
        "authenticity_token",
        "Zq9XkLmPwR4t7Bv2Hn8Ks3Jd",
        "kQzPwM",
        "lot 123456",
        "cus_NffrFeUfNV2Hib",
        "4111 1111 1111 1111",
        "%00",
    ]
    return corpus


def test_the_skill_scripts_form_state_rule_is_graftpunks() -> None:
    """The script skips a recorded value that holds nothing by a copy of
    graftpunk.har.digest.form_state, held to graftpunk's answers here."""
    script = _load_script()
    differ = [value for value in _value_corpus() if script.form_state(value) != form_state(value)]
    assert differ == []
    assert script.form_state("true")
    assert not script.form_state("Zq9XkLmPwR4t7Bv2Hn8Ks3Jd")


@pytest.mark.parametrize(
    "name",
    [
        "_MIN_HEX_LEN",
        "_MIN_PREFIXED_TAIL",
        "_MIN_BASE64_LEN",
        "_MIN_BASE64_SWITCHES",
        "_MAX_CAPITALS_RUN",
        "_VOWELS",
        "_WORD_CONSONANT_PAIRS",
        "_MAX_VALUE_WORD_LEN",
        "_QUANTITY_UNITS",
        "_CAMEL_JOINING_WORDS",
        "_MAX_CAMEL_PARTS",
        "_MIN_UPPER_CAMEL_LEN",
    ],
)
def test_the_skill_scripts_copied_constants_are_graftpunks(name: str) -> None:
    assert getattr(_load_script(), name) == getattr(paths_module, name)


@pytest.mark.parametrize(
    ("recorded", "reported"),
    [("true", False), ("USD", False), ("Zq9XkLmPwR4t7Bv2Hn8Ks3Jd", True)],
    ids=["true", "a-word", "opaque"],
)
def test_a_recorded_value_that_holds_nothing_is_not_looked_for(
    recorded: str, reported: bool
) -> None:
    """A name flagged by its name alone records values such as ``true``; a fixture
    holding the JSON literal ``true`` cannot invent it away. An opaque value is
    still reported, in both copies."""
    script = _load_script()
    fixture = '{"orders": [], "active": true, "currency": "USD", "t": "Zq9XkLmPwR4t7Bv2Hn8Ks3Jd"}'
    values = {"logged_in_session": {recorded}}
    package = recorded_leaks(fixture, values)
    assert package == script.recorded_leaks(script._Fixture(fixture), values)
    assert bool(package) is reported


def test_the_script_reads_no_file_outside_the_run_as_the_package(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    outside = tmp_path / "outside"
    outside.mkdir()
    har = _containment_run(run_dir, outside)
    assert _load_script().recorded_values(har, {"csrf-token"}, run_dir) == {}
    assert recorded_values(har, {"csrf-token"}, run_dir) == {}
