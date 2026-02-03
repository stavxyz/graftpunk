"""NoDriver-based browser backend using CDP-direct automation.

This backend uses nodriver for browser automation, communicating directly
with Chrome via the Chrome DevTools Protocol (CDP) without the WebDriver
binary. This eliminates a major detection vector used by anti-bot systems.

Note:
    This backend uses ``asyncio.run()`` for each operation to bridge nodriver's
    async API to a synchronous interface. This means it cannot be used from
    within an already-running async context (e.g., FastAPI, asyncio event loop).
    If you need async support, use nodriver directly.

Logging:
    This module uses structured logging with intentional log levels:

    - **ERROR**: Operations that fail and raise exceptions (start, navigate)
    - **WARNING**: Operations that fail silently, including property getters
      returning empty values due to errors (browser crash, CDP failure)
    - **DEBUG**: Expected conditions like browser already started/stopped

Example:
    >>> from graftpunk.backends.nodriver import NoDriverBackend
    >>> backend = NoDriverBackend(headless=False)
    >>> backend.start()
    >>> backend.navigate("https://example.com")
    >>> print(backend.page_title)
    >>> backend.stop()
"""

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from graftpunk.backends.base import Cookie
from graftpunk.exceptions import BrowserError
from graftpunk.logging import get_logger

if TYPE_CHECKING:
    import nodriver

LOG = get_logger(__name__)


