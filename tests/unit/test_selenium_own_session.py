"""A selenium launch in a session of its own, so a terminal's Ctrl+C reaches only Python (#335).

Every Chrome and chromedriver here is a stand-in: a Python child spawned
through the same ``subprocess.Popen`` names undetected-chromedriver and
Selenium use, so sessions, signals and the reaper act on real processes and
no browser starts.
"""

from __future__ import annotations

import inspect
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import suppress
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import selenium.webdriver.common.service as selenium_service
import undetected_chromedriver
from selenium.webdriver.common.service import Service as SeleniumService
from structlog.testing import capture_logs

from graftpunk import browser_launch, chrome_orphans, signals
from graftpunk.browser_launch import (
    CHROME_SHARES_SESSION_CONSEQUENCE,
    CleanupReport,
    SeleniumProcesses,
    attach_selenium_processes,
    check_in_own_session,
    release_selenium_processes,
    selenium_in_own_session,
)
from graftpunk.chrome_orphans import OWNER_SWITCH, owner_switch

pytestmark = pytest.mark.skipif(os.name != "posix", reason="sessions are a POSIX notion")

_SLEEP = "import time; time.sleep(60)"


@pytest.fixture()
def spawned() -> Iterator[list[subprocess.Popen[Any]]]:
    """Every stand-in a test starts, killed at the end whatever happened."""
    processes: list[subprocess.Popen[Any]] = []
    yield processes
    for process in processes:
        if process.poll() is None:
            process.kill()
            process.wait(5)


@pytest.fixture()
def armed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let this process signal, which the unit suite's autouse fixture forbids."""
    monkeypatch.setattr(chrome_orphans, "_ARMED", True)


@pytest.fixture()
def handlers_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """As in ``gp``: the handlers are installed. The orphan sweep is stubbed out."""
    monkeypatch.setattr(signals, "termination_cleanup_installed", lambda: True)
    monkeypatch.setattr(browser_launch, "prepare_browser_launch", CleanupReport)


def _sleeper(popen: Any, *extra: str) -> subprocess.Popen[Any]:
    return popen([sys.executable, "-c", _SLEEP, *extra], stdin=subprocess.PIPE)


def _exited_within(process: subprocess.Popen[Any], timeout_s: float) -> int | None:
    try:
        return process.wait(timeout_s)
    except subprocess.TimeoutExpired:
        return None


class TestTheSeamStillFits:
    """The block rebinds one name in each of two library modules; these say when that breaks."""

    def test_undetected_chromedriver_is_the_release_the_seam_was_written_against(self) -> None:
        # Version pin: undetected-chromedriver's Chrome.__init__ starts Chrome
        # with its one subprocess.Popen call. Another release may move it;
        # re-read the source, and update this pin and the two below.
        assert metadata.version("undetected-chromedriver") == "3.5.5"

    def test_undetected_chromedriver_starts_chrome_through_its_subprocess_name(self) -> None:
        source = inspect.getsource(undetected_chromedriver.Chrome.__init__)

        assert undetected_chromedriver.subprocess is subprocess
        assert "browser = subprocess.Popen(" in source
        assert "selenium.webdriver.chromium.service.ChromiumService(" in source
        assert "start_new_session" not in source

    def test_selenium_starts_chromedriver_through_its_subprocess_name(self) -> None:
        source = inspect.getsource(SeleniumService._start_process)

        assert selenium_service.subprocess is subprocess
        assert "self.process = subprocess.Popen(" in source
        assert "**self.popen_kw" in source


