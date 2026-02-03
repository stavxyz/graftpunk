"""Output formatters for CLI plugin responses.

Provides a protocol-based formatter system with entry-point discovery,
allowing third-party packages to register custom output formatters via
the ``graftpunk.formatters`` entry-point group.
"""

import importlib.metadata
import json
from typing import Any, Protocol, runtime_checkable

from rich.console import Console
from rich.json import JSON
from rich.table import Table

from graftpunk.logging import get_logger
from graftpunk.plugins.cli_plugin import CommandResult

LOG = get_logger(__name__)


@runtime_checkable
class OutputFormatter(Protocol):
    """Protocol for custom output formatters.

    Implementations must expose a ``name`` property (used as the ``--format``
    flag value) and a ``format`` method that renders data to a Rich console.
    """

    @property
    def name(self) -> str:
        """Formatter name used in --format flag."""
        ...

    def format(self, data: Any, console: Console) -> None:
        """Format and print data to the console."""
        ...


# ---------------------------------------------------------------------------
# Built-in formatters
# ---------------------------------------------------------------------------


class JsonFormatter:
    """Output as formatted JSON with syntax highlighting."""

    name = "json"

    def format(self, data: Any, console: Console) -> None:
        json_str = json.dumps(data, indent=2, default=str)
        console.print(JSON(json_str))


class TableFormatter:
    """Output as a rich table (for lists of dicts or single dicts)."""

    name = "table"

    def format(self, data: Any, console: Console) -> None:
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
            JsonFormatter().format(data, console)


class RawFormatter:
    """Output raw string representation."""

    name = "raw"

    def format(self, data: Any, console: Console) -> None:
        if isinstance(data, str):
            console.print(data)
        else:
            console.print(json.dumps(data, default=str))


BUILTIN_FORMATTERS: dict[str, OutputFormatter] = {
    "json": JsonFormatter(),
    "table": TableFormatter(),
    "raw": RawFormatter(),
}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

FORMATTERS_GROUP = "graftpunk.formatters"


def discover_formatters() -> dict[str, OutputFormatter]:
    """Discover all formatters (built-in + entry points).

    Returns:
        Dictionary mapping formatter name to formatter instance.
        Built-in formatters are always present; entry-point formatters
        are merged on top (and may override built-ins).
    """
    formatters: dict[str, OutputFormatter] = dict(BUILTIN_FORMATTERS)
    for ep in importlib.metadata.entry_points(group=FORMATTERS_GROUP):
        try:
            cls = ep.load()
            instance = cls()
            if not isinstance(instance, OutputFormatter):
                LOG.warning(
                    "formatter_protocol_mismatch",
                    entry_point=ep.name,
                    type=type(instance).__name__,
                )
                continue
            formatters[instance.name] = instance
        except Exception:  # noqa: BLE001
            LOG.warning("formatter_load_failed", entry_point=ep.name)
    return formatters


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def format_output(data: Any, format_type: str, console: Console) -> None:
    """Format and print command output.

    Args:
        data: Response data to format. May be a CommandResult (unwrapped
            automatically) or raw data.
        format_type: Formatter name (e.g. ``"json"``, ``"table"``, ``"raw"``).
        console: Rich console for output.
    """
    formatters = discover_formatters()
    formatter = formatters.get(format_type)
    if formatter is None:
        LOG.warning("unknown_format", format=format_type)
        formatter = formatters["json"]  # fallback

    # Unwrap CommandResult
    if isinstance(data, CommandResult):
        if data.format_hint and data.format_hint in formatters and format_type == "json":
            # When format_type is the default ("json"), the plugin's format_hint
            # overrides it. Note: we cannot distinguish "user explicitly passed
            # --format json" from "default json" — in both cases the hint wins.
            formatter = formatters[data.format_hint]
        data = data.data

    formatter.format(data, console)
