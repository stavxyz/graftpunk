"""Backend-specific browser capture implementations."""

from __future__ import annotations

import base64
import contextlib
import datetime
import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from graftpunk.logging import get_logger

try:
    import selenium.common.exceptions
except ImportError:
    selenium = None  # type: ignore[assignment]

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


def _build_har_entry(request_data: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Build a HAR 1.2 entry dict from correlated request/response data."""
    response = request_data.get("response", {})
    entry: dict[str, Any] = {
        "startedDateTime": _wall_time_to_iso(request_data.get("timestamp")),
        "time": 0,
        "request": {
            "method": request_data.get("method", "GET"),
            "url": request_data.get("url", ""),
            "headers": [
                {"name": k, "value": v} for k, v in request_data.get("headers", {}).items()
            ],
            "cookies": [],
            "queryString": [],
        },
        "response": {
            "status": response.get("status", 0),
            "statusText": response.get("statusText", ""),
            "headers": [{"name": k, "value": v} for k, v in response.get("headers", {}).items()],
            "cookies": [],
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

    def get_header_profiles(self) -> dict[str, dict[str, str]]:
        """Return header profiles classified from captured requests."""
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

    def start_capture(self) -> None:
        """Begin capturing browser data."""
        LOG.debug("selenium_capture_started")

    def stop_capture(self) -> None:
        """Stop capturing, parse performance log CDP events, and fetch bodies.

        Parses Network.requestWillBeSent, Network.responseReceived, and
        Runtime.consoleAPICalled events from the performance log. Then fetches
        POST bodies and response bodies via CDP commands.
        """
        # Parse performance log for network and console events
        try:
            perf_log = self._driver.get_log("performance")
        except selenium.common.exceptions.WebDriverException as exc:
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
                # Note: CDP reuses request_id for redirect chains. We
                # intentionally capture only the final destination request data.
                self._request_map[rid] = {
                    "url": request.get("url", ""),
                    "method": request.get("method", "GET"),
                    "headers": request.get("headers", {}),
                    "post_data": request.get("postData"),
                    "has_post_data": request.get("hasPostData", False),
                    "timestamp": params.get("wallTime"),
                }
            elif method == "Network.responseReceived":
                response = params.get("response", {})
                rid = params.get("requestId", "")
                if rid not in self._request_map:
                    self._request_map[rid] = {
                        "url": response.get("url", ""),
                        "method": "GET",
                        "headers": {},
                        "timestamp": None,
                        "has_post_data": False,
                        "post_data": None,
                    }
                self._request_map[rid]["response"] = {
                    "status": response.get("status", 0),
                    "statusText": response.get("statusText", ""),
                    "headers": response.get("headers", {}),
                    "mimeType": response.get("mimeType", ""),
                }
            elif method == "Runtime.consoleAPICalled":
                args = params.get("args", [])
                self._console_logs.append(
                    {
                        "level": params.get("type", "log"),
                        "args": [arg.get("value", str(arg)) for arg in args],
                        "timestamp": params.get("timestamp", time.time()),
                    }
                )

        # Fetch bodies for all captured requests
        for request_id, data in list(self._request_map.items()):
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
            self._console_logs.extend(browser_logs)
        except selenium.common.exceptions.WebDriverException as exc:
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
        except selenium.common.exceptions.WebDriverException as exc:
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
        return [_build_har_entry(data, rid) for rid, data in self._request_map.items()]

    def get_console_logs(self) -> list[dict[str, Any]]:
        """Return collected browser console logs.

        Returns:
            List of console log entry dicts.
        """
        return self._console_logs

    def get_header_profiles(self) -> dict[str, dict[str, str]]:
        """Return header profiles classified from captured network requests."""
        from graftpunk.observe.headers import extract_header_profiles

        return extract_header_profiles(self._request_map)

    async def take_screenshot(self) -> bytes | None:
        """Take a screenshot asynchronously (wraps sync method)."""
        return self.take_screenshot_sync()

    async def get_page_source(self) -> str | None:
        """Get the current page HTML source asynchronously."""
        try:
            return self._driver.page_source
        except selenium.common.exceptions.WebDriverException as exc:
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
        self._warned_no_screenshots: bool = False

    @property
    def _tab(self) -> Any | None:
        """Get the current tab via the get_tab callable."""
        return self._get_tab() if self._get_tab else None

    def start_capture(self) -> None:
        """Begin capturing browser data."""
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
        return [_build_har_entry(data, rid) for rid, data in self._request_map.items()]

    def get_console_logs(self) -> list[dict[str, Any]]:
        """Return collected browser console logs.

        Returns:
            List of console log entry dicts.
        """
        return self._console_logs

    def get_header_profiles(self) -> dict[str, dict[str, str]]:
        """Return header profiles classified from captured network requests."""
        from graftpunk.observe.headers import extract_header_profiles

        return extract_header_profiles(self._request_map)

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
        except Exception:
            LOG.exception("nodriver_get_page_source_failed")
            return None

    async def start_capture_async(self) -> None:
        """Begin capturing browser data asynchronously (CDP event listeners)."""
        tab = self._tab
        if tab is None:
            LOG.warning("nodriver_start_capture_async_no_tab")
            return

        import nodriver.cdp.network as network
        import nodriver.cdp.runtime as cdp_runtime

        await tab.send(network.enable())  # type: ignore[attr-defined]
        tab.add_handler(network.RequestWillBeSent, self._on_request)  # type: ignore[attr-defined]
        tab.add_handler(network.ResponseReceived, self._on_response)  # type: ignore[attr-defined]

        # Console log capture
        await tab.send(cdp_runtime.enable())  # type: ignore[attr-defined]
        tab.add_handler(cdp_runtime.ConsoleAPICalled, self._on_console)  # type: ignore[attr-defined]

        LOG.debug("nodriver_capture_async_started")

    async def stop_capture_async(self) -> None:
        """Stop capturing and fetch request/response bodies asynchronously."""
        tab = self._tab
        if tab is None:
            return

        import nodriver.cdp.network as cdp_net

        for request_id, data in list(self._request_map.items()):
            # Fetch POST body if has_post_data but no inline post_data
            if data.get("has_post_data") and not data.get("post_data"):
                try:
                    result = await tab.send(
                        cdp_net.get_request_post_data(cdp_net.RequestId(request_id))  # type: ignore[attr-defined]
                    )
                    data["post_data"] = result
                except Exception as exc:  # noqa: BLE001 — CDP body fetch is best-effort
                    LOG.warning(
                        "nodriver_post_data_fetch_failed",
                        request_id=request_id,
                        error=str(exc),
                    )

            # Fetch response body
            response = data.get("response", {})
            mime = response.get("mimeType", "")
            if _is_text_mime(mime) or _is_binary_mime(mime):
                try:
                    body, base64_encoded = await tab.send(
                        cdp_net.get_response_body(cdp_net.RequestId(request_id))  # type: ignore[attr-defined]
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
                except Exception as exc:  # noqa: BLE001 — CDP body fetch is best-effort
                    LOG.warning(
                        "nodriver_response_body_fetch_failed",
                        request_id=request_id,
                        error=str(exc),
                    )

    def _on_request(self, event: Any) -> None:
        """Handle a CDP RequestWillBeSent event."""
        try:
            # Note: CDP reuses request_id for redirect chains. We intentionally
            # capture only the final destination request data.
            self._request_map[str(event.request_id)] = {
                "url": event.request.url,
                "method": event.request.method,
                "headers": (dict(event.request.headers) if event.request.headers else {}),
                "post_data": getattr(event.request, "post_data", None),
                "has_post_data": getattr(event.request, "has_post_data", False),
                "timestamp": getattr(event, "wall_time", None),
            }
        except Exception:
            LOG.exception("nodriver_on_request_failed")

    def _on_response(self, event: Any) -> None:
        """Handle a CDP ResponseReceived event and correlate with request data."""
        try:
            rid = str(event.request_id)
            if rid not in self._request_map:
                self._request_map[rid] = {
                    "url": event.response.url,
                    "method": "GET",
                    "headers": {},
                    "timestamp": None,
                    "has_post_data": False,
                    "post_data": None,
                }
            self._request_map[rid]["response"] = {
                "status": event.response.status,
                "statusText": getattr(event.response, "status_text", "") or "",
                "headers": (dict(event.response.headers) if event.response.headers else {}),
                "mimeType": event.response.mime_type or "",
            }
        except Exception:
            LOG.exception("nodriver_on_response_failed")

    def _on_console(self, event: Any) -> None:
        """Handle a CDP ConsoleAPICalled event."""
        try:
            self._console_logs.append(
                {
                    "level": (
                        event.type_.value if hasattr(event.type_, "value") else str(event.type_)
                    ),
                    "args": [getattr(arg, "value", str(arg)) for arg in (event.args or [])],
                    "timestamp": getattr(event, "timestamp", None) or time.time(),
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
        if selenium is None:
            msg = "Selenium is not installed. Install it with: pip install selenium"
            raise ImportError(msg)
        return SeleniumCaptureBackend(
            driver,
            max_body_size=max_body_size,
            bodies_dir=bodies_dir,
        )
    msg = f"Unknown capture backend type: {backend_type!r}. Must be 'selenium' or 'nodriver'."
    raise ValueError(msg)