class TestTheBlock:
    def test_a_launch_inside_it_starts_in_a_new_session_and_is_registered(
        self, spawned: list[subprocess.Popen[Any]]
    ) -> None:
        with selenium_in_own_session() as launch:
            chrome = _sleeper(undetected_chromedriver.subprocess.Popen)
            driver = _sleeper(selenium_service.subprocess.Popen, "--port=0")
        spawned += [chrome, driver]

        assert launch.applied is True
        assert launch.processes.pids == [chrome.pid, driver.pid]
        assert os.getsid(chrome.pid) == chrome.pid
        assert os.getsid(driver.pid) == driver.pid
        # chromedriver gets the owner marker here; Chrome gets it in its options.
        assert driver.args[-1] == owner_switch()
        assert not any(str(arg).startswith(OWNER_SWITCH) for arg in chrome.args)
        # Still registered after the block: the driver is in use until it is quit.
        assert launch.processes in signals.live_browsers()
        assert undetected_chromedriver.subprocess is subprocess
        assert selenium_service.subprocess is subprocess
        assert browser_launch._CURRENT_SELENIUM_LAUNCH.get() is None

    def test_another_threads_launch_while_it_is_open_is_untouched(
        self, spawned: list[subprocess.Popen[Any]]
    ) -> None:
        elsewhere: list[subprocess.Popen[Any]] = []
        with selenium_in_own_session() as launch:
            thread = threading.Thread(
                target=lambda: elsewhere.append(
                    _sleeper(selenium_service.subprocess.Popen, "--port=0")
                )
            )
            thread.start()
            thread.join(10)
        spawned += elsewhere
        [process] = elsewhere

        assert os.getsid(process.pid) == os.getsid(0)
        assert process.args[-1] == "--port=0"
        assert launch.processes.pids == []

    def test_an_exception_leaving_it_ends_what_it_spawned(
        self, spawned: list[subprocess.Popen[Any]]
    ) -> None:
        with pytest.raises(KeyboardInterrupt), selenium_in_own_session() as launch:
            spawned.append(_sleeper(undetected_chromedriver.subprocess.Popen))
            spawned.append(_sleeper(selenium_service.subprocess.Popen))
            raise KeyboardInterrupt

        assert [process.returncode for process in spawned] == [-signal.SIGTERM] * 2
        assert launch.processes not in signals.live_browsers()

    def test_overlapping_blocks_restore_the_modules_once(self) -> None:
        outer = selenium_in_own_session()
        outer.__enter__()
        with selenium_in_own_session():
            pass
        assert undetected_chromedriver.subprocess is not subprocess
        outer.__exit__(None, None, None)

        assert undetected_chromedriver.subprocess is subprocess
        assert selenium_service.subprocess is subprocess
        assert browser_launch._selenium_proxy_users == 0

    def test_a_module_something_else_rebound_is_left_alone_with_a_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = SimpleNamespace(Popen=subprocess.Popen)
        monkeypatch.setattr(selenium_service, "subprocess", sentinel)

        with capture_logs() as logs, selenium_in_own_session() as launch:
            pass

        assert launch.applied is False
        assert selenium_service.subprocess is sentinel
        assert undetected_chromedriver.subprocess is subprocess
        [event] = [e for e in logs if e["event"] == "chrome_own_session_unavailable"]
        assert event["reason"] == "launcher_changed"
        assert event["consequence"] == CHROME_SHARES_SESSION_CONSEQUENCE


