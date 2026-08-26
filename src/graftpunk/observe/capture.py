"""Backend-specific browser capture implementations."""

from __future__ import annotations

import base64
import contextlib
import datetime
import inspect
import json
import math
import os
import tempfile
import time
import urllib.parse
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from graftpunk.logging import get_logger

try:
    from selenium.common.exceptions import WebDriverException as _WebDriverError

    _HAS_SELENIUM = True
except ImportError:  # selenium is an optional extra
    _HAS_SELENIUM = False

    class _WebDriverError(Exception):
        """Stand-in so `except _WebDriverError` stays valid without selenium."""


LOG = get_logger(__name__)

# Body size cap
MAX_RESPONSE_BODY_SIZE = 5 * 1024 * 1024  # 5MB

# MIME type constants for body handling
BINARY_MIME_PREFIXES = (
    "image/",
    "audio/",
    "video/",
    "application/pdf",
    "application/zip",
    "application/gzip",
    "application/octet-stream",
    "application/vnd.openxmlformats-",
    "application/vnd.ms-",
)

TEXT_MIME_KEYWORDS = ("json", "html", "text", "xml", "javascript", "css")


def _is_binary_mime(mime: str) -> bool:
    """Check if MIME type represents a binary/file format."""
    mime_lower = mime.lower()
    return any(mime_lower.startswith(prefix) for prefix in BINARY_MIME_PREFIXES)


def _is_text_mime(mime: str) -> bool:
    """Check if MIME type represents a text format worth capturing body for."""
    mime_lower = mime.lower()
    return any(kw in mime_lower for kw in TEXT_MIME_KEYWORDS)


def _should_stream_to_disk(mime: str, body_size: int, max_body_size: int) -> bool:
    """Determine if a response body should be streamed to disk."""
    return _is_binary_mime(mime) or (_is_text_mime(mime) and body_size > max_body_size)


def _mime_to_extension(mime: str) -> str:
    """Map MIME type to file extension for disk-streamed bodies."""
    mime_lower = mime.lower().split(";")[0].strip()
    mapping = {
        "application/json": ".json",
        "text/html": ".html",
        "text/xml": ".xml",
        "application/xml": ".xml",
        "text/plain": ".txt",
        "text/css": ".css",
        "application/javascript": ".js",
        "text/javascript": ".js",
        "application/pdf": ".pdf",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/svg+xml": ".svg",
    }
    return mapping.get(mime_lower, ".bin")


def _process_response_body(
    response: dict[str, Any],
    body: str,
    base64_encoded: bool,
    request_id: str,
    mime: str,
    max_body_size: int,
    bodies_dir: Path | None,
) -> None:
    """Decode, size-check, and store a response body (in-memory or on disk).

    Modifies *response* dict in-place to add ``body`` text or ``_bodyFile`` reference,
    and updates ``bodySize``.
    """
    if base64_encoded:
        body_bytes = base64.b64decode(body)
    else:
        body_bytes = body.encode("utf-8") if isinstance(body, str) else body

    body_size = len(body_bytes) if body_bytes else 0
    response["bodySize"] = body_size

    if bodies_dir and _should_stream_to_disk(mime, body_size, max_body_size):
        bodies_dir.mkdir(parents=True, exist_ok=True)
        ext = _mime_to_extension(mime)
        body_filename = f"{request_id}{ext}"
        body_path = bodies_dir / body_filename
        if isinstance(body_bytes, bytes):
            body_path.write_bytes(body_bytes)
        else:
            body_path.write_text(body_bytes, encoding="utf-8")
        response["_bodyFile"] = f"bodies/{body_filename}"
    elif _is_text_mime(mime) and body_size <= max_body_size:
        if isinstance(body, str) and not base64_encoded:
            response["body"] = body
        else:
            response["body"] = (
                body_bytes.decode("utf-8", errors="replace")
                if isinstance(body_bytes, bytes)
                else str(body_bytes)
            )


def _wall_time_to_iso(wall_time: float | None) -> str:
    """Convert CDP wall_time (unix epoch float) to ISO 8601 string."""
    if wall_time is None:
        return datetime.datetime.now(datetime.UTC).isoformat()
    return datetime.datetime.fromtimestamp(wall_time, tz=datetime.UTC).isoformat()


def _response_summary(
    status: int,
    status_text: str | None,
    headers: Mapping[str, Any] | None,
    mime_type: str | None,
) -> dict[str, Any]:
    """The response fields kept per request, in one shape for both backends."""
    return {
        "status": status,
        "statusText": status_text or "",
        "headers": dict(headers) if headers else {},
        "mimeType": mime_type or "",
    }


