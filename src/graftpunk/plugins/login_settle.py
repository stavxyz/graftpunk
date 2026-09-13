"""The post-submit wait of the declarative login engine.

After the last login step the page settles on its own schedule: a site that
finishes through an identity-provider redirect can take tens of seconds to mint
its cookies. This module owns the wait that follows, for both backends: the poll
for the configured success or failure signal, the rule one tick is decided by,
the document-readiness wait, and the grace window a login with no success signal
spends watching for its failure text.

``login_engine`` drives it through ``wait_for_login_outcome_nodriver`` and
``wait_for_login_outcome_selenium``; everything else here is internal to the wait.
"""

from __future__ import annotations

import asyncio
import fnmatch
import time
from typing import TYPE_CHECKING, Any, NamedTuple

from graftpunk.logging import get_logger

if TYPE_CHECKING:
    from graftpunk.plugins.cli_plugin import LoginConfig

LOG = get_logger(__name__)

_LOGIN_POLL_INTERVAL = 0.5  # seconds between post-submit checks for the login signal
# Seconds a login with no configured success signal watches for its failure signal.
# The engine slept this long before the poll existed, and a site that renders its
# error a second or two after the submit needs the window to say anything at all.
_NO_SIGNAL_GRACE = 3.0
# Page text that identifies a rate-limited response (HTTP 429 body) rather than
# a login outcome. Matched case-insensitively against the post-submit page.
_RATE_LIMIT_MARKERS = ("too many requests",)


def _warn_no_login_validation(site_name: str) -> None:
    """Log a warning when no login validation is configured."""
    LOG.warning(
        "login_no_validation_configured",
        plugin=site_name,
        hint="Consider adding login_failure or login_success to validate login result",
    )


def _warn_rate_limited(site_name: str) -> None:
    """Log a warning when the page came from the site's rate limiter."""
    LOG.warning(
        "login_rate_limited",
        plugin=site_name,
        hint=(
            "The site returned a 'Too Many Requests' page. This is not a "
            "credentials problem; wait before retrying."
        ),
    )


def _warn_failure_text(site_name: str, failure_text: str) -> None:
    """Log a warning when the configured failure text is on the page."""
    LOG.warning(
        "login_failure_text_detected",
        plugin=site_name,
        text=failure_text,
        hint=(
            "The configured failure text is on the page. Sites show it for "
            "wrong credentials, but also for an empty or malformed submission."
        ),
    )


def _check_login_result(*, page_text: str, failure_text: str, site_name: str) -> bool:
    """Rule on the last page a login with no success signal was able to read.

    The two grace-window watchers are the only callers, and a login on that path
    has no success signal by definition, so the page text and the configured
    failure text are everything there is to go on. The element-versus-marker rule
    belongs to ``_login_tick_verdict``, which the signal poll decides its ticks
    with (polish round 1).

    Args:
        page_text: Current page text/source content.
        failure_text: Text to search for indicating failure (empty = skip).
        site_name: Plugin name (for logging).

    Returns:
        True if login appears successful, False if it failed.
    """
    lowered = page_text.lower()

    # A rate-limited response is a page from the site's limiter, not a verdict on
    # the credentials, and nothing on this path speaks for the login, so the
    # marker rules.
    if any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
        _warn_rate_limited(site_name)
        return False

    if failure_text and failure_text.lower() in lowered:
        _warn_failure_text(site_name, failure_text)
        return False

    if not failure_text:
        _warn_no_login_validation(site_name)

    return True


# One poll tick's reading of the page, before the deadline is consulted. A failing
# tick names its reason, so the loop that ends on it can log that reason and no
# failure the wait returns is left unexplained (tidy round, 2026-09-13).
_TICK_PENDING = "pending"
_TICK_SUCCESS = "success"
_TICK_FAILURE_TEXT = "failure_text"
_TICK_RATE_LIMITED = "rate_limited"
_TICK_FAILURES = (_TICK_FAILURE_TEXT, _TICK_RATE_LIMITED)


def _success_signal_configured(login_config: LoginConfig) -> bool:
    """Whether there is a post-submit success signal to wait for at all."""
    return bool(login_config.success or login_config.success_url)


