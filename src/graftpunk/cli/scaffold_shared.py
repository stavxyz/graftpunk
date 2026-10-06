"""Shared state for ``gp plugin``'s scaffold commands.

``scaffold_commands.py`` (``new``) and ``scaffold_project_commands.py``
(``info``, ``add-command``, ``upgrade``, ``check``) both attach their
commands to ``plugin_app`` here, both call :func:`command_selections` to
parse a ``--command`` value, and both call :func:`print_other_host` to word
a command that calls another host; splitting any of those three per module
would give the CLI two Typer sub-apps, two parsers, or two wordings instead
of one.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.markup import escape

from graftpunk.devtools.scaffold.render import OtherHostCommand
from graftpunk.devtools.scaffold.selection import CommandSelection
from graftpunk.har.naming import EndpointSpecError, parse_command_spec
from graftpunk.logging import get_logger

LOG = get_logger(__name__)
console = Console()

plugin_app = typer.Typer(
    name="plugin", help="Scaffold, extend, inspect, upgrade, and check a graftpunk plugin project."
)


def command_selections(values: list[str]) -> tuple[CommandSelection, ...]:
    """Every ``--command`` value as a selection, refusing the first that does not parse
    with :func:`parse_command_spec`'s own text. Every scaffold command that takes
    ``--command`` calls this, and none splits a value itself."""
    selections: list[CommandSelection] = []
    for value in values:
        try:
            name, method, template = parse_command_spec(value)
        except EndpointSpecError as exc:
            LOG.debug("scaffold_refused", reason="bad_command")
            console.print(f"[red]--command: {escape(str(exc))}[/red]", soft_wrap=True)
            raise typer.Exit(1) from None
        selections.append(CommandSelection(name=name, method=method, template=template))
    return tuple(selections)


def print_other_host(other: OtherHostCommand) -> None:
    """The line ``gp plugin new`` and ``gp plugin add-command`` print for a command
    whose request is an absolute URL. ``soft_wrap`` keeps it whole for a reader
    that matches it; ``highlight=False`` keeps Rich from colouring the hosts."""
    if other.base_host is None:
        line = (
            f"{other.name} calls {other.host}; the plugin sets no base_url gp can read "
            "as a URL, so its request is an absolute URL"
        )
    else:
        line = (
            f"{other.name} calls {other.host}, not {other.base_host}; "
            "its request is an absolute URL"
        )
    console.print(escape(line), soft_wrap=True, highlight=False)
