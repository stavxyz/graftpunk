"""When a login step may act, and which element it acts on (#275).

A client-rendered page puts its login link in the server HTML long before the
script that handles it arrives: the engine found the link, clicked it, and the
click went nowhere. This module owns the two checks the engine now makes before
it fills or clicks:

- **Readiness.** Before a step that has no ``wait_for`` of its own, wait until
  the document has loaded and the page has gone quiet: no DOM change, no script
  or data request starting or finishing, and none in flight, for
  ``_READY_QUIET_WINDOW`` seconds, counted from when this wait started. The wait
  never runs longer than ``_READY_MAX_WAIT`` or the login's own ``timeout``,
  whichever is smaller, and a page that never goes quiet is acted on at that
  point, as before. On nodriver, a plugin that names a hydration marker with
  ``wait_for`` has already said when the page is ready, so the default stands
  aside for that step. Selenium rejects ``wait_for``, so it always waits.
- **The target.** A selector that matches more than one element acts on the
  first visible match, and says so; a selector whose matches are all hidden, or
  whose chosen match is disabled, fails with the count rather than clicking
  something no user could.

The page-side probes return JSON strings, so nodriver's ``return_by_value``
quirk (a falsy value comes back as the RemoteObject itself) never applies. A
probe that cannot be read is retried until the cap and then leaves the engine
doing what it did before this module.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, NamedTuple

from graftpunk.logging import get_logger

LOG = get_logger(__name__)

# Seconds the page must stay quiet before a step acts on it.
_READY_QUIET_WINDOW = 0.5
# The most one readiness wait spends, before LoginConfig.timeout caps it further.
# A request in flight for longer than this no longer counts as in flight.
_READY_MAX_WAIT = 5.0
# Seconds between readiness reads.
_READY_POLL_INTERVAL = 0.1
# The most matches the target probe describes one by one; the count is exact.
_TARGET_PROBE_LIMIT = 50

# Request types whose completion can change what a click does: the page's own
# scripts, frames, and the data they fetch. Images, fonts, styles, beacons and
# event streams are left out, so an analytics ping does not hold every login to
# the cap. A fetch or XHR that stays open (a long poll, a streamed response)
# would, so one older than _READY_MAX_WAIT no longer counts as in flight.
_ACTIVE_REQUEST_TYPES = frozenset({"Document", "Script", "XHR", "Fetch"})

# The readiness probe. It installs one MutationObserver per document; the first
# read starts that document's quiet clock. The wait in Python never counts quiet
# time from before it started, so a page that went quiet during an earlier step
# cannot satisfy a later one. Two more things keep the page busy:
# - a resource of the kinds the CDP tracker counts finishing (resource timing
#   initiatorType script, fetch, xmlhttprequest, iframe, frame or navigation);
#   images, styles, fonts and beacons are left out;
# - a <script src> the browser fetches that has no timing entry yet, which is
#   the hydration chunk this wait exists for. A script the browser never fetches
#   would count as pending forever, so these are left out: a classic script with
#   nomodule (on a browser that knows nomodule), a type that is neither empty, a
#   JavaScript MIME type nor "module" (text/plain, application/json,
#   text/template and the like), and a src that is not http or https (data:,
#   blob:). The check is skipped once the timing buffer is full (250 entries by
#   default), since a script past that point never gets an entry.
# Style attribute changes are left out of the observer: an animation writes them
# every frame and would keep the page busy forever.
_READY_JS = """(() => {
  const now = performance.now();
  let quiet = window.__graftpunkQuiet;
  if (!quiet) {
    quiet = window.__graftpunkQuiet = {last: now};
    try {
      new MutationObserver(() => { quiet.last = performance.now(); }).observe(document, {
        subtree: true, childList: true, characterData: true,
        attributes: true, attributeFilter: ["class", "disabled", "hidden", "href"],
      });
    } catch (e) {}
  }
  const active = new Set(["script", "fetch", "xmlhttprequest", "iframe", "frame", "navigation"]);
  const entries = performance.getEntriesByType("resource");
  let lastResource = 0;
  for (const entry of entries) {
    if (active.has(entry.initiatorType) && entry.responseEnd > lastResource) {
      lastResource = entry.responseEnd;
    }
  }
  const jsTypes = new Set([
    "application/ecmascript", "application/javascript", "application/x-ecmascript",
    "application/x-javascript", "text/ecmascript", "text/javascript", "text/javascript1.0",
    "text/javascript1.1", "text/javascript1.2", "text/javascript1.3", "text/javascript1.4",
    "text/javascript1.5", "text/jscript", "text/livescript", "text/x-ecmascript",
    "text/x-javascript",
  ]);
  const knowsNoModule = typeof HTMLScriptElement !== "undefined"
    && "noModule" in HTMLScriptElement.prototype;
  const fetches = (script) => {
    const type = (script.getAttribute("type") || "").trim().toLowerCase();
    const classic = type === "" || jsTypes.has(type);
    if (!classic && type !== "module") return false;
    if (classic && knowsNoModule && script.hasAttribute("nomodule")) return false;
    return /^https?:/i.test(script.src);
  };
  let pending = 0;
  if (entries.length < 250) {
    const fetched = new Set(entries.map((entry) => entry.name));
    for (const script of document.querySelectorAll("script[src]")) {
      if (fetches(script) && !fetched.has(script.src)) pending += 1;
    }
  }
  return JSON.stringify({
    state: document.readyState,
    quiet_ms: pending ? 0 : now - Math.max(quiet.last, lastResource),
    pending_scripts: pending,
  });
})()"""


def target_js(selector: str) -> str:
    """The page-side probe for *selector*: the match count, and per match whether
    it is visible and whether it is disabled. An expression, so selenium runs it
    behind ``return``.

    Visible means laid out (it has client rects), not hidden by ``visibility``,
    ``display`` or ``opacity: 0``, and not pushed off the page's left or top edge
    or past the right edge of the viewport, which is where an off-canvas drawer
    parks its copy of a link. An element below the fold counts as visible.
    """
    return (
        "((selector, limit) => {"
        " let nodes;"
        " try { nodes = document.querySelectorAll(selector); }"
        " catch (e) { return JSON.stringify({error: String(e)}); }"
        " const shown = (el) => {"
        "  if (!el.isConnected || el.getClientRects().length === 0) return false;"
        "  if (typeof el.checkVisibility === 'function') {"
        "   if (!el.checkVisibility({visibilityProperty: true, checkVisibilityCSS: true,"
        "     opacityProperty: true, checkOpacity: true})) return false;"
        "  } else {"
        "   const style = getComputedStyle(el);"
        "   if (style.visibility === 'hidden' || style.display === 'none'"
        "     || style.opacity === '0') return false;"
        "  }"
        "  const box = el.getBoundingClientRect();"
        "  if (box.right + window.scrollX <= 0 || box.bottom + window.scrollY <= 0) return false;"
        "  return box.left < window.innerWidth;"
        " };"
        " const listed = Array.from(nodes).slice(0, limit);"
        " return JSON.stringify({count: nodes.length,"
        "  visible: listed.map(shown), disabled: listed.map((el) => el.matches(':disabled'))});"
        f"}})({json.dumps(selector)}, {_TARGET_PROBE_LIMIT})"
    )


def field_value_js(selector: str, index: int) -> str:
    """Read the value of match *index* of *selector* from the live document.

    By index, so the read-back and the clear check look at the element the step
    typed into, which is not the first match when a hidden copy comes first.
    """
    return (
        "((selector, index) => {"
        " const el = document.querySelectorAll(selector)[index];"
        " return JSON.stringify({found: !!el, value: el ? String(el.value) : null});"
        f"}})({json.dumps(selector)}, {int(index)})"
    )


@dataclass(frozen=True)
class PageReadiness:
    """One read of the page: its ``document.readyState`` and how long it has been quiet."""

    state: str
    quiet_for: float  # seconds


def parse_page_readiness(raw: object) -> PageReadiness | None:
    """The readiness probe's answer, or None when it is not one."""
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    state = data.get("state")
    quiet_ms = data.get("quiet_ms")
    if not isinstance(state, str) or isinstance(quiet_ms, bool):
        return None
    if not isinstance(quiet_ms, (int, float)):
        return None
    return PageReadiness(state=state, quiet_for=max(float(quiet_ms), 0.0) / 1000.0)


