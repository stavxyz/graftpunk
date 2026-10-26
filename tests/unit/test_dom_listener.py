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
    INTERACTION_TYPE,
    LISTENER_SCRIPT,
    MAX_INTERACTIONS_PER_SECOND,
    STRUCTURAL_DEPTH,
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
    def test_capture_phase_listeners_for_snapshots_and_interactions(self, tmp_path: Path) -> None:
        result = _run(tmp_path, passwords=["x"], fire=[])
        assert result["threw"] is None
        assert sorted(each["type"] for each in result["listeners"]) == [
            "click",
            "click",
            "focusin",
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
        """Each key is far enough from the last for the debounce not to hide it."""
        apart = DEBOUNCE_MS + 1
        result = _run(
            tmp_path,
            passwords=["x"],
            fire=[
                {"type": "keydown", "key": "a"},
                {"type": "keydown", "key": "Tab", "after": apart},
                {"type": "keydown", "key": "Enter", "after": apart},
            ],
        )
        assert [call["time"] for call in result["calls"]] == [1700000000000 + 2 * apart]

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


# A fake page with an element tree for the interaction events (#267, part 3):
# elements with tags, attributes, text, and values, a selector engine for the
# selectors the listener writes (tag, #id with CSS escapes, [attr="value"],
# :nth-of-type(n), descendant and child combinators, and comma lists), and
# CSS.escape as the CSSOM spec defines it. Each fired event names its target by a
# harness-only key, never an attribute.
_DOM_HARNESS = r"""
const config = JSON.parse(process.argv[2]);
const listeners = [];
const calls = [];
let now = 1700000000000;
Date.now = () => now;
// The monotonic clock, apart from the wall clock so a test can step the wall back.
let mono = 5000;
globalThis.performance = { now: () => mono };

function cssEscape(value) {
  const s = String(value);
  let out = "";
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c === 0) { out += "\uFFFD"; continue; }
    if ((c >= 1 && c <= 31) || c === 127 || (i === 0 && c >= 48 && c <= 57)
        || (i === 1 && c >= 48 && c <= 57 && s.charCodeAt(0) === 45)) {
      out += "\\" + c.toString(16) + " ";
      continue;
    }
    if (i === 0 && c === 45 && s.length === 1) { out += "\\-"; continue; }
    if (c >= 128 || c === 45 || c === 95 || (c >= 48 && c <= 57)
        || (c >= 65 && c <= 90) || (c >= 97 && c <= 122)) {
      out += s[i];
      continue;
    }
    out += "\\" + s[i];
  }
  return out;
}
function cssUnescape(s) {
  return s.replace(/\\([0-9a-fA-F]{1,6}) ?|\\([\s\S])/g,
    (m, hex, ch) => hex ? String.fromCodePoint(parseInt(hex, 16)) : ch);
}

function splitTop(text, separators) {
  const parts = [];
  let depth = 0, quote = null, start = 0;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === "\\") {
      // An escape: one character, or up to six hex digits and one optional space.
      const hex = text.slice(i + 1).match(/^[0-9a-fA-F]{1,6} ?/);
      i += hex ? hex[0].length : 1;
      continue;
    }
    if (quote) { if (ch === quote) quote = null; continue; }
    if (ch === '"' || ch === "'") { quote = ch; continue; }
    if (ch === "[" || ch === "(") depth++;
    else if (ch === "]" || ch === ")") depth--;
    else if (depth === 0 && separators.includes(ch)) {
      parts.push(text.slice(start, i));
      start = i + 1;
    }
  }
  parts.push(text.slice(start));
  return parts;
}
function parseCompound(text) {
  const tests = [];
  let rest = text.trim();
  if (!rest) throw new Error("empty compound");
  while (rest) {
    let m;
    if ((m = rest.match(/^[a-zA-Z][\w-]*/))) {
      const tag = m[0].toLowerCase();
      tests.push((el) => el.localName === tag);
    } else if ((m = rest.match(/^#((?:\\[0-9a-fA-F]{1,6} ?|\\[\s\S]|[\w-]|[^\x00-\x7f])+)/))) {
      const id = cssUnescape(m[1]);
      tests.push((el) => el.getAttribute("id") === id);
    } else if ((m = rest.match(/^\[([\w-]+)(?:=("((?:\\[\s\S]|[^"\\])*)"|[\w-]+))?( i)?\]/))) {
      const name = m[1];
      const value = m[3] !== undefined ? cssUnescape(m[3]) : m[2];
      const fold = Boolean(m[4]);
      tests.push((el) => {
        const got = el.getAttribute(name);
        if (got === null) return false;
        if (value === undefined) return true;
        return fold ? got.toLowerCase() === value.toLowerCase() : got === value;
      });
    } else if ((m = rest.match(/^:nth-of-type\((\d+)\)/))) {
      const n = Number(m[1]);
      tests.push((el) => {
        let index = 1;
        for (let s = el.previousElementSibling; s; s = s.previousElementSibling) {
          if (s.localName === el.localName) index++;
        }
        return index === n;
      });
    } else {
      throw new Error("unsupported selector: " + rest);
    }
    rest = rest.slice(m[0].length);
  }
  return (el) => tests.every((t) => t(el));
}
function parseComplex(text) {
  const steps = [];
  for (const part of splitTop(text.replace(/\s*>\s*/g, " > ").trim(), [" "])) {
    if (part === ">") steps.push(">");
    else if (part) {
      if (steps.length && typeof steps[steps.length - 1] === "function") steps.push(" ");
      steps.push(parseCompound(part));
    }
  }
  return (el) => {
    const go = (node, i) => {
      if (!steps[i](node)) return false;
      if (i === 0) return true;
      const comb = steps[i - 1];
      if (comb === ">") return Boolean(node.parentElement) && go(node.parentElement, i - 2);
      for (let a = node.parentElement; a; a = a.parentElement) if (go(a, i - 2)) return true;
      return false;
    };
    return go(el, steps.length - 1);
  };
}
function matcher(selector) {
  if (config.throwOnQuery) throw new Error("query boom");
  const alternatives = splitTop(selector, [","]).map((p) => parseComplex(p));
  return (el) => alternatives.some((m) => m(el));
}

const all = [];
const byKey = {};
// The element and every element under it, its shadow trees left out.
function descendants(el) {
  const found = [];
  for (const child of el.children) found.push(child, ...descendants(child));
  return found;
}
function build(spec, parent, shadowHost = null) {
  const el = {
    nodeType: 1,
    localName: spec.tag,
    attrs: spec.attrs || {},
    value: spec.value === undefined ? "" : spec.value,
    textContent: spec.text || "",
    parentElement: parent,
    // Set on the top elements of a shadow tree: their parent is the shadow root.
    shadowHost: shadowHost,
    inShadow: Boolean(shadowHost) || Boolean(parent && parent.inShadow),
    hidden: Boolean(spec.hidden) || Boolean(parent && parent.hidden),
    children: [],
    querySelectorAll(selector) {
      const match = matcher(selector);
      return descendants(this).filter((each) => match(each));
    },
    getClientRects() { return this.hidden ? [] : [{}]; },
    get control() {
      if (this.localName !== "label") return undefined;
      const target = this.getAttribute("for");
      if (target !== null) return all.find((each) => each.getAttribute("id") === target) || null;
      return descendants(this).find((each) =>
        ["input", "select", "textarea"].includes(each.localName)) || null;
    },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null;
    },
    get previousElementSibling() {
      if (!this.parentElement) return null;
      const siblings = this.parentElement.children;
      const i = siblings.indexOf(this);
      return i > 0 ? siblings[i - 1] : null;
    },
    closest(selector) {
      const match = matcher(selector);
      for (let node = this; node; node = node.parentElement) if (match(node)) return node;
      return null;
    },
    matches(selector) { return matcher(selector)(this); },
  };
  if (!el.inShadow) all.push(el);
  if (spec.key) byKey[spec.key] = el;
  for (const child of spec.children || []) el.children.push(build(child, el));
  // A shadow tree's elements are reached only through composedPath().
  for (const child of spec.shadow || []) build(child, null, el);
  return el;
}
function composedPath(inner) {
  const path = [];
  for (let node = inner; node; node = node.parentElement || node.shadowHost) path.push(node);
  path.push({ nodeType: 9 });
  return path;
}
const root = build(config.dom, null);
Object.defineProperty(root, "outerHTML", { get: () => "<html></html>" });
globalThis.document = {
  URL: "https://myshop.example/?next=%2F",
  documentElement: root,
  querySelectorAll(selector) {
    if (config.throwOnDocumentQuery) throw new Error("document query boom");
    const match = matcher(selector);
    return all.filter((el) => match(el));
  },
};
if (!config.noCss) globalThis.CSS = { escape: cssEscape };
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
  for (let r = 0; r < (fired.repeat || 1); r++) {
    now += (fired.after || 0) + (fired.wall || 0);
    mono += fired.after || 0;
    const inner = fired.target === undefined ? undefined : byKey[fired.target];
    const event = {
      type: fired.type,
      // A click a page script synthesizes (element.click()) is not trusted.
      isTrusted: !fired.untrusted,
      // At a window listener a click inside a shadow tree is retargeted to its host.
      target: fired.retarget === undefined ? inner : byKey[fired.retarget],
      composedPath() { return inner ? composedPath(inner) : []; },
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
}
// The snapshot listener sends the page on a click while the password holds a
// value; those calls carry no type and are reported apart.
console.log(JSON.stringify({
  threw,
  calls: calls.filter((call) => call.type !== undefined),
  snapshots: calls.filter((call) => call.type === undefined).length,
  events,
}));
"""


def _chain(top: str, divs: int, key: str) -> dict:
    """A button under *divs* divs under a *top* element."""
    node: dict = {"tag": "button", "text": "Go", **({"key": key} if key else {})}
    for _ in range(divs):
        node = {"tag": "div", "children": [node]}
    return {"tag": top, "children": [node]}


def _deep(key: str) -> dict:
    """A button four divs down: the same structure twice is not told apart by a path
    of four levels."""
    node: dict = {"tag": "button", "text": "Open", "key": key}
    for _ in range(4):
        node = {"tag": "div", "children": [node]}
    return node


# A page: a header with a sign-in link (its text inside a span), a dialog with a
# formless login, and two lookalike subtrees.
_PAGE = {
    "tag": "html",
    "children": [
        {
            "tag": "body",
            "children": [
                {
                    "tag": "header",
                    "children": [
                        {"tag": "a", "attrs": {"href": "/"}, "text": "Home", "key": "home"},
                        {
                            "tag": "a",
                            "attrs": {"href": "#", "id": "signin-link"},
                            "key": "signin",
                            "children": [{"tag": "span", "text": "Sign in", "key": "signin-text"}],
                        },
                        {"tag": "a", "attrs": {"href": "#"}, "text": "Help", "key": "help"},
                    ],
                },
                {
                    "tag": "div",
                    "attrs": {"role": "dialog", "class": "modal"},
                    "children": [
                        {
                            "tag": "input",
                            "attrs": {"type": "email", "id": "m-email", "name": "email"},
                            "value": "alice@example.com",
                            "key": "email",
                        },
                        {
                            "tag": "input",
                            "attrs": {"type": "password", "name": "password"},
                            "value": "hunter2hunter2",
                            "key": "password",
                        },
                        {
                            "tag": "label",
                            "attrs": {"id": "1st:remember"},
                            "text": "Remember me",
                            "key": "label",
                        },
                        {"tag": "summary", "text": "More", "key": "summary"},
                        {
                            "tag": "div",
                            "attrs": {"role": "button", "id": "dup"},
                            "text": "Close",
                            "key": "close",
                        },
                        {
                            "tag": "button",
                            "attrs": {"id": "dup", "name": "go", "type": "submit"},
                            "text": "Sign in",
                            "key": "go",
                        },
                        {"tag": "p", "text": "plain text", "key": "plain"},
                        {"tag": "select", "attrs": {"id": "country"}, "key": "select"},
                        {"tag": "textarea", "attrs": {"name": "note"}, "key": "textarea"},
                    ],
                },
                {"tag": "section", "children": [_deep("deep1")]},
                {"tag": "section", "children": [_deep("deep2")]},
                {
                    "tag": "nav",
                    "children": [
                        {"tag": "input", "attrs": {"type": "button"}, "key": "input-button"},
                        {
                            "tag": "div",
                            "attrs": {"class": "opener"},
                            "children": [{"tag": "span", "text": "Log in", "key": "div-opener"}],
                        },
                        {"tag": "label", "attrs": {"for": "m-email"}, "key": "far-label"},
                        {
                            "tag": "label",
                            "key": "wrap-label",
                            "children": [{"tag": "input", "attrs": {"name": "q"}}],
                        },
                        {"tag": "button", "attrs": {"name": 'a"b\\c\nd\re'}, "key": "odd-name"},
                        {"tag": "button", "attrs": {"id": "x" * 129}, "key": "long-id"},
                        {"tag": "button", "attrs": {"id": "y" * 128}, "key": "max-id"},
                        {"tag": "select", "attrs": {"name": "n" * 129}, "key": "long-name"},
                        {"tag": "textarea", "attrs": {"name": "m" * 128}, "key": "max-name"},
                        {
                            "tag": "signin-button",
                            "key": "host",
                            "shadow": [{"tag": "button", "attrs": {"id": "inner"}, "key": "inner"}],
                        },
                    ],
                },
                # Unique at four levels: the twin differs at the fourth element up.
                {"tag": "aside", "children": [_chain("p", 2, "depth-4")]},
                {"tag": "aside", "children": [_chain("span", 2, "")]},
                # Unique only at five levels: the twins differ at the fifth element up.
                _chain("article", 3, "depth-5"),
                _chain("article", 3, ""),
                {
                    "tag": "main",
                    "children": [
                        {
                            "tag": "header",
                            "children": [
                                {"tag": "a", "attrs": {"id": "by-hidden"}, "key": "by-hidden"}
                            ],
                        },
                        {
                            "tag": "div",
                            "hidden": True,
                            "children": [
                                {"tag": "input", "attrs": {"type": "password", "name": "pw2"}}
                            ],
                        },
                    ],
                },
                {
                    "tag": "main",
                    "children": [
                        {
                            "tag": "header",
                            "children": [
                                {"tag": "a", "attrs": {"id": "by-shown"}, "key": "by-shown"}
                            ],
                        },
                        {
                            "tag": "div",
                            "children": [
                                {"tag": "input", "attrs": {"type": "password", "name": "pw3"}}
                            ],
                        },
                    ],
                },
            ],
        }
    ],
}

_UNCHANGED = {"prevented": False, "stopped": False, "escaped": None}


def _run_dom(
    tmp_path: Path,
    fire: list[dict],
    *,
    binding: bool = True,
    no_css: bool = False,
    throw_on_query: bool = False,
    throw_on_document_query: bool = False,
) -> dict:
    harness = tmp_path / "dom_harness.js"
    harness.write_text(_DOM_HARNESS)
    config = {
        "script": LISTENER_SCRIPT,
        "bindingName": BINDING_NAME,
        "binding": binding,
        "fire": fire,
        "dom": _PAGE,
        "noCss": no_css,
        "throwOnQuery": throw_on_query,
        "throwOnDocumentQuery": throw_on_document_query,
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


def _sent(result: dict) -> list[tuple[str, str | None]]:
    return [(call["kind"], call["selector"]) for call in result["calls"]]


class TestTheInteractionConstants:
    def test_the_payload_type_the_rate_and_the_depth_are_fixed(self) -> None:
        assert INTERACTION_TYPE == "interaction"
        assert MAX_INTERACTIONS_PER_SECOND == 20
        assert STRUCTURAL_DEPTH == 4
        assert f'"{INTERACTION_TYPE}"' in LISTENER_SCRIPT


@needs_node
class TestInteractionEvents:
    """The clicks and focus moves the listener sends (#267, part 3): a selector, the
    document URL, and the time, never text or a value."""

    def test_a_click_sends_its_unique_id(self, tmp_path: Path) -> None:
        result = _run_dom(tmp_path, [{"type": "click", "target": "signin"}])
        (call,) = result["calls"]
        assert call == {
            "type": "interaction",
            "kind": "click",
            "selector": "#signin-link",
            "url": "https://myshop.example/?next=%2F",
            "time": 1700000000000,
            "inLogin": False,
        }
        assert result["events"] == [_UNCHANGED]

    def test_a_click_inside_a_link_is_the_link_s(self, tmp_path: Path) -> None:
        result = _run_dom(tmp_path, [{"type": "click", "target": "signin-text"}])
        assert _sent(result) == [("click", "#signin-link")]

    def test_an_id_that_needs_escaping_is_escaped(self, tmp_path: Path) -> None:
        result = _run_dom(tmp_path, [{"type": "click", "target": "label"}])
        assert _sent(result) == [("click", "#\\31 st\\:remember")]

    def test_a_shared_id_falls_to_a_unique_name(self, tmp_path: Path) -> None:
        result = _run_dom(tmp_path, [{"type": "click", "target": "go"}])
        assert _sent(result) == [("click", 'button[name="go"]')]

    def test_no_id_and_no_name_gives_a_short_structural_path(self, tmp_path: Path) -> None:
        result = _run_dom(
            tmp_path, [{"type": "click", "target": "help"}, {"type": "click", "target": "summary"}]
        )
        assert _sent(result) == [
            ("click", "a:nth-of-type(3)"),
            ("click", "summary:nth-of-type(1)"),
        ]

    def test_a_shared_id_with_no_name_falls_to_the_path(self, tmp_path: Path) -> None:
        """The role=button div: its id is shared, and it has no name."""
        result = _run_dom(tmp_path, [{"type": "click", "target": "close"}])
        assert _sent(result) == [
            ("click", "body:nth-of-type(1) > div:nth-of-type(1) > div:nth-of-type(1)")
        ]

    def test_nothing_unique_within_four_levels_sends_no_selector(self, tmp_path: Path) -> None:
        result = _run_dom(tmp_path, [{"type": "click", "target": "deep1"}])
        assert _sent(result) == [("click", None)]

    def test_without_css_escape_an_id_is_not_used(self, tmp_path: Path) -> None:
        result = _run_dom(tmp_path, [{"type": "click", "target": "signin"}], no_css=True)
        assert _sent(result) == [("click", "a:nth-of-type(2)")]

    def test_a_click_on_no_control_is_the_target_s(self, tmp_path: Path) -> None:
        """Review round 1: a click on a span in a div opener is reported as the span,
        so an earlier unrelated click cannot stand in for it."""
        result = _run_dom(
            tmp_path,
            [
                {"type": "click", "target": "div-opener"},
                {"type": "click", "target": "email"},
                {"type": "click"},
            ],
        )
        assert _sent(result) == [
            ("click", "div:nth-of-type(1) > span:nth-of-type(1)"),
            ("click", "#m-email"),
        ]
        assert result["events"] == [_UNCHANGED] * 3

    def test_a_button_input_is_a_control(self, tmp_path: Path) -> None:
        result = _run_dom(tmp_path, [{"type": "click", "target": "input-button"}])
        assert _sent(result) == [("click", "nav:nth-of-type(1) > input:nth-of-type(1)")]

    def test_a_click_a_script_made_is_dropped_and_its_focus_kept(self, tmp_path: Path) -> None:
        result = _run_dom(
            tmp_path,
            [
                {"type": "click", "target": "signin", "untrusted": True},
                {"type": "focusin", "target": "email", "untrusted": True},
            ],
        )
        assert _sent(result) == [("focus", "#m-email")]

    def test_a_click_in_a_shadow_tree_is_the_inner_element_s(self, tmp_path: Path) -> None:
        """The inner button is the clicked control; the document cannot select it, so
        the selector is null (an unresolved opener, never the host)."""
        result = _run_dom(tmp_path, [{"type": "click", "target": "inner", "retarget": "host"}])
        assert _sent(result) == [("click", None)]

    def test_a_click_inside_the_login_is_flagged(self, tmp_path: Path) -> None:
        result = _run_dom(
            tmp_path,
            [
                {"type": "click", "target": "signin"},
                {"type": "click", "target": "go"},
                {"type": "click", "target": "far-label"},
                {"type": "click", "target": "wrap-label"},
                {"type": "click", "target": "input-button"},
                {"type": "click", "target": "by-hidden"},
                {"type": "click", "target": "by-shown"},
            ],
        )
        assert [call["inLogin"] for call in result["calls"]] == [
            False,
            True,
            True,
            True,
            False,
            False,
            True,
        ]

    def test_a_name_with_quotes_backslashes_and_line_breaks_is_escaped(
        self, tmp_path: Path
    ) -> None:
        result = _run_dom(tmp_path, [{"type": "click", "target": "odd-name"}])
        assert _sent(result) == [("click", 'button[name="a\\"b\\\\c\\a d\\d e"]')]

    def test_an_id_or_a_name_over_128_characters_is_not_used(self, tmp_path: Path) -> None:
        result = _run_dom(
            tmp_path,
            [
                {"type": "click", "target": "long-id"},
                {"type": "click", "target": "max-id"},
                {"type": "focusin", "target": "long-name"},
                {"type": "focusin", "target": "max-name"},
            ],
        )
        assert _sent(result) == [
            ("click", "button:nth-of-type(2)"),
            ("click", "#" + "y" * 128),
            ("focus", "nav:nth-of-type(1) > select:nth-of-type(1)"),
            ("focus", 'textarea[name="' + "m" * 128 + '"]'),
        ]

    def test_a_path_of_exactly_four_steps_is_used_and_five_is_not(self, tmp_path: Path) -> None:
        result = _run_dom(
            tmp_path,
            [{"type": "click", "target": "depth-4"}, {"type": "click", "target": "depth-5"}],
        )
        assert _sent(result) == [
            (
                "click",
                "p:nth-of-type(1) > div:nth-of-type(1) > div:nth-of-type(1)"
                " > button:nth-of-type(1)",
            ),
            ("click", None),
        ]

    def test_a_failing_selector_sends_the_event_with_none(self, tmp_path: Path) -> None:
        result = _run_dom(
            tmp_path, [{"type": "click", "target": "signin"}], throw_on_document_query=True
        )
        assert _sent(result) == [("click", None)]
        assert result["events"] == [_UNCHANGED]

    def test_focus_on_a_field_sends_it(self, tmp_path: Path) -> None:
        result = _run_dom(
            tmp_path,
            [
                {"type": "focusin", "target": "email"},
                {"type": "focusin", "target": "password", "after": 300},
                {"type": "focusin", "target": "select", "after": 1},
                {"type": "focusin", "target": "textarea", "after": 1},
                {"type": "focusin", "target": "go", "after": 1},
                {"type": "focusin", "target": "signin", "after": 1},
            ],
        )
        assert _sent(result) == [
            ("focus", "#m-email"),
            ("focus", 'input[name="password"]'),
            ("focus", "#country"),
            ("focus", 'textarea[name="note"]'),
        ]
        assert [call["time"] for call in result["calls"]][:2] == [1700000000000, 1700000000300]

    def test_no_text_and_no_value_is_sent(self, tmp_path: Path) -> None:
        result = _run_dom(
            tmp_path,
            [
                {"type": "click", "target": "signin-text"},
                {"type": "focusin", "target": "email"},
                {"type": "focusin", "target": "password"},
                {"type": "click", "target": "go"},
            ],
        )
        assert len(result["calls"]) == 4
        sent = json.dumps(result["calls"])
        for secret in ("alice@example.com", "hunter2hunter2", "Sign in", "Remember", "href"):
            assert secret not in sent
        assert {frozenset(call) for call in result["calls"]} == {
            frozenset({"type", "kind", "selector", "url", "time"}),
            frozenset({"type", "kind", "selector", "url", "time", "inLogin"}),
        }

    def test_at_most_the_rate_per_second(self, tmp_path: Path) -> None:
        result = _run_dom(
            tmp_path,
            [
                {"type": "click", "target": "signin", "repeat": MAX_INTERACTIONS_PER_SECOND + 5},
                {"type": "focusin", "target": "email", "after": 999},
                {"type": "focusin", "target": "email", "after": 1},
            ],
        )
        assert _sent(result) == [("click", "#signin-link")] * MAX_INTERACTIONS_PER_SECOND + [
            ("focus", "#m-email")
        ]

    def test_forty_attempts_in_two_milliseconds_send_twenty(self, tmp_path: Path) -> None:
        """A sliding window: one event, then a burst straddling the second after it.
        A window reset each second would let 39 of the burst through."""
        result = _run_dom(
            tmp_path,
            [
                {"type": "click", "target": "signin"},
                {"type": "click", "target": "signin", "after": 999},
                {"type": "click", "target": "signin", "after": 0.05, "repeat": 39},
            ],
        )
        assert len(result["calls"]) == MAX_INTERACTIONS_PER_SECOND + 1
        assert len(result["calls"][1:]) == MAX_INTERACTIONS_PER_SECOND

    def test_a_wall_clock_set_back_does_not_stop_the_events(self, tmp_path: Path) -> None:
        result = _run_dom(
            tmp_path,
            [
                {"type": "click", "target": "signin", "repeat": MAX_INTERACTIONS_PER_SECOND},
                {"type": "focusin", "target": "email", "after": 1000, "wall": -100_000},
            ],
        )
        assert _sent(result)[-1] == ("focus", "#m-email")

    def test_without_the_binding_nothing_is_sent_or_thrown(self, tmp_path: Path) -> None:
        result = _run_dom(
            tmp_path,
            [{"type": "click", "target": "signin"}, {"type": "focusin", "target": "email"}],
            binding=False,
        )
        assert result["threw"] is None
        assert result["calls"] == []
        assert result["events"] == [_UNCHANGED] * 2

    def test_a_failure_never_reaches_the_page(self, tmp_path: Path) -> None:
        result = _run_dom(
            tmp_path,
            [
                {"type": "click", "target": "signin"},
                {"type": "focusin", "target": "email"},
                {"type": "focusin"},
            ],
            throw_on_query=True,
        )
        assert result["threw"] is None
        # The click fails finding its control and is dropped; the focus fails only
        # its selector, so it is sent with none (review round 1, M5).
        assert _sent(result) == [("focus", None)]
        assert result["events"] == [_UNCHANGED] * 3
