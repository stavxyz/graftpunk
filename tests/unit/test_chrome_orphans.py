"""The orphaned-Chrome reaper: identification, signalling, and profile cleanup (#96)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog
from structlog.testing import capture_logs

from graftpunk import chrome_orphans
from graftpunk.chrome_orphans import (
    OWNER_SWITCH,
    ChromeProcess,
    ProcessOps,
    _pid_alive,
    base_browser_args,
    browser_temp_profile,
    find_orphans,
    list_chrome_processes,
    owner_switch,
    reap_orphans,
    remove_browser_temp_profile,
    remove_stale_temp_profiles,
    terminate_nodriver_browser,
)

# A stand-in for Chrome's argv0. The space is deliberate: macOS ships Chrome
# under a path that has one, and the parser must not split the args column on
# it.
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _row(pid: int, ppid: int, args: str, *, pad: bool = True) -> str:
    """One ``ps -eo pid=,ppid=,args=`` line. macOS and Linux right-align the numbers."""
    if pad:
        return f"{pid:>7} {ppid:>7} {args}"
    return f"{pid} {ppid} {args}"


def _chrome_args(*, port: int = 9222, owner: int | None = None, profile: str | None = None) -> str:
    """A Chrome command line as nodriver builds it."""
    args = f"{CHROME} --test-type --remote-debugging-port={port}"
    if owner is not None:
        args += f" {OWNER_SWITCH}={owner}"
    if profile is not None:
        args += f" --user-data-dir={profile}"
    return args


def _proc(
    pid: int, ppid: int, *, owner: int | None = None, profile: str | None = None
) -> ChromeProcess:
    return ChromeProcess(
        pid=pid,
        ppid=ppid,
        args=_chrome_args(owner=owner, profile=profile),
        owner_pid=owner,
        user_data_dir=profile,
    )


class FakeKernel:
    """A fake process table, signal delivery and clock, offered as one ProcessOps.

    A signal that would end a process removes its row, so a test asserts on the
    table that is left rather than on which mock was called. The clock only
    moves when the code under test sleeps, so a three second grace period costs
    the test nothing.
    """

    def __init__(
        self,
        rows: dict[int, str],
        *,
        alive_pids: frozenset[int] = frozenset(),
        ignores_sigterm: frozenset[int] = frozenset(),
        kill_raises: dict[int, OSError] | None = None,
    ) -> None:
        self.rows = dict(rows)
        self.alive_pids = alive_pids
        self.ignores_sigterm = ignores_sigterm
        self.kill_raises = kill_raises or {}
        self.signals: list[tuple[int, int]] = []
        self.reads = 0
        self.now = 0.0

    @property
    def ops(self) -> ProcessOps:
        return ProcessOps(
            read_table=self.read_table,
            kill=self.kill,
            pid_alive=self.pid_alive,
            sleep=self.sleep,
            monotonic=self.monotonic,
        )

    def read_table(self) -> str:
        self.reads += 1
        return "\n".join(self.rows.values())

    def pid_alive(self, pid: int) -> bool:
        return pid in self.alive_pids

    def kill(self, pid: int, signum: int) -> None:
        self.signals.append((pid, signum))
        exc = self.kill_raises.get(pid)
        if exc is not None:
            raise exc
        ends_it = signum == signal.SIGKILL or (
            signum == signal.SIGTERM and pid not in self.ignores_sigterm
        )
        if ends_it:
            self.rows.pop(pid, None)

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def _ops(table: str) -> ProcessOps:
    """Ops whose only fake is the process table; nothing here signals anything."""
    return ProcessOps(read_table=lambda: table)


def _profile_dir(root: Path, name: str, *, age_seconds: float = 0.0) -> Path:
    """A directory under *root*, optionally backdated so the age guard sees it as stale."""
    path = root / name
    path.mkdir(parents=True)
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(path, (old, old))
    return path


@pytest.fixture()
def armed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-arm the reaper, which the unit suite's autouse fixture disarms.

    Every test that takes this fixture also injects a fake process table or, in
    the one real-process test, a real table filtered to its own children.
    """
    monkeypatch.setattr(chrome_orphans, "_ARMED", True)