@dataclass(frozen=True)
class TargetProbe:
    """What the page says about one selector's matches, in document order."""

    count: int
    visible: tuple[bool, ...]
    disabled: tuple[bool, ...]


def parse_target_probe(raw: object) -> TargetProbe | None:
    """The target probe's answer, or None when it is not one (or the selector threw)."""
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    count = data.get("count")
    visible = data.get("visible")
    disabled = data.get("disabled")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    if not isinstance(visible, list) or not isinstance(disabled, list):
        return None
    if len(visible) != len(disabled) or len(visible) != min(count, _TARGET_PROBE_LIMIT):
        return None
    return TargetProbe(
        count=count,
        visible=tuple(v is True for v in visible),
        disabled=tuple(d is True for d in disabled),
    )


class TargetChoice(NamedTuple):
    """The match to act on (0-based), or None and the reason none can be acted on yet."""

    index: int | None
    problem: str = ""


def choose_target(probe: TargetProbe) -> TargetChoice:
    """Pick the match a step acts on.

    One match is acted on whether or not it is visible, as it always was: hiding
    a single field behind a styled label is common, and nodriver's click and
    typing reach it. Several matches are where a hidden copy (a mobile menu, a
    template) can stand ahead of the element a user sees, so the first visible
    one is chosen. Either way a disabled choice is not acted on, because a click
    on it does nothing and the login would fail later and further from the cause.
    """
    if probe.count == 0:
        return TargetChoice(None, "matches no element")
    if probe.count == 1:
        if probe.disabled[0]:
            return TargetChoice(None, "matches one element, and it is disabled")
        return TargetChoice(0)
    shown = [i for i, visible in enumerate(probe.visible) if visible]
    if not shown:
        return TargetChoice(None, f"matches {probe.count} elements and none of them is visible")
    index = shown[0]
    if probe.disabled[index]:
        return TargetChoice(
            None,
            f"matches {probe.count} elements, and the first visible one "
            f"(match {index + 1}) is disabled",
        )
    return TargetChoice(index)


