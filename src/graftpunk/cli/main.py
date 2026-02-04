"""graftpunk CLI - turn any website into an API.

Manage encrypted browser sessions from the terminal.
"""

import asyncio
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

import click
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

import graftpunk
from graftpunk.cli.http_commands import http_app
from graftpunk.cli.keepalive_commands import keepalive_app
from graftpunk.cli.plugin_commands import GraftpunkApp, resolve_session_name
from graftpunk.cli.session_commands import session_app
from graftpunk.config import get_settings
from graftpunk.logging import configure_logging, enable_network_debug, get_logger
from graftpunk.observe import OBSERVE_BASE_DIR
from graftpunk.plugins import (
    discover_keepalive_handlers,
    discover_site_plugins,
    discover_storage_backends,
)
from graftpunk.plugins.yaml_plugin import create_yaml_plugins
from graftpunk.session_context import resolve_session

# Configure logging early (before plugin registration) using env vars directly.
# We avoid calling get_settings() here because GraftpunkSettings.__init__ creates
# directories as a side effect, which breaks test isolation.
# The -v/-vv and --log-format flags in main_callback() may reconfigure later.
configure_logging(
    level=os.environ.get("GRAFTPUNK_LOG_LEVEL", "WARNING"),
    json_output=os.environ.get("GRAFTPUNK_LOG_FORMAT", "console") == "json",
)

LOG = get_logger(__name__)

app = GraftpunkApp(
    name="graftpunk",
    help="""
    🔌 graftpunk - turn any website into an API

    Graft scriptable access onto authenticated web services.
    Log in once, script forever.

    \b
    Quick start:
      gp session list          Show all cached sessions
      gp session show <name>   View session details
      gp session clear <name>  Remove a session
      gp config                Show current configuration

    \b
    Documentation: https://github.com/stavxyz/graftpunk
    """,
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)
console = Console()


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    verbose: Annotated[
        int,
        typer.Option(
            "--verbose",
            "-v",
            count=True,
            help="Increase verbosity (-v for info, -vv for debug)",
        ),
    ] = 0,
    log_format: Annotated[
        str | None,
        typer.Option(
            "--log-format",
            help="Log output format: console (human-readable) or json (structured)",
        ),
    ] = None,
    network_debug: Annotated[
        bool,
        typer.Option(
            "--network-debug",
            help="Enable deep HTTP/network debug logging (urllib3, httpx, http.client)",
        ),
    ] = False,
    observe: Annotated[
        str,
        typer.Option(
            "--observe",
            click_type=click.Choice(["off", "full"]),
            help="Observability capture mode",
        ),
    ] = "off",
) -> None:
    """graftpunk - turn any website into an API."""
    settings = get_settings()
    json_output = (log_format or settings.log_format) == "json"

    # Reconfigure logging if -v flags or --log-format override the settings default
    if verbose >= 2:
        configure_logging(level="DEBUG", json_output=json_output)
    elif verbose >= 1:
        configure_logging(level="INFO", json_output=json_output)
    elif log_format is not None:
        configure_logging(level=settings.log_level, json_output=json_output)

    if network_debug:
        enable_network_debug()

    ctx.ensure_object(dict)["observe_mode"] = observe


@app.command("version")
def version() -> None:
    """Show graftpunk version and installation info."""
    settings = get_settings()
    console.print(
        Panel(
            f"[bold cyan]graftpunk[/bold cyan] v{graftpunk.__version__}\n\n"
            f"[dim]Config:[/dim]  {settings.config_dir}\n"
            f"[dim]Storage:[/dim] {settings.storage_backend}",
            title="Turn any website into an API",
            border_style="cyan",
        )
    )


# Observe subcommand group
observe_app = typer.Typer(
    name="observe",
    help="View and manage observability data (HAR, screenshots, logs).",
)


@observe_app.callback(invoke_without_command=True)
def observe_callback(
    ctx: typer.Context,
    session: Annotated[
        str | None,
        typer.Option("--session", "-s", help="Session name to scope observe commands to"),
    ] = None,
) -> None:
    """View and manage observability data (HAR, screenshots, logs)."""
    resolved = resolve_session(session)
    if resolved and session:
        resolved = resolve_session_name(resolved)
    ctx.ensure_object(dict)["observe_session"] = resolved
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit(0)


