"""#268 part 3: the wrong-password step of ``gp observe interactive``.

The recorder side: with ``failure_step``, the page is read raw FAILURE_READ_S after
each credential post's response and again when the user marks the last one failed;
only the failure texts that survive are kept, on that post's HAR entry. Driven with
the fake tab and fake clock of the after-read tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from graftpunk.cli.main import app
from graftpunk.cli.observe_browser import ObserveSession, run_observe_interactive
from graftpunk.observe import capture
from graftpunk.observe.capture import NodriverCaptureBackend
from tests.unit.cli_harness import strip_ansi
from tests.unit.test_observe_post_data import (
    _LOGIN_MODAL,
    _POST_MS,
    LOGIN,
    FakeClock,
    FakeTab,
    _binding_called,
    _idle,
    _never,
    _response_received,
    _start,
    _stop,
    _will_be_sent,
)

_WRONG = json.dumps({"username": "jsmith", "password": "wrongpass1"})
_RIGHT = json.dumps({"username": "jsmith", "password": "realsecret9"})
_TOAST = "Invalid username or password."
_STAYS = "Too many attempts may lock you out."
_FAILED_PAGE = _LOGIN_MODAL.replace(
    '<div id="app">', f'<div id="app"><div role="alert">{_TOAST}</div><p>{_STAYS}</p>'
)
# The toast has dismissed itself by the time the user presses Enter.
_FAILED_PAGE_LATER = _LOGIN_MODAL.replace('<div id="app">', f'<div id="app"><p>{_STAYS}</p>')
_LANDED = "<html><body><div id='account-menu'>Your orders</div></body></html>"


async def _recording(
    tmp_path: Path, *, step: bool = True
) -> tuple[FakeTab, NodriverCaptureBackend, FakeClock, list[float]]:
    tab = FakeTab({}, _never)
    tab.dom_html = _FAILED_PAGE
    clock = FakeClock()
    backend = await _start(tab, dom_snapshots_dir=tmp_path / "snapshots", failure_step=step)
    backend._now, backend._sleep = clock.time, clock.sleep
    reads: list[float] = []
    tab.on_dom_read = lambda: reads.append(clock.now)
    return tab, backend, clock, reads


async def _post(tab: FakeTab, rid: str, body: str = _WRONG, *, respond: bool = True) -> None:
    # The listener's snapshot is the post's own, so every DOM read is a read after it.
    await tab.dispatch(_binding_called(_LOGIN_MODAL, time_ms=_POST_MS - 40))
    await tab.dispatch(_will_be_sent(rid, inline=body, url=LOGIN))
    if respond:
        await tab.dispatch(_response_received(rid, url=LOGIN))


def _request(backend: NodriverCaptureBackend, rid_url_index: int = 0) -> dict[str, Any]:
    return backend.get_har_entries()[rid_url_index]["request"]


def _run_text(tmp_path: Path) -> str:
    return "".join(path.read_text() for path in tmp_path.rglob("*") if path.is_file())


class TestTheReadsForTheFailedPost:
    @pytest.mark.asyncio
    async def test_the_1_5_s_read_and_the_enter_read_give_the_candidates(
        self, tmp_path: Path
    ) -> None:
        tab, backend, clock, reads = await _recording(tmp_path)
        await _post(tab, "90.1")
        await clock.advance(1.4, backend)
        assert reads == []
        await clock.advance(0.1, backend)
        assert reads == [capture.FAILURE_READ_S]
        assert capture.FAILURE_READ_S == 1.5
        tab.dom_html = _FAILED_PAGE_LATER
        await clock.advance(0.5, backend)
        assert backend.mark_failed_login() is True
        await _idle(backend, clock)
        assert reads == [1.5, 2.0]
        await _stop(backend, tab)

        request = _request(backend)
        assert request["_userMarkedFailed"] is True
        # The toast only the 1.5 s read caught ranks first: it was in an alert.
        assert request["_failureTextCandidates"] == [_TOAST, _STAYS]
        # Text only: no failure page is written, and the after-read keeps no text.
        assert _TOAST not in _run_text(tmp_path)
        assert _STAYS not in _run_text(tmp_path)

    @pytest.mark.asyncio
    async def test_enter_before_the_1_5_s_read_waits_for_it(self, tmp_path: Path) -> None:
        tab, backend, clock, reads = await _recording(tmp_path)
        await _post(tab, "90.2")
        await clock.advance(1, backend)
        assert backend.mark_failed_login() is True
        await _idle(backend, clock)
        assert reads == [1.0]
        tab.dom_html = _FAILED_PAGE_LATER
        await clock.advance(0.5, backend)
        assert reads == [1.0, 1.5]
        await _stop(backend, tab)
        assert _request(backend)["_failureTextCandidates"] == [_TOAST, _STAYS]

    @pytest.mark.asyncio
    async def test_the_last_credential_post_before_enter_is_the_one_marked(
        self, tmp_path: Path
    ) -> None:
        tab, backend, clock, _reads = await _recording(tmp_path)
        await _post(tab, "91.1")
        await clock.advance(2, backend)
        await _post(tab, "91.2")
        await clock.advance(2, backend)
        assert backend.mark_failed_login() is True
        await _idle(backend, clock)
        await _stop(backend, tab)

        first, second = (entry["request"] for entry in backend.get_har_entries())
        assert "_userMarkedFailed" not in first
        assert "_failureTextCandidates" not in first
        assert second["_userMarkedFailed"] is True
        assert second["_failureTextCandidates"] == [_TOAST, _STAYS]

    @pytest.mark.asyncio
    async def test_enter_with_no_credential_post_marks_nothing_and_keeps_the_step(
        self, tmp_path: Path
    ) -> None:
        tab, backend, clock, _reads = await _recording(tmp_path)
        assert backend.mark_failed_login() is False
        await _post(tab, "92.1")
        await clock.advance(2, backend)
        assert backend.mark_failed_login() is True
        assert backend.mark_failed_login() is False
        await _idle(backend, clock)
        await _stop(backend, tab)
        assert _request(backend)["_failureTextCandidates"] == [_TOAST, _STAYS]

    @pytest.mark.asyncio
    async def test_posts_after_the_mark_get_no_1_5_s_read(self, tmp_path: Path) -> None:
        tab, backend, clock, reads = await _recording(tmp_path)
        await _post(tab, "93.1")
        await clock.advance(2, backend)
        backend.mark_failed_login()
        await _idle(backend, clock)
        assert reads == [1.5, 2.0]
        await _post(tab, "93.2", _RIGHT)
        await clock.advance(2, backend)
        # The failed post's own after-read at 3 s; none at 3.5 s for the new post.
        assert reads == [1.5, 2.0, 3.0]
        await clock.advance(1, backend)
        # The new post's after-read, 3 s after its response.
        assert reads == [1.5, 2.0, 3.0, 5.0]
        await _stop(backend, tab)


class TestTheRealLogin:
    @pytest.mark.asyncio
    async def test_a_candidate_on_the_real_login_s_page_is_dropped(self, tmp_path: Path) -> None:
        tab, backend, clock, _reads = await _recording(tmp_path)
        await _post(tab, "94.1")
        await clock.advance(2, backend)
        backend.mark_failed_login()
        await _idle(backend, clock)
        tab.dom_html = _LANDED.replace("Your orders", f"Your orders <p>{_STAYS}</p>")
        await _post(tab, "94.2", _RIGHT)
        await clock.advance(3, backend)
        await _stop(backend, tab)

        failed = _request(backend, 0)
        assert failed["_failureTextCandidates"] == [_TOAST]
        assert "_failureTextCandidates" not in _request(backend, 1)

    @pytest.mark.asyncio
    async def test_a_candidate_holding_the_real_post_s_value_is_dropped(
        self, tmp_path: Path
    ) -> None:
        tab, backend, clock, _reads = await _recording(tmp_path)
        tab.dom_html = _FAILED_PAGE.replace(_STAYS, "No account named jsmith2024 here.")
        await _post(tab, "95.1", json.dumps({"username": "bob_tester", "password": "wrongpass1"}))
        await clock.advance(2, backend)
        backend.mark_failed_login()
        await _idle(backend, clock)
        tab.dom_html = _LANDED
        real = json.dumps({"username": "jsmith2024", "password": "realsecret9"})
        await _post(tab, "95.2", real, respond=False)
        await _stop(backend, tab)
        assert _request(backend, 0)["_failureTextCandidates"] == [_TOAST]

    @pytest.mark.asyncio
    async def test_a_real_post_whose_values_cannot_be_read_drops_them_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.unit.test_observe_post_data import _unreadable_credential_body

        tab, backend, clock, _reads = await _recording(tmp_path)
        await _post(tab, "96.1")
        await clock.advance(2, backend)
        backend.mark_failed_login()
        await _idle(backend, clock)
        unreadable = _unreadable_credential_body(monkeypatch)
        await _post(tab, "96.2", unreadable, respond=False)
        await _stop(backend, tab)
        assert _request(backend, 0)["_failureTextCandidates"] == []


class TestStopAndPrivacy:
    @pytest.mark.asyncio
    async def test_stop_cancels_a_pending_1_5_s_read(self, tmp_path: Path) -> None:
        tab, backend, clock, reads = await _recording(tmp_path)
        await _post(tab, "97.1")
        await clock.advance(1, backend)
        await _stop(backend, tab)
        await clock.advance(60, backend)
        assert reads == []
        assert backend._after_read_tasks == set()
        assert backend._step_reads == {}
        assert backend._step_before == {}
        assert backend._step_posts == []

    @pytest.mark.asyncio
    async def test_stop_cancels_the_enter_read(self, tmp_path: Path) -> None:
        import asyncio

        tab, backend, clock, _reads = await _recording(tmp_path)
        await _post(tab, "97.2")
        await clock.advance(2, backend)
        tab.dom_gate = asyncio.Event()
        earlier = set(backend._after_read_tasks)
        backend.mark_failed_login()
        (enter_read,) = backend._after_read_tasks - earlier
        for _ in range(5):
            await asyncio.sleep(0)
        assert not enter_read.done()
        await _stop(backend, tab)
        assert enter_read.cancelled()
        request = _request(backend)
        assert request["_userMarkedFailed"] is True
        assert request["_failureTextCandidates"] == []

    @pytest.mark.asyncio
    async def test_without_the_step_nothing_is_read_or_kept(self, tmp_path: Path) -> None:
        tab, backend, clock, reads = await _recording(tmp_path, step=False)
        await _post(tab, "98.1")
        await clock.advance(2, backend)
        assert reads == []
        assert backend.mark_failed_login() is False
        assert backend.failed_login_secrets() == ()
        await clock.advance(1, backend)
        assert reads == [3.0]
        await _stop(backend, tab)
        request = _request(backend)
        assert "_userMarkedFailed" not in request
        assert "_failureTextCandidates" not in request
        assert backend._step_posts == []
        assert backend._step_before == {}
        assert backend._step_reads == {}

    @pytest.mark.asyncio
    async def test_the_failed_password_is_handed_to_the_run_s_redaction(
        self, tmp_path: Path
    ) -> None:
        from graftpunk.observe.run import save_observe_run
        from graftpunk.observe.storage import ObserveStorage

        storage = ObserveStorage(tmp_path, "myshop", "r1")
        tab = FakeTab({}, _never)
        tab.dom_html = _FAILED_PAGE
        clock = FakeClock()
        backend = NodriverCaptureBackend(
            object(),
            get_tab=lambda: tab,
            bodies_dir=storage.run_dir / "bodies",
            dom_snapshots_dir=storage.run_dir / "snapshots",
            failure_step=True,
        )
        backend._now, backend._sleep = clock.time, clock.sleep
        await backend.start_capture_async()
        await _post(tab, "99.1")
        await clock.advance(2, backend)
        backend.mark_failed_login()
        await _idle(backend, clock)
        assert backend.failed_login_secrets() == ("wrongpass1",)
        tab.dom_html = _LANDED
        await save_observe_run(
            storage, backend, "interactive-final", redact=backend.failed_login_secrets()
        )

        har = (storage.run_dir / "network.har").read_text()
        assert "wrongpass1" not in har
        (entry,) = json.loads(har)["log"]["entries"]
        assert entry["request"]["_userMarkedFailed"] is True
        assert entry["request"]["_failureTextCandidates"] == [_TOAST, _STAYS]
        assert _TOAST not in _run_text(storage.run_dir).replace(har, "")


class TestTheLoginPageAsServed:
    def test_the_latest_html_response_of_the_sending_page(self) -> None:
        backend = NodriverCaptureBackend(object(), dom_snapshots_dir=Path("/nonexistent"))
        post = {"url": LOGIN, "method": "POST", "sender_url": LOGIN + "?next=%2F"}
        page = {"url": LOGIN, "response": {"mimeType": "text/html", "body": "<p>old</p>"}}
        again = {"url": LOGIN + "#top", "response": {"mimeType": "text/html", "body": "<p>new</p>"}}
        script = {"url": LOGIN, "response": {"mimeType": "application/json", "body": "{}"}}
        backend._request_map = {"1": page, "2": again, "3": script, "4": post}
        assert backend._served_page(post) == "<p>new</p>"
        backend._request_map = {"3": script, "4": post}
        assert backend._served_page(post) is None


# The prompt: gp observe interactive --wrong-password-first reads Enter on stdin
# inside the asyncio loop, beside the SIGINT handler that stops the recording. A pipe
# stands in for the terminal; the loop's own reader and signal handler are real.


class _FakeTerminal:
    """A stdin whose file descriptor is a pipe's read end."""

    def __init__(self, *, tty: bool = True) -> None:
        self.read_fd, self.write_fd = os.pipe()
        self.tty = tty

    def fileno(self) -> int:
        return self.read_fd

    def isatty(self) -> bool:
        return self.tty

    def type(self, data: bytes) -> None:
        os.write(self.write_fd, data)

    def close_input(self) -> None:
        os.close(self.write_fd)

    def close(self) -> None:
        for fd in (self.read_fd, self.write_fd):
            with contextlib.suppress(OSError):
                os.close(fd)


