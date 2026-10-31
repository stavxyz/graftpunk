"""HAR file parser.

Parses HAR (HTTP Archive) format files into structured Python objects
for analysis.

HAR format specification: http://www.softwareishard.com/blog/har-12-spec/
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from graftpunk.har.selectors import MAX_SHOWN_FIELDS, bounded_selector
from graftpunk.logging import get_logger

LOG = get_logger(__name__)


class HARParseError(Exception):
    """Raised when HAR file cannot be parsed."""


# The interaction kinds gp observe's page listener records (graftpunk.observe.dom_listener).
_INTERACTION_KINDS = frozenset({"click", "focus"})


@dataclass(frozen=True)
class LoginInteraction:
    """A click or a focus move the page's listener recorded before a credential post
    (``request._loginInteractions``, #267): its kind, the element's selector (None
    when the page had no unique one, or it held an account value), and how many
    milliseconds before the post it happened. ``in_login`` marks a click the page
    saw inside the login itself (on a field's label, or in a container holding a
    shown password input), which is never the click that opened it."""

    kind: str
    selector: str | None
    ms_before: float
    in_login: bool = False
    # A click's state as the page saw it: on a control (a link, a button, a
    # button-like input, [role=button], or summary), inside a dialog, and on a
    # control the click before it did not show. Each True only when the HAR says
    # true.
    control: bool = False
    in_dialog: bool = False
    revealed: bool = False
    # The selectors of the fields shown on the page at a click, at most
    # MAX_SHOWN_FIELDS, each within MAX_SELECTOR_LENGTH.
    shown_fields: tuple[str, ...] = ()
    # The page's path when it happened (no query or fragment), or None.
    path: str | None = None


@dataclass
class HARRequest:
    """Parsed HTTP request from HAR entry."""

    method: str
    url: str
    headers: dict[str, str]
    # {"name", "value", ...}; "value" is None when the HAR entry has no value field.
    cookies: list[dict[str, Any]]
    post_data: str | None = None
    query_string: list[dict[str, str]] = field(default_factory=list)
    # HAR's own postData.mimeType: what the request declared its body to be,
    # for captures that carry no Content-Type request header.
    post_data_mime_type: str | None = None
    # The recorder saw the request announce a body (CDP hasPostData) and does
    # not hold it: the capture's request._postDataNotCaptured. A request with no
    # post_data, no post_data_file and this False sent no body.
    post_data_not_captured: bool = False
    # Why the body is not held, when the recorder left it out on purpose
    # (request._postDataOmitted.reason): "over_max_body_size" or "binary". None
    # when it was not left out, including a body the recorder could not get.
    post_data_omitted: str | None = None
    # A body the recorder wrote to bodies/ instead of the HAR
    # (request._postDataFile), relative to the HAR's directory. post_data holds
    # its text when the file is UTF-8 and was found under that directory; a file
    # that is not UTF-8 sets post_data_binary; a file that is missing, unreadable
    # or outside that directory sets post_data_not_captured.
    post_data_file: str | None = None
    post_data_binary: bool = False
    # The body's size in bytes as the recorder wrote it (request._postDataSize).
    post_data_size: int | None = None
    # The page's DOM as the request was sent, which gp observe saves for a
    # credential-shaped POST (request._domSnapshot, relative to the HAR's
    # directory): the reference, kept only when it names a file under that
    # directory, and the file's text when it is UTF-8. dom_snapshot_url is the page
    # it was taken on (request._domSnapshotUrl).
    dom_snapshot_file: str | None = None
    dom_snapshot: str | None = None
    dom_snapshot_url: str | None = None
    # The page gp observe read a few seconds after a credential post's response,
    # structure only (request._domAfter, relative to the HAR's directory): the
    # reference and the file's text, read as dom_snapshot is.
    dom_after_file: str | None = None
    dom_after: str | None = None
    # The clicks and focus moves gp observe recorded before a credential post, oldest
    # first (request._loginInteractions); an item of another shape is skipped.
    login_interactions: tuple[LoginInteraction, ...] = ()
    # The credential post the user marked as a wrong password in gp observe
    # interactive --wrong-password-first (request._userMarkedFailed is true), and the
    # failure texts the page after it suggests (request._failureTextCandidates):
    # strings of FAILURE_TEXT_LENGTH characters, at most MAX_FAILURE_TEXTS.
    user_marked_failed: bool = False
    failure_text_candidates: tuple[str, ...] = ()
    # The request's CDP resource type as the recorder saw it (request._resourceType):
    # "Document" for a navigation. None when the HAR does not say.
    resource_type: str | None = None
    # The value of every Cookie header, in any case, from the raw HAR list: the
    # headers dict keeps one header of each spelling, and a HAR from another
    # recorder can split the cookies over several headers.
    cookie_headers: tuple[str, ...] = ()


@dataclass
class HARResponse:
    """Parsed HTTP response from HAR entry."""

    status: int
    status_text: str
    headers: dict[str, str]
    # {"name", "value", ...}; "value" is None when the HAR entry has no value field.
    cookies: list[dict[str, Any]]
    content_type: str | None = None
    body: str | None = None
    body_size: int = 0
    body_file: str | None = None  # relative path to body file on disk
    # names only, never values; a response can carry more than one Set-Cookie
    # header and the headers dict above collapses duplicates to the last one
    set_cookie_names: tuple[str, ...] = ()
    # names a Set-Cookie header line sets with the HttpOnly attribute, read from
    # the raw headers: a HAR's response.cookies array often omits httpOnly even
    # when the header carries it, so the array alone cannot say a cookie was not
    # HttpOnly
    set_cookie_http_only: tuple[str, ...] = ()
    # the value of every Set-Cookie header, in any case, from the raw headers
    # list, for the same reason as set_cookie_names
    set_cookie_headers: tuple[str, ...] = ()
    # (name, attributes) for every cookie a Set-Cookie header line sets, in order,
    # attribute names in lower case (_set_cookie_lines); never a cookie's value
    set_cookie_attributes: tuple[tuple[str, dict[str, str]], ...] = ()
    # the _bodyFile resolves outside the HAR's directory (a reference or a
    # symlink that leaves it), so the body was not read
    body_file_outside: bool = False
    # where a redirect sent the client: the HAR's own redirectURL when the
    # recorder filled it in, else the Location header, else empty
    redirect_url: str = ""


@dataclass
class HAREntry:
    """Single request/response pair from HAR file."""

    request: HARRequest
    response: HARResponse
    timestamp: datetime
    time_ms: float = 0.0  # Total time in milliseconds


@dataclass
class ParseError:
    """Error encountered while parsing a single HAR entry.

    Attributes:
        index: Zero-based index of the entry in the HAR file's entries array.
        url: URL of the request that failed to parse, or "unknown" if unavailable.
        error: Human-readable error message describing the parse failure.
    """

    index: int
    url: str
    error: str


@dataclass
class HARParseResult:
    """Result of parsing a HAR file.

    Supports partial success: entries that fail to parse are recorded as
    errors while valid entries are still returned. This allows callers to
    process valid data while being aware of parsing failures.

    Attributes:
        entries: Successfully parsed HAR entries, in original file order.
        errors: Parse errors for entries that could not be processed.
    """

    entries: list[HAREntry]
    errors: list[ParseError] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        """Return True if any parse errors occurred."""
        return bool(self.errors)


def _parse_headers(headers_list: list[dict[str, str]]) -> dict[str, str]:
    """Convert HAR headers array to dict.

    Args:
        headers_list: List of {name, value} dicts from HAR.

    Returns:
        Dictionary mapping header names to values.
        Later values overwrite earlier ones for duplicate headers.
    """
    return {h["name"]: h.get("value", "") for h in headers_list if h.get("name")}


def _parse_cookies(cookies_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse cookies from HAR format.

    Args:
        cookies_list: List of cookie dicts from HAR.

    Returns:
        List of cookie dicts with name, value, and optional attributes.
    """
    result: list[dict[str, Any]] = []
    for cookie in cookies_list:
        parsed = {
            "name": cookie.get("name", ""),
            # A missing value is not an empty one: None marks a value the
            # recorder did not keep (graftpunk.har.digest reads it so).
            "value": cookie.get("value"),
        }
        # Include optional attributes if present
        for attr in ["path", "domain", "expires", "httpOnly", "secure", "sameSite"]:
            if attr in cookie:
                parsed[attr] = cookie[attr]
        result.append(parsed)
    return result


