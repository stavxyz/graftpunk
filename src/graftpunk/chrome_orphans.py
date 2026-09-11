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

POSIX only. The default process table comes from ``ps -ww -eo pid=,ppid=,args=``,
which Windows does not have: there the reader returns nothing and the two
functions that signal or delete return nothing. An injected table is still
parsed, so the parsing tests are meaningful on any platform. ``-ww`` disables
``ps``'s line-length truncation of the args column, verified on this macOS;
it is also a valid flag for procps ``ps`` on Linux.

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
_PS_ARGV = ["ps", "-ww", "-eo", "pid=,ppid=,args="]
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

    A non-positive pid is never a single process to signal (0 means the
    caller's process group, negative means a process group, and os.kill on
    either can reach more than one process), so it is reported alive without
    asking the kernel: a parsed ``--graftpunk-owner-pid=-1`` must never be
    treated as a dead owner.
    """
    if pid <= 0:
        return True
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
    # The same switch parser the marker and the profile use, not a substring
    # of the argv: a process merely mentioning the switch name is not one that
    # was given a debugging port.
    if _switch_value(args, _DEBUG_PORT_SWITCH) is None:
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


def _removable_temp_profile(user_data_dir: str | Path | None) -> Path | None:
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
) -> list[Path]:
    """Delete the nodriver temp profiles that no live browser is using.

    The sweep for the profiles already leaked: directories no listed browser
    names in its ``--user-data-dir``, older than *older_than_seconds*. "In
    use" is decided from the parsed switch rather than a substring of the raw
    table, so an unrelated process that merely mentions the path does not
    protect it. The age guard is what keeps a directory another process created
    moments ago, before its Chrome has shown up in ``ps``.

    ``_switch_value`` reads a switch's value up to the next whitespace, so a
    temp directory whose path contains a space would come back truncated for
    every live browser under it, and none of them would then match a
    candidate's real path in ``in_use``. Rather than risk deleting a live
    browser's profile out from under it, the sweep skips entirely when the
    temp root itself contains whitespace.

    Args:
        older_than_seconds: How old a directory must be to count as abandoned.
        ops: The operating system calls to use.

    Returns:
        The directories removed. Empty when the module is disarmed, off
        POSIX, or the temp root contains whitespace.
    """
    if not _armed_for("sweep"):
        return []

    temp_root = Path(tempfile.gettempdir())
    if any(char.isspace() for char in str(temp_root)):
        LOG.debug("stale_profile_sweep_skipped", reason="temp root contains whitespace")
        return []

    ops = ops or DEFAULT_OPS
    in_use = {
        os.path.normpath(proc.user_data_dir)
        for proc in list_chrome_processes(ops)
        if proc.user_data_dir
    }
    try:
        candidates = sorted(
            path
            for path in temp_root.iterdir()
            if _removable_temp_profile(path) is not None and path.is_dir()
        )
    except OSError as exc:
        LOG.warning("chrome_orphan_cleanup_failed", pid=None, error=str(exc))
        return []

    removed = []
    for path in candidates:
        if os.path.normpath(str(path)) in in_use:
            continue
        try:
            age = time.time() - path.stat().st_mtime
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
    path = _removable_temp_profile(browser_temp_profile(browser))
    if path is None or not path.exists():
        return None
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        LOG.debug("chrome_temp_profile_remove_failed", path=str(path))
        return None
    LOG.debug("chrome_temp_profile_removed", path=str(path))
    return path


def terminate_nodriver_browser(browser: Any, *, ops: ProcessOps | None = None) -> None:
    """Send the Chrome subprocess behind *browser* one SIGTERM. Never raises.

    The whole sequence a signal handler is allowed to run: find the pid, signal
    it once, log what happened. Both implementations of
    :class:`graftpunk.signals.TerminatableBrowser` delegate here, so the
    backend's browser and the one ``gp observe`` drives directly are ended the
    same way and the sequence is written once.

    It deliberately does not delete the temp profile. A handler runs between
    the signal arriving and the process dying, and an ``rmtree`` there is
    unbounded work on a directory the dying Chrome may still be writing to. The
    directory is left for a later launch's stale sweep, which removes it once
    it is older than the threshold and no live browser names it.

    Deregistering the handle is the caller's business: the backend does it in
    ``_reset_state``, and the observe stop path does it explicitly.

    Args:
        browser: A ``nodriver.Browser``, or anything carrying a ``_process``.
        ops: The operating system calls to use, and the seam tests inject.
    """
    if not _armed_for("terminate"):
        return
    pid = getattr(getattr(browser, "_process", None), "pid", None)
    if not isinstance(pid, int):
        return
    try:
        (ops or DEFAULT_OPS).kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        LOG.debug("chrome_process_already_gone", pid=pid)
        return
    except OSError as exc:
        LOG.debug("chrome_process_terminate_failed", pid=pid, error=str(exc))
        return
    LOG.debug("chrome_process_terminated", pid=pid)
