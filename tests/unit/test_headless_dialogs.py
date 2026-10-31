"""Every headless nodriver browser graftpunk drives answers JavaScript dialogs (#320).

A page's ``alert()`` stops its script, and its load, until someone answers it, and
nobody can in a headless browser. The fake browser here behaves that way: its
``Browser.get`` of a page that opens a dialog returns only once the dialog has been
answered through CDP, so a dialog nobody answers times the navigation out.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nodriver.cdp.page import JavascriptDialogOpening
from nodriver.cdp.target import TargetCreated
from nodriver.cdp.util import parse_json_event
from structlog.testing import capture_logs

from graftpunk import dialogs
from graftpunk.backends.nodriver import NoDriverBackend
from graftpunk.plugins import LoginConfig, LoginStep, SitePlugin
from tests.unit.conftest import FakeChrome, LoopBoundBrowser, LoopBoundTab
from tests.unit.test_observe_post_data import FakeTab, _always

pytestmark = pytest.mark.usefixtures("release_nodriver_loops")

LOGIN_URL = "https://myshop.example/login"
_REAL_OWN_TAB = dialogs._own_tab


def _alert() -> Any:
    return parse_json_event(
        {
            "method": "Page.javascriptDialogOpening",
            "params": {
                "url": LOGIN_URL,
                "frameId": "frame-1",
                "message": "Welcome back",
                "type": "alert",
                "hasBrowserHandler": False,
            },
        }
    )


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


def _target_created(target_id: str, kind: str = "page") -> Any:
    return parse_json_event(
        {
            "method": "Target.targetCreated",
            "params": {
                "targetInfo": {
                    "targetId": target_id,
                    "type": kind,
                    "title": "",
                    "url": "about:blank",
                    "attached": False,
                    "canAccessOpener": True,
                }
            },
        }
    )


async def _open_dialog(tab: DialogTab) -> None:
    """The tab's page opens an alert; returns once it is answered, as the page's
    script does."""
    tab.closed = asyncio.Event()
    for callback in tab.handlers.get(JavascriptDialogOpening, []):
        asyncio.create_task(callback(_alert()))  # noqa: RUF006 - nodriver does the same
    await tab.closed.wait()


class DialogBrowser(LoopBoundBrowser):
    """``Browser.get(LOGIN_URL)`` opens an alert and returns once it is answered.

    Like nodriver 0.50.6's Browser, it is its own connection to Chrome
    (``add_handler``, ``send``), and a target Chrome reports gets a Tab object in
    ``targets`` only in ``update_targets``. ``own_tabs`` are the Tab objects the
    answerer made for itself (``graftpunk.dialogs._own_tab``), by target id."""

    def __init__(self, misuse: list[str], start_kwargs: dict[str, Any], chrome: Any) -> None:
        super().__init__(misuse, start_kwargs, chrome)
        self.tabs = [DialogTab("about:blank", misuse)]
        self.tabs[0].target = SimpleNamespace(type_="page", target_id="target-1")
        self.connection = None
        self.handlers: dict[type, list[Any]] = {}
        self.sent: list[str] = []
        self._unlisted: list[DialogTab] = []
        self.own_tabs: dict[str, DialogTab] = {}

    @property
    def targets(self) -> list[Any]:
        return self.tabs

    def add_handler(self, event_type: type, callback: Any) -> None:
        self.handlers.setdefault(event_type, []).append(callback)

    async def send(self, cmd: Any) -> Any:
        await self._on_own_loop("Browser.send")
        self.sent.append(next(cmd)["method"])
        try:
            cmd.send({})
        except StopIteration as stop:
            return stop.value
        raise AssertionError("a browser command did not finish after one response")

    async def update_targets(self) -> None:
        self.tabs.extend(self._unlisted)
        self._unlisted.clear()

    def own_tab(self, info: Any) -> DialogTab:
        tab = DialogTab("about:blank", self._misuse)
        tab.target = info
        self.own_tabs[str(info.target_id)] = tab
        return tab

    async def open_tab(self, target_id: str) -> DialogTab:
        """A page opens a popup (or a program a tab): Chrome reports the target,
        and nodriver lists a Tab object for it; returns the answerer's own Tab."""
        listed = DialogTab("about:blank", self._misuse)
        listed.target = SimpleNamespace(type_="page", target_id=target_id)
        self._unlisted.append(listed)
        for callback in self.handlers.get(TargetCreated, []):
            await callback(_target_created(target_id))
        return self.own_tabs[target_id]

    async def get(self, url: str) -> Any:
        tab = await super().get(url)
        if url == LOGIN_URL:
            await _open_dialog(tab)
        return tab


