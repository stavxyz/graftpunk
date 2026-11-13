"""No unit test can signal the test run, its parent or its process group (the
``_never_signal_the_suite`` fixture in ``tests/unit/conftest.py``)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys

import pytest

from tests.unit.conftest import signals_the_suite

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")


@pytest.mark.parametrize(
    "target",
    [
        pytest.param(lambda: os.getpid(), id="this process"),
        pytest.param(lambda: os.getppid(), id="the parent"),
        pytest.param(lambda: 0, id="the caller's group"),
        pytest.param(lambda: -1, id="every process"),
        pytest.param(lambda: -os.getpgid(0), id="this group by negative pid"),
    ],
)
def test_kill_refuses_the_test_run(target) -> None:  # noqa: ANN001
    assert signals_the_suite(target(), group=False, sig=signal.SIGTERM) is not None


@pytest.mark.parametrize(
    "target",
    [
        pytest.param(lambda: 0, id="the caller's group"),
        pytest.param(lambda: os.getpgid(0), id="this group"),
    ],
)
def test_killpg_refuses_the_test_run(target) -> None:  # noqa: ANN001
    assert signals_the_suite(target(), group=True, sig=signal.SIGHUP) is not None


def test_a_liveness_probe_of_the_test_run_is_allowed() -> None:
    assert signals_the_suite(os.getpid(), group=False, sig=0) is None
    assert signals_the_suite(os.getpgid(0), group=True, sig=0) is None


def test_a_child_in_a_session_of_its_own_can_be_signalled() -> None:
    child = subprocess.Popen(  # noqa: S603 - a fixed command run by this interpreter
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    try:
        assert signals_the_suite(child.pid, group=False, sig=signal.SIGTERM) is None
        assert signals_the_suite(child.pid, group=True, sig=signal.SIGTERM) is None
        os.killpg(child.pid, signal.SIGTERM)
        assert child.wait(timeout=30) == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)


def test_os_kill_of_this_process_fails_the_test_instead_of_sending() -> None:
    """The live check uses a signal this test handles, so a guard that let it
    through would be seen here rather than ending the worker."""
    received: list[int] = []
    previous = signal.signal(signal.SIGUSR1, lambda signum, _frame: received.append(signum))
    try:
        with pytest.raises(pytest.fail.Exception, match="the test run itself"):
            os.kill(os.getpid(), signal.SIGUSR1)
    finally:
        signal.signal(signal.SIGUSR1, previous)
    assert received == []


def _with_sigusr1_recorded(send) -> list[int]:  # noqa: ANN001
    """Run *send* with a SIGUSR1 handler that records instead of ending the worker,
    and return what it recorded."""
    received: list[int] = []
    previous = signal.signal(signal.SIGUSR1, lambda signum, _frame: received.append(signum))
    try:
        send()
    finally:
        signal.signal(signal.SIGUSR1, previous)
    return received


def test_raise_signal_fails_the_test_instead_of_raising() -> None:
    def send() -> None:
        with pytest.raises(pytest.fail.Exception, match="signals_itself"):
            signal.raise_signal(signal.SIGUSR1)

    assert _with_sigusr1_recorded(send) == []


def test_pthread_kill_fails_the_test_instead_of_sending() -> None:
    import threading

    def send() -> None:
        with pytest.raises(pytest.fail.Exception, match="signals_itself"):
            signal.pthread_kill(threading.get_ident(), signal.SIGUSR1)

    assert _with_sigusr1_recorded(send) == []


@pytest.mark.signals_itself
def test_a_test_marked_signals_itself_can_raise_a_signal_on_itself() -> None:
    assert _with_sigusr1_recorded(lambda: signal.raise_signal(signal.SIGUSR1)) == [signal.SIGUSR1]


@pytest.mark.signals_itself
def test_the_mark_does_not_open_os_kill() -> None:
    def send() -> None:
        with pytest.raises(pytest.fail.Exception, match="the test run itself"):
            os.kill(os.getpid(), signal.SIGUSR1)

    assert _with_sigusr1_recorded(send) == []


def test_the_orphan_reaper_kills_through_the_guarded_os_kill() -> None:
    """chrome_orphans' default operations look os.kill up when they send, so the
    guard covers the reaper too (review 2, N2)."""
    from graftpunk.chrome_orphans import DEFAULT_OPS

    def send() -> None:
        with pytest.raises(pytest.fail.Exception, match="the test run itself"):
            DEFAULT_OPS.kill(os.getpid(), signal.SIGUSR1)

    assert _with_sigusr1_recorded(send) == []