def _url_signal_holds(*, url: str, pre_submit_url: str, success_url: str) -> bool:
    """Whether the URL half of the success signal holds for this reading of the page.

    A URL signal means a navigation happened, so it counts only once the URL differs
    from *pre_submit_url*, the one read just before the last submit: a glob loose
    enough to match the login page itself (``*example.com*``) would otherwise report
    success on the first tick and cache a pre-login session (fix round 1).

    An empty *pre_submit_url* is not a baseline but the absence of one (the read came
    back empty, which nodriver does mid-navigation), and every readable URL differs
    from it, which handed the same loose glob the same false success. With no known
    baseline the URL signal cannot be confirmed at all, so it never holds; the wait
    says so in its debug trail and in the timeout warning (polish round 1).
    """
    return bool(pre_submit_url) and url != pre_submit_url and fnmatch.fnmatchcase(url, success_url)


def _missing_login_signals(
    *,
    url: str,
    pre_submit_url: str,
    success_found: bool | None,
    success_selector: str,
    success_url: str,
) -> list[str]:
    """The configured success signals that do not hold for this reading of the page.

    Empty when every configured signal holds, so ``not _missing_login_signals(...)``
    is the success test and the same list names what never appeared in the timeout
    warning.
    """
    missing: list[str] = []
    if success_selector and success_found is not True:
        missing.append(f"success element '{success_selector}'")
    if success_url and not _url_signal_holds(
        url=url, pre_submit_url=pre_submit_url, success_url=success_url
    ):
        missing.append(f"success URL pattern '{success_url}'")
    return missing


def _login_tick_verdict(
    *,
    page_text: str,
    url: str,
    pre_submit_url: str,
    failure_text: str,
    success_selector: str,
    success_url: str,
    success_found: bool | None,
) -> str:
    """Decide one post-submit poll tick from what the page shows right now.

    Pure, and the single rule both backends poll with: the configured failure text
    ends the wait, a rate-limit page ends it too, every configured success signal
    holding ends it successfully, and anything else is still pending.

    Only a *found success element* vetoes the rate-limit marker: page_text is raw
    HTML, and an inlined i18n bundle or error catalogue on a real post-login page
    can carry the marker text. A URL glob carries no such weight, because a
    limiter answering the post-submit redirect serves its 429 body at the landing
    URL itself; letting the glob veto the marker there captured cookies off the
    limiter's page and cached a dead session (polish round 1, 2026-09-13).

    Returns:
        ``_TICK_SUCCESS``, ``_TICK_PENDING``, or the reason the tick failed:
        ``_TICK_FAILURE_TEXT`` or ``_TICK_RATE_LIMITED``.
    """
    lowered = page_text.lower()
    signal_holds = bool(success_selector or success_url) and not _missing_login_signals(
        url=url,
        pre_submit_url=pre_submit_url,
        success_found=success_found,
        success_selector=success_selector,
        success_url=success_url,
    )
    if failure_text and failure_text.lower() in lowered:
        return _TICK_FAILURE_TEXT
    if success_found is not True and any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
        return _TICK_RATE_LIMITED
    return _TICK_SUCCESS if signal_holds else _TICK_PENDING


def _warn_login_tick_failure(*, verdict: str, site_name: str, failure_text: str) -> None:
    """Log the warning that names why this tick ended the wait.

    Every failure the wait returns carries one, so a login reported as failed is
    never left unexplained (tidy round, 2026-09-13).
    """
    if verdict == _TICK_RATE_LIMITED:
        _warn_rate_limited(site_name)
    elif verdict == _TICK_FAILURE_TEXT:
        _warn_failure_text(site_name, failure_text)


def _warn_login_signal_timeout(
    *,
    login_config: LoginConfig,
    site_name: str,
    url: str,
    pre_submit_url: str,
    success_found: bool | None,
) -> None:
    """Warn that the configured success signal never appeared before the deadline.

    The one place both backends report a timeout from, so the message cannot drift
    between them.
    """
    missing = _missing_login_signals(
        url=url,
        pre_submit_url=pre_submit_url,
        success_found=success_found,
        success_selector=login_config.success,
        success_url=login_config.success_url,
    )
    hint = (
        "The page never showed the configured login success signal. Raise "
        "LoginConfig.timeout when the site redirects slowly, or correct the "
        "signal to match the page the login actually lands on."
    )
    if login_config.success_url and not pre_submit_url:
        hint += (
            " The URL before submit could not be read, so a URL signal cannot "
            "be confirmed; use a `success` selector for this site."
        )
    LOG.warning(
        "login_signal_timeout",
        plugin=site_name,
        missing=" and ".join(missing),
        url=url,
        timeout=f"{login_config.timeout:g}s",
        hint=hint,
    )


