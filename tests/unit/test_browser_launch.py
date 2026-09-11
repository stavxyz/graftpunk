"""The pre-launch composition root: the opt-out, the sweeps, and the arming (#96)."""

from __future__ import annotations

import signal
from pathlib import Path

import pytest

import graftpunk
from graftpunk import chrome_orphans, signals
from graftpunk.browser_launch import (
    CleanupReport,
    arm_termination_handlers,
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
    a file that starts a nodriver browser has to name both shared helpers.
    """

    @staticmethod
    def _launch_sites() -> dict[str, str]:
        package = Path(graftpunk.__file__).parent
        return {
            str(path.relative_to(package)): text
            for path in sorted(package.rglob("*.py"))
            for text in [path.read_text()]
            if "nodriver.start(" in text or "uc.start(" in text
        }

    def test_the_sites_are_the_three_this_suite_knows_about(self) -> None:
        assert set(self._launch_sites()) == {
            "backends/nodriver.py",
            "cli/main.py",
            "tokens.py",
        }

    def test_each_one_uses_the_shared_switches_and_the_shared_sweep(self) -> None:
        missing = {
            name: [
                helper
                for helper in ("base_browser_args", "prepare_browser_launch")
                if helper not in text
            ]
            for name, text in self._launch_sites().items()
        }

        assert {name: gaps for name, gaps in missing.items() if gaps} == {}
