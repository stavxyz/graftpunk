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


def _check_login_result(
    *,
    page_text: str,
    failure_text: str,
    success_found: bool | None,
    success_selector: str,
    site_name: str,
) -> bool:
    """Check login result using failure text and success selector.

    Args:
        page_text: Current page text/source content.
        failure_text: Text to search for indicating failure (empty = skip).
        success_found: True if success element was found, False if not,
            None if no selector configured.
        success_selector: The CSS selector used (for logging).
        site_name: Plugin name (for logging).

    Returns:
        True if login appears successful, False if it failed.
    """
    lowered = page_text.lower()

    # A rate-limited response is a page from the site's limiter, not a verdict
    # on the credentials. It never contains the success element, so a found
    # success element always wins: page_text is raw HTML, and an inlined i18n
    # bundle or error catalogue on a real post-login page can contain the
    # marker text. Only refine a failure, never veto a success.
    if success_found is not True and any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
        LOG.warning(
            "login_rate_limited",
            plugin=site_name,
            hint=(
                "The site returned a 'Too Many Requests' page. This is not a "
                "credentials problem; wait before retrying."
            ),
        )
        return False

    if failure_text and failure_text.lower() in lowered:
        LOG.warning(
            "login_failure_text_detected",
            plugin=site_name,
            text=failure_text,
            hint=(
                "The configured failure text is on the page. Sites show it for "
                "wrong credentials, but also for an empty or malformed submission."
            ),
        )
        return False

    if success_found is False:
        LOG.warning(
            "login_success_element_not_found",
            plugin=site_name,
            selector=success_selector,
            hint=(
                "The page never showed the configured success element. The login "
                "may still be on the form, or the site may have redirected elsewhere."
            ),
        )
        return False

    if not failure_text and success_found is None:
        _warn_no_login_validation(site_name)

    return True


# One poll tick's reading of the page, before the deadline is consulted.
_TICK_PENDING = "pending"
_TICK_SUCCESS = "success"
_TICK_FAILURE = "failure"


def _success_signal_configured(login_config: LoginConfig) -> bool:
    """Whether there is a post-submit success signal to wait for at all."""
    return bool(login_config.success or login_config.success_url)


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

    A URL signal means a navigation happened, so it counts only once the URL differs
    from *pre_submit_url*, the one read just before the last submit: a glob loose
    enough to match the login page itself (``*example.com*``) would otherwise report
    success on the first tick and cache a pre-login session (fix round 1).
    """
    missing: list[str] = []
    if success_selector and success_found is not True:
        missing.append(f"success element '{success_selector}'")
    if success_url and not (url != pre_submit_url and fnmatch.fnmatchcase(url, success_url)):
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

    A found success signal keeps its veto over the rate-limit marker, as in
    ``_check_login_result``: page_text is raw HTML, and an inlined i18n bundle or
    error catalogue on a real post-login page can carry the marker text.

    Returns:
        One of ``_TICK_SUCCESS``, ``_TICK_FAILURE``, ``_TICK_PENDING``.
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
        return _TICK_FAILURE
    if not signal_holds and any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
        return _TICK_FAILURE
    return _TICK_SUCCESS if signal_holds else _TICK_PENDING


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
    LOG.warning(
        "login_signal_timeout",
        plugin=site_name,
        missing=" and ".join(missing),
        url=url,
        timeout=f"{login_config.timeout:g}s",
        hint=(
            "The page never showed the configured login success signal. Raise "
            "LoginConfig.timeout when the site redirects slowly, or correct the "
            "signal to match the page the login actually lands on."
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
    """One readable poll tick: what the page said, where it was, what was found."""

    page_text: str
    url: str
    success_found: bool | None


async def _read_login_tick_nodriver(
    tab: Any,  # nodriver.Tab
    success_selector: str,
    site_name: str,
) -> _TickReading | None:
    """One tick's reading of the tab, or None when the page could not be read.

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
        return None
    return _TickReading(page_text, url, success_found)