_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")


def _set_cookie_lines(headers_list: list[Any]) -> list[tuple[str, dict[str, str]]]:
    """``(name, attributes)`` for every cookie a ``Set-Cookie`` header in
    *headers_list* sets, in order: the one ``Set-Cookie`` parse of the package,
    which ``set_cookie_names``, ``set_cookie_http_only``, and
    ``set_cookie_attributes`` all read, so they cannot drift.

    Reads the raw headers list (not the ``_parse_headers`` dict), because that
    dict collapses duplicate header names to the last value and a response can
    carry more than one ``Set-Cookie`` header; a header is matched by name in
    any case, and one that is not an object, or whose name or value is not a
    string, is skipped. Each line of a header value is one cookie, since
    Chromium joins several ``Set-Cookie`` headers into one value with newlines;
    a line ends at ``\\r\\n``, a bare ``\\r`` or ``\\n``, and an empty line sets
    nothing. A cookie's name is before the first ``=`` of the part before the
    first ``;``; a line whose part holds no ``=`` sets no cookie. Its attributes
    are the ``;``-separated parts after it, keyed by the attribute name in lower
    case with the text after its first ``=`` stripped (empty for a flag such as
    ``HttpOnly``); a later attribute of one name replaces an earlier one, and a
    part with no name is skipped. The cookie's value is never returned.

    The standard library's ``http.cookies.SimpleCookie`` is not used: on Python
    3.12 it drops, without an error, a cookie whose value holds a space or is
    JSON text, or whose name holds a bracket, and it reads an
    attribute it does not know (``Priority=High``) as a new cookie of that name,
    so the attributes after it are given to a cookie no response set. It also
    reads one header, not Chromium's newline-joined lines. The parse here never
    reads the value, so a value of any shape cannot lose a cookie.
    """
    cookies: list[tuple[str, dict[str, str]]] = []
    for value in _header_values_named(headers_list, "set-cookie"):
        for line in _LINE_BREAK_RE.split(value):
            pair, *parts = line.split(";")
            if "=" not in pair:
                continue
            name = pair.split("=", 1)[0].strip()
            if not name:
                continue
            attributes: dict[str, str] = {}
            for part in parts:
                key, _sep, attribute_value = part.partition("=")
                key = key.strip().lower()
                if key:
                    attributes[key] = attribute_value.strip()
            cookies.append((name, attributes))
    return cookies


