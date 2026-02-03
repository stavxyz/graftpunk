"""HAR file parser.

Parses HAR (HTTP Archive) format files into structured Python objects
for analysis.

HAR format specification: http://www.softwareishard.com/blog/har-12-spec/
"""

from __future__ import annotations

import json
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
            "value": cookie.get("value", ""),
        }
        # Include optional attributes if present
        for attr in ["path", "domain", "expires", "httpOnly", "secure"]:
            if attr in cookie:
                parsed[attr] = cookie[attr]
        result.append(parsed)
    return result


_TEXT_CONTENT_KEYWORDS = ("json", "html", "text", "xml", "javascript", "css")


def _is_text_content(mime: str) -> bool:
    """Check if a MIME type represents text content."""
    mime_lower = mime.lower()
    return any(kw in mime_lower for kw in _TEXT_CONTENT_KEYWORDS)


def _parse_request(request_data: dict[str, Any]) -> HARRequest:
    """Parse request section of HAR entry.

    Args:
        request_data: Request dict from HAR entry.

    Returns:
        Parsed HARRequest object.
    """
    post_data = None
    if "postData" in request_data:
        post_data_obj = request_data["postData"]
        if isinstance(post_data_obj, dict):
            post_data = post_data_obj.get("text", "")
        elif isinstance(post_data_obj, str):
            post_data = post_data_obj

    return HARRequest(
        method=request_data.get("method", "GET"),
        url=request_data.get("url", ""),
        headers=_parse_headers(request_data.get("headers", [])),
        cookies=_parse_cookies(request_data.get("cookies", [])),
        post_data=post_data,
        query_string=request_data.get("queryString", []),
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
    headers = _parse_headers(response_data.get("headers", []))
    content_type = headers.get("Content-Type") or headers.get("content-type")

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
    )


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
            request = _parse_request(entry_data.get("request", {}))
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
            required HAR structure).
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
        raise HARParseError(f"Invalid JSON in HAR file: {exc}") from exc

    validate_har_schema(data)
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
