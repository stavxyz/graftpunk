---
type: plan
---

# Orphaned Chrome Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A nodriver Chrome whose Python parent died without cleanup is found and killed before the next browser starts, every stop deletes the temp profile nodriver leaves behind, and SIGTERM or SIGHUP to the CLI ends the browsers it launched (#96).

**Architecture:** One new dependency-light module, `src/graftpunk/backends/chrome_orphans.py`, owns everything that reads the process table, signals a process, or deletes a profile directory. It identifies graftpunk's own Chromes by a marker switch (`--graftpunk-owner-pid=<pid>`) that every launch passes through `browser_args` and that survives into `ps`, so identification is exact rather than heuristic. The two launch sites (`NoDriverBackend._start_async` and the `gp observe` path, which calls `nodriver.start` directly) both call the same three helpers, so they cannot drift. A module-level `WeakSet` of live `BrowserSession` instances plus a new `src/graftpunk/cli/signals.py` cover the catchable signals; SIGKILL and the OOM killer stay uncatchable, and the reaper is their backstop.

**Tech Stack:** Python 3.11+, nodriver 0.48.1, Typer CLI, pydantic-settings, structlog, pytest (`uv run pytest`), ruff, ty 0.0.75. No new dependency: the process table comes from `ps`, not `psutil`.

**Spec:** `docs/superpowers/specs/2026-09-07-chrome-orphan-cleanup-design.md` (approved; read it alongside this plan, and resolve any conflict in favour of the spec, except where a task carries a Design note recording a reviewed deviation).

## Global Constraints

- Python `>=3.11` typing throughout: `X | None`, never `Optional[X]`. `from __future__ import annotations` at the top of every new module.
- structlog event-style logging: `LOG = get_logger(__name__)` at module scope, event name first, values as keyword arguments (`src/graftpunk/logging.py:169` (`def get_logger(name: str | None = None) -> structlog.BoundLogger:`)).
- Gate command, green at every commit: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
- Tests assert behaviour, never mock-call counts (`assert_called_once_with`, `call_count`). Inject the process table and the kill function as the spec says, and assert on the resulting table, the returned list, the files on disk, and the captured structlog events.
- No test may kill or launch a real Chrome. The one real-process test spawns a `python -c` sleeper wearing Chrome's switches, per the spec.
- Placeholders only, in tests and docs: `myshop`, `alice@example.com`, `fmtsite`. Never name a real site, store, account, or domain.
- No em dashes or en dashes in any new prose, docstring, comment, or commit message. Use commas, colons, parentheses, or separate sentences.
- No attribution trailers in commits. Commit subjects are `type(scope): subject (#96)`.
- POSIX only, with a documented no-op on win32: the default process table reader is skipped there and every function that depends on it returns nothing.
- Nothing in the new code may raise into a browser start or hang a signal handler. Cleanup failures are logged and swallowed; the signal handler's work is bounded to one SIGTERM per live session plus one `rmtree`, then the default action.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/graftpunk/backends/chrome_orphans.py` (new) | The whole mechanism: the marker switch, the `ps` reader and parser, the orphan rules, the reaper, the stale-profile sweep, the per-browser temp-profile deletion, and the one guarded entry point both launch sites call. Depends on nothing in graftpunk but `graftpunk.logging` at module scope. |
| `src/graftpunk/config.py` | One new settings field, `keep_orphaned_chrome`, read from `GRAFTPUNK_KEEP_ORPHANED_CHROME`. |
| `src/graftpunk/backends/nodriver.py` | `_start_async` marks and sweeps; `_stop_async` deletes the temp profile; a new `terminate_browser_process()` for signal handlers. |
| `src/graftpunk/cli/main.py` | The `gp observe` launch site: the same sweep, the same marker, and one `_stop_observe_browser` helper replacing the three bare `browser.stop()` calls. `main_callback` installs the termination cleanup. |
| `src/graftpunk/session.py` | The `_LIVE_SESSIONS` WeakSet, its `live_sessions()` snapshot accessor, and `BrowserSession.terminate_browser_process()`. |
| `src/graftpunk/cli/signals.py` (new) | `install_termination_cleanup()`: the SIGTERM and SIGHUP handlers, installed only into slots the host has left at `SIG_DFL`. |
| `tests/unit/conftest.py` | The autouse guard that keeps the unit suite away from the real process table. |
| `tests/unit/test_chrome_orphans.py` (new) | The module's own contract, including the one real-process test. |
| `tests/unit/test_nodriver_backend.py` | The backend's marker, its sweep ordering, the leak fix, and `terminate_browser_process`. |
| `tests/unit/test_observe_interactive.py` | The `gp observe` launch site's marker and profile cleanup. |
| `tests/unit/test_termination_signals.py` (new) | The live-session registry and the signal handlers, including the subprocess test. |
| `docs/HOW_IT_WORKS.md`, `README.md`, `CHANGELOG.md` | User-facing documentation of the cleanup, the marker, and the opt-out. |

---

### Task 1: the orphan module

**Files:**
- Create: `src/graftpunk/backends/chrome_orphans.py`
- Test: `tests/unit/test_chrome_orphans.py` (new)

**Interfaces:**
- Consumes: `graftpunk.logging.get_logger` (`src/graftpunk/logging.py:169` (`def get_logger(name: str | None = None) -> structlog.BoundLogger:`)) at module scope, and nothing else in graftpunk. `graftpunk.config.get_settings` and `graftpunk.console` are imported inside `cleanup_orphans_before_launch` only, so importing this module stays cheap and cycle-free for both the backend and the CLI.
- Produces, all used by Tasks 2, 3 and 4:
  - `OWNER_SWITCH: str` (`"--graftpunk-owner-pid"`).
  - `owner_switch(pid: int | None = None) -> str`.
  - `ChromeProcess` (frozen dataclass): `pid: int`, `ppid: int`, `args: str`, `owner_pid: int | None`, `user_data_dir: str | None`.
  - `list_chrome_processes(process_table: Callable[[], str] | None = None) -> list[ChromeProcess]`.
  - `find_orphans(processes: Iterable[ChromeProcess], *, pid_alive: Callable[[int], bool] = _pid_alive) -> list[ChromeProcess]`.
  - `reap_orphans(*, grace_seconds: float = 3.0, poll_interval: float = 0.1, process_table: Callable[[], str] | None = None, kill: Callable[[int, int], None] = os.kill, pid_alive: Callable[[int], bool] = _pid_alive, sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic) -> list[ChromeProcess]`.
  - `remove_stale_temp_profiles(*, older_than_seconds: float = 3600.0, process_table: Callable[[], str] | None = None, now: Callable[[], float] = time.time) -> list[Path]`.
  - `browser_temp_profile(browser: Any) -> Path | None` and `remove_browser_temp_profile(browser: Any) -> Path | None`.
  - `cleanup_orphans_before_launch() -> int`, the guarded entry point Tasks 2 and 3 call. It reads `Settings.keep_orphaned_chrome`, which Task 2 adds; until then the attribute does not exist, so this task does not call it from anywhere and its own tests do not exercise it. Task 2 adds the field and the tests that do.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_chrome_orphans.py`:

```python
"""The orphaned-Chrome reaper: identification, signalling, and profile cleanup (#96)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs

from graftpunk.backends.chrome_orphans import (
    OWNER_SWITCH,
    ChromeProcess,
    _pid_alive,
    find_orphans,
    list_chrome_processes,
    owner_switch,
    reap_orphans,
    remove_stale_temp_profiles,
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


class FakeTable:
    """A mutable process table and the kill function that mutates it.

    The pair stands in for the kernel: a signal that would end a process
    removes its row, so a test asserts on the table that is left rather than
    on which mock was called.
    """

    def __init__(
        self,
        rows: dict[int, str],
        *,
        ignores_sigterm: frozenset[int] = frozenset(),
        kill_raises: dict[int, OSError] | None = None,
    ) -> None:
        self.rows = dict(rows)
        self.ignores_sigterm = ignores_sigterm
        self.kill_raises = kill_raises or {}
        self.signals: list[tuple[int, int]] = []

    def read(self) -> str:
        return "\n".join(self.rows.values())

    def kill(self, pid: int, signum: int) -> None:
        self.signals.append((pid, signum))
        exc = self.kill_raises.get(pid)
        if exc is not None:
            raise exc
        if signum == signal.SIGKILL or (
            signum == signal.SIGTERM and pid not in self.ignores_sigterm
        ):
            self.rows.pop(pid, None)


class FakeClock:
    """A monotonic clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _profile_dir(root: Path, name: str, *, age_seconds: float = 0.0) -> Path:
    """A directory under *root*, optionally backdated so the age guard sees it as stale."""
    path = root / name
    path.mkdir(parents=True)
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(path, (old, old))
    return path


@pytest.fixture()
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``tempfile.gettempdir()`` at a sandbox, so no test can delete a real profile."""
    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(root))
    return root


class TestOwnerSwitch:
    def test_defaults_to_this_process(self) -> None:
        assert owner_switch() == f"{OWNER_SWITCH}={os.getpid()}"

    def test_names_an_explicit_pid(self) -> None:
        assert owner_switch(4242) == "--graftpunk-owner-pid=4242"


class TestListChromeProcesses:
    def test_parses_a_padded_table_and_reads_both_switches(self) -> None:
        table = "\n".join(
            [
                _row(1, 0, "/sbin/launchd"),
                _row(4242, 4240, _chrome_args(owner=4240, profile="/var/folders/T/uc_abc")),
            ]
        )

        (proc,) = list_chrome_processes(lambda: table)

        assert (proc.pid, proc.ppid) == (4242, 4240)
        assert proc.owner_pid == 4240
        assert proc.user_data_dir == "/var/folders/T/uc_abc"

    def test_parses_an_unpadded_table(self) -> None:
        table = _row(50, 1, _chrome_args(owner=49), pad=False)

        (proc,) = list_chrome_processes(lambda: table)

        assert (proc.pid, proc.ppid, proc.owner_pid) == (50, 1, 49)

    def test_keeps_an_argv_that_contains_spaces_whole(self) -> None:
        (proc,) = list_chrome_processes(lambda: _row(4242, 1, _chrome_args()))

        assert proc.args.startswith(CHROME)
        assert "--remote-debugging-port=9222" in proc.args

    def test_a_browser_without_a_debugging_port_is_not_listed(self) -> None:
        """The user's own Chrome and its renderers. No CDP port, never ours."""
        table = "\n".join(
            [
                _row(700, 1, CHROME),
                _row(701, 700, f"{CHROME} --type=renderer"),
            ]
        )

        assert list_chrome_processes(lambda: table) == []

    def test_a_process_without_the_marker_has_no_owner(self) -> None:
        profile = "/var/folders/T/uc_x"
        (proc,) = list_chrome_processes(lambda: _row(4242, 1, _chrome_args(profile=profile)))

        assert proc.owner_pid is None
        assert proc.user_data_dir == profile

    def test_blank_and_short_rows_are_skipped(self) -> None:
        table = "\n\n   \nbroken\n" + _row(4242, 1, _chrome_args())

        assert [p.pid for p in list_chrome_processes(lambda: table)] == [4242]

    def test_a_non_numeric_owner_reads_as_no_owner(self) -> None:
        table = _row(4242, 1, f"{CHROME} --remote-debugging-port=9222 {OWNER_SWITCH}=nonsense")

        (proc,) = list_chrome_processes(lambda: table)

        assert proc.owner_pid is None

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX only: the default reader runs ps")
    def test_a_missing_ps_yields_an_empty_table_and_one_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*args: object, **kwargs: object) -> None:
            raise FileNotFoundError("ps")

        monkeypatch.setattr("graftpunk.backends.chrome_orphans.subprocess.run", boom)

        with capture_logs() as logs:
            assert list_chrome_processes() == []

        assert [e["event"] for e in logs] == ["chrome_process_table_unavailable"]