def _debug_unknown_baseline(
    *, login_config: LoginConfig, site_name: str, pre_submit_url: str, backend: str
) -> None:
    """Note in the debug trail that a configured ``success_url`` cannot be confirmed.

    The baseline is the URL read just before the submit, and nodriver leaves a
    tab's URL unreadable mid-navigation. Without it the URL signal never holds, so
    a login configured only that way waits out its whole timeout; an author reading
    the trail should find the reason at the start of the wait, not infer it.
    """
    if not login_config.success_url or pre_submit_url:
        return
    LOG.debug(
        "login_pre_submit_url_unknown",
        plugin=site_name,
        backend=backend,
        success_url=login_config.success_url,
        hint=(
            "The URL before submit could not be read, so a URL signal cannot be "
            "confirmed; use a `success` selector for this site."
        ),
    )


def _poll_sleep_seconds(remaining: float) -> float:
    """One poll interval, or what is left of the budget when that is less.

    Sleeping a whole interval past the deadline would make a ``timeout`` smaller
    than the interval cost the interval instead (fix round 1).
    """
    return min(_LOGIN_POLL_INTERVAL, max(0.0, remaining))


def _tab_url(tab: Any) -> str:
    """The nodriver tab's current URL, or an empty string when it cannot be read."""
    try:
        url = tab.url
    except Exception as exc:  # noqa: BLE001 (the URL is one input to the poll, not the login)
        LOG.debug("login_url_read_failed", error=str(exc), backend="nodriver")
        return ""
    return url if isinstance(url, str) else ""


def _driver_url(driver: Any) -> str:
    """The selenium driver's current URL, or an empty string when it cannot be read."""
    try:
        url = driver.current_url
    except Exception as exc:  # noqa: BLE001 (the URL is one input to the poll, not the login)
        LOG.debug("login_url_read_failed", error=str(exc), backend="selenium")
        return ""
    return url if isinstance(url, str) else ""


class _TickReading(NamedTuple):
    """One poll tick: what the page said, where it was, what was found.

    ``error`` is empty on a tick that was read. A tick that could not be read
    carries the error text instead, with no page text, so a caller can both keep
    waiting and say afterwards why it never saw a page.
    """

    page_text: str
    url: str
    success_found: bool | None
    error: str = ""


class _ReadWindow:
    """What a wait has read so far: the last tick it managed to read, or why it read none.

    Both waits rule on the last reading they got, so a tick that fails after a good
    one must not replace that reading with nothing, and a window of nothing but
    failures has to be told apart from a window whose pages were all clean: the
    first has confirmed nothing about the login, and the empty page text it leaves
    behind would read as "the failure text is not on the page" (tidy round,
    2026-09-13; extended to the signal poll in polish round 1).
    """

    def __init__(self) -> None:
        self.page_read = False
        self.page_text = ""
        self.error = "the page was never read"
        self.url = ""
        self.success_found: bool | None = None

    def record(self, reading: _TickReading) -> None:
        """Fold one tick's reading into the window."""
        self.url = reading.url
        if reading.error:
            self.error = reading.error
            return
        self.page_read = True
        self.page_text = reading.page_text
        self.success_found = reading.success_found


async def _read_login_tick_nodriver(
    tab: Any,  # nodriver.Tab
    success_selector: str,
    site_name: str,
) -> _TickReading:
    """One tick's reading of the tab, or a reading carrying the error instead.

    A ProtocolException means the document node went invalid mid-navigation, which
    is the window this poll exists for (see ``_select_with_retry``): the tick is
    unreadable, not failed, so the caller treats it as pending and keeps waiting.
    """
    from nodriver.core.connection import ProtocolException

    try:
        page_text = await tab.get_content()
        url = _tab_url(tab)
        success_found: bool | None = None
        if success_selector:
            # query_selector returns immediately, found or not: the poll is the
            # retry, and a probe that slept inside nodriver would stretch the cadence.
            element = await tab.query_selector(success_selector)
            success_found = element is not None
    except ProtocolException as exc:
        LOG.debug(
            "login_tick_read_failed",
            plugin=site_name,
            error=str(exc),
            exc_type=type(exc).__name__,
            backend="nodriver",
        )
        return _TickReading("", _tab_url(tab), None, error=str(exc))
    return _TickReading(page_text, url, success_found)


