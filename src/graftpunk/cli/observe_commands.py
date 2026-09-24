"""The ``gp observe`` sub-app: every command, and the shared run resolver.

Owns ``observe_app`` and all seven commands (``list``, ``show``, ``clean``,
``go``, ``interactive``, ``digest``, ``fixtures``). ``main.py`` imports the
sub-app and attaches it; the browser machinery the capture commands drive
lives in the leaf module ``observe_browser``.
"""

from __future__ import annotations

import asyncio
import fnmatch
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
from graftpunk.console import err_console
from graftpunk.devtools.captures import (
    IgnoreFileReadError,
    ensure_ignored,
    find_repo_root,
    is_tracked,
    write_sidecar,
)
from graftpunk.devtools.captures_rule import CAPTURES_DIR
from graftpunk.har.digest import (
    DigestSource,
    RunDigest,
    body_params,
    digest,
    endpoint_template,
    flagged_names_of,
    redacted_names_of,
)
from graftpunk.har.naming import (
    EndpointSpecError,
    capture_filename,
    capture_slug,
    parse_endpoint,
)
from graftpunk.har.parser import HAREntry, parse_har_file
from graftpunk.har.report import (
    DEFAULT_ENDPOINT_LIMIT,
    render_endpoints_json,
    render_json,
    render_markdown,
)
from graftpunk.logging import get_logger
from graftpunk.observe import OBSERVE_BASE_DIR
from graftpunk.observe.storage import session_dirname
from graftpunk.plugins import infer_site_name
from graftpunk.session_context import resolve_session

console = Console()
LOG = get_logger(__name__)

