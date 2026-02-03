"""CLI command for importing HAR files and generating plugins.

This command analyzes HAR files captured from browser developer tools
and generates graftpunk plugins from them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from graftpunk.config import get_settings
from graftpunk.har import (
    APIEndpoint,
    AuthFlow,
    HARParseResult,
    detect_auth_flow,
    discover_api_endpoints,
    extract_domain,
    parse_har_file,
)
from graftpunk.har.generator import generate_plugin_code, generate_yaml_plugin
from graftpunk.har.parser import HARParseError
from graftpunk.logging import get_logger
from graftpunk.plugins import infer_site_name

LOG = get_logger(__name__)
console = Console()


def import_har(
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
    # Validate format_type
    valid_formats = {"python", "yaml"}
    if format_type not in valid_formats:
        console.print(f"[red]Invalid format '{format_type}'. Valid formats: python, yaml[/red]")
        raise typer.Exit(1)

    # Parse HAR file
    console.print(f"[dim]Parsing HAR file:[/dim] {har_file}")

    try:
        result: HARParseResult = parse_har_file(har_file)
    except HARParseError as exc:
        console.print(f"[red]Failed to parse HAR file: {exc}[/red]")
        raise typer.Exit(1) from None
    except FileNotFoundError:
        console.print(f"[red]File not found: {har_file}[/red]")
        raise typer.Exit(1) from None

    entries = result.entries

    # Warn user about parse errors
    if result.has_errors:
        console.print(f"[yellow]Warning: {len(result.errors)} entries failed to parse[/yellow]")
        # Show first few errors for context
        for error in result.errors[:3]:
            console.print(f"  [dim]Entry {error.index}: {error.error}[/dim]")
        if len(result.errors) > 3:
            console.print(f"  [dim]... and {len(result.errors) - 3} more[/dim]")
        console.print()

    if not entries:
        console.print("[yellow]No HTTP entries found in HAR file[/yellow]")
        raise typer.Exit(1) from None

    console.print(f"[dim]Found {len(entries)} HTTP requests[/dim]\n")

    # Extract domain
    domain = extract_domain(entries)
    if not domain:
        console.print("[red]Could not determine domain from HAR file[/red]")
        raise typer.Exit(1) from None

    # Use provided name or infer from domain
    site_name = name or infer_site_name(domain)
    console.print(f"[bold]Site:[/bold] {site_name} ({domain})\n")

    # Detect auth flow
    console.print("[dim]Analyzing authentication flow...[/dim]")
    auth_flow = detect_auth_flow(entries)

    if auth_flow:
        console.print(
            Panel(
                _format_auth_flow(auth_flow),
                title="[green]Auth Flow Detected[/green]",
                border_style="green",
            )
        )
    else:
        console.print("[dim]No authentication flow detected[/dim]\n")

    # Discover API endpoints
    endpoints = []
    if discover_api:
        console.print("[dim]Discovering API endpoints...[/dim]")
        endpoints = discover_api_endpoints(entries, domain)

        if endpoints:
            _print_endpoints_table(endpoints)
        else:
            console.print("[dim]No API endpoints discovered[/dim]\n")

    # Generate plugin code
    if format_type == "yaml":
        plugin_code = generate_yaml_plugin(site_name, domain, auth_flow, endpoints)
        extension = ".yaml"
    else:
        plugin_code = generate_plugin_code(site_name, domain, auth_flow, endpoints)
        extension = ".py"

    # Determine output path
    if output:
        output_path = output
    else:
        settings = get_settings()
        plugins_dir = settings.config_dir / "plugins"
        output_path = plugins_dir / f"{site_name}{extension}"

    # Dry run - just show the code
    if dry_run:
        console.print(
            Panel(
                plugin_code,
                title=f"[cyan]Generated Plugin ({format_type})[/cyan]",
                border_style="cyan",
            )
        )
        console.print(f"\n[dim]Would write to: {output_path}[/dim]")
        return

    # Write the file
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(plugin_code)
        console.print(f"\n[green]Generated plugin:[/green] {output_path}")

        # Show next steps
        console.print("\n[bold]Next steps:[/bold]")
        console.print(f"  1. Review and edit: [cyan]{output_path}[/cyan]")
        if format_type == "python":
            console.print("  2. Register as entry point or move to plugins directory")
        console.print(f"  3. Test: [cyan]gp {site_name} --help[/cyan]")

    except OSError as exc:
        console.print(f"[red]Failed to write plugin: {exc}[/red]")
        raise typer.Exit(1) from None


def _format_auth_flow(auth_flow: AuthFlow) -> str:
    """Format auth flow for display."""

    if not isinstance(auth_flow, AuthFlow):
        return ""

    lines = []
    for i, step in enumerate(auth_flow.steps, 1):
        step_icon = {
            "form_page": "📄",
            "login_submit": "🔐",
            "redirect": "↪",
            "authenticated": "✓",
            "oauth": "🔑",
        }.get(step.step_type, "•")

        lines.append(f"  {i}. {step_icon} {step.description}")

    if auth_flow.session_cookies:
        cookies = ", ".join(auth_flow.session_cookies[:5])
        if len(auth_flow.session_cookies) > 5:
            cookies += f" (+{len(auth_flow.session_cookies) - 5} more)"
        lines.append(f"\n[dim]Session cookies:[/dim] {cookies}")

    lines.append(f"[dim]Auth type:[/dim] {auth_flow.auth_type}")

    return "\n".join(lines)


def _print_endpoints_table(endpoints: list[APIEndpoint]) -> None:
    """Print API endpoints in a table."""
    table = Table(
        title=f"API Endpoints ({len(endpoints)} found)",
        title_style="bold",
        header_style="bold cyan",
        border_style="dim",
    )
    table.add_column("Method", style="cyan", no_wrap=True, width=8)
    table.add_column("Path", style="white")
    table.add_column("Params", style="dim")

    # Show first 15 endpoints
    for endpoint in endpoints[:15]:
        params = ", ".join(endpoint.params) if endpoint.params else "—"
        table.add_row(endpoint.method, endpoint.path, params)

    if len(endpoints) > 15:
        table.add_row("...", f"(+{len(endpoints) - 15} more)", "")

    console.print(table)
    console.print()
