"""The pre-launch composition root: the opt-out, the sweeps, and the arming (#96)."""

from __future__ import annotations

import signal
from pathlib import Path

import pytest

from graftpunk import chrome_orphans, signals
from graftpunk.browser_launch import CleanupReport, prepare_browser_launch
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
        self, armed: None, no_sweeps: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(signals, "auto_install", True)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

        prepare_browser_launch()

        assert signal.getsignal(signal.SIGTERM) is signals._handle_termination

    def test_it_arms_nothing_when_the_flag_is_unset(
        self, armed: None, no_sweeps: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(signals, "auto_install", False)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

        prepare_browser_launch()

        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL

    def test_a_disarmed_process_arms_nothing_either(
        self, no_sweeps: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One switch covers signals, processes and directories alike.

        The unit suite runs disarmed, so a test that drives a launch must not
        acquire this worker's signal slots even with the CLI flag set.
        """
        monkeypatch.setattr(signals, "auto_install", True)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

        prepare_browser_launch()

        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