def _ordered_request_items(
    request_map: dict[str, dict[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    """``request_map`` items in chronological order.

    A finalized redirect hop is re-inserted at the end of the map, so plain
    insertion order would list it after requests that started later. Sort by
    wall time (stable, so ties keep insertion order). Entries without a wall
    time sort last: ``_build_har_entry`` renders their ``startedDateTime`` as
    "now", so last is the only order consistent with what they say.
    """
    return sorted(
        request_map.items(),
        key=lambda item: (
            item[1].get("timestamp") is None,
            item[1].get("timestamp") or 0.0,
        ),
    )


def _header_roles_in_order(request_map: dict[str, dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Header roles extracted in the same chronological order the HAR uses."""
    from graftpunk.observe.headers import extract_header_roles

    return extract_header_roles(dict(_ordered_request_items(request_map)))


def _har_entries_in_order(request_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """HAR entries in chronological order (see ``_ordered_request_items``)."""
    return [_build_har_entry(data) for _rid, data in _ordered_request_items(request_map)]


def _is_redirect_hop(data: dict[str, Any]) -> bool:
    """True if ``data`` is a finalized redirect hop; body fetching must skip it.

    A 3xx has no response body, and the synthetic key is not a CDP id.
    """
    return bool(data.get("_redirect_hop"))


def _note_lost_hop_body(request_id: str, data: dict[str, Any]) -> None:
    """Warn once if a redirect hop's POST body was never captured.

    A hop's POST body that CDP did not inline is not retrievable by request id
    after the redirect (the id now addresses the final hop), so say so rather
    than drop it silently. One-shot per entry, so a second drain of the same
    map cannot repeat it.
    """
    if data.get("has_post_data") and not data.get("post_data") and not data.get("_hop_warned"):
        data["_hop_warned"] = True
        LOG.warning(
            "redirect_hop_post_data_unavailable",
            request_id=request_id,
            url=data.get("url"),
            hint="Request body was not inlined by CDP and is lost across the redirect",
        )


def _finalize_redirect_hop(
    request_map: dict[str, dict[str, Any]],
    request_id: str,
    redirect_response: dict[str, Any],
    next_url: str,
) -> str | None:
    """Move the in-flight request for ``request_id`` aside as a completed redirect hop.

    CDP reuses ``requestId`` across a redirect chain: each hop arrives as a
    new ``Network.requestWillBeSent`` carrying ``redirectResponse`` for the
    request it replaces. Overwriting the map entry dropped every hop but the
    last -- for a server-rendered login, the ``POST /login`` itself (issue
    #153). HAR 1.2 wants one entry per hop, linked by ``response.redirectURL``.

    The finished hop is re-keyed as ``"<id>:redirect:<n>"`` (a synthetic key,
    not a CDP request id) and flagged ``_redirect_hop`` so body fetches skip
    it: a 3xx has no body worth fetching, and CDP would reject the key anyway.
    The caller then records the announced request under ``request_id``. That
    order -- hop re-inserted first, successor written second -- is what keeps
    a hop ahead of its successor when their wall times tie, since
    ``_ordered_request_items`` relies on a stable sort.

    Returns the hop's new key, or None if there was nothing to finalize.
    """
    previous = request_map.pop(request_id, None)
    if previous is None:
        return None  # capture started mid-chain; nothing to finalize
    hop_index = 1 + sum(1 for key in request_map if key.startswith(f"{request_id}:redirect:"))
    previous["response"] = {**redirect_response, "redirectURL": next_url}
    previous["_redirect_hop"] = True
    hop_key = f"{request_id}:redirect:{hop_index}"
    request_map[hop_key] = previous
    return hop_key


def _console_timestamp(raw: Any) -> float:
    """Normalise a ``Runtime.consoleAPICalled`` timestamp to seconds since epoch.

    CDP's ``Runtime.Timestamp`` is *milliseconds* since epoch and Selenium's
    browser-log entries carry a millisecond epoch as well, while the fallback
    when an event carries none is ``time.time()`` (seconds). Storing them as-is
    mixed units in ``console.jsonl`` (issue #158). Rather than trusting each
    producer's unit, treat any value above ``1e11`` as milliseconds (``1e11``
    seconds is the year 5138; ``1e11`` milliseconds is 1973), so both producers
    and any already-normalised value land in seconds. Missing, zero or
    non-numeric values fall back to now rather than aborting the capture.
    """
    try:
        value = float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        return time.time()
    if not value or not math.isfinite(value):
        return time.time()
    return value / 1000.0 if value > 1e11 else value


def _merge_headers_ci(
    base: Mapping[str, Any] | None, override: Mapping[str, Any] | None
) -> dict[str, Any]:
    """``base`` updated by ``override``, matching header names case-insensitively.

    ``responseReceived`` reports canonical casing (``Content-Type``) while the
    ExtraInfo events report wire casing (``content-type`` on HTTP/2); a plain
    dict merge kept both and the "raw wins" rule silently failed.
    """
    folded: dict[str, str] = {}  # lowercase name -> raw name as first seen in override
    override_ci: dict[str, Any] = {}
    for name, value in (override or {}).items():
        raw = folded.setdefault(name.lower(), name)
        # Two override names differing only in case: join like Chromium joins
        # repeated headers, so nothing is dropped and no duplicate row appears.
        if raw in override_ci:
            joiner = "; " if raw.lower() == "cookie" else "\n"
            override_ci[raw] = f"{override_ci[raw]}{joiner}{value}"
        else:
            override_ci[raw] = value
    merged = {k: v for k, v in (base or {}).items() if k.lower() not in folded}
    merged.update(override_ci)
    return merged


def _header_values(headers: Mapping[str, str], name: str) -> list[str]:
    """Every value of header ``name`` (case-insensitive), in order."""
    wanted = name.lower()
    return [v for k, v in headers.items() if k.lower() == wanted and v]


def _normalized_netloc(netloc: str, scheme: str) -> str:
    """Host (lowercased) plus port, with userinfo and the scheme's default port dropped."""
    host = netloc.rsplit("@", 1)[-1].lower()
    default = {"http": "80", "https": "443"}.get(scheme.lower())
    if ":" in host and not host.endswith("]"):
        name, port = host.rsplit(":", 1)
        if port == default:
            return name
    return host


def _same_url(a: str | None, b: str | None, base: str | None = None) -> bool:
    """Compare two URLs (relative ones resolved against ``base``) ignoring host
    case, default ports, userinfo and a trailing slash on the path."""
    if not a or not b:
        return False
    if base:
        a, b = urllib.parse.urljoin(base, a), urllib.parse.urljoin(base, b)
    pa, pb = urllib.parse.urlsplit(a), urllib.parse.urlsplit(b)
    return (
        pa.scheme.lower() == pb.scheme.lower()
        and _normalized_netloc(pa.netloc, pa.scheme) == _normalized_netloc(pb.netloc, pb.scheme)
        and (pa.path.rstrip("/") or "/") == (pb.path.rstrip("/") or "/")
        and pa.query == pb.query
    )


def _request_headers_match(headers: Mapping[str, str], entry: dict[str, Any]) -> bool | None:
    """Does a request ExtraInfo belong to ``entry``? None when undeterminable.

    HTTP/2 wire headers carry ``:method``/``:authority``/``:path``; HTTP/1.1
    carries only ``Host``. Compare whatever is present against the entry's URL
    and method; with nothing to compare, return None. An entry synthesized from
    a response alone (``_from_response_only``) has a guessed method and the
    final URL, so nothing about it can be compared: also None.
    """
    if entry.get("_from_response_only"):
        return None
    lower = {k.lower(): v for k, v in headers.items()}
    parsed = urllib.parse.urlsplit(entry.get("url", ""))
    checks: list[bool] = []
    if ":method" in lower:
        checks.append(lower[":method"].upper() == str(entry.get("method", "GET")).upper())
    if ":path" in lower:
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        checks.append(lower[":path"] == (path or "/"))
    authority = lower.get(":authority") or lower.get("host")
    if authority:
        checks.append(
            _normalized_netloc(authority, parsed.scheme)
            == _normalized_netloc(parsed.netloc, parsed.scheme)
        )
    if not checks:
        return None
    return all(checks)


class _ExtraInfoCorrelator:
    """Attach ``Network.*ExtraInfo`` headers to the right request entry.

    CDP guarantees neither that ``requestWillBeSentExtraInfo`` follows
    ``requestWillBeSent`` nor that ``responseReceivedExtraInfo`` precedes
    ``responseReceived`` (both dataclass docstrings say so); across a redirect
    chain every hop shares one request id, and ``redirectHasExtraInfo`` on the
    continuation says whether the hop it replaces has ExtraInfo of both kinds.
    Measured on a real redirecting login with nodriver 0.48.1 and 0.50.3 the
    order was always ``requestWillBeSent -> requestWillBeSentExtraInfo ->
    responseReceivedExtraInfo(3xx) -> requestWillBeSent(continuation)``, and
    0.48.1 was measured to drop most request-side ExtraInfo events. Nothing
    here depends on order; events are matched, buffered, or reported:

    - A **response** ExtraInfo attaches to the entry for its id whose known
      response status equals the event's ``statusCode`` (and whose redirect
      ``Location`` matches the event's, when both are present, so same-status
      chains stay apart) and which has not yet received its own. If no entry
      has a matching status yet -- the live request has not been answered, or
      a continuation has not been announced -- the event is buffered and
      re-resolved whenever a status becomes known. Without a ``statusCode``
      (Selenium perf logs may omit it) it goes to the first entry lacking its own.
    - A **request** ExtraInfo attaches to the entry for its id whose URL and
      method match the wire headers (``:method``/``:path``/``:authority`` on
      HTTP/2, ``Host`` on HTTP/1.1) and which has not received its own. When
      the headers cannot discriminate (HTTP/1.1, same host), the live request
      is preferred over a hop still waiting: a hop's event was measured to
      arrive before the continuation, so an undeterminable event after it is
      far more likely the successor's than a late one for the hop. If nothing
      fits (every candidate already has its own: the successor is not yet
      announced), the event is buffered and applied when the next entry for
      the id is created. This is the residual ambiguity: on HTTP/1.1, a hop's
      request ExtraInfo arriving late swaps with the successor's (each lands on
      the other) -- an order never observed. Likewise a redirect loop (two hops
      with the same status *and* ``Location``) with one dropped event cannot
      be told apart.
    - At drain, anything still buffered whose id has exactly one entry lacking
      that kind is attached as a last resort (counted as ``fallback``), so a
      guard false negative degrades to a plausible attachment rather than a
      silently missing ``Set-Cookie``; the rest is counted in
      ``extra_info_unmatched`` together with redirect hops that never received
      theirs.

    Raw headers are kept beside the entry (``_request_extra_headers`` /
    ``_response_extra_headers``) and merged only when the HAR is built, so
    header-role extraction keeps seeing the renderer headers it was designed
    for. ``_request_extra_seen`` / ``_response_extra_seen`` mark an entry that
    has been given an event (directly or from the buffer); an entry takes at
    most one of each kind.
    """

    def __init__(self) -> None:
        self._pending_request: dict[str, list[dict[str, Any]]] = {}
        self._pending_response: dict[str, list[tuple[dict[str, Any], int | None]]] = {}
        self._hops: dict[str, list[str]] = {}  # request id -> finalized hop keys, in order

    def _chain(
        self, request_map: dict[str, dict[str, Any]], request_id: str
    ) -> list[dict[str, Any]]:
        """Every entry for a CDP request id, redirect hops first, then the live one."""
        entries = [request_map[k] for k in self._hops.get(request_id, []) if k in request_map]
        live = request_map.get(request_id)
        return entries + ([live] if live is not None else [])

    @staticmethod
    def _attach(entry: dict[str, Any], kind: str, headers: Mapping[str, Any]) -> None:
        entry[f"_{kind}_extra_headers"] = _merge_headers_ci(
            entry.get(f"_{kind}_extra_headers"), headers
        )
        entry[f"_{kind}_extra_seen"] = True

    # -- resolution points -------------------------------------------------

    def hop_finalized(
        self,
        request_map: dict[str, dict[str, Any]],
        request_id: str,
        hop_key: str | None,
        redirect_has_extra_info: bool,
    ) -> None:
        """Record the hop for ``request_id``. ``redirectHasExtraInfo`` false means
        no ExtraInfo of either kind will ever come for it, so it must not absorb
        a later status-less event."""
        hop = request_map.get(hop_key) if hop_key else None
        if hop is None or hop_key is None:
            return
        self._hops.setdefault(request_id, []).append(hop_key)
        if not redirect_has_extra_info:
            hop["_request_extra_seen"] = True
            hop["_response_extra_seen"] = True

    def entry_created(self, request_map: dict[str, dict[str, Any]], request_id: str) -> None:
        """A new entry exists for ``request_id``: give it buffered request headers."""
        for headers in self._pending_request.pop(request_id, []):
            self.request_extra(request_map, request_id, headers)

    def status_known(self, request_map: dict[str, dict[str, Any]], request_id: str) -> None:
        """A response status became known for ``request_id`` (a hop was finalized
        or the live request was answered): retry buffered response headers."""
        for headers, status in self._pending_response.pop(request_id, []):
            self.response_extra(request_map, request_id, headers, status)

    # -- events -------------------------------------------------------------

    def request_extra(
        self,
        request_map: dict[str, dict[str, Any]],
        request_id: str,
        headers: Mapping[str, Any] | None,
    ) -> None:
        headers = dict(headers or {})
        candidates = [
            e for e in self._chain(request_map, request_id) if not e.get("_request_extra_seen")
        ]
        if not candidates:
            self._pending_request.setdefault(request_id, []).append(headers)
            return
        verdicts = [_request_headers_match(headers, e) for e in candidates]
        matched = [e for e, ok in zip(candidates, verdicts, strict=True) if ok]
        if matched:
            # Several equal matches (HTTP/1.1, same host) are undeterminable too:
            # candidates are in chain order, so the last match is the live request.
            self._attach(matched[-1], "request", headers)
        elif all(v is None for v in verdicts):
            self._attach(candidates[-1], "request", headers)  # prefer the live request
        else:
            self._pending_request.setdefault(request_id, []).append(headers)  # none of these

    def response_extra(
        self,
        request_map: dict[str, dict[str, Any]],
        request_id: str,
        headers: Mapping[str, Any] | None,
        status_code: int | None = None,
    ) -> None:
        headers = dict(headers or {})
        candidates = [
            e for e in self._chain(request_map, request_id) if not e.get("_response_extra_seen")
        ]
        if status_code is None:
            if candidates:
                self._attach(candidates[0], "response", headers)
            else:
                self._pending_response.setdefault(request_id, []).append((headers, None))
            return
        location = next((v for k, v in headers.items() if k.lower() == "location"), None)
        for entry in candidates:
            response = entry.get("response") or {}
            if response.get("status") != status_code:
                continue
            hop_location = next(
                (v for k, v in response.get("headers", {}).items() if k.lower() == "location"), None
            )
            if (
                location
                and hop_location
                and not _same_url(location, hop_location, entry.get("url"))
            ):
                continue
            self._attach(entry, "response", headers)
            return
        self._pending_response.setdefault(request_id, []).append((headers, status_code))

    def _fallback(self, request_map: dict[str, dict[str, Any]], kind: str) -> int:
        """Last resort at drain: a buffered event whose id has exactly one entry
        still lacking that kind is attached there. Returns how many were."""
        pending: dict[str, list[Any]] = (
            self._pending_request if kind == "request" else self._pending_response
        )
        attached = 0
        for request_id in list(pending):
            lacking = [
                e for e in self._chain(request_map, request_id) if not e.get(f"_{kind}_extra_seen")
            ]
            if len(lacking) == 1 and len(pending[request_id]) == 1:
                item = pending.pop(request_id)[0]
                headers = item[0] if kind == "response" else item
                self._attach(lacking[0], kind, headers)
                attached += 1
        return attached

    def log_unmatched(self, request_map: dict[str, dict[str, Any]]) -> None:
        """At drain time, attach what can still be attached as a last resort, then
        report ExtraInfo that never found its entry and redirect hops that never
        received theirs (a dropped event; measured on nodriver 0.48.1 for the
        request side)."""
        fallback = self._fallback(request_map, "request") + self._fallback(request_map, "response")
        req = sum(len(v) for v in self._pending_request.values())
        resp = sum(len(v) for v in self._pending_response.values())
        hops = [e for e in request_map.values() if e.get("_redirect_hop")]
        hops_no_req = sum(1 for e in hops if not e.get("_request_extra_seen"))
        hops_no_resp = sum(1 for e in hops if not e.get("_response_extra_seen"))
        if fallback or req or resp or hops_no_req or hops_no_resp:
            LOG.debug(
                "extra_info_unmatched",
                fallback=fallback,
                request_events=req,
                response_events=resp,
                hops_without_request_extra=hops_no_req,
                hops_without_response_extra=hops_no_resp,
                hint="Raw wire headers (e.g. Set-Cookie) for these are not in the HAR",
            )


def _parse_cookie_pairs(fragments: list[str]) -> list[dict[str, str]]:
    """``name=value`` pairs from cookie fragments.

    Fragments without ``=`` or with an empty name are skipped (RFC 6265 lets a
    server send a nameless ``=value``; HAR has no representation for it).
    """
    cookies: list[dict[str, str]] = []
    for fragment in fragments:
        name, sep, value = fragment.strip().partition("=")
        if sep and name:
            cookies.append({"name": name.strip(), "value": value.strip()})
    return cookies


def _request_cookies(headers: Mapping[str, str]) -> list[dict[str, str]]:
    """HAR ``request.cookies`` from every raw ``Cookie`` header value."""
    fragments = [
        f
        for value in _header_values(headers, "cookie")
        for line in value.split("\n")
        for f in line.split(";")
    ]
    return _parse_cookie_pairs(fragments)


_COOKIE_FLAG_ATTRS = {"httponly": "httpOnly", "secure": "secure"}
_COOKIE_VALUE_ATTRS = {"path": "path", "domain": "domain", "expires": "expires"}


def _response_cookies(headers: Mapping[str, str]) -> list[dict[str, Any]]:
    """HAR ``response.cookies`` from every ``Set-Cookie`` line.

    Chromium joins multiple ``Set-Cookie`` headers with newlines. Each line's
    first ``name=value`` is the cookie; ``Path``/``Domain``/``Expires`` and the
    ``HttpOnly``/``Secure`` flags map onto the HAR 1.2 cookie fields.
    ``expires`` is kept as the raw HTTP-date. ``Max-Age`` and ``SameSite`` have
    no HAR 1.2 field and are dropped (the raw header line is still in
    ``response.headers``). A quoted value containing ``;`` is not supported
    (it is split like any other).
    """
    cookies: list[dict[str, Any]] = []
    lines = [line for value in _header_values(headers, "set-cookie") for line in value.split("\n")]
    for line in lines:
        first, *attrs = line.split(";")
        parsed = _parse_cookie_pairs([first])
        if not parsed:
            continue
        cookie: dict[str, Any] = parsed[0]
        for attr in attrs:
            key, _, val = attr.strip().partition("=")
            lowered = key.strip().lower()
            if lowered in _COOKIE_FLAG_ATTRS:
                cookie[_COOKIE_FLAG_ATTRS[lowered]] = True
            elif lowered in _COOKIE_VALUE_ATTRS and val:
                cookie[_COOKIE_VALUE_ATTRS[lowered]] = val.strip()
        cookies.append(cookie)
    return cookies


def _build_har_entry(request_data: dict[str, Any]) -> dict[str, Any]:
    """Build a HAR 1.2 entry dict from correlated request/response data."""
    response = request_data.get("response", {})
    # Raw wire headers (ExtraInfo) win over the renderer/filtered ones of the
    # same name, matched case-insensitively (see _merge_headers_ci).
    request_headers = _merge_headers_ci(
        request_data.get("headers"), request_data.get("_request_extra_headers")
    )
    response_headers = _merge_headers_ci(
        response.get("headers"), request_data.get("_response_extra_headers")
    )
    entry: dict[str, Any] = {
        "startedDateTime": _wall_time_to_iso(request_data.get("timestamp")),
        "time": 0,
        "request": {
            "method": request_data.get("method", "GET"),
            "url": request_data.get("url", ""),
            "headers": [{"name": k, "value": v} for k, v in request_headers.items()],
            "cookies": _request_cookies(request_headers),
            "queryString": [],
        },
        "response": {
            "status": response.get("status", 0),
            "statusText": response.get("statusText", ""),
            "headers": [{"name": k, "value": v} for k, v in response_headers.items()],
            "cookies": _response_cookies(response_headers),
            "redirectURL": response.get("redirectURL", ""),
            "content": {
                "mimeType": response.get("mimeType", ""),
                "size": response.get("bodySize", 0),
                "text": response.get("body"),
                "_bodyFile": response.get("_bodyFile"),
            },
        },
    }
    # Add POST data if present
    post_data = request_data.get("post_data")
    if post_data:
        content_type = request_data.get("headers", {}).get("Content-Type", "")
        entry["request"]["postData"] = {
            "mimeType": content_type,
            "text": post_data,
        }
    return entry


@runtime_checkable
class CaptureBackend(Protocol):
    """Protocol for browser capture backends."""

    def start_capture(self) -> None:
        """Begin capturing browser data."""
        ...

    def stop_capture(self) -> None:
        """Stop capturing and finalize data collection."""
        ...

    def take_screenshot_sync(self) -> bytes | None:
        """Take a synchronous screenshot.

        Returns:
            PNG image data as bytes, or None if capture failed.
        """
        ...

    def get_har_entries(self) -> list[dict[str, Any]]:
        """Return collected HAR (HTTP Archive) entries.

        Returns:
            List of HAR entry dicts.
        """
        ...

    def get_console_logs(self) -> list[dict[str, Any]]:
        """Return collected browser console logs.

        Returns:
            List of console log entry dicts.
        """
        ...

    def get_header_roles(self) -> dict[str, dict[str, str]]:
        """Return header roles classified from captured requests."""
        ...

    async def take_screenshot(self) -> bytes | None:
        """Take a screenshot asynchronously."""
        ...

    async def get_page_source(self) -> str | None:
        """Get the current page HTML source asynchronously."""
        ...

    async def start_capture_async(self) -> None:
        """Begin capturing browser data asynchronously (CDP event listeners)."""
        ...

    async def stop_capture_async(self) -> None:
        """Stop capturing and fetch pending data asynchronously."""
        ...


class SeleniumCaptureBackend:
    """Capture backend for Selenium WebDriver.

    Captures full request/response data including bodies and console logs
    by parsing Chrome DevTools Protocol events from the performance log.
    """

    def __init__(
        self,
        driver: Any,
        max_body_size: int = MAX_RESPONSE_BODY_SIZE,
        bodies_dir: Path | None = None,
    ) -> None:
        self._driver = driver
        self._max_body_size = max_body_size
        self._bodies_dir = bodies_dir
        self._request_map: dict[str, dict[str, Any]] = {}
        self._console_logs: list[dict[str, Any]] = []
        self._extra_info = _ExtraInfoCorrelator()
        self._stopped: bool = False

    def start_capture(self) -> None:
        """Begin capturing browser data."""
        self._stopped = False
        self._extra_info = _ExtraInfoCorrelator()  # never carry slots across captures
        LOG.debug("selenium_capture_started")

    def stop_capture(self) -> None:
        """Stop capturing, parse performance log CDP events, and fetch bodies.

        Parses Network.requestWillBeSent, Network.responseReceived, and
        Runtime.consoleAPICalled events from the performance log. Then fetches
        POST bodies and response bodies via CDP commands.

        Idempotent: the performance log is drained on the first call and a
        second call (e.g. the login engine extracting header roles, then
        BrowserSession.__exit__ flushing observe data) must not re-fetch every
        body and log one eviction warning per request.
        """
        if self._stopped:
            LOG.debug("selenium_capture_already_stopped")
            return
        self._stopped = True
        # Parse performance log for network and console events
        try:
            perf_log = self._driver.get_log("performance")
        except _WebDriverError as exc:
            LOG.error(
                "performance_log_collection_failed",
                error=str(exc),
                exc_type=type(exc).__name__,
            )
            perf_log = []
            from graftpunk import console as gp_console

            gp_console.warn(
                "Performance log collection failed — HAR capture will be empty. "
                "Ensure Chrome was started with performance logging enabled."
            )

        for entry in perf_log:
            try:
                parsed = json.loads(entry["message"])
                msg = parsed.get("message", {})
                method = msg.get("method", "")
                params = msg.get("params", {})
            except (json.JSONDecodeError, KeyError, TypeError):
                continue

            if method == "Network.requestWillBeSent":
                request = params.get("request", {})
                rid = params.get("requestId", "")
                redirect = params.get("redirectResponse")
                if redirect is not None:
                    # One HAR entry per redirect hop (see _finalize_redirect_hop).
                    hop_key = _finalize_redirect_hop(
                        self._request_map,
                        rid,
                        _response_summary(
                            redirect.get("status", 0),
                            redirect.get("statusText"),
                            redirect.get("headers"),
                            redirect.get("mimeType"),
                        ),
                        request.get("url", ""),
                    )
                    self._extra_info.hop_finalized(
                        self._request_map, rid, hop_key, bool(params.get("redirectHasExtraInfo"))
                    )
                self._request_map[rid] = {
                    "url": request.get("url", ""),
                    "method": request.get("method", "GET"),
                    "headers": request.get("headers", {}),
                    "post_data": request.get("postData"),
                    "has_post_data": request.get("hasPostData", False),
                    "timestamp": params.get("wallTime"),
                }
                self._extra_info.entry_created(self._request_map, rid)
                if redirect is not None:
                    self._extra_info.status_known(self._request_map, rid)
            elif method == "Network.responseReceived":
                response = params.get("response", {})
                rid = params.get("requestId", "")
                if rid not in self._request_map:
                    self._request_map[rid] = {
                        "url": response.get("url", ""),
                        "method": "GET",
                        "headers": {},
                        # Perf log is replayed at stop time: this is stop time, not
                        # arrival. responseReceived carries no wallTime; orphans sort last.
                        "timestamp": time.time(),
                        "has_post_data": False,
                        "post_data": None,
                        "_from_response_only": True,  # method/URL are guesses
                    }
                    self._extra_info.entry_created(self._request_map, rid)
                self._request_map[rid]["response"] = _response_summary(
                    response.get("status", 0),
                    response.get("statusText"),
                    response.get("headers"),
                    response.get("mimeType"),
                )
                self._extra_info.status_known(self._request_map, rid)
            elif method == "Network.requestWillBeSentExtraInfo":
                self._extra_info.request_extra(
                    self._request_map, params.get("requestId", ""), params.get("headers")
                )
            elif method == "Network.responseReceivedExtraInfo":
                self._extra_info.response_extra(
                    self._request_map,
                    params.get("requestId", ""),
                    params.get("headers"),
                    params.get("statusCode"),
                )
            elif method == "Runtime.consoleAPICalled":
                args = params.get("args", [])
                self._console_logs.append(
                    {
                        "level": params.get("type", "log"),
                        "args": [arg.get("value", str(arg)) for arg in args],
                        "timestamp": _console_timestamp(params.get("timestamp")),
                    }
                )

        self._extra_info.log_unmatched(self._request_map)

        # Fetch bodies for all captured requests
        for request_id, data in list(self._request_map.items()):
            if _is_redirect_hop(data):
                _note_lost_hop_body(request_id, data)
                continue
            # Fetch POST body if hasPostData but no inline postData
            if data.get("has_post_data") and not data.get("post_data"):
                try:
                    result = self._driver.execute_cdp_cmd(
                        "Network.getRequestPostData", {"requestId": request_id}
                    )
                    data["post_data"] = result.get("postData")
                except Exception as exc:  # noqa: BLE001 — CDP body fetch is best-effort
                    LOG.warning(
                        "selenium_post_data_fetch_failed",
                        request_id=request_id,
                        error=str(exc),
                    )

            # Fetch response body
            response = data.get("response", {})
            mime = response.get("mimeType", "")
            if _is_text_mime(mime) or _is_binary_mime(mime):
                try:
                    result = self._driver.execute_cdp_cmd(
                        "Network.getResponseBody", {"requestId": request_id}
                    )
                    body = result.get("body", "")
                    base64_encoded = result.get("base64Encoded", False)
                    _process_response_body(
                        response=response,
                        body=body,
                        base64_encoded=base64_encoded,
                        request_id=request_id,
                        mime=mime,
                        max_body_size=self._max_body_size,
                        bodies_dir=self._bodies_dir,
                    )
                except Exception as exc:  # noqa: BLE001 — CDP body fetch is best-effort
                    LOG.warning(
                        "selenium_response_body_fetch_failed",
                        request_id=request_id,
                        error=str(exc),
                    )

        # Also collect "browser" log for backward compat. CDP consoleAPICalled
        # events above cover JS console output; browser logs may contain additional
        # entries (e.g. network errors) that Selenium surfaces separately.
        try:
            browser_logs = self._driver.get_log("browser")
            # Same unit rule as the CDP console events (issue #158); entries
            # without a timestamp pass through untouched.
            self._console_logs.extend(
                {**e, "timestamp": _console_timestamp(e["timestamp"])} if "timestamp" in e else e
                for e in browser_logs
            )
        except _WebDriverError as exc:
            LOG.error(
                "console_log_collection_failed",
                error=str(exc),
                exc_type=type(exc).__name__,
            )
            from graftpunk import console as gp_console

            gp_console.warn("Browser log collection failed — console logs may be incomplete.")

    def take_screenshot_sync(self) -> bytes | None:
        """Take a screenshot via Selenium WebDriver.

        Returns:
            PNG image data as bytes, or None if screenshot failed.
        """
        try:
            return self._driver.get_screenshot_as_png()
        except _WebDriverError as exc:
            LOG.error(
                "screenshot_failed",
                error=str(exc),
                exc_type=type(exc).__name__,
            )
            return None

    def get_har_entries(self) -> list[dict[str, Any]]:
        """Return collected HAR 1.2 entries built from correlated request/response data.

        Returns:
            List of HAR entry dicts.
        """
        return _har_entries_in_order(self._request_map)

    def get_console_logs(self) -> list[dict[str, Any]]:
        """Return collected browser console logs.

        Returns:
            List of console log entry dicts.
        """
        return self._console_logs

    def get_header_roles(self) -> dict[str, dict[str, str]]:
        """Return header roles classified from captured network requests."""

        return _header_roles_in_order(self._request_map)

    async def take_screenshot(self) -> bytes | None:
        """Take a screenshot asynchronously (wraps sync method)."""
        return self.take_screenshot_sync()

    async def get_page_source(self) -> str | None:
        """Get the current page HTML source asynchronously."""
        try:
            return self._driver.page_source
        except _WebDriverError as exc:
            LOG.error(
                "selenium_get_page_source_failed",
                error=str(exc),
                exc_type=type(exc).__name__,
            )
            return None

    async def stop_capture_async(self) -> None:
        """Stop capturing and fetch pending data asynchronously (wraps sync)."""
        self.stop_capture()

    async def start_capture_async(self) -> None:
        """Begin capturing browser data asynchronously (no-op for Selenium)."""
        self.start_capture()


def _eager_send_kwargs(tab: Any) -> dict[str, Any]:
    """Internal kwargs for the eager body fetch, adapted to the installed nodriver.

    nodriver <= 0.48 exposes ``Connection.send(cdp_obj, _is_update=False)``;
    passing ``_is_update=True`` skips ``_register_handlers()`` on every fetch
    (issue #64 Fix 2). nodriver >= 0.50.1 renamed the flag to ``_attach`` (which
    also suppresses ``sessionId`` -- different semantics) and merges any unknown
    kwargs straight into the CDP message, so a stale ``_is_update=True`` puts a
    top-level property on the wire and Chrome rejects the whole command with
    ``-32600`` (issue #146). 0.50's ``send()`` no longer re-registers handlers,
    so there is nothing to skip there.

    Only pass ``_is_update`` when ``send()`` actually declares it. If the
    signature cannot be introspected, pass nothing rather than guess.
    """
    try:
        params = inspect.signature(tab.send).parameters
    except (TypeError, ValueError):
        return {}
    if "_is_update" in params:
        return {"_is_update": True}
    return {}


class NodriverCaptureBackend:
    """Capture backend for nodriver (async Chrome DevTools Protocol).

    Captures full request/response data including bodies and console logs.
    Synchronous screenshot is not available since nodriver requires async.
    """

    def __init__(
        self,
        browser: Any,
        get_tab: Callable[[], Any] | None = None,
        max_body_size: int = MAX_RESPONSE_BODY_SIZE,
        bodies_dir: Path | None = None,
    ) -> None:
        self._browser = browser
        self._get_tab = get_tab
        self._max_body_size = max_body_size
        self._bodies_dir = bodies_dir
        self._request_map: dict[str, dict[str, Any]] = {}
        self._console_logs: list[dict[str, Any]] = []
        self._extra_info = _ExtraInfoCorrelator()
        self._warned_no_screenshots: bool = False
        self._bodies_fetched: set[str] = set()  # request IDs with eagerly-fetched bodies
        self._eager_fetch_attempts: int = 0
        self._eager_fetch_failures: int = 0

    @property
    def _tab(self) -> Any | None:
        """Get the current tab via the get_tab callable."""
        return self._get_tab() if self._get_tab else None

    def start_capture(self) -> None:
        """Begin capturing browser data."""
        self._extra_info = _ExtraInfoCorrelator()  # never carry slots across captures
        LOG.debug("nodriver_capture_started")

    def stop_capture(self) -> None:
        """Stop capturing browser data."""
        LOG.debug("nodriver_capture_stopped")

    def take_screenshot_sync(self) -> bytes | None:
        """Synchronous screenshot not available for nodriver.

        Returns:
            Always None (nodriver requires async for screenshots).
        """
        if not self._warned_no_screenshots:
            LOG.warning("nodriver_screenshot_sync_not_available")
            self._warned_no_screenshots = True
        return None

    def get_har_entries(self) -> list[dict[str, Any]]:
        """Return collected HAR 1.2 entries built from correlated request/response data.

        Returns:
            List of HAR entry dicts.
        """
        return _har_entries_in_order(self._request_map)

    def get_console_logs(self) -> list[dict[str, Any]]:
        """Return collected browser console logs.

        Returns:
            List of console log entry dicts.
        """
        return self._console_logs

    def get_header_roles(self) -> dict[str, dict[str, str]]:
        """Return header roles classified from captured network requests."""

        return _header_roles_in_order(self._request_map)

    async def take_screenshot(self) -> bytes | None:
        """Take a screenshot asynchronously via nodriver.

        Returns:
            PNG image data as bytes, or None if capture failed.
        """
        tab = self._tab
        if tab is None:
            return None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            await tab.save_screenshot(tmp_path)
            return Path(tmp_path).read_bytes()
        except ConnectionError:
            LOG.warning("nodriver_browser_disconnected", operation="screenshot")
            return None
        except Exception:
            LOG.exception("nodriver_screenshot_async_failed")
            return None
        finally:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink(missing_ok=True)

    async def get_page_source(self) -> str | None:
        """Get the current page HTML source asynchronously.

        Returns:
            HTML source string, or None if not available.
        """
        tab = self._tab
        if tab is None:
            return None
        try:
            return await tab.get_content()
        except ConnectionError:
            LOG.warning("nodriver_browser_disconnected", operation="page_source")
            return None
        except Exception:
            LOG.exception("nodriver_get_page_source_failed")
            return None

    async def start_capture_async(self) -> None:
        """Begin capturing browser data asynchronously (CDP event listeners)."""
        self.start_capture()  # resets per-capture correlator state
        tab = self._tab
        if tab is None:
            LOG.warning("nodriver_start_capture_async_no_tab")
            return

        import nodriver.cdp.network as network
        import nodriver.cdp.runtime as cdp_runtime

        await tab.send(
            network.enable(
                max_total_buffer_size=100 * 1024 * 1024,  # 100 MB total buffer
                max_resource_buffer_size=10 * 1024 * 1024,  # 10 MB per resource
                # Keeps bodies fetchable longer. Load-bearing on nodriver >= 0.50
                # (flattened sessions): measured on 0.50.3, getResponseBody
                # returns -32000 for every request without it and succeeds for
                # nearly all with it. See issue #146.
                enable_durable_messages=True,
            )
        )
        tab.add_handler(network.RequestWillBeSent, self._on_request)
        tab.add_handler(network.ResponseReceived, self._on_response)
        tab.add_handler(network.LoadingFinished, self._on_loading_finished)
        # Raw wire headers (Cookie / Set-Cookie) only arrive on the ExtraInfo
        # events; the filtered ones above never carry them (issue #157). The
        # classes exist on 0.48.1 and 0.50.3; guard so an older codegen
        # degrades to filtered headers instead of breaking capture.
        for name, handler in (
            ("RequestWillBeSentExtraInfo", self._on_request_extra_info),
            ("ResponseReceivedExtraInfo", self._on_response_extra_info),
        ):
            event_cls = getattr(network, name, None)
            if event_cls is None:
                LOG.debug("nodriver_extra_info_event_unavailable", event_name=name)
                continue
            tab.add_handler(event_cls, handler)

        # Console log capture
        await tab.send(cdp_runtime.enable())
        tab.add_handler(cdp_runtime.ConsoleAPICalled, self._on_console)

        LOG.debug("nodriver_capture_async_started")

    async def stop_capture_async(self) -> None:
        """Stop capturing and fetch request/response bodies asynchronously."""
        tab = self._tab
        if tab is None:
            return

        import nodriver.cdp.network as cdp_net

        self._extra_info.log_unmatched(self._request_map)

        # One aggregate line so a systematic eager-fetch failure (e.g. a
        # nodriver internals change, issue #146) is visible at a glance rather
        # than buried in per-request warnings that read as ordinary eviction.
        if self._eager_fetch_failures:
            LOG.warning(
                "nodriver_eager_body_fetch_summary",
                failed=self._eager_fetch_failures,
                attempted=self._eager_fetch_attempts,
                hint=(
                    "Bodies not fetched eagerly are retried below while the browser "
                    "is alive; a 100% failure rate points at a nodriver/CDP mismatch, "
                    "not eviction."
                ),
            )

        for request_id, data in list(self._request_map.items()):
            if _is_redirect_hop(data):
                _note_lost_hop_body(request_id, data)
                continue
            # Fetch POST body if has_post_data but no inline post_data
            if data.get("has_post_data") and not data.get("post_data"):
                try:
                    result = await tab.send(
                        cdp_net.get_request_post_data(cdp_net.RequestId(request_id))
                    )
                    data["post_data"] = result
                except ConnectionError as exc:
                    LOG.warning(
                        "nodriver_browser_disconnected_during_stop",
                        phase="post_data",
                        request_id=request_id,
                        error=str(exc),
                        exc_type=type(exc).__name__,
                    )
                    return
                except Exception as exc:  # noqa: BLE001 — CDP body fetch is best-effort
                    LOG.warning(
                        "nodriver_post_data_fetch_failed",
                        request_id=request_id,
                        error=str(exc),
                    )

            # Skip response body if already eagerly fetched by _on_loading_finished
            if request_id in self._bodies_fetched:
                continue

            # Fetch response body (fallback for any missed by eager fetch)
            response = data.get("response", {})
            mime = response.get("mimeType", "")
            if _is_text_mime(mime) or _is_binary_mime(mime):
                try:
                    body, base64_encoded = await tab.send(
                        cdp_net.get_response_body(cdp_net.RequestId(request_id))
                    )
                    _process_response_body(
                        response=response,
                        body=body,
                        base64_encoded=base64_encoded,
                        request_id=request_id,
                        mime=mime,
                        max_body_size=self._max_body_size,
                        bodies_dir=self._bodies_dir,
                    )
                except ConnectionError as exc:
                    LOG.warning(
                        "nodriver_browser_disconnected_during_stop",
                        phase="response_body",
                        request_id=request_id,
                        error=str(exc),
                        exc_type=type(exc).__name__,
                    )
                    return
                except Exception as exc:  # noqa: BLE001 — CDP body fetch is best-effort
                    LOG.warning(
                        "nodriver_response_body_fetch_failed",
                        request_id=request_id,
                        error=str(exc),
                    )

    def _on_request(self, event: Any) -> None:
        """Handle a CDP RequestWillBeSent event."""
        try:
            rid = str(event.request_id)
            redirect = getattr(event, "redirect_response", None)
            if redirect is not None:
                # One HAR entry per redirect hop (see _finalize_redirect_hop).
                hop_key = _finalize_redirect_hop(
                    self._request_map,
                    rid,
                    _response_summary(
                        redirect.status, redirect.status_text, redirect.headers, redirect.mime_type
                    ),
                    event.request.url,
                )
                self._extra_info.hop_finalized(
                    self._request_map,
                    rid,
                    hop_key,
                    bool(getattr(event, "redirect_has_extra_info", False)),
                )
            self._request_map[rid] = {
                "url": event.request.url,
                "method": event.request.method,
                "headers": (dict(event.request.headers) if event.request.headers else {}),
                "post_data": getattr(event.request, "post_data", None),
                "has_post_data": getattr(event.request, "has_post_data", False),
                "timestamp": getattr(event, "wall_time", None),
            }
            self._extra_info.entry_created(self._request_map, rid)
            if redirect is not None:
                self._extra_info.status_known(self._request_map, rid)
        except Exception:
            LOG.exception("nodriver_on_request_failed")

    def _on_request_extra_info(self, event: Any) -> None:
        """Handle a CDP RequestWillBeSentExtraInfo event (raw request headers)."""
        try:
            self._extra_info.request_extra(self._request_map, str(event.request_id), event.headers)
        except Exception:
            LOG.exception("nodriver_on_request_extra_info_failed")

    def _on_response_extra_info(self, event: Any) -> None:
        """Handle a CDP ResponseReceivedExtraInfo event (raw response headers)."""
        try:
            self._extra_info.response_extra(
                self._request_map,
                str(event.request_id),
                event.headers,
                getattr(event, "status_code", None),
            )
        except Exception:
            LOG.exception("nodriver_on_response_extra_info_failed")

    def _on_response(self, event: Any) -> None:
        """Handle a CDP ResponseReceived event and correlate with request data."""
        try:
            rid = str(event.request_id)
            if rid not in self._request_map:
                self._request_map[rid] = {
                    "url": event.response.url,
                    "method": "GET",
                    "headers": {},
                    "timestamp": time.time(),  # arrival time; the request was never seen
                    "has_post_data": False,
                    "post_data": None,
                    "_from_response_only": True,  # method/URL are guesses
                }
                self._extra_info.entry_created(self._request_map, rid)
            self._request_map[rid]["response"] = _response_summary(
                event.response.status,
                event.response.status_text,
                event.response.headers,
                event.response.mime_type,
            )
            self._extra_info.status_known(self._request_map, rid)
        except Exception:
            LOG.exception("nodriver_on_response_failed")

    async def _on_loading_finished(self, event: Any) -> None:
        """Handle CDP LoadingFinished — eagerly fetch response body while still in buffer.

        CDP may evict response bodies from its internal buffer after LoadingFinished.
        Fetching eagerly ensures bodies are captured before eviction.
        ``stop_capture_async`` retries any bodies not eagerly fetched, but will
        bail immediately on connection errors (browser dead), so eager fetch
        is the primary mechanism for capturing bodies.
        """
        data: dict[str, Any] | None = None
        try:
            import nodriver.cdp.network as cdp_net

            rid = str(event.request_id)

            data = self._request_map.get(rid)
            if data is None:
                return

            response = data.get("response")
            if response is None:
                return

            mime = response.get("mimeType", "")
            if not (_is_text_mime(mime) or _is_binary_mime(mime)):
                return

            tab = self._tab
            if tab is None:
                return

            self._eager_fetch_attempts += 1
            body, base64_encoded = await tab.send(
                cdp_net.get_response_body(cdp_net.RequestId(rid)),
                **_eager_send_kwargs(tab),
            )
            _process_response_body(
                response=response,
                body=body,
                base64_encoded=base64_encoded,
                request_id=rid,
                mime=mime,
                max_body_size=self._max_body_size,
                bodies_dir=self._bodies_dir,
            )
            self._bodies_fetched.add(rid)
        except Exception as exc:  # noqa: BLE001 — best-effort; stop_capture_async retries non-connection errors
            self._eager_fetch_failures += 1
            url = data.get("url", "unknown") if data is not None else "unknown"
            LOG.warning(
                "nodriver_eager_body_fetch_failed",
                request_id=str(getattr(event, "request_id", "unknown")),
                url=url,
                error=str(exc),
                exc_type=type(exc).__name__,
            )

    def _on_console(self, event: Any) -> None:
        """Handle a CDP ConsoleAPICalled event."""
        try:
            self._console_logs.append(
                {
                    "level": (
                        event.type_.value if hasattr(event.type_, "value") else str(event.type_)
                    ),
                    "args": [getattr(arg, "value", str(arg)) for arg in (event.args or [])],
                    "timestamp": _console_timestamp(getattr(event, "timestamp", None)),
                }
            )
        except Exception:
            LOG.exception("nodriver_on_console_failed")


def create_capture_backend(
    backend_type: str,
    driver: Any,
    get_tab: Callable[[], Any] | None = None,
    max_body_size: int = MAX_RESPONSE_BODY_SIZE,
    bodies_dir: Path | None = None,
) -> CaptureBackend:
    """Factory to create the appropriate capture backend.

    Args:
        backend_type: Either "nodriver" or "selenium".
        driver: The browser driver/browser instance.
        get_tab: Optional callable that returns the current nodriver tab.
        max_body_size: Maximum response body size to keep in memory (bytes).
        bodies_dir: Directory to stream large/binary bodies to disk.

    Returns:
        A CaptureBackend implementation.
    """
    if backend_type == "nodriver":
        return NodriverCaptureBackend(
            driver,
            get_tab=get_tab,
            max_body_size=max_body_size,
            bodies_dir=bodies_dir,
        )
    if backend_type == "selenium":
        if not _HAS_SELENIUM:
            msg = "Selenium is not installed. Install it with: pip install selenium"
            raise ImportError(msg)
        return SeleniumCaptureBackend(
            driver,
            max_body_size=max_body_size,
            bodies_dir=bodies_dir,
        )
    msg = f"Unknown capture backend type: {backend_type!r}. Must be 'selenium' or 'nodriver'."
    raise ValueError(msg)
