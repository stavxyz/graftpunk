"""Request bodies CDP does not inline on ``Network.requestWillBeSent`` (#269).

A fetch or XHR whose body Chrome does not put on the event (a Blob body, for
one) arrives with ``hasPostData`` true and no ``postData``; the recorder has to
ask ``Network.getRequestPostData`` for it. These tests drive the nodriver backend
with real nodriver event objects through a fake tab that dispatches handlers the
way nodriver does (sync handlers inline, async ones as tasks) and answers CDP
commands the way Chrome does, including Chrome's ``-32000`` refusal.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from nodriver.cdp.util import parse_json_event
from nodriver.core.connection import ProtocolException
from structlog.testing import capture_logs

from graftpunk.observe import capture
from graftpunk.observe.capture import NodriverCaptureBackend, SeleniumCaptureBackend

SEARCH = "https://myshop.example/api/search"
LOGIN = "https://myshop.example/login"
HOME = "https://myshop.example/"
BODY = '{"query": "alice", "page": 1}'
RESPONSE = '{"results": []}'


def _response_json(url: str, status: int = 200, headers: dict[str, str] | None = None) -> dict:
    return {
        "url": url,
        "status": status,
        "statusText": "OK" if status == 200 else "Found",
        "headers": headers or {"Content-Type": "application/json"},
        "mimeType": "application/json" if status == 200 else "text/html",
        "charset": "utf-8",
        "connectionReused": False,
        "connectionId": 1,
        "encodedDataLength": 20,
        "securityState": "secure",
    }


def _will_be_sent(
    rid: str,
    *,
    inline: str | None = None,
    url: str = SEARCH,
    method: str = "POST",
    redirect_from: str | None = None,
    entries: list[bytes] | None = None,
    document_url: str = HOME,
    resource_type: str = "Fetch",
    headers: dict[str, str] | None = None,
) -> Any:
    request: dict[str, Any] = {
        "url": url,
        "method": method,
        "headers": {"Content-Type": "application/json", **(headers or {})},
        "initialPriority": "High",
        "referrerPolicy": "strict-origin-when-cross-origin",
    }
    if method == "POST":
        request["hasPostData"] = True
    if inline is not None:
        request["postData"] = inline
    if entries is not None:
        request["postDataEntries"] = [{"bytes": base64.b64encode(e).decode()} for e in entries]
    params: dict[str, Any] = {
        "requestId": rid,
        "loaderId": "loader-1",
        "documentURL": document_url,
        "request": request,
        "timestamp": 1.0,
        "wallTime": 1_700_000_000.0,
        "initiator": {"type": "script"},
        "redirectHasExtraInfo": False,
        "type": resource_type,
    }
    if redirect_from is not None:
        params["redirectResponse"] = _response_json(redirect_from, 302, {"Location": url})
    return parse_json_event({"method": "Network.requestWillBeSent", "params": params})


def _response_received(rid: str, url: str = SEARCH) -> Any:
    return parse_json_event(
        {
            "method": "Network.responseReceived",
            "params": {
                "requestId": rid,
                "loaderId": "loader-1",
                "timestamp": 2.0,
                "type": "Fetch",
                "hasExtraInfo": False,
                "response": _response_json(url),
            },
        }
    )


def _loading_finished(rid: str) -> Any:
    return parse_json_event(
        {
            "method": "Network.loadingFinished",
            "params": {"requestId": rid, "timestamp": 3.0, "encodedDataLength": 20},
        }
    )


def _refused() -> ProtocolException:
    return ProtocolException({"code": -32000, "message": "No resource with given id was found"})


class FakeTab:
    """A nodriver tab double: handler dispatch as nodriver does it, CDP replies as
    Chrome gives them.

    ``post_data_available(request_id, tab)`` and ``response_available(request_id,
    tab)`` say whether Chrome still answers ``getRequestPostData`` and
    ``getResponseBody`` for that request; when it does not, the command fails with
    the ``-32000`` error Chrome sends. Both also fail on a CDP session other than
    the one ``Network.enable`` went to: ``reattach()`` does what nodriver 0.50.6's
    ``Browser.get(url)`` does, a new session on the same target, after which
    ``send`` addresses the new one unless the caller passes ``sessionId``.
    Measured live, Chrome answered ``getRequestPostData`` only on the enabling
    session, and ``getResponseBody`` on either while durable messages were on but
    on neither without them; the fake holds response bodies to the stricter rule
    so the recorder does not depend on the durable buffer. ``stopping`` is set by
    tests just before ``stop_capture_async``.
    """

    def __init__(
        self,
        bodies: dict[str, str | tuple[str, bool]],
        post_data_available: Callable[[str, FakeTab], bool],
        response_available: Callable[[str, FakeTab], bool] = lambda _rid, _t: True,
    ) -> None:
        self.bodies = bodies
        self.post_data_available = post_data_available
        self.response_available = response_available
        self.handlers: dict[type, list[Callable[..., Any]]] = {}
        self.finished: set[str] = set()
        self.stopping = False
        self.stop_task: asyncio.Task[Any] | None = None
        self.session_id: str | None = "session-1"
        self.network_sessions: set[str | None] = set()
        self.post_data_requests: list[str] = []
        # The live page DOM.getDocument and DOM.getOuterHTML describe: its HTML,
        # or None to refuse both as Chrome does for a target that went away.
        self.dom_html: str | None = None
        self.dom_url = HOME
        self.dom_requests: list[str] = []
        # Every command sent, as (method, params), and the methods to refuse.
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.refuse: set[str] = set()
        # Run as DOM.getDocument is asked (an event that lands while DevTools
        # answers), and a gate it waits on before answering (a read still running
        # at stop).
        self.on_dom_read: Callable[[], None] | None = None
        self.dom_gate: asyncio.Event | None = None

    def reattach(self) -> None:
        assert self.session_id is not None
        self.session_id = f"session-{int(self.session_id.rsplit('-', 1)[1]) + 1}"

    def add_handler(self, event_type: type, callback: Callable[..., Any]) -> None:
        self.handlers.setdefault(event_type, []).append(callback)

    async def send(self, cmd: Any, **kwargs: Any) -> Any:
        if getattr(cmd, "__name__", "") == "get_document":
            if self.on_dom_read is not None:
                self.on_dom_read()
            if self.dom_gate is not None:
                await self.dom_gate.wait()
        return self._answer(cmd, kwargs.get("sessionId", self.session_id))

    def _answer(self, cmd: Any, session: str | None, *, post_data_now: bool | None = None) -> Any:
        message = next(cmd)
        method = message["method"]
        rid = message.get("params", {}).get("requestId")
        enabled = session in self.network_sessions
        self.sent.append((method, dict(message.get("params", {}))))
        if method in self.refuse:
            raise _refused()
        if method == "Network.enable":
            self.network_sessions.add(session)
        if method == "Network.getRequestPostData":
            self.post_data_requests.append(rid)
            available = (
                self.post_data_available(rid, self) if post_data_now is None else post_data_now
            )
            if not enabled or rid not in self.bodies or not available:
                raise _refused()
            body = self.bodies[rid]
            text, encoded = body if isinstance(body, tuple) else (body, False)
            reply: dict[str, Any] = {"postData": text, "base64Encoded": encoded}
        elif method.startswith("DOM."):
            self.dom_requests.append(method)
            if method == "DOM.disable":
                reply = {}
            elif self.dom_html is None:
                raise ProtocolException({"code": -32000, "message": "Cannot find context"})
            elif method == "DOM.getDocument":
                reply = {
                    "root": {
                        "nodeId": 1,
                        "backendNodeId": 2,
                        "nodeType": 9,
                        "nodeName": "#document",
                        "localName": "",
                        "nodeValue": "",
                        "documentURL": self.dom_url,
                    }
                }
            else:
                reply = {"outerHTML": self.dom_html}
        elif method == "Page.addScriptToEvaluateOnNewDocument":
            reply = {"identifier": "script-1"}
        elif method == "Network.getResponseBody":
            if not enabled or not self.response_available(rid, self):
                raise _refused()
            reply = {"body": RESPONSE, "base64Encoded": False}
        else:
            reply = {}
        try:
            cmd.send(reply)
        except StopIteration as stop:
            return stop.value
        raise AssertionError(f"{method} did not finish on one reply")

    async def dispatch(self, event: Any, *, settle: bool = True) -> None:
        if type(event).__name__ == "LoadingFinished":
            self.finished.add(str(event.request_id))
        for callback in self.handlers.get(type(event), []):
            if inspect.iscoroutinefunction(callback):
                asyncio.create_task(callback(event))  # noqa: RUF006 - nodriver does the same
            else:
                callback(event)
        # Let the tasks nodriver would have scheduled run before the next event.
        for _ in range(5 if settle else 0):
            await asyncio.sleep(0)


async def _start(tab: FakeTab, **kwargs: Any) -> NodriverCaptureBackend:
    backend = NodriverCaptureBackend(object(), get_tab=lambda: tab, **kwargs)
    await backend.start_capture_async()
    return backend


async def _stop(backend: NodriverCaptureBackend, tab: FakeTab) -> None:
    tab.stopping = True
    tab.stop_task = asyncio.current_task()
    await backend.stop_capture_async()


async def _record(tab: FakeTab, events: list[Any], **kwargs: Any) -> NodriverCaptureBackend:
    backend = await _start(tab, **kwargs)
    for event in events:
        await tab.dispatch(event)
    await _stop(backend, tab)
    return backend


def _fetch_post(rid: str, *, inline: str | None = None) -> list[Any]:
    return [_will_be_sent(rid, inline=inline), _response_received(rid), _loading_finished(rid)]


def _redirected_login(rid: str) -> list[Any]:
    """A login POST answered 302, then the GET it redirected to, under one id."""
    return [
        _will_be_sent(rid, url=LOGIN),
        _will_be_sent(rid, url=HOME, method="GET", redirect_from=LOGIN),
        _response_received(rid, url=HOME),
        _loading_finished(rid),
    ]


def _only_entry(backend: Any) -> dict[str, Any]:
    (entry,) = backend.get_har_entries()
    return entry


def _marks(backend: Any) -> list[tuple[str, str, Any]]:
    return [
        (e["request"]["method"], e["request"]["url"], e["request"].get("_postDataNotCaptured"))
        for e in backend.get_har_entries()
    ]


def _events(logs: list[dict[str, Any]], name: str) -> list[str]:
    return [e["request_id"] for e in logs if e["event"] == name]


def _never(_rid: str, _tab: FakeTab) -> bool:
    return False


def _always(_rid: str, _tab: FakeTab) -> bool:
    return True


def _in_flight(rid: str, tab: FakeTab) -> bool:
    return rid not in tab.finished


def _at_stop(_rid: str, tab: FakeTab) -> bool:
    return tab.stopping


def _before_stop(_rid: str, tab: FakeTab) -> bool:
    return not tab.stopping


class TestNodriverPostDataFetch:
    @pytest.mark.asyncio
    async def test_a_body_chrome_answers_for_only_while_in_flight_is_kept(self) -> None:
        """Fetched on requestWillBeSent, before the response is consumed: a body
        Chrome stops answering for once the request finished still reaches the HAR."""
        tab = FakeTab({"7.2": BODY}, _in_flight)

        backend = await _record(tab, _fetch_post("7.2"))

        request = _only_entry(backend)["request"]
        assert request["postData"] == {"mimeType": "application/json", "text": BODY}
        assert "_postDataNotCaptured" not in request

    @pytest.mark.asyncio
    async def test_a_body_chrome_answers_for_only_at_stop_is_still_kept(self) -> None:
        """The stop-time fetch stays as the retry for a request-time failure."""
        tab = FakeTab({"7.3": BODY}, _at_stop)

        backend = await _record(tab, _fetch_post("7.3"))

        assert _only_entry(backend)["request"]["postData"]["text"] == BODY

    @pytest.mark.asyncio
    async def test_an_inline_body_is_not_fetched_again(self) -> None:
        tab = FakeTab({}, _never)

        backend = await _record(tab, [_will_be_sent("7.4", inline=BODY), _response_received("7.4")])

        request = _only_entry(backend)["request"]
        assert request["postData"]["text"] == BODY
        assert "_postDataNotCaptured" not in request
        assert tab.post_data_requests == []

    @pytest.mark.asyncio
    async def test_an_empty_body_chrome_returns_is_a_captured_empty_body(self) -> None:
        """An empty string is what the request sent, not a body that went missing."""
        tab = FakeTab({"7.7": ""}, _always)

        with capture_logs() as logs:
            backend = await _record(tab, _fetch_post("7.7"))

        request = _only_entry(backend)["request"]
        assert request["postData"] == {"mimeType": "application/json", "text": ""}
        assert "_postDataNotCaptured" not in request
        assert _events(logs, "post_data_unavailable") == []
        assert tab.post_data_requests == ["7.7"]

    @pytest.mark.asyncio
    async def test_a_body_chrome_never_answers_for_is_marked_and_logged(self) -> None:
        """A body Chrome will not return is said to be lost, not dropped silently."""
        tab = FakeTab({"7.5": BODY}, _never)

        with capture_logs() as logs:
            backend = await _record(tab, _fetch_post("7.5"))

        request = _only_entry(backend)["request"]
        assert "postData" not in request
        assert request["_postDataNotCaptured"] is True
        unavailable = [e for e in logs if e["event"] == "post_data_unavailable"]
        assert [(e["request_id"], e["method"], e["url"]) for e in unavailable] == [
            ("7.5", "POST", SEARCH)
        ]
        assert "No resource with given id was found" in unavailable[0]["error"]
        assert unavailable[0]["log_level"] == "warning"

    @pytest.mark.asyncio
    async def test_a_second_stop_logs_nothing_more(self) -> None:
        tab = FakeTab({"7.8": BODY}, _never)

        with capture_logs() as logs:
            backend = await _record(tab, _fetch_post("7.8"))
            await backend.stop_capture_async()

        assert _events(logs, "post_data_unavailable") == ["7.8"]

    @pytest.mark.asyncio
    async def test_the_marker_survives_the_har_file_round_trip(self, tmp_path: Any) -> None:
        from graftpunk.har.parser import parse_har_file

        tab = FakeTab({}, _never)
        backend = await _record(tab, _fetch_post("7.6"))
        har = tmp_path / "network.har"
        har.write_text(json.dumps({"log": {"entries": backend.get_har_entries()}}))

        (entry,) = parse_har_file(har).entries

        assert entry.request.post_data is None
        assert entry.request.post_data_not_captured is True


class TestStopPaths:
    """Every body still missing is marked, however stop ends, a redirect hop's too."""

    async def _plain_and_hop(self, tab: FakeTab) -> NodriverCaptureBackend:
        backend = await _start(tab)
        for event in [*_fetch_post("8.1"), *_redirected_login("8.2")]:
            await tab.dispatch(event)
        return backend

    @pytest.mark.asyncio
    async def test_a_lost_hop_body_is_marked_and_logged(self) -> None:
        tab = FakeTab({"8.1": BODY}, _always)
        backend = await self._plain_and_hop(tab)

        with capture_logs() as logs:
            await _stop(backend, tab)

        assert _marks(backend) == [
            ("POST", SEARCH, None),
            ("POST", LOGIN, True),
            ("GET", HOME, None),
        ]
        assert _events(logs, "redirect_hop_post_data_unavailable") == ["8.2:redirect:1"]

    @pytest.mark.asyncio
    async def test_a_dead_browser_marks_every_body_it_left_unfetched(self) -> None:
        """The stop-time loop returns at the first ConnectionError; the hop behind
        it is marked by the sweep that runs after."""
        tab = FakeTab({}, _never)
        backend = await self._plain_and_hop(tab)

        async def dead(cmd: Any, **_kwargs: Any) -> Any:
            raise ConnectionError("browser went away")

        tab.send = dead  # type: ignore[method-assign]
        with capture_logs() as logs:
            await _stop(backend, tab)

        assert [mark for _m, _u, mark in _marks(backend)] == [True, True, None]
        assert _events(logs, "post_data_unavailable") == ["8.1"]
        assert _events(logs, "redirect_hop_post_data_unavailable") == ["8.2:redirect:1"]

    @pytest.mark.asyncio
    async def test_no_tab_at_stop_still_marks_every_missing_body(self) -> None:
        tab = FakeTab({}, _never)
        current: dict[str, FakeTab | None] = {"tab": tab}
        backend = NodriverCaptureBackend(object(), get_tab=lambda: current["tab"])
        await backend.start_capture_async()
        for event in [*_fetch_post("8.3"), *_redirected_login("8.4")]:
            await tab.dispatch(event)
        current["tab"] = None

        with capture_logs() as logs:
            await backend.stop_capture_async()

        assert [mark for _m, _u, mark in _marks(backend)] == [True, True, None]
        assert _events(logs, "post_data_unavailable") == ["8.3"]
        assert _events(logs, "redirect_hop_post_data_unavailable") == ["8.4:redirect:1"]

    @pytest.mark.asyncio
    async def test_an_exception_escaping_the_stop_time_fetch_still_marks(self) -> None:
        """A cancellation is not caught by the fetch loop; the marks go on anyway."""
        tab = FakeTab({}, _never)
        backend = await _start(tab)
        for event in _fetch_post("8.5"):
            await tab.dispatch(event)

        async def cancelled(cmd: Any, **_kwargs: Any) -> Any:
            raise asyncio.CancelledError

        tab.send = cancelled  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError):
            await _stop(backend, tab)

        assert _only_entry(backend)["request"]["_postDataNotCaptured"] is True


