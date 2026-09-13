"""``gp plugin new``: the CLI surface for the scaffold. Argument handling only."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.console import Console
from rich.markup import escape

import graftpunk
from graftpunk.cli.observe_commands import resolve_run
from graftpunk.cli.plugin_commands import derive_reserved_cli_names
from graftpunk.devtools.captures import CAPTURES_DIR
from graftpunk.devtools.scaffold.project import (
    NotAPluginSuiteError,
    ScaffoldConflictError,
    write_scaffold,
)
from graftpunk.devtools.scaffold.pyproject_edit import PyprojectEditError
from graftpunk.devtools.scaffold.render import ScaffoldSpec, fixture_paths
from graftpunk.har.digest import DigestSource, digest
from graftpunk.logging import get_logger

LOG = get_logger(__name__)
console = Console()

plugin_app = typer.Typer(name="plugin", help="Scaffold a new graftpunk plugin.")

_BackendName = Literal["nodriver", "selenium"]
_SUPPORTED_BACKENDS: tuple[_BackendName, ...] = ("nodriver", "selenium")
_PRERELEASE_SUFFIX_RE = re.compile(r"(a|b|rc|dev)\d*$")

# The reserved top-level CLI names, snapshotted by register() at attach time
# (before any site plugin's own sub-app is mounted): see register()'s
# docstring for why this must be a snapshot rather than a live query.
_reserved_names: frozenset[str] = frozenset()


def register(app: typer.Typer) -> None:
    """Attach ``plugin_app`` to *app* and snapshot its reserved top-level names.

    Called from ``main.py`` right before ``register_plugin_commands(app)``
    runs, so the snapshot holds only the CLI's own built-in names (session,
    http, config, keepalive, observe, plugins, plugin, ...) and never an
    installed site plugin's ``site_name``: plugin discovery has not mounted
    anything onto *app* yet at this point. A snapshot, rather than deriving
    fresh from a live app reference, also means this module never has to
    import ``graftpunk.cli.main`` (main.py already imports this module at
    module scope; the reverse import would be a real cycle, not just a lazy
    one deferred past load time).
    """
    global _reserved_names
    app.add_typer(plugin_app)
    _reserved_names = derive_reserved_cli_names(app)


def reserved_cli_names() -> frozenset[str]:
    """The reserved set snapshotted by the last ``register()`` call.

    ``gp plugin new`` reads this to refuse a plugin name before generating
    anything.
    """
    return _reserved_names


def _graftpunk_version_floor() -> str:
    """The running graftpunk's release, floored to ``major.minor.0``.

    A pre-release or local checkout (``1.17.0.dev3+g1234abc``) floors at its
    base release (``1.17.0``), per the spec.
    """
    version = re.split(r"[-+]", graftpunk.__version__)[0]
    version = _PRERELEASE_SUFFIX_RE.sub("", version)
    parts = (version.split(".") + ["0", "0"])[:2]
    return f"{parts[0]}.{parts[1]}.0"


@plugin_app.command("new")
def plugin_new(
    name: Annotated[str, typer.Argument(help="Plugin name: site_name, and the package suffix")],
    url: Annotated[
        str, typer.Option("--url", help="Base URL (ignored when --from-run supplies one)")
    ] = "",
    from_run: Annotated[
        str | None,
        typer.Option(
            "--from-run", help="SESSION: fill the scaffold from this session's newest run"
        ),
    ] = None,
    run: Annotated[
        str | None,
        typer.Option(
            "--run", help="RUN_ID: use this run instead of the newest one (requires --from-run)"
        ),
    ] = None,
    dir_: Annotated[Path, typer.Option("--dir", help="Target directory")] = Path("."),
    backend: Annotated[str, typer.Option("--backend", help="nodriver or selenium")] = "nodriver",
    new: Annotated[bool, typer.Option("--new", help="Force a new project in --dir")] = False,
) -> None:
    """Scaffold a new plugin: a fresh project, or a member of the suite in --dir."""
    if backend not in _SUPPORTED_BACKENDS:
        LOG.debug("scaffold_refused", reason="bad_backend", backend=backend)
        console.print(
            f"[red]--backend must be one of {_SUPPORTED_BACKENDS}, got '{escape(backend)}'[/red]"
        )
        raise typer.Exit(1)
    # ty narrows `backend: str` to `_BackendName` from the membership check
    # above (against a tuple typed `tuple[_BackendName, ...]`): no cast needed.

    if name in reserved_cli_names():
        LOG.debug("scaffold_refused", reason="reserved_name", name=name)
        console.print(
            f"[red]'{escape(name)}' is a reserved command name and cannot be a plugin name.[/red]"
        )
        raise typer.Exit(1)

    if run is not None and from_run is None:
        LOG.debug("scaffold_refused", reason="run_without_from_run")
        console.print("[red]--run requires --from-run.[/red]")
        raise typer.Exit(1)

    digest_result = None
    base_url = url
    if from_run is not None:
        run_dir = resolve_run(from_run, run)
        source = DigestSource.from_run_dir(run_dir, session=from_run, run_id=run_dir.name)
        digest_result = digest(source)
        base_url = url or f"https://{digest_result.primary_host}"

    try:
        spec = ScaffoldSpec(
            name=name,
            mode="new_project",  # write_scaffold decides the real mode
            backend=backend,
            base_url=base_url,
            digest=digest_result,
            graftpunk_version=_graftpunk_version_floor(),
        )
        result = write_scaffold(dir_, spec, force_new=new)
    except ScaffoldConflictError as exc:
        LOG.debug("scaffold_refused", reason="conflict", conflicts=len(exc.conflicts))
        console.print("[red]Refusing to overwrite existing file(s):[/red]")
        for path in exc.conflicts:
            console.print(f"  {escape(str(path))}", soft_wrap=True)
        raise typer.Exit(1) from None
    except PyprojectEditError as exc:
        LOG.debug("scaffold_refused", reason="pyproject_edit_error")
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    except OSError as exc:
        # A CLI refusal is a red line and exit 1, never a Rich traceback: an
        # unwritable --dir is the user's mistake to correct, not a crash.
        # write_scaffold has already removed whatever it wrote before failing.
        LOG.debug("scaffold_refused", reason="os_error", error=str(exc))
        target = exc.filename or str(dir_)
        reason = exc.strerror or str(exc)
        console.print(f"[red]Could not write {escape(str(target))}: {escape(reason)}[/red]")
        raise typer.Exit(1) from None
    except NotAPluginSuiteError as exc:
        # Before the ValueError arm below: it is a ValueError subclass, and the
        # two conditions are different (a directory holding someone else's
        # project, versus a name the generator cannot use).
        LOG.debug("scaffold_refused", reason="not_a_plugin_suite")
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    except ValueError as exc:
        LOG.debug("scaffold_refused", reason="invalid_name")
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None

    LOG.info("scaffold_written", mode=result.mode, dir=str(dir_))
    console.print(f"[green]{result.mode.replace('_', ' ').title()}:[/green]")
    for path in result.written:
        # soft_wrap: Console.print's default wrapping breaks a path mid-word at
        # 80 columns, so a listing meant to be copied could not be (polish round
        # 1, 2026-09-12).
        console.print(f"  {escape(str(path))}", soft_wrap=True)
    if result.gitignore_updated:
        console.print(f"[dim]Added {escape(CAPTURES_DIR)}/ to .gitignore[/dim]")
    _print_next_steps(dataclasses.replace(spec, mode=result.mode))


def _print_next_steps(spec: ScaffoldSpec) -> None:
    """Name the fixture each generated endpoint test looks for.

    A ``--from-run`` project's suite fails on its first run until those files
    exist, and nothing in the output said so (polish round 1, 2026-09-12). The
    paths come from the same rule the generated tests use, so this list is what
    ``FixtureSession`` will go looking for.
    """
    targets = fixture_paths(spec)
    if not targets:
        return
    console.print("[bold]Next:[/bold] the endpoint tests fail until these fixtures exist:")
    for relative in targets:
        console.print(f"  {escape(relative)}", soft_wrap=True)
    console.print(
        "[dim]Derive each one from a capture of the same name: gp observe fixtures --help[/dim]",
        soft_wrap=True,
    )
