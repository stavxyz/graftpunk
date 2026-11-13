"""Generic data-to-file export utilities.

Provides ``flatten_dict``, ``ordered_keys``, ``json_to_csv``,
``json_to_pdf``, and ``get_downloads_dir`` for converting lists of
flat or nested dicts into CSV and PDF files respectively, and
resolving the download directory for file-based output.  Intended
for use by plugin download/export commands.
"""

from __future__ import annotations

import csv
import json
import numbers
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from graftpunk._safe_text import safe_text
from graftpunk.logging import get_logger

if TYPE_CHECKING:
    from fpdf import FPDF

LOG = get_logger(__name__)

_DEFAULT_DOWNLOADS_DIR = "gp-downloads"

# A spreadsheet runs a CSV cell that starts with one of these as a formula. The
# list is OWASP's (https://owasp.org/www-community/attacks/CSV_Injection),
# full-width forms included.
_FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r", "\n", "＝", "＋", "－", "＠")


def _neutralize_formula(text: str) -> str:
    """Prefix *text* with ``'`` when a spreadsheet would read it as a formula (#288)."""
    return "'" + text if text.startswith(_FORMULA_LEADS) else text


def _csv_cell(value: object) -> object:
    """A ``csv.writer`` cell for *value*: a number or ``None`` as is, anything else neutralised.

    A number is left alone, so ``-5`` stays a number to a spreadsheet; the
    same characters held as a string are site text and get the prefix.
    """
    if value is None or isinstance(value, numbers.Number):
        # csv.writer calls str(), which raises past Python's 4300-digit limit.
        return _json_value(value) if isinstance(value, int) else value
    return _neutralize_formula(safe_text(value))


def _json_key(key: object) -> object:
    """*key* as JSON can write it: a string, number, bool or None as it is,
    and any other key (a tuple, a frozenset) as its ``str()``."""
    if key is None or isinstance(key, str | int | float | bool):
        return _json_value(key)
    return safe_text(key)


def _json_ready(value: object, active: set[int]) -> object:
    """*value* with every dict key at any depth that JSON cannot write as its
    ``str()``; a later key that reads the same as an earlier one is numbered."""
    if isinstance(value, dict | list | tuple):
        if id(value) in active:
            # A container that holds itself, written where the loop closes as
            # Python's repr writes it; json.dumps raised on it.
            return "{...}" if isinstance(value, dict) else "[...]"
        active.add(id(value))
        try:
            if isinstance(value, dict):
                ready: dict[object, object] = {}
                for key, item in value.items():
                    name = _json_key(key)
                    candidate, number = name, 1
                    while candidate in ready:
                        number += 1
                        candidate = f"{name} ({number})"
                    ready[candidate] = _json_ready(item, active)
                return ready
            return [_json_ready(item, active) for item in value]
        finally:
            active.discard(id(value))
    return _json_value(value)


def json_text(value: object, *, indent: int | None = None) -> str:
    """``json.dumps(value, default=safe_text)`` that also writes a dict key
    JSON cannot (a tuple, a frozenset) at any depth as its text: ``default`` is
    never asked about a key, so such a key raised ``TypeError``. An int past
    Python's 4300-digit limit, which ``json.dumps`` cannot write, is its
    :func:`safe_text`. Every formatter and export that writes a value as JSON
    text uses this."""
    return json.dumps(_json_ready(value, set()), indent=indent, default=safe_text)


# An int this many bits long may pass Python's 4300-digit str() limit; only
# such an int is tried (4300 decimal digits are about 14284 bits).
_LONG_INT_BITS = 14000


def _json_value(value: object) -> object:
    """*value*, or its :func:`safe_text` when it is an int ``str()`` refuses
    (past Python's 4300-digit limit)."""
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value.bit_length() > _LONG_INT_BITS
    ):
        try:
            str(value)
        except ValueError:
            return safe_text(value)
    return value


# fpdf2's core fonts (Helvetica) encode Latin-1 only, and raised on a curly
# quote, an en dash or any letter past it (#290). fpdf2 ships no TTF, so
# graftpunk bundles DejaVu Sans (licence in fonts/LICENSE-DejaVu.txt).
_PDF_FONT = "DejaVuSans"
_FONTS_DIR = Path(__file__).parent / "fonts"


