"""graftpunk helpers the graft skill's fixture-leaks script copies, held equal to
it where the two must agree. The fixtures check itself is tested in
test_graftpunk_testing.py."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from graftpunk.har.digest import DigestSource, digest, flagged_values_of
from graftpunk.har.parser import parse_har_file
from graftpunk.testing.decoding import combined_forms, decoded_forms, value_forms
from tests.unit.guide_harness import REPO_ROOT

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
