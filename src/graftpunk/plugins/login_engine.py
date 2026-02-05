"""Declarative login engine for plugins.

Generates login() methods from declarative configuration (CSS selectors,
success/failure indicators). Handles browser lifecycle, cookie transfer,
and session caching automatically.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any

from graftpunk import BrowserSession, cache_session
from graftpunk.exceptions import PluginError
from graftpunk.logging import get_logger

if TYPE_CHECKING:
    from graftpunk.plugins.cli_plugin import SitePlugin

LOG = get_logger(__name__)

_POST_SUBMIT_DELAY = 3  # seconds to wait after form submission for page to settle
_ELEMENT_WAIT_TIMEOUT = 30  # seconds to wait for element during page transitions
_ELEMENT_RETRY_INTERVAL = 1.0  # seconds between retry attempts


async def _select_with_retry(
    tab: Any,  # nodriver.Tab — can't import due to upstream SyntaxError in CDP codegen
    selector: str,
    *,
    timeout: float = _ELEMENT_WAIT_TIMEOUT,
    interval: float = _ELEMENT_RETRY_INTERVAL,
) -> Any:  # nodriver.Element | None
    """Wait for a CSS selector, retrying through page transitions.

    nodriver's tab.select() handles the case where an element doesn't exist
    yet (returns None, retries internally). But during cross-origin redirects
    or page transitions, the document node itself becomes invalid, causing a
    ProtocolException that bypasses select()'s retry loop.

    This wrapper catches ProtocolException and retries the entire select()
    call, giving the browser time to complete redirects and render the form.

    Args:
        tab: nodriver tab instance.
        selector: CSS selector string.
        timeout: Total seconds to wait before giving up (must be positive).
        interval: Seconds between retry attempts (must be positive).

    Returns:
        The matched element, or None if not found within timeout.

    Raises:
        ValueError: If timeout or interval are not positive.
        ProtocolException: If timeout expires and last failure was a protocol error.
    """
    if timeout <= 0:
        raise ValueError(f"timeout must be positive, got {timeout}")
    if interval <= 0:
        raise ValueError(f"interval must be positive, got {interval}")
    from nodriver.core.connection import ProtocolException

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_exc: ProtocolException | None = None

    while loop.time() < deadline:
        remaining = deadline - loop.time()
        try:
            # Cap each attempt at 5s so a single tab.select() call doesn't
            # consume the full remaining budget inside nodriver's own retry loop.
            per_attempt = min(5.0, remaining)
            element = await tab.select(selector, timeout=per_attempt)
            if element is not None:
                if last_exc is not None:
                    LOG.info(
                        "login_element_retry_recovered",
                        selector=selector,
                    )
                return element
            LOG.debug(
                "login_element_select_returned_none",
                selector=selector,
                remaining=f"{remaining:.1f}s",
            )
        except ProtocolException as exc:
            if last_exc is None:
                LOG.info(
                    "login_element_retry_started",
                    selector=selector,
                    timeout=f"{timeout:.1f}s",
                    hint="Page may be redirecting; retrying element selection",
                )
            last_exc = exc
            LOG.debug(
                "login_element_retry",
                selector=selector,
                error=str(exc),
                remaining=f"{remaining:.1f}s",
            )
        await asyncio.sleep(interval)

    if last_exc is not None:
        raise last_exc
    LOG.debug("login_element_not_found", selector=selector, timeout=f"{timeout:.1f}s")
    return None


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
    if failure_text and failure_text.lower() in page_text.lower():
        LOG.warning("login_failure_text_detected", plugin=site_name, text=failure_text)
        return False

    if success_found is False:
        LOG.warning(
            "login_success_element_not_found",
            plugin=site_name,
            selector=success_selector,
        )
        return False

    if not failure_text and success_found is None:
        _warn_no_login_validation(site_name)

    return True


def _build_token_cache(
    token_config: Any,
    token_results: dict[str, str],
) -> dict[str, Any]:
    """Build CachedToken dict from extracted token values.

    Args:
        token_config: Token extraction configuration with .tokens list.
        token_results: Mapping of token names to extracted values.

    Returns:
        Dict mapping token names to CachedToken instances.
    """
    from graftpunk.tokens import CachedToken

    tcache: dict[str, CachedToken] = {}
    for t in token_config.tokens:
        if t.name in token_results:
            tcache[t.name] = CachedToken(
                name=t.name,
                value=token_results[t.name],
                extracted_at=time.time(),
                ttl=t.cache_duration,
            )
    return tcache


async def _extract_and_cache_tokens_nodriver(
    plugin: SitePlugin,
    session: Any,
    tab: Any,
    base_url: str,
) -> None:
    """Extract and cache tokens for nodriver backend during login.

    Args:
        plugin: Plugin instance with optional token_config.
        session: Browser session to cache tokens on.
        tab: Active nodriver tab to extract from.
        base_url: Plugin base URL.
    """
    token_config = getattr(plugin, "token_config", None)
    if token_config is None:
        return

    from graftpunk.tokens import _CACHE_ATTR, extract_tokens_from_tab

    page_tokens = [t for t in token_config.tokens if t.source == "page" and t.pattern]
    token_results = await extract_tokens_from_tab(tab, page_tokens, base_url) if page_tokens else {}

    # Build token cache from page extraction results and cookie lookups
    for t in token_config.tokens:
        if t.source == "cookie" and t.cookie_name:
            val = session.cookies.get(t.cookie_name)
            if val:
                token_results[t.name] = val

    tcache = _build_token_cache(token_config, token_results)
    if tcache:
        setattr(session, _CACHE_ATTR, tcache)
        LOG.info("login_tokens_extracted", count=len(tcache))


def _extract_and_cache_tokens_selenium(
    plugin: SitePlugin,
    session: Any,
    base_url: str,
) -> None:
    """Extract and cache tokens for selenium backend during login.

    Args:
        plugin: Plugin instance with optional token_config.
        session: Browser session with driver to extract from.
        base_url: Plugin base URL.
    """
    token_config = getattr(plugin, "token_config", None)
    if token_config is None:
        return

    from graftpunk.tokens import _CACHE_ATTR

    token_results: dict[str, str] = {}
    for t in token_config.tokens:
        if t.source == "cookie" and t.cookie_name:
            val = session.cookies.get(t.cookie_name)
            if val:
                token_results[t.name] = val
        elif t.source == "page" and t.pattern:
            try:
                session.driver.get(f"{base_url}{t.page_url}")
                time.sleep(2)
                match = re.search(t.pattern, session.driver.page_source)
                if match:
                    token_results[t.name] = match.group(1)
                else:
                    LOG.warning(
                        "login_token_pattern_not_found",
                        token=t.name,
                        url=f"{base_url}{t.page_url}",
                    )
            except Exception as exc:  # noqa: BLE001 — best-effort token extraction
                LOG.warning("login_token_extraction_failed", token=t.name, error=str(exc))

    tcache = _build_token_cache(token_config, token_results)
    if tcache:
        setattr(session, _CACHE_ATTR, tcache)
        LOG.info("login_tokens_extracted", count=len(tcache))


def generate_login_method(plugin: SitePlugin) -> Any:
    """Generate a login method from declarative plugin attributes.

    Returns an async function for nodriver backend, sync for selenium.

    Args:
        plugin: Plugin instance with declarative login attributes.

    Returns:
        Callable login method (async or sync depending on backend).
    """
    backend = getattr(plugin, "backend", "selenium")

    if backend == "nodriver":
        return _generate_nodriver_login(plugin)
    return _generate_selenium_login(plugin)


def _generate_nodriver_login(plugin: SitePlugin) -> Any:
    """Generate async login method for nodriver backend."""

    async def login(credentials: dict[str, str]) -> bool:
        if plugin.login_config is None:
            raise PluginError(
                f"Plugin '{plugin.site_name}' has no login configuration. "
                "Add a LoginConfig to your plugin definition."
            )
        base_url = plugin.base_url.rstrip("/")
        login_url = plugin.login_config.url
        fields = plugin.login_config.fields
        submit_selector = plugin.login_config.submit
        failure_text = plugin.login_config.failure

        async with BrowserSession(backend="nodriver", headless=False) as session:
            tab = await session.driver.get(f"{base_url}{login_url}")

            # Start header capture for profile extraction (lightweight, no body fetching)
            from graftpunk.observe.capture import create_capture_backend

            _header_capture = create_capture_backend(
                "nodriver", session.driver, get_tab=lambda: tab
            )
            await _header_capture.start_capture_async()

            # Wait for a specific element before interacting (e.g., a form
            # that appears after a redirect completes)
            wait_for_selector = plugin.login_config.wait_for
            if wait_for_selector:
                from nodriver.core.connection import ProtocolException

                _wait_err = (
                    f"Timed out waiting for '{wait_for_selector}' to appear. "
                    "The page may not have loaded or redirected as expected."
                )
                try:
                    wait_el = await _select_with_retry(tab, wait_for_selector)
                except ProtocolException as exc:
                    raise PluginError(_wait_err) from exc
                if wait_el is None:
                    raise PluginError(_wait_err)

            # Fill fields (click before send_keys to prevent keystroke loss)
            for field_name, selector in fields.items():
                value = credentials.get(field_name, "")
                try:
                    element = await _select_with_retry(tab, selector)
                    if element is None:
                        raise PluginError(
                            f"Login field '{field_name}' not found using selector '{selector}'. "
                            "Check your plugin's login.fields configuration."
                        )
                    await element.click()
                    await element.send_keys(value)
                except PluginError:
                    raise
                except Exception as exc:
                    raise PluginError(
                        f"Failed to fill login field '{field_name}' (selector: '{selector}'): {exc}"
                    ) from exc

            # Click submit
            try:
                submit = await _select_with_retry(tab, submit_selector)
                if submit is None:
                    raise PluginError(
                        f"Submit button not found using selector '{submit_selector}'. "
                        "Check your plugin's login.submit configuration."
                    )
                await submit.click()
            except PluginError:
                raise
            except Exception as exc:
                raise PluginError(
                    f"Failed to click submit button (selector: '{submit_selector}'): {exc}"
                ) from exc

            # Fixed delay to allow page to settle after form submission
            await asyncio.sleep(_POST_SUBMIT_DELAY)

            # Check success/failure
            page_text = await tab.get_content()
            success_selector = plugin.login_config.success
            success_found: bool | None = None
            if success_selector:
                # Bare select (no retry): page has settled after submit delay;
                # retrying here would mask genuine login failures.
                success_element = await tab.select(success_selector)
                success_found = success_element is not None

            if not _check_login_result(
                page_text=page_text,
                failure_text=failure_text,
                success_found=success_found,
                success_selector=success_selector or "",
                site_name=plugin.site_name,
            ):
                return False

            # Capture current URL before caching (used for domain display)
            try:
                if tab and hasattr(tab, "url"):
                    session.current_url = tab.url or f"{base_url}{login_url}"
                else:
                    session.current_url = f"{base_url}{login_url}"
            except Exception as exc:  # noqa: BLE001 — URL is optional metadata for display
                LOG.debug("login_url_capture_failed", error=str(exc), backend="nodriver")
                session.current_url = f"{base_url}{login_url}"

            # Extract header profiles from captured network requests
            session._gp_header_profiles = _header_capture.get_header_profiles()

            # Transfer cookies and cache
            await session.transfer_nodriver_cookies_to_session()

            # Extract tokens using the already-open browser (avoids separate launch)
            try:
                await _extract_and_cache_tokens_nodriver(plugin, session, tab, base_url)
            except Exception as exc:  # noqa: BLE001 — best-effort; login already succeeded
                LOG.warning(
                    "login_token_extraction_failed",
                    plugin=plugin.site_name,
                    error=str(exc),
                )

            cache_session(session, plugin.session_name)
            return True

    return login


def _generate_selenium_login(plugin: SitePlugin) -> Any:
    """Generate sync login method for selenium backend."""
    import selenium.common.exceptions
    from selenium.common.exceptions import NoSuchElementException

    def login(credentials: dict[str, str]) -> bool:
        if plugin.login_config is None:
            raise PluginError(
                f"Plugin '{plugin.site_name}' has no login configuration. "
                "Add a LoginConfig to your plugin definition."
            )
        base_url = plugin.base_url.rstrip("/")
        login_url = plugin.login_config.url
        fields = plugin.login_config.fields
        submit_selector = plugin.login_config.submit
        failure_text = plugin.login_config.failure
        success_selector = plugin.login_config.success

        with BrowserSession(backend="selenium", headless=False) as session:
            # Start header capture for profile extraction
            from graftpunk.observe.capture import create_capture_backend

            _header_capture = create_capture_backend("selenium", session.driver)
            _header_capture.start_capture()

            session.driver.get(f"{base_url}{login_url}")

            if plugin.login_config.wait_for:
                raise PluginError(
                    f"Plugin '{plugin.site_name}' uses wait_for, which requires "
                    "the nodriver backend. Set backend='nodriver' or remove wait_for."
                )

            # Fill fields (click before send_keys to prevent keystroke loss)
            for field_name, selector in fields.items():
                value = credentials.get(field_name, "")
                try:
                    element = session.driver.find_element("css selector", selector)
                    element.click()
                    element.send_keys(value)
                except (selenium.common.exceptions.WebDriverException, PluginError) as exc:
                    raise PluginError(
                        f"Failed to fill login field '{field_name}' (selector: '{selector}'): {exc}"
                    ) from exc

            # Click submit
            try:
                submit_el = session.driver.find_element("css selector", submit_selector)
                submit_el.click()
            except (selenium.common.exceptions.WebDriverException, PluginError) as exc:
                raise PluginError(
                    f"Failed to click submit button (selector: '{submit_selector}'): {exc}"
                ) from exc

            # Fixed delay to allow page to settle after form submission
            time.sleep(_POST_SUBMIT_DELAY)

            # Check success/failure
            page_text = session.driver.page_source
            success_found: bool | None = None
            if success_selector:
                try:
                    session.driver.find_element("css selector", success_selector)
                    success_found = True
                except NoSuchElementException:
                    success_found = False

            if not _check_login_result(
                page_text=page_text,
                failure_text=failure_text,
                success_found=success_found,
                success_selector=success_selector or "",
                site_name=plugin.site_name,
            ):
                return False

            # Capture current URL before caching (used for domain display)
            try:
                session.current_url = session.driver.current_url
            except Exception as exc:  # noqa: BLE001 — URL is optional metadata for display
                LOG.debug("login_url_capture_failed", error=str(exc), backend="selenium")
                session.current_url = f"{base_url}{login_url}"

            # Stop capture to parse perf log, then extract profiles
            _header_capture.stop_capture()
            session._gp_header_profiles = _header_capture.get_header_profiles()

            # Cache session
            session.transfer_driver_cookies_to_session()

            # Extract tokens using the already-open browser (avoids separate launch)
            try:
                _extract_and_cache_tokens_selenium(plugin, session, base_url)
            except Exception as exc:  # noqa: BLE001 — best-effort; login already succeeded
                LOG.warning(
                    "login_token_extraction_failed",
                    plugin=plugin.site_name,
                    error=str(exc),
                )

            cache_session(session, plugin.session_name)
            return True

    return login
