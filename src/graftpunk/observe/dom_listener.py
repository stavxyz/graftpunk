"""The listener ``gp observe`` installs in an isolated world while it records (#267).

A login modal's page script can remove the modal as soon as the login answers,
before a DevTools call made on ``Network.requestWillBeSent`` comes back (measured
on 2026-10-24: against a server answering at once, every such snapshot missed
it). So the page is serialized inside the page instead, synchronously, as the
user acts: a capture-phase listener on ``window`` for ``click``, ``submit``, and
an Enter ``keydown`` runs before the page's own handlers for the event, and when
a password input holds a value it sends ``document.documentElement.outerHTML``,
the document URL, and ``Date.now()`` through a binding. The recorder keeps the
last few and gives a credential post the latest one taken before it.

The same script reports the user's clicks and focus moves, so the digest can
find the click that opened a login (part 3 of #267). Every trusted ``click`` is
reported (one a script made with ``element.click()`` is not), as the nearest
``a``, ``button``, ``input`` of type ``button``, ``submit``, ``image``, or
``reset``, ``[role=button]``, or ``summary`` on the event's composed path (so a
click in an open shadow tree is the inner element's, not its host's), else the
innermost element; and a ``focusin`` when its target is an ``input``, ``select``,
or ``textarea``. Each sends ``{"type": "interaction", "kind": "click" | "focus",
"selector", "url", "time"}``, a click also ``inLogin``: a selector computed in the
page or null, the document URL, ``Date.now()``, and whether the click was inside
the login (on a ``label`` whose control is an input, select, or textarea, or on
an element that, or one of whose :data:`LOGIN_CONTAINER_LEVELS` nearest
ancestors short of ``<body>``, holds a shown password input). The selector is
``#id`` (spelled with ``CSS.escape``) when no other element of the document has
that id, else ``tag[name="..."]`` when that picks one element, else the shortest
path of at most :data:`STRUCTURAL_DEPTH` ``tag:nth-of-type(n)`` steps joined by
`` > `` that picks one element, else null; an id or a name over
:data:`MAX_SELECTOR_NAME` characters is not used, and a selector that fails to
compute is null, the event still sent. An element in a shadow tree has no
selector the document can use. No text, no value, and no attribute beyond the id
or name a selector spells is sent; at most :data:`MAX_INTERACTIONS_PER_SECOND`
are sent in any one second, a sliding window on the monotonic
``performance.now()``, so a page that synthesizes events cannot flood the
recorder. A snapshot's payload carries no ``type``.

The script runs in its own world (``WORLD_NAME``), where the binding lives, so
the page's scripts see neither; isolated worlds share the DOM, so its listener
sees the page's events. It never throws into the page, never changes an event,
and does nothing when the binding is missing. ``outerHTML`` holds no typed
value (a value is a property, not markup) unless the page mirrors it into an
attribute; the recorder's sanitizer drops every ``value`` attribute either way.
"""

from __future__ import annotations

__all__ = [
    "BINDING_NAME",
    "DEBOUNCE_MS",
    "INTERACTION_TYPE",
    "LISTENER_SCRIPT",
    "LOGIN_CONTAINER_LEVELS",
    "MAX_INTERACTIONS_PER_SECOND",
    "MAX_SELECTOR_NAME",
    "STRUCTURAL_DEPTH",
    "WORLD_NAME",
]

WORLD_NAME = "graftpunk-observe"
BINDING_NAME = "__graftpunkObserveSnapshot"
# One user action sends one snapshot: the same HTML again within this many
# milliseconds (a click on a submit button, then the form's submit) is skipped.
DEBOUNCE_MS = 200
# The "type" of an interaction payload; a snapshot's payload has none.
INTERACTION_TYPE = "interaction"
# A person clicks and tabs a few times a second; more than this is a script.
MAX_INTERACTIONS_PER_SECOND = 20
# The most elements a structural selector names, the element included.
STRUCTURAL_DEPTH = 4
# The longest id or name a selector spells; a longer one is not used, so a page
# cannot hand the recorder a selector of any size.
MAX_SELECTOR_NAME = 128
# How many ancestors of a clicked element are looked at for a shown password input.
LOGIN_CONTAINER_LEVELS = 6

