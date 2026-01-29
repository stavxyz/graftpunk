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

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpie.context
import httpie.sessions
import requestium
import requests.utils
import selenium.common.exceptions
import slugify as slugify_lib
import webdriver_manager.chrome

from graftpunk.chrome import get_chrome_version
from graftpunk.exceptions import BrowserError, ChromeDriverError
from graftpunk.logging import get_logger

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
        The ``backend`` parameter is validated and stored for serialization,
        but this class currently uses the selenium/stealth driver directly.
        For full backend abstraction, use ``get_backend()`` from
        ``graftpunk.backends`` instead.

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
                Note: Currently validated and stored for serialization only.
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

        if backend == "nodriver":
            # Use nodriver backend directly (CDP-based, no ChromeDriver)
            # NOTE: We don't start the backend here to avoid async context conflicts.
            # The backend is started lazily when accessing the driver property,
            # or the plugin can start it manually in async context.
            LOG.info("creating_nodriver_browser_session", headless=headless)
            try:
                self._backend_instance = get_backend("nodriver", headless=headless)
                # Don't start here - let it be started in async context by plugin
                # or lazily on first driver access

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
        """
        backend_instance = getattr(self, "_backend_instance", None)
        if getattr(self, "_backend_type", "selenium") == "nodriver" and backend_instance is not None:
            return backend_instance._browser
        # Fall back to requestium's driver
        return getattr(self, "_webdriver", None)

    async def start_async(self) -> None:
        """Start the browser asynchronously (for nodriver backend).

        Call this method in async context before using the driver with nodriver.

        Example:
            session = BrowserSession(backend="nodriver")
            await session.start_async()
            await session.driver.get("https://example.com")
        """
        backend_instance = getattr(self, "_backend_instance", None)
        if backend_instance is not None and hasattr(backend_instance, "_start_async"):
            LOG.info("starting_nodriver_backend_async")
            await backend_instance._start_async()
            backend_instance._started = True

    def quit(self) -> None:
        """Close the browser and clean up resources.

        For nodriver backend, stops the backend properly.
        For selenium backend, quits the WebDriver.
        """
        backend_instance = getattr(self, "_backend_instance", None)
        backend_type = getattr(self, "_backend_type", "selenium")

        if backend_type == "nodriver" and backend_instance is not None:
            LOG.info("stopping_nodriver_session")
            backend_instance.stop()
            self._backend_instance = None
        elif hasattr(self, "_webdriver") and self._webdriver is not None:
            LOG.info("stopping_selenium_session")
            try:
                self._webdriver.quit()
            except Exception as exc:
                LOG.warning("error_stopping_selenium_session", error=str(exc))
            self._webdriver = None

    def __getstate__(self) -> dict[str, Any]:
        """Get state for pickling.

        Returns:
            State dictionary with serializable data.
        """
        backend_type = getattr(self, "_backend_type", "selenium")

        if backend_type == "nodriver":
            # For nodriver, we only need minimal state (cookies from requests.Session)
            import requests

            state = requests.Session.__getstate__(self) if hasattr(requests.Session, "__getstate__") else {}
            state["_backend_type"] = backend_type
            state["_use_stealth"] = getattr(self, "_use_stealth", False)
            state["cookies"] = dict(self.cookies) if hasattr(self, "cookies") else {}
            state["headers"] = dict(self.headers) if hasattr(self, "headers") else {}
            state["session_name"] = getattr(self, "_session_name", "default")
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
        state["current_url"] = self.driver.current_url
        state["session_name"] = self.session_name
        # Backend abstraction state
        state["_backend_type"] = backend_type
        state["_use_stealth"] = getattr(self, "_use_stealth", True)
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        """Restore state from pickle.

        Args:
            state: State dictionary to restore from.

        Raises:
            BrowserError: If session creation fails.
        """
        super().__setstate__(state)
        self.__dict__.update(state)
        for key, value in state.items():
            setattr(self, key, value)

        # Restore backend type (default to selenium for legacy sessions)
        self._backend_type = state.get("_backend_type", "selenium")
        self._use_stealth = state.get("_use_stealth", True)

        # Only transfer cookies to driver if we have one
        # (for API-only usage, _driver will be None)
        if self._driver is not None:
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

        current_url = getattr(self, "current_url", getattr(self.driver, "current_url", ""))
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
