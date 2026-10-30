"""Inline JSON state in HTML pages (#271, part 2).

A server-rendered page that keeps its data in one inline ``<script>`` (an
assignment such as ``window.App.props = {...};``, or a
``<script type="application/json">`` block) is reported with the target and the
top-level shape of the largest such object. Only a shape is reported, never a
value. A run whose pages hold no such object digests, projects, and scaffolds
exactly as graftpunk 1.31.1 did, which the golden file pins.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from graftpunk.devtools.scaffold.pysrc import GENERATED_LINE_LENGTH
from graftpunk.devtools.scaffold.render import ScaffoldSpec, render, render_command
from graftpunk.devtools.scaffold.selection import CommandSelection, planned_commands
from graftpunk.har import inline_state
from graftpunk.har.digest import DigestSource, Endpoint, RunDigest, digest
from graftpunk.har.report import (
    endpoints_projection,
    render_endpoints_json,
    render_json,
    render_markdown,
    summarize_shape,
)
from tests.unit.test_har_digest import _entry, _write_har
from tests.unit.test_har_report import _ENDPOINT_V1, _INLINE_ENDPOINT_V1

_HOST = "https://myshop.example.com"
_FIXTURES = Path(__file__).parent.parent / "fixtures"
_GOLDEN = _FIXTURES / "digest_without_inline_json.json"
_ITEMS = [{"name": f"Red widget {i}", "price": i, "in_stock": True} for i in range(12)]
_STATE = {"results": _ITEMS, "search": {"query": "red widget", "page": 1}, "total": 12}
_STATE_TEXT = json.dumps(_STATE)
_SHAPE = "object{results, search, total}"
_PLANTED = "Red widget 7"


def _page(*scripts: str) -> str:
    return (
        '<html><head><title>Widgets</title></head><body><div id="root"></div>'
        + "".join(scripts)
        + "</body></html>"
    )


def _script(body: str, attrs: str = "") -> str:
    return f"<script{attrs}>{body}</script>"


def _html(path: str, body: str) -> dict:
    return _entry("GET", f"{_HOST}{path}", content_type="text/html; charset=utf-8", body=body)


def _json(path: str, body: str) -> dict:
    return _entry("GET", f"{_HOST}{path}", body=body)


# Pages near a qualifying object, none of which holds one: each must digest as it
# did before inline JSON state was read.
_NEAR_MISSES: dict[str, str] = {
    "function-call": _page(_script(f"init({_STATE_TEXT});")),
    "template-literal": _page(_script("window.App.view = `<p>${name}</p>`;")),
    "object-literal": _page(
        _script("window.App.props = {results: [" + ", ".join(["1"] * 300) + "]};")
    ),
    "small-object": _page(_script('window.App.props = {"page": 1};')),
    "malformed": _page(_script(f"window.App.props = {_STATE_TEXT[:-1]};")),
    "array": _page(_script(f"window.App.items = {json.dumps(_ITEMS * 2)};")),
    "ld-json": _page(_script(_STATE_TEXT, ' type="application/ld+json"')),
    "var-declaration": _page(_script(f"var __STATE__ = {_STATE_TEXT};")),
    "bare-name": _page(_script(f"STATE = {_STATE_TEXT};")),
    "external-script": _page(_script("", ' src="/static/app.js"')),
    "comparison": _page(_script(f"if (window.App.props == {_STATE_TEXT}) {{ go(); }}")),
    "json-array": _page(_script(json.dumps(_ITEMS * 2), ' type="application/json"')),
    # A browser ignores the body of a script that has a src.
    "src-with-body": _page(_script(f"window.App.props = {_STATE_TEXT};", ' src="/static/app.js"')),
    "template-type": _page(
        _script(f"window.App.props = {_STATE_TEXT};", ' type="text/x-template"')
    ),
}


def _golden_runs(base: Path) -> dict[str, tuple[DigestSource, tuple[CommandSelection, ...]]]:
    runs: dict[str, list[dict]] = {
        name: [_html("/search", page), _json("/api/orders", json.dumps({"orders": []}))]
        for name, page in _NEAR_MISSES.items()
    }
    # A qualifying script in a body that is not HTML is never read.
    runs["not-html"] = [
        _json("/api/page", json.dumps({"html": _script(f"window.App.props = {_STATE_TEXT};")})),
        _entry(
            "GET",
            f"{_HOST}/notes",
            content_type="text/plain",
            body=_script(f"window.App.props = {_STATE_TEXT};"),
        ),
    ]
    sources: dict[str, tuple[DigestSource, tuple[CommandSelection, ...]]] = {}
    for name, entries in runs.items():
        run = base / name
        run.mkdir(parents=True)
        commands = (
            (CommandSelection("search", "GET", "/search"),) if name == "function-call" else ()
        )
        sources[name] = (DigestSource(har_path=_write_har(run, entries)), commands)
    sources["sample"] = (DigestSource(har_path=_FIXTURES / "sample.har"), ())
    return sources


def golden_outputs(base: Path) -> dict[str, dict[str, str]]:
    """Every output the digest gives for each golden run, with *base* spelled
    ``<run>`` and the fixtures directory ``<fixtures>``: the markdown, the JSON, the
    endpoints projection, and the plugin and test module the generator writes, by
    default and, where a run names one, for a ``--command``."""
    outputs: dict[str, dict[str, str]] = {}
    for name, (source, commands) in _golden_runs(base).items():
        result = digest(source)
        files: dict[str, str] = {
            "markdown": render_markdown(result),
            "json": render_json(result),
            "endpoints": render_endpoints_json(result),
        }
        for label, selected in (("default", ()), ("command", commands)):
            if label == "command" and not selected:
                continue
            rendered = render(_spec(result, selected))
            files[f"{label}-plugin"] = rendered["src/graftpunk_myshop/plugin.py"]
            files[f"{label}-tests"] = rendered["tests/test_plugin.py"]
        outputs[name] = {
            key: value.replace(str(base), "<run>").replace(str(_FIXTURES), "<fixtures>")
            for key, value in files.items()
        }
    return outputs


def _spec(result: RunDigest, commands: tuple[CommandSelection, ...] = ()) -> ScaffoldSpec:
    return ScaffoldSpec(
        name="myshop",
        mode="new_project",
        backend="nodriver",
        base_url=_HOST,
        digest=result,
        graftpunk_version="1.31.1",
        commands=commands,
    )


def test_a_run_without_inline_state_digests_as_1_31_1_did(tmp_path: Path) -> None:
    """The golden file was written from these runs by graftpunk at da034e21 (main,
    1.31.1 plus #271 part 1); every output must match it byte for byte."""
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    assert golden_outputs(tmp_path) == golden


def _digest_of(tmp_path: Path, entries: list[dict]) -> RunDigest:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return digest(DigestSource.from_har(_write_har(tmp_path, entries)))


def _page_endpoint(tmp_path: Path, page: str) -> Endpoint:
    (endpoint,) = _digest_of(tmp_path, [_html("/search", page)]).endpoints
    return endpoint


def _found(tmp_path: Path, page: str) -> tuple[str, str] | None:
    """The target and the shape summary the digest reports for *page*."""
    state = _page_endpoint(tmp_path, page).inline_json
    if state is None:
        return None
    return state.target, summarize_shape(state.shape)


class TestTheFormsRead:
    @pytest.mark.parametrize(
        ("script", "target"),
        [
            (f"window.App = {_STATE_TEXT};", "window.App"),
            (f"App.props = {_STATE_TEXT};", "App.props"),
            (f"window.App.props = {_STATE_TEXT};", "window.App.props"),
            (f"window.__INITIAL_STATE__={_STATE_TEXT}", "window.__INITIAL_STATE__"),
            (f"window.$app.props =\n  {_STATE_TEXT};\nrender();", "window.$app.props"),
            (f"window.App = window.App || {{}}; window.App.props = {_STATE_TEXT};", None),
        ],
    )
    def test_an_assignment(self, tmp_path: Path, script: str, target: str | None) -> None:
        endpoint = _page_endpoint(tmp_path, _page(_script(script)))
        assert endpoint.inline_json is not None
        assert endpoint.inline_json.target == (target or "window.App.props")
        assert sorted(endpoint.inline_json.shape.children or {}) == ["results", "search", "total"]

    def test_a_typed_script_assignment(self, tmp_path: Path) -> None:
        page = _page(_script(f"window.App.props = {_STATE_TEXT};", ' type="text/javascript"'))
        assert _found(tmp_path, page) == ("window.App.props", _SHAPE)

    def test_a_json_script_with_an_id(self, tmp_path: Path) -> None:
        page = _page(_script(_STATE_TEXT, ' id="__NEXT_DATA__" type="application/json"'))
        assert _found(tmp_path, page) == ("script#__NEXT_DATA__", _SHAPE)

    def test_a_json_script_without_an_id(self, tmp_path: Path) -> None:
        page = _page(_script(f"\n  {_STATE_TEXT}\n", " type='application/json'"))
        assert _found(tmp_path, page) == ('script[type="application/json"]', _SHAPE)

    def test_upper_case_tags_and_attributes(self, tmp_path: Path) -> None:
        page = _page(f'<SCRIPT TYPE="application/json" ID="data">{_STATE_TEXT}</SCRIPT>')
        assert _found(tmp_path, page) == ("script#data", _SHAPE)

    @pytest.mark.parametrize("name", sorted(_NEAR_MISSES))
    def test_a_near_miss_is_not_read(self, tmp_path: Path, name: str) -> None:
        assert inline_state.inline_json(_NEAR_MISSES[name]) is None
        assert _page_endpoint(tmp_path, _NEAR_MISSES[name]).inline_json is None

    def test_an_object_just_under_the_floor_is_not_read_and_one_at_it_is(
        self, tmp_path: Path
    ) -> None:
        def sized(chars: int) -> str:
            filler = "x" * (chars - len('{"results": ""}'))
            text = json.dumps({"results": filler})
            assert len(text) == chars
            return _page(_script(f"window.App.props = {text};"))

        assert inline_state.inline_json(sized(inline_state.MIN_OBJECT_CHARS - 1)) is None
        assert _found(tmp_path, sized(inline_state.MIN_OBJECT_CHARS)) == (
            "window.App.props",
            "object{results}",
        )

    def test_the_larger_of_two_wins(self, tmp_path: Path) -> None:
        small = json.dumps({"config": "y" * inline_state.MIN_OBJECT_CHARS})
        for scripts in (
            (f"window.Config = {small};", f"window.App.props = {_STATE_TEXT};"),
            (f"window.App.props = {_STATE_TEXT};", f"window.Config = {small};"),
        ):
            assert len(small) < len(_STATE_TEXT)
            page = _page(*(_script(s) for s in scripts))
            assert _found(tmp_path, page) == ("window.App.props", _SHAPE)
        # And within one script.
        page = _page(_script(f"window.Config = {small}; window.App.props = {_STATE_TEXT};"))
        assert _found(tmp_path, page) == ("window.App.props", _SHAPE)

    def test_a_malformed_object_before_a_good_one_is_skipped(self, tmp_path: Path) -> None:
        page = _page(
            _script(f"window.Broken = {_STATE_TEXT[:-1]};"),
            _script(f"window.App.props = {_STATE_TEXT};"),
        )
        assert _found(tmp_path, page) == ("window.App.props", _SHAPE)

    def test_an_object_too_deep_for_the_parser_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The JSON parser refuses a nesting past the interpreter's recursion limit
        with RecursionError; a patch stands in for that refusal on every decode but
        the HAR's own, so the test does not depend on where one Python version's
        limit falls."""
        original = json.JSONDecoder.raw_decode

        def refuse(self: json.JSONDecoder, s: str, idx: int = 0) -> tuple[Any, int]:
            if s.startswith('{"log"'):
                return original(self, s, idx)
            raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr(json.JSONDecoder, "raw_decode", refuse)
        page = _page(_script(f"window.App.props = {_STATE_TEXT};"))
        assert inline_state.inline_json(page) is None
        assert _page_endpoint(tmp_path, page).inline_json is None

    def test_a_real_nesting_too_deep_is_ignored(self, tmp_path: Path) -> None:
        """200,000 nested arrays: Python 3.11 and 3.12 both refuse it with
        RecursionError (checked 2026-10-30); a smaller object after it is still read."""
        deep = '{"a": ' + "[" * 200_000 + "]" * 200_000 + "}"
        page = _page(
            _script(f"window.Deep = {deep};"), _script(f"window.App.props = {_STATE_TEXT};")
        )
        assert _found(tmp_path, page) == ("window.App.props", _SHAPE)

    def test_a_script_left_open_ends_the_search(self, tmp_path: Path) -> None:
        page = _page(_script(f"window.App.props = {_STATE_TEXT};")) + "<script>window.X = {"
        assert _found(tmp_path, page) == ("window.App.props", _SHAPE)
        assert inline_state.inline_json(f"<script>window.App.props = {_STATE_TEXT};") is None


class TestTheBounds:
    def test_a_script_over_the_size_cap_is_not_examined(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = f"window.App.props = {_STATE_TEXT};"
        page = _page(_script(body))
        monkeypatch.setattr(inline_state, "MAX_SCRIPT_CHARS", len(body))
        assert _found(tmp_path, page) == ("window.App.props", _SHAPE)
        monkeypatch.setattr(inline_state, "MAX_SCRIPT_CHARS", len(body) - 1)
        assert inline_state.inline_json(page) is None

    def test_only_the_first_scripts_are_examined(self, tmp_path: Path) -> None:
        filler = [_script("go();")] * (inline_state.MAX_SCRIPTS - 1)
        last = _script(f"window.App.props = {_STATE_TEXT};")
        assert _found(tmp_path, _page(*filler, last)) == ("window.App.props", _SHAPE)
        assert inline_state.inline_json(_page(*filler, _script("go();"), last)) is None

    def test_only_the_first_decodes_are_tried(self, tmp_path: Path) -> None:
        failing = [_script("window.A.b = {x};")] * (inline_state.MAX_DECODES - 1)
        last = _script(f"window.App.props = {_STATE_TEXT};")
        assert _found(tmp_path, _page(*failing, last)) == ("window.App.props", _SHAPE)
        assert inline_state.inline_json(_page(*failing, failing[0], last)) is None

    def test_only_the_first_decodes_are_tried_in_json_scripts(self, tmp_path: Path) -> None:
        failing = [_script("{x}", ' type="application/json"')] * (inline_state.MAX_DECODES - 1)
        last = _script(_STATE_TEXT, ' type="application/json"')
        target = 'script[type="application/json"]'
        assert _found(tmp_path, _page(*failing, last)) == (target, _SHAPE)
        assert inline_state.inline_json(_page(*failing, failing[0], last)) is None

    def test_only_the_first_decodes_are_tried_within_one_script(self, tmp_path: Path) -> None:
        def page(failing: int) -> str:
            return _page(
                _script("window.A.b = {x}; " * failing + f"window.App.props = {_STATE_TEXT};")
            )

        assert _found(tmp_path, page(inline_state.MAX_DECODES - 1)) == ("window.App.props", _SHAPE)
        assert inline_state.inline_json(page(inline_state.MAX_DECODES)) is None

    def test_a_decoded_object_s_own_text_is_not_searched_again(self, tmp_path: Path) -> None:
        """Assignments spelled inside a qualifying object's strings are skipped with
        it, so they never use up the decodes a later, larger object needs."""
        first = json.dumps({"code": "a.b = {}; " * 2 * inline_state.MAX_DECODES})
        assert inline_state.MIN_OBJECT_CHARS <= len(first) < len(_STATE_TEXT)
        page = _page(_script(f"window.Code = {first}; window.App.props = {_STATE_TEXT};"))
        assert _found(tmp_path, page) == ("window.App.props", _SHAPE)

    def test_cpu_time_grows_linearly_with_the_page(self) -> None:
        """Eight times the page within 32 times the CPU time, on a page built to
        defeat a quadratic scan: near-miss tag openings, one dotted name as long as
        the page is large and never assigned an object, many assignments that are
        not objects, an object that fails at its last character, the object that
        qualifies, and then scripts that are never closed."""

        def page(units: int) -> str:
            state = json.dumps({"results": [{"name": "x", "price": i} for i in range(units * 10)]})
            body = (
                ".".join(["abc"] * (units * 10))
                + " = 1; "
                + "a.b = 1; " * units
                + f"window.Broken = {state[:-1]}; "
                + f"window.App.props = {state};"
            )
            return (
                "<div>"
                + "<scrip " * (units * 10)
                + f"</div><script>{body}</script>"
                + "<script>" * (units * 20)
            )

        small_page, large_page = page(400), page(3200)
        assert len(large_page) < inline_state.MAX_SCRIPT_CHARS
        found = inline_state.inline_json(large_page)
        assert found is not None
        assert found.target == "window.App.props"
        small = _cpu_seconds(lambda: inline_state.inline_json(small_page))
        large = _cpu_seconds(lambda: inline_state.inline_json(large_page))
        assert large < 32 * small, (small, large)


def _cpu_seconds(work: Callable[[], object]) -> float:
    """The least CPU time of three runs of *work*: CPU time, never wall clock, and
    the least of three, so a busy machine slows neither side of a ratio."""
    times = []
    for _ in range(3):
        start = time.process_time()
        work()
        times.append(time.process_time() - start)
    return min(times)


class TestNamesAndValues:
    def test_a_target_part_holding_an_id_is_a_placeholder(self, tmp_path: Path) -> None:
        page = _page(_script(f"window.user_40912873.props = {_STATE_TEXT};"))
        assert _found(tmp_path, page) == ("window.{window_id}.props", _SHAPE)
        page = _page(_script(f"cus_NffrFeUfNV2Hib.props = {_STATE_TEXT};"))
        assert _found(tmp_path, page) == ("{id}.props", _SHAPE)

    def test_a_script_id_holding_an_id_is_a_placeholder(self, tmp_path: Path) -> None:
        page = _page(_script(_STATE_TEXT, ' type="application/json" id="alice@example.com"'))
        assert _found(tmp_path, page) == ("script#{id}", _SHAPE)

    def test_a_key_holding_an_id_is_a_placeholder(self, tmp_path: Path) -> None:
        state = {"results": [], "by_account": {}, "acct_40912873": {"name": "x" * 600}}
        page = _page(_script(f"window.App.props = {json.dumps(state)};"))
        assert _found(tmp_path, page) == ("window.App.props", "object{by_account, results, {key}}")

    def test_no_value_reaches_any_output(self, tmp_path: Path) -> None:
        result = _digest_of(
            tmp_path, [_html("/search", _page(_script(f"window.App.props = {_STATE_TEXT};")))]
        )
        assert result.endpoints[0].inline_json is not None
        files = render(_spec(result))
        for text in (
            render_markdown(result),
            render_json(result),
            render_endpoints_json(result),
            *files.values(),
        ):
            assert _PLANTED not in text
            assert "red widget" not in text


class TestTheReports:
    def _result(self, tmp_path: Path) -> RunDigest:
        return _digest_of(
            tmp_path,
            [
                _html("/search", _page(_script(f"window.App.props = {_STATE_TEXT};"))),
                _html("/about", _page()),
                _json("/api/orders", json.dumps({"orders": []})),
            ],
        )

    def test_the_markdown_names_the_target_and_the_shape(self, tmp_path: Path) -> None:
        markdown = render_markdown(self._result(tmp_path))
        block = markdown.split("### GET /search\n", 1)[1].split("###", 1)[0]
        assert "- shape: non-JSON\n" in block
        assert f"- inline JSON state: `window.App.props`, {_SHAPE}\n" in block
        about = markdown.split("### GET /about\n", 1)[1].split("###", 1)[0]
        assert "inline JSON" not in about

    def test_the_markdown_keeps_its_order(self, tmp_path: Path) -> None:
        markdown = render_markdown(self._result(tmp_path))
        headings = [line for line in markdown.splitlines() if line.startswith("### ")]
        assert headings == ["### GET /api/orders", "### GET /about", "### GET /search"]

    def test_the_projection_carries_the_target_and_the_shape(self, tmp_path: Path) -> None:
        payload = endpoints_projection(self._result(tmp_path))
        assert payload["schema"] == 1
        by_template = {entry["template"]: entry for entry in payload["endpoints"]}
        assert by_template["/search"]["inline_json"] == {
            "target": "window.App.props",
            "shape": _SHAPE,
        }
        assert set(by_template["/search"]) == _INLINE_ENDPOINT_V1
        assert set(by_template["/about"]) == _ENDPOINT_V1
        assert set(by_template["/api/orders"]) == _ENDPOINT_V1

    def test_the_json_carries_the_target_and_the_shape_node(self, tmp_path: Path) -> None:
        endpoints = json.loads(render_json(self._result(tmp_path)))["endpoints"]
        by_template = {entry["template"]: entry for entry in endpoints}
        state = by_template["/search"]["inline_json"]
        assert state["target"] == "window.App.props"
        assert sorted(state["shape"]["children"]) == ["results", "search", "total"]
        assert "inline_json" not in by_template["/about"]

    def test_it_describes_the_fixture_recording(self, tmp_path: Path) -> None:
        """Two recordings of one page: the state reported is the fixture
        recording's, the one the generated test reads."""
        other = json.dumps({"other": "z" * 600})
        result = _digest_of(
            tmp_path,
            [
                _html("/search", _page(_script(f"window.App.props = {_STATE_TEXT};"))),
                _html("/search", _page(_script(f"window.Other = {other};"))),
            ],
        )
        (endpoint,) = result.endpoints
        assert endpoint.inline_json is not None
        assert endpoint.inline_json.target == "window.App.props"

    def test_a_family_row_carries_its_fixture_member_s_state(self, tmp_path: Path) -> None:
        page = _page(_script(f"window.App.props = {_STATE_TEXT};"))
        result = _digest_of(
            tmp_path,
            [
                _html("/search/north-region/items/", page),
                _html("/search/south-region/items/", page),
            ],
        )
        (family,) = (e for e in result.endpoints if e.family_members)
        assert family.inline_json is not None
        assert family.inline_json.target == "window.App.props"
        assert all(e.inline_json is not None for e in result.endpoints)


# --- the scaffold ---------------------------------------------------------------


_EXAMPLE_START = "# def inline_state(page: str) -> dict:"
_EXAMPLE_END = "#     return state"


def _uncommented(code: str) -> str:
    """*code* with the inline JSON reader uncommented, as an author who wants it
    would leave it: each line from its ``def`` to its ``return`` loses its ``# ``
    (a bare ``#`` becomes a blank line)."""
    out: list[str] = []
    inside = False
    for line in code.split("\n"):
        stripped = line.lstrip()
        pad = line[: len(line) - len(stripped)]
        if stripped == _EXAMPLE_START:
            inside = True
        if inside:
            out.append("" if stripped == "#" else pad + stripped[2:])
            if stripped == _EXAMPLE_END:
                inside = False
            continue
        out.append(line)
    assert not inside
    return "\n".join(out)


def _example_lines(code: str) -> list[str]:
    lines = code.split("\n")
    start = next(i for i, line in enumerate(lines) if line.strip() == _EXAMPLE_START)
    end = next(i for i, line in enumerate(lines) if line.strip() == _EXAMPLE_END)
    return lines[start : end + 1]


def _run_example(code: str, page: str) -> Any:
    """The uncommented reader, defined on its own and called on *page*."""
    source = _uncommented("\n".join(_example_lines(code)))
    namespace: dict[str, Any] = {}
    exec(compile(textwrap.dedent(source), "example.py", "exec"), namespace)  # noqa: S102
    return namespace["inline_state"](page)


def _ruff(tree: Path) -> None:
    for argv in (["check", str(tree)], ["format", "--check", str(tree)]):
        linted = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [sys.executable, "-m", "ruff", *argv], capture_output=True, text=True
        )
        assert linted.returncode == 0, linted.stdout + linted.stderr