def _read_login_tick_selenium(
    driver: Any,  # selenium WebDriver
    success_selector: str,
    site_name: str,
) -> _TickReading:
    """The selenium twin of :func:`_read_login_tick_nodriver`.

    A missing element is NoSuchElementException and reads as "not yet"; any other
    WebDriverException is a read taken while the browser was navigating, so the
    tick is unreadable rather than failed.
    """
    from selenium.common.exceptions import NoSuchElementException, WebDriverException

    try:
        page_text = driver.page_source
        url = _driver_url(driver)
        success_found: bool | None = None
        if success_selector:
            try:
                driver.find_element("css selector", success_selector)
                success_found = True
            except NoSuchElementException:
                success_found = False
    except WebDriverException as exc:
        LOG.debug(
            "login_tick_read_failed",
            plugin=site_name,
            error=str(exc),
            exc_type=type(exc).__name__,
            backend="selenium",
        )
        return _TickReading("", _driver_url(driver), None, error=str(exc))
    return _TickReading(page_text, url, success_found)


def _warn_login_page_unreadable(*, site_name: str, error: str, url: str) -> None:
    """Warn that no tick of the wait ever read the page.

    A login whose page could not be read even once has not been confirmed by
    anything, and the empty page text it leaves behind reads as "no failure text
    on the page", which is not a success (tidy round, 2026-09-13). Both the signal
    poll and the grace window end this way, so the reason is the same one either
    of them reports.
    """
    LOG.warning(
        "login_page_unreadable",
        plugin=site_name,
        error=error,
        url=url,
        hint=(
            "The page could not be read at any point after the submit, so the "
            "login cannot be confirmed. The browser may have closed or navigated "
            "away; run with --observe=full to capture what happened."
        ),
    )


async def _wait_for_login_signal_nodriver(
    *,
    tab: Any,  # nodriver.Tab
    login_config: LoginConfig,
    failure_text: str,
    site_name: str,
    deadline: float,
    pre_submit_url: str,
) -> bool:
    """Poll the tab until the login signal, the failure signal, or *deadline*.

    Sites that finish their login through an identity-provider redirect mint their
    cookies tens of seconds after the submit, so the engine waits for the signal it
    is configured to look for rather than for a fixed delay.

    The first tick comes one interval in, not immediately: a success element that
    was already on the login page would otherwise be read on a page the submit has
    not yet replaced. *pre_submit_url* is the URL read just before the last submit,
    and a URL signal counts only once the page has moved off it.

    A poll whose every tick failed to read is reported as an unreadable page rather
    than as a missing signal: the signal was never looked at, so naming it would
    send an author after the wrong thing (polish round 1).

    Returns:
        True when every configured success signal holds, False on a failure signal,
        on a window that read nothing, or when the deadline passes (all are logged).
    """
    loop = asyncio.get_running_loop()
    success_selector = login_config.success
    success_url = login_config.success_url
    last = _ReadWindow()
    while True:
        await asyncio.sleep(_poll_sleep_seconds(deadline - loop.time()))
        reading = await _read_login_tick_nodriver(tab, success_selector, site_name)
        last.record(reading)
        if reading.error:
            verdict = _TICK_PENDING
        else:
            verdict = _login_tick_verdict(
                page_text=reading.page_text,
                url=reading.url,
                pre_submit_url=pre_submit_url,
                failure_text=failure_text,
                success_selector=success_selector,
                success_url=success_url,
                success_found=reading.success_found,
            )
        if verdict == _TICK_SUCCESS:
            return True
        if verdict in _TICK_FAILURES:
            _warn_login_tick_failure(
                verdict=verdict, site_name=site_name, failure_text=failure_text
            )
            return False
        if loop.time() >= deadline:
            break

    if not last.page_read:
        _warn_login_page_unreadable(site_name=site_name, error=last.error, url=last.url)
        return False
    _warn_login_signal_timeout(
        login_config=login_config,
        site_name=site_name,
        url=last.url,
        pre_submit_url=pre_submit_url,
        success_found=last.success_found,
    )
    return False


