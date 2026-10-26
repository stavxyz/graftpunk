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
find the click that opened a login (part 3 of #267). A ``click`` is reported as
the nearest ``a``, ``button``, ``[role=button]``, ``summary``, or ``label`` at or
above its target (a click on anything else is not reported), and a ``focusin``
when its target is an ``input``, ``select``, or ``textarea``. Each sends
``{"type": "interaction", "kind": "click" | "focus", "selector", "url", "time"}``:
a selector computed in the page or null, the document URL, and ``Date.now()``.
The selector is ``#id`` (spelled with ``CSS.escape``) when no other element of
the document has that id, else ``tag[name="..."]`` when that picks one element,
else the shortest path of at most :data:`STRUCTURAL_DEPTH` ``tag:nth-of-type(n)``
steps joined by `` > `` that picks one element, else null. No text, no value,
and no attribute beyond the id or name a selector spells is sent; at most
:data:`MAX_INTERACTIONS_PER_SECOND` are sent in any one second, so a page that
synthesizes clicks cannot flood the recorder. A snapshot's payload carries no
``type``.

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
    "MAX_INTERACTIONS_PER_SECOND",
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
    if (id && typeof escape === "function") {
      const byId = "#" + escape.call(globalThis.CSS, id);
      if (unique(byId)) return byId;
    }
    const named = element.getAttribute("name");
    if (named) {
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
  let windowStart = 0;
  let sentInWindow = 0;
  const interaction = (event) => {
    try {
      const send = globalThis[name];
      if (typeof send !== "function") return;
      const target = event.target;
      if (!target || typeof target.closest !== "function") return;
      let element = null;
      let kind = null;
      if (event.type === "click") {
        element = target.closest('a, button, [role=button], summary, label');
        kind = "click";
      } else if (/^(input|select|textarea)$/.test(String(target.localName))) {
        element = target;
        kind = "focus";
      }
      if (!element) return;
      const now = Date.now();
      if (now - windowStart >= 1000) {
        windowStart = now;
        sentInWindow = 0;
      }
      if (sentInWindow >= __RATE__) return;
      sentInWindow++;
      const selector = selectorOf(element);
      send(
        JSON.stringify({
          type: "__TYPE__",
          kind: kind,
          selector: selector,
          url: String(document.URL),
          time: now,
        })
      );
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
)
