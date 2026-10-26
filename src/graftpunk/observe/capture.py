"""Backend-specific browser capture implementations."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import datetime
import inspect
import json
import math
import os
import tempfile
import time
import urllib.parse
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NamedTuple, Protocol, runtime_checkable

from graftpunk._distribution import install_command
from graftpunk.har.parser import cookie_header_pairs
from graftpunk.logging import get_logger

try:
    from selenium.common.exceptions import WebDriverException as _WebDriverError

    _HAS_SELENIUM = True
except ImportError:  # graftpunk-lite without its [browser] extra
    _HAS_SELENIUM = False

    class _WebDriverError(Exception):
        """Stand-in so `except _WebDriverError` stays valid without selenium."""


LOG = get_logger(__name__)

# Body size cap
MAX_RESPONSE_BODY_SIZE = 5 * 1024 * 1024  # 5MB
# How long stop waits for request-time body fetches still in flight.
_POST_DATA_SETTLE_TIMEOUT_S = 5.0
# The most stop then waits for the tasks it has cancelled to finish, and the most a
# late read waits for its DOM.disable, which a page that has stopped answering
# DevTools never answers (nodriver's send waits with no bound). The task's wait is
# the shorter, so a snapshot cancelled on such a page counts itself before stop
# logs its summary.
_STOP_WAIT_S = 3.0
_DOM_DISABLE_WAIT_S = 2.0

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

TEXT_MIME_KEYWORDS = (
    "json",
    "html",
    "text",
    "xml",
    "javascript",
    "css",
    "csv",
    "x-www-form-urlencoded",
    "yaml",
)

# How many of the page's own DOM snapshots (graftpunk.observe.dom_listener) the
# recorder keeps: one user action sends one, so a few cover the actions before a
# credential post.
DOM_SNAPSHOT_BUFFER = 5
# A listener snapshot older than this before a credential post is not that post's:
# the user's action and the request it sends are milliseconds apart, and an old
# snapshot is a page the user has since left or changed.
DOM_SNAPSHOT_MAX_AGE_MS = 30_000
# The largest page HTML a snapshot keeps, in characters; a larger one is skipped and
# counted, as sanitizing it would hold the recorder for seconds.
DOM_SNAPSHOT_MAX_CHARS = 5 * 1024 * 1024


class _ListenerSnapshot(NamedTuple):
    """A DOM snapshot: when (ms since the epoch, the page's ``Date.now()``), on which
    document, and its HTML as the page serialized it (sanitized only when a credential
    post takes it)."""

    time_ms: float
    url: str
    # None for a page over DOM_SNAPSHOT_MAX_CHARS, kept as a marker.
    html: str | None


def _wall_time_ms(wall_time: Any) -> float | None:
    """CDP's ``wallTime`` (seconds since the epoch) in milliseconds, the unit of the
    page's ``Date.now()``; None when it is missing or not a finite number."""
    if isinstance(wall_time, bool) or not isinstance(wall_time, int | float):
        return None
    ms = float(wall_time) * 1000.0
    return ms if math.isfinite(ms) else None


def _sending_page(event: Any) -> str | None:
    """The URL of the page that sent the request of a ``Network.requestWillBeSent``
    *event*: its ``documentURL``, except for a navigation (a ``Document`` request, a
    form post), whose ``documentURL`` is the URL being loaded; then the ``Referer``
    header, else the initiator's URL, else None (a page with a strict
    ``Referrer-Policy``; which initiators carry a URL is unverified)."""
    resource = getattr(event, "type_", None)
    resource = getattr(resource, "value", resource)
    document_url = getattr(event, "document_url", None)
    if resource != "Document":
        return document_url
    initiator = getattr(getattr(event, "initiator", None), "url", None)
    return _referer(event) or initiator or None


def _referer(event: Any) -> str:
    headers = getattr(getattr(event, "request", None), "headers", None) or {}
    return next(
        (str(value) for name, value in dict(headers).items() if name.lower() == "referer"), ""
    )


def _origin_only(event: Any) -> bool:
    """Whether a navigation's sending page is known only as an origin: its Referer
    has no path at all, or is ``/`` with no query on a request to another origin.
    Chrome's default ``strict-origin-when-cross-origin`` sends a cross-origin form
    post's Referer as the origin and ``/`` (measured on 2026-10-24: ``Referer:
    http://127.0.0.1:8767/`` from ``/form.html``), and a same-origin one whole, so a
    ``/`` Referer on a same-origin request is the page ``/`` itself."""
    referer = _referer(event)
    if not _is_navigation(event) or not referer:
        return False
    try:
        parts = urllib.parse.urlsplit(referer)
    except ValueError:
        return False
    if parts.query or parts.path not in ("", "/"):
        return False
    request_url = getattr(getattr(event, "request", None), "url", None)
    return parts.path == "" or _origin(referer) != _origin(request_url)


def _is_navigation(event: Any) -> bool:
    resource = getattr(event, "type_", None)
    return getattr(resource, "value", resource) == "Document"


def _origin(url: Any) -> tuple[str, str, int | None] | None:
    """*url*'s scheme, host, and port (the default one filled in), or None."""
    if not isinstance(url, str):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if not scheme or not parts.hostname:
        return None
    if port is None:
        port = {"http": 80, "https": 443}.get(scheme)
    return scheme, parts.hostname.lower(), port


# Statuses that carry no body by definition: nothing to fetch or store.
_BODYLESS_STATUSES = frozenset({204, 205, 304})


def _is_binary_mime(mime: str) -> bool:
    """Check if MIME type represents a binary/file format."""
    mime_lower = mime.lower()
    return any(mime_lower.startswith(prefix) for prefix in BINARY_MIME_PREFIXES)


def _is_text_mime(mime: str) -> bool:
    """Check if MIME type represents a text format that can go into the HAR as text."""
    mime_lower = mime.lower()
    return any(kw in mime_lower for kw in TEXT_MIME_KEYWORDS)


def _has_no_body(status: Any) -> bool:
    """True for a 1xx, 204, 205 or 304 response, which has no body to fetch."""
    return isinstance(status, int) and (100 <= status < 200 or status in _BODYLESS_STATUSES)


def _body_expected(data: dict[str, Any], response: dict[str, Any]) -> bool:
    """Whether the browser captures fetch this response's body.

    Every response's body is fetched, whatever its type (an unknown or missing
    type is kept in ``bodies/``), except where HTTP says there is none: a HEAD
    request, or a 1xx, 204, 205 or 304 status.
    """
    return data.get("method") != "HEAD" and not _has_no_body(response.get("status"))


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
        "text/csv": ".csv",
        "application/csv": ".csv",
        "application/x-www-form-urlencoded": ".txt",
        "application/javascript": ".js",
        "text/javascript": ".js",
        "application/pdf": ".pdf",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/svg+xml": ".svg",
    }
    return mapping.get(mime_lower, ".bin")


