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

from graftpunk import _distribution
from graftpunk.devtools.scaffold.pyproject_edit import (
    BothDistributions,
    CannotRaiseFloor,
    DynamicDependencies,
    FloorVerdict,
    RaisedFloor,
)
from graftpunk.devtools.scaffold.render import OtherHostCommand, graftpunk_version_floor
from graftpunk.devtools.scaffold.selection import CommandSelection
from graftpunk.har.naming import EndpointSpecError, parse_command_key
from graftpunk.logging import get_logger

LOG = get_logger(__name__)
console = Console()

plugin_app = typer.Typer(
    name="plugin", help="Scaffold, extend, inspect, upgrade, and check a graftpunk plugin project."
)


def command_selections(values: list[str]) -> tuple[CommandSelection, ...]:
    """Every ``--command`` value as a selection, refusing the first that does not parse
    with :func:`parse_command_key`'s own text (a GraphQL operation in parentheses
    included). Every scaffold command that takes ``--command`` calls this, and none
    splits a value itself."""
    selections: list[CommandSelection] = []
    for value in values:
        try:
            name, method, template, operation = parse_command_key(value)
        except EndpointSpecError as exc:
            LOG.debug("scaffold_refused", reason="bad_command")
            console.print(f"[red]--command: {escape(str(exc))}[/red]", soft_wrap=True)
            raise typer.Exit(1) from None
        selections.append(
            CommandSelection(name=name, method=method, template=template, operation=operation)
        )
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


def print_unchecked_first_plugin(owner: str | None) -> None:
    """The line ``gp plugin new`` and ``gp plugin upgrade`` print when a suite's shared
    conftest checks none of the first plugin's fixtures, which they leave as it is
    unless asked: the upgrade that adds the check (#370)."""
    if owner is None:
        return
    console.print(
        escape(
            f"tests/conftest.py: {owner}'s fixtures are unchecked; gp plugin upgrade "
            f"--plugin {owner} adds the check, which fails {owner}'s tests on any of its "
            "fixtures that lacks a sidecar."
        ),
        soft_wrap=True,
    )


def print_floor(floor: FloorVerdict) -> None:
    """One line on the project's graftpunk requirement after a write that needs
    the running graftpunk: the raise it got, or how to make it by hand."""
    at = graftpunk_version_floor()
    # A plugin requires graftpunk, whichever distribution this gp came from: it is
    # what gp plugin new writes, and a plugin requiring graftpunk-lite would put
    # both distributions into its users' environments.
    wanted = f"graftpunk[browser]>={at}"
    # graftpunk-lite[browser] records and logs in as graftpunk does.
    full_note = (
        " Developing a plugin (recording, logging in) needs the browser stack: "
        f"{_distribution.install_commands('browser')}, or switch to graftpunk."
        if _distribution.name() == _distribution.LITE and not _distribution.has_browser_stack()
        else ""
    )
    if isinstance(floor, RaisedFloor):
        line = f"pyproject.toml: {floor.name}>={at} (was >={floor.previous}); reinstall the project"
        console.print(escape(line), soft_wrap=True, highlight=False)
    elif isinstance(floor, DynamicDependencies):
        console.print(
            "[bold]Next:[/bold] "
            + escape(
                f"this project's dependencies are dynamic; make sure whatever supplies "
                f"them requires {wanted}, then reinstall.{full_note}"
            ),
            soft_wrap=True,
        )
    elif isinstance(floor, CannotRaiseFloor) and floor.requirement is None:
        console.print(
            "[bold]Next:[/bold] "
            + escape(
                f"add {wanted} to pyproject.toml's [project] dependencies and "
                f"reinstall; it has no graftpunk or graftpunk-lite requirement.{full_note}"
            ),
            soft_wrap=True,
        )
    elif isinstance(floor, BothDistributions):
        console.print(
            "[bold]Next:[/bold] "
            + escape(
                f"pyproject.toml requires both graftpunk and graftpunk-lite "
                f"({' and '.join(floor.requirements)}), which install the same files; "
                f"keep graftpunk: replace them with {wanted} and reinstall.{full_note}"
            ),
            soft_wrap=True,
        )
    elif isinstance(floor, CannotRaiseFloor):
        console.print(
            "[bold]Next:[/bold] "
            + escape(f"raise {floor.requirement} in pyproject.toml to >={at} and reinstall"),
            soft_wrap=True,
        )
