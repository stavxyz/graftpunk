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
from typing import TYPE_CHECKING, Any, Self

import selenium.common.exceptions
import webdriver_manager.chrome

from graftpunk.backends.base import Cookie
from graftpunk.chrome import get_chrome_version
from graftpunk.exceptions import BrowserError, ChromeDriverError
from graftpunk.logging import get_logger

if TYPE_CHECKING:
    from selenium.webdriver.remote.webdriver import WebDriver

LOG = get_logger(__name__)


class SeleniumBackend:
    """Browser backend using Selenium + undetected-chromedriver.

    This is the default backend that wraps graftpunk's existing stealth
    browser automation stack. It supports two modes:

    1. Stealth mode (use_stealth=True): Uses undetected-chromedriver with
       selenium-stealth for anti-detection. Recommended for most sites.
       Default window size: 1920,1080 (for anti-detection).

    2. Standard mode (use_stealth=False): Uses regular Selenium with
       webdriver-manager. Faster startup, but more detectable.
       Default window size: 800,600. Override with ``window_size='1024,768'``.

    Attributes:
        BACKEND_TYPE: Identifier for this backend type ("selenium").

    Note:
        The ``default_timeout`` parameter is stored for serialization but
        not actively enforced. It's reserved for future implementation of
        operation timeouts.
    """

    BACKEND_TYPE: str = "selenium"

    # Expected error patterns during browser cleanup - these indicate normal
    # shutdown race conditions and should be logged at DEBUG level
    _EXPECTED_WEBDRIVER_STOP_PATTERNS: frozenset[str] = frozenset(
        [
            "unable to connect",
            "no such window",
            "chrome not reachable",
            "session deleted",
            "target window already closed",
        ]
    )

    _EXPECTED_OS_STOP_PATTERNS: frozenset[str] = frozenset(
        [
            "no such process",
            "broken pipe",
        ]
    )

    def _is_expected_webdriver_stop_error(self, error_str: str) -> bool:
        """Check if WebDriverException message indicates expected cleanup behavior."""
        error_lower = error_str.lower()
        return any(pattern in error_lower for pattern in self._EXPECTED_WEBDRIVER_STOP_PATTERNS)

    def _is_expected_os_stop_error(self, error_str: str) -> bool:
        """Check if OSError message indicates expected cleanup behavior."""
        error_lower = error_str.lower()
        return any(pattern in error_lower for pattern in self._EXPECTED_OS_STOP_PATTERNS)

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
        self._driver: WebDriver | None = None
        self._started = False

    def _get_driver(self) -> "WebDriver":
        """Get driver with None-check for type narrowing.

        Returns:
            The WebDriver instance.

        Raises:
            AssertionError: If driver is None (indicates bug - should never
                happen when is_running is True).
        """
        assert self._driver is not None, "Driver is None - this is a bug"
        return self._driver

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
        try:
            from graftpunk.stealth import create_stealth_driver
        except ImportError as exc:
            raise BrowserError(
                "Stealth dependencies not installed. Install with: pip install graftpunk[standard]"
            ) from exc

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

        # Get ChromeDriver path - wrap in try/except for network/permission errors
        try:
            driver_path = webdriver_manager.chrome.ChromeDriverManager(
                driver_version=chrome_version
            ).install()
        except Exception as exc:
            LOG.error("chromedriver_install_failed", error=str(exc))
            raise BrowserError(f"Failed to install ChromeDriver: {exc}") from exc

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
            if self._is_expected_webdriver_stop_error(str(exc)):
                LOG.debug("selenium_backend_stop_expected", error=str(exc))
            else:
                LOG.warning("selenium_backend_stop_unexpected", error=str(exc))
        except OSError as exc:
            if self._is_expected_os_stop_error(str(exc)):
                LOG.debug("selenium_backend_stop_expected_os", error=str(exc))
            else:
                LOG.warning("selenium_backend_stop_unexpected_os", error=str(exc))
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
            - An error occurred retrieving the URL (logged at warning level)
        """
        if not self.is_running:
            return ""
        try:
            assert self._driver is not None  # Type narrowing for mypy
            return self._driver.current_url or ""
        except selenium.common.exceptions.WebDriverException as exc:
            LOG.warning("selenium_get_current_url_failed", error=str(exc))
            return ""

    @property
    def page_title(self) -> str:
        """Get the current page title.

        Returns:
            Page title, or empty string if:
            - Browser is not running
            - No page is loaded
            - An error occurred retrieving the title (logged at warning level)
        """
        if not self.is_running:
            return ""
        try:
            assert self._driver is not None  # Type narrowing for mypy
            return self._driver.title or ""
        except selenium.common.exceptions.WebDriverException as exc:
            LOG.warning("selenium_get_page_title_failed", error=str(exc))
            return ""

    @property
    def page_source(self) -> str:
        """Get the current page HTML source.

        Returns:
            Page HTML source, or empty string if:
            - Browser is not running
            - No page is loaded
            - An error occurred retrieving the source (logged at warning level)
        """
        if not self.is_running:
            return ""
        try:
            assert self._driver is not None  # Type narrowing for mypy
            return self._driver.page_source or ""
        except selenium.common.exceptions.WebDriverException as exc:
            LOG.warning("selenium_get_page_source_failed", error=str(exc))
            return ""

    @property
    def driver(self) -> Any:
        """Get the underlying WebDriver.

        Starts the browser if not already running (lazy initialization).

        Returns:
            Selenium WebDriver or undetected_chromedriver.Chrome instance.
        """
        if not self.is_running:
            self.start()
        return self._driver

    def get_cookies(self) -> list[Cookie]:
        """Get all cookies from the browser.

        Note:
            Unlike ``set_cookies()`` and ``navigate()``, this method does NOT
            auto-start the browser.

        Returns:
            List of cookie dicts from Selenium's get_cookies(),
            or empty list if browser is not running or an error occurs
            (errors logged at warning level).
        """
        if not self.is_running:
            return []
        try:
            assert self._driver is not None  # Type narrowing for mypy
            return self._driver.get_cookies() or []
        except selenium.common.exceptions.WebDriverException as exc:
            LOG.warning("selenium_get_cookies_failed", error=str(exc))
            return []

    def set_cookies(self, cookies: list[Cookie]) -> int:
        """Set cookies in the browser.

        This is a best-effort operation. If setting individual cookies fails,
        a warning is logged but no exception is raised. The method continues
        attempting to set remaining cookies.

        Args:
            cookies: List of Cookie dicts to set. Each must have 'name' and
                'value'; other fields are optional.

        Returns:
            Number of cookies successfully set.
        """
        if not self.is_running:
            self.start()

        assert self._driver is not None  # Type narrowing for mypy
        success_count = 0
        failed_names: list[str] = []
        for cookie in cookies:
            try:
                self._driver.add_cookie(cookie)
                success_count += 1
            except selenium.common.exceptions.WebDriverException as exc:
                failed_names.append(cookie.get("name", "<unknown>"))
                LOG.warning(
                    "selenium_backend_cookie_set_failed",
                    cookie_name=cookie.get("name"),
                    error=str(exc),
                )

        if failed_names:
            LOG.warning(
                "selenium_backend_cookies_partially_set",
                total=len(cookies),
                success_count=success_count,
                failed_count=len(failed_names),
            )

        return success_count

    def delete_all_cookies(self) -> bool:
        """Delete all cookies from the browser.

        Returns:
            True if cookies were successfully deleted or browser was not running
            (no-op), False if an error occurred (error is logged at warning level).
        """
        if not self.is_running:
            return True  # No cookies to delete
        try:
            assert self._driver is not None  # Type narrowing for mypy
            self._driver.delete_all_cookies()
            return True
        except selenium.common.exceptions.WebDriverException as exc:
            LOG.warning("selenium_backend_delete_cookies_failed", error=str(exc))
            return False

    def get_user_agent(self) -> str:
        """Get the browser's User-Agent string.

        Returns:
            User-Agent string from navigator.userAgent,
            or empty string if browser is not running or an error occurs
            (errors logged at warning level).
        """
        if not self.is_running:
            return ""
        try:
            assert self._driver is not None  # Type narrowing for mypy
            return self._driver.execute_script("return navigator.userAgent") or ""
        except selenium.common.exceptions.WebDriverException as exc:
            LOG.warning("selenium_get_user_agent_failed", error=str(exc))
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
    def from_state(cls, state: dict[str, Any]) -> Self:
        """Recreate backend instance from serialized state.

        Args:
            state: Dict from get_state() call.

        Returns:
            New SeleniumBackend instance (not started).
        """
        # Warn if backend_type doesn't match
        saved_backend = state.get("backend_type")
        if saved_backend and saved_backend != cls.BACKEND_TYPE:
            LOG.warning(
                "backend_type_mismatch",
                expected=cls.BACKEND_TYPE,
                saved=saved_backend,
                hint=f"State was saved by {saved_backend} backend but is being "
                f"restored as {cls.BACKEND_TYPE}. Some settings may not apply.",
            )

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

    def __enter__(self) -> Self:
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