def _write(root: Path, files: dict[str, str]) -> Path:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if relative.endswith(".py"):
            for number, line in enumerate(content.splitlines(), start=1):
                assert len(line) <= GENERATED_LINE_LENGTH, f"{relative}:{number}: {line!r}"
    return root


_PAGES = {
    "assignment": (
        _page(_script(f"window.App.props = {_STATE_TEXT};")),
        "window.App.props",
    ),
    "next-data": (
        _page(_script(_STATE_TEXT, ' id="__NEXT_DATA__" type="application/json"')),
        "script#__NEXT_DATA__",
    ),
    "json-script": (
        _page(_script(_STATE_TEXT, ' type="application/json"')),
        'script[type="application/json"]',
    ),
    "placeholder": (
        _page(_script(f"window.user_40912873.props = {_STATE_TEXT};")),
        "window.{window_id}.props",
    ),
    "long-name": (
        _page(_script(f"window.{'VeryLongApplicationName' * 4}.props = {_STATE_TEXT};")),
        f"window.{'VeryLongApplicationName' * 4}.props",
    ),
}


def _example_stub(tmp_path: Path) -> list[str]:
    """The search stub ``gp plugin new`` writes for the assignment page, from its
    decorator to its last line, at class-body indentation."""
    result = _digest_of(tmp_path, [_html("/search", _PAGES["assignment"][0])])
    code = render(_spec(result))["src/graftpunk_myshop/plugin.py"]
    lines = code.rstrip("\n").split("\n")
    return lines[next(i for i, line in enumerate(lines) if line == "    @command(") :]