def cookie_header_pairs(values: Iterable[str]) -> list[dict[str, str]]:
    """``{"name", "value"}`` for every cookie the ``Cookie`` header *values* send,
    in order: every line of a value (Chromium joins several headers with line
    breaks, and HTTP/2 can split one into several), and every ``;``-separated
    ``name=value`` pair, name and value stripped. A pair with no ``=`` or no name
    is skipped (RFC 6265 lets a server send a nameless ``=value``; HAR has no
    representation for it). The recorder writes ``request.cookies`` with this
    and the parser falls back to it, so the two cannot drift."""
    cookies: list[dict[str, str]] = []
    for value in values:
        for line in _LINE_BREAK_RE.split(value):
            for pair in line.split(";"):
                name, sep, cookie_value = pair.partition("=")
                if sep and name.strip():
                    cookies.append({"name": name.strip(), "value": cookie_value.strip()})
    return cookies


def _header_values_named(headers_list: list[Any], wanted: str) -> list[str]:
    """The value of every header named *wanted* (lower case) in *headers_list*, the
    raw HAR list (the ``_parse_headers`` dict keeps only the last header of a
    name), in any case. A header that is not an object, or whose name or value is
    not a string, is skipped, since what reads these must never fail an entry
    that parses without it."""
    values: list[str] = []
    for header in headers_list:
        if not isinstance(header, dict):
            continue
        name, value = header.get("name"), header.get("value")
        if isinstance(name, str) and name.lower() == wanted and isinstance(value, str):
            values.append(value)
    return values


