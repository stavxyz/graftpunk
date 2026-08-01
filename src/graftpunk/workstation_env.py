"""Workstation env file: parse, resolve, and write ~/.config/graftpunk/env.

Single owner of file knowledge AND of the env-beats-file precedence rule
(see lookup()). Import-light: imports graftpunk.paths, never graftpunk.config
(config imports this module).

Spec: docs/rfcs/2026-07-28-workstation-env.md
"""

import contextlib
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from typing import Literal

# Importing graftpunk.paths inits the graftpunk package, whose __init__
# module-level-imports config (and the client stack). No cycle results:
# config's workstation_env import is function-local (inside get_settings).
from graftpunk import paths
from graftpunk.logging import get_logger

LOG = get_logger(__name__)

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_COMMAND_RE = re.compile(r"^\$\((.*)\)$", re.DOTALL)

STATIC = "static"
COMMAND = "command"


@dataclass(frozen=True)
class Entry:
    name: str
    raw_value: str
    kind: Literal["static", "command"]
    line_no: int
    command: str | None = None  # inner text of $(...) for COMMAND entries


@dataclass(frozen=True)
class _Classified:
    kind: Literal["static", "command"]
    cleaned: str
    command: str | None
    warn_mixed: bool  # static value containing $( that was NOT single-quoted