def _add_unicode_font(pdf: FPDF) -> None:
    """Register DejaVu Sans, regular and bold, as ``_PDF_FONT`` on *pdf*."""
    pdf.add_font(_PDF_FONT, fname=_FONTS_DIR / "DejaVuSans.ttf")
    pdf.add_font(_PDF_FONT, style="B", fname=_FONTS_DIR / "DejaVuSans-Bold.ttf")


# What a character DejaVu Sans has no glyph for (CJK, for one) is drawn as.
# fpdf2 left such a character out of the drawn text without a mark, so
# "A中B" read "AB" on the page and in a copy of its text.
_NO_GLYPH = "�"
# Line breaks and tabs have no glyph and are fpdf2's to lay out.
_LAYOUT_CHARACTERS = frozenset("\n\r\t")


def _drawable(pdf: FPDF, text: str, missing: set[str], *, bold: bool = False) -> str:
    """*text* with each character the font has no glyph for as ``_NO_GLYPH``,
    each such character added to *missing*."""
    cmap = pdf.fonts[_PDF_FONT.lower() + ("B" if bold else "")].cmap
    if all(ord(c) in cmap or c in _LAYOUT_CHARACTERS for c in set(text)):
        return text
    kept = []
    for c in text:
        if ord(c) in cmap or c in _LAYOUT_CHARACTERS:
            kept.append(c)
        else:
            missing.add(c)
            kept.append(_NO_GLYPH)
    return "".join(kept)


def get_downloads_dir() -> Path:
    """Resolve the download directory for file-based output.

    Uses the ``GP_DOWNLOADS_DIR`` environment variable if set,
    otherwise defaults to ``./gp-downloads/``. Creates the
    directory if it doesn't exist.

    Returns:
        Path to the downloads directory.
    """
    dir_path = Path(os.environ.get("GP_DOWNLOADS_DIR", _DEFAULT_DOWNLOADS_DIR)).resolve()
    dir_path.mkdir(parents=True, exist_ok=True)
    return dir_path


def flatten_dict(
    d: dict[str, object],
    parent_key: str = "",
    sep: str = ".",
) -> dict[str, object]:
    """Recursively flatten a nested dict to dot-notation keys.

    Args:
        d: Dictionary to flatten.
        parent_key: Prefix for all keys (used in recursion).
        sep: Separator between parent and child keys.

    Returns:
        Flat dictionary with dot-notation (or custom separator) keys.
    """
    items: list[tuple[str, object]] = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{safe_text(k)}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(cast("dict[str, object]", v), new_key, sep=sep).items())
        elif isinstance(v, list):
            if not v:
                items.append((new_key, ""))
            elif isinstance(v[0], dict):
                items.append((new_key, json_text(v)))
            else:
                items.append((new_key, ", ".join(safe_text(i) for i in v)))
        else:
            items.append((new_key, v))
    return dict(items)


def ordered_keys(rows: list[dict[str, object]]) -> list[str]:
    """Collect superset of keys from all rows, preserving first-appearance order."""
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                columns.append(key)
                seen.add(key)
    return columns


VALUE_COLUMN = "value"
"""The column a non-dict row's value is written in when a formatter writes a
list as a table (:func:`table_rows`)."""


def table_rows(items: Iterable[object]) -> list[dict[str, object]]:
    """*items* as table rows: a dict as it is, and any other item (a string, a
    number, a list) as the one-column row ``{"value": item}``.

    The one rule every formatter that writes a list as a table follows (table,
    csv, xlsx, pdf, :func:`json_to_csv` and :func:`json_to_pdf`), so a list
    that mixes dicts and other values, or holds no dict, renders instead of
    raising. A dict row with its own ``value`` key shares that column.
    """
    return [
        cast("dict[str, object]", item) if isinstance(item, dict) else {VALUE_COLUMN: item}
        for item in items
    ]


