"""The login engine's page-side probes, run as JavaScript (#275).

The readiness probe and the target probe are JavaScript the engine evaluates in
the page. These tests run that exact source under Node against a small fake of
the browser objects it touches (``document``, ``performance``, script and element
nodes), so the rules written in JavaScript are tested as JavaScript. The fakes
model the browser's answers, not its behaviour: a real page is exercised by the
live probes recorded in the pull request. Without ``node`` on PATH these tests
are skipped.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

import pytest

from graftpunk.plugins.login_ready import _READY_JS, target_js

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

_BASE = "https://app.example.com"

# A fake browser environment, configured from SPEC (JSON), then EXPR is evaluated
# and its string result printed.
_HARNESS = """
const spec = SPEC;
globalThis.window = globalThis;
window.scrollX = spec.scrollX || 0;
window.scrollY = spec.scrollY || 0;
window.innerWidth = spec.innerWidth || 1024;
globalThis.performance = {
  now: () => spec.now,
  getEntriesByType: (type) => (type === "resource" ? spec.entries || [] : []),
};
globalThis.MutationObserver = class { constructor() {} observe() {} };
if (spec.knowsNoModule !== false) {
  globalThis.HTMLScriptElement = function () {};
  HTMLScriptElement.prototype.noModule = false;
}
if (spec.quietLast !== undefined) window.__graftpunkQuiet = {last: spec.quietLast};
const script = (s) => ({
  src: s.src,
  getAttribute: (name) => (name in s ? s[name] : null),
  hasAttribute: (name) => !!s[name],
});
const node = (n) => ({
  isConnected: n.connected !== false,
  getClientRects: () => ({length: n.rects === undefined ? 1 : n.rects}),
  checkVisibility: spec.noCheckVisibility ? undefined : (opts) => {
    if (n.cssHidden) return false;
    if (n.transparent && opts.opacityProperty && opts.checkOpacity) return false;
    return true;
  },
  getBoundingClientRect: () => n.box || {left: 10, top: 10, right: 110, bottom: 40},
  matches: (query) => query === ":disabled" && !!n.disabled,
  _style: {visibility: n.cssHidden ? "hidden" : "visible", display: "block",
           opacity: n.transparent ? "0" : "1"},
});
globalThis.getComputedStyle = (el) => el._style;
globalThis.document = {
  readyState: spec.readyState || "complete",
  documentElement: {scrollWidth: spec.scrollWidth || window.innerWidth},
  querySelectorAll: (selector) => {
    if (selector === "script[src]") return (spec.scripts || []).map(script);
    if (selector === "link[rel][href]") {
      const links = (spec.links || []).concat(
        (spec.preloads || []).map((href) => ({href, rel: "modulepreload"})),
      );
      return links.map((l) => ({href: l.href, getAttribute: (name) => l[name] ?? null}));
    }
    if (spec.selectorThrows) throw new SyntaxError("bad selector");
    return (spec.nodes || []).map(node);
  },
};
if (spec.priorEntries) {
  // An earlier read of the same document, which saw these entries.
  const later = spec.entries;
  spec.entries = spec.priorEntries;
  EXPR;
  spec.entries = later;
}
if (spec.reads) {
  // Successive reads of one document: each applies its changes to the page,
  // then reads. All the readings are printed, in order.
  const readings = [];
  for (const change of spec.reads) {
    Object.assign(spec, change);
    readings.push(JSON.parse(EXPR));
  }
  process.stdout.write(JSON.stringify(readings));
} else {
  process.stdout.write(EXPR);
}
"""


def _run(expression: str, spec: dict[str, Any]) -> Any:
    assert NODE is not None
    program = _HARNESS.replace("SPEC", json.dumps(spec)).replace("EXPR", expression)
    result = subprocess.run(  # noqa: S603 - our own program, run by node
        [NODE, "-e", program], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(result.stdout)


def _ready(**spec: Any) -> dict[str, Any]:
    spec.setdefault("now", 10_000)
    spec.setdefault("quietLast", 1_000)
    return _run(_READY_JS, spec)


def _entry(name: str, initiator: str = "script", end: float = 500) -> dict[str, Any]:
    return {"name": name, "initiatorType": initiator, "responseEnd": end}


class TestPendingScripts:
    def test_a_fetched_script_with_no_timing_entry_is_pending(self) -> None:
        reading = _ready(scripts=[{"src": f"{_BASE}/chunk.js"}])
        assert reading["pending_scripts"] == 1
        assert reading["quiet_ms"] == 0

    def test_a_script_with_its_timing_entry_is_not_pending(self) -> None:
        reading = _ready(scripts=[{"src": f"{_BASE}/app.js"}], entries=[_entry(f"{_BASE}/app.js")])
        assert reading["pending_scripts"] == 0
        assert reading["quiet_ms"] == 9_000

    @pytest.mark.parametrize("flag", ["async", "defer"])
    def test_an_async_or_defer_script_counts_by_its_entry(self, flag: str) -> None:
        src = f"{_BASE}/{flag}.js"
        assert _ready(scripts=[{"src": src, flag: True}])["pending_scripts"] == 1
        fetched = _ready(scripts=[{"src": src, flag: True}], entries=[_entry(src)])
        assert fetched["pending_scripts"] == 0

    def test_a_module_script_is_fetched(self) -> None:
        reading = _ready(scripts=[{"src": f"{_BASE}/app.mjs", "type": "module"}])
        assert reading["pending_scripts"] == 1

    def test_a_nomodule_script_is_never_fetched_by_a_browser_that_knows_nomodule(self) -> None:
        reading = _ready(scripts=[{"src": f"{_BASE}/legacy.js", "nomodule": True}])
        assert reading["pending_scripts"] == 0
        assert reading["quiet_ms"] > 0

    def test_a_nomodule_script_is_fetched_by_a_browser_that_does_not_know_it(self) -> None:
        reading = _ready(
            scripts=[{"src": f"{_BASE}/legacy.js", "nomodule": True}], knowsNoModule=False
        )
        assert reading["pending_scripts"] == 1

    def test_a_module_script_ignores_nomodule_and_is_fetched(self) -> None:
        """The HTML spec skips nomodule only for a classic script."""
        script = {"src": f"{_BASE}/app.mjs", "type": "module", "nomodule": True}
        assert _ready(scripts=[script])["pending_scripts"] == 1

    @pytest.mark.parametrize(
        ("attributes", "pending"),
        [
            ({"language": "vbscript"}, 0),  # type string text/vbscript
            ({"language": "javascript"}, 1),  # text/javascript
            ({"language": "javascript1.2"}, 1),
            ({"type": "", "language": "vbscript"}, 1),  # an empty type wins: JavaScript
            ({"language": ""}, 1),
            ({"type": "text/plain", "language": "javascript"}, 0),  # the type wins
        ],
    )
    def test_the_language_attribute_sets_the_type_when_there_is_none(
        self, attributes: dict[str, str], pending: int
    ) -> None:
        script = {"src": f"{_BASE}/legacy.js", **attributes}
        assert _ready(scripts=[script])["pending_scripts"] == pending

    def test_a_script_fetched_before_a_clear_stays_fetched(self) -> None:
        """After performance.clearResourceTimings() the buffer has no entry for a
        script that loaded long ago; the probe remembers the entries it read."""
        earlier = [_entry(f"{_BASE}/app.js"), _entry(f"{_BASE}/vendor.js")]
        reading = _ready(
            scripts=[{"src": f"{_BASE}/app.js"}, {"src": f"{_BASE}/vendor.js"}],
            priorEntries=earlier,
            entries=[],
        )
        assert reading["pending_scripts"] == 0

    def test_a_script_inserted_after_a_clear_is_pending_until_its_entry_arrives(self) -> None:
        app, chunk = f"{_BASE}/app.js", f"{_BASE}/chunk.js"
        readings = _ready(
            reads=[
                {"now": 1_000, "scripts": [{"src": app}], "entries": [_entry(app, end=500)]},
                {"now": 2_000, "entries": []},  # the page clears the buffer
                {"now": 3_000, "scripts": [{"src": app}, {"src": chunk}]},  # route chunk
                {"now": 4_000, "entries": [_entry(chunk, end=3_500)]},
            ]
        )
        assert [r["pending_scripts"] for r in readings] == [0, 0, 1, 0]
        assert readings[2]["quiet_ms"] == 0
        assert readings[3]["quiet_ms"] == 500

    def test_a_clear_does_not_erase_the_last_finish_time(self) -> None:
        """A script that finished 100 ms ago and was then cleared still counts."""
        readings = _ready(
            reads=[
                {"now": 9_850, "entries": [_entry(f"{_BASE}/a.js", end=9_800)]},
                {"now": 9_900, "entries": []},
            ]
        )
        assert readings[1]["quiet_ms"] == 100

    def test_a_script_cleared_before_any_read_saw_it_holds_the_wait(self) -> None:
        """The safe direction: unseen, it reads as still downloading."""
        reading = _ready(scripts=[{"src": f"{_BASE}/app.js"}], entries=[])
        assert reading["pending_scripts"] == 1

    def test_a_buffer_that_only_grew_keeps_the_check(self) -> None:
        reading = _ready(
            scripts=[{"src": f"{_BASE}/late.js"}],
            priorEntries=[_entry(f"{_BASE}/app.js")],
            entries=[_entry(f"{_BASE}/app.js"), _entry(f"{_BASE}/b.js")],
        )
        assert reading["pending_scripts"] == 1

    @pytest.mark.parametrize(
        "kind", ["text/plain", "application/json", "text/template", "text/partytown"]
    )
    def test_a_non_javascript_type_is_never_fetched(self, kind: str) -> None:
        reading = _ready(scripts=[{"src": f"{_BASE}/tag.js", "type": kind}])
        assert reading["pending_scripts"] == 0

    @pytest.mark.parametrize("kind", ["", "text/javascript", " Application/JavaScript "])
    def test_a_javascript_type_is_fetched(self, kind: str) -> None:
        reading = _ready(scripts=[{"src": f"{_BASE}/app.js", "type": kind}])
        assert reading["pending_scripts"] == 1

    @pytest.mark.parametrize("src", ["data:text/javascript,1", "blob:https://app.example.com/x"])
    def test_data_and_blob_urls_get_no_timing_entry_and_are_left_out(self, src: str) -> None:
        assert _ready(scripts=[{"src": src}])["pending_scripts"] == 0

    def test_a_full_timing_buffer_skips_the_check(self) -> None:
        entries = [_entry(f"{_BASE}/img{i}.png", "img") for i in range(250)]
        reading = _ready(scripts=[{"src": f"{_BASE}/late.js"}], entries=entries)
        assert reading["pending_scripts"] == 0


class TestQuietClock:
    def test_a_script_finishing_resets_the_quiet_time(self) -> None:
        reading = _ready(entries=[_entry(f"{_BASE}/a.js", "script", end=9_800)])
        assert reading["quiet_ms"] == 200

    @pytest.mark.parametrize("initiator", ["img", "css", "link", "beacon", "other"])
    def test_images_styles_and_beacons_do_not(self, initiator: str) -> None:
        reading = _ready(entries=[_entry(f"{_BASE}/x", initiator, end=9_800)])
        assert reading["quiet_ms"] == 9_000

    @pytest.mark.parametrize("initiator", ["fetch", "xmlhttprequest", "iframe"])
    def test_data_requests_and_frames_do(self, initiator: str) -> None:
        reading = _ready(entries=[_entry(f"{_BASE}/x", initiator, end=9_900)])
        assert reading["quiet_ms"] == 100

    def test_the_first_read_of_a_document_starts_its_clock(self) -> None:
        reading = _run(_READY_JS, {"now": 10_000})
        assert reading["quiet_ms"] == 0

    def test_the_document_state_is_reported(self) -> None:
        assert _ready(readyState="interactive")["state"] == "interactive"

    @pytest.mark.parametrize("initiator", ["link", "other"])
    def test_a_script_preload_finishing_resets_the_quiet_time(self, initiator: str) -> None:
        """A bundler's entry module runs once its preloaded chunks arrive."""
        chunk = f"{_BASE}/vendor.js"
        reading = _ready(entries=[_entry(chunk, initiator, end=9_800)], preloads=[chunk])
        assert reading["quiet_ms"] == 200

    @pytest.mark.parametrize(
        ("rel", "as_", "counts"),
        [
            ("modulepreload", None, True),
            ("ModulePreload", None, True),
            ("preload", "script", True),
            ("Preload", "SCRIPT", True),
            ("preload prefetch", "script", True),
            ("  prefetch\tpreload ", " Script ", True),
            ("stylesheet modulepreload", None, True),
            ("preload", "style", False),
            ("preload", None, False),
            ("prefetch", "script", False),
            ("preloads", "script", False),
        ],
    )
    def test_preload_links_match_rel_as_a_token_list_in_any_case(
        self, rel: str, as_: str | None, counts: bool
    ) -> None:
        chunk = f"{_BASE}/vendor.js"
        link: dict[str, str] = {"href": chunk, "rel": rel}
        if as_ is not None:
            link["as"] = as_
        reading = _ready(entries=[_entry(chunk, "other", end=9_800)], links=[link])
        assert reading["quiet_ms"] == (200 if counts else 9_000)

    def test_a_stylesheet_link_finishing_does_not(self) -> None:
        reading = _ready(
            entries=[_entry(f"{_BASE}/site.css", "link", end=9_800)],
            preloads=[f"{_BASE}/vendor.js"],
        )
        assert reading["quiet_ms"] == 9_000


