"""Browser session management with pluggable browser backends.

This module provides an enhanced browser session with cookie persistence,
HTTPie export, and automatic ChromeDriver management. It supports multiple
browser backends through the `backend` parameter.

Available backends:
    - selenium: Selenium + undetected-chromedriver (default)
    - nodriver: CDP-direct Chrome automation

Note:
    For direct backend usage without requestium, use ``get_backend()``
    from ``graftpunk.backends`` instead.

Example:
    >>> from graftpunk import BrowserSession
    >>> # Default selenium backend
    >>> session = BrowserSession(headless=False, use_stealth=True)
    >>> # Explicit backend selection
    >>> session = BrowserSession(backend="selenium", headless=False)
"""

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpie.context
import httpie.sessions
import requestium
import requests.cookies
import requests.utils
import selenium.common.exceptions
import slugify as slugify_lib
import webdriver_manager.chrome

from graftpunk import console as gp_console
from graftpunk.chrome import get_chrome_version
from graftpunk.exceptions import BrowserError, ChromeDriverError
from graftpunk.logging import get_logger
from graftpunk.observe import OBSERVE_BASE_DIR
from graftpunk.observe.capture import create_capture_backend
from graftpunk.observe.storage import ObserveStorage

LOG = get_logger(__name__)


# requestium.Session has no type stubs, inheritance typing unavailable
class BrowserSession(requestium.Session):
    """Extended Requestium session with pluggable browser backends.

    This class combines browser automation with the requests library for
    HTTP calls, providing seamless cookie transfer and session persistence.

    Supports multiple browser backends through the `backend` parameter:
        - "selenium": Default. Uses undetected-chromedriver + selenium-stealth.
        - "nodriver": CDP-direct Chrome automation.

    Note:
        For the nodriver backend, the browser is started via
        ``await session.start_async()`` rather than at construction time.

    Attributes:
        session_name: Identifier for this session (auto-generated or manual).

    Example:
        >>> session = BrowserSession(headless=False, use_stealth=True)
        >>> session.driver.get("https://example.com")
        >>> session.transfer_driver_cookies_to_session()
    """

    def __init__(
        self,
        *args: Any,
        headless: bool = True,
        default_timeout: int = 15,
        use_stealth: bool = True,
        backend: str = "selenium",
        observe_mode: str = "off",
        **kwargs: Any,
    ) -> None:
        """Initialize browser session.

        Args:
            *args: Positional arguments passed to requestium.Session.
            headless: If True, run browser in headless mode.
            default_timeout: Default timeout for element waits in seconds.
            use_stealth: If True, use undetected-chromedriver with anti-detection.
            backend: Browser backend to use. Default "selenium".
                Available: "selenium" (default), "nodriver".
            observe_mode: Observability capture mode. One of "off", "full".
                Default "off".
            **kwargs: Additional keyword arguments passed to requestium.Session.

        Raises:
            BrowserError: If browser session creation fails.
            ValueError: If unknown backend is specified.
        """
        # Validate backend
        from graftpunk.backends import get_backend, list_backends

        available = list_backends()
        if backend not in available:
            raise ValueError(f"Unknown backend '{backend}'. Available: {', '.join(available)}")

        self._backend_type = backend
        self._use_stealth = use_stealth
        self._backend_instance = None  # For nodriver backend
        self.current_url: str = ""  # Captured before caching for domain display
        self._observe_mode = observe_mode
        self._capture: Any = None
        self._observe_storage: ObserveStorage | None = None

        if backend == "nodriver":
            # Use nodriver backend directly (CDP-based, no ChromeDriver)
            # NOTE: We don't start the backend here to avoid async context conflicts.
            # Call await session.start_async() in async context before using the driver.
            LOG.info("creating_nodriver_browser_session", headless=headless)
            try:
                self._backend_instance = get_backend("nodriver", headless=headless)

                # Initialize minimal session (no driver creation)
                import requests

                requests.Session.__init__(self)

                LOG.info("nodriver_browser_session_initialized", headless=headless)
            except Exception as exc:
                LOG.error("failed_to_create_nodriver_session", error=str(exc))
                raise BrowserError(f"Failed to create nodriver browser session: {exc}") from exc
        elif use_stealth:
            # Use stealth driver (undetected-chromedriver + selenium-stealth)
            from graftpunk.stealth import create_stealth_driver

            LOG.info("creating_stealth_browser_session", headless=headless)
            try:
                # Create stealth driver directly
                self._stealth_driver = create_stealth_driver(headless=headless)

                # Initialize requestium Session without creating a new driver
                # We'll override the driver property to use our stealth driver
                kwargs_minimal = kwargs.copy()
                kwargs_minimal["webdriver_path"] = None  # Skip driver creation
                super().__init__(*args, **kwargs_minimal)

                # Replace requestium's driver with our stealth driver
                self._webdriver = self._stealth_driver

                LOG.info("stealth_browser_session_initialized", headless=headless)
            except (selenium.common.exceptions.WebDriverException, OSError) as exc:
                LOG.error("failed_to_create_stealth_session", error=str(exc))
                raise BrowserError(f"Failed to create stealth browser session: {exc}") from exc
        else:
            # Use standard requestium approach
            try:
                chrome_version = get_chrome_version(major=True)
                LOG.info("detected_chrome_version", version=chrome_version)
            except ChromeDriverError as exc:
                LOG.error("chrome_version_detection_failed", error=str(exc))
                raise BrowserError(f"Failed to detect Chrome version: {exc}") from exc

            defaults: dict[str, Any] = {
                "webdriver_path": webdriver_manager.chrome.ChromeDriverManager(
                    driver_version=chrome_version
                ).install(),
                "browser": "chrome",
                "default_timeout": default_timeout,
            }
            defaults.update(kwargs)
            defaults.setdefault("webdriver_options", {"arguments": []})

            if headless:
                defaults["webdriver_options"]["arguments"].append("headless")

            # Set compact window size (800x600 instead of Chrome's large default)
            defaults["webdriver_options"]["arguments"].append("window-size=800,600")

            try:
                super().__init__(*args, **defaults)
                LOG.info("browser_session_initialized", headless=headless)
            except selenium.common.exceptions.SessionNotCreatedException as exc:
                LOG.error("failed_to_create_browser_session", error=str(exc))
                raise BrowserError(
                    "Failed to create browser session. Try clearing session cache with: gp clear"
                ) from exc

    @property
    def session_name(self) -> str:
        """Get the session name based on page title or hostname.

        Returns:
            Slugified session name.
        """
        if not hasattr(self, "_session_name"):
            hostname = urlparse(self.driver.current_url).hostname
            self._session_name = slugify_lib.slugify(self.driver.title) or hostname or "default"
            if self._session_name == "default":
                LOG.warning(
                    "session_name_fallback_to_default",
                    hint="Could not determine session name from browser state. "
                    "Set session_name explicitly to avoid collisions.",
                )
        return self._session_name

    @session_name.setter
    def session_name(self, value: str) -> None:
        """Set the session name.

        Args:
            value: Session name to set.
        """
        self._session_name = value

    @property
    def driver(self) -> Any:
        """Get the browser driver.

        For nodriver backend, returns the nodriver Browser instance.
        For selenium backend, returns the selenium WebDriver.

        Note: For nodriver backend, call `await session.start_async()` first
        in async context to initialize the browser.

        Returns:
            Browser driver instance.

        Raises:
            BrowserError: If the browser driver is not available.
        """
        backend_type = getattr(self, "_backend_type", "selenium")
        backend_instance = getattr(self, "_backend_instance", None)
        if backend_type == "nodriver":
            if backend_instance is None:
                raise BrowserError(
                    "Nodriver backend not initialized. "
                    "The session may have been closed or not yet created."
                )
            browser = backend_instance._browser
            if browser is None:
                raise BrowserError(
                    "Nodriver browser not started. "
                    "Call 'await session.start_async()' before accessing the driver."
                )
            return browser
        # Selenium backend
        webdriver = getattr(self, "_webdriver", None)
        if webdriver is None:
            raise BrowserError(
                "Selenium WebDriver not available. "
                "The browser session may have been closed or not yet initialized."
            )
        return webdriver

    async def transfer_nodriver_cookies_to_session(self) -> None:
        """Transfer cookies from nodriver browser to the requests session.

        Call this after login before caching the session so that browser
        cookies are available for subsequent API calls.

        Raises:
            BrowserError: If backend is not initialized or browser is not started.
        """
        backend_instance = getattr(self, "_backend_instance", None)
        if backend_instance is None or not hasattr(backend_instance, "_browser"):
            raise BrowserError(
                "Cannot transfer cookies: nodriver backend not initialized. "
                "Call start_async() before transferring cookies."
            )
        browser = backend_instance._browser
        if browser is None:
            raise BrowserError(
                "Cannot transfer cookies: browser is None. Ensure browser was started successfully."
            )
        cookies = await browser.cookies.get_all()
        for cookie in cookies:
            if cookie.value is None:
                LOG.debug("skipping_none_valued_cookie", name=cookie.name, domain=cookie.domain)
                continue
            self.cookies.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path)
        LOG.info("transferred_nodriver_cookies", count=len(cookies))

    async def start_async(self) -> None:
        """Start the browser asynchronously (for nodriver backend).

        Call this method in async context before using the driver with nodriver.
        Must be awaited before accessing the driver property.

        Raises:
            BrowserError: If backend is not nodriver or backend instance is missing.

        Example:
            >>> session = BrowserSession(backend="nodriver")
            >>> await session.start_async()
            >>> await session.driver.get("https://example.com")
        """
        backend_type = getattr(self, "_backend_type", "selenium")
        if backend_type != "nodriver":
            raise BrowserError(
                "start_async() is only supported for the nodriver backend. "
                f"Current backend: '{backend_type}'."
            )
        backend_instance = getattr(self, "_backend_instance", None)
        if backend_instance is None:
            raise BrowserError(
                "Nodriver backend instance is None. "
                "The session may have been closed or not properly initialized."
            )
        LOG.info("starting_nodriver_backend_async")
        await backend_instance._start_async()
        backend_instance._started = True

    def _start_observe(self) -> None:
        """Initialize observability capture if mode is not 'off' and driver exists."""
        if self._observe_mode == "off":
            return
        try:
            driver = self.driver
        except BrowserError:
            LOG.warning(
                "observe_start_skipped_no_driver",
                mode=self._observe_mode,
            )
            gp_console.warn(
                f"Observability capture unavailable: no browser driver. "
                f"Requested mode '{self._observe_mode}' will have no effect."
            )
            return
        import datetime

        run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
        session_name = getattr(self, "_session_name", "default")
        self._observe_storage = ObserveStorage(OBSERVE_BASE_DIR, session_name, run_id)
        self._capture = create_capture_backend(self._backend_type, driver)
        self._capture.start_capture()
        LOG.info("observe_capture_started", mode=self._observe_mode, run_id=run_id)

    def _take_error_screenshot(self, exc_type: type[BaseException] | None) -> None:
        """Take error screenshot if an exception occurred."""
        if exc_type is None or self._capture is None:
            return
        try:
            png_data = self._capture.take_screenshot_sync()
            if png_data is not None and self._observe_storage is not None:
                self._observe_storage.save_screenshot(0, "error-on-exit", png_data)
            elif png_data is None:
                LOG.warning("error_screenshot_unavailable")
        except Exception as exc:
            LOG.error("observe_screenshot_save_failed", error=str(exc), exc_type=type(exc).__name__)

    def _write_observe_data(self) -> None:
        """Write HAR and console logs to storage."""
        if self._observe_storage is None or self._capture is None:
            return
        try:
            self._observe_storage.write_har(self._capture.get_har_entries())
        except Exception as exc:
            LOG.error("observe_har_write_failed", error=str(exc), exc_type=type(exc).__name__)
        try:
            self._observe_storage.write_console_logs(self._capture.get_console_logs())
        except Exception as exc:
            LOG.error(
                "observe_console_log_write_failed", error=str(exc), exc_type=type(exc).__name__
            )

    def _stop_observe(
        self,
        exc_type: type[BaseException] | None = None,
    ) -> None:
        """Stop observability capture and flush data to storage.

        Each step is wrapped individually so one failure does not prevent
        the others from completing. Errors are logged at error level since
        the user explicitly opted into observability.
        """
        if self._capture is None:
            return
        self._take_error_screenshot(exc_type)
        try:
            self._capture.stop_capture()
        except Exception as exc:
            LOG.error("observe_stop_capture_failed", error=str(exc), exc_type=type(exc).__name__)
        self._write_observe_data()
        LOG.info("observe_capture_stopped")

    async def _stop_observe_async(
        self,
        exc_type: type[BaseException] | None = None,
    ) -> None:
        """Async version of _stop_observe that fetches bodies via CDP.

        Each step is wrapped individually so one failure does not prevent
        the others from completing. Errors are logged at error level since
        the user explicitly opted into observability.
        """
        if self._capture is None:
            return
        self._take_error_screenshot(exc_type)
        try:
            await self._capture.stop_capture_async()
        except Exception as exc:
            LOG.error("observe_stop_capture_failed", error=str(exc), exc_type=type(exc).__name__)
        self._write_observe_data()
        LOG.info("observe_capture_stopped")

    def __enter__(self) -> "BrowserSession":
        self._start_observe()
        return self

    def __exit__(  # type: ignore[override]  # requests.Session uses *args
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self._stop_observe(exc_type)
        self.quit()

    async def __aenter__(self) -> "BrowserSession":
        await self.start_async()
        self._start_observe()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        await self._stop_observe_async(exc_type)
        await self._quit_async()

    def _quit_selenium(self) -> None:
        """Stop the selenium WebDriver if present."""
        if hasattr(self, "_webdriver") and self._webdriver is not None:
            LOG.info("stopping_selenium_session")
            try:
                self._webdriver.quit()
            except Exception as exc:  # noqa: BLE001 — selenium raises varied exception types
                LOG.error(
                    "error_stopping_selenium_session", error=str(exc), exc_type=type(exc).__name__
                )
            self._webdriver = None

    def quit(self) -> None:
        """Close the browser and clean up resources.

        Properly closes the browser driver and releases all associated resources.
        For nodriver backend, stops the backend. For selenium backend, quits the
        WebDriver. Safe to call multiple times.

        Raises:
            No exceptions raised; errors are logged internally.
        """
        backend_instance = getattr(self, "_backend_instance", None)
        backend_type = getattr(self, "_backend_type", "selenium")

        if backend_type == "nodriver" and backend_instance is not None:
            LOG.info("stopping_nodriver_session")
            try:
                backend_instance.stop()
            except (RuntimeError, OSError) as exc:
                LOG.error(
                    "error_stopping_nodriver_session", error=str(exc), exc_type=type(exc).__name__
                )
            self._backend_instance = None
        else:
            self._quit_selenium()

    async def _quit_async(self) -> None:
        """Async version of quit for use in ``__aexit__``.

        Uses the backend's async stop path to avoid nested ``asyncio.run()``
        errors when shutting down from within an existing event loop.
        """
        backend_instance = getattr(self, "_backend_instance", None)
        backend_type = getattr(self, "_backend_type", "selenium")

        if backend_type == "nodriver" and backend_instance is not None:
            LOG.info("stopping_nodriver_session")
            try:
                # Guard for potential non-NoDriverBackend implementations
                if hasattr(backend_instance, "stop_async"):
                    await backend_instance.stop_async()
                else:
                    backend_instance.stop()
            except (RuntimeError, OSError) as exc:
                LOG.error(
                    "error_stopping_nodriver_session", error=str(exc), exc_type=type(exc).__name__
                )
            self._backend_instance = None
        else:
            self._quit_selenium()

    def __getstate__(self) -> dict[str, Any]:
        """Get state for pickling.

        Serializes the session state, excluding non-picklable browser driver objects.
        For nodriver backend, only HTTP session state is preserved. For selenium
        backend, driver handles are cleared and HTTP session state is preserved
        along with browser configuration.

        Returns:
            State dictionary with serializable session data including cookies,
            headers, current URL, and backend configuration.
        """
        backend_type = getattr(self, "_backend_type", "selenium")

        if backend_type == "nodriver":
            # For nodriver, we only need minimal state (cookies from requests.Session).
            # requests.Session has no custom __getstate__, so we build state manually.
            state: dict[str, Any] = {}
            state["_backend_type"] = backend_type
            state["_use_stealth"] = getattr(self, "_use_stealth", False)
            state["cookies"] = self.cookies if hasattr(self, "cookies") else {}
            state["headers"] = dict(self.headers) if hasattr(self, "headers") else {}
            state["session_name"] = getattr(self, "_session_name", "default")
            # Capture current_url: prefer explicit attribute, then try browser tab
            current_url = getattr(self, "current_url", "") or ""
            if not current_url:
                try:
                    browser = self.driver
                    if browser and hasattr(browser, "targets") and browser.targets:
                        current_url = getattr(browser.targets[0], "url", "") or ""
                except Exception as exc:  # noqa: BLE001
                    LOG.debug("url_capture_failed_during_serialization", error=str(exc))
            state["current_url"] = current_url
            state["_gp_header_profiles"] = getattr(self, "_gp_header_profiles", {})
            state["_gp_cached_tokens"] = getattr(self, "_gp_cached_tokens", {})
            return state

        # Selenium/requestium path
        state = super().__getstate__()
        state["_driver"] = None
        state["_driver_initializer"] = self._driver_initializer
        state["webdriver_path"] = self.webdriver_path
        state["default_timeout"] = self.default_timeout
        state["webdriver_options"] = self.webdriver_options
        state["_last_requests_url"] = self._last_requests_url
        state["cookies"] = self.cookies
        state["headers"] = self.headers
        state["auth"] = self.auth
        state["proxies"] = self.proxies
        state["hooks"] = self.hooks
        state["params"] = self.params
        state["verify"] = self.verify
        try:
            state["current_url"] = self.driver.current_url
        except (BrowserError, selenium.common.exceptions.WebDriverException) as exc:
            state["current_url"] = ""
            LOG.warning("driver_current_url_unavailable_during_serialization", error=str(exc))
        state["session_name"] = self.session_name
        # Backend abstraction state
        state["_backend_type"] = backend_type
        state["_use_stealth"] = getattr(self, "_use_stealth", True)
        state["_gp_header_profiles"] = getattr(self, "_gp_header_profiles", {})
        state["_gp_cached_tokens"] = getattr(self, "_gp_cached_tokens", {})
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        """Restore state from pickle.

        Reconstructs the session from a pickled state dictionary. For nodriver
        backend, only HTTP session state is restored (browser is not available).
        For selenium backend, WebDriver must be recreated after unpickling.

        Args:
            state: State dictionary containing serialized session data with keys
                like 'cookies', 'headers', '_backend_type', '_use_stealth',
                and other session configuration.

        Raises:
            BrowserError: If session restoration fails (typically for selenium
                backend when WebDriver creation is needed).
        """
        # Restore backend type first to determine how to restore
        self._backend_type = state.get("_backend_type", "selenium")
        self._use_stealth = state.get("_use_stealth", True)

        if self._backend_type == "nodriver":
            # For nodriver sessions, just restore as requests.Session
            # (browser is not serialized, only HTTP client state)
            import requests

            requests.Session.__init__(self)
            # Restore headers if present
            if "headers" in state and state["headers"]:
                self.headers.update(state["headers"])
            # Restore cookies if present
            if "cookies" in state and state["cookies"]:
                self.cookies = state["cookies"]
            # Restore session name
            if "session_name" in state:
                self._session_name = state["session_name"]
            self._backend_instance = None  # No browser restored
            self._gp_header_profiles = state.get("_gp_header_profiles", {})
            self._gp_cached_tokens = state.get("_gp_cached_tokens", {})
        else:
            # Selenium/requestium path
            # requestium/requests don't define __setstate__, so this resolves
            # to object.__setstate__ which does self.__dict__.update(state).
            # We keep __dict__.update as a safety net to ensure our custom
            # attributes (_backend_type, _use_stealth, etc.) are restored,
            # in case a future requestium version overrides __setstate__.
            super().__setstate__(state)
            self.__dict__.update(state)
            self._gp_header_profiles = state.get("_gp_header_profiles", {})
            self._gp_cached_tokens = state.get("_gp_cached_tokens", {})

            # Only transfer cookies to driver if we have one
            # (for API-only usage, _driver will be None)
            if getattr(self, "_driver", None) is not None:
                try:
                    self.transfer_session_cookies_to_driver()
                except selenium.common.exceptions.SessionNotCreatedException as exc:
                    LOG.error("failed_to_restore_session", error=str(exc))
                    raise BrowserError(
                        "Failed to restore session. Try clearing cache with: gp clear"
                    ) from exc

    def save_httpie_session(self, session_name: str | None = None) -> Path:
        """Save session cookies to HTTPie format for CLI HTTP requests.

        Args:
            session_name: Optional session name. Uses self.session_name if not provided.

        Returns:
            Path to the saved HTTPie session file.
        """
        if session_name is None:
            session_name = self.session_name

        LOG.info("saving_httpie_session", name=session_name)

        env = httpie.context.Environment()
        httpie_session_path = Path(env.config.directory) / "sessions" / f"{session_name}.json"

        try:
            driver_url = self.driver.current_url
        except (BrowserError, selenium.common.exceptions.WebDriverException) as exc:
            driver_url = ""
            LOG.warning("driver_url_unavailable_for_httpie_session", error=str(exc))
        current_url = getattr(self, "current_url", driver_url)
        parsed = urlparse(current_url)
        current_hostname = f"{parsed.scheme}://{parsed.hostname}" if current_url else ""

        httpie_session = httpie.sessions.get_httpie_session(
            env=env,
            config_dir=env.config.directory,
            session_name=str(httpie_session_path),
            url=current_hostname,
            host=None,
        )

        cookiejar_dict = requests.utils.dict_from_cookiejar(self.cookies)
        cookiejar = requests.utils.cookiejar_from_dict(cookiejar_dict)
        httpie_session.cookie_jar = cookiejar
        httpie_session.load()
        httpie_session.post_process_data(httpie_session)
        httpie_session.save()

        LOG.info(
            "successfully_saved_httpie_session",
            name=session_name,
            path=str(httpie_session_path),
        )

        return httpie_session_path


async def inject_cookies_to_nodriver(
    tab: Any, cookies: "requests.cookies.RequestsCookieJar"
) -> int:
    """Inject cached session cookies into a nodriver browser tab via CDP.

    This is the inverse of transfer_nodriver_cookies_to_session() — it loads
    cookies FROM a cached RequestsCookieJar INTO a nodriver browser.

    Can be called before any navigation. CookieParam includes the domain
    field, so CDP can set cookies on any domain without needing to be on
    that domain first.

    Args:
        tab: nodriver Tab instance (the active browser tab).
        cookies: RequestsCookieJar from a cached session.

    Returns:
        Number of cookies injected.
    """
    import nodriver.cdp.network as cdp_net
    import nodriver.cdp.storage as cdp_storage

    # IMPORTANT: Do NOT use nodriver's browser.cookies.set_all() — it has a bug
    # where set_all() calls get_all() first, then overwrites the caller's
    # cookies parameter with existing browser cookies, effectively ignoring input.
    # Use the low-level CDP approach instead.
    cookie_params = []
    for cookie in cookies:
        if cookie.value is None:
            continue
        cookie_params.append(
            cdp_net.CookieParam(  # type: ignore[attr-defined]
                name=cookie.name,
                value=cookie.value,
                domain=cookie.domain,
                path=cookie.path,
            )
        )
    if cookie_params:
        await tab.send(cdp_storage.set_cookies(cookie_params))
    return len(cookie_params)
