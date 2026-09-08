---
type: plan
---

# Orphaned Chrome Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A nodriver Chrome whose Python parent died without cleanup is found and killed before the next browser starts, every stop deletes the temp profile nodriver leaves behind, and SIGTERM or SIGHUP to a graftpunk process ends the browsers it launched (#96).

**Architecture:** Two new dependency-light library modules. `src/graftpunk/chrome_orphans.py` owns everything that reads the process table, signals a process, or deletes a profile directory; `src/graftpunk/signals.py` owns the termination handlers and a weak registry of live browser handles. Browsers are identified as graftpunk's own by a marker switch (`--graftpunk-owner-pid=<pid>`) that every launch passes through `browser_args` and that survives into `ps`, so identification is exact rather than heuristic. The two launch sites (`NoDriverBackend._start_async` and the `gp observe` path, which calls `nodriver.start` directly) both start from `base_browser_args()` and both run the same pre-launch sweep, so they cannot drift. The signal registry holds handles that can end one browser, not sessions, so nothing in the signal path knows what a `BrowserSession` is. SIGKILL and the OOM killer stay uncatchable, and the pre-launch sweep is their backstop.

**Tech Stack:** Python 3.11+, nodriver 0.48.1, Typer CLI, pydantic-settings, structlog, pytest (`uv run pytest`), ruff, ty 0.0.75. No new dependency: the process table comes from `ps`, not `psutil`.

**Spec:** `docs/superpowers/specs/2026-09-07-chrome-orphan-cleanup-design.md` (approved, and revised on 2026-09-07 to the shapes the review rulings set; read it alongside this plan, and resolve any conflict in favour of the spec, except where a task carries a Design note recording a reviewed deviation).

## Global Constraints