class TestTheProcesses:
    def test_the_signal_handler_ends_each_process_still_running(
        self, armed: None, spawned: list[subprocess.Popen[Any]]
    ) -> None:
        running = _sleeper(subprocess.Popen)
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait(10)
        spawned += [running, exited]
        processes = SeleniumProcesses()
        processes.add(running)
        processes.add(exited)

        processes._terminate_for_signal()

        assert _exited_within(running, 10) == -signal.SIGTERM

    def test_the_signal_handler_ends_a_chrome_known_by_pid_only(
        self, armed: None, spawned: list[subprocess.Popen[Any]]
    ) -> None:
        """The standard path's Chrome, which chromedriver spawned: held by ``add_pid``."""
        chrome = _sleeper(subprocess.Popen, "--remote-debugging-port=0", owner_switch())
        spawned.append(chrome)
        processes = SeleniumProcesses()
        processes.add_pid(chrome.pid)

        processes._terminate_for_signal()

        assert _exited_within(chrome, 10) == -signal.SIGTERM

    def test_the_signal_handler_never_signals_a_process_that_has_exited(
        self, monkeypatch: pytest.MonkeyPatch, spawned: list[subprocess.Popen[Any]]
    ) -> None:
        """Its pid is free for the kernel to hand to another program."""
        running = _sleeper(subprocess.Popen)
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait(10)
        spawned += [running, exited]
        processes = SeleniumProcesses()
        processes.add(running)
        processes.add(exited)
        signaled: list[int] = []
        monkeypatch.setattr(chrome_orphans, "terminate_pid", signaled.append)

        processes._terminate_for_signal()

        assert signaled == [running.pid]

    def test_a_pid_now_held_by_a_process_without_our_marker_is_never_signaled(
        self, armed: None, spawned: list[subprocess.Popen[Any]]
    ) -> None:
        """A Chrome known by pid exited and the kernel reused its pid: neither the
        signal handler nor ``end()`` may signal what holds it now."""
        unmarked = _sleeper(subprocess.Popen, "--remote-debugging-port=0")
        someone_elses = _sleeper(subprocess.Popen, "--remote-debugging-port=0", owner_switch(1))
        spawned += [unmarked, someone_elses]
        processes = SeleniumProcesses()
        processes.add_pid(unmarked.pid)
        processes.add_pid(someone_elses.pid)

        processes._terminate_for_signal()
        processes.end()

        assert _exited_within(unmarked, 1) is None
        assert _exited_within(someone_elses, 0.1) is None
        assert processes not in signals.live_browsers()

    def test_end_kills_a_process_that_ignores_sigterm(
        self, monkeypatch: pytest.MonkeyPatch, spawned: list[subprocess.Popen[Any]]
    ) -> None:
        stubborn = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('ready', flush=True); time.sleep(60)",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        spawned.append(stubborn)
        assert stubborn.stdout is not None
        assert stubborn.stdout.readline().strip() == "ready"
        monkeypatch.setattr(browser_launch, "_END_TERM_TIMEOUT_S", 0.2)
        processes = SeleniumProcesses()
        processes.add(stubborn)

        processes.end()

        assert stubborn.returncode == -signal.SIGKILL
        stubborn.stdout.close()

    def test_a_slow_ps_does_not_delay_the_held_processes_signal(
        self,
        armed: None,
        monkeypatch: pytest.MonkeyPatch,
        spawned: list[subprocess.Popen[Any]],
    ) -> None:
        """graftpunk spawned the held processes: they are signaled before any ``ps``."""
        held = _sleeper(subprocess.Popen)
        spawned.append(held)
        processes = SeleniumProcesses()
        processes.add(held)
        seen_during_ps: list[int | None] = []

        def slow_ps(ops: Any = None) -> list[Any]:
            seen_during_ps.append(_exited_within(held, 5))
            return []

        monkeypatch.setattr(chrome_orphans, "list_chrome_processes", slow_ps)

        processes._terminate_for_signal()

        assert seen_during_ps == [-signal.SIGTERM]

    def test_an_interrupted_ps_skips_only_the_processes_known_by_pid(
        self,
        armed: None,
        monkeypatch: pytest.MonkeyPatch,
        spawned: list[subprocess.Popen[Any]],
    ) -> None:
        """A Ctrl+C during the handler's ``ps`` must not cancel the held processes'
        SIGTERM, and must not leave the handler."""
        held = _sleeper(subprocess.Popen)
        by_pid = _sleeper(subprocess.Popen, "--remote-debugging-port=0", owner_switch())
        spawned += [held, by_pid]
        processes = SeleniumProcesses()
        processes.add(held)
        processes.add_pid(by_pid.pid)

        def interrupted_ps(ops: Any = None) -> list[Any]:
            raise KeyboardInterrupt

        monkeypatch.setattr(chrome_orphans, "list_chrome_processes", interrupted_ps)

        processes._terminate_for_signal()

        assert _exited_within(held, 10) == -signal.SIGTERM
        assert _exited_within(by_pid, 0.5) is None

    def test_a_reaped_chromedrivers_reused_pid_is_no_parent_of_ours(
        self,
        armed: None,
        monkeypatch: pytest.MonkeyPatch,
        spawned: list[subprocess.Popen[Any]],
    ) -> None:
        """A held chromedriver exited and was reaped, and the kernel gave its pid to
        a program whose browser now lists it as parent: that browser is not ours."""
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait(10)
        stranger = _sleeper(subprocess.Popen, "--remote-debugging-port=0")
        spawned += [gone, stranger]
        row = chrome_orphans.ChromeProcess(
            pid=stranger.pid,
            ppid=gone.pid,
            args=f"{sys.executable} --remote-debugging-port=0",
            owner_pid=None,
            user_data_dir=None,
        )
        monkeypatch.setattr(chrome_orphans, "list_chrome_processes", lambda ops=None: [row])
        processes = SeleniumProcesses()
        processes.add(gone)

        processes._terminate_for_signal()
        processes.end()

        assert _exited_within(stranger, 1) is None

    def test_a_non_positive_pid_is_never_signaled(self, armed: None) -> None:
        """``kill(0, ...)`` would reach this process's own group, a negative pid a whole group."""
        kills: list[int] = []
        ops = chrome_orphans.ProcessOps(kill=lambda pid, signum: kills.append(pid))

        assert chrome_orphans.terminate_pid(0, ops=ops) is False
        assert chrome_orphans.terminate_pid(-1, ops=ops) is False
        assert kills == []

    def test_releasing_a_quit_driver_leaves_the_registry(self) -> None:
        processes = SeleniumProcesses()
        processes.add_pid(2**22)
        driver = SimpleNamespace()
        attach_selenium_processes(driver, processes)
        assert processes in signals.live_browsers()

        release_selenium_processes(driver)
        release_selenium_processes(SimpleNamespace())  # a driver without any: a no-op

        assert processes not in signals.live_browsers()

    def test_the_outcome_check_warns_for_a_process_in_this_session(
        self, spawned: list[subprocess.Popen[Any]]
    ) -> None:
        with selenium_in_own_session():
            apart = _sleeper(undetected_chromedriver.subprocess.Popen)
        spawned.append(apart)

        with capture_logs() as logs:
            assert check_in_own_session([apart.pid]) is True
            assert check_in_own_session([apart.pid, os.getpid()]) is False

        [event] = [e for e in logs if e["event"] == "chrome_not_in_own_session"]
        assert event["log_level"] == "warning"
        assert event["pid"] == os.getpid()
        assert event["consequence"] == CHROME_SHARES_SESSION_CONSEQUENCE


