"""Every headless nodriver browser graftpunk drives answers JavaScript dialogs (#320, #353).

A page's ``alert()`` stops its script, and its load, until someone answers it, and
nobody can in a headless browser. The fake browser here behaves that way: its
``Browser.get`` of a page that opens a dialog returns only once the dialog has been
answered through CDP, so a dialog nobody answers times the navigation out. The
pages the browser gains later are answered on a connection of the answerer's own
to the browser's endpoint, which :class:`ChromeEndpoint` stands in for.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nodriver.cdp.page import JavascriptDialogOpening
from nodriver.cdp.util import parse_json_event
from structlog.testing import capture_logs

from graftpunk import dialogs
from graftpunk.backends.nodriver import NoDriverBackend
from graftpunk.dialogs import _open_socket as _real_open_socket
from graftpunk.plugins import LoginConfig, LoginStep, SitePlugin
from tests.unit.conftest import FakeChrome, LoopBoundBrowser, LoopBoundTab
from tests.unit.test_observe_post_data import FakeTab, _always

pytestmark = pytest.mark.usefixtures("release_nodriver_loops")

LOGIN_URL = "https://myshop.example/login"
ENDPOINT_URL = "ws://127.0.0.1:9222/devtools/browser/fake"
_DIALOG_PARAMS = {
    "url": LOGIN_URL,
    "frameId": "frame-1",
    "message": "Welcome back",
    "type": "alert",
    "hasBrowserHandler": False,
}


def _alert() -> Any:
    return parse_json_event({"method": "Page.javascriptDialogOpening", "params": _DIALOG_PARAMS})


class DialogTab(LoopBoundTab):
    """A tab whose page opens an ``alert()`` while it loads (see :class:`DialogBrowser`)."""

    def __init__(self, url: str, misuse: list[str]) -> None:
        super().__init__(url, misuse)
        self.handlers: dict[type, list[Any]] = {}
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.closed: asyncio.Event | None = None

    def add_handler(self, event_type: type, callback: Any) -> None:
        self.handlers.setdefault(event_type, []).append(callback)

    async def send(self, cmd: Any) -> Any:
        await self._on_own_loop("Tab.send")
        request = next(cmd)
        method = request["method"]
        self.sent.append((method, dict(request.get("params", {}))))
        if method == "Page.handleJavaScriptDialog" and self.closed is not None:
            self.closed.set()
        try:
            cmd.send({})
        except StopIteration as stop:
            return stop.value
        raise AssertionError(f"{method} did not finish after one response")


async def _open_dialog(tab: DialogTab) -> None:
    """The tab's page opens an alert; returns once it is answered, as the page's
    script does."""
    tab.closed = asyncio.Event()
    for callback in tab.handlers.get(JavascriptDialogOpening, []):
        asyncio.create_task(callback(_alert()))  # noqa: RUF006 - nodriver does the same
    await tab.closed.wait()


class DialogBrowser(LoopBoundBrowser):
    """``Browser.get(LOGIN_URL)`` opens an alert and returns once it is answered."""

    def __init__(self, misuse: list[str], start_kwargs: dict[str, Any], chrome: Any) -> None:
        super().__init__(misuse, start_kwargs, chrome)
        self.tabs = [DialogTab("about:blank", misuse)]
        self.tabs[0].target = SimpleNamespace(type_="page", target_id="target-1")
        self.websocket_url = ENDPOINT_URL

    @property
    def targets(self) -> list[Any]:
        return self.tabs

    async def get(self, url: str) -> Any:
        tab = await super().get(url)
        if url == LOGIN_URL:
            await _open_dialog(tab)
        return tab


class ChromeEndpoint:
    """Chrome's browser endpoint as the new-tab answerer's own connection sees it.

    Each command is recorded and answered with an empty result, unless its method
    is in ``refuse`` (an error reply, ``refusal`` its message) or ``silent`` (no
    reply until the test sends one with :meth:`reply`). ``before_reply`` runs as each
    command arrives, before its reply. A test plays Chrome's side with
    :meth:`attach`, :meth:`dialog` and :meth:`detach`."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.sent: list[dict[str, Any]] = []
        self.refuse: set[str] = set()
        self.refusal = "no"
        self.silent: set[str] = set()
        self.before_reply: Any = None
        self.closed = False
        self.close_gate: asyncio.Event | None = None
        self._inbox: asyncio.Queue[str | None] = asyncio.Queue()

    async def send(self, text: str) -> None:
        message = json.loads(text)
        self.sent.append(message)
        if self.before_reply is not None:
            self.before_reply(message)
        if message["method"] in self.silent:
            return
        self.reply(message, refused=message["method"] in self.refuse)

    def reply(self, message: dict[str, Any], *, refused: bool = False) -> None:
        """Chrome's reply to *message*: an empty result, or the ``refusal`` error."""
        reply: dict[str, Any] = {"id": message["id"]}
        if refused:
            reply["error"] = {"code": -32000, "message": self.refusal}
        else:
            reply["result"] = {}
        self._inbox.put_nowait(json.dumps(reply))

    def __aiter__(self) -> ChromeEndpoint:
        return self

    async def __anext__(self) -> str:
        item = await self._inbox.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        """Close the connection, from either side. A websockets close waits for the
        closing handshake, so this one suspends too: ``close_gate``, when set, until
        the test sets it, else for a few turns of the loop."""
        if not self.closed:
            self.closed = True
            self._inbox.put_nowait(None)
        if self.close_gate is not None:
            await self.close_gate.wait()
        for _ in range(3):
            await asyncio.sleep(0)

    def _event(self, method: str, params: dict[str, Any], session: str | None = None) -> None:
        message: dict[str, Any] = {"method": method, "params": params}
        if session is not None:
            message["sessionId"] = session
        self._inbox.put_nowait(json.dumps(message))

    def attach(
        self, session: str, target_id: str, kind: str = "page", *, waiting: bool = True
    ) -> None:
        """Chrome attaches the connection to a target it created (held before its
        first script when *waiting*) or one that existed when auto-attach began."""
        self._event(
            "Target.attachedToTarget",
            {
                "sessionId": session,
                "targetInfo": {
                    "targetId": target_id,
                    "type": kind,
                    "title": "",
                    "url": "about:blank",
                    "attached": True,
                    "canAccessOpener": True,
                },
                "waitingForDebugger": waiting,
            },
        )

    def dialog(self, session: str) -> None:
        self._event("Page.javascriptDialogOpening", _DIALOG_PARAMS, session)

    def detach(self, session: str, target_id: str) -> None:
        self._event("Target.detachedFromTarget", {"sessionId": session, "targetId": target_id})

    def commands(self, method: str, session: str | None) -> list[dict[str, Any]]:
        """The *method* commands sent on *session*, in order."""
        return [m for m in self.sent if m["method"] == method and m.get("sessionId") == session]

    def on(self, session: str | None) -> list[str]:
        """The methods sent on *session* (None: on the browser target itself)."""
        return [m["method"] for m in self.sent if m.get("sessionId") == session]

    async def wait_for(self, method: str, session: str | None, count: int = 1) -> None:
        async with asyncio.timeout(2):
            while self.on(session).count(method) < count:
                await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def endpoints(monkeypatch: pytest.MonkeyPatch) -> list[ChromeEndpoint]:
    """Each connection the new-tab answerer opens, in order."""
    opened: list[ChromeEndpoint] = []

    async def open_socket(url: str) -> ChromeEndpoint:
        endpoint = ChromeEndpoint(url)
        opened.append(endpoint)
        return endpoint

    monkeypatch.setattr(dialogs, "_open_socket", open_socket)
    monkeypatch.setattr(dialogs, "_RECONNECT_DELAYS_S", (0.0, 0.0, 0.0))
    return opened