class SlowTab(FakeTab):
    """A request-time body that arrives after a delay; Chrome refuses the stop pass.

    A fetch counts as the stop pass when it runs in the task that called stop, so a
    request-time fetch that only starts after stop began is still answered.
    """

    def __init__(self, delay: float) -> None:
        super().__init__({"8.6": BODY}, _before_stop)
        self.delay = delay

    async def send(self, cmd: Any, **kwargs: Any) -> Any:
        asked_in_flight = asyncio.current_task() is not self.stop_task
        if asked_in_flight and getattr(cmd, "__name__", "") == "get_request_post_data":
            await asyncio.sleep(self.delay)
        return self._answer(
            cmd, kwargs.get("sessionId", self.session_id), post_data_now=asked_in_flight
        )


class TestRequestTimeFetchesAtStop:
    @pytest.mark.asyncio
    async def test_a_body_still_arriving_at_stop_is_waited_for_not_flagged(self) -> None:
        tab = SlowTab(delay=0.05)

        with capture_logs() as logs:
            backend = await _record(tab, _fetch_post("8.6"))

        request = _only_entry(backend)["request"]
        assert request["postData"]["text"] == BODY
        assert "_postDataNotCaptured" not in request
        assert _events(logs, "post_data_unavailable") == []

    @pytest.mark.asyncio
    async def test_a_fetch_that_outlasts_the_wait_is_cancelled_and_marked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(capture, "_POST_DATA_SETTLE_TIMEOUT_S", 0.01)
        tab = SlowTab(delay=0.5)

        with capture_logs() as logs:
            backend = await _record(tab, _fetch_post("8.6"))
        await asyncio.sleep(0.6)

        request = _only_entry(backend)["request"]
        assert "postData" not in request
        assert request["_postDataNotCaptured"] is True
        assert [e["count"] for e in logs if e["event"].endswith("_fetch_cancelled")] == [1]


BINARY = bytes(range(0x80, 0x89))  # 9 bytes, none of them valid UTF-8 alone


def _b64(raw: bytes) -> tuple[str, bool]:
    return (base64.b64encode(raw).decode(), True)


class TestStopBeforeTheFetchStarts:
    @pytest.mark.asyncio
    async def test_a_fetch_task_not_yet_started_at_stop_is_waited_for(self) -> None:
        """nodriver creates the fetch task with the event; stop in the same loop turn
        still waits for it instead of flagging a body about to arrive."""
        tab = SlowTab(delay=0.05)
        backend = await _start(tab)
        for event in _fetch_post("8.6"):
            await tab.dispatch(event, settle=False)

        with capture_logs() as logs:
            await _stop(backend, tab)

        request = _only_entry(backend)["request"]
        assert request["postData"]["text"] == BODY
        assert _events(logs, "post_data_unavailable") == []

    @pytest.mark.asyncio
    async def test_a_stop_cancelled_while_it_waits_still_marks_and_stops_the_fetch(
        self,
    ) -> None:
        tab = SlowTab(delay=0.3)
        backend = await _start(tab)
        for event in _fetch_post("8.6"):
            await tab.dispatch(event)

        stopping = asyncio.create_task(_stop(backend, tab))
        await asyncio.sleep(0.05)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        await asyncio.sleep(0.4)

        request = _only_entry(backend)["request"]
        assert request["_postDataNotCaptured"] is True
        assert "postData" not in request


class TestBodySizeCap:
    """A request body over ``max_body_size``, or one that is not UTF-8, is handled as
    an oversized response body is: written to ``bodies/`` when the capture has that
    directory, and otherwise left out of the HAR with its size and reason."""

    @pytest.mark.asyncio
    async def test_an_inline_body_over_the_cap_is_left_out(self) -> None:
        tab = FakeTab({}, _never)

        with capture_logs() as logs:
            backend = await _record(tab, _fetch_post("8.7", inline=BODY), max_body_size=10)

        request = _only_entry(backend)["request"]
        assert "postData" not in request
        assert request["_postDataNotCaptured"] is True
        assert request["_postDataOmitted"] == {"size": len(BODY), "reason": "over_max_body_size"}
        assert [
            (e["request_id"], e["size"], e["reason"])
            for e in logs
            if e["event"] == "post_data_omitted"
        ] == [("8.7", len(BODY), "over_max_body_size")]
        assert _events(logs, "post_data_unavailable") == []
        assert tab.post_data_requests == []

    @pytest.mark.asyncio
    async def test_a_fetched_body_over_the_cap_is_left_out(self) -> None:
        tab = FakeTab({"8.8": BODY}, _always)

        backend = await _record(tab, _fetch_post("8.8"), max_body_size=10)

        request = _only_entry(backend)["request"]
        assert "postData" not in request
        assert request["_postDataOmitted"] == {"size": len(BODY), "reason": "over_max_body_size"}

    @pytest.mark.asyncio
    async def test_a_body_at_the_cap_is_kept(self) -> None:
        tab = FakeTab({"8.9": BODY}, _always)

        backend = await _record(tab, _fetch_post("8.9"), max_body_size=len(BODY))

        assert _only_entry(backend)["request"]["postData"]["text"] == BODY

    @pytest.mark.asyncio
    async def test_the_cap_is_measured_in_bytes(self) -> None:
        """Six characters, twelve UTF-8 bytes: over a cap of ten."""
        tab = FakeTab({"8.10": "\u00e9" * 6}, _always)

        backend = await _record(tab, _fetch_post("8.10"), max_body_size=10)

        assert _only_entry(backend)["request"]["_postDataOmitted"]["size"] == 12

    @pytest.mark.asyncio
    async def test_a_body_over_the_cap_goes_to_bodies_when_the_capture_has_it(
        self, tmp_path: Any
    ) -> None:
        from graftpunk.har.parser import parse_har_file

        tab = FakeTab({"8.11": BODY}, _always)
        bodies = tmp_path / "bodies"

        with capture_logs() as logs:
            backend = await _record(tab, _fetch_post("8.11"), max_body_size=10, bodies_dir=bodies)

        request = _only_entry(backend)["request"]
        assert (request["_postDataFile"], request["_postDataSize"]) == (
            "bodies/8.11.request.json",
            len(BODY),
        )
        assert "postData" not in request
        assert "_postDataNotCaptured" not in request
        assert (bodies / "8.11.request.json").read_text() == BODY
        assert [
            e for e in logs if e["event"] in ("post_data_omitted", "post_data_unavailable")
        ] == []
        har = tmp_path / "network.har"
        har.write_text(json.dumps({"log": {"entries": backend.get_har_entries()}}))
        (entry,) = parse_har_file(har).entries
        assert (entry.request.post_data, entry.request.post_data_file) == (
            BODY,
            "bodies/8.11.request.json",
        )

    @pytest.mark.asyncio
    async def test_a_binary_body_is_never_stored_as_replacement_text(self) -> None:
        """A base64 body that is not UTF-8 is left out with its decoded size, not
        written into the HAR as U+FFFD characters."""
        tab = FakeTab({"8.12": _b64(BINARY)}, _always)

        with capture_logs() as logs:
            backend = await _record(tab, _fetch_post("8.12"))

        request = _only_entry(backend)["request"]
        assert "postData" not in request
        assert request["_postDataOmitted"] == {"size": len(BINARY), "reason": "binary"}
        assert [e["reason"] for e in logs if e["event"] == "post_data_omitted"] == ["binary"]

    @pytest.mark.asyncio
    async def test_a_binary_body_goes_to_bodies_as_its_bytes(self, tmp_path: Any) -> None:
        from graftpunk.har.parser import parse_har_file

        tab = FakeTab({"8.13": _b64(BINARY)}, _always)
        bodies = tmp_path / "bodies"

        backend = await _record(tab, _fetch_post("8.13"), bodies_dir=bodies)

        request = _only_entry(backend)["request"]
        assert request["_postDataFile"] == "bodies/8.13.request.json"
        assert (bodies / "8.13.request.json").read_bytes() == BINARY
        har = tmp_path / "network.har"
        har.write_text(json.dumps({"log": {"entries": backend.get_har_entries()}}))
        (entry,) = parse_har_file(har).entries
        assert (entry.request.post_data, entry.request.post_data_file) == (
            None,
            "bodies/8.13.request.json",
        )


