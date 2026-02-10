"""GraftpunkSession — requests.Session subclass with browser header replay."""

# Role Registry
# ==============
# A "role" is a named set of HTTP headers that simulates a specific browser
# request type (navigation, XHR, form submission, or any custom role defined
# by a plugin).  Built-in roles register at import time using the same
# register_role() call that plugins use.

from __future__ import annotations

from typing import Any, Final

import requests

from graftpunk.logging import get_logger
from graftpunk.tokens import _CSRF_TOKENS_ATTR

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

# HTTP methods that trigger CSRF token injection.  Browsers only enforce
# CSRF protection for state-changing operations; read-only methods (GET,
# HEAD, OPTIONS) never carry tokens.
_MUTATION_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Canonical Chrome request-type headers used as a fallback when a captured
# role for the detected request type is not available.
_CANONICAL_HTML_ACCEPT: Final[str] = (
    "text/html,application/xhtml+xml,application/xml;"
    "q=0.9,image/avif,image/webp,image/apng,*/*;"
    "q=0.8,application/signed-exchange;v=b3;q=0.7"
)

# ---------------------------------------------------------------------------
# Role Registry — public API
# ---------------------------------------------------------------------------

_ROLE_REGISTRY: dict[str, dict[str, str]] = {}


def register_role(name: str, headers: dict[str, str]) -> None:
    """Register a header role (built-in or plugin-defined).

    Overwrites any existing role with the same name, logging a warning
    when this happens.  This allows plugins to customize built-in roles.

    Not thread-safe — intended for use at import time or during plugin
    discovery, not from concurrent request handlers.

    Args:
        name: Role name (e.g. ``"xhr"``, ``"navigation"``, ``"api"``).
            Must be a non-empty string.
        headers: Dict of HTTP headers that define this role.
            Must contain at least one header.

    Raises:
        ValueError: If *name* is empty or *headers* is empty.
    """
    if not name or not name.strip():
        raise ValueError("Role name must be a non-empty string")
    if not headers:
        raise ValueError(f"Role '{name}' must have at least one header")
    if name in _ROLE_REGISTRY:
        LOG.warning("role_overwritten", role=name)
    _ROLE_REGISTRY[name] = dict(headers)


def list_roles() -> list[str]:
    """Return sorted list of all registered role names."""
    return sorted(_ROLE_REGISTRY.keys())


def get_role_headers(name: str) -> dict[str, str] | None:
    """Get headers for a registered role.

    Args:
        name: Role name.

    Returns:
        Copy of the header dict, or None if not registered.
    """
    headers = _ROLE_REGISTRY.get(name)
    return dict(headers) if headers is not None else None