@observe_app.command("list")
def observe_list(ctx: typer.Context) -> None:
    """List all observability runs."""
    if not OBSERVE_BASE_DIR.exists():
        console.print("[dim]No observe data found.[/dim]")
        return

    observe_session = ctx.ensure_object(dict).get("observe_session")

    runs: list[tuple[str, str]] = []
    if observe_session:
        session_dir = OBSERVE_BASE_DIR / observe_session
        if session_dir.is_dir():
            for run_dir in sorted(session_dir.iterdir()):
                if run_dir.is_dir():
                    runs.append((session_dir.name, run_dir.name))
    else:
        for session_dir in sorted(OBSERVE_BASE_DIR.iterdir()):
            if not session_dir.is_dir():
                continue
            for run_dir in sorted(session_dir.iterdir()):
                if run_dir.is_dir():
                    runs.append((session_dir.name, run_dir.name))

    if not runs:
        console.print("[dim]No observe runs found.[/dim]")
        return

    table = Table(
        title="Observe Runs",
        title_style="bold",
        header_style="bold cyan",
        border_style="dim",
    )
    table.add_column("Session", style="cyan")
    table.add_column("Run ID", style="white")

    for session_name, run_id in runs:
        table.add_row(session_name, run_id)

    console.print(table)
    console.print(f"\n[dim]{len(runs)} run(s)[/dim]")


@observe_app.command("show")
def observe_show(
    ctx: typer.Context,
    session_name: Annotated[
        str | None,
        typer.Argument(help="Session name to show runs for", metavar="SESSION"),
    ] = None,
    run_id: Annotated[
        str | None,
        typer.Argument(help="Specific run ID (default: latest)", metavar="RUN_ID"),
    ] = None,
) -> None:
    """Show details of an observability run."""
    if session_name is None:
        session_name = ctx.ensure_object(dict).get("observe_session")
    if session_name is None:
        console.print("[red]Session name required. Use --session or pass SESSION argument.[/red]")
        raise typer.Exit(1)
    session_dir = OBSERVE_BASE_DIR / session_name
    if not session_dir.exists() or not session_dir.is_dir():
        console.print(f"[red]No runs found for session '{session_name}'[/red]")
        raise typer.Exit(1)

    if run_id is None:
        # Use the latest run
        run_dirs = sorted(
            [d for d in session_dir.iterdir() if d.is_dir()],
            key=lambda d: d.name,
        )
        if not run_dirs:
            console.print(f"[red]No runs found for session '{session_name}'[/red]")
            raise typer.Exit(1)
        run_dir = run_dirs[-1]
    else:
        run_dir = session_dir / run_id
        if not run_dir.exists():
            console.print(f"[red]Run '{run_id}' not found for session '{session_name}'[/red]")
            raise typer.Exit(1)

    info = f"[bold]{session_name}[/bold] / {run_dir.name}\n"
    info += f"[dim]Path:[/dim] {run_dir}\n"

    # List files in the run directory
    files = sorted(run_dir.iterdir())
    file_list = []
    for f in files:
        if f.is_dir():
            subfiles = list(f.iterdir())
            file_list.append(f"  {f.name}/ ({len(subfiles)} files)")
        else:
            size = f.stat().st_size
            file_list.append(f"  {f.name} ({size} bytes)")

    if file_list:
        info += "[dim]Contents:[/dim]\n" + "\n".join(file_list)

    console.print(Panel(info.strip(), border_style="cyan"))