class TestRequestBodyFileReference:
    def test_a_reference_outside_the_hars_directory_is_not_read(self, tmp_path: Any) -> None:
        from graftpunk.har.parser import parse_har_file

        run = tmp_path / "run"
        run.mkdir()
        (tmp_path / "outside.json").write_text(BODY)
        entry = {
            "startedDateTime": "2026-10-19T10:00:00.000Z",
            "time": 0,
            "request": {
                "method": "POST",
                "url": SEARCH,
                "headers": [],
                "cookies": [],
                "queryString": [],
                "_postDataFile": "../outside.json",
            },
            "response": {"status": 200, "statusText": "OK", "headers": [], "cookies": []},
        }
        har = run / "network.har"
        har.write_text(json.dumps({"log": {"entries": [entry]}}))

        (parsed,) = parse_har_file(har).entries

        assert (parsed.request.post_data, parsed.request.post_data_file) == (
            None,
            "../outside.json",
        )


def _selenium_backend(
    execute: Callable[[str, dict[str, Any]], dict[str, Any]],
    *,
    inline: str | None = None,
    **kwargs: Any,
):
    def perf(method: str, params: dict[str, Any]) -> dict[str, str]:
        return {"message": json.dumps({"message": {"method": method, "params": params}})}

    request: dict[str, Any] = {
        "url": SEARCH,
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "hasPostData": True,
    }
    if inline is not None:
        request["postData"] = inline
    log = [
        perf(
            "Network.requestWillBeSent",
            {"requestId": "9.1", "request": request, "wallTime": 1_700_000_000.0},
        ),
        perf(
            "Network.responseReceived",
            {
                "requestId": "9.1",
                "response": {"status": 200, "headers": {}, "mimeType": "application/json"},
            },
        ),
    ]

    class Driver:
        def get_log(self, kind: str) -> list[dict[str, str]]:
            return log if kind == "performance" else []

        def execute_cdp_cmd(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            return execute(method, params)

    return SeleniumCaptureBackend(Driver(), **kwargs)


class TestSeleniumPostData:
    def test_a_body_chrome_refuses_is_marked_and_logged(self) -> None:
        def execute(method: str, _params: dict[str, Any]) -> dict[str, Any]:
            if method == "Network.getRequestPostData":
                raise RuntimeError("No resource with given id was found")
            return {"body": "{}", "base64Encoded": False}

        backend = _selenium_backend(execute)
        with capture_logs() as logs:
            backend.stop_capture()

        request = _only_entry(backend)["request"]
        assert request["_postDataNotCaptured"] is True
        assert _events(logs, "post_data_unavailable") == ["9.1"]

    def test_a_fetched_body_over_the_cap_is_left_out(self) -> None:
        def execute(method: str, _params: dict[str, Any]) -> dict[str, Any]:
            if method == "Network.getRequestPostData":
                return {"postData": BODY, "base64Encoded": False}
            return {"body": "{}", "base64Encoded": False}

        backend = _selenium_backend(execute, max_body_size=10)
        backend.stop_capture()

        assert _only_entry(backend)["request"]["_postDataOmitted"] == {
            "size": len(BODY),
            "reason": "over_max_body_size",
        }


class FakeTab048(FakeTab):
    """The shape of nodriver 0.48.1, read from its source: no session ids (each target
    has its own websocket), ``send(cdp_obj, _is_update=False)``, and a
    ``Browser.get`` that does not re-attach."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.session_id = None

    async def send(self, cmd: Any, _is_update: bool = False) -> Any:  # type: ignore[override]
        return self._answer(cmd, None)


class FakeTabNoSessionKeyword(FakeTab):
    """A ``send`` that cannot name a session on a tab that does re-attach. No nodriver
    release is known to combine the two; it checks the mark when the pin cannot apply."""

    async def send(self, cmd: Any, _is_update: bool = False) -> Any:  # type: ignore[override]
        return self._answer(cmd, self.session_id)


class TestCaptureSession:
    """#269's cause, measured live on nodriver 0.50.6: ``gp observe`` navigates with
    ``browser.get(url)``, which attaches a second CDP session to the tab, and Chrome
    answers ``getRequestPostData`` only on the session that enabled Network."""

    async def _record_across_reattach(self, tab: FakeTab) -> NodriverCaptureBackend:
        backend = await _start(tab)
        tab.reattach()
        for event in _fetch_post("9.2"):
            await tab.dispatch(event)
        tab.reattach()
        await _stop(backend, tab)
        return backend

    @pytest.mark.asyncio
    async def test_the_request_time_fetch_asks_the_capture_session(self) -> None:
        backend = await self._record_across_reattach(FakeTab({"9.2": BODY}, _in_flight))

        request = _only_entry(backend)["request"]
        assert request["postData"]["text"] == BODY
        assert "_postDataNotCaptured" not in request

    @pytest.mark.asyncio
    async def test_the_stop_time_fetch_asks_the_capture_session(self) -> None:
        backend = await self._record_across_reattach(FakeTab({"9.2": BODY}, _at_stop))

        assert _only_entry(backend)["request"]["postData"]["text"] == BODY

    @pytest.mark.asyncio
    async def test_the_eager_response_fetch_asks_the_capture_session(self) -> None:
        tab = FakeTab({"9.2": BODY}, _always, response_available=_before_stop)

        backend = await self._record_across_reattach(tab)

        assert _only_entry(backend)["response"]["content"]["text"] == RESPONSE

    @pytest.mark.asyncio
    async def test_the_stop_time_response_fetch_asks_the_capture_session(self) -> None:
        tab = FakeTab({"9.2": BODY}, _always, response_available=_at_stop)

        backend = await self._record_across_reattach(tab)

        assert _only_entry(backend)["response"]["content"]["text"] == RESPONSE

    @pytest.mark.asyncio
    async def test_a_nodriver_048_shaped_tab_keeps_the_body(self) -> None:
        """No session to pin and no re-attach to route around: the plain fetch works.
        Measured live on 2026-10-19 (probe 6 on nodriver 0.48.1): every body, the
        Blob ones included, was kept with no session named."""
        tab = FakeTab048({"9.2": BODY}, _always)

        backend = await _record(tab, _fetch_post("9.2"))

        assert _only_entry(backend)["request"]["postData"]["text"] == BODY

    @pytest.mark.asyncio
    async def test_a_send_that_cannot_name_a_session_marks_a_body_after_a_reattach(
        self,
    ) -> None:
        tab = FakeTabNoSessionKeyword({"9.2": BODY}, _always)

        with capture_logs() as logs:
            backend = await self._record_across_reattach(tab)

        assert _only_entry(backend)["request"]["_postDataNotCaptured"] is True
        assert _events(logs, "post_data_unavailable") == ["9.2"]


def _refuse_post_data(method: str, _params: dict[str, Any]) -> dict[str, Any]:
    if method == "Network.getRequestPostData":
        raise RuntimeError("No resource with given id was found")
    return {"body": "{}", "base64Encoded": False}


def _answer_post_data(method: str, _params: dict[str, Any]) -> dict[str, Any]:
    if method == "Network.getRequestPostData":
        return {"postData": BODY, "base64Encoded": False}
    return {"body": "{}", "base64Encoded": False}


async def _over_cap_entry(path: str, bodies: Any) -> dict[str, Any]:
    """The HAR request for one over-cap body, recorded through *path*."""
    if path == "nodriver inline":
        backend = await _record(
            FakeTab({}, _never),
            _fetch_post("9.6", inline=BODY),
            max_body_size=10,
            bodies_dir=bodies,
        )
    elif path == "nodriver stop-time fetch":
        backend = await _record(
            FakeTab({"9.6": BODY}, _at_stop),
            _fetch_post("9.6"),
            max_body_size=10,
            bodies_dir=bodies,
        )
    elif path == "nodriver request-time fetch":
        backend = await _record(
            FakeTab({"9.6": BODY}, _in_flight),
            _fetch_post("9.6"),
            max_body_size=10,
            bodies_dir=bodies,
        )
    elif path == "selenium inline":
        backend = _selenium_backend(
            _refuse_post_data, inline=BODY, max_body_size=10, bodies_dir=bodies
        )
        backend.stop_capture()
    else:
        backend = _selenium_backend(_answer_post_data, max_body_size=10, bodies_dir=bodies)
        backend.stop_capture()
    return _only_entry(backend)["request"]


class TestEveryRecordingPathKeepsBodiesInTheDirectory:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "nodriver inline",
            "nodriver stop-time fetch",
            "nodriver request-time fetch",
            "selenium inline",
            "selenium fetched",
        ],
    )
    async def test_an_over_cap_body_goes_to_bodies(self, tmp_path: Any, path: str) -> None:
        request = await _over_cap_entry(path, tmp_path / "bodies")

        assert "_postDataNotCaptured" not in request
        assert (tmp_path / request["_postDataFile"]).read_text() == BODY

    @pytest.mark.asyncio
    async def test_a_failed_write_leaves_the_body_out_and_says_so(self, tmp_path: Any) -> None:
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("")

        with capture_logs() as logs:
            request = await _over_cap_entry("nodriver inline", blocker / "bodies")

        assert "_postDataFile" not in request
        assert request["_postDataNotCaptured"] is True
        assert request["_postDataOmitted"] == {"size": len(BODY), "reason": "over_max_body_size"}
        assert [e["event"] for e in logs if e["event"].startswith("post_data_")] == [
            "post_data_file_write_failed",
            "post_data_omitted",
        ]


class TestRedirectHopBodies:
    @pytest.mark.asyncio
    async def test_a_307_hop_and_its_successor_each_keep_their_own_file(
        self, tmp_path: Any
    ) -> None:
        """CDP keeps one request id across the chain; each entry's file is its own."""
        first, second = '{"a": "' + "x" * 40 + '"}', '{"b": "' + "y" * 40 + '"}'
        events = [
            _will_be_sent("9.3", url=LOGIN, inline=first),
            _will_be_sent("9.3", url=SEARCH, inline=second, redirect_from=LOGIN),
            _response_received("9.3"),
        ]

        backend = await _record(
            FakeTab({}, _never), events, max_body_size=10, bodies_dir=tmp_path / "bodies"
        )

        files = [e["request"]["_postDataFile"] for e in backend.get_har_entries()]
        assert files == ["bodies/9.3.request.json", "bodies/9.3.request-2.json"]
        assert [(tmp_path / f).read_text() for f in files] == [first, second]


class LateRequestTab(FakeTab):
    """A new POST with an uninlined body starts while stop fetches a response body."""

    fired = False

    async def send(self, cmd: Any, **kwargs: Any) -> Any:
        if self.stopping and getattr(cmd, "__name__", "") == "get_response_body" and not self.fired:
            self.fired = True
            event = _will_be_sent("9.5")
            for callback in self.handlers[type(event)]:
                if inspect.iscoroutinefunction(callback):
                    asyncio.create_task(callback(event))  # noqa: RUF006 - nodriver does the same
                else:
                    callback(event)
        return self._answer(cmd, kwargs.get("sessionId", self.session_id))


class TestARequestStartingAsStopEnds:
    @pytest.mark.asyncio
    async def test_its_body_is_marked_and_never_written_afterwards(self) -> None:
        tab = LateRequestTab({"9.5": BODY}, _always, response_available=_at_stop)
        backend = await _start(tab)
        for event in _fetch_post("9.4", inline=BODY):
            await tab.dispatch(event)

        with capture_logs() as logs:
            await _stop(backend, tab)
            await asyncio.sleep(0.05)

        late = [e["request"] for e in backend.get_har_entries() if e["request"]["url"] == SEARCH]
        assert [("postData" in r, r.get("_postDataNotCaptured")) for r in late] == [
            (True, None),
            (False, True),
        ]
        assert _events(logs, "post_data_unavailable") == ["9.5"]


class TestHeaderOnlyCapture:
    """A capture whose HAR is never written (the login engine's header capture)
    fetches no request body and logs nothing about one."""

    @pytest.mark.asyncio
    async def test_nodriver_fetches_and_logs_nothing(self) -> None:
        tab = FakeTab({"9.7": BODY}, _always)
        events = [*_fetch_post("9.7"), *_fetch_post("9.8", inline=BODY), *_redirected_login("9.9")]

        with capture_logs() as logs:
            backend = await _record(tab, events, max_body_size=10, record_request_bodies=False)

        assert tab.post_data_requests == []
        assert [e["event"] for e in logs if "post_data" in e["event"]] == []
        assert all(
            not any(key.startswith("_postData") for key in e["request"])
            for e in backend.get_har_entries()
        )

    def test_selenium_fetches_and_logs_nothing(self) -> None:
        asked: list[str] = []

        def execute(method: str, params: dict[str, Any]) -> dict[str, Any]:
            asked.append(method)
            return _refuse_post_data(method, params)

        backend = _selenium_backend(execute, record_request_bodies=False)
        with capture_logs() as logs:
            backend.stop_capture()

        assert "Network.getRequestPostData" not in asked
        assert [e["event"] for e in logs if "post_data" in e["event"]] == []
        assert "_postDataNotCaptured" not in _only_entry(backend)["request"]

    @pytest.mark.asyncio
    async def test_the_login_engines_header_capture_is_one(self) -> None:
        from graftpunk.plugins.login_engine import _start_login_capture
        from tests.unit.test_login_headless_observe import _make_plugin

        tab = FakeTab({"9.7": BODY}, _always)
        capture_backend, storage = _start_login_capture(
            _make_plugin("nodriver"), "nodriver", object(), "off", get_tab=lambda: tab
        )
        await capture_backend.start_capture_async()
        for event in _fetch_post("9.7"):
            await tab.dispatch(event)

        assert storage is None
        assert tab.post_data_requests == []


def _run_dir_har(run: Any, entries: list[dict[str, Any]]) -> None:
    (run / "network.har").write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}))


