"""BSC CLI main application.

This module provides the main Typer application for BSC with commands
for session management, keepalive, and plugin management.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

import typer
from rich.console import Console
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
    help="BSC (Browser Session Cache) - Browser session caching and keepalive management.",
    no_args_is_help=True,
)
console = Console()


@app.command("version")
def version() -> None:
    """Show BSC version information."""
    console.print(f"BSC version {bsc.__version__}")


@app.command("list")
def list_cmd(
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output in JSON format"),
    ] = False,
) -> None:
    """List all cached sessions."""
    sessions = list_sessions_with_metadata()

    if not sessions:
        console.print("[yellow]No cached sessions found.[/yellow]")
        return

    if json_output:
        import json

        console.print(json.dumps(sessions, indent=2))
        return

    # Rich table output
    table = Table(title="Cached Sessions")
    table.add_column("Name", style="cyan")
    table.add_column("Domain", style="green")
    table.add_column("Status", style="blue")
    table.add_column("Cookies", justify="right")
    table.add_column("Modified", style="dim")

    for session in sessions:
        status_style = "green" if session.get("status") == "active" else "red"
        table.add_row(
            session.get("name", ""),
            session.get("domain", "-"),
            f"[{status_style}]{session.get('status', 'unknown')}[/{status_style}]",
            str(session.get("cookie_count", 0)),
            session.get("modified_at", "")[:19] if session.get("modified_at") else "-",
        )

    console.print(table)


@app.command("show")
def show(
    name: Annotated[str, typer.Argument(help="Session name to show")],
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output in JSON format"),
    ] = False,
) -> None:
    """Show details for a specific session."""
    metadata = get_session_metadata(name)

    if not metadata:
        console.print(f"[red]Session '{name}' not found.[/red]")
        raise typer.Exit(1)

    if json_output:
        import json

        console.print(json.dumps(metadata, indent=2))
        return

    console.print(f"[bold]Session: {name}[/bold]")
    console.print(f"  Domain: {metadata.get('domain', '-')}")
    console.print(f"  Status: {metadata.get('status', 'unknown')}")
    console.print(f"  Cookies: {metadata.get('cookie_count', 0)}")
    console.print(f"  Created: {metadata.get('created_at', '-')}")
    console.print(f"  Modified: {metadata.get('modified_at', '-')}")
    console.print(f"  Expires: {metadata.get('expires_at', 'never')}")

    if metadata.get("cookie_domains"):
        console.print(f"  Cookie domains: {', '.join(metadata['cookie_domains'])}")


@app.command("clear")
def clear(
    name: Annotated[
        str | None,
        typer.Argument(help="Session name to clear (omit to clear all)"),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Skip confirmation prompt"),
    ] = False,
) -> None:
    """Clear cached session(s)."""
    if name:
        # Clear specific session
        if not force:
            confirm = typer.confirm(f"Clear session '{name}'?")
            if not confirm:
                console.print("[yellow]Cancelled.[/yellow]")
                return

        removed = clear_session_cache(name)
        if removed:
            console.print(f"[green]Cleared session: {name}[/green]")
        else:
            console.print(f"[yellow]Session '{name}' not found.[/yellow]")
    else:
        # Clear all sessions
        sessions = list_sessions()
        if not sessions:
            console.print("[yellow]No sessions to clear.[/yellow]")
            return

        if not force:
            confirm = typer.confirm(f"Clear all {len(sessions)} session(s)?")
            if not confirm:
                console.print("[yellow]Cancelled.[/yellow]")
                return

        removed = clear_session_cache()
        console.print(f"[green]Cleared {len(removed)} session(s).[/green]")


@app.command("export")
def export(
    name: Annotated[str, typer.Argument(help="Session name to export")],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Output file path"),
    ] = None,
) -> None:
    """Export session cookies to HTTPie format."""
    try:
        session = load_session(name)
    except SessionNotFoundError:
        console.print(f"[red]Session '{name}' not found.[/red]")
        raise typer.Exit(1) from None
    except SessionExpiredError as exc:
        console.print(f"[red]Session '{name}' has expired: {exc}[/red]")
        raise typer.Exit(1) from None
    except BSCError as exc:
        console.print(f"[red]Failed to load session '{name}': {exc}[/red]")
        raise typer.Exit(1) from None

    try:
        # Cast to BrowserSession since load_session returns SessionLike but we need
        # the save_httpie_session method which is specific to BrowserSession
        browser_session = cast("BrowserSession", session)
        httpie_path = browser_session.save_httpie_session(name)
        console.print(f"[green]Exported to: {httpie_path}[/green]")
        console.print(f"\nUsage: http --session={name} https://example.com/api")
    except (OSError, AttributeError) as exc:
        console.print(f"[red]Failed to export: {exc}[/red]")
        raise typer.Exit(1) from None


# Keepalive subcommand group
keepalive_app = typer.Typer(
    name="keepalive",
    help="Keepalive daemon management.",
    no_args_is_help=True,
)
app.add_typer(keepalive_app)


@keepalive_app.command("status")
def keepalive_status() -> None:
    """Show keepalive daemon status."""
    pid = read_keepalive_pid()
    state = read_keepalive_state()

    if not pid:
        console.print("[yellow]Keepalive daemon is not running.[/yellow]")
        return

    console.print("[bold]Keepalive Daemon Status[/bold]")
    console.print(f"  PID: {pid}")

    if state:
        console.print(f"  Status: {state.daemon_status.value}")
        console.print(f"  Current session: {state.current_session or '(searching)'}")
        console.print(f"  Interval: {state.interval} minutes")
        console.print(f"  Watch mode: {state.watch}")
        console.print(f"  Max switches: {state.max_switches or 'unlimited'}")
    else:
        console.print("  [dim]State file not found[/dim]")


@keepalive_app.command("stop")
def keepalive_stop() -> None:
    """Stop the keepalive daemon."""
    import os
    import signal

    pid = read_keepalive_pid()
    if not pid:
        console.print("[yellow]Keepalive daemon is not running.[/yellow]")
        return

    try:
        os.kill(pid, signal.SIGTERM)
        console.print(f"[green]Sent SIGTERM to daemon (PID {pid}).[/green]")
    except ProcessLookupError:
        console.print("[yellow]Daemon process not found (already stopped?).[/yellow]")
    except PermissionError:
        console.print(f"[red]Permission denied to stop daemon (PID {pid}).[/red]")


# Plugins subcommand
@app.command("plugins")
def plugins() -> None:
    """List installed plugins."""
    storage = discover_storage_backends()
    handlers = discover_keepalive_handlers()
    site_plugins = discover_site_plugins()

    console.print("[bold]Installed Plugins[/bold]\n")

    console.print("[cyan]Storage Backends:[/cyan]")
    if storage:
        for name in sorted(storage.keys()):
            console.print(f"  - {name}")
    else:
        console.print("  [dim](none)[/dim]")

    console.print("\n[cyan]Keepalive Handlers:[/cyan]")
    if handlers:
        for name in sorted(handlers.keys()):
            console.print(f"  - {name}")
    else:
        console.print("  [dim](none)[/dim]")

    console.print("\n[cyan]Site Plugins:[/cyan]")
    if site_plugins:
        for name in sorted(site_plugins.keys()):
            console.print(f"  - {name}")
    else:
        console.print("  [dim](none)[/dim]")


@app.command("config")
def config() -> None:
    """Show current BSC configuration."""
    settings = get_settings()

    console.print("[bold]BSC Configuration[/bold]\n")
    console.print(f"  Config directory: {settings.config_dir}")
    console.print(f"  Sessions directory: {settings.sessions_dir}")
    console.print(f"  Storage backend: {settings.storage_backend}")
    console.print(f"  Session TTL: {settings.session_ttl_hours} hours")
    console.print(f"  Log level: {settings.log_level}")


if __name__ == "__main__":
    app()
