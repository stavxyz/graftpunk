"""JavaScript dialogs in a headless recording (#320).

A page's ``alert()``, ``confirm()``, ``prompt()`` or ``beforeunload`` dialog stops its
script until someone answers it, and in a headless run nobody can, so the recording
never saved. A headless capture answers each through CDP; a visible one leaves them
to the user. The fake tab is the post-data tests' (real nodriver event objects, CDP
replies as Chrome gives them).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from nodriver.cdp.page import JavascriptDialogOpening
from nodriver.cdp.util import parse_json_event
from structlog.testing import capture_logs

from graftpunk import dialogs
from graftpunk.observe import capture
from graftpunk.observe.capture import NodriverCaptureBackend
from tests.unit.test_observe_post_data import FakeTab, _always, _start, _stop

MESSAGE = "Your balance for account 0042-7731 is due"
DEFAULT_PROMPT = "acct-0042-7731"
PAGE_URL = "https://myshop.example/account/statements?acct=0042-7731#top"


def _dialog(kind: str, *, default_prompt: str | None = DEFAULT_PROMPT) -> Any:
    params: dict[str, Any] = {
        "url": PAGE_URL,
        "frameId": "frame-1",
        "message": MESSAGE,
        "type": kind,
        "hasBrowserHandler": False,
    }
    if default_prompt is not None:
        params["defaultPrompt"] = default_prompt
    return parse_json_event({"method": "Page.javascriptDialogOpening", "params": params})


def _answers(tab: FakeTab) -> list[dict[str, Any]]:
    return [params for method, params in tab.sent if method == "Page.handleJavaScriptDialog"]


def _no_page_text_in(logs: list[dict[str, Any]]) -> None:
    text = json.dumps(logs, default=str)
    for secret in (MESSAGE, DEFAULT_PROMPT, "/account/statements", "0042-7731"):
        assert secret not in text


class SessionTab(FakeTab):
    """Records the CDP session each command was answered on."""

    def __init__(self) -> None:
        super().__init__({}, _always)
        self.sessions: list[tuple[str, str | None]] = []

    def _answer(self, cmd: Any, session: str | None, **kwargs: Any) -> Any:
        result = super()._answer(cmd, session, **kwargs)
        self.sessions.append((self.sent[-1][0], session))
        return result


class TestHeadlessCaptureAnswersDialogs:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["alert", "confirm", "prompt", "beforeunload"])
    async def test_each_dialog_is_accepted_once_and_logged_without_its_text(
        self, kind: str
    ) -> None:
        tab = FakeTab({}, _always)
        with capture_logs() as logs:
            backend = await _start(tab, answer_dialogs=True)
            await tab.dispatch(_dialog(kind))
            await _stop(backend, tab)

        expected: dict[str, Any] = {"accept": True}
        if kind == "prompt":
            expected["promptText"] = DEFAULT_PROMPT
        assert _answers(tab) == [expected]
        assert backend.dialogs_answered == 1
        (answered,) = [e for e in logs if e["event"] == "nodriver_dialog_answered"]
        assert answered["dialog_type"] == kind
        assert answered["choice"] == "accept"
        assert answered["host"] == "myshop.example"
        (summary,) = [e for e in logs if e["event"] == "nodriver_dialog_summary"]
        assert (summary["answered"], summary["failed"]) == (1, 0)
        _no_page_text_in(logs)

    @pytest.mark.asyncio
    async def test_a_prompt_with_no_default_text_is_accepted_empty(self) -> None:
        tab = FakeTab({}, _always)
        backend = await _start(tab, answer_dialogs=True)
        await tab.dispatch(_dialog("prompt", default_prompt=None))
        await _stop(backend, tab)

        assert _answers(tab) == [{"accept": True, "promptText": ""}]

    @pytest.mark.asyncio
    async def test_the_page_domain_is_enabled_without_dom_snapshots(self) -> None:
        """The dialog event comes only with the Page domain on; DOM snapshots (which
        enable it too) are off here."""
        tab = FakeTab({}, _always)
        await _start(tab, answer_dialogs=True)

        assert ("Page.enable", {}) in tab.sent
        assert JavascriptDialogOpening in tab.handlers

    @pytest.mark.asyncio
    async def test_the_answer_goes_to_the_session_that_enabled_the_page_domain(self) -> None:
        """nodriver 0.50.6's Browser.get(url) opens a second CDP session on the tab;
        Chrome holds the open dialog for the session whose Page domain saw it."""
        tab = SessionTab()
        backend = await _start(tab, answer_dialogs=True)
        tab.reattach()
        await tab.dispatch(_dialog("alert"))
        await _stop(backend, tab)

        assert ("Page.enable", "session-1") in tab.sessions
        assert ("Page.handleJavaScriptDialog", "session-1") in tab.sessions


class TestTheCountTheRunReports:
    @pytest.mark.asyncio
    async def test_a_dialog_answered_before_the_capture_started_is_counted(self) -> None:
        """A headless NoDriverBackend answers from its start, before the login page
        loads; the capture shares that answerer, so it neither answers twice nor
        misses the count."""
        tab = FakeTab({}, _always)
        await dialogs.answer_dialogs(tab)
        await tab.dispatch(_dialog("alert"))
        backend = await _start(tab, answer_dialogs=True)
        await tab.dispatch(_dialog("confirm"))
        await _stop(backend, tab)

        assert len(_answers(tab)) == 2
        assert backend.dialogs_answered == 2

    @pytest.mark.asyncio
    async def test_the_summary_ends_stop_and_matches_the_saved_count(self) -> None:
        tab = FakeTab({}, _always)
        backend = await _start(tab, answer_dialogs=True)
        with capture_logs() as logs:
            await _stop(backend, tab)
        await tab.dispatch(_dialog("alert"))

        assert logs[-1]["event"] == "nodriver_dialog_summary"
        assert (logs[-1]["answered"], logs[-1]["failed"]) == (0, 0)
        # Answered after stop: the run's count stays the one stop logged.
        assert len(_answers(tab)) == 1
        assert backend.dialogs_answered == 0


class TestAFailedAnswerNeverHoldsUpTheRun:
    @pytest.mark.asyncio
    async def test_a_refused_answer_is_a_debug_line_and_the_run_stops(self) -> None:
        """The dialog closed already, or the target went away: Chrome refuses."""
        tab = FakeTab({}, _always)
        tab.refuse = {"Page.handleJavaScriptDialog"}
        with capture_logs() as logs:
            backend = await _start(tab, answer_dialogs=True)
            await tab.dispatch(_dialog("confirm"))
            await _stop(backend, tab)

        assert backend.dialogs_answered == 0
        (failed,) = [e for e in logs if e["event"] == "nodriver_dialog_answer_failed"]
        assert failed["log_level"] == "debug"
        assert failed["dialog_type"] == "confirm"
        (summary,) = [e for e in logs if e["event"] == "nodriver_dialog_summary"]
        assert (summary["answered"], summary["failed"]) == (0, 1)
        _no_page_text_in(logs)

    @pytest.mark.asyncio
    async def test_an_answer_chrome_never_replies_to_is_given_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dialogs, "_DIALOG_ANSWER_WAIT_S", 0.01)

        class Silent(FakeTab):
            async def send(self, cmd: Any, **kwargs: Any) -> Any:
                if getattr(cmd, "__name__", "") == "handle_java_script_dialog":
                    await asyncio.Event().wait()
                return await super().send(cmd, **kwargs)

        tab = Silent({}, _always)
        backend = await _start(tab, answer_dialogs=True)
        handler = tab.handlers[JavascriptDialogOpening][0]
        with capture_logs() as logs:
            await asyncio.wait_for(handler(_dialog("alert")), timeout=2)
        await asyncio.wait_for(_stop(backend, tab), timeout=2)

        (failed,) = [e for e in logs if e["event"] == "nodriver_dialog_answer_failed"]
        assert failed["exc_type"] == "TimeoutError"
        assert backend.dialogs_answered == 0

    @pytest.mark.asyncio
    async def test_a_target_that_refuses_the_page_domain_is_still_recorded(self) -> None:
        tab = FakeTab({}, _always)
        tab.refuse = {"Page.enable"}
        with capture_logs() as logs:
            backend = await _start(tab, answer_dialogs=True)
            await _stop(backend, tab)

        assert [e["event"] for e in logs if e["log_level"] == "warning"] == [
            "nodriver_dialog_answering_unavailable"
        ]


class TestAVisibleCaptureLeavesDialogsToTheUser:
    @pytest.mark.asyncio
    async def test_no_dialog_handler_and_no_page_domain(self) -> None:
        tab = FakeTab({}, _always)
        backend = await _start(tab)
        await _stop(backend, tab)

        assert JavascriptDialogOpening not in tab.handlers
        assert not any(method.startswith("Page.") for method, _params in tab.sent)
        assert backend.dialogs_answered == 0

    def test_the_factory_passes_the_choice_to_the_nodriver_backend(self) -> None:
        for answer in (True, False):
            backend = capture.create_capture_backend(
                "nodriver", object(), get_tab=lambda: None, answer_dialogs=answer
            )
            assert isinstance(backend, NodriverCaptureBackend)
            assert backend._answer_dialogs is answer