class TestBodiesThroughTheDigest:
    """A recording through the real backend, its HAR written to a run directory and
    read back by the digest and the stub renderer, as ``gp plugin new`` does."""

    async def _digest_of(self, run: Any, tab: FakeTab, events: list[Any], **kwargs: Any):
        from graftpunk.har.digest import DigestSource, digest

        backend = await _record(tab, events, bodies_dir=run / "bodies", **kwargs)
        _run_dir_har(run, backend.get_har_entries())
        return digest(DigestSource.from_run_dir(run, session="myshop", run_id="r1"))

    @staticmethod
    def _stub_comments(result: Any) -> str:
        from graftpunk.devtools.scaffold.render import ScaffoldSpec, render

        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example",
            digest=result,
        )
        return " ".join(
            line.strip().lstrip("#").strip()
            for line in render(spec)["src/graftpunk_myshop/plugin.py"].splitlines()
            if line.strip().startswith("#")
        )

    @pytest.mark.asyncio
    async def test_a_binary_blob_body_is_named_with_its_file(self, tmp_path: Any) -> None:
        from graftpunk.har.report import endpoints_projection, render_markdown

        result = await self._digest_of(
            tmp_path, FakeTab({"9.1": _b64(BINARY)}, _always), _fetch_post("9.1")
        )

        (endpoint,) = result.endpoints
        assert (endpoint.body_binary, endpoint.body_binary_file, endpoint.body_binary_size) == (
            1,
            "bodies/9.1.request.json",
            len(BINARY),
        )
        assert (endpoint.body_not_captured, endpoint.body_omitted) == (0, 0)
        assert (
            "- body not text: 1 of 1 recording(s) sent a binary body, kept as "
            f"`bodies/9.1.request.json` ({len(BINARY)} bytes)"
        ) in render_markdown(result)
        assert endpoints_projection(result)["endpoints"][0]["body_not_captured"] is True
        assert (
            "GP-FILL: the request body is not text (1 of 1 recording(s) sent a binary body, "
            f"kept in run myshop/r1 as `bodies/9.1.request.json` ({len(BINARY)} bytes))"
        ) in self._stub_comments(result)

    @pytest.mark.asyncio
    async def test_a_body_whose_file_is_gone_counts_as_not_captured(self, tmp_path: Any) -> None:
        from graftpunk.har.report import endpoints_projection, render_markdown

        tab = FakeTab({"9.2": BODY}, _always)
        backend = await _record(
            tab, _fetch_post("9.2"), max_body_size=10, bodies_dir=tmp_path / "bodies"
        )
        _run_dir_har(tmp_path, backend.get_har_entries())
        (tmp_path / "bodies" / "9.2.request.json").unlink()

        from graftpunk.har.digest import DigestSource, digest

        result = digest(DigestSource.from_run_dir(tmp_path, session="myshop", run_id="r1"))

        (endpoint,) = result.endpoints
        assert (endpoint.body_not_captured, endpoint.body_binary) == (1, 0)
        assert "- body not captured: 1 of 1 recording(s)" in render_markdown(result)
        assert endpoints_projection(result)["endpoints"][0]["body_not_captured"] is True
        assert "GP-FILL: the request body was not captured" in self._stub_comments(result)

    @pytest.mark.asyncio
    async def test_a_text_body_in_bodies_gives_its_fields(self, tmp_path: Any) -> None:
        result = await self._digest_of(
            tmp_path, FakeTab({"9.3": BODY}, _always), _fetch_post("9.3"), max_body_size=10
        )

        (endpoint,) = result.endpoints
        assert endpoint.body_params == {"page": "int", "query": "str"}
        assert (endpoint.body_not_captured, endpoint.body_omitted, endpoint.body_binary) == (
            0,
            0,
            0,
        )


class TestInlineBodyEdgeCases:
    @pytest.mark.asyncio
    async def test_a_lone_surrogate_in_an_inline_body_is_kept(self) -> None:
        """CDP's JSON can carry an unpaired \\ud800 escape; measuring the body must not
        raise and lose it."""
        body = '{"q": "a\ud800b"}'

        backend = await _record(FakeTab({}, _never), _fetch_post("9.11", inline=body))

        assert _only_entry(backend)["request"]["postData"]["text"] == body

    def test_an_inline_body_wins_over_a_file_reference(self, tmp_path: Any) -> None:
        from graftpunk.har.parser import parse_har_file

        (tmp_path / "bodies").mkdir()
        (tmp_path / "bodies" / "x.json").write_text('{"from": "file"}')
        entry = {
            "startedDateTime": "2026-10-19T10:00:00.000Z",
            "time": 0,
            "request": {
                "method": "POST",
                "url": SEARCH,
                "headers": [],
                "cookies": [],
                "queryString": [],
                "postData": {"mimeType": "application/json", "text": BODY},
                "_postDataFile": "bodies/x.json",
            },
            "response": {"status": 200, "statusText": "OK", "headers": [], "cookies": []},
        }
        _run_dir_har(tmp_path, [entry])

        (parsed,) = parse_har_file(tmp_path / "network.har").entries

        assert parsed.request.post_data == BODY


class TestRequestBodyFileSymlink:
    def test_a_symlink_inside_bodies_that_leaves_the_run_is_not_read(self, tmp_path: Any) -> None:
        from graftpunk.har.parser import parse_har_file

        run = tmp_path / "run"
        (run / "bodies").mkdir(parents=True)
        (tmp_path / "secret.json").write_text(BODY)
        (run / "bodies" / "link.json").symlink_to(tmp_path / "secret.json")
        entry = {
            "startedDateTime": "2026-10-19T10:00:00.000Z",
            "time": 0,
            "request": {
                "method": "POST",
                "url": SEARCH,
                "headers": [],
                "cookies": [],
                "queryString": [],
                "_postDataFile": "bodies/link.json",
            },
            "response": {"status": 200, "statusText": "OK", "headers": [], "cookies": []},
        }
        _run_dir_har(run, [entry])

        (parsed,) = parse_har_file(run / "network.har").entries

        assert parsed.request.post_data is None
        assert parsed.request.post_data_not_captured is True


class OverlapTab(FakeTab):
    """The request-time fetch answers after the settle wait, during the stop-time
    retry for the same request, which is slower still and also answered."""

    async def send(self, cmd: Any, **kwargs: Any) -> Any:
        if getattr(cmd, "__name__", "") == "get_request_post_data":
            in_stop = asyncio.current_task() is self.stop_task
            await asyncio.sleep(0.15 if in_stop else 0.05)
        return self._answer(cmd, kwargs.get("sessionId", self.session_id))


