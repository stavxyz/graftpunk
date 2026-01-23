"""BSC CLI - browser session cache command-line interface.

Manage encrypted browser sessions from the terminal.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

import bsc
from bsc import (
    clear_session_cache,
    get_session_metadata,
    list_sessions,
    list_sessions_with_metadata,
    load_session,
)
from bsc.config import get_settings
from bsc.exceptions import BSCError, SessionExpiredError, SessionNotFoundError
from bsc.keepalive.state import read_keepalive_pid, read_keepalive_state
from bsc.plugins import (
    discover_keepalive_handlers,
    discover_site_plugins,
    discover_storage_backends,
)

if TYPE_CHECKING:
    from bsc.session import BrowserSession

app = typer.Typer(
    name="bsc",
    help="""
    🔐 BSC - Browser Session Cache

    Securely cache and restore authenticated browser sessions.
    Sessions are encrypted with Fernet (AES-128) and stored locally or in the cloud.

    \b
    Quick start:
      bsc list              Show all cached sessions
      bsc show <name>       View session details
      bsc clear <name>      Remove a session
      bsc config            Show current configuration

    \b
    Documentation: https://github.com/stavxyz/bsc
    """,
    no_args_is_help=True,
    rich_markup_mode="rich",
)
console = Console()


@app.command("version")
def version() -> None:
    """Show BSC version and installation info."""
    settings = get_settings()
    console.print(
        Panel(
            f"[bold cyan]BSC[/bold cyan] v{bsc.__version__}\n\n"
            f"[dim]Config:[/dim]  {settings.config_dir}\n"
            f"[dim]Storage:[/dim] {settings.storage_backend}",
            title="Browser Session Cache",
            border_style="cyan",
        )
    )


@app.command("list")
def list_cmd(
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            "-j",
            help="Output as JSON for scripting",
        ),
    ] = False,
) -> None:
    """List all cached sessions with status and metadata."""
    sessions = list_sessions_with_metadata()

    if not sessions:
        console.print(
            Panel(
                "[dim]No sessions cached yet.[/dim]\n\n"
                "Use your application to create a session, then cache it with:\n"
                "[cyan]from bsc import cache_session[/cyan]",
                title="📭 No Sessions",
                border_style="yellow",
            )
        )
        return

    if json_output:
        import json

        console.print(json.dumps(sessions, indent=2, default=str))
        return

    table = Table(
        title="🔐 Cached Sessions",
        title_style="bold",
        header_style="bold cyan",
        border_style="dim",
    )
    table.add_column("Session", style="cyan", no_wrap=True)
    table.add_column("Domain", style="white")
    table.add_column("Status", justify="center")
    table.add_column("Cookies", justify="right", style="dim")
    table.add_column("Last Modified", style="dim")

    for session in sessions:
        status = session.get("status", "unknown")
        if status == "active":
            status_display = "[green]● active[/green]"
        elif status == "logged_out":
            status_display = "[red]○ logged out[/red]"
        else:
            status_display = f"[yellow]? {status}[/yellow]"

        modified = session.get("modified_at", "")
        if modified:
            modified = modified[:16].replace("T", " ")

        table.add_row(
            session.get("name", ""),
            session.get("domain") or "[dim]—[/dim]",
            status_display,
            str(session.get("cookie_count", 0)),
            modified or "[dim]—[/dim]",
        )

    console.print(table)
    console.print(f"\n[dim]{len(sessions)} session(s) cached[/dim]")


@app.command("show")
def show(
    name: Annotated[
        str,
        typer.Argument(
            help="Name of the session to display",
            metavar="SESSION",
        ),
    ],
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            "-j",
            help="Output as JSON for scripting",
        ),
    ] = False,
) -> None:
    """Show detailed information about a cached session."""
    metadata = get_session_metadata(name)

    if not metadata:
        console.print(f"[red]✗ Session '{name}' not found[/red]")
        raise typer.Exit(1)

    if json_output:
        import json

        console.print(json.dumps(metadata, indent=2, default=str))
        return

    status = metadata.get("status", "unknown")
    if status == "active":
        status_icon = "[green]●[/green]"
        status_text = "[green]active[/green]"
    elif status == "logged_out":
        status_icon = "[red]○[/red]"
        status_text = "[red]logged out[/red]"
    else:
        status_icon = "[yellow]?[/yellow]"
        status_text = f"[yellow]{status}[/yellow]"

    created = metadata.get("created_at", "—")
    modified = metadata.get("modified_at", "—")
    expires = metadata.get("expires_at", "never")

    if created and created != "—":
        created = created[:19].replace("T", " ")
    if modified and modified != "—":
        modified = modified[:19].replace("T", " ")
    if expires and expires != "never":
        expires = expires[:19].replace("T", " ")

    info = f"""
{status_icon} [bold]{name}[/bold]  {status_text}

