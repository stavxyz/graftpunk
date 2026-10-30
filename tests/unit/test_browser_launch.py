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

import graftpunk
from graftpunk import chrome_orphans, signals
from graftpunk.browser_launch import (
    CleanupReport,
    arm_termination_handlers,
    browser_connect_failure_message,
    chrome_in_own_session,
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
        with chrome_in_own_session() as applied:
            sid, pid = await _session_of_a_child_launched_by_nodriver()

        assert applied is True
        assert sid == pid
        assert sid != os.getsid(0)

    @_POSIX_ONLY
    async def test_a_launch_after_it_shares_this_session(self) -> None:
        with chrome_in_own_session():
            pass

        sid, _ = await _session_of_a_child_launched_by_nodriver()

        assert nodriver_launcher.asyncio is asyncio
        assert sid == os.getsid(0)

    def test_it_puts_the_launcher_back_when_the_launch_raises(self) -> None:
        with pytest.raises(RuntimeError, match="launch failed"), chrome_in_own_session():
            raise RuntimeError("launch failed")

        assert nodriver_launcher.asyncio is asyncio

    def test_a_launcher_it_does_not_recognise_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stranger = object()
        monkeypatch.setattr(nodriver_launcher, "asyncio", stranger)

        with chrome_in_own_session() as applied:
            assert nodriver_launcher.asyncio is stranger

        assert applied is False
        assert nodriver_launcher.asyncio is stranger

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