class _FakeUcChrome:
    """undetected-chromedriver's ``Chrome`` as far as processes go.

    It starts "Chrome" and then "chromedriver" through the two ``subprocess``
    names the real one uses, keeps the pids where the real one keeps them, and
    its ``quit()`` ends both as the real one does (SIGKILL to chromedriver,
    SIGTERM to Chrome).
    """

    created: list[_FakeUcChrome] = []
    popen_chrome: Any = None

    def __init__(self, options: Any, user_data_dir: str, version_main: Any) -> None:
        popen = self.popen_chrome or undetected_chromedriver.subprocess.Popen
        self.options = options
        self.chrome = _sleeper(popen, *options.arguments)
        self.browser_pid = self.chrome.pid
        self.service = SimpleNamespace(
            process=_sleeper(self.popen_chrome or selenium_service.subprocess.Popen, "--port=0")
        )
        self.created.append(self)

    def quit(self) -> None:
        with suppress(OSError):
            self.service.process.kill()
        with suppress(OSError):
            os.kill(self.browser_pid, signal.SIGTERM)

    @property
    def processes(self) -> list[subprocess.Popen[Any]]:
        return [self.chrome, self.service.process]


@pytest.fixture()
def fake_uc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[type[_FakeUcChrome]]:
    """create_stealth_driver with the stand-in Chrome, no version probe and no stealth JS."""
    fake = type("FakeUcChrome", (_FakeUcChrome,), {"created": []})
    monkeypatch.setattr(undetected_chromedriver, "Chrome", fake)
    monkeypatch.setattr("graftpunk.stealth.get_chrome_version", lambda major: "156")
    monkeypatch.setattr("graftpunk.stealth._apply_stealth", lambda driver: None)
    monkeypatch.setattr("graftpunk.stealth.get_profile_dir", lambda: tmp_path / "profile")
    yield fake
    for driver in fake.created:
        for process in driver.processes:
            if process.poll() is None:
                process.kill()
                process.wait(5)