@pytest.fixture()
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``tempfile.gettempdir()`` at a sandbox, so no test can delete a real profile."""
    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(root))
    return root


class TestBrowserArgs:
    def test_the_marker_defaults_to_this_process(self) -> None:
        assert owner_switch() == f"{OWNER_SWITCH}={os.getpid()}"

    def test_the_marker_names_an_explicit_pid(self) -> None:
        assert owner_switch(4242) == "--graftpunk-owner-pid=4242"

    def test_both_launch_sites_start_from_the_same_switches(self) -> None:
        assert base_browser_args() == ["--test-type", owner_switch()]

    def test_the_caller_gets_a_list_it_may_extend(self) -> None:
        """Each call returns a fresh list, so one site's extras cannot reach the other."""
        first = base_browser_args()
        first.append("--lang=en-US")

        assert base_browser_args() == ["--test-type", owner_switch()]


class TestListChromeProcesses:
    def test_parses_a_padded_table_and_reads_both_switches(self) -> None:
        table = "\n".join(
            [
                _row(1, 0, "/sbin/launchd"),
                _row(4242, 4240, _chrome_args(owner=4240, profile="/var/folders/T/uc_abc")),
            ]
        )

        (proc,) = list_chrome_processes(_ops(table))

        assert (proc.pid, proc.ppid) == (4242, 4240)
        assert proc.owner_pid == 4240
        assert proc.user_data_dir == "/var/folders/T/uc_abc"

    def test_parses_an_unpadded_table(self) -> None:
        (proc,) = list_chrome_processes(_ops(_row(50, 1, _chrome_args(owner=49), pad=False)))

        assert (proc.pid, proc.ppid, proc.owner_pid) == (50, 1, 49)

    def test_keeps_an_argv_that_contains_spaces_whole(self) -> None:
        (proc,) = list_chrome_processes(_ops(_row(4242, 1, _chrome_args())))

        assert proc.args.startswith(CHROME)
        assert "--remote-debugging-port=9222" in proc.args

    def test_a_browser_without_a_debugging_port_is_not_listed(self) -> None:
        """The user's own Chrome and its renderers. No CDP port, never ours."""
        table = "\n".join([_row(700, 1, CHROME), _row(701, 700, f"{CHROME} --type=renderer")])

        assert list_chrome_processes(_ops(table)) == []

    def test_a_process_without_the_marker_has_no_owner(self) -> None:
        profile = "/var/folders/T/uc_x"

        (proc,) = list_chrome_processes(_ops(_row(4242, 1, _chrome_args(profile=profile))))

        assert proc.owner_pid is None
        assert proc.user_data_dir == profile

    def test_blank_and_short_rows_are_skipped(self) -> None:
        table = "\n\n   \nbroken\n" + _row(4242, 1, _chrome_args())

        assert [p.pid for p in list_chrome_processes(_ops(table))] == [4242]

    def test_a_non_numeric_owner_reads_as_no_owner(self) -> None:
        table = _row(4242, 1, f"{CHROME} --remote-debugging-port=9222 {OWNER_SWITCH}=nonsense")

        (proc,) = list_chrome_processes(_ops(table))

        assert proc.owner_pid is None

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX only: the default reader runs ps")
    def test_a_missing_ps_yields_an_empty_table_and_one_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*args: object, **kwargs: object) -> None:
            raise FileNotFoundError("ps")

        monkeypatch.setattr("graftpunk.chrome_orphans.subprocess.run", boom)

        with capture_logs() as logs:
            assert list_chrome_processes() == []

        assert [e["event"] for e in logs] == ["chrome_process_table_unavailable"]