def _read_login_tick_selenium(
    driver: Any,  # selenium WebDriver
    success_selector: str,
    site_name: str,
) -> _TickReading | None:
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
        return None
    return _TickReading(page_text, url, success_found)


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

    Returns:
        True when every configured success signal holds, False on a failure signal
        or when the deadline passes (both are logged).
    """
    loop = asyncio.get_running_loop()
    success_selector = login_config.success
    success_url = login_config.success_url
    url = ""
    page_text = ""
    success_found: bool | None = None
    while True:
        await asyncio.sleep(_poll_sleep_seconds(deadline - loop.time()))
        reading = await _read_login_tick_nodriver(tab, success_selector, site_name)
        if reading is None:
            verdict = _TICK_PENDING
        else:
            page_text, url, success_found = reading
            verdict = _login_tick_verdict(
                page_text=page_text,
                url=url,
                pre_submit_url=pre_submit_url,
                failure_text=failure_text,
                success_selector=success_selector,
                success_url=success_url,
                success_found=success_found,
            )
        if verdict == _TICK_SUCCESS:
            return True
        if verdict == _TICK_FAILURE:
            # Called for the warning that names which signal decided it; the
            # verdict above is what returns.
            _check_login_result(
                page_text=page_text,
                failure_text=failure_text,
                success_found=success_found,
                success_selector=success_selector,
                site_name=site_name,
            )
            return False
        if loop.time() >= deadline:
            break

    _warn_login_signal_timeout(
        login_config=login_config,
        site_name=site_name,
        url=url,
        pre_submit_url=pre_submit_url,
        success_found=success_found,
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
    url = ""
    page_text = ""
    success_found: bool | None = None
    while True:
        time.sleep(_poll_sleep_seconds(deadline - time.monotonic()))
        reading = _read_login_tick_selenium(driver, success_selector, site_name)
        if reading is None:
            verdict = _TICK_PENDING
        else:
            page_text, url, success_found = reading
            verdict = _login_tick_verdict(
                page_text=page_text,
                url=url,
                pre_submit_url=pre_submit_url,
                failure_text=failure_text,
                success_selector=success_selector,
                success_url=success_url,
                success_found=success_found,
            )
        if verdict == _TICK_SUCCESS:
            return True
        if verdict == _TICK_FAILURE:
            # Called for the warning that names which signal decided it; the
            # verdict above is what returns.
            _check_login_result(
                page_text=page_text,
                failure_text=failure_text,
                success_found=success_found,
                success_selector=success_selector,
                site_name=site_name,
            )
            return False
        if time.monotonic() >= deadline:
            break

    _warn_login_signal_timeout(
        login_config=login_config,
        site_name=site_name,
        url=url,
        pre_submit_url=pre_submit_url,
        success_found=success_found,
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

    Returns:
        True when the login stands, False when the page said otherwise.
    """
    loop = asyncio.get_running_loop()
    page_text = ""
    while True:
        await asyncio.sleep(_poll_sleep_seconds(deadline - loop.time()))
        reading = await _read_login_tick_nodriver(tab, "", site_name)
        if reading is not None:
            page_text = reading.page_text
            if _failure_showing(page_text=page_text, failure_text=failure_text):
                break
        if loop.time() >= deadline:
            break
    return _check_login_result(
        page_text=page_text,
        failure_text=failure_text,
        success_found=None,
        success_selector="",
        site_name=site_name,
    )


def _watch_for_failure_selenium(
    *,
    driver: Any,  # selenium WebDriver
    failure_text: str,
    site_name: str,
    deadline: float,
) -> bool:
    """The selenium twin of :func:`_watch_for_failure_nodriver`."""
    page_text = ""
    while True:
        time.sleep(_poll_sleep_seconds(deadline - time.monotonic()))
        reading = _read_login_tick_selenium(driver, "", site_name)
        if reading is not None:
            page_text = reading.page_text
            if _failure_showing(page_text=page_text, failure_text=failure_text):
                break
        if time.monotonic() >= deadline:
            break
    return _check_login_result(
        page_text=page_text,
        failure_text=failure_text,
        success_found=None,
        success_selector="",
        site_name=site_name,
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
        == _TICK_FAILURE
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
        await asyncio.sleep(_LOGIN_POLL_INTERVAL)


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
        time.sleep(_LOGIN_POLL_INTERVAL)


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
