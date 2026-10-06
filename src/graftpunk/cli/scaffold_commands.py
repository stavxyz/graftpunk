"""``gp plugin new``: argument handling only, over ``graftpunk.devtools``.

``info``, ``add-command``, ``upgrade``, and ``check`` live in
``scaffold_project_commands.py``; both modules attach their commands to the
same ``plugin_app``, from ``scaffold_shared.py``. :func:`register` imports
``scaffold_project_commands`` itself, so ``plugin_app`` carries all five
commands wherever ``register`` is called, not only when ``main.py`` happens
to have imported that module first. ``register`` and the reserved-names
snapshot stay here rather than in the shared module because tests patch
``scaffold_commands._reserved_names`` directly, and that patch only reaches
the global a function reads when the function is defined in this module.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.markup import escape

from graftpunk.cli.observe_commands import resolve_run
from graftpunk.cli.plugin_commands import derive_reserved_cli_names
from graftpunk.cli.scaffold_shared import (
    LOG,
    command_selections,
    console,
    plugin_app,
    print_other_host,
)
from graftpunk.devtools.captures_rule import CAPTURES_DIR
from graftpunk.devtools.errors import ScaffoldWriteError
from graftpunk.devtools.scaffold.project import (
    NotAPluginSuiteError,
    ScaffoldConflictError,
    write_scaffold,
)
from graftpunk.devtools.scaffold.pyproject_edit import PyprojectEditError
from graftpunk.devtools.scaffold.render import (
    ScaffoldSpec,
    base_host,
    fixture_paths,
    graftpunk_version_floor,
    other_host_commands,
    validate_plugin_name,
)
from graftpunk.devtools.scaffold.selection import CommandSelectionError
from graftpunk.devtools.scaffold.write import InvalidChangeError
from graftpunk.har.digest import DigestSource, digest
from graftpunk.har.parser import HARParseError

_BackendName = Literal["nodriver", "selenium"]
_SUPPORTED_BACKENDS: tuple[_BackendName, ...] = ("nodriver", "selenium")

# The reserved top-level CLI names, snapshotted by register() at attach time
# (before any site plugin's own sub-app is mounted): see register()'s
# docstring for why this must be a snapshot rather than a live query.
_reserved_names: frozenset[str] = frozenset()


def register(app: typer.Typer) -> None:
    """Attach ``plugin_app`` to *app* and snapshot its reserved top-level names.

    Imports ``graftpunk.cli.scaffold_project_commands`` first, a local import
    (that module does not import this one, so there is no cycle), so
    ``info``/``add-command``/``upgrade``/``check`` attach to ``plugin_app``
    here too, rather than depending on some other caller having imported that
    module first.

    Called from ``main.py`` right before ``register_plugin_commands(app)``
    runs, so the snapshot holds only the CLI's own built-in names (session,
    http, config, keepalive, observe, plugins, plugin, ...) and never an
    installed site plugin's ``site_name``: plugin discovery has not mounted
    anything onto *app* yet at this point. A snapshot, rather than deriving
    fresh from a live app reference, also means this module never has to
    import ``graftpunk.cli.main`` (the reverse import would be a real cycle,
    not just a lazy one deferred past load time).
    """
    global _reserved_names
    import graftpunk.cli.scaffold_project_commands  # noqa: F401 - attaches info/add-command/upgrade/check to plugin_app

    app.add_typer(plugin_app)
    _reserved_names = derive_reserved_cli_names(app)


def reserved_cli_names() -> frozenset[str]:
    """The reserved set snapshotted by the last ``register()`` call.

    ``gp plugin new`` reads this to refuse a plugin name before generating
    anything.
    """
    return _reserved_names


def _name_refusal(name: str) -> tuple[str, str] | None:
    """The refusal ``gp plugin new`` gives for *name*, as (log reason, message), or None.

    One owner for the two name checks: the reserved top-level names snapshotted
    at attach time, then the name rule ``ScaffoldSpec`` enforces. ``--check-name``
    and the real run both call this, so their refusals cannot differ.
    """
    if name in reserved_cli_names():
        return "reserved_name", f"'{name}' is a reserved command name and cannot be a plugin name."
    try:
        validate_plugin_name(name)
    except ValueError as exc:
        return "invalid_name", str(exc)
    return None


@plugin_app.command("new")
def plugin_new(
    name: Annotated[str, typer.Argument(help="Plugin name: site_name, and the package suffix")],
    url: Annotated[
        str, typer.Option("--url", help="Base URL (overrides the host taken from --from-run)")
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
    check_name: Annotated[
        bool,
        typer.Option("--check-name", help="Check NAME the way this command would, write nothing"),
    ] = False,
    command: Annotated[
        list[str],
        typer.Option(
            "--command",
            help='"NAME=METHOD template": stub only these endpoints, under these names '
            "(repeatable; needs --from-run)",
        ),
    ] = [],  # noqa: B006 - Typer reads this default at decoration time, never mutated per-call
) -> None:
    """Scaffold a new plugin: a fresh project, or a member of the suite in --dir."""
    refusal = _name_refusal(name)
    if check_name:
        if refusal is not None:
            console.print(f"[red]{escape(refusal[1])}[/red]", soft_wrap=True)
            raise typer.Exit(1)
        console.print(f"'{escape(name)}' is an acceptable plugin name.")
        return
    if backend not in _SUPPORTED_BACKENDS:
        LOG.debug("scaffold_refused", reason="bad_backend", backend=backend)
        console.print(
            f"[red]--backend must be one of {_SUPPORTED_BACKENDS}, got '{escape(backend)}'[/red]",
            soft_wrap=True,
        )
        raise typer.Exit(1)
    # ty narrows `backend: str` to `_BackendName` from the membership check
    # above (against a tuple typed `tuple[_BackendName, ...]`): no cast needed.

    if refusal is not None:
        LOG.debug("scaffold_refused", reason=refusal[0], name=name)
        console.print(f"[red]{escape(refusal[1])}[/red]", soft_wrap=True)
        raise typer.Exit(1)

    if run is not None and from_run is None:
        LOG.debug("scaffold_refused", reason="run_without_from_run")
        console.print("[red]--run requires --from-run.[/red]", soft_wrap=True)
        raise typer.Exit(1)

    selections = command_selections(command)
    if selections and from_run is None:
        LOG.debug("scaffold_refused", reason="command_without_from_run")
        console.print("[red]--command requires --from-run.[/red]", soft_wrap=True)
        raise typer.Exit(1)

    if url and base_host(url) is None:
        LOG.debug("scaffold_refused", reason="url_without_host")
        console.print(
            "[red]--url must be an http:// or https:// URL with a host, "
            f"got '{escape(url)}'.[/red]",
            soft_wrap=True,
        )
        raise typer.Exit(1)

    digest_result = None
    base_url = url
    if from_run is not None:
        run_dir = resolve_run(from_run, run)
        source = DigestSource.from_run_dir(run_dir, session=from_run, run_id=run_dir.name)
        try:
            digest_result = digest(source)
        except (FileNotFoundError, HARParseError) as exc:
            LOG.debug("scaffold_refused", reason="digest_load_error", har_path=str(source.har_path))
            console.print(
                f"[red]Could not read the recording: {escape(str(exc))}[/red]",
                soft_wrap=True,
            )
            raise typer.Exit(1) from None
        base_url = url or f"https://{digest_result.primary_host}"

    try:
        spec = ScaffoldSpec(
            name=name,
            mode="new_project",  # write_scaffold decides the real mode
            backend=backend,
            base_url=base_url,
            digest=digest_result,
            graftpunk_version=graftpunk_version_floor(),
            commands=selections,
        )
        result = write_scaffold(dir_, spec, force_new=new)
    except ScaffoldConflictError as exc:
        LOG.debug("scaffold_refused", reason="conflict", conflicts=len(exc.conflicts))
        for header, paths in exc.kinds:
            console.print(f"[red]{escape(header)}:[/red]", soft_wrap=True)
            for path in paths:
                console.print(f"  {escape(str(path))}", soft_wrap=True)
        raise typer.Exit(1) from None
    except PyprojectEditError as exc:
        LOG.debug("scaffold_refused", reason="pyproject_edit_error")
        console.print(f"[red]{escape(str(exc))}[/red]", soft_wrap=True)
        raise typer.Exit(1) from None
    except ScaffoldWriteError as exc:
        # The writer restored what it could before raising, and its message is
        # the whole refusal on one line: the path, the OS error, and any path
        # it could not put back.
        LOG.debug("scaffold_refused", reason="os_error", error=str(exc.error))
        console.print(f"[red]{escape(str(exc))}[/red]", soft_wrap=True)
        raise typer.Exit(1) from None
    except OSError as exc:
        # A read before anything was written failed (the suite's pyproject.toml
        # or .gitignore): a red line and exit 1, never a Rich traceback. A failed
        # write is the ScaffoldWriteError arm above.
        LOG.debug("scaffold_refused", reason="os_error", error=str(exc))
        target = exc.filename or str(dir_)
        reason = exc.strerror or str(exc)
        console.print(
            f"[red]Could not write {escape(str(target))}: {escape(reason)}[/red]", soft_wrap=True
        )
        raise typer.Exit(1) from None
    except CommandSelectionError as exc:
        LOG.debug("scaffold_refused", reason="bad_command")
        console.print(f"[red]{escape(str(exc))}[/red]", soft_wrap=True)
        raise typer.Exit(1) from None
    except NotAPluginSuiteError as exc:
        # Before the ValueError arm below: it is a ValueError subclass, and the
        # two conditions are different (a directory holding someone else's
        # project, versus a name the generator cannot use).
        LOG.debug("scaffold_refused", reason="not_a_plugin_suite")
        console.print(f"[red]{escape(str(exc))}[/red]", soft_wrap=True)
        raise typer.Exit(1) from None
    except InvalidChangeError as exc:
        # Also before the ValueError arm: a rendered file that fails its own
        # grammar check is the generator's fault, not an invalid name.
        LOG.debug("scaffold_refused", reason="invalid_change")
        console.print(f"[red]{escape(str(exc))}[/red]", soft_wrap=True)
        raise typer.Exit(1) from None
    except ValueError as exc:
        LOG.debug("scaffold_refused", reason="invalid_name")
        console.print(f"[red]{escape(str(exc))}[/red]", soft_wrap=True)
        raise typer.Exit(1) from None

    LOG.info("scaffold_written", mode=result.mode, dir=str(dir_))
    console.print(f"[green]{result.mode.replace('_', ' ').title()}:[/green]")
    for path in result.written:
        # soft_wrap: Console.print's default wrapping breaks a path mid-word at
        # 80 columns, so a listing meant to be copied could not be.
        console.print(f"  {escape(str(path))}", soft_wrap=True)
    if result.gitignore_updated:
        console.print(f"[dim]Added {escape(CAPTURES_DIR)}/ to .gitignore[/dim]")
    for other in other_host_commands(spec):
        print_other_host(other)
    _print_next_steps(dataclasses.replace(spec, mode=result.mode))


def _print_next_steps(spec: ScaffoldSpec) -> None:
    """Name the fixture each generated endpoint test looks for.

    A ``--from-run`` project's suite fails on its first run until those files
    exist, and nothing in the output said so. The
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