class TestTheStub:
    def _plugin(self, tmp_path: Path, page: str, path: str = "/search") -> tuple[str, dict]:
        result = _digest_of(tmp_path, [_html(path, page)])
        files = render(_spec(result))
        return files["src/graftpunk_myshop/plugin.py"], files

    def test_the_comment_names_the_target_and_the_shape(self, tmp_path: Path) -> None:
        code, _files = self._plugin(tmp_path, _PAGES["assignment"][0])
        lines = [line.strip() for line in code.split("\n")]
        start = next(i for i, line in enumerate(lines) if line.startswith("# The page carries"))
        end = lines.index(_EXAMPLE_START)
        comment = " ".join(" ".join(line.removeprefix("#") for line in lines[start:end]).split())
        assert comment == (
            "The page carries its data as inline JSON state: window.App.props, shaped "
            f"{_SHAPE} (gp observe digest). To return that object instead of the page, "
            "uncomment inline_state below, return inline_state(ctx.request_text(...)) with "
            "the request's own arguments, and set the return annotation to dict."
        )

    def test_the_request_is_unchanged(self, tmp_path: Path) -> None:
        with_state, _ = self._plugin(tmp_path / "a", _PAGES["assignment"][0])
        without, _ = self._plugin(tmp_path / "b", _page())

        def live(code: str) -> list[str]:
            return [line for line in code.split("\n") if line.strip() and line.strip()[0] != "#"]

        assert live(with_state) == live(without)
        assert "        return ctx.request_text(" in with_state

    @pytest.mark.parametrize("name", sorted(_PAGES))
    def test_the_example_compiles_and_is_ruff_clean_both_ways(
        self, tmp_path: Path, name: str
    ) -> None:
        page, _target = _PAGES[name]
        code, files = self._plugin(tmp_path / "run", page)
        compile(code, "plugin.py", "exec")
        _ruff(_write(tmp_path / "commented", files))
        uncommented = _uncommented(code)
        assert uncommented != code
        compile(uncommented, "plugin.py", "exec")
        tree = {**files, "src/graftpunk_myshop/plugin.py": uncommented}
        _ruff(_write(tmp_path / "uncommented", tree))

    @pytest.mark.parametrize("name", sorted(_PAGES))
    def test_the_example_reads_the_state_from_the_page(self, tmp_path: Path, name: str) -> None:
        page, target = _PAGES[name]
        assert _found(tmp_path / "found", page) == (target, _SHAPE)
        code, _files = self._plugin(tmp_path / "run", page)
        assert _run_example(code, page) == _STATE

    def test_the_example_reads_past_a_smaller_earlier_object(self, tmp_path: Path) -> None:
        small = json.dumps({"config": "y" * inline_state.MIN_OBJECT_CHARS})
        page = _page(
            _script(f"window.Config = {small};"),
            _script(f"window.App.props = {_STATE_TEXT};"),
        )
        code, _files = self._plugin(tmp_path, page)
        assert _run_example(code, page) == _STATE

    def test_the_example_says_so_when_the_state_is_gone(self, tmp_path: Path) -> None:
        code, _files = self._plugin(tmp_path, _PAGES["assignment"][0])
        with pytest.raises(ValueError, match="the inline JSON state is not on the page"):
            _run_example(code, _page())

    def test_after_a_path_parameter_too(self, tmp_path: Path) -> None:
        code, files = self._plugin(
            tmp_path / "run", _PAGES["assignment"][0], path="/orders/40912873"
        )
        assert '        order_id = _quote_path(order_id, safe="")' in code
        _ruff(_write(tmp_path / "commented", files))
        uncommented = _uncommented(code)
        _ruff(_write(tmp_path / "tree", {**files, "src/graftpunk_myshop/plugin.py": uncommented}))
        assert _run_example(code, _PAGES["assignment"][0]) == _STATE

    def test_add_command_writes_the_same_stub(self, tmp_path: Path) -> None:
        result = _digest_of(tmp_path, [_html("/search", _PAGES["assignment"][0])])
        (command,) = planned_commands(result, (CommandSelection("search", "GET", "/search"),))
        lines = render_command(command, result, _HOST).lines
        assert lines == tuple(_example_stub(tmp_path / "new"))

    def test_no_state_writes_no_example(self, tmp_path: Path) -> None:
        code, _files = self._plugin(tmp_path, _page())
        assert "inline JSON" not in code
        assert "inline_state" not in code
