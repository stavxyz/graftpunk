"""Output formatters for CLI plugin responses.

Provides a protocol-based formatter system with entry-point discovery,
allowing third-party packages to register custom output formatters via
the ``graftpunk.formatters`` entry-point group.
"""

import csv
import datetime
import importlib.metadata
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from rich.console import Console
from rich.json import JSON
from rich.rule import Rule
from rich.table import Table

from graftpunk import console as gp_console
from graftpunk.logging import get_logger
from graftpunk.plugins.cli_plugin import CommandResult
from graftpunk.plugins.export import get_downloads_dir, ordered_keys
from graftpunk.plugins.output_config import (
    OutputConfig,
    ViewConfig,
    apply_column_filter,
    extract_view_data,
    parse_view_arg,
)

LOG = get_logger(__name__)


@runtime_checkable
class OutputFormatter(Protocol):
    """Protocol for custom output formatters.

    Implementations must expose a ``name`` attribute (used as the ``--format``
    flag value) and a ``format`` method that renders data to a Rich console.
    """

    @property
    def name(self) -> str:
        """Formatter name used in --format flag."""
        ...

    def format(
        self,
        data: Any,
        console: Console,
        output_config: OutputConfig | None = None,
        output_path: str = "",
    ) -> None:
        """Format and print data to the console.

        Args:
            data: Response data to format.
            console: Rich console for output.
            output_config: Optional view/column configuration.
            output_path: If non-empty, write output to this file path
                instead of the console. Text formatters create the file
                and write text; file formatters (xlsx, pdf) use this as
                the destination path.
        """
        ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_view_data(data: Any, view: ViewConfig) -> Any | None:
    """Extract and filter data for a single view.

    Applies path extraction via ``extract_view_data`` and column filtering
    via ``apply_column_filter`` in one step.

    Returns:
        The resolved data, or ``None`` if path extraction yields nothing.
    """
    view_data = extract_view_data(data, view.path) if view.path else data
    if view_data is None:
        return None
    if isinstance(view_data, list) and view_data and isinstance(view_data[0], dict):
        view_data = apply_column_filter(view_data, view.columns)
    elif isinstance(view_data, dict) and view.columns:
        view_data = apply_column_filter([view_data], view.columns)[0]
    return view_data