class TestTheStealthDriver:
    def test_with_the_handlers_both_processes_have_their_own_session_and_the_marker(
        self, handlers_installed: None, fake_uc: type[_FakeUcChrome]
    ) -> None:
        from graftpunk.stealth import create_stealth_driver

        with capture_logs() as logs:
            driver = create_stealth_driver(headless=True)

        chrome, chromedriver = driver.processes
        assert os.getsid(chrome.pid) == chrome.pid
        assert os.getsid(chromedriver.pid) == chromedriver.pid
        assert owner_switch() in driver.options.arguments
        assert chromedriver.args[-1] == owner_switch()
        processes = driver._graftpunk_processes
        assert processes.pids == [chrome.pid, chromedriver.pid]
        assert processes in signals.live_browsers()
        assert [e for e in logs if e["event"] == "chrome_not_in_own_session"] == []

    def test_in_gp_the_launch_arms_the_handlers_itself(
        self, armed: None, monkeypatch: pytest.MonkeyPatch, fake_uc: type[_FakeUcChrome]
    ) -> None:
        """``gp`` sets ``auto_install`` and nothing else: a selenium-only run has no
        other arming site, so without this launch's arming the handlers are never
        installed and Chrome and chromedriver stay in the terminal's group."""
        from graftpunk.stealth import create_stealth_driver

        monkeypatch.setattr(signals, "auto_install", True)
        monkeypatch.setattr(browser_launch, "prepare_browser_launch", CleanupReport)
        assert signals.termination_cleanup_installed() is False

        driver = create_stealth_driver(headless=True)

        assert signals.termination_cleanup_installed() is True
        chrome, chromedriver = driver.processes
        assert os.getsid(chrome.pid) == chrome.pid
        assert os.getsid(chromedriver.pid) == chromedriver.pid

    def test_with_the_handlers_the_orphan_sweep_runs_once_before_chrome_starts(
        self,
        handlers_installed: None,
        monkeypatch: pytest.MonkeyPatch,
        fake_uc: type[_FakeUcChrome],
    ) -> None:
        """For a user whose plugins are all selenium, this is the only sweep that
        ends a chromedriver and Chrome left by a Ctrl+\\ or a group SIGKILL."""
        from graftpunk.stealth import create_stealth_driver

        started_before_sweep: list[int] = []

        def sweep() -> CleanupReport:
            started_before_sweep.append(len(fake_uc.created))
            return CleanupReport()

        monkeypatch.setattr(browser_launch, "prepare_browser_launch", sweep)

        create_stealth_driver(headless=True)

        assert started_before_sweep == [0]
        assert len(fake_uc.created) == 1

    def test_without_the_handlers_it_launches_as_before(
        self, monkeypatch: pytest.MonkeyPatch, fake_uc: type[_FakeUcChrome]
    ) -> None:
        """A library program that never installed them keeps its signal behaviour."""
        from graftpunk.stealth import create_stealth_driver

        monkeypatch.setattr(signals, "termination_cleanup_installed", lambda: False)
        swept: list[str] = []
        monkeypatch.setattr(browser_launch, "prepare_browser_launch", lambda: swept.append("x"))

        driver = create_stealth_driver(headless=True)

        chrome, chromedriver = driver.processes
        assert os.getsid(chrome.pid) == os.getsid(0)
        assert os.getsid(chromedriver.pid) == os.getsid(0)
        assert owner_switch() not in driver.options.arguments
        assert chromedriver.args[-1] == "--port=0"
        assert not hasattr(driver, "_graftpunk_processes")
        assert swept == []

    def test_a_launch_that_bypassed_the_seam_is_logged_with_what_it_costs(
        self, handlers_installed: None, fake_uc: type[_FakeUcChrome]
    ) -> None:
        """The outcome is checked, not the seam: an undetected-chromedriver that
        no longer calls Popen through the rebound name warns, for each process."""
        from graftpunk.stealth import create_stealth_driver

        fake_uc.popen_chrome = subprocess.Popen
        with capture_logs() as logs:
            driver = create_stealth_driver(headless=True)

        events = [e for e in logs if e["event"] == "chrome_not_in_own_session"]
        assert sorted(e["pid"] for e in events) == sorted(p.pid for p in driver.processes)
        assert {e["log_level"] for e in events} == {"warning"}

    def test_a_start_interrupted_after_the_spawn_ends_both(
        self,
        handlers_installed: None,
        fake_uc: type[_FakeUcChrome],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from graftpunk.stealth import create_stealth_driver

        def interrupted(driver: Any) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr("graftpunk.stealth._apply_stealth", interrupted)

        with pytest.raises(KeyboardInterrupt):
            create_stealth_driver(headless=True)

        [driver] = fake_uc.created
        assert [_exited_within(p, 10) for p in driver.processes] == [-signal.SIGTERM] * 2
        assert driver._graftpunk_processes not in signals.live_browsers()


class TestAPlainSeleniumLogin:
    """The login engine's shape, observe off: ``with BrowserSession(backend="selenium")``.
    Chrome and chromedriver no longer get the terminal's Ctrl+C, so the
    session's exit has to end them, as it does on an exception and a normal exit."""

    @staticmethod
    def _assert_ended(driver: _FakeUcChrome) -> None:
        chrome, chromedriver = driver.processes
        assert _exited_within(chrome, 10) == -signal.SIGTERM
        assert _exited_within(chromedriver, 10) == -signal.SIGKILL
        assert driver._graftpunk_processes not in signals.live_browsers()

    def test_a_normal_exit_ends_both(
        self, handlers_installed: None, fake_uc: type[_FakeUcChrome]
    ) -> None:
        from graftpunk.session import BrowserSession

        with BrowserSession(backend="selenium", headless=True):
            pass

        self._assert_ended(fake_uc.created[0])

    def test_an_exception_ends_both(
        self, handlers_installed: None, fake_uc: type[_FakeUcChrome]
    ) -> None:
        from graftpunk.session import BrowserSession

        with pytest.raises(RuntimeError), BrowserSession(backend="selenium", headless=True):
            raise RuntimeError("a step failed")

        self._assert_ended(fake_uc.created[0])

    def test_a_ctrl_c_ends_both(
        self, handlers_installed: None, fake_uc: type[_FakeUcChrome]
    ) -> None:
        from graftpunk.session import BrowserSession

        if signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
            pytest.skip("needs Python's default SIGINT handler")

        sessions: list[bool] = []
        with pytest.raises(KeyboardInterrupt), BrowserSession(backend="selenium", headless=True):
            sessions += [os.getsid(p.pid) == p.pid for p in fake_uc.created[0].processes]
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(30)

        assert sessions == [True, True]
        self._assert_ended(fake_uc.created[0])


class _InterruptedCapture:
    """An observe capture whose error screenshot meets a second Ctrl+C."""

    def take_screenshot_sync(self) -> Any:
        raise KeyboardInterrupt

    def stop_capture(self) -> None:
        pass

    async def stop_capture_async(self) -> None:
        pass


class TestEveryWayOutOfASessionEndsTheBrowser:
    """A Ctrl+C no longer ends chromedriver and Chrome by itself, so each way a
    session can be left before its quit, or before its ``with`` owns it, has
    to end them."""

    def test_a_second_ctrl_c_during_the_observe_save(
        self, handlers_installed: None, fake_uc: type[_FakeUcChrome]
    ) -> None:
        from graftpunk.session import BrowserSession

        with (
            pytest.raises(KeyboardInterrupt),
            BrowserSession(backend="selenium", headless=True) as session,
        ):
            session._capture = _InterruptedCapture()
            raise KeyboardInterrupt  # the first Ctrl+C

        TestAPlainSeleniumLogin._assert_ended(fake_uc.created[0])

    def test_a_second_ctrl_c_during_the_async_observe_save(
        self, handlers_installed: None, fake_uc: type[_FakeUcChrome]
    ) -> None:
        import asyncio

        from graftpunk.session import BrowserSession

        async def started() -> None:
            pass

        async def login() -> None:
            session = BrowserSession(backend="selenium", headless=True)
            session.start_async = started  # type: ignore[method-assign]
            async with session:
                session._capture = _InterruptedCapture()
                raise KeyboardInterrupt  # the first Ctrl+C

        with pytest.raises(KeyboardInterrupt):
            asyncio.run(login())

        TestAPlainSeleniumLogin._assert_ended(fake_uc.created[0])

    def test_a_ctrl_c_while_the_observe_capture_starts(
        self,
        handlers_installed: None,
        monkeypatch: pytest.MonkeyPatch,
        fake_uc: type[_FakeUcChrome],
    ) -> None:
        """``__exit__`` never runs when ``__enter__`` raises."""
        from graftpunk.session import BrowserSession

        def interrupted(self: Any) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(BrowserSession, "_start_observe", interrupted)

        with pytest.raises(KeyboardInterrupt), BrowserSession(backend="selenium", headless=True):
            pass

        TestAPlainSeleniumLogin._assert_ended(fake_uc.created[0])

    def test_a_ctrl_c_while_the_async_observe_capture_starts(
        self,
        handlers_installed: None,
        monkeypatch: pytest.MonkeyPatch,
        fake_uc: type[_FakeUcChrome],
    ) -> None:
        import asyncio

        from graftpunk.session import BrowserSession

        async def started() -> None:
            pass

        async def interrupted(self: Any) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(BrowserSession, "_start_observe_async", interrupted)

        async def login() -> None:
            session = BrowserSession(backend="selenium", headless=True)
            session.start_async = started  # type: ignore[method-assign]
            async with session:
                pass

        with pytest.raises(KeyboardInterrupt):
            asyncio.run(login())

        TestAPlainSeleniumLogin._assert_ended(fake_uc.created[0])

    def test_a_requestium_session_that_fails_to_come_up(
        self,
        handlers_installed: None,
        monkeypatch: pytest.MonkeyPatch,
        fake_uc: type[_FakeUcChrome],
    ) -> None:
        """The driver is up before requestium's ``__init__`` runs."""
        import requestium

        from graftpunk.session import BrowserSession

        def interrupted(self: Any, *args: Any, **kwargs: Any) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(requestium.Session, "__init__", interrupted)

        with pytest.raises(KeyboardInterrupt):
            BrowserSession(backend="selenium", headless=True)

        TestAPlainSeleniumLogin._assert_ended(fake_uc.created[0])

    def test_a_ctrl_c_in_a_login_before_its_with_block(
        self,
        handlers_installed: None,
        monkeypatch: pytest.MonkeyPatch,
        fake_uc: type[_FakeUcChrome],
    ) -> None:
        """The login engine sets the session's name and redaction between the
        launch and the ``with``."""
        from graftpunk.plugins import login_engine
        from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep, SitePlugin

        class Plugin(SitePlugin):
            site_name = "probe"
            session_name = "probe"
            help_text = "t"
            base_url = "https://example.invalid"

        Plugin.login_config = LoginConfig(
            steps=[LoginStep(fields={"username": "#u"}, submit="#s")], url="/login"
        )

        def interrupted(credentials: Any) -> Any:
            raise KeyboardInterrupt

        monkeypatch.setattr(login_engine.Redaction, "from_credentials", interrupted)
        login = login_engine._generate_selenium_login(Plugin())

        with pytest.raises(KeyboardInterrupt):
            login({"username": "someone"}, headless=True)

        TestAPlainSeleniumLogin._assert_ended(fake_uc.created[0])


_FAKE_CHROMEDRIVER = """#!{python}
import subprocess, sys, time
marker = [arg for arg in sys.argv[1:] if arg.startswith("--graftpunk-owner-pid=")]
subprocess.Popen([sys.executable, "-c", {sleep!r}, "--remote-debugging-port=0", *marker])
time.sleep(60)
"""


@pytest.fixture()
def fake_chromedriver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict[str, Any]]:
    """The standard path with a stand-in chromedriver that spawns a stand-in Chrome.

    Yields what the fake ``webdriver.Chrome`` saw: ``service``, ``options`` and
    ``chrome``, the pid of the Chrome the fake chromedriver spawned. Every
    stand-in is killed at the end whatever happened.
    """
    chromedriver_path = tmp_path / "chromedriver"
    chromedriver_path.write_text(_FAKE_CHROMEDRIVER.format(python=sys.executable, sleep=_SLEEP))
    chromedriver_path.chmod(0o755)
    manager = SimpleNamespace(install=lambda: str(chromedriver_path))
    monkeypatch.setattr(
        "graftpunk.backends.selenium.webdriver_manager.chrome.ChromeDriverManager",
        lambda driver_version: manager,
    )
    monkeypatch.setattr("graftpunk.backends.selenium.get_chrome_version", lambda major: "156")
    monkeypatch.setattr(SeleniumService, "is_connectable", lambda self: True)
    seen: dict[str, Any] = {}
    yield seen
    with suppress(KeyError, OSError):
        os.kill(seen["chrome"], signal.SIGKILL)
    service = seen.get("service")
    if service is not None and service.process.poll() is None:
        service.process.kill()
        service.process.wait(5)