def _target(nodes: list[dict[str, Any]], **spec: Any) -> dict[str, Any]:
    return _run(target_js("a.signin"), {"now": 0, "nodes": nodes, **spec})


class TestTargetProbe:
    def test_counts_and_flags_each_match(self) -> None:
        probe = _target([{}, {"disabled": True}])
        assert probe == {"count": 2, "visible": [True, True], "disabled": [False, True]}

    @pytest.mark.parametrize(
        "hidden",
        [
            {"rects": 0},  # display: none, here or on an ancestor
            {"connected": False},
            {"cssHidden": True},
            {"transparent": True},
            {"box": {"left": -300, "top": 10, "right": -20, "bottom": 40}},  # off-canvas left
            {"box": {"left": 10, "top": -90, "right": 110, "bottom": -60}},  # above the page
            {"box": {"left": 1100, "top": 10, "right": 1300, "bottom": 40}},  # past the right
        ],
    )
    def test_a_copy_no_user_can_see_is_not_visible(self, hidden: dict[str, Any]) -> None:
        assert _target([hidden, {}])["visible"] == [False, True]

    def test_a_page_wider_than_the_window_keeps_its_right_column_visible(self) -> None:
        """A fixed-width layout in a narrow window: the column is reachable by
        scrolling right, so it is not an off-canvas copy."""
        column = {"box": {"left": 1_100, "top": 10, "right": 1_300, "bottom": 40}}
        assert _target([column], innerWidth=1_024, scrollWidth=1_400)["visible"] == [True]

    def test_an_unclipped_drawer_that_widens_the_page_reads_as_visible(self) -> None:
        """A known limit, pinned: an absolutely positioned or transformed copy past
        the window that no ancestor clips widens the page's scroll width to cover
        itself, and nothing the probe reads tells it from a fixed-width column."""
        drawer = {"box": {"left": 1_100, "top": 10, "right": 1_400, "bottom": 40}}
        assert _target([drawer], innerWidth=1_024, scrollWidth=1_400)["visible"] == [True]

    def test_past_the_right_edge_of_a_page_that_cannot_scroll_is_hidden(self) -> None:
        drawer = {"box": {"left": 1_024, "top": 10, "right": 1_324, "bottom": 40}}
        assert _target([drawer], innerWidth=1_024, scrollWidth=1_024)["visible"] == [False]

    def test_scrolled_right_the_page_edge_still_decides(self) -> None:
        column = {"box": {"left": 700, "top": 10, "right": 900, "bottom": 40}}
        assert _target([column], innerWidth=1_024, scrollWidth=1_400, scrollX=600)["visible"] == [
            True
        ]
        past = {"box": {"left": 900, "top": 10, "right": 1_000, "bottom": 40}}
        assert _target([past], innerWidth=1_024, scrollWidth=1_400, scrollX=600)["visible"] == [
            False
        ]

    def test_an_element_below_the_fold_is_visible(self) -> None:
        below = {"box": {"left": 10, "top": 3_000, "right": 110, "bottom": 3_030}}
        assert _target([below])["visible"] == [True]

    def test_a_page_scrolled_down_keeps_an_element_above_the_viewport_visible(self) -> None:
        above = {"box": {"left": 10, "top": -400, "right": 110, "bottom": -370}}
        assert _target([above], scrollY=800)["visible"] == [True]

    def test_without_check_visibility_the_computed_style_decides(self) -> None:
        probe = _target([{"cssHidden": True}, {"transparent": True}, {}], noCheckVisibility=True)
        assert probe["visible"] == [False, False, True]

    def test_only_the_first_fifty_matches_are_described(self) -> None:
        probe = _target([{}] * 60)
        assert probe["count"] == 60
        assert len(probe["visible"]) == 50

    def test_a_selector_the_page_rejects_is_reported_as_an_error(self) -> None:
        assert "error" in _target([], selectorThrows=True)