class TestAFetchPastTheWaitIsCancelledBeforeTheRetry:
    """Left running, it answers beside the stop-time retry and the body is stored
    twice: a second file that looks like a redirect's, or a second omitted log."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [_b64(BINARY), BODY], ids=["binary", "over-cap text"])
    async def test_one_file_and_one_reference(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, body: Any
    ) -> None:
        monkeypatch.setattr(capture, "_POST_DATA_SETTLE_TIMEOUT_S", 0.01)
        tab = OverlapTab({"9.1": body}, _always)

        backend = await _record(
            tab, _fetch_post("9.1"), bodies_dir=tmp_path / "bodies", max_body_size=10
        )
        await asyncio.sleep(0.3)

        written = sorted(p.name for p in (tmp_path / "bodies").iterdir() if ".request" in p.name)
        assert written == ["9.1.request.json"]
        assert _only_entry(backend)["request"]["_postDataFile"] == "bodies/9.1.request.json"

    @pytest.mark.asyncio
    async def test_one_omitted_log_without_a_bodies_directory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(capture, "_POST_DATA_SETTLE_TIMEOUT_S", 0.01)
        tab = OverlapTab({"9.1": _b64(BINARY)}, _always)

        with capture_logs() as logs:
            await _record(tab, _fetch_post("9.1"))
            await asyncio.sleep(0.3)

        assert _events(logs, "post_data_omitted") == ["9.1"]


class TestAfterStop:
    @pytest.mark.asyncio
    async def test_a_request_after_stop_is_marked_and_not_fetched(self) -> None:
        tab = FakeTab({"9.2": BODY}, _always)
        backend = await _record(tab, _fetch_post("9.1", inline=BODY))
        asked_before = list(tab.post_data_requests)

        with capture_logs() as logs:
            for event in _fetch_post("9.2"):
                await tab.dispatch(event)

        assert tab.post_data_requests == asked_before
        marks = [e["request"].get("_postDataNotCaptured") for e in backend.get_har_entries()]
        assert marks == [None, True]
        assert _events(logs, "post_data_unavailable") == ["9.2"]

    @pytest.mark.asyncio
    async def test_a_request_after_a_stop_with_no_tab_is_marked(self) -> None:
        """Stop with no tab also closes the capture, so a later request's body is
        marked rather than left unjudged."""
        tab = FakeTab({"9.2": BODY}, _always)
        current: dict[str, FakeTab | None] = {"tab": tab}
        backend = NodriverCaptureBackend(object(), get_tab=lambda: current["tab"])
        await backend.start_capture_async()
        current["tab"] = None
        await backend.stop_capture_async()

        for event in _fetch_post("9.2"):
            await tab.dispatch(event)

        assert _only_entry(backend)["request"]["_postDataNotCaptured"] is True

    @pytest.mark.asyncio
    async def test_a_restarted_capture_fetches_again(self) -> None:
        tab = FakeTab({"9.3": BODY}, _in_flight)
        backend = await _record(tab, _fetch_post("9.1", inline=BODY))
        tab.stopping = False
        await backend.start_capture_async()
        for event in _fetch_post("9.3"):
            await tab.dispatch(event)
        await _stop(backend, tab)

        restarted = [e["request"] for e in backend.get_har_entries()][-1]
        assert restarted["postData"]["text"] == BODY


class TestHeaderOnlyCaptureEdges:
    @pytest.mark.asyncio
    async def test_nodriver_stopped_without_a_tab_logs_nothing(self) -> None:
        tab = FakeTab({}, _never)
        current: dict[str, FakeTab | None] = {"tab": tab}
        backend = NodriverCaptureBackend(
            object(), get_tab=lambda: current["tab"], record_request_bodies=False
        )
        await backend.start_capture_async()
        for event in [*_fetch_post("9.4"), *_redirected_login("9.5")]:
            await tab.dispatch(event)
        current["tab"] = None

        with capture_logs() as logs:
            await backend.stop_capture_async()

        assert [e["event"] for e in logs if "post_data" in e["event"]] == []
        assert all("_postDataNotCaptured" not in e["request"] for e in backend.get_har_entries())

    def test_selenium_keeps_no_inline_body_and_notes_no_hop(self) -> None:
        def perf(method: str, params: dict[str, Any]) -> dict[str, str]:
            return {"message": json.dumps({"message": {"method": method, "params": params}})}

        def request(url: str, method: str, **extra: Any) -> dict[str, Any]:
            return {"url": url, "method": method, "headers": {}, **extra}

        log = [
            perf(
                "Network.requestWillBeSent",
                {
                    "requestId": "9.6",
                    "request": request(SEARCH, "POST", hasPostData=True, postData=BODY),
                },
            ),
            perf(
                "Network.requestWillBeSent",
                {"requestId": "9.7", "request": request(LOGIN, "POST", hasPostData=True)},
            ),
            perf(
                "Network.requestWillBeSent",
                {
                    "requestId": "9.7",
                    "request": request(HOME, "GET"),
                    "redirectResponse": {"status": 302, "headers": {"Location": "/"}},
                },
            ),
        ]
        asked: list[str] = []

        class Driver:
            def get_log(self, kind: str) -> list[dict[str, str]]:
                return log if kind == "performance" else []

            def execute_cdp_cmd(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
                asked.append(method)
                return {"body": "{}", "base64Encoded": False}

        backend = SeleniumCaptureBackend(Driver(), max_body_size=10, record_request_bodies=False)
        with capture_logs() as logs:
            backend.stop_capture()

        assert [e["event"] for e in logs if "post_data" in e["event"]] == []
        assert "Network.getRequestPostData" not in asked
        assert all(
            not any(key.startswith("_postData") or key == "postData" for key in e["request"])
            for e in backend.get_har_entries()
        )

    @pytest.mark.asyncio
    async def test_the_login_engines_fallback_after_a_storage_failure_is_one(self) -> None:
        from unittest.mock import patch

        from graftpunk.plugins.login_engine import _start_login_capture
        from tests.unit.test_login_headless_observe import _make_plugin

        tab = FakeTab({"9.8": BODY}, _always)
        with patch("graftpunk.observe.storage.ObserveStorage", side_effect=OSError("read-only")):
            capture_backend, storage = _start_login_capture(
                _make_plugin("nodriver"), "nodriver", object(), "full", get_tab=lambda: tab
            )
        await capture_backend.start_capture_async()
        for event in _fetch_post("9.8"):
            await tab.dispatch(event)

        assert storage is None
        assert tab.post_data_requests == []


class TestParserBodyFileFailures:
    @staticmethod
    def _har(ref: str) -> dict[str, Any]:
        return {
            "log": {
                "entries": [
                    {
                        "startedDateTime": "2026-10-19T10:00:00.000Z",
                        "time": 0,
                        "request": {
                            "method": "POST",
                            "url": SEARCH,
                            "headers": [],
                            "cookies": [],
                            "queryString": [],
                            "_postDataFile": ref,
                        },
                        "response": {
                            "status": 200,
                            "statusText": "OK",
                            "headers": [],
                            "cookies": [],
                        },
                    }
                ]
            }
        }

    def test_a_har_read_without_its_directory_counts_the_body_as_not_captured(self) -> None:
        from graftpunk.har.parser import parse_har_string

        (entry,) = parse_har_string(json.dumps(self._har("bodies/1.request.json"))).entries

        assert (entry.request.post_data_not_captured, entry.request.post_data_binary) == (
            True,
            False,
        )

    def test_an_unreadable_body_file_counts_as_not_captured(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pathlib import Path

        from graftpunk.har.parser import parse_har_file

        (tmp_path / "bodies").mkdir()
        (tmp_path / "bodies" / "1.request.json").write_text(BODY)
        (tmp_path / "network.har").write_text(json.dumps(self._har("bodies/1.request.json")))

        def refuse(self: Path) -> bytes:
            raise PermissionError(13, "Permission denied", str(self))

        monkeypatch.setattr(Path, "read_bytes", refuse)
        (entry,) = parse_har_file(tmp_path / "network.har").entries

        assert (entry.request.post_data_not_captured, entry.request.post_data_binary) == (
            True,
            False,
        )


# What Chrome put on requestWillBeSent for a Uint8Array POST of these nine bytes,
# recorded live on 2026-10-19 (probe8-raw-params.out): postData maps each byte to
# the code point of the same value, and postDataEntries carries the bytes.
LIVE_BINARY = bytes([0x00, 0xFF, 0xFE, 0x80, 0x81, 0xC3, 0x28, 0x10, 0x9F])
LIVE_BINARY_POSTDATA = "\x00\xff\xfe\x80\x81\xc3(\x10\x9f"
LIVE_UTF8 = "h\u00e9llo \u20ac".encode()


def _inline_binary_post(rid: str) -> list[Any]:
    return [
        _will_be_sent(rid, inline=LIVE_BINARY_POSTDATA, entries=[LIVE_BINARY]),
        _response_received(rid),
        _loading_finished(rid),
    ]


class TestInlineBodyBytes:
    """A body Chrome inlines is read from postDataEntries, not from postData (#269,
    live probe 7: a Uint8Array POST was stored as 15 bytes of text for 9 sent)."""

    @pytest.mark.asyncio
    async def test_an_inline_binary_body_goes_to_bodies_as_the_bytes_sent(
        self, tmp_path: Any
    ) -> None:
        tab = FakeTab({}, _never)

        backend = await _record(tab, _inline_binary_post("10.1"), bodies_dir=tmp_path / "bodies")

        request = _only_entry(backend)["request"]
        assert "postData" not in request
        assert (tmp_path / request["_postDataFile"]).read_bytes() == LIVE_BINARY
        assert request["_postDataSize"] == len(LIVE_BINARY)
        assert tab.post_data_requests == []

    @pytest.mark.asyncio
    async def test_an_inline_binary_body_without_a_bodies_directory_is_left_out(self) -> None:
        backend = await _record(FakeTab({}, _never), _inline_binary_post("10.2"))

        request = _only_entry(backend)["request"]
        assert request["_postDataOmitted"] == {"size": len(LIVE_BINARY), "reason": "binary"}

    @pytest.mark.asyncio
    async def test_inline_utf8_bytes_stay_inline_text(self) -> None:
        events = [
            _will_be_sent("10.3", inline="h\u00e9llo \u20ac", entries=[LIVE_UTF8]),
            _response_received("10.3"),
        ]

        backend = await _record(FakeTab({}, _never), events)

        assert _only_entry(backend)["request"]["postData"]["text"] == "h\u00e9llo \u20ac"

    @pytest.mark.asyncio
    async def test_several_entries_are_joined(self, tmp_path: Any) -> None:
        events = [
            _will_be_sent(
                "10.4", inline=LIVE_BINARY_POSTDATA, entries=[LIVE_BINARY[:4], LIVE_BINARY[4:]]
            ),
            _response_received("10.4"),
        ]

        backend = await _record(FakeTab({}, _never), events, bodies_dir=tmp_path / "bodies")

        request = _only_entry(backend)["request"]
        assert (tmp_path / request["_postDataFile"]).read_bytes() == LIVE_BINARY

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "post_data",
        [LIVE_BINARY_POSTDATA, "caf\u00e9\x0cmenu", "caf\u00e9 \u00fc"],
        ids=["mapped bytes", "text with a control character", "accented text"],
    )
    async def test_without_entries_postdata_is_kept_as_chrome_gave_it(
        self, tmp_path: Any, post_data: str
    ) -> None:
        """No postDataEntries: the body is the postData text, whatever it holds.
        Whether any Chrome sends a binary body inline without entries is unverified."""
        events = [_will_be_sent("10.5", inline=post_data), _response_received("10.5")]

        backend = await _record(FakeTab({}, _never), events, bodies_dir=tmp_path / "bodies")

        request = _only_entry(backend)["request"]
        assert request["postData"]["text"] == post_data
        assert "_postDataFile" not in request

    @pytest.mark.asyncio
    async def test_an_entry_without_bytes_falls_back_to_postdata(self) -> None:
        """CDP's PostDataEntry.bytes is optional: a list with one missing is treated
        like no entries."""
        event = _will_be_sent("10.6", inline=BODY)
        event.request.post_data_entries = [
            type("Entry", (), {"bytes_": base64.b64encode(b"{").decode()})(),
            type("Entry", (), {"bytes_": None})(),
        ]

        backend = await _record(FakeTab({}, _never), [event, _response_received("10.6")])

        assert _only_entry(backend)["request"]["postData"]["text"] == BODY

    @pytest.mark.asyncio
    async def test_entries_that_are_not_base64_fall_back_to_postdata(self) -> None:
        """Strict base64: an entry with a stray character is not read as its prefix."""
        event = _will_be_sent("10.8", inline=BODY)
        event.request.post_data_entries = [type("Entry", (), {"bytes_": "QUJD!"})()]

        with capture_logs() as logs:
            backend = await _record(FakeTab({}, _never), [event, _response_received("10.8")])

        assert _only_entry(backend)["request"]["postData"]["text"] == BODY
        assert [e["event"] for e in logs if e["event"] == "post_data_entries_not_base64"] == [
            "post_data_entries_not_base64"
        ]

    @pytest.mark.asyncio
    async def test_an_inline_binary_redirect_hop_keeps_its_own_bytes(self, tmp_path: Any) -> None:
        events = [
            _will_be_sent("10.9", url=LOGIN, inline=LIVE_BINARY_POSTDATA, entries=[LIVE_BINARY]),
            _will_be_sent("10.9", url=HOME, method="GET", redirect_from=LOGIN),
            _response_received("10.9", url=HOME),
        ]

        backend = await _record(FakeTab({}, _never), events, bodies_dir=tmp_path / "bodies")

        hop = backend.get_har_entries()[0]["request"]
        assert hop["method"] == "POST"
        assert (tmp_path / hop["_postDataFile"]).read_bytes() == LIVE_BINARY

    def test_selenium_reads_the_entries_too(self, tmp_path: Any) -> None:
        def perf(method: str, params: dict[str, Any]) -> dict[str, str]:
            return {"message": json.dumps({"message": {"method": method, "params": params}})}

        request = {
            "url": SEARCH,
            "method": "POST",
            "headers": {"Content-Type": "application/octet-stream"},
            "hasPostData": True,
            "postData": "\u00c3(\u00c3(",  # Chrome maps these bytes to code points
            "postDataEntries": [{"bytes": base64.b64encode(b"\xc3(\xc3(").decode()}],
        }
        log = [perf("Network.requestWillBeSent", {"requestId": "10.10", "request": request})]

        class Driver:
            def get_log(self, kind: str) -> list[dict[str, str]]:
                return log if kind == "performance" else []

            def execute_cdp_cmd(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
                return {}

        backend = SeleniumCaptureBackend(Driver(), bodies_dir=tmp_path / "bodies")
        backend.stop_capture()

        request_entry = _only_entry(backend)["request"]
        assert request_entry["_postDataFile"] == "bodies/10.10.request.bin"
        assert (tmp_path / request_entry["_postDataFile"]).read_bytes() == b"\xc3(\xc3("

    @pytest.mark.asyncio
    async def test_through_the_digest_and_the_stub(self, tmp_path: Any) -> None:
        from graftpunk.har.digest import DigestSource, digest
        from graftpunk.har.report import endpoints_projection, render_markdown

        backend = await _record(
            FakeTab({}, _never), _inline_binary_post("10.11"), bodies_dir=tmp_path / "bodies"
        )
        _run_dir_har(tmp_path, backend.get_har_entries())

        result = digest(DigestSource.from_run_dir(tmp_path, session="myshop", run_id="r1"))

        assert (
            "- body not text: 1 of 1 recording(s) sent a binary body, kept as "
            f"`bodies/10.11.request.json` ({len(LIVE_BINARY)} bytes)"
        ) in render_markdown(result)
        assert endpoints_projection(result)["endpoints"][0]["body_not_captured"] is True
        assert (
            "GP-FILL: the request body is not text (1 of 1 recording(s) sent a binary body, "
            f"kept in run myshop/r1 as `bodies/10.11.request.json` ({len(LIVE_BINARY)} bytes))"
        ) in TestBodiesThroughTheDigest._stub_comments(result)


class TestRequestBodyFilesAreRedacted:
    """A login POST over the HAR's cap lands in bodies/; the submitted secret is
    scrubbed there as it is in an inline postData."""

    SECRET = "hunter2-correct-horse"  # noqa: S105 - a test value, not a credential

    async def _saved_run(
        self, tmp_path: Any, body: Any, *, redact: Any = None, **kwargs: Any
    ) -> tuple[Any, list]:
        from graftpunk.observe.run import save_observe_run
        from graftpunk.observe.storage import ObserveStorage

        storage = ObserveStorage(tmp_path, "myshop", "r1")
        tab = FakeTab({"11.1": body}, _always)
        backend = NodriverCaptureBackend(
            object(), get_tab=lambda: tab, bodies_dir=storage.run_dir / "bodies", **kwargs
        )
        await backend.start_capture_async()
        for event in _fetch_post("11.1"):
            await tab.dispatch(event)
        with capture_logs() as logs:
            await save_observe_run(
                storage, backend, "login", redact=[self.SECRET] if redact is None else redact
            )
        return storage.run_dir, logs

    @pytest.mark.asyncio
    async def test_a_text_login_body_in_bodies_is_scrubbed(self, tmp_path: Any) -> None:
        """Its size is recounted in bytes: the padding is not ASCII."""
        from graftpunk.observe.run import REDACTED

        body = json.dumps(
            {"username": "alice@example.com", "password": self.SECRET, "pad": "\u00e9" * 40},
            ensure_ascii=False,
        )
        run, _logs = await self._saved_run(tmp_path, body, max_body_size=10)

        (entry,) = json.loads((run / "network.har").read_text())["log"]["entries"]
        stored = (run / entry["request"]["_postDataFile"]).read_text()
        assert self.SECRET not in stored
        assert REDACTED in stored
        assert entry["request"]["_postDataSize"] == len(stored.encode())

    @pytest.mark.asyncio
    async def test_a_generator_of_secrets_reaches_the_body_files_too(self, tmp_path: Any) -> None:
        body = json.dumps({"password": self.SECRET, "pad": "x" * 40})
        run, _logs = await self._saved_run(
            tmp_path, body, redact=(s for s in [self.SECRET]), max_body_size=10
        )

        (entry,) = json.loads((run / "network.har").read_text())["log"]["entries"]
        assert self.SECRET not in (run / entry["request"]["_postDataFile"]).read_text()

    @pytest.mark.asyncio
    async def test_a_binary_body_has_the_secret_masked_byte_for_byte(self, tmp_path: Any) -> None:
        """1.22.0 scrubbed a binary login body's ASCII password through its U+FFFD
        text; the file keeps that, masking each form's bytes at the same length."""
        recorded = b"\xff\x00user=a&pw=" + self.SECRET.encode() + b"&\xfe"
        run, logs = await self._saved_run(tmp_path, _b64(recorded))

        (entry,) = json.loads((run / "network.har").read_text())["log"]["entries"]
        stored = (run / entry["request"]["_postDataFile"]).read_bytes()
        assert self.SECRET.encode() not in stored
        assert stored == recorded.replace(self.SECRET.encode(), b"*" * len(self.SECRET))
        assert [e["file"] for e in logs if e["event"] == "request_body_file_scrubbed"] == [
            entry["request"]["_postDataFile"]
        ]

    @pytest.mark.asyncio
    async def test_a_binary_body_without_the_secret_is_left_untouched(self, tmp_path: Any) -> None:
        run, logs = await self._saved_run(tmp_path, _b64(BINARY))

        (entry,) = json.loads((run / "network.har").read_text())["log"]["entries"]
        assert (run / entry["request"]["_postDataFile"]).read_bytes() == BINARY
        assert [e for e in logs if e["event"].startswith("request_body_file_")] == []


