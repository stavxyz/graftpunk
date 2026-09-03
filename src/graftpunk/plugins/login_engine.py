"""Declarative login engine for plugins.

Generates login() methods from declarative configuration (CSS selectors,
success/failure indicators). Handles browser lifecycle, cookie transfer,
and session caching automatically.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.parse
from typing import TYPE_CHECKING, Any

from graftpunk import console as gp_console
from graftpunk.exceptions import PluginError
from graftpunk.logging import get_logger
from graftpunk.plugins.cli_plugin import cache_login_session

if TYPE_CHECKING:
    from graftpunk.plugins.cli_plugin import SitePlugin

# NOTE: `BrowserSession` is imported lazily inside the two login bodies below,
# NOT at module scope. This module is on the CLI's eager import path
# (cli/main.py -> cli/plugin_commands.py -> cli/login_commands.py -> here), so a
# module-level `from graftpunk import BrowserSession` pulls in the whole browser
# stack and makes EVERY `gp` invocation — even `gp --version` — fail on a base
# install without the [browser] extra. See test_cli_import_stays_browser_free.

LOG = get_logger(__name__)

_POST_SUBMIT_DELAY = 3  # seconds to wait after form submission for page to settle
_ELEMENT_WAIT_TIMEOUT = 30  # seconds to wait for element during page transitions
_ELEMENT_RETRY_INTERVAL = 1.0  # seconds between retry attempts
_LOGIN_NAV_TIMEOUT = 60  # seconds — login page may redirect through SSO/IdP chains
_FIELD_SETTLE_DELAY = 0.4  # seconds between send_keys and value read-back (see _fill_field)
_FIELD_FILL_ATTEMPTS = 3  # select+type attempts before giving up on a field
# Page text that identifies a rate-limited response (HTTP 429 body) rather than
# a login outcome. Matched case-insensitively against the post-submit page.
_RATE_LIMIT_MARKERS = ("too many requests",)


def _resolve_url(base_url: str, url: str) -> str:
    """Resolve a configured plugin URL against ``base_url``.

    A ``LoginConfig.url`` or a token page URL may be either a path appended to
    ``base_url`` (the common same-host case) or an **absolute** URL, used as-is,
    when the target host differs from the API ``base_url`` (e.g. a login form on
    ``www.example.com`` while the API ``base_url`` is ``api.example.com``). An
    empty string yields ``base_url`` itself.

    Args:
        base_url: The plugin's API base URL (no trailing slash).
        url: A path (``/login``), an absolute URL (``https://host/login``), or
            an empty string.

    Returns:
        The absolute URL to navigate to.
    """
    # Absolute when it carries a scheme (http/https, any case — urlsplit
    # lower-cases the scheme). Otherwise treat it as a path onto base_url.
    return url if urllib.parse.urlsplit(url).scheme else f"{base_url}{url}"


# TODO: Replace Any type annotations with proper nodriver.Tab / nodriver.Element
# types once the upstream SyntaxError in nodriver's CDP codegen is fixed for
# Python 3.14. The bug is in auto-generated CDP domain modules that use invalid
# syntax. Track: https://github.com/niceno/nodriver — when fixed, add
# nodriver.Tab and nodriver.Element to the TYPE_CHECKING import block above.
async def _select_with_retry(
    tab: Any,  # nodriver.Tab — can't import due to upstream SyntaxError in CDP codegen
    selector: str,
    *,
    timeout: float | None = None,
    interval: float | None = None,
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
            Defaults to the current value of ``_ELEMENT_WAIT_TIMEOUT``
            (resolved at call time, not definition time, so monkeypatching
            the module constant takes effect).
        interval: Seconds between retry attempts (must be positive).
            Defaults to the current value of ``_ELEMENT_RETRY_INTERVAL``
            (resolved at call time for the same reason).

    Returns:
        The matched element, or None if not found within timeout.

    Raises:
        ValueError: If timeout or interval are not positive.
        ProtocolException: If timeout expires and last failure was a protocol error.
    """
    if timeout is None:
        timeout = _ELEMENT_WAIT_TIMEOUT
    if interval is None:
        interval = _ELEMENT_RETRY_INTERVAL
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


async def _wait_for_element(
    tab: Any,
    selector: str,
    error_context: str,
) -> None:
    """Wait for an element to appear or raise PluginError with context.

    Args:
        tab: nodriver tab instance.
        selector: CSS selector to wait for.
        error_context: Prefix for error message (e.g., "Step 1" or "Login page").

    Raises:
        PluginError: If element not found or protocol error during wait.
    """
    from nodriver.core.connection import ProtocolException

    error_msg = (
        f"{error_context}: Timed out waiting for '{selector}' to appear. "
        "The page may not have loaded or redirected as expected."
    )
    try:
        element = await _select_with_retry(tab, selector)
    except ProtocolException as exc:
        raise PluginError(error_msg) from exc
    if element is None:
        raise PluginError(error_msg)


async def _read_field_value(tab: Any, selector: str) -> str | None:
    """Read an input's current value from the live DOM, by selector.

    Deliberately re-queries the document rather than using the element handle
    that was typed into: if the page swapped the form, the handle points at a
    detached node whose value is meaningless.

    The page-side expression always returns a JSON string, so nodriver's
    ``evaluate(return_by_value=True)`` quirk (a falsy value comes back as the
    RemoteObject itself) never applies.

    Returns:
        The value as a string. ``""`` when the selector matches nothing in the
        live document (the field is not there to hold a value). ``None`` only
        when the read itself failed (evaluate raised, JS threw, unparseable
        result) -- "cannot verify", which callers must not confuse with
        "empty".
    """
    js = (
        f"(() => {{ const el = document.querySelector({json.dumps(selector)}); "
        "return JSON.stringify({found: !!el, value: el ? String(el.value) : null}); })()"
    )
    try:
        result = await tab.evaluate(js, return_by_value=True)
    except Exception as exc:  # noqa: BLE001 — verification is best-effort
        LOG.debug("login_field_readback_failed", selector=selector, error=str(exc))
        return None
    if not isinstance(result, str):
        # nodriver returns ExceptionDetails when the JS throws.
        LOG.debug(
            "login_field_readback_unparseable",
            selector=selector,
            result_type=type(result).__name__,
            detail=getattr(result, "text", None),
        )
        return None
    try:
        data = json.loads(result)
    except ValueError:
        LOG.debug("login_field_readback_unparseable", selector=selector, result_type="str")
        return None
    if not isinstance(data, dict) or not data.get("found"):
        LOG.debug("login_field_selector_vanished", selector=selector)
        return ""
    value = data.get("value")
    return value if isinstance(value, str) else None


async def _clear_field(element: Any, tab: Any, selector: str) -> None:
    """Empty the field and make sure it stayed empty.

    Keystrokes from a previous attempt can flush after the renderer lag that
    the settle delay exists for; if the read-back is non-empty right after
    clearing, clear once more so nothing is doubled (issue #148 measured that
    doubled text is a worse failure than an empty field).
    """
    await element.clear_input()
    current = await _read_field_value(tab, selector)
    if current:
        LOG.debug("login_field_clear_repeated", selector=selector, residual_len=len(current))
        await element.clear_input()


async def _fill_field(
    tab: Any,
    selector: str,
    value: str,
    *,
    field_name: str,
    step_idx: int,
) -> None:
    """Type ``value`` into the field at ``selector``, verifying it landed.

    Server-rendered sites that re-render the login form shortly after load
    (jQuery/select2 and friends) turn field filling into a race: the handle
    from ``_select_with_retry`` can be detached by the time ``send_keys`` runs,
    the key events dispatch fine, and the characters go nowhere (issue #148).

    Each attempt re-selects the element, clears it, types, waits
    ``_FIELD_SETTLE_DELAY`` (Input.dispatchKeyEvent is acknowledged before the
    renderer updates the value, so an immediate read sees an empty field even
    on success), then reads the value back from the live DOM by selector.
    Interacting with a detached handle can also raise (click/clear resolve the
    backend node); that counts as a failed attempt, not a terminal error.

    Outcomes after ``_FIELD_FILL_ATTEMPTS``:

    - value read back equals ``value``: done.
    - read-back is non-empty but different: the site normalises input
      (lowercasing, masks, autocomplete). Warn and proceed; the browser's own
      validation still guards a genuinely empty required field.
    - read-back is empty (or the selector no longer matches anything): the
      typed value never reached the live element. Raise.
    - read-back impossible: the single fill stands (best-effort verification).

    Raises:
        PluginError: If the field cannot be found, or never accepts the value.
    """
    last_actual: str | None = None
    for attempt in range(1, _FIELD_FILL_ATTEMPTS + 1):
        element = await _select_with_retry(tab, selector)
        if element is None:
            raise PluginError(
                f"Step {step_idx}: Login field '{field_name}' not found "
                f"using selector '{selector}'. "
                "Check your plugin's login step configuration."
            )
        try:
            await element.click()
            await _clear_field(element, tab, selector)
            await element.send_keys(value)
        except Exception as exc:
            if attempt == _FIELD_FILL_ATTEMPTS:
                raise
            LOG.warning(
                "login_field_interaction_failed",
                field=field_name,
                selector=selector,
                attempt=attempt,
                error=str(exc),
                exc_type=type(exc).__name__,
                hint="Element handle may be detached (page re-rendered the form?); re-selecting",
            )
            continue
        if not value:
            return

        await asyncio.sleep(_FIELD_SETTLE_DELAY)
        actual = await _read_field_value(tab, selector)
        if actual is None:
            LOG.debug("login_field_unverifiable", field=field_name, selector=selector)
            return
        if actual == value:
            if attempt > 1:
                LOG.info(
                    "login_field_fill_recovered",
                    field=field_name,
                    selector=selector,
                    attempt=attempt,
                )
            return
        last_actual = actual
        LOG.warning(
            "login_field_value_mismatch",
            field=field_name,
            selector=selector,
            attempt=attempt,
            expected_len=len(value),
            actual_len=len(actual),
            hint="Typed into a detached node (page re-rendered the form?); re-selecting",
        )

    if last_actual:
        LOG.warning(
            "login_field_value_normalized",
            field=field_name,
            selector=selector,
            expected_len=len(value),
            actual_len=len(last_actual),
            hint=(
                "The field holds a non-empty value that differs from what was typed; "
                "the site appears to normalise input. Proceeding with the fill."
            ),
        )
        return

    raise PluginError(
        f"Step {step_idx}: Login field '{field_name}' (selector '{selector}') "
        f"did not accept input after {_FIELD_FILL_ATTEMPTS} attempts. "
        "The page appears to replace the form after load; the typed value never "
        "reached the live element. Try a step-level wait_for on the form, or a "
        "more specific selector."
    )


def _start_login_capture(
    plugin: SitePlugin,
    backend_type: str,
    driver: Any,
    observe_mode: str,
    get_tab: Any = None,
    session_name: str | None = None,
) -> tuple[Any, Any]:
    """Create the capture backend for a login run.

    Returns ``(capture, storage)``. With ``observe_mode == "off"`` the capture
    is the lightweight header-only one used for role extraction and storage is
    None. Otherwise it is a full capture (bodies streamed to the run dir) and
    storage is an ``ObserveStorage`` under the OPERATING session name --
    ``session_name`` when the CLI computed one (``base@label``), else the
    plugin's base name -- so ``gp --observe=full <plugin> login`` files the run
    where the reader looks for that account (#151).
    """
    from graftpunk.observe.capture import create_capture_backend

    if observe_mode == "off":
        return create_capture_backend(backend_type, driver, get_tab=get_tab), None

    # Why the login engine owns this for nodriver instead of handing
    # observe_mode to BrowserSession like the selenium path: the nodriver
    # capture needs a get_tab callable for the tab that the login opens, and
    # its eager body fetch only runs from start_capture_async(), neither of
    # which BrowserSession's sync _start_observe can provide.
    from graftpunk.observe import OBSERVE_BASE_DIR
    from graftpunk.observe.run import make_run_id
    from graftpunk.observe.storage import ObserveStorage, session_dirname

    # Opt-in diagnostics must never break the login: an unsafe session name or
    # an unwritable base dir degrades to the header-only capture with a warning.
    session_slug = session_dirname(session_name or plugin.session_name)
    try:
        storage = ObserveStorage(OBSERVE_BASE_DIR, session_slug, make_run_id())
    except (ValueError, OSError) as exc:
        LOG.warning(
            "login_observe_unavailable",
            plugin=plugin.site_name,
            session_name=session_slug,
            error=str(exc),
            exc_type=type(exc).__name__,
        )
        gp_console.warn(f"Observability capture unavailable for this login: {exc}")
        return create_capture_backend(backend_type, driver, get_tab=get_tab), None
    capture = create_capture_backend(
        backend_type,
        driver,
        get_tab=get_tab,
        bodies_dir=storage.run_dir / "bodies",
    )
    LOG.info(
        "login_observe_capture_started",
        plugin=plugin.site_name,
        mode=observe_mode,
        run_dir=str(storage.run_dir),
    )
    return capture, storage


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
                session.driver.get(_resolve_url(base_url, t.page_url))
                time.sleep(2)
                match = re.search(t.pattern, session.driver.page_source)
                if match:
                    token_results[t.name] = match.group(1)
                else:
                    LOG.warning(
                        "login_token_pattern_not_found",
                        token=t.name,
                        url=_resolve_url(base_url, t.page_url),
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
    # TODO: Both _generate_nodriver_login and _generate_selenium_login contain
    # identical "no login configuration" guards (checking plugin.login_config is
    # None). Extract this check here in generate_login_method() so the guard
    # runs once at generation time rather than at every login() call. This also
    # removes duplication between the two generators.
    backend = getattr(plugin, "backend", "selenium")

    login = (
        _generate_nodriver_login(plugin)
        if backend == "nodriver"
        else _generate_selenium_login(plugin)
    )
    # Lets the CLI tell a generated login from a plugin's hand-written one
    # (help text, which options to offer) without inspecting docstrings.
    login._gp_generated_login = True
    return login


def _generate_nodriver_login(plugin: SitePlugin) -> Any:
    """Generate async login method for nodriver backend."""

    async def login(
        credentials: dict[str, str],
        *,
        headless: bool | None = None,
        observe_mode: str = "off",
        session_name: str | None = None,
        account_identifier: str | None = None,
    ) -> bool:
        """Log in with a nodriver browser.

        Args:
            credentials: Field name -> value.
            headless: Override ``LoginConfig.headless`` for this call; None
                means use the config value.
            observe_mode: "off" or "full". "full" records an observe run
                (screenshot, page source, HAR with bodies, console) under the
                plugin's session name, whether or not the login succeeds.
            session_name: The operating session name to cache under; None
                falls back to ``plugin.session_name``. The CLI passes the
                account-qualified name it computed (``base@label``).
            account_identifier: The unslugified login identifier to record in
                the cached session's metadata; None records nothing.
        """
        if plugin.login_config is None:
            raise PluginError(
                f"Plugin '{plugin.site_name}' has no login configuration. "
                "Add a LoginConfig to your plugin definition."
            )
        base_url = plugin.base_url.rstrip("/")
        login_url = plugin.login_config.url
        login_target = _resolve_url(base_url, login_url)
        failure_text = plugin.login_config.failure
        run_headless = plugin.login_config.headless if headless is None else headless

        from graftpunk import BrowserSession  # lazy: browser stack ([browser] extra)

        async with BrowserSession(backend="nodriver", headless=run_headless) as session:
            try:
                async with asyncio.timeout(_LOGIN_NAV_TIMEOUT):
                    tab = await session.driver.get(login_target)
            except TimeoutError:
                LOG.error(
                    "login_page_navigation_timeout",
                    plugin=plugin.site_name,
                    url=login_target,
                    timeout=_LOGIN_NAV_TIMEOUT,
                )
                raise PluginError(
                    f"Timed out loading login page ({_LOGIN_NAV_TIMEOUT}s). "
                    f"The site may be unreachable or blocked by a WAF. "
                    f"URL: {login_target}"
                ) from None

            # Header capture for role extraction; a full observe run when requested.
            _header_capture, observe_storage = _start_login_capture(
                plugin,
                "nodriver",
                session.driver,
                observe_mode,
                get_tab=lambda: tab,
                session_name=session_name,
            )
            await _header_capture.start_capture_async()
            try:
                return await _run_nodriver_steps(
                    plugin=plugin,
                    session=session,
                    tab=tab,
                    credentials=credentials,
                    login_target=login_target,
                    failure_text=failure_text,
                    base_url=base_url,
                    header_capture=_header_capture,
                    session_name=session_name,
                    account_identifier=account_identifier,
                )
            finally:
                if observe_storage is not None:
                    from graftpunk.observe.run import save_observe_run

                    # Never let a storage problem replace the login outcome
                    # (or the PluginError being unwound) with a disk error.
                    try:
                        await save_observe_run(
                            observe_storage,
                            _header_capture,
                            "login",
                            console=gp_console.err_console,
                            redact=credentials.values(),
                        )
                    except Exception as exc:  # noqa: BLE001 — diagnostics are best-effort
                        LOG.error(
                            "login_observe_save_failed",
                            plugin=plugin.site_name,
                            run_dir=str(observe_storage.run_dir),
                            error=str(exc),
                            exc_type=type(exc).__name__,
                        )

    return login


async def _run_nodriver_steps(
    *,
    plugin: SitePlugin,
    session: Any,
    tab: Any,
    credentials: dict[str, str],
    login_target: str,
    failure_text: str,
    base_url: str,
    header_capture: Any,
    session_name: str | None = None,
    account_identifier: str | None = None,
) -> bool:
    """Execute the configured login steps on an open tab and cache on success.

    *session_name* and *account_identifier* are the operating identity the CLI
    computed; both default to None, which caches under ``plugin.session_name``
    with no recorded identifier.
    """
    assert plugin.login_config is not None  # noqa: S101 — checked by caller
    # Top-level wait_for: wait for a specific element before any steps
    # (e.g., a form that appears after a redirect completes)
    if plugin.login_config.wait_for:
        await _wait_for_element(tab, plugin.login_config.wait_for, "Login page")

    # Execute each step in sequence: wait_for -> fill fields -> submit -> delay
    for step_idx, step in enumerate(plugin.login_config.steps, start=1):
        # Step-level wait_for: wait for element before this step
        if step.wait_for:
            await _wait_for_element(tab, step.wait_for, f"Step {step_idx}")

        # Fill fields, verifying each value landed (see _fill_field)
        for field_name, selector in step.fields.items():
            value = credentials.get(field_name, "")
            try:
                await _fill_field(tab, selector, value, field_name=field_name, step_idx=step_idx)
            except PluginError:
                raise
            except Exception as exc:
                raise PluginError(
                    f"Step {step_idx}: Failed to fill login field '{field_name}' "
                    f"(selector: '{selector}'): {exc}"
                ) from exc

        # Click submit if specified for this step
        if step.submit:
            try:
                submit = await _select_with_retry(tab, step.submit)
                if submit is None:
                    raise PluginError(
                        f"Step {step_idx}: Submit button not found "
                        f"using selector '{step.submit}'. "
                        "Check your plugin's login step configuration."
                    )
                await submit.click()
            except PluginError:
                raise
            except Exception as exc:
                raise PluginError(
                    f"Step {step_idx}: Failed to click submit button "
                    f"(selector: '{step.submit}'): {exc}"
                ) from exc

        # Step-level delay after submit
        if step.delay > 0:
            await asyncio.sleep(step.delay)

    # Fixed delay to allow page to settle after all steps complete
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
            session.current_url = tab.url or login_target
        else:
            session.current_url = login_target
    except Exception as exc:  # noqa: BLE001 — URL is optional metadata for display
        LOG.debug("login_url_capture_failed", error=str(exc), backend="nodriver")
        session.current_url = login_target

    # Extract header roles from captured network requests
    session._gp_header_roles = header_capture.get_header_roles()

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

    cache_login_session(plugin, session, name=session_name, identifier=account_identifier)
    return True


def _generate_selenium_login(plugin: SitePlugin) -> Any:
    """Generate sync login method for selenium backend."""
    import selenium.common.exceptions
    from selenium.common.exceptions import NoSuchElementException

    def login(
        credentials: dict[str, str],
        *,
        headless: bool | None = None,
        observe_mode: str = "off",
        session_name: str | None = None,
        account_identifier: str | None = None,
    ) -> bool:
        """Log in with a selenium browser.

        Args:
            credentials: Field name -> value.
            headless: Override ``LoginConfig.headless`` for this call; None
                means use the config value.
            observe_mode: "off" or "full". "full" makes the BrowserSession
                record an observe run (HAR, console, error screenshot) under
                the plugin's session name.
            session_name: The operating session name to cache under; None
                falls back to ``plugin.session_name``. The CLI passes the
                account-qualified name it computed (``base@label``).
            account_identifier: The unslugified login identifier to record in
                the cached session's metadata; None records nothing.
        """
        if plugin.login_config is None:
            raise PluginError(
                f"Plugin '{plugin.site_name}' has no login configuration. "
                "Add a LoginConfig to your plugin definition."
            )
        base_url = plugin.base_url.rstrip("/")
        login_url = plugin.login_config.url
        login_target = _resolve_url(base_url, login_url)
        failure_text = plugin.login_config.failure
        success_selector = plugin.login_config.success
        run_headless = plugin.login_config.headless if headless is None else headless

        from graftpunk import BrowserSession  # lazy: browser stack ([browser] extra)

        # BrowserSession owns observe for selenium (its capture drains Chrome's
        # performance log, which is single-consumer), so hand it the mode and
        # the session name the run should be filed under.
        browser_session = BrowserSession(
            backend="selenium", headless=run_headless, observe_mode=observe_mode
        )
        # The operating name the CLI computed, so the write-back and any
        # observe run land under this account's session (#151).
        browser_session.session_name = session_name or plugin.session_name
        # The observe HAR carries the login POST; scrub the credentials.
        browser_session.observe_redact = credentials.values()
        with browser_session as session:
            # Header capture for role extraction: reuse the session's observe
            # capture when it has one, else start a lightweight one.
            _header_capture = session.capture if observe_mode != "off" else None
            if _header_capture is None:
                from graftpunk.observe.capture import create_capture_backend

                _header_capture = create_capture_backend("selenium", session.driver)
                _header_capture.start_capture()

            session.driver.get(login_target)

            # Top-level wait_for is not supported for selenium
            if plugin.login_config.wait_for:
                raise PluginError(
                    f"Plugin '{plugin.site_name}' uses wait_for, which requires "
                    "the nodriver backend. Set backend='nodriver' or remove wait_for."
                )

            # Execute each step in sequence
            for step_idx, step in enumerate(plugin.login_config.steps, start=1):
                # Step-level wait_for is not supported for selenium
                if step.wait_for:
                    raise PluginError(
                        f"Step {step_idx}: step.wait_for is not supported for selenium. "
                        "Use nodriver for per-step wait_for."
                    )

                # Fill fields (click before send_keys to prevent keystroke loss)
                for field_name, selector in step.fields.items():
                    value = credentials.get(field_name, "")
                    try:
                        element = session.driver.find_element("css selector", selector)
                        element.click()
                        element.send_keys(value)
                    except (
                        selenium.common.exceptions.WebDriverException,
                        PluginError,
                    ) as exc:
                        raise PluginError(
                            f"Step {step_idx}: Failed to fill login field '{field_name}' "
                            f"(selector: '{selector}'): {exc}"
                        ) from exc

                # Click submit if specified for this step
                if step.submit:
                    try:
                        submit_el = session.driver.find_element("css selector", step.submit)
                        submit_el.click()
                    except (
                        selenium.common.exceptions.WebDriverException,
                        PluginError,
                    ) as exc:
                        raise PluginError(
                            f"Step {step_idx}: Failed to click submit button "
                            f"(selector: '{step.submit}'): {exc}"
                        ) from exc

                # Step-level delay after submit
                if step.delay > 0:
                    time.sleep(step.delay)

            # Fixed delay to allow page to settle after all steps complete
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
                session.current_url = login_target

            # Stop capture to parse perf log, then extract roles
            _header_capture.stop_capture()
            session._gp_header_roles = _header_capture.get_header_roles()

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

            cache_login_session(plugin, session, name=session_name, identifier=account_identifier)
            return True

    return login
