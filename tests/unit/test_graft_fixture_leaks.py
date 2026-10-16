"""The graft skill's fixture-leaks script: every captured value still in a fixture is
reported, so the skill can rewrite it before anything is committed."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from graftpunk.devtools.scaffold.pysrc import wrapped_docstring_lines
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
    assert lines == [
        "still in the fixture: 'shipped' "
        '(line 1: ...d": "ORD-9000001", "status": "»shipped«", "total": 999, "customer": {...)'
    ]


def test_nothing_left_exits_0(tmp_path: Path) -> None:
    result = _run(tmp_path, json.dumps({"name": "Alice Example"}), json.dumps({"name": "Pat"}))
    assert result.returncode == 0
    assert "no captured value is left" in result.stdout


_DATED_CAPTURE = json.dumps({"orders": [{"placed": "2026-09-30", "status": "shipped"}]})


def _stub_module(template: str, body: str = "return ctx", session: str = "myshop") -> str:
    """A plugin module whose stub docstring carries gp's provenance line, rendered
    and wrapped the way the generator writes it."""
    summary = f"GET {template}: seen 2 time(s) in run {session}/20261006-101500-4242."
    docstring = "\n".join(wrapped_docstring_lines(summary))
    return (
        f'class MyshopPlugin:\n    def orders(ctx):\n        """\n{docstring}\n'
        f'        """\n        {body}\n'
    )


@pytest.mark.parametrize(
    "template",
    ["/api/orders", "/api/accounts/{account_id}/orders/{order_id}/invoices/{invoice_id}/lines"],
    ids=["one-line", "wrapped"],
)
def test_the_run_id_in_gps_provenance_line_is_not_a_leak(tmp_path: Path, template: str) -> None:
    """The run id's year matches a captured date, but the run id is gp's, not the
    account's."""
    result = _run(tmp_path, _DATED_CAPTURE, _stub_module(template))
    assert result.returncode == 0, result.stdout


def test_a_run_label_cut_mid_word_by_the_wrap_is_still_gps(tmp_path: Path) -> None:
    """A session name has no length limit, and the docstring wrap cuts a label longer
    than its line mid-word; the label still ends at its own period."""
    module = _stub_module("/api/orders", session="shop" * 25)
    assert any(line.strip().endswith("shop") for line in module.splitlines()), module
    result = _run(tmp_path, _DATED_CAPTURE, module)
    assert result.returncode == 0, result.stdout


def test_a_closing_period_the_wrap_left_alone_still_ends_the_label(tmp_path: Path) -> None:
    module = _stub_module("/a", session="s" * 131)
    assert any(line.strip() == "." for line in module.splitlines()), module
    result = _run(tmp_path, _DATED_CAPTURE, module)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    ("next_line", "reported"),
    [
        ("ACCT_4471023.", "4471023"),
        ("acct_4471023.", "4471023"),
        ("4471023.", "4471023"),
        ("see\n4471023.", "4471023"),
        ("023.", "023"),
    ],
)
def test_a_value_after_a_provenance_phrase_missing_its_period_is_still_a_leak(
    tmp_path: Path, next_line: str, reported: str
) -> None:
    """A hand edit that drops the label's period must not let the phrase swallow what
    follows: only the run id's date and time are blanked, so an account value
    there, even one short enough to join the process id, is still reported."""
    capture = json.dumps({"account": "ACCT_4471023", "code": "023"})
    module = f"# seen 2 time(s) in run myshop/20261006-101500-4242\n{next_line}\n"
    result = _run(tmp_path, capture, module)
    assert result.returncode == 1
    assert reported in result.stdout


@pytest.mark.parametrize(
    ("capture_value", "phrase"),
    [
        pytest.param(
            "acct-4471023",
            "seen 2 time(s) in run acct-4471023/20261006-101500-4242.",
            id="session-slot",
        ),
        pytest.param(
            "4471023", "seen 4471023 time(s) in run myshop/20261006-101500-4242.", id="count-slot"
        ),
        pytest.param(
            "4471023", "seen 2 time(s) in run myshop/20261006-101500-\n4471023.", id="split-pid"
        ),
        pytest.param(
            "ORD-99887766", "seen 2 time(s) in run myshop/99887766-101500-4242.", id="not-a-date"
        ),
    ],
)
def test_a_captured_value_shaped_into_a_provenance_phrase_is_still_a_leak(
    tmp_path: Path, capture_value: str, phrase: str
) -> None:
    """Only the run id's date and time are blanked: the count, the session, and the
    process id are still searched, and a label whose date is not a date is left
    alone."""
    result = _run(tmp_path, json.dumps({"a": capture_value}), f"# {phrase}\n")
    assert result.returncode == 1, result.stdout