[dim]Domain:[/dim]     {metadata.get("domain") or "—"}
[dim]Cookies:[/dim]    {metadata.get("cookie_count", 0)}
[dim]Created:[/dim]    {created}
[dim]Modified:[/dim]   {modified}
[dim]Expires:[/dim]    {expires}"""

    if metadata.get("cookie_domains"):
        domains = ", ".join(metadata["cookie_domains"][:5])
        if len(metadata["cookie_domains"]) > 5:
            domains += f" (+{len(metadata['cookie_domains']) - 5} more)"
        info += f"\n[dim]Domains:[/dim]    {domains}"

    console.print(Panel(info.strip(), border_style="cyan"))


@app.command("clear")
def clear(
    name: Annotated[
        str | None,
        typer.Argument(
            help="Session to remove (omit to clear all)",
            metavar="SESSION",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help="Skip confirmation prompt",
        ),
    ] = False,
) -> None:
    """Remove cached session(s).

    Clear a specific session by name, or clear all sessions if no name is given.
    """
    if name:
        if not force:
            confirm = typer.confirm(f"Remove session '{name}'?")
            if not confirm:
                console.print("[dim]Cancelled[/dim]")
                return

        removed = clear_session_cache(name)
        if removed:
            console.print(f"[green]✓ Removed session: {name}[/green]")
        else:
            console.print(f"[yellow]Session '{name}' not found[/yellow]")
    else:
        sessions = list_sessions()
        if not sessions:
            console.print("[dim]No sessions to clear[/dim]")
            return

        if not force:
            confirm = typer.confirm(f"Remove all {len(sessions)} session(s)?")
            if not confirm:
                console.print("[dim]Cancelled[/dim]")
                return

        removed = clear_session_cache()
        console.print(f"[green]✓ Removed {len(removed)} session(s)[/green]")


@app.command("export")
def export(
    name: Annotated[
        str,
        typer.Argument(
            help="Session to export",
            metavar="SESSION",
        ),
    ],
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Output file path (default: HTTPie sessions dir)",
        ),
    ] = None,
) -> None:
    """Export session cookies to HTTPie format.

    Exports cookies so you can use them with HTTPie:

        http --session=SESSION https://example.com/api
    """
    try:
        session = load_session(name)
    except SessionNotFoundError:
        console.print(f"[red]✗ Session '{name}' not found[/red]")
        raise typer.Exit(1) from None
    except SessionExpiredError as exc:
        console.print(f"[red]✗ Session expired: {exc}[/red]")
        raise typer.Exit(1) from None
    except BSCError as exc:
        console.print(f"[red]✗ Failed to load: {exc}[/red]")
        raise typer.Exit(1) from None

    try:
        browser_session = cast("BrowserSession", session)
        httpie_path = browser_session.save_httpie_session(name)

        console.print(f"[green]✓ Exported to:[/green] {httpie_path}\n")
        console.print("[dim]Usage:[/dim]")
        console.print(f"  http --session={name} https://example.com/api")
    except (OSError, AttributeError) as exc:
        console.print(f"[red]✗ Export failed: {exc}[/red]")
        raise typer.Exit(1) from None


# Keepalive subcommand group
keepalive_app = typer.Typer(
    name="keepalive",
    help="Manage the session keepalive daemon.",
    no_args_is_help=True,
)
app.add_typer(keepalive_app)


@keepalive_app.command("status")
def keepalive_status() -> None:
    """Show keepalive daemon status."""
    pid = read_keepalive_pid()
    state = read_keepalive_state()

    if not pid:
        console.print(
            Panel(
                "[dim]Daemon is not running[/dim]",
                title="⏸ Keepalive Status",
                border_style="yellow",
            )
        )
        return

    if state:
        status = state.daemon_status.value
        if status == "running":
            status_display = "[green]● running[/green]"
        elif status == "stopped":
            status_display = "[red]○ stopped[/red]"
        else:
            status_display = f"[yellow]? {status}[/yellow]"

        info = f"""
{status_display}  PID {pid}

