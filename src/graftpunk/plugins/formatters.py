"""Output formatters for CLI plugin responses.

Provides a protocol-based formatter system with entry-point discovery,
allowing third-party packages to register custom output formatters via
the ``graftpunk.formatters`` entry-point group.
"""

import csv
import datetime
import decimal
import importlib.metadata
import io
import math
import numbers
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from rich.cells import cell_len
from rich.console import Console
from rich.json import JSON
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from graftpunk import console as gp_console
from graftpunk._safe_text import safe_text
from graftpunk.console import stream_is_tty, visible_text, write_raw
from graftpunk.logging import get_logger
from graftpunk.plugins.cli_plugin import CommandResult
from graftpunk.plugins.export import (
    _neutralize_formula,
    get_downloads_dir,
    json_text,
    ordered_keys,
    table_rows,
)
from graftpunk.plugins.output_config import (
    OutputConfig,
    ViewConfig,
    apply_column_filter,
    extract_view_data,
    select_views,
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

    @property
    def binary(self) -> bool:
        """Whether this formatter produces binary output (e.g. xlsx, pdf)."""
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
    if isinstance(view_data, list) and view_data:
        # A list with no dict too: its items are the value column, which the
        # filter keeps or drops like any other (apply_column_filter).
        view_data = apply_column_filter(view_data, view.columns)
    elif isinstance(view_data, dict) and view.columns:
        view_data = apply_column_filter([view_data], view.columns)[0]
    return view_data


def write_rendered_to_file(
    output_path: str,
    render_fn: Callable[[Console], None],
) -> None:
    """Redirect Rich rendering (tables) to a file.

    Calls *render_fn* with a StringIO-backed, colour-less Console, 200 columns
    wide, and writes what it printed to *output_path* as UTF-8, creating
    parent directories. The file never carries ANSI escapes, even under
    FORCE_COLOR (#144). Only for output that is *meant* to be rendered; data
    payloads go through :func:`graftpunk.console.write_raw`.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    file_console = Console(
        file=buf, width=200, emoji=False, color_system=None, force_terminal=False
    )
    render_fn(file_console)
    Path(output_path).write_text(buf.getvalue(), encoding="utf-8")


# Plugins imported these private names before the public ones existed (#284).
_write_to_file = write_rendered_to_file
_stream_is_tty = stream_is_tty


def _warn_fallback(output_path: str, wanted: str, wrote: str) -> None:
    """Say when a file named for one format is about to hold another."""
    if output_path:
        gp_console.warn(f"{output_path}: the data cannot be written as {wanted}; wrote {wrote}.")


def _write_empty_csv(
    output_config: OutputConfig | None, console: Console, output_path: str
) -> None:
    """Truncate *output_path* for an empty result, so yesterday's file never survives.

    Writes the header row when the default view lists its columns, else an
    empty file. The console gets nothing, as before.
    """
    if not output_path:
        return
    view = output_config.get_default_view() if output_config else None
    text = ""
    if view and view.columns and view.columns.mode == "include":
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerow(
            [_neutralize_formula(safe_text(c)) for c in view.columns.columns]
        )
        text = buf.getvalue()
    write_raw(text, console, output_path)
    gp_console.info(f"Saved: {output_path}")


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
    """Output as JSON: syntax-highlighted on a terminal, plain text otherwise."""

    name = "json"
    binary = False

    def format(
        self,
        data: Any,
        console: Console,
        output_config: OutputConfig | None = None,
        output_path: str = "",
    ) -> None:
        json_str = json_text(data, indent=2)
        if output_path or not stream_is_tty(console):
            # A file or a pipe wants parseable JSON, not a wrapped, highlighted render.
            write_raw(json_str + "\n", console, output_path)
            return
        console.print(JSON(json_str), soft_wrap=True)  # never break a string across lines


class TableFormatter:
    """Output as a rich table (for lists of dicts or single dicts)."""

    name = "table"
    binary = False

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
            write_rendered_to_file(output_path, render)
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
        if isinstance(data, list) and any(isinstance(item, dict) for item in data):
            # Every row's keys, in first-seen order, and a non-dict row in the
            # value column, as the other formatters write a list.
            rows = table_rows(data)
            headers = ordered_keys(rows)
            table = Table(header_style="bold cyan", border_style="dim")
            # Cells are data: wrap in Text so Rich never markup-parses "[/LB]" (#145).
            for header in headers:
                table.add_column(Text(_table_text(header)))
            for row in rows:
                table.add_row(*[Text(_table_text(row.get(h, ""))) for h in headers])
            console.print(table)
        elif isinstance(data, dict):
            table = Table(show_header=False, border_style="dim")
            table.add_column("Key", style="cyan")
            table.add_column("Value")
            for key, value in data.items():
                if isinstance(value, (dict, list)):
                    value = json_text(value)
                table.add_row(Text(_table_text(key)), Text(_table_text(value)))
            console.print(table)
        else:
            JsonFormatter().format(data, console)


def _table_text(value: Any) -> str:
    """A table cell's or heading's text: :func:`safe_text`, so a value whose
    ``str`` raises, an int past the digit limit and a lone surrogate render,
    with the characters that hang Rich neutralised (:func:`_rich_safe`)."""
    return _rich_safe(safe_text(value))


_KEPT_CONTROLS = frozenset("\t\n")


def _rich_safe(text: str) -> str:
    """*text* without what makes Rich 14.3.1 loop forever while it wraps a
    table cell: a character Rich gives no width, at the start of the text or
    after whitespace, followed by a double-width one, cut to one cell
    (``Text("\\u200f\\u4e2d").truncate(1)`` had not returned after 20 s).

    Measured against plain Rich, in a subprocess with a timeout, for each
    format character (ZWSP, ZWNJ, LRM, RLM, the bidi embeddings, overrides
    and isolates, WJ, BOM, tags) and for combining marks: each hangs at the
    start of the text or after a space, and none hangs after a letter or
    inside a word, where it joins or follows a grapheme Rich can draw. So:

    - a character of no width is dropped only at the start of the text or
      after whitespace, where it has nothing to attach to; inside a word or
      an emoji sequence it is kept (ZWJ and ZWNJ, emoji tag characters, bidi
      marks, combining marks);
    - a ZWJ at the end of the text or before whitespace, with nothing to join,
      is dropped: Rich 14.3.1 raises ``StopIteration`` measuring it;
    - a control character other than tab and newline (Cc) is drawn as U+FFFD.

    Text without such characters is returned as it is.
    """
    if text.isascii() and text.isprintable():
        return text
    out: list[str] = []
    last = len(text) - 1
    for i, c in enumerate(text):
        if unicodedata.category(c) == "Cc" and c not in _KEPT_CONTROLS:
            out.append("\ufffd")
            continue
        if (not out or out[-1].isspace()) and _no_width(c):
            continue
        if c == _ZWJ and (i == last or text[i + 1].isspace()):
            # Rich 14.3.1's cell_len raises StopIteration on a ZWJ with nothing
            # after it to join ("a\u200d" and "a\u200d b" both raise).
            continue
        out.append(c)
    return "".join(out)


_ZWJ = "\u200d"


def _no_width(c: str) -> bool:
    """Whether Rich gives *c* no cell width (its own measure, as Rich lays it out)."""
    try:
        return cell_len(c) == 0
    except Exception:  # noqa: BLE001 - a character Rich cannot measure: treated as having none
        return True


class RawFormatter:
    """Output the payload verbatim (strings as-is, anything else as compact JSON)."""

    name = "raw"
    binary = False

    def format(
        self,
        data: Any,
        console: Console,
        output_config: OutputConfig | None = None,
        output_path: str = "",
    ) -> None:
        text = data if isinstance(data, str) else json_text(data)
        if not output_path and not text.endswith("\n"):
            text += "\n"  # a file gets the exact payload; a terminal gets a line
        write_raw(text, console, output_path)


def _csv_text(value: Any) -> str:
    """One CSV cell: a number as written, anything else neutralised (#288)."""
    if isinstance(value, numbers.Number):
        return safe_text(value)
    if isinstance(value, (dict, list)):
        return _neutralize_formula(json_text(value))
    return _neutralize_formula(safe_text(value))


class CsvFormatter:
    """Output as CSV (comma-separated values).

    A cell that is not a number and would start a spreadsheet formula gets a
    leading ``'`` (#288).
    """

    name = "csv"
    binary = False

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
            _warn_fallback(output_path, "CSV", "raw text")
            RawFormatter().format(data, console, output_path=output_path)
            return
        if not data:
            LOG.debug("csv_format_empty_list")
            _write_empty_csv(output_config, console, output_path)
            return
        # The default view's column filter, before the table check: it keeps
        # or drops the value column of a list with no dict like any other.
        view = output_config.get_default_view() if output_config else None
        if view and view.columns:
            data = apply_column_filter(data, view.columns)
        if not any(isinstance(item, dict) for item in data):
            # A list with no dict at all (strings, numbers) is not a table.
            LOG.warning(
                "csv_format_unsupported_type",
                data_type="list[non-dict]",
                fallback="raw",
            )
            _warn_fallback(output_path, "CSV", "raw text")
            RawFormatter().format(data, console, output_path=output_path)
            return
        data = table_rows(data)

        if not data:
            LOG.debug("csv_format_empty_after_config")
            return

        headers = ordered_keys(data)
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow([_neutralize_formula(safe_text(h)) for h in headers])
        for row in data:
            writer.writerow([_csv_text(row.get(h, "")) for h in headers])
        if output_path:
            write_raw(buf.getvalue(), console, output_path)
            gp_console.info(f"Saved: {output_path}")
            return
        write_raw(buf.getvalue(), console)


class XlsxFormatter:
    """Output as an Excel XLSX file with one worksheet per view."""

    name = "xlsx"
    binary = True

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

        # Strings go through _write_xlsx_cell; these options also keep any
        # plain write() from storing "=..." as a formula or a URL as a link (#288).
        workbook = xlsxwriter.Workbook(
            str(filepath), {"strings_to_formulas": False, "strings_to_urls": False}
        )
        try:
            bold = workbook.add_format({"bold": True})

            if output_config and output_config.views:
                used: set[str] = set()
                for view in output_config.views:
                    view_data = _resolve_view_data(data, view)
                    if view_data is None:
                        LOG.debug("xlsx_view_empty", view=view.name, path=view.path)
                        continue
                    sheet_name = _sheet_name(view.title or view.name, used)
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

        if isinstance(data, list) and data:
            # Every row's keys in first-seen order, and a non-dict row (a list
            # of strings too) in the value column, as the other formatters
            # write a list. A key need not be a string (1, None, a tuple): the
            # header shows its str(), and values are looked up by the key
            # itself (#310).
            rows = table_rows(data)
            headers = ordered_keys(rows)
            for col, header in enumerate(headers):
                _write_xlsx_cell(worksheet, 0, col, safe_text(header), bold)
            for row_idx, row in enumerate(rows, start=1):
                for col_idx, header in enumerate(headers):
                    _write_xlsx_cell(worksheet, row_idx, col_idx, row.get(header, ""))
            # Auto-size columns: sample first 100 rows for performance,
            # cap cell width at 50 chars to prevent very wide columns.
            for col_idx, header in enumerate(headers):
                max_len = len(safe_text(header))
                for row in rows[:100]:
                    val = safe_text(row.get(header, ""))
                    max_len = max(max_len, min(len(val), 50))
                worksheet.set_column(col_idx, col_idx, max_len + 2)
        elif isinstance(data, dict):
            worksheet.write(0, 0, "Key", bold)
            worksheet.write(0, 1, "Value", bold)
            for row_idx, (key, value) in enumerate(data.items(), start=1):
                _write_xlsx_cell(worksheet, row_idx, 0, safe_text(key))
                _write_xlsx_cell(worksheet, row_idx, 1, value)
            worksheet.set_column(0, 0, 20)
            worksheet.set_column(1, 1, 40)
        elif isinstance(data, list):
            pass  # Empty list — just create the sheet
        else:
            _write_xlsx_cell(worksheet, 0, 0, safe_text(data))


# The cell types xlsxwriter's write() stores natively; it raises TypeError on
# any other (a tuple, a set, an arbitrary object).
_XLSX_NATIVE = (
    str,
    bool,
    numbers.Real,
    decimal.Decimal,
    datetime.date,
    datetime.time,
    datetime.timedelta,
)


def _write_xlsx_cell(worksheet: Any, row: int, col: int, value: Any, fmt: Any = None) -> None:
    """Write *value*, a non-empty string always as a string cell (#288).

    ``strings_to_formulas=False`` alone still lets ``write()`` store
    ``{=...}`` as an array formula, so strings take ``write_string``. An empty
    string goes to ``write()``, which leaves the cell blank, where
    ``write_string`` would store an empty text cell.

    Every value is written; none raises (#310). A dict, list or tuple is
    written as its JSON (:func:`json_text`). A number Excel cannot hold (NaN,
    an infinity, ``Decimal("NaN")``, an int or Decimal too large for a float)
    is written as its ``str()`` (``nan``, ``inf``, ``NaN``, the digits). A
    datetime or time with a UTC offset is written as its ``isoformat()``, as
    Excel has no time zones. A string has each lone surrogate as U+FFFD. Any
    other value xlsxwriter cannot store is written as its ``str()``, as the CSV
    formatter writes it.
    """
    if isinstance(value, (dict, list, tuple)):
        value = json_text(value)
    elif isinstance(value, bool):
        pass
    elif isinstance(value, numbers.Real | decimal.Decimal):
        if not _fits_a_float(value):
            value = safe_text(value)
    elif isinstance(value, datetime.datetime | datetime.time):
        if value.utcoffset() is not None:
            value = value.isoformat()
    elif value is not None and not isinstance(value, _XLSX_NATIVE):
        value = safe_text(value)
    if isinstance(value, str):
        value = visible_text(value)
    try:
        if isinstance(value, str) and value:
            worksheet.write_string(row, col, value, fmt)
        else:
            worksheet.write(row, col, value, fmt)
    except (TypeError, ValueError, OverflowError):
        # A value of a native type xlsxwriter still refuses: its text.
        worksheet.write_string(row, col, safe_text(value), fmt)


# Characters Excel refuses in a worksheet name; xlsxwriter raised on each, and
# on a name used twice (two view titles alike in their first 31 characters).
_SHEET_NAME_REFUSED = str.maketrans(dict.fromkeys("[]:*?/\\", "_"))


def _sheet_name(title: str, used: set[str]) -> str:
    """*title* as a worksheet name Excel takes: refused characters as ``_``,
    cut to 31 characters and only then stripped of ``'`` at both ends (a cut
    could leave one at the end), never empty, and numbered when an earlier
    sheet has it (compared without case, as Excel does). Adds the name to
    *used*."""
    base = _sheet_base(visible_text(title).translate(_SHEET_NAME_REFUSED), 31)
    name, number = base, 1
    while name.lower() in used:
        number += 1
        suffix = f" ({number})"
        name = _sheet_base(base, 31 - len(suffix)) + suffix
    used.add(name.lower())
    return name


def _sheet_base(text: str, limit: int) -> str:
    """*text* cut to *limit* characters, then stripped of ``'`` at both ends;
    ``Sheet`` when nothing is left."""
    return text[:limit].strip("'") or "Sheet"


def _fits_a_float(value: numbers.Real | decimal.Decimal) -> bool:
    """Whether Excel can hold *value*: a finite float, once converted."""
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError, TypeError):
        return False


class PdfFormatter:
    """Output as a PDF file with table layout."""

    name = "pdf"
    binary = True

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
            _warn_fallback(output_path, "a PDF table", "JSON")
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


# The -o suffixes that pick a formatter when neither -f nor a plugin's own
# format_hint chose one (#284). Any other suffix keeps the CLI default.
_SUFFIX_FORMATS: dict[str, str] = {
    ".json": "json",
    ".csv": "csv",
    ".xlsx": "xlsx",
    ".pdf": "pdf",
}


def _output_suffix(output_path: str) -> str:
    """The lower-cased suffix of *output_path*, counting a dotfile name such as ``.csv``.

    ``Path(".csv").suffix`` is empty, so the name is split by hand.
    """
    name = Path(output_path).name.lower()
    _, dot, ext = name.rpartition(".")
    return f".{ext}" if dot else ""


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

    The format is chosen in this order (#284):

    1. An explicit ``--format``/``-f`` (*user_explicit*) always wins.
    2. A ``format_hint`` naming a formatter the plugin registered (in
       ``SitePlugin.format_overrides`` or ``CommandResult.format_overrides``,
       under any name, a built-in one included) or one installed through
       the ``graftpunk.formatters`` entry-point group under a name that is
       not built in applies everywhere: the terminal, a pipe and
       *output_path*, as through 1.23.0. The formatter receives
       *output_path* and *console* and decides from them.
    3. A ``format_hint`` naming one of the six built-in formatters
       (``json``, ``table``, ``raw``, ``csv``, ``xlsx`` or ``pdf``) that the
       plugin did not override applies only when there is no *output_path*
       and the console's stream is a terminal
       (:func:`graftpunk.console.stream_is_tty`).
    4. With *output_path*, a ``.json``, ``.csv``, ``.xlsx`` or ``.pdf``
       suffix (any case, and a bare ``.csv`` name too) picks that formatter.
    5. Otherwise *format_type*, the CLI default.

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
            on the command line. When True, neither ``format_hint`` on a
            ``CommandResult`` nor the *output_path* suffix is consulted, so
            the user's choice wins.
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
    hint_applied = False

    # Unwrap CommandResult
    if isinstance(data, CommandResult):
        output_config = data.output_config  # Extract config

        # Level 1: per-command overrides (highest priority)
        if data.format_overrides:
            formatters.update(data.format_overrides)

        hint = data.format_hint
        if not user_explicit and hint:
            # Decided by registration, so installing a package whose entry
            # point shares a plugin's formatter name cannot reroute it.
            plugin_owned = hint in (plugin_formatters or {}) or hint in (
                data.format_overrides or {}
            )
            if hint not in formatters:
                LOG.warning(
                    "unknown_format_hint",
                    hint=hint,
                    available=sorted(formatters.keys()),
                )
            elif plugin_owned or hint not in BUILTIN_FORMATTERS:
                # The plugin's or a package's own formatter: it sees
                # output_path and the console, so it decides for files and
                # pipes itself.
                hint_applied = True
                effective_format = hint
            elif not output_path and stream_is_tty(console):
                hint_applied = True
                effective_format = hint
        data = data.data

    if not user_explicit and output_path and not hint_applied:
        suffix_format = _SUFFIX_FORMATS.get(_output_suffix(output_path))
        if suffix_format in formatters:
            effective_format = suffix_format

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
            output_config = select_views(output_config, view_args)

    formatter.format(data, console, output_config=output_config, output_path=output_path)