def test_blanking_adds_no_text_the_module_never_held(tmp_path: Path) -> None:
    """The date and time are blanked in place, so no pair of words is made up from the
    phrase's parts: a captured "2 myshop" is not reported for an unedited phrase."""
    result = _run(tmp_path, json.dumps({"a": "2 myshop"}), _stub_module("/api/orders"))
    assert result.returncode == 0, result.stdout


def test_a_session_given_in_capitals_is_still_gps(tmp_path: Path) -> None:
    """The label is the session name as typed to --from-run, which gp accepts in any case."""
    result = _run(tmp_path, _DATED_CAPTURE, _stub_module("/api/orders", session="MyShop"))
    assert result.returncode == 0, result.stdout


def test_an_unreadable_fixture_is_named(tmp_path: Path) -> None:
    (tmp_path / "capture.json").write_text("{}")
    missing = tmp_path / "plugin.py"
    assert PYTHON is not None
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [PYTHON, "-I", str(SCRIPT), str(tmp_path / "capture.json"), str(missing)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert f"fixture-leaks: cannot read the fixture {missing}:" in result.stderr


def test_a_digit_run_written_into_the_blanked_slot_is_the_documented_blind_spot(
    tmp_path: Path,
) -> None:
    """The run id's date and time are blanked, so a captured digit run written by hand
    into those 14 digits is not caught, as the script's docstring says. Pinned so a
    change that widens or narrows the blind spot is seen."""
    module = "# seen 2 time(s) in run myshop/20261006-101500-9999.\n"
    result = _run(tmp_path, json.dumps({"t": "101500"}), module)
    assert result.returncode == 0, result.stdout


_GP_SIDECAR = {"capture_sha256": "a" * 64, "flagged_names": ["session_id"]}


@pytest.mark.parametrize(
    "written",
    [
        pytest.param({**_GP_SIDECAR, "capture_sha256": "b" + "a" * 63}, id="mistyped-hash"),
        pytest.param({**_GP_SIDECAR, "flagged_names": []}, id="dropped-name"),
        pytest.param({"capture_sha256": _GP_SIDECAR["capture_sha256"]}, id="missing-key"),
        pytest.param({**_GP_SIDECAR, "extra": 1}, id="extra-key"),
    ],
)
def test_a_sidecar_that_differs_from_gps_is_reported(tmp_path: Path, written: dict) -> None:
    """The skill writes the sidecar's text with the Write tool; a mistyped hash would
    switch off the suite's unchanged-copy check, a dropped name its name check."""
    (tmp_path / "capture.json.meta.json").write_text(json.dumps(_GP_SIDECAR))
    result = _run(tmp_path, json.dumps({"n": "Alice Example"}), json.dumps({"n": "Pat"}), written)
    assert result.returncode == 1
    assert "the sidecar differs from the capture's in" in result.stdout
    changed = sorted(
        k for k in written.keys() | _GP_SIDECAR.keys() if written.get(k) != _GP_SIDECAR.get(k)
    )
    assert f"differs from the capture's in {', '.join(changed)}:" in result.stdout


def test_a_run_without_a_sidecar_ignores_gps(tmp_path: Path) -> None:
    """The plugin-module and test-module runs pass no sidecar: nothing to compare."""
    (tmp_path / "capture.json.meta.json").write_text(json.dumps(_GP_SIDECAR))
    result = _run(tmp_path, json.dumps({"n": "Alice Example"}), json.dumps({"n": "Pat"}))
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("text", ["not json", "[1, 2]"], ids=["not-json", "not-object"])
def test_an_unreadable_gp_sidecar_exits_2(tmp_path: Path, text: str) -> None:
    (tmp_path / "capture.json.meta.json").write_text(text)
    result = _run(
        tmp_path, json.dumps({"n": "Alice Example"}), json.dumps({"n": "Pat"}), _GP_SIDECAR
    )
    assert result.returncode == 2
    assert "the capture's sidecar" in result.stderr


def test_a_sidecar_equal_to_gps_as_json_passes(tmp_path: Path) -> None:
    (tmp_path / "capture.json.meta.json").write_text(json.dumps(_GP_SIDECAR, indent=2))
    reordered = dict(reversed(list(_GP_SIDECAR.items())))
    result = _run(tmp_path, json.dumps({"n": "Alice Example"}), json.dumps({"n": "Pat"}), reordered)
    assert result.returncode == 0, result.stdout


def test_the_same_digits_elsewhere_in_the_module_are_still_a_leak(tmp_path: Path) -> None:
    result = _run(tmp_path, _DATED_CAPTURE, _stub_module("/api/orders", 'since = "2026"'))
    assert result.returncode == 1
    assert "'2026'" in result.stdout


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
    capture = json.dumps({"email": "m.okonkwo@example.org"})
    result = _run(tmp_path, capture, json.dumps({"email": "m.okonkwo@example.com"}))
    assert result.returncode == 1
    assert "'m.okonkwo'" in result.stdout


def test_a_python_module_with_escaped_copies_is_checked(tmp_path: Path) -> None:
    """A module is neither JSON nor HTML; its backslash escapes and percent-encoding
    are decoded all the same."""
    capture = json.dumps({"name": "José García", "url": "/a/b/Q7", "email": "ann@myshop.example"})
    module = 'NAME = "Jos\\u00e9 Garc\\u00eda"\nURL = "\\/a\\/b\\/Q7"\nQ = "ann%40myshop.example"\n'
    result = _run(tmp_path, capture, module)
    assert result.returncode == 1
    for value in ("José García", "/a/b/Q7", "ann@myshop.example"):
        assert repr(value) in result.stdout, value


def test_a_decomposed_accent_is_the_same_value(tmp_path: Path) -> None:
    capture = json.dumps({"name": "Renée Okafor"}, ensure_ascii=False)
    fixture = json.dumps({"name": "Renée Okafor"}, ensure_ascii=False)
    result = _run(tmp_path, capture, fixture)
    assert result.returncode == 1
    assert "'Renée Okafor'" in result.stdout


def test_an_escaped_surrogate_pair_is_rejoined(tmp_path: Path) -> None:
    capture = json.dumps({"tag": "Ann \U0001f600 Q7"}, ensure_ascii=False)
    # A lone surrogate elsewhere in the file must not stop the pair being rejoined.
    module = 'TAG = "Ann \\ud83d\\ude00 Q7"\nX = "\\ud800"\n'
    result = _run(tmp_path, capture, module)
    assert result.returncode == 1
    assert repr("Ann \U0001f600 Q7") in result.stdout


def test_a_capitalised_word_is_not_found_inside_a_lowercase_name(tmp_path: Path) -> None:
    """Page words such as Total or Shipping would otherwise flood a plugin module's
    check through its parameter names."""
    capture = "<h1>Statement</h1><p>The Total for Shipping</p>"
    result = _run(tmp_path, capture, "def summary(total, shipping):\n    return other\n")
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    "module",
    [
        "OKONKWO_ID = 7\n",
        "def test_okonkwo_order():\n    pass\n",
        'EMAIL = "okonkwo@example.com"\n',
    ],
)
def test_a_name_in_an_identifier_or_address_is_caught(tmp_path: Path, module: str) -> None:
    """Constants, test names, and email addresses carry a name in capitals or in
    lower case joined by an underscore, an at sign, or a dot."""
    result = _run(tmp_path, json.dumps({"name": "Adaeze Okonkwo"}), module)
    assert result.returncode == 1
    assert "'Okonkwo'" in result.stdout


