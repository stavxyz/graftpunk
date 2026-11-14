"""Session management commands — gp session list/show/clear/export."""

import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from graftpunk import (
    get_session_metadata,
    list_sessions_with_metadata,
    load_session,
)
from graftpunk.cache import list_incomplete_sessions, list_sessions, remove_session_objects
from graftpunk.cli import plugin_commands
from graftpunk.cli.errors import needs_browser_stack
from graftpunk.cli.plugin_commands import resolve_session_name_or_exit
from graftpunk.exceptions import (
    GraftpunkError,
    SessionExpiredError,
    SessionNotFoundError,
    SessionNotFullyClearedError,
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
console = Console(emoji=False)


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


# Left out first when the table does not fit the terminal: gp session show and
# --json carry them, while Account is the column that tells two sessions of one
# site apart (#179).
_NARROW_DROPPED = frozenset(("Cookies", "Backend", "Location"))
_UNBOUNDED_WIDTH = 100_000


def _session_row(session: dict) -> dict[str, Text | str]:
    """The cells of one session's row in ``gp session list``, by column."""
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

    return {
        "Session": Text(str(session.get("name", ""))),
        "Domain": _cell(session.get("domain")),
        "Account": _cell(session.get("account_identifier") or _label_of(session.get("name", ""))),
        "Status": status_display,
        "Cookies": Text(str(session.get("cookie_count", 0))),
        "Last Modified": _cell(modified),
        "Backend": _cell(session.get("storage_backend")),
        "Location": _cell(session.get("storage_location")),
    }


def _sessions_table(rows: list[dict[str, Text | str]], *, narrow: bool) -> Table:
    """The ``gp session list`` table; *narrow* leaves out :data:`_NARROW_DROPPED`
    and keeps Account on one line, so Domain and Last Modified wrap first. A
    value is folded where it wraps, never cut to an ellipsis, except an Account
    or Session wider than the terminal leaves room for."""
    table = Table(
        title="Cached Sessions",
        title_style="bold",
        header_style="bold cyan",
        border_style="dim",
    )
    columns: tuple[tuple[str, dict[str, Any]], ...] = (
        ("Session", {"style": "cyan", "no_wrap": True}),
        ("Domain", {"style": "white", "overflow": "fold"}),
        ("Account", {"style": "white", "no_wrap": narrow}),
        ("Status", {"justify": "center"}),
        ("Cookies", {"justify": "right", "style": "dim"}),
        ("Last Modified", {"style": "dim", "overflow": "fold"}),
        ("Backend", {"style": "dim", "no_wrap": True}),
        ("Location", {"style": "dim"}),
    )
    shown = [
        (name, options) for name, options in columns if not (narrow and name in _NARROW_DROPPED)
    ]
    for name, options in shown:
        # "Last Modified" would fold mid-word in the column a narrow table leaves it.
        header = "Modified" if narrow and name == "Last Modified" else name
        table.add_column(header, **options)
    for row in rows:
        table.add_row(*(row[name] for name, _ in shown))
    return table


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

    rows = [_session_row(session) for session in sessions]
    table = _sessions_table(rows, narrow=False)
    # Measured unbounded: a measure within the terminal is capped at its width.
    unbounded = console.options.update(max_width=_UNBOUNDED_WIDTH)
    narrow = console.measure(table, options=unbounded).maximum > console.width
    if narrow:
        table = _sessions_table(rows, narrow=True)
    console.print(table)
    console.print(f"\n[dim]{len(sessions)} session(s) cached[/dim]")
    if narrow:
        console.print(
            "[dim]Cookies, Backend and Location are left out at this width "
            "(see gp session show).[/dim]"
        )


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
    name = resolve_session_name_or_exit(name, backend_override=backend_override)
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
@needs_browser_stack
def export(
    ctx: typer.Context,
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

        http --session=PATH https://example.com/api

    where PATH is the exported file, which the command prints.
    """
    backend_override = ctx.obj.get("storage_backend") if ctx.obj else None
    name = resolve_session_name_or_exit(name, backend_override=backend_override)
    try:
        session = load_session(name, backend_override=backend_override)
    except SessionNotFoundError:
        console.print(f"[red]✗ Session '{escape(str(name))}' not found[/red]")
        raise typer.Exit(1) from None
    except SessionExpiredError as exc:
        console.print(f"[red]✗ Session expired: {escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    except GraftpunkError as exc:
        console.print(f"[red]✗ Failed to load: {escape(str(exc))}[/red]")
        raise typer.Exit(1) from None

    # httpie is part of the browser stack, which load_session above needed.
    # importing_httpie keeps httpie's cookie policy out of the process (#253).
    from graftpunk.cookies import importing_httpie

    with importing_httpie():
        from httpie.config import ConfigFileError

    try:
        browser_session = cast("BrowserSession", session)
        httpie_path = browser_session.save_httpie_session(name, output=output)

        console.print(f"[green]✓ Exported to:[/green] {escape(str(httpie_path))}\n")
        console.print("[dim]Usage:[/dim]")
        # HTTPie reads a --session value containing a path separator as that
        # file; a bare name would be looked up under sessions/<host>/ instead.
        session_arg = shlex.quote(str(httpie_path))
        # One line however narrow the terminal: a wrapped path pastes as two
        # arguments.
        console.print(
            f"  http --session={escape(session_arg)} https://example.com/api", soft_wrap=True
        )
    except (OSError, AttributeError, ConfigFileError) as exc:
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
    """Remove cached session(s), with every object stored under each one.

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
        # Slots a failed save left without metadata: not sessions, but loading
        # one says to clear, so clearing everything removes them too (#293).
        incomplete = [
            {"name": name, "domain": _INCOMPLETE} for name in _incomplete_or_exit(backend_override)
        ]
        if not all_metadata and not incomplete:
            console.print("[dim]No sessions to clear[/dim]")
            return

        if not force:
            count = len(all_metadata) + len(incomplete)
            console.print(f"[yellow]This will remove {count} session(s):[/yellow]")
            for s in [*all_metadata, *incomplete]:
                console.print(
                    f"  - {escape(str(s['name']))} ({escape(str(s.get('domain') or 'no domain'))})"
                )
            if not typer.confirm("Remove all sessions?"):
                console.print("[dim]Cancelled[/dim]")
                return

        _clear_and_report([*all_metadata, *incomplete], backend_override)
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

        _clear_and_report(matches, backend_override)
    else:
        target, match = _slot_to_clear(target, all_metadata, backend_override)

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

        _clear_and_report([match], backend_override)


@session_app.command("use")
def session_use(
    ctx: typer.Context,
    name: Annotated[
        str,
        typer.Argument(help="Session name or plugin alias to set as active", metavar="SESSION"),
    ],
) -> None:
    """Set the active session for subsequent commands.

    Writes a .gp-session file in the current directory.
    Override per-shell with GRAFTPUNK_SESSION env var.
    """
    backend_override = ctx.obj.get("storage_backend") if ctx.obj else None
    resolved = resolve_session_name_or_exit(name, backend_override=backend_override)
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


# How gp session clear lists a slot that holds objects but no complete session.
_INCOMPLETE = "incomplete, not a full session"


def _incomplete_or_exit(backend_override: str | None) -> list[str]:
    """The backend's incomplete slots; a storage error prints one line and exits 1."""
    try:
        return list_incomplete_sessions(backend_override=backend_override)
    except (ValueError, StorageError) as exc:
        console.print(f"[red]✗ Storage backend error: {escape(str(exc))}[/red]")
        raise typer.Exit(1) from None


def _slot_to_clear(
    target: str, all_metadata: list[dict], backend_override: str | None
) -> tuple[str, dict | None]:
    """The slot ``gp session clear <target>`` removes, and its listing entry.

    The exact slot comes first, complete or holding leftovers only: a clear
    never removes a slot the user did not name (#382). Then, for a plugin site
    name, its base when only leftovers are cached there, as the exact-base rule
    takes a complete base. Only with neither is the name resolved, so a
    labelled sibling is reached only when no slot of that name exists.
    """
    exact = next((s for s in all_metadata if s["name"] == target), None)
    if exact is not None:
        return target, exact
    try:
        listed = target in list_sessions(backend_override=backend_override)
    except (ValueError, StorageError) as exc:
        console.print(f"[red]✗ Storage backend error: {escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    if listed:
        # A session with no readable metadata (a legacy flat file): still its
        # own slot, under whatever name an older release gave it.
        return target, {"name": target, "domain": None}
    incomplete = _incomplete_or_exit(backend_override)
    if target in incomplete:
        return target, {"name": target, "domain": _INCOMPLETE}
    base = plugin_commands._plugin_session_map.get(target)
    if base is not None and base in incomplete:
        return base, {"name": base, "domain": _INCOMPLETE}
    resolved = resolve_session_name_or_exit(target, backend_override=backend_override)
    return resolved, next((s for s in all_metadata if s["name"] == resolved), None)


def _remove_each(
    slots: list[dict], backend_override: str | None
) -> tuple[list[tuple[dict, list[str]]], bool]:
    """Delete everything stored under each slot; pair each removed slot with what went.

    A slot unsafe to delete under, a storage error, or a delete that left files
    behind prints one line naming the slot (and what it kept) and the others go
    on; the second value says whether any failed.
    """
    removed = []
    failed = False
    for slot in slots:
        try:
            objects = remove_session_objects(slot["name"], backend_override=backend_override)
        except SessionNotFullyClearedError as exc:
            if exc.removed:
                removed.append((slot, exc.removed))
            kept = ", ".join(exc.remaining)
            console.print(
                f"[red]✗ {escape(str(slot['name']))} kept {len(exc.remaining)} file(s): "
                f"{escape(kept)}[/red]"
            )
            failed = True
            continue
        except (ValueError, StorageError) as exc:
            console.print(
                f"[red]✗ Could not clear {escape(str(slot['name']))}: {escape(str(exc))}[/red]"
            )
            failed = True
            continue
        if objects:
            removed.append((slot, list(objects)))
    return removed, failed


def _clear_and_report(slots: list[dict], backend_override: str | None) -> None:
    """Clear *slots*, print what went, and exit 1 when any slot could not be cleared."""
    removed, failed = _remove_each(slots, backend_override)
    _print_removed(removed)
    if failed:
        raise typer.Exit(1)


def _print_removed(removed: list[tuple[dict, list[str]]]) -> None:
    """Print each removed session and every object deleted with it."""
    if not removed:
        console.print("[dim]No sessions removed[/dim]")
        return

    console.print(f"\n[green]Removed {len(removed)} session(s):[/green]")
    for s, objects in removed:
        domain = escape(str(s.get("domain") or "no domain"))
        console.print(f"  - {escape(str(s.get('name', '?')))} ({domain})")
        for key in objects:
            console.print(f"      [dim]{escape(str(key))}[/dim]")