@observe_app.command("clean")
def observe_clean(
    ctx: typer.Context,
    session_name: Annotated[
        str | None,
        typer.Argument(help="Session to clean (omit to clean all)", metavar="SESSION"),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Skip confirmation prompt"),
    ] = False,
) -> None:
    """Remove observability data."""
    if session_name is None:
        session_name = ctx.ensure_object(dict).get("observe_session")
    if not OBSERVE_BASE_DIR.exists():
        console.print("[dim]No observe data to clean.[/dim]")
        return

    if session_name:
        target = OBSERVE_BASE_DIR / session_name
        if not target.exists():
            console.print(f"[dim]No data for session '{session_name}'[/dim]")
            return
        if not force:
            confirm = typer.confirm(f"Remove observe data for '{session_name}'?")
            if not confirm:
                console.print("[dim]Cancelled[/dim]")
                return
        shutil.rmtree(target)
        console.print(f"[green]Removed observe data for '{session_name}'[/green]")
    else:
        if not force:
            confirm = typer.confirm("Remove all observe data?")
            if not confirm:
                console.print("[dim]Cancelled[/dim]")
                return
        shutil.rmtree(OBSERVE_BASE_DIR)
        console.print("[green]Removed all observe data[/green]")


def _require_observe_session(ctx: typer.Context) -> str:
    """Extract and validate the observe session from context, or exit."""
    session_name = ctx.ensure_object(dict).get("observe_session")
    if not session_name:
        console.print(
            "[red]No session specified. Use --session, GRAFTPUNK_SESSION, or gp session use.[/red]"
        )
        raise typer.Exit(1)
    return session_name


@observe_app.command("go")
def observe_go(
    ctx: typer.Context,
    url: Annotated[
        str,
        typer.Argument(help="The URL to navigate to and capture"),
    ],
    wait: Annotated[
        float,
        typer.Option("--wait", "-w", help="Seconds to wait after page load"),
    ] = 3.0,
    max_body_size: Annotated[
        int,
        typer.Option("--max-body-size", help="Max response body size in bytes (default 5MB)"),
    ] = 5 * 1024 * 1024,
    interactive: Annotated[
        bool,
        typer.Option("--interactive", "-i", help="Keep browser open for manual exploration"),
    ] = False,
) -> None:
    """Open a URL in an authenticated browser and capture observability data.

    Loads the cached session cookies, opens a nodriver browser, injects
    cookies, navigates to the URL, and captures screenshots, page source,
    and HAR data.

    Requires an active session (via --session, GRAFTPUNK_SESSION, or gp session use).
    """
    session_name = _require_observe_session(ctx)

    if interactive:
        from graftpunk.logging import suppress_asyncio_noise

        with suppress_asyncio_noise():
            asyncio.run(_run_observe_interactive(session_name, url, max_body_size))
        return

    asyncio.run(_run_observe_go(session_name, url, wait, max_body_size))


async def _setup_observe_session(
    session_name: str,
    url: str,
    max_body_size: int,
    headless: bool,
) -> tuple[Any, Any, Any, Any] | None:
    """Set up browser session, inject cookies, initialize capture, and navigate to URL.

    Returns:
        Tuple of (browser, tab, storage, backend) or None on failure.
        The returned tab is the post-navigation tab.
    """
    import datetime

    import nodriver

    from graftpunk import load_session
    from graftpunk.exceptions import SessionExpiredError, SessionNotFoundError
    from graftpunk.observe.capture import NodriverCaptureBackend
    from graftpunk.observe.storage import ObserveStorage
    from graftpunk.session import inject_cookies_to_nodriver

    try:
        session = load_session(session_name)
    except SessionNotFoundError:
        console.print(
            f"[red]Session '{session_name}' not found.[/red]\n"
            f"[dim]Run 'gp session list' to see available sessions, "
            f"or log in first with your plugin's login command.[/dim]"
        )
        return None
    except SessionExpiredError as exc:
        console.print(
            f"[red]Session '{session_name}' is expired or corrupted.[/red]\n[dim]{exc}[/dim]"
        )
        return None
    except Exception as exc:  # noqa: BLE001 — CLI boundary: user-friendly error
        LOG.error("session_load_failed", session_name=session_name, error=str(exc))
        console.print(f"[red]Failed to load session '{session_name}': {exc}[/red]")
        return None

    browser = await nodriver.start(headless=headless)
    try:
        tab = browser.main_tab

        injected, filtered = await inject_cookies_to_nodriver(tab, session.cookies)
        msg = f"[dim]Injected {injected} cookie(s)"
        if filtered:
            msg += f" ({filtered} bot-detection cookie(s) filtered)"
        msg += "[/dim]"
        console.print(msg)

        run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
        storage = ObserveStorage(OBSERVE_BASE_DIR, session_name, run_id)
        bodies_dir = storage.run_dir / "bodies"
        backend = NodriverCaptureBackend(
            browser,
            get_tab=lambda: tab,
            bodies_dir=bodies_dir,
            max_body_size=max_body_size,
        )
        await backend.start_capture_async()

        tab = await browser.get(url)
        return browser, tab, storage, backend
    except Exception:
        browser.stop()
        raise