class TestFindOrphans:
    def test_a_marker_naming_a_dead_owner_is_an_orphan(self) -> None:
        proc = _proc(4242, 1, owner=999999)

        assert find_orphans([proc], pid_alive=lambda pid: False) == [proc]

    def test_a_marker_naming_a_live_owner_is_left_alone(self) -> None:
        proc = _proc(4242, 4240, owner=4240)

        assert find_orphans([proc], pid_alive=lambda pid: True) == []

    def test_a_reparented_temp_profile_without_a_marker_is_an_orphan(self) -> None:
        """The legacy rule: browsers launched before the marker existed."""
        proc = _proc(4242, 1, profile="/var/folders/T/uc_abc")

        assert find_orphans([proc], pid_alive=lambda pid: False) == [proc]

    def test_a_temp_profile_with_a_living_parent_is_left_alone(self) -> None:
        proc = _proc(4242, 4240, profile="/var/folders/T/uc_abc")

        assert find_orphans([proc], pid_alive=lambda pid: False) == []

    def test_a_custom_profile_reparented_to_pid_1_is_left_alone(self) -> None:
        """Somebody else's headless Chrome: no marker, no uc_ profile, not ours."""
        proc = _proc(4242, 1, profile="/home/alice/chrome-profile")

        assert find_orphans([proc], pid_alive=lambda pid: False) == []

    def test_a_live_marker_beats_the_legacy_rule(self) -> None:
        """A deliberately detached run: ppid 1 and a uc_ profile, but its owner is alive."""
        proc = _proc(4242, 1, owner=4240, profile="/var/folders/T/uc_abc")

        assert find_orphans([proc], pid_alive=lambda pid: True) == []

    def test_the_matching_rule_is_logged_with_the_process(self) -> None:
        # capture_logs() keeps the library's import-time WARNING filter by
        # default, which would drop this INFO event before it is captured;
        # reset_defaults() lifts it (the autouse _reset_structlog fixture in
        # tests/conftest.py restores it after the test).
        structlog.reset_defaults()
        with capture_logs() as logs:
            find_orphans([_proc(4242, 1, owner=999999)], pid_alive=lambda pid: False)

        (event,) = [e for e in logs if e["event"] == "chrome_orphan_found"]
        assert event["pid"] == 4242
        assert event["owner_pid"] == 999999
        assert event["rule"] == "owner_dead"


