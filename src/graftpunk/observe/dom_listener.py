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

The same script reports the user's clicks and focus moves, so the digest can find
the click that opened a login (part 3 of #267), and with each click what it
revealed. Every trusted ``click`` is reported (one a script made with
``element.click()`` is not), and a ``focusin`` when its target is an ``input``,
``select``, or ``textarea``. Each sends ``{"type": "interaction", "kind": "click"
| "focus", "selector", "url", "time"}``: a selector computed in the page or null,
the document URL, and ``Date.now()``. A click also sends ``control``, whether a
control is on the event's composed path (the nearest ``a``, ``button``, ``input``
of type ``button``, ``submit``, ``image``, or ``reset``, ``[role=button]``, or
``summary``, inside an open shadow tree too, whose selector is the click's; a
click on anything else has a null selector); ``inLogin``, whether it was inside
the login (on a ``label`` whose control is a field a person types into: a
``select``, a ``textarea``, or an ``input`` of type ``text``, ``email``, ``tel``,
``url``, ``search``, ``password``, or ``number``, or of no type; or on an element
that, or one of whose :data:`LOGIN_CONTAINER_LEVELS` nearest ancestors short of
``<body>``, holds a shown password input); ``inDialog``, whether an element on the
composed path is a ``<dialog>``, has a ``role`` whose first token is ``dialog`` or
``alertdialog``, or has ``aria-modal="true"``; ``revealed``, whether the clicked
control was not drawn at the previous trusted click on this page (the first click
of a page is not); and ``shownFields``, the selectors of up to
:data:`MAX_SHOWN_FIELDS` fields shown on the page (text-like and password inputs,
inputs marked ``current-password``, selects, and textareas). Drawn is
``checkVisibility`` with its opacity and visibility checks when the browser has
it, else a layout box, a computed ``visibility`` that is not ``hidden``, and no
element from it up with opacity 0, and not under ``aria-hidden="true"`` or
``inert``; a field is shown when it is drawn and its box has an area inside the
viewport, so a field moved off screen is not, while a control scrolled out of view
still counts as drawn. The selector is ``#id`` (spelled with ``CSS.escape``) when
no other element of the document has that id, else ``tag[name="..."]`` when that
picks one element (every control character in the name as its hex escape, a NUL
included), else the shortest path of at most :data:`STRUCTURAL_DEPTH`
``tag:nth-of-type(n)`` steps joined by `` > `` that picks one element, else null;
an id or a name over :data:`MAX_SELECTOR_NAME` characters is not used, so such an
element falls through to the next rule (a name, else the structural path, which
spells only tag names and positions), and a selector that fails to compute is
null, the event still sent. An element in a shadow tree has no selector the
document can use. No text, no value, and no attribute beyond the id or name a
selector spells is sent; at most :data:`MAX_INTERACTIONS_PER_SECOND` are sent in
any one second, a sliding window on the monotonic ``performance.now()``, so a page
that synthesizes events cannot flood the recorder, and the page's controls are
listed only on those clicks. A snapshot's payload carries no ``type``.

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
    "MAX_SHOWN_FIELDS",
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
# The most shown fields a click lists: a login shows two or three.
MAX_SHOWN_FIELDS = 16

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
      // Every control character as its hex escape: a newline is "\\a ", a NUL "\\0 ".
      .replace(/[\\x00-\\x1f\\x7f]/g, (c) => "\\\\" + c.charCodeAt(0).toString(16) + " ") +
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
  const pathOf = (event) =>
    typeof event.composedPath === "function" ? event.composedPath() : [event.target];
  // The nearest control on the event's composed path (inside an open shadow tree
  // too, not only its host), or null.
  const controlOf = (path) => {
    for (const node of path) {
      if (isElement(node) && typeof node.matches === "function" && node.matches(controls)) {
        return node;
      }
    }
    return null;
  };
  // Whether an element on the path is a dialog: a <dialog>, a role whose first
  // token is dialog or alertdialog, or aria-modal="true".
  const inDialogOf = (path) =>
    path.some((node) => {
      if (!isElement(node) || typeof node.getAttribute !== "function") return false;
      if (String(node.localName) === "dialog") return true;
      const role = String(node.getAttribute("role") || "").trim().toLowerCase().split(/\\s+/)[0];
      if (role === "dialog" || role === "alertdialog") return true;
      return String(node.getAttribute("aria-modal") || "").trim().toLowerCase() === "true";
    });
  // Whether the page draws *el* for a person: checkVisibility with opacity and
  // visibility when the browser has it, else a layout box, visibility not hidden,
  // and no element from it up with opacity 0; and not under aria-hidden="true" or
  // inert.
  const drawn = (el) => {
    if (typeof el.closest === "function" && el.closest('[aria-hidden="true" i], [inert]')) {
      return false;
    }
    if (typeof el.checkVisibility === "function") {
      return el.checkVisibility({ opacityProperty: true, visibilityProperty: true });
    }
    if (typeof el.getClientRects === "function" && !el.getClientRects().length) return false;
    if (typeof globalThis.getComputedStyle !== "function") return true;
    if (globalThis.getComputedStyle(el).visibility === "hidden") return false;
    for (let node = el; isElement(node); node = node.parentElement) {
      if (parseFloat(globalThis.getComputedStyle(node).opacity) === 0) return false;
    }
    return true;
  };
  // Drawn, with a box of some area inside the viewport: a field moved off screen (a
  // honeypot, a closed drawer) is not shown.
  const shown = (el) => {
    if (!drawn(el)) return false;
    if (typeof el.getBoundingClientRect !== "function") return true;
    const box = el.getBoundingClientRect();
    const width = globalThis.innerWidth;
    const height = globalThis.innerHeight;
    return (
      box.width > 0 &&
      box.height > 0 &&
      box.right > 0 &&
      box.bottom > 0 &&
      (typeof width !== "number" || box.left < width) &&
      (typeof height !== "number" || box.top < height)
    );
  };
  // The password inputs under *root*: type password, or marked current-password.
  const passwords = (root) => {
    const found = Array.from(root.querySelectorAll('input[type="password" i]'));
    for (const input of root.querySelectorAll("input[autocomplete]")) {
      const tokens = String(input.getAttribute("autocomplete")).toLowerCase().split(/\\s+/);
      if (tokens.includes("current-password") && !found.includes(input)) found.push(input);
    }
    return found;
  };
  const holdsPassword = (root) => passwords(root).some(shown);
  // A field a person fills in: a select, a textarea, a text-like input (below), or
  // an input marked current-password.
  const fieldLike = (el) => {
    if (textLike(el)) return true;
    const tokens = String(el.getAttribute("autocomplete") || "").toLowerCase().split(/\\s+/);
    return String(el.localName) === "input" && tokens.includes("current-password");
  };
  // The selectors of the first __MAX_FIELDS__ fields shown on the page.
  const shownFields = () => {
    const found = [];
    for (const el of document.querySelectorAll("input, select, textarea")) {
      if (found.length >= __MAX_FIELDS__) break;
      if (!fieldLike(el) || !shown(el)) continue;
      let selector = null;
      try {
        selector = selectorOf(el);
      } catch (error) {
        selector = null;
      }
      if (selector) found.push(selector);
    }
    return found;
  };
  // The controls drawn at the last trusted click on this page (scrolling does not
  // hide one, so the viewport does not count here), or null before the first.
  let drawnControls = null;
  const revealedAndRemember = (control) => {
    const revealed = Boolean(control) && drawnControls !== null && !drawnControls.has(control);
    drawnControls = new WeakSet(Array.from(document.querySelectorAll(controls)).filter(drawn));
    return revealed;
  };
  // A field a person types into; a checkbox, a radio, or a button is not one.
  const textLike = (control) => {
    const tag = String(control.localName);
    if (tag === "select" || tag === "textarea") return true;
    if (tag !== "input") return false;
    const type = String(control.getAttribute("type") || "").toLowerCase();
    return /^(|text|email|tel|url|search|password|number)$/.test(type);
  };
  // A click inside the login: on a label for a field a person types into, or on an
  // element whose own container (it or an ancestor within __LOGIN_LEVELS__ levels,
  // short of <body>) holds a shown password input as it is clicked.
  const insideLogin = (element) => {
    const label = typeof element.closest === "function" ? element.closest("label") : null;
    const control = label ? label.control : null;
    if (control && textLike(control)) return true;
    let node = element;
    for (let level = 0; level <= __LOGIN_LEVELS__ && isElement(node); level++) {
      if (/^(body|html)$/.test(String(node.localName))) break;
      if (typeof node.querySelectorAll === "function" && holdsPassword(node)) return true;
      node = node.parentElement;
    }
    return false;
  };
  // Each part of a click's state is computed on its own: one that fails is false.
  const attempt = (compute) => {
    try {
      return compute() === true;
    } catch (error) {
      return false;
    }
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
      let path = null;
      let control = null;
      let kind = null;
      if (event.type === "click") {
        // A click a script made (element.click()) is no person's.
        if (!event.isTrusted) return;
        path = pathOf(event);
        control = controlOf(path);
        // The element clicked: the control, else the innermost element (for the
        // flags only: a click on no control has no selector).
        element = control || path.find(isElement) || null;
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
      const target = kind === "click" ? control : element;
      let selector = null;
      try {
        selector = target ? selectorOf(target) : null;
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
        payload.control = Boolean(control);
        payload.inLogin = attempt(() => insideLogin(element));
        payload.inDialog = attempt(() => inDialogOf(path));
        payload.revealed = attempt(() => revealedAndRemember(control));
        let fields = [];
        try {
          fields = shownFields();
        } catch (error) {
          fields = [];
        }
        payload.shownFields = fields;
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
    .replace("__MAX_FIELDS__", str(MAX_SHOWN_FIELDS))
)