@pytest.fixture()
def dialog_chrome(monkeypatch: pytest.MonkeyPatch) -> FakeChrome:
    nodriver = pytest.importorskip("nodriver")
    chrome = FakeChrome()

    async def start(**kwargs: Any) -> DialogBrowser:
        await asyncio.sleep(0)
        browser = DialogBrowser(chrome.misuse, kwargs, chrome)
        chrome.browsers.append(browser)
        return browser

    monkeypatch.setattr(nodriver, "start", start)
    return chrome


def _answers(tab: DialogTab) -> list[dict[str, Any]]:
    return [params for method, params in tab.sent if method == "Page.handleJavaScriptDialog"]


def _endpoint_answers(endpoint: ChromeEndpoint) -> list[tuple[str | None, dict[str, Any]]]:
    return [
        (m.get("sessionId"), m["params"])
        for m in endpoint.sent
        if m["method"] == "Page.handleJavaScriptDialog"
    ]


class TestTheHeadlessBackendAnswersFromItsStart:
    async def test_a_dialog_the_first_page_opens_while_loading_is_answered(
        self, dialog_chrome: FakeChrome
    ) -> None:
        backend = NoDriverBackend(headless=True)
        await backend._start_async()
        try:
            (browser,) = dialog_chrome.browsers
            tab = browser.tabs[0]
            assert tab.sent[0] == ("Page.enable", {})
            await asyncio.wait_for(browser.get(LOGIN_URL), timeout=2)
        finally:
            await backend.stop_async()

        assert _answers(tab) == [{"accept": True}]

    async def test_a_visible_backend_leaves_dialogs_to_the_user(
        self, dialog_chrome: FakeChrome, endpoints: list[ChromeEndpoint]
    ) -> None:
        backend = NoDriverBackend(headless=False)
        await backend._start_async()
        try:
            (browser,) = dialog_chrome.browsers
            tab = browser.tabs[0]
        finally:
            await backend.stop_async()

        assert JavascriptDialogOpening not in tab.handlers
        assert tab.sent == []
        assert endpoints == []


class TestAProgramCanOptOut:
    async def test_a_headless_backend_built_with_answer_dialogs_false_leaves_them_open(
        self, dialog_chrome: FakeChrome, endpoints: list[ChromeEndpoint]
    ) -> None:
        backend = NoDriverBackend(headless=True, answer_dialogs=False)
        await backend._start_async()
        try:
            (browser,) = dialog_chrome.browsers
            tab = browser.tabs[0]
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(browser.get(LOGIN_URL), timeout=0.2)
        finally:
            await backend.stop_async()

        assert JavascriptDialogOpening not in tab.handlers
        assert tab.sent == []
        # No connection of its own: no new page is held or answered.
        assert endpoints == []
        assert not hasattr(browser, dialogs._BROWSER_ATTR)

    def test_the_session_passes_the_choice_to_the_backend(self) -> None:
        from graftpunk.session import BrowserSession

        for choice in (True, False):
            session = BrowserSession(backend="nodriver", headless=True, answer_dialogs=choice)
            assert session._backend_instance.answers_dialogs is choice

    def test_the_choice_survives_the_backend_state(self) -> None:
        backend = NoDriverBackend(headless=True, answer_dialogs=False)
        restored = NoDriverBackend.from_state(backend.get_state())

        assert restored.answers_dialogs is False
        assert "answer_dialogs" not in restored._options


async def _until(condition: Any) -> None:
    async with asyncio.timeout(2):
        while not condition():
            await asyncio.sleep(0)


async def _new_tab_answerer(
    endpoints: list[ChromeEndpoint], browser: Any | None = None
) -> tuple[dialogs.NewPageAnswerer, ChromeEndpoint]:
    browser = browser or SimpleNamespace(websocket_url=ENDPOINT_URL, targets=[])
    answerer = await dialogs.answer_dialogs_in_new_tabs(browser)
    assert isinstance(answerer, dialogs.NewPageAnswerer)
    return answerer, endpoints[-1]


