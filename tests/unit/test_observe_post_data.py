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
import inspect
import json
from collections.abc import Callable
from typing import Any

import pytest
from nodriver.cdp.util import parse_json_event
from nodriver.core.connection import ProtocolException
from structlog.testing import capture_logs

from graftpunk.observe.capture import NodriverCaptureBackend, SeleniumCaptureBackend

SEARCH = "https://myshop.example/api/search"
BODY = '{"query": "alice", "page": 1}'


def _will_be_sent(rid: str, *, inline: str | None = None, url: str = SEARCH) -> Any:
    request: dict[str, Any] = {
        "url": url,
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "hasPostData": True,
        "initialPriority": "High",
        "referrerPolicy": "strict-origin-when-cross-origin",
    }
    if inline is not None:
        request["postData"] = inline
    return parse_json_event(
        {
            "method": "Network.requestWillBeSent",
            "params": {
                "requestId": rid,
                "loaderId": "loader-1",
                "documentURL": "https://myshop.example/",
                "request": request,
                "timestamp": 1.0,
                "wallTime": 1_700_000_000.0,
                "initiator": {"type": "script"},
                "redirectHasExtraInfo": False,
                "type": "Fetch",
            },
        }
    )


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
                "response": {
                    "url": url,
                    "status": 200,
                    "statusText": "OK",
                    "headers": {"Content-Type": "application/json"},
                    "mimeType": "application/json",
                    "charset": "utf-8",
                    "connectionReused": False,
                    "connectionId": 1,
                    "encodedDataLength": 20,
                    "securityState": "secure",
                },
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


class FakeTab:
    """A nodriver tab double: handler dispatch as nodriver does it, CDP replies as
    Chrome gives them.

    ``post_data_available(request_id, tab)`` says whether Chrome still answers
    ``getRequestPostData`` for that request; when it does not, the command fails
    with the ``-32000`` error Chrome sends. As in Chrome, it also fails when the
    command goes to a CDP session other than the one ``Network.enable`` went to:
    ``reattach()`` does what nodriver's ``Browser.get(url)`` does, a new session
    on the same target, after which ``send`` addresses the new one unless the
    caller passes ``sessionId``.
    """

    def __init__(
        self,
        bodies: dict[str, str],
        post_data_available: Callable[[str, FakeTab], bool],
    ) -> None:
        self.bodies = bodies
        self.post_data_available = post_data_available
        self.handlers: dict[type, list[Callable[..., Any]]] = {}
        self.finished: set[str] = set()
        self.session_id = "session-1"
        self.network_sessions: set[str] = set()

    def reattach(self) -> None:
        self.session_id = f"session-{int(self.session_id.rsplit('-', 1)[1]) + 1}"

    def add_handler(self, event_type: type, callback: Callable[..., Any]) -> None:
        self.handlers.setdefault(event_type, []).append(callback)

    async def send(self, cmd: Any, **kwargs: Any) -> Any:
        return self._answer(cmd, kwargs.get("sessionId", self.session_id))

    def _answer(self, cmd: Any, session: str) -> Any:
        message = next(cmd)
        method = message["method"]
        rid = message.get("params", {}).get("requestId")
        if method == "Network.enable":
            self.network_sessions.add(session)
        if method == "Network.getRequestPostData":
            if (
                session not in self.network_sessions
                or rid not in self.bodies
                or not self.post_data_available(rid, self)
            ):
                raise ProtocolException(
                    {"code": -32000, "message": "No resource with given id was found"}
                )
            reply: dict[str, Any] = {"postData": self.bodies[rid], "base64Encoded": False}
        elif method == "Network.getResponseBody":
            reply = {"body": '{"results": []}', "base64Encoded": False}
        else:
            reply = {}
        try:
            cmd.send(reply)
        except StopIteration as stop:
            return stop.value
        raise AssertionError(f"{method} did not finish on one reply")

    async def dispatch(self, event: Any) -> None:
        if type(event).__name__ == "LoadingFinished":
            self.finished.add(str(event.request_id))
        for callback in self.handlers.get(type(event), []):
            if inspect.iscoroutinefunction(callback):
                asyncio.create_task(callback(event))  # noqa: RUF006 - nodriver does the same
            else:
                callback(event)
        # Let the tasks nodriver would have scheduled run before the next event.
        for _ in range(5):
            await asyncio.sleep(0)


async def _record(tab: FakeTab, events: list[Any]) -> NodriverCaptureBackend:
    backend = NodriverCaptureBackend(object(), get_tab=lambda: tab)
    await backend.start_capture_async()
    for event in events:
        await tab.dispatch(event)
    await backend.stop_capture_async()
    return backend


def _fetch_post(rid: str) -> list[Any]:
    return [_will_be_sent(rid), _response_received(rid), _loading_finished(rid)]


def _only_entry(backend: Any) -> dict[str, Any]:
    (entry,) = backend.get_har_entries()
    return entry


