---
type: spec
---

# Orphaned Chrome cleanup for nodriver sessions

**Issue:** https://github.com/stavxyz/graftpunk/issues/96
**Date:** 2026-09-07
**Status:** approved 2026-09-07 (plan validated at a199724)

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
   that registry on stop (the backend's `_deregister_browser`, as it stood at 165459c before this change)
   to silence nodriver's stdout chatter, so nodriver never removes the
   directory and graftpunk does not either. The probe went from 163 to 164
   `uc_*` directories under `$TMPDIR` across one start and stop. Those 163
   directories are the accumulated leak on this machine.
3. **A marker switch survives into Chrome's command line.** Passing
   `--graftpunk-owner-pid=<pid>` through `browser_args` to `nodriver.start`
   showed up in `ps` for the Chrome process (Chrome ignores switches it does
   not know; `--test-type`, which graftpunk already passes at
   the backend's `browser_args` list at 165459c,
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

### Part 1: orphan detection and reaping, `src/graftpunk/chrome_orphans.py`

A new module with no graftpunk dependencies beyond `graftpunk.logging`, so the
backend and the CLI can both import it.

> **Design note (2026-09-07):** the module sits at the top level beside
> `chrome.py`, not under `backends/`: it is a process-hygiene utility with no
> backend in it, and the CLI imports it too. The API below is the shape the
> SOLID review ruled: one `ProcessOps` collaborator instead of five separate
> callable seams, `base_browser_args()` so both launch sites start from the
> same switch list, one `_removable_temp_profile` predicate for every "is this
> ours to delete" question (including the legacy orphan rule, which no longer
> uses a looser substring test), an `_ARMED` module flag the unit suite turns
> off at the origin, and a `CleanupReport` from the composition root rather
> than a count plus a print.

```python
OWNER_SWITCH = "--graftpunk-owner-pid"
_ARMED = True   # the unit suite sets this False through one autouse fixture

def owner_switch(pid: int | None = None) -> str:
    """The marker switch for this process: ``--graftpunk-owner-pid=<pid>``."""

def base_browser_args() -> list[str]:
    """["--test-type", owner_switch()]: what both launch sites start from."""

@dataclass(frozen=True)
class ProcessOps:
    """The OS calls this module makes, in one injectable place. Tests pass a fake kernel."""
    read_table: Callable[[], str] = _read_ps_table    # ps -eo pid=,ppid=,args=
    kill: Callable[[int, int], None] = os.kill
    pid_alive: Callable[[int], bool] = _pid_alive
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic

@dataclass(frozen=True)
class ChromeProcess:
    pid: int
    ppid: int
    args: str
    owner_pid: int | None          # from the marker switch, when present
    user_data_dir: str | None      # from --user-data-dir=, when present

def list_chrome_processes(ops: ProcessOps | None = None) -> list[ChromeProcess]:
    """Every process whose argv carries --remote-debugging-port. POSIX only; [] elsewhere."""

def _removable_temp_profile(user_data_dir: str | Path | None) -> Path | None:
    """The one "is this ours to delete" predicate: a directory whose name starts with
       ``uc_`` sitting directly under tempfile.gettempdir(), which is where nodriver's
       mkdtemp puts it. Every deletion site and the legacy orphan rule consult this."""

def find_orphans(processes: Iterable[ChromeProcess], *, ops: ProcessOps | None = None) -> list[ChromeProcess]:
    """The subset that nobody owns any more:
       (a) the marker names an owner pid that is not alive, or
       (b) no marker, _removable_temp_profile accepts the --user-data-dir, and ppid == 1.
    A process whose owner is alive, or a Chrome without a debugging port (the user's browser), never matches."""

def reap_orphans(*, grace_seconds: float = 3.0, poll_interval: float = 0.5,
                 ops: ProcessOps | None = None) -> list[ChromeProcess]:
    """SIGTERM each orphan, wait up to grace_seconds for it to leave the table, SIGKILL the rest,
       then remove each orphan's temp profile directory when _removable_temp_profile accepts it.
       Per-process failures are logged and never raise. Returns the orphans acted on.
       [] when _ARMED is False or off POSIX. Half-second polling: six table reads, not thirty."""

def remove_stale_temp_profiles(*, older_than_seconds: float = 3600.0,
                               ops: ProcessOps | None = None) -> list[Path]:
    """Remove the temp profiles no listed browser names in its parsed --user-data-dir and
       whose mtime is older than the threshold. The age guard keeps a directory another
       process created moments ago, before its Chrome shows in ps."""

def terminate_nodriver_browser(browser, *, ops: ProcessOps | None = None) -> None:
    """The whole sequence a signal handler may run: find the pid, one SIGTERM, one log line.
       Both TerminatableBrowser implementations delegate here. Bounded on purpose: it does
       NOT remove the temp profile (see Part 3). Deregistering the handle is the caller's."""
```

`src/graftpunk/browser_launch.py` is the composition root both launch sites
call. It is the one module that imports settings, signals and orphan cleanup
together, which is what keeps the claim above ("no graftpunk dependencies
beyond `graftpunk.logging`") true of `chrome_orphans` itself:

```python
@dataclass(frozen=True)
class CleanupReport:
    reaped: list[ChromeProcess]
    profiles_removed: list[Path]     # truthy when either list is non-empty

def prepare_browser_launch() -> CleanupReport:
    """Arm the termination handlers when the CLI asked for them and the orphan module is
       armed, apply the opt-out, run the two sweeps under separate guards.
       Never raises, and prints nothing: the caller decides what its user should see."""
```

> **Design note (2026-09-07):** three shapes from the re-review. The
> composition root lives in its own module rather than inside `chrome_orphans`,
> and is named for what a caller wants (prepare a launch) rather than for one
> of the things it does. `terminate_nodriver_browser` exists so the two
> handler implementations share one sequence instead of copying it. And
> `remove_stale_temp_profiles` no longer takes an injected clock: its tests
> backdate directories with `os.utime`, which exercises the real ``st_mtime``
> path.

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
exit when their parent's IPC channel closes). The marker is on the browser
process only: Chrome copies an allowlisted subset of switches to its helpers
(`CopySwitchesFrom` over `kSwitchNames` in
`content/browser/renderer_host/render_process_host_impl.cc`), and a vendor
switch is not on that list. A helper carries `--user-data-dir` but no marker,
so one that outlived its browser is matched only if it also carries
`--remote-debugging-port` and satisfies rule (b); a helper with neither is out
of scope for this design.

> **Design note (2026-09-07):** this paragraph previously said helpers carry
> the same switches and are handled by the same rules. The fact-check measured
> otherwise (a live probe: the renderer carried 25 of its browser's 39
> switches, including `--user-data-dir` but not the vendor ones), so the claim
> is corrected here rather than left for the implementation to inherit.

Opt-out: `GRAFTPUNK_KEEP_ORPHANED_CHROME=1` (a `GraftpunkSettings` field beside
`browser_executable_path` at `src/graftpunk/config.py:87` (`browser_executable_path: str | None = Field(`))
skips Part 1 entirely, for users who deliberately keep detached browsers.

Structured events: `chrome_orphan_found` (pid, ppid, owner_pid, user_data_dir,
rule), `chrome_orphan_terminated`, `chrome_orphan_killed`,
`chrome_orphan_profile_removed`, `chrome_orphan_cleanup_failed` (pid, error),
`chrome_temp_profile_removed`. `gp observe` prints one line when the pass
reaped anything ("Cleaned up N orphaned Chrome process(es) from earlier runs.")
so the user learns why a start took a few seconds longer. The backend logs
`chrome_orphans_cleaned` with the counts and prints nothing.

> **Design note (2026-09-07):** the printing was originally described as "the
> CLI prints one line", which in practice meant the shared cleanup function
> printing for every caller, including a plugin login writing to a pipe. The
> function now returns a `CleanupReport` and prints nothing; the interactive
> `gp observe` command is the one caller that turns it into a message.

Windows: `list_chrome_processes` returns `[]` with one debug log; nothing else
changes. The issue's problem statement is about POSIX parent death.

### Part 2: the backend marks, reaps, and stops leaking

`NoDriverBackend._start_async` (`NoDriverBackend._start_async`):

- Before the first `uc.start` attempt: `await asyncio.to_thread(prepare_browser_launch)`,
  which applies the opt-out and runs both sweeps. Bounded (the grace period)
  and it never raises into the start path. The backend logs the report's
  counts as `chrome_orphans_cleaned`.
- `browser_args` starts from `base_browser_args()` and extends it with any
  configured extras.

> **Design note (2026-09-07):** the sweep shells out to `ps` and can sleep
> through a three second grace period, so both async launch sites run it on a
> worker thread rather than blocking the event loop that is about to start a
> browser.

`NoDriverBackend._stop_async` (`NoDriverBackend._stop_async`):
after `_reap_browser_process` returns, remove the browser's profile directory
when `not browser.config.uses_custom_data_dir` (nodriver's own flag for "we
created a temp dir"), with `shutil.rmtree(..., ignore_errors=True)` and a
`chrome_temp_profile_removed` event. A custom `profile_dir` is never touched.
This is the fix for finding 2.

The backend also implements `_terminate_for_signal(self) -> None`, the
`TerminatableBrowser` protocol of Part 3: two calls, `terminate_nodriver_browser`
for the SIGTERM and then `_reset_state()` so the object is coherent afterwards.
It touches only `self._browser._process`, never the event loop, so it is safe
where `stop()` (which runs `asyncio.run`) is not, and it does not remove the
temp profile (Part 3 says why). The backend registers itself with the Part 3
registry once its browser is up and leaves the registry in `_reset_state`,
which both stop paths call from a `finally`, so the entry outlives the risky
part of a stop.

The `gp observe` interactive path (`src/graftpunk/cli/main.py:524` (`browser = await nodriver.start(`))
runs the same `prepare_browser_launch` before its own `nodriver.start`, passes
`base_browser_args()`, and registers a small `_ObserveBrowserHandle` around the
browser it drives. That handle is returned to the caller beside the browser, so
the stop path can release the registry entry and remove the temp profile after
its `browser.stop()`. Routing that path through `NoDriverBackend` instead is a
larger refactor and out of scope; the shared helpers keep the two launch sites
in step.

> **Design note (2026-09-07):** the observe path registering a handle of its
> own is new here. Without it, a SIGTERM during `gp observe` would end nothing,
> which is the launch site least likely to be covered by an orderly shutdown.
> The handle travels in the tuple `_setup_observe_session` already returns
> rather than as an attribute written onto nodriver's `Browser` object: the
> stop path has to release the same handle that was registered, and a private
> attribute on a third-party object is a coupling neither side can see.

### Part 3: SIGTERM and SIGHUP end the browser too

A new `src/graftpunk/signals.py` holds a runtime-checkable
`TerminatableBrowser` protocol with one method, `_terminate_for_signal() -> None`
(handler-only, and named accordingly), and a `weakref.WeakSet` registry with
`register_live_browser`, `unregister_live_browser` and a `live_browsers()`
snapshot. `NoDriverBackend` registers itself when its browser is up and leaves
in `_reset_state`; the `gp observe` path registers an `_ObserveBrowserHandle`
around the browser it drives directly. Nothing in the signal path knows what a
`BrowserSession` is, and `src/graftpunk/session.py` is not modified.

The same module's `install_termination_cleanup()`: for each of SIGTERM and
SIGHUP whose current disposition is `SIG_DFL`, install a handler that calls
`_terminate_for_signal()` on every registered handle (each call wrapped,
failures logged), logs `termination_signal_received` with the signal name,
restores `SIG_DFL`, and re-raises the same signal at the process
(`os.kill(os.getpid(), signum)`) so the exit status is the conventional
128 + signum and any parent supervisor sees the real cause. A slot that is not
`SIG_DFL` (a host already handles it) is left alone and logged at debug.

Every handler implementation delegates to `terminate_nodriver_browser`, which
sends one SIGTERM and logs once. It deliberately does not remove the temp
profile: a handler runs while the process is on its way out, and an `rmtree` on
a directory the dying Chrome may still be writing to is exactly the unbounded
work a handler must not do. The profile of a browser ended by a signal is left
for a later launch's stale sweep, which removes it once it is older than the
threshold and no live browser names it. The orderly stop paths still delete
their own profile immediately.

Arming is lazy. The module carries `auto_install: bool = False`; the CLI's
`main_callback` (`src/graftpunk/cli/main.py:122` (`@app.callback(invoke_without_command=True)`))
sets it, which claims no signal slot, and `browser_launch.arm_termination_handlers()`
installs the handlers at the first browser launch, and only when the orphan
module is armed: one flag means "this process may not touch signals, processes,
or directories". Importing `graftpunk` as a library installs nothing, and
neither does a `gp` command that opens no browser; a host that wants the
behaviour calls `install_termination_cleanup()` itself or sets the same flag.

> **Design note (2026-09-10):** arming is a separate call, made on the calling
> thread, and not part of `prepare_browser_launch`. `signal.signal()` succeeds
> only on the main thread, and the sweeps run under `asyncio.to_thread`, so an
> arming step inside the threaded sweep failed silently on every launch (the
> Task 3 review reproduced it: the SIGTERM slot stayed at `SIG_DFL`). Both
> launch sites call `arm_termination_handlers()` first, then await the sweep.

> **Design note (2026-09-07):** two changes from the shape first written here.
> The registry holds browser handles rather than `BrowserSession` instances, so
> the signal path does not depend on the session module and does not dispatch
> on a backend name, and `gp observe`, which never builds a `BrowserSession`,
> is covered by the same mechanism. And `main_callback` sets a flag instead of
> installing handlers, so `gp session list` no longer claims two process-wide
> signal slots for a command that opens no browser.
>
> **Design note (2026-09-07):** the handler no longer removes the temp profile.
> The first version of this part had each session's cleanup send a SIGTERM and
> `rmtree` the profile; the re-review pointed out that this is unbounded work
> inside a signal handler, on a directory the process being killed may still
> hold open. The stale sweep already exists to collect exactly these
> directories, so the handler hands them to it.

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
one warning. The signal handler's work is bounded to one SIGTERM per
registered browser and nothing else, then the default action: no directory
removal, no waiting, no second pass over the process table.

### Testing

Unit tests inject a fake `ProcessOps` (table, kill, liveness, sleep, clock);
none touches a real Chrome. The unit suite is disarmed at the origin by one
autouse fixture that sets `chrome_orphans._ARMED` False, so a test that drives
a browser launch cannot signal anything even by accident; the reaper's own
tests re-arm it and inject a fake table.

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
- `prepare_browser_launch`: the opt-out skips both sweeps; a failure in one
  sweep does not cost the other; handlers are armed only when the CLI flag is
  set and the orphan module is armed.
- `terminate_nodriver_browser`: one SIGTERM, a `ProcessLookupError` swallowed,
  and the temp profile still on disk afterwards.
- Backend: `_start_async` passes `base_browser_args()` and runs the sweep
  before `uc.start` (nodriver patched as the existing backend tests do);
  `_stop_async` removes a temp profile and leaves a custom `profile_dir`;
  a started backend is in the registry and a stopped one is not;
  `_terminate_for_signal` delegates the signal, leaves the profile alone, and
  leaves `is_running` False.
- Signals: `install_termination_cleanup` leaves a non-default slot alone; the
  registry holds handles weakly and hands out a snapshot; a subprocess test
  sends SIGTERM to a child Python that has a registered fake browser and
  asserts the child exits with 143 and the fake's `_terminate_for_signal` ran
  (observed through a file the fake writes).
- Observe: the browser is registered while it is up and released on stop, the
  handle comes back in the returned tuple, and the one-line message appears
  only when the pass reaped something.
- The leak fix is covered by asserting the specific temp profile directory is
  gone after a mocked start and stop, and that a custom `profile_dir` is not.

### Documentation

`docs/HOW_IT_WORKS.md` gains a short "Browser process hygiene" subsection:
what is cleaned at start, the marker switch (on the browser process only), the
opt-out, the signals graftpunk handles and how they are armed, and the
process-group fact. `README.md`'s configuration table gains the opt-out
variable. `CHANGELOG.md` `[Unreleased]`: Added (orphan cleanup,
SIGTERM/SIGHUP handling through `graftpunk.signals`, the opt-out) and Fixed
(the temp profile leak), referencing #96.
