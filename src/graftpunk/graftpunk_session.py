"""GraftpunkSession — requests.Session subclass with browser header replay."""

from __future__ import annotations

from typing import Any

import requests

from graftpunk.logging import get_logger

LOG = get_logger(__name__)


class GraftpunkSession(requests.Session):
    """A requests.Session that auto-applies captured browser header profiles.

    Header profiles are dicts of real browser headers captured during login,
    classified into profiles: "navigation", "xhr", "form". This session
    auto-detects which profile to apply based on request characteristics,
    or allows explicit override.

    Profile headers are applied as defaults — any headers explicitly passed
    by the caller take precedence and are not overwritten.
    """

    def __init__(
        self,
        header_profiles: dict[str, dict[str, str]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._gp_header_profiles: dict[str, dict[str, str]] = header_profiles or {}
        self.gp_default_profile: str | None = None
        # Snapshot the default session headers so we can distinguish
        # user-modified headers from requests library defaults.
        self._gp_default_session_headers: dict[str, str] = dict(self.headers)

    def headers_for(self, profile: str) -> dict[str, str]:
        """Get the header dict for a specific profile.

        Args:
            profile: Profile name ("navigation", "xhr", or "form").

        Returns:
            Header dict for the profile, or empty dict if not found.
        """
        return dict(self._gp_header_profiles.get(profile, {}))

    def _detect_profile(self, request: requests.Request) -> str:
        """Auto-detect the appropriate header profile for a request.

        Args:
            request: The request about to be sent.

        Returns:
            Profile name ("navigation", "xhr", or "form").
        """
        if self.gp_default_profile:
            return self.gp_default_profile

        method = (request.method or "GET").upper()

        # Check for explicit Accept: application/json in caller headers
        caller_headers = request.headers or {}
        caller_accept = caller_headers.get("Accept", "")
        if "application/json" in caller_accept:
            return "xhr"

        # POST/PUT/PATCH with json= → xhr
        if method in ("POST", "PUT", "PATCH") and request.json is not None:
            return "xhr"

        # POST with data= (string or dict) → form
        if method == "POST" and request.data:
            return "form"

        # Default: navigation
        return "navigation"

    def _is_user_set_header(self, key: str) -> bool:
        """Check if a session header was explicitly changed by the user.

        Compares the current value against the snapshot taken at init time.
        If the value differs or the key is new, the user set it.
        """
        current_value = self.headers.get(key)
        default_value = self._gp_default_session_headers.get(key)
        return current_value != default_value

    def prepare_request(self, request: requests.Request, **kwargs: Any) -> requests.PreparedRequest:
        """Prepare a request with auto-detected profile headers.

        Profile headers are applied as session-level defaults so that
        any headers explicitly set by the caller take precedence.
        """
        if not self._gp_header_profiles:
            return super().prepare_request(request, **kwargs)

        profile_name = self._detect_profile(request)
        profile_headers = self._gp_header_profiles.get(profile_name)

        if not profile_headers:
            # Fall back to navigation if requested profile not available
            profile_headers = self._gp_header_profiles.get("navigation", {})

        if profile_headers:
            # Apply profile headers as session defaults (lowest priority).
            # Priority: caller headers > request headers >
            # user session headers > profile > defaults.
            original_session_headers = dict(self.headers)

            # Start from profile headers as the base
            merged = dict(profile_headers)
            # Layer session headers on top, but only if user changed them
            for key, value in original_session_headers.items():
                if self._is_user_set_header(key):
                    merged[key] = value
                elif key not in merged:
                    # Keep defaults that profile doesn't override
                    merged[key] = value

            self.headers.clear()
            self.headers.update(merged)

            try:
                prepared = super().prepare_request(request, **kwargs)
            finally:
                # Restore original session headers
                self.headers.clear()
                self.headers.update(original_session_headers)

            return prepared

        return super().prepare_request(request, **kwargs)
