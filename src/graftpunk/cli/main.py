"""graftpunk CLI.

Manage encrypted browser sessions from the terminal.
"""

import enum
import os
from collections.abc import Mapping
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

import graftpunk

# Workstation env bootstrap MUST precede plugin registration below (which
# constructs the settings singleton) so file statics are visible to it.
# It's sandwiched between two configure_logging() calls below: the first
# (pre-bootstrap) ensures any warning bootstrap itself emits goes to stderr,
# the second (post-bootstrap) picks up file statics for
# GRAFTPUNK_LOG_LEVEL/GRAFTPUNK_LOG_FORMAT. get_settings() also calls
# ensure_bootstrap() — idempotent, order-owned there.
from graftpunk import signals, workstation_env
from graftpunk.cli.config_commands import config_app
from graftpunk.cli.http_commands import http_app
from graftpunk.cli.keepalive_commands import keepalive_app
from graftpunk.cli.observe_commands import observe_app
from graftpunk.cli.session_commands import session_app
from graftpunk.config import get_settings
from graftpunk.console import err_console
from graftpunk.logging import (
    configure_logging,
    configured_by_consumer,
    enable_network_debug,
    get_logger,
)
from graftpunk.plugins import (
    discover_keepalive_handlers,
    discover_site_plugins,
    discover_storage_backends,
)
from graftpunk.plugins.yaml_plugin import create_yaml_plugins

# Configure logging BEFORE workstation-env bootstrap so any warning
# ensure_bootstrap() emits (permissive file mode, malformed line, mixed
# substitution) routes through structlog's configured stderr renderer
# instead of structlog's default PrintLoggerFactory (stdout) -- which would
# otherwise corrupt piped `gp` output for CSV/JSON consumers. We avoid
# calling get_settings() here because GraftpunkSettings.__init__ creates
# directories as a side effect, which breaks test isolation; env vars are
# read directly instead.
#
# A host program that embeds this Typer app and configured structlog itself
# keeps its configuration: only the -v/--log-format flags in main_callback()
# (an explicit CLI invocation) reconfigure over it.


def _configure_cli_logging_from_env() -> None:
    if configured_by_consumer():
        return
    configure_logging(
        level=os.environ.get("GRAFTPUNK_LOG_LEVEL", "WARNING"),
        json_output=os.environ.get("GRAFTPUNK_LOG_FORMAT", "console") == "json",
    )


_configure_cli_logging_from_env()

workstation_env.ensure_bootstrap()

# Re-configure logging: ensure_bootstrap() may have just injected file-static
# GRAFTPUNK_LOG_LEVEL/GRAFTPUNK_LOG_FORMAT into os.environ (only when the
# real env didn't already set them), so re-read them now. configure_logging()
# is idempotent, so this is a no-op unless the file actually changed one of
# those two vars.
# The -v/-vv and --log-format flags in main_callback() may reconfigure later.
_configure_cli_logging_from_env()

LOG = get_logger(__name__)

app = typer.Typer(
    name="graftpunk",
    help=f"""
    🔌 graftpunk — {escape(graftpunk.DESCRIPTION)}

    {escape(graftpunk.LONG_DESCRIPTION)}

    \b
    Quick start:
      gp session list          Show all cached sessions
      gp session show <name>   View session details
      gp session clear <name>  Remove a session
      gp config                Show config / manage the workstation env file

    \b
    Documentation: https://github.com/stavxyz/graftpunk
    """,
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)
console = Console()


class ObserveMode(enum.StrEnum):
    off = "off"
    full = "full"


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
        ObserveMode,
        typer.Option(
            "--observe",
            help="Observability capture mode",
        ),
    ] = ObserveMode.off,
) -> None:
    """graftpunk CLI entry point; the help text lives on the ``typer.Typer(help=...)`` above."""
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

    # Arm the termination handlers for the first browser this command opens.
    # Setting a flag claims no signal slot, so a command that never starts a
    # browser leaves the process's dispositions exactly as it found them (#96).
    signals.auto_install = True

    ctx.ensure_object(dict)["observe_mode"] = observe.value


@app.command("version")
def version() -> None:
    """Show graftpunk version and installation info."""
    settings = get_settings()
    console.print(
        Panel(
            f"[bold cyan]graftpunk[/bold cyan] v{graftpunk.__version__}\n"
            f"{escape(graftpunk.DESCRIPTION)}\n\n"
            f"[dim]Config:[/dim]  {escape(str(settings.config_dir))}\n"
            f"[dim]Storage:[/dim] {escape(str(settings.storage_backend))}",
            title="graftpunk",
            border_style="cyan",
        )
    )


# Observe subcommand group (defined in observe_commands.py)
app.add_typer(observe_app)

# Session subcommand group (defined in session_commands.py)
app.add_typer(session_app)

# Keepalive subcommand group (defined in keepalive_commands.py)
app.add_typer(keepalive_app)

# HTTP subcommand group (defined in http_commands.py)
app.add_typer(http_app)

# Config subcommand group (defined in config_commands.py)
app.add_typer(config_app)


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
            items = "\n".join(f"  [green]✓[/green] {escape(str(n))}" for n in sorted(names))
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


# Register plugin commands dynamically at module load time so they appear in --help.
# Plugin sub-apps (including the scaffold's plugin_app, below) are attached
# with app.add_typer() at import time.
_registered_plugins: dict[str, str] = {}
try:
    from graftpunk.cli.plugin_commands import register_plugin_commands
    from graftpunk.cli.scaffold_commands import register as register_scaffold_commands

    # Attaches plugin_app and snapshots the reserved top-level names from
    # *app* right before plugin discovery mounts any site plugin's own
    # sub-app, so the snapshot never includes an installed plugin's name.
    register_scaffold_commands(app)

    _registered_plugins = register_plugin_commands(app)
    if _registered_plugins:
        LOG.debug("plugins_registered", count=len(_registered_plugins))
except (SystemExit, KeyboardInterrupt):
    raise
except Exception as exc:
    LOG.exception("plugin_registration_failed", error=str(exc))
    # Notify user - plugins are optional but they should know if they fail
    # stderr: gp's stdout may be a piped JSON/CSV stream.
    err_console.print(f"[yellow]Warning: Plugin registration failed: {escape(str(exc))}[/yellow]")

if __name__ == "__main__":
    app()