# Register built-in roles
register_role(
    "navigation",
    {
        "Accept": _CANONICAL_HTML_ACCEPT,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    },
)
register_role(
    "xhr",
    {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "X-Requested-With": "XMLHttpRequest",
    },
)
register_role(
    "form",
    {
        "Accept": _CANONICAL_HTML_ACCEPT,
        "Content-Type": "application/x-www-form-urlencoded",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    },
)


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
    """A requests.Session that auto-applies captured browser header roles.

    Header roles are dicts of real browser headers captured during login,
    classified into roles: "navigation", "xhr", "form". This session
    auto-detects which role to apply based on request characteristics,
    or allows explicit override.

    Role headers are applied as defaults — any headers explicitly passed
    by the caller take precedence and are not overwritten.
    """

    def __init__(
        self,
        header_roles: dict[str, dict[str, str]] | None = None,
        *,
        base_url: str = "",
        **kwargs: Any,
    ) -> None:
        """Initialize a GraftpunkSession.

        Args:
            header_roles: Dict mapping role names to header dicts.
                Roles: "navigation", "xhr", "form", or any custom name.
            base_url: Base URL for constructing Referer headers from paths.
            **kwargs: Additional arguments passed to requests.Session.
        """
        super().__init__(**kwargs)
        self._gp_header_roles: dict[str, dict[str, str]] = (
            dict(header_roles) if header_roles else {}
        )
        self._gp_csrf_tokens: dict[str, str] = {}
        self.gp_default_role: str | None = None
        self.gp_base_url: str = base_url
        # Apply browser identity headers (User-Agent, sec-ch-ua, etc.)
        # BEFORE taking the snapshot so _is_user_set_header treats them
        # as defaults rather than user-set overrides.
        self._apply_browser_identity()
        # Snapshot the default session headers so we can distinguish
        # user-modified headers from requests library defaults.
        self._gp_default_session_headers: dict[str, str] = dict(self.headers)

    def headers_for(self, role: str) -> dict[str, str]:
        """Get the header dict for a specific role.

        Args:
            role: Role name ("navigation", "xhr", "form", or custom).

        Returns:
            Header dict for the role, or empty dict if not found.
        """
        return dict(self._gp_header_roles.get(role, {}))

    def clear_header_roles(self) -> None:
        """Remove all captured header roles from this session."""
        self._gp_header_roles.clear()

    def merge_header_roles(self, roles: dict[str, dict[str, str]]) -> None:
        """Merge additional header roles into this session.

        Args:
            roles: Dict mapping role names to header dicts.
        """
        self._gp_header_roles.update(roles)

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

    def _resolve_role(self, role_name: str) -> dict[str, str] | None:
        """Resolve a role name to its header dict (captured or registered).

        Checks captured roles first, then falls back to the role registry.
        Logs when falling back or when the role name is unknown.

        Args:
            role_name: Role name ("navigation", "xhr", "form", or custom).

        Returns:
            Copy of the header dict, or None if role is unknown.
        """
        captured = self._gp_header_roles.get(role_name)
        if captured is not None:
            return dict(captured)

        registered = get_role_headers(role_name)
        if registered is not None:
            LOG.debug(
                "role_not_captured_using_registered",
                detected=role_name,
                available=list(self._gp_header_roles.keys()),
            )
            return registered

        LOG.warning(
            "unknown_role_no_headers_applied",
            role=role_name,
            available_roles=list(self._gp_header_roles.keys()),
            registered_roles=list_roles(),
        )
        return None

    def _role_headers_for(self, role_name: str) -> dict[str, str]:
        """Get request-type headers for a role, excluding identity headers.

        Returns captured role headers if available, falling back to
        registered role headers. Browser identity headers (User-Agent,
        sec-ch-ua, etc.) are excluded -- they're already on self.headers.

        Args:
            role_name: Role name ("navigation", "xhr", "form", or custom).

        Returns:
            Dict of request-type headers, or empty dict if role unknown.
        """
        headers = self._resolve_role(role_name)
        if headers is None:
            return {}

        # Strip identity headers — they're session-level defaults.
        # Case-insensitive filter handles mixed-case CDP headers.
        lower_identity = {h.lower() for h in _BROWSER_IDENTITY_HEADERS}
        return {k: v for k, v in headers.items() if k.lower() not in lower_identity}

    def _apply_browser_identity(self) -> None:
        """Copy browser identity headers from roles onto the session.

        Extracts identity headers (User-Agent, sec-ch-ua, etc.) from the first
        role that contains a User-Agent. All roles originate from the same
        browser, so we only need to check the first role with identity data.
        """
        for role_headers in self._gp_header_roles.values():
            if _case_insensitive_get(role_headers, "User-Agent") is None:
                continue

            for header_name in _BROWSER_IDENTITY_HEADERS:
                value = _case_insensitive_get(role_headers, header_name)
                if value is not None:
                    self.headers[header_name] = value
            return

        if self._gp_header_roles:
            LOG.warning(
                "no_browser_identity_in_roles",
                available=list(self._gp_header_roles.keys()),
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
        """Make a request with XHR role headers.

        Applies captured XHR headers (or registered defaults),
        plus browser identity headers from the session. Caller-supplied
        headers override role headers.

        Args:
            method: HTTP method (GET, POST, PUT, PATCH, DELETE, etc.).
            url: Request URL.
            referer: Referer path ("/page") or full URL. Paths are joined
                with gp_base_url. Omit to send no Referer.
            headers: Additional headers that override role headers.
            **kwargs: Passed through to requests.Session.request().

        Returns:
            The response object.
        """
        return self.request_with_role(
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
        """Make a request with navigation role headers.

        Applies captured navigation headers (or registered defaults),
        plus browser identity headers from the session. Simulates a browser
        page navigation (clicking a link, entering a URL).

        Args:
            method: HTTP method (typically GET).
            url: Request URL.
            referer: Referer path ("/page") or full URL. Paths are joined
                with gp_base_url. Omit to send no Referer.
            headers: Additional headers that override role headers.
            **kwargs: Passed through to requests.Session.request().

        Returns:
            The response object.
        """
        return self.request_with_role(
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
        """Make a request with form submission role headers.

        Applies captured form headers (or registered defaults),
        plus browser identity headers from the session. Simulates a browser
        form submission.

        Args:
            method: HTTP method (typically POST).
            url: Request URL.
            referer: Referer path ("/page") or full URL. Paths are joined
                with gp_base_url. Omit to send no Referer.
            headers: Additional headers that override role headers.
            **kwargs: Passed through to requests.Session.request().

        Returns:
            The response object.
        """
        return self.request_with_role(
            "form", method, url, referer=referer, headers=headers, **kwargs
        )

    def request_with_role(
        self,
        role_name: str,
        method: str,
        url: str,
        *,
        referer: str | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        """Make a request with explicit role headers.

        Applies the named role's headers (captured during login, plugin-
        defined, or registered fallback), then delegates to ``self.request()``.
        Used by ``xhr()``, ``navigate()``, ``form_submit()``, and the
        ``gp http --role`` flag.

        Role names can be any string.  Built-in roles (``"xhr"``,
        ``"navigation"``, ``"form"``) fall back to registered Fetch-spec
        headers when no captured role exists.  Custom roles use headers
        registered via ``register_role()`` or stored in ``_gp_header_roles``.

        Args:
            role_name: Role to apply (e.g. ``"xhr"``, ``"navigation"``,
                ``"form"``, or any custom role name).
            method: HTTP method.
            url: Request URL.
            referer: Optional Referer path or URL.
            headers: Optional caller headers (override role headers).
            **kwargs: Passed through to requests.Session.request().

        Returns:
            The response object.
        """
        role_headers = self._role_headers_for(role_name)

        if referer is not None:
            role_headers["Referer"] = self._resolve_referer(referer)

        # Caller headers override role headers
        if headers:
            role_headers.update(headers)

        # Note: self.request() calls prepare_request(), which runs
        # _detect_role() again and may select a different role.
        # However, our explicit headers are passed as request-level
        # headers, which take precedence over session-level role
        # headers in the requests merge logic (see prepare_request's
        # priority chain).
        return self.request(method.upper(), url, headers=role_headers, **kwargs)

    def _detect_role(self, request: requests.Request) -> str:
        """Auto-detect the appropriate header role for a request.

        Args:
            request: The request about to be sent.

        Returns:
            Role name ("navigation", "xhr", or "form").
        """
        if self.gp_default_role:
            return self.gp_default_role

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

        Uses ``setdefault`` so that caller-supplied headers are never overwritten.

        Args:
            prepared: The prepared request to conditionally add tokens to.
        """
        csrf_tokens: dict[str, str] = getattr(self, _CSRF_TOKENS_ATTR, {})
        if not csrf_tokens:
            return
        if (prepared.method or "GET").upper() not in _MUTATION_METHODS:
            return
        for name, value in csrf_tokens.items():
            prepared.headers.setdefault(name, value)

    def prepare_request(self, request: requests.Request, **kwargs: Any) -> requests.PreparedRequest:
        """Prepare a request with auto-detected role headers.

        Overrides the base implementation to inject header roles based on
        request characteristics. Role headers are applied as session-level
        defaults so that any headers explicitly set by the caller take precedence.

        Session headers are temporarily modified during preparation and restored
        afterward (even on exception) to avoid permanently altering session state.

        CSRF tokens (stored separately by ``tokens.prepare_session``) are injected
        only on mutation methods (POST/PUT/PATCH/DELETE).

        Args:
            request: The request to prepare.
            **kwargs: Additional arguments passed to requests.Session.prepare_request().

        Returns:
            A PreparedRequest with applied role headers (if configured).
        """
        if not self._gp_header_roles:
            prepared = super().prepare_request(request, **kwargs)
        elif role_headers := self._resolve_role(self._detect_role(request)):
            # Apply role headers as session defaults (lowest priority).
            # Priority: request headers (caller-supplied) >
            # user-modified session headers > role > session defaults.
            original_session_headers = dict(self.headers)

            # Start from role headers as the base
            merged = dict(role_headers)
            # Layer session headers on top, but only if user changed them
            for key, value in original_session_headers.items():
                if self._is_user_set_header(key):
                    merged[key] = value
                elif key not in merged:
                    # Keep defaults that role doesn't override
                    merged[key] = value

            self.headers.clear()
            self.headers.update(merged)

            try:
                prepared = super().prepare_request(request, **kwargs)
            finally:
                # Restore original session headers
                self.headers.clear()
                self.headers.update(original_session_headers)
        else:
            prepared = super().prepare_request(request, **kwargs)

        self._inject_csrf_tokens(prepared)
        return prepared
