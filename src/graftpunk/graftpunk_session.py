"""GraftpunkSession — requests.Session subclass with browser header replay."""

from __future__ import annotations

from typing import Any, Final

import requests

from graftpunk.logging import get_logger

LOG = get_logger(__name__)

# Headers that identify the browser itself (shared across all request types).
# These are invariant across navigation/xhr/form — they reflect the browser's
# identity and locale settings, not what kind of request is being made.
_BROWSER_IDENTITY_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "User-Agent",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "Accept-Language",
        "Accept-Encoding",
    }
)

# Canonical Chrome request-type headers used as a fallback when a captured
# profile for the detected request type is not available.
_CANONICAL_HTML_ACCEPT: Final[str] = (
    "text/html,application/xhtml+xml,application/xml;"
    "q=0.9,image/avif,image/webp,image/apng,*/*;"
    "q=0.8,application/signed-exchange;v=b3;q=0.7"
)

_CANONICAL_REQUEST_HEADERS: Final[dict[str, dict[str, str]]] = {
    "navigation": {
        "Accept": _CANONICAL_HTML_ACCEPT,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    },
    "xhr": {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "X-Requested-With": "XMLHttpRequest",
    },
    "form": {
        "Accept": _CANONICAL_HTML_ACCEPT,
        "Content-Type": "application/x-www-form-urlencoded",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    },
}


def _case_insensitive_get(mapping: dict[str, str], key: str) -> str | None:
    """Look up *key* in *mapping* using case-insensitive comparison.

    Args:
        mapping: Header dict to search.
        key: Header name to find (case-insensitive).

    Returns:
        The value if found, otherwise ``None``.
    """
    lower_key = key.lower()
    return next((v for k, v in mapping.items() if k.lower() == lower_key), None)


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
        """Initialize a GraftpunkSession.

        Args:
            header_profiles: Dict mapping profile names to header dicts.
                Profiles: "navigation", "xhr", "form".
            **kwargs: Additional arguments passed to requests.Session.
        """
        super().__init__(**kwargs)
        self._gp_header_profiles: dict[str, dict[str, str]] = header_profiles or {}
        self.gp_default_profile: str | None = None
        # Apply browser identity headers (User-Agent, sec-ch-ua, etc.)
        # BEFORE taking the snapshot so _is_user_set_header treats them
        # as defaults rather than user-set overrides.
        self._apply_browser_identity()
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

    def _apply_browser_identity(self) -> None:
        """Copy browser identity headers from profiles onto the session.

        Extracts identity headers (User-Agent, sec-ch-ua, etc.) from the first
        profile that contains a User-Agent. All profiles originate from the same
        browser, so we only need to check the first profile with identity data.
        """
        for profile_headers in self._gp_header_profiles.values():
            if _case_insensitive_get(profile_headers, "User-Agent") is None:
                continue

            for header_name in _BROWSER_IDENTITY_HEADERS:
                value = _case_insensitive_get(profile_headers, header_name)
                if value is not None:
                    self.headers[header_name] = value
            break

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

        Args:
            key: Header name to check.

        Returns:
            True if the header differs from its initial value, False otherwise.
        """
        current_value = self.headers.get(key)
        default_value = self._gp_default_session_headers.get(key)
        return current_value != default_value

    def prepare_request(self, request: requests.Request, **kwargs: Any) -> requests.PreparedRequest:
        """Prepare a request with auto-detected profile headers.

        Overrides the base implementation to inject header profiles based on
        request characteristics. Profile headers are applied as session-level
        defaults so that any headers explicitly set by the caller take precedence.

        Args:
            request: The request to prepare.
            **kwargs: Additional arguments passed to requests.Session.prepare_request().

        Returns:
            A PreparedRequest with applied profile headers (if configured).
        """
        if not self._gp_header_profiles:
            return super().prepare_request(request, **kwargs)

        profile_name = self._detect_profile(request)
        profile_headers = self._gp_header_profiles.get(profile_name)

        if not profile_headers:
            LOG.debug(
                "profile_not_captured_using_canonical",
                detected=profile_name,
                available=list(self._gp_header_profiles.keys()),
            )
            profile_headers = dict(_CANONICAL_REQUEST_HEADERS.get(profile_name, {}))

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