- Python `>=3.11` typing throughout: `X | None`, never `Optional[X]`. `from __future__ import annotations` at the top of every new module.
- structlog event-style logging: `LOG = get_logger(__name__)` at module scope, event name first, values as keyword arguments (`src/graftpunk/logging.py:169` (`def get_logger(name: str | None = None) -> structlog.BoundLogger:`)).
- Gate command, green at every commit: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`
- Tests assert behaviour, never mock-call counts (`assert_called_once_with`, `call_count`). Inject the process table and the kill function as the spec says, and assert on the resulting table, the returned values, the files on disk, and the captured structlog events.
- No test may kill or launch a real Chrome. The unit suite is disarmed at the origin by one autouse fixture; the one real-process test spawns a `python -c` sleeper wearing Chrome's switches, per the spec.
- Placeholders only, in tests and docs: `myshop`, `alice@example.com`, `fmtsite`. Never name a real site, store, account, or domain.
- No em dashes or en dashes in any new prose, docstring, comment, or commit message. Use commas, colons, parentheses, or separate sentences.
- No attribution trailers in commits. Commit subjects are `type(scope): subject (#96)`.
- POSIX only, with a documented no-op on win32: the default process table reader is skipped there and every function that signals or deletes returns nothing.
- Nothing in the new code may raise into a browser start or hang a signal handler. Cleanup failures are logged and swallowed; the signal handler's work is bounded to one SIGTERM per live browser plus one `rmtree`, then the default action.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/graftpunk/chrome_orphans.py` (new) | The mechanism: the marker switch, `base_browser_args()`, the `ProcessOps` collaborator, the `ps` parser, the orphan rules, the reaper, the stale-profile sweep, the one temp-profile deletion predicate, and `cleanup_orphans_before_launch()`. Imports nothing from graftpunk at module scope but `graftpunk.logging`. |
| `src/graftpunk/signals.py` (new) | `TerminatableBrowser`, the weak registry of live browser handles, `install_termination_cleanup()`, and the `auto_install` flag. Imports nothing from graftpunk but `graftpunk.logging`. |
| `src/graftpunk/config.py` | One new settings field, `keep_orphaned_chrome`, read from `GRAFTPUNK_KEEP_ORPHANED_CHROME`. |
| `src/graftpunk/backends/nodriver.py` | `_start_async` sweeps, marks, and registers; `_stop_async` deletes the temp profile; `_reset_state` unregisters; a new `_terminate_for_signal()`. |
| `src/graftpunk/cli/main.py` | `main_callback` sets `signals.auto_install`. The `gp observe` launch site: the same sweep and marker, an `_ObserveBrowserHandle` in the registry, the one-line user message, and one `_stop_observe_browser` helper replacing the three bare `browser.stop()` calls. |
| `tests/unit/conftest.py` | The one autouse fixture that disarms the reaper for the whole unit suite. |
| `tests/unit/test_chrome_orphans.py` (new) | The orphan module's contract, including the one real-process test. |
| `tests/unit/test_termination_signals.py` (new) | The registry, the protocol, and the handlers, including the subprocess test. |
| `tests/unit/test_nodriver_backend.py` | The backend's sweep, marker, registration, leak fix, and `_terminate_for_signal`. |
| `tests/unit/test_observe_interactive.py` | The `gp observe` launch site's marker, registration, message, and profile cleanup. |
| `docs/HOW_IT_WORKS.md`, `README.md`, `CHANGELOG.md` | User-facing documentation of the cleanup, the marker, and the opt-out. |

`src/graftpunk/session.py` is not modified. The signal registry holds browser handles, so `BrowserSession` needs no part in it.

---

### Task 1: the orphan module

**Files:**
- Create: `src/graftpunk/chrome_orphans.py`
- Test: `tests/unit/test_chrome_orphans.py` (new), `tests/unit/conftest.py` (one autouse fixture)

**Interfaces:**
- Consumes: `graftpunk.logging.get_logger` (`src/graftpunk/logging.py:169` (`def get_logger(name: str | None = None) -> structlog.BoundLogger:`)) at module scope, and nothing else in graftpunk.
- Produces, all used by Tasks 3 and 4:
  - `OWNER_SWITCH: str` (`"--graftpunk-owner-pid"`), `owner_switch(pid: int | None = None) -> str`, and `base_browser_args() -> list[str]`.
  - `ProcessOps` (frozen dataclass): `read_table`, `kill`, `pid_alive`, `sleep`, `monotonic`, plus the module instance `DEFAULT_OPS`.
  - `ChromeProcess` (frozen dataclass): `pid: int`, `ppid: int`, `args: str`, `owner_pid: int | None`, `user_data_dir: str | None`.
  - `list_chrome_processes(ops: ProcessOps | None = None) -> list[ChromeProcess]`.
  - `find_orphans(processes: Iterable[ChromeProcess], *, ops: ProcessOps | None = None) -> list[ChromeProcess]`.
  - `reap_orphans(*, grace_seconds: float = 3.0, poll_interval: float = 0.5, ops: ProcessOps | None = None) -> list[ChromeProcess]`.
  - `remove_stale_temp_profiles(*, older_than_seconds: float = 3600.0, ops: ProcessOps | None = None, now: Callable[[], float] = time.time) -> list[Path]`.
  - `browser_temp_profile(browser: Any) -> Path | None` and `remove_browser_temp_profile(browser: Any) -> Path | None`.
  - The module flag `_ARMED`, which Task 1's conftest fixture turns off for the unit suite.

`CleanupReport` and `cleanup_orphans_before_launch()` are added to this module in Task 3, where the settings field and the signals module they need already exist.

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

from graftpunk import chrome_orphans
from graftpunk.chrome_orphans import (
    OWNER_SWITCH,
    ChromeProcess,
    ProcessOps,
    _pid_alive,
    base_browser_args,
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

    def test_a_real_orphan_is_ended_and_a_live_owners_browser_is_not(self, armed: None) -> None:
        orphan = _spawn_sleeper(_a_pid_that_is_gone())
        mine = _spawn_sleeper(os.getpid())
        try:
            reaped = reap_orphans(ops=_ps_limited_to({orphan.pid, mine.pid}))

            assert [p.pid for p in reaped] == [orphan.pid]
            assert orphan.wait(timeout=10) == -signal.SIGTERM
            assert mine.poll() is None
        finally:
            for proc in (orphan, mine):
                proc.kill()
                proc.wait(timeout=10)
```

Then add the autouse guard to `tests/unit/conftest.py`, directly after the `_wide_consoles` fixture ends at `tests/unit/conftest.py:32` (`        monkeypatch.setattr(console, "width", 220)`):

```python
@pytest.fixture(autouse=True)
def _disarm_chrome_orphan_cleanup(monkeypatch):  # noqa: ANN001, ANN201
    """No unit test may signal a process or delete a directory it does not own.

    The reaper reads the real process table and sends real signals, and the
    browser launch sites call it for real in tests that drive a start. Disarm
    it at the origin, once, for the whole unit suite: reap_orphans and
    remove_stale_temp_profiles then do nothing, whoever calls them, so a new
    caller is covered the day it is written. The reaper's own tests re-arm it
    and inject a fake process table.
    """
    monkeypatch.setattr("graftpunk.chrome_orphans._ARMED", False)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_chrome_orphans.py -q`

Expected: collection error, `ModuleNotFoundError: No module named 'graftpunk.chrome_orphans'`. The conftest fixture's `monkeypatch.setattr` raises the same error for every other unit test, which is why Step 3 lands the module and the fixture together.

- [ ] **Step 3: Write the implementation**

Create `src/graftpunk/chrome_orphans.py`:

```python
"""Find and end the Chrome processes graftpunk's nodriver sessions left behind (#96).

When the Python process that launched a nodriver Chrome dies without running
its cleanup (SIGKILL, an OOM kill, a closed terminal, a crash), Chrome
survives. The survivors hold their debug ports and their memory, and they
accumulate. This module is the backstop: before a new browser starts, both
launch sites ask it to find the survivors nobody owns any more and end them.

Identification is exact rather than heuristic. Every graftpunk launch passes
``--graftpunk-owner-pid=<pid>`` in ``browser_args``; Chrome ignores switches it
does not recognise, so the marker rides through into the browser process's
command line and shows up in ``ps``. It is on the browser process only:
Chrome copies an allowlisted subset of switches to its renderer and GPU
helpers, and a vendor switch is not on that list. Ending the browser process
is what ends its helpers, since they exit when their parent's IPC channel
closes. A helper that somehow outlives its browser is matched only if it
carries ``--remote-debugging-port`` and satisfies the reparented-profile rule;
one that carries neither is out of scope for this module.

A second, narrower rule covers browsers launched before the marker existed and
other nodriver users' leftovers: no marker, a nodriver temp profile as the
``--user-data-dir``, and a parent of pid 1.

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
which Windows does not have: there the reader returns nothing and the two
functions that signal or delete return nothing. An injected table is still
parsed, so the parsing tests are meaningful on any platform.

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
_POLL_INTERVAL_S = 0.5
_STALE_PROFILE_SECONDS = 3600.0

# Signalling and deleting are armed in production and disarmed by the unit
# suite, at the origin: one autouse fixture sets this False and every test is
# covered, including the ones that drive a browser start through a launch site
# that calls in here. Guarding here rather than patching each caller means a
# new caller is covered the day it is written.
_ARMED = True


def owner_switch(pid: int | None = None) -> str:
    """The marker switch for *pid*, or for this process.

    Args:
        pid: The owner to name. Defaults to this process.

    Returns:
        ``--graftpunk-owner-pid=<pid>``, ready to append to ``browser_args``.
    """
    return f"{OWNER_SWITCH}={os.getpid() if pid is None else pid}"


def base_browser_args() -> list[str]:
    """The Chrome switches every graftpunk launch passes.

    ``--test-type``, which graftpunk has passed since before this change, and
    the owner marker that makes the browser identifiable as ours. Both launch
    sites start from this list and extend it, so a switch added here reaches
    the backend and ``gp observe`` alike.

    Returns:
        A fresh list each call, so one caller's extras cannot reach the other.
    """
    return ["--test-type", owner_switch()]


def _read_ps_table() -> str:
    """The process table from ``ps``, or an empty table and one log line."""
    if os.name != "posix":
        LOG.debug("chrome_process_table_unsupported", os_name=os.name)
        return ""
    try:
        result = subprocess.run(  # noqa: S603 - PATH lookup of ps is intentional
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


@dataclass(frozen=True)
class ProcessOps:
    """The operating system calls this module makes, in one injectable place.

    One collaborator rather than five keyword seams: a test builds a fake
    kernel (a table, a kill that mutates it, a clock that only moves when the
    code sleeps) and passes it once.

    Attributes:
        read_table: Returns the raw ``ps`` table.
        kill: Sends a signal to a pid.
        pid_alive: Whether a pid names a live process.
        sleep: Waits, during the grace period.
        monotonic: The clock the grace period is measured against.
    """

    read_table: Callable[[], str] = _read_ps_table
    kill: Callable[[int, int], None] = os.kill
    pid_alive: Callable[[int], bool] = _pid_alive
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic


DEFAULT_OPS = ProcessOps()


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


def _armed_for(action: str) -> bool:
    """Whether this pass may signal a process or delete a directory."""
    if not _ARMED:
        LOG.debug("chrome_orphan_action_disarmed", action=action)
        return False
    if os.name != "posix":
        LOG.debug("chrome_orphan_action_unsupported", action=action, os_name=os.name)
        return False
    return True


def _switch_value(args: str, switch: str) -> str | None:
    """The value of ``--switch=value`` in an argv string, or None.

    A value is read up to the next whitespace, so a path containing a space is
    truncated. A truncated path then fails the temp profile predicate below,
    which is the safe direction: the reaper leaves it alone.
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


def list_chrome_processes(ops: ProcessOps | None = None) -> list[ChromeProcess]:
    """Every process whose argv carries ``--remote-debugging-port``.

    The debugging port is the filter that separates automated browsers from the
    user's own: a Chrome the user opened has no CDP port and never appears
    here, and nodriver always passes one (``Config.__call__`` appends
    ``--remote-debugging-port=<port>``). Renderer and GPU helpers inherit
    ``--user-data-dir`` but not the vendor marker, so a helper is identified by
    the reparented-profile rule or not at all; in practice the browser process
    is what gets ended, and its helpers follow it.

    Args:
        ops: The operating system calls to use. Defaults to the real ones.

    Returns:
        The matching rows. Empty on a platform with no ``ps``, or when ``ps``
        is missing or fails.
    """
    table = (ops or DEFAULT_OPS).read_table()
    processes = []
    for row in table.splitlines():
        parsed = _parse_row(row)
        if parsed is not None:
            processes.append(parsed)
    return processes


def _removable_temp_profile(user_data_dir: str | None) -> Path | None:
    """*user_data_dir* as a Path when it is a directory nodriver made for itself.

    The one predicate every deletion site consults, and the one the legacy
    orphan rule identifies a temp profile with, so "is this ours to delete"
    has a single answer. nodriver creates its profile with
    ``tempfile.mkdtemp(prefix="uc_")``, which always lands directly under
    :func:`tempfile.gettempdir`; a plugin's persistent ``profile_dir`` does
    not, whatever it is called.
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


def find_orphans(
    processes: Iterable[ChromeProcess],
    *,
    ops: ProcessOps | None = None,
) -> list[ChromeProcess]:
    """The subset of *processes* that nobody owns any more.

    Two rules, in order:

    a. The marker names an owner pid that is not alive. Exact: this is
       graftpunk's own browser and the process that launched it is gone. A
       reused pid makes this rule skip an orphan, never kill a live owner's
       browser.
    b. No marker, the ``--user-data-dir`` is a nodriver temp profile, and the
       parent is pid 1. This covers browsers launched before the marker
       existed and other nodriver users' leftovers; a process reparented to
       pid 1 has no living parent by definition. Under a systemd user session
       an orphan may be reparented to a subreaper other than pid 1, in which
       case this rule does not fire and that legacy orphan survives until rule
       (a) covers it on the next launch. That is the conservative direction.

    A process whose owner is alive never matches, and a browser without a
    debugging port never reached this function.

    Args:
        processes: Rows from :func:`list_chrome_processes`.
        ops: The operating system calls to use. Only ``pid_alive`` is read.

    Returns:
        The orphans, in table order.
    """
    pid_alive = (ops or DEFAULT_OPS).pid_alive
    orphans = []
    for proc in processes:
        if proc.owner_pid is not None:
            rule = "owner_dead" if not pid_alive(proc.owner_pid) else None
        elif proc.ppid == 1 and _removable_temp_profile(proc.user_data_dir) is not None:
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


def _deliver(
    proc: ChromeProcess, signum: int, kill: Callable[[int, int], None], event: str
) -> bool:
    """Send one signal to one orphan. Never raises.

    Returns:
        True when the signal was delivered. A process that has already exited
        (``ProcessLookupError``) and a signal the kernel refused both return
        False, the first at debug because it is a normal race and the second at
        warning because it is not.
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
    ops: ProcessOps | None = None,
) -> list[ChromeProcess]:
    """End every orphaned Chrome, then delete the temp profiles they held.

    SIGTERM first, so Chrome can flush and exit cleanly. Then up to
    *grace_seconds* of polling the table for the ones that are still there, and
    SIGKILL for whatever is left. Ending the browser process takes its renderer
    and GPU helpers with it, because they exit when their parent's IPC channel
    closes.

    Per-process failures are logged and never raised: this runs on the way into
    a browser start.

    Args:
        grace_seconds: How long to wait for a SIGTERM to be honoured.
        poll_interval: How often to re-read the table while waiting. Half a
            second, so a three second grace costs at most six table reads.
        ops: The operating system calls to use, and the seam every test injects.

    Returns:
        The orphans this pass acted on: every one that received at least one
        signal the kernel accepted. One that had already exited, or that the
        kernel refused, is logged and left out, so the count is what was
        actually cleaned up. Empty when the module is disarmed or off POSIX.
    """
    if not _armed_for("reap"):
        return []

    ops = ops or DEFAULT_OPS
    orphans = find_orphans(list_chrome_processes(ops), ops=ops)
    if not orphans:
        return []

    acted_on = []
    for proc in orphans:
        if _deliver(proc, signal.SIGTERM, ops.kill, "chrome_orphan_terminated"):
            acted_on.append(proc)
    if not acted_on:
        return []

    remaining = list(acted_on)
    deadline = ops.monotonic() + grace_seconds
    while remaining and ops.monotonic() < deadline:
        ops.sleep(poll_interval)
        still_listed = {proc.pid for proc in list_chrome_processes(ops)}
        remaining = [proc for proc in remaining if proc.pid in still_listed]

    for proc in remaining:
        _deliver(proc, signal.SIGKILL, ops.kill, "chrome_orphan_killed")

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
    ops: ProcessOps | None = None,
    now: Callable[[], float] = time.time,
) -> list[Path]:
    """Delete the nodriver temp profiles that no live browser is using.

    The sweep for the profiles already leaked: directories no listed browser
    names in its ``--user-data-dir``, older than *older_than_seconds*. "In
    use" is decided from the parsed switch rather than a substring of the raw
    table, so an unrelated process that merely mentions the path does not
    protect it. The age guard is what keeps a directory another process created
    moments ago, before its Chrome has shown up in ``ps``.

    Args:
        older_than_seconds: How old a directory must be to count as abandoned.
        ops: The operating system calls to use.
        now: The wall clock, for tests.

    Returns:
        The directories removed. Empty when the module is disarmed or off POSIX.
    """
    if not _armed_for("sweep"):
        return []

    ops = ops or DEFAULT_OPS
    in_use = {
        os.path.normpath(proc.user_data_dir)
        for proc in list_chrome_processes(ops)
        if proc.user_data_dir
    }
    temp_root = Path(tempfile.gettempdir())
    try:
        candidates = sorted(
            path
            for path in temp_root.iterdir()
            if _removable_temp_profile(str(path)) is not None and path.is_dir()
        )
    except OSError as exc:
        LOG.warning("chrome_orphan_cleanup_failed", pid=None, error=str(exc))
        return []

    removed = []
    for path in candidates:
        if os.path.normpath(str(path)) in in_use:
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
    us a directory": it is False exactly when nodriver made one itself. A
    custom ``profile_dir`` is therefore never reported here.

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

    Ownership is nodriver's ``uses_custom_data_dir`` flag; safety is
    :func:`_removable_temp_profile`, the same predicate the reaper and the
    stale sweep use, so no path outside the temp directory is ever deleted
    here.

    Returns:
        The directory removed, or None when there was nothing to remove.
    """
    path = _removable_temp_profile(str(browser_temp_profile(browser) or ""))
    if path is None or not path.exists():
        return None
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        LOG.debug("chrome_temp_profile_remove_failed", path=str(path))
        return None
    LOG.debug("chrome_temp_profile_removed", path=str(path))
    return path
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_chrome_orphans.py -q`

Expected: PASS, 39 tests (4 in `TestBrowserArgs`, 8 in `TestListChromeProcesses`, 7 in `TestFindOrphans`, 10 in `TestReapOrphans`, 7 in `TestRemoveStaleTempProfiles`, 3 in `TestRealProcesses`).

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`

Expected: all four green. Nothing but the conftest fixture imports the new module yet, so the rest of the suite is unaffected.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/chrome_orphans.py tests/unit/test_chrome_orphans.py tests/unit/conftest.py
git commit -m "feat(chrome-orphans): find and reap orphaned nodriver Chrome processes (#96)"
```

> **Design note (2026-09-07, SOLID 11):** the module is `src/graftpunk/chrome_orphans.py`, top level beside `chrome.py`, not under `backends/`. It is a process-hygiene utility with no backend in it, and the CLI imports it too. The spec's Part 1 heading has been updated to the same path.
>
> **Design note (2026-09-07, SOLID 17):** one `ProcessOps` collaborator replaces the five separate keyword seams the first draft had (`process_table`, `kill`, `pid_alive`, `sleep`, `monotonic`). Each function now takes `ops` plus its own behaviour knobs, and a test builds one fake kernel instead of wiring five lambdas per call.
>
> **Design note (2026-09-07, SOLID 4 and 5):** the unit suite's safety guard is a module-level `_ARMED` flag checked by the two functions that signal or delete, set False by one autouse fixture. The first draft patched `cleanup_orphans_before_launch` in a list of consumer modules, which silently stops protecting anything the day a third consumer appears.
>
> **Design note (2026-09-07, SOLID 6, 7, 14, 15 and fact-check F9):** `base_browser_args()` owns the switch list both launch sites start from, and `_removable_temp_profile` is the single "is this ours to delete" predicate. It now also decides the legacy orphan rule's "is this a temp profile", replacing the looser `"/uc_" in path` substring test, so the identification rule and the deletion rule cannot disagree.
>
> **Design note (2026-09-07, SOLID 9 and 10):** the grace period polls every 0.5 seconds rather than every 0.1, so the default three second wait costs at most six extra `ps` reads. `test_the_default_grace_reads_the_table_seven_times` pins that.
>
> **Design note (2026-09-07, fact-check F2, F3 and F5 to F7):** the module docstring and `list_chrome_processes` no longer claim the marker reaches renderer and GPU helpers. Chrome copies only an allowlisted subset of switches to its children, which excludes a vendor switch, so the marker is on the browser process alone; ending the browser is what ends its helpers, and a helper that outlives its browser without a debugging port is stated as out of scope. New prose also says only that graftpunk has always passed `--test-type`, without repeating the unverified claim about the unsupported-flag banner.
>
> **Design note (2026-09-07, fact-check F4):** the `structlog.reset_defaults()` comments say what is true: the autouse `_reset_structlog` fixture resets structlog again after the test, and graftpunk's import-time WARNING filter is not re-armed. The first draft said the fixture restored it.
>
> **Design note (2026-09-07, fact-check suggestion):** `remove_stale_temp_profiles` decides "in use" from parsed `--user-data-dir` values rather than a substring test on the raw `ps` output, so an unrelated process that merely mentions the path cannot keep a leaked directory alive. `test_a_profile_merely_mentioned_in_the_table_is_still_removed` pins it.

---

### Task 2: the signal module and the live browser registry

**Files:**
- Create: `src/graftpunk/signals.py`
- Test: `tests/unit/test_termination_signals.py` (new)

**Interfaces:**
- Consumes: `graftpunk.logging.get_logger` only.
- Produces:
  - `TerminatableBrowser`, a runtime-checkable Protocol with one method, `_terminate_for_signal() -> None`.
  - `register_live_browser(handle) -> None`, `unregister_live_browser(handle) -> None`, `live_browsers() -> list[TerminatableBrowser]`.
  - `install_termination_cleanup() -> None` and the module-private `_handle_termination(signum, frame)`.
  - `auto_install: bool`, the flag the CLI sets and `cleanup_orphans_before_launch` reads in Task 3.

`src/graftpunk/session.py` is not touched: the registry holds browser handles, so `BrowserSession` has no part in the signal path.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_termination_signals.py`:

```python
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
    """The flag is opt-in: a library consumer's signal slots stay theirs."""
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
```

Then add this fixture to `tests/unit/conftest.py`, below `_disarm_chrome_orphan_cleanup`:

```python
@pytest.fixture(autouse=True)
def _restore_termination_signals():  # noqa: ANN201
    """Give each test the SIGTERM and SIGHUP slots back.

    A browser launch arms the handlers in this worker process. Left in place,
    one test's handler becomes ambient state for the rest of the run and the
    signal tests cannot tell an install apart from a leftover.
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

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_termination_signals.py -q`

Expected: collection error, `ModuleNotFoundError: No module named 'graftpunk.signals'`.

- [ ] **Step 3: Write the implementation**

Create `src/graftpunk/signals.py`:

```python
"""Termination signals end the browsers this process started (#96).

``BrowserSession.__exit__``, ``quit()`` and nodriver's own ``atexit`` handler
all run only when Python exits normally. A SIGTERM from a supervisor, or the
SIGHUP a closing terminal sends, kills Python where it stands and leaves Chrome
running. This module installs the handlers that close that gap.

The registry holds browser HANDLES, not sessions: anything that can end one
browser without an event loop. ``NoDriverBackend`` registers itself once its
browser is up; ``gp observe``, which drives a ``nodriver`` browser directly,
registers a small adapter of its own. Nothing here knows what a
``BrowserSession`` is, and a future backend gets the behaviour by implementing
one method.

Nothing is installed by importing graftpunk. The CLI sets :data:`auto_install`
in its root callback and the first browser launch arms the handlers, so a
command that never opens a browser leaves the process's signal dispositions
exactly as it found them. A host program can call
:func:`install_termination_cleanup` itself, and a slot it already handles is
left alone either way.

SIGKILL and the OOM killer stay uncatchable, which is why the pre-launch
reaper exists (:mod:`graftpunk.chrome_orphans`).
"""

from __future__ import annotations

import os
import signal
import weakref
from types import FrameType
from typing import Protocol, runtime_checkable

from graftpunk.logging import get_logger

LOG = get_logger(__name__)

# Set True by the CLI's root callback, read at the first browser launch.
auto_install: bool = False

_HANDLED_SIGNAL_NAMES = ("SIGTERM", "SIGHUP")


@runtime_checkable
class TerminatableBrowser(Protocol):
    """Something that can end one browser without touching an event loop."""

    def _terminate_for_signal(self) -> None:
        """End this browser now, from a signal handler.

        The name is private on purpose: this is for the handler in this
        module, not for application code, which stops a browser through
        ``BrowserSession`` or the backend's ``stop()`` and gets an orderly
        shutdown. An implementation must not raise, must not await, and must
        leave its object in a coherent state.
        """
        ...  # pragma: no cover - protocol declaration


_LIVE_BROWSERS: weakref.WeakSet[TerminatableBrowser] = weakref.WeakSet()


def register_live_browser(handle: TerminatableBrowser) -> None:
    """Add *handle* to the set the termination handler will end.

    Held weakly: registering never keeps a browser alive, so a caller that
    forgets to unregister leaks nothing.
    """
    _LIVE_BROWSERS.add(handle)


def unregister_live_browser(handle: TerminatableBrowser) -> None:
    """Remove *handle*. A no-op when it was never registered."""
    _LIVE_BROWSERS.discard(handle)


def live_browsers() -> list[TerminatableBrowser]:
    """A snapshot of the registered handles.

    A snapshot rather than the set itself: the handler unregisters handles as
    it ends them, and it must not iterate a WeakSet that is being mutated,
    whether by itself or by the garbage collector.
    """
    return list(_LIVE_BROWSERS)


def _handle_termination(signum: int, _frame: FrameType | None) -> None:
    """End every live browser, then die of the signal that arrived.

    Restoring ``SIG_DFL`` and re-raising, rather than calling ``sys.exit``,
    keeps the exit status the conventional 128 + signum, so a supervisor
    watching this process still sees the real cause of death.

    Every handle's call is wrapped: one browser that will not go must not stop
    the others from being asked, and must not stall the process on its way out.
    """
    name = signal.Signals(signum).name
    for handle in live_browsers():
        try:
            handle._terminate_for_signal()
        except Exception as exc:  # noqa: BLE001 - a handler has nowhere to raise
            LOG.warning("termination_cleanup_failed", signal=name, error=str(exc))
    LOG.info("termination_signal_received", signal=name)
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def install_termination_cleanup() -> None:
    """Take the SIGTERM and SIGHUP slots, if nobody else has them.

    Idempotent: after the first call the slots no longer hold ``SIG_DFL``, so a
    second call finds them taken and leaves them alone. Signal dispositions can
    only be set from the main thread, and not every platform has SIGHUP, so
    both cases are debug logs rather than failures.
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

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_termination_signals.py -q`

Expected: PASS, 12 tests (5 in `TestLiveBrowserRegistry`, 2 in `TestTerminatableBrowserProtocol`, 3 in `TestInstallTerminationCleanup`, the `auto_install` default, and the subprocess test).

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`

Expected: all four green.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/signals.py tests/unit/test_termination_signals.py tests/unit/conftest.py
git commit -m "feat(signals): termination handlers and a registry of live browsers (#96)"
```

> **Design note (2026-09-07, SOLID 1 and 2):** the module is `src/graftpunk/signals.py`, in the library layer beside `session.py`, not under `cli/`. Nothing in it is CLI-specific, and a library consumer that wants the behaviour should not have to import a CLI package to get it. The CLI imports it; the docs and CHANGELOG name `graftpunk.signals.install_termination_cleanup()`. The spec's Part 3 has been updated to the same path.
>
> **Design note (2026-09-07, SOLID 3 and 13):** the registry holds `TerminatableBrowser` handles rather than `BrowserSession` instances, and `BrowserSession.terminate_browser_process()` is not added at all. The first draft put a session registry in `session.py` and had the session dispatch on its backend name, which is the "gates on backend name" smell and made the signal path depend on the whole session module. `src/graftpunk/session.py` is now untouched by this plan.
>
> **Design note (2026-09-07, SOLID 16):** `auto_install` is a flag, not an install. The first draft had `main_callback` install handlers for every `gp` invocation, including `gp session list`, which claims two process-wide signal slots for a command that opens no browser. Now the CLI sets the flag and the first browser launch arms the handlers.

---

### Task 3: the backend sweeps, marks, registers, and stops leaking

**Files:**
- Modify: `src/graftpunk/config.py` (one field), `src/graftpunk/chrome_orphans.py` (append `CleanupReport` and `cleanup_orphans_before_launch`), `src/graftpunk/backends/nodriver.py` (imports, `_start_async`, `_stop_async`, `_reset_state`, and one new method)
- Test: `tests/unit/test_nodriver_backend.py` (append)

**Interfaces:**
- Consumes: `base_browser_args`, `remove_browser_temp_profile`, `reap_orphans`, `remove_stale_temp_profiles` from Task 1; `register_live_browser`, `unregister_live_browser`, `auto_install`, `install_termination_cleanup` from Task 2.
- Produces:
  - `GraftpunkSettings.keep_orphaned_chrome: bool` (default False, from `GRAFTPUNK_KEEP_ORPHANED_CHROME`).
  - `CleanupReport` (frozen dataclass): `reaped: list[ChromeProcess]`, `profiles_removed: list[Path]`, truthy when either is non-empty.
  - `cleanup_orphans_before_launch() -> CleanupReport`, which Task 4 also calls.
  - `NoDriverBackend._terminate_for_signal(self) -> None`, satisfying `TerminatableBrowser`.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_nodriver_backend.py`, extend the stdlib import block that opens the file, above `tests/unit/test_nodriver_backend.py:4` (`from pathlib import Path`), so its first lines read:

```python
import asyncio
import os
import signal
from pathlib import Path
```

Extend the mock import at `tests/unit/test_nodriver_backend.py:5` (`from unittest.mock import MagicMock, patch`) to:

```python
from unittest.mock import AsyncMock, MagicMock, patch
```

and add the orphan-module import directly below `tests/unit/test_nodriver_backend.py:11` (`from graftpunk.backends.nodriver import NoDriverBackend`):

```python
from graftpunk.chrome_orphans import OWNER_SWITCH, base_browser_args
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

    async def test_configured_browser_args_follow_the_shared_ones(self) -> None:
        started: list = []
        backend = NoDriverBackend(browser_args=["--lang=en-US"])

        with patch.dict("sys.modules", {"nodriver": self._fake_nodriver(started)}):
            await backend._start_async()

        assert started[0]["browser_args"] == [*base_browser_args(), "--lang=en-US"]

    async def test_the_sweep_runs_before_the_browser_starts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reaping after the launch would leave the new Chrome competing with the old."""
        from graftpunk.chrome_orphans import CleanupReport

        order: list[str] = []

        def fake_cleanup() -> CleanupReport:
            order.append("sweep")
            return CleanupReport()

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

    async def test_a_started_backend_is_in_the_signal_registry(self) -> None:
        from graftpunk.signals import live_browsers

        backend = NoDriverBackend()
        try:
            with patch.dict("sys.modules", {"nodriver": self._fake_nodriver([])}):
                await backend._start_async()

            assert backend in live_browsers()
        finally:
            backend._reset_state()

    async def test_stopping_leaves_the_signal_registry(self) -> None:
        from graftpunk.signals import live_browsers

        backend = NoDriverBackend()
        with patch.dict("sys.modules", {"nodriver": self._fake_nodriver([])}):
            await backend._start_async()
        backend._started = True

        await backend.stop_async()

        assert backend not in live_browsers()


class TestCleanupOrphansBeforeLaunch:
    """The composition root: opt-out, two independent sweeps, no output (#96)."""

    def test_the_opt_out_skips_both_sweeps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from graftpunk.chrome_orphans import cleanup_orphans_before_launch
        from graftpunk.config import reset_settings

        calls: list[str] = []
        monkeypatch.setenv("GRAFTPUNK_KEEP_ORPHANED_CHROME", "1")
        reset_settings()
        monkeypatch.setattr("graftpunk.chrome_orphans.reap_orphans", lambda: calls.append("reap"))
        monkeypatch.setattr(
            "graftpunk.chrome_orphans.remove_stale_temp_profiles", lambda: calls.append("sweep")
        )

        report = cleanup_orphans_before_launch()

        assert calls == []
        assert not report

    def test_a_failing_reap_does_not_stop_the_profile_sweep(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Separate try blocks: one broken sweep must not cost the other."""
        from graftpunk.chrome_orphans import cleanup_orphans_before_launch

        def boom() -> list:
            raise OSError("ps went missing")

        monkeypatch.setattr("graftpunk.chrome_orphans.reap_orphans", boom)
        monkeypatch.setattr(
            "graftpunk.chrome_orphans.remove_stale_temp_profiles", lambda: [tmp_path / "uc_gone"]
        )

        report = cleanup_orphans_before_launch()

        assert report.reaped == []
        assert report.profiles_removed == [tmp_path / "uc_gone"]
        assert report

    def test_an_empty_pass_is_falsey(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from graftpunk.chrome_orphans import cleanup_orphans_before_launch

        monkeypatch.setattr("graftpunk.chrome_orphans.reap_orphans", list)
        monkeypatch.setattr("graftpunk.chrome_orphans.remove_stale_temp_profiles", list)

        assert not cleanup_orphans_before_launch()

    def test_it_arms_the_handlers_when_the_cli_asked_for_them(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk import signals
        from graftpunk.chrome_orphans import cleanup_orphans_before_launch

        monkeypatch.setattr("graftpunk.chrome_orphans.reap_orphans", list)
        monkeypatch.setattr("graftpunk.chrome_orphans.remove_stale_temp_profiles", list)
        monkeypatch.setattr(signals, "auto_install", True)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

        cleanup_orphans_before_launch()

        assert signal.getsignal(signal.SIGTERM) is signals._handle_termination

    def test_it_arms_nothing_when_the_flag_is_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from graftpunk import signals
        from graftpunk.chrome_orphans import cleanup_orphans_before_launch

        monkeypatch.setattr("graftpunk.chrome_orphans.reap_orphans", list)
        monkeypatch.setattr("graftpunk.chrome_orphans.remove_stale_temp_profiles", list)
        monkeypatch.setattr(signals, "auto_install", False)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

        cleanup_orphans_before_launch()

        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


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

    async def test_a_temp_profile_is_removed_on_stop(self, temp_profiles: Path) -> None:
        profile = temp_profiles / "uc_abc"
        (profile / "Default").mkdir(parents=True)
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = self._browser(profile, custom=False)

        await backend._stop_async()

        assert not profile.exists()

    async def test_a_custom_profile_dir_survives_stop(self, temp_profiles: Path) -> None:
        """A plugin's persistent profile is the user's data, never ours to delete."""
        profile = temp_profiles / "persistent-profile"
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


class TestNoDriverBackendTerminateForSignal:
    """The loop-free path the signal handler takes (#96)."""

    @staticmethod
    def _browser(profile: Path, pid: int = 4242) -> MagicMock:
        browser = MagicMock()
        browser._process = MagicMock()
        browser._process.pid = pid
        browser.config.uses_custom_data_dir = False
        browser.config.user_data_dir = str(profile)
        return browser

    def test_it_signals_the_subprocess_and_removes_the_profile(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # monkeypatch resolves this to the real os module and restores it after
        # the test; nothing else in this worker sends a signal meanwhile.
        sent: list[tuple[int, int]] = []
        monkeypatch.setattr(
            "graftpunk.backends.nodriver.os.kill",
            lambda pid, signum: sent.append((pid, signum)),
        )
        profile = temp_profiles / "uc_live"
        profile.mkdir()
        backend = NoDriverBackend()
        backend._browser = self._browser(profile)
        backend._started = True

        backend._terminate_for_signal()

        assert sent == [(4242, signal.SIGTERM)]
        assert not profile.exists()

    def test_it_leaves_the_backend_coherent(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """is_running must not claim a browser this method just ended."""
        from graftpunk.signals import live_browsers, register_live_browser

        monkeypatch.setattr("graftpunk.backends.nodriver.os.kill", lambda pid, signum: None)
        profile = temp_profiles / "uc_live"
        profile.mkdir()
        backend = NoDriverBackend()
        backend._browser = self._browser(profile)
        backend._started = True
        register_live_browser(backend)

        backend._terminate_for_signal()

        assert backend.is_running is False
        assert backend._browser is None
        assert backend not in live_browsers()

    def test_a_process_that_already_exited_is_not_an_error(
        self, temp_profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def gone(pid: int, signum: int) -> None:
            raise ProcessLookupError(3, "No such process")

        monkeypatch.setattr("graftpunk.backends.nodriver.os.kill", gone)
        profile = temp_profiles / "uc_live"
        profile.mkdir()
        backend = NoDriverBackend()
        backend._browser = self._browser(profile)

        backend._terminate_for_signal()

        assert not profile.exists()

    def test_no_browser_is_a_no_op(self) -> None:
        backend = NoDriverBackend()

        backend._terminate_for_signal()

    def test_the_backend_satisfies_the_protocol(self) -> None:
        from graftpunk.signals import TerminatableBrowser

        assert isinstance(NoDriverBackend(), TerminatableBrowser)


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

These use one new fixture. Add it directly above `tests/unit/test_nodriver_backend.py:16` (`class TestNoDriverBackendProtocol:`):

```python
@pytest.fixture()
def temp_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A sandbox that ``tempfile.gettempdir()`` points at.

    ``_removable_temp_profile`` only deletes a directory sitting directly under
    the temp directory, which is where nodriver puts its profiles. A test
    profile has to be somewhere that predicate accepts.
    """
    import tempfile

    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(root))
    return root
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_nodriver_backend.py -q -k "OrphanCleanup or CleanupOrphansBeforeLaunch or TempProfileCleanup or TerminateForSignal or KeepOrphanedChrome"`

Expected: FAIL.
- `test_the_owner_marker_rides_in_browser_args`: `AssertionError: assert ['--test-type'] == ['--test-type', '--graftpunk-owner-pid=NNNNN']`.
- `test_the_sweep_runs_before_the_browser_starts`: `ImportError: cannot import name 'CleanupReport' from 'graftpunk.chrome_orphans'`.
- `TestCleanupOrphansBeforeLaunch`: `ImportError: cannot import name 'cleanup_orphans_before_launch' from 'graftpunk.chrome_orphans'`.
- `test_a_temp_profile_is_removed_on_stop`: `AssertionError: assert not True` (the directory is still there).
- `TestNoDriverBackendTerminateForSignal`: `AttributeError: 'NoDriverBackend' object has no attribute '_terminate_for_signal'`.
- `TestKeepOrphanedChromeSetting`: `AttributeError: 'GraftpunkSettings' object has no attribute 'keep_orphaned_chrome'`.

- [ ] **Step 3: Write the implementation**

First, in `src/graftpunk/config.py`, add the field to `GraftpunkSettings` directly after the `browser_executable_path` field's closing `)` (the block that starts at `src/graftpunk/config.py:87` (`    browser_executable_path: str | None = Field(`)) and before `model_config`:

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

Next, append to `src/graftpunk/chrome_orphans.py`:

```python
@dataclass(frozen=True)
class CleanupReport:
    """What one pre-launch cleanup pass actually did.

    Attributes:
        reaped: The orphaned processes this pass ended.
        profiles_removed: The stale temp profile directories it deleted.
    """

    reaped: list[ChromeProcess] = dataclasses.field(default_factory=list)
    profiles_removed: list[Path] = dataclasses.field(default_factory=list)

    def __bool__(self) -> bool:
        """True when the pass changed something, so a caller can report it once."""
        return bool(self.reaped or self.profiles_removed)


def cleanup_orphans_before_launch() -> CleanupReport:
    """Arm the termination handlers, end orphaned Chromes, sweep stale profiles.

    The one entry point both launch sites call, so the backend and
    ``gp observe`` cannot drift. It applies the opt-out, runs the two sweeps
    under separate guards so a failure in one does not cost the other, and
    never raises: a browser start must not fail because a cleanup pass did.

    It prints nothing. The caller decides whether its user wants a line about
    it: ``gp observe`` prints one, a plugin login only logs, and neither
    decision belongs in here.

    Settings and the signal module are imported here rather than at module
    scope, so this module keeps depending on nothing but ``graftpunk.logging``
    and no import cycle is possible.

    Returns:
        What was cleaned up. Falsey when nothing was.
    """
    from graftpunk import signals
    from graftpunk.config import get_settings

    if signals.auto_install:
        try:
            signals.install_termination_cleanup()
        except Exception as exc:  # noqa: BLE001 - a launch must not fail on this
            LOG.warning("termination_handler_install_failed", error=str(exc))

    try:
        if get_settings().keep_orphaned_chrome:
            LOG.debug("chrome_orphan_cleanup_skipped", reason="opt_out")
            return CleanupReport()
    except Exception as exc:  # noqa: BLE001 - unreadable settings are not a launch failure
        LOG.warning("chrome_orphan_cleanup_failed", stage="settings", error=str(exc))
        return CleanupReport()

    try:
        reaped = reap_orphans()
    except Exception as exc:  # noqa: BLE001 - cleanup must never fail a browser start
        LOG.warning("chrome_orphan_cleanup_failed", stage="reap", error=str(exc))
        reaped = []

    try:
        profiles_removed = remove_stale_temp_profiles()
    except Exception as exc:  # noqa: BLE001 - cleanup must never fail a browser start
        LOG.warning("chrome_orphan_cleanup_failed", stage="sweep", error=str(exc))
        profiles_removed = []

    return CleanupReport(reaped=reaped, profiles_removed=profiles_removed)
```

`CleanupReport` uses `dataclasses.field`, so extend the module's dataclass import. Replace the `from dataclasses import dataclass` line with:

```python
import dataclasses
from dataclasses import dataclass
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
from graftpunk.chrome_orphans import (
    base_browser_args,
    cleanup_orphans_before_launch,
    remove_browser_temp_profile,
)
from graftpunk.exceptions import BrowserError
from graftpunk.logging import get_logger
from graftpunk.signals import register_live_browser, unregister_live_browser
```

In `_start_async`, replace lines 312 to 317, from the `_patch_nodriver_cookie_parsing()` call through `src/graftpunk/backends/nodriver.py:315` (`        browser_args = ["--test-type"]`) and the two lines that follow it, with:

```python
        _patch_nodriver_cookie_parsing()

        # End the Chromes earlier runs left behind before adding one more, and
        # arm the termination handlers if the CLI asked for them (#96). The
        # sweep shells out to ps and sleeps through a grace period, so it runs
        # on a worker thread rather than blocking this event loop.
        report = await asyncio.to_thread(cleanup_orphans_before_launch)
        if report:
            LOG.info(
                "chrome_orphans_cleaned",
                reaped=len(report.reaped),
                profiles_removed=len(report.profiles_removed),
            )

        browser_args = base_browser_args()
        if "browser_args" in self._options:
            browser_args.extend(self._options["browser_args"])
```

Still in `_start_async`, register the backend the moment its browser exists. Replace `src/graftpunk/backends/nodriver.py:337` (`                self._browser = await uc.start(**start_kwargs)`) with:

```python
                self._browser = await uc.start(**start_kwargs)
                # Registered before the first tab: a signal arriving during
                # navigation must still find this browser (#96).
                register_live_browser(self)
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

Replace `_reset_state` (`src/graftpunk/backends/nodriver.py:226` (`    def _reset_state(self) -> None:`)) and its body with:

```python
    def _reset_state(self) -> None:
        """Reset browser state after stop, and leave the signal registry.

        Both stop paths call this from a ``finally``, so the registry entry
        outlives the risky part of a stop: a stop that raises still leaves a
        handle the termination handler can use, and a stop that succeeds takes
        the entry with it (#96).
        """
        unregister_live_browser(self)
        self._browser = None
        self._page = None
        self._started = False
        LOG.info("nodriver_backend_stopped")
```

Finally, add this method immediately after `stop_async` ends, before the `@property` line above `src/graftpunk/backends/nodriver.py:502` (`    def is_running(self) -> bool:`):

```python
    def _terminate_for_signal(self) -> None:
        """End the browser process from a signal handler, then reset this backend.

        The loop-free path (:class:`graftpunk.signals.TerminatableBrowser`).
        ``stop()`` runs ``asyncio.run()`` and ``stop_async()`` needs a running
        loop; a signal handler has neither, so this touches only the subprocess
        pid and the profile directory. It never raises: the caller is on its
        way to the default signal action and has nowhere to put an exception.

        It finishes with ``_reset_state()`` so the object is coherent
        afterwards: ``is_running`` must not claim a browser this method just
        ended, and the registry entry goes with it.
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
        self._reset_state()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_nodriver_backend.py tests/unit/test_session.py -q`

Expected: PASS, including the 19 new tests and the whole existing `TestNoDriverBackendStopReap` class (`tests/unit/test_nodriver_backend.py:955` (`class TestNoDriverBackendStopReap:`)), whose `MagicMock` browsers report a truthy `uses_custom_data_dir` and so are read as custom profiles and left alone.

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`

Expected: all four green.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/config.py src/graftpunk/chrome_orphans.py src/graftpunk/backends/nodriver.py tests/unit/test_nodriver_backend.py
git commit -m "fix(nodriver): sweep orphans, mark the browser, and stop leaking the temp profile (#96)"
```

> **Design note (2026-09-07, SOLID 8):** `cleanup_orphans_before_launch` returns a `CleanupReport` and prints nothing. The first draft returned an `int` and printed to the console from inside the module, which put a user-interface decision in a process-hygiene utility and gave a plugin login the same output as an interactive `gp observe` run. The backend logs `chrome_orphans_cleaned`; only the observe site prints (Task 4). The two sweeps have separate `try` blocks, so a `ps` failure still lets the profile sweep run.
>
> **Design note (2026-09-07, SOLID 9 and 10):** both async call sites run the sweep through `asyncio.to_thread`. It shells out to `ps` and can sleep through a three second grace period, which would otherwise block the event loop that is about to start a browser.
>
> **Design note (2026-09-07, SOLID 12 and 18):** `_terminate_for_signal` (underscore, matching the protocol) ends with `_reset_state()`, so `is_running` is honest afterwards, and `_reset_state` is where the registry entry is released. Both stop paths already call `_reset_state` from a `finally`, so a stop that raises part way through still leaves a usable handle registered until the reset actually happens.
>
> **Design note (2026-09-07, fact-check F1 and F8):** `import os` and `import signal` go in the stdlib block at the top of `tests/unit/test_nodriver_backend.py`, not beside the graftpunk imports, or isort (I001) fails. The settings class is `GraftpunkSettings`; the spec's Part 1 said `Settings`.
>
> **Design note (2026-09-07, fact-check F5 to F7):** the pre-existing comment at `src/graftpunk/backends/nodriver.py:314` (`        # --test-type suppresses Chrome's "unsupported flag" warning banner`) is deleted with the line it annotated, because that list moves into `base_browser_args()`. The claim could not be verified against current Chromium (`bad_flags_prompt.cc` iterates a fixed array that does not include `--test-type`), so it is not carried into the new location; the new docstring says only that graftpunk has always passed the switch.

---

### Task 4: the `gp observe` launch site

**Files:**
- Modify: `src/graftpunk/cli/main.py` (imports, `main_callback`, the observe launch, and the three `browser.stop()` calls)
- Test: `tests/unit/test_observe_interactive.py` (append)

**Interfaces:**
- Consumes: `base_browser_args`, `cleanup_orphans_before_launch`, `remove_browser_temp_profile` from Tasks 1 and 3; `register_live_browser`, `unregister_live_browser`, and the `auto_install` flag from Task 2.
- Produces: `_ObserveBrowserHandle`, `_register_observe_browser(browser)` and `_stop_observe_browser(browser)` in `graftpunk.cli.main`. Nothing outside this module calls them.

`gp observe` calls `nodriver.start` directly rather than going through `NoDriverBackend` (`src/graftpunk/cli/main.py:480` (`    import nodriver`)). Routing it through the backend is a larger refactor and out of scope, so it uses the same shared helpers and registers a handle of its own.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_observe_interactive.py`:

```python
class TestObserveBrowserHygiene:
    """gp observe marks its Chrome, registers it, and cleans up after it (#96)."""

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
    async def test_the_shared_switches_reach_nodriver_start(self, tmp_path: Path) -> None:
        from graftpunk.chrome_orphans import base_browser_args
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
        assert kwargs["browser_args"] == base_browser_args()

    @pytest.mark.asyncio
    async def test_the_sweep_runs_before_the_browser_starts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk.chrome_orphans import CleanupReport
        from graftpunk.cli.main import _setup_observe_session

        order: list[str] = []

        def fake_cleanup() -> CleanupReport:
            order.append("sweep")
            return CleanupReport()

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
    async def test_a_reaped_run_tells_the_user_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from graftpunk.chrome_orphans import ChromeProcess, CleanupReport
        from graftpunk.cli.main import _setup_observe_session

        orphan = ChromeProcess(pid=4242, ppid=1, args="", owner_pid=999999, user_data_dir=None)
        monkeypatch.setattr(
            "graftpunk.cli.main.cleanup_orphans_before_launch",
            lambda: CleanupReport(reaped=[orphan]),
        )
        mock_nodriver = MagicMock()
        mock_nodriver.start = AsyncMock(return_value=self._browser(None))
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

        assert "Cleaned up 1 orphaned Chrome process(es)" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_the_browser_is_registered_for_signals_and_released_on_stop(
        self, tmp_path: Path
    ) -> None:
        from graftpunk.cli.main import _run_observe_go
        from graftpunk.signals import live_browsers

        registered: list = []
        browser = self._browser(None)
        mock_nodriver = MagicMock()
        mock_nodriver.start = AsyncMock(return_value=browser)
        storage = MagicMock()
        storage.run_dir = tmp_path / "run"

        async def record_then_save(*args: object, **kwargs: object) -> None:
            registered.extend(live_browsers())

        with (
            patch.dict("sys.modules", {"nodriver": mock_nodriver}),
            patch(
                "graftpunk.observe.capture.NodriverCaptureBackend",
                return_value=self._capture_backend(),
            ),
            patch("graftpunk.observe.storage.ObserveStorage", return_value=storage),
            patch("graftpunk.cli.main.save_observe_run", new=record_then_save),
        ):
            await _run_observe_go(
                "test-ns", "https://example.com", 0.0, 5 * 1024 * 1024, session_name=None
            )

        # Registered while the browser was up, and gone once it was stopped.
        assert any(getattr(h, "_browser", None) is browser for h in registered)
        assert not any(getattr(h, "_browser", None) is browser for h in live_browsers())

    @pytest.mark.asyncio
    async def test_the_temp_profile_is_removed_after_the_browser_stops(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The leak fix at the second launch site: observe made the directory too."""
        import tempfile

        from graftpunk.cli.main import _run_observe_go

        temp_root = tmp_path / "tmp"
        temp_root.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp_root))
        profile = temp_root / "uc_observe"
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


class TestObserveArmsTheSignalHandlers:
    """The root callback sets a flag; it takes no signal slot by itself (#96)."""

    def test_the_callback_sets_the_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from graftpunk import signals

        monkeypatch.setattr(signals, "auto_install", False)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

        result = runner.invoke(app, ["version"])

        assert result.exit_code == 0, result.output
        assert signals.auto_install is True
        # A command that opens no browser leaves the slot alone.
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_observe_interactive.py -q -k "BrowserHygiene or ArmsTheSignalHandlers"`

Expected: FAIL.
- `test_the_shared_switches_reach_nodriver_start`: `AssertionError: assert ['--test-type'] == ['--test-type', '--graftpunk-owner-pid=NNNNN']`.
- `test_the_sweep_runs_before_the_browser_starts`: `AttributeError: <module 'graftpunk.cli.main'> does not have the attribute 'cleanup_orphans_before_launch'`.
- `test_the_browser_is_registered_for_signals_and_released_on_stop`: `AssertionError: assert False` (nothing was registered).
- `test_the_temp_profile_is_removed_after_the_browser_stops`: `AssertionError: assert not True`.
- `test_the_callback_sets_the_flag`: `AssertionError: assert False is True`.

- [ ] **Step 3: Write the implementation**

In `src/graftpunk/cli/main.py`, replace `src/graftpunk/cli/main.py:30` (`from graftpunk import workstation_env`) with:

```python
from graftpunk import signals, workstation_env
from graftpunk.chrome_orphans import (
    base_browser_args,
    cleanup_orphans_before_launch,
    remove_browser_temp_profile,
)
```

`graftpunk.chrome_orphans` and `graftpunk.signals` import only the standard library and `graftpunk.logging` at module scope, so neither adds to CLI start-up cost and neither can cycle. The module-level name is `signals`, one letter from the standard library's `signal`, which is already imported at `src/graftpunk/cli/main.py:10` (`import signal`); keep the two apart by always writing `signals.auto_install`.

At the end of `main_callback`, replace `src/graftpunk/cli/main.py:171` (`    ctx.ensure_object(dict)["observe_mode"] = observe.value`) with:

```python
    # Arm the termination handlers for the first browser this command opens.
    # Setting a flag claims no signal slot, so a command that never starts a
    # browser leaves the process's dispositions exactly as it found them (#96).
    signals.auto_install = True

    ctx.ensure_object(dict)["observe_mode"] = observe.value
```

Add these three definitions immediately above `_setup_observe_session` (`src/graftpunk/cli/main.py:454` (`async def _setup_observe_session(`)):

```python
_OBSERVE_HANDLE_ATTR = "_graftpunk_signal_handle"


class _ObserveBrowserHandle:
    """A signal-handler grip on the browser ``gp observe`` drives directly.

    The observe command calls ``nodriver.start`` itself rather than going
    through ``NoDriverBackend``, so it registers this adapter to get the same
    SIGTERM and SIGHUP cleanup the backend gets (#96). It implements
    ``graftpunk.signals.TerminatableBrowser``.
    """

    def __init__(self, browser: Any) -> None:
        self._browser = browser

    def _terminate_for_signal(self) -> None:
        """SIGTERM the browser process and delete the temp profile it was using."""
        pid = getattr(getattr(self._browser, "_process", None), "pid", None)
        if isinstance(pid, int):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                LOG.debug("chrome_process_already_gone", pid=pid)
            except OSError as exc:
                LOG.debug("chrome_process_terminate_failed", pid=pid, error=str(exc))
        remove_browser_temp_profile(self._browser)


def _register_observe_browser(browser: Any) -> None:
    """Give the termination handler a grip on *browser*, and the stop path a way back.

    The handle rides on the browser object because ``_setup_observe_session``
    hands its caller only the browser, and the stop path has to unregister the
    same handle that was registered. The registry holds it weakly, so the
    browser object owns its lifetime.
    """
    handle = _ObserveBrowserHandle(browser)
    setattr(browser, _OBSERVE_HANDLE_ATTR, handle)
    signals.register_live_browser(handle)


def _stop_observe_browser(browser: Any) -> None:
    """Stop the observe browser, leave the signal registry, and delete its temp profile.

    nodriver removes that directory only from its own atexit handler, which
    never runs for a browser we stopped ourselves, so every observe run used to
    leak one directory under the temp dir (#96).
    """
    handle = getattr(browser, _OBSERVE_HANDLE_ATTR, None)
    if handle is not None:
        signals.unregister_live_browser(handle)
    browser.stop()
    remove_browser_temp_profile(browser)
```

In `_setup_observe_session`, insert the sweep and change the launch arguments. Replace `src/graftpunk/cli/main.py:515` (`    # Isolate Chrome from SIGINT: child process inherits SIG_IGN disposition,`) through `src/graftpunk/cli/main.py:527` (`                    browser_args=["--test-type"],`) with:

```python
    # End the Chromes earlier runs left behind before adding one more, and arm
    # the termination handlers (#96). gp observe launches nodriver directly
    # rather than through NoDriverBackend, so it calls the same helpers the
    # backend does. This is the interactive path, so it says what it did.
    report = await asyncio.to_thread(cleanup_orphans_before_launch)
    if report.reaped:
        console.print(
            f"[dim]Cleaned up {len(report.reaped)} orphaned Chrome process(es) "
            f"from earlier runs.[/dim]"
        )

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
                    browser_args=base_browser_args(),
                )
```

Register the browser as soon as the retry block is done, on line 549, the `try:` that opens the block whose first statement is `src/graftpunk/cli/main.py:550` (`        tab = browser.main_tab`). Replace that `try:` line with:

```python
    _register_observe_browser(browser)
    try:
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

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/unit/test_observe_interactive.py tests/unit/test_observe_session_naming.py tests/unit/test_cli.py -q`

Expected: PASS, including the 6 new tests and the existing SIGINT-isolation test at `tests/unit/test_observe_interactive.py:405` (`            captured_handler.append(signal.getsignal(signal.SIGINT))`), which still sees `SIG_IGN` during `nodriver.start` because the sweep runs before that block, not inside it.

- [ ] **Step 5: Run the full gate**

Run: `NO_COLOR=1 FORCE_COLOR= uv run pytest tests/ -q && uvx ruff check . && uvx ruff format --check . && uvx ty@0.0.75 check src/`

Expected: all four green.

- [ ] **Step 6: Commit**

```bash
git add src/graftpunk/cli/main.py tests/unit/test_observe_interactive.py
git commit -m "fix(observe): mark, register and clean up the browser gp observe launches (#96)"
```

> **Design note (2026-09-07, SOLID 3):** the observe path registers `_ObserveBrowserHandle`, a five line adapter around the `nodriver` browser it drives directly, so a SIGTERM during a recording ends that browser too. Before this, only a `NoDriverBackend` browser was covered, which is the launch site the issue's orphans did not come from.
>
> **Design note (2026-09-07, SOLID 8):** the one-line "Cleaned up N orphaned Chrome process(es) from earlier runs." message lives here and nowhere else. This is the interactive command where a start that takes a few seconds longer needs explaining; a plugin login writing to a pipe does not.
>
> **Design note (2026-09-07, SOLID 16):** `main_callback` sets `signals.auto_install` and touches no signal slot. `test_the_callback_sets_the_flag` asserts both halves: the flag is set, and `gp version` leaves SIGTERM at `SIG_DFL`.
>
> **Design note (2026-09-07):** the spec says to remove the temp profile "after its `browser.stop()`". There are three such calls in this module, so they become one `_stop_observe_browser` helper rather than three copies. The failure path inside `_setup_observe_session` is included deliberately: a browser that failed on the way up leaked its profile exactly like one that succeeded.

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

**A marker on every browser.** Each launch passes `--graftpunk-owner-pid=<pid>` in `browser_args`, beside the `--test-type` switch graftpunk has always passed. Chrome ignores switches it does not recognise, and the marker survives into the command line that `ps` reports, so a later run can tell graftpunk's own browsers apart from the user's and knows which process owned each one. It is on the browser process only: Chrome copies an allowlisted subset of switches to its renderer and GPU helpers, and a vendor switch is not on that list. Ending the browser process is what ends its helpers.

**A sweep before each start.** Before `nodriver.start`, both launch sites (the `NoDriverBackend` and `gp observe`) scan `ps` for processes carrying `--remote-debugging-port`. One whose marker names a pid that is no longer alive is ended with SIGTERM, then SIGKILL after a three second grace period. A narrower second rule covers browsers launched before the marker existed: no marker, a nodriver temp profile as its `--user-data-dir`, and a parent of pid 1. A browser without a debugging port, and any browser whose owner is still alive, is never touched. The same pass then deletes nodriver temp profiles that no live browser names in its `--user-data-dir` and that are more than an hour old. Set `GRAFTPUNK_KEEP_ORPHANED_CHROME=1` to skip all of it, for a workflow that deliberately leaves detached browsers running.

**A profile deleted on every stop.** nodriver creates its temp profile with `tempfile.mkdtemp(prefix="uc_")` and deletes it only from an `atexit` handler that iterates its registry of live browsers. graftpunk removes its browser from that registry on stop, to keep that handler's output off stdout, so nothing used to delete the directory and every session leaked one. Both stop paths now delete it themselves. A custom `profile_dir` is never touched: nodriver's own `uses_custom_data_dir` flag says which is which, and only a directory sitting directly under the temp directory is ever removed.

**Signals.** A live browser registers a handle with `graftpunk.signals`, and the SIGTERM and SIGHUP handlers terminate every registered browser, restore the default disposition, and re-raise the signal, so the exit status stays the conventional 128 + signum. A slot a host program already handles is left alone. Importing graftpunk installs nothing, and neither does running a `gp` command that opens no browser: the CLI sets `graftpunk.signals.auto_install`, and the handlers are armed by the first browser launch. A library consumer opts in by calling `graftpunk.signals.install_termination_cleanup()` or by setting the same flag. SIGKILL and the OOM killer cannot be caught, which is what the start-time sweep is for.

Chrome shares the Python process group. nodriver launches it with `asyncio.create_subprocess_exec` and passes no `start_new_session`, so the child inherits its parent's session and process group: a terminal's SIGHUP and `kill -- -<pgid>` reach Chrome without any process-group handling in graftpunk.
```

- [ ] **Step 2: Add the environment variable to the README table**

In `README.md`, add this row at the end of the Configuration table that starts at `./README.md:389` (`| Variable | Default | Description |`), directly below the `GRAFTPUNK_BROWSER_EXECUTABLE_PATH` row on line 396:

```markdown
| `GRAFTPUNK_KEEP_ORPHANED_CHROME` | _(unset)_ | Set to `1` to keep the Chrome processes earlier runs left behind, instead of ending them before a `nodriver` browser starts |
```

- [ ] **Step 3: Add the `[Unreleased]` changelog section**

`CHANGELOG.md` has no `[Unreleased]` heading: 1.16.0 was released on 2026-09-07 and its section is the first one in the file. Insert this immediately above `CHANGELOG.md:8` (`## [1.16.0] - 2026-09-07`):

```markdown
## [Unreleased]

### Added

- **Orphaned Chrome cleanup** ([#96](https://github.com/stavxyz/graftpunk/issues/96)). When the Python process that launched a `nodriver` browser dies without running its cleanup (SIGKILL, an OOM kill, a closed terminal, a crash), Chrome survives and keeps its debug port and its memory. Every graftpunk launch now passes `--graftpunk-owner-pid=<pid>` in `browser_args`, which Chrome ignores and `ps` reports, and every launch first ends the browsers whose owner is gone: SIGTERM, then SIGKILL after a three second grace period. A narrower rule covers browsers launched before the marker existed (no marker, a nodriver temp profile, and a parent of pid 1). A browser without a debugging port, and any browser whose owner is still alive, is never touched. The pass also deletes nodriver temp profiles that no live browser names and that are more than an hour old. POSIX only: on Windows nothing is scanned and nothing is signalled.
- **SIGTERM and SIGHUP end the browser** ([#96](https://github.com/stavxyz/graftpunk/issues/96)). A live browser registers a handle with the new `graftpunk.signals` module, whose handlers terminate every registered browser, restore the default disposition, and re-raise the signal, so the exit status stays the conventional 128 + signum and a supervisor still sees the real cause. A signal slot a host program already handles is left alone. Importing graftpunk installs nothing, and neither does a `gp` command that opens no browser: the CLI sets `graftpunk.signals.auto_install` and the first browser launch arms the handlers. A library consumer opts in with `graftpunk.signals.install_termination_cleanup()`.
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

> **Design note (2026-09-07, SOLID 1, 2 and 16):** the documentation names `graftpunk.signals.install_termination_cleanup()`, the library path, and says plainly that a `gp` command which opens no browser claims no signal slot.
>
> **Design note (2026-09-07, fact-check F2, F3 and F5 to F7):** the docs say the marker is on the browser process only and that helpers are ended by ending their browser, and they do not repeat the unverified claim about `--test-type` suppressing Chrome's unsupported-flag banner.
>
> **Design note (2026-09-07):** the spec's Documentation section names `docs/HOW_IT_WORKS.md` and `CHANGELOG.md`. The README row is added as well, because `GRAFTPUNK_KEEP_ORPHANED_CHROME` is a user-facing environment variable and every other one is listed in that table.