def _declared_charset(content_type: str) -> str | None:
    """The ``charset`` parameter of a ``Content-Type`` value, or None."""
    for param in content_type.split(";")[1:]:
        name, sep, value = param.partition("=")
        if sep and name.strip().lower() == "charset":
            return value.strip().strip("\"'") or None
    return None


def _decode_text(raw: bytes, charset: str | None) -> str | None:
    """*raw* as text in its declared *charset*, else as UTF-8; None when that fails.

    No charset is guessed: a guess can be wrong, and the HAR would then hold
    text whose bytes are lost. A body that does not decode is kept as raw
    bytes in ``bodies/`` instead.
    """
    try:
        return raw.decode(charset or "utf-8")
    except (UnicodeDecodeError, LookupError):
        return None


def _write_body_file(bodies_dir: Path, name: str, raw: bytes) -> str:
    """Write *raw* to ``bodies_dir/name``, owner-only, and return its run-relative path."""
    from graftpunk.observe.storage import make_private_dir, write_private_file

    bodies_dir.parent.mkdir(parents=True, exist_ok=True)
    make_private_dir(bodies_dir)
    write_private_file(bodies_dir / name, raw)
    return f"bodies/{name}"


def _process_response_body(
    response: dict[str, Any],
    body: str | bytes,
    base64_encoded: bool,
    request_id: str,
    mime: str,
    max_body_size: int,
    bodies_dir: Path | None,
) -> None:
    """Store a response body in the HAR as text, in ``bodies_dir``, or mark it left out.

    A body of a text type (:func:`_is_text_mime`) within *max_body_size* goes
    into the HAR as text. A ``str`` body (CDP's, when not base64) is that text
    already; bytes are decoded with the charset *mime* declares, else as
    UTF-8, and never with a guessed charset. Every other body, one of a
    binary, unknown or missing type, a larger one and one that does not
    decode, is written to ``bodies_dir`` as raw bytes, named
    ``<request_id><ext>``, referenced as ``_bodyFile``, with why in
    ``_bodyFileReason``. Without ``bodies_dir`` (or when the write fails) it
    is left out and marked: ``_bodyNotCaptured`` holds why. An empty body is
    stored as empty text.

    Modifies *response* in place and sets ``bodySize``.
    """
    if base64_encoded:
        body_bytes = base64.b64decode(body)
        text: str | None = None
    elif isinstance(body, str):
        body_bytes = body.encode("utf-8", errors="surrogatepass")
        text = body
    else:
        body_bytes = body
        text = None

    body_size = len(body_bytes)
    response["bodySize"] = body_size
    if body_size == 0:
        response["body"] = ""
        return

    # Binary first, on the bare type: an Office type names "xml", and a
    # parameter can hold any word ("name=context.bin" holds "text").
    bare = mime.split(";")[0].strip()
    if _is_binary_mime(bare):
        reason = "binary"
    elif not _is_text_mime(bare):
        reason = "not_text"
    elif body_size > max_body_size:
        reason = "over_max_body_size"
    else:
        if text is None:
            text = _decode_text(body_bytes, _declared_charset(mime))
        if text is not None:
            response["body"] = text
            return
        reason = "not_decodable"

    if bodies_dir is not None:
        try:
            response["_bodyFile"] = _write_body_file(
                bodies_dir, f"{request_id}{_mime_to_extension(mime)}", body_bytes
            )
            response["_bodyFileReason"] = reason
            return
        except OSError as exc:
            LOG.warning("response_body_file_write_failed", request_id=request_id, error=str(exc))
            reason = "write_failed"
    response["_bodyNotCaptured"] = reason
    LOG.info("response_body_not_captured", request_id=request_id, size=body_size, reason=reason)


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


class _PostBody(NamedTuple):
    """A request body as received: its bytes, and its text when they are UTF-8."""

    raw: bytes
    text: str | None


def _post_body(result: Any) -> _PostBody | None:
    """The request body from an inline ``postData`` or ``getRequestPostData``'s result.

    nodriver 0.48 returns the ``postData`` string alone; nodriver 0.50 returns the
    pair ``(postData, base64Encoded)``, as the raw CDP reply Selenium gets carries
    both fields. A base64 body is decoded, whitespace inside it ignored. Its text
    is ``None`` when the bytes are not UTF-8: HAR's ``postData`` has no encoding
    field, and decoding with replacement would turn each such byte into U+FFFD,
    written as six characters in the HAR. A body flagged base64 that is not valid
    base64 is kept as the text Chrome sent.
    """
    if result is None:
        return None
    if not isinstance(result, tuple):
        return _PostBody(result.encode("utf-8", errors="surrogatepass"), result)
    text, base64_encoded = result
    if text is None:
        return None
    if not base64_encoded:
        return _PostBody(text.encode("utf-8", errors="surrogatepass"), text)
    try:
        raw = base64.b64decode("".join(text.split()), validate=True)
    except (binascii.Error, ValueError):
        LOG.warning("post_data_not_base64", length=len(text))
        return _PostBody(text.encode("utf-8", errors="surrogatepass"), text)
    try:
        return _PostBody(raw, raw.decode("utf-8"))
    except UnicodeDecodeError:
        return _PostBody(raw, None)


