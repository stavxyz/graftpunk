"""Observe capture for HTTP made with ``requests``: plugin commands and ``gp http``.

A plugin command runs on a ``requests`` session, with no browser to record
from, so ``gp --observe=full`` records that session's traffic. While the
capture runs, the session's ``send`` and each mounted adapter's ``send`` are
wrapped: the adapters note each response, each redirect hop included, with
when its request was sent, whether it went through ``Session.request``,
``Session.send`` or carried its own ``hooks=``; the session call records them
once requests is done with them. Nothing here reads a response body, so a
request does what it does with observe off. The entries are built by the
browser captures' own builder and bodies follow their rules
(:func:`graftpunk.observe.capture._process_response_body`).

Entries are spooled to a file in the run directory as they are recorded, so a
long command holds at most one call's responses in memory for the recorder;
bodies too large or not text for the HAR are written to ``bodies/`` at once.
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
    out and marked. The body is the one requests read (``Response._content``);
    this never reads a body. With *body_unread* set, requests left the body
    unread, and the entry says why with ``_bodyNotCaptured`` and
    ``_bodyNotCapturedReason``, except a streamed response with no body to
    miss (a redirect hop or ``Content-Length: 0``).
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
        # The body as requests read it; this never reads it itself.
        content = response.__dict__.get("_content")
        _process_response_body(
            summary,
            content if isinstance(content, bytes) else b"",
            False,
            request_id,
            content_type,
            max_body_size,
            bodies_dir,
        )
    elif body_unread != "streamed" or not (response.is_redirect or _has_empty_body(response)):
        # A streamed redirect hop or empty response has no body to have missed.
        summary["_bodyNotCaptured"] = body_unread
    data["response"] = summary

    entry = _build_har_entry(data)
    entry["time"] = elapsed * 1000
    return entry


class _RecordingAdapterSend:
    """Stands in for one adapter's ``send`` while a capture runs.

    It notes each response the adapter returns, with when its request was
    sent, and reads nothing: the body is read where requests reads it, so a
    request does exactly what it does with observe off. It is an instance
    attribute of the adapter, never pickled with it: ``HTTPAdapter.__getstate__``
    keeps only the adapter's settings.
    """

    def __init__(self, capture: HttpCaptureBackend, original: Any) -> None:
        self.capture = capture
        self.original = original

    def __call__(self, request: requests.PreparedRequest, *args: Any, **kwargs: Any) -> Any:
        started = time.time()
        response = self.original(request, *args, **kwargs)
        self.capture._seen(response, started)
        return response


class _RecordingSessionSend:
    """Stands in for the session's ``send`` while a capture runs.

    requests follows redirects by calling ``self.send`` again, so one outer
    call holds every hop of a request. When the outer call ends, requests has
    read every body it reads (each hop's in ``resolve_redirects``, the last one
    unless streamed), and the hops the adapters noted are recorded then, with
    the bodies as requests left them. Whether the call returned or raised, what
    it returns or raises is the original's. It is an instance attribute,
    outside ``requests.Session.__attrs__``, so it is never pickled.
    """

    def __init__(self, capture: HttpCaptureBackend, original: Any) -> None:
        self.capture = capture
        self.original = original

    def __call__(self, request: requests.PreparedRequest, **kwargs: Any) -> Any:
        local = self.capture._local
        outer = getattr(local, "depth", 0) == 0
        if outer:
            local.seen = []
        local.depth = getattr(local, "depth", 0) + 1
        result: requests.Response | None = None
        error: BaseException | None = None
        try:
            result = self.original(request, **kwargs)
            return result
        except BaseException as exc:
            error = exc
            raise
        finally:
            local.depth -= 1
            if outer:
                seen, local.seen = local.seen, None
                self.capture._finish_call(seen, result, error, bool(kwargs.get("stream")))


def _unread_reason(
    response: requests.Response, *, stream: bool, error: BaseException | None, last: bool
) -> str | None:
    """Why requests left *response*'s body unread, or None when it read it."""
    content = response.__dict__.get("_content", False)
    if content is not False:
        return None
    if error is not None and last:
        return f"read_failed: {type(error).__name__}"
    if stream:
        return "streamed"
    # requests reads every non-streamed body; a hop whose read failed is one it
    # tolerated in resolve_redirects and drained raw.
    return "read_failed"