def _cookie_header_values(headers_list: list[Any]) -> list[str]:
    """The value of every ``Cookie`` header in *headers_list* (:func:`_header_values_named`)."""
    return _header_values_named(headers_list, "cookie")


def _parse_set_cookie_names(headers_list: list[Any]) -> tuple[str, ...]:
    """The NAME of every cookie a ``Set-Cookie`` header in *headers_list* sets,
    every line of a newline-joined value included (:func:`_set_cookie_lines`),
    in order, deduplicated."""
    return tuple(dict.fromkeys(name for name, _attributes in _set_cookie_lines(headers_list)))


def _parse_set_cookie_http_only(headers_list: list[Any]) -> tuple[str, ...]:
    """The NAME of every cookie a ``Set-Cookie`` header in *headers_list* sets with
    the ``HttpOnly`` attribute (:func:`_set_cookie_lines`), in order,
    deduplicated."""
    return tuple(
        dict.fromkeys(
            name for name, attributes in _set_cookie_lines(headers_list) if "httponly" in attributes
        )
    )


_TEXT_CONTENT_KEYWORDS = ("json", "html", "text", "xml", "javascript", "css")


def _is_text_content(mime: str) -> bool:
    """Check if a MIME type represents text content."""
    mime_lower = mime.lower()
    return any(kw in mime_lower for kw in _TEXT_CONTENT_KEYWORDS)


def _request_body_file(ref: str, base_dir: Path | None) -> tuple[str | None, bool]:
    """``(text, readable)`` for the request body file *ref* names.

    *text* is the file's text when it is UTF-8. *readable* is False when the
    file is missing, cannot be read, or resolves outside *base_dir* (a
    reference or a symlink that leaves the HAR's directory is not followed), so
    the body counts as not captured; ``(None, True)`` is a body that is not text.
    """
    if base_dir is None:
        return None, False
    root = base_dir.resolve()
    path = (root / ref).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None, False
    try:
        raw = path.read_bytes()
    except OSError:
        return None, False
    try:
        return raw.decode("utf-8"), True
    except UnicodeDecodeError:
        return None, True


def _shown_fields(raw: Any) -> tuple[str, ...]:
    """A click's ``shownFields``: the strings within ``MAX_SELECTOR_LENGTH``, at most
    ``MAX_SHOWN_FIELDS``; nothing when it is not a list."""
    if not isinstance(raw, list):
        return ()
    kept = (bounded_selector(field) for field in raw)
    return tuple(field for field in kept if field is not None)[:MAX_SHOWN_FIELDS]


def _login_interactions(raw: Any) -> tuple[LoginInteraction, ...]:
    """``request._loginInteractions`` as :class:`LoginInteraction` items, oldest first
    (the largest ``msBefore`` first). An item that is not an object with a known
    ``kind``, a string or null ``selector``, and a finite, non-negative ``msBefore``
    is skipped; a selector over ``graftpunk.har.selectors.MAX_SELECTOR_LENGTH``
    characters is read as None, and ``inLogin`` counts only when it is ``true``."""
    if not isinstance(raw, list):
        return ()
    kept: list[LoginInteraction] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind, selector, before = item.get("kind"), item.get("selector"), item.get("msBefore")
        if (
            kind not in _INTERACTION_KINDS
            or not (selector is None or isinstance(selector, str))
            or isinstance(before, bool)
            or not isinstance(before, int | float)
            or not math.isfinite(before)
            or before < 0
        ):
            continue
        kept.append(
            LoginInteraction(
                kind,
                bounded_selector(selector),
                float(before),
                in_login=item.get("inLogin") is True,
                control=item.get("control") is True,
                in_dialog=item.get("inDialog") is True,
                revealed=item.get("revealed") is True,
                shown_fields=_shown_fields(item.get("shownFields")),
                path=item.get("path") if isinstance(item.get("path"), str) else None,
            )
        )
    return tuple(sorted(kept, key=lambda each: -each.ms_before))


# The failure texts a recording may suggest: as many and as long as the recorder
# keeps (graftpunk.observe.failure_text), so a hand-edited HAR cannot widen them.
MAX_FAILURE_TEXTS = 3
FAILURE_TEXT_LENGTH = range(8, 201)