class TestEveryNewPageIsAnsweredFromItsFirstScript:
    """A tab or popup opened after the start is held before its first script until
    its answerer is in place (#353)."""

    async def test_the_backend_asks_chrome_to_hold_each_new_page(
        self, dialog_chrome: FakeChrome, endpoints: list[ChromeEndpoint]
    ) -> None:
        backend = NoDriverBackend(headless=True)
        await backend._start_async()
        try:
            (endpoint,) = endpoints
            (command,) = endpoint.sent
        finally:
            await backend.stop_async()

        assert endpoint.url == ENDPOINT_URL
        assert command["method"] == "Target.setAutoAttach"
        assert "sessionId" not in command
        assert command["params"] == {
            "autoAttach": True,
            "waitForDebuggerOnStart": True,
            "flatten": True,
            "filter": [{"type": "page"}],
        }

    async def test_a_new_page_gets_the_page_domain_before_it_is_resumed(
        self, endpoints: list[ChromeEndpoint]
    ) -> None:
        answerer, endpoint = await _new_tab_answerer(endpoints)
        endpoint.attach("session-2", "target-2")
        await endpoint.wait_for("Runtime.runIfWaitingForDebugger", "session-2")

        assert endpoint.on("session-2") == ["Page.enable", "Runtime.runIfWaitingForDebugger"]
        assert answerer.pages == {"session-2": "target-2"}

    async def test_a_popup_s_first_script_dialogs_are_each_answered_once_on_its_session(
        self, endpoints: list[ChromeEndpoint]
    ) -> None:
        answerer, endpoint = await _new_tab_answerer(endpoints)
        endpoint.attach("session-2", "target-2")
        await endpoint.wait_for("Runtime.runIfWaitingForDebugger", "session-2")
        with capture_logs() as logs:
            endpoint.dialog("session-2")
            await endpoint.wait_for("Page.handleJavaScriptDialog", "session-2")
            endpoint.dialog("session-2")
            await _until(lambda: answerer.answered == 2)

        assert _endpoint_answers(endpoint) == [("session-2", {"accept": True})] * 2
        assert (answerer.answered, answerer.failed) == (2, 0)
        answered = [e for e in logs if e["event"] == "nodriver_dialog_answered"]
        assert [e["host"] for e in answered] == ["myshop.example"] * 2

    async def test_a_page_whose_install_fails_is_still_resumed(
        self, endpoints: list[ChromeEndpoint]
    ) -> None:
        answerer, endpoint = await _new_tab_answerer(endpoints)
        endpoint.refuse = {"Page.enable"}
        with capture_logs() as logs:
            endpoint.attach("session-2", "target-2")
            await endpoint.wait_for("Runtime.runIfWaitingForDebugger", "session-2")
            await _until(lambda: not answerer._tasks)

        assert endpoint.on("session-2") == ["Page.enable", "Runtime.runIfWaitingForDebugger"]
        assert answerer.pages == {}
        assert [e["event"] for e in logs if e["log_level"] == "warning"] == [
            "nodriver_dialog_answering_unavailable"
        ]

    async def test_a_page_whose_install_gets_no_reply_is_resumed_after_the_bound(
        self, endpoints: list[ChromeEndpoint], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dialogs, "_INSTALL_WAIT_S", 0.01)
        answerer, endpoint = await _new_tab_answerer(endpoints)
        endpoint.silent = {"Page.enable"}
        endpoint.attach("session-2", "target-2")
        await endpoint.wait_for("Runtime.runIfWaitingForDebugger", "session-2")

        assert endpoint.on("session-2") == ["Page.enable", "Runtime.runIfWaitingForDebugger"]

    async def test_a_page_whose_install_cannot_be_sent_is_still_resumed(
        self, endpoints: list[ChromeEndpoint], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answerer, endpoint = await _new_tab_answerer(endpoints)
        real_send = endpoint.send

        async def send(text: str) -> None:
            if json.loads(text)["method"] == "Page.enable":
                raise RuntimeError("socket busy")
            await real_send(text)

        monkeypatch.setattr(endpoint, "send", send)
        endpoint.attach("session-2", "target-2")
        await endpoint.wait_for("Runtime.runIfWaitingForDebugger", "session-2")

        assert answerer.pages == {}

    async def test_a_target_that_is_no_page_is_resumed_and_left_without_an_answerer(
        self, endpoints: list[ChromeEndpoint]
    ) -> None:
        """The auto-attach asks for pages only; a Chrome that attaches another target
        all the same gets it resumed and detached."""
        answerer, endpoint = await _new_tab_answerer(endpoints)
        endpoint.attach("session-w", "worker-1", kind="service_worker")
        await endpoint.wait_for("Target.detachFromTarget", None)

        assert endpoint.on("session-w") == ["Runtime.runIfWaitingForDebugger"]
        (detach,) = [m for m in endpoint.sent if m["method"] == "Target.detachFromTarget"]
        assert detach["params"] == {"sessionId": "session-w"}
        assert answerer.pages == {}

    async def test_a_page_with_an_answerer_of_its_own_is_left_to_it(
        self, endpoints: list[ChromeEndpoint]
    ) -> None:
        """The first tab, which the backend answers on nodriver's Tab, is attached
        too when auto-attach begins: it is not answered twice."""
        first = FakeTab({}, _always)
        first.target = SimpleNamespace(type_="page", target_id="target-1")
        await dialogs.answer_dialogs(first)
        browser = SimpleNamespace(websocket_url=ENDPOINT_URL, targets=[first])
        answerer, endpoint = await _new_tab_answerer(endpoints, browser)
        endpoint.attach("session-1", "target-1", waiting=False)
        await endpoint.wait_for("Target.detachFromTarget", None)

        # Resumed (harmless when not held) and detached, never answered here.
        assert endpoint.on("session-1") == ["Runtime.runIfWaitingForDebugger"]
        assert answerer.pages == {}

    async def test_a_page_that_gains_an_answerer_later_is_answered_by_that_one(
        self, endpoints: list[ChromeEndpoint]
    ) -> None:
        tab = FakeTab({}, _always)
        tab.target = SimpleNamespace(type_="page", target_id="target-2")
        browser = SimpleNamespace(websocket_url=ENDPOINT_URL, targets=[tab])
        answerer, endpoint = await _new_tab_answerer(endpoints, browser)
        endpoint.attach("session-2", "target-2")
        await endpoint.wait_for("Runtime.runIfWaitingForDebugger", "session-2")
        tab_answerer = await dialogs.answer_dialogs(tab)
        assert tab_answerer is not None and tab_answerer.ready
        # Chrome reports the dialog on every session with the Page domain on.
        endpoint.dialog("session-2")
        await tab.dispatch(_alert())
        for _ in range(20):
            await asyncio.sleep(0)

        assert [p for m, p in tab.sent if m == "Page.handleJavaScriptDialog"] == [{"accept": True}]
        assert tab_answerer.answered == 1
        assert _endpoint_answers(endpoint) == []
        assert answerer.answered == 0

    async def test_a_page_whose_own_answerer_is_still_installing_is_answered_here(
        self, endpoints: list[ChromeEndpoint]
    ) -> None:
        """Until the Tab's answerer has enabled the Page domain, Chrome may report the
        dialog to it on no session, so the new-page answerer keeps the page; once it
        has, the page is left to it."""
        gate = asyncio.Event()

        class SlowTab(FakeTab):
            async def send(self, cmd: Any, **kwargs: Any) -> Any:
                if getattr(cmd, "__name__", "") == "enable":
                    await gate.wait()
                return await super().send(cmd, **kwargs)

        tab = SlowTab({}, _always)
        tab.target = SimpleNamespace(type_="page", target_id="target-2")
        browser = SimpleNamespace(websocket_url=ENDPOINT_URL, targets=[tab])
        answerer, endpoint = await _new_tab_answerer(endpoints, browser)
        installing = asyncio.create_task(dialogs.answer_dialogs(tab))
        await _until(lambda: hasattr(tab, dialogs._TAB_ATTR))
        endpoint.attach("session-2", "target-2")
        await endpoint.wait_for("Runtime.runIfWaitingForDebugger", "session-2")
        endpoint.dialog("session-2")
        await _until(lambda: answerer.answered == 1)

        gate.set()
        tab_answerer = await installing
        assert tab_answerer is not None and tab_answerer.ready
        endpoint.dialog("session-2")
        for _ in range(20):
            await asyncio.sleep(0)

        assert answerer.pages == {"session-2": "target-2"}
        assert _endpoint_answers(endpoint) == [("session-2", {"accept": True})]
        assert answerer.answered == 1

    async def test_a_page_whose_own_answerer_failed_is_answered_here(
        self, endpoints: list[ChromeEndpoint]
    ) -> None:
        tab = FakeTab({}, _always)
        tab.refuse = {"Page.enable"}
        tab.target = SimpleNamespace(type_="page", target_id="target-2")
        tab_answerer = await dialogs.answer_dialogs(tab)
        assert tab_answerer is not None and not tab_answerer.ready
        browser = SimpleNamespace(websocket_url=ENDPOINT_URL, targets=[tab])
        answerer, endpoint = await _new_tab_answerer(endpoints, browser)
        endpoint.attach("session-2", "target-2")
        await endpoint.wait_for("Runtime.runIfWaitingForDebugger", "session-2")
        endpoint.dialog("session-2")
        await _until(lambda: answerer.answered == 1)

        assert _endpoint_answers(endpoint) == [("session-2", {"accept": True})]

    async def test_a_dialog_both_answerers_hear_is_answered_once(
        self, endpoints: list[ChromeEndpoint]
    ) -> None:
        """A dialog that opens while the Tab's answerer is still installing is
        answered here; should the Tab's answerer hear it too once installed, Chrome
        refuses that second answer, which is a debug line."""
        gate = asyncio.Event()

        class SlowTab(FakeTab):
            async def send(self, cmd: Any, **kwargs: Any) -> Any:
                if getattr(cmd, "__name__", "") == "enable":
                    await gate.wait()
                return await super().send(cmd, **kwargs)

        tab = SlowTab({}, _always)
        tab.target = SimpleNamespace(type_="page", target_id="target-2")
        browser = SimpleNamespace(websocket_url=ENDPOINT_URL, targets=[tab])
        answerer, endpoint = await _new_tab_answerer(endpoints, browser)
        installing = asyncio.create_task(dialogs.answer_dialogs(tab))
        await _until(lambda: hasattr(tab, dialogs._TAB_ATTR))
        endpoint.attach("session-2", "target-2")
        await endpoint.wait_for("Runtime.runIfWaitingForDebugger", "session-2")
        endpoint.dialog("session-2")
        await _until(lambda: answerer.answered == 1)
        gate.set()
        tab_answerer = await installing
        assert tab_answerer is not None
        tab.refuse = {"Page.handleJavaScriptDialog"}  # the dialog is closed by now
        with capture_logs() as logs:
            await tab.dispatch(_alert())
            await _until(lambda: tab_answerer.failed == 1)

        assert _endpoint_answers(endpoint) == [("session-2", {"accept": True})]
        assert tab_answerer.answered == 0
        failed = [e for e in logs if e["event"] == "nodriver_dialog_answer_failed"]
        assert [e["log_level"] for e in failed] == ["debug"]
        assert [e for e in logs if e["log_level"] == "warning"] == []

    async def test_a_refused_answer_is_counted_failed(
        self, endpoints: list[ChromeEndpoint]
    ) -> None:
        answerer, endpoint = await _new_tab_answerer(endpoints)
        endpoint.refuse = {"Page.handleJavaScriptDialog"}
        endpoint.attach("session-2", "target-2")
        await endpoint.wait_for("Runtime.runIfWaitingForDebugger", "session-2")
        endpoint.dialog("session-2")
        await _until(lambda: answerer.failed == 1)

        assert (answerer.answered, answerer.failed) == (0, 1)


class TestANewPageAnswererLetsGo:
    async def test_a_closed_target_s_session_is_dropped(
        self, endpoints: list[ChromeEndpoint]
    ) -> None:
        """Chrome detaches the session when the tab closes or its target is
        destroyed: the answerer keeps nothing for it, and answers nothing there."""
        answerer, endpoint = await _new_tab_answerer(endpoints)
        endpoint.attach("session-2", "target-2")
        endpoint.attach("session-3", "target-3")
        await endpoint.wait_for("Runtime.runIfWaitingForDebugger", "session-3")
        await endpoint.wait_for("Runtime.runIfWaitingForDebugger", "session-2")
        endpoint.detach("session-2", "target-2")
        endpoint.dialog("session-2")
        endpoint.dialog("session-3")
        await endpoint.wait_for("Page.handleJavaScriptDialog", "session-3")

        assert answerer.pages == {"session-3": "target-3"}
        assert _endpoint_answers(endpoint) == [("session-3", {"accept": True})]

    async def test_the_connection_ends_with_the_browser(
        self, endpoints: list[ChromeEndpoint]
    ) -> None:
        """A stop that ends Chrome without the browser's loop marks the end expected
        first: the connection Chrome then closes is neither reconnected nor lost."""
        answerer, endpoint = await _new_tab_answerer(endpoints)
        endpoint.attach("session-2", "target-2")
        await endpoint.wait_for("Runtime.runIfWaitingForDebugger", "session-2")
        conn = answerer._conn
        assert conn is not None
        with capture_logs() as logs:
            answerer.expect_end()
            await endpoint.close()  # what a stopped Chrome does to the websocket
            await _until(lambda: answerer.closed)
            for _ in range(20):
                await asyncio.sleep(0)

        assert answerer.pages == {}
        assert len(endpoints) == 1
        assert [e["event"] for e in logs] == []
        with pytest.raises(ConnectionError):
            await answerer._call(conn, "Page.enable", {}, "session-2")

    async def test_aclose_closes_the_connection(self, endpoints: list[ChromeEndpoint]) -> None:
        answerer, endpoint = await _new_tab_answerer(endpoints)
        await answerer.aclose()

        assert endpoint.closed
        assert answerer.closed


def _warnings(logs: list[dict[str, Any]]) -> list[str]:
    return [e["event"] for e in logs if e["log_level"] == "warning"]


def _reconnected(answerer: dialogs.NewPageAnswerer) -> Any:
    """Whether *answerer*'s reconnect has finished, with auto-attach on again."""
    return lambda: (
        answerer._reconnector is not None
        and answerer._reconnector.done()
        and answerer._conn is not None
        and answerer._conn.up
    )


class TestTheConnectionLastsAsLongAsTheBrowser:
    """The answerer's connection ends only when asked to or with the browser; one
    Chrome closes otherwise is reconnected, and a loss it cannot repair is reported."""

    async def test_the_connection_sends_no_keepalive_pings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import websockets

        connect = AsyncMock(return_value="socket")
        monkeypatch.setattr(websockets, "connect", connect)

        assert await _real_open_socket(ENDPOINT_URL) == "socket"
        connect.assert_awaited_once_with(
            ENDPOINT_URL,
            max_size=dialogs._MAX_MESSAGE_BYTES,
            ping_interval=None,
            ping_timeout=None,
        )

    async def test_aclose_is_no_loss(self, endpoints: list[ChromeEndpoint]) -> None:
        answerer, endpoint = await _new_tab_answerer(endpoints)
        with capture_logs() as logs:
            await answerer.aclose()

        assert endpoint.closed
        assert answerer.ended
        assert _warnings(logs) == []

    async def test_a_listener_cancelled_from_outside_closes_the_socket_and_is_replaced(
        self, endpoints: list[ChromeEndpoint]
    ) -> None:
        """Cancelled from outside (a loop shutting down): the socket must close, or
        Chrome keeps holding new pages for a connection nobody reads, and no
        reconnect is started on a loop that is going away."""
        browser = SimpleNamespace(websocket_url=ENDPOINT_URL, targets=[])
        answerer, endpoint = await _new_tab_answerer(endpoints, browser)
        answerer.answered, answerer.failed = 2, 1
        assert answerer._conn is not None and answerer._conn.listener is not None
        with capture_logs() as logs:
            answerer._conn.listener.cancel()
            await _until(lambda: endpoint.closed and answerer.ended)
            for _ in range(20):
                await asyncio.sleep(0)

        assert len(endpoints) == 1
        assert answerer._reconnector is None
        (lost,) = [e for e in logs if e["event"] == "nodriver_dialog_answerer_lost"]
        assert (lost["log_level"], lost["reason"], lost["attempts"]) == ("warning", "cancelled", 0)
        assert ENDPOINT_URL not in json.dumps(logs, default=str)

        again, second = await _new_tab_answerer(endpoints, browser)
        assert again is not answerer
        assert second is not endpoint and not second.closed
        assert second.on(None) == ["Target.setAutoAttach"]
        assert dialogs.new_tab_dialog_counts(browser) == (2, 1)

    async def test_a_connection_chrome_ends_is_reconnected_with_no_warning(
        self, endpoints: list[ChromeEndpoint]
    ) -> None:
        browser = SimpleNamespace(websocket_url=ENDPOINT_URL, targets=[])
        answerer, endpoint = await _new_tab_answerer(endpoints, browser)
        answerer.answered = 2
        with capture_logs() as logs:
            await endpoint.close()
            await _until(_reconnected(answerer))

        assert len(endpoints) == 2
        second = endpoints[1]
        assert second.on(None) == ["Target.setAutoAttach"]
        assert not answerer.ended
        assert _warnings(logs) == []
        assert [(e["event"], e["attempt"]) for e in logs] == [
            ("nodriver_dialog_answerer_reconnecting", 1),
            ("nodriver_dialog_answerer_reconnected", 1),
        ]
        assert logs[0]["reason"] == "closed_by_chrome"
        # The same answerer, its counts and its rules, on the new connection.
        assert await dialogs.answer_dialogs_in_new_tabs(browser) is answerer
        second.attach("session-5", "target-5")
        await second.wait_for("Runtime.runIfWaitingForDebugger", "session-5")
        second.dialog("session-5")
        await _until(lambda: answerer.answered == 3)
        assert _endpoint_answers(second) == [("session-5", {"accept": True})]

    async def test_three_failed_reconnects_give_up_with_one_warning(
        self, endpoints: list[ChromeEndpoint], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        browser = SimpleNamespace(websocket_url=ENDPOINT_URL, targets=[])
        answerer, endpoint = await _new_tab_answerer(endpoints, browser)
        real_open = dialogs._open_socket
        monkeypatch.setattr(dialogs, "_open_socket", AsyncMock(side_effect=OSError("refused")))
        with capture_logs() as logs:
            await endpoint.close()
            await _until(lambda: answerer.ended)

        assert dialogs._open_socket.await_count == 3  # type: ignore[attr-defined]
        attempts = [e for e in logs if e["event"] == "nodriver_dialog_answerer_reconnecting"]
        assert [(e["attempt"], e["log_level"]) for e in attempts] == [
            (1, "info"),
            (2, "info"),
            (3, "info"),
        ]
        assert [e["reason"] for e in attempts] == [
            "closed_by_chrome",
            "reconnect_failed",
            "reconnect_failed",
        ]
        (lost,) = [e for e in logs if e["log_level"] == "warning"]
        assert lost["event"] == "nodriver_dialog_answerer_lost"
        assert (lost["attempts"], lost["reason"], lost["exc_type"]) == (
            3,
            "reconnect_failed",
            "OSError",
        )
        assert "until the browser stops" in lost["hint"]
        assert ENDPOINT_URL not in json.dumps(logs, default=str)
        # A program can still start a new one.
        monkeypatch.setattr(dialogs, "_open_socket", real_open)
        again, _second = await _new_tab_answerer(endpoints, browser)
        assert again is not answerer

    async def test_a_reconnect_that_chrome_refuses_auto_attach_on_is_retried(
        self, endpoints: list[ChromeEndpoint], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answerer, endpoint = await _new_tab_answerer(endpoints)
        real_open = dialogs._open_socket
        opened = 0

        async def refusing_once(url: str) -> Any:
            nonlocal opened
            opened += 1
            replacement = await real_open(url)
            if opened == 1:
                replacement.refuse = {"Target.setAutoAttach"}
            return replacement

        monkeypatch.setattr(dialogs, "_open_socket", refusing_once)
        with capture_logs() as logs:
            await endpoint.close()
            await _until(_reconnected(answerer))
            await _until(lambda: endpoints[1].closed)

        assert _warnings(logs) == []
        reconnected = [e for e in logs if e["event"] == "nodriver_dialog_answerer_reconnected"]
        assert [e["attempt"] for e in reconnected] == [2]
        assert endpoints[2].on(None) == ["Target.setAutoAttach"]

    async def test_no_reconnect_after_aclose(
        self, endpoints: list[ChromeEndpoint], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answerer, endpoint = await _new_tab_answerer(endpoints)
        with capture_logs() as logs:
            await answerer.aclose()
            await endpoint.close()  # Chrome's side of the close, too
            for _ in range(20):
                await asyncio.sleep(0)

        assert len(endpoints) == 1
        assert answerer.ended and answerer.closed
        assert [e["event"] for e in logs] == []

    async def test_aclose_during_a_reconnect_s_wait_ends_it(
        self, endpoints: list[ChromeEndpoint], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dialogs, "_RECONNECT_DELAYS_S", (30.0,))
        answerer, endpoint = await _new_tab_answerer(endpoints)
        with capture_logs() as logs:
            await endpoint.close()
            await _until(lambda: answerer._reconnector is not None)
            async with asyncio.timeout(1):
                await answerer.aclose()

        assert answerer._reconnector is not None and answerer._reconnector.done()
        assert len(endpoints) == 1
        assert answerer.ended
        assert _warnings(logs) == []

    async def test_aclose_re_raises_its_caller_s_cancellation(
        self, endpoints: list[ChromeEndpoint]
    ) -> None:
        """A stop inside ``asyncio.timeout`` or a TaskGroup that is cancelled while
        the socket closes must end with the cancellation, not carry on."""
        answerer, endpoint = await _new_tab_answerer(endpoints)
        endpoint.close_gate = asyncio.Event()
        after: list[str] = []

        async def stopping() -> None:
            await answerer.aclose()
            after.append("went on")

        task = asyncio.create_task(stopping())
        await _until(lambda: endpoint.closed)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert after == []
        assert endpoint.closed
        assert answerer.ended and answerer.closed
        endpoint.close_gate.set()

    async def test_aclose_waits_at_most_one_close_bound_for_a_close_chrome_never_answers(
        self, endpoints: list[ChromeEndpoint], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dialogs, "_INSTALL_WAIT_S", 0.2)
        answerer, endpoint = await _new_tab_answerer(endpoints)
        endpoint.close_gate = asyncio.Event()  # never set: the close never finishes
        loop = asyncio.get_running_loop()
        began = loop.time()
        await answerer.aclose()
        took = loop.time() - began

        assert 0.15 <= took < 0.3
        assert endpoint.closed and answerer.ended
        endpoint.close_gate.set()

    async def test_a_stop_while_an_install_is_in_flight_logs_no_warning(
        self, endpoints: list[ChromeEndpoint]
    ) -> None:
        answerer, endpoint = await _new_tab_answerer(endpoints)
        endpoint.silent = {"Page.enable"}
        with capture_logs() as logs:
            endpoint.attach("session-2", "target-2")
            await endpoint.wait_for("Page.enable", "session-2")
            await answerer.aclose()
            await _until(lambda: not answerer._tasks)

        assert _warnings(logs) == []
        assert [(e["event"], e["log_level"]) for e in logs] == [
            ("nodriver_dialog_new_target_install_cut_by_close", "debug")
        ]

    async def test_a_cancelled_start_leaves_nothing_running(
        self, endpoints: list[ChromeEndpoint], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_open = dialogs._open_socket

        async def unanswered(url: str) -> Any:
            endpoint = await real_open(url)
            endpoint.silent = {"Target.setAutoAttach"}
            return endpoint

        monkeypatch.setattr(dialogs, "_open_socket", unanswered)
        browser = SimpleNamespace(websocket_url=ENDPOINT_URL, targets=[])
        starting = asyncio.create_task(dialogs.answer_dialogs_in_new_tabs(browser))
        await _until(lambda: bool(endpoints) and bool(endpoints[0].sent))
        half_started = getattr(browser, dialogs._BROWSER_ATTR)
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting

        assert endpoints[0].closed
        assert half_started.ended and half_started.closed
        monkeypatch.setattr(dialogs, "_open_socket", real_open)
        again = await dialogs.answer_dialogs_in_new_tabs(browser)
        assert again is not half_started
        assert len(endpoints) == 2

    async def test_a_timed_out_command_leaves_no_reply_slot(
        self, endpoints: list[ChromeEndpoint], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dialogs, "_INSTALL_WAIT_S", 0.01)
        answerer, endpoint = await _new_tab_answerer(endpoints)
        endpoint.silent = {"Page.enable"}
        for n in range(2, 5):
            endpoint.attach(f"session-{n}", f"target-{n}")
        for n in range(2, 5):
            await endpoint.wait_for("Runtime.runIfWaitingForDebugger", f"session-{n}")
        await _until(lambda: not answerer._tasks)

        assert answerer._conn is not None and answerer._conn.replies == {}

    async def test_a_timed_out_answer_leaves_no_reply_slot(
        self, endpoints: list[ChromeEndpoint], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dialogs, "_DIALOG_ANSWER_WAIT_S", 0.01)
        answerer, endpoint = await _new_tab_answerer(endpoints)
        endpoint.attach("session-2", "target-2")
        await endpoint.wait_for("Runtime.runIfWaitingForDebugger", "session-2")
        endpoint.silent = {"Page.handleJavaScriptDialog"}
        endpoint.dialog("session-2")
        await _until(lambda: answerer.failed == 1)

        assert answerer._conn is not None and answerer._conn.replies == {}

    async def test_the_backend_s_stop_closes_the_connection(
        self, dialog_chrome: FakeChrome, endpoints: list[ChromeEndpoint]
    ) -> None:
        backend = NoDriverBackend(headless=True)
        await backend.start_async()
        (browser,) = dialog_chrome.browsers
        answerer = getattr(browser, dialogs._BROWSER_ATTR)
        (endpoint,) = endpoints
        assert not endpoint.closed
        with capture_logs() as logs:
            await backend.stop_async()

        assert endpoint.closed
        assert answerer.ended
        assert "nodriver_dialog_answerer_lost" not in [e["event"] for e in logs]

    async def test_a_stop_without_the_browser_s_loop_logs_no_loss(
        self, dialog_chrome: FakeChrome, endpoints: list[ChromeEndpoint]
    ) -> None:
        """``stop()`` from inside the browser's own loop ends Chrome without that
        loop: the end is marked expected first, so the connection Chrome's exit
        closes is neither reconnected nor reported lost."""
        backend = NoDriverBackend(headless=True)
        await backend.start_async()
        (endpoint,) = endpoints
        with capture_logs() as logs:
            backend.stop()
            await endpoint.close()  # what Chrome's exit does to the websocket
            for _ in range(20):
                await asyncio.sleep(0)

        events = [e["event"] for e in logs]
        assert "nodriver_backend_stop_in_own_loop" in events
        assert "nodriver_dialog_answerer_lost" not in events
        assert "nodriver_dialog_answerer_reconnecting" not in events
        assert len(endpoints) == 1


class TestPagesArriveAsChromeSendsThem:
    async def test_a_burst_of_attaches_with_replies_out_of_order(
        self, endpoints: list[ChromeEndpoint]
    ) -> None:
        answerer, endpoint = await _new_tab_answerer(endpoints)
        endpoint.silent = {"Page.enable"}
        sessions = [f"session-{n}" for n in range(2, 8)]
        for session in sessions:
            endpoint.attach(session, session.replace("session", "target"))
        for session in sessions:
            await endpoint.wait_for("Page.enable", session)
        enables = [m for m in endpoint.sent if m["method"] == "Page.enable"]
        for message in reversed(enables):
            endpoint.reply(message)
        for session in sessions:
            await endpoint.wait_for("Runtime.runIfWaitingForDebugger", session)

        for session in sessions:
            assert endpoint.on(session) == ["Page.enable", "Runtime.runIfWaitingForDebugger"]
        assert answerer.pages == {s: s.replace("session", "target") for s in sessions}
        for session in sessions:
            endpoint.dialog(session)
        await _until(lambda: answerer.answered == len(sessions))
        assert sorted(s for s, _p in _endpoint_answers(endpoint)) == sorted(sessions)

    async def test_attaches_before_the_auto_attach_reply_are_handled(
        self, endpoints: list[ChromeEndpoint], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Chrome attaches the targets that already exist as it turns auto-attach on,
        before its reply: the first tab (left to its own answerer) and another page."""
        first = FakeTab({}, _always)
        first.target = SimpleNamespace(type_="page", target_id="target-1")
        await dialogs.answer_dialogs(first)
        real_open = dialogs._open_socket

        async def attaching_first(url: str) -> Any:
            endpoint = await real_open(url)

            def before_reply(message: dict[str, Any]) -> None:
                if message["method"] == "Target.setAutoAttach":
                    endpoint.attach("session-1", "target-1", waiting=False)
                    endpoint.attach("session-9", "target-9", waiting=False)

            endpoint.before_reply = before_reply
            return endpoint

        monkeypatch.setattr(dialogs, "_open_socket", attaching_first)
        browser = SimpleNamespace(websocket_url=ENDPOINT_URL, targets=[first])
        answerer, endpoint = await _new_tab_answerer(endpoints, browser)
        await endpoint.wait_for("Target.detachFromTarget", None)
        await endpoint.wait_for("Page.enable", "session-9")
        await _until(lambda: not answerer._tasks)

        # Resumed (harmless when not held) and detached, never answered here.
        assert endpoint.on("session-1") == ["Runtime.runIfWaitingForDebugger"]
        assert endpoint.on("session-9") == ["Page.enable", "Runtime.runIfWaitingForDebugger"]
        assert answerer.pages == {"session-9": "target-9"}

    async def test_a_popup_closed_during_its_install_is_one_debug_line(
        self, endpoints: list[ChromeEndpoint]
    ) -> None:
        answerer, endpoint = await _new_tab_answerer(endpoints)
        endpoint.silent = {"Page.enable"}
        endpoint.refusal = "Session with given id not found."
        endpoint.refuse = {"Runtime.runIfWaitingForDebugger"}
        with capture_logs() as logs:
            endpoint.attach("session-2", "target-2")
            await endpoint.wait_for("Page.enable", "session-2")
            endpoint.detach("session-2", "target-2")
            await _until(
                lambda: answerer._conn is not None and "session-2" not in answerer._conn.attached
            )
            (enable,) = endpoint.commands("Page.enable", "session-2")
            endpoint.reply(enable, refused=True)
            await endpoint.wait_for("Runtime.runIfWaitingForDebugger", "session-2")
            await _until(lambda: not answerer._tasks)

        assert answerer.pages == {}
        assert [(e["event"], e["log_level"]) for e in logs] == [
            ("nodriver_dialog_new_target_gone", "debug")
        ]

    async def test_a_failed_detach_has_its_own_warning(
        self, endpoints: list[ChromeEndpoint]
    ) -> None:
        answerer, endpoint = await _new_tab_answerer(endpoints)
        endpoint.refuse = {"Target.detachFromTarget"}
        with capture_logs() as logs:
            endpoint.attach("session-w", "worker-1", kind="service_worker")
            await endpoint.wait_for("Target.detachFromTarget", None)
            await _until(lambda: not answerer._tasks)

        assert _warnings(logs) == ["nodriver_dialog_new_target_detach_failed"]

    async def test_a_connection_lost_between_attach_and_resume_is_reported(
        self, endpoints: list[ChromeEndpoint], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The resume cannot be sent on a connection that is gone: the page is not
        taken for one that closed, so the code says so at warning level (the
        reconnect below resumes it). The fake's close suspends, as a websockets
        close does, so the page's task runs while the listener is still closing."""
        answerer, endpoint = await _new_tab_answerer(endpoints)
        monkeypatch.setattr(dialogs, "_open_socket", AsyncMock(side_effect=OSError("refused")))
        monkeypatch.setattr(dialogs, "_RECONNECT_DELAYS_S", (0.0,))
        endpoint.silent = {"Page.enable"}
        with capture_logs() as logs:
            endpoint.attach("session-2", "target-2")
            await endpoint.wait_for("Page.enable", "session-2")
            await endpoint.close()
            await _until(lambda: answerer.ended and not answerer._tasks)

        assert endpoint.on("session-2") == ["Page.enable"]
        assert answerer.pages == {}
        assert sorted(_warnings(logs)) == [
            "nodriver_dialog_answerer_lost",
            "nodriver_dialog_answering_unavailable",
            "nodriver_dialog_new_target_resume_failed",
        ]
        assert "nodriver_dialog_new_target_gone" not in [e["event"] for e in logs]

    async def test_a_drop_and_a_reconnect_release_a_page_the_drop_left_held(
        self, endpoints: list[ChromeEndpoint], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Chrome reports such a page not waiting when the new connection's
        auto-attach attaches it (measured live on 2026-11-03), and only a resume
        releases it: every page attached gets one, after its install."""
        browser = SimpleNamespace(websocket_url=ENDPOINT_URL, targets=[])
        answerer, endpoint = await _new_tab_answerer(endpoints, browser)
        real_open = dialogs._open_socket

        async def attaching_existing(url: str) -> Any:
            replacement = await real_open(url)

            def before_reply(message: dict[str, Any]) -> None:
                if message["method"] == "Target.setAutoAttach":
                    replacement.attach("session-3", "target-2", waiting=False)

            replacement.before_reply = before_reply
            return replacement

        monkeypatch.setattr(dialogs, "_open_socket", attaching_existing)
        endpoint.silent = {"Page.enable"}
        endpoint.attach("session-2", "target-2")
        await endpoint.wait_for("Page.enable", "session-2")
        await endpoint.close()
        await _until(lambda: len(endpoints) == 2)
        second = endpoints[1]
        await second.wait_for("Runtime.runIfWaitingForDebugger", "session-3")
        await _until(lambda: not answerer._tasks)

        assert endpoint.on("session-2") == ["Page.enable"]
        assert second.on("session-3") == ["Page.enable", "Runtime.runIfWaitingForDebugger"]
        assert not answerer.ended
        assert answerer.pages == {"session-3": "target-2"}
        second.dialog("session-3")
        await _until(lambda: answerer.answered == 1)

    async def test_a_resume_refused_on_a_page_that_was_not_held_is_a_debug_line(
        self, endpoints: list[ChromeEndpoint]
    ) -> None:
        """A page that was never held goes on as it was: it is answered, and a
        refused resume there warns nobody."""
        answerer, endpoint = await _new_tab_answerer(endpoints)
        endpoint.refuse = {"Runtime.runIfWaitingForDebugger"}
        with capture_logs() as logs:
            endpoint.attach("session-2", "target-2", waiting=False)
            await endpoint.wait_for("Runtime.runIfWaitingForDebugger", "session-2")
            await _until(lambda: not answerer._tasks)

        assert _warnings(logs) == []
        assert [e["event"] for e in logs] == ["nodriver_dialog_page_resume_failed"]
        assert answerer.pages == {"session-2": "target-2"}
        endpoint.dialog("session-2")
        await _until(lambda: answerer.answered == 1)

    async def test_a_reconnect_leaves_the_first_tab_to_its_own_answerer(
        self, endpoints: list[ChromeEndpoint]
    ) -> None:
        first = FakeTab({}, _always)
        first.target = SimpleNamespace(type_="page", target_id="target-1")
        await dialogs.answer_dialogs(first)
        browser = SimpleNamespace(websocket_url=ENDPOINT_URL, targets=[first])
        answerer, endpoint = await _new_tab_answerer(endpoints, browser)
        await endpoint.close()
        await _until(_reconnected(answerer))
        second = endpoints[1]
        second.attach("session-1", "target-1", waiting=False)
        await second.wait_for("Target.detachFromTarget", None)
        await _until(lambda: not answerer._tasks)
        second.dialog("session-1")
        await first.dispatch(_alert())
        for _ in range(20):
            await asyncio.sleep(0)

        assert second.on("session-1") == ["Runtime.runIfWaitingForDebugger"]
        assert answerer.pages == {}
        assert _endpoint_answers(second) == []
        assert [p for m, p in first.sent if m == "Page.handleJavaScriptDialog"] == [
            {"accept": True}
        ]


class TestOneNewPageAnswererPerBrowser:
    async def test_a_second_request_shares_the_first(self, endpoints: list[ChromeEndpoint]) -> None:
        browser = SimpleNamespace(websocket_url=ENDPOINT_URL, targets=[])
        first = await dialogs.answer_dialogs_in_new_tabs(browser)
        second = await dialogs.answer_dialogs_in_new_tabs(browser)

        assert first is second
        assert len(endpoints) == 1

    async def test_an_endpoint_that_refuses_the_connection_is_one_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dialogs, "_open_socket", AsyncMock(side_effect=OSError("refused")))
        browser = SimpleNamespace(websocket_url=ENDPOINT_URL, targets=[])
        with capture_logs() as logs:
            answerer = await dialogs.answer_dialogs_in_new_tabs(browser)

        assert answerer is not None
        assert answerer.closed
        assert [e["event"] for e in logs] == ["nodriver_dialog_answering_new_tabs_unavailable"]
        assert dialogs.new_tab_dialog_counts(browser) == (0, 0)

    async def test_an_endpoint_that_refuses_auto_attach_is_one_warning(
        self, endpoints: list[ChromeEndpoint], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_open = dialogs._open_socket

        async def refusing(url: str) -> Any:
            endpoint = await real_open(url)
            endpoint.refuse = {"Target.setAutoAttach"}
            return endpoint

        monkeypatch.setattr(dialogs, "_open_socket", refusing)
        with capture_logs() as logs:
            answerer = await dialogs.answer_dialogs_in_new_tabs(
                SimpleNamespace(websocket_url=ENDPOINT_URL, targets=[])
            )

        assert answerer is not None and answerer.closed
        assert endpoints[0].closed
        assert [e["event"] for e in logs] == ["nodriver_dialog_answering_new_tabs_unavailable"]

    async def test_a_browser_without_an_endpoint_is_one_warning(
        self, endpoints: list[ChromeEndpoint]
    ) -> None:
        with capture_logs() as logs:
            answerer = await dialogs.answer_dialogs_in_new_tabs(SimpleNamespace())

        assert answerer is None
        assert endpoints == []
        assert [e["reason"] for e in logs] == ["no_websocket_url"]


def _plugin(*, headless: bool) -> SitePlugin:
    class P(SitePlugin):
        site_name = "dlg"
        session_name = "dlg-session"
        help_text = "t"
        base_url = "https://myshop.example"

    P.backend = "nodriver"  # type: ignore[assignment]
    P.login_config = LoginConfig(
        steps=[LoginStep(fields={"username": "#u", "password": "#p"}, submit="#s")],
        url="/login",
        failure="Bad login.",
        headless=headless,
    )
    return P()


class TestALoginPageThatAlertsWhileLoading:
    """The dialog opens during the login's first navigation, before any capture starts."""

    async def test_a_headless_login_answers_it_and_goes_on(self, dialog_chrome: FakeChrome) -> None:
        from graftpunk.plugins import login_engine

        steps = AsyncMock(return_value=True)
        with (
            patch.object(login_engine, "_run_nodriver_steps", steps),
            patch.object(login_engine, "_LOGIN_NAV_TIMEOUT", 2),
        ):
            login = login_engine._generate_nodriver_login(_plugin(headless=True))
            assert await login({"username": "u", "password": "p"}) is True

        (browser,) = dialog_chrome.browsers
        assert _answers(browser.tabs[0]) == [{"accept": True}]
        steps.assert_awaited_once()

    async def test_the_login_passes_its_headless_override_to_the_browser(
        self, dialog_chrome: FakeChrome
    ) -> None:
        """A config that says visible, overridden to headless for this call: the
        dialog is still answered."""
        from graftpunk.plugins import login_engine

        with (
            patch.object(login_engine, "_run_nodriver_steps", AsyncMock(return_value=True)),
            patch.object(login_engine, "_LOGIN_NAV_TIMEOUT", 2),
        ):
            login = login_engine._generate_nodriver_login(_plugin(headless=False))
            assert await login({"username": "u", "password": "p"}, headless=True) is True

        (browser,) = dialog_chrome.browsers
        assert _answers(browser.tabs[0]) == [{"accept": True}]


class TestTheLoginCaptureAnswersWhenHeadless:
    """Both login captures, recorded or not, share the tab's answerer when headless."""

    @pytest.mark.parametrize("observe_mode", ["off", "full"])
    @pytest.mark.parametrize("headless", [True, False])
    async def test_the_capture_is_asked_to_answer_dialogs_exactly_when_headless(
        self, tmp_path: Any, observe_mode: str, headless: bool
    ) -> None:
        from graftpunk.plugins import login_engine

        tab = MagicMock()
        instance = MagicMock()
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        instance.driver.get = AsyncMock(return_value=tab)
        capture = MagicMock()
        capture.start_capture_async = AsyncMock()
        made = MagicMock(return_value=capture)
        with (
            patch("graftpunk.BrowserSession", MagicMock(return_value=instance)),
            patch("graftpunk.observe.capture.create_capture_backend", made),
            patch("graftpunk.observe.OBSERVE_BASE_DIR", tmp_path),
            patch.object(login_engine, "_run_nodriver_steps", AsyncMock(return_value=True)),
            patch("graftpunk.observe.run.save_observe_run", AsyncMock()),
        ):
            login = login_engine._generate_nodriver_login(_plugin(headless=not headless))
            await login(
                {"username": "u", "password": "p"},
                headless=headless,
                observe_mode=observe_mode,
            )

        assert made.call_args.kwargs["answer_dialogs"] is headless


class TestOneAnswererPerTab:
    @pytest.mark.asyncio
    async def test_a_second_request_shares_the_first_answerer(self) -> None:
        """The backend answers from its start, and a recording asks again: one
        handler, one Page.enable, one answer per dialog."""
        tab = FakeTab({}, _always)
        first = await dialogs.answer_dialogs(tab)
        second = await dialogs.answer_dialogs(tab)

        assert first is second
        assert len(tab.handlers[JavascriptDialogOpening]) == 1
        assert [m for m, _p in tab.sent if m == "Page.enable"] == ["Page.enable"]

    async def test_an_unattached_tab_is_attached_before_the_page_domain_is_enabled(
        self,
    ) -> None:
        """A popup's Tab on nodriver 0.50.6 has no session yet: the answerer fixes
        the one Page is enabled on, and answers there."""

        class UnattachedTab(DialogTab):
            session_id: str | None = None

            async def attach(self) -> None:
                self.sent.append(("attach", {}))
                self.session_id = "session-1"

        tab = UnattachedTab("about:blank", [])
        answerer = await dialogs.answer_dialogs(tab)

        assert tab.sent == [("attach", {}), ("Page.enable", {})]
        assert answerer is not None
        assert answerer._session_id == "session-1"

    @pytest.mark.asyncio
    async def test_a_tab_that_cannot_take_a_handler_is_one_warning(self) -> None:
        with capture_logs() as logs:
            answerer = await dialogs.answer_dialogs(MagicMock())

        assert isinstance(answerer, dialogs.DialogAnswerer)
        assert [e["event"] for e in logs] == ["nodriver_dialog_answering_unavailable"]