def _classify(value: str) -> _Classified:
    """Classify a raw value string. The ONLY owner of quote semantics.

    Quote handling runs first: a fully single-quoted value is ALWAYS static
    (the escape hatch for a literal ``$(``, and never warned about); a fully
    double-quoted value is unquoted and then command detection proceeds. A
    value is a command iff the ENTIRE value is one ``$(...)``. Mixed
    literal+command is static, flagged for a warning (warn_mixed) — the
    caller decides from this flag, never by re-sniffing quotes.
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return _Classified(STATIC, value[1:-1], None, warn_mixed=False)
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
    match = _COMMAND_RE.match(value)
    if match:
        return _Classified(COMMAND, value, match.group(1), warn_mixed=False)
    return _Classified(STATIC, value, None, warn_mixed="$(" in value)


class WorkstationEnv:
    def __init__(self, entries: dict[str, Entry]):
        self._entries = entries
        self._memo: dict[str, str | None] = {}
        # Companion to _memo, populated only on command failure. Kept
        # private/separate rather than folded into _memo's value type so
        # lookup()'s public contract (Optional[str]) never changes; only
        # last_resolve_error() (the CLI's --resolve path) reads it.
        self._memo_stderr: dict[str, str] = {}

    def entries(self) -> list[Entry]:
        return sorted(self._entries.values(), key=lambda e: e.line_no)

    def get(self, name: str) -> Entry | None:
        return self._entries.get(name)

    def resolve_file_value(self, name: str) -> str | None:
        """FILE-tier resolution only — never consults os.environ.

        Statics return their value; commands execute via /bin/sh once per
        process (results AND failures memoized). This is the single owner
        of command-execution mechanics, and the public primitive behind
        `gp config get --resolve` (which deliberately bypasses the env
        tier: it answers "what does the FILE say").
        """
        entry = self._entries.get(name)
        if entry is None:
            return None
        if entry.kind == STATIC:
            return entry.raw_value
        if name in self._memo:
            return self._memo[name]
        result = subprocess.run(  # noqa: S603
            ["/bin/sh", "-c", entry.command],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            LOG.error(
                "workstation_env_command_failed",
                name=name,
                returncode=result.returncode,
                stderr=stderr,
            )
            self._memo[name] = None
            self._memo_stderr[name] = stderr
            return None
        value = result.stdout
        if value.endswith("\n"):
            value = value[:-1]
        self._memo[name] = value
        return value

    def last_resolve_error(self, name: str) -> str | None:
        """Stderr text from NAME's most recent failed command resolution.

        None if NAME has never failed (or hasn't been resolved yet). A
        narrow companion to resolve_file_value()'s memoized None, for the
        CLI's `--resolve` path (get_cmd) to show the user WHY a command
        failed — lookup()'s own contract stays a plain Optional[str].
        """
        return self._memo_stderr.get(name)

    def lookup(self, name: str) -> str | None:
        """THE precedence rule: real env -> file -> None.

        A non-empty os.environ value wins (an empty-string value counts as
        a miss — deliberate, unified across all consumption points).
        Callers never hand-sequence env-then-file; this is the only
        implementation of the ordering.
        """
        env_value = os.environ.get(name)
        if env_value:
            return env_value
        return self.resolve_file_value(name)

    def command_entry_names(self) -> set[str]:
        return {e.name for e in self._entries.values() if e.kind == COMMAND}

    def inject_static(self) -> None:
        """Bootstrap: set static entries into os.environ where absent.

        Deliberate asymmetry with lookup(): injection gates on PRESENCE
        (an operator's explicit empty-string export is respected and never
        overwritten), while lookup() treats empty-string as a miss (an
        empty credential is never useful; fall to the file tier).
        """
        for entry in self._entries.values():
            if entry.kind == STATIC and entry.name not in os.environ:
                os.environ[entry.name] = entry.raw_value


def _parse(text: str, source: str) -> dict[str, Entry]:
    entries: dict[str, Entry] = {}
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, sep, raw = line.partition("=")
        name = name.strip()
        if not sep or not _NAME_RE.match(name):
            LOG.warning("workstation_env_malformed_line", source=f"{source}:{line_no}")
            continue
        classified = _classify(raw)
        if classified.warn_mixed:
            LOG.warning(
                "workstation_env_partial_substitution_unsupported",
                name=name,
                source=f"{source}:{line_no}",
            )
        entries[name] = Entry(
            name=name,
            raw_value=classified.cleaned,
            kind=classified.kind,
            line_no=line_no,
            command=classified.command,
        )
    return entries


_cached: WorkstationEnv | None = None


def load() -> WorkstationEnv:
    """Parse the workstation env file (absent file -> empty), cached per-process.

    Cache coherence: set_entry()/unset_entry() invalidate this cache, so a
    write-then-lookup in one process always sees fresh entries.
    """
    global _cached
    if _cached is not None:
        return _cached
    path = paths.workstation_env_path()
    if not path.is_file():
        _cached = WorkstationEnv({})
        return _cached
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        LOG.warning(
            "workstation_env_permissive_mode",
            path=str(path),
            mode=oct(mode),
            hint="expected 0600 file permissions",
        )
    _cached = WorkstationEnv(_parse(path.read_text(encoding="utf-8"), str(path)))
    return _cached


def _invalidate_cache() -> None:
    global _cached, _bootstrapped
    _cached = None
    _bootstrapped = False


_bootstrapped = False


def ensure_bootstrap() -> None:
    """Idempotent: parse the file and inject statics, once per process.

    Called by config.get_settings() (before first model construction —
    which makes the ordering invariant structural for every entry path,
    CLI and library alike) and by cli/main.py at module scope (only so
    statics precede the early logging config).
    """
    global _bootstrapped
    if _bootstrapped:
        return
    load().inject_static()
    _bootstrapped = True


def _atomic_write(path, lines: list[str]) -> None:
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".env-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("".join(lines))
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    os.chmod(path, 0o600)


def _entry_line_pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf"^\s*{re.escape(name)}\s*=")


def set_entry(name: str, value: str) -> None:
    """Add or surgically replace NAME=value; preserves every other line.

    Replaces the LAST occurrence when the name is duplicated (read semantics
    are last-wins), warning about the others. Appends new names at the end.
    Atomic write (temp + os.replace), 0600 re-asserted.
    """
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid variable name: {name!r}")
    if "\n" in value or "\r" in value:
        # A newline in value would let a single set_entry() call inject a
        # second, attacker-controlled NAME=value line (e.g.
        # K=$'x\nEVIL=1') into the file we otherwise write one line at a
        # time.
        raise ValueError(f"value for {name!r} must not contain newlines")
    path = paths.workstation_env_path()
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.is_file() else []
    pattern = _entry_line_pattern(name)
    hits = [i for i, line in enumerate(lines) if pattern.match(line)]
    if len(hits) > 1:
        LOG.warning("workstation_env_duplicate_entries", name=name, count=len(hits))
    if hits:
        lines[hits[-1]] = f"{name}={value}\n"
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{name}={value}\n")
    _atomic_write(path, lines)
    _invalidate_cache()


def unset_entry(name: str) -> int:
    """Remove ALL occurrences of NAME (git-config --unset-all semantics).

    Duplicate lines are never intentional; removing only one would silently
    resurrect an earlier occurrence. Returns the number of lines removed.
    """
    path = paths.workstation_env_path()
    if not path.is_file():
        return 0
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    pattern = _entry_line_pattern(name)
    kept = [line for line in lines if not pattern.match(line)]
    removed = len(lines) - len(kept)
    if removed > 1:
        LOG.warning("workstation_env_removed_duplicates", name=name, count=removed)
    if removed:
        _atomic_write(path, kept)
        _invalidate_cache()
    return removed


def ensure_file() -> None:
    """Create the workstation env file (empty, 0600, parents) if absent.

    The public create-if-absent operation — callers (e.g. `gp config edit`)
    must never reach for the private _atomic_write.
    """
    path = paths.workstation_env_path()
    if not path.is_file():
        _atomic_write(path, [])