_DEFAULT_FIXTURE_LIMIT = 5

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
    ``--out`` directory is the user's to correct.
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
    endpoints_json: Annotated[
        bool,
        typer.Option(
            "--endpoints-json",
            help="Print the versioned endpoint projection a program reads (uncapped)",
        ),
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
    if as_json and endpoints_json:
        # On stderr: a caller asking for JSON reads stdout as JSON.
        err_console.print("[red]Pass --json or --endpoints-json, not both.[/red]")
        raise typer.Exit(1)
    source = _digest_source(session, run, har)
    result = digest(source, all_hosts=all_hosts)
    if endpoints_json:
        text = render_endpoints_json(result)
    elif as_json:
        text = render_json(result)
    else:
        text = render_markdown(result, limit=limit)
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


def _matches_template(entry_method: str, entry_template: str, endpoint: tuple[str, str]) -> bool:
    """True when the entry is *endpoint*: the same method, and a template equal to the
    pattern's or matching it as a glob. *endpoint* is :func:`parse_endpoint`'s pair;
    this function never splits a pattern itself."""
    method, template = endpoint
    if method != entry_method:
        return False
    return entry_template == template or fnmatch.fnmatch(entry_template, template)


def _parsed_matches(patterns: list[str]) -> list[tuple[str, str]]:
    """Every ``--match`` value as its pair, refusing the first that does not parse."""
    parsed: list[tuple[str, str]] = []
    for pattern in patterns:
        try:
            parsed.append(parse_endpoint(pattern))
        except EndpointSpecError as exc:
            console.print(f"[red]--match: {escape(str(exc))}[/red]")
            raise typer.Exit(1) from None
    return parsed


def _capture_text(entry: HAREntry) -> str | None:
    """The text a fixture for *entry* holds: its body; an empty string for a 3xx or
    a 204 recorded with no text (graftpunk's own recorder writes ``"text": null``
    for a redirect hop), which has no body by definition; None for any other
    response recorded with no text (a binary one), which gets no fixture."""
    if entry.response.body is not None:
        return entry.response.body
    status = entry.response.status
    return "" if 300 <= status < 400 or status == 204 else None


def _colliding_file_names(
    entries: list[HAREntry], run_digest: RunDigest, endpoints: list[tuple[str, str]]
) -> dict[str, set[str]]:
    """The capture stems two or more matched endpoints would share (``/a_b`` and
    ``/a/b`` both name ``get_a_b``, whatever each one's extension), each with those
    endpoints: ``FixtureSession`` looks a fixture up by stem."""
    keys_by_name: dict[str, set[str]] = {}
    # Keyed on the folded stem, printed as the first real stem seen under it.
    shown: dict[str, str] = {}
    for entry in entries:
        try:
            path = urlparse(entry.request.url).path or "/"
        except ValueError:
            continue
        template = endpoint_template(run_digest, path)
        method = entry.request.method.upper()
        if _capture_text(entry) is None or not any(
            _matches_template(method, template, e) for e in endpoints
        ):
            continue
        # Case-folded: on a case-insensitive filesystem get_Users and get_users
        # are one file.
        stem = capture_slug(method, template)
        name = stem.casefold()
        shown.setdefault(name, stem)
        keys_by_name.setdefault(name, set()).add(f"{method} {template}")
    return {shown[name]: keys for name, keys in keys_by_name.items() if len(keys) > 1}


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
    endpoints = _parsed_matches(match)

    run_dir = resolve_run(session, run)
    har_path = run_dir / "network.har"
    if not har_path.exists():
        console.print(f"[red]No network.har in run '{escape(run_dir.name)}'[/red]")
        raise typer.Exit(1)

    target_dir = out if out is not None else Path.cwd() / CAPTURES_DIR

    # Everything read from the run comes before any write: a digest that fails
    # must not leave an edited .gitignore or an empty target behind.
    entries = parse_har_file(har_path).entries
    run_digest = digest(DigestSource.from_run_dir(run_dir, session=session, run_id=run_dir.name))
    flagged = flagged_names_of(run_digest, entries)
    redacted = redacted_names_of(run_digest, entries)

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
    # .gitignore behind.
    repo_root = find_repo_root(target_dir)
    if repo_root is not None:
        try:
            relative = str(target_dir.resolve().relative_to(repo_root))
        except ValueError:
            relative = CAPTURES_DIR
        gitignore = repo_root / ".gitignore"
        try:
            added = ensure_ignored(repo_root, relative)
        except IsADirectoryError:
            console.print(
                f"[red]Refusing to write {escape(str(gitignore))}: it is a directory, "
                "not a file[/red]",
                soft_wrap=True,
            )
            raise typer.Exit(1) from None
        except UnicodeDecodeError:
            # The same refusal gp plugin new gives this file.
            console.print(
                f"[red]Refusing to write {escape(str(gitignore))}: it is not UTF-8 text[/red]",
                soft_wrap=True,
            )
            raise typer.Exit(1) from None
        except OSError as exc:
            # Which step failed: a read never changed the file; an append may have.
            step = "read" if isinstance(exc, IgnoreFileReadError) else "write"
            reason = exc.strerror or str(exc)
            console.print(
                f"[red]Could not {step} {escape(str(gitignore))}: {escape(reason)}[/red]",
                soft_wrap=True,
            )
            raise typer.Exit(1) from None
        if added:
            console.print(f"[dim]Added '{escape(relative)}/' to .gitignore[/dim]")
    else:
        console.print(
            "[yellow]Not inside a git work tree: nothing protects this directory "
            "from being committed.[/yellow]"
        )

    collisions = _colliding_file_names(entries, run_digest, endpoints)
    if collisions:
        for filename, keys in sorted(collisions.items()):
            console.print(
                f"[red]Refusing to write {escape(filename)}: "
                f"{escape(' and '.join(sorted(keys)))} would share that fixture name. "
                "Narrow --match to one of them.[/red]",
                soft_wrap=True,
                highlight=False,
            )
        raise typer.Exit(1)

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _refuse_write(target_dir, exc)
    per_template_count: dict[str, int] = {}
    written: list[Path] = []
    matched: set[tuple[str, str]] = set()
    for entry in entries:
        try:
            path = urlparse(entry.request.url).path or "/"
        except ValueError:
            # A URL urlparse cannot split: the digest dropped it as an error too.
            continue
        # The digest's own template, collapse included: --match takes the
        # template the digest printed, and a generated test looks for the file
        # named after it.
        template = endpoint_template(run_digest, path)
        method = entry.request.method.upper()
        hits = [e for e in endpoints if _matches_template(method, template, e)]
        if not hits:
            continue
        matched.update(hits)
        content_type = entry.response.content_type or "application/octet-stream"
        text = _capture_text(entry)
        if text is None:
            # A capture holds no text for a binary response, and writing
            # `body or ""` put a zero-byte file on disk that reads as a real
            # (empty) fixture.
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

        filename = capture_filename(method, template, content_type)
        if seen > 0:
            # "#" never occurs in a path (it starts the fragment), so a repeat
            # cannot take the name of a template with a numeric last segment.
            stem, _, ext = filename.rpartition(".")
            filename = f"{stem}#{seen}.{ext}"
        file_path = target_dir / filename
        try:
            file_path.write_text(text, encoding="utf-8")
            write_sidecar(
                file_path,
                status=entry.response.status,
                content_type=content_type,
                body_params=body_params(entry),
                flagged_names=flagged,
                redacted_names=redacted,
            )
        except OSError as exc:
            _refuse_write(file_path, exc)
        written.append(file_path)
        # soft_wrap: see digest_cmd. A path listing that breaks mid-word at 80
        # columns cannot be copied.
        console.print(f"[green]Wrote:[/green] {escape(str(file_path))}", soft_wrap=True)

    for pattern in endpoints:
        if pattern not in matched:
            method, template = pattern
            console.print(
                f"[yellow]No entries matched --match {escape(method)} {escape(template)}.[/yellow]",
                soft_wrap=True,
                highlight=False,
            )


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
