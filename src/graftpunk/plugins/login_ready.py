"""When a login step may act, and which element it acts on (#275).

A client-rendered page puts its login link in the server HTML long before the
script that handles it arrives: the engine found the link, clicked it, and the
click went nowhere. This module owns the two checks the engine now makes before
it fills or clicks:

- **Readiness.** Before a step that has no ``wait_for`` of its own, wait until
  the document has loaded and the page has gone quiet: no DOM change, no script
  or data request starting or finishing, and none in flight, for
  ``_READY_QUIET_WINDOW`` seconds. The wait never runs longer than
  ``_READY_MAX_WAIT`` or the login's own ``timeout``, whichever is smaller,
  and a page that never goes quiet is acted on at that point, as before.
  A plugin that names a hydration marker with ``wait_for`` has already said when
  the page is ready, so the default stands aside for that step.
- **The target.** A selector that matches more than one element acts on the
  first visible match, and says so; a selector whose matches are all hidden, or
  whose chosen match is disabled, fails with the count rather than clicking
  something no user could.

The page-side probes return JSON strings, so nodriver's ``return_by_value``
quirk (a falsy value comes back as the RemoteObject itself) never applies.
Everything here is best-effort in the same way as the field read-back: a probe
that cannot be read leaves the engine doing what it did before this module.
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
_READY_MAX_WAIT = 5.0
# Seconds between readiness reads.
_READY_POLL_INTERVAL = 0.1
# The most matches the target probe describes one by one; the count is exact.
_TARGET_PROBE_LIMIT = 50

# Request types whose completion can change what a click does: the page's own
# scripts, frames, and the data they fetch. Images, fonts, styles, beacons and
# event streams are left out, so an analytics ping or a long-lived stream does
# not hold every login to the cap.
_ACTIVE_REQUEST_TYPES = frozenset({"Document", "Script", "XHR", "Fetch"})

# Installs one MutationObserver per document (the first read starts its quiet
# clock), and reads the newest resource timing entry, which covers requests that
# finished before the observer existed. A <script src> with no timing entry yet
# is still downloading, which is the hydration chunk this wait is for; the check
# is skipped once the timing buffer is full (250 entries by default), since a
# script past that point never gets an entry. Style attribute changes are left
# out: an animation writes them every frame and would keep the page busy forever.
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
  const entries = performance.getEntriesByType("resource");
  let lastResource = 0;
  for (const entry of entries) {
    if (entry.responseEnd > lastResource) lastResource = entry.responseEnd;
  }
  let pending = 0;
  if (entries.length < 250) {
    const fetched = new Set(entries.map((entry) => entry.name));
    for (const script of document.querySelectorAll("script[src]")) {
      if (!fetched.has(script.src)) pending += 1;
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
    behind ``return``."""
    return (
        "((selector, limit) => {"
        " let nodes;"
        " try { nodes = document.querySelectorAll(selector); }"
        " catch (e) { return JSON.stringify({error: String(e)}); }"
        " const shown = (el) => {"
        "  if (!el.isConnected || el.getClientRects().length === 0) return false;"
        "  if (typeof el.checkVisibility === 'function')"
        "   return el.checkVisibility({visibilityProperty: true, checkVisibilityCSS: true});"
        "  const style = getComputedStyle(el);"
        "  return style.visibility !== 'hidden' && style.display !== 'none';"
        " };"
        " const listed = Array.from(nodes).slice(0, limit);"
        " return JSON.stringify({count: nodes.length,"
        "  visible: listed.map(shown), disabled: listed.map((el) => el.matches(':disabled'))});"
        f"}})({json.dumps(selector)}, {_TARGET_PROBE_LIMIT})"
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
        self._in_flight: set[str] = set()
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
        self._in_flight.add(str(event.request_id))
        self._last_change = time.monotonic()

    def _on_done(self, event: Any) -> None:
        request_id = str(getattr(event, "request_id", ""))
        if request_id in self._in_flight:
            self._in_flight.discard(request_id)
            self._last_change = time.monotonic()

    def quiet_for(self) -> float:
        """Seconds since the last tracked request started or ended; 0 while any is in flight."""
        if self._in_flight:
            return 0.0
        return time.monotonic() - self._last_change


def _ready(reading: PageReadiness, network: NetworkActivity | None) -> bool:
    quiet = reading.quiet_for
    if network is not None:
        quiet = min(quiet, network.quiet_for())
    return reading.state == "complete" and quiet >= _READY_QUIET_WINDOW


def _log_not_quiet(
    *, site_name: str, step: int, reading: PageReadiness, waited: float, backend: str
) -> None:
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
            "element only the page's script renders."
        ),
    )


async def await_page_ready_nodriver(
    tab: Any,
    *,
    budget: float,
    step: int,
    site_name: str,
    network: NetworkActivity | None = None,
) -> None:
    """Wait until the page is loaded and quiet, for at most ``min(_READY_MAX_WAIT, budget)``."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + min(_READY_MAX_WAIT, budget)
    while True:
        try:
            raw = await tab.evaluate(_READY_JS, return_by_value=True)
        except Exception as exc:  # broad by design: readiness is best-effort
            LOG.debug("login_page_ready_unavailable", plugin=site_name, step=step, error=str(exc))
            return
        reading = parse_page_readiness(raw)
        if reading is None:
            LOG.debug("login_page_ready_unreadable", plugin=site_name, step=step)
            return
        now = loop.time()
        if _ready(reading, network):
            LOG.debug("login_page_ready", plugin=site_name, step=step, waited=now - started)
            return
        if now >= deadline:
            _log_not_quiet(
                site_name=site_name,
                step=step,
                reading=reading,
                waited=now - started,
                backend="nodriver",
            )
            return
        await asyncio.sleep(min(_READY_POLL_INTERVAL, deadline - now))


def await_page_ready_selenium(driver: Any, *, budget: float, step: int, site_name: str) -> None:
    """The selenium twin of :func:`await_page_ready_nodriver`, with the DOM probe alone."""
    started = time.monotonic()
    deadline = started + min(_READY_MAX_WAIT, budget)
    while True:
        try:
            raw = driver.execute_script("return " + _READY_JS)
        except Exception as exc:  # broad by design: readiness is best-effort
            LOG.debug("login_page_ready_unavailable", plugin=site_name, step=step, error=str(exc))
            return
        reading = parse_page_readiness(raw)
        if reading is None:
            LOG.debug("login_page_ready_unreadable", plugin=site_name, step=step)
            return
        now = time.monotonic()
        if _ready(reading, None):
            LOG.debug("login_page_ready", plugin=site_name, step=step, waited=now - started)
            return
        if now >= deadline:
            _log_not_quiet(
                site_name=site_name,
                step=step,
                reading=reading,
                waited=now - started,
                backend="selenium",
            )
            return
        time.sleep(min(_READY_POLL_INTERVAL, deadline - now))
