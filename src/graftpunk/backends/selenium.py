"""Selenium-based browser backend using undetected-chromedriver.

This backend wraps graftpunk's existing stealth browser automation stack,
providing the BrowserBackend interface while maintaining full backward
compatibility with existing functionality.

Example:
    >>> from graftpunk.backends.selenium import SeleniumBackend
    >>> backend = SeleniumBackend(headless=True, use_stealth=True)
    >>> backend.start()
    >>> backend.navigate("https://example.com")
    >>> print(backend.page_title)
    >>> backend.stop()
"""

from pathlib import Path
from typing import Any

import selenium.common.exceptions
import webdriver_manager.chrome

from graftpunk.chrome import get_chrome_version
from graftpunk.exceptions import BrowserError, ChromeDriverError
from graftpunk.logging import get_logger

LOG = get_logger(__name__)


class SeleniumBackend:
    """Browser backend using Selenium + undetected-chromedriver.

    This is the default backend that wraps graftpunk's existing stealth
    browser automation stack. It supports two modes:

    1. Stealth mode (use_stealth=True): Uses undetected-chromedriver with
       selenium-stealth for anti-detection. Recommended for most sites.

    2. Standard mode (use_stealth=False): Uses regular Selenium with
       webdriver-manager. Faster startup, but more detectable.

    Attributes:
        BACKEND_TYPE: Identifier for this backend type ("selenium").
    """

    BACKEND_TYPE: str = "selenium"

    def __init__(
        self,
        headless: bool = True,
        profile_dir: Path | None = None,
        use_stealth: bool = True,
        default_timeout: int = 15,
        **options: Any,
    ) -> None:
        """Initialize the Selenium backend.

        Args:
            headless: Run browser in headless mode.
            profile_dir: Directory for browser profile persistence. If None,
                stealth mode uses ~/.config/graftpunk/chrome_profile (persistent),
                while standard mode uses a temporary profile (auto-deleted).
            use_stealth: Use undetected-chromedriver with stealth measures.
            default_timeout: Default timeout for element waits in seconds.
                Note: Currently stored for serialization but not actively
                enforced for all operations.
            **options: Additional options (window_size, etc.).
        """
        self._headless = headless
        self._profile_dir = profile_dir
        self._use_stealth = use_stealth
        self._default_timeout = default_timeout
        self._options = options
        self._driver: Any | None = None
        self._started = False

    def start(
        self,
        headless: bool | None = None,
        profile_dir: Path | None = None,
        **options: Any,
    ) -> None:
        """Start the browser.

        Idempotent - calling when already started is a no-op.

        Args:
            headless: Override headless setting.
            profile_dir: Override profile directory.
            **options: Additional backend-specific options.

        Raises:
            BrowserError: If browser fails to start.
        """
        if self._started:
            LOG.debug("selenium_backend_already_started")
            return

        # Allow overrides at start time
        if headless is not None:
            self._headless = headless
        if profile_dir is not None:
            self._profile_dir = profile_dir
        self._options.update(options)

        LOG.info(
            "selenium_backend_starting",
            headless=self._headless,
            use_stealth=self._use_stealth,
        )

        try:
            if self._use_stealth:
                self._start_stealth_driver()
            else:
                self._start_standard_driver()

            self._started = True
            LOG.info("selenium_backend_started")

        except selenium.common.exceptions.WebDriverException as exc:
            LOG.error("selenium_backend_start_failed", error=str(exc))
            raise BrowserError(f"Failed to start Selenium browser: {exc}") from exc
        except OSError as exc:
            LOG.error("selenium_backend_start_failed_os", error=str(exc))
            raise BrowserError(f"Failed to start Selenium browser: {exc}") from exc

    def _start_stealth_driver(self) -> None:
        """Start browser with stealth mode (undetected-chromedriver)."""
        from graftpunk.stealth import create_stealth_driver

        self._driver = create_stealth_driver(
            headless=self._headless,
            profile_dir=self._profile_dir,
        )

    def _start_standard_driver(self) -> None:
        """Start browser with standard Selenium (no stealth)."""
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service

        try:
            chrome_version = get_chrome_version(major=True)
            LOG.info("detected_chrome_version", version=chrome_version)
        except ChromeDriverError as exc:
            LOG.error("chrome_version_detection_failed", error=str(exc))
            raise BrowserError(f"Failed to detect Chrome version: {exc}") from exc

        # Get ChromeDriver path
        driver_path = webdriver_manager.chrome.ChromeDriverManager(
            driver_version=chrome_version
        ).install()

        # Configure options
        chrome_options = webdriver.ChromeOptions()

        if self._headless:
            chrome_options.add_argument("--headless=new")

        # Window size from options or default
        window_size = self._options.get("window_size", "800,600")
        chrome_options.add_argument(f"--window-size={window_size}")

        # Create driver
        service = Service(driver_path)
        self._driver = webdriver.Chrome(service=service, options=chrome_options)

    def stop(self) -> None:
        """Stop the browser and release resources.

        Idempotent - calling when already stopped is a no-op.
        """
        if not self._started:
            return

        LOG.info("selenium_backend_stopping")
        try:
            assert self._driver is not None  # Type narrowing for mypy
            self._driver.quit()
        except selenium.common.exceptions.WebDriverException as exc:
            # Expected errors during browser cleanup - log and continue
            LOG.warning("selenium_backend_stop_error", error=str(exc))
        except OSError as exc:
            # Process-level errors during cleanup - log and continue
            LOG.warning("selenium_backend_stop_error_os", error=str(exc))
        finally:
            self._driver = None
            self._started = False
            LOG.info("selenium_backend_stopped")

    @property
    def is_running(self) -> bool:
        """Whether the browser is currently started.

        Returns:
            True if browser is started, False otherwise.
        """
        return self._started and self._driver is not None

    def navigate(self, url: str) -> None:
        """Navigate to a URL.

        Args:
            url: URL to navigate to.

        Raises:
            BrowserError: If navigation fails or browser not started.
        """
        if not self.is_running:
            self.start()

        LOG.debug("selenium_backend_navigating", url=url)
        try:
            assert self._driver is not None  # Type narrowing for mypy
            self._driver.get(url)
        except selenium.common.exceptions.WebDriverException as exc:
            LOG.error("selenium_backend_navigation_failed", url=url, error=str(exc))
            raise BrowserError(f"Navigation failed: {exc}") from exc

    @property
    def current_url(self) -> str:
        """Get the current page URL.

        Returns:
            Current URL, or empty string if:
            - Browser is not running
            - No page is loaded
            - An error occurred retrieving the URL (logged at debug level)
        """
        if not self.is_running:
            return ""
        try:
            assert self._driver is not None  # Type narrowing for mypy
            return self._driver.current_url or ""
        except selenium.common.exceptions.WebDriverException as exc:
            LOG.debug("selenium_get_current_url_failed", error=str(exc))
            return ""

    @property
    def page_title(self) -> str:
        """Get the current page title.

        Returns:
            Page title, or empty string if:
            - Browser is not running
            - No page is loaded
            - An error occurred retrieving the title (logged at debug level)
        """
        if not self.is_running:
            return ""
        try:
            assert self._driver is not None  # Type narrowing for mypy
            return self._driver.title or ""
        except selenium.common.exceptions.WebDriverException as exc:
            LOG.debug("selenium_get_page_title_failed", error=str(exc))
            return ""

    @property
    def page_source(self) -> str:
        """Get the current page HTML source.

        Returns:
            Page HTML source, or empty string if:
            - Browser is not running
            - No page is loaded
            - An error occurred retrieving the source (logged at debug level)
        """
        if not self.is_running:
            return ""
        try:
            assert self._driver is not None  # Type narrowing for mypy
            return self._driver.page_source or ""
        except selenium.common.exceptions.WebDriverException as exc:
            LOG.debug("selenium_get_page_source_failed", error=str(exc))
            return ""

    @property
    def driver(self) -> Any:
        """Get the underlying WebDriver.

        Starts the browser if not already running (lazy initialization).

        Returns:
            Selenium WebDriver or undetected_chromedriver.Chrome instance.
        """
        if not self._started:
            self.start()
        return self._driver

    def get_cookies(self) -> list[dict[str, Any]]:
        """Get all cookies from the browser.

        Returns:
            List of cookie dicts from Selenium's get_cookies(),
            or empty list if browser is not running or an error occurs
            (errors logged at debug level).
        """
        if not self.is_running:
            return []
        try:
            assert self._driver is not None  # Type narrowing for mypy
            return self._driver.get_cookies() or []
        except selenium.common.exceptions.WebDriverException as exc:
            LOG.debug("selenium_get_cookies_failed", error=str(exc))
            return []

    def set_cookies(self, cookies: list[dict[str, Any]]) -> None:
        """Set cookies in the browser.

        This is a best-effort operation. If setting individual cookies fails,
        a warning is logged but no exception is raised. The method continues
        attempting to set remaining cookies.

        Args:
            cookies: List of cookie dicts to set.
        """
        if not self.is_running:
            self.start()

        assert self._driver is not None  # Type narrowing for mypy
        for cookie in cookies:
            try:
                self._driver.add_cookie(cookie)
            except selenium.common.exceptions.WebDriverException as exc:
                LOG.warning(
                    "selenium_backend_cookie_set_failed",
                    cookie_name=cookie.get("name"),
                    error=str(exc),
                )

    def delete_all_cookies(self) -> None:
        """Delete all cookies from the browser."""
        if not self.is_running:
            return
        try:
            assert self._driver is not None  # Type narrowing for mypy
            self._driver.delete_all_cookies()
        except selenium.common.exceptions.WebDriverException as exc:
            LOG.warning("selenium_backend_delete_cookies_failed", error=str(exc))

    def get_user_agent(self) -> str:
        """Get the browser's User-Agent string.

        Returns:
            User-Agent string from navigator.userAgent,
            or empty string if browser is not running or an error occurs
            (errors logged at debug level).
        """
        if not self.is_running:
            return ""
        try:
            assert self._driver is not None  # Type narrowing for mypy
            return self._driver.execute_script("return navigator.userAgent") or ""
        except selenium.common.exceptions.WebDriverException as exc:
            LOG.debug("selenium_get_user_agent_failed", error=str(exc))
            return ""

    def get_state(self) -> dict[str, Any]:
        """Get serializable state for session persistence.

        Returns:
            Dict with backend configuration for recreation.
        """
        state: dict[str, Any] = {
            "backend_type": self.BACKEND_TYPE,
            "headless": self._headless,
            "use_stealth": self._use_stealth,
            "default_timeout": self._default_timeout,
        }
        if self._profile_dir is not None:
            state["profile_dir"] = str(self._profile_dir)
        state.update(self._options)
        return state

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "SeleniumBackend":
        """Recreate backend instance from serialized state.

        Args:
            state: Dict from get_state() call.

        Returns:
            New SeleniumBackend instance (not started).
        """
        # Extract known parameters
        headless = state.get("headless", True)
        use_stealth = state.get("use_stealth", True)
        default_timeout = state.get("default_timeout", 15)

        profile_dir = state.get("profile_dir")
        if profile_dir is not None:
            profile_dir = Path(profile_dir)

        # Remaining items are additional options
        known_keys = (
            "backend_type",
            "headless",
            "use_stealth",
            "default_timeout",
            "profile_dir",
        )
        options = {k: v for k, v in state.items() if k not in known_keys}

        return cls(
            headless=headless,
            profile_dir=profile_dir,
            use_stealth=use_stealth,
            default_timeout=default_timeout,
            **options,
        )

    def __enter__(self) -> "SeleniumBackend":
        """Context manager entry - start browser.

        Returns:
            Self for use in with statement.
        """
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Context manager exit - stop browser."""
        self.stop()

    def __repr__(self) -> str:
        """Return string representation.

        Returns:
            String showing backend type and status.
        """
        status = "running" if self.is_running else "stopped"
        mode = "stealth" if self._use_stealth else "standard"
        return f"<SeleniumBackend {mode} {status}>"
