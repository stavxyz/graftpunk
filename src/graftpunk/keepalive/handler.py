"""Keepalive handler protocol and implementations.

This module defines the KeepaliveHandler protocol that site-specific
handlers must implement, plus a generic HTTP handler for simple cases.
"""

from typing import Protocol, TypedDict, runtime_checkable

import requests

from graftpunk.logging import get_logger

LOG = get_logger(__name__)


class SessionStatus(TypedDict, total=False):
    """Standard session status returned by keepalive handlers.

    Attributes:
        is_active: Whether the session is currently active
        max_age_ms: Maximum session age in milliseconds
        time_to_expiry_ms: Time until token/session expires in milliseconds
        message: Optional status message from the server
    """

    is_active: bool
    max_age_ms: int
    time_to_expiry_ms: int
    message: str


@runtime_checkable
class KeepaliveHandler(Protocol):
    """Protocol for site-specific keepalive logic.

    Site-specific handlers implement this protocol to provide
    touch/validate functionality for their particular platform.

    Implementations should be registered via entry points:
        [project.entry-points."graftpunk.keepalive_handlers"]
        mysite = "mypackage.handler:MySiteHandler"

    Example:
        >>> class MySiteHandler:
        ...     site_name = "My Site"
        ...
        ...     def touch_session(self, api_session):
        ...         response = api_session.post("https://mysite.com/api/touch")
        ...         return response.ok, {"is_active": True}
        ...
        ...     def validate_session(self, api_session):
        ...         response = api_session.get("https://mysite.com/api/me")
        ...         return response.ok
        ...
        ...     def get_session_status(self, api_session):
        ...         response = api_session.get("https://mysite.com/api/session")
        ...         data = response.json()
        ...         return {"is_active": data["active"], "max_age_ms": 1800000}
    """

    @property
    def site_name(self) -> str:
        """Display name for logging (e.g., 'Human Interest').

        Returns:
            Human-readable site name.
        """
        ...

    def touch_session(
        self,
        api_session: requests.Session,
    ) -> tuple[bool, SessionStatus | None]:
        """Touch session to keep it alive.

        This is the primary keepalive operation. It should make whatever
        API call(s) are needed to prevent the session from timing out.

        Args:
            api_session: requests.Session with cookies from cached browser session.

        Returns:
            Tuple of (success, status). Success is True if the touch succeeded.
            Status contains optional session information like expiry time.
        """
        ...

    def validate_session(
        self,
        api_session: requests.Session,
    ) -> bool:
        """Verify session is truly active.

        Called when touch succeeds but the session might be in a questionable
        state (e.g., token shows expired). This makes an authenticated API
        call to verify the session is actually valid.

        Args:
            api_session: requests.Session with cookies from cached browser session.

        Returns:
            True if the session is valid and authenticated.
        """
        ...

    def get_session_status(
        self,
        api_session: requests.Session,
    ) -> SessionStatus:
        """Get current session status without touching.

        Called to check session state without extending its lifetime.
        Useful for determining optimal touch interval.

        Args:
            api_session: requests.Session with cookies from cached browser session.

        Returns:
            SessionStatus with current session information.
        """
        ...


