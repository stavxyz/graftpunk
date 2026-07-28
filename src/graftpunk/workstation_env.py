"""Workstation env file: parse, resolve, and write ~/.config/graftpunk/env.

Single owner of file knowledge AND of the env-beats-file precedence rule
(see lookup()). Import-light: imports graftpunk.paths, never graftpunk.config
(config imports this module).

Spec: docs/rfcs/2026-07-28-workstation-env.md
"""

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

    def entries(self) -> list[Entry]:
        return sorted(self._entries.values(), key=lambda e: e.line_no)

    def get(self, name: str) -> Entry | None:
        return self._entries.get(name)


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
    global _cached
    _cached = None
