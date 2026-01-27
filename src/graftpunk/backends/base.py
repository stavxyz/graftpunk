"""Base protocols and types for browser automation backends.

This module defines the BrowserBackend protocol that all browser automation
backends must implement. The protocol enables pluggable backends while
maintaining a consistent interface for browser lifecycle, navigation,
cookie management, and serialization.

Example:
    >>> from graftpunk.backends import get_backend
    >>> backend = get_backend("selenium", headless=True)
    >>> backend.start()
    >>> backend.navigate("https://example.com")
    >>> cookies = backend.get_cookies()
    >>> backend.stop()
"""

from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class BrowserBackend(Protocol):
    """Protocol defining the browser automation backend interface.

    All browser backends (selenium, nodriver, camoufox, playwright) must
    implement this protocol to be usable with graftpunk's BrowserSession.

    The protocol uses sync methods for the public API. Backends that wrap
    async libraries (nodriver, playwright) handle async internally.

    Attributes:
        BACKEND_TYPE: Class-level identifier for this backend type.
    """

    BACKEND_TYPE: str

    def start(
        self,
        headless: bool = True,
        profile_dir: Path | None = None,
        **options: Any,
    ) -> None:
        """Initialize and start the browser.

        This method must be idempotent - calling it when already started
        should be a no-op.

        Args:
            headless: Run browser without visible window.
            profile_dir: Directory for persistent browser profile.
            **options: Backend-specific options.

        Raises:
            BrowserError: If browser fails to start.
        """
        ...

    def stop(self) -> None:
        """Cleanly stop the browser and release resources.

        This method must be idempotent - calling it when already stopped
        should be a no-op.
        """
        ...

    @property
    def is_running(self) -> bool:
        """Whether the browser is currently running.

        Returns:
            True if browser is started and responsive, False otherwise.
        """
        ...

    def navigate(self, url: str) -> None:
        """Navigate to a URL.

        Args:
            url: URL to navigate to.

        Raises:
            BrowserError: If navigation fails.
        """
        ...

    @property
    def current_url(self) -> str:
        """Get the current page URL.

        Returns:
            Current URL, or empty string if no page loaded.
        """
        ...

    @property
    def page_title(self) -> str:
        """Get the current page title.

        Returns:
            Page title, or empty string if no page loaded.
        """
        ...

    @property
    def page_source(self) -> str:
        """Get the current page HTML source.

        Returns:
            Page HTML source, or empty string if no page loaded.
        """
        ...

    @property
    def driver(self) -> Any:
        """Get the underlying native driver object.

        This provides escape-hatch access to the native driver for
        backend-specific operations not covered by the protocol.

        Returns:
            Native driver (WebDriver, nodriver.Browser, Page, etc.)
            Type varies by backend implementation.

        Note:
            Using this directly couples code to a specific backend.
            Prefer protocol methods when possible.
        """
        ...

    def get_cookies(self) -> list[dict[str, Any]]:
        """Get all cookies from the browser.

        Returns:
            List of cookie dicts with keys: name, value, domain, path,
            secure, httpOnly, expiry (if set), sameSite (if set).
        """
        ...

    def set_cookies(self, cookies: list[dict[str, Any]]) -> None:
        """Set cookies in the browser.

        Args:
            cookies: List of cookie dicts to set. Each dict should have
                at minimum 'name' and 'value' keys.

        Raises:
            BrowserError: If cookies cannot be set (e.g., wrong domain).
        """
        ...

    def delete_all_cookies(self) -> None:
        """Delete all cookies from the browser."""
        ...

    def get_user_agent(self) -> str:
        """Get the browser's User-Agent string.

        Returns:
            User-Agent string, or empty string if not available.
        """
        ...

    def get_state(self) -> dict[str, Any]:
        """Get serializable state for session persistence.

        Returns:
            Dict containing:
                - backend_type: str identifying the backend
                - headless: bool
                - profile_dir: str | None
                - Any other options needed to recreate the backend
        """
        ...

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "BrowserBackend":
        """Recreate backend instance from serialized state.

        Args:
            state: Dict from get_state() call.

        Returns:
            New backend instance configured from state.
            Note: Browser is NOT started - call start() separately.
        """
        ...

    def __enter__(self) -> "BrowserBackend":
        """Context manager entry - start browser.

        Returns:
            Self for use in with statement.
        """
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Context manager exit - stop browser."""
        ...