async def _save_observe_results(
    storage: Any,
    backend: Any,
    screenshot_label: str,
) -> None:
    """Save screenshot, page source, HAR, and console logs."""
    screenshot_data = await backend.take_screenshot()
    if screenshot_data:
        path = storage.save_screenshot(1, screenshot_label, screenshot_data)
        console.print(f"[green]Screenshot saved:[/green] {path}")
    else:
        console.print("[yellow]Screenshot capture failed[/yellow]")

    page_source = await backend.get_page_source()
    if page_source:
        source_path = storage.run_dir / "page-source.html"
        source_path.write_text(page_source, encoding="utf-8")
        console.print(f"[green]Page source saved:[/green] {source_path}")
    else:
        console.print("[yellow]Page source capture failed[/yellow]")

    await backend.stop_capture_async()

    har_entries = backend.get_har_entries()
    if har_entries:
        storage.write_har(har_entries)
        console.print(f"[green]HAR data saved:[/green] {len(har_entries)} entries")

    console_logs = backend.get_console_logs()
    if console_logs:
        storage.write_console_logs(console_logs)
        console.print(f"[green]Console logs saved:[/green] {len(console_logs)} entries")

    console.print(f"\n[bold]Observe run:[/bold] {storage.run_dir}")


async def _run_observe_go(session_name: str, url: str, wait: float, max_body_size: int) -> None:
    """Async implementation of observe go."""
    result = await _setup_observe_session(session_name, url, max_body_size, headless=True)
    if result is None:
        return

    browser, tab, storage, backend = result

    try:
        await tab.sleep(wait)
        await _save_observe_results(storage, backend, "observe-go")
    finally:
        browser.stop()


async def _run_observe_interactive(session_name: str, url: str, max_body_size: int) -> None:
    """Async implementation of observe interactive."""
    import signal

    result = await _setup_observe_session(session_name, url, max_body_size, headless=False)
    if result is None:
        return

    browser, tab, storage, backend = result

    try:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGINT, stop_event.set)
        except NotImplementedError:
            console.print(
                "[red]Interactive mode requires Unix/macOS (signal handlers unavailable).[/red]"
            )
            return

        console.print("\n[bold]Recording... press Ctrl+C to stop and save[/bold]\n")

        try:
            await stop_event.wait()
        finally:
            loop.remove_signal_handler(signal.SIGINT)

        console.print("\n[dim]Recording stopped. Saving capture...[/dim]")
        await _save_observe_results(storage, backend, "interactive-final")
    finally:
        browser.stop()


@observe_app.command("interactive")
def observe_interactive(
    ctx: typer.Context,
    url: Annotated[
        str,
        typer.Argument(help="The starting URL to navigate to"),
    ],
    max_body_size: Annotated[
        int,
        typer.Option("--max-body-size", help="Max response body size in bytes (default 5MB)"),
    ] = 5 * 1024 * 1024,
) -> None:
    """Record an interactive browser session into a HAR file.

    Opens an authenticated browser, navigates to the URL, and records all
    network traffic while you click around. Press Ctrl+C to stop and save.

    Requires an active session (via --session, GRAFTPUNK_SESSION, or gp session use).
    """
    session_name = _require_observe_session(ctx)

    from graftpunk.logging import suppress_asyncio_noise

    with suppress_asyncio_noise():
        asyncio.run(_run_observe_interactive(session_name, url, max_body_size))


app.add_typer(observe_app)

