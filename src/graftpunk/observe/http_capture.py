"""Observe capture for HTTP made with ``requests``: plugin commands and ``gp http``.

A plugin command runs on a ``requests`` session, with no browser to record
from, so ``gp --observe=full`` records its traffic at the session's transport
adapters: while the capture runs, each mounted adapter's ``send`` is wrapped,
so every request the session sends is seen, each redirect hop included,
whether it went through ``Session.request``, ``Session.send`` or carried its
own ``hooks=``. The entries are built by the browser captures' own builder and
bodies follow their rules (:func:`graftpunk.observe.capture._process_response_body`).

Entries are spooled to a file in the run directory as they arrive, so a long
command holds at most one response body in memory for the recorder; bodies
too large or not text for the HAR are written to ``bodies/`` at once.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import requests
import requests.adapters

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

#: The spool's name in the run directory; it is removed once the HAR is written.
SPOOL_NAME = "network.har.spool"


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


def _has_empty_body(response: requests.Response) -> bool:
    """True when the response says it has no body (``Content-Length: 0``)."""
    return response.headers.get("Content-Length", "").strip() == "0"


def har_entry_for_response(
    response: requests.Response,
    request_id: str,
    *,
    started: float,
    elapsed: float,
    max_body_size: int = MAX_RESPONSE_BODY_SIZE,
    bodies_dir: Path | None = None,
    body_unread: str | None = None,
) -> dict[str, Any]:
    """One HAR 1.2 entry for *response* and the request that produced it.

    Built the way the browser captures build theirs, so the digest, ``gp
    observe`` and ``gp plugin new`` read it the same. *started* is when the
    request was sent (seconds since the epoch) and *elapsed* how long it took,
    in seconds.

    The request body is kept like a browser capture's: UTF-8 within
    *max_body_size* in the HAR, otherwise in *bodies_dir* as
    ``<request_id>.request<ext>``. The response body follows
    :func:`graftpunk.observe.capture._process_response_body`: text within
    *max_body_size* goes into the HAR, decoded with its declared charset, else
    as UTF-8 (no charset is guessed); any other body, one that does not
    decode included, goes to *bodies_dir* as raw bytes; without one it is left
    out and marked. With *body_unread* set,
    the response body is not read at all (a streamed response, or one whose
    read failed) and the entry says why with ``_bodyNotCaptured`` and
    ``_bodyNotCapturedReason``, unless the response has no body to read (a
    redirect hop or ``Content-Length: 0``).
    """
    request = response.request
    request_headers: Mapping[str, str] = request.headers if request is not None else {}
    data: dict[str, Any] = {
        "url": (request.url if request is not None else None) or response.url,
        "method": (request.method if request is not None else None) or "GET",
        "headers": dict(request_headers),
        "timestamp": started,
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

    content_type = response.headers.get("Content-Type", "")
    summary = _response_summary(
        response.status_code or 0,
        response.reason,
        _response_headers(response),
        content_type.split(";")[0].strip(),
    )
    location = response.headers.get("Location")
    if response.is_redirect and location:
        summary["redirectURL"] = urllib.parse.urljoin(response.url or "", location)
    if body_unread is None:
        _process_response_body(
            summary,
            response.content or b"",
            False,
            request_id,
            content_type,
            max_body_size,
            bodies_dir,
        )
    elif not (response.is_redirect or _has_empty_body(response)):
        summary["_bodyNotCaptured"] = body_unread
    data["response"] = summary

    entry = _build_har_entry(data)
    entry["time"] = elapsed * 1000
    return entry


class _RecordingSend:
    """Stands in for one adapter's ``send`` while a capture runs.

    It is an instance attribute of the adapter, never pickled with it:
    ``HTTPAdapter.__getstate__`` keeps only the adapter's settings, so a session
    pickled mid-command (``cache_session(ctx.session)``) and loaded later does
    not record, and carries none of the entries.
    """

    def __init__(self, capture: HttpCaptureBackend, original: Any) -> None:
        self.capture = capture
        self.original = original

    def __call__(self, request: requests.PreparedRequest, *args: Any, **kwargs: Any) -> Any:
        stream = bool(kwargs.get("stream", args[0] if args else False))
        started = time.time()
        response = self.original(request, *args, **kwargs)
        if stream:
            self.capture.record(response, started, time.time() - started, body_unread="streamed")
            return response
        try:
            # requests reads it right after send returns; reading it here
            # first changes nothing for the caller.
            response.content  # noqa: B018
        except Exception as exc:
            self.capture.record(
                response,
                started,
                time.time() - started,
                body_unread=f"read_failed: {type(exc).__name__}",
            )
            raise
        self.capture.record(response, started, time.time() - started)
        return response


class HttpCaptureBackend:
    """Capture backend recording a ``requests`` session at its transport adapters.

    ``start_capture`` wraps the ``send`` of every adapter mounted on the
    session (see :class:`_RecordingSend`) and ``stop_capture`` puts it back. An
    adapter whose pickled state is not ``HTTPAdapter``'s is left unwrapped and
    logged, since its wrapper could be pickled with the session; an adapter
    mounted after the capture starts is not recorded. Recording never raises
    into the request: a response it cannot record is logged and skipped.

    With *spool_path*, each entry is appended to that file as JSON as it
    arrives, and :meth:`iter_har_entries` reads them back one at a time;
    without it they are kept in memory. There is no browser, so screenshots,
    page source and console logs are always empty.
    """

    def __init__(
        self,
        session: requests.Session,
        max_body_size: int = MAX_RESPONSE_BODY_SIZE,
        bodies_dir: Path | None = None,
        spool_path: Path | None = None,
    ) -> None:
        self._session = session
        self._max_body_size = max_body_size
        self._bodies_dir = bodies_dir
        self._spool_path = spool_path
        self._entries: list[dict[str, Any]] = []
        self._count = 0
        self._lock = threading.Lock()
        self._wrapped: list[requests.adapters.BaseAdapter] = []

    def _next_id(self) -> str:
        with self._lock:
            self._count += 1
            return f"http-{self._count}"

    def record(
        self,
        response: requests.Response,
        started: float,
        elapsed: float,
        *,
        body_unread: str | None = None,
    ) -> None:
        """Add one entry for *response*; log and skip it if it cannot be built."""
        request_id = self._next_id()
        try:
            entry = har_entry_for_response(
                response,
                request_id,
                started=started,
                elapsed=elapsed,
                max_body_size=self._max_body_size,
                bodies_dir=self._bodies_dir,
                body_unread=body_unread,
            )
            self._keep(entry)
        except Exception as exc:  # noqa: BLE001 - recording must never break the request
            LOG.error(
                "http_capture_record_failed",
                request_id=request_id,
                url=getattr(response, "url", None),
                error=str(exc),
                exc_type=type(exc).__name__,
            )

    def _keep(self, entry: dict[str, Any]) -> None:
        if self._spool_path is None:
            with self._lock:
                self._entries.append(entry)
            return
        from graftpunk.observe.storage import write_private_file

        line = json.dumps(entry) + "\n"
        with self._lock:
            write_private_file(self._spool_path, line, append=True)

    def start_capture(self) -> None:
        """Begin recording every request the session's mounted adapters send."""
        for prefix, adapter in self._session.adapters.items():
            if isinstance(adapter.__dict__.get("send"), _RecordingSend):
                continue
            if type(adapter).__getstate__ is not requests.adapters.HTTPAdapter.__getstate__:
                LOG.warning(
                    "http_capture_adapter_not_recorded",
                    prefix=prefix,
                    adapter=type(adapter).__name__,
                    hint="Its pickled state is not HTTPAdapter's, so it is not wrapped",
                )
                continue
            adapter.send = _RecordingSend(self, adapter.send)
            self._wrapped.append(adapter)
        LOG.debug("http_capture_started", adapters=len(self._wrapped))

    def stop_capture(self) -> None:
        """Stop recording and put each adapter's ``send`` back. Safe to call twice."""
        for adapter in self._wrapped:
            wrapper = adapter.__dict__.get("send")
            if isinstance(wrapper, _RecordingSend) and wrapper.capture is self:
                original = wrapper.original
                if getattr(original, "__self__", None) is adapter and (
                    getattr(original, "__func__", None) is getattr(type(adapter), "send", None)
                ):
                    del adapter.__dict__["send"]
                else:
                    adapter.send = original
        self._wrapped = []
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

    def iter_har_entries(self) -> Iterator[dict[str, Any]]:
        """The HAR entries recorded so far, one at a time, in the order sent."""
        if self._spool_path is None:
            yield from list(self._entries)
            return
        if not self._spool_path.exists():
            return
        with self._spool_path.open(encoding="utf-8") as spool:
            for line in spool:
                if line.strip():
                    yield json.loads(line)

    def get_har_entries(self) -> list[dict[str, Any]]:
        """The HAR entries recorded so far, in the order the responses arrived."""
        return list(self.iter_har_entries())

    def discard_spool(self) -> None:
        """Remove the spool file, once its entries are in the HAR."""
        if self._spool_path is not None:
            self._spool_path.unlink(missing_ok=True)

    def get_console_logs(self) -> list[dict[str, Any]]:
        """There is no browser console."""
        return []

    def get_header_roles(self) -> dict[str, dict[str, str]]:
        """Header roles are learned from browser captures only."""
        return {}