def _failure_texts(raw: Any) -> tuple[str, ...]:
    """``request._failureTextCandidates``: the strings of ``FAILURE_TEXT_LENGTH``
    characters, at most ``MAX_FAILURE_TEXTS``; nothing when it is not a list."""
    if not isinstance(raw, list):
        return ()
    kept = [text for text in raw if isinstance(text, str) and len(text) in FAILURE_TEXT_LENGTH]
    return tuple(kept[:MAX_FAILURE_TEXTS])


def _parse_request(request_data: dict[str, Any], base_dir: Path | None = None) -> HARRequest:
    """Parse request section of HAR entry.

    Args:
        request_data: Request dict from HAR entry.
        base_dir: Directory containing the HAR file, used to resolve
            _postDataFile references to request bodies written to disk.

    Returns:
        Parsed HARRequest object.
    """
    post_data = None
    post_data_mime_type = None
    if "postData" in request_data:
        post_data_obj = request_data["postData"]
        if isinstance(post_data_obj, dict):
            post_data = post_data_obj.get("text", "")
            post_data_mime_type = post_data_obj.get("mimeType")
        elif isinstance(post_data_obj, str):
            post_data = post_data_obj
    post_data_file = request_data.get("_postDataFile")
    post_data_binary = False
    file_unreadable = False
    if not isinstance(post_data_file, str):
        post_data_file = None
    elif post_data is None:
        post_data, readable = _request_body_file(post_data_file, base_dir)
        post_data_binary = readable and post_data is None
        file_unreadable = not readable
    size = request_data.get("_postDataSize")
    omitted = request_data.get("_postDataOmitted")
    omitted_reason = omitted.get("reason") if isinstance(omitted, dict) else None
    snapshot_file = request_data.get("_domSnapshot")
    snapshot: str | None = None
    if isinstance(snapshot_file, str):
        # The same refusal as a request body file's: a reference that leaves the
        # HAR's directory, or a file that is missing or not text, is not read.
        snapshot, readable = _request_body_file(snapshot_file, base_dir)
        if not readable:
            snapshot_file = None
    else:
        snapshot_file = None
    snapshot_url = request_data.get("_domSnapshotUrl")
    after_file = request_data.get("_domAfter")
    after: str | None = None
    if isinstance(after_file, str):
        after, readable = _request_body_file(after_file, base_dir)
        if not readable:
            after_file = None
    else:
        after_file = None
    resource_type = request_data.get("_resourceType")

    return HARRequest(
        method=request_data.get("method", "GET"),
        url=request_data.get("url", ""),
        headers=_parse_headers(request_data.get("headers", [])),
        # A recorder that leaves the array out (or empty) still kept the header,
        # and a cookie the request sends must still be seen (#274).
        cookies=_parse_cookies(request_data.get("cookies") or [])
        or cookie_header_pairs(_cookie_header_values(request_data.get("headers", []))),
        post_data=post_data,
        query_string=request_data.get("queryString", []),
        post_data_mime_type=post_data_mime_type,
        post_data_not_captured=request_data.get("_postDataNotCaptured") is True or file_unreadable,
        post_data_omitted=omitted_reason if isinstance(omitted_reason, str) else None,
        post_data_file=post_data_file,
        post_data_binary=post_data_binary,
        post_data_size=size if isinstance(size, int) and not isinstance(size, bool) else None,
        dom_snapshot_file=snapshot_file,
        dom_snapshot=snapshot,
        dom_snapshot_url=snapshot_url if isinstance(snapshot_url, str) else None,
        dom_after_file=after_file,
        dom_after=after,
        login_interactions=_login_interactions(request_data.get("_loginInteractions")),
        user_marked_failed=request_data.get("_userMarkedFailed") is True,
        failure_text_candidates=_failure_texts(request_data.get("_failureTextCandidates")),
        resource_type=resource_type if isinstance(resource_type, str) else None,
        cookie_headers=tuple(_cookie_header_values(request_data.get("headers", []))),
    )


