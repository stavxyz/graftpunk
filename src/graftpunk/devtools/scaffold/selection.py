"""Which commands a scaffold render or a stub insert produces, and under which names.

An explicit selection (``--command "<name>=<METHOD> <template>"``) is checked
against the digest here, once, with every refusal; the renderer is handed the
planned commands and decides only how each stub is spelled (graft skill spec,
2026-09-21). ``render.py`` and ``insert.py`` import this module; it imports
neither.
"""

from __future__ import annotations

import keyword
import re
from collections.abc import Sequence
from dataclasses import dataclass

from graftpunk.devtools.errors import DevtoolsRefusal
from graftpunk.devtools.scaffold import policy
from graftpunk.har import naming
from graftpunk.har.digest import Endpoint, RunDigest
from graftpunk.har.naming import to_cli_name

__all__ = [
    "CommandSelection",
    "CommandSelectionError",
    "PlannedCommand",
    "command_identifier",
    "plan_command",
    "planned_commands",
]

_MAX_COMMAND_NAME = 40
# keyword.softkwlist on Python 3.13, the newest version CI runs
# (.github/workflows/python-quality.yml). Fixed here rather than read from the
# running interpreter, so a name refused on one Python is refused on every one:
# 3.11 lacks "type", which 3.12 added.
_SOFT_KEYWORDS = ("_", "case", "match", "type")
_COMMAND_NAME_RE = re.compile(rf"[A-Za-z][A-Za-z0-9_-]{{0,{_MAX_COMMAND_NAME - 1}}}")


@dataclass(frozen=True)
class CommandSelection:
    """One ``--command "<name>=<METHOD> <template>"``: an endpoint, under a name."""

    name: str
    method: str
    template: str


class CommandSelectionError(DevtoolsRefusal, ValueError):
    """An explicit selection the generator cannot render; nothing is written."""


def command_identifier(name: str) -> str:
    """The Python method name for the command a person named *name*.

    Raises:
        CommandSelectionError: *name* is not a letter followed by letters, digits,
            hyphens, and underscores within ``_MAX_COMMAND_NAME``, or it maps to
            a Python keyword.
    """
    if not _COMMAND_NAME_RE.fullmatch(name):
        raise CommandSelectionError(
            f"Command name {name!r} must start with a letter and contain only letters, "
            f"digits, hyphens, and underscores, and be at most {_MAX_COMMAND_NAME} characters."
        )
    identifier = name.replace("-", "_")
    if keyword.iskeyword(identifier) or identifier in _SOFT_KEYWORDS:
        raise CommandSelectionError(f"Command name {name!r} is a Python keyword; choose another.")
    return identifier


@dataclass(frozen=True)
class PlannedCommand:
    """One stub to render: its method name, the ``name=`` to pin when kebab-casing the
    method would not produce the agreed name, the HTTP method, and the endpoint."""

    identifier: str
    name_pin: str | None
    method: str
    endpoint: Endpoint

    @property
    def registered_name(self) -> str:
        """The name the CLI registers, by the rule the ``command`` decorator applies.
        The stub's help placeholder and the inserter's collision check both read
        it."""
        return naming.registered_name(self.name_pin, self.identifier)


def plan_command(d: RunDigest, selection: CommandSelection) -> PlannedCommand:
    """*selection* checked against *d*.

    Raises:
        CommandSelectionError: A name ``command_identifier`` refuses, a name that is
            reserved, an endpoint the digest does not hold, or one the digest marks
            ``login_flow``.
    """
    identifier = command_identifier(selection.name)
    if identifier in policy.RESERVED_COMMAND_NAMES:
        raise CommandSelectionError(
            f"Command name {selection.name!r} is reserved: every public SitePlugin "
            f"attribute, and the commands graftpunk registers for every plugin (login), "
            f"cannot be a command name."
        )
    endpoint = next(
        (
            e
            for e in d.endpoints
            if e.template == selection.template and selection.method in e.methods
        ),
        None,
    )
    shown = f"{selection.method} {selection.template}"
    if endpoint is None:
        raise CommandSelectionError(
            f"{shown} is not an endpoint in this run's digest; gp observe digest lists the "
            f"ones it holds."
        )
    if endpoint.login_flow:
        raise CommandSelectionError(
            f"{shown} is part of the login flow, which login_config drives; it cannot be a command."
        )
    name_pin = None if to_cli_name(identifier) == selection.name else selection.name
    return PlannedCommand(identifier, name_pin, selection.method, endpoint)


def planned_commands(d: RunDigest, selections: Sequence[CommandSelection]) -> list[PlannedCommand]:
    """The explicit *selections*, each checked against *d*, uncapped and in the order
    given. Only the explicit case: whether a render uses a selection or its own
    default choice is the renderer's decision.

    Raises:
        CommandSelectionError: See :func:`plan_command`, or a name given twice.
    """
    planned: list[PlannedCommand] = []
    seen: set[str] = set()
    for selection in selections:
        command = plan_command(d, selection)
        if command.identifier in seen:
            raise CommandSelectionError(f"Command name {selection.name!r} is given twice.")
        seen.add(command.identifier)
        planned.append(command)
    return planned