def warn_ambiguous_selector(
    *, selector: str, probe: TargetProbe, chosen: int, context: str, site_name: str
) -> None:
    """Say that a selector matched several elements, and which one the step used."""
    LOG.warning(
        "login_selector_ambiguous",
        plugin=site_name,
        step=context,
        selector=selector,
        matches=probe.count,
        visible=sum(probe.visible),
        chosen=chosen + 1,
        hint=(
            "The selector matches more than one element, so the engine acted on the "
            "first visible one. Narrow the selector until it matches one element."
        ),
    )


class NetworkActivity:
    """The page's script and data requests, as the browser reports them over CDP.

    The DOM probe cannot see a request in flight, and a hydration chunk still
    downloading is exactly the gap between "the link is on the page" and "the
    link works". Fed by the tab's Network events once :meth:`attach` has run.
    """

    def __init__(self) -> None:
        self._in_flight: dict[str, float] = {}  # request id -> when it started
        self._last_change = time.monotonic()

    def attach(self, tab: Any) -> bool:
        """Register on *tab*'s Network events; False when that cannot be done."""
        try:
            import nodriver.cdp.network as network

            tab.add_handler(network.RequestWillBeSent, self._on_request)
            tab.add_handler(network.LoadingFinished, self._on_done)
            tab.add_handler(network.LoadingFailed, self._on_done)
        except Exception as exc:  # broad by design: readiness falls back to the DOM probe
            LOG.debug("login_network_activity_unavailable", error=str(exc))
            return False
        return True

    def _on_request(self, event: Any) -> None:
        kind = getattr(getattr(event, "type_", None), "value", None)
        if kind not in _ACTIVE_REQUEST_TYPES:
            return
        now = time.monotonic()
        self._in_flight[str(event.request_id)] = now
        self._last_change = now

    def _on_done(self, event: Any) -> None:
        request_id = str(getattr(event, "request_id", ""))
        if self._in_flight.pop(request_id, None) is not None:
            self._last_change = time.monotonic()

    def quiet_for(self) -> float:
        """Seconds since the last tracked request started or ended; 0 while any is in flight.

        A request in flight for longer than ``_READY_MAX_WAIT`` is not counted: a
        long poll, or a finish event that never arrived, would otherwise hold every
        later step of the login to the cap.
        """
        now = time.monotonic()
        if any(now - started < _READY_MAX_WAIT for started in self._in_flight.values()):
            return 0.0
        return now - self._last_change