@pytest.fixture(autouse=True)
def _own_tabs_from_the_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dialogs, "_own_tab", lambda browser, info: browser.own_tab(info))


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
        self, dialog_chrome: FakeChrome
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
        assert TargetCreated not in browser.handlers


class TestAProgramCanOptOut:
    async def test_a_headless_backend_built_with_answer_dialogs_false_leaves_them_open(
        self, dialog_chrome: FakeChrome
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
        assert TargetCreated not in browser.handlers
        assert browser.sent == []

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


class TestEveryTabIsAnswered:
    """A tab or popup opened after the start gets its own answerer (#320)."""

    async def test_a_popup_that_alerts_is_answered_once(self, dialog_chrome: FakeChrome) -> None:
        backend = NoDriverBackend(headless=True)
        await backend._start_async()
        try:
            (browser,) = dialog_chrome.browsers
            assert browser.sent == ["Target.setDiscoverTargets"]
            # Discovery reports the targets that exist: the first tab has its answerer.
            for callback in browser.handlers[TargetCreated]:
                await callback(_target_created("target-1"))
            assert browser.own_tabs == {}
            popup = await browser.open_tab("target-2")
            # Chrome reports a target again (a second caller): still one answerer.
            for callback in browser.handlers[TargetCreated]:
                await callback(_target_created("target-2"))
            await asyncio.wait_for(_open_dialog(popup), timeout=2)
        finally:
            await backend.stop_async()

        assert list(browser.own_tabs) == ["target-2"]
        assert len(popup.handlers[JavascriptDialogOpening]) == 1
        assert [m for m, _p in popup.sent if m == "Page.enable"] == ["Page.enable"]
        assert _answers(popup) == [{"accept": True}]
        assert _answers(browser.tabs[0]) == []
        # The Tab nodriver lists for a program to use is left to it.
        assert browser._unlisted[0].sent == []

    async def test_a_target_that_is_no_page_gets_no_answerer(
        self, dialog_chrome: FakeChrome
    ) -> None:
        backend = NoDriverBackend(headless=True)
        await backend._start_async()
        try:
            (browser,) = dialog_chrome.browsers
            for callback in browser.handlers[TargetCreated]:
                await callback(_target_created("worker-1", kind="service_worker"))
        finally:
            await backend.stop_async()

        assert browser.own_tabs == {}

    async def test_nodriver_048_answers_on_the_tab_it_made(self) -> None:
        """nodriver 0.48 reads the browser's own events on ``browser.connection``,
        and its own handler makes each target's Tab (a websocket per target)."""
        browser = DialogBrowser([], {}, FakeChrome())
        connection = DialogBrowser([], {}, FakeChrome())
        browser.connection = connection
        await dialogs.answer_dialogs_in_new_tabs(browser)
        assert connection.sent == ["Target.setDiscoverTargets"]
        assert browser.handlers == {}

        popup = DialogTab("about:blank", [])
        popup.target = SimpleNamespace(type_="page", target_id="target-2")
        browser._unlisted.append(popup)
        for callback in connection.handlers[TargetCreated]:
            await callback(_target_created("target-2"))
        await asyncio.wait_for(_open_dialog(popup), timeout=2)

        assert browser.own_tabs == {}
        assert _answers(popup) == [{"accept": True}]

    async def test_the_answerer_s_own_tab_is_a_nodriver_tab_on_the_target(self) -> None:
        from nodriver.core.tab import Tab

        info = _target_created("target-9").target_info
        browser = SimpleNamespace(websocket_url="ws://127.0.0.1:9/devtools/browser/x")
        tab = _REAL_OWN_TAB(browser, info)

        assert isinstance(tab, Tab)
        assert (tab.target, tab.parent, tab.session_id) == (info, browser, None)
        assert tab.websocket_url == browser.websocket_url

    async def test_the_handler_is_registered_once_per_browser(self) -> None:
        browser = DialogBrowser([], {}, FakeChrome())
        await dialogs.answer_dialogs_in_new_tabs(browser)
        await dialogs.answer_dialogs_in_new_tabs(browser)

        assert len(browser.handlers[TargetCreated]) == 1
        assert browser.sent == ["Target.setDiscoverTargets"]


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