def _wait_for_login_signal_selenium(
    *,
    driver: Any,  # selenium WebDriver
    login_config: LoginConfig,
    failure_text: str,
    site_name: str,
    deadline: float,
    pre_submit_url: str,
) -> bool:
    """The selenium twin of :func:`_wait_for_login_signal_nodriver`."""
    success_selector = login_config.success
    success_url = login_config.success_url
    last = _ReadWindow()
    while True:
        time.sleep(_poll_sleep_seconds(deadline - time.monotonic()))
        reading = _read_login_tick_selenium(driver, success_selector, site_name)
        last.record(reading)
        if reading.error:
            verdict = _TICK_PENDING
        else:
            verdict = _login_tick_verdict(
                page_text=reading.page_text,
                url=reading.url,
                pre_submit_url=pre_submit_url,
                failure_text=failure_text,
                success_selector=success_selector,
                success_url=success_url,
                success_found=reading.success_found,
            )
        if verdict == _TICK_SUCCESS:
            return True
        if verdict in _TICK_FAILURES:
            _warn_login_tick_failure(
                verdict=verdict, site_name=site_name, failure_text=failure_text
            )
            return False
        if time.monotonic() >= deadline:
            break

    if not last.page_read:
        _warn_login_page_unreadable(site_name=site_name, error=last.error, url=last.url)
        return False
    _warn_login_signal_timeout(
        login_config=login_config,
        site_name=site_name,
        url=last.url,
        pre_submit_url=pre_submit_url,
        success_found=last.success_found,
    )
    return False


async def _watch_for_failure_nodriver(
    *,
    tab: Any,  # nodriver.Tab
    failure_text: str,
    site_name: str,
    deadline: float,
) -> bool:
    """Watch a login with no success signal for its failure signal, then rule on it.

    There is nothing to wait *for* here, so the engine spends the window it always
    spent (the retired fixed post-submit delay) watching for the one signal such a
    plugin does have: the ``failure`` text, and the rate-limit page. It ends early
    the moment either shows, and the verdict and its warnings are the single check
    this path has always taken.

    A window in which no tick could be read at all ends in failure: the page said
    nothing, and an empty page text passed to the verdict would read as "the
    failure text is not on the page", which is not a confirmation of anything.

    Returns:
        True when the login stands, False when the page said otherwise or was
        never readable.
    """
    loop = asyncio.get_running_loop()
    last = _ReadWindow()
    while True:
        await asyncio.sleep(_poll_sleep_seconds(deadline - loop.time()))
        reading = await _read_login_tick_nodriver(tab, "", site_name)
        last.record(reading)
        if not reading.error and _failure_showing(
            page_text=reading.page_text, failure_text=failure_text
        ):
            break
        if loop.time() >= deadline:
            break
    if not last.page_read:
        _warn_login_page_unreadable(site_name=site_name, error=last.error, url=last.url)
        return False
    return _check_login_result(
        page_text=last.page_text, failure_text=failure_text, site_name=site_name
    )


def _watch_for_failure_selenium(
    *,
    driver: Any,  # selenium WebDriver
    failure_text: str,
    site_name: str,
    deadline: float,
) -> bool:
    """The selenium twin of :func:`_watch_for_failure_nodriver`."""
    last = _ReadWindow()
    while True:
        time.sleep(_poll_sleep_seconds(deadline - time.monotonic()))
        reading = _read_login_tick_selenium(driver, "", site_name)
        last.record(reading)
        if not reading.error and _failure_showing(
            page_text=reading.page_text, failure_text=failure_text
        ):
            break
        if time.monotonic() >= deadline:
            break
    if not last.page_read:
        _warn_login_page_unreadable(site_name=site_name, error=last.error, url=last.url)
        return False
    return _check_login_result(
        page_text=last.page_text, failure_text=failure_text, site_name=site_name
    )


def _failure_showing(*, page_text: str, failure_text: str) -> bool:
    """Whether this reading of the page already says the login failed.

    The same rule ``_login_tick_verdict`` applies, asked of a page with no success
    signal to weigh it against.
    """
    return (
        _login_tick_verdict(
            page_text=page_text,
            url="",
            pre_submit_url="",
            failure_text=failure_text,
            success_selector="",
            success_url="",
            success_found=None,
        )
        in _TICK_FAILURES
    )


