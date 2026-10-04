"""``gp plugin info``, ``add-command``, ``upgrade``, and ``check``: argument
handling only, over ``graftpunk.devtools``.

``gp plugin new`` lives in ``scaffold_commands.py``; both modules attach their
commands to the ``plugin_app`` in ``scaffold_shared.py``, and
``scaffold_commands.register()`` imports this module itself before it
attaches ``plugin_app``, so this module's commands are there wherever
``register()`` is called.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from graftpunk.cli.observe_commands import resolve_run
from graftpunk.cli.scaffold_shared import LOG, command_selections, console, plugin_app
from graftpunk.devtools.errors import DevtoolsRefusal
from graftpunk.devtools.plugin_check import check_project
from graftpunk.devtools.plugin_info import info_payload
from graftpunk.devtools.plugin_project import read_project
from graftpunk.devtools.scaffold import policy
from graftpunk.devtools.scaffold.insert import add_command
from graftpunk.devtools.scaffold.pyproject_edit import CannotRaiseFloor, RaisedFloor
from graftpunk.devtools.scaffold.render import graftpunk_version_floor
from graftpunk.devtools.scaffold.upgrade import upgrade_project
from graftpunk.har.digest import DigestSource, digest
from graftpunk.har.parser import HARParseError


def _print_floor(floor: RaisedFloor | CannotRaiseFloor | None) -> None:
    """One line on the project's graftpunk requirement after a write that needs
    the running graftpunk: the raise it got, or how to make it by hand."""
    at = graftpunk_version_floor()
    if isinstance(floor, RaisedFloor):
        line = f"pyproject.toml: graftpunk>={at} (was >={floor.previous})"
        console.print(escape(line), soft_wrap=True, highlight=False)
    elif isinstance(floor, CannotRaiseFloor) and floor.requirement is None:
        console.print(
            "[bold]Next:[/bold] "
            + escape(
                f"add graftpunk>={at} to pyproject.toml's [project] dependencies and "
                "reinstall; it has no graftpunk requirement"
            ),
            soft_wrap=True,
        )
    elif isinstance(floor, CannotRaiseFloor):
        console.print(
            "[bold]Next:[/bold] "
            + escape(
                f"raise graftpunk in pyproject.toml to >={at} and reinstall; "
                f"it reads {floor.requirement}"
            ),
            soft_wrap=True,
        )


@plugin_app.command("info")
def plugin_info(
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON (the only form)")] = False,
    dir_: Annotated[Path, typer.Option("--dir", help="Project directory")] = Path("."),
) -> None:
    """Describe the plugin project in --dir: its classification, plugins, and commands."""
    if not as_json:
        console.print("[red]gp plugin info prints JSON only: pass --json.[/red]", soft_wrap=True)
        raise typer.Exit(1)
    try:
        payload = info_payload(read_project(dir_))
    except DevtoolsRefusal as exc:
        LOG.debug("plugin_info_refused", reason=type(exc).__name__)
        console.print(f"[red]{escape(str(exc))}[/red]", soft_wrap=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@plugin_app.command("add-command")
def plugin_add_command(
    plugin: Annotated[
        str,
        typer.Argument(help="The plugin's entry-point name, as gp plugin info --json reports it"),
    ],
    from_run: Annotated[
        str, typer.Option("--from-run", help="SESSION: take the endpoint from its newest run")
    ],
    command: Annotated[
        str, typer.Option("--command", help='"NAME=METHOD template": the command to add')
    ],
    run: Annotated[
        str | None, typer.Option("--run", help="RUN_ID: use this run instead of the newest one")
    ] = None,
    dir_: Annotated[Path, typer.Option("--dir", help="Project directory")] = Path("."),
) -> None:
    """Add one command stub to a plugin, in the shape gp plugin new writes."""
    (selection,) = command_selections([command])
    run_dir = resolve_run(from_run, run)
    source = DigestSource.from_run_dir(run_dir, session=from_run, run_id=run_dir.name)
    try:
        run_digest = digest(source)
    except (FileNotFoundError, HARParseError) as exc:
        LOG.debug("add_command_refused", reason="digest_load_error", har_path=str(source.har_path))
        console.print(
            f"[red]Could not read the recording: {escape(str(exc))}[/red]",
            soft_wrap=True,
        )
        raise typer.Exit(1) from None
    try:
        added = add_command(dir_, plugin, run_digest, selection)
    except DevtoolsRefusal as exc:
        LOG.debug("add_command_refused", reason=type(exc).__name__)
        console.print(f"[red]{escape(str(exc))}[/red]", soft_wrap=True)
        raise typer.Exit(1) from None
    console.print(
        f"[green]Added[/green] {escape(added.cli_name)} to {escape(str(added.module))}",
        soft_wrap=True,
    )
    if added.fixture is None:
        console.print(
            "[bold]Next:[/bold] gp observe fixtures writes no fixture for this endpoint; "
            "write its test against a fixture of your own.",
            soft_wrap=True,
        )
    else:
        console.print(
            f"[bold]Next:[/bold] its test looks for {escape(added.fixture)}", soft_wrap=True
        )
    _print_floor(added.floor)


@plugin_app.command("upgrade")
def plugin_upgrade(
    dir_: Annotated[Path, typer.Option("--dir", help="Project directory")] = Path("."),
) -> None:
    """Bring a plugin project up to the current generated shape, changing nothing it has."""
    try:
        applied = upgrade_project(dir_)
    except DevtoolsRefusal as exc:
        LOG.debug("upgrade_refused", reason=type(exc).__name__)
        console.print(f"[red]{escape(str(exc))}[/red]", soft_wrap=True)
        raise typer.Exit(1) from None
    if not applied.changed:
        console.print("Nothing to upgrade: the project already has every requirement.")
        return
    for requirement in applied.requirements:
        console.print(
            f"{escape(requirement.path)}: added {escape(requirement.name)}", soft_wrap=True
        )
    if applied.created_fixtures_tree:
        console.print(f"{escape(policy.FIXTURES_TREE)}: created", soft_wrap=True)
    _print_floor(applied.floor)


@plugin_app.command("check")
def plugin_check(
    dir_: Annotated[Path, typer.Option("--dir", help="Project directory")] = Path("."),
) -> None:
    """Lint a plugin project: markers left, one plugin class per module, project wiring."""
    findings = check_project(dir_)
    for finding in findings:
        console.print(escape(str(finding)), soft_wrap=True, highlight=False)
    if findings:
        console.print(f"[red]gp plugin check: {len(findings)} finding(s).[/red]", soft_wrap=True)
        raise typer.Exit(1)
    console.print("[green]gp plugin check: no findings.[/green]")