class _Untouchable:
    """A stdin no part of the command may use."""

    def fileno(self) -> int:
        raise AssertionError("stdin was read")

    def isatty(self) -> bool:
        raise AssertionError("stdin was read")


async def _until(condition: Callable[[], bool]) -> None:
    """Until *condition* holds; the loop turns between checks let the reader and the
    signal handler run."""
    for _ in range(2000):
        if condition():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("the condition never held")


async def _turns(count: int = 20) -> None:
    for _ in range(count):
        await asyncio.sleep(0)


class _Interactive:
    """run_observe_interactive with the browser set-up, the save, and the stop
    replaced; the console writes to a buffer."""

    def __init__(self, marks: list[bool] | None = None) -> None:
        self.backend = MagicMock()
        self.backend.mark_failed_login = MagicMock(side_effect=marks or [True])
        self.backend.failed_login_secrets = MagicMock(return_value=("wrongpass1",))
        self.out = StringIO()
        self.console = Console(file=self.out, no_color=True, width=200)
        self.setup = AsyncMock(
            return_value=ObserveSession(
                browser=MagicMock(),
                handle=MagicMock(),
                tab=MagicMock(),
                storage=MagicMock(),
                backend=self.backend,
            )
        )
        self.save = AsyncMock()

    @property
    def text(self) -> str:
        return " ".join(self.out.getvalue().split())

    def patches(self) -> contextlib.ExitStack:
        stack = contextlib.ExitStack()
        where = "graftpunk.cli.observe_browser"
        stack.enter_context(patch(f"{where}.setup_observe_session", self.setup))
        stack.enter_context(patch(f"{where}.save_observe_run", self.save))
        stack.enter_context(patch(f"{where}.stop_observe_browser"))
        stack.enter_context(patch(f"{where}.console", self.console))
        return stack

    def start(self, stdin: Any, *, step: bool = True) -> asyncio.Future[None]:
        kwargs: dict[str, Any] = {"session_name": None, "stdin": stdin}
        if step:
            kwargs["wrong_password_first"] = True
        return asyncio.ensure_future(
            run_observe_interactive("myshop", "https://myshop.example/login", 1024, **kwargs)
        )