class TestRequestBodyFileRedactionEdges:
    """redact_request_body_files on its own: what it skips, and what a failure costs."""

    SECRET = TestRequestBodyFilesAreRedacted.SECRET

    def _entries(self, *refs: str) -> list[dict[str, Any]]:
        return [{"request": {"_postDataFile": ref, "_postDataSize": 0}} for ref in refs]

    def test_a_file_outside_the_run_is_not_touched(self, tmp_path: Any) -> None:
        from graftpunk.observe.run import redact_request_body_files

        run = tmp_path / "run"
        (run / "bodies").mkdir(parents=True)
        outside = tmp_path / "outside.txt"
        outside.write_text(f"pw={self.SECRET}")
        (run / "bodies" / "link.json").symlink_to(outside)

        redact_request_body_files(
            self._entries("../outside.txt", "bodies/link.json"), [self.SECRET], run
        )

        assert outside.read_text() == f"pw={self.SECRET}"

    def test_a_missing_file_is_skipped_and_the_rest_are_scrubbed(self, tmp_path: Any) -> None:
        from graftpunk.observe.run import redact_request_body_files

        (tmp_path / "bodies").mkdir()
        kept = tmp_path / "bodies" / "2.request.json"
        kept.write_text(f"pw={self.SECRET}")

        with capture_logs() as logs:
            redact_request_body_files(
                self._entries("bodies/gone.json", "bodies/2.request.json"), [self.SECRET], tmp_path
            )

        assert self.SECRET not in kept.read_text()
        # A file that is not there has nothing in it to scrub: skipped, not an error.
        assert [e for e in logs if e["event"] == "request_body_file_not_scrubbed"] == []

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
    def test_an_unreadable_file_is_logged_and_the_rest_are_scrubbed(self, tmp_path: Any) -> None:
        from graftpunk.observe.run import redact_request_body_files

        (tmp_path / "bodies").mkdir()
        locked = tmp_path / "bodies" / "1.request.json"
        locked.write_text(f"pw={self.SECRET}")
        locked.chmod(0)
        kept = tmp_path / "bodies" / "2.request.json"
        kept.write_text(f"pw={self.SECRET}")

        try:
            with capture_logs() as logs:
                redact_request_body_files(
                    self._entries("bodies/1.request.json", "bodies/2.request.json"),
                    [self.SECRET],
                    tmp_path,
                )
        finally:
            locked.chmod(0o600)

        assert self.SECRET not in kept.read_text()
        assert [
            (e["file"], e["reason"]) for e in logs if e["event"] == "request_body_file_not_scrubbed"
        ] == [("bodies/1.request.json", "error")]

    @pytest.mark.parametrize("content", ["text", "binary"])
    def test_a_failed_rewrite_leaves_the_file_whole(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, content: str
    ) -> None:
        """The rewrite is a temporary file beside the body file, renamed over it; a
        failed rename leaves the original as it was and no temporary file behind."""
        from graftpunk.observe import run as run_module

        (tmp_path / "bodies").mkdir()
        body = tmp_path / "bodies" / "1.request.json"
        recorded = f"pw={self.SECRET}".encode()
        if content == "binary":
            recorded = b"\xff" + recorded
        body.write_bytes(recorded)
        renamed_from: list[Path] = []

        def no_space(src: Any, dst: Any) -> None:
            renamed_from.append(Path(src))
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(run_module.os, "replace", no_space)
        run_module.redact_request_body_files(
            self._entries("bodies/1.request.json"), [self.SECRET], tmp_path
        )

        assert body.read_bytes() == recorded
        assert [src.parent for src in renamed_from] == [body.parent]
        assert sorted(p.name for p in (tmp_path / "bodies").iterdir()) == ["1.request.json"]

    def test_a_file_with_nothing_to_scrub_is_not_rewritten(self, tmp_path: Any) -> None:
        from graftpunk.observe.run import redact_request_body_files

        (tmp_path / "bodies").mkdir()
        body = tmp_path / "bodies" / "1.request.json"
        body.write_text('{"q": "nothing secret"}')
        inode = body.stat().st_ino

        redact_request_body_files(self._entries("bodies/1.request.json"), [self.SECRET], tmp_path)

        assert body.stat().st_ino == inode

    @pytest.mark.parametrize("mode", [0o644, 0o640, 0o444])
    @pytest.mark.parametrize("content", ["text", "binary"])
    def test_a_scrubbed_file_keeps_its_mode(self, tmp_path: Any, mode: int, content: str) -> None:
        """The rewrite goes through a temporary file, which mkstemp creates as 0600."""
        from graftpunk.observe.run import redact_request_body_files

        (tmp_path / "bodies").mkdir()
        body = tmp_path / "bodies" / "1.request.json"
        payload = f"pw={self.SECRET}".encode()
        body.write_bytes(payload if content == "text" else b"\xff" + payload)
        body.chmod(mode)

        redact_request_body_files(self._entries("bodies/1.request.json"), [self.SECRET], tmp_path)

        assert self.SECRET.encode() not in body.read_bytes()
        assert stat.S_IMODE(body.stat().st_mode) == mode

    def test_a_secret_inside_a_longer_form_is_masked_whole(self) -> None:
        """Longest form first: masking a short form that sits inside a longer one
        first would leave the longer form's other bytes behind."""
        from graftpunk.observe.run import _scrub_bytes

        assert _scrub_bytes(b"\xff[abcdefgh]", ["abc", "abcdefgh"]) == b"\xff[********]"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
    def test_the_scrub_logs_carry_no_secret(self, tmp_path: Any) -> None:
        """The scrubbed-file and not-scrubbed lines name the file and why, nothing else."""
        from graftpunk.observe.run import redact_request_body_files

        (tmp_path / "bodies").mkdir()
        binary = tmp_path / "bodies" / "1.request.bin"
        binary.write_bytes(b"\xff" + f"pw={self.SECRET}".encode())
        locked = tmp_path / "bodies" / "2.request.json"
        locked.write_text(f"pw={self.SECRET}")
        locked.chmod(0)

        try:
            with capture_logs() as logs:
                redact_request_body_files(
                    self._entries("bodies/1.request.bin", "bodies/2.request.json"),
                    [self.SECRET],
                    tmp_path,
                )
        finally:
            locked.chmod(0o600)

        events = {e["event"] for e in logs}
        assert {"request_body_file_scrubbed", "request_body_file_not_scrubbed"} <= events
        assert all(self.SECRET not in repr(e) for e in logs)


# A login modal a script rendered after a click: in no HTML response, only in the
# live DOM at the moment the credential post was sent (#267, part 2).
_LOGIN_MODAL = (
    '<html><head><script>var token = "abc";</script></head><body>'
    '<div id="app"><div class="modal" id="login-modal">'
    '<input type="email" id="login-username" name="username" value="alice@example.com">'
    '<input type="password" id="login-password" name="password" value="hunter2hunter2">'
    '<button type="submit" id="login-submit">Sign in</button></div></div></body></html>'
)
_LOGIN_BODY = '{"username": "alice@example.com", "password": "hunter2hunter2"}'