[dim]Session:[/dim]     {state.current_session or "(searching...)"}
[dim]Interval:[/dim]    {state.interval} minutes
[dim]Watch mode:[/dim]  {"yes" if state.watch else "no"}
[dim]Max switches:[/dim] {state.max_switches or "unlimited"}"""
    else:
        info = f"PID {pid}\n\n[dim]State file not found[/dim]"

    console.print(Panel(info.strip(), title="🔄 Keepalive Status", border_style="cyan"))


@keepalive_app.command("stop")
def keepalive_stop() -> None:
    """Stop the keepalive daemon."""
    import os
    import signal

    pid = read_keepalive_pid()
    if not pid:
        console.print("[yellow]Daemon is not running[/yellow]")
        return

    try:
        os.kill(pid, signal.SIGTERM)
        console.print(f"[green]✓ Stopped daemon (PID {pid})[/green]")
    except ProcessLookupError:
        console.print("[yellow]Daemon already stopped[/yellow]")
    except PermissionError:
        console.print(f"[red]✗ Permission denied (PID {pid})[/red]")


@app.command("plugins")
def plugins() -> None:
    """List discovered plugins (storage, handlers, sites)."""
    storage = discover_storage_backends()
    handlers = discover_keepalive_handlers()
    site_plugins = discover_site_plugins()

    total = len(storage) + len(handlers) + len(site_plugins)

    sections = []

    if storage:
        items = "\n".join(f"  [green]✓[/green] {name}" for name in sorted(storage.keys()))
        sections.append(f"[bold]Storage Backends[/bold]\n{items}")
    else:
        sections.append("[bold]Storage Backends[/bold]\n  [dim](none installed)[/dim]")

    if handlers:
        items = "\n".join(f"  [green]✓[/green] {name}" for name in sorted(handlers.keys()))
        sections.append(f"[bold]Keepalive Handlers[/bold]\n{items}")
    else:
        sections.append("[bold]Keepalive Handlers[/bold]\n  [dim](none installed)[/dim]")

    if site_plugins:
        items = "\n".join(f"  [green]✓[/green] {name}" for name in sorted(site_plugins.keys()))
        sections.append(f"[bold]Site Plugins[/bold]\n{items}")
    else:
        sections.append("[bold]Site Plugins[/bold]\n  [dim](none installed)[/dim]")

    content = "\n\n".join(sections)
    console.print(
        Panel(
            content,
            title=f"🔌 Plugins ({total} installed)",
            border_style="cyan",
        )
    )


@app.command("config")
def config() -> None:
    """Show current BSC configuration."""
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
[dim]Log level:[/dim]          {settings.log_level}"""

    console.print(Panel(info.strip(), title="⚙ Configuration", border_style="cyan"))


if __name__ == "__main__":
    app()
