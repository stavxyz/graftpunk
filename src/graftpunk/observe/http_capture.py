"""Observe capture for HTTP made with ``requests``: plugin commands and ``gp http``.

A plugin command runs on a ``requests`` session, with no browser to record
from, so ``gp --observe=full`` records its traffic with a response hook on that
session. Each response the session receives becomes one HAR entry, redirect
hops included (``requests`` runs the hook once per hop). The entries are built
by the browser captures' own builder, and bodies follow their rules: a text
body up to the size cap goes into the HAR, a larger or binary one into the
run's ``bodies/``.
"""

from __future__ import annotations

import itertools
import time
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import requests

from graftpunk.logging import get_logger
from graftpunk.observe.capture import (
    MAX_RESPONSE_BODY_SIZE,
    _build_har_entry,
    _keep_post_data,
    _PostBody,
    _process_response_body,
    _response_summary,
)

LOG = get_logger(__name__)


def _request_body(body: Any) -> _PostBody | None:
    """The prepared request's body as bytes and text, or None for a streamed one."""
    if isinstance(body, str):
        return _PostBody(body.encode("utf-8", errors="surrogatepass"), body)
    if isinstance(body, bytes | bytearray):
        raw = bytes(body)
        try:
            return _PostBody(raw, raw.decode("utf-8"))
        except UnicodeDecodeError:
            return _PostBody(raw, None)
    return None


def _response_headers(response: requests.Response) -> dict[str, str]:
    """The response headers, every ``Set-Cookie`` line kept.

    ``response.headers`` joins repeated headers with ``", "``, which runs the
    cookies together; the raw urllib3 headers still hold each line. They are
    joined with newlines, as Chrome hands the browser captures several
    ``Set-Cookie`` headers, so the HAR's ``response.cookies`` parse the same.
    """
    headers = dict(response.headers)
    raw_headers = getattr(response.raw, "headers", None)
    getlist = getattr(raw_headers, "getlist", None)
    if getlist is None:
        return headers
    for name in list(headers):
        if name.lower() == "set-cookie":
            lines = getlist(name)
            if lines:
                headers[name] = "\n".join(lines)
    return headers


def har_entry_for_response(
    response: requests.Response,
    request_id: str,
    *,
    max_body_size: int = MAX_RESPONSE_BODY_SIZE,
    bodies_dir: Path | None = None,
    stream: bool = False,
) -> dict[str, Any]:
    """One HAR 1.2 entry for *response* and the request that produced it.

    Built the way the browser captures build theirs, so the digest, ``gp
    observe`` and ``gp plugin new`` read it the same. A body that is not
    UTF-8, or larger than *max_body_size*, goes to *bodies_dir* named after
    *request_id*; without one it is left out and marked. With *stream* true
    and the body not yet read, the response body is not read here (reading
    it would take it from the caller) and the entry says so with
    ``response.content._bodyNotCaptured``.
    """
    request = response.request
    elapsed = response.elapsed.total_seconds()
    request_headers: Mapping[str, str] = request.headers if request is not None else {}
    data: dict[str, Any] = {
        "url": (request.url if request is not None else None) or response.url,
        "method": (request.method if request is not None else None) or "GET",
        "headers": dict(request_headers),
        "timestamp": time.time() - elapsed,
    }
    if request is not None and request.body is not None:
        body = _request_body(request.body)
        if body is None:
            data["_post_data_unavailable"] = True
            LOG.warning(
                "post_data_unavailable",
                request_id=request_id,
                method=data["method"],
                url=data["url"],
                error="streamed request body",
                hint="A request body sent from a file or generator is not recorded",
            )
        else:
            _keep_post_data(
                data,
                body,
                max_body_size=max_body_size,
                bodies_dir=bodies_dir,
                request_id=request_id,
            )

    headers = _response_headers(response)
    content_type = response.headers.get("Content-Type", "")
    summary = _response_summary(
        response.status_code or 0,
        response.reason,
        headers,
        content_type.split(";")[0].strip(),
    )
    location = response.headers.get("Location")
    if response.is_redirect and location:
        summary["redirectURL"] = urllib.parse.urljoin(response.url or "", location)
    unread = stream and not getattr(response, "_content_consumed", True)
    if not unread:
        _process_response_body(
            summary,
            response.content or b"",
            False,
            request_id,
            content_type,
            max_body_size,
            bodies_dir,
        )
    data["response"] = summary

    entry = _build_har_entry(data)
    entry["time"] = elapsed * 1000
    if unread:
        entry["response"]["content"]["_bodyNotCaptured"] = True
    return entry


class HttpCaptureBackend:
    """Capture backend recording a ``requests`` session through a response hook.

    ``start_capture`` adds the hook and ``stop_capture`` removes it. The hook
    never raises into the request: a response it cannot record is logged and
    skipped. There is no browser, so screenshots, page source and console logs
    are always empty.
    """

    def __init__(
        self,
        session: requests.Session,
        max_body_size: int = MAX_RESPONSE_BODY_SIZE,
        bodies_dir: Path | None = None,
    ) -> None:
        self._session = session
        self._max_body_size = max_body_size
        self._bodies_dir = bodies_dir
        self._entries: list[dict[str, Any]] = []
        self._ids = itertools.count(1)

    def _on_response(self, response: requests.Response, *_args: Any, **kwargs: Any) -> None:
        request_id = f"http-{next(self._ids)}"
        try:
            self._entries.append(
                har_entry_for_response(
                    response,
                    request_id,
                    max_body_size=self._max_body_size,
                    bodies_dir=self._bodies_dir,
                    stream=bool(kwargs.get("stream")),
                )
            )
        except Exception as exc:  # noqa: BLE001 - recording must never break the request
            LOG.error(
                "http_capture_record_failed",
                request_id=request_id,
                url=getattr(response, "url", None),
                error=str(exc),
                exc_type=type(exc).__name__,
            )

    def start_capture(self) -> None:
        """Begin recording every response the session receives."""
        hooks = self._session.hooks.setdefault("response", [])
        if self._on_response not in hooks:
            hooks.append(self._on_response)
        LOG.debug("http_capture_started")

    def stop_capture(self) -> None:
        """Stop recording. Safe to call more than once."""
        hooks = self._session.hooks.get("response", [])
        while self._on_response in hooks:
            hooks.remove(self._on_response)
        LOG.debug("http_capture_stopped")

    async def start_capture_async(self) -> None:
        """Same as :meth:`start_capture`; nothing here is asynchronous."""
        self.start_capture()

    async def stop_capture_async(self) -> None:
        """Same as :meth:`stop_capture`; nothing here is asynchronous."""
        self.stop_capture()

    def take_screenshot_sync(self) -> bytes | None:
        """There is no page to take a screenshot of."""
        return None

    async def take_screenshot(self) -> bytes | None:
        """There is no page to take a screenshot of."""
        return None

    async def get_page_source(self) -> str | None:
        """There is no page."""
        return None

    def get_har_entries(self) -> list[dict[str, Any]]:
        """The HAR entries recorded so far, in the order the responses arrived."""
        return list(self._entries)

    def get_console_logs(self) -> list[dict[str, Any]]:
        """There is no browser console."""
        return []

    def get_header_roles(self) -> dict[str, dict[str, str]]:
        """Header roles are learned from browser captures only."""
        return {}