LISTENER_SCRIPT = (
    """(() => {
  const name = "__BINDING__";
  let lastHtml = null;
  let lastAt = 0;
  const filled = () => {
    for (const input of document.querySelectorAll('input[type="password" i]')) {
      if (input.value) return true;
    }
    return false;
  };
  const listener = (event) => {
    try {
      if (event.type === "keydown" && event.key !== "Enter") return;
      const send = globalThis[name];
      if (typeof send !== "function" || !filled()) return;
      const html = document.documentElement.outerHTML;
      const now = Date.now();
      if (html === lastHtml && now - lastAt < __DEBOUNCE__) return;
      lastHtml = html;
      lastAt = now;
      send(JSON.stringify({ html: html, url: String(document.URL), time: now }));
    } catch (error) {
      // Nothing reaches the page: a snapshot is never worth breaking it.
    }
  };
  const unique = (selector) => document.querySelectorAll(selector).length === 1;
  const quoted = (value) =>
    '"' +
    value
      .replace(/\\\\/g, "\\\\\\\\")
      .replace(/"/g, '\\\\"')
      .replace(/\\n/g, "\\\\a ")
      .replace(/\\r/g, "\\\\d ") +
    '"';
  const selectorOf = (element) => {
    const tag = String(element.localName);
    const id = element.getAttribute("id");
    const escape = globalThis.CSS && globalThis.CSS.escape;
    if (id && id.length <= __MAX_NAME__ && typeof escape === "function") {
      const byId = "#" + escape.call(globalThis.CSS, id);
      if (unique(byId)) return byId;
    }
    const named = element.getAttribute("name");
    if (named && named.length <= __MAX_NAME__) {
      const byName = tag + "[name=" + quoted(named) + "]";
      if (unique(byName)) return byName;
    }
    let path = "";
    let node = element;
    for (let depth = 0; depth < __DEPTH__ && node && node.nodeType === 1; depth++) {
      const nodeTag = String(node.localName);
      let index = 1;
      for (let s = node.previousElementSibling; s; s = s.previousElementSibling) {
        if (s.localName === nodeTag) index++;
      }
      const step = nodeTag + ":nth-of-type(" + index + ")";
      path = path ? step + " > " + path : step;
      if (unique(path)) return path;
      node = node.parentElement;
    }
    return null;
  };
  const controls =
    "a, button, input[type=button i], input[type=submit i], input[type=image i], " +
    "input[type=reset i], [role=button], summary";
  const isElement = (node) => Boolean(node) && node.nodeType === 1;
  // The clicked element: the nearest control on the event's composed path (inside
  // an open shadow tree too, not only its host), else the innermost element.
  const clicked = (event) => {
    const path = typeof event.composedPath === "function" ? event.composedPath() : [];
    for (const node of path) {
      if (isElement(node) && typeof node.matches === "function" && node.matches(controls)) {
        return node;
      }
    }
    const inner = path.find(isElement);
    return inner || (isElement(event.target) ? event.target : null);
  };
  // A shown password input under *node*: a hidden modal's is not the login yet.
  const holdsPassword = (node) => {
    for (const input of node.querySelectorAll('input[type="password" i]')) {
      if (typeof input.getClientRects !== "function" || input.getClientRects().length) {
        return true;
      }
    }
    return false;
  };
  // A click inside the login: on a label for a field, or on an element whose own
  // container (it or an ancestor within __LOGIN_LEVELS__ levels, short of <body>)
  // holds a shown password input as it is clicked.
  const insideLogin = (element) => {
    const label = typeof element.closest === "function" ? element.closest("label") : null;
    const control = label ? label.control : null;
    if (control && /^(input|select|textarea)$/.test(String(control.localName))) return true;
    let node = element;
    for (let level = 0; level <= __LOGIN_LEVELS__ && isElement(node); level++) {
      if (/^(body|html)$/.test(String(node.localName))) break;
      if (typeof node.querySelectorAll === "function" && holdsPassword(node)) return true;
      node = node.parentElement;
    }
    return false;
  };
  const clock = () =>
    globalThis.performance && typeof globalThis.performance.now === "function"
      ? globalThis.performance.now()
      : Date.now();
  // When each of the last few events was sent, by the monotonic clock: at most
  // __RATE__ in any one second.
  const recent = [];
  const interaction = (event) => {
    try {
      const send = globalThis[name];
      if (typeof send !== "function") return;
      let element = null;
      let kind = null;
      if (event.type === "click") {
        // A click a script made (element.click()) is no person's.
        if (!event.isTrusted) return;
        element = clicked(event);
        kind = "click";
      } else if (
        isElement(event.target) &&
        /^(input|select|textarea)$/.test(String(event.target.localName))
      ) {
        element = event.target;
        kind = "focus";
      }
      if (!element) return;
      const at = clock();
      while (recent.length && at - recent[0] >= 1000) recent.shift();
      if (recent.length >= __RATE__) return;
      recent.push(at);
      let selector = null;
      try {
        selector = selectorOf(element);
      } catch (error) {
        selector = null;
      }
      const payload = {
        type: "__TYPE__",
        kind: kind,
        selector: selector,
        url: String(document.URL),
        time: Date.now(),
      };
      if (kind === "click") {
        let inLogin = false;
        try {
          inLogin = insideLogin(element);
        } catch (error) {
          inLogin = false;
        }
        payload.inLogin = inLogin;
      }
      send(JSON.stringify(payload));
    } catch (error) {
      // As above: an interaction is never worth breaking the page.
    }
  };
  try {
    for (const type of ["click", "submit", "keydown"]) {
      window.addEventListener(type, listener, { capture: true, passive: true });
    }
    for (const type of ["click", "focusin"]) {
      window.addEventListener(type, interaction, { capture: true, passive: true });
    }
  } catch (error) {
    // As above.
  }
})();
""".replace("__BINDING__", BINDING_NAME)
    .replace("__DEBOUNCE__", str(DEBOUNCE_MS))
    .replace("__DEPTH__", str(STRUCTURAL_DEPTH))
    .replace("__RATE__", str(MAX_INTERACTIONS_PER_SECOND))
    .replace("__TYPE__", INTERACTION_TYPE)
    .replace("__MAX_NAME__", str(MAX_SELECTOR_NAME))
    .replace("__LOGIN_LEVELS__", str(LOGIN_CONTAINER_LEVELS))
)