def _parse_response(response_data: dict[str, Any], base_dir: Path | None = None) -> HARResponse:
    """Parse response section of HAR entry.

    Args:
        response_data: Response dict from HAR entry.
        base_dir: Directory containing the HAR file, used to resolve _bodyFile
            references to disk-streamed response bodies.

    Returns:
        Parsed HARResponse object.
    """
    raw_headers = response_data.get("headers", [])
    headers = _parse_headers(raw_headers)
    content_type = headers.get("Content-Type") or headers.get("content-type")
    set_cookie_names = _parse_set_cookie_names(raw_headers)
    set_cookie_http_only = _parse_set_cookie_http_only(raw_headers)

    # Extract body content
    body = None
    body_size = 0
    body_file = None
    body_file_outside = False
    content = response_data.get("content", {})
    if isinstance(content, dict):
        body = content.get("text")
        body_size = content.get("size", 0)
        body_file_ref = content.get("_bodyFile")

        # If body is stored on disk and not already inline, load it
        if body_file_ref and base_dir and body is None:
            body_file = body_file_ref
            root = base_dir.resolve()
            body_path = (root / body_file_ref).resolve()
            # The same refusal as a request body file's: a reference or a symlink
            # that leaves the HAR's directory is not read.
            body_file_outside = not body_path.is_relative_to(root)
            if not body_file_outside and body_path.exists():
                mime = content.get("mimeType", "")
                if _is_text_content(mime):
                    body = body_path.read_text(encoding="utf-8", errors="replace")
                else:
                    body = f"[binary file: {body_file_ref}]"
                body_size = body_path.stat().st_size
        elif body_file_ref:
            body_file = body_file_ref

    return HARResponse(
        status=response_data.get("status", 0),
        status_text=response_data.get("statusText", ""),
        headers=headers,
        cookies=_parse_cookies(response_data.get("cookies", [])),
        content_type=content_type,
        body=body,
        body_size=body_size,
        body_file=body_file,
        set_cookie_names=set_cookie_names,
        set_cookie_http_only=set_cookie_http_only,
        set_cookie_attributes=tuple(_set_cookie_lines(raw_headers)),
        set_cookie_headers=tuple(_header_values_named(raw_headers, "set-cookie")),
        body_file_outside=body_file_outside,
        redirect_url=_redirect_url(response_data, headers),
    )


def _redirect_url(response_data: dict[str, Any], headers: dict[str, str]) -> str:
    """Where this response sent the client, or an empty string.

    Chrome writes the target into the HAR's own ``redirectURL`` field; other
    recorders leave that empty and only carry the ``Location`` header, so both
    are read, in that order.
    """
    recorded = response_data.get("redirectURL")
    if isinstance(recorded, str) and recorded:
        return recorded
    for name, value in headers.items():
        if name.lower() == "location" and isinstance(value, str) and value:
            return value
    return ""


# The time an entry whose startedDateTime does not parse is given: it sorts such
# an entry first, and a reader comparing a time with it (the digest's cookie
# expiry) must treat it as no time at all.
UNPARSED_TIMESTAMP = datetime.min.replace(tzinfo=UTC)


def _parse_timestamp(started: str) -> datetime:
    """Parse ISO 8601 timestamp from HAR.

    Args:
        started: ISO 8601 timestamp string.

    Returns:
        Parsed datetime object.
    """
    # HAR timestamps are ISO 8601 format
    # Examples: "2023-01-15T10:30:00.000Z", "2023-01-15T10:30:00+00:00"
    try:
        # Try parsing with Z suffix
        if started.endswith("Z"):
            return datetime.fromisoformat(started.replace("Z", "+00:00"))
        return datetime.fromisoformat(started)
    except ValueError:
        # Use epoch as fallback - makes failures visible in chronological ordering
        LOG.warning("timestamp_parse_failed", timestamp=started)
        return UNPARSED_TIMESTAMP


def validate_har_schema(data: dict[str, Any]) -> None:
    """Validate HAR data has required structure.

    Args:
        data: Parsed JSON data from HAR file.

    Raises:
        HARParseError: If required fields are missing.
    """
    if not isinstance(data, dict):
        raise HARParseError("HAR file must contain a JSON object")

    if "log" not in data:
        raise HARParseError("HAR file must contain 'log' object")

    log = data["log"]
    if not isinstance(log, dict):
        raise HARParseError("'log' must be an object")

    if "entries" not in log:
        raise HARParseError("HAR log must contain 'entries' array")

    entries = log["entries"]
    if not isinstance(entries, list):
        raise HARParseError("'entries' must be an array")