class TestReapOrphans:
    def test_nothing_to_do_sends_no_signals(self) -> None:
        table = FakeTable({4242: _row(4242, 4240, _chrome_args(owner=4240))})
        clock = FakeClock()

        reaped = reap_orphans(
            process_table=table.read,
            kill=table.kill,
            pid_alive=lambda pid: True,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        assert reaped == []
        assert table.signals == []
        assert list(table.rows) == [4242]

    def test_an_orphan_that_exits_on_sigterm_is_never_killed(self) -> None:
        table = FakeTable({4242: _row(4242, 1, _chrome_args(owner=999999))})
        clock = FakeClock()

        reaped = reap_orphans(
            process_table=table.read,
            kill=table.kill,
            pid_alive=lambda pid: False,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        assert [p.pid for p in reaped] == [4242]
        assert table.signals == [(4242, signal.SIGTERM)]
        assert table.rows == {}

    def test_an_orphan_that_ignores_sigterm_is_killed_after_the_grace(self) -> None:
        table = FakeTable(
            {4242: _row(4242, 1, _chrome_args(owner=999999))},
            ignores_sigterm=frozenset({4242}),
        )
        clock = FakeClock()

        reaped = reap_orphans(
            grace_seconds=0.3,
            poll_interval=0.1,
            process_table=table.read,
            kill=table.kill,
            pid_alive=lambda pid: False,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        assert [p.pid for p in reaped] == [4242]
        assert table.signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
        assert table.rows == {}
        assert clock.now == pytest.approx(0.3)

    def test_a_process_that_vanished_is_logged_and_the_pass_continues(self) -> None:
        """A pid can exit between the scan and the signal. The next orphan still gets one."""
        table = FakeTable(
            {
                4242: _row(4242, 1, _chrome_args(owner=999999)),
                4243: _row(4243, 1, _chrome_args(owner=999999)),
            },
            kill_raises={4242: ProcessLookupError(3, "No such process")},
        )
        clock = FakeClock()

        # The already-gone case logs at DEBUG, below the library's import-time
        # WARNING filter; reset_defaults() lifts it for this test.
        structlog.reset_defaults()
        with capture_logs() as logs:
            reaped = reap_orphans(
                process_table=table.read,
                kill=table.kill,
                pid_alive=lambda pid: False,
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            )

        assert [p.pid for p in reaped] == [4243]
        assert (4243, signal.SIGTERM) in table.signals
        assert list(table.rows) == [4242]
        assert any(
            e["event"] == "chrome_orphan_cleanup_failed" and e["pid"] == 4242 for e in logs
        ), logs

    def test_a_kill_that_is_refused_is_logged_and_the_pass_continues(self) -> None:
        table = FakeTable(
            {4242: _row(4242, 1, _chrome_args(owner=999999))},
            kill_raises={4242: PermissionError(1, "Operation not permitted")},
        )
        clock = FakeClock()

        with capture_logs() as logs:
            reaped = reap_orphans(
                process_table=table.read,
                kill=table.kill,
                pid_alive=lambda pid: False,
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            )

        assert reaped == []
        assert list(table.rows) == [4242]
        assert any(
            e["event"] == "chrome_orphan_cleanup_failed" and e["log_level"] == "warning"
            for e in logs
        ), logs

    def test_the_orphans_temp_profile_is_removed(self, temp_root: Path) -> None:
        profile = _profile_dir(temp_root, "uc_dead")
        (profile / "Default").mkdir()
        table = FakeTable({4242: _row(4242, 1, _chrome_args(owner=999999, profile=str(profile)))})
        clock = FakeClock()

        reap_orphans(
            process_table=table.read,
            kill=table.kill,
            pid_alive=lambda pid: False,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        assert not profile.exists()

    def test_a_profile_outside_the_temp_dir_is_left(self, tmp_path: Path, temp_root: Path) -> None:
        """A plugin's persistent profile_dir is not ours to delete, uc_ name or not."""
        outside = _profile_dir(tmp_path / "persistent", "uc_keep")
        table = FakeTable({4242: _row(4242, 1, _chrome_args(owner=999999, profile=str(outside)))})
        clock = FakeClock()

        reaped = reap_orphans(
            process_table=table.read,
            kill=table.kill,
            pid_alive=lambda pid: False,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        assert [p.pid for p in reaped] == [4242]
        assert outside.exists()

    def test_a_live_owners_chrome_is_never_signalled(self) -> None:
        """Two rows, one orphan. The live owner's browser keeps running."""
        table = FakeTable(
            {
                4242: _row(4242, 1, _chrome_args(owner=999999)),
                4243: _row(4243, 4240, _chrome_args(owner=4240)),
            }
        )
        clock = FakeClock()

        reap_orphans(
            process_table=table.read,
            kill=table.kill,
            pid_alive=lambda pid: pid == 4240,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        assert [pid for pid, _ in table.signals] == [4242]
        assert list(table.rows) == [4243]


class TestRemoveStaleTempProfiles:
    def test_an_old_unreferenced_profile_is_removed(self, temp_root: Path) -> None:
        stale = _profile_dir(temp_root, "uc_stale", age_seconds=7200)

        removed = remove_stale_temp_profiles(process_table=lambda: "")

        assert removed == [stale]
        assert not stale.exists()

    def test_a_fresh_profile_is_kept(self, temp_root: Path) -> None:
        """Another process may have made it seconds ago, before its Chrome reached ps."""
        fresh = _profile_dir(temp_root, "uc_fresh")

        assert remove_stale_temp_profiles(process_table=lambda: "") == []
        assert fresh.exists()

    def test_a_referenced_profile_is_kept_however_old(self, temp_root: Path) -> None:
        live = _profile_dir(temp_root, "uc_live", age_seconds=7200)
        table = _row(4242, 1, _chrome_args(owner=os.getpid(), profile=str(live)))

        assert remove_stale_temp_profiles(process_table=lambda: table) == []
        assert live.exists()

    def test_a_directory_that_is_not_a_profile_is_never_touched(self, temp_root: Path) -> None:
        other = _profile_dir(temp_root, "pytest-of-alice", age_seconds=7200)

        assert remove_stale_temp_profiles(process_table=lambda: "") == []
        assert other.exists()

    def test_a_file_named_like_a_profile_is_not_removed(self, temp_root: Path) -> None:
        path = temp_root / "uc_notadir"
        path.write_text("x")
        old = time.time() - 7200
        os.utime(path, (old, old))

        assert remove_stale_temp_profiles(process_table=lambda: "") == []
        assert path.exists()


def _ps_limited_to(pids: set[int]):  # noqa: ANN202
    """The real ``ps`` table, cut down to the pids this test spawned.

    Real ps and real os.kill, but nothing else on this machine can match: a
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

    return table


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

    def test_a_real_orphan_is_ended_and_a_live_owners_browser_is_not(self) -> None:
        orphan = _spawn_sleeper(_a_pid_that_is_gone())
        mine = _spawn_sleeper(os.getpid())
        try:
            reaped = reap_orphans(process_table=_ps_limited_to({orphan.pid, mine.pid}))

            assert [p.pid for p in reaped] == [orphan.pid]
            assert orphan.wait(timeout=10) == -signal.SIGTERM
            assert mine.poll() is None
        finally:
            for proc in (orphan, mine):
                proc.kill()
                proc.wait(timeout=10)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_chrome_orphans.py -q`

Expected: collection error, `ModuleNotFoundError: No module named 'graftpunk.backends.chrome_orphans'`.

- [ ] **Step 3: Write the implementation**

Create `src/graftpunk/backends/chrome_orphans.py`:

```python
"""Find and end the Chrome processes graftpunk's nodriver sessions left behind (#96).

When the Python process that launched a nodriver Chrome dies without running
its cleanup (SIGKILL, an OOM kill, a closed terminal, a crash), Chrome
survives. The survivors hold their debug ports and their memory, and they
accumulate. This module is the backstop: before a new browser starts, both
launch sites ask it to find the survivors nobody owns any more and end them.

Identification is exact rather than heuristic. Every graftpunk launch passes
``--graftpunk-owner-pid=<pid>`` in ``browser_args``; Chrome ignores switches
it does not recognise (and ``--test-type``, which graftpunk already passes,
suppresses the "unsupported flag" banner), so the marker rides through into
Chrome's command line and shows up in ``ps`` for the browser process and its
helpers. A process carrying the marker whose owner pid is gone belongs to
nobody. A second, narrower rule covers browsers launched before the marker
existed and other nodriver users' leftovers: no marker, a nodriver temp
profile (``uc_*``) as its ``--user-data-dir``, and a parent of pid 1.

Chrome already shares graftpunk's process group. nodriver launches it with
``asyncio.create_subprocess_exec`` and passes no ``start_new_session``, so the
child inherits the parent's session and process group; a terminal SIGHUP and
``kill -- -<pgid>`` reach Chrome without any process-group code here.

This module also owns the fix for the leak that makes reaping necessary so
often: nodriver creates its temp profile with ``tempfile.mkdtemp(prefix="uc_")``
and deletes it only from an ``atexit`` handler that iterates its registry of
live browsers. graftpunk removes its browser from that registry on stop, so
nothing deletes the directory. :func:`remove_browser_temp_profile` does.

POSIX only. The default process table comes from ``ps -eo pid=,ppid=,args=``,
which Windows does not have: there the default reader is skipped with one
debug log and every function that depends on it returns nothing. An injected
table is still parsed, so the parsing tests are meaningful on any platform.

Everything here is best effort. A cleanup pass must never turn a browser start
into a failure or a signal into a hang, so per-item ``OSError`` is logged and
swallowed.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graftpunk.logging import get_logger

LOG = get_logger(__name__)

OWNER_SWITCH = "--graftpunk-owner-pid"

_DEBUG_PORT_SWITCH = "--remote-debugging-port"
_USER_DATA_DIR_SWITCH = "--user-data-dir"
_TEMP_PROFILE_PREFIX = "uc_"
_PS_ARGV = ["ps", "-eo", "pid=,ppid=,args="]
_PS_TIMEOUT_S = 5.0
_GRACE_SECONDS = 3.0
_POLL_INTERVAL_S = 0.1
_STALE_PROFILE_SECONDS = 3600.0


def owner_switch(pid: int | None = None) -> str:
    """The marker switch for *pid*, or for this process.

    Args:
        pid: The owner to name. Defaults to this process.

    Returns:
        ``--graftpunk-owner-pid=<pid>``, ready to append to ``browser_args``.
    """
    return f"{OWNER_SWITCH}={os.getpid() if pid is None else pid}"


@dataclass(frozen=True)
class ChromeProcess:
    """One row of the process table that looks like a CDP-controlled browser.

    Attributes:
        pid: The process id.
        ppid: The parent process id. 1 means the parent is gone and init
            adopted it.
        args: The whole argv column, verbatim.
        owner_pid: The pid from the marker switch, when the process carries
            one. None for a browser graftpunk did not launch, or launched
            before the marker existed.
        user_data_dir: The value of ``--user-data-dir``, when present.
    """

    pid: int
    ppid: int
    args: str
    owner_pid: int | None
    user_data_dir: str | None


def _read_ps_table() -> str:
    """The process table from ``ps``, or an empty table and one warning."""
    try:
        result = subprocess.run(  # noqa: S603, S607 - PATH lookup of ps is intentional
            _PS_ARGV,
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOG.warning("chrome_process_table_unavailable", error=str(exc))
        return ""
    if result.returncode != 0:
        LOG.warning("chrome_process_table_unavailable", error=result.stderr.strip())
        return ""
    return result.stdout


def _process_table(process_table: Callable[[], str] | None) -> str | None:
    """The raw table to scan, or None when this platform has no default reader.

    An injected table is always used, on any platform. The default reader runs
    ``ps``, which Windows does not have, so there it is skipped and the
    callers return nothing.
    """
    if process_table is not None:
        return process_table()
    if os.name != "posix":
        LOG.debug("chrome_orphan_scan_unsupported", os_name=os.name)
        return None
    return _read_ps_table()


def _switch_value(args: str, switch: str) -> str | None:
    """The value of ``--switch=value`` in an argv string, or None.

    A value is read up to the next whitespace, so a path containing a space is
    truncated. A truncated path then fails every "is this a temp profile
    directory that exists" check below, which is the safe direction: the
    reaper leaves it alone.
    """
    marker = f"{switch}="
    for token in args.split():
        if token.startswith(marker):
            return token[len(marker) :]
    return None


def _parse_row(row: str) -> ChromeProcess | None:
    """One ``pid ppid args`` line as a :class:`ChromeProcess`, or None to skip it."""
    fields = row.strip().split(maxsplit=2)
    if len(fields) < 3:
        return None
    pid_text, ppid_text, args = fields
    if _DEBUG_PORT_SWITCH not in args:
        return None
    try:
        pid = int(pid_text)
        ppid = int(ppid_text)
    except ValueError:
        return None
    owner_text = _switch_value(args, OWNER_SWITCH)
    owner_pid: int | None
    try:
        owner_pid = int(owner_text) if owner_text is not None else None
    except ValueError:
        owner_pid = None
    return ChromeProcess(
        pid=pid,
        ppid=ppid,
        args=args,
        owner_pid=owner_pid,
        user_data_dir=_switch_value(args, _USER_DATA_DIR_SWITCH),
    )


def list_chrome_processes(process_table: Callable[[], str] | None = None) -> list[ChromeProcess]:
    """Every process whose argv carries ``--remote-debugging-port``.

    The debugging port is the filter that separates automated browsers from
    the user's own: a Chrome the user opened has no CDP port and never appears
    here. Renderer and GPU helpers of an automated browser do appear, because
    they inherit the same switches, which is what lets the rules cover a
    helper whose browser process is already gone.

    Args:
        process_table: A callable returning the raw table, for tests. Defaults
            to running ``ps -eo pid=,ppid=,args=``.

    Returns:
        The matching rows. Empty on a platform with no default reader, or when
        ``ps`` is missing or fails.
    """
    table = _process_table(process_table)
    if table is None:
        return []
    processes = []
    for row in table.splitlines():
        parsed = _parse_row(row)
        if parsed is not None:
            processes.append(parsed)
    return processes


def _pid_alive(pid: int) -> bool:
    """Whether *pid* names a live process.

    ``os.kill(pid, 0)`` delivers no signal and only asks the kernel whether it
    could. A process owned by another user answers ``PermissionError``, which
    means it exists. Anything else unknown counts as alive: the conservative
    direction, because the cost of a false "alive" is one orphan surviving
    until the next launch, and the cost of a false "dead" is killing somebody's
    browser.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _is_temp_profile(user_data_dir: str | None) -> bool:
    """Whether *user_data_dir* looks like a directory nodriver made for itself."""
    return bool(user_data_dir) and f"/{_TEMP_PROFILE_PREFIX}" in str(user_data_dir)


def find_orphans(
    processes: Iterable[ChromeProcess],
    *,
    pid_alive: Callable[[int], bool] = _pid_alive,
) -> list[ChromeProcess]:
    """The subset of *processes* that nobody owns any more.

    Two rules, in order:

    a. The marker names an owner pid that is not alive. Exact: this is
       graftpunk's own Chrome and the process that launched it is gone. A
       reused pid makes this rule skip an orphan, never kill a live owner's
       browser.
    b. No marker, the profile is a nodriver temp profile (``uc_`` in
       ``--user-data-dir``), and the parent is pid 1. This covers browsers
       launched before the marker existed and other nodriver users'
       leftovers; a process reparented to pid 1 has no living parent by
       definition. Under a systemd user session an orphan may be reparented to
       a subreaper other than pid 1, in which case this rule does not fire and
       that legacy orphan survives until rule (a) covers it on the next
       launch. That is the conservative direction.

    A process whose owner is alive never matches, and a browser without a
    debugging port never reached this function.

    Args:
        processes: Rows from :func:`list_chrome_processes`.
        pid_alive: The liveness test, for tests.

    Returns:
        The orphans, in table order.
    """
    orphans = []
    for proc in processes:
        if proc.owner_pid is not None:
            rule = "owner_dead" if not pid_alive(proc.owner_pid) else None
        elif proc.ppid == 1 and _is_temp_profile(proc.user_data_dir):
            rule = "reparented_temp_profile"
        else:
            rule = None
        if rule is None:
            continue
        LOG.info(
            "chrome_orphan_found",
            pid=proc.pid,
            ppid=proc.ppid,
            owner_pid=proc.owner_pid,
            user_data_dir=proc.user_data_dir,
            rule=rule,
        )
        orphans.append(proc)
    return orphans


def _removable_temp_profile(user_data_dir: str | None) -> Path | None:
    """*user_data_dir* as a Path when it is a ``uc_*`` directory under the temp dir.

    The guard that keeps the reaper away from a plugin's persistent
    ``profile_dir``: only a directory nodriver itself created with
    ``tempfile.mkdtemp(prefix="uc_")`` is ours to delete, and that always
    lands directly under :func:`tempfile.gettempdir`.
    """
    if not user_data_dir:
        return None
    candidate = Path(user_data_dir)
    if not candidate.name.startswith(_TEMP_PROFILE_PREFIX):
        return None
    try:
        parent = candidate.resolve().parent
        temp_root = Path(tempfile.gettempdir()).resolve()
    except OSError:
        return None
    return candidate if parent == temp_root else None


def _deliver(
    proc: ChromeProcess, signum: int, kill: Callable[[int, int], None], event: str
) -> bool:
    """Send one signal to one orphan. Never raises.

    Returns:
        True when the signal was delivered. A process that has already exited
        (``ProcessLookupError``) and a signal the kernel refused both return
        False, the first at debug because it is a normal race and the second
        at warning because it is not.
    """
    try:
        kill(proc.pid, signum)
    except ProcessLookupError as exc:
        LOG.debug("chrome_orphan_cleanup_failed", pid=proc.pid, error=str(exc))
        return False
    except OSError as exc:
        LOG.warning("chrome_orphan_cleanup_failed", pid=proc.pid, error=str(exc))
        return False
    LOG.info(event, pid=proc.pid)
    return True


def reap_orphans(
    *,
    grace_seconds: float = _GRACE_SECONDS,
    poll_interval: float = _POLL_INTERVAL_S,
    process_table: Callable[[], str] | None = None,
    kill: Callable[[int, int], None] = os.kill,
    pid_alive: Callable[[int], bool] = _pid_alive,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[ChromeProcess]:
    """End every orphaned Chrome, then delete the temp profiles they held.

    SIGTERM first, so Chrome can flush and exit cleanly. Then up to
    *grace_seconds* of polling the table for the ones that are still there,
    and SIGKILL for whatever is left. Killing the browser process takes its
    renderer and GPU helpers with it, because they exit when their parent's
    IPC channel closes; a helper whose browser process is already gone is
    matched by the same rules and handled in the same pass.

    Per-process failures are logged and never raised: this runs on the way
    into a browser start.

    Args:
        grace_seconds: How long to wait for a SIGTERM to be honoured.
        poll_interval: How often to re-read the table while waiting.
        process_table: The table reader, for tests.
        kill: The signal sender, for tests.
        pid_alive: The liveness test, for tests.
        sleep: The wait, for tests.
        monotonic: The clock, for tests. Injected beside *sleep* so a test can
            run the whole grace period without spending it.

    Returns:
        The orphans this pass acted on: every one that received at least one
        signal the kernel accepted. One that had already exited, or that the
        kernel refused, is logged and left out, so the count is what was
        actually cleaned up.
    """
    orphans = find_orphans(list_chrome_processes(process_table), pid_alive=pid_alive)
    if not orphans:
        return []

    acted_on = [
        proc for proc in orphans if _deliver(proc, signal.SIGTERM, kill, "chrome_orphan_terminated")
    ]
    if not acted_on:
        return []

    remaining = list(acted_on)
    deadline = monotonic() + grace_seconds
    while remaining and monotonic() < deadline:
        sleep(poll_interval)
        still_listed = {proc.pid for proc in list_chrome_processes(process_table)}
        remaining = [proc for proc in remaining if proc.pid in still_listed]

    for proc in remaining:
        _deliver(proc, signal.SIGKILL, kill, "chrome_orphan_killed")

    for proc in acted_on:
        path = _removable_temp_profile(proc.user_data_dir)
        if path is None or not path.exists():
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            LOG.info("chrome_orphan_profile_removed", pid=proc.pid, path=str(path))

    return acted_on


def remove_stale_temp_profiles(
    *,
    older_than_seconds: float = _STALE_PROFILE_SECONDS,
    process_table: Callable[[], str] | None = None,
    now: Callable[[], float] = time.time,
) -> list[Path]:
    """Delete ``uc_*`` directories under the temp dir that nothing is using.

    The sweep for the profiles already leaked: directories no live process
    names in its argv, older than *older_than_seconds*. The age guard is what
    keeps a directory another process created moments ago, before its Chrome
    has shown up in ``ps``.

    Args:
        older_than_seconds: How old a directory must be to count as abandoned.
        process_table: The table reader, for tests.
        now: The wall clock, for tests.

    Returns:
        The directories removed.
    """
    table = _process_table(process_table)
    if table is None:
        return []
    temp_root = Path(tempfile.gettempdir())
    try:
        candidates = sorted(
            path
            for path in temp_root.iterdir()
            if path.name.startswith(_TEMP_PROFILE_PREFIX) and path.is_dir()
        )
    except OSError as exc:
        LOG.warning("chrome_orphan_cleanup_failed", pid=None, error=str(exc))
        return []

    removed = []
    for path in candidates:
        if str(path) in table:
            continue
        try:
            age = now() - path.stat().st_mtime
        except OSError as exc:
            LOG.debug("chrome_orphan_cleanup_failed", pid=None, error=str(exc))
            continue
        if age < older_than_seconds:
            continue
        shutil.rmtree(path, ignore_errors=True)
        if path.exists():
            continue
        removed.append(path)
        LOG.debug("chrome_temp_profile_removed", path=str(path))
    return removed


def browser_temp_profile(browser: Any) -> Path | None:
    """The temp profile nodriver created for *browser*, or None if it created none.

    ``Config.uses_custom_data_dir`` is nodriver's own flag for "the caller gave
    us a directory": it is False exactly when nodriver made one itself with
    ``tempfile.mkdtemp(prefix="uc_")``. A custom ``profile_dir`` is therefore
    never reported here and never deleted.

    Args:
        browser: A ``nodriver.Browser``, or anything carrying a ``config``.
    """
    config = getattr(browser, "config", None)
    if config is None:
        return None
    try:
        if config.uses_custom_data_dir:
            return None
        user_data_dir = config.user_data_dir
        return Path(user_data_dir) if user_data_dir else None
    except (AttributeError, TypeError, ValueError, OSError):
        LOG.debug("chrome_temp_profile_lookup_failed", exc_info=True)
        return None


def remove_browser_temp_profile(browser: Any) -> Path | None:
    """Delete the temp profile *browser* was using, if nodriver created it.

    Called right after a browser is stopped. nodriver removes that directory
    only from its ``atexit`` handler, which iterates its registry of live
    browsers; graftpunk deregisters its browser on stop to silence that
    handler's stdout, so without this call nothing ever deletes it and every
    session leaks one directory.

    Returns:
        The directory removed, or None when there was nothing to remove.
    """
    path = browser_temp_profile(browser)
    if path is None or not path.exists():
        return None
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        LOG.debug("chrome_temp_profile_remove_failed", path=str(path))
        return None
    LOG.debug("chrome_temp_profile_removed", path=str(path))
    return path


def cleanup_orphans_before_launch() -> int:
    """Reap orphans and sweep stale temp profiles before a browser starts.

    The one entry point both launch sites call, so the backend and
    ``gp observe`` cannot drift: it applies the opt-out, runs the two sweeps,
    tells the user once when it actually ended something, and never raises. A
    browser start must not fail because a cleanup pass did.

    Settings and the console are imported here rather than at module scope, so
    the module itself keeps depending on nothing but ``graftpunk.logging``,
    which is what lets the backend and the CLI both import it freely.

    Returns:
        How many orphaned processes were ended. 0 when the opt-out is set or
        when anything went wrong.
    """
    from graftpunk import console as gp_console
    from graftpunk.config import get_settings

    try:
        if get_settings().keep_orphaned_chrome:
            LOG.debug("chrome_orphan_cleanup_skipped", reason="opt_out")
            return 0
        orphans = reap_orphans()
        remove_stale_temp_profiles()
    except Exception as exc:  # noqa: BLE001 - cleanup must never fail a browser start
        LOG.warning("chrome_orphan_cleanup_failed", pid=None, error=str(exc))
        return 0

    if orphans:
        gp_console.info(f"Cleaned up {len(orphans)} orphaned Chrome process(es) from earlier runs.")
    return len(orphans)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_chrome_orphans.py -q`

Expected: PASS, 33 tests (2 in `TestOwnerSwitch`, 8 in `TestListChromeProcesses`, 7 in `TestFindOrphans`, 8 in `TestReapOrphans`, 5 in `TestRemoveStaleTempProfiles`, 3 in `TestRealProcesses`).

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`

Expected: all four green. Nothing imports the new module yet, so the rest of the suite is unaffected.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/backends/chrome_orphans.py tests/unit/test_chrome_orphans.py
git commit -m "feat(chrome-orphans): find and reap orphaned nodriver Chrome processes (#96)"
```

> **Design note (2026-09-07):** the spec sketches `reap_orphans(*, grace_seconds=3.0, kill=os.kill, ...)`. The `...` is filled in with `process_table`, `pid_alive`, `sleep` and `monotonic`, all injectable, because the grace wait is otherwise untestable without spending three real seconds per case. `poll_interval` is exposed for the same reason.
>
> **Design note (2026-09-07):** the spec says Windows returns `[]` with one debug log. The guard is on the *default reader* rather than on the function, so an injected table is parsed on any platform and the parsing tests stay meaningful there. The observable contract the spec states is unchanged: with no argument, nothing is scanned and nothing is signalled off-POSIX.
>
> **Design note (2026-09-07):** `cleanup_orphans_before_launch` is not in the spec's function list; the spec has each launch site call `reap_orphans()` and `remove_stale_temp_profiles()` itself. One wrapper is used instead because the opt-out check, the never-raise guard and the user-facing line are identical at both sites and would otherwise be written twice. It imports `graftpunk.config` and `graftpunk.console` inside the function, so the module-scope dependency rule the spec sets ("no graftpunk dependencies beyond `graftpunk.logging`") still holds and no import cycle is possible.

---

### Task 2: the backend marks, sweeps, and stops leaking

**Files:**
- Create: nothing.
- Modify: `src/graftpunk/config.py` (one field after `src/graftpunk/config.py:87` (`    browser_executable_path: str | None = Field(`)), `src/graftpunk/backends/nodriver.py` (imports at `src/graftpunk/backends/nodriver.py:39` (`from graftpunk.logging import get_logger`), `_start_async` at `src/graftpunk/backends/nodriver.py:297` (`    async def _start_async(self, _max_attempts: int = 3) -> None:`), `_stop_async` at `src/graftpunk/backends/nodriver.py:424` (`    async def _stop_async(self) -> None:`), and a new method after `stop_async`)
- Test: `tests/unit/test_nodriver_backend.py` (append), `tests/unit/conftest.py` (one autouse fixture)

**Interfaces:**
- Consumes: `cleanup_orphans_before_launch`, `owner_switch`, `remove_browser_temp_profile` from Task 1.
- Produces:
  - `GraftpunkSettings.keep_orphaned_chrome: bool` (default False, from `GRAFTPUNK_KEEP_ORPHANED_CHROME`), which Task 1's `cleanup_orphans_before_launch` reads.
  - `NoDriverBackend.terminate_browser_process(self) -> None`, which Task 4's `BrowserSession.terminate_browser_process` forwards to.

The `_start_async` retry loop, the `_reap_browser_process` call and the `_deregister_browser` call are all untouched.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_nodriver_backend.py`, extend the mock import at `tests/unit/test_nodriver_backend.py:5` (`from unittest.mock import MagicMock, patch`) to:

```python
from unittest.mock import AsyncMock, MagicMock, patch
```

and add these imports below the existing `from graftpunk.backends.nodriver import NoDriverBackend` at `tests/unit/test_nodriver_backend.py:11`:

```python
import os

from graftpunk.backends.chrome_orphans import OWNER_SWITCH, owner_switch
```

Then append to the file:

```python
class TestNoDriverBackendOrphanCleanup:
    """The start path marks its own Chrome and clears the ones earlier runs left (#96)."""

    @staticmethod
    def _fake_nodriver(record: list) -> MagicMock:
        """A stand-in nodriver module whose start() records what it was given."""
        mock_uc = MagicMock()

        async def fake_start(**kwargs: object) -> MagicMock:
            record.append(kwargs)
            browser = MagicMock()
            browser.get = AsyncMock(return_value=MagicMock())
            return browser

        mock_uc.start = fake_start
        return mock_uc

    async def test_the_owner_marker_rides_in_browser_args(self) -> None:
        started: list = []
        backend = NoDriverBackend()

        with patch.dict("sys.modules", {"nodriver": self._fake_nodriver(started)}):
            await backend._start_async()

        (kwargs,) = started
        assert kwargs["browser_args"] == ["--test-type", f"{OWNER_SWITCH}={os.getpid()}"]

    async def test_configured_browser_args_follow_the_marker(self) -> None:
        started: list = []
        backend = NoDriverBackend(browser_args=["--lang=en-US"])

        with patch.dict("sys.modules", {"nodriver": self._fake_nodriver(started)}):
            await backend._start_async()

        assert started[0]["browser_args"] == ["--test-type", owner_switch(), "--lang=en-US"]

    async def test_the_sweep_runs_before_the_browser_starts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reaping after the launch would leave the new Chrome competing with the old."""
        order: list[str] = []

        def fake_cleanup() -> int:
            order.append("sweep")
            return 0

        monkeypatch.setattr(
            "graftpunk.backends.nodriver.cleanup_orphans_before_launch", fake_cleanup
        )
        mock_uc = MagicMock()

        async def fake_start(**kwargs: object) -> MagicMock:
            order.append("start")
            browser = MagicMock()
            browser.get = AsyncMock(return_value=MagicMock())
            return browser

        mock_uc.start = fake_start
        backend = NoDriverBackend()

        with patch.dict("sys.modules", {"nodriver": mock_uc}):
            await backend._start_async()

        assert order == ["sweep", "start"]


class TestNoDriverBackendTempProfileCleanup:
    """Every stop used to leak the temp profile nodriver made for it (#96)."""

    @staticmethod
    def _browser(profile: Path | None, *, custom: bool) -> MagicMock:
        browser = MagicMock()
        browser._process = None
        browser.config.uses_custom_data_dir = custom
        browser.config.user_data_dir = str(profile) if profile is not None else None
        browser.stop = MagicMock()
        return browser

    async def test_a_temp_profile_is_removed_on_stop(self, tmp_path: Path) -> None:
        profile = tmp_path / "uc_abc"
        (profile / "Default").mkdir(parents=True)
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = self._browser(profile, custom=False)

        await backend._stop_async()

        assert not profile.exists()

    async def test_a_custom_profile_dir_survives_stop(self, tmp_path: Path) -> None:
        """A plugin's persistent profile is the user's data, never ours to delete."""
        profile = tmp_path / "persistent-profile"
        profile.mkdir()
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = self._browser(profile, custom=True)

        await backend._stop_async()

        assert profile.exists()

    async def test_a_browser_with_no_profile_recorded_is_harmless(self) -> None:
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = self._browser(None, custom=False)

        await backend._stop_async()


class TestNoDriverBackendTerminateBrowserProcess:
    """The loop-free path a signal handler can take (#96)."""

    @staticmethod
    def _browser(profile: Path, pid: int = 4242) -> MagicMock:
        browser = MagicMock()
        browser._process = MagicMock()
        browser._process.pid = pid
        browser.config.uses_custom_data_dir = False
        browser.config.user_data_dir = str(profile)
        return browser

    def test_it_signals_the_subprocess_and_removes_the_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # monkeypatch resolves this to the real os module and restores it after
        # the test; nothing else in this worker sends a signal meanwhile.
        sent: list[tuple[int, int]] = []
        monkeypatch.setattr(
            "graftpunk.backends.nodriver.os.kill",
            lambda pid, signum: sent.append((pid, signum)),
        )
        profile = tmp_path / "uc_live"
        profile.mkdir()
        backend = NoDriverBackend()
        backend._browser = self._browser(profile)

        backend.terminate_browser_process()

        assert sent == [(4242, signal.SIGTERM)]
        assert not profile.exists()

    def test_a_process_that_already_exited_is_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def gone(pid: int, signum: int) -> None:
            raise ProcessLookupError(3, "No such process")

        monkeypatch.setattr("graftpunk.backends.nodriver.os.kill", gone)
        profile = tmp_path / "uc_live"
        profile.mkdir()
        backend = NoDriverBackend()
        backend._browser = self._browser(profile)

        backend.terminate_browser_process()

        assert not profile.exists()

    def test_no_browser_is_a_no_op(self) -> None:
        backend = NoDriverBackend()

        backend.terminate_browser_process()


class TestKeepOrphanedChromeSetting:
    """The opt-out, for users who deliberately keep detached browsers (#96)."""

    def test_the_default_is_to_clean_up(self) -> None:
        from graftpunk.config import get_settings

        assert get_settings().keep_orphaned_chrome is False

    def test_the_environment_variable_turns_it_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from graftpunk.config import get_settings, reset_settings

        monkeypatch.setenv("GRAFTPUNK_KEEP_ORPHANED_CHROME", "1")
        reset_settings()

        assert get_settings().keep_orphaned_chrome is True
```

Add `import signal` and `from pathlib import Path` to the file's imports if they are not already there. `Path` is imported at `tests/unit/test_nodriver_backend.py:4` (`from pathlib import Path`); `signal` is not, so add `import signal` beside `import asyncio` at `tests/unit/test_nodriver_backend.py:3` (`import asyncio`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_nodriver_backend.py -q -k "OrphanCleanup or TempProfileCleanup or TerminateBrowserProcess or KeepOrphanedChrome"`

Expected: FAIL.
- `test_the_owner_marker_rides_in_browser_args`: `AssertionError: assert ['--test-type'] == ['--test-type', '--graftpunk-owner-pid=NNNNN']`.
- `test_the_sweep_runs_before_the_browser_starts`: `AttributeError: <module 'graftpunk.backends.nodriver'> does not have the attribute 'cleanup_orphans_before_launch'`.
- `test_a_temp_profile_is_removed_on_stop`: `AssertionError: assert not True` (the directory is still there).
- `TestNoDriverBackendTerminateBrowserProcess`: `AttributeError: 'NoDriverBackend' object has no attribute 'terminate_browser_process'`.
- `TestKeepOrphanedChromeSetting`: `AttributeError: 'GraftpunkSettings' object has no attribute 'keep_orphaned_chrome'`.

- [ ] **Step 3: Write the implementation**

First, in `src/graftpunk/config.py`, add the field directly after the `browser_executable_path` field's closing `)` (the block that starts at `src/graftpunk/config.py:87` (`    browser_executable_path: str | None = Field(`)) and before `model_config`:

```python
    keep_orphaned_chrome: bool = Field(
        default=False,
        description=(
            "Keep the Chrome processes earlier runs left behind instead of ending them "
            "before a nodriver browser starts. Set GRAFTPUNK_KEEP_ORPHANED_CHROME=1 when "
            "you deliberately leave detached browsers running."
        ),
    )
```

Next, in `src/graftpunk/backends/nodriver.py`, replace the import block at `src/graftpunk/backends/nodriver.py:29` (`import asyncio`) through `src/graftpunk/backends/nodriver.py:39` (`from graftpunk.logging import get_logger`) with:

```python
import asyncio
import io
import os
import signal
import sys
from collections.abc import Coroutine
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from graftpunk.backends.base import Cookie
from graftpunk.backends.chrome_orphans import (
    cleanup_orphans_before_launch,
    owner_switch,
    remove_browser_temp_profile,
)
from graftpunk.exceptions import BrowserError
from graftpunk.logging import get_logger
```

In `_start_async`, replace `src/graftpunk/backends/nodriver.py:312` (`        _patch_nodriver_cookie_parsing()`) through `src/graftpunk/backends/nodriver.py:315` (`        browser_args = ["--test-type"]`) with:

```python
        _patch_nodriver_cookie_parsing()

        # End the Chromes earlier runs left behind before adding one more, and
        # sweep the temp profiles nodriver never removed (#96). Bounded by the
        # reaper's grace period, and it never raises into the start path.
        cleanup_orphans_before_launch()

        # --test-type suppresses Chrome's "unsupported flag" warning banner.
        # The owner marker rides in the same list and survives into Chrome's
        # command line, so a later run can tell this browser apart from the
        # user's own (#96).
        browser_args = ["--test-type", owner_switch()]
```

In `_stop_async`, replace `src/graftpunk/backends/nodriver.py:438` (`            proc = getattr(self._browser, "_process", None)`) with:

```python
            proc = getattr(self._browser, "_process", None)
            # The same defensive capture for the profile: read it while the
            # browser object is still the one we started.
            browser = self._browser
```

and replace `src/graftpunk/backends/nodriver.py:463` (`                await _reap_browser_process(proc)`) with:

```python
                await _reap_browser_process(proc)
                # nodriver deletes its temp profile only from the atexit
                # handler that iterates the registry _deregister_browser just
                # removed us from, so without this every stop leaks one
                # directory under the temp dir (#96). A custom profile_dir is
                # never touched.
                remove_browser_temp_profile(browser)
```

Finally, add this method immediately after `stop_async` ends, before the `is_running` property at `src/graftpunk/backends/nodriver.py:502` (`    def is_running(self) -> bool:`) and its `@property` decorator:

```python
    def terminate_browser_process(self) -> None:
        """Send SIGTERM to this backend's Chrome and delete its temp profile.

        The loop-free path, for a signal handler. ``stop()`` runs
        ``asyncio.run()`` and ``stop_async()`` needs a running loop; a signal
        handler has neither, so this touches only the subprocess pid and the
        profile directory. It never raises: the caller is on its way to the
        default signal action and has nowhere to put an exception.
        """
        browser = self._browser
        if browser is None:
            return
        pid = getattr(getattr(browser, "_process", None), "pid", None)
        if isinstance(pid, int):
            try:
                os.kill(pid, signal.SIGTERM)
                LOG.debug("chrome_process_terminated", pid=pid)
            except ProcessLookupError:
                LOG.debug("chrome_process_already_gone", pid=pid)
            except OSError as exc:
                LOG.debug("chrome_process_terminate_failed", pid=pid, error=str(exc))
        remove_browser_temp_profile(browser)
```

Then add the guard fixture to `tests/unit/conftest.py`, directly after the `_wide_consoles` fixture ends at `tests/unit/conftest.py:32` (`        monkeypatch.setattr(console, "width", 220)`):

```python
@pytest.fixture(autouse=True)
def _no_real_process_reaping(monkeypatch):  # noqa: ANN001, ANN201
    """No unit test may signal a process it did not spawn.

    The browser launch sites call cleanup_orphans_before_launch(), which reads
    the real process table and sends real signals. Tests drive those paths for
    real, so without this they would end this developer's own browsers. The
    reaper's own tests call reap_orphans() and its neighbours directly, with an
    injected table, so they are unaffected.
    """
    monkeypatch.setattr("graftpunk.backends.nodriver.cleanup_orphans_before_launch", lambda: 0)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_nodriver_backend.py tests/unit/test_session.py -q`

Expected: PASS, including the 11 new tests and the whole existing `TestNoDriverBackendStopReap` class (`tests/unit/test_nodriver_backend.py:955` (`class TestNoDriverBackendStopReap:`)), whose `MagicMock` browsers report a truthy `uses_custom_data_dir` and so are read as custom profiles and left alone.

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`

Expected: all four green.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/config.py src/graftpunk/backends/nodriver.py tests/unit/conftest.py tests/unit/test_nodriver_backend.py
git commit -m "fix(nodriver): mark the browser, sweep orphans, and stop leaking the temp profile (#96)"
```

> **Design note (2026-09-07):** the task brief places these tests in `tests/unit/test_backends.py`, following "that file's existing nodriver patching style". That file has no nodriver patching: its only nodriver reference is one `ImportError` test at `tests/unit/test_backends.py:865` (`            mock_import.side_effect = ImportError("No module named 'nodriver'")`). The style the brief describes lives in `tests/unit/test_nodriver_backend.py`, which is where every `NoDriverBackend` test and the `_make_browser` fake at `tests/unit/test_nodriver_backend.py:1004` (`    def _make_browser(proc: MagicMock | None) -> MagicMock:`) already are, so the new tests go there.
>
> **Design note (2026-09-07):** the guard fixture lands with the implementation rather than with the failing tests. Added a step earlier, its `monkeypatch.setattr` would raise `AttributeError` for every unit test in the suite, not just the ones under test, and the failing-test run would say nothing useful.

---

### Task 3: the `gp observe` launch site

**Files:**
- Modify: `src/graftpunk/cli/main.py` (the import block after `src/graftpunk/cli/main.py:54` (`from graftpunk.session_context import resolve_session`), the launch at `src/graftpunk/cli/main.py:524` (`                browser = await nodriver.start(`), and the three `browser.stop()` calls), `tests/unit/conftest.py` (extend the guard fixture)
- Test: `tests/unit/test_observe_interactive.py` (append)

**Interfaces:**
- Consumes: `cleanup_orphans_before_launch`, `owner_switch`, `remove_browser_temp_profile` from Task 1.
- Produces: `_stop_observe_browser(browser: Any) -> None` in `graftpunk.cli.main`, the single stop path for the observe browser. Nothing outside this module calls it.

`gp observe` calls `nodriver.start` directly rather than going through `NoDriverBackend` (`src/graftpunk/cli/main.py:480` (`    import nodriver`)). Routing it through the backend is a larger refactor and out of scope, so it calls the same three helpers.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_observe_interactive.py`:

```python
class TestObserveBrowserHygiene:
    """gp observe marks its Chrome and cleans up the profile it created (#96)."""

    @staticmethod
    def _browser(profile: Path | None) -> MagicMock:
        """A fake browser whose config says nodriver made the profile directory."""
        tab = MagicMock()
        tab.sleep = AsyncMock()
        browser = MagicMock()
        browser.main_tab = tab
        browser.get = AsyncMock(return_value=tab)
        browser.config.uses_custom_data_dir = False
        browser.config.user_data_dir = str(profile) if profile is not None else None
        return browser

    @staticmethod
    def _capture_backend() -> MagicMock:
        backend = MagicMock()
        backend.start_capture_async = AsyncMock()
        return backend

    @pytest.mark.asyncio
    async def test_the_owner_marker_reaches_nodriver_start(self, tmp_path: Path) -> None:
        from graftpunk.backends.chrome_orphans import owner_switch
        from graftpunk.cli.main import _setup_observe_session

        started: list = []

        async def fake_start(**kwargs: object) -> MagicMock:
            started.append(kwargs)
            return self._browser(None)

        mock_nodriver = MagicMock()
        mock_nodriver.start = fake_start
        storage = MagicMock()
        storage.run_dir = tmp_path / "run"

        with (
            patch.dict("sys.modules", {"nodriver": mock_nodriver}),
            patch(
                "graftpunk.observe.capture.NodriverCaptureBackend",
                return_value=self._capture_backend(),
            ),
            patch("graftpunk.observe.storage.ObserveStorage", return_value=storage),
        ):
            await _setup_observe_session(
                "test-ns", "https://example.com", 5 * 1024 * 1024, headless=True, session_name=None
            )

        (kwargs,) = started
        assert kwargs["browser_args"] == ["--test-type", owner_switch()]

    @pytest.mark.asyncio
    async def test_the_sweep_runs_before_the_browser_starts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk.cli.main import _setup_observe_session

        order: list[str] = []

        def fake_cleanup() -> int:
            order.append("sweep")
            return 0

        monkeypatch.setattr("graftpunk.cli.main.cleanup_orphans_before_launch", fake_cleanup)

        async def fake_start(**kwargs: object) -> MagicMock:
            order.append("start")
            return self._browser(None)

        mock_nodriver = MagicMock()
        mock_nodriver.start = fake_start
        storage = MagicMock()
        storage.run_dir = tmp_path / "run"

        with (
            patch.dict("sys.modules", {"nodriver": mock_nodriver}),
            patch(
                "graftpunk.observe.capture.NodriverCaptureBackend",
                return_value=self._capture_backend(),
            ),
            patch("graftpunk.observe.storage.ObserveStorage", return_value=storage),
        ):
            await _setup_observe_session(
                "test-ns", "https://example.com", 5 * 1024 * 1024, headless=True, session_name=None
            )

        assert order == ["sweep", "start"]

    @pytest.mark.asyncio
    async def test_the_temp_profile_is_removed_after_the_browser_stops(
        self, tmp_path: Path
    ) -> None:
        """The leak fix at the second launch site: observe made the directory too."""
        from graftpunk.cli.main import _run_observe_go

        profile = tmp_path / "uc_observe"
        (profile / "Default").mkdir(parents=True)
        mock_nodriver = MagicMock()
        mock_nodriver.start = AsyncMock(return_value=self._browser(profile))
        storage = MagicMock()
        storage.run_dir = tmp_path / "run"

        with (
            patch.dict("sys.modules", {"nodriver": mock_nodriver}),
            patch(
                "graftpunk.observe.capture.NodriverCaptureBackend",
                return_value=self._capture_backend(),
            ),
            patch("graftpunk.observe.storage.ObserveStorage", return_value=storage),
            patch("graftpunk.cli.main.save_observe_run", new_callable=AsyncMock),
        ):
            await _run_observe_go(
                "test-ns", "https://example.com", 0.0, 5 * 1024 * 1024, session_name=None
            )

        assert not profile.exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_observe_interactive.py -q -k "BrowserHygiene"`

Expected: FAIL.
- `test_the_owner_marker_reaches_nodriver_start`: `AssertionError: assert ['--test-type'] == ['--test-type', '--graftpunk-owner-pid=NNNNN']`.
- `test_the_sweep_runs_before_the_browser_starts`: `AttributeError: <module 'graftpunk.cli.main'> does not have the attribute 'cleanup_orphans_before_launch'`.
- `test_the_temp_profile_is_removed_after_the_browser_stops`: `AssertionError: assert not True`.

- [ ] **Step 3: Write the implementation**

In `src/graftpunk/cli/main.py`, add the import between `src/graftpunk/cli/main.py:30` (`from graftpunk import workstation_env`) and `src/graftpunk/cli/main.py:31` (`from graftpunk.cli.config_commands import config_app`), which is where import sorting puts `graftpunk.backends.chrome_orphans`:

```python
from graftpunk.backends.chrome_orphans import (
    cleanup_orphans_before_launch,
    owner_switch,
    remove_browser_temp_profile,
)
```

This module imports only the standard library and `graftpunk.logging` at module scope, so it adds nothing to CLI start-up cost and cannot cycle.

Add this helper immediately above `_setup_observe_session` (`src/graftpunk/cli/main.py:454` (`async def _setup_observe_session(`)):

```python
def _stop_observe_browser(browser: Any) -> None:
    """Stop the observe browser and delete the temp profile nodriver made for it.

    nodriver removes that directory only from its own atexit handler, which
    never runs for a browser we stopped ourselves, so every observe run used
    to leak one directory under the temp dir (#96).
    """
    browser.stop()
    remove_browser_temp_profile(browser)
```

In `_setup_observe_session`, insert the sweep and change the launch arguments. Replace `src/graftpunk/cli/main.py:515` (`    # Isolate Chrome from SIGINT: child process inherits SIG_IGN disposition,`) through `src/graftpunk/cli/main.py:527` (`                    browser_args=["--test-type"],`) with:

```python
    # End the Chromes earlier runs left behind before adding one more (#96).
    # gp observe launches nodriver directly rather than through
    # NoDriverBackend, so it calls the same helpers the backend does.
    cleanup_orphans_before_launch()

    # Isolate Chrome from SIGINT: child process inherits SIG_IGN disposition,
    # so Ctrl+C only reaches Python. We save data, then explicitly stop Chrome.
    old_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        # Retry logic matching NoDriverBackend._start_async()
        max_attempts = 3
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                browser = await nodriver.start(
                    headless=headless,
                    sandbox=False,
                    browser_args=["--test-type", owner_switch()],
                )
```

Then replace all three `browser.stop()` calls with `_stop_observe_browser(browser)`:

- the failure path inside `_setup_observe_session`, at `src/graftpunk/cli/main.py:575` (`    except Exception:`) plus the line after it (line 576);
- the `finally` of `_run_observe_go`, line 596, two lines below `src/graftpunk/cli/main.py:594` (`        await save_observe_run(storage, backend, "observe-go", console=console)`);
- the `finally` of `_run_observe_interactive`, line 638, two lines below `src/graftpunk/cli/main.py:636` (`        await save_observe_run(storage, backend, "interactive-final", console=console)`).

After the edits those three blocks read:

```python
    except Exception:
        _stop_observe_browser(browser)
        raise
```

```python
    try:
        await tab.sleep(wait)
        await save_observe_run(storage, backend, "observe-go", console=console)
    finally:
        _stop_observe_browser(browser)
```

```python
        console.print("\n[dim]Recording stopped. Saving capture...[/dim]")
        await save_observe_run(storage, backend, "interactive-final", console=console)
    finally:
        _stop_observe_browser(browser)
```

Finally, extend the guard fixture in `tests/unit/conftest.py` so it covers this launch site too. Replace its `monkeypatch.setattr(...)` call with:

```python
    for module in ("graftpunk.backends.nodriver", "graftpunk.cli.main"):
        monkeypatch.setattr(f"{module}.cleanup_orphans_before_launch", lambda: 0)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_observe_interactive.py tests/unit/test_observe_session_naming.py tests/unit/test_cli.py -q`

Expected: PASS, including the 3 new tests and the existing SIGINT-isolation test at `tests/unit/test_observe_interactive.py:405` (`            captured_handler.append(signal.getsignal(signal.SIGINT))`), which still sees `SIG_IGN` during `nodriver.start` because the sweep runs before that block, not inside it.

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`

Expected: all four green.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/cli/main.py tests/unit/conftest.py tests/unit/test_observe_interactive.py
git commit -m "fix(observe): mark and clean up the browser gp observe launches (#96)"
```

> **Design note (2026-09-07):** the spec says to remove the temp profile "after its `browser.stop()`". There are three such calls in this module, so they become one `_stop_observe_browser` helper rather than three copies of the same two lines. The failure path inside `_setup_observe_session` is included deliberately: a browser that failed on the way up leaked its profile exactly like one that succeeded.

---

### Task 4: SIGTERM and SIGHUP end the browsers too

**Files:**
- Create: `src/graftpunk/cli/signals.py`
- Modify: `src/graftpunk/session.py` (module state after the imports, the nodriver branch of `__init__`, `quit`, `_quit_async`, and one new method), `src/graftpunk/cli/main.py` (`main_callback`), `tests/unit/conftest.py` (one autouse fixture)
- Test: `tests/unit/test_termination_signals.py` (new)

**Interfaces:**
- Consumes: `NoDriverBackend.terminate_browser_process()` from Task 2.
- Produces:
  - `graftpunk.session._LIVE_SESSIONS: weakref.WeakSet["BrowserSession"]` and `graftpunk.session.live_sessions() -> list["BrowserSession"]`.
  - `BrowserSession.terminate_browser_process(self) -> None`.
  - `graftpunk.cli.signals.install_termination_cleanup() -> None` and the module-private `_handle_termination(signum, frame)`.

SIGKILL and the OOM killer stay uncatchable; Task 1 is their backstop.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_termination_signals.py`:

```python
"""SIGTERM and SIGHUP end the browsers this process started (#96)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import weakref
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from graftpunk.cli.signals import _handle_termination, install_termination_cleanup
from graftpunk.session import live_sessions


def _nodriver_session():  # noqa: ANN202
    """A BrowserSession on the nodriver backend, with no browser behind it."""
    from graftpunk.session import BrowserSession

    with (
        patch("graftpunk.backends.get_backend", return_value=MagicMock()),
        patch("graftpunk.backends.list_backends", return_value=["selenium", "nodriver"]),
        patch("requests.Session.__init__"),
    ):
        return BrowserSession(backend="nodriver")


class TestLiveSessionRegistry:
    """The registry a signal handler reads to find the browsers to end."""

    def test_a_nodriver_session_registers_itself(self) -> None:
        session = _nodriver_session()

        assert session in live_sessions()

    def test_quit_deregisters(self) -> None:
        session = _nodriver_session()

        session.quit()

        assert session not in live_sessions()

    async def test_quit_async_deregisters(self) -> None:
        session = _nodriver_session()

        await session._quit_async()

        assert session not in live_sessions()

    def test_a_selenium_session_is_not_registered(self) -> None:
        """Only the nodriver backend leaves a subprocess behind to end."""
        from graftpunk.session import BrowserSession

        with (
            patch("graftpunk.backends.list_backends", return_value=["selenium", "nodriver"]),
            patch("graftpunk.stealth.create_stealth_driver", return_value=MagicMock()),
            patch("requestium.Session.__init__"),
        ):
            session = BrowserSession(backend="selenium", use_stealth=True)

        assert session not in live_sessions()

    def test_the_registry_holds_only_weak_references(self) -> None:
        """A forgotten session must not be kept alive by the registry."""
        import gc

        session = _nodriver_session()
        ref = weakref.ref(session)

        del session
        gc.collect()

        assert ref() is None


class TestTerminateBrowserProcess:
    """The session forwards to the backend, and only for nodriver."""

    def test_it_forwards_to_the_nodriver_backend(self) -> None:
        session = _nodriver_session()
        backend = session._backend_instance
        calls: list[str] = []
        backend.terminate_browser_process = lambda: calls.append("terminated")

        session.terminate_browser_process()

        assert calls == ["terminated"]

    def test_a_session_with_no_backend_is_a_no_op(self) -> None:
        session = _nodriver_session()
        session._backend_instance = None

        session.terminate_browser_process()


class TestInstallTerminationCleanup:
    """Which signal slots the CLI is allowed to take."""

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


CHILD_SCRIPT = '''
import sys
import time

from graftpunk.cli.signals import install_termination_cleanup
from graftpunk.session import _LIVE_SESSIONS

marker = sys.argv[1]
ready = sys.argv[2]


class FakeSession:
    """Stands in for a BrowserSession: it records that it was asked to stop."""

    def terminate_browser_process(self):
        with open(marker, "w") as handle:
            handle.write("terminated")


session = FakeSession()
_LIVE_SESSIONS.add(session)
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_termination_signals.py -q`

Expected: collection error, `ModuleNotFoundError: No module named 'graftpunk.cli.signals'`.

- [ ] **Step 3: Write the implementation**

First, in `src/graftpunk/session.py`, add the `weakref` import to the standard-library block, replacing `src/graftpunk/session.py:23` (`import asyncio`) through `src/graftpunk/session.py:25` (`from typing import Any, cast`) with:

```python
import asyncio
import weakref
from pathlib import Path
from typing import Any, cast
```

Add the registry and its accessor directly above `class BrowserSession(requestium.Session):` (`src/graftpunk/session.py:73`):

```python
# Every live nodriver-backed session, weakly held. A signal handler needs to
# find the browsers this process started without any of them being kept alive
# by the lookup itself (#96). Selenium sessions are not registered: their
# WebDriver is a child of chromedriver, not of this process.
_LIVE_SESSIONS: weakref.WeakSet["BrowserSession"] = weakref.WeakSet()


def live_sessions() -> list["BrowserSession"]:
    """A snapshot of the browser sessions that have not been quit yet.

    A snapshot rather than the set itself: a signal handler must not iterate a
    WeakSet that the garbage collector may mutate underneath it.
    """
    return list(_LIVE_SESSIONS)
```

In `__init__`, register the session at the end of the nodriver branch by replacing `src/graftpunk/session.py:171` (`                LOG.info("nodriver_browser_session_initialized", headless=headless)`) with:

```python
                _LIVE_SESSIONS.add(self)
                LOG.info("nodriver_browser_session_initialized", headless=headless)
```

In `quit` (`src/graftpunk/session.py:536` (`    def quit(self) -> None:`)), insert the deregistration as the first statement of the body, on line 546, so the two lines read:

```python
        _LIVE_SESSIONS.discard(self)
        backend_instance = getattr(self, "_backend_instance", None)
```

Make the same insertion as the first statement of `_quit_async`'s body (`src/graftpunk/session.py:561` (`    async def _quit_async(self) -> None:`)), on line 567, so those two lines read the same way:

```python
        _LIVE_SESSIONS.discard(self)
        backend_instance = getattr(self, "_backend_instance", None)
```

Add this method directly after `_quit_async` ends, before `src/graftpunk/session.py:586` (`    def __getstate__(self) -> dict[str, Any]:`):

```python
    def terminate_browser_process(self) -> None:
        """End the browser subprocess without touching the event loop.

        The path a signal handler takes: ``quit()`` runs ``asyncio.run()``
        through the backend and ``_quit_async`` needs a running loop, neither
        of which a signal handler has. This sends one SIGTERM and deletes the
        temp profile, and does nothing at all for a selenium session, whose
        driver is chromedriver's child rather than this process's.
        """
        if getattr(self, "_backend_type", "selenium") != "nodriver":
            return
        backend_instance = getattr(self, "_backend_instance", None)
        terminate = getattr(backend_instance, "terminate_browser_process", None)
        if terminate is None:
            return
        terminate()
```

Next, create `src/graftpunk/cli/signals.py`:

```python
"""Termination signals end the browsers this process started (#96).

``BrowserSession.__exit__``, ``quit()`` and nodriver's own ``atexit`` handler
all run only when Python exits normally. A SIGTERM from a supervisor, or the
SIGHUP a closing terminal sends, kills Python where it stands and leaves
Chrome running. This module installs the handlers that close that gap.

Only the CLI installs them. Importing graftpunk as a library installs nothing:
a host program owns its own signal dispositions, and a slot that is not
``SIG_DFL`` is left exactly as it was found. SIGKILL and the OOM killer stay
uncatchable, which is why the orphan reaper exists
(:mod:`graftpunk.backends.chrome_orphans`).
"""

from __future__ import annotations

import os
import signal
from types import FrameType

from graftpunk.logging import get_logger

LOG = get_logger(__name__)

_HANDLED_SIGNAL_NAMES = ("SIGTERM", "SIGHUP")


def _handle_termination(signum: int, _frame: FrameType | None) -> None:
    """End every live browser, then die of the signal that arrived.

    Restoring ``SIG_DFL`` and re-raising, rather than calling ``sys.exit``,
    keeps the exit status the conventional 128 + signum, so a supervisor
    watching this process still sees the real cause of death.

    Every per-session call is wrapped: one browser that will not go must not
    stop the others from being asked, and must not stall the process on its
    way out.
    """
    from graftpunk.session import live_sessions

    name = signal.Signals(signum).name
    for session in live_sessions():
        try:
            session.terminate_browser_process()
        except Exception as exc:  # noqa: BLE001 - a handler has nowhere to raise
            LOG.warning("termination_cleanup_failed", signal=name, error=str(exc))
    LOG.info("termination_signal_received", signal=name)
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def install_termination_cleanup() -> None:
    """Take the SIGTERM and SIGHUP slots, if nobody else has them.

    Idempotent: after the first call the slots no longer hold ``SIG_DFL``, so
    a second call finds them taken and leaves them alone. Signal dispositions
    can only be set from the main thread, and not every platform has SIGHUP,
    so both cases are debug logs rather than failures.
    """
    for name in _HANDLED_SIGNAL_NAMES:
        signum = getattr(signal, name, None)
        if signum is None:
            LOG.debug("termination_handler_not_installed", signal=name, reason="no_such_signal")
            continue
        try:
            current = signal.getsignal(signum)
        except (OSError, ValueError) as exc:
            LOG.debug("termination_handler_not_installed", signal=name, reason=str(exc))
            continue
        if current is not signal.SIG_DFL:
            LOG.debug("termination_handler_not_installed", signal=name, reason="slot_in_use")
            continue
        try:
            signal.signal(signum, _handle_termination)
        except (OSError, ValueError) as exc:
            LOG.debug("termination_handler_not_installed", signal=name, reason=str(exc))
```

Then wire it into the CLI. In `src/graftpunk/cli/main.py`, add the import beside the other `graftpunk.cli` imports, directly below `src/graftpunk/cli/main.py:35` (`from graftpunk.cli.session_commands import session_app`):

```python
from graftpunk.cli.signals import install_termination_cleanup
```

and call it at the end of `main_callback`, replacing `src/graftpunk/cli/main.py:171` (`    ctx.ensure_object(dict)["observe_mode"] = observe.value`) with:

```python
    # A supervisor's SIGTERM or a closing terminal's SIGHUP kills Python where
    # it stands; without this, the browser it launched survives (#96).
    install_termination_cleanup()

    ctx.ensure_object(dict)["observe_mode"] = observe.value
```

Finally, add this fixture to `tests/unit/conftest.py`, below `_no_real_process_reaping`:

```python
@pytest.fixture(autouse=True)
def _restore_termination_signals():  # noqa: ANN201
    """Give each test the SIGTERM and SIGHUP slots back.

    Every CliRunner invocation runs main_callback, which installs the
    termination cleanup in this worker process. Left in place, one test's
    handler becomes ambient state for the rest of the run and the signal tests
    cannot tell an install apart from a leftover.
    """
    import signal

    saved = {}
    for name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is not None:
            saved[signum] = signal.getsignal(signum)
    yield
    for signum, handler in saved.items():
        if handler is not None:
            signal.signal(signum, handler)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_termination_signals.py tests/unit/test_session.py tests/unit/test_cli.py -q`

Expected: PASS, 11 new tests (5 in `TestLiveSessionRegistry`, 2 in `TestTerminateBrowserProcess`, 3 in `TestInstallTerminationCleanup`, and the subprocess test).

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`

Expected: all four green.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/cli/signals.py src/graftpunk/session.py src/graftpunk/cli/main.py tests/unit/conftest.py tests/unit/test_termination_signals.py
git commit -m "feat(cli): SIGTERM and SIGHUP end the browsers this process started (#96)"
```

> **Design note (2026-09-07):** the spec names only `_LIVE_SESSIONS`. `live_sessions()` is added beside it because a signal handler must not iterate a `WeakSet` the collector may mutate; the handler and the tests both read the snapshot, and nothing outside `session.py` touches the set except the child script in the subprocess test, which registers a fake into it deliberately.

---

### Task 5: documentation and changelog

**Files:**
- Modify: `docs/HOW_IT_WORKS.md` (a new subsection at the end of the "Browser Backends" section), `README.md` (one row in the Configuration table), `CHANGELOG.md` (a new `[Unreleased]` section)
- Test: none. This task is prose; the gate still runs.

**Interfaces:**
- Consumes: everything Tasks 1 to 4 built. No new code.
- Produces: nothing other tasks depend on.

- [ ] **Step 1: Add the "Browser process hygiene" subsection**

In `docs/HOW_IT_WORKS.md`, insert this at the end of the `### BrowserSession` subsection (`docs/HOW_IT_WORKS.md:43` (`### BrowserSession`)), after its last paragraph (the one beginning `**Serialization:**`, line 77) and before the `---` that closes the "Browser Backends" section on line 79. It sits with the backend material because everything in it is about the browser subprocess:

```markdown
### Browser process hygiene

The nodriver backend leaves a Chrome subprocess behind whenever the Python process that launched it dies without running its cleanup: SIGKILL, an OOM kill, a closed terminal, a crash. graftpunk handles that in three places.

**A marker on every browser.** Each launch passes `--graftpunk-owner-pid=<pid>` in `browser_args`, beside the `--test-type` switch that suppresses Chrome's unsupported-flag banner. Chrome ignores switches it does not recognise, and the marker survives into the command line that `ps` reports, so a later run can tell graftpunk's own browsers apart from the user's and knows which process owned each one.

**A sweep before each start.** Before `nodriver.start`, both launch sites (the `NoDriverBackend` and `gp observe`) scan `ps` for processes carrying `--remote-debugging-port`. One whose marker names a pid that is no longer alive is ended with SIGTERM, then SIGKILL after a three second grace period. A narrower second rule covers browsers launched before the marker existed: no marker, a nodriver temp profile (`uc_*`) as its `--user-data-dir`, and a parent of pid 1. A browser without a debugging port, and any browser whose owner is still alive, is never touched. The same pass then deletes `uc_*` directories under the temp directory that no live process references and that are more than an hour old. Set `GRAFTPUNK_KEEP_ORPHANED_CHROME=1` to skip all of it, for a workflow that deliberately leaves detached browsers running.

**A profile deleted on every stop.** nodriver creates its temp profile with `tempfile.mkdtemp(prefix="uc_")` and deletes it only from an `atexit` handler that iterates its registry of live browsers. graftpunk removes its browser from that registry on stop, to keep that handler's output off stdout, so nothing used to delete the directory and every session leaked one. Both stop paths now delete it themselves. A custom `profile_dir` is never touched: nodriver's own `uses_custom_data_dir` flag says which is which.

**Signals.** `gp` installs SIGTERM and SIGHUP handlers that terminate every live browser, restore the default disposition, and re-raise the signal, so the exit status stays the conventional 128 + signum. A slot a host program already handles is left alone, and importing graftpunk as a library installs nothing: call `graftpunk.cli.signals.install_termination_cleanup()` to opt in. SIGKILL and the OOM killer cannot be caught, which is what the start-time sweep is for.

Chrome shares the Python process group. nodriver launches it with `asyncio.create_subprocess_exec` and passes no `start_new_session`, so the child inherits its parent's session and process group: a terminal's SIGHUP and `kill -- -<pgid>` reach Chrome without any process-group handling in graftpunk.
```

- [ ] **Step 2: Add the environment variable to the README table**

In `README.md`, add this row directly below `README.md:396` (`| \`GRAFTPUNK_BROWSER_EXECUTABLE_PATH\` | _(system Chrome)_ | Path to a Chrome/Chromium binary for the \`nodriver\` backend (e.g. Chrome-for-Testing on machines/CI without a system Chrome install) |`):

```markdown
| `GRAFTPUNK_KEEP_ORPHANED_CHROME` | _(unset)_ | Set to `1` to keep the Chrome processes earlier runs left behind, instead of ending them before a `nodriver` browser starts |
```

- [ ] **Step 3: Add the `[Unreleased]` changelog section**

`CHANGELOG.md` has no `[Unreleased]` heading: 1.16.0 was released on 2026-09-07 and its section is the first one in the file. Insert this immediately above `CHANGELOG.md:8` (`## [1.16.0] - 2026-09-07`):

```markdown
## [Unreleased]

### Added

- **Orphaned Chrome cleanup** ([#96](https://github.com/stavxyz/graftpunk/issues/96)). When the Python process that launched a `nodriver` browser dies without running its cleanup (SIGKILL, an OOM kill, a closed terminal, a crash), Chrome survives and keeps its debug port and its memory. Every graftpunk launch now passes `--graftpunk-owner-pid=<pid>` in `browser_args`, which Chrome ignores and `ps` reports, and every launch first ends the browsers whose owner is gone: SIGTERM, then SIGKILL after a three second grace period. A narrower rule covers browsers launched before the marker existed (no marker, a `uc_*` temp profile, and a parent of pid 1). A browser without a debugging port, and any browser whose owner is still alive, is never touched. The pass also deletes `uc_*` directories under the temp directory that nothing references and that are more than an hour old. POSIX only: on Windows nothing is scanned and nothing is signalled.
- **SIGTERM and SIGHUP end the browser** ([#96](https://github.com/stavxyz/graftpunk/issues/96)). `gp` installs handlers for both signals that terminate every live browser session, restore the default disposition, and re-raise the signal, so the exit status stays the conventional 128 + signum and a supervisor still sees the real cause. A signal slot a host program already handles is left alone. Importing graftpunk as a library installs nothing; a host that wants the behaviour calls `graftpunk.cli.signals.install_termination_cleanup()`.
- **`GRAFTPUNK_KEEP_ORPHANED_CHROME`** ([#96](https://github.com/stavxyz/graftpunk/issues/96)). Set it to `1` to skip the start-time cleanup entirely, for a workflow that deliberately leaves detached browsers running.

### Fixed

- **The `nodriver` temp profile no longer leaks on every stop** ([#96](https://github.com/stavxyz/graftpunk/issues/96)). nodriver creates its profile with `tempfile.mkdtemp(prefix="uc_")` and deletes it only from an `atexit` handler that iterates its registry of live browsers. graftpunk removes its browser from that registry on stop, to keep that handler's output off stdout, so nothing deleted the directory and each session left one behind under the temp directory. Both stop paths, the backend's and `gp observe`'s, now delete it. A custom `profile_dir` is never touched.
```

- [ ] **Step 4: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`

Expected: all four green. `ruff format --check` covers fenced Python in Markdown outside `docs/superpowers/`, and the blocks added here are Markdown, not Python.

- [ ] **Step 5: Commit**

```bash
git add docs/HOW_IT_WORKS.md README.md CHANGELOG.md
git commit -m "docs(browser): document browser process hygiene and the cleanup opt-out (#96)"
```

> **Design note (2026-09-07):** the spec's Documentation section names `docs/HOW_IT_WORKS.md` and `CHANGELOG.md`. The README row is added as well, because `GRAFTPUNK_KEEP_ORPHANED_CHROME` is a user-facing environment variable and every other one is listed in that table.