def _inline_post_body(post_data: Any, entries: Any) -> _PostBody | None:
    """The request body Chrome inlined on ``Network.requestWillBeSent``.

    ``postDataEntries`` (a list of base64 ``bytes``) is the body's bytes, and is
    used whenever every entry carries them: measured on 2026-10-19, Chrome sends
    it beside every inline ``postData``, and the entries joined equal the bytes
    the server received for a string, ``URLSearchParams``, ``Uint8Array`` and
    text-only multipart body. ``postData`` alone is not the bytes: Chrome
    decodes UTF-8 bytes as UTF-8, but maps each byte of a body that is not
    UTF-8 to the code point of the same value (``b"\\x00\\xff"`` arrives as
    ``"\\x00\\xff"``), so storing it as text turned a binary body into text
    of another length that the digest reads as an endpoint with no parameters.

    Without entries, or with an entry that carries no ``bytes`` or bytes that
    are not base64, the body is stored as the ``postData`` text Chrome gave, as
    before ``postDataEntries`` was read. Whether any Chrome sends a binary body
    inline without entries is unverified.
    """
    if isinstance(entries, list) and entries and all(isinstance(b, str) for b in entries):
        try:
            raw = b"".join(base64.b64decode(b, validate=True) for b in entries)
        except (binascii.Error, ValueError):
            LOG.warning("post_data_entries_not_base64", entries=len(entries))
        else:
            try:
                return _PostBody(raw, raw.decode("utf-8"))
            except UnicodeDecodeError:
                return _PostBody(raw, None)
    if not isinstance(post_data, str):
        return None
    return _post_body(post_data)


def _cdp_entries(entries: Any) -> list[Any] | None:
    """The base64 strings of CDP ``postDataEntries``, from a raw dict or nodriver objects."""
    if not entries:
        return None
    return [
        entry.get("bytes") if isinstance(entry, dict) else getattr(entry, "bytes_", None)
        for entry in entries
    ]