# Session subcommand group (defined in session_commands.py)
app.add_typer(session_app)

# Keepalive subcommand group (defined in keepalive_commands.py)
app.add_typer(keepalive_app)

# HTTP subcommand group (defined in http_commands.py)
app.add_typer(http_app)


@app.command("plugins")
def plugins() -> None:
    """List discovered plugins (storage, handlers, sites, CLI)."""
    storage = discover_storage_backends()
    handlers = discover_keepalive_handlers()
    site_plugins = discover_site_plugins()
    yaml_plugins, _ = create_yaml_plugins()  # Errors shown via plugin_commands
    from graftpunk.plugins.python_loader import discover_python_plugins

    python_file_plugins = discover_python_plugins()

    # Combine all plugin names from all sources
    all_plugin_names = set(site_plugins.keys())
    for plugin in yaml_plugins:
        all_plugin_names.add(plugin.site_name)
    for plugin in python_file_plugins.plugins:
        all_plugin_names.add(plugin.site_name)

    total = len(storage) + len(handlers) + len(all_plugin_names)

    def _fmt_section(title: str, names: set[str] | Mapping[str, object]) -> str:
        if names:
            items = "\n".join(f"  [green]✓[/green] {n}" for n in sorted(names))
            return f"[bold]{title}[/bold]\n{items}"
        return f"[bold]{title}[/bold]\n  [dim](none installed)[/dim]"

    sections = [
        _fmt_section("Storage Backends", storage),
        _fmt_section("Keepalive Handlers", handlers),
        _fmt_section("Site Plugins", all_plugin_names),
    ]

    content = "\n\n".join(sections)
    console.print(
        Panel(
            content,
            title=f"🔌 Plugins ({total} installed)",
            border_style="cyan",
        )
    )


@app.command("import-har")
def import_har_cmd(
    har_file: Annotated[
        Path,
        typer.Argument(
            help="Path to HAR file to import",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    name: Annotated[
        str,
        typer.Option(
            "--name",
            "-n",
            help="Plugin name (default: inferred from domain)",
        ),
    ] = "",
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Output file path (default: ~/.config/graftpunk/plugins/)",
        ),
    ] = None,
    format_type: Annotated[
        str,
        typer.Option(
            "--format",
            "-f",
            help="Output format: python or yaml",
        ),
    ] = "python",
    discover_api: Annotated[
        bool,
        typer.Option(
            "--discover-api/--no-discover-api",
            help="Discover API endpoints from requests",
        ),
    ] = True,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show what would be generated without writing files",
        ),
    ] = False,
) -> None:
    """Import HAR file and generate a graftpunk plugin.

    Analyzes HTTP traffic captured in HAR format to detect authentication
    flows and API endpoints, then generates a plugin you can customize.

    \b
    Examples:
        gp import-har auth-flow.har --name mysite
        gp import-har capture.har --format yaml --dry-run
        gp import-har api-trace.har -o ./my_plugin.py
    """
    from graftpunk.cli.import_har import import_har

    import_har(
        har_file=har_file,
        name=name,
        output=output,
        format_type=format_type,
        discover_api=discover_api,
        dry_run=dry_run,
    )


@app.command("config")
def config() -> None:
    """Show current graftpunk configuration."""
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

    console.print(Panel(info.strip(), title="⚙ Configuration", border_style="cyan"))


# Register plugin commands dynamically at module load time so they appear in --help.
# GraftpunkApp.__call__ injects plugin groups into the Click group before running.
_registered_plugins: dict[str, str] = {}
try:
    from graftpunk.cli.plugin_commands import register_plugin_commands

    _registered_plugins = register_plugin_commands(app)
    if _registered_plugins:
        LOG.debug("plugins_registered", count=len(_registered_plugins))
except (SystemExit, KeyboardInterrupt):
    raise
except Exception as exc:
    LOG.exception("plugin_registration_failed", error=str(exc))
    # Notify user - plugins are optional but they should know if they fail
    console.print(f"[yellow]Warning: Plugin registration failed: {exc}[/yellow]")

if __name__ == "__main__":
    app()