def json_to_csv(
    data: list[dict[str, object]],
    output_path: str | Path,
    *,
    flatten: bool = True,
) -> tuple[str, int]:
    """Convert a list of dicts to a CSV file.

    A header or a value that is not a number and would start a spreadsheet
    formula gets a leading ``'`` (#288); numbers and ``None`` are written as before.

    Args:
        data: List of dictionaries to write as CSV rows.
        output_path: Destination file path (string or Path).
        flatten: If True, flatten nested dicts to dot-notation keys.

    Returns:
        Tuple of (absolute path string, row count).
    """
    output = Path(output_path).resolve()

    if not data:
        output.write_text("")
        return str(output), 0

    rows = [flatten_dict(row) for row in table_rows(data)] if flatten else table_rows(data)
    columns = ordered_keys(rows)

    with open(output, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([_neutralize_formula(safe_text(c)) for c in columns])
        for row in rows:
            writer.writerow([_csv_cell(row.get(c)) for c in columns])

    return str(output), len(rows)


def json_to_pdf(
    data: list[dict[str, object]],
    output_path: str | Path,
    *,
    title: str = "Report",
    vendor: str | None = None,
    vendor_info: str | None = None,
    logo: str | Path | None = None,
    metadata: dict[str, str] | None = None,
    flatten: bool = True,
) -> tuple[str, int]:
    """Convert a list of dicts to a PDF file with a table layout.

    Args:
        data: List of dictionaries to render as table rows.
        output_path: Destination file path (string or Path).
        title: Title displayed at the top of the PDF.
        vendor: Optional vendor/company name rendered as a header.
        vendor_info: Optional address/contact line below the vendor name.
        logo: Optional path to a logo image rendered beside vendor name.
        metadata: Optional key-value pairs rendered below the title.
        flatten: If True, flatten nested dicts to dot-notation keys.

    Returns:
        Tuple of (absolute path string, page count).
    """
    output = Path(output_path).resolve()
    missing: set[str] = set()
    runs = [0]
    probe = _new_pdf()

    def text(value: object, *, bold: bool = False) -> str:
        # fpdf2 deletes every "\r" before it lays text out.
        bounded, was_shortened = _bounded_runs(probe, safe_text(value).replace("\r", ""))
        runs[0] += was_shortened
        return _drawable(probe, bounded, missing, bold=bold)

    # fpdf2's cell() raises on empty text: an empty vendor line or title draws
    # nothing, and a metadata pair whose value is empty is left out.
    header = _PdfHeader(
        logo=logo,
        vendor=text(vendor, bold=True) if vendor else "",
        vendor_info=text(vendor_info) if vendor_info else "",
        title=text(title, bold=True) if title else "",
        metadata=[
            (text(f"{safe_text(key)}: ", bold=True), text(value))
            for key, value in (metadata or {}).items()
            if safe_text(value)
        ],
    )
    # The probe has drawn nothing yet: the first attempt draws on it.
    pdf = _start_pdf(header, probe)
    page_width = pdf.w - pdf.l_margin - pdf.r_margin

    if not data:
        pdf.set_font(_PDF_FONT, size=10)
        pdf.cell(text="No data.")
        return _write_pdf(pdf, output, missing, 0, runs[0])

    rows = [flatten_dict(row) for row in table_rows(data)] if flatten else table_rows(data)
    columns = ordered_keys(rows)

    # Build table data: header row + data rows. A key need not be a string (1,
    # None, a tuple); fpdf2 takes only text, so the header shows its text (#310).
    table_data: list[list[str]] = [[text(col, bold=True) for col in columns]]
    for row in rows:
        table_data.append([text(row.get(col, "")) for col in columns])

    # Render table. Drawn once as it is; a row fpdf2 cannot fit on one page
    # makes it raise, and the document is then drawn again with each cell cut
    # to the lines a page holds (_fit_part). Measuring every cell up front
    # took two to three times as long on ordinary tables.
    pdf.set_font(_PDF_FONT, size=7)
    margin, parts = _column_layout(pdf, table_data, page_width)
    try:
        cut = _draw_table(pdf, table_data, parts, margin, page_width, fit=False)
    except Exception:  # noqa: BLE001 - drawn again, every cell fitted to a page
        pdf = _start_pdf(header)
        cut = _draw_table(pdf, table_data, parts, margin, page_width, fit=True)
    return _write_pdf(pdf, output, missing, cut, runs[0])


def _write_pdf(
    pdf: FPDF, output: Path, missing: set[str], cut: int = 0, runs: int = 0
) -> tuple[str, int]:
    """Write *pdf* to *output*, after one warning naming the characters drawn as
    ``_NO_GLYPH`` (the first ten, as code points), one counting the cells cut to
    fit a page, and one counting the texts whose runs of characters with no
    width were shortened."""
    if missing:
        LOG.warning(
            "pdf_characters_not_in_font",
            count=len(missing),
            characters=[f"U+{ord(c):04X}" for c in sorted(missing)[:10]],
            drawn_as="U+FFFD",
        )
    if cut:
        LOG.warning("pdf_cells_truncated", count=cut, marked_with="U+2026")
    if runs:
        LOG.warning("pdf_zero_width_runs_shortened", count=runs, kept_per_run=_MAX_ZERO_WIDTH_RUN)
    pdf.output(str(output))
    return str(output), pdf.pages_count


@dataclass(frozen=True)
class _PdfHeader:
    """What a pdf draws above its table, as drawable text: an empty string
    draws nothing."""

    logo: str | Path | None
    vendor: str
    vendor_info: str
    title: str
    metadata: list[tuple[str, str]]


def _new_pdf() -> FPDF:
    """An A4 page with DejaVu Sans registered and fpdf2's 15 mm page-break margin."""
    from fpdf import FPDF

    pdf = FPDF()
    _add_unicode_font(pdf)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    return pdf


def _start_pdf(header: _PdfHeader, pdf: FPDF | None = None) -> FPDF:
    """*pdf* (a new one when None) with *header* drawn: the vendor lines and
    logo, the title, and the metadata pairs. A given *pdf* must have drawn
    nothing yet."""
    pdf = pdf or _new_pdf()
    page_width = pdf.w - pdf.l_margin - pdf.r_margin
    if header.vendor or header.logo:
        logo_w = 0
        if header.logo:
            if Path(header.logo).exists():
                logo_w = 25
                pdf.image(str(header.logo), x=pdf.l_margin, y=pdf.get_y(), w=logo_w)
            else:
                LOG.warning("pdf_logo_not_found", path=str(header.logo))
        x_after_logo = pdf.l_margin + logo_w + (3 if logo_w else 0)
        pdf.set_x(x_after_logo)
        if header.vendor:
            pdf.set_font(_PDF_FONT, style="B", size=18)
            pdf.cell(text=header.vendor, new_x="LMARGIN", new_y="NEXT")
        if header.vendor_info:
            pdf.set_x(x_after_logo)
            pdf.set_font(_PDF_FONT, size=8)
            pdf.cell(text=header.vendor_info, new_x="LMARGIN", new_y="NEXT")
        if logo_w:
            pdf.set_y(pdf.get_y() + 2)
        pdf.ln(2)
        y = pdf.get_y()
        pdf.line(pdf.l_margin, y, pdf.l_margin + page_width, y)
        pdf.ln(4)
    if header.title:
        pdf.set_font(_PDF_FONT, style="B", size=14)
        pdf.cell(text=header.title, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)
    if header.metadata:
        for key, value in header.metadata:
            pdf.set_font(_PDF_FONT, style="B", size=9)
            pdf.cell(text=key, new_x="END")
            pdf.set_font(_PDF_FONT, size=9)
            pdf.cell(text=value, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)
    return pdf


# The most characters with no width (combining marks, zero-width spaces) kept
# in a row. fpdf2's line breaking is quadratic in the characters of one line:
# a cell of 50,000 combining marks took 100 s to fit, and 8,000 zero-width
# spaces took 2.5 s to measure once. No text a reader can see needs more.
_MAX_ZERO_WIDTH_RUN = 16


def _bounded_runs(pdf: FPDF, text: str) -> tuple[str, bool]:
    """*text* with each run of more than ``_MAX_ZERO_WIDTH_RUN`` characters of
    no width (in the regular font) shortened to that many, and whether any
    was. Line breaks and tabs are fpdf2's to lay out and end a run."""
    if len(text) <= _MAX_ZERO_WIDTH_RUN:
        return text, False
    pdf.set_font(_PDF_FONT, size=7)
    zero = {c for c in set(text) if c not in _LAYOUT_CHARACTERS and pdf.get_string_width(c) == 0}
    if not zero:
        return text, False
    kept: list[str] = []
    run = 0
    shortened = False
    for c in text:
        if c in zero:
            run += 1
            if run > _MAX_ZERO_WIDTH_RUN:
                shortened = True
                continue
        else:
            run = 0
        kept.append(c)
    return ("".join(kept), True) if shortened else (text, False)


# What ends a cell cut to fit one page.
_CUT_MARK = "…"

# fpdf2's default cell margin (1 mm), and the least a table that would
# otherwise split is drawn with: DejaVu Sans is wider than the Helvetica of
# 1.42.0, and at 0.4 mm a page holds at least as many columns as 1.42.0 drew.
_CELL_MARGIN = 1.0
_MIN_CELL_MARGIN = 0.4
# fpdf2 raises "Not enough horizontal space to render a single character" when
# a column, less a cell margin on each side, is narrower than a character it
# holds (measured: 42 columns of W fit an A4 page, 43 raise).
_WIDTH_SLACK = 0.01


def _widest(pdf: FPDF, table_data: list[list[str]], extra: str = "") -> float:
    """The widest character any heading (in bold) or cell of *table_data*
    holds, and of *extra* in either weight."""
    pdf.set_font(_PDF_FONT, style="B", size=7)
    widest = max(
        (pdf.get_string_width(c) for c in set("".join(table_data[0])) | set(extra)), default=0.0
    )
    pdf.set_font(_PDF_FONT, size=7)
    body = set().union(*(set(cell) for row in table_data[1:] for cell in row), set(extra))
    return max(widest, max((pdf.get_string_width(c) for c in body), default=0.0))


def _split(columns: list[int], fits: int) -> list[list[int]]:
    """*columns* in consecutive parts of at most *fits* columns, each part after
    the first led by the first column again, so a row can be matched across
    parts."""
    if len(columns) <= fits:
        return [columns]
    if fits <= 1:
        return [[col] for col in columns]
    first, rest, step = columns[0], columns[1:], fits - 1
    return [[first, *rest[i : i + step]] for i in range(0, len(rest), step)]


def _column_layout(
    pdf: FPDF, table_data: list[list[str]], page_width: float
) -> tuple[float, list[list[int]]]:
    """The cell margin and the column indexes of each table the pdf draws.

    A column must hold the widest character in the table plus a margin each
    side. A table whose columns fit at fpdf2's 1 mm margin is one table, drawn
    as before; one that fits at a margin down to 0.4 mm is one table at that
    margin; a wider one is split at 0.4 mm (:func:`_split`). The cut mark is
    not counted here: a column needs room for it only when one of its cells is
    cut (:func:`_draw_table`).
    """
    count = len(table_data[0]) if table_data else 0
    columns = list(range(count))
    if count <= 1:
        return _CELL_MARGIN, [columns]
    widest = _widest(pdf, table_data)
    if widest + 2 * _CELL_MARGIN + _WIDTH_SLACK <= page_width / count:
        return _CELL_MARGIN, [columns]
    margin = (page_width / count - widest - _WIDTH_SLACK) / 2
    if margin >= _MIN_CELL_MARGIN:
        return margin, [columns]
    fits = int(page_width // (widest + 2 * _MIN_CELL_MARGIN + _WIDTH_SLACK))
    return _MIN_CELL_MARGIN, _split(columns, fits)


def _draw_table(
    pdf: FPDF,
    table_data: list[list[str]],
    parts: list[list[int]],
    margin: float,
    page_width: float,
    *,
    fit: bool,
) -> int:
    """Draw *table_data* as one table per part, and return how many cells were
    cut. With *fit*, each cell is first cut to the lines a page holds
    (:func:`_fit_part`); a part with a cut cell whose columns cannot hold the
    cut mark is split again at the width the mark needs."""
    from fpdf.fonts import FontFace

    headings_style = FontFace(emphasis="BOLD", fill_color=(220, 220, 220))
    saved_margin = pdf.c_margin
    pdf.c_margin = margin
    cut = 0
    drawn = 0
    try:
        for part in parts:
            work: list[tuple[list[int], tuple[list[list[str]], int] | None]] = [(part, None)]
            if fit:
                try:
                    width = page_width / max(len(part), 1)
                    fitted = _fit_part(pdf, _columns_of(table_data, part), width, exact=True)
                    work = [(part, fitted)]
                except _MarkTooWide:
                    needed = _widest(pdf, table_data, _CUT_MARK) + 2 * margin + _WIDTH_SLACK
                    pieces = _split(part, max(int(page_width // needed), 1))
                    work = [(piece, None) for piece in pieces]
            for piece, done in work:
                col_width = page_width / max(len(piece), 1)
                rows = _columns_of(table_data, piece)
                # Without fit, cells are fitted by the estimate (_estimate_lines);
                # with it, by fpdf2's own wrapping.
                rows, piece_cut = done or _fit_part(pdf, rows, col_width, exact=fit)
                cut += piece_cut
                if drawn:
                    pdf.ln(4)
                drawn += 1
                pdf.set_font(_PDF_FONT, size=7)
                with pdf.table(
                    headings_style=headings_style,
                    col_widths=(col_width,) * len(piece),
                    first_row_as_headings=True,
                ) as table:
                    for row_data in rows:
                        row_obj = table.row()
                        for cell_text in row_data:
                            row_obj.cell(cell_text)
    finally:
        pdf.c_margin = saved_margin
    return cut


def _columns_of(table_data: list[list[str]], part: list[int]) -> list[list[str]]:
    return [[row[col] for col in part] for row in table_data]


class _MarkTooWide(Exception):  # noqa: N818 - a signal, not an error
    """A cell must be cut and its column cannot hold the cut mark: the part is
    split again at the width the mark needs (:func:`_draw_table`)."""


def _fit_part(
    pdf: FPDF, rows: list[list[str]], col_width: float, *, exact: bool
) -> tuple[list[list[str]], int]:
    """*rows* (headings first) with each cell cut to the lines one page holds,
    and how many cells were cut.

    fpdf2 cannot split a table row across pages: a row taller than a page
    raised ``The row with index N is too high``, and DejaVu Sans, wider than
    Helvetica, reaches that with shorter text. A row is as tall as its tallest
    cell, and every page repeats the heading row, so a heading keeps at most a
    quarter of a page's lines and a body cell what the page has left, less one
    line of slack. A cut cell ends in ``_CUT_MARK``.
    """
    pdf.set_font(_PDF_FONT, size=7)
    line_height = 2 * pdf.font_size  # fpdf2's table default
    total = int((pdf.page_break_trigger - pdf.t_margin) // line_height)
    width = col_width  # the width fpdf2's table hands multi_cell
    cut = 0

    pdf.set_font(_PDF_FONT, style="B", size=7)
    heading_cap = max(1, total // 4)
    bold: dict[str, float] = {}
    regular: dict[str, float] = {}
    headings = []
    heading_lines = 1
    usable = max(width - 2 * pdf.c_margin, 0.01)
    for text in rows[0]:
        fitted, lines, was_cut = _fit_cell(pdf, text, width, heading_cap, bold, exact=exact)
        if lines > 1 and not was_cut:
            # The cheap bound overstates; the heading row's height sets every
            # body cell's room, so count its lines.
            lines = (
                len(pdf.multi_cell(w=width, text=fitted, dry_run=True, output="LINES"))
                if exact
                else len(_estimate_lines(fitted, usable, bold, heading_cap + 1))
            )
        headings.append(fitted)
        heading_lines = max(heading_lines, lines)
        cut += was_cut

    pdf.set_font(_PDF_FONT, size=7)
    body_cap = max(1, total - heading_lines - 1)
    body = []
    for row in rows[1:]:
        fitted_row = []
        for text in row:
            fitted, _lines, was_cut = _fit_cell(pdf, text, width, body_cap, regular, exact=exact)
            fitted_row.append(fitted)
            cut += was_cut
        body.append(fitted_row)
    return [headings, *body], cut


def _fit_cell(
    pdf: FPDF, text: str, width: float, cap: int, widths: dict[str, float], *, exact: bool
) -> tuple[str, int, bool]:
    """*text* cut to *cap* lines at *width* in the current font, how many lines
    it takes (an upper estimate when it was not wrapped), and whether it was
    cut. *widths* caches each character's width in that font.

    Without *exact*, lines are counted and cut by :func:`_estimate_lines`, a
    light copy of fpdf2's word wrapping; with it, by fpdf2's own ``multi_cell``
    (two to three times the cost of drawing the cell), which the export falls
    back to when a row the estimate passed still does not fit.
    """
    if not text:
        return text, 1, False
    usable = max(width - 2 * pdf.c_margin, 0.01)
    for c in set(text) - widths.keys():
        widths[c] = pdf.get_string_width(c)
    breaks = text.count("\n") + text.count("\x0c")
    # Word wrapping leaves at most about a line's width unused per line, so
    # three times the text's width in lines, plus its line breaks, is more
    # than the count: a text under it fits without being wrapped.
    bound = breaks + 1 + 3 * int(sum(map(widths.__getitem__, text)) // usable + 1)
    if bound <= cap:
        return text, min(bound, cap), False
    # A line holds at most usable / (narrowest visible character) visible
    # characters, each followed by at most _MAX_ZERO_WIDTH_RUN of no width
    # (_bounded_runs), so cap lines hold no more characters than this; wrapping
    # that much of a long text decides its first cap lines.
    narrowest = min((w for c, w in widths.items() if w > 0 and c in text), default=usable)
    per_line = (int(usable / narrowest) + 2) * (_MAX_ZERO_WIDTH_RUN + 1)
    prefix = text[: cap * per_line + 1]
    if not exact:
        lines = _estimate_lines(prefix, usable, widths, cap + 1)
        if len(lines) <= cap:
            return text, len(lines), False
    else:
        lines = pdf.multi_cell(w=width, text=prefix, dry_run=True, output="LINES")
        if len(lines) <= cap and len(prefix) < len(text):
            # Not expected by the bound above; wrapped whole rather than trusted.
            lines = pdf.multi_cell(w=width, text=text, dry_run=True, output="LINES")
        if len(lines) <= cap:
            return text, len(lines), False
    if widths.setdefault(_CUT_MARK, pdf.get_string_width(_CUT_MARK)) > usable:
        raise _MarkTooWide
    kept = list(lines[:cap])
    while True:
        last = kept[-1]
        while last and sum(map(widths.__getitem__, last)) + widths[_CUT_MARK] > usable:
            last = last[:-1]
        kept[-1] = last + _CUT_MARK
        fitted = "\n".join(kept)
        if (
            not exact
            or len(pdf.multi_cell(w=width, text=fitted, dry_run=True, output="LINES")) <= cap
        ):
            return fitted, cap, True
        if len(kept) == 1:
            return _CUT_MARK, 1, True
        kept = kept[:-1]


try:  # fpdf2's own list of the spaces a line may break at
    from fpdf.line_break import BREAKING_SPACE_SYMBOLS_STR as _BREAKING_SPACES
except ImportError:  # pragma: no cover - an fpdf2 without it
    _BREAKING_SPACES = " "


def _estimate_lines(text: str, usable: float, widths: dict[str, float], limit: int) -> list[str]:
    """The lines fpdf2's word wrapping makes of *text* at *usable* width, as
    closely as a few lines can copy it, stopping after *limit* lines: a line
    breaks at a newline or form feed, at the last space before the text passes
    the width (the space dropped), or, for a word wider than a line, before the
    character that passes it. Words are measured whole, so a cell costs a few
    operations per word."""
    lines: list[str] = []
    for paragraph in _HARD_BREAKS.split(text):
        line: list[str] = []
        line_width = 0.0
        pending_space = ""
        for token in _SPACES.split(paragraph):
            if not token:
                continue
            if token[0] in _BREAKING_SPACES:
                pending_space += token
                continue
            space_width = sum(map(widths.__getitem__, pending_space))
            word_width = sum(map(widths.__getitem__, token))
            if line and line_width + space_width + word_width > usable:
                lines.append("".join(line))
                if len(lines) >= limit:
                    return lines
                line, line_width, space_width = [], 0.0, 0.0
                pending_space = ""
            if line_width + space_width + word_width <= usable:
                line.append(pending_space + token)
                line_width += space_width + word_width
            else:
                # A word wider than the line: broken before the character that
                # passes the width, as fpdf2 does with no space to break at.
                start = len(line) == 0
                for c in pending_space + token if not start else token:
                    w = widths[c]
                    if line and line_width + w > usable:
                        lines.append("".join(line))
                        if len(lines) >= limit:
                            return lines
                        line, line_width = [], 0.0
                    line.append(c)
                    line_width += w
            pending_space = ""
        lines.append("".join(line))
        if len(lines) >= limit:
            return lines
    return lines


_HARD_BREAKS = re.compile("[\n\x0c]")
_SPACES = re.compile(f"([{re.escape(_BREAKING_SPACES)}]+)")
