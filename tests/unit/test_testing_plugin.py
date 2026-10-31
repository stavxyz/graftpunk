"""graftpunk helpers the graft skill's fixture-leaks script copies, held equal to
it where the two must agree. The fixtures check itself is tested in
test_graftpunk_testing.py."""

from __future__ import annotations

import importlib.util
from types import ModuleType

import pytest

from graftpunk.har.documents import token_values
from graftpunk.testing.decoding import decoded_forms
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


def test_the_skill_script_reads_token_values_as_the_digest_does() -> None:
    """``fixture-leaks.py --har`` reads a meta or hidden-input token's value as
    ``gp observe fixtures`` does (graftpunk.har.documents.token_values)."""
    html = (
        '<meta name="csrf-token" content="a&amp;b"><meta name="viewport">'
        '<input type="HIDDEN" name="t" value="x1"><input type="text" name="q" value="s">'
        '<input type="hidden" name="t" value="x2"><input type="hidden" name="e" value="">'
    )
    parser = _load_script()._TokenValues()
    parser.feed(html)
    parser.close()
    script: dict[str, set[str]] = {}
    for name, value in parser.pairs:
        if name and value:
            script.setdefault(name, set()).add(value)
    assert {name: frozenset(found) for name, found in script.items()} == token_values(html)
