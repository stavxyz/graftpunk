"""``gp plugin new``: the CLI surface for the scaffold. Argument handling only."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.console import Console
from rich.markup import escape

import graftpunk
from graftpunk.cli.observe_commands import resolve_run
from graftpunk.cli.plugin_commands import reserved_cli_names
from graftpunk.devtools.captures import CAPTURES_DIR
from graftpunk.devtools.scaffold.project import ScaffoldConflictError, write_scaffold
from graftpunk.devtools.scaffold.render import ScaffoldSpec
from graftpunk.har.digest import DigestSource, digest
from graftpunk.logging import get_logger

LOG = get_logger(__name__)
console = Console()

plugin_app = typer.Typer(name="plugin", help="Scaffold a new graftpunk plugin.")

_SUPPORTED_BACKENDS: tuple[str, ...] = ("nodriver", "selenium")
_PRERELEASE_SUFFIX_RE = re.compile(r"(a|b|rc|dev)\d*$")


def _graftpunk_version_floor() -> str:
    """The running graftpunk's release, floored to ``major.minor.0``.

    A pre-release or local checkout (``1.17.0.dev3+g1234abc``) floors at its
    base release (``1.17.0``), per the spec.
    """
    version = re.split(r"[-+]", graftpunk.__version__)[0]
    version = _PRERELEASE_SUFFIX_RE.sub("", version)
    parts = (version.split(".") + ["0", "0"])[:2]
    return f"{parts[0]}.{parts[1]}.0"


def _backend_literal(value: str) -> Literal["nodriver", "selenium"]:
    """*value*, already checked against ``_SUPPORTED_BACKENDS`` by the caller.

    The check happens before this is called; this only carries the
    narrowing so ``ScaffoldSpec`` receives a ``Literal`` without a
    suppression.
    """
    if value == "nodriver":
        return "nodriver"
    return "selenium"


@plugin_app.command("new")
def plugin_new(
    name: Annotated[str, typer.Argument(help="Plugin name: site_name, and the package suffix")],
    url: Annotated[
        str, typer.Option("--url", help="Base URL (ignored when --from-run supplies one)")
    ] = "",
    from_run: Annotated[
        tuple[str, str] | None,
        typer.Option(
            "--from-run",
            help="SESSION RUN_ID: fill the scaffold from an observe run "
            "(pass an empty RUN_ID to use the latest run)",
        ),
    ] = None,
    dir_: Annotated[Path, typer.Option("--dir", help="Target directory")] = Path("."),
    backend: Annotated[str, typer.Option("--backend", help="nodriver or selenium")] = "nodriver",
    new: Annotated[bool, typer.Option("--new", help="Force a new project in --dir")] = False,
) -> None:
    """Scaffold a new plugin: a fresh project, or a member of the suite in --dir."""
    if backend not in _SUPPORTED_BACKENDS:
        console.print(
            f"[red]--backend must be one of {_SUPPORTED_BACKENDS}, got '{escape(backend)}'[/red]"
        )
        raise typer.Exit(1)

    if name in reserved_cli_names():
        console.print(
            f"[red]'{escape(name)}' is a reserved command name and cannot be a plugin name.[/red]"
        )
        raise typer.Exit(1)

    digest_result = None
    base_url = url
    if from_run:
        session_name, run_id_raw = from_run
        run_id = run_id_raw or None

        run_dir = resolve_run(session_name, run_id)
        source = DigestSource.from_run_dir(run_dir, session=session_name, run_id=run_dir.name)
        digest_result = digest(source)
        base_url = url or f"https://{digest_result.primary_host}"

    try:
        spec = ScaffoldSpec(
            name=name,
            mode="new_project",  # write_scaffold decides the real mode
            backend=_backend_literal(backend),
            base_url=base_url,
            digest=digest_result,
            graftpunk_version=_graftpunk_version_floor(),
        )
        result = write_scaffold(dir_, spec, force_new=new)
    except ScaffoldConflictError as exc:
        console.print("[red]Refusing to overwrite existing file(s):[/red]")
        for path in exc.conflicts:
            console.print(f"  {escape(str(path))}")
        raise typer.Exit(1) from None
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None

    console.print(f"[green]{result.mode.replace('_', ' ').title()}:[/green]")
    for path in result.written:
        console.print(f"  {escape(str(path))}")
    if result.gitignore_updated:
        console.print(f"[dim]Added {escape(CAPTURES_DIR)}/ to .gitignore[/dim]")