class NoDriverBackend:
    """Browser backend using nodriver (CDP-direct).

    NoDriver communicates with Chrome via CDP without the WebDriver binary,
    avoiding the most common detection signals. This backend wraps nodriver's
    async API with a synchronous interface for compatibility with graftpunk's
    BrowserSession.

    Attributes:
        BACKEND_TYPE: Identifier for this backend type ("nodriver").

    Warning:
        This backend uses ``asyncio.run()`` for each operation. It cannot be
        used from within an already-running async event loop (e.g., FastAPI,
        asyncio tasks). If you need async support, use nodriver directly.

    Note:
        Default headless=False because nodriver is more detectable in headless
        mode. For best anti-detection, run in visible (headed) mode.

    Design Note:
        This class maintains both ``_started`` and ``_browser`` state. While
        ``_browser is not None`` could theoretically replace ``_started``,
        we keep both for defensive programming: ``is_running`` checks both
        to handle edge cases where the browser crashes (``_browser`` becomes
        invalid but ``_started`` is still True). The ``stop()`` method
        always resets both in a finally block to ensure consistent state.
    """

    BACKEND_TYPE: str = "nodriver"

    # Expected error patterns during browser cleanup - these indicate normal
    # shutdown race conditions and should be logged at DEBUG level
    _EXPECTED_STOP_PATTERNS: frozenset[str] = frozenset(
        [
            "cannot schedule",
            "browser is already closed",
            "no such process",
            "event loop is closed",
            "target closed",
        ]
    )

    def _is_expected_stop_error(self, error_str: str) -> bool:
        """Check if error message indicates expected cleanup behavior."""
        error_lower = error_str.lower()
        return any(pattern in error_lower for pattern in self._EXPECTED_STOP_PATTERNS)

    def __init__(
        self,
        headless: bool = False,
        profile_dir: Path | None = None,
        default_timeout: int = 15,
        **options: Any,
    ) -> None:
        """Initialize the NoDriver backend.

        Args:
            headless: Run browser in headless mode. Default False for better
                anti-detection (nodriver is more detectable when headless).
            profile_dir: Directory for browser profile persistence. If None,
                nodriver uses a temporary profile that is auto-deleted on exit.
                Note: When profile_dir is None, nodriver uses a temporary profile
                that is deleted on exit. In contrast, SeleniumBackend's stealth
                mode defaults to a persistent profile at
                ``~/.config/graftpunk/chrome_profile``.
            default_timeout: Default timeout for operations in seconds.
                Note: Currently stored for serialization but not actively
                enforced for all operations. Reserved for future use.
            **options: Additional options passed to nodriver.start():
                - browser_args: List of Chrome arguments
                - browser_executable_path: Path to Chrome binary
                - lang: Browser language (e.g., "en-US")
        """
        self._headless = headless
        self._profile_dir = profile_dir
        self._default_timeout = default_timeout
        self._options = options
        self._browser: nodriver.Browser | None = None
        self._page: nodriver.Tab | None = None
        self._started = False

    def _run_async(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Run an async coroutine synchronously.

        Args:
            coro: Coroutine to execute.

        Returns:
            Result of the coroutine.

        Raises:
            RuntimeError: If called from within an existing async event loop.
        """
        # Check if we're already in an async context
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running loop - this is what we want, proceed with asyncio.run()
            pass
        else:
            # There IS a running loop - we can't use asyncio.run()
            # Close the coroutine to avoid "coroutine was never awaited" warning
            coro.close()
            raise RuntimeError(
                "NoDriverBackend cannot be used from within an async context "
                "(e.g., FastAPI, asyncio tasks). Use nodriver directly for "
                "async applications, or run this code outside the event loop."
            )

        return asyncio.run(coro)

    async def _start_async(self) -> None:
        """Async implementation of browser start."""
        try:
            import nodriver as uc
        except ImportError as exc:
            raise BrowserError(
                "nodriver package not installed. Install with: pip install graftpunk[nodriver]"
            ) from exc

        # --test-type suppresses Chrome's "unsupported flag" warning banner
        browser_args = ["--test-type"]
        if "browser_args" in self._options:
            browser_args.extend(self._options["browser_args"])

        start_kwargs: dict[str, Any] = {
            "headless": self._headless,
            "sandbox": self._options.get("sandbox", False),
            "browser_args": browser_args,
        }

        if self._profile_dir is not None:
            start_kwargs["user_data_dir"] = str(self._profile_dir)

        # Pass through additional options
        if "browser_executable_path" in self._options:
            start_kwargs["browser_executable_path"] = self._options["browser_executable_path"]
        if "lang" in self._options:
            start_kwargs["lang"] = self._options["lang"]

        self._browser = await uc.start(**start_kwargs)
        # Get initial page/tab - navigate to blank page
        self._page = await self._browser.get("about:blank")

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
            LOG.debug("nodriver_backend_already_started")
            return

        # Allow overrides at start time
        if headless is not None:
            self._headless = headless
        if profile_dir is not None:
            self._profile_dir = profile_dir
        self._options.update(options)

        LOG.info(
            "nodriver_backend_starting",
            headless=self._headless,
        )

        try:
            self._run_async(self._start_async())
            self._started = True
            LOG.info("nodriver_backend_started")
        except (RuntimeError, ConnectionError, TimeoutError, OSError) as exc:
            LOG.error(
                "nodriver_backend_start_failed",
                error=str(exc),
                headless=self._headless,
                profile_dir=str(self._profile_dir) if self._profile_dir else None,
            )
            raise BrowserError(f"Failed to start NoDriver browser: {exc}") from exc

    async def _stop_async(self) -> None:
        """Wrap browser stop for asyncio.run() compatibility.

        Note:
            nodriver's Browser.stop() is synchronous, but we wrap it in
            an async method because _run_async() requires a coroutine to
            pass to asyncio.run().
        """
        if self._browser is not None:
            try:
                self._browser.stop()
            except (RuntimeError, OSError) as exc:
                if self._is_expected_stop_error(str(exc)):
                    LOG.debug("nodriver_backend_stop_expected", error=str(exc))
                else:
                    LOG.warning("nodriver_backend_stop_unexpected", error=str(exc))

    def stop(self) -> None:
        """Stop the browser and release resources.

        Idempotent - calling when already stopped is a no-op.
        """
        if not self._started:
            return

        LOG.info("nodriver_backend_stopping")
        try:
            self._run_async(self._stop_async())
        except (RuntimeError, OSError) as exc:
            if self._is_expected_stop_error(str(exc)):
                LOG.debug("nodriver_backend_stop_expected", error=str(exc))
            else:
                LOG.warning("nodriver_backend_stop_unexpected", error=str(exc))
        finally:
            self._browser = None
            self._page = None
            self._started = False
            LOG.info("nodriver_backend_stopped")

    @property
    def is_running(self) -> bool:
        """Whether the browser is currently started.

        Returns:
            True if browser is started, False otherwise.
        """
        return self._started and self._browser is not None

    async def _navigate_async(self, url: str) -> None:
        """Async implementation of navigation."""
        assert self._browser is not None  # Type narrowing for ty
        self._page = await self._browser.get(url)

    def navigate(self, url: str) -> None:
        """Navigate to a URL.

        Args:
            url: URL to navigate to.

        Raises:
            BrowserError: If navigation fails or browser not started.
        """
        if not self.is_running:
            self.start()

        LOG.debug("nodriver_backend_navigating", url=url)
        try:
            self._run_async(self._navigate_async(url))
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            LOG.error("nodriver_backend_navigation_failed", url=url, error=str(exc))
            raise BrowserError(f"Navigation failed: {exc}") from exc

    async def _get_current_url_async(self) -> str:
        """Async implementation of current URL retrieval."""
        if self._page is None:
            return ""
        try:
            result = await self._page.evaluate("window.location.href")
            return str(result) if result else ""
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            # CDP operations can fail; warn user
            LOG.warning("nodriver_get_current_url_failed", error=str(exc))
            return ""

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
            return self._run_async(self._get_current_url_async())
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            # asyncio.run() can fail if browser crashed; warn user
            LOG.warning("nodriver_current_url_failed", error=str(exc))
            return ""

    async def _get_page_title_async(self) -> str:
        """Async implementation of page title retrieval."""
        if self._page is None:
            return ""
        try:
            result = await self._page.evaluate("document.title")
            return str(result) if result else ""
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            # CDP operations can fail; warn user
            LOG.warning("nodriver_get_page_title_failed", error=str(exc))
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
            return self._run_async(self._get_page_title_async())
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            # asyncio.run() can fail if browser crashed; warn user
            LOG.warning("nodriver_page_title_failed", error=str(exc))
            return ""

    async def _get_page_source_async(self) -> str:
        """Async implementation of page source retrieval."""
        if self._page is None:
            return ""
        try:
            return await self._page.get_content() or ""
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            # CDP operations can fail; warn user
            LOG.warning("nodriver_get_page_source_failed", error=str(exc))
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
            return self._run_async(self._get_page_source_async())
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            # asyncio.run() can fail if browser crashed; warn user
            LOG.warning("nodriver_page_source_failed", error=str(exc))
            return ""

    @property
    def driver(self) -> Any:
        """Get the underlying nodriver Browser object.

        Starts the browser if not already running (lazy initialization).

        Returns:
            nodriver.Browser instance.

        Note:
            Using this directly couples code to nodriver.
            Prefer protocol methods when possible.
        """
        if not self.is_running:
            self.start()
        return self._browser

    async def _get_cookies_async(self) -> list[Cookie]:
        """Async implementation of cookie retrieval."""
        if self._page is None:
            return []
        try:
            cookies = await self._page.get_cookies()
            return cookies or []
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            # CDP operations can fail; warn user
            LOG.warning("nodriver_get_cookies_failed", error=str(exc))
            return []

    def get_cookies(self) -> list[Cookie]:
        """Get all cookies from the browser.

        Note:
            Unlike ``set_cookies()`` and ``navigate()``, this method does NOT
            auto-start the browser.

        Returns:
            List of cookie dicts from nodriver's get_cookies() (CDP format),
            or empty list if browser is not running or an error occurs
            (errors logged at warning level).
        """
        if not self.is_running:
            return []
        try:
            return self._run_async(self._get_cookies_async())
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            # asyncio.run() can fail if browser crashed; warn user
            LOG.warning("nodriver_cookies_failed", error=str(exc))
            return []

    async def _set_cookies_async(self, cookies: list[Cookie]) -> bool:
        """Async implementation of cookie setting.

        Returns:
            True if cookies were set, False if page was not available.
        """
        if self._page is None:
            return False
        await self._page.set_cookies(cookies)
        return True

    def set_cookies(self, cookies: list[Cookie]) -> int:
        """Set cookies in the browser.

        This is a best-effort operation. If setting cookies fails, a warning
        is logged but no exception is raised.

        Args:
            cookies: List of Cookie dicts to set. Each must have 'name' and
                'value'; other fields are optional.

        Returns:
            Number of cookies successfully set (all or none for nodriver,
            since it sets cookies in a single CDP call). Returns 0 if no
            page is available or an error occurs.
        """
        if not self.is_running:
            self.start()

        try:
            success = self._run_async(self._set_cookies_async(cookies))
            if not success:
                LOG.warning(
                    "nodriver_backend_cookie_set_no_page",
                    cookie_count=len(cookies),
                    is_running=self.is_running,
                    browser_present=self._browser is not None,
                )
                return 0
            return len(cookies)
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            # asyncio.run() can fail if browser crashed; best-effort operation
            LOG.warning(
                "nodriver_backend_cookie_set_failed",
                error=str(exc),
                cookie_count=len(cookies),
                cookie_names=[c.get("name", "<unknown>") for c in cookies],
                is_running=self.is_running,
            )
            return 0

    async def _delete_all_cookies_async(self) -> bool:
        """Async implementation of cookie deletion.

        Returns:
            True if cookies were successfully deleted, False if an error occurred.
        """
        if self._page is None:
            LOG.debug("nodriver_delete_cookies_no_page")
            return True  # No page = no cookies to delete
        # nodriver doesn't have delete_all_cookies, so use CDP directly
        try:
            await self._page.send(
                "Network.clearBrowserCookies",  # type: ignore[arg-type]
            )
            return True
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            # CDP operations can fail; warn user
            LOG.warning("nodriver_clear_cookies_cdp_failed", error=str(exc))
            return False

    def delete_all_cookies(self) -> bool:
        """Delete all cookies from the browser.

        Returns:
            True if cookies were successfully deleted or browser was not running
            (no-op), False if an error occurred (error is logged at warning level).
        """
        if not self.is_running:
            return True  # No cookies to delete
        try:
            return self._run_async(self._delete_all_cookies_async())
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            # asyncio.run() can fail if browser crashed; best-effort operation
            LOG.warning("nodriver_backend_delete_cookies_failed", error=str(exc))
            return False

    async def _get_user_agent_async(self) -> str:
        """Async implementation of user agent retrieval."""
        if self._page is None:
            return ""
        try:
            result = await self._page.evaluate("navigator.userAgent")
            return str(result) if result else ""
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            # CDP operations can fail; warn user
            LOG.warning("nodriver_get_user_agent_failed", error=str(exc))
            return ""

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
            return self._run_async(self._get_user_agent_async())
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            # asyncio.run() can fail if browser crashed; warn user
            LOG.warning("nodriver_user_agent_failed", error=str(exc))
            return ""

    def get_state(self) -> dict[str, Any]:
        """Get serializable state for session persistence.

        Returns:
            Dict with backend configuration for recreation.
        """
        state: dict[str, Any] = {
            "backend_type": self.BACKEND_TYPE,
            "headless": self._headless,
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
            New NoDriverBackend instance (not started).
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
        headless = state.get("headless", False)
        default_timeout = state.get("default_timeout", 15)

        profile_dir = state.get("profile_dir")
        if profile_dir is not None:
            profile_dir = Path(profile_dir)

        # Remaining items are additional options
        known_keys = ("backend_type", "headless", "default_timeout", "profile_dir")
        options = {k: v for k, v in state.items() if k not in known_keys}

        return cls(
            headless=headless,
            profile_dir=profile_dir,
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
        mode = "headless" if self._headless else "headed"
        return f"<NoDriverBackend {mode} {status}>"