class TestDomSnapshotAtTheCredentialPost:
    """With ``dom_snapshots_dir`` set (``gp observe`` only), a credential-shaped POST
    snapshots the page's DOM, sanitized and redacted, into ``snapshots/``."""

    async def _run(
        self,
        tmp_path: Path,
        body: str,
        *,
        inline: bool = True,
        snapshots: bool = True,
        dom_html: str | None = _LOGIN_MODAL,
    ) -> tuple[FakeTab, NodriverCaptureBackend, list[dict[str, Any]]]:
        tab = FakeTab({"21.1": body}, _always)
        tab.dom_html = dom_html
        tab.dom_url = "https://myshop.example/account?next=%2Forders#top"
        kwargs: dict[str, Any] = {"bodies_dir": tmp_path / "bodies"}
        if snapshots:
            kwargs["dom_snapshots_dir"] = tmp_path / "snapshots"
        with capture_logs() as logs:
            backend = await _record(
                tab,
                [
                    _will_be_sent("21.1", inline=body if inline else None, url=LOGIN),
                    _response_received("21.1", url=LOGIN),
                    _loading_finished("21.1"),
                ],
                **kwargs,
            )
        return tab, backend, logs

    @pytest.mark.asyncio
    async def test_a_credential_post_writes_the_snapshot_and_links_it(self, tmp_path: Path) -> None:
        tab, backend, _logs = await self._run(tmp_path, _LOGIN_BODY)

        request = _only_entry(backend)["request"]
        assert request["_domSnapshot"] == "snapshots/21.1.html"
        assert request["_domSnapshotUrl"] == "https://myshop.example/account"
        assert request["postData"]["text"] == _LOGIN_BODY
        stored = tmp_path / "snapshots" / "21.1.html"
        assert stat.S_IMODE(stored.stat().st_mode) == 0o600
        text = stored.read_text()
        assert 'id="login-password"' in text
        assert "hunter2hunter2" not in text
        assert "alice@example.com" not in text
        assert "value=" not in text
        assert "token" not in text
        assert tab.dom_requests == ["DOM.getDocument", "DOM.getOuterHTML", "DOM.disable"]

    @pytest.mark.asyncio
    async def test_a_post_without_a_password_field_takes_no_snapshot(self, tmp_path: Path) -> None:
        tab, backend, _logs = await self._run(tmp_path, BODY)

        assert "_domSnapshot" not in _only_entry(backend)["request"]
        assert tab.dom_requests == []
        assert not (tmp_path / "snapshots").exists()

    @pytest.mark.asyncio
    async def test_a_new_password_post_takes_no_snapshot(self, tmp_path: Path) -> None:
        body = '{"email": "alice@example.com", "new_password": "x1y2z3", "password_confirm": "x"}'
        tab, backend, _logs = await self._run(tmp_path, body)

        assert "_domSnapshot" not in _only_entry(backend)["request"]
        assert tab.dom_requests == []

    @pytest.mark.asyncio
    async def test_a_failed_snapshot_leaves_the_har_as_it_was_and_logs(
        self, tmp_path: Path
    ) -> None:
        _tab, backend, logs = await self._run(tmp_path, _LOGIN_BODY, dom_html=None)

        request = _only_entry(backend)["request"]
        assert request["postData"]["text"] == _LOGIN_BODY
        assert "_domSnapshot" not in request
        assert "_domSnapshotUrl" not in request
        failed = [e for e in logs if e["event"] == "nodriver_dom_snapshot_failed"]
        assert [(e["log_level"], e["request_id"]) for e in failed] == [("debug", "21.1")]
        (summary,) = [e for e in logs if e["event"] == "nodriver_dom_snapshot_summary"]
        assert (summary["failed"], summary["attempted"]) == (1, 1)
        assert not (tmp_path / "snapshots").exists()

    @pytest.mark.asyncio
    async def test_without_the_flag_nothing_is_snapshotted(self, tmp_path: Path) -> None:
        tab, backend, _logs = await self._run(tmp_path, _LOGIN_BODY, snapshots=False)

        assert "_domSnapshot" not in _only_entry(backend)["request"]
        assert tab.dom_requests == []
        assert not (tmp_path / "snapshots").exists()

    @pytest.mark.asyncio
    async def test_a_body_fetched_after_the_event_still_triggers(self, tmp_path: Path) -> None:
        tab, backend, _logs = await self._run(tmp_path, _LOGIN_BODY, inline=False)

        request = _only_entry(backend)["request"]
        assert tab.post_data_requests == ["21.1"]
        assert request["_domSnapshot"] == "snapshots/21.1.html"
        assert "hunter2hunter2" not in (tmp_path / "snapshots" / "21.1.html").read_text()

    @pytest.mark.asyncio
    async def test_one_snapshot_per_credential_post(self, tmp_path: Path) -> None:
        tab, _backend, _logs = await self._run(tmp_path, _LOGIN_BODY)

        assert tab.dom_requests.count("DOM.getDocument") == 1
        assert sorted(p.name for p in (tmp_path / "snapshots").iterdir()) == ["21.1.html"]

    @pytest.mark.asyncio
    async def test_a_saved_run_keeps_the_snapshot_beside_the_har(self, tmp_path: Path) -> None:
        from graftpunk.har.parser import parse_har_file
        from graftpunk.observe.run import save_observe_run
        from graftpunk.observe.storage import ObserveStorage

        storage = ObserveStorage(tmp_path, "myshop", "r1")
        tab = FakeTab({}, _never)
        tab.dom_html = _LOGIN_MODAL
        backend = NodriverCaptureBackend(
            object(),
            get_tab=lambda: tab,
            bodies_dir=storage.run_dir / "bodies",
            dom_snapshots_dir=storage.run_dir / "snapshots",
        )
        await backend.start_capture_async()
        for event in [
            _will_be_sent("22.1", inline=_LOGIN_BODY, url=LOGIN),
            _response_received("22.1", url=LOGIN),
        ]:
            await tab.dispatch(event)
        await save_observe_run(storage, backend, "observe-go")

        (entry,) = parse_har_file(storage.run_dir / "network.har").entries
        assert entry.request.dom_snapshot_url == HOME
        assert entry.request.dom_snapshot is not None
        assert 'id="login-modal"' in entry.request.dom_snapshot


# The wallTime _will_be_sent gives every request, in ms since the epoch.
_POST_MS = 1_700_000_000_000


def _binding_called(html: str, *, time_ms: float, url: str = LOGIN, name: str = "") -> Any:
    from graftpunk.observe.dom_listener import BINDING_NAME

    return parse_json_event(
        {
            "method": "Runtime.bindingCalled",
            "params": {
                "name": name or BINDING_NAME,
                "payload": json.dumps({"html": html, "url": url, "time": time_ms}),
                "executionContextId": 7,
            },
        }
    )