def test_an_entity_in_an_html_comment_is_decoded(tmp_path: Path) -> None:
    capture = "<div><!-- owner Bj&ouml;rn --><p>hi</p></div>"
    result = _run(tmp_path, capture, "<div><p>Björn</p></div>")
    assert result.returncode == 1
    assert "'Björn'" in result.stdout


@pytest.mark.parametrize(
    "fixture",
    [
        '<a href="/statements?m=tomasz.w%40example.org&amp;id=900">next</a>',
        '<a href="/statements?m=tomasz.w%40example.net&amp;id=900">next</a>',
        '{"next": "/orders?email=tomasz.w%40example.org&page=2"}',
    ],
)
def test_an_email_in_a_query_string_is_caught_both_ways(tmp_path: Path, fixture: str) -> None:
    """The capture's link is percent-encoded and so is the fixture's; the email, or
    its local part under a new domain, is still found."""
    capture = '<a href="/statements?m=tomasz.w%40example.org&amp;id=4471">next</a>'
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


def test_a_sidecar_written_as_bad_json_names_the_sidecar(tmp_path: Path) -> None:
    """harden.md's exit-2 case for a sidecar the skill wrote: the slip is named, so the
    skill writes that file again rather than treating the capture as unreadable."""
    (tmp_path / "capture.json").write_text("{}")
    (tmp_path / "fixture.json").write_text("{}")
    (tmp_path / "fixture.json.meta.json").write_text("{not json")
    assert PYTHON is not None
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [
            PYTHON,
            "-I",
            str(SCRIPT),
            *(
                str(tmp_path / n)
                for n in ("capture.json", "fixture.json", "fixture.json.meta.json")
            ),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert (
        f"fixture-leaks: cannot read the sidecar {tmp_path / 'fixture.json.meta.json'}:"
        in result.stderr
    )


def test_a_capture_that_is_not_text_exits_2(tmp_path: Path) -> None:
    """harden.md's exit-2 case: a PDF capture is compared with nothing, and says so."""
    capture = tmp_path / "invoice.pdf"
    capture.write_bytes(b"%PDF-1.4\n\xff\xfe")
    fixture = tmp_path / "fixture.pdf"
    fixture.write_text("invented\n")
    assert PYTHON is not None
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [PYTHON, "-I", str(SCRIPT), str(capture), str(fixture)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert f"fixture-leaks: cannot read the capture {capture}:" in result.stderr
    assert result.stdout == ""


def _leak_lines(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line.startswith("still in the fixture:")]


def test_each_place_names_its_line_and_marks_the_match(tmp_path: Path) -> None:
    """The skill keeps a value only for a stated reason, so it is told where the
    value is (#238): the 1-based line of the raw file and the match marked in it."""
    capture = json.dumps({"placed": "2026-09-30", "status": "shipped"})
    module = (
        '"""Orders for myshop."""\n'
        "\n"
        "# Recorded from https://myshop.example/login in a browser on 2026-10-13.\n"
        "STATUS = 'pending'\n"
    )
    result = _run(tmp_path, capture, module)
    assert result.returncode == 1
    assert _leak_lines(result.stdout) == [
        "still in the fixture: '2026' (line 3: ...example/login in a browser on »2026«-10-13.)"
    ]


def test_a_value_on_several_lines_names_each_up_to_five(tmp_path: Path) -> None:
    capture = json.dumps({"id": "4471023"})
    fixture = "\n".join(f"row {n} 4471023" for n in range(1, 8))
    result = _run(tmp_path, capture, fixture)
    assert _leak_lines(result.stdout) == [
        *(f"still in the fixture: '4471023' (line {n}: row {n} »4471023«)" for n in range(1, 6)),
        "still in the fixture: '4471023' (and 2 more lines)",
    ]


def test_a_long_line_is_cut_around_the_match(tmp_path: Path) -> None:
    """The file may hold other captured values, so no more of the line than the
    match's neighborhood is printed."""
    capture = json.dumps({"id": "4471023"})
    fixture = json.dumps({"head": "H" * 200, "id": "4471023", "tail": "T" * 200})
    result = _run(tmp_path, capture, fixture)
    (line,) = _leak_lines(result.stdout)
    assert line == (
        "still in the fixture: '4471023' "
        f'(line 1: ...{"H" * 20}", "id": "»4471023«", "tail": "{"T" * 18}...)'
    )


def test_a_long_value_and_match_are_shortened_in_the_middle(tmp_path: Path) -> None:
    value = "Alice " + "x" * 100 + " Smith"
    cut = f"{value[:30]}...{value[-30:]}"
    result = _run(tmp_path, json.dumps({"note": value}), json.dumps({"note": value}))
    whole = [line for line in _leak_lines(result.stdout) if line.startswith(f"{_PREFIX}{cut!r}")]
    assert whole == [f'{_PREFIX}{cut!r} (line 1: {{"note": "»{cut}«"}})']


_PREFIX = "still in the fixture: "


def test_a_value_found_only_decoded_says_so_without_a_line(tmp_path: Path) -> None:
    """A line number refers to the raw file, where an escaped copy does not read
    as the value, so the decoded match is shown instead."""
    capture = json.dumps({"name": "José García"}, ensure_ascii=False)
    result = _run(tmp_path, capture, json.dumps({"name": "José García"}))
    assert result.returncode == 1
    lines = [line for line in _leak_lines(result.stdout) if "José García'" in line]
    assert lines == [f"{_PREFIX}'José García' (found decoded: »José García«)"]


def test_a_match_in_another_case_is_placed(tmp_path: Path) -> None:
    capture = json.dumps({"note": "Leave with Okonkwo"})
    result = _run(tmp_path, capture, "x = 1\nOKONKWO_ID = 2\n")
    assert f"{_PREFIX}'Okonkwo' (line 2: »OKONKWO«_ID = 2)" in _leak_lines(result.stdout)


def test_the_neighborhood_shows_gps_run_label_as_written(tmp_path: Path) -> None:
    """The run label's date is blanked for the search, not for the output: a line
    is shown as the file holds it."""
    capture = json.dumps({"placed": "2026-09-30"})
    module = "# seen 2 time(s) in run myshop/20261006-101500-4242. since 2026\n"
    result = _run(tmp_path, capture, module)
    assert _leak_lines(result.stdout) == [
        f"{_PREFIX}'2026' (line 1: ...p/20261006-101500-4242. since »2026«)"
    ]


def test_control_characters_around_a_match_are_escaped_in_the_output(tmp_path: Path) -> None:
    """A fixture line may hold terminal escapes or a line separator; the context
    printed around a match must not pass them through or split the output line."""
    capture = json.dumps({"order": "ORD-4471023"})
    fixture = "note \x1b[2J\x07 ORD-4471023 \u2028 tail\n"
    result = _run(tmp_path, capture, fixture)
    assert result.returncode == 1
    for raw in ("\x1b", "\x07", "\u2028"):
        assert raw not in result.stdout, repr(raw)
    found = [line for line in result.stdout.splitlines() if "ORD-4471023" in line]
    assert len(found) == 1, result.stdout
    assert "\\x1b" in found[0] and "\\u2028" in found[0]


def test_the_context_is_the_files_own_text(tmp_path: Path) -> None:
    """The search runs on the compatibility form, where a ligature is two letters;
    the context is cut from the file as written, so it can be searched for there."""
    capture = json.dumps({"id": "4471023", "firm": "ﬁnance Ltd"}, ensure_ascii=False)
    fixture = "ﬁﬁﬁ ⑴ 4471023\nfinance Ltd\n"
    result = _run(tmp_path, capture, fixture)
    lines = _leak_lines(result.stdout)
    assert f"{_PREFIX}'4471023' (line 1: ﬁﬁﬁ ⑴ »4471023«)" in lines
    assert f"{_PREFIX}'ﬁnance Ltd' (line 2: »finance Ltd«)" in lines


def test_marks_in_the_file_are_escaped_so_the_match_is_unambiguous(tmp_path: Path) -> None:
    capture = json.dumps({"id": "4471023"})
    result = _run(tmp_path, capture, "a »x« 4471023 >>y<<\n")
    assert _leak_lines(result.stdout) == [
        f"{_PREFIX}'4471023' (line 1: a \\xbbx\\xab »4471023« >>y<<)"
    ]


def test_only_the_first_values_are_placed_then_the_rest_counted(tmp_path: Path) -> None:
    """An unchanged copy of a large capture holds thousands of values; their
    places would flood the skill's context, so past fifty only a count prints."""
    ids = [str(1000000 + n) for n in range(60)]
    result = _run(tmp_path, json.dumps({"ids": ids}), json.dumps({"ids": ids}))
    assert result.returncode == 1
    lines = _leak_lines(result.stdout)
    assert [line.split(" (")[0] for line in lines] == [f"{_PREFIX}{v!r}" for v in ids[:50]]
    assert result.stdout.splitlines()[-1] == (
        "fixture-leaks: 10 more values not shown; rewrite those above and run again."
    )
