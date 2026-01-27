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

    All browser backends must implement this protocol to be usable with
    graftpunk's BrowserSession. Use ``list_backends()`` to see available
    implementations.

    The protocol uses sync methods for the public API. Backends that wrap
    async libraries (like nodriver) handle async internally.

    Attributes:
        BACKEND_TYPE: Class-level identifier for this backend type.

    Note:
        Backends are NOT thread-safe. Each thread should use its own
        backend instance.
    """

    BACKEND_TYPE: str

    def start(
        self,
        headless: bool | None = None,
        profile_dir: Path | None = None,
        **options: Any,
    ) -> None:
        """Initialize and start the browser.

        This method must be idempotent - calling it when already started
        should be a no-op.

        Args:
            headless: Override headless setting from __init__. If None, uses
                the value from construction. Defaults vary by backend:
                SeleniumBackend defaults to True, NoDriverBackend to False.
            profile_dir: Directory for persistent browser profile.
            **options: Backend-specific options.

        Raises:
            BrowserError: If browser fails to start (implementation-specific;
                the Protocol cannot enforce exception types).
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
        """Whether the browser is currently started.

        Returns:
            True if browser is started, False otherwise.
        """
        ...

    def navigate(self, url: str) -> None:
        """Navigate to a URL.

        If the browser is not running, it will be started automatically
        using the headless setting from __init__.

        Args:
            url: URL to navigate to.

        Raises:
            BrowserError: If navigation fails or browser cannot be started
                (implementation-specific; the Protocol cannot enforce
                exception types).
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

        Starts the browser if not already running (lazy initialization).

        Returns:
            Native driver object. Type varies by backend:
            - SeleniumBackend: Selenium WebDriver or undetected_chromedriver.Chrome
            - NoDriverBackend: nodriver.Browser instance

        Note:
            Using this directly couples code to a specific backend.
            Prefer protocol methods when possible.
        """
        ...

    def get_cookies(self) -> list[dict[str, Any]]:
        """Get all cookies from the browser.

        Returns:
            List of cookie dicts. Keys vary by backend but typically include
            'name' and 'value'. Selenium returns keys like 'domain', 'path',
            'secure', 'httpOnly'. NoDriver returns CDP-format cookies.
            Returns empty list if browser is not running.
        """
        ...

    def set_cookies(self, cookies: list[dict[str, Any]]) -> None:
        """Set cookies in the browser.

        If the browser is not running, it will be started automatically.

        This is a best-effort operation in implementations. If setting
        individual cookies fails, a warning is logged but no exception
        is raised. The method continues attempting to set remaining cookies.

        Args:
            cookies: List of cookie dicts to set. Each dict should have
                at minimum 'name' and 'value' keys.
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
            state: Dict from get_state() call. All keys are optional;
                missing keys use backend defaults.

        Returns:
            New backend instance configured from state.
            Note: Browser is NOT started - call start() separately.

        Note:
            Implementations should be tolerant of missing keys for forward
            compatibility. Unknown keys are typically stored in options.
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