def _fake_webdriver(seen: dict[str, Any], *, interrupt: bool = False) -> type:
    """``webdriver.Chrome`` as far as processes go, raising KeyboardInterrupt when *interrupt*."""

    class FakeWebDriver:
        """Starts the service as ChromiumDriver does, and waits for its "Chrome"."""

        def __init__(self, service: Any, options: Any) -> None:
            service.start()
            seen["service"] = service
            seen["options"] = options
            deadline = time.monotonic() + 10
            while not (chromes := browser_launch.chromes_started_by(service.process.pid)):
                assert time.monotonic() < deadline, "the fake chromedriver never spawned"
                time.sleep(0.05)
            [seen["chrome"]] = chromes
            if interrupt:
                raise KeyboardInterrupt

        def quit(self) -> None:
            os.kill(seen["chrome"], signal.SIGTERM)
            seen["service"].process.terminate()
            seen["service"].process.wait(10)

    return FakeWebDriver


def _gone_within(pid: int, timeout_s: float) -> bool:
    """Whether *pid*, a process this test did not spawn, is gone within *timeout_s*."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


class TestTheStandardSeleniumPath:
    """``SeleniumBackend(use_stealth=False)``: chromedriver through Selenium's own
    ``popen_kw``, and its Chrome in chromedriver's session."""

    def test_chromedriver_and_its_chrome_are_out_of_this_session_and_registered(
        self,
        handlers_installed: None,
        monkeypatch: pytest.MonkeyPatch,
        fake_chromedriver: dict[str, Any],
    ) -> None:
        from graftpunk.backends.selenium import SeleniumBackend

        seen = fake_chromedriver
        monkeypatch.setattr("selenium.webdriver.Chrome", _fake_webdriver(seen))
        backend = SeleniumBackend(use_stealth=False, headless=True)
        with capture_logs() as logs:
            backend.start()
        chromedriver = seen["service"].process
        processes = backend._driver._graftpunk_processes
        chrome = seen["chrome"]

        assert os.getsid(chromedriver.pid) == chromedriver.pid
        assert os.getsid(chrome) == chromedriver.pid
        assert chromedriver.args[-1] == owner_switch()
        assert owner_switch() in seen["options"].arguments
        assert processes.pids == [chromedriver.pid, chrome]
        assert processes in signals.live_browsers()
        assert [e for e in logs if e["event"] == "chrome_not_in_own_session"] == []

        backend.stop()

        assert processes not in signals.live_browsers()

    def test_a_start_interrupted_after_chromedriver_spawned_chrome_ends_both(
        self,
        handlers_installed: None,
        monkeypatch: pytest.MonkeyPatch,
        fake_chromedriver: dict[str, Any],
    ) -> None:
        """Chrome is not yet held when ``webdriver.Chrome`` raises: only the lookup
        of chromedriver's children finds it, and a SIGTERM to chromedriver alone
        would leave it running in a session no Ctrl+C reaches."""
        from graftpunk.backends.selenium import SeleniumBackend

        seen = fake_chromedriver
        monkeypatch.setattr("selenium.webdriver.Chrome", _fake_webdriver(seen, interrupt=True))
        backend = SeleniumBackend(use_stealth=False, headless=True)

        with pytest.raises(KeyboardInterrupt):
            backend.start()

        assert _exited_within(seen["service"].process, 10) == -signal.SIGTERM
        assert _gone_within(seen["chrome"], 10)
        assert seen["service"]._graftpunk_processes not in signals.live_browsers()

    def test_a_signal_while_the_driver_starts_ends_the_chrome_chromedriver_spawned(
        self,
        handlers_installed: None,
        armed: None,
        monkeypatch: pytest.MonkeyPatch,
        fake_chromedriver: dict[str, Any],
    ) -> None:
        """Before ``webdriver.Chrome`` returns, Chrome's pid is not yet held, and a
        SIGTERM to chromedriver alone leaves its Chrome running: the handler
        looks up chromedriver's children itself."""
        from graftpunk.backends.selenium import SeleniumBackend

        seen = fake_chromedriver
        fake = _fake_webdriver(seen)

        class SignaledWhileStarting(fake):  # type: ignore[misc, valid-type]
            def __init__(self, service: Any, options: Any) -> None:
                super().__init__(service, options)
                service._graftpunk_processes._terminate_for_signal()

        monkeypatch.setattr("selenium.webdriver.Chrome", SignaledWhileStarting)
        backend = SeleniumBackend(use_stealth=False, headless=True)
        backend.start()

        assert _exited_within(seen["service"].process, 10) == -signal.SIGTERM
        assert _gone_within(seen["chrome"], 10)
        backend._driver._graftpunk_processes.release()

    def test_a_chromedriver_left_in_this_session_is_logged_with_what_it_costs(
        self,
        handlers_installed: None,
        monkeypatch: pytest.MonkeyPatch,
        fake_chromedriver: dict[str, Any],
    ) -> None:
        """The outcome is checked, not the seam: a Selenium whose ``popen_kw`` no
        longer reaches the spawn warns, for chromedriver and for its Chrome."""
        from graftpunk.backends import selenium as selenium_backend

        class SharingService(selenium_backend._RegisteringService):
            def __init__(self, path: str, processes: SeleniumProcesses) -> None:
                super().__init__(path, processes)
                self.popen_kw = {}

        seen = fake_chromedriver
        monkeypatch.setattr(selenium_backend, "_RegisteringService", SharingService)
        monkeypatch.setattr("selenium.webdriver.Chrome", _fake_webdriver(seen))
        backend = selenium_backend.SeleniumBackend(use_stealth=False, headless=True)
        with capture_logs() as logs:
            backend.start()

        events = [e for e in logs if e["event"] == "chrome_not_in_own_session"]
        assert sorted(e["pid"] for e in events) == sorted(
            [seen["service"].process.pid, seen["chrome"]]
        )
        assert {e["consequence"] for e in events} == {CHROME_SHARES_SESSION_CONSEQUENCE}

        backend.stop()