class HttpCaptureBackend:
    """Capture backend recording a ``requests`` session's traffic.

    ``start_capture`` wraps the session's ``send`` and the ``send`` of every
    adapter mounted on it (see :class:`_RecordingSessionSend` and
    :class:`_RecordingAdapterSend`); ``stop_capture`` puts them back. Each
    response is recorded once requests is done with the call that produced it,
    so recording reads no body requests would not read and changes no outcome,
    hook or cookie. An adapter whose pickled state is not ``HTTPAdapter``'s is
    not wrapped (its wrapper could be pickled with the session), and neither is
    one mounted after the capture starts: a request through either is still
    recorded from the response the session returns, timed from its
    ``elapsed``, but one that raises is not, and a warning says so. Recording
    never raises into the request: a response it cannot record is logged and
    skipped. A response that arrives after ``stop_capture`` is dropped.

    With *spool_path*, each entry is appended to that file as JSON when it is
    recorded, scrubbed of *redact* first, and :meth:`iter_har_entries` reads
    them back one at a time; without it they are kept in memory. There is no
    browser, so screenshots, page source and console logs are always empty.
    """

    def __init__(
        self,
        session: requests.Session,
        max_body_size: int = MAX_RESPONSE_BODY_SIZE,
        bodies_dir: Path | None = None,
        spool_path: Path | None = None,
        redact: tuple[str, ...] = (),
    ) -> None:
        self._session = session
        self._max_body_size = max_body_size
        self._bodies_dir = bodies_dir
        self._spool_path = spool_path
        self._redact = tuple(redact)
        self._entries: list[dict[str, Any]] = []
        self._count = 0
        self._lock = threading.Lock()
        self._local = threading.local()
        self._stopped = False
        self._warned_unwrapped = False
        self._wrapped: list[Any] = []

    def _next_id(self) -> str:
        with self._lock:
            self._count += 1
            return f"http-{self._count}"

    def _seen(self, response: requests.Response, started: float) -> None:
        """Note a response an adapter returned, for its session call to record."""
        seen = getattr(self._local, "seen", None)
        if seen is not None:
            seen.append((response, started))
            return
        # An adapter used without the session's send: nothing will read the
        # body on requests' behalf here, and the recorder reads none itself.
        self.record(response, started, 0.0, body_unread="not_read")

    def _finish_call(
        self,
        seen: list[tuple[requests.Response, float]],
        result: requests.Response | None,
        error: BaseException | None,
        stream: bool,
    ) -> None:
        """Record every response of one session call, as requests left it."""
        try:
            noted = {id(response) for response, _started in seen}
            if result is not None:
                for response in [*result.history, result]:
                    if id(response) not in noted:
                        self._warn_unwrapped()
                        elapsed = response.elapsed.total_seconds()
                        seen.append((response, time.time() - elapsed))
            seen.sort(key=lambda pair: pair[1])
            for index, (response, started) in enumerate(seen):
                reason = _unread_reason(
                    response, stream=stream, error=error, last=index == len(seen) - 1
                )
                self.record(response, started, response.elapsed.total_seconds(), body_unread=reason)
        except Exception as exc:  # noqa: BLE001 - recording must never break the request
            LOG.error("http_capture_record_failed", error=str(exc), exc_type=type(exc).__name__)

    def _warn_unwrapped(self) -> None:
        if self._warned_unwrapped:
            return
        self._warned_unwrapped = True
        LOG.warning(
            "http_capture_adapter_not_recorded",
            hint=(
                "A request went through an adapter the capture did not wrap (mounted "
                "after it started, or with its own pickled state); it is recorded from "
                "the response, and a request through it that raises is not recorded"
            ),
        )

    def record(
        self,
        response: requests.Response,
        started: float,
        elapsed: float,
        *,
        body_unread: str | None = None,
    ) -> None:
        """Add one entry for *response*; log and skip it if it cannot be built."""
        if self._stopped:
            LOG.debug("http_capture_response_after_stop", url=getattr(response, "url", None))
            return
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
        if self._redact:
            from graftpunk.observe.run import redact_har_entries, redact_request_body_files

            (entry,) = redact_har_entries([entry], self._redact)
            if self._bodies_dir is not None:
                redact_request_body_files([entry], self._redact, self._bodies_dir.parent)
        with self._lock:
            # Checked under the lock stop_capture takes: once the run is being
            # written, a late response must not recreate the spool.
            if self._stopped:
                LOG.debug("http_capture_response_after_stop", url=entry["request"]["url"])
                return
            if self._spool_path is None:
                self._entries.append(entry)
                return
            from graftpunk.observe.storage import write_private_file

            write_private_file(self._spool_path, json.dumps(entry) + "\n", append=True)

    def start_capture(self) -> None:
        """Begin recording every request the session sends."""
        with self._lock:
            self._stopped = False
        session_send = self._session.__dict__.get("send")
        if not isinstance(session_send, _RecordingSessionSend):
            self._session.__dict__["send"] = _RecordingSessionSend(self, self._session.send)
            self._wrapped.append(self._session)
        for prefix, adapter in self._session.adapters.items():
            if isinstance(adapter.__dict__.get("send"), _RecordingAdapterSend):
                continue
            if type(adapter).__getstate__ is not requests.adapters.HTTPAdapter.__getstate__:
                LOG.warning(
                    "http_capture_adapter_not_recorded",
                    prefix=prefix,
                    adapter=type(adapter).__name__,
                    hint="Its pickled state is not HTTPAdapter's, so it is not wrapped",
                )
                continue
            adapter.send = _RecordingAdapterSend(self, adapter.send)
            self._wrapped.append(adapter)
        LOG.debug("http_capture_started", wrapped=len(self._wrapped))

    def stop_capture(self) -> None:
        """Stop recording and put each ``send`` back. Safe to call twice."""
        with self._lock:
            self._stopped = True
        for owner in self._wrapped:
            wrapper = owner.__dict__.get("send")
            if not isinstance(wrapper, _RecordingAdapterSend | _RecordingSessionSend):
                continue
            if wrapper.capture is not self:
                continue
            original = wrapper.original
            if getattr(original, "__self__", None) is owner and (
                getattr(original, "__func__", None) is getattr(type(owner), "send", None)
            ):
                del owner.__dict__["send"]
            else:
                owner.send = original
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
        """The HAR entries recorded so far, one at a time, in the order recorded."""
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
        """The HAR entries recorded so far, in the order recorded."""
        return list(self.iter_har_entries())

    def discard_spool(self) -> None:
        """Remove the spool file."""
        if self._spool_path is not None:
            self._spool_path.unlink(missing_ok=True)

    def get_console_logs(self) -> list[dict[str, Any]]:
        """There is no browser console."""
        return []

    def get_header_roles(self) -> dict[str, dict[str, str]]:
        """Header roles are learned from browser captures only."""
        return {}
