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
        *,
        base_url: str = "",
        **kwargs: Any,
    ) -> None:
        """Initialize a GraftpunkSession.

        Args:
            header_profiles: Dict mapping profile names to header dicts.
                Profiles: "navigation", "xhr", "form".
            base_url: Base URL for constructing Referer headers from paths.
            **kwargs: Additional arguments passed to requests.Session.
        """
        super().__init__(**kwargs)
        self._gp_header_profiles: dict[str, dict[str, str]] = header_profiles or {}
        self.gp_default_profile: str | None = None
        self.gp_base_url: str = base_url
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

    def _resolve_referer(self, referer: str) -> str:
        """Resolve a Referer value from a path or full URL.

        Args:
            referer: A URL path ("/invoice/list") or full URL.
                Paths are joined with gp_base_url. Full URLs (starting
                with "http") are returned as-is.

        Returns:
            The resolved Referer URL string.

        Raises:
            ValueError: If a relative path is given but gp_base_url is not set.
        """
        if referer.startswith(("http://", "https://")):
            return referer

        if not self.gp_base_url:
            raise ValueError(
                f"Cannot resolve relative referer path {referer!r} without "
                f"gp_base_url. Either set gp_base_url on the session or pass "
                f"a full URL as referer."
            )

        base = self.gp_base_url.rstrip("/")
        path = referer if referer.startswith("/") else f"/{referer}"
        return f"{base}{path}"

    def _resolve_profile(self, profile_name: str) -> dict[str, str] | None:
        """Resolve a profile name to its header dict (captured or canonical).

        Checks captured profiles first, then falls back to canonical
        Fetch-spec headers. Logs when falling back or when the profile
        name is unknown.

        Args:
            profile_name: Profile name ("navigation", "xhr", or "form").

        Returns:
            Copy of the header dict, or None if profile is unknown.
        """
        captured = self._gp_header_profiles.get(profile_name)
        if captured:
            return dict(captured)

        canonical = _CANONICAL_REQUEST_HEADERS.get(profile_name)
        if canonical is not None:
            LOG.debug(
                "profile_not_captured_using_canonical",
                detected=profile_name,
                available=list(self._gp_header_profiles.keys()),
            )
            return dict(canonical)

        LOG.warning(
            "unknown_profile_no_headers_applied",
            profile=profile_name,
            available_profiles=list(self._gp_header_profiles.keys()),
            available_canonical=list(_CANONICAL_REQUEST_HEADERS.keys()),
        )
        return None

    def _profile_headers_for(self, profile_name: str) -> dict[str, str]:
        """Get request-type headers for a profile, excluding identity headers.

        Returns captured profile headers if available, falling back to
        canonical Fetch-spec headers. Browser identity headers (User-Agent,
        sec-ch-ua, etc.) are excluded -- they're already on self.headers.

        Args:
            profile_name: Profile name ("navigation", "xhr", or "form").

        Returns:
            Dict of request-type headers, or empty dict if profile unknown.
        """
        headers = self._resolve_profile(profile_name)
        if headers is None:
            return {}

        # Strip identity headers — they're session-level defaults.
        # Case-insensitive filter handles mixed-case CDP headers.
        lower_identity = {h.lower() for h in _BROWSER_IDENTITY_HEADERS}
        return {k: v for k, v in headers.items() if k.lower() not in lower_identity}

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
            return

        if self._gp_header_profiles:
            LOG.warning(
                "no_browser_identity_in_profiles",
                available=list(self._gp_header_profiles.keys()),
            )

    def xhr(
        self,
        method: str,
        url: str,
        *,
        referer: str | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        """Make a request with XHR profile headers.

        Applies captured XHR headers (or canonical Fetch-spec defaults),
        plus browser identity headers from the session. Caller-supplied
        headers override profile headers.

        Args:
            method: HTTP method (GET, POST, PUT, PATCH, DELETE, etc.).
            url: Request URL.
            referer: Referer path ("/page") or full URL. Paths are joined
                with gp_base_url. Omit to send no Referer.
            headers: Additional headers that override profile headers.
            **kwargs: Passed through to requests.Session.request().

        Returns:
            The response object.
        """
        return self._request_with_profile(
            "xhr", method, url, referer=referer, headers=headers, **kwargs
        )

    def navigate(
        self,
        method: str,
        url: str,
        *,
        referer: str | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        """Make a request with navigation profile headers.

        Applies captured navigation headers (or canonical Fetch-spec defaults),
        plus browser identity headers from the session. Simulates a browser
        page navigation (clicking a link, entering a URL).

        Args:
            method: HTTP method (typically GET).
            url: Request URL.
            referer: Referer path ("/page") or full URL. Paths are joined
                with gp_base_url. Omit to send no Referer.
            headers: Additional headers that override profile headers.
            **kwargs: Passed through to requests.Session.request().

        Returns:
            The response object.
        """
        return self._request_with_profile(
            "navigation", method, url, referer=referer, headers=headers, **kwargs
        )

    def form_submit(
        self,
        method: str,
        url: str,
        *,
        referer: str | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        """Make a request with form submission profile headers.

        Applies captured form headers (or canonical Fetch-spec defaults),
        plus browser identity headers from the session. Simulates a browser
        form submission.

        Args:
            method: HTTP method (typically POST).
            url: Request URL.
            referer: Referer path ("/page") or full URL. Paths are joined
                with gp_base_url. Omit to send no Referer.
            headers: Additional headers that override profile headers.
            **kwargs: Passed through to requests.Session.request().

        Returns:
            The response object.
        """
        return self._request_with_profile(
            "form", method, url, referer=referer, headers=headers, **kwargs
        )

    def _request_with_profile(
        self,
        profile_name: str,
        method: str,
        url: str,
        *,
        referer: str | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        """Make a request with explicit profile headers.

        Internal implementation for xhr(), navigate(), and form_submit().
        Composes profile headers, Referer, and caller overrides, then
        delegates to self.request().

        Args:
            profile_name: Profile to apply ("xhr", "navigation", or "form").
            method: HTTP method.
            url: Request URL.
            referer: Optional Referer path or URL.
            headers: Optional caller headers (override profile headers).
            **kwargs: Passed through to requests.Session.request().

        Returns:
            The response object.
        """
        profile_headers = self._profile_headers_for(profile_name)

        if referer is not None:
            profile_headers["Referer"] = self._resolve_referer(referer)

        # Caller headers override profile headers
        if headers:
            profile_headers.update(headers)

        # Note: self.request() calls prepare_request(), which runs
        # _detect_profile() again and may select a different profile.
        # However, our explicit headers are passed as request-level
        # headers, which take precedence over session-level profile
        # headers in the requests merge logic (see prepare_request's
        # priority chain).
        return self.request(method.upper(), url, headers=profile_headers, **kwargs)

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

        # Non-GET/POST methods are always XHR — browsers have no mechanism
        # to issue DELETE/PUT/PATCH/HEAD/OPTIONS as navigation requests.
        # HTML forms only support GET and POST (HTML spec §4.10.18.6).
        if method not in ("GET", "POST"):
            return "xhr"

        # Check for explicit Accept: application/json in caller headers
        caller_headers = request.headers or {}
        caller_accept = caller_headers.get("Accept", "")
        if "application/json" in caller_accept:
            return "xhr"

        # POST with json= → xhr
        if method == "POST" and request.json is not None:
            return "xhr"

        # POST with data= (string or dict) → form
        if method == "POST" and request.data:
            return "form"

        # Default: navigation (GET without Accept: application/json)
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

    def _inject_csrf_tokens(self, prepared: requests.PreparedRequest) -> None:
        """Inject CSRF tokens into a prepared request, mutation methods only.

        CSRF tokens stored in ``_gp_csrf_tokens`` (set by ``tokens.prepare_session``)
        are added to the request headers only for POST, PUT, PATCH, and DELETE.
        This matches browser behavior: CSRF protection is for state-changing
        operations, not read-only GETs.

        Args:
            prepared: The prepared request to conditionally add tokens to.
        """
        csrf_tokens: dict[str, str] = getattr(self, "_gp_csrf_tokens", {})
        if not csrf_tokens:
            return
        if (prepared.method or "GET").upper() not in ("POST", "PUT", "PATCH", "DELETE"):
            return
        for name, value in csrf_tokens.items():
            prepared.headers.setdefault(name, value)

    def prepare_request(self, request: requests.Request, **kwargs: Any) -> requests.PreparedRequest:
        """Prepare a request with auto-detected profile headers.

        Overrides the base implementation to inject header profiles based on
        request characteristics. Profile headers are applied as session-level
        defaults so that any headers explicitly set by the caller take precedence.

        Session headers are temporarily modified during preparation and restored
        afterward (even on exception) to avoid permanently altering session state.

        CSRF tokens (stored separately by ``tokens.prepare_session``) are injected
        only on mutation methods (POST/PUT/PATCH/DELETE).

        Args:
            request: The request to prepare.
            **kwargs: Additional arguments passed to requests.Session.prepare_request().

        Returns:
            A PreparedRequest with applied profile headers (if configured).
        """
        if not self._gp_header_profiles:
            prepared = super().prepare_request(request, **kwargs)
            self._inject_csrf_tokens(prepared)
            return prepared

        profile_name = self._detect_profile(request)
        profile_headers = self._resolve_profile(profile_name)

        if profile_headers:
            # Apply profile headers as session defaults (lowest priority).
            # Priority: request headers (caller-supplied) >
            # user-modified session headers > profile > session defaults.
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

            self._inject_csrf_tokens(prepared)
            return prepared

        prepared = super().prepare_request(request, **kwargs)
        self._inject_csrf_tokens(prepared)
        return prepared
