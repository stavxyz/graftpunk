"""Header role classification and extraction from CDP request data."""

from __future__ import annotations

from typing import Any

from graftpunk.client_headers import client_sets_header
from graftpunk.public_suffix import Origin, origin, same_site, serialized_origin

# Headers excluded from roles: request-specific (cookie, referer, origin,
# content-type) or ephemeral security tokens (x-csrf-token). The ones the HTTP
# client or the transport supplies (host, connection, accept-encoding,
# content-length, priority, and every HTTP/2 pseudo-header) are
# graftpunk.client_headers.client_sets_header's, checked beside this set.
EXCLUDED_HEADERS: frozenset[str] = frozenset(
    {
        "cookie",
        "content-type",
        "referer",
        "origin",
        "x-csrf-token",
    }
)


def kept_in_role(name: str) -> bool:
    """Whether a captured request header *name* belongs in a header role: neither
    in :data:`EXCLUDED_HEADERS` nor one the client sets itself, any case."""
    return name.lower() not in EXCLUDED_HEADERS and not client_sets_header(name)


def classify_request(headers: dict[str, str]) -> str | None:
    """Classify a request into a header role based on its headers.

    Classification priority:
    1. sec-fetch-mode (most reliable — Chrome always sends it)
    2. Content-Type (for form detection)
    3. X-Requested-With / Accept heuristics (for XHR detection)

    Args:
        headers: Request headers dict (case-insensitive keys expected from CDP).

    Returns:
        Role name ("navigation", "xhr", or "form"), or None if not classifiable.
    """
    # Normalize header keys to lowercase for comparison
    lower = {k.lower(): v for k, v in headers.items()}

    # 1. sec-fetch-mode (highest priority)
    sec_fetch = lower.get("sec-fetch-mode", "")
    if sec_fetch == "navigate":
        return "navigation"
    if sec_fetch == "cors":
        return "xhr"

    # 2. Content-Type for form detection
    content_type = lower.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        return "form"

    # 3. XHR heuristics
    if lower.get("x-requested-with", "").lower() == "xmlhttprequest":
        return "xhr"
    accept = lower.get("accept", "")
    if "application/json" in accept:
        return "xhr"

    # 4. Navigation heuristic (Accept contains text/html without sec-fetch-mode)
    if "text/html" in accept:
        return "navigation"

    return None


def _rank(sent_to: Origin | None, site: Origin | None) -> int:
    """How well a request sent to *sent_to* stands for the site at *site*: 0 for
    its origin, 1 for another origin of the same site, 2 for anything else."""
    if site is None or sent_to == site:
        return 0
    if sent_to is not None and same_site(sent_to, site):
        return 1
    return 2


def _role_requests(
    request_map: dict[str, dict[str, Any]], site_url: str | None
) -> dict[str, dict[str, Any]]:
    """The request each role is taken from: per role, the first request in
    *request_map* order sent to *site_url*'s origin, else the first to another
    origin of its site, else the first of all. Without a *site_url*, the first."""
    site = origin(site_url) if site_url else None
    chosen: dict[str, tuple[int, dict[str, Any]]] = {}
    for data in request_map.values():
        headers = data.get("headers", {})
        if not headers:
            continue
        role = classify_request(headers)
        if role is None:
            continue
        rank = _rank(origin(data.get("url")), site)
        if role not in chosen or rank < chosen[role][0]:
            chosen[role] = (rank, data)
    return {role: data for role, (_rank_, data) in chosen.items()}


def extract_header_roles(
    request_map: dict[str, dict[str, Any]], site_url: str | None = None
) -> dict[str, dict[str, str]]:
    """Extract header roles from a capture backend's request map.

    Classifies each captured request and stores the full header set (minus
    excluded headers) of one request per role: the first one sent to
    *site_url*'s origin (the plugin's ``base_url``), else to another origin of
    that site, else the first of all (#228). A login's first CORS request is
    often an identity provider's or a tracker's, and its headers are not the
    ones the site's own pages send.

    Args:
        request_map: The capture backend's _request_map dict. Each entry
            has at least a "headers" key with the request header dict, and
            a "url" for the origin preference.
        site_url: The site's URL, or None to take each role's first request.

    Returns:
        Dict mapping role name to header dict, e.g.
        {"navigation": {"User-Agent": "...", ...}, "xhr": {...}}.
    """
    return {
        role: {k: v for k, v in data["headers"].items() if kept_in_role(k)}
        for role, data in _role_requests(request_map, site_url).items()
    }


def header_role_origins(
    request_map: dict[str, dict[str, Any]], site_url: str | None = None
) -> dict[str, str]:
    """The origin (``https://host[:port]``) each role :func:`extract_header_roles`
    returns was captured from; a role whose request has no readable URL is left
    out. ``GraftpunkSession`` sends a captured role as captured to that origin
    only."""
    origins: dict[str, str] = {}
    for role, data in _role_requests(request_map, site_url).items():
        sent_to = origin(data.get("url"))
        if sent_to is not None:
            origins[role] = serialized_origin(sent_to)
    return origins
