"""The pre-launch composition root: the opt-out, the sweeps, and the arming (#96)."""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import signal
import sys
from pathlib import Path

import nodriver.core.browser as nodriver_launcher
import pytest
from structlog.testing import capture_logs

import graftpunk
from graftpunk import browser_launch, chrome_orphans, signals
from graftpunk.browser_launch import (
    CHROME_SHARES_SESSION_CONSEQUENCE,
    CleanupReport,
    arm_termination_handlers,
    browser_connect_failure_message,
    check_chrome_in_own_session,
    chrome_in_own_session,
    chrome_leads_own_session,
    prepare_browser_launch,
)
from graftpunk.chrome_orphans import ChromeProcess
from graftpunk.config import reset_settings


@pytest.fixture()
def armed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-arm the orphan module, which the unit suite's autouse fixture disarms."""
    monkeypatch.setattr(chrome_orphans, "_ARMED", True)


@pytest.fixture()
def no_sweeps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both sweeps do nothing, so a test can look at everything else."""
    monkeypatch.setattr(chrome_orphans, "reap_orphans", list)
    monkeypatch.setattr(chrome_orphans, "remove_stale_temp_profiles", list)


def _orphan(pid: int = 4242) -> ChromeProcess:
    return ChromeProcess(pid=pid, ppid=1, args="", owner_pid=999999, user_data_dir=None)


class TestTheOptOut:
    def test_it_skips_both_sweeps(self, armed: None, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        monkeypatch.setenv("GRAFTPUNK_KEEP_ORPHANED_CHROME", "1")
        reset_settings()
        monkeypatch.setattr(chrome_orphans, "reap_orphans", lambda: calls.append("reap"))
        monkeypatch.setattr(
            chrome_orphans, "remove_stale_temp_profiles", lambda: calls.append("sweep")
        )

        report = prepare_browser_launch()

        assert calls == []
        assert not report

    def test_unreadable_settings_are_not_a_launch_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom() -> None:
            raise OSError("the config directory vanished")

        monkeypatch.setattr("graftpunk.browser_launch.get_settings", boom)

        assert not prepare_browser_launch()


class TestTheSweeps:
    def test_a_failing_reap_does_not_stop_the_profile_sweep(
        self, armed: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Separate try blocks: one broken sweep must not cost the other."""

        def boom() -> list:
            raise OSError("ps went missing")

        monkeypatch.setattr(chrome_orphans, "reap_orphans", boom)
        monkeypatch.setattr(
            chrome_orphans, "remove_stale_temp_profiles", lambda: [tmp_path / "uc_gone"]
        )

        report = prepare_browser_launch()

        assert report.reaped == []
        assert report.profiles_removed == [tmp_path / "uc_gone"]
        assert report

    def test_a_failing_sweep_keeps_what_the_reap_found(
        self, armed: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom() -> list:
            raise OSError("the temp dir is unreadable")

        monkeypatch.setattr(chrome_orphans, "reap_orphans", lambda: [_orphan()])
        monkeypatch.setattr(chrome_orphans, "remove_stale_temp_profiles", boom)

        report = prepare_browser_launch()

        assert [p.pid for p in report.reaped] == [4242]
        assert report.profiles_removed == []

    def test_an_empty_pass_is_falsey(self, armed: None, no_sweeps: None) -> None:
        assert not prepare_browser_launch()

    def test_a_pass_that_did_something_is_truthy(
        self, armed: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(chrome_orphans, "reap_orphans", lambda: [_orphan()])
        monkeypatch.setattr(
            chrome_orphans, "remove_stale_temp_profiles", lambda: [tmp_path / "uc_gone"]
        )

        report = prepare_browser_launch()

        assert report == CleanupReport(reaped=[_orphan()], profiles_removed=[tmp_path / "uc_gone"])
        assert report


class TestArmingTheHandlers:
    """Handlers are armed at the first launch, not at import and not per command."""

    def test_it_arms_them_when_the_cli_asked(
        self, armed: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(signals, "auto_install", True)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

        arm_termination_handlers()

        assert signal.getsignal(signal.SIGTERM) is signals._handle_termination

    def test_it_arms_nothing_when_the_flag_is_unset(
        self, armed: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(signals, "auto_install", False)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

        arm_termination_handlers()

        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL

    def test_a_disarmed_process_arms_nothing_either(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One switch covers signals, processes and directories alike.

        The unit suite runs disarmed, so a test that drives a launch must not
        acquire this worker's signal slots even with the CLI flag set.
        """
        monkeypatch.setattr(signals, "auto_install", True)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

        arm_termination_handlers()

        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL

    def test_the_sweeps_do_not_arm_anything(
        self, armed: None, no_sweeps: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cleanup pass runs on a worker thread, where signal.signal raises.

        Arming from in there would be swallowed at debug and the handlers
        would never be installed, so the sweeps must not try.
        """
        monkeypatch.setattr(signals, "auto_install", True)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

        prepare_browser_launch()

        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


class TestEveryLaunchSiteIsWiredUp:
    """A launch site that skips the hygiene is a bug the file itself can show.

    The third site went a whole change without the marker, the sweep or the
    profile removal because nothing counted the sites (#96). This counts them:
    a file that starts a nodriver browser has to name both shared helpers, and
    the shared connect-failure message too.
    """

    @staticmethod
    def _launch_sites() -> dict[str, str]:
        package = Path(graftpunk.__file__).parent
        return {
            str(path.relative_to(package)): text
            for path in sorted(package.rglob("*.py"))
            for text in [path.read_text()]
            if "nodriver.start(" in text
            or "uc.start(" in text
            or re.search(r"^from nodriver(?:\.\w+)* import .*\bstart\b", text, re.MULTILINE)
        }

    def test_the_sites_are_the_three_this_suite_knows_about(self) -> None:
        assert set(self._launch_sites()) == {
            "backends/nodriver.py",
            "cli/observe_browser.py",
            "tokens.py",
        }

    def test_each_one_uses_the_shared_switches_the_shared_sweep_and_the_shared_message(
        self,
    ) -> None:
        missing = {
            name: [
                helper
                for helper in (
                    "base_browser_args",
                    "prepare_browser_launch",
                    "browser_connect_failure_message",
                )
                if helper not in text
            ]
            for name, text in self._launch_sites().items()
        }

        assert {name: gaps for name, gaps in missing.items() if gaps} == {}


class TestTheConnectFailureMessage:
    def test_it_names_the_attempt_count_and_the_remedy(self) -> None:
        assert browser_connect_failure_message(3) == (
            "Failed to connect to browser after 3 attempts. Chrome may be "
            "slow to start; try again, or check for stale Chrome processes."
        )


_POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="sessions are a POSIX notion")

# A child that prints its session id and its own pid, then exits.
_PRINT_SID = "import os; print(os.getsid(0), os.getpid())"


async def _session_of_a_child_launched_by_nodriver() -> tuple[int, int]:
    """Launch a child the way nodriver launches Chrome, and return (its sid, its pid).

    The call is nodriver's own: ``Browser.start`` calls
    ``asyncio.create_subprocess_exec`` through the ``asyncio`` name bound in
    ``nodriver.core.browser``, and so does this.
    """
    proc = await nodriver_launcher.asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _PRINT_SID,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        close_fds=True,
    )
    out, _ = await proc.communicate()
    sid, pid = (int(field) for field in out.split())
    assert pid == proc.pid
    return sid, pid


class TestChromeInItsOwnSession:
    """A terminal's Ctrl+C reaches only Python, not the Chrome gp observe drives (#333)."""

    def test_nodriver_still_launches_chrome_the_way_the_seam_expects(self) -> None:
        # The seam swaps the asyncio that nodriver's launcher module looks up. If
        # nodriver stops calling asyncio.create_subprocess_exec there, or starts
        # passing start_new_session itself, this is the test that says so.
        source = inspect.getsource(nodriver_launcher.Browser.start)

        assert nodriver_launcher.asyncio is asyncio
        assert "asyncio.create_subprocess_exec(" in source
        assert "start_new_session" not in source

    @_POSIX_ONLY
    async def test_a_launch_inside_it_starts_a_new_session(self) -> None:
        with chrome_in_own_session() as launch:
            sid, pid = await _session_of_a_child_launched_by_nodriver()

        assert launch.applied is True
        assert [p.pid for p in launch.processes] == [pid]
        assert sid == pid
        assert sid != os.getsid(0)
        # Reset, so a later launch in this context is not recorded on a
        # released block.
        assert browser_launch._CURRENT_LAUNCH.get() is None

    def test_a_name_something_else_bound_meanwhile_is_not_overwritten(self) -> None:
        sentinel = object()
        try:
            with chrome_in_own_session():
                nodriver_launcher.asyncio = sentinel  # type: ignore[assignment]

            assert nodriver_launcher.asyncio is sentinel
        finally:
            nodriver_launcher.asyncio = asyncio
            browser_launch._proxy_users = 0

    @_POSIX_ONLY
    async def test_a_launch_after_it_shares_this_session(self) -> None:
        with chrome_in_own_session():
            pass

        sid, _ = await _session_of_a_child_launched_by_nodriver()

        assert nodriver_launcher.asyncio is asyncio
        assert sid == os.getsid(0)

    @_POSIX_ONLY
    async def test_a_launch_outside_nodrivers_launcher_is_untouched_while_it_is_open(
        self,
    ) -> None:
        with chrome_in_own_session() as launch:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", _PRINT_SID, stdout=asyncio.subprocess.PIPE
            )
            out, _ = await proc.communicate()

        sid, _ = (int(field) for field in out.split())
        assert sid == os.getsid(0)
        assert launch.processes == []

    @_POSIX_ONLY
    async def test_overlapping_blocks_both_launch_in_a_new_session_and_restore_once(
        self,
    ) -> None:
        """A opens, B opens, A closes, then B launches: B's launch still gets a new session."""
        b_open, a_closed = asyncio.Event(), asyncio.Event()
        launched: dict[str, tuple[int, int]] = {}

        async def first() -> None:
            with chrome_in_own_session():
                launched["a"] = await _session_of_a_child_launched_by_nodriver()
                await b_open.wait()
            a_closed.set()

        async def second() -> None:
            with chrome_in_own_session():
                b_open.set()
                await a_closed.wait()
                assert nodriver_launcher.asyncio is not asyncio
                launched["b"] = await _session_of_a_child_launched_by_nodriver()

        with capture_logs() as logs:
            await asyncio.gather(first(), second())

        assert all(sid == pid for sid, pid in launched.values())
        assert set(launched) == {"a", "b"}
        assert nodriver_launcher.asyncio is asyncio
        assert [e for e in logs if e["event"] == "chrome_own_session_unavailable"] == []

    def test_it_puts_the_launcher_back_when_the_launch_raises(self) -> None:
        with pytest.raises(RuntimeError, match="launch failed"), chrome_in_own_session():
            raise RuntimeError("launch failed")

        assert nodriver_launcher.asyncio is asyncio

    def test_a_launcher_it_does_not_recognise_is_left_alone_with_a_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stranger = object()
        monkeypatch.setattr(nodriver_launcher, "asyncio", stranger)

        with capture_logs() as logs, chrome_in_own_session() as launch:
            assert nodriver_launcher.asyncio is stranger

        assert launch.applied is False
        assert nodriver_launcher.asyncio is stranger
        [event] = [e for e in logs if e["event"] == "chrome_own_session_unavailable"]
        assert event["reason"] == "launcher_changed"
        # A block that did not apply leaves the count of proxy users alone.
        assert browser_launch._proxy_users == 0
        assert event["log_level"] == "warning"
        assert "Ctrl+C" in event["consequence"]

    def test_a_launcher_that_cannot_be_imported_is_a_warning_not_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_module(name: str) -> object:
            raise ImportError(f"No module named {name!r}")

        monkeypatch.setattr("graftpunk.browser_launch.importlib.import_module", no_module)
        ran = False

        with capture_logs() as logs, chrome_in_own_session() as launch:
            ran = True

        assert ran is True
        assert launch.applied is False
        assert nodriver_launcher.asyncio is asyncio
        [event] = [e for e in logs if e["event"] == "chrome_own_session_unavailable"]
        assert event["reason"] == "import"
        assert browser_launch._proxy_users == 0
        assert event["log_level"] == "warning"

    @_POSIX_ONLY
    async def test_a_chrome_is_registered_with_the_signal_handlers_once_spawned(
        self, armed: None
    ) -> None:
        """A SIGTERM or SIGHUP before nodriver.start returns still ends that Chrome."""
        with chrome_in_own_session() as launch:
            proc = await nodriver_launcher.asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(30)"
            )
            try:
                registered = [
                    h for h in signals.live_browsers() if getattr(h, "_process", None) is proc
                ]
                assert len(registered) == 1
                registered[0]._terminate_for_signal()
                assert await asyncio.wait_for(proc.wait(), timeout=10) == -signal.SIGTERM
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()

        assert launch.processes == [proc]
        assert not [h for h in signals.live_browsers() if getattr(h, "_process", None) is proc]

    @_POSIX_ONLY
    async def test_end_launched_ends_a_failed_attempts_chrome_and_its_registry_entry(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from nodriver.core import util as nodriver_util

        registry: set[object] = set()
        monkeypatch.setattr(nodriver_util, "get_registered_instances", lambda: registry)
        removed: list[object] = []
        monkeypatch.setattr(chrome_orphans, "remove_browser_temp_profile", removed.append)

        with chrome_in_own_session() as launch:
            proc = await nodriver_launcher.asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(30)"
            )
            browser = type("Browser", (), {"_process": proc})()
            registry.add(browser)
            await launch.end_launched()

        assert proc.returncode == -signal.SIGTERM
        assert registry == set()
        assert removed == [browser]

    @_POSIX_ONLY
    async def test_end_launched_keeps_the_profile_of_a_chrome_still_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Removing the profile under a live Chrome leaves it half deleted."""
        from nodriver.core import util as nodriver_util

        registry: set[object] = set()
        monkeypatch.setattr(nodriver_util, "get_registered_instances", lambda: registry)
        removed: list[object] = []
        monkeypatch.setattr(chrome_orphans, "remove_browser_temp_profile", removed.append)

        async def does_not_end(process: object) -> None:
            return None

        monkeypatch.setattr(browser_launch, "_end_process", does_not_end)

        with chrome_in_own_session() as launch:
            proc = await nodriver_launcher.asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(30)"
            )
            try:
                registry.add(type("Browser", (), {"_process": proc})())
                await launch.end_launched()
                assert proc.returncode is None
            finally:
                proc.kill()
                await proc.wait()

        assert registry == set()
        assert removed == []

    @_POSIX_ONLY
    async def test_end_launched_escalates_to_sigkill_for_a_chrome_ignoring_sigterm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(browser_launch, "_END_TERM_TIMEOUT_S", 0.2)
        stubborn = (
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('ready', flush=True)\n"
            "time.sleep(30)\n"
        )
        with chrome_in_own_session() as launch:
            proc = await nodriver_launcher.asyncio.create_subprocess_exec(
                sys.executable, "-c", stubborn, stdout=asyncio.subprocess.PIPE
            )
            try:
                assert proc.stdout is not None
                assert (await proc.stdout.readline()).strip() == b"ready"
                await launch.end_launched()
                # Read before the cleanup below, which would SIGKILL it too.
                ended_with = proc.returncode
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()

        assert ended_with == -signal.SIGKILL

    @_POSIX_ONLY
    async def test_end_launched_does_not_wait_for_helpers_holding_chromes_pipes(
        self,
    ) -> None:
        """A stand-in exits on SIGTERM at once while a helper it spawned holds its
        stdout for 3 s; ``Process.wait()`` would wait for that pipe too."""
        import time

        chrome = (
            "import signal, subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'])\n"
            "signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
            "print('ready', flush=True)\n"
            "time.sleep(30)\n"
        )
        with chrome_in_own_session() as launch:
            proc = await nodriver_launcher.asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                chrome,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                assert proc.stdout is not None
                assert (await proc.stdout.readline()).strip() == b"ready"
                began = time.monotonic()
                await launch.end_launched()
                took = time.monotonic() - began
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()

        assert proc.returncode == 0
        assert took < 1.5

    @_POSIX_ONLY
    async def test_the_outcome_check_says_nothing_when_getsid_fails(self) -> None:
        """A pid that has exited (ESRCH) cannot be told; it is not a shared session."""
        proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
        await proc.wait()
        browser = type("Browser", (), {"_process": proc})()

        with capture_logs() as logs:
            assert chrome_leads_own_session(browser) is None
            assert check_chrome_in_own_session(browser) is True

        assert logs == []

    @_POSIX_ONLY
    async def test_the_outcome_check_warns_when_chrome_shares_this_session(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(30)"
        )
        try:
            browser = type("Browser", (), {"_process": proc})()
            with capture_logs() as logs:
                assert chrome_leads_own_session(browser) is False
                assert check_chrome_in_own_session(browser) is False
        finally:
            proc.kill()
            await proc.wait()

        [event] = [e for e in logs if e["event"] == "chrome_not_in_own_session"]
        assert event["log_level"] == "warning"
        assert event["consequence"] == CHROME_SHARES_SESSION_CONSEQUENCE

    @_POSIX_ONLY
    async def test_the_outcome_check_passes_a_chrome_in_its_own_session(self) -> None:
        with chrome_in_own_session():
            proc = await nodriver_launcher.asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(30)"
            )
        try:
            browser = type("Browser", (), {"_process": proc})()
            with capture_logs() as logs:
                assert check_chrome_in_own_session(browser) is True
        finally:
            proc.kill()
            await proc.wait()

        assert logs == []

    def test_the_outcome_check_says_nothing_without_a_pid(self) -> None:
        browser = type("Browser", (), {"_process": None})()

        with capture_logs() as logs:
            assert chrome_leads_own_session(browser) is None
            assert check_chrome_in_own_session(browser) is True

        assert logs == []

    @_POSIX_ONLY
    async def test_the_reaper_still_finds_and_ends_a_browser_in_its_own_session(
        self, armed: None
    ) -> None:
        # An owner that has exited: the pid of a child that already finished.
        gone = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
        await gone.wait()
        args = ["--remote-debugging-port=0", chrome_orphans.owner_switch(gone.pid)]
        with chrome_in_own_session():
            proc = await nodriver_launcher.asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(30)", *args
            )
        try:
            assert os.getsid(proc.pid) == proc.pid
            found = [p for p in chrome_orphans.list_chrome_processes() if p.pid == proc.pid]
            assert [p.owner_pid for p in chrome_orphans.find_orphans(found)] == [gone.pid]

            browser = type("Browser", (), {"_process": proc})()
            assert chrome_orphans.terminate_nodriver_browser(browser) is True
            assert await asyncio.wait_for(proc.wait(), timeout=10) == -signal.SIGTERM
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
