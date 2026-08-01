"""gp config — inspect and manage graftpunk configuration.

Bare `gp config` renders the settings panel (behavior migrated verbatim from
the old root-level command); the verbs manage the workstation env file
(spec: docs/rfcs/2026-07-28-workstation-env.md).
"""

import os
import subprocess

import typer
from rich.console import Console
from rich.panel import Panel

from graftpunk import paths, workstation_env
from graftpunk.cli.login_commands import SECRET_KEYWORDS
from graftpunk.config import get_settings

console = Console()
err_console = Console(stderr=True)

config_app = typer.Typer(
    name="config",
    help="Show configuration; manage the workstation env file.",
    invoke_without_command=True,
    no_args_is_help=False,
)


def render_settings_panel(target_console: Console) -> None:
    """The settings display formerly at `gp config` (migrated verbatim)."""
    settings = get_settings()

    storage_display = settings.storage_backend
    if settings.storage_backend == "supabase":
        storage_display = f"{settings.storage_backend} [dim](cloud)[/dim]"
    elif settings.storage_backend == "local":
        storage_display = f"{settings.storage_backend} [dim](filesystem)[/dim]"

    info = f"""
[dim]Config directory:[/dim]   {settings.config_dir}
[dim]Sessions directory:[/dim] {settings.sessions_dir}
[dim]Storage backend:[/dim]    {storage_display}
[dim]Session TTL:[/dim]        {settings.session_ttl_hours}h ({settings.session_ttl_hours // 24}d)
[dim]Log level:[/dim]          {settings.log_level}
[dim]Log format:[/dim]         {settings.log_format}"""

    target_console.print(Panel(info.strip(), title="⚙ Configuration", border_style="cyan"))


@config_app.callback(invoke_without_command=True)
def config_root(ctx: typer.Context) -> None:
    """Show current graftpunk configuration."""
    if ctx.invoked_subcommand is None:
        render_settings_panel(console)


@config_app.command("show")
def show() -> None:
    """Show current graftpunk configuration."""
    render_settings_panel(console)


@config_app.command("path")
def path_cmd() -> None:
    """Print the workstation env file's path."""
    typer.echo(str(paths.workstation_env_path()))


@config_app.command("list")
def list_cmd() -> None:
    """List workstation env entries (raw values; commands unevaluated).

    Command entries carry a trailing `[command]` marker — per the spec's
    design note, list surfaces each entry's kind so misclassifications
    (e.g. an accidentally-static secret) are visible at a glance.
    """
    for entry in workstation_env.load().entries():
        suffix = "  [command]" if entry.kind == workstation_env.COMMAND else ""
        typer.echo(f"{entry.name}={entry.raw_value}{suffix}")


@config_app.command("get")
def get_cmd(
    name: str,
    resolve: bool = typer.Option(
        False,
        "--resolve",
        help="Evaluate a command value and print the result (may print a secret). "
        "Inspects the FILE's stored value directly — the one intentional "
        "exception to lookup()-only resolution.",
    ),
) -> None:
    """Print the raw stored value for NAME (or resolve it with --resolve)."""
    entry = workstation_env.load().get(name)
    if entry is None:
        err_console.print(f"[red]{name} is not set in {paths.workstation_env_path()}[/red]")
        raise typer.Exit(1)
    if not resolve:
        typer.echo(entry.raw_value)
        return
    # File-tier resolution via the module's primitive — command-execution
    # mechanics stay with their single owner (workstation_env).
    value = workstation_env.load().resolve_file_value(name)
    if value is None:
        err_console.print(f"[red]command for {name} failed (see log output)[/red]")
        raise typer.Exit(1)
    typer.echo(value)


@config_app.command("set")
def set_cmd(name: str, value: str) -> None:
    """Add or replace NAME=VALUE (stored verbatim; quote $(…) in your shell)."""
    try:
        workstation_env.set_entry(name, value)
    except (ValueError, OSError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    is_secret_name = any(kw in name.lower() for kw in SECRET_KEYWORDS)
    if is_secret_name and "$(" not in value:
        err_console.print(
            f"[yellow]warning:[/yellow] {name} looks secret but the value is a "
            "literal — if you meant a command, single-quote it: "
            f"gp config set {name} '$(op read ...)'. A plaintext secret is now "
            "on disk (0600)."
        )


@config_app.command("unset")
def unset_cmd(name: str) -> None:
    """Remove NAME (all occurrences). Idempotent."""
    try:
        workstation_env.unset_entry(name)
    except OSError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


@config_app.command("edit")
def edit_cmd() -> None:
    """Open the workstation env file in $VISUAL, else $EDITOR, else vi."""
    workstation_env.ensure_file()
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    env_path = paths.workstation_env_path()
    exit_code = subprocess.call([editor, str(env_path)])  # noqa: S603
    # Some editors write via a temp-file-then-rename (replace-on-write),
    # which leaves the new inode with the process umask's permissions
    # rather than preserving the original 0600 -- re-assert it here so
    # every write path (not just workstation_env's own writers) honors the
    # spec's "every CLI write is 0600" invariant.
    os.chmod(env_path, 0o600)
    raise typer.Exit(exit_code)
