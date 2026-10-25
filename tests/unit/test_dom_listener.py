"""The isolated-world listener gp observe installs while recording (#267, part 2).

The script runs in node against a small fake DOM: it snapshots the page on a
click, a submit, or an Enter keydown while a password input holds a value, and
never touches the event or the page when anything is missing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from graftpunk.observe.dom_listener import (
    BINDING_NAME,
    DEBOUNCE_MS,
    LISTENER_SCRIPT,
    WORLD_NAME,
)

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

# A fake window and document: the listener's whole view of the page. Each case
# sets the password values, the binding, and the events to fire, and prints what
# the binding received and what happened to each event.
_HARNESS = r"""
const config = JSON.parse(process.argv[2]);
const listeners = [];
const calls = [];
let now = 1700000000000;
Date.now = () => now;
const inputs = config.passwords.map((value) => ({ value }));
const doc = {
  URL: "https://myshop.example/account?next=%2F",
  documentElement: { get outerHTML() {
    if (config.throwOnSerialize) throw new Error("boom");
    return "<html><body>" + config.html + "</body></html>";
  } },
  querySelectorAll(selector) {
    if (!/password/.test(selector)) return [];
    return inputs;
  },
};
globalThis.document = doc;
globalThis.window = globalThis;
globalThis.addEventListener = (type, handler, options) => {
  listeners.push({ type, handler, options });
};
if (config.binding) {
  globalThis[config.bindingName] = (payload) => {
    if (typeof payload !== "string") throw new Error("binding takes a string");
    calls.push(JSON.parse(payload));
  };
}
let threw = null;
try {
  require("vm").runInThisContext(config.script);
} catch (error) {
  threw = String(error);
}
const events = [];
for (const fired of config.fire) {
  now += fired.after || 0;
  if (fired.html !== undefined) config.html = fired.html;
  const event = {
    type: fired.type,
    key: fired.key,
    prevented: false,
    stopped: false,
    preventDefault() { this.prevented = true; },
    stopPropagation() { this.stopped = true; },
    stopImmediatePropagation() { this.stopped = true; },
  };
  let escaped = null;
  for (const l of listeners.filter((l) => l.type === fired.type)) {
    try { l.handler(event); } catch (error) { escaped = String(error); }
  }
  events.push({ prevented: event.prevented, stopped: event.stopped, escaped });
}
console.log(JSON.stringify({
  threw,
  calls,
  events,
  listeners: listeners.map((l) => ({ type: l.type, options: l.options })),
}));
"""


def _run(
    tmp_path: Path,
    *,
    passwords: list[str],
    fire: list[dict],
    binding: bool = True,
    html: str = '<input type="password">',
    throw_on_serialize: bool = False,
) -> dict:
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS)
    config = {
        "script": LISTENER_SCRIPT,
        "bindingName": BINDING_NAME,
        "binding": binding,
        "passwords": passwords,
        "fire": fire,
        "html": html,
        "throwOnSerialize": throw_on_serialize,
    }
    assert NODE is not None
    out = subprocess.run(  # noqa: S603 - node from PATH, our own harness
        [NODE, str(harness), json.dumps(config)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(out.stdout)


class TestTheScriptStructure:
    def test_names_are_fixed_and_the_debounce_is_short(self) -> None:
        assert WORLD_NAME == "graftpunk-observe"
        assert BINDING_NAME.isidentifier()
        assert BINDING_NAME in LISTENER_SCRIPT
        assert 0 < DEBOUNCE_MS <= 1000

    def test_it_never_changes_an_event(self) -> None:
        for call in ("preventDefault", "stopPropagation", "stopImmediatePropagation"):
            assert call not in LISTENER_SCRIPT

    @needs_node
    def test_it_parses(self, tmp_path: Path) -> None:
        script = tmp_path / "listener.js"
        script.write_text(LISTENER_SCRIPT)
        assert NODE is not None
        subprocess.run([NODE, "--check", str(script)], check=True, timeout=30)  # noqa: S603


@needs_node
class TestTheScriptInAFakePage:
    def test_capture_phase_listeners_for_the_three_events(self, tmp_path: Path) -> None:
        result = _run(tmp_path, passwords=["x"], fire=[])
        assert result["threw"] is None
        assert sorted(each["type"] for each in result["listeners"]) == [
            "click",
            "keydown",
            "submit",
        ]
        assert all(each["options"]["capture"] is True for each in result["listeners"])

    def test_a_click_with_a_filled_password_sends_the_page(self, tmp_path: Path) -> None:
        result = _run(tmp_path, passwords=["", "hunter2"], fire=[{"type": "click"}])
        (call,) = result["calls"]
        assert call["html"] == '<html><body><input type="password"></body></html>'
        assert call["url"] == "https://myshop.example/account?next=%2F"
        assert call["time"] == 1700000000000
        assert result["events"] == [{"prevented": False, "stopped": False, "escaped": None}]

    def test_an_empty_password_sends_nothing(self, tmp_path: Path) -> None:
        result = _run(tmp_path, passwords=["", ""], fire=[{"type": "click"}, {"type": "submit"}])
        assert result["calls"] == []

    def test_only_enter_among_keys(self, tmp_path: Path) -> None:
        result = _run(
            tmp_path,
            passwords=["x"],
            fire=[{"type": "keydown", "key": "a"}, {"type": "keydown", "key": "Enter"}],
        )
        assert len(result["calls"]) == 1

    def test_one_action_sends_one_snapshot(self, tmp_path: Path) -> None:
        """A click on a submit button fires click then submit on the same page."""
        result = _run(
            tmp_path,
            passwords=["x"],
            fire=[
                {"type": "click"},
                {"type": "submit", "after": 5},
                {"type": "click", "after": DEBOUNCE_MS + 1},
                {"type": "click", "after": 1, "html": "<p>changed</p>"},
            ],
        )
        assert len(result["calls"]) == 3

    def test_without_the_binding_it_does_nothing(self, tmp_path: Path) -> None:
        result = _run(tmp_path, passwords=["x"], fire=[{"type": "click"}], binding=False)
        assert result["threw"] is None
        assert result["calls"] == []
        assert result["events"] == [{"prevented": False, "stopped": False, "escaped": None}]

    def test_a_failure_never_reaches_the_page(self, tmp_path: Path) -> None:
        result = _run(tmp_path, passwords=["x"], fire=[{"type": "click"}], throw_on_serialize=True)
        assert result["calls"] == []
        assert result["events"] == [{"prevented": False, "stopped": False, "escaped": None}]