class TestSnapshotFromTheIsolatedWorldListener:
    """The page's own snapshot, sent through the binding as the user acted, is the
    one a credential post takes: one not after the post, on the post's page origin."""

    _LIVE = '<html><body><div id="after-the-response"></div></body></html>'

    async def _run(
        self, tmp_path: Path, before: list[Any], *, inline: bool = True, live: str | None = None
    ) -> tuple[FakeTab, NodriverCaptureBackend, list[dict[str, Any]]]:
        tab = FakeTab({"31.1": _LOGIN_BODY}, _always)
        tab.dom_html = live
        with capture_logs() as logs:
            backend = await _record(
                tab,
                [
                    *before,
                    _will_be_sent("31.1", inline=_LOGIN_BODY if inline else None, url=LOGIN),
                    _response_received("31.1", url=LOGIN),
                    _loading_finished("31.1"),
                ],
                dom_snapshots_dir=tmp_path / "snapshots",
            )
        return tab, backend, logs

    def _stored(self, tmp_path: Path) -> str:
        return (tmp_path / "snapshots" / "31.1.html").read_text()

    @pytest.mark.asyncio
    async def test_the_listener_is_installed_in_an_isolated_world(self, tmp_path: Path) -> None:
        from graftpunk.observe.dom_listener import BINDING_NAME, LISTENER_SCRIPT, WORLD_NAME

        tab, _backend, _logs = await self._run(tmp_path, [])

        methods = [method for method, _params in tab.sent]
        assert methods.index("Page.enable") < methods.index("Page.addScriptToEvaluateOnNewDocument")
        sent = dict(tab.sent)
        assert sent["Runtime.addBinding"] == {
            "name": BINDING_NAME,
            "executionContextName": WORLD_NAME,
        }
        assert sent["Page.addScriptToEvaluateOnNewDocument"] == {
            "source": LISTENER_SCRIPT,
            "worldName": WORLD_NAME,
            "runImmediately": True,
        }

    @pytest.mark.asyncio
    async def test_without_the_flag_nothing_is_installed(self) -> None:
        tab = FakeTab({}, _never)
        await _record(tab, [_will_be_sent("31.2", inline=_LOGIN_BODY, url=LOGIN)])
        methods = {method for method, _params in tab.sent}
        assert "Runtime.addBinding" not in methods
        assert "Page.addScriptToEvaluateOnNewDocument" not in methods

    @pytest.mark.asyncio
    async def test_a_listener_that_cannot_be_installed_leaves_the_capture_running(
        self, tmp_path: Path
    ) -> None:
        tab = FakeTab({}, _never)
        tab.refuse = {"Page.addScriptToEvaluateOnNewDocument"}
        tab.dom_html = _LOGIN_MODAL
        with capture_logs() as logs:
            backend = await _record(
                tab,
                [_will_be_sent("31.3", inline=_LOGIN_BODY, url=LOGIN)],
                dom_snapshots_dir=tmp_path / "snapshots",
            )
        assert [e["event"] for e in logs if e["event"] == "nodriver_dom_listener_unavailable"]
        assert _only_entry(backend)["request"]["_domSnapshot"] == "snapshots/31.3.html"

    @pytest.mark.asyncio
    async def test_a_snapshot_sent_before_the_post_is_the_post_s(self, tmp_path: Path) -> None:
        tab, backend, logs = await self._run(
            tmp_path, [_binding_called(_LOGIN_MODAL, time_ms=_POST_MS - 40)]
        )

        request = _only_entry(backend)["request"]
        assert request["_domSnapshot"] == "snapshots/31.1.html"
        assert request["_domSnapshotUrl"] == LOGIN
        stored = self._stored(tmp_path)
        assert 'id="login-password"' in stored
        assert "hunter2hunter2" not in stored
        assert "alice@example.com" not in stored
        # Sanitized, not only redacted (review I2): no value attribute and no script.
        assert "value=" not in stored
        assert "token" not in stored
        assert "<script></script>" in stored
        assert tab.dom_requests == []
        assert not [e for e in logs if e["event"] == "nodriver_dom_snapshot_late"]

    @pytest.mark.asyncio
    async def test_a_snapshot_at_the_post_s_own_millisecond_still_counts(
        self, tmp_path: Path
    ) -> None:
        """CDP's wallTime is seconds since the epoch, the page's Date.now() ms."""
        await self._run(tmp_path, [_binding_called(_LOGIN_MODAL, time_ms=_POST_MS)])
        assert 'id="login-modal"' in self._stored(tmp_path)

    @pytest.mark.asyncio
    async def test_a_snapshot_sent_after_the_post_is_not_used(self, tmp_path: Path) -> None:
        tab, _backend, logs = await self._run(
            tmp_path, [_binding_called(_LOGIN_MODAL, time_ms=_POST_MS + 1)], live=self._LIVE
        )

        stored = self._stored(tmp_path)
        assert "after-the-response" in stored
        assert "login-modal" not in stored
        assert tab.dom_requests == ["DOM.getDocument", "DOM.getOuterHTML", "DOM.disable"]
        assert [e["request_id"] for e in logs if e["event"] == "nodriver_dom_snapshot_late"] == [
            "31.1"
        ]

    @pytest.mark.asyncio
    async def test_a_snapshot_from_another_origin_is_not_used(self, tmp_path: Path) -> None:
        await self._run(
            tmp_path,
            [
                _binding_called(
                    _LOGIN_MODAL, time_ms=_POST_MS - 40, url="https://ads.example.net/frame"
                )
            ],
            live=self._LIVE,
        )
        assert "login-modal" not in self._stored(tmp_path)

    @pytest.mark.asyncio
    async def test_the_latest_qualifying_snapshot_wins(self, tmp_path: Path) -> None:
        older = _LOGIN_MODAL.replace('id="login-modal"', 'id="older-modal"')
        await self._run(
            tmp_path,
            [
                _binding_called(older, time_ms=_POST_MS - 900),
                _binding_called(_LOGIN_MODAL, time_ms=_POST_MS - 40),
                _binding_called(older, time_ms=_POST_MS + 50),
            ],
        )
        stored = self._stored(tmp_path)
        assert 'id="login-modal"' in stored
        assert "older-modal" not in stored

    @pytest.mark.asyncio
    async def test_only_the_last_few_snapshots_are_kept(self, tmp_path: Path) -> None:
        from graftpunk.observe.capture import DOM_SNAPSHOT_BUFFER

        later = [
            _binding_called("<p>x</p>", time_ms=_POST_MS - 10, url="https://other.example/")
            for _ in range(DOM_SNAPSHOT_BUFFER)
        ]
        await self._run(
            tmp_path,
            [_binding_called(_LOGIN_MODAL, time_ms=_POST_MS - 500), *later],
            live=self._LIVE,
        )
        assert "login-modal" not in self._stored(tmp_path)

    @pytest.mark.asyncio
    async def test_another_binding_s_call_is_ignored(self, tmp_path: Path) -> None:
        await self._run(
            tmp_path,
            [_binding_called(_LOGIN_MODAL, time_ms=_POST_MS - 40, name="somethingElse")],
            live=self._LIVE,
        )
        assert "login-modal" not in self._stored(tmp_path)

    @pytest.mark.asyncio
    async def test_a_malformed_payload_is_dropped(self, tmp_path: Path) -> None:
        from graftpunk.observe.dom_listener import BINDING_NAME

        bad = parse_json_event(
            {
                "method": "Runtime.bindingCalled",
                "params": {"name": BINDING_NAME, "payload": "not json", "executionContextId": 7},
            }
        )
        await self._run(tmp_path, [bad], live=self._LIVE)
        assert "after-the-response" in self._stored(tmp_path)

    @pytest.mark.asyncio
    async def test_a_body_fetched_late_takes_the_listener_s_snapshot(self, tmp_path: Path) -> None:
        tab, _backend, _logs = await self._run(
            tmp_path, [_binding_called(_LOGIN_MODAL, time_ms=_POST_MS - 40)], inline=False
        )
        assert tab.post_data_requests == ["31.1"]
        assert 'id="login-modal"' in self._stored(tmp_path)

    @pytest.mark.asyncio
    async def test_with_no_snapshot_and_no_page_the_har_is_intact(self, tmp_path: Path) -> None:
        _tab, backend, logs = await self._run(tmp_path, [])
        request = _only_entry(backend)["request"]
        assert "_domSnapshot" not in request
        assert request["postData"]["text"] == _LOGIN_BODY
        assert [e["event"] for e in logs if e["event"] == "nodriver_dom_snapshot_failed"]

    @pytest.mark.asyncio
    async def test_a_snapshot_older_than_the_limit_is_not_used(self, tmp_path: Path) -> None:
        from graftpunk.observe.capture import DOM_SNAPSHOT_MAX_AGE_MS

        await self._run(
            tmp_path,
            [_binding_called(_LOGIN_MODAL, time_ms=_POST_MS - DOM_SNAPSHOT_MAX_AGE_MS - 1)],
            live=self._LIVE,
        )
        assert "login-modal" not in self._stored(tmp_path)

    @pytest.mark.asyncio
    async def test_a_snapshot_just_inside_the_limit_is_used(self, tmp_path: Path) -> None:
        from graftpunk.observe.capture import DOM_SNAPSHOT_MAX_AGE_MS

        await self._run(
            tmp_path,
            [_binding_called(_LOGIN_MODAL, time_ms=_POST_MS - DOM_SNAPSHOT_MAX_AGE_MS)],
            live=self._LIVE,
        )
        assert 'id="login-modal"' in self._stored(tmp_path)

    @pytest.mark.asyncio
    async def test_a_snapshot_attaches_to_one_post_only(self, tmp_path: Path) -> None:
        tab = FakeTab({}, _never)
        tab.dom_html = self._LIVE
        await _record(
            tab,
            [
                _binding_called(_LOGIN_MODAL, time_ms=_POST_MS - 40),
                _will_be_sent("32.1", inline=_LOGIN_BODY, url=LOGIN),
                _will_be_sent("32.2", inline=_LOGIN_BODY, url=LOGIN),
            ],
            dom_snapshots_dir=tmp_path / "snapshots",
        )
        assert 'id="login-modal"' in (tmp_path / "snapshots" / "32.1.html").read_text()
        assert "login-modal" not in (tmp_path / "snapshots" / "32.2.html").read_text()

    @pytest.mark.asyncio
    async def test_a_navigation_post_is_matched_to_the_page_that_sent_it(
        self, tmp_path: Path
    ) -> None:
        """Review M3: a form post navigates, and Chrome gives a navigation request the
        URL being loaded as its documentURL; the page that sent it is the Referer."""
        target = "https://auth.myshop-sso.example/session"
        tab = FakeTab({}, _never)
        await _record(
            tab,
            [
                _binding_called(_LOGIN_MODAL, time_ms=_POST_MS - 40),
                _will_be_sent(
                    "33.1",
                    inline="username=alice%40example.com&password=hunter2hunter2",
                    url=target,
                    document_url=target,
                    resource_type="Document",
                    headers={
                        "Referer": "https://myshop.example/",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                ),
            ],
            dom_snapshots_dir=tmp_path / "snapshots",
        )
        stored = (tmp_path / "snapshots" / "33.1.html").read_text()
        assert 'id="login-modal"' in stored
        assert "hunter2hunter2" not in stored
        assert tab.dom_requests == []

    @pytest.mark.asyncio
    async def test_a_redirect_handled_before_the_task_keeps_the_post_s_snapshot(
        self, tmp_path: Path
    ) -> None:
        """Review I2 (superpowers): CDP reuses the request id for the redirect, and the
        snapshot is bound to the POST's own entry when its request is seen."""
        tab = FakeTab({}, _never)
        backend = await _start(tab, dom_snapshots_dir=tmp_path / "snapshots")
        await tab.dispatch(_binding_called(_LOGIN_MODAL, time_ms=_POST_MS - 40))
        await tab.dispatch(_will_be_sent("34.1", inline=_LOGIN_BODY, url=LOGIN), settle=False)
        await tab.dispatch(_will_be_sent("34.1", url=HOME, method="GET", redirect_from=LOGIN))
        await tab.dispatch(_response_received("34.1", url=HOME))
        await _stop(backend, tab)

        post = next(e for e in backend.get_har_entries() if e["request"]["method"] == "POST")
        assert post["request"]["_domSnapshot"] == "snapshots/34.1.html"
        assert 'id="login-modal"' in (tmp_path / "snapshots" / "34.1.html").read_text()
        assert all("_dom_snapshot_due" not in data for data in backend._request_map.values())

    @pytest.mark.asyncio
    async def test_a_post_that_is_not_a_post_takes_no_snapshot(self, tmp_path: Path) -> None:
        tab = FakeTab({}, _never)
        tab.dom_html = _LOGIN_MODAL
        backend = await _record(
            tab,
            [
                _binding_called(_LOGIN_MODAL, time_ms=_POST_MS - 40),
                _will_be_sent("35.1", inline=_LOGIN_BODY, url=LOGIN, method="PUT"),
            ],
            dom_snapshots_dir=tmp_path / "snapshots",
        )
        assert "_domSnapshot" not in _only_entry(backend)["request"]
        assert not (tmp_path / "snapshots").exists()

    @pytest.mark.asyncio
    async def test_a_header_only_capture_never_snapshots(self, tmp_path: Path) -> None:
        tab = FakeTab({}, _never)
        tab.dom_html = _LOGIN_MODAL
        await _record(
            tab,
            [_will_be_sent("36.1", inline=_LOGIN_BODY, url=LOGIN)],
            dom_snapshots_dir=tmp_path / "snapshots",
            record_request_bodies=False,
        )
        assert not (tmp_path / "snapshots").exists()
        assert "Page.addScriptToEvaluateOnNewDocument" not in {m for m, _p in tab.sent}

    @pytest.mark.asyncio
    async def test_a_page_over_the_size_cap_is_skipped_and_counted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(capture, "DOM_SNAPSHOT_MAX_CHARS", 100)
        _tab, _backend, logs = await self._run(
            tmp_path, [_binding_called(_LOGIN_MODAL, time_ms=_POST_MS - 40)], live=_LOGIN_MODAL
        )
        assert not (tmp_path / "snapshots").exists()
        (summary,) = [e for e in logs if e["event"] == "nodriver_dom_snapshot_summary"]
        assert summary["too_large"] == 2

    @pytest.mark.asyncio
    async def test_the_late_read_of_another_origin_is_not_stored(self, tmp_path: Path) -> None:
        tab = FakeTab({"37.1": _LOGIN_BODY}, _always)
        tab.dom_html = self._LIVE
        tab.dom_url = "https://myshop-account.example/home"
        with capture_logs() as logs:
            backend = await _record(
                tab,
                [_will_be_sent("37.1", inline=_LOGIN_BODY, url=LOGIN)],
                dom_snapshots_dir=tmp_path / "snapshots",
            )
        assert "_domSnapshot" not in _only_entry(backend)["request"]
        assert [e["request_id"] for e in logs if e["event"] == "nodriver_dom_snapshot_failed"] == [
            "37.1"
        ]
        assert "DOM.disable" in tab.dom_requests

    @pytest.mark.asyncio
    async def test_a_listener_snapshot_landing_during_the_read_is_preferred(
        self, tmp_path: Path
    ) -> None:
        """Review M8: Runtime.bindingCalled can arrive after requestWillBeSent."""
        tab = FakeTab({}, _never)
        tab.dom_html = self._LIVE
        late_event = _binding_called(_LOGIN_MODAL, time_ms=_POST_MS - 40)
        tab.on_dom_read = lambda: [
            callback(late_event) for callback in tab.handlers[type(late_event)]
        ]
        with capture_logs() as logs:
            await _record(
                tab,
                [_will_be_sent("38.1", inline=_LOGIN_BODY, url=LOGIN)],
                dom_snapshots_dir=tmp_path / "snapshots",
            )
        assert 'id="login-modal"' in (tmp_path / "snapshots" / "38.1.html").read_text()
        assert not [e for e in logs if e["event"] == "nodriver_dom_snapshot_late"]

    @pytest.mark.asyncio
    async def test_a_listener_snapshot_landing_during_a_failed_read_is_kept(
        self, tmp_path: Path
    ) -> None:
        tab = FakeTab({}, _never)
        tab.dom_html = None
        late_event = _binding_called(_LOGIN_MODAL, time_ms=_POST_MS - 40)
        tab.on_dom_read = lambda: [
            callback(late_event) for callback in tab.handlers[type(late_event)]
        ]
        await _record(
            tab,
            [_will_be_sent("39.1", inline=_LOGIN_BODY, url=LOGIN)],
            dom_snapshots_dir=tmp_path / "snapshots",
        )
        assert 'id="login-modal"' in (tmp_path / "snapshots" / "39.1.html").read_text()

    @pytest.mark.asyncio
    async def test_a_read_still_running_at_stop_is_cancelled_and_counted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(capture, "_POST_DATA_SETTLE_TIMEOUT_S", 0.05)
        tab = FakeTab({}, _never)
        tab.dom_html = self._LIVE
        tab.dom_gate = asyncio.Event()
        with capture_logs() as logs:
            backend = await _record(
                tab,
                [_will_be_sent("40.1", inline=_LOGIN_BODY, url=LOGIN)],
                dom_snapshots_dir=tmp_path / "snapshots",
            )
        assert "_domSnapshot" not in _only_entry(backend)["request"]
        assert [
            e["request_id"] for e in logs if e["event"] == "nodriver_dom_snapshot_cancelled"
        ] == ["40.1"]
        (summary,) = [e for e in logs if e["event"] == "nodriver_dom_snapshot_summary"]
        assert summary["failed"] == 1
