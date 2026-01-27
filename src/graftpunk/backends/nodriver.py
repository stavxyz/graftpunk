"""NoDriver-based browser backend using CDP-direct automation.

This backend uses nodriver for browser automation, communicating directly
with Chrome via the Chrome DevTools Protocol (CDP) without the WebDriver
binary. This eliminates a major detection vector used by anti-bot systems.

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
from typing import Any

from graftpunk.exceptions import BrowserError
from graftpunk.logging import get_logger

LOG = get_logger(__name__)


class NoDriverBackend:
    """Browser backend using nodriver (CDP-direct).

    NoDriver communicates with Chrome via CDP without the WebDriver binary,
    avoiding the most common detection signals. This backend wraps nodriver's
    async API with a synchronous interface for compatibility with graftpunk's
    BrowserSession.

    Attributes:
        BACKEND_TYPE: Identifier for this backend type ("nodriver").

    Note:
        Default headless=False because nodriver is more detectable in headless
        mode. For best anti-detection, run in visible (headed) mode.
    """

    BACKEND_TYPE: str = "nodriver"

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
                nodriver auto-deletes the profile on exit.
            default_timeout: Default timeout for operations in seconds.
            **options: Additional options passed to nodriver.start():
                - browser_args: List of Chrome arguments
                - browser_executable_path: Path to Chrome binary
                - lang: Browser language (e.g., "en-US")
        """
        self._headless = headless
        self._profile_dir = profile_dir
        self._default_timeout = default_timeout
        self._options = options
        self._browser: Any = None
        self._page: Any = None
        self._started = False

    def _run_async(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Run an async coroutine synchronously.

        Args:
            coro: Coroutine to execute.

        Returns:
            Result of the coroutine.
        """
        return asyncio.run(coro)

    async def _start_async(self) -> None:
        """Async implementation of browser start."""
        import nodriver as uc

        start_kwargs: dict[str, Any] = {
            "headless": self._headless,
        }

        if self._profile_dir is not None:
            start_kwargs["user_data_dir"] = str(self._profile_dir)

        # Pass through additional options
        if "browser_args" in self._options:
            start_kwargs["browser_args"] = self._options["browser_args"]
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
        except Exception as exc:
            LOG.error("nodriver_backend_start_failed", error=str(exc))
            raise BrowserError(f"Failed to start NoDriver browser: {exc}") from exc

    async def _stop_async(self) -> None:
        """Async implementation of browser stop."""
        if self._browser is not None:
            try:
                self._browser.stop()
            except Exception as exc:
                LOG.warning("nodriver_backend_stop_warning", error=str(exc))

    def stop(self) -> None:
        """Stop the browser and release resources.

        Idempotent - calling when already stopped is a no-op.
        """
        if not self._started:
            return

        LOG.info("nodriver_backend_stopping")
        try:
            self._run_async(self._stop_async())
        except Exception as exc:
            LOG.warning("nodriver_backend_stop_error", error=str(exc))
        finally:
            self._browser = None
            self._page = None
            self._started = False
            LOG.info("nodriver_backend_stopped")

    @property
    def is_running(self) -> bool:
        """Whether the browser is currently running.

        Returns:
            True if browser is started.
        """
        return self._started and self._browser is not None

    async def _navigate_async(self, url: str) -> None:
        """Async implementation of navigation."""
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
        except Exception as exc:
            LOG.error("nodriver_backend_navigation_failed", url=url, error=str(exc))
            raise BrowserError(f"Navigation failed: {exc}") from exc

    async def _get_current_url_async(self) -> str:
        """Async implementation of current URL retrieval."""
        if self._page is None:
            return ""
        try:
            return await self._page.evaluate("window.location.href") or ""
        except Exception:
            return ""

    @property
    def current_url(self) -> str:
        """Get the current page URL.

        Returns:
            Current URL, or empty string if no page loaded.
        """
        if not self.is_running:
            return ""
        try:
            return self._run_async(self._get_current_url_async())
        except Exception:
            return ""

    async def _get_page_title_async(self) -> str:
        """Async implementation of page title retrieval."""
        if self._page is None:
            return ""
        try:
            return await self._page.evaluate("document.title") or ""
        except Exception:
            return ""

    @property
    def page_title(self) -> str:
        """Get the current page title.

        Returns:
            Page title, or empty string if no page loaded.
        """
        if not self.is_running:
            return ""
        try:
            return self._run_async(self._get_page_title_async())
        except Exception:
            return ""

    async def _get_page_source_async(self) -> str:
        """Async implementation of page source retrieval."""
        if self._page is None:
            return ""
        try:
            return await self._page.get_content() or ""
        except Exception:
            return ""

    @property
    def page_source(self) -> str:
        """Get the current page HTML source.

        Returns:
            Page HTML source, or empty string if no page loaded.
        """
        if not self.is_running:
            return ""
        try:
            return self._run_async(self._get_page_source_async())
        except Exception:
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
        if not self._started:
            self.start()
        return self._browser

    async def _get_cookies_async(self) -> list[dict[str, Any]]:
        """Async implementation of cookie retrieval."""
        if self._page is None:
            return []
        try:
            cookies = await self._page.get_cookies()
            return cookies or []
        except Exception:
            return []

    def get_cookies(self) -> list[dict[str, Any]]:
        """Get all cookies from the browser.

        Returns:
            List of cookie dicts from nodriver's get_cookies().
        """
        if not self.is_running:
            return []
        try:
            return self._run_async(self._get_cookies_async())
        except Exception:
            return []

    async def _set_cookies_async(self, cookies: list[dict[str, Any]]) -> None:
        """Async implementation of cookie setting."""
        if self._page is not None:
            await self._page.set_cookies(cookies)

    def set_cookies(self, cookies: list[dict[str, Any]]) -> None:
        """Set cookies in the browser.

        Args:
            cookies: List of cookie dicts to set.

        Raises:
            BrowserError: If cookies cannot be set.
        """
        if not self.is_running:
            self.start()

        try:
            self._run_async(self._set_cookies_async(cookies))
        except Exception as exc:
            LOG.warning("nodriver_backend_cookie_set_failed", error=str(exc))

    async def _delete_all_cookies_async(self) -> None:
        """Async implementation of cookie deletion."""
        if self._page is not None:
            # nodriver doesn't have delete_all_cookies, so use CDP directly
            try:
                await self._page.send(
                    "Network.clearBrowserCookies",
                )
            except Exception as exc:
                LOG.debug("nodriver_clear_cookies_cdp_failed", error=str(exc))

    def delete_all_cookies(self) -> None:
        """Delete all cookies from the browser."""
        if not self.is_running:
            return
        try:
            self._run_async(self._delete_all_cookies_async())
        except Exception as exc:
            LOG.warning("nodriver_backend_delete_cookies_failed", error=str(exc))

    async def _get_user_agent_async(self) -> str:
        """Async implementation of user agent retrieval."""
        if self._page is None:
            return ""
        try:
            return await self._page.evaluate("navigator.userAgent") or ""
        except Exception:
            return ""

    def get_user_agent(self) -> str:
        """Get the browser's User-Agent string.

        Returns:
            User-Agent string from navigator.userAgent.
        """
        if not self.is_running:
            return ""
        try:
            return self._run_async(self._get_user_agent_async())
        except Exception:
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
    def from_state(cls, state: dict[str, Any]) -> "NoDriverBackend":
        """Recreate backend instance from serialized state.

        Args:
            state: Dict from get_state() call.

        Returns:
            New NoDriverBackend instance (not started).
        """
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

    def __enter__(self) -> "NoDriverBackend":
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
