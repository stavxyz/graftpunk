"""HAR file parser.

Parses HAR (HTTP Archive) format files into structured Python objects
for analysis.

HAR format specification: http://www.softwareishard.com/blog/har-12-spec/
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from graftpunk.logging import get_logger

LOG = get_logger(__name__)


class HARParseError(Exception):
    """Raised when HAR file cannot be parsed."""


@dataclass
class HARRequest:
    """Parsed HTTP request from HAR entry."""

    method: str
    url: str
    headers: dict[str, str]
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


@dataclass
class HARResponse:
    """Parsed HTTP response from HAR entry."""

    status: int
    status_text: str
    headers: dict[str, str]
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
        for attr in ["path", "domain", "expires", "httpOnly", "secure"]:
            if attr in cookie:
                parsed[attr] = cookie[attr]
        result.append(parsed)
    return result


_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")


def _set_cookie_lines(headers_list: list[dict[str, str]]) -> list[tuple[str, bool]]:
    """``(name, http_only)`` for every cookie a ``Set-Cookie`` header in
    *headers_list* sets, in order: the one parse both ``set_cookie_names`` and
    ``set_cookie_http_only`` read, so they cannot drift.

    Reads the raw headers list (not the ``_parse_headers`` dict), because that
    dict collapses duplicate header names to the last value and a response can
    carry more than one ``Set-Cookie`` header. Each line of a header value is
    one cookie, since Chromium joins several ``Set-Cookie`` headers into one
    value with newlines; a line ends at ``\\r\\n``, a bare ``\\r`` or ``\\n``,
    and an empty line sets nothing. A cookie's name is before the first ``=`` of the part
    before the first ``;``; a line whose part holds no ``=`` sets no cookie. Its
    attributes are the ``;``-separated parts after it, matched by attribute name
    without regard to case. Values are never returned.
    """
    cookies: list[tuple[str, bool]] = []
    for header in headers_list:
        if header.get("name", "").lower() != "set-cookie":
            continue
        for line in _LINE_BREAK_RE.split(header.get("value", "")):
            pair, *attributes = line.split(";")
            if "=" not in pair:
                continue
            name = pair.split("=", 1)[0].strip()
            if not name:
                continue
            http_only = any(
                attribute.split("=", 1)[0].strip().lower() == "httponly" for attribute in attributes
            )
            cookies.append((name, http_only))
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


def _cookie_header_values(headers_list: list[Any]) -> list[str]:
    """The value of every ``Cookie`` header in *headers_list*, the raw HAR list (the
    ``_parse_headers`` dict keeps only the last header of a name), in any case.
    A header whose name or value is not a string is skipped, since the fallback
    that reads these must never fail an entry that parses without it."""
    values: list[str] = []
    for header in headers_list:
        name, value = header.get("name"), header.get("value")
        if isinstance(name, str) and name.lower() == "cookie" and isinstance(value, str):
            values.append(value)
    return values


def _parse_set_cookie_names(headers_list: list[dict[str, str]]) -> tuple[str, ...]:
    """The NAME of every cookie a ``Set-Cookie`` header in *headers_list* sets,
    every line of a newline-joined value included (:func:`_set_cookie_lines`),
    in order, deduplicated."""
    return tuple(dict.fromkeys(name for name, _http_only in _set_cookie_lines(headers_list)))


def _parse_set_cookie_http_only(headers_list: list[dict[str, str]]) -> tuple[str, ...]:
    """The NAME of every cookie a ``Set-Cookie`` header in *headers_list* sets with
    the ``HttpOnly`` attribute (:func:`_set_cookie_lines`), in order,
    deduplicated."""
    return tuple(
        dict.fromkeys(name for name, http_only in _set_cookie_lines(headers_list) if http_only)
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
    content = response_data.get("content", {})
    if isinstance(content, dict):
        body = content.get("text")
        body_size = content.get("size", 0)
        body_file_ref = content.get("_bodyFile")

        # If body is stored on disk and not already inline, load it
        if body_file_ref and base_dir and body is None:
            body_file = body_file_ref
            body_path = base_dir / body_file_ref
            if body_path.exists():
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
        return datetime.min.replace(tzinfo=UTC)


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
