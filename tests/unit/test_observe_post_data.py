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
) -> Any:
    request: dict[str, Any] = {
        "url": url,
        "method": method,
        "headers": {"Content-Type": "application/json"},
        "initialPriority": "High",
        "referrerPolicy": "strict-origin-when-cross-origin",
    }
    if method == "POST":
        request["hasPostData"] = True
    if inline is not None:
        request["postData"] = inline
    params: dict[str, Any] = {
        "requestId": rid,
        "loaderId": "loader-1",
        "documentURL": HOME,
        "request": request,
        "timestamp": 1.0,
        "wallTime": 1_700_000_000.0,
        "initiator": {"type": "script"},
        "redirectHasExtraInfo": False,
        "type": "Fetch",
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
        bodies: dict[str, str],
        post_data_available: Callable[[str, FakeTab], bool],
        response_available: Callable[[str, FakeTab], bool] = lambda _rid, _t: True,
    ) -> None:
        self.bodies = bodies
        self.post_data_available = post_data_available
        self.response_available = response_available
        self.handlers: dict[type, list[Callable[..., Any]]] = {}
        self.finished: set[str] = set()
        self.stopping = False
        self.session_id: str | None = "session-1"
        self.network_sessions: set[str | None] = set()
        self.post_data_requests: list[str] = []

    def reattach(self) -> None:
        assert self.session_id is not None
        self.session_id = f"session-{int(self.session_id.rsplit('-', 1)[1]) + 1}"

    def add_handler(self, event_type: type, callback: Callable[..., Any]) -> None:
        self.handlers.setdefault(event_type, []).append(callback)

    async def send(self, cmd: Any, **kwargs: Any) -> Any:
        return self._answer(cmd, kwargs.get("sessionId", self.session_id))

    def _answer(self, cmd: Any, session: str | None, *, post_data_now: bool | None = None) -> Any:
        message = next(cmd)
        method = message["method"]
        rid = message.get("params", {}).get("requestId")
        enabled = session in self.network_sessions
        if method == "Network.enable":
            self.network_sessions.add(session)
        if method == "Network.getRequestPostData":
            self.post_data_requests.append(rid)
            available = (
                self.post_data_available(rid, self) if post_data_now is None else post_data_now
            )
            if not enabled or rid not in self.bodies or not available:
                raise _refused()
            reply: dict[str, Any] = {"postData": self.bodies[rid], "base64Encoded": False}
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


async def _start(tab: FakeTab, **kwargs: Any) -> NodriverCaptureBackend:
    backend = NodriverCaptureBackend(object(), get_tab=lambda: tab, **kwargs)
    await backend.start_capture_async()
    return backend


async def _stop(backend: NodriverCaptureBackend, tab: FakeTab) -> None:
    tab.stopping = True
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
    """A request-time body that arrives after a delay; Chrome refuses it at stop."""

    def __init__(self, delay: float) -> None:
        super().__init__({"8.6": BODY}, _before_stop)
        self.delay = delay

    async def send(self, cmd: Any, **kwargs: Any) -> Any:
        asked_in_flight = not self.stopping
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


class TestBodySizeCap:
    """A request body over the cap a response body has is left out of the HAR, and
    the entry says so with its size."""

    @pytest.mark.asyncio
    async def test_an_inline_body_over_the_cap_is_left_out(self) -> None:
        tab = FakeTab({}, _never)

        with capture_logs() as logs:
            backend = await _record(tab, _fetch_post("8.7", inline=BODY), max_body_size=10)

        request = _only_entry(backend)["request"]
        assert "postData" not in request
        assert (request["_postDataNotCaptured"], request["_postDataOmittedSize"]) == (
            True,
            len(BODY),
        )
        assert [
            (e["request_id"], e["size"]) for e in logs if e["event"] == "post_data_omitted"
        ] == [("8.7", len(BODY))]
        assert _events(logs, "post_data_unavailable") == []
        assert tab.post_data_requests == []

    @pytest.mark.asyncio
    async def test_a_fetched_body_over_the_cap_is_left_out(self) -> None:
        tab = FakeTab({"8.8": BODY}, _always)

        backend = await _record(tab, _fetch_post("8.8"), max_body_size=10)

        request = _only_entry(backend)["request"]
        assert "postData" not in request
        assert request["_postDataOmittedSize"] == len(BODY)

    @pytest.mark.asyncio
    async def test_a_body_at_the_cap_is_kept(self) -> None:
        tab = FakeTab({"8.9": BODY}, _always)

        backend = await _record(tab, _fetch_post("8.9"), max_body_size=len(BODY))

        assert _only_entry(backend)["request"]["postData"]["text"] == BODY


def _selenium_backend(execute: Callable[[str, dict[str, Any]], dict[str, Any]], **kwargs: Any):
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

        assert _only_entry(backend)["request"]["_postDataOmittedSize"] == len(BODY)


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
        Whether nodriver 0.48 recovers a Blob body live is unverified."""
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
