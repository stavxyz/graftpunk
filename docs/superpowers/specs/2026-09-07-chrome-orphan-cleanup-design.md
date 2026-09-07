---
type: spec
---

# Orphaned Chrome cleanup for nodriver sessions

**Issue:** https://github.com/stavxyz/graftpunk/issues/96
**Date:** 2026-09-07
**Status:** design, awaiting approval

## Problem

When the Python process that launched a nodriver Chrome dies without running
its cleanup (SIGKILL, OOM, a closed terminal, a crash), Chrome survives. The
survivors hold their debug ports, consume memory, and accumulate; the issue
records eight of them from two dead sessions persisting for days. The existing
cleanup (`BrowserSession.__aexit__`, `quit()`, nodriver's `atexit` handler)
runs only when Python exits normally.

Three facts measured on this machine on 2026-09-07 (nodriver 0.48.1, macOS,
Python 3.12) reshape the issue's proposal:

1. **Chrome already shares the Python process group.** nodriver launches Chrome
   with `asyncio.create_subprocess_exec(exe, *params, stdin=PIPE, stdout=PIPE,
   stderr=PIPE, close_fds=is_posix)` in `nodriver/core/browser.py` and passes
   no `start_new_session`, so the subprocess inherits the parent's session and
   process group. A probe confirmed it: Chrome pid 88035 and Python pid 88033
   both reported pgid 88029. The issue's "process group management" approach
   needs no code; terminal SIGHUP and `kill -- -<pgid>` already reach Chrome.
2. **Every stop leaks the temp profile.** nodriver creates the profile with
   `tempfile.mkdtemp(prefix="uc_")` (`nodriver/core/config.py`) and removes it
   only in its `atexit` handler, `deconstruct_browser`, which iterates
   nodriver's registry of live browsers. graftpunk removes its browser from
   that registry on stop (`src/graftpunk/backends/nodriver.py:408` (`def _deregister_browser`))
   to silence nodriver's stdout chatter, so nodriver never removes the
   directory and graftpunk does not either. The probe went from 163 to 164
   `uc_*` directories under `$TMPDIR` across one start and stop. Those 163
   directories are the accumulated leak on this machine.
3. **A marker switch survives into Chrome's command line.** Passing
   `--graftpunk-owner-pid=<pid>` through `browser_args` to `nodriver.start`
   showed up in `ps` for the Chrome process (Chrome ignores switches it does
   not know; `--test-type`, which graftpunk already passes at
   `src/graftpunk/backends/nodriver.py:315` (`browser_args = ["--test-type"]`),
   suppresses the "unsupported flag" banner). That gives exact identification of
   graftpunk's own Chromes and of who owned them, without a process library.

`psutil` is not installed and is not a dependency. `ps` is present on macOS and
Linux. The CLI handles only SIGINT today
(`src/graftpunk/cli/main.py:517` (`old_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)`),
`src/graftpunk/cli/main.py:616` (`loop.add_signal_handler(signal.SIGINT, stop_event.set)`)).
The `gp observe` interactive path launches nodriver directly
(`src/graftpunk/cli/main.py:524` (`browser = await nodriver.start(`)) rather than
through `NoDriverBackend`, so anything the backend does must reach it too.

## Design

Three parts. The first is the backstop the issue asks for and covers every way
a parent can die. The second stops the leak that makes the first necessary so
often. The third covers the catchable signals.

### Part 1: orphan detection and reaping, `src/graftpunk/backends/chrome_orphans.py`

A new module with no graftpunk dependencies beyond `graftpunk.logging`, so the
backend and the CLI can both import it.

```python
OWNER_SWITCH = "--graftpunk-owner-pid"

def owner_switch(pid: int | None = None) -> str:
    """The marker switch for this process: ``--graftpunk-owner-pid=<pid>``."""

@dataclass(frozen=True)
class ChromeProcess:
    pid: int
    ppid: int
    args: str
    owner_pid: int | None          # from the marker switch, when present
    user_data_dir: str | None      # from --user-data-dir=, when present

def list_chrome_processes(process_table: Callable[[], str] | None = None) -> list[ChromeProcess]:
    """Every process whose argv carries --remote-debugging-port. POSIX only; [] elsewhere.

    process_table defaults to running ``ps -eo pid=,ppid=,args=``; tests pass a fake."""

def find_orphans(processes: Iterable[ChromeProcess], *, pid_alive: Callable[[int], bool] = _pid_alive) -> list[ChromeProcess]:
    """The subset that nobody owns any more:
       (a) the marker names an owner pid that is not alive, or
       (b) no marker, the profile is a nodriver temp profile (``/uc_`` in --user-data-dir), and ppid == 1.
    A process whose owner is alive, or a Chrome without a debugging port (the user's browser), never matches."""

def reap_orphans(*, grace_seconds: float = 3.0, kill: Callable[[int, int], None] = os.kill, ...) -> list[ChromeProcess]:
    """SIGTERM each orphan, wait up to grace_seconds for it to leave the table, SIGKILL the rest,
       then remove each orphan's temp profile directory when it is a ``uc_*`` directory under tempfile.gettempdir().
       Per-process failures are logged and never raise. Returns the orphans acted on."""

def remove_stale_temp_profiles(*, older_than_seconds: float = 3600.0) -> list[Path]:
    """Remove ``uc_*`` directories under tempfile.gettempdir() that no live process references
       in its argv and whose mtime is older than the threshold. The age guard keeps a
       directory another process created moments ago, before its Chrome shows in ps."""
```

Rule (b) exists for orphans launched before this change (they carry no marker)
and for other nodriver users' orphans; a process reparented to pid 1 has no
living parent by definition. On Linux under a systemd user session, orphans may
be reparented to a subreaper other than pid 1; rule (b) then does not fire and
those legacy orphans survive until the marker rule covers them on the next
launch. That is the conservative direction: the rule never kills a process that
might still be someone's.

Rule (a) treats "owner pid not alive" as `os.kill(pid, 0)` raising
`ProcessLookupError`. A reused pid makes the reaper skip an orphan, never kill a
live owner's Chrome.

Killing the browser process takes its renderer and GPU helpers with it (they
exit when their parent's IPC channel closes). Helpers that show up in the table
with the same switches are matched and handled by the same rules, so a helper
whose browser process is already gone is covered too.

Opt-out: `GRAFTPUNK_KEEP_ORPHANED_CHROME=1` (a `Settings` field beside
`browser_executable_path` at `src/graftpunk/config.py:87` (`browser_executable_path: str | None = Field(`))
skips Part 1 entirely, for users who deliberately keep detached browsers.

Structured events: `chrome_orphan_found` (pid, ppid, owner_pid, user_data_dir,
rule), `chrome_orphan_terminated`, `chrome_orphan_killed`,
`chrome_orphan_profile_removed`, `chrome_orphan_cleanup_failed` (pid, error),
`chrome_temp_profile_removed`. The CLI prints one line when it reaped anything
("Cleaned up N orphaned Chrome process(es) from earlier runs.") so the user
learns why a start took a few seconds longer.

Windows: `list_chrome_processes` returns `[]` with one debug log; nothing else
changes. The issue's problem statement is about POSIX parent death.

### Part 2: the backend marks, reaps, and stops leaking

`NoDriverBackend._start_async` (`src/graftpunk/backends/nodriver.py:297` (`async def _start_async(self, _max_attempts: int = 3) -> None:`)):

- Before the first `uc.start` attempt, when the opt-out is not set:
  `reap_orphans()` then `remove_stale_temp_profiles()`. Both are bounded (the
  grace period) and never raise into the start path.
- `browser_args` gains `owner_switch()` next to `--test-type`.

`NoDriverBackend._stop_async` (`src/graftpunk/backends/nodriver.py:424` (`async def _stop_async(self) -> None:`)):
after `_reap_browser_process` returns, remove the browser's profile directory
when `not browser.config.uses_custom_data_dir` (nodriver's own flag for "we
created a temp dir"), with `shutil.rmtree(..., ignore_errors=True)` and a
`chrome_temp_profile_removed` event. A custom `profile_dir` is never touched.
This is the fix for finding 2.

The backend also exposes `terminate_browser_process(self) -> None`: a
synchronous, loop-free method that sends SIGTERM to the Chrome subprocess it
launched and removes its temp profile, for use from a signal handler (Part 3).
It touches only `self._browser._process` and the profile path, never the event
loop, so it is safe where `stop()` (which runs `asyncio.run`) is not.

The `gp observe` interactive path (`src/graftpunk/cli/main.py:524` (`browser = await nodriver.start(`))
calls `reap_orphans()` and `remove_stale_temp_profiles()` before its own
`nodriver.start`, adds `owner_switch()` to its `browser_args`, and removes the
temp profile after its `browser.stop()`. Routing that path through
`NoDriverBackend` instead is a larger refactor and out of scope; the three
shared helpers keep the two launch sites in step.

### Part 3: SIGTERM and SIGHUP end the browser too

`src/graftpunk/session.py` keeps a module-level `weakref.WeakSet` of live
`BrowserSession` instances, added in `__init__` and discarded in `quit()`
(`src/graftpunk/session.py:536` (`    def quit(self) -> None:`)) and
`_quit_async`. `BrowserSession.terminate_browser_process()` forwards to the
backend's method when the backend is nodriver.

A new `src/graftpunk/cli/signals.py` with `install_termination_cleanup()`: for
each of SIGTERM and SIGHUP whose current disposition is `SIG_DFL`, install a
handler that calls `terminate_browser_process()` on every live session (each
call wrapped, failures logged), logs `termination_signal_received` with the
signal name, restores `SIG_DFL`, and re-raises the same signal at the process
(`os.kill(os.getpid(), signum)`) so the exit status is the conventional
128 + signum and any parent supervisor sees the real cause. The CLI's
`main_callback` (`src/graftpunk/cli/main.py:122` (`@app.callback(invoke_without_command=True)`))
calls it once. Importing `graftpunk` as a library installs nothing; a host that
wants the behaviour calls `install_termination_cleanup()` itself. A slot that
is not `SIG_DFL` (a host already handles it) is left alone and logged at debug.

SIGKILL and the OOM killer remain uncatchable; Part 1 is their backstop, as the
issue says.

### Acceptance criteria, mapped

- Stale nodriver Chromes detected and killed before launch: Part 1 + Part 2.
- Only nodriver-launched Chromes targeted: the marker rule is exact; the
  legacy rule requires the `uc_` profile, a debugging port, and ppid 1.
- Same process group: already true; documented in the module docstring and
  `docs/HOW_IT_WORKS.md`, with the probe's evidence.
- SIGTERM and SIGHUP trigger cleanup: Part 3.
- Structured logging: the events above.
- Tests: below.

### Error handling

Nothing in Parts 1 to 3 may turn a browser start into a failure or a signal
into a hang. `reap_orphans` and `remove_stale_temp_profiles` catch per-item
`OSError` and log; a `ps` that is missing or fails yields an empty table and
one warning. The signal handler's work is bounded to one SIGTERM per session
plus one `rmtree`, then the default action.

### Testing

Unit tests inject the process table and the kill function; none touches a real
Chrome:

- `list_chrome_processes` parses a fixture `ps` output (macOS and Linux
  shapes, args containing spaces, a Chrome without a debugging port that must
  not appear).
- `find_orphans`: marker with dead owner (matched), marker with live owner
  (skipped), legacy `uc_` profile with ppid 1 (matched), `uc_` profile with a
  living parent (skipped), custom profile with ppid 1 and no marker (skipped).
- `reap_orphans` with a fake table that drops a pid after SIGTERM (terminated,
  no SIGKILL), one that ignores SIGTERM (SIGKILL after the grace), one whose
  kill raises `ProcessLookupError` (logged, continues), and profile removal
  only for `uc_*` directories under the temp dir (a directory outside it is left).
- `remove_stale_temp_profiles`: an old unreferenced `uc_` dir is removed; a
  fresh one is kept; one referenced by a table entry is kept.
- One real-process test on POSIX: spawn `python -c "import time; time.sleep(60)" --remote-debugging-port=0 --graftpunk-owner-pid=<a pid that does not exist>`
  and assert `reap_orphans()` (real `ps`, real `os.kill`) ends it and reports
  it; the same spawn with `--graftpunk-owner-pid=<os.getpid()>` is left alive
  and is killed by the test's own teardown.
- Backend: `_start_async` adds the marker to `browser_args` and calls the
  reaper before `uc.start` (nodriver patched as the existing backend tests
  do); `_stop_async` removes a temp profile and leaves a custom `profile_dir`.
- Signals: `install_termination_cleanup` leaves a non-default slot alone;
  a subprocess test sends SIGTERM to a child Python that has a registered
  fake session and asserts the child exits with 143 and the fake session's
  `terminate_browser_process` ran (observed through a file the fake writes).
- The leak fix is also covered by counting `uc_*` directories across a
  mocked start and stop.

### Documentation

`docs/HOW_IT_WORKS.md` gains a short "Browser process hygiene" subsection:
what is cleaned at start, the marker switch, the opt-out, the signals the CLI
handles, and the process-group fact. `CHANGELOG.md` `[Unreleased]`: Added
(orphan cleanup, SIGTERM/SIGHUP handling, the opt-out) and Fixed (the temp
profile leak), referencing #96.
