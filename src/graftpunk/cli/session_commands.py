"""Session management commands — gp session list/show/clear/export."""

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from graftpunk import (
    clear_session_cache,
    get_session_metadata,
    list_sessions_with_metadata,
    load_session,
)
from graftpunk.cli.errors import exit_ambiguous_session
from graftpunk.cli.plugin_commands import resolve_session_name
from graftpunk.exceptions import (
    AmbiguousSessionError,
    GraftpunkError,
    SessionExpiredError,
    SessionNotFoundError,
    StorageError,
)
from graftpunk.plugins.formatters import write_raw
from graftpunk.session_context import clear_active_session, set_active_session
from graftpunk.session_identity import split_session_name

if TYPE_CHECKING:
    from graftpunk.session import BrowserSession

session_app = typer.Typer(
    name="session",
    help="Manage encrypted browser sessions.",
)
console = Console()


@session_app.callback(invoke_without_command=True)
def session_callback(
    ctx: typer.Context,
    storage_backend: Annotated[
        str | None,
        typer.Option(
            "--storage-backend",
            help="Storage backend to use (local, s3, supabase)",
        ),
    ] = None,
) -> None:
    """Manage encrypted browser sessions."""
    ctx.ensure_object(dict)
    ctx.obj["storage_backend"] = storage_backend
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())


def _cell(value: object) -> Text | str:
    """A table cell for session metadata: data is never markup-parsed; empty shows a dash."""
    return Text(str(value)) if value else "[dim]—[/dim]"


def _label_of(name: str) -> str:
    """The ``@label`` half of a session name, or "" for a bare base name."""
    return split_session_name(name)[1] or ""


@session_app.command("list")
def session_list(
    ctx: typer.Context,
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output as JSON for scripting"),
    ] = False,
) -> None:
    """List all cached sessions with status and metadata."""
    backend_override = ctx.obj.get("storage_backend") if ctx.obj else None
    try:
        sessions = list_sessions_with_metadata(backend_override=backend_override)
    except (ValueError, StorageError) as exc:
        console.print(f"[red]✗ Storage backend error: {escape(str(exc))}[/red]")
        raise typer.Exit(1) from None

    if not sessions:
        console.print(
            Panel(
                "[dim]No sessions cached yet.[/dim]\n\n"
                "Use a plugin to log in and create a session:\n"
                "[cyan]gp <plugin> login[/cyan]",
                title="No Sessions",
                border_style="yellow",
            )
        )
        return

    if json_output:
        import json

        write_raw(json.dumps(sessions, indent=2, default=str) + "\n", console)
        return

    table = Table(
        title="Cached Sessions",
        title_style="bold",
        header_style="bold cyan",
        border_style="dim",
    )
    table.add_column("Session", style="cyan", no_wrap=True)
    table.add_column("Domain", style="white")
    table.add_column("Account", style="white")
    table.add_column("Status", justify="center")
    table.add_column("Cookies", justify="right", style="dim")
    table.add_column("Last Modified", style="dim")
    table.add_column("Backend", style="dim", no_wrap=True)
    table.add_column("Location", style="dim")

    for session in sessions:
        status = session.get("status", "unknown")
        if status == "active":
            status_display = "[green]● active[/green]"
        elif status == "logged_out":
            status_display = "[red]○ logged out[/red]"
        else:
            status_display = f"[yellow]? {escape(str(status))}[/yellow]"

        modified = session.get("modified_at", "")
        if modified:
            modified = modified[:16].replace("T", " ")

        table.add_row(
            Text(str(session.get("name", ""))),
            _cell(session.get("domain")),
            _cell(session.get("account_identifier") or _label_of(session.get("name", ""))),
            status_display,
            Text(str(session.get("cookie_count", 0))),
            _cell(modified),
            _cell(session.get("storage_backend")),
            _cell(session.get("storage_location")),
        )

    console.print(table)
    console.print(f"\n[dim]{len(sessions)} session(s) cached[/dim]")