def _bare_page_url(url: str) -> str:
    """*url* as scheme, host, and path: the page a DOM snapshot was taken on, as a
    run stores it, without the query, fragment, or userinfo that can carry a token
    or an account. Empty when it cannot be split."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    host = parts.netloc.rpartition("@")[2]
    return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))


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
    if _post_data_missing(data) and not data.get("_hop_warned"):
        data["_hop_warned"] = True
        data["_post_data_unavailable"] = True
        LOG.warning(
            "redirect_hop_post_data_unavailable",
            request_id=request_id,
            url=data.get("url"),
            hint="Request body was not inlined by CDP and is lost across the redirect",
        )


def _post_data_missing(data: dict[str, Any]) -> bool:
    """True if the request announced a body (``hasPostData``) the recorder neither
    holds nor deliberately left out.

    An empty string is a body Chrome returned, so it counts as captured: only
    ``None`` is missing.
    """
    return (
        bool(data.get("has_post_data"))
        and data.get("post_data") is None
        and not data.get("_post_data_file")
        and not data.get("_post_data_omitted")
    )


def _request_content_type(data: dict[str, Any]) -> str:
    headers = data.get("headers") or {}
    return next((v for k, v in headers.items() if k.lower() == "content-type"), "")


def _keep_post_data(
    data: dict[str, Any],
    body: _PostBody | None,
    *,
    max_body_size: int,
    bodies_dir: Path | None,
    request_id: str,
) -> None:
    """Store *body* as the request's body, as a response body would be stored.

    A UTF-8 body within ``max_body_size`` (measured on its bytes) goes into the
    HAR's ``postData``. A larger one, or one that is not UTF-8, is written to
    ``bodies_dir`` as ``<request id>.request<ext>`` (``-2``, ``-3`` and so on when
    that name exists, as for the request a 307 or 308 redirects to, which keeps
    the hop's CDP request id) and referenced from the HAR
    as ``request._postDataFile``, the way an oversized or binary response body
    is written there and referenced as ``_bodyFile``. With no ``bodies_dir`` (or
    when the write fails) it is left out: the entry is marked as not captured,
    with ``_postDataOmitted`` giving its size and why, and logged once as
    ``post_data_omitted``.
    """
    if body is None:
        return
    size = len(body.raw)
    if body.text is not None and size <= max_body_size:
        data["post_data"] = body.text
        return
    reason = "binary" if body.text is None else "over_max_body_size"
    if bodies_dir is not None:
        # CDP keeps one request id across a redirect chain, and a 307 or 308 hop
        # resends its body, so the hop and its successor each get their own file.
        stem, ext = f"{request_id}.request", _mime_to_extension(_request_content_type(data))
        name, copy = f"{stem}{ext}", 2
        try:
            while (bodies_dir / name).exists():
                name, copy = f"{stem}-{copy}{ext}", copy + 1
            _write_body_file(bodies_dir, name, body.raw)
        except OSError as exc:
            LOG.warning("post_data_file_write_failed", request_id=request_id, error=str(exc))
        else:
            data["post_data"] = None
            data["_post_data_file"] = f"bodies/{name}"
            data["_post_data_size"] = size
            return
    data["post_data"] = None
    data["_post_data_omitted"] = {"size": size, "reason": reason}
    data["_post_data_unavailable"] = True
    LOG.warning(
        "post_data_omitted",
        request_id=request_id,
        method=data.get("method"),
        url=data.get("url"),
        size=size,
        reason=reason,
        max_body_size=max_body_size,
    )


def _flag_uncaptured_post_data(request_map: dict[str, dict[str, Any]]) -> None:
    """Mark and log, once each, the requests whose body is still missing after every fetch.

    The mark becomes the HAR entry's ``request._postDataNotCaptured``, so the
    digest and ``gp plugin new`` can say the body was not captured instead of
    reporting a request with no parameters. A redirect hop goes to
    ``_note_lost_hop_body``, which marks and logs it under its own event; the
    stop-time loop does that too, but this sweep also runs when that loop never
    reached the hop (no tab, or the browser died first).
    """
    for request_id, data in request_map.items():
        if _is_redirect_hop(data):
            _note_lost_hop_body(request_id, data)
            continue
        if not _post_data_missing(data) or data.get("_post_data_unavailable"):
            continue
        data["_post_data_unavailable"] = True
        LOG.warning(
            "post_data_unavailable",
            request_id=request_id,
            method=data.get("method"),
            url=data.get("url"),
            error=data.get("_post_data_error", "not fetched"),
            hint="The request sent a body that CDP did not inline and would not return",
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
    return cookie_header_pairs(_header_values(headers, "cookie"))


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
    if response.get("_bodyFileReason"):
        # Why the body is in bodies/ rather than the HAR (HAR 1.2 custom field).
        entry["response"]["content"]["_bodyFileReason"] = response["_bodyFileReason"]
    not_captured = response.get("_bodyNotCaptured")
    if not_captured:
        # HAR 1.2 custom fields, as _bodyFile is: the body is not in the HAR
        # or in bodies/, and why.
        content = entry["response"]["content"]
        content["_bodyNotCaptured"] = True
        content["_bodyNotCapturedReason"] = str(not_captured)
    # Add POST data if present
    post_data = request_data.get("post_data")
    if post_data is not None:
        content_type = request_data.get("headers", {}).get("Content-Type", "")
        entry["request"]["postData"] = {
            "mimeType": content_type,
            "text": post_data,
        }
    elif request_data.get("_post_data_file"):
        # HAR 1.2 allows custom fields that start with an underscore, as _bodyFile does.
        entry["request"]["_postDataFile"] = request_data["_post_data_file"]
        entry["request"]["_postDataSize"] = request_data.get("_post_data_size", 0)
    elif request_data.get("_post_data_unavailable"):
        entry["request"]["_postDataNotCaptured"] = True
        omitted = request_data.get("_post_data_omitted")
        if omitted:
            entry["request"]["_postDataOmitted"] = dict(omitted)
    if request_data.get("_dom_snapshot"):
        # The page's DOM as the request was sent (HAR 1.2 custom fields, as
        # _postDataFile is): the file under the run, and the page's bare URL.
        entry["request"]["_domSnapshot"] = request_data["_dom_snapshot"]
        entry["request"]["_domSnapshotUrl"] = request_data.get("_dom_snapshot_url", "")
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
        record_request_bodies: bool = True,
    ) -> None:
        self._driver = driver
        self._max_body_size = max_body_size
        self._bodies_dir = bodies_dir
        # False for a capture whose HAR is never written (the login engine's
        # header-only capture): no request body is kept, fetched or logged about.
        self._record_request_bodies = record_request_bodies
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
                    "post_data": None,
                    "has_post_data": request.get("hasPostData", False),
                    "timestamp": params.get("wallTime"),
                }
                if self._record_request_bodies:
                    _keep_post_data(
                        self._request_map[rid],
                        _inline_post_body(
                            request.get("postData"), _cdp_entries(request.get("postDataEntries"))
                        ),
                        max_body_size=self._max_body_size,
                        bodies_dir=self._bodies_dir,
                        request_id=rid,
                    )
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
                if self._record_request_bodies:
                    _note_lost_hop_body(request_id, data)
                continue
            # Fetch POST body if hasPostData but no inline postData. Selenium reads
            # the performance log only now, so this is the one chance to ask.
            if self._record_request_bodies and _post_data_missing(data):
                try:
                    result = self._driver.execute_cdp_cmd(
                        "Network.getRequestPostData", {"requestId": request_id}
                    )
                    _keep_post_data(
                        data,
                        _post_body((result.get("postData"), result.get("base64Encoded", False))),
                        max_body_size=self._max_body_size,
                        bodies_dir=self._bodies_dir,
                        request_id=request_id,
                    )
                except Exception as exc:  # noqa: BLE001 - CDP body fetch is best-effort
                    data["_post_data_error"] = f"{type(exc).__name__}: {exc}"

            # Fetch response body
            response = data.get("response", {})
            mime = response.get("mimeType", "")
            if _body_expected(data, response):
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
                    response["_bodyNotCaptured"] = f"fetch_failed: {type(exc).__name__}"
                    LOG.warning(
                        "selenium_response_body_fetch_failed",
                        request_id=request_id,
                        error=str(exc),
                    )
        if self._record_request_bodies:
            _flag_uncaptured_post_data(self._request_map)

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


def _capture_session_kwargs(tab: Any, session_id: Any) -> dict[str, Any]:
    """Send kwargs that address a body fetch to the CDP session capture enabled.

    On nodriver 0.50.6, ``Browser.get(url)`` (which ``gp observe`` uses for its
    first navigation) and ``Tab.get(url)`` each navigate and then call
    ``attach()`` again, which opens a second CDP session on the same target
    without detaching the first. Network events keep arriving from the first
    session (``Connection.process_event`` does not filter by session), but
    ``tab.send`` now addresses the second, whose Network agent was never
    enabled, and Chrome answers ``Network.getRequestPostData`` there with
    ``-32000 No resource with given id was found`` for every request (#269,
    measured on nodriver 0.50.6). nodriver 0.50 merges send kwargs into the
    CDP message, so ``sessionId`` sends the command to the session that saw the
    request. nodriver 0.48.1 has no session ids (each target has its own
    websocket) and its ``Browser.get`` does not re-attach (read from its source),
    so nothing is passed there and nothing needs to be; a live recording on
    0.48.1 on 2026-10-19 kept every body with no session named.
    """
    if not isinstance(session_id, str) or getattr(tab, "session_id", None) == session_id:
        return {}
    try:
        params = inspect.signature(tab.send).parameters.values()
    except (TypeError, ValueError):
        return {}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return {"sessionId": session_id}
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
        record_request_bodies: bool = True,
        dom_snapshots_dir: Path | None = None,
    ) -> None:
        self._browser = browser
        self._get_tab = get_tab
        self._max_body_size = max_body_size
        self._bodies_dir = bodies_dir
        # Set only by gp observe's own browser setup: a credential-shaped POST then
        # snapshots the page's DOM into this directory (_snapshot_dom). The login
        # engine's capture never passes it; it knows its selectors already.
        self._dom_snapshots_dir = dom_snapshots_dir if record_request_bodies else None
        self._dom_snapshot_attempts = 0
        self._dom_snapshot_failures = 0
        # Credential posts whose page was over DOM_SNAPSHOT_MAX_CHARS (a listener
        # snapshot's marker, or the late read).
        self._dom_snapshot_too_large = 0
        # Credential posts whose late DevTools read was of another page than theirs.
        self._dom_snapshot_late_skipped = 0
        self._dom_snapshotted: set[str] = set()
        # The page's own snapshots, newest last (_on_binding_called); one a credential
        # post takes is removed, so it is given to no other post.
        self._listener_snapshots: deque[_ListenerSnapshot] = deque(maxlen=DOM_SNAPSHOT_BUFFER)
        # False for a capture whose HAR is never written (the login engine's
        # header-only capture): no request body is kept, fetched or logged about.
        self._record_request_bodies = record_request_bodies
        # Set when stop has judged every request body; a request-time fetch that
        # has not started by then marks its entry instead of writing into it.
        self._post_data_closed = False
        self._request_map: dict[str, dict[str, Any]] = {}
        self._console_logs: list[dict[str, Any]] = []
        self._extra_info = _ExtraInfoCorrelator()
        self._warned_no_screenshots: bool = False
        self._bodies_fetched: set[str] = set()  # request IDs with eagerly-fetched bodies
        self._eager_fetch_attempts: int = 0
        self._eager_fetch_failures: int = 0
        # The CDP session Network.enable went to; see _capture_session_kwargs.
        self._capture_session_id: str | None = None
        # Request-time body fetches still running; stop_capture_async settles them.
        self._post_data_tasks: set[asyncio.Task[Any]] = set()

    @property
    def _tab(self) -> Any | None:
        """Get the current tab via the get_tab callable."""
        return self._get_tab() if self._get_tab else None

    def start_capture(self) -> None:
        """Begin capturing browser data."""
        self._extra_info = _ExtraInfoCorrelator()  # never carry slots across captures
        self._post_data_closed = False
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
        self._capture_session_id = getattr(tab, "session_id", None)
        tab.add_handler(network.RequestWillBeSent, self._on_request)
        # After _on_request: nodriver calls sync handlers inline and runs async ones
        # as tasks, so the entry this fills in already exists. It fetches only for
        # an entry _on_request marked pending, which a header-only capture never does.
        tab.add_handler(network.RequestWillBeSent, self._on_request_post_data)
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

        if self._dom_snapshots_dir is not None:
            await self._install_dom_listener(tab)

        # Console log capture
        await tab.send(cdp_runtime.enable())
        tab.add_handler(cdp_runtime.ConsoleAPICalled, self._on_console)

        LOG.debug("nodriver_capture_async_started")

    async def stop_capture_async(self) -> None:
        """Stop capturing and fetch request/response bodies asynchronously."""
        tab = self._tab
        if tab is None:
            self._post_data_closed = True
            if self._record_request_bodies:
                _flag_uncaptured_post_data(self._request_map)
            self._mark_unfetched_response_bodies("no_tab")
            return

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

        try:
            await self._settle_request_time_fetches()
            if (
                self._dom_snapshot_failures
                or self._dom_snapshot_too_large
                or self._dom_snapshot_late_skipped
            ):
                LOG.warning(
                    "nodriver_dom_snapshot_summary",
                    failed=self._dom_snapshot_failures,
                    attempted=self._dom_snapshot_attempts,
                    too_large=self._dom_snapshot_too_large,
                    late_skipped=self._dom_snapshot_late_skipped,
                    hint=(
                        "Of the credential posts attempted, the page's DOM was not saved "
                        "for those failed (an error, or cancelled at stop), too_large (a "
                        "page over 5 MB), or late_skipped (no listener snapshot, and the "
                        "late DevTools read was of another page than the one that sent "
                        "the post, or no sending page was known, so none was read). The "
                        "HAR is complete. Run with debug logging to see each request and "
                        "why."
                    ),
                )
            await self._fetch_bodies_at_stop(tab)
        finally:
            # Whatever ended stop, no request-time fetch writes after the marks: a
            # running one is cancelled, and one not yet started sees the flag.
            self._post_data_closed = True
            for task in self._post_data_tasks:
                task.cancel()
            if self._record_request_bodies:
                _flag_uncaptured_post_data(self._request_map)

    async def _settle_request_time_fetches(self) -> None:
        """Let the request-time body fetches still running finish first.

        Otherwise a body still arriving is retried, flagged and logged as
        ``post_data_unavailable`` at stop, and then written anyway. A fetch still
        running after the wait is cancelled here, before the stop-time pass, so it
        cannot answer beside that pass's own retry and store the body twice;
        ``stop_capture_async``'s ``finally`` cancels again for a stop that ends
        early.
        """
        # nodriver creates a fetch task for each event and does not keep it; a task
        # joins _post_data_tasks on its first step. One pass of the loop lets every
        # task already scheduled take that step before the snapshot.
        await asyncio.sleep(0)
        pending = {task for task in self._post_data_tasks if not task.done()}
        if not pending:
            return
        _done, still_running = await asyncio.wait(pending, timeout=_POST_DATA_SETTLE_TIMEOUT_S)
        if not still_running:
            return
        for task in still_running:
            task.cancel()
        # Each cancelled task runs its own cleanup (a snapshot counts itself) first,
        # for at most _STOP_WAIT_S: one stuck on a DevTools reply is left behind.
        _done, unfinished = await asyncio.wait(still_running, timeout=_STOP_WAIT_S)
        LOG.warning("nodriver_request_time_post_data_fetch_cancelled", count=len(still_running))
        if unfinished:
            LOG.warning("nodriver_request_time_tasks_unfinished", count=len(unfinished))

    def _mark_unfetched_response_bodies(self, reason: str) -> None:
        """Mark each response body stop will not fetch now, so none is left out unsaid.

        A body already in the HAR or ``bodies/``, or already marked, is left as is.
        """
        for request_id, data in self._request_map.items():
            response = data.get("response")
            if (
                response is None
                or _is_redirect_hop(data)
                or request_id in self._bodies_fetched
                or not _body_expected(data, response)
                or "body" in response
                or response.get("_bodyFile")
                or response.get("_bodyNotCaptured")
            ):
                continue
            response["_bodyNotCaptured"] = reason

    async def _fetch_bodies_at_stop(self, tab: Any) -> None:
        """Fetch what the event handlers did not: request bodies the request-time
        fetch missed, and response bodies the eager fetch missed.

        Returns early, leaving the rest unfetched, once the browser is gone.
        """
        import nodriver.cdp.network as cdp_net

        stop_failures = 0
        # The summary goes out however the loop ends, a disconnect included: the
        # per-request lines are debug lines, so it is the only warning about them.
        try:
            for request_id, data in list(self._request_map.items()):
                if _is_redirect_hop(data):
                    if self._record_request_bodies:
                        _note_lost_hop_body(request_id, data)
                    continue
                # Retry a request body the request-time fetch did not get.
                if self._record_request_bodies and _post_data_missing(data):
                    try:
                        result = await tab.send(
                            cdp_net.get_request_post_data(cdp_net.RequestId(request_id)),
                            **_capture_session_kwargs(tab, self._capture_session_id),
                        )
                        _keep_post_data(
                            data,
                            _post_body(result),
                            max_body_size=self._max_body_size,
                            bodies_dir=self._bodies_dir,
                            request_id=request_id,
                        )
                        data.pop("_post_data_error", None)
                    except ConnectionError as exc:
                        LOG.warning(
                            "nodriver_browser_disconnected_during_stop",
                            phase="post_data",
                            request_id=request_id,
                            error=str(exc),
                            exc_type=type(exc).__name__,
                        )
                        self._mark_unfetched_response_bodies("browser_disconnected")
                        return
                    except Exception as exc:  # noqa: BLE001 (CDP body fetch is best-effort)
                        data["_post_data_error"] = f"{type(exc).__name__}: {exc}"

                # Skip response body if already eagerly fetched by _on_loading_finished
                if request_id in self._bodies_fetched:
                    continue

                # Fetch response body (fallback for any missed by eager fetch)
                response = data.get("response", {})
                mime = response.get("mimeType", "")
                if _body_expected(data, response):
                    try:
                        body, base64_encoded = await tab.send(
                            cdp_net.get_response_body(cdp_net.RequestId(request_id)),
                            **_capture_session_kwargs(tab, self._capture_session_id),
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
                        self._mark_unfetched_response_bodies("browser_disconnected")
                        return
                    except Exception as exc:  # noqa: BLE001 (CDP body fetch is best-effort)
                        response["_bodyNotCaptured"] = f"fetch_failed: {type(exc).__name__}"
                        stop_failures += 1
                        # Per request, so debug; the count is one warning below (#275).
                        LOG.debug(
                            "nodriver_response_body_fetch_failed",
                            request_id=request_id,
                            url=data.get("url", "unknown"),
                            error=str(exc),
                        )
        finally:
            if stop_failures:
                LOG.warning(
                    "nodriver_response_body_fetch_summary",
                    failed=stop_failures,
                    hint=(
                        "These bodies are marked fetch_failed in the HAR. Run with debug "
                        "logging to see each request and its error."
                    ),
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
                "post_data": None,
                "has_post_data": getattr(event.request, "has_post_data", False),
                "timestamp": getattr(event, "wall_time", None),
                # The page that sent it: a DOM snapshot must come from its origin.
                "sender_url": _sending_page(event),
                "navigation": _is_navigation(event),
                "sender_origin_only": _origin_only(event),
            }
            if self._record_request_bodies:
                body = _inline_post_body(
                    getattr(event.request, "post_data", None),
                    _cdp_entries(getattr(event.request, "post_data_entries", None)),
                )
                _keep_post_data(
                    self._request_map[rid],
                    body,
                    max_body_size=self._max_body_size,
                    bodies_dir=self._bodies_dir,
                    request_id=rid,
                )
                if _post_data_missing(self._request_map[rid]):
                    self._request_map[rid]["_post_data_pending"] = True
                elif body is not None and self._snapshot_due(rid, self._request_map[rid], body):
                    self._start_snapshot(rid, self._request_map[rid], body.text)
            self._extra_info.entry_created(self._request_map, rid)
            if redirect is not None:
                self._extra_info.status_known(self._request_map, rid)
        except Exception:
            LOG.exception("nodriver_on_request_failed")

    async def _on_request_post_data(self, event: Any) -> None:
        """Fetch a request body CDP did not inline, while the request is in flight.

        ``Network.requestWillBeSent`` carries ``hasPostData`` without
        ``postData`` for a body Chrome does not inline (a Blob body, for one).
        Asking now rather than at stop time keeps a body Chrome no longer holds
        once the request finished or its page went away. A failure here is
        retried by ``stop_capture_async``; only a body both attempts miss is
        marked and logged as ``post_data_unavailable``.

        The task looks the entry up under the request id when it starts, and
        writes the body through that dict. A redirect that re-keys the entry as
        a hop after the fetch is sent still gets the hop's own body. A redirect
        processed before the task starts leaves the lookup on the successor,
        which carries no pending flag unless it too has a body CDP did not
        inline; the hop's body is then reported by ``_note_lost_hop_body``.
        """
        rid = str(getattr(event, "request_id", ""))
        data = self._request_map.get(rid)
        if data is None or not data.pop("_post_data_pending", False):
            return
        if self._post_data_closed:
            data["_post_data_error"] = "recording stopped before the body was fetched"
            _flag_uncaptured_post_data({rid: data})
            return
        tab = self._tab
        if tab is None:
            return
        task = asyncio.current_task()
        if task is not None:
            self._post_data_tasks.add(task)
        try:
            import nodriver.cdp.network as cdp_net

            result = await tab.send(
                cdp_net.get_request_post_data(cdp_net.RequestId(rid)),
                **_eager_send_kwargs(tab),
                **_capture_session_kwargs(tab, self._capture_session_id),
            )
            body = _post_body(result)
            _keep_post_data(
                data,
                body,
                max_body_size=self._max_body_size,
                bodies_dir=self._bodies_dir,
                request_id=rid,
            )
            # A body Chrome did not inline is known only now, later than the page's
            # state at the post: still the nearest moment there is.
            # The entry this task looked up when it started: a redirect reported
            # under the same request id meanwhile has re-keyed it as a hop, and the
            # snapshot still belongs to it.
            if body is not None and self._snapshot_due(rid, data, body):
                await self._snapshot_dom(tab, rid, data, body.text, self._claim_snapshot(rid, data))
        except Exception as exc:  # noqa: BLE001 - best-effort; stop_capture_async retries
            data["_post_data_error"] = f"{type(exc).__name__}: {exc}"
            LOG.debug(
                "nodriver_request_time_post_data_fetch_failed",
                request_id=rid,
                error=str(exc),
                exc_type=type(exc).__name__,
            )
        finally:
            self._post_data_tasks.discard(task)  # type: ignore[arg-type]

    def _snapshot_due(self, rid: str, data: dict[str, Any], body: _PostBody) -> bool:
        """Whether the request *rid*, whose own entry is *data*, with *body* gets a DOM
        snapshot: snapshots are on, it is a POST whose body is credential-shaped by
        the digest's own rule (:func:`graftpunk.har.digest.is_credential_body`: a
        password-like field and no new-password one), and none was taken for it yet.
        *data* is never looked up again by request id, which a redirect reuses."""
        if self._dom_snapshots_dir is None or body.text is None:
            return False
        if str(data.get("method", "")).upper() != "POST" or rid in self._dom_snapshotted:
            return False
        from graftpunk.har.digest import is_credential_body

        return is_credential_body(body.text, _request_content_type(data))

    def _claim_snapshot(self, rid: str, data: dict[str, Any]) -> _ListenerSnapshot | None:
        """Count a snapshot for *rid*, so no other is started for it, and take the
        listener snapshot that is its (:meth:`_take_listener_snapshot`), if any."""
        self._dom_snapshotted.add(rid)
        self._dom_snapshot_attempts += 1
        return self._take_listener_snapshot(data)

    def _start_snapshot(self, rid: str, data: dict[str, Any], body_text: str | None) -> None:
        """Bind a snapshot to the credential post *rid*'s own entry *data* now, while the
        request is being seen, and write it in a task. A redirect that CDP reports
        under the same request id before the task runs re-keys the entry as a hop,
        and the task still writes into the POST's dict. The body text lives only in
        that task, which stop cancels if it is still running."""
        picked = self._claim_snapshot(rid, data)
        tab = self._tab
        try:
            if tab is None or self._post_data_closed:
                raise RuntimeError("no tab, or the recording has stopped")
            task = asyncio.get_running_loop().create_task(
                self._snapshot_dom(tab, rid, data, body_text, picked)
            )
        except RuntimeError as exc:
            self._dom_snapshot_failures += 1
            LOG.debug("nodriver_dom_snapshot_not_attached", request_id=rid, error=str(exc))
            return
        self._post_data_tasks.add(task)
        task.add_done_callback(self._post_data_tasks.discard)

    async def _install_dom_listener(self, tab: Any) -> None:
        """Install the isolated-world listener (graftpunk.observe.dom_listener) that
        sends the page's DOM through a binding as the user acts. A page whose target
        refuses either command is still recorded, its credential posts snapshotted
        by DevTools calls alone (_snapshot_dom), and the failure is one warning."""
        import nodriver.cdp.page as cdp_page
        import nodriver.cdp.runtime as cdp_runtime

        from graftpunk.observe.dom_listener import BINDING_NAME, LISTENER_SCRIPT, WORLD_NAME

        tab.add_handler(cdp_runtime.BindingCalled, self._on_binding_called)
        try:
            await tab.send(
                cdp_runtime.add_binding(name=BINDING_NAME, execution_context_name=WORLD_NAME)
            )
            # Without the Page domain enabled, Chrome runs the script in no document
            # created after this (measured on 2026-10-24 with nodriver 0.50.6: it
            # ran only in the document open at the time and on about:blank).
            await tab.send(cdp_page.enable())
            await tab.send(
                cdp_page.add_script_to_evaluate_on_new_document(
                    source=LISTENER_SCRIPT, world_name=WORLD_NAME, run_immediately=True
                )
            )
        except Exception as exc:  # noqa: BLE001 - the recording goes on without it
            LOG.warning(
                "nodriver_dom_listener_unavailable",
                error=str(exc),
                exc_type=type(exc).__name__,
                hint="Credential posts are snapshotted by DevTools calls, which can be late.",
            )

    def _on_binding_called(self, event: Any) -> None:
        """Keep a DOM snapshot the page's listener sent, as the page serialized it,
        among the last ``DOM_SNAPSHOT_BUFFER``. A call to another binding, or a payload
        that is not the listener's (``html``, ``url``, ``time``), is dropped, and a
        page over ``DOM_SNAPSHOT_MAX_CHARS`` is kept as a marker without its HTML (a
        post that takes it is counted as too large; one no post takes costs nothing).
        Nothing is parsed here: nodriver calls this inline while it reads the
        browser's messages."""
        from graftpunk.observe.dom_listener import BINDING_NAME

        if getattr(event, "name", None) != BINDING_NAME:
            return
        try:
            payload = json.loads(getattr(event, "payload", ""))
            html, url, time_ms = payload["html"], payload["url"], payload["time"]
            if (
                not isinstance(html, str)
                or not isinstance(url, str)
                or isinstance(time_ms, bool)
                or not isinstance(time_ms, int | float)
            ):
                raise ValueError("not a listener snapshot")
        except Exception as exc:  # noqa: BLE001 - a bad payload costs only itself
            LOG.debug("nodriver_dom_snapshot_payload_dropped", error=str(exc))
            return
        # A page over the cap is kept as a marker with no HTML: the post that takes it
        # is counted as too large, once, and a click no post follows costs nothing.
        kept = html if len(html) <= DOM_SNAPSHOT_MAX_CHARS else None
        self._listener_snapshots.append(_ListenerSnapshot(float(time_ms), url, kept))

    @staticmethod
    def _sender(data: dict[str, Any]) -> str | None:
        """The URL of the page that sent the request *data*, or None for a navigation
        that names none (no Referer, and an initiator with no URL)."""
        if data.get("sender_url"):
            return str(data["sender_url"])
        return None if data.get("navigation") else data.get("url")

    def _take_listener_snapshot(self, data: dict[str, Any]) -> _ListenerSnapshot | None:
        """Remove and return the latest snapshot the page's listener sent no later than
        the request *data* was sent (its ``wallTime``), and no more than
        ``DOM_SNAPSHOT_MAX_AGE_MS`` before it, from the origin of the page that sent
        it; None when none is. A navigation that names no sending page
        (:meth:`_sender`) takes the newest such snapshot from any origin: a form post
        from a page with a strict ``Referrer-Policy``."""
        wall_ms = _wall_time_ms(data.get("timestamp"))
        sender = self._sender(data)
        origin = _origin(sender) if sender is not None else None
        if wall_ms is None or (origin is None and sender is not None):
            return None
        for snapshot in reversed(self._listener_snapshots):
            if wall_ms - DOM_SNAPSHOT_MAX_AGE_MS <= snapshot.time_ms <= wall_ms and (
                sender is None or _origin(snapshot.url) == origin
            ):
                self._listener_snapshots.remove(snapshot)
                return snapshot
        return None

    async def _read_dom(self, tab: Any) -> tuple[str, str]:
        """The page's document URL and HTML, by ``DOM.getDocument(depth=-1,
        pierce=True)`` then ``DOM.getOuterHTML`` of the document, as nodriver's
        ``Tab.get_content`` reads the page source; then ``DOM.disable``, since
        ``getDocument`` turns on the DOM domain's events for the target."""
        import nodriver.cdp.dom as cdp_dom

        session = _capture_session_kwargs(tab, self._capture_session_id)
        try:
            document = await tab.send(cdp_dom.get_document(depth=-1, pierce=True), **session)
            html = await tab.send(
                cdp_dom.get_outer_html(backend_node_id=document.backend_node_id), **session
            )
        finally:
            # Bounded: this also runs when stop cancels a read the page never answered.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(tab.send(cdp_dom.disable(), **session), _DOM_DISABLE_WAIT_S)
        return getattr(document, "document_url", None) or "", html

    async def _late_page(
        self, tab: Any, rid: str, data: dict[str, Any]
    ) -> _ListenerSnapshot | None:
        """The page by DevTools (:meth:`_read_dom`), for a post no listener snapshot
        was taken for: after the request left, so the page's response handler may
        have changed it. A listener snapshot that arrived while DevTools answered is
        taken instead. None, counted, for a page over ``DOM_SNAPSHOT_MAX_CHARS`` or
        another page than the one that sent the post: by scheme, host, and path (a
        redirect or a navigation has moved the tab on), or by origin when the sending
        page is known only as one (an origin-only Referer). With no sending page known
        nothing is read, as there is nothing to check the read against: the post is
        counted as late-skipped. A read that fails raises."""
        sender = self._sender(data)
        if sender is None:
            picked = self._take_listener_snapshot(data)
            if picked is not None:
                return picked
            self._dom_snapshot_late_skipped += 1
            LOG.debug(
                "nodriver_dom_snapshot_late_skipped", request_id=rid, reason="no_sending_page"
            )
            return None
        try:
            url, html = await self._read_dom(tab)
        except Exception:
            picked = self._take_listener_snapshot(data)
            if picked is None:
                raise
            return picked
        picked = self._take_listener_snapshot(data)
        if picked is not None:
            return picked
        same_page = (
            _origin(url) == _origin(sender)
            if data.get("sender_origin_only")
            else _bare_page_url(url) == _bare_page_url(sender)
        )
        if not same_page:
            self._dom_snapshot_late_skipped += 1
            LOG.debug("nodriver_dom_snapshot_late_skipped", request_id=rid, reason="another_page")
            return None
        if len(html) > DOM_SNAPSHOT_MAX_CHARS:
            self._dom_snapshot_too_large += 1
            LOG.debug("nodriver_dom_snapshot_too_large", request_id=rid, chars=len(html))
            return None
        LOG.debug(
            "nodriver_dom_snapshot_late",
            request_id=rid,
            hint="No snapshot from the page's listener; read by DevTools after the request.",
        )
        return _ListenerSnapshot(0.0, url, html)

    async def _snapshot_dom(
        self,
        tab: Any,
        rid: str,
        data: dict[str, Any],
        body_text: str | None,
        picked: _ListenerSnapshot | None,
    ) -> None:
        """Write the credential post *rid*'s snapshot: *picked*, the page's listener's
        snapshot taken before the page's handlers ran, else the late DevTools read
        (:meth:`_late_page`). It is sanitized and redacted of the post's own values
        (:func:`graftpunk.observe.snapshot.stored_dom_snapshot`) in a worker thread,
        written owner-only as ``snapshots/<rid>.html``, and linked from *data*, the
        post's own entry. Nothing waits for it: the request and the page go on, and
        a snapshot that fails, or is cancelled at stop, is one debug line and a count
        in the stop summary, never a change to the entry."""
        snapshots_dir = self._dom_snapshots_dir
        if snapshots_dir is None:
            return
        name = f"{rid}.html"
        try:
            from graftpunk.observe.snapshot import stored_dom_snapshot
            from graftpunk.observe.storage import make_private_dir, write_private_file

            page = picked or await self._late_page(tab, rid, data)
            if page is None:
                return
            if page.html is None:
                self._dom_snapshot_too_large += 1
                LOG.debug("nodriver_dom_snapshot_too_large", request_id=rid)
                return
            stored = await asyncio.to_thread(
                stored_dom_snapshot, page.html, body_text, _request_content_type(data)
            )
            snapshots_dir.parent.mkdir(parents=True, exist_ok=True)
            make_private_dir(snapshots_dir)
            write_private_file(snapshots_dir / name, stored)
        except asyncio.CancelledError:
            self._dom_snapshot_failures += 1
            LOG.debug("nodriver_dom_snapshot_cancelled", request_id=rid)
            raise
        except Exception as exc:  # noqa: BLE001 - best-effort; the HAR never depends on it
            self._dom_snapshot_failures += 1
            LOG.debug(
                "nodriver_dom_snapshot_failed",
                request_id=rid,
                error=str(exc),
                exc_type=type(exc).__name__,
            )
            return
        data["_dom_snapshot"] = f"{snapshots_dir.name}/{name}"
        data["_dom_snapshot_url"] = _bare_page_url(page.url)

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
            if not _body_expected(data, response):
                return

            tab = self._tab
            if tab is None:
                return

            self._eager_fetch_attempts += 1
            body, base64_encoded = await tab.send(
                cdp_net.get_response_body(cdp_net.RequestId(rid)),
                **_eager_send_kwargs(tab),
                **_capture_session_kwargs(tab, self._capture_session_id),
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
            # Debug, not warning: it fires per request (every script, style and
            # image), and hundreds of these buried a login's verdict (#275). The
            # count goes out as one warning when the capture stops.
            LOG.debug(
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
    record_request_bodies: bool = True,
) -> CaptureBackend:
    """Factory to create the appropriate capture backend.

    Args:
        backend_type: Either "nodriver" or "selenium".
        driver: The browser driver/browser instance.
        get_tab: Optional callable that returns the current nodriver tab.
        max_body_size: Largest request or response body kept in the HAR itself
            (bytes); a larger or binary body goes to ``bodies_dir``.
        bodies_dir: Directory to stream large/binary request and response bodies to
            disk. Without one, such a request body is left out and marked.
        record_request_bodies: False for a capture whose HAR is never written:
            request bodies are then neither kept, fetched nor logged about.

    Returns:
        A CaptureBackend implementation.
    """
    if backend_type == "nodriver":
        return NodriverCaptureBackend(
            driver,
            get_tab=get_tab,
            max_body_size=max_body_size,
            bodies_dir=bodies_dir,
            record_request_bodies=record_request_bodies,
        )
    if backend_type == "selenium":
        if not _HAS_SELENIUM:
            msg = f"Selenium is not installed. Install it with: {install_command('browser')}"
            raise ImportError(msg)
        return SeleniumCaptureBackend(
            driver,
            max_body_size=max_body_size,
            bodies_dir=bodies_dir,
            record_request_bodies=record_request_bodies,
        )
    msg = f"Unknown capture backend type: {backend_type!r}. Must be 'selenium' or 'nodriver'."
    raise ValueError(msg)
