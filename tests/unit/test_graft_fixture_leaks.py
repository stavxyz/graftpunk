"""The graft skill's fixture-leaks script: every captured value still in a fixture is
reported, so the skill can rewrite it before anything is committed."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.guide_harness import REPO_ROOT

SCRIPT = REPO_ROOT / "skills" / "graft" / "scripts" / "fixture-leaks.py"
PYTHON = shutil.which("python3")

_CAPTURE = {
    "orders": [
        {
            "id": "ORD-4471023",
            "status": "shipped",
            "total": 12345,
            "customer": {"name": "Alice Smith", "email": "alice@example.com"},
            "invoice": "/orders/4471023/invoice",
            "gift": False,
        }
    ],
    "page": 1,
}


def _run(tmp_path: Path, capture: str, fixture: str, sidecar: dict | None = None):
    (tmp_path / "capture.json").write_text(capture)
    (tmp_path / "fixture.json").write_text(fixture)
    args = [str(tmp_path / "capture.json"), str(tmp_path / "fixture.json")]
    if sidecar is not None:
        (tmp_path / "fixture.json.meta.json").write_text(json.dumps(sidecar))
        args.append(str(tmp_path / "fixture.json.meta.json"))
    assert PYTHON is not None
    return subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [PYTHON, "-I", str(SCRIPT), *args], capture_output=True, text=True
    )


def test_an_unchanged_copy_reports_every_value(tmp_path: Path) -> None:
    text = json.dumps(_CAPTURE)
    result = _run(tmp_path, text, text)
    assert result.returncode == 1
    for value in ("ORD-4471023", "shipped", "12345", "Alice Smith", "alice@example.com"):
        assert repr(value) in result.stdout, value


def test_an_id_inside_another_string_is_still_caught(tmp_path: Path) -> None:
    """Rewriting the order id field is not enough when the same number survives in
    a URL; a substring search finds it where a whole-value comparison would not."""
    fixture = json.dumps(
        {
            "orders": [
                {
                    "id": "ORD-9000001",
                    "status": "shipped",
                    "total": 999,
                    "customer": {"name": "Pat Sample", "email": "pat@example.net"},
                    "invoice": "/orders/4471023/invoice",
                    "gift": False,
                }
            ],
            "page": 1,
        }
    )
    result = _run(tmp_path, json.dumps(_CAPTURE), fixture)
    assert result.returncode == 1
    assert "'4471023'" in result.stdout
    assert "'Alice Smith'" not in result.stdout


def test_a_fully_invented_fixture_reports_only_what_was_kept_on_purpose(tmp_path: Path) -> None:
    """A status the command branches on may stay; it is reported for the skill to
    judge, and nothing from the account is."""
    fixture = json.dumps(
        {
            "orders": [
                {
                    "id": "ORD-9000001",
                    "status": "shipped",
                    "total": 999,
                    "customer": {"name": "Pat Sample", "email": "pat@example.net"},
                    "invoice": "/orders/9000001/invoice",
                    "gift": False,
                }
            ],
            "page": 1,
        }
    )
    result = _run(tmp_path, json.dumps(_CAPTURE), fixture)
    assert result.returncode == 1
    lines = [line for line in result.stdout.splitlines() if line.startswith("still in")]
    assert lines == ["still in the fixture: 'shipped'"]


def test_nothing_left_exits_0(tmp_path: Path) -> None:
    result = _run(tmp_path, json.dumps({"name": "Alice Example"}), json.dumps({"name": "Pat"}))
    assert result.returncode == 0
    assert "no captured value is left" in result.stdout


def test_an_html_capture_checks_text_and_attribute_values(tmp_path: Path) -> None:
    capture = '<ul class="orders"><li data-id="4471023">Alice Example</li></ul>'
    fixture = '<ul class="orders"><li data-id="9000001">Alice Example</li></ul>'
    result = _run(tmp_path, capture, fixture)
    assert result.returncode == 1
    assert "'Alice Example'" in result.stdout
    assert "'4471023'" not in result.stdout
    # The class a parser selects on is reported too; keeping it is the skill's call.
    assert "'orders'" in result.stdout


def test_an_escaped_copy_is_still_caught(tmp_path: Path) -> None:
    """json.dumps writes a non-ASCII name as \\u escapes, so a search of the raw text
    alone misses the copy."""
    capture = json.dumps({"name": "José García"}, ensure_ascii=False)
    result = _run(tmp_path, capture, json.dumps({"name": "José García"}))
    assert result.returncode == 1
    assert "'José García'" in result.stdout


def test_an_entity_escaped_copy_in_html_is_still_caught(tmp_path: Path) -> None:
    result = _run(tmp_path, "<p>Smith &amp; Sons</p>", "<p>Smith &amp; Sons</p>")
    assert result.returncode == 1
    assert "'Smith & Sons'" in result.stdout


def test_a_value_inside_a_shortened_string_is_still_caught(tmp_path: Path) -> None:
    """Cutting an address off a sentence leaves the name; the word pairs and the
    capitalised words of the original catch it."""
    capture = json.dumps({"note": "Ship to Jane Doe at Elm Road"})
    result = _run(tmp_path, capture, json.dumps({"note": "Ship to Jane Doe"}))
    assert result.returncode == 1
    assert "'Jane Doe'" in result.stdout


def test_a_copy_in_another_case_is_still_caught(tmp_path: Path) -> None:
    capture = json.dumps({"note": "Leave with Okonkwo, ask for Margaret"})
    result = _run(tmp_path, capture, json.dumps({"note": "OKONKWO, MARGARET"}))
    assert result.returncode == 1
    assert "'Okonkwo'" in result.stdout
    assert "'Margaret'" in result.stdout


def test_an_email_kept_under_a_new_domain_is_caught(tmp_path: Path) -> None:
    capture = json.dumps({"email": "m.okonkwo@fastmail.com"})
    result = _run(tmp_path, capture, json.dumps({"email": "m.okonkwo@example.com"}))
    assert result.returncode == 1
    assert "'m.okonkwo'" in result.stdout


def test_a_python_module_with_escaped_copies_is_checked(tmp_path: Path) -> None:
    """A module is neither JSON nor HTML; its backslash escapes and percent-encoding
    are decoded all the same."""
    capture = json.dumps({"name": "José García", "url": "/a/b/Q7", "email": "ann@shop.example"})
    module = 'NAME = "Jos\\u00e9 Garc\\u00eda"\nURL = "\\/a\\/b\\/Q7"\nQ = "ann%40shop.example"\n'
    result = _run(tmp_path, capture, module)
    assert result.returncode == 1
    for value in ("José García", "/a/b/Q7", "ann@shop.example"):
        assert repr(value) in result.stdout, value


def test_a_decomposed_accent_is_the_same_value(tmp_path: Path) -> None:
    capture = json.dumps({"name": "Renée Okafor"}, ensure_ascii=False)
    fixture = json.dumps({"name": "Renée Okafor"}, ensure_ascii=False)
    result = _run(tmp_path, capture, fixture)
    assert result.returncode == 1
    assert "'Renée Okafor'" in result.stdout


def test_an_escaped_surrogate_pair_is_rejoined(tmp_path: Path) -> None:
    capture = json.dumps({"tag": "Ann \U0001f600 Q7"}, ensure_ascii=False)
    module = 'TAG = "Ann \\ud83d\\ude00 Q7"\n'
    result = _run(tmp_path, capture, module)
    assert result.returncode == 1
    assert repr("Ann \U0001f600 Q7") in result.stdout


def test_a_capitalised_word_is_not_found_inside_a_lowercase_name(tmp_path: Path) -> None:
    """Page words such as Total or Shipping would otherwise flood a plugin module's
    check through its parameter names."""
    capture = "<h1>Your Order</h1><p>The Total for Shipping</p>"
    result = _run(tmp_path, capture, "def your_order(total, shipping):\n    return other\n")
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    "fixture",
    [
        '<a href="/statements?m=tomasz.w%40proton.me&amp;id=900">next</a>',
        '<a href="/statements?m=tomasz.w%40example.net&amp;id=900">next</a>',
        '{"next": "/orders?email=tomasz.w%40proton.me&page=2"}',
    ],
)
def test_an_email_in_a_query_string_is_caught_both_ways(tmp_path: Path, fixture: str) -> None:
    """The capture's link is percent-encoded and so is the fixture's; the email, or
    its local part under a new domain, is still found."""
    capture = '<a href="/statements?m=tomasz.w%40proton.me&amp;id=4471">next</a>'
    result = _run(tmp_path, capture, fixture)
    assert result.returncode == 1
    assert "'tomasz.w'" in result.stdout


def test_two_plain_lowercase_words_are_not_reported(tmp_path: Path) -> None:
    capture = json.dumps({"note": "Leave it at the door"})
    result = _run(tmp_path, capture, json.dumps({"note": "put it at the gate"}))
    assert "'at the'" not in result.stdout
    assert "'it at'" not in result.stdout


def test_an_identifier_key_holding_a_number_is_caught(tmp_path: Path) -> None:
    capture = json.dumps({"acct_99887766": {"plan": "pro"}})
    result = _run(tmp_path, capture, json.dumps({"acct_99887766": {"plan": "basic"}}))
    assert result.returncode == 1
    assert "'99887766'" in result.stdout


def test_an_account_value_used_as_a_key_is_caught(tmp_path: Path) -> None:
    capture = json.dumps({"alice@example.com": {"plan": "pro"}})
    result = _run(tmp_path, capture, json.dumps({"alice@example.com": {"plan": "basic"}}))
    assert result.returncode == 1
    assert "'alice@example.com'" in result.stdout


def test_an_html_comment_is_checked(tmp_path: Path) -> None:
    capture = "<div><!-- acct 99887766 --><p>hi</p></div>"
    result = _run(tmp_path, capture, "<div><!-- acct 99887766 --><p>yo</p></div>")
    assert result.returncode == 1
    assert "'99887766'" in result.stdout


def test_a_sidecar_that_is_not_an_object_exits_2(tmp_path: Path) -> None:
    for name, text in (("capture.json", "{}"), ("fixture.json", "{}"), ("side.json", "[]")):
        (tmp_path / name).write_text(text)
    assert PYTHON is not None
    names = ("capture.json", "fixture.json", "side.json")
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [PYTHON, "-I", str(SCRIPT), *(str(tmp_path / n) for n in names)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "not a JSON object" in result.stderr


def test_the_sidecar_lists_are_printed_for_review(tmp_path: Path) -> None:
    sidecar = {"body_params": ["page"], "flagged_names": ["sessionId"]}
    result = _run(tmp_path, json.dumps({"a": "Alice Example"}), json.dumps({"a": "x"}), sidecar)
    assert "sidecar body_params: page" in result.stdout
    assert "sidecar flagged_names: sessionId" in result.stdout


@pytest.mark.parametrize("argc", [0, 1, 4])
def test_a_wrong_argument_count_exits_2(tmp_path: Path, argc: int) -> None:
    assert PYTHON is not None
    args = [str(tmp_path / f"f{i}") for i in range(argc)]
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [PYTHON, "-I", str(SCRIPT), *args], capture_output=True, text=True
    )
    assert result.returncode == 2


def test_an_unreadable_input_exits_2(tmp_path: Path) -> None:
    assert PYTHON is not None
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [PYTHON, "-I", str(SCRIPT), str(tmp_path / "missing"), str(tmp_path / "missing")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "cannot read" in result.stderr
