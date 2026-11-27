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


def classify_request(headers: dict[str, str], method: str | None = None) -> str | None:
    """Classify a request into a header role based on its headers.

    Classification priority:
    1. ``Sec-Fetch-Mode: cors`` is an XHR.
    2. A form Content-Type on any other mode is a form: a browser submits a form
       as a navigation (``Sec-Fetch-Mode: navigate``), so this comes before
       the navigation rule. With *method* given, only a POST can be a form.
    3. ``Sec-Fetch-Mode: navigate`` is a navigation.
    4. X-Requested-With / Accept heuristics (for XHR detection), then an Accept
       of ``text/html`` for a navigation.

    Args:
        headers: Request headers dict (case-insensitive keys expected from CDP).
        method: The request's method, when known.

    Returns:
        Role name ("navigation", "xhr", or "form"), or None if not classifiable.
    """
    # Normalize header keys to lowercase for comparison
    lower = {k.lower(): v for k, v in headers.items()}

    sec_fetch = lower.get("sec-fetch-mode", "")
    if sec_fetch == "cors":
        return "xhr"

    content_type = lower.get("content-type", "")
    form_body = (
        "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type
    )
    if form_body and (method is None or method.upper() == "POST"):
        return "form"

    if sec_fetch == "navigate":
        return "navigation"

    # XHR heuristics
    if lower.get("x-requested-with", "").lower() == "xmlhttprequest":
        return "xhr"
    accept = lower.get("accept", "")
    if "application/json" in accept:
        return "xhr"

    # Navigation heuristic (Accept contains text/html without sec-fetch-mode)
    if "text/html" in accept:
        return "navigation"

    return None


def sent_headers(data: dict[str, Any]) -> dict[str, str]:
    """The request headers of a capture backend's request-map entry *data* as
    Chrome sent them.

    ``Network.requestWillBeSent`` reports the renderer's headers (``headers``),
    which lack what the network stack adds (Accept, Accept-Language, every
    Sec-Fetch-* header on a navigation) and, on the GET after a form POST's
    redirect, still hold the POST's Content-Type and Origin.
    ``requestWillBeSentExtraInfo`` reports the headers sent, kept as
    ``_request_extra_headers``; when an entry has them they are the set, so no
    stale renderer header is kept, and each name takes the renderer's spelling
    where the renderer has the same name in another case (HTTP/2 sends every
    name in lower case). Without them (CDP does not promise them for every
    request, and nodriver 0.48.1 was measured to drop most), the renderer's
    headers are used.
    """
    renderer = data.get("headers") or {}
    wire = data.get("_request_extra_headers") or {}
    if not wire:
        return dict(renderer)
    spelling = {name.lower(): name for name in renderer}
    return {spelling.get(name.lower(), name): value for name, value in wire.items()}


def _rank(sent_to: Origin | None, site: Origin | None) -> int:
    """How well a request sent to *sent_to* stands for the site at *site*: 0 for
    its origin, 1 for another origin of the same site, 2 for anything else."""
    if site is None or sent_to == site:
        return 0
    if sent_to is not None and same_site(sent_to, site):
        return 1
    return 2


def _header(headers: dict[str, str], name: str) -> str | None:
    return next((v for k, v in headers.items() if k.lower() == name), None)


def _navigation_key(headers: dict[str, str], rank: int) -> tuple[bool, int, bool]:
    """Sort key of a navigation request, best first: a top-level document load
    before a frame's, then the origin rank, then a load the site sent before
    one the login opened as a typed address (``Sec-Fetch-Site: none``, which
    Chrome 158 sent for nodriver's ``Tab.get``). No ``Sec-Fetch-Dest`` (no sent
    headers, or a site Chrome sends no ``Sec-Fetch-*`` to) counts as a document."""
    dest = _header(headers, "sec-fetch-dest")
    framed = dest is not None and dest != "document"
    return (framed, rank, _header(headers, "sec-fetch-site") == "none")


def _role_requests(
    request_map: dict[str, dict[str, Any]], site_url: str | None
) -> dict[str, dict[str, Any]]:
    """The request each role is taken from: per role, the first request in
    *request_map* order sent to *site_url*'s origin, else the first to another
    origin of its site, else the first of all. Without a *site_url*, the first.
    The navigation role is chosen by :func:`_navigation_key`."""
    site = origin(site_url) if site_url else None
    chosen: dict[str, tuple[tuple[bool, int, bool], dict[str, Any]]] = {}
    for data in request_map.values():
        headers = sent_headers(data)
        if not headers:
            continue
        role = classify_request(headers, data.get("method"))
        if role is None:
            continue
        rank = _rank(origin(data.get("url")), site)
        key = _navigation_key(headers, rank) if role == "navigation" else (False, rank, False)
        if role not in chosen or key < chosen[role][0]:
            chosen[role] = (key, data)
    return {role: data for role, (_key, data) in chosen.items()}


def _role_headers(role: str, data: dict[str, Any]) -> dict[str, str]:
    """The headers *role* keeps of the request-map entry *data*."""
    headers = {k: v for k, v in sent_headers(data).items() if kept_in_role(k)}
    if role == "navigation":
        # The role goes only to the origin it was captured from, where a page
        # load is same-origin: "none" would say the user typed every command's
        # address, and a captured same-site or cross-site (an SSO callback)
        # names the page the login came from. 1.46.0 sent the registered
        # role's same-origin.
        headers = {
            k: "same-origin" if k.lower() == "sec-fetch-site" else v for k, v in headers.items()
        }
    return headers


def extract_header_roles(
    request_map: dict[str, dict[str, Any]], site_url: str | None = None
) -> dict[str, dict[str, str]]:
    """Extract header roles from a capture backend's request map.

    Classifies each captured request and stores the header set Chrome sent
    (:func:`sent_headers`, minus excluded headers) of one request per role:
    the first one sent to
    *site_url*'s origin (the plugin's ``base_url``), else to another origin of
    that site, else the first of all (#228). A login's first CORS request is
    often an identity provider's or a tracker's, and its headers are not the
    ones the site's own pages send. The navigation role prefers a top-level
    document to a frame's load and, after the origin order, a page load the site
    sent to one the login typed (``Sec-Fetch-Site: none``); its
    ``Sec-Fetch-Site`` is always stored as ``same-origin``.

    Args:
        request_map: The capture backend's _request_map dict. Each entry
            has a "headers" key with the renderer's request headers, a
            "_request_extra_headers" key with the headers sent when Chrome
            reported them, a "method", and a "url" for the origin preference.
        site_url: The site's URL, or None to take each role's first request.

    Returns:
        Dict mapping role name to header dict, e.g.
        {"navigation": {"User-Agent": "...", ...}, "xhr": {...}}.
    """
    return {
        role: _role_headers(role, data)
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