class TestNodriverPostDataFetch:
    @pytest.mark.asyncio
    async def test_a_body_chrome_answers_for_only_while_in_flight_is_kept(self) -> None:
        """Fetched on requestWillBeSent, before the response is consumed: a body
        Chrome stops answering for once the request finished still reaches the HAR."""
        tab = FakeTab({"7.2": BODY}, lambda rid, t: rid not in t.finished)

        backend = await _record(tab, _fetch_post("7.2"))

        request = _only_entry(backend)["request"]
        assert request["postData"] == {"mimeType": "application/json", "text": BODY}
        assert "_postDataNotCaptured" not in request

    @pytest.mark.asyncio
    async def test_a_body_chrome_answers_for_only_at_stop_is_still_kept(self) -> None:
        """The stop-time fetch stays as the retry for a request-time failure."""
        stopping = {"now": False}
        tab = FakeTab({"7.3": BODY}, lambda _rid, _t: stopping["now"])
        backend = NodriverCaptureBackend(object(), get_tab=lambda: tab)
        await backend.start_capture_async()
        for event in _fetch_post("7.3"):
            await tab.dispatch(event)
        stopping["now"] = True
        await backend.stop_capture_async()

        assert _only_entry(backend)["request"]["postData"]["text"] == BODY

    @pytest.mark.asyncio
    async def test_an_inline_body_is_not_fetched_again(self) -> None:
        tab = FakeTab({}, lambda _rid, _t: False)

        backend = await _record(tab, [_will_be_sent("7.4", inline=BODY), _response_received("7.4")])

        request = _only_entry(backend)["request"]
        assert request["postData"]["text"] == BODY
        assert "_postDataNotCaptured" not in request

    @pytest.mark.asyncio
    async def test_a_body_chrome_never_answers_for_is_marked_and_logged(self) -> None:
        """A body Chrome will not return is said to be lost, not dropped silently."""
        tab = FakeTab({"7.5": BODY}, lambda _rid, _t: False)

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
    async def test_the_marker_survives_the_har_file_round_trip(self, tmp_path: Any) -> None:
        from graftpunk.har.parser import parse_har_file

        tab = FakeTab({}, lambda _rid, _t: False)
        backend = await _record(tab, _fetch_post("7.6"))
        har = tmp_path / "network.har"
        har.write_text(json.dumps({"log": {"entries": backend.get_har_entries()}}))

        (entry,) = parse_har_file(har).entries

        assert entry.request.post_data is None
        assert entry.request.post_data_not_captured is True

    @pytest.mark.asyncio
    async def test_a_dead_browser_marks_every_body_it_left_unfetched(self) -> None:
        tab = FakeTab({}, lambda _rid, _t: False)
        backend = NodriverCaptureBackend(object(), get_tab=lambda: tab)
        await backend.start_capture_async()
        for rid in ("8.1", "8.2"):
            for event in _fetch_post(rid):
                await tab.dispatch(event)

        async def dead(cmd: Any, **_kwargs: Any) -> Any:
            raise ConnectionError("browser went away")

        tab.send = dead  # type: ignore[method-assign]
        await backend.stop_capture_async()

        entries = backend.get_har_entries()
        assert [e["request"].get("_postDataNotCaptured") for e in entries] == [True, True]


class TestSeleniumPostDataFailure:
    def test_a_body_chrome_refuses_is_marked_and_logged(self) -> None:
        def perf(method: str, params: dict[str, Any]) -> dict[str, str]:
            return {"message": json.dumps({"message": {"method": method, "params": params}})}

        log = [
            perf(
                "Network.requestWillBeSent",
                {
                    "requestId": "9.1",
                    "request": {
                        "url": SEARCH,
                        "method": "POST",
                        "headers": {"Content-Type": "application/json"},
                        "hasPostData": True,
                    },
                    "wallTime": 1_700_000_000.0,
                },
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
                if method == "Network.getRequestPostData":
                    raise RuntimeError("No resource with given id was found")
                return {"body": "{}", "base64Encoded": False}

        backend = SeleniumCaptureBackend(Driver())
        with capture_logs() as logs:
            backend.stop_capture()

        request = _only_entry(backend)["request"]
        assert request["_postDataNotCaptured"] is True
        assert [e["request_id"] for e in logs if e["event"] == "post_data_unavailable"] == ["9.1"]


class FakeTab048(FakeTab):
    """nodriver 0.48's ``send(cdp_obj, _is_update=False)``: no session can be named."""

    async def send(self, cmd: Any, _is_update: bool = False) -> Any:  # type: ignore[override]
        return self._answer(cmd, self.session_id)


class TestCaptureSession:
    """#269's cause, measured live on nodriver 0.50.6: ``gp observe`` navigates with
    ``browser.get(url)``, which attaches a second CDP session to the tab, and Chrome
    answers ``getRequestPostData`` only on the session that enabled Network."""

    async def _record_across_reattach(self, tab: FakeTab) -> NodriverCaptureBackend:
        backend = NodriverCaptureBackend(object(), get_tab=lambda: tab)
        await backend.start_capture_async()
        tab.reattach()
        for event in _fetch_post("9.2"):
            await tab.dispatch(event)
        tab.reattach()
        await backend.stop_capture_async()
        return backend

    @pytest.mark.asyncio
    async def test_bodies_are_fetched_from_the_session_capture_enabled(self) -> None:
        tab = FakeTab({"9.2": BODY}, lambda _rid, _t: True)

        backend = await self._record_across_reattach(tab)

        request = _only_entry(backend)["request"]
        assert request["postData"]["text"] == BODY
        assert "_postDataNotCaptured" not in request

    @pytest.mark.asyncio
    async def test_a_nodriver_that_cannot_name_a_session_marks_the_body(self) -> None:
        tab = FakeTab048({"9.2": BODY}, lambda _rid, _t: True)

        with capture_logs() as logs:
            backend = await self._record_across_reattach(tab)

        assert _only_entry(backend)["request"]["_postDataNotCaptured"] is True
        assert [e["request_id"] for e in logs if e["event"] == "post_data_unavailable"] == ["9.2"]
