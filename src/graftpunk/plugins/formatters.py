"""Output formatters for CLI plugin responses."""

import json
from typing import Any

from rich.console import Console
from rich.json import JSON
from rich.table import Table


def format_output(data: Any, format_type: str, console: Console) -> None:
    """Format and print command output.

    Args:
        data: Response data to format.
        format_type: One of "json", "table", "raw".
        console: Rich console for output.
    """
    if format_type == "json":
        _format_json(data, console)
    elif format_type == "table":
        _format_table(data, console)
    else:  # raw
        _format_raw(data, console)


def _format_json(data: Any, console: Console) -> None:
    """Output as formatted JSON with syntax highlighting."""
    json_str = json.dumps(data, indent=2, default=str)
    console.print(JSON(json_str))


def _format_table(data: Any, console: Console) -> None:
    """Output as a rich table (for lists of dicts or single dicts)."""
    if isinstance(data, list) and data and isinstance(data[0], dict):
        # List of dicts - display as table with columns
        headers = list(data[0].keys())

        table = Table(header_style="bold cyan", border_style="dim")
        for header in headers:
            table.add_column(header)

        for row in data:
            table.add_row(*[str(row.get(h, "")) for h in headers])

        console.print(table)
    elif isinstance(data, dict):
        # Single dict - display as key/value pairs
        table = Table(show_header=False, border_style="dim")
        table.add_column("Key", style="cyan")
        table.add_column("Value")

        for key, value in data.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, default=str)
            table.add_row(str(key), str(value))

        console.print(table)
    else:
        # Fallback to JSON for other types
        _format_json(data, console)


def _format_raw(data: Any, console: Console) -> None:
    """Output raw string representation."""
    if isinstance(data, str):
        console.print(data)
    else:
        console.print(json.dumps(data, default=str))
