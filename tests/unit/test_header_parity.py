"""scripts/header_parity.py's comparison, with no git and no second tree."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).parents[2] / "scripts" / "header_parity.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("header_parity", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


URL = "https://myshop.example/a"


def _run(*headers: tuple[str, str]) -> list:
    return [["GET", URL, [list(h) for h in headers]]]


def test_identical_runs_have_no_differences() -> None:
    hp = _script()
    run = {"a": _run(("Accept", "*/*"), ("User-Agent", "UA"))}
    assert hp.differences(run, run) == []


def test_each_kind_of_difference_is_named_by_its_header() -> None:
    hp = _script()
    old = {
        "changed": _run(("Accept", "*/*")),
        "gone": _run(("Accept", "*/*"), ("X-Old", "1")),
        "added": _run(("Accept", "*/*")),
        "order": _run(("A", "1"), ("B", "2")),
        "spelled": _run(("Accept", "x")),
        "only-old": _run(),
        "method": [["GET", URL, []]],
    }
    new = {
        "changed": _run(("Accept", "text/html")),
        "gone": _run(("Accept", "*/*")),
        "added": _run(("Accept", "*/*"), ("X-New", "1")),
        "order": _run(("B", "2"), ("A", "1")),
        "spelled": _run(("accept", "x")),
        "only-new": _run(),
        "method": [["POST", URL, []]],
    }
    found = {(case, header) for case, header, _what in hp.differences(old, new)}
    assert found == {
        ("changed", "accept"),
        ("gone", "x-old"),
        ("added", "x-new"),
        ("order", "<order>"),
        ("spelled", "accept"),
        ("only-old", "<case>"),
        ("only-new", "<case>"),
        ("method", "<request>"),
    }


def test_an_added_or_gone_header_is_not_also_an_order_difference() -> None:
    hp = _script()
    old = {"a": _run(("A", "1"), ("B", "2"))}
    new = {"a": _run(("A", "1"), ("X", "9"), ("B", "2"))}
    assert [h for _c, h, _w in hp.differences(old, new)] == ["x"]


def test_only_listed_headers_of_matching_cases_are_allowed() -> None:
    hp = _script()
    allowed = [{"case": "http2 *", "headers": ["accept"], "reason": "lower-case role"}]
    found = [
        ("http2 own get", "accept", "Accept: */* is now accept: text/html"),
        ("http2 own get", "x-other", "added"),
        ("captured own get", "accept", "Accept: */* is now Accept: text/html"),
    ]
    lines, status = hp.report(found, allowed)
    assert status == 1
    assert lines[0].startswith("allowed  http2 own get | accept")
    assert "(lower-case role)" in lines[0]
    assert lines[1].startswith("DIFF     http2 own get | x-other")
    assert lines[2].startswith("DIFF     captured own get | accept")
    assert lines[-1] == "3 differences, 1 allowed, 2 not"


def test_only_allowed_differences_exit_0() -> None:
    hp = _script()
    allowed = [{"case": "*", "headers": ["accept"], "reason": "r"}]
    lines, status = hp.report([("a", "accept", "x")], allowed)
    assert status == 0
    assert lines[-1] == "1 differences, 1 allowed, 0 not"


def test_an_unreadable_allowlist_is_a_usage_error(tmp_path: Path) -> None:
    hp = _script()
    bad = tmp_path / "allowed.json"
    bad.write_text("{}")
    assert hp.main(["--allowed", str(bad)]) == 2
    assert hp.main(["--allowed", str(tmp_path / "missing.json")]) == 2