class GenericHTTPHandler:
    """Generic HTTP keepalive handler for simple REST APIs.

    This handler makes a configurable HTTP request to a touch endpoint
    and optionally validates by calling another endpoint.

    Example:
        >>> handler = GenericHTTPHandler(
        ...     site_name="My API",
        ...     touch_url="https://api.example.com/session/touch",
        ...     validate_url="https://api.example.com/me",
        ... )
        >>> handler.touch_session(api_session)
        (True, {'is_active': True})
    """

    def __init__(
        self,
        site_name: str,
        touch_url: str,
        touch_method: str = "POST",
        validate_url: str | None = None,
        status_url: str | None = None,
        timeout: int = 30,
    ) -> None:
        """Initialize generic HTTP handler.

        Args:
            site_name: Display name for logging.
            touch_url: URL to call for touch/keepalive operation.
            touch_method: HTTP method for touch (POST, PUT, PATCH, GET).
            validate_url: Optional URL to call for session validation.
            status_url: Optional URL to call for session status.
            timeout: Request timeout in seconds.
        """
        self._site_name = site_name
        self.touch_url = touch_url
        self.touch_method = touch_method.upper()
        self.validate_url = validate_url
        self.status_url = status_url
        self.timeout = timeout

    @property
    def site_name(self) -> str:
        """Display name for logging."""
        return self._site_name

    def touch_session(
        self,
        api_session: requests.Session,
    ) -> tuple[bool, SessionStatus | None]:
        """Touch session by calling the configured endpoint.

        Args:
            api_session: requests.Session with authentication.

        Returns:
            Tuple of (success, status).
        """
        try:
            response = api_session.request(
                method=self.touch_method,
                url=self.touch_url,
                timeout=self.timeout,
            )

            success = response.ok
            status: SessionStatus = {"is_active": success}

            # Try to parse JSON response for additional info
            if success:
                try:
                    data = response.json()
                    if isinstance(data, dict):
                        if "maxAge" in data:
                            status["max_age_ms"] = data["maxAge"]
                        if "timeToSessionExpiration" in data:
                            status["time_to_expiry_ms"] = data["timeToSessionExpiration"]
                        if "isActive" in data:
                            status["is_active"] = data["isActive"]
                        if "message" in data:
                            status["message"] = data["message"]
                except (ValueError, KeyError):
                    pass

            LOG.info(
                "touch_session_result",
                site=self._site_name,
                success=success,
                status_code=response.status_code,
            )
            return success, status

        except requests.RequestException as exc:
            LOG.warning(
                "touch_session_failed",
                site=self._site_name,
                error=str(exc),
            )
            return False, None

    def validate_session(
        self,
        api_session: requests.Session,
    ) -> bool:
        """Validate session by calling the validation endpoint.

        Args:
            api_session: requests.Session with authentication.

        Returns:
            True if session is valid.
        """
        if not self.validate_url:
            # No validation URL configured, assume valid
            return True

        try:
            response = api_session.get(
                self.validate_url,
                timeout=self.timeout,
            )
            valid = response.ok

            LOG.info(
                "validate_session_result",
                site=self._site_name,
                valid=valid,
                status_code=response.status_code,
            )
            return valid

        except requests.RequestException as exc:
            LOG.warning(
                "validate_session_failed",
                site=self._site_name,
                error=str(exc),
            )
            return False

    def get_session_status(
        self,
        api_session: requests.Session,
    ) -> SessionStatus:
        """Get session status from the status endpoint.

        Args:
            api_session: requests.Session with authentication.

        Returns:
            SessionStatus with available information.
        """
        status: SessionStatus = {"is_active": False}

        if not self.status_url:
            # No status URL, try to infer from touch
            success, touch_status = self.touch_session(api_session)
            if touch_status:
                return touch_status
            status["is_active"] = success
            return status

        try:
            response = api_session.get(
                self.status_url,
                timeout=self.timeout,
            )

            if response.ok:
                status["is_active"] = True
                try:
                    data = response.json()
                    if isinstance(data, dict):
                        if "maxAge" in data:
                            status["max_age_ms"] = data["maxAge"]
                        if "timeToSessionExpiration" in data:
                            status["time_to_expiry_ms"] = data["timeToSessionExpiration"]
                        if "isActive" in data:
                            status["is_active"] = data["isActive"]
                except (ValueError, KeyError):
                    pass

            LOG.info(
                "get_session_status_result",
                site=self._site_name,
                status=status,
            )
            return status

        except requests.RequestException as exc:
            LOG.warning(
                "get_session_status_failed",
                site=self._site_name,
                error=str(exc),
            )
            return status