def _write_to_file(
    output_path: str,
    render_fn: Callable[[Console], None],
) -> None:
    """Redirect console-based rendering to a file.

    Creates parent directories, renders into a StringIO-backed Console,
    and writes the captured text to *output_path*.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    file_console = Console(file=buf, width=200)
    render_fn(file_console)
    Path(output_path).write_text(buf.getvalue())


def _resolve_output_filepath(output_path: str, extension: str) -> Path:
    """Resolve the output file path for file-based formatters.

    If *output_path* is non-empty, uses it (creating parent dirs).
    Otherwise auto-generates a timestamped filename in the downloads dir.
    """
    if output_path:
        filepath = Path(output_path)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        return filepath
    downloads_dir = get_downloads_dir()
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return downloads_dir / f"output-{timestamp}.{extension}"


# ---------------------------------------------------------------------------
# Built-in formatters
# ---------------------------------------------------------------------------


class JsonFormatter:
    """Output as formatted JSON with syntax highlighting."""

    name = "json"

    def format(
        self,
        data: Any,
        console: Console,
        output_config: OutputConfig | None = None,
        output_path: str = "",
    ) -> None:
        json_str = json.dumps(data, indent=2, default=str)
        if output_path:
            _write_to_file(output_path, lambda c: c.print(JSON(json_str)))
            return
        console.print(JSON(json_str))


class TableFormatter:
    """Output as a rich table (for lists of dicts or single dicts)."""

    name = "table"

    def format(
        self,
        data: Any,
        console: Console,
        output_config: OutputConfig | None = None,
        output_path: str = "",
    ) -> None:
        """Format data as a rich table.

        When output_config has views, delegates to _render_views for
        multi-view rendering. Otherwise renders a single auto-detected table.
        """

        def render(c: Console) -> None:
            if output_config and output_config.views:
                self._render_views(data, c, output_config)
            else:
                self._render_data(data, c)

        if output_path:
            _write_to_file(output_path, render)
            return
        render(console)

    def _render_views(
        self,
        data: Any,
        console: Console,
        output_config: OutputConfig,
    ) -> None:
        """Render one or more views from OutputConfig.

        Args:
            data: The full response data to extract views from.
            console: Rich console for output.
            output_config: Configuration containing view definitions.
        """
        views = output_config.views
        multi = len(views) > 1
        first = True

        for view in views:
            view_data = _resolve_view_data(data, view)
            if view_data is None:
                LOG.debug("multi_view_empty", view=view.name, path=view.path)
                continue
            if multi:
                if not first:
                    console.print("")
                console.print(Rule(title=view.title or view.name))
            self._render_data(view_data, console)
            first = False

        if first:
            LOG.warning("multi_view_all_empty", view_count=len(views))

    def _render_data(self, data: Any, console: Console) -> None:
        """Render a single data object as a table.

        Args:
            data: Data to render (list of dicts, dict, or other).
            console: Rich console for output.
        """
        if isinstance(data, list) and data and isinstance(data[0], dict):
            headers = list(data[0].keys())
            table = Table(header_style="bold cyan", border_style="dim")
            for header in headers:
                table.add_column(header)
            for row in data:
                table.add_row(*[str(row.get(h, "")) for h in headers])
            console.print(table)
        elif isinstance(data, dict):
            table = Table(show_header=False, border_style="dim")
            table.add_column("Key", style="cyan")
            table.add_column("Value")
            for key, value in data.items():
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, default=str)
                table.add_row(str(key), str(value))
            console.print(table)
        else:
            JsonFormatter().format(data, console)


class RawFormatter:
    """Output raw string representation."""

    name = "raw"

    def format(
        self,
        data: Any,
        console: Console,
        output_config: OutputConfig | None = None,
        output_path: str = "",
    ) -> None:
        def render(c: Console) -> None:
            if isinstance(data, str):
                c.print(data)
            else:
                c.print(json.dumps(data, default=str))

        if output_path:
            _write_to_file(output_path, render)
            return
        render(console)


class CsvFormatter:
    """Output as CSV (comma-separated values)."""

    name = "csv"

    def format(
        self,
        data: Any,
        console: Console,
        output_config: OutputConfig | None = None,
        output_path: str = "",
    ) -> None:
        if isinstance(data, str):
            LOG.debug("csv_format_string_passthrough", length=len(data))
            RawFormatter().format(data, console, output_path=output_path)
            return

        # Warn if multiple views exist — CSV can only render one
        if output_config and len(output_config.views) > 1:
            view_names = [v.name for v in output_config.views]
            default = output_config.get_default_view()
            default_name = default.name if default else view_names[0]
            gp_console.warn(
                f"Multiple views available ({', '.join(view_names)}). "
                f"Use --view to select. Showing default view: {default_name}"
            )

        # Apply output config path extraction BEFORE type conversion
        if output_config:
            view = output_config.get_default_view()
            if view and view.path:
                extracted = extract_view_data(data, view.path)
                if extracted is not None:
                    data = extracted

        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            LOG.warning(
                "csv_format_unsupported_type",
                data_type=type(data).__name__,
                fallback="raw",
            )
            RawFormatter().format(data, console, output_path=output_path)
            return
        if not data:
            LOG.debug("csv_format_empty_list")
            return
        if not all(isinstance(item, dict) for item in data):
            LOG.warning(
                "csv_format_unsupported_type",
                data_type="list[mixed]",
                fallback="raw",
            )
            RawFormatter().format(data, console, output_path=output_path)
            return

        # Apply column filter from output config
        if output_config:
            view = output_config.get_default_view()
            if view and view.columns:
                data = apply_column_filter(data, view.columns)

        if not data:
            LOG.debug("csv_format_empty_after_config")
            return

        headers = ordered_keys(data)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        for row in data:
            writer.writerow(
                [
                    json.dumps(v, default=str) if isinstance(v, (dict, list)) else str(v)
                    for v in (row.get(h, "") for h in headers)
                ]
            )
        if output_path:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_text(buf.getvalue())
            gp_console.info(f"Saved: {output_path}")
            return
        console.print(buf.getvalue(), end="")


class XlsxFormatter:
    """Output as an Excel XLSX file with one worksheet per view."""

    name = "xlsx"

    def format(
        self,
        data: Any,
        console: Console,
        output_config: OutputConfig | None = None,
        output_path: str = "",
    ) -> None:
        """Format data as an Excel XLSX file.

        Creates a ``.xlsx`` workbook in the downloads directory (or at
        *output_path* if provided). When *output_config* contains views,
        each view becomes a separate worksheet; otherwise a single
        ``Sheet1`` is written.

        Args:
            data: Response data to format.
            console: Rich console (unused — output goes to a file).
            output_config: Optional view configuration.
            output_path: If non-empty, use this path instead of
                auto-generating one in the downloads directory.
        """
        import xlsxwriter  # type: ignore[unresolved-import]

        filepath = _resolve_output_filepath(output_path, "xlsx")

        workbook = xlsxwriter.Workbook(str(filepath))
        try:
            bold = workbook.add_format({"bold": True})

            if output_config and output_config.views:
                for view in output_config.views:
                    view_data = _resolve_view_data(data, view)
                    if view_data is None:
                        LOG.debug("xlsx_view_empty", view=view.name, path=view.path)
                        continue
                    # Excel worksheet names are limited to 31 characters
                    sheet_name = (view.title or view.name)[:31]
                    self._write_sheet(workbook, sheet_name, view_data, bold)
            else:
                self._write_sheet(workbook, "Sheet1", data, bold)
        finally:
            workbook.close()
        gp_console.info(f"Saved: {filepath}")

    def _write_sheet(
        self,
        workbook: Any,
        sheet_name: str,
        data: Any,
        bold: Any,
    ) -> None:
        """Write data to a named worksheet.

        Args:
            workbook: An open xlsxwriter Workbook.
            sheet_name: Name for the worksheet tab.
            data: Data to write (list of dicts, dict, or scalar).
            bold: A bold cell format for header rows.
        """
        worksheet = workbook.add_worksheet(sheet_name)

        if isinstance(data, list) and data and isinstance(data[0], dict):
            headers = list(data[0].keys())
            for col, header in enumerate(headers):
                worksheet.write(0, col, header, bold)
            for row_idx, row in enumerate(data, start=1):
                for col_idx, header in enumerate(headers):
                    value = row.get(header, "")
                    if isinstance(value, (dict, list)):
                        value = json.dumps(value, default=str)
                    worksheet.write(row_idx, col_idx, value)
            # Auto-size columns: sample first 100 rows for performance,
            # cap cell width at 50 chars to prevent very wide columns.
            for col_idx, header in enumerate(headers):
                max_len = len(header)
                for row in data[:100]:
                    val = str(row.get(header, ""))
                    max_len = max(max_len, min(len(val), 50))
                worksheet.set_column(col_idx, col_idx, max_len + 2)
        elif isinstance(data, dict):
            worksheet.write(0, 0, "Key", bold)
            worksheet.write(0, 1, "Value", bold)
            for row_idx, (key, value) in enumerate(data.items(), start=1):
                worksheet.write(row_idx, 0, str(key))
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, default=str)
                worksheet.write(row_idx, 1, value)
            worksheet.set_column(0, 0, 20)
            worksheet.set_column(1, 1, 40)
        elif isinstance(data, list):
            pass  # Empty list — just create the sheet
        else:
            worksheet.write(0, 0, str(data))


class PdfFormatter:
    """Output as a PDF file with table layout."""

    name = "pdf"

    def format(
        self,
        data: Any,
        console: Console,
        output_config: OutputConfig | None = None,
        output_path: str = "",
    ) -> None:
        """Format data as a PDF file with a table layout.

        Creates a ``.pdf`` file in the downloads directory (or at
        *output_path* if provided) using ``json_to_pdf`` from the
        export module. When *output_config* contains views, the
        default view is applied before rendering.

        Args:
            data: Response data to format.
            console: Rich console (used only when falling back to JSON
                for non-tabular data; PDF output goes to a file).
            output_config: Optional view configuration.
            output_path: If non-empty, use this path instead of
                auto-generating one in the downloads directory.
        """
        from graftpunk.plugins.export import json_to_pdf

        # Apply view data extraction (use default view if available)
        if output_config and output_config.views:
            view = output_config.get_default_view()
            if view:
                view_data = _resolve_view_data(data, view)
                if view_data is not None:
                    data = view_data

        # Normalize data to list of dicts for json_to_pdf
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            # Can't render as PDF table -- fall back to JSON
            JsonFormatter().format(data, console, output_path=output_path)
            return

        filepath = _resolve_output_filepath(output_path, "pdf")

        json_to_pdf(data, filepath)
        gp_console.info(f"Saved: {filepath}")


BUILTIN_FORMATTERS: dict[str, OutputFormatter] = {
    "json": JsonFormatter(),
    "table": TableFormatter(),
    "raw": RawFormatter(),
    "csv": CsvFormatter(),
    "xlsx": XlsxFormatter(),
    "pdf": PdfFormatter(),
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
            LOG.warning("formatter_load_failed", entry_point=ep.name, exc_info=True)
    return formatters


def format_output(
    data: Any,
    format_type: str,
    console: Console,
    *,
    user_explicit: bool = False,
    view_args: tuple[str, ...] = (),
    output_path: str = "",
    plugin_formatters: dict[str, OutputFormatter] | None = None,
) -> None:
    """Format and print command output.

    Formatter resolution follows a three-level override hierarchy:

    1. **Per-command** (highest priority): ``CommandResult.format_overrides``
    2. **Plugin-wide**: ``plugin_formatters`` parameter (from
       ``SitePlugin.format_overrides``)
    3. **Core** (lowest priority): built-in + entry-point formatters
       discovered by :func:`discover_formatters`

    Args:
        data: Response data to format. May be a CommandResult (unwrapped
            automatically) or raw data.
        format_type: Formatter name (e.g. ``"json"``, ``"table"``, ``"raw"``).
        console: Rich console for output.
        user_explicit: True when the user explicitly passed ``--format``/``-f``
            on the command line. When True, ``format_hint`` on a
            ``CommandResult`` is ignored so the user's choice wins.
        view_args: Tuple of ``--view`` CLI values. Each element is either
            a view name (``"items"``) or ``"name:col1,col2,..."`` to
            restrict columns. Empty tuple means show all views.
        output_path: If non-empty, write output to this file path instead
            of the console. Passed through to the formatter.
        plugin_formatters: Plugin-wide formatter overrides. Keys are format
            names, values are OutputFormatter instances. These override core
            formatters but are themselves overridden by per-command overrides
            on CommandResult.
    """
    # Level 3: core formatters (built-in + entry points)
    formatters = discover_formatters()

    # Level 2: plugin-wide overrides
    if plugin_formatters:
        formatters.update(plugin_formatters)

    output_config = None
    effective_format = format_type

    # Unwrap CommandResult
    if isinstance(data, CommandResult):
        output_config = data.output_config  # Extract config

        # Level 1: per-command overrides (highest priority)
        if data.format_overrides:
            formatters.update(data.format_overrides)

        if not user_explicit and data.format_hint:
            if data.format_hint in formatters:
                effective_format = data.format_hint
            else:
                LOG.warning(
                    "unknown_format_hint",
                    hint=data.format_hint,
                    available=sorted(formatters.keys()),
                )
        data = data.data

    # Resolve the formatter for the effective format
    formatter = formatters.get(effective_format)
    if formatter is None:
        available = ", ".join(sorted(formatters.keys()))
        raise ValueError(f"Unknown output format {effective_format!r}. Available: {available}")

    # Apply --view filtering
    if view_args:
        if output_config is None:
            gp_console.warn("--view has no effect: this command does not define views.")
        else:
            names: list[str] = []
            column_overrides: dict[str, list[str]] = {}
            for arg in view_args:
                name, cols = parse_view_arg(arg)
                names.append(name)
                if cols:
                    column_overrides[name] = cols
            output_config = output_config.filter_views(names, column_overrides)

    formatter.format(data, console, output_config=output_config, output_path=output_path)