class TestFindOrphans:
    def test_a_marker_naming_a_dead_owner_is_an_orphan(self) -> None:
        proc = _proc(4242, 1, owner=999999)

        assert find_orphans([proc], ops=FakeKernel({}).ops) == [proc]

    def test_a_marker_naming_a_live_owner_is_left_alone(self) -> None:
        proc = _proc(4242, 4240, owner=4240)

        assert find_orphans([proc], ops=FakeKernel({}, alive_pids=frozenset({4240})).ops) == []

    def test_a_reparented_temp_profile_without_a_marker_is_an_orphan(self, temp_root: Path) -> None:
        """The legacy rule: browsers launched before the marker existed."""
        proc = _proc(4242, 1, profile=str(_profile_dir(temp_root, "uc_abc")))

        assert find_orphans([proc], ops=FakeKernel({}).ops) == [proc]

    def test_a_temp_profile_with_a_living_parent_is_left_alone(self, temp_root: Path) -> None:
        proc = _proc(4242, 4240, profile=str(_profile_dir(temp_root, "uc_abc")))

        assert find_orphans([proc], ops=FakeKernel({}).ops) == []

    def test_a_custom_profile_reparented_to_pid_1_is_left_alone(self, tmp_path: Path) -> None:
        """Somebody else's headless Chrome: no marker, no temp profile, not ours."""
        proc = _proc(4242, 1, profile=str(tmp_path / "chrome-profile"))

        assert find_orphans([proc], ops=FakeKernel({}).ops) == []

    def test_a_live_marker_beats_the_legacy_rule(self, temp_root: Path) -> None:
        """A deliberately detached run: ppid 1 and a temp profile, but its owner is alive."""
        proc = _proc(4242, 1, owner=4240, profile=str(_profile_dir(temp_root, "uc_abc")))

        assert find_orphans([proc], ops=FakeKernel({}, alive_pids=frozenset({4240})).ops) == []

    def test_the_matching_rule_is_logged_with_the_process(self) -> None:
        # capture_logs() keeps the WARNING filter graftpunk installs at import,
        # which would drop this INFO event before it is captured;
        # reset_defaults() lifts it for the rest of this test. The autouse
        # _reset_structlog fixture in tests/conftest.py resets structlog again
        # afterwards, so the next test starts from structlog's own defaults.
        structlog.reset_defaults()
        with capture_logs() as logs:
            find_orphans([_proc(4242, 1, owner=999999)], ops=FakeKernel({}).ops)

        (event,) = [e for e in logs if e["event"] == "chrome_orphan_found"]
        assert event["pid"] == 4242
        assert event["owner_pid"] == 999999
        assert event["rule"] == "owner_dead"


class TestReapOrphans:
    def test_a_disarmed_reaper_signals_nothing(self) -> None:
        """The unit suite's guard, asserted at the origin rather than trusted."""
        kernel = FakeKernel({4242: _row(4242, 1, _chrome_args(owner=999999))})

        assert reap_orphans(ops=kernel.ops) == []
        assert kernel.signals == []
        assert list(kernel.rows) == [4242]

    def test_nothing_to_do_sends_no_signals(self, armed: None) -> None:
        kernel = FakeKernel(
            {4242: _row(4242, 4240, _chrome_args(owner=4240))},
            alive_pids=frozenset({4240}),
        )

        assert reap_orphans(ops=kernel.ops) == []
        assert kernel.signals == []
        assert list(kernel.rows) == [4242]

    def test_an_orphan_that_exits_on_sigterm_is_never_killed(self, armed: None) -> None:
        kernel = FakeKernel({4242: _row(4242, 1, _chrome_args(owner=999999))})

        reaped = reap_orphans(ops=kernel.ops)

        assert [p.pid for p in reaped] == [4242]
        assert kernel.signals == [(4242, signal.SIGTERM)]
        assert kernel.rows == {}

    def test_an_orphan_that_ignores_sigterm_is_killed_after_the_grace(self, armed: None) -> None:
        kernel = FakeKernel(
            {4242: _row(4242, 1, _chrome_args(owner=999999))},
            ignores_sigterm=frozenset({4242}),
        )

        reaped = reap_orphans(grace_seconds=0.3, poll_interval=0.1, ops=kernel.ops)

        assert [p.pid for p in reaped] == [4242]
        assert kernel.signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
        assert kernel.rows == {}
        assert kernel.now == pytest.approx(0.3)

    def test_the_default_grace_reads_the_table_seven_times(self, armed: None) -> None:
        """Three seconds at half a second per poll: one scan and six checks, not thirty."""
        kernel = FakeKernel(
            {4242: _row(4242, 1, _chrome_args(owner=999999))},
            ignores_sigterm=frozenset({4242}),
        )

        reap_orphans(ops=kernel.ops)

        assert kernel.reads == 7
        assert kernel.now == pytest.approx(3.0)

    def test_a_process_that_vanished_is_logged_and_the_pass_continues(self, armed: None) -> None:
        """A pid can exit between the scan and the signal. The next orphan still gets one."""
        kernel = FakeKernel(
            {
                4242: _row(4242, 1, _chrome_args(owner=999999)),
                4243: _row(4243, 1, _chrome_args(owner=999999)),
            },
            kill_raises={4242: ProcessLookupError(3, "No such process")},
        )

        # The already-gone case logs at DEBUG, below the WARNING filter
        # graftpunk installs at import; reset_defaults() lifts it for this test.
        structlog.reset_defaults()
        with capture_logs() as logs:
            reaped = reap_orphans(ops=kernel.ops)

        assert [p.pid for p in reaped] == [4243]
        assert (4243, signal.SIGTERM) in kernel.signals
        assert list(kernel.rows) == [4242]
        assert any(
            e["event"] == "chrome_orphan_cleanup_failed" and e["pid"] == 4242 for e in logs
        ), logs

    def test_a_kill_that_is_refused_is_logged_and_the_pass_continues(self, armed: None) -> None:
        kernel = FakeKernel(
            {4242: _row(4242, 1, _chrome_args(owner=999999))},
            kill_raises={4242: PermissionError(1, "Operation not permitted")},
        )

        with capture_logs() as logs:
            reaped = reap_orphans(ops=kernel.ops)

        assert reaped == []
        assert list(kernel.rows) == [4242]
        assert any(
            e["event"] == "chrome_orphan_cleanup_failed" and e["log_level"] == "warning"
            for e in logs
        ), logs

    def test_the_orphans_temp_profile_is_removed(self, armed: None, temp_root: Path) -> None:
        profile = _profile_dir(temp_root, "uc_dead")
        (profile / "Default").mkdir()
        kernel = FakeKernel({4242: _row(4242, 1, _chrome_args(owner=999999, profile=str(profile)))})

        reap_orphans(ops=kernel.ops)

        assert not profile.exists()

    def test_a_profile_outside_the_temp_dir_is_left(
        self, armed: None, tmp_path: Path, temp_root: Path
    ) -> None:
        """A plugin's persistent profile_dir is not ours to delete, uc_ name or not."""
        outside = _profile_dir(tmp_path / "persistent", "uc_keep")
        kernel = FakeKernel({4242: _row(4242, 1, _chrome_args(owner=999999, profile=str(outside)))})

        reaped = reap_orphans(ops=kernel.ops)

        assert [p.pid for p in reaped] == [4242]
        assert outside.exists()

    def test_a_live_owners_chrome_is_never_signalled(self, armed: None) -> None:
        """Two rows, one orphan. The live owner's browser keeps running."""
        kernel = FakeKernel(
            {
                4242: _row(4242, 1, _chrome_args(owner=999999)),
                4243: _row(4243, 4240, _chrome_args(owner=4240)),
            },
            alive_pids=frozenset({4240}),
        )

        reap_orphans(ops=kernel.ops)

        assert [pid for pid, _ in kernel.signals] == [4242]
        assert list(kernel.rows) == [4243]


