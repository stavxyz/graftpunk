"""Browser session management using Requestium (Selenium + requests).

This module provides an enhanced Requestium session with cookie persistence,
HTTPie export, and automatic ChromeDriver management.
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

from bsc.chrome import get_chrome_version
from bsc.exceptions import BrowserError, ChromeDriverError
from bsc.logging import get_logger

LOG = get_logger(__name__)


# requestium.Session has no type stubs, inheritance typing unavailable
class BrowserSession(requestium.Session):  # type: ignore[misc]
    """Extended Requestium session with improved persistence and logging.

    This class combines Selenium WebDriver for browser automation with the
    requests library for HTTP calls, providing seamless cookie transfer and
    session persistence.
    """

    def __init__(
        self,
        *args: Any,
        headless: bool = True,
        default_timeout: int = 15,
        use_stealth: bool = True,
        **kwargs: Any,
    ) -> None:
        """Initialize browser session.

        Args:
            *args: Positional arguments passed to requestium.Session.
            headless: If True, run browser in headless mode.
            default_timeout: Default timeout for element waits in seconds.
            use_stealth: If True, use undetected-chromedriver with anti-detection measures.
            **kwargs: Additional keyword arguments passed to requestium.Session.
        """
        self._use_stealth = use_stealth

        if use_stealth:
            # Use stealth driver (undetected-chromedriver + selenium-stealth)
            from bsc.stealth import create_stealth_driver

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
                    "Failed to create browser session. Try clearing session cache with: bsc clear"
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

    def __getstate__(self) -> dict[str, Any]:
        """Get state for pickling.

        Returns:
            State dictionary with serializable data.
        """
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

        # Only transfer cookies to driver if we have one
        # (for API-only usage, _driver will be None)
        if self._driver is not None:
            try:
                self.transfer_session_cookies_to_driver()
            except selenium.common.exceptions.SessionNotCreatedException as exc:
                LOG.error("failed_to_restore_session", error=str(exc))
                raise BrowserError(
                    "Failed to restore session. Try clearing cache with: bsc clear"
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