class TestTheReaper:
    def test_it_finds_and_ends_a_chromedriver_and_a_chrome_left_in_their_own_session(
        self, armed: None, tmp_path: Path
    ) -> None:
        """An owner that died without ending them: the marker names a finished pid."""
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait(10)
        marker = owner_switch(gone.pid)
        chromedriver_path = tmp_path / "Application Support" / "undetected_chromedriver"
        chromedriver_path.parent.mkdir()
        chromedriver_path.write_text("")
        # The reaper matches chromedriver by its executable, which ps shows as
        # argv[0]; a script would show its interpreter there. So the stand-in
        # is cat under chromedriver's argv[0], blocked opening a FIFO named
        # like chromedriver's first switch ("--" ends cat's options).
        os.mkfifo(tmp_path / "--port=0")
        (tmp_path / marker).write_text("")
        with selenium_in_own_session():
            chrome = _sleeper(
                undetected_chromedriver.subprocess.Popen, "--remote-debugging-port=0", marker
            )
            chromedriver = subprocess.Popen(  # noqa: S603 - cat, under a stand-in argv[0]
                [str(chromedriver_path), "--", "--port=0", marker],
                executable="/bin/cat",
                cwd=tmp_path,
                stdout=subprocess.DEVNULL,
                start_new_session=True,
            )
        ours = {str(chrome.pid), str(chromedriver.pid)}
        # The real table, cut to this test's two rows: a reap here must not
        # end some other program's orphan.
        ops = chrome_orphans.ProcessOps(
            read_table=lambda: "\n".join(
                row
                for row in chrome_orphans._read_ps_table().splitlines()
                if row.split()[:1] and row.split()[0] in ours
            )
        )
        try:
            listed = chrome_orphans.list_chrome_processes(ops)
            assert sorted(p.pid for p in listed) == sorted([chrome.pid, chromedriver.pid])

            reaped = chrome_orphans.reap_orphans(grace_seconds=10, poll_interval=0.1, ops=ops)

            assert sorted(p.pid for p in reaped) == sorted([chrome.pid, chromedriver.pid])
            assert _exited_within(chrome, 10) == -signal.SIGTERM
            assert _exited_within(chromedriver, 10) == -signal.SIGTERM
        finally:
            for process in (chrome, chromedriver):
                if process.poll() is None:
                    process.kill()
                    process.wait(5)
