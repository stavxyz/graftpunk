"""GraftpunkSession — requests.Session subclass with browser header replay."""

# Role Registry
# ==============
# A "role" is a named set of HTTP headers that simulates a specific browser
# request type (navigation, XHR, form submission, or any custom role defined
# by a plugin).  Built-in roles register at import time using the same
# register_role() call that plugins use.

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Final

import requests

from graftpunk.client_headers import client_sets_header
from graftpunk.cookies import BrowserCookiesMixin
from graftpunk.logging import get_logger
from graftpunk.public_suffix import (
    Origin,
    origin,
    registrable_domain,
    same_site,
    serialized_origin,
)
from graftpunk.tokens import _CSRF_ORIGIN_ATTR, _CSRF_TOKENS_ATTR, _HEADER_ROLE_ORIGINS_ATTR

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


def replayable_roles(roles: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """Captured *roles* without any header the HTTP client sets itself
    (:func:`graftpunk.client_headers.client_sets_header`). A role ``gp observe``
    captured before 1.41.0, restored from a cached session, can hold Chrome's
    Priority and Accept-Encoding, and a replayed Accept-Encoding offering zstd gets
    a body the client cannot decode (#370). A plugin's own declared roles never pass
    through here: what a plugin declares is sent."""
    return {
        role: {k: v for k, v in headers.items() if not client_sets_header(k)}
        for role, headers in roles.items()
    }


def apply_plugin_context(
    session: Any, plugin: Any, *, declared_roles_override_captured: bool
) -> None:
    """Put a plugin's ``base_url`` and its declared ``header_roles`` on *session*.

    The one step plugin commands, the Python client and ``gp http`` share. The
    ``base_url`` becomes ``gp_base_url``, the origin the cross-origin rules
    compare with (``GraftpunkSession._resolve_role``). Each declared role is
    merged with ``merge_header_roles``, so it is sent to every origin, adjusted
    for one other than the site's. With *declared_roles_override_captured* (``gp
    http``, as before) a declared role replaces a captured role of the same name;
    without it (commands and the client) a declared role only fills in a name
    the login did not capture, and the captured one keeps precedence, so a
    plugin's same-origin requests do not change. A session that is not a
    ``GraftpunkSession`` gets neither.
    """
    base_url = getattr(plugin, "base_url", "") or ""
    if base_url and hasattr(session, "gp_base_url"):
        session.gp_base_url = base_url
    declared = getattr(plugin, "header_roles", None)
    if not declared or not hasattr(session, "merge_header_roles"):
        return
    captured = getattr(session, "_gp_header_roles", None) or {}
    plugin_name = getattr(plugin, "site_name", "unknown")
    if declared_roles_override_captured:
        for role_name in declared:
            if role_name in captured:
                LOG.debug("plugin_role_overwrites_captured", role=role_name, plugin=plugin_name)
        session.merge_header_roles(dict(declared))
        return
    for role_name in declared:
        if role_name in captured:
            LOG.debug("captured_role_kept_over_plugin_role", role=role_name, plugin=plugin_name)
    session.merge_header_roles(
        {name: headers for name, headers in declared.items() if name not in captured}
    )


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


def _cross_origin_headers(
    headers: dict[str, str], *, fetch_site: str, origin: str, method: str, declared: bool
) -> dict[str, str]:
    """*headers* (a role's) as a browser sends them to another origin.

    ``Sec-Fetch-Site`` becomes *fetch_site* (``same-site`` or ``cross-site``),
    and ``Origin`` is added the way Chrome adds it: on every CORS-mode request
    (``Sec-Fetch-Mode: cors``) and on every method but GET and HEAD. A built-in
    or registered role also loses ``X-Requested-With`` (jQuery, which sets it,
    leaves it off a cross-origin request, where it would need a CORS
    preflight); a role the plugin declared (*declared*) keeps it, since its
    author chose it, and some APIs check it as their CSRF guard.
    """
    adjusted: dict[str, str] = {}
    for name, value in headers.items():
        lower = name.lower()
        if lower == "x-requested-with" and not declared:
            continue
        adjusted[name] = fetch_site if lower == "sec-fetch-site" else value
    cors = _case_insensitive_get(adjusted, "Sec-Fetch-Mode") == "cors"
    sends_origin = cors or method.upper() not in ("GET", "HEAD")
    if sends_origin and _case_insensitive_get(adjusted, "Origin") is None:
        adjusted["Origin"] = origin
    return adjusted


# The attribute a PreparedRequest this session prepared carries, so each
# redirect hop can redo what the session added for the hop's own URL.
_HOP_MARKS_ATTR = "_gp_hop_marks"


@dataclass(frozen=True)
class _HopMarks:
    """What the session added to one prepared request, all names lowercased.

    *role* is the role its headers came from (None when none applied),
    *caller* the header names the caller set (request ``headers=``, a
    ``referer``, or a session header the caller changed), which no hop
    touches, *role_headers* the names the role added, and *csrf* the CSRF
    token names injected. *detected* is the role ``_detect_role`` gave the
    first request (None when the session has no roles), which a hop falls
    back to when *role* gives it nothing, and *method* that request's method.
    """

    role: str | None
    caller: frozenset[str]
    role_headers: frozenset[str]
    csrf: frozenset[str]
    detected: str | None = None
    method: str = "GET"


_LOWER_IDENTITY_HEADERS: Final[frozenset[str]] = frozenset(
    name.lower() for name in _BROWSER_IDENTITY_HEADERS
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


class GraftpunkSession(BrowserCookiesMixin, requests.Session):
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
        # Roles a plugin declared (merge_header_roles), which, unlike captured
        # ones, are sent to every origin the plugin calls.
        self._gp_declared_roles: set[str] = set()
        # (token name, target site) pairs a withheld CSRF token was logged for.
        self._gp_csrf_withheld: set[tuple[str, str]] = set()
        # request_with_role()'s (role, caller header names) for the
        # prepare_request() it is about to cause, per thread (_role_handoff).
        self._gp_role_handoff = threading.local()
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
        """Merge a plugin's declared header roles into this session.

        A declared role is sent to every origin, adjusted for one other than
        ``gp_base_url``'s, where a captured role is sent only to that origin
        (``_resolve_role``).

        Args:
            roles: Dict mapping role names to header dicts.
        """
        self._gp_header_roles.update(roles)
        self._declared_roles().update(roles)

    def _declared_roles(self) -> set[str]:
        """The names ``merge_header_roles()`` merged, set up on first use.

        requests pickles a session through its ``__attrs__`` list, so one
        restored from a pickle has no set until this makes one.
        """
        declared = getattr(self, "_gp_declared_roles", None)
        if declared is None:
            declared = self._gp_declared_roles = set()
        return declared

    def _captured_origin(self, role_name: str) -> Origin | None:
        """The origin *role_name*'s captured headers were recorded on, else
        ``gp_base_url``'s, else None (a session that knows neither)."""
        recorded = (getattr(self, _HEADER_ROLE_ORIGINS_ATTR, None) or {}).get(role_name)
        return origin(recorded or getattr(self, "gp_base_url", "") or "")

    def _cross_origin_site(self, url: str | None) -> tuple[str, str] | None:
        """(``Sec-Fetch-Site``, ``Origin``) for a request to *url* from ``gp_base_url``'s
        origin, or None when the request is same-origin or either names no host.

        Origins and sites are compared as Chrome compares them
        (:mod:`graftpunk.public_suffix`): ``https://api.myshop.example`` and
        ``https://myshop.example:8443`` are same-site to ``https://myshop.example``,
        and ``http://myshop.example`` and ``https://cdn.example.net`` are
        cross-site to it.
        """
        base = origin(getattr(self, "gp_base_url", "") or "")
        target = origin(url)
        if base is None or target is None or base == target:
            return None
        fetch_site = "same-site" if same_site(base, target) else "cross-site"
        return fetch_site, serialized_origin(base)

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

    def _resolve_role(
        self, role_name: str, url: str | None = None, method: str = "GET"
    ) -> dict[str, str] | None:
        """Resolve a role name to its header dict (captured or registered).

        Checks captured roles first, then falls back to the role registry.
        Logs when falling back or when the role name is unknown.

        A captured role is what Chrome sent to one origin at login: the one
        ``_gp_header_role_origins`` records (the login prefers requests to the
        site's own origin, ``graftpunk.observe.headers.extract_header_roles``),
        or ``gp_base_url``'s for a session captured before that was recorded.
        Only a request to that origin gets it as captured (#228); any other
        request, a redirect hop included (``rebuild_auth``), never does. A
        request to another origin than ``gp_base_url``'s (another host, port,
        or scheme) gets a plugin's declared role, or else the registered one,
        as a browser on the site's page would send it there
        (``_cross_origin_headers``): ``Sec-Fetch-Site`` same-site or
        cross-site, an ``Origin`` where Chrome sends one, and no
        ``X-Requested-With`` unless the plugin declared it. A request to
        ``gp_base_url``'s origin whose role was captured on another origin gets
        the registered role as it is. A role that was only captured sends
        nothing where its captured headers do not go.

        Args:
            role_name: Role name ("navigation", "xhr", "form", or custom).
            url: The request's URL, when known.
            method: The request's method, for the ``Origin`` rule.

        Returns:
            Copy of the header dict, or None if role is unknown.
        """
        captured = self._gp_header_roles.get(role_name)
        declared = captured is not None and role_name in self._declared_roles()
        if captured is not None and not declared:
            target = origin(url)
            captured_at = self._captured_origin(role_name)
            if target is None or captured_at is None or target == captured_at:
                return dict(captured)
        cross = self._cross_origin_site(url)
        if cross is None:
            if declared:
                assert captured is not None
                return dict(captured)
            if captured is not None and get_role_headers(role_name) is None:
                LOG.debug("captured_role_not_sent_to_another_origin", role=role_name)
                return {}
        else:
            fetch_site, base_origin = cross
            template = (
                captured
                if captured is not None and role_name in self._declared_roles()
                else get_role_headers(role_name)
            )
            if template is not None:
                return _cross_origin_headers(
                    template,
                    fetch_site=fetch_site,
                    origin=base_origin,
                    method=method,
                    declared=template is captured,
                )
            if captured is not None:
                LOG.debug("captured_role_not_sent_cross_origin", role=role_name)
                return {}

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

    def _role_headers_for(
        self, role_name: str, url: str | None = None, method: str = "GET"
    ) -> dict[str, str]:
        """Get request-type headers for a role, excluding identity headers.

        Returns captured role headers if available, falling back to
        registered role headers, for a request of *method* to *url*
        (``_resolve_role``). Browser identity headers (User-Agent,
        sec-ch-ua, etc.) are excluded -- they're already on self.headers.

        Args:
            role_name: Role name ("navigation", "xhr", "form", or custom).
            url: The request's URL, when known.
            method: The request's method.

        Returns:
            Dict of request-type headers, or empty dict if role unknown.
        """
        headers = self._resolve_role(role_name, url, method)
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
        role_headers = self._role_headers_for(role_name, url, method)

        if referer is not None:
            role_headers["Referer"] = self._resolve_referer(referer)

        # Caller headers override role headers
        if headers:
            role_headers.update(headers)
        caller = frozenset(name.lower() for name in headers or {})
        if referer is not None:
            caller |= {"referer"}

        # Note: self.request() calls prepare_request(), which runs
        # _detect_role() again and may select a different role.
        # However, our explicit headers are passed as request-level
        # headers, which take precedence over session-level role
        # headers in the requests merge logic (see prepare_request's
        # priority chain). prepare_request() reads the role and the
        # caller's names from this thread's hand-off, for the redirect hops.
        handoff = self._role_handoff()
        handoff.explicit = (role_name, caller)
        try:
            return self.request(method.upper(), url, headers=role_headers, **kwargs)
        finally:
            handoff.explicit = None

    def _role_handoff(self) -> threading.local:
        """This thread's slot for request_with_role()'s hand-off to prepare_request().

        Per thread, so one thread's role never reaches another thread's request
        between the hand-off and the prepare that reads it. Made on first use,
        since a session restored from a pickle has none.
        """
        handoff = getattr(self, "_gp_role_handoff", None)
        if handoff is None:
            handoff = self._gp_role_handoff = threading.local()
        return handoff

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

    def _inject_csrf_tokens(
        self, prepared: requests.PreparedRequest, caller: frozenset[str] = frozenset()
    ) -> frozenset[str]:
        """Inject CSRF tokens into a prepared request, mutation methods only; the
        names injected, lowercased.

        CSRF tokens stored in ``_gp_csrf_tokens`` (set by ``tokens.prepare_session``)
        are added to the request headers only for POST, PUT, PATCH, and DELETE.
        This matches browser behavior: CSRF protection is for state-changing
        operations, not read-only GETs.

        A token goes only to the site it was obtained for (the origin
        ``prepare_session`` recorded, else ``gp_base_url``): an ``api.``
        subdomain or another port of that site gets it, another registrable
        domain does not, since a browser never attaches one site's token to
        another site's request on its own. A withheld token is logged at
        warning level once per token name and target site in this session.
        With no recorded origin every request gets the tokens, as before.

        Uses ``setdefault`` so that caller-supplied headers are never
        overwritten; a name in *caller* (a redirect hop's caller headers) is
        left alone too.

        Args:
            prepared: The prepared request to conditionally add tokens to.
            caller: Header names (lowercased) the caller set.
        """
        csrf_tokens: dict[str, str] = getattr(self, _CSRF_TOKENS_ATTR, {})
        if not csrf_tokens:
            return frozenset()
        if (prepared.method or "GET").upper() not in _MUTATION_METHODS:
            return frozenset()
        assert prepared.headers is not None, "PreparedRequest.headers is None after preparation"
        obtained_for = origin(
            getattr(self, _CSRF_ORIGIN_ATTR, "") or getattr(self, "gp_base_url", "") or ""
        )
        target = origin(prepared.url)
        withheld = (
            obtained_for is not None and target is not None and not same_site(obtained_for, target)
        )
        injected: set[str] = set()
        for name, value in csrf_tokens.items():
            if name.lower() in caller or name in prepared.headers:
                continue
            if withheld:
                assert target is not None
                self._warn_withheld_token(name, target[1])
                continue
            prepared.headers[name] = value
            injected.add(name.lower())
        return frozenset(injected)

    def _warn_withheld_token(self, name: str, host: str) -> None:
        """Log, once per token and target site in this session, a CSRF token withheld."""
        seen = getattr(self, "_gp_csrf_withheld", None)
        if seen is None:
            seen = self._gp_csrf_withheld = set()
        key = (name, registrable_domain(host.rstrip(".")))
        if key in seen:
            return
        seen.add(key)
        LOG.warning(
            "csrf_token_withheld_cross_site",
            token=name,
            host=host,
            hint="A CSRF token goes only to the site it was obtained for; pass it in "
            "headers= to send it to another site on purpose.",
        )

    def prepare_request(self, request: requests.Request, **kwargs: Any) -> requests.PreparedRequest:
        """Prepare a request with auto-detected role headers.

        Overrides the base implementation to inject header roles based on
        request characteristics. Role headers are applied as session-level
        defaults so that any headers explicitly set by the caller take precedence.

        Session headers are temporarily modified during preparation and restored
        afterward (even on exception) to avoid permanently altering session state.

        CSRF tokens (stored separately by ``tokens.prepare_session``) are injected
        only on mutation methods (POST/PUT/PATCH/DELETE), and only to the site
        they were obtained for (``_inject_csrf_tokens``).

        The prepared request carries what this added (``_HopMarks``), so that
        ``rebuild_auth`` can redo it for each redirect hop's URL.

        Args:
            request: The request to prepare.
            **kwargs: Passed on to the next prepare_request in the MRO
                (BrowserCookiesMixin's, which, like requests', takes none).

        Returns:
            A PreparedRequest with applied role headers (if configured).
        """
        handoff = self._role_handoff()
        explicit = getattr(handoff, "explicit", None)
        handoff.explicit = None
        request_names = {name.lower() for name in request.headers or {}}
        if explicit is not None:
            role_name: str | None = explicit[0]
            caller = set(explicit[1])
            role_added = request_names - caller
        else:
            role_name = None
            caller = set(request_names)
            role_added = set()
        user_set = {key.lower() for key in self.headers if self._is_user_set_header(key)}
        caller |= user_set

        detected = self._detect_role(request) if self._gp_header_roles else None
        if detected is not None and (
            role_headers := self._resolve_role(detected, request.url, request.method or "GET")
        ):
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
            role_added |= {key.lower() for key in role_headers} - caller - request_names
            if role_name is None:
                role_name = detected
        else:
            prepared = super().prepare_request(request, **kwargs)

        csrf = self._inject_csrf_tokens(prepared, frozenset(caller))
        setattr(
            prepared,
            _HOP_MARKS_ATTR,
            _HopMarks(
                role=role_name,
                caller=frozenset(caller),
                role_headers=frozenset(role_added - _LOWER_IDENTITY_HEADERS),
                csrf=csrf,
                detected=detected,
                method=(request.method or "GET").upper(),
            ),
        )
        return prepared

    def rebuild_auth(
        self, prepared_request: requests.PreparedRequest, response: requests.Response
    ) -> None:
        """requests' per-hop redirect hook, also redoing the role headers and CSRF
        tokens for the hop's URL.

        requests copies each hop from the one before and never calls
        ``prepare_request`` again, so without this a same-origin request that
        redirects to another site would carry the captured role headers and the
        CSRF tokens there. Each hop drops what the session added to the hop
        before (never a header the caller set), applies the role again for its
        own URL and method (``_role_headers_for``), and injects the CSRF tokens
        again by the same-site rule. When the role gives the hop nothing (a role
        only captured, on another origin), the hop gets the role auto-detection
        would give a fresh request to its URL, and a session default the hop
        lost and nothing replaced (``Accept``) comes back. A ``Content-Type`` the
        role holds is not added back to a hop requests made bodiless.
        """
        super().rebuild_auth(prepared_request, response)
        marks = getattr(getattr(response, "request", None), _HOP_MARKS_ATTR, None)
        if not isinstance(marks, _HopMarks):
            return
        headers = prepared_request.headers
        if headers is None:
            return
        removed: set[str] = set()
        for name in list(headers):
            lower = name.lower()
            if lower not in marks.caller and (lower in marks.role_headers or lower in marks.csrf):
                del headers[name]
                removed.add(lower)
        method = prepared_request.method or "GET"
        role = marks.role
        role_headers = self._role_headers_for(role, prepared_request.url, method) if role else {}
        detected = marks.detected
        if not role_headers and self._gp_header_roles:
            detected = self._detect_hop_role(prepared_request, marks)
            role_headers = self._role_headers_for(detected, prepared_request.url, method)
        added: set[str] = set()
        for name, value in role_headers.items():
            lower = name.lower()
            if lower in marks.caller or lower in _LOWER_IDENTITY_HEADERS:
                continue
            if lower == "content-type" and prepared_request.body is None:
                continue
            headers[name] = value
            added.add(lower)
        for name, value in self.headers.items():
            lower = name.lower()
            if lower in removed and lower not in added and lower != "content-type":
                headers[name] = value
        csrf = self._inject_csrf_tokens(prepared_request, marks.caller)
        setattr(
            prepared_request,
            _HOP_MARKS_ATTR,
            _HopMarks(role, marks.caller, frozenset(added), csrf, detected, method.upper()),
        )

    def _detect_hop_role(self, hop: requests.PreparedRequest, marks: _HopMarks) -> str:
        """The role ``_detect_role`` would give a fresh request like redirect *hop*:
        the first request's when the method is the same, else one read from the
        hop's method and the headers the caller set (a 303 made it a GET)."""
        method = (hop.method or "GET").upper()
        if marks.detected is not None and marks.method == method:
            return marks.detected
        caller_headers = {
            name: value
            for name, value in (hop.headers or {}).items()
            if name.lower() in marks.caller
        }
        return self._detect_role(
            requests.Request(method=method, url=hop.url, headers=caller_headers)
        )