def _quiet_since(
    reading: PageReadiness, network: NetworkActivity | None, *, waited: float
) -> float:
    """How long the page has been quiet, counting no time from before this wait began.

    The page keeps its quiet clock across steps, so without the floor a step that
    follows a navigating click could read the old document, quiet since the last
    step, and act on the new one before it has loaded.
    """
    quiet = min(reading.quiet_for, waited)
    if network is not None:
        quiet = min(quiet, network.quiet_for())
    return quiet


def _ready(reading: PageReadiness, network: NetworkActivity | None, *, waited: float) -> bool:
    return (
        reading.state == "complete"
        and _quiet_since(reading, network, waited=waited) >= _READY_QUIET_WINDOW
    )


def _log_not_quiet(
    *,
    site_name: str,
    step: int,
    reading: PageReadiness | None,
    waited: float,
    backend: str,
    error: str,
) -> None:
    if reading is None:
        LOG.info(
            "login_page_ready_unreadable",
            plugin=site_name,
            step=step,
            waited=f"{waited:.1f}s",
            backend=backend,
            error=error,
            hint=(
                "The page's readiness could not be read before the wait ran out, so "
                "the step went ahead."
            ),
        )
        return
    LOG.info(
        "login_page_not_quiet",
        plugin=site_name,
        step=step,
        state=reading.state,
        waited=f"{waited:.1f}s",
        backend=backend,
        hint=(
            "The page was still changing when the readiness wait ran out, so the step "
            "went ahead. If a click does nothing, give the step a wait_for naming an "
            "element only the page's script renders (nodriver)."
        ),
    )


class _ReadOutcome(NamedTuple):
    reading: PageReadiness | None
    error: str


def _read_outcome(raw: object) -> _ReadOutcome:
    reading = parse_page_readiness(raw)
    return _ReadOutcome(reading, "" if reading is not None else "unparseable readiness read")


async def await_page_ready_nodriver(
    tab: Any,
    *,
    budget: float,
    step: int,
    site_name: str,
    network: NetworkActivity | None = None,
) -> None:
    """Wait until the page is loaded and quiet, for at most ``min(_READY_MAX_WAIT, budget)``.

    A read that fails (the execution context is torn down mid-navigation) or
    returns something unexpected counts as not ready, and the wait reads again.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + min(_READY_MAX_WAIT, budget)
    last = _ReadOutcome(None, "")
    while True:
        try:
            outcome = _read_outcome(await tab.evaluate(_READY_JS, return_by_value=True))
        except Exception as exc:  # broad by design: an unreadable read is a not-ready read
            outcome = _ReadOutcome(None, f"{type(exc).__name__}: {exc}")
        if outcome.reading is not None or last.reading is None:
            last = outcome
        now = loop.time()
        if outcome.reading is not None and _ready(outcome.reading, network, waited=now - started):
            LOG.debug("login_page_ready", plugin=site_name, step=step, waited=now - started)
            return
        if now >= deadline:
            _log_not_quiet(
                site_name=site_name,
                step=step,
                reading=last.reading,
                waited=now - started,
                backend="nodriver",
                error=last.error,
            )
            return
        await asyncio.sleep(min(_READY_POLL_INTERVAL, deadline - now))


def await_page_ready_selenium(driver: Any, *, budget: float, step: int, site_name: str) -> None:
    """The selenium twin of :func:`await_page_ready_nodriver`, with the DOM probe alone."""
    started = time.monotonic()
    deadline = started + min(_READY_MAX_WAIT, budget)
    last = _ReadOutcome(None, "")
    while True:
        try:
            outcome = _read_outcome(driver.execute_script("return " + _READY_JS))
        except Exception as exc:  # broad by design: an unreadable read is a not-ready read
            outcome = _ReadOutcome(None, f"{type(exc).__name__}: {exc}")
        if outcome.reading is not None or last.reading is None:
            last = outcome
        now = time.monotonic()
        if outcome.reading is not None and _ready(outcome.reading, None, waited=now - started):
            LOG.debug("login_page_ready", plugin=site_name, step=step, waited=now - started)
            return
        if now >= deadline:
            _log_not_quiet(
                site_name=site_name,
                step=step,
                reading=last.reading,
                waited=now - started,
                backend="selenium",
                error=last.error,
            )
            return
        time.sleep(min(_READY_POLL_INTERVAL, deadline - now))
