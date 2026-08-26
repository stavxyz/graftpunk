"""Keepalive daemon CLI commands."""

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from graftpunk.keepalive.state import read_keepalive_pid, read_keepalive_state

console = Console()

keepalive_app = typer.Typer(
    name="keepalive",
    help="Manage the session keepalive daemon.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


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
            status_display = f"[yellow]? {escape(str(status))}[/yellow]"

        info = f"""
{status_display}  PID {pid}

[dim]Session:[/dim]     {escape(str(state.current_session or "(searching...)"))}
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
        raise typer.Exit(1) from None
    except OSError as exc:
        console.print(f"[red]✗ Failed to stop daemon: {escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
