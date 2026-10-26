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

The script runs in its own world (``WORLD_NAME``), where the binding lives, so
the page's scripts see neither; isolated worlds share the DOM, so its listener
sees the page's events. It never throws into the page, never changes an event,
and does nothing when the binding is missing. ``outerHTML`` holds no typed
value (a value is a property, not markup) unless the page mirrors it into an
attribute; the recorder's sanitizer drops every ``value`` attribute either way.
"""

from __future__ import annotations

__all__ = ["BINDING_NAME", "DEBOUNCE_MS", "LISTENER_SCRIPT", "WORLD_NAME"]

WORLD_NAME = "graftpunk-observe"
BINDING_NAME = "__graftpunkObserveSnapshot"
# One user action sends one snapshot: the same HTML again within this many
# milliseconds (a click on a submit button, then the form's submit) is skipped.
DEBOUNCE_MS = 200

LISTENER_SCRIPT = """(() => {
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
  try {
    for (const type of ["click", "submit", "keydown"]) {
      window.addEventListener(type, listener, { capture: true, passive: true });
    }
  } catch (error) {
    // As above.
  }
})();
""".replace("__BINDING__", BINDING_NAME).replace("__DEBOUNCE__", str(DEBOUNCE_MS))