def _ctrl_c() -> None:
    signal.raise_signal(signal.SIGINT)


class TestThePrompt:
    @pytest.mark.asyncio
    async def test_enter_marks_the_failed_post_and_ctrl_c_saves_without_its_password(
        self,
    ) -> None:
        run, terminal = _Interactive(), _FakeTerminal()
        try:
            with run.patches():
                task = run.start(terminal)
                await _until(lambda: "press Enter here" in run.text)
                assert "throwaway wrong password" in run.text
                assert "never your real one with a typo" in run.text
                run.backend.mark_failed_login.assert_not_called()
                terminal.type(b"\n")
                await _until(lambda: "Marked" in run.text)
                assert "log in for real" in run.text.split("Marked")[-1]
                _ctrl_c()
                await task
        finally:
            terminal.close()
        assert run.setup.await_args.kwargs["failure_step"] is True
        run.backend.mark_failed_login.assert_called_once_with()
        assert run.save.await_args.kwargs["redact"] == ("wrongpass1",)

    @pytest.mark.asyncio
    async def test_enter_before_any_credential_post_asks_again(self) -> None:
        run, terminal = _Interactive([False, True]), _FakeTerminal()
        try:
            with run.patches():
                task = run.start(terminal)
                await _until(lambda: "press Enter here" in run.text)
                terminal.type(b"\n")
                await _until(lambda: "No credential post yet" in run.text)
                assert "Marked" not in run.text
                terminal.type(b"typed by mistake\n")
                await _until(lambda: "Marked" in run.text)
                assert run.backend.mark_failed_login.call_count == 2
                _ctrl_c()
                await task
        finally:
            terminal.close()

    @pytest.mark.asyncio
    async def test_input_after_the_mark_is_not_read(self) -> None:
        run, terminal = _Interactive([True]), _FakeTerminal()
        try:
            with run.patches():
                task = run.start(terminal)
                await _until(lambda: "press Enter here" in run.text)
                terminal.type(b"\n")
                await _until(lambda: "Marked" in run.text)
                terminal.type(b"\n\n")
                await _turns()
                _ctrl_c()
                await task
        finally:
            terminal.close()
        run.backend.mark_failed_login.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_ctrl_c_during_the_step_saves_and_stops_reading_stdin(self) -> None:
        run, terminal = _Interactive(), _FakeTerminal()
        try:
            with run.patches():
                task = run.start(terminal)
                await _until(lambda: "press Enter here" in run.text)
                _ctrl_c()
                await task
                # The loop no longer watches stdin: there is no reader left to remove.
                assert asyncio.get_running_loop().remove_reader(terminal.read_fd) is False
        finally:
            terminal.close()
        run.backend.mark_failed_login.assert_not_called()
        run.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_end_of_input_skips_the_step_and_keeps_recording(self) -> None:
        run, terminal = _Interactive(), _FakeTerminal()
        try:
            with run.patches():
                task = run.start(terminal)
                await _until(lambda: "press Enter here" in run.text)
                terminal.close_input()
                await _until(lambda: "skipped" in run.text)
                await _turns()
                assert not task.done()
                _ctrl_c()
                await task
        finally:
            terminal.close()
        run.backend.mark_failed_login.assert_not_called()
        run.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_stdin_that_is_no_terminal_refuses_before_the_browser_opens(self) -> None:
        run, terminal = _Interactive(), _FakeTerminal(tty=False)
        try:
            with run.patches():
                task = run.start(terminal)
                with pytest.raises(typer.Exit) as exited:
                    await task
        finally:
            terminal.close()
        assert exited.value.exit_code == 1
        assert "needs a terminal" in run.text
        run.setup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_without_the_flag_stdin_is_untouched_and_the_run_is_main_s(self) -> None:
        run = _Interactive()
        with run.patches():
            task = run.start(_Untouchable(), step=False)
            await _until(lambda: "Recording..." in run.text)
            _ctrl_c()
            await task
        assert run.setup.await_args.kwargs["failure_step"] is False
        # main's lines, word for word.
        assert run.text == (
            "Recording... press Ctrl+C to stop and save Tip: Use Ctrl+C in this terminal "
            "for a complete capture (screenshot, page source, and network data). Closing "
            "the browser window will preserve network data only. Recording stopped. "
            "Saving capture..."
        )
        run.backend.mark_failed_login.assert_not_called()
        run.backend.failed_login_secrets.assert_not_called()
        assert run.save.await_args.kwargs == {"console": run.console}


class TestTheFlag:
    @pytest.mark.parametrize("flag", [True, False])
    def test_the_command_passes_it_on(self, flag: bool) -> None:
        args = ["observe", "--no-session", "interactive", "https://myshop.example/login"]
        with (
            patch(
                "graftpunk.cli.observe_commands.run_observe_interactive", new_callable=MagicMock
            ) as interactive,
            patch("graftpunk.cli.observe_commands.asyncio"),
        ):
            result = CliRunner().invoke(app, [*args, "--wrong-password-first"] if flag else args)
        assert result.exit_code == 0, result.output
        assert interactive.call_args.kwargs["wrong_password_first"] is flag

    def test_the_help_names_it(self) -> None:
        result = CliRunner().invoke(app, ["observe", "interactive", "--help"])
        assert "--wrong-password-first" in strip_ansi(result.output)
