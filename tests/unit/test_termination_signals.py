"""SIGTERM and SIGHUP end the browsers this process started (#96)."""

from __future__ import annotations

import gc
import os
import signal
import subprocess
import sys
import time
import weakref
from pathlib import Path

import pytest

from graftpunk import signals
from graftpunk.signals import (
    TerminatableBrowser,
    _handle_termination,
    install_termination_cleanup,
    live_browsers,
    register_live_browser,
    unregister_live_browser,
)


class FakeBrowser:
    """A handle that records the one call the signal handler makes."""

    def __init__(self) -> None:
        self.terminated = 0

    def _terminate_for_signal(self) -> None:
        self.terminated += 1


class TestLiveBrowserRegistry:
    def test_a_registered_handle_is_listed(self) -> None:
        handle = FakeBrowser()

        register_live_browser(handle)

        assert handle in live_browsers()

    def test_unregistering_removes_it(self) -> None:
        handle = FakeBrowser()
        register_live_browser(handle)

        unregister_live_browser(handle)

        assert handle not in live_browsers()

    def test_unregistering_something_never_registered_is_a_no_op(self) -> None:
        unregister_live_browser(FakeBrowser())

    def test_the_registry_holds_only_weak_references(self) -> None:
        """A forgotten browser must not be kept alive by the registry."""
        handle = FakeBrowser()
        register_live_browser(handle)
        ref = weakref.ref(handle)

        del handle
        gc.collect()

        assert ref() is None

    def test_live_browsers_is_a_snapshot(self) -> None:
        """The handler unregisters as it goes, so it must not iterate the live set."""
        handle = FakeBrowser()
        register_live_browser(handle)

        listed = live_browsers()
        unregister_live_browser(handle)

        assert handle in listed


class TestTerminatableBrowserProtocol:
    def test_a_handle_with_the_method_satisfies_it(self) -> None:
        assert isinstance(FakeBrowser(), TerminatableBrowser)

    def test_an_object_without_the_method_does_not(self) -> None:
        assert not isinstance(object(), TerminatableBrowser)


class TestInstallTerminationCleanup:
    """Which signal slots graftpunk is allowed to take."""

    @pytest.fixture(autouse=True)
    def _default_slots(self):  # noqa: ANN202
        """Start from SIG_DFL in both slots, whatever the ambient state is."""
        saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
        for sig in saved:
            signal.signal(sig, signal.SIG_DFL)
        yield
        for sig, handler in saved.items():
            if handler is not None:
                signal.signal(sig, handler)

    def test_a_default_slot_gets_the_handler(self) -> None:
        install_termination_cleanup()

        assert signal.getsignal(signal.SIGTERM) is _handle_termination
        assert signal.getsignal(signal.SIGHUP) is _handle_termination

    def test_a_slot_the_host_already_handles_is_left_alone(self) -> None:
        """Importing graftpunk into a host that manages its own signals changes nothing."""
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

        install_termination_cleanup()

        assert signal.getsignal(signal.SIGTERM) is signal.SIG_IGN
        assert signal.getsignal(signal.SIGHUP) is _handle_termination

    def test_installing_twice_keeps_one_handler(self) -> None:
        install_termination_cleanup()
        install_termination_cleanup()

        assert signal.getsignal(signal.SIGTERM) is _handle_termination


def test_importing_graftpunk_arms_nothing() -> None:
    """The flag is opt-in: a library consumer's signal slots stay theirs.

    The unit suite's autouse ``_restore_termination_signals`` fixture puts
    ``auto_install`` back to False after every test, so this asserts the
    module's own default rather than whatever the last CLI invocation left.
    """
    assert signals.auto_install is False


CHILD_SCRIPT = '''
import sys
import time

from graftpunk.signals import install_termination_cleanup, register_live_browser

marker = sys.argv[1]
ready = sys.argv[2]


class FakeBrowser:
    """Stands in for a live browser: it records that it was asked to stop."""

    def _terminate_for_signal(self):
        with open(marker, "w") as handle:
            handle.write("terminated")


browser = FakeBrowser()
register_live_browser(browser)
install_termination_cleanup()
with open(ready, "w") as handle:
    handle.write("ready")
time.sleep(30)
'''


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX only: no SIGTERM disposition to restore")
def test_sigterm_ends_the_browsers_then_kills_the_process(tmp_path: Path) -> None:
    """End to end, in a real process: the cleanup runs and the exit status is 128 + 15."""
    script = tmp_path / "child.py"
    script.write_text(CHILD_SCRIPT)
    marker = tmp_path / "terminated"
    ready = tmp_path / "ready"

    child = subprocess.Popen(  # noqa: S603
        [sys.executable, str(script), str(marker), str(ready)]
    )
    try:
        deadline = time.monotonic() + 30.0
        while not ready.exists() and time.monotonic() < deadline:
            if child.poll() is not None:
                pytest.fail(f"the child exited early with {child.returncode}")
            time.sleep(0.05)
        assert ready.exists(), "the child never installed its handler"

        os.kill(child.pid, signal.SIGTERM)
        returncode = child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)

    # Popen reports a signal death as -signum; a shell would report 128 + 15.
    assert returncode == -signal.SIGTERM
    assert marker.read_text() == "terminated"