async def _await_document_ready_nodriver(tab: Any, *, deadline: float, site_name: str) -> None:
    """Wait until the tab reports ``document.readyState == "complete"``, or *deadline*.

    Best-effort: the success signal can appear on a document still fetching the
    response that carries the session cookie, and cookies captured a moment later
    are the point of the wait. A readyState that cannot be read (a protocol error
    mid-redirect, a backend that does not evaluate) is not a login failure, so it
    ends the wait rather than the login.
    """
    loop = asyncio.get_running_loop()
    while True:
        try:
            state = await tab.evaluate("document.readyState")
        except Exception as exc:  # noqa: BLE001 (readiness is best-effort, see docstring)
            LOG.debug(
                "login_document_ready_unavailable",
                plugin=site_name,
                error=str(exc),
                exc_type=type(exc).__name__,
                backend="nodriver",
            )
            return
        if not isinstance(state, str):
            LOG.debug(
                "login_document_ready_unreadable",
                plugin=site_name,
                state_type=type(state).__name__,
                backend="nodriver",
            )
            return
        if state == "complete":
            return
        if loop.time() >= deadline:
            LOG.debug(
                "login_document_ready_timeout",
                plugin=site_name,
                state=state,
                backend="nodriver",
            )
            return
        await asyncio.sleep(_poll_sleep_seconds(deadline - loop.time()))


def _await_document_ready_selenium(driver: Any, *, deadline: float, site_name: str) -> None:
    """The selenium twin of :func:`_await_document_ready_nodriver`."""
    while True:
        try:
            state = driver.execute_script("return document.readyState")
        except Exception as exc:  # noqa: BLE001 (readiness is best-effort, see the nodriver twin)
            LOG.debug(
                "login_document_ready_unavailable",
                plugin=site_name,
                error=str(exc),
                exc_type=type(exc).__name__,
                backend="selenium",
            )
            return
        if not isinstance(state, str):
            LOG.debug(
                "login_document_ready_unreadable",
                plugin=site_name,
                state_type=type(state).__name__,
                backend="selenium",
            )
            return
        if state == "complete":
            return
        if time.monotonic() >= deadline:
            LOG.debug(
                "login_document_ready_timeout",
                plugin=site_name,
                state=state,
                backend="selenium",
            )
            return
        time.sleep(_poll_sleep_seconds(deadline - time.monotonic()))


async def wait_for_login_outcome_nodriver(
    *,
    tab: Any,  # nodriver.Tab
    login_config: LoginConfig,
    failure_text: str,
    site_name: str,
    pre_submit_url: str,
) -> bool:
    """The whole post-submit wait for the nodriver backend.

    With a success signal configured: poll for it until ``login_config.timeout``,
    then wait for the document to finish loading and pause for
    ``login_config.settle`` before the caller captures cookies. With none: watch
    for the failure signal across the grace window and rule on what the page says.

    Returns:
        True when the login stands, False when it does not (the reason is logged).
    """
    if not _success_signal_configured(login_config):
        return await _watch_for_failure_nodriver(
            tab=tab,
            failure_text=failure_text,
            site_name=site_name,
            deadline=asyncio.get_running_loop().time() + _NO_SIGNAL_GRACE,
        )
    _debug_unknown_baseline(
        login_config=login_config,
        site_name=site_name,
        pre_submit_url=pre_submit_url,
        backend="nodriver",
    )
    deadline = asyncio.get_running_loop().time() + login_config.timeout
    if not await _wait_for_login_signal_nodriver(
        tab=tab,
        login_config=login_config,
        failure_text=failure_text,
        site_name=site_name,
        deadline=deadline,
        pre_submit_url=pre_submit_url,
    ):
        return False
    await _await_document_ready_nodriver(tab, deadline=deadline, site_name=site_name)
    await asyncio.sleep(login_config.settle)
    return True


def wait_for_login_outcome_selenium(
    *,
    driver: Any,  # selenium WebDriver
    login_config: LoginConfig,
    failure_text: str,
    site_name: str,
    pre_submit_url: str,
) -> bool:
    """The selenium twin of :func:`wait_for_login_outcome_nodriver`."""
    if not _success_signal_configured(login_config):
        return _watch_for_failure_selenium(
            driver=driver,
            failure_text=failure_text,
            site_name=site_name,
            deadline=time.monotonic() + _NO_SIGNAL_GRACE,
        )
    _debug_unknown_baseline(
        login_config=login_config,
        site_name=site_name,
        pre_submit_url=pre_submit_url,
        backend="selenium",
    )
    deadline = time.monotonic() + login_config.timeout
    if not _wait_for_login_signal_selenium(
        driver=driver,
        login_config=login_config,
        failure_text=failure_text,
        site_name=site_name,
        deadline=deadline,
        pre_submit_url=pre_submit_url,
    ):
        return False
    _await_document_ready_selenium(driver, deadline=deadline, site_name=site_name)
    time.sleep(login_config.settle)
    return True