def _parse_entries(data: dict[str, Any], base_dir: Path | None = None) -> HARParseResult:
    """Parse entries from validated HAR data.

    Args:
        data: Validated HAR data with log.entries.
        base_dir: Directory containing the HAR file, passed through to
            _parse_response for _bodyFile resolution.

    Returns:
        HARParseResult containing successfully parsed entries and any errors.
    """
    entries: list[HAREntry] = []
    errors: list[ParseError] = []

    for idx, entry_data in enumerate(data["log"]["entries"]):
        try:
            request = _parse_request(entry_data.get("request", {}), base_dir=base_dir)
            response = _parse_response(entry_data.get("response", {}), base_dir=base_dir)
            timestamp = _parse_timestamp(entry_data.get("startedDateTime", ""))
            time_ms = entry_data.get("time", 0.0)

            entries.append(
                HAREntry(
                    request=request,
                    response=response,
                    timestamp=timestamp,
                    time_ms=time_ms,
                )
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            # Collect error for caller and log for debugging
            request_data = entry_data.get("request") or {}
            if isinstance(request_data, dict):
                url = request_data.get("url", "unknown")
            else:
                url = "unknown"
            errors.append(ParseError(index=idx, url=url, error=str(exc)))
            LOG.warning(
                "entry_parse_failed",
                error=str(exc),
                entry_index=idx,
                url=url,
            )
            continue

    return HARParseResult(entries=entries, errors=errors)


def parse_har_file(filepath: Path | str) -> HARParseResult:
    """Parse HAR file and return structured result with entries and errors.

    Supports partial success: valid entries are returned even if some entries
    fail to parse. Check result.has_errors to see if any parsing failures occurred.

    Args:
        filepath: Path to HAR file.

    Returns:
        HARParseResult containing parsed entries and any errors.

    Raises:
        HARParseError: If file structure is invalid (not valid JSON or missing
            required HAR structure), the file is not valid UTF-8, or it cannot
            be read for another OS reason (for example, it is a directory).
        FileNotFoundError: If file does not exist.
    """
    filepath = Path(filepath)
    base_dir = filepath.parent

    if not filepath.exists():
        raise FileNotFoundError(f"HAR file not found: {filepath}")

    try:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise HARParseError(f"{filepath}: not valid JSON ({exc})") from exc
    except UnicodeDecodeError as exc:
        raise HARParseError(f"{filepath}: not valid UTF-8 ({exc})") from exc
    except OSError as exc:
        # FileNotFoundError is already reported above; anything else that open()
        # or read() can raise (a directory, a permission error) is this
        # function's business to name, not an uncaught traceback at the CLI.
        # exc.strerror, not str(exc): str(OSError) already ends with the path
        # (repr-quoted), which would print it twice after the f-string prefix.
        raise HARParseError(f"{filepath}: {exc.strerror or exc}") from exc

    try:
        validate_har_schema(data)
    except HARParseError as exc:
        # validate_har_schema's own messages carry no path; this is its one
        # caller that reads a file, so the path belongs here, not there.
        raise HARParseError(f"{filepath}: {exc}") from exc
    result = _parse_entries(data, base_dir=base_dir)

    LOG.info(
        "har_file_parsed",
        filepath=str(filepath),
        entries=len(result.entries),
        errors=len(result.errors),
    )
    return result


def parse_har_string(content: str) -> HARParseResult:
    """Parse HAR content from string and return structured result.

    Supports partial success: valid entries are returned even if some entries
    fail to parse. Check result.has_errors to see if any parsing failures occurred.

    Args:
        content: HAR file content as string.

    Returns:
        HARParseResult containing parsed entries and any errors.

    Raises:
        HARParseError: If content structure is invalid (not valid JSON or
            missing required HAR structure).
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise HARParseError(f"Invalid JSON in HAR content: {exc}") from exc

    validate_har_schema(data)
    return _parse_entries(data)