class TestRemoveStaleTempProfiles:
    def test_a_disarmed_sweep_deletes_nothing(self, temp_root: Path) -> None:
        stale = _profile_dir(temp_root, "uc_stale", age_seconds=7200)

        assert remove_stale_temp_profiles(ops=_ops("")) == []
        assert stale.exists()

    def test_an_old_unreferenced_profile_is_removed(self, armed: None, temp_root: Path) -> None:
        stale = _profile_dir(temp_root, "uc_stale", age_seconds=7200)

        removed = remove_stale_temp_profiles(ops=_ops(""))

        assert removed == [stale]
        assert not stale.exists()

    def test_a_temp_root_with_whitespace_skips_the_sweep(
        self, armed: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A live --user-data-dir would truncate at the space; nothing is safe to delete."""
        spaced_root = tmp_path / "tmp dir"
        spaced_root.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(spaced_root))
        stale = _profile_dir(spaced_root, "uc_stale", age_seconds=7200)

        assert remove_stale_temp_profiles(ops=_ops("")) == []
        assert stale.exists()

    def test_a_fresh_profile_is_kept(self, armed: None, temp_root: Path) -> None:
        """Another process may have made it seconds ago, before its Chrome reached ps."""
        fresh = _profile_dir(temp_root, "uc_fresh")

        assert remove_stale_temp_profiles(ops=_ops("")) == []
        assert fresh.exists()

    def test_a_profile_a_live_browser_names_is_kept_however_old(
        self, armed: None, temp_root: Path
    ) -> None:
        """In use is decided from the parsed --user-data-dir, not a substring of the table."""
        live = _profile_dir(temp_root, "uc_live", age_seconds=7200)
        table = _row(4242, 1, _chrome_args(owner=os.getpid(), profile=str(live)))

        assert remove_stale_temp_profiles(ops=_ops(table)) == []
        assert live.exists()

    def test_a_profile_merely_mentioned_in_the_table_is_still_removed(
        self, armed: None, temp_root: Path
    ) -> None:
        """A path in some unrelated process's argv is not a browser using it."""
        stale = _profile_dir(temp_root, "uc_stale", age_seconds=7200)
        table = _row(4242, 1, f"/bin/cat {stale}")

        assert remove_stale_temp_profiles(ops=_ops(table)) == [stale]
        assert not stale.exists()

    def test_a_directory_that_is_not_a_profile_is_never_touched(
        self, armed: None, temp_root: Path
    ) -> None:
        other = _profile_dir(temp_root, "pytest-of-alice", age_seconds=7200)

        assert remove_stale_temp_profiles(ops=_ops("")) == []
        assert other.exists()

    def test_a_file_named_like_a_profile_is_not_removed(self, armed: None, temp_root: Path) -> None:
        path = temp_root / "uc_notadir"
        path.write_text("x")
        old = time.time() - 7200
        os.utime(path, (old, old))

        assert remove_stale_temp_profiles(ops=_ops("")) == []
        assert path.exists()


class TestTerminateNodriverBrowser:
    """The bounded sequence a signal handler is allowed to run (#96)."""

    @staticmethod
    def _browser(pid: int | None, profile: Path | None = None) -> SimpleNamespace:
        """A stand-in for a nodriver Browser: a subprocess handle and a config."""
        process = SimpleNamespace(pid=pid) if pid is not None else None
        config = SimpleNamespace(
            uses_custom_data_dir=False,
            user_data_dir=str(profile) if profile is not None else None,
        )
        return SimpleNamespace(_process=process, config=config)

    def test_a_disarmed_process_signals_nothing(self) -> None:
        kernel = FakeKernel({})

        terminate_nodriver_browser(self._browser(4242), ops=kernel.ops)

        assert kernel.signals == []

    def test_it_sends_one_sigterm(self, armed: None) -> None:
        kernel = FakeKernel({})

        terminate_nodriver_browser(self._browser(4242), ops=kernel.ops)

        assert kernel.signals == [(4242, signal.SIGTERM)]

    def test_it_leaves_the_temp_profile_for_the_stale_sweep(
        self, armed: None, temp_root: Path
    ) -> None:
        """Bounded work only: no rmtree between the signal and the process exiting."""
        profile = _profile_dir(temp_root, "uc_live")
        kernel = FakeKernel({})

        terminate_nodriver_browser(self._browser(4242, profile), ops=kernel.ops)

        assert profile.exists()

    def test_a_process_that_already_exited_is_not_an_error(self, armed: None) -> None:
        kernel = FakeKernel({}, kill_raises={4242: ProcessLookupError(3, "No such process")})

        terminate_nodriver_browser(self._browser(4242), ops=kernel.ops)

        assert kernel.signals == [(4242, signal.SIGTERM)]

    def test_a_refused_signal_is_not_an_error(self, armed: None) -> None:
        kernel = FakeKernel({}, kill_raises={4242: PermissionError(1, "Operation not permitted")})

        terminate_nodriver_browser(self._browser(4242), ops=kernel.ops)

        assert kernel.signals == [(4242, signal.SIGTERM)]

    def test_a_browser_with_no_subprocess_is_a_no_op(self, armed: None) -> None:
        kernel = FakeKernel({})

        terminate_nodriver_browser(self._browser(None), ops=kernel.ops)

        assert kernel.signals == []


class TestBrowserTempProfile:
    """What nodriver made for a browser, and whether it is ours to touch."""

    @staticmethod
    def _browser(*, custom: bool, user_data_dir: str | None) -> SimpleNamespace:
        return SimpleNamespace(
            config=SimpleNamespace(uses_custom_data_dir=custom, user_data_dir=user_data_dir)
        )

    def test_a_custom_data_dir_is_never_reported(self, tmp_path: Path) -> None:
        browser = self._browser(custom=True, user_data_dir=str(tmp_path / "profile_dir"))

        assert browser_temp_profile(browser) is None

    def test_a_nodriver_made_dir_is_reported(self, temp_root: Path) -> None:
        profile = _profile_dir(temp_root, "uc_abc")
        browser = self._browser(custom=False, user_data_dir=str(profile))

        assert browser_temp_profile(browser) == profile

    def test_a_config_that_raises_is_reported_as_no_profile(self) -> None:
        class BoomConfig:
            @property
            def uses_custom_data_dir(self) -> bool:
                raise AttributeError("no such attribute")

        assert browser_temp_profile(SimpleNamespace(config=BoomConfig())) is None


class TestRemoveBrowserTempProfile:
    """The rmtree call that runs after a browser has already stopped, outside _ARMED."""

    @staticmethod
    def _browser(*, custom: bool, user_data_dir: str) -> SimpleNamespace:
        return SimpleNamespace(
            config=SimpleNamespace(uses_custom_data_dir=custom, user_data_dir=user_data_dir)
        )

    def test_a_temp_profile_under_the_temp_root_is_removed(self, temp_root: Path) -> None:
        profile = _profile_dir(temp_root, "uc_live")
        browser = self._browser(custom=False, user_data_dir=str(profile))

        removed = remove_browser_temp_profile(browser)

        assert removed == profile
        assert not profile.exists()

    def test_a_temp_profile_outside_the_temp_root_survives(
        self, tmp_path: Path, temp_root: Path
    ) -> None:
        outside = _profile_dir(tmp_path / "persistent", "uc_keep")
        browser = self._browser(custom=False, user_data_dir=str(outside))

        assert remove_browser_temp_profile(browser) is None
        assert outside.exists()

    def test_a_custom_profile_dir_survives_however_it_is_named(self, temp_root: Path) -> None:
        """The custom flag protects it before the name or location is ever checked."""
        custom = _profile_dir(temp_root, "uc_custom")
        browser = self._browser(custom=True, user_data_dir=str(custom))

        assert remove_browser_temp_profile(browser) is None
        assert custom.exists()


def _ps_limited_to(pids: set[int]) -> ProcessOps:
    """Real ops, but a table cut down to the pids this test spawned.

    Real ps and real os.kill, and nothing else on this machine can match: a
    genuine orphan from a real session must survive a test run untouched.
    """

    def table() -> str:
        out = subprocess.run(  # noqa: S603
            ["ps", "-eo", "pid=,ppid=,args="],  # noqa: S607 - PATH lookup of ps is intentional
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
        wanted = {str(pid) for pid in pids}
        rows = []
        for line in out.splitlines():
            fields = line.strip().split(maxsplit=1)
            if fields and fields[0] in wanted:
                rows.append(line)
        return "\n".join(rows)

    return ProcessOps(read_table=table)


def _a_pid_that_is_gone() -> int:
    """A pid that has exited and been reaped, so ``os.kill(pid, 0)`` raises."""
    done = subprocess.Popen([sys.executable, "-c", ""])  # noqa: S603
    done.wait(timeout=30)
    try:
        os.kill(done.pid, 0)
    except ProcessLookupError:
        return done.pid
    pytest.skip("the pid was reused before the test could use it")


def _spawn_sleeper(owner_pid: int) -> subprocess.Popen:
    """A harmless stand-in for a Chrome: a sleeping python wearing Chrome's switches.

    No test starts a browser. The argv is all the reaper matches on, so a
    sleeper carrying --remote-debugging-port and the owner marker is
    indistinguishable to it from a real orphan.
    """
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            "--remote-debugging-port=0",
            f"{OWNER_SWITCH}={owner_pid}",
        ]
    )
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if any(p.pid == proc.pid for p in list_chrome_processes(_ps_limited_to({proc.pid}))):
            return proc
        time.sleep(0.05)
    proc.kill()
    proc.wait(timeout=10)
    pytest.fail("the sleeper never appeared in ps")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX only: no ps, no SIGTERM semantics")
class TestRealProcesses:
    """The one place real signals are sent, and only ever to this test's own children."""

    def test_this_process_is_alive(self) -> None:
        assert _pid_alive(os.getpid()) is True

    def test_a_reaped_child_is_not_alive(self) -> None:
        assert _pid_alive(_a_pid_that_is_gone()) is False

    def test_a_non_positive_pid_is_reported_alive_without_asking_the_kernel(self) -> None:
        """0 and negative pids name a process group, not one process; never signal one."""
        assert _pid_alive(0) is True
        assert _pid_alive(-1) is True

    def test_a_real_orphan_is_ended_and_a_live_owners_browser_is_not(self, armed: None) -> None:
        procs: list[subprocess.Popen] = []
        try:
            orphan = _spawn_sleeper(_a_pid_that_is_gone())
            procs.append(orphan)
            mine = _spawn_sleeper(os.getpid())
            procs.append(mine)

            reaped = reap_orphans(ops=_ps_limited_to({orphan.pid, mine.pid}))

            assert [p.pid for p in reaped] == [orphan.pid]
            assert orphan.wait(timeout=10) == -signal.SIGTERM
            assert mine.poll() is None
        finally:
            for proc in procs:
                proc.kill()
                proc.wait(timeout=10)
