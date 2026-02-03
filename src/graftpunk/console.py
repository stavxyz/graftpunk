"""Centralized terminal output for graftpunk.

All user-facing output should go through this module. Plugins should prefer
these helpers over importing Rich directly.

Key principle: stderr for status/progress, stdout for data.
"""

from __future__ import annotations

from rich.console import Console

# stderr console for status messages (spinners, success/error)
err_console = Console(stderr=True)

# stdout console for data output (JSON, tables)
out_console = Console()


def success(message: str, *, console: Console | None = None) -> None:
    """Print a success message (green checkmark) to stderr."""
    c = console or err_console
    c.print(f"[green]  \u2713 {message}[/green]")


def error(message: str, *, console: Console | None = None) -> None:
    """Print an error message (red X) to stderr."""
    c = console or err_console
    c.print(f"[red]  \u2717 {message}[/red]")


def warn(message: str, *, console: Console | None = None) -> None:
    """Print a warning message (yellow) to stderr."""
    c = console or err_console
    c.print(f"[yellow]  \u26a0 {message}[/yellow]")


def info(message: str, *, console: Console | None = None) -> None:
    """Print an info message (dim) to stderr."""
    c = console or err_console
    c.print(f"[dim]  {message}[/dim]")