@session_app.command("show")
def show(
    ctx: typer.Context,
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
    backend_override = ctx.obj.get("storage_backend") if ctx.obj else None
    try:
        name = resolve_session_name(name)
    except AmbiguousSessionError as exc:
        exit_ambiguous_session(exc)
    try:
        metadata = get_session_metadata(name, backend_override=backend_override)
    except (ValueError, StorageError) as exc:
        console.print(f"[red]✗ Storage backend error: {escape(str(exc))}[/red]")
        raise typer.Exit(1) from None

    if not metadata:
        console.print(f"[red]✗ Session '{escape(str(name))}' not found[/red]")
        raise typer.Exit(1)

    if json_output:
        import json

        write_raw(json.dumps(metadata, indent=2, default=str) + "\n", console)
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
        status_text = f"[yellow]{escape(str(status))}[/yellow]"

    created = metadata.get("created_at", "—")
    modified = metadata.get("modified_at", "—")
    expires = metadata.get("expires_at", "never")

    if created and created != "—":
        created = created[:19].replace("T", " ")
    if modified and modified != "—":
        modified = modified[:19].replace("T", " ")
    if expires and expires != "never":
        expires = expires[:19].replace("T", " ")

    backend = metadata.get("storage_backend") or "—"
    location = metadata.get("storage_location") or "—"

    account = metadata.get("account_identifier") or "—"
    label = _label_of(str(name)) or "—"

    info = f"""
{status_icon} [bold]{escape(str(name))}[/bold]  {status_text}

[dim]Domain:[/dim]     {escape(str(metadata.get("domain") or "—"))}
[dim]Account:[/dim]    {escape(str(account))}
[dim]Label:[/dim]      {escape(str(label))}
[dim]Cookies:[/dim]    {metadata.get("cookie_count", 0)}
[dim]Created:[/dim]    {escape(str(created))}
[dim]Modified:[/dim]   {escape(str(modified))}
[dim]Expires:[/dim]    {escape(str(expires))}
[dim]Backend:[/dim]    {escape(str(backend))}
[dim]Location:[/dim]   {escape(str(location))}"""

    if metadata.get("cookie_domains"):
        domains = ", ".join(metadata["cookie_domains"][:5])
        if len(metadata["cookie_domains"]) > 5:
            domains += f" (+{len(metadata['cookie_domains']) - 5} more)"
        info += f"\n[dim]Domains:[/dim]    {escape(str(domains))}"

    console.print(Panel(info.strip(), border_style="cyan"))


@session_app.command("export")
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
        name = resolve_session_name(name)
    except AmbiguousSessionError as exc:
        exit_ambiguous_session(exc)
    try:
        session = load_session(name)
    except SessionNotFoundError:
        console.print(f"[red]✗ Session '{escape(str(name))}' not found[/red]")
        raise typer.Exit(1) from None
    except SessionExpiredError as exc:
        console.print(f"[red]✗ Session expired: {escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    except GraftpunkError as exc:
        console.print(f"[red]✗ Failed to load: {escape(str(exc))}[/red]")
        raise typer.Exit(1) from None

    try:
        browser_session = cast("BrowserSession", session)
        httpie_path = browser_session.save_httpie_session(name)

        console.print(f"[green]✓ Exported to:[/green] {escape(str(httpie_path))}\n")
        console.print("[dim]Usage:[/dim]")
        console.print(f"  http --session={escape(str(name))} https://example.com/api")
    except (OSError, AttributeError) as exc:
        console.print(f"[red]✗ Export failed: {escape(str(exc))}[/red]")
        raise typer.Exit(1) from None


@session_app.command("clear")
def session_clear(
    ctx: typer.Context,
    target: Annotated[
        str | None,
        typer.Argument(
            help="Session name or domain to clear",
            metavar="TARGET",
        ),
    ] = None,
    all_sessions: Annotated[
        bool,
        typer.Option("--all", "-a", help="Clear all sessions"),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Skip confirmation prompt"),
    ] = False,
) -> None:
    """Remove cached session(s).

    Clear by session name, domain, or all:

        gp session clear hackernews       Clear session named "hackernews"
        gp session clear example.com      Clear all sessions for domain
        gp session clear --all            Clear everything (prompts first)
        gp session clear -af              Clear everything without prompt
    """
    if not target and not all_sessions:
        console.print("[red]Specify a session name, domain, or use --all[/red]")
        raise typer.Exit(1)

    backend_override = ctx.obj.get("storage_backend") if ctx.obj else None
    try:
        all_metadata = list_sessions_with_metadata(
            backend_override=backend_override,
        )
    except (ValueError, StorageError) as exc:
        console.print(f"[red]✗ Storage backend error: {escape(str(exc))}[/red]")
        raise typer.Exit(1) from None

    if all_sessions:
        if not all_metadata:
            console.print("[dim]No sessions to clear[/dim]")
            return

        if not force:
            console.print(f"[yellow]This will remove {len(all_metadata)} session(s):[/yellow]")
            for s in all_metadata:
                console.print(
                    f"  - {escape(str(s['name']))} ({escape(str(s.get('domain') or 'no domain'))})"
                )
            if not typer.confirm("Remove all sessions?"):
                console.print("[dim]Cancelled[/dim]")
                return

        removed = []
        for s in all_metadata:
            result = clear_session_cache(
                s["name"],
                backend_override=backend_override,
            )
            if result:
                removed.append(s)

        _print_removed(removed)
        return

    assert target is not None
    is_domain = "." in target

    if is_domain:
        matches = [s for s in all_metadata if s.get("domain") == target]
        if not matches:
            console.print(f"[red]No sessions found for domain '{escape(str(target))}'[/red]")
            raise typer.Exit(1)

        if not force:
            shown = escape(str(target))
            msg = f"[yellow]Found {len(matches)} session(s) for domain '{shown}':[/yellow]"
            console.print(msg)
            for s in matches:
                console.print(f"  - {escape(str(s['name']))} ({s.get('cookie_count', 0)} cookies)")
            if not typer.confirm(f"Remove {len(matches)} session(s)?"):
                console.print("[dim]Cancelled[/dim]")
                return

        removed = []
        for s in matches:
            result = clear_session_cache(
                s["name"],
                backend_override=backend_override,
            )
            if result:
                removed.append(s)

        _print_removed(removed)
    else:
        try:
            target = resolve_session_name(target)
        except AmbiguousSessionError as exc:
            exit_ambiguous_session(exc)
        match = next((s for s in all_metadata if s["name"] == target), None)

        if not match:
            console.print(f"[red]Session '{escape(str(target))}' not found[/red]")
            raise typer.Exit(1)

        if not force:
            console.print("[yellow]Session to remove:[/yellow]")
            domain = escape(str(match.get("domain") or "no domain"))
            console.print(f"  - {escape(str(match['name']))} ({domain})")
            if not typer.confirm("Remove this session?"):
                console.print("[dim]Cancelled[/dim]")
                return

        clear_session_cache(target, backend_override=backend_override)
        _print_removed([match])


@session_app.command("use")
def session_use(
    name: Annotated[
        str,
        typer.Argument(help="Session name or plugin alias to set as active", metavar="SESSION"),
    ],
) -> None:
    """Set the active session for subsequent commands.

    Writes a .gp-session file in the current directory.
    Override per-shell with GRAFTPUNK_SESSION env var.
    """
    try:
        resolved = resolve_session_name(name)
    except AmbiguousSessionError as exc:
        exit_ambiguous_session(exc)
    path = set_active_session(resolved)
    console.print(f"[green]Active session set to '{escape(str(resolved))}'[/green]")
    if resolved != name:
        console.print(f"[dim](resolved from plugin '{escape(str(name))}')[/dim]")
    console.print(f"[dim]Written to: {escape(str(path))}[/dim]")


@session_app.command("unset")
def session_unset() -> None:
    """Clear the active session.

    Removes the .gp-session file in the current directory.
    """
    clear_active_session()
    console.print("[green]Active session cleared[/green]")


def _print_removed(removed: list[dict]) -> None:
    """Print the list of removed sessions."""
    if not removed:
        console.print("[dim]No sessions removed[/dim]")
        return

    console.print(f"\n[green]Removed {len(removed)} session(s):[/green]")
    for s in removed:
        domain = escape(str(s.get("domain") or "no domain"))
        console.print(f"  - {escape(str(s.get('name', '?')))} ({domain})")
