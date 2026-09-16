"""The ``gp observe`` sub-app: every command, and the shared run resolver.

Owns ``observe_app`` and all seven commands (``list``, ``show``, ``clean``,
``go``, ``interactive``, ``digest``, ``fixtures``). ``main.py`` imports the
sub-app and attaches it; the browser machinery the capture commands drive
lives in the leaf module ``observe_browser``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json as jsonlib
import shutil
from pathlib import Path
from typing import Annotated, NoReturn
from urllib.parse import urlparse

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from graftpunk.cli.observe_browser import run_observe_go, run_observe_interactive
from graftpunk.cli.plugin_commands import resolve_session_name_or_exit
from graftpunk.devtools.captures import CAPTURES_DIR, ensure_ignored, find_repo_root, is_tracked
from graftpunk.har.digest import DigestSource, body_params, digest
from graftpunk.har.naming import capture_filename
from graftpunk.har.parser import parse_har_file
from graftpunk.har.paths import template_path
from graftpunk.har.report import DEFAULT_ENDPOINT_LIMIT, render_json, render_markdown
from graftpunk.logging import get_logger
from graftpunk.observe import OBSERVE_BASE_DIR
from graftpunk.observe.storage import session_dirname
from graftpunk.plugins import infer_site_name
from graftpunk.session_context import resolve_session

console = Console()
LOG = get_logger(__name__)

_DEFAULT_FIXTURE_LIMIT = 5
_HTTP_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"}
)

observe_app = typer.Typer(
    name="observe",
    help="View and manage observability data (HAR, screenshots, logs).",
)


def resolve_run(session_name: str, run_id: str | None, *, base_dir: Path | None = None) -> Path:
    """The run directory (session[, run]) means: the newest run when *run_id* is omitted.

    ``base_dir`` defaults to this module's ``OBSERVE_BASE_DIR`` binding, which
    every production caller relies on; the keyword exists for tests that
    resolve against a temporary tree (its four callers are in
    ``tests/unit/test_observe_commands.py``).

    Raises:
        typer.Exit: No runs exist for the session, or the named run is missing.
    """
    base = base_dir if base_dir is not None else OBSERVE_BASE_DIR
    session_dir = base / session_dirname(session_name)
    if not session_dir.is_dir():
        console.print(f"[red]No runs found for session '{escape(session_name)}'[/red]")
        raise typer.Exit(1)

    if run_id is None:
        run_dirs = sorted(d for d in session_dir.iterdir() if d.is_dir())
        if not run_dirs:
            console.print(f"[red]No runs found for session '{escape(session_name)}'[/red]")
            raise typer.Exit(1)
        return run_dirs[-1]

    run_dir = session_dir / run_id
    if not run_dir.exists():
        console.print(
            f"[red]Run '{escape(run_id)}' not found for session '{escape(session_name)}'[/red]"
        )
        raise typer.Exit(1)
    return run_dir


def _refuse_write(path: Path, exc: OSError) -> NoReturn:
    """Report an unwritable *path* as a refusal, not as a Rich traceback.

    Every CLI refusal is a red line and exit 1: an unwritable ``--output`` or
    ``--out`` directory is the user's to correct (final fix wave, 2026-09-12).
    """
    target = exc.filename or str(path)
    reason = exc.strerror or str(exc)
    console.print(f"[red]Could not write {escape(str(target))}: {escape(reason)}[/red]")
    raise typer.Exit(1) from None


def _digest_source(session_name: str | None, run_id: str | None, har: Path | None) -> DigestSource:
    if har is not None and session_name is not None:
        console.print("[red]Pass a session or --har, not both.[/red]")
        raise typer.Exit(1)
    if har is not None:
        if not har.exists():
            console.print(f"[red]HAR file not found: {escape(str(har))}[/red]")
            raise typer.Exit(1)
        return DigestSource.from_har(har)
    if session_name is None:
        console.print("[red]Session name required, or pass --har.[/red]")
        raise typer.Exit(1)
    run_dir = resolve_run(session_name, run_id)
    if not (run_dir / "network.har").exists():
        console.print(f"[red]No network.har in run '{escape(run_dir.name)}'[/red]")
        raise typer.Exit(1)
    return DigestSource.from_run_dir(run_dir, session=session_name, run_id=run_dir.name)


def digest_cmd(
    session: Annotated[str | None, typer.Argument(metavar="SESSION")] = None,
    run: Annotated[str | None, typer.Argument(metavar="RUN_ID")] = None,
    har: Annotated[
        Path | None, typer.Option("--har", help="Digest a bare HAR file instead of a run")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the complete digest as JSON")
    ] = False,
    all_hosts: Annotated[
        bool, typer.Option("--all-hosts", help="Model every host, not just the primary one")
    ] = False,
    limit: Annotated[
        int, typer.Option("--limit", min=1, help="Max endpoints in the markdown form")
    ] = DEFAULT_ENDPOINT_LIMIT,
    output: Annotated[
        Path | None, typer.Option("--output", help="Write to a file instead of stdout")
    ] = None,
) -> None:
    """Read a HAR (a run or a bare file) into a readable digest of hosts, endpoints, login, and
    tokens."""
    source = _digest_source(session, run, har)
    result = digest(source, all_hosts=all_hosts)
    text = render_json(result) if as_json else render_markdown(result, limit=limit)
    if output is not None:
        try:
            output.write_text(text, encoding="utf-8")
        except OSError as exc:
            _refuse_write(output, exc)
        console.print(f"[green]Digest written:[/green] {escape(str(output))}")
    else:
        # soft_wrap=True: Console.print's default wrapping breaks a long
        # unbroken token (e.g. a HAR path) mid-word, corrupting --json output
        # (found running TestDigestCommand.test_digest_json_flag: the printed
        # text failed json.loads because a path was hard-wrapped inside a
        # string value; deviation from the brief, which omitted soft_wrap).
        console.print(text, markup=False, highlight=False, soft_wrap=True)


def _matches_template(entry_method: str, entry_template: str, pattern: str) -> bool:
    method_part, _, path_part = pattern.strip().partition(" ")
    if method_part.upper() != entry_method:
        return False
    return entry_template == path_part or fnmatch.fnmatch(entry_template, path_part)


def _validate_match_patterns(patterns: list[str]) -> None:
    """Refuse a ``--match`` value that cannot match anything.

    The matcher partitions on a space, so ``--match "/orders"`` reads as the
    method ``/orders`` against an empty template and silently matches nothing:
    the user sees "No entries matched" and no way to tell a typo from an empty
    run (final fix wave, 2026-09-12).
    """
    for pattern in patterns:
        method_part, separator, path_part = pattern.strip().partition(" ")
        if not separator or not path_part.strip() or method_part.upper() not in _HTTP_METHODS:
            console.print(
                f"[red]--match '{escape(pattern)}' is not a \"METHOD template\" pair. "
                f"Write the method, a space, then the template, as in "
                f'"GET /orders/{{order_id}}".[/red]'
            )
            raise typer.Exit(1)


def fixtures_cmd(
    session: Annotated[str, typer.Argument(metavar="SESSION")],
    run: Annotated[str | None, typer.Argument(metavar="RUN_ID")] = None,
    match: Annotated[
        list[str], typer.Option("--match", help='"METHOD template" (repeatable)')
    ] = [],  # noqa: B006 - Typer reads this default at decoration time, never mutated per-call
    out: Annotated[Path | None, typer.Option("--out", help=f"Defaults to ./{CAPTURES_DIR}")] = None,
    limit: Annotated[
        int, typer.Option("--limit", min=1, help="Max files per matched template")
    ] = _DEFAULT_FIXTURE_LIMIT,
    allow_tracked: Annotated[
        bool, typer.Option("--allow-tracked", help="Write even onto a tracked path")
    ] = False,
) -> None:
    """Write captured response bodies exactly as recorded, named for deriving test fixtures.

    Only text bodies are written: an entry the capture holds no text for (an image, a
    font, any binary response) is named on stdout and skipped.
    """
    if not match:
        console.print("[red]--match is required (repeatable).[/red]")
        raise typer.Exit(1)
    _validate_match_patterns(match)

    run_dir = resolve_run(session, run)
    har_path = run_dir / "network.har"
    if not har_path.exists():
        console.print(f"[red]No network.har in run '{escape(run_dir.name)}'[/red]")
        raise typer.Exit(1)

    target_dir = out if out is not None else Path.cwd() / CAPTURES_DIR

    if not allow_tracked and target_dir.exists():
        tracked = [p for p in sorted(target_dir.rglob("*")) if p.is_file() and is_tracked(p)]
        if tracked:
            console.print("[red]Refusing to write: these paths are tracked by git:[/red]")
            for path in tracked:
                console.print(f"  {escape(str(path))}", soft_wrap=True)
            console.print("[dim]Pass --allow-tracked to write anyway.[/dim]")
            raise typer.Exit(1)

    # Below the tracked-path check: the ignore line exists to protect files
    # this command is about to write, so a refusal must not leave an edited
    # .gitignore behind (polish round 1, 2026-09-12).
    repo_root = find_repo_root(target_dir)
    if repo_root is not None:
        try:
            relative = str(target_dir.resolve().relative_to(repo_root))
        except ValueError:
            relative = CAPTURES_DIR
        if ensure_ignored(repo_root, relative):
            console.print(f"[dim]Added '{escape(relative)}/' to .gitignore[/dim]")
    else:
        console.print(
            "[yellow]Not inside a git work tree: nothing protects this directory "
            "from being committed.[/yellow]"
        )

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _refuse_write(target_dir, exc)
    entries = parse_har_file(har_path).entries
    per_template_count: dict[str, int] = {}
    written: list[Path] = []
    for entry in entries:
        path = urlparse(entry.request.url).path or "/"
        template, _ = template_path(path)
        method = entry.request.method.upper()
        if not any(_matches_template(method, template, pattern) for pattern in match):
            continue
        content_type = entry.response.content_type or "application/octet-stream"
        if entry.response.body is None:
            # A capture holds no text for a binary response, and writing
            # `body or ""` put a zero-byte file on disk that reads as a real
            # (empty) fixture (final fix wave, 2026-09-12).
            console.print(
                f"[dim]Skipped {escape(method)} {escape(template)}: "
                f"no text body ({escape(content_type)})[/dim]"
            )
            continue

        key = f"{method} {template}"
        seen = per_template_count.get(key, 0)
        if seen >= limit:
            continue
        per_template_count[key] = seen + 1

        filename = capture_filename(method, path, content_type)
        if seen > 0:
            stem, _, ext = filename.rpartition(".")
            filename = f"{stem}_{seen}.{ext}"
        file_path = target_dir / filename
        meta_path = target_dir / f"{filename}.meta.json"
        try:
            file_path.write_text(entry.response.body, encoding="utf-8")
            meta_path.write_text(
                jsonlib.dumps(
                    {
                        "url": entry.request.url,
                        "status": entry.response.status,
                        "content_type": content_type,
                        "body_params": sorted(body_params(entry)),
                        "captured_at": entry.timestamp.isoformat(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            _refuse_write(file_path, exc)
        written.append(file_path)
        # soft_wrap: see digest_cmd. A path listing that breaks mid-word at 80
        # columns cannot be copied (polish round 1, 2026-09-12).
        console.print(f"[green]Wrote:[/green] {escape(str(file_path))}", soft_wrap=True)

    if not written:
        console.print("[yellow]No entries matched --match.[/yellow]")


# Attached here, above the decorated commands below, because Typer lists
# commands in attachment order and these two led `gp observe --help` when
# main.py called register() before defining the rest.
observe_app.command("digest")(digest_cmd)
observe_app.command("fixtures")(fixtures_cmd)


@observe_app.callback(invoke_without_command=True)
def observe_callback(
    ctx: typer.Context,
    session: Annotated[
        str | None,
        typer.Option("--session", "-s", help="Session name to scope observe commands to"),
    ] = None,
    no_session: Annotated[
        bool,
        typer.Option("--no-session", help="Run without loading a cached session"),
    ] = False,
) -> None:
    """View and manage observability data (HAR, screenshots, logs)."""
    if no_session and session:
        console.print("[red]Cannot use --session and --no-session together.[/red]")
        raise typer.Exit(1)

    obj = ctx.ensure_object(dict)
    obj["observe_no_session"] = no_session

    if no_session:
        obj["observe_session"] = None
    else:
        resolved = resolve_session(session)
        if resolved and session:
            resolved = resolve_session_name_or_exit(resolved)
        obj["observe_session"] = resolved
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
        # The writer (opt-in login capture) slugifies the session name into
        # its run-dir name (e.g. "myshop@alice" -> "myshop-alice"); the
        # lookup must apply the identical transformation or labelled runs
        # are undiscoverable (#151).
        session_dir = OBSERVE_BASE_DIR / session_dirname(observe_session)
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
        table.add_row(escape(session_name), escape(run_id))

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
    run_dir = resolve_run(session_name, run_id)

    info = f"[bold]{escape(session_name)}[/bold] / {escape(run_dir.name)}\n"
    info += f"[dim]Path:[/dim] {escape(str(run_dir))}\n"

    # List files in the run directory
    files = sorted(run_dir.iterdir())
    file_list = []
    for f in files:
        if f.is_dir():
            subfiles = list(f.iterdir())
            file_list.append(f"  {escape(f.name)}/ ({len(subfiles)} files)")
        else:
            size = f.stat().st_size
            file_list.append(f"  {escape(f.name)} ({size} bytes)")

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
        # See observe_list: the lookup dir must match the writer's slugified name.
        target = OBSERVE_BASE_DIR / session_dirname(session_name)
        if not target.exists():
            console.print(f"[dim]No data for session '{escape(session_name)}'[/dim]")
            return
        if not force:
            confirm = typer.confirm(f"Remove observe data for '{session_name}'?")
            if not confirm:
                console.print("[dim]Cancelled[/dim]")
                return
        shutil.rmtree(target)
        console.print(f"[green]Removed observe data for '{escape(session_name)}'[/green]")
    else:
        if not force:
            confirm = typer.confirm("Remove all observe data?")
            if not confirm:
                console.print("[dim]Cancelled[/dim]")
                return
        shutil.rmtree(OBSERVE_BASE_DIR)
        console.print("[green]Removed all observe data[/green]")


def _resolve_observe_context(ctx: typer.Context, url: str) -> tuple[str, str | None]:
    """Resolve observe namespace and session name from context.

    Returns:
        Tuple of (namespace, session_name). ``session_name`` is ``None``
        when ``--no-session`` is set or no session is available.

    Raises:
        typer.Exit: If no session is specified and ``--no-session`` is not set.
    """
    obj = ctx.ensure_object(dict)
    no_session = obj.get("observe_no_session", False)
    session_name: str | None = None if no_session else obj.get("observe_session")

    if session_name:
        return session_name, session_name

    if no_session:
        inferred = infer_site_name(url)
        if not inferred:
            LOG.warning("namespace_inference_failed", url=url, fallback="unknown")
            inferred = "unknown"
        return inferred, None

    # No session and no --no-session: require a session (original behavior)
    console.print(
        "[red]No session specified. Use --session, GRAFTPUNK_SESSION, "
        "gp session use, or --no-session.[/red]"
    )
    raise typer.Exit(1)


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
    """Open a URL in a browser and capture observability data.

    Opens a nodriver browser, injects cached session cookies (if available),
    navigates to the URL, and captures screenshots, page source, and HAR data.

    Use --no-session to open the browser without cookies.
    """
    namespace, session_name = _resolve_observe_context(ctx, url)

    if interactive:
        from graftpunk.logging import suppress_asyncio_noise

        with suppress_asyncio_noise():
            asyncio.run(
                run_observe_interactive(namespace, url, max_body_size, session_name=session_name)
            )
        return

    asyncio.run(run_observe_go(namespace, url, wait, max_body_size, session_name=session_name))


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

    Opens a browser, navigates to the URL, and records all network traffic
    while you click around. Press Ctrl+C to stop and save.

    Use --no-session to open the browser without cookies.
    """
    namespace, session_name = _resolve_observe_context(ctx, url)

    from graftpunk.logging import suppress_asyncio_noise

    with suppress_asyncio_noise():
        asyncio.run(
            run_observe_interactive(namespace, url, max_body_size, session_name=session_name)
        )
