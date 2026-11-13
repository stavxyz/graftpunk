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
from collections.abc import Iterable
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
    if all(ord(c) in cmap or c in _LAYOUT_CHARACTERS for c in text):
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
    from fpdf import FPDF
    from fpdf.fonts import FontFace

    output = Path(output_path).resolve()
    pdf = FPDF()
    _add_unicode_font(pdf)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    page_width = pdf.w - pdf.l_margin - pdf.r_margin
    missing: set[str] = set()

    # Vendor header with optional logo
    if vendor or logo:
        logo_w = 0
        if logo:
            if Path(logo).exists():
                logo_w = 25
                pdf.image(str(logo), x=pdf.l_margin, y=pdf.get_y(), w=logo_w)
            else:
                LOG.warning("pdf_logo_not_found", path=str(logo))

        x_after_logo = pdf.l_margin + logo_w + (3 if logo_w else 0)
        pdf.set_x(x_after_logo)

        if vendor:
            pdf.set_font(_PDF_FONT, style="B", size=18)
            pdf.cell(text=_drawable(pdf, vendor, missing, bold=True), new_x="LMARGIN", new_y="NEXT")
        if vendor_info:
            pdf.set_x(x_after_logo)
            pdf.set_font(_PDF_FONT, size=8)
            pdf.cell(text=_drawable(pdf, vendor_info, missing), new_x="LMARGIN", new_y="NEXT")

        if logo_w:
            pdf.set_y(pdf.get_y() + 2)
        pdf.ln(2)

        y = pdf.get_y()
        pdf.line(pdf.l_margin, y, pdf.l_margin + page_width, y)
        pdf.ln(4)

    # Title. fpdf2's cell() raises on empty text, so an empty title draws none.
    if title:
        pdf.set_font(_PDF_FONT, style="B", size=14)
        pdf.cell(text=_drawable(pdf, title, missing, bold=True), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

    # Metadata section: a pair whose value is empty is left out, as cell()
    # raises on empty text.
    shown = [(safe_text(key), safe_text(value)) for key, value in (metadata or {}).items()]
    shown = [(key, value) for key, value in shown if value]
    if shown:
        for key, value in shown:
            pdf.set_font(_PDF_FONT, style="B", size=9)
            pdf.cell(text=_drawable(pdf, f"{key}: ", missing, bold=True), new_x="END")
            pdf.set_font(_PDF_FONT, size=9)
            pdf.cell(text=_drawable(pdf, value, missing), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)

    if not data:
        pdf.set_font(_PDF_FONT, size=10)
        pdf.cell(text="No data.")
        return _write_pdf(pdf, output, missing)

    rows = [flatten_dict(row) for row in table_rows(data)] if flatten else table_rows(data)
    columns = ordered_keys(rows)

    # Build table data: header row + data rows. A key need not be a string (1,
    # None, a tuple); fpdf2 takes only text, so the header shows its str() (#310).
    table_data: list[list[str]] = [
        [_drawable(pdf, safe_text(col), missing, bold=True) for col in columns]
    ]
    for row in rows:
        table_data.append([_drawable(pdf, safe_text(row.get(col, "")), missing) for col in columns])

    # Render table
    pdf.set_font(_PDF_FONT, size=7)
    headings_style = FontFace(emphasis="BOLD", fill_color=(220, 220, 220))

    cut = 0
    for part_index, part in enumerate(_column_parts(pdf, table_data, page_width)):
        if part_index:
            pdf.ln(4)
        col_width = page_width / max(len(part), 1)
        part_rows, part_cut = _fit_part(
            pdf, [[row[col] for col in part] for row in table_data], col_width
        )
        cut += part_cut
        pdf.set_font(_PDF_FONT, size=7)
        with pdf.table(
            headings_style=headings_style,
            col_widths=(col_width,) * len(part),
            first_row_as_headings=True,
        ) as table:
            for row_data in part_rows:
                row_obj = table.row()
                for cell_text in row_data:
                    row_obj.cell(cell_text)

    return _write_pdf(pdf, output, missing, cut)


def _write_pdf(pdf: FPDF, output: Path, missing: set[str], cut: int = 0) -> tuple[str, int]:
    """Write *pdf* to *output*, after one warning naming the characters drawn as
    ``_NO_GLYPH`` (the first ten, as code points) and one counting the cells
    cut to fit a page."""
    if missing:
        LOG.warning(
            "pdf_characters_not_in_font",
            count=len(missing),
            characters=[f"U+{ord(c):04X}" for c in sorted(missing)[:10]],
            drawn_as="U+FFFD",
        )
    if cut:
        LOG.warning("pdf_cells_truncated", count=cut, marked_with="U+2026")
    pdf.output(str(output))
    return str(output), pdf.pages_count


# What ends a cell cut to fit one page.
_CUT_MARK = "…"


def _fit_part(pdf: FPDF, rows: list[list[str]], col_width: float) -> tuple[list[list[str]], int]:
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
    width = col_width - pdf.line_width
    cut = 0

    pdf.set_font(_PDF_FONT, style="B", size=7)
    heading_cap = max(1, total // 4)
    headings = []
    heading_lines = 1
    for text in rows[0]:
        fitted, lines, was_cut = _fit_cell(pdf, text, width, heading_cap)
        headings.append(fitted)
        heading_lines = max(heading_lines, lines)
        cut += was_cut

    pdf.set_font(_PDF_FONT, size=7)
    body_cap = max(1, total - heading_lines - 1)
    body = []
    for row in rows[1:]:
        fitted_row = []
        for text in row:
            fitted, _lines, was_cut = _fit_cell(pdf, text, width, body_cap)
            fitted_row.append(fitted)
            cut += was_cut
        body.append(fitted_row)
    return [headings, *body], cut


def _fit_cell(pdf: FPDF, text: str, width: float, cap: int) -> tuple[str, int, bool]:
    """*text* cut to *cap* lines at *width* in the current font, how many lines
    it takes, and whether it was cut."""
    if not text:
        return text, 1, False
    usable = max(width - 2 * pdf.c_margin, 0.01)
    # Word wrapping leaves at most about a line's width unused per line, so
    # three times the text's width in lines, plus its line breaks, is more
    # than the count: a text under it fits without being measured.
    breaks = text.count("\n") + text.count("\r")
    bound = breaks + 1 + 3 * int(pdf.get_string_width(text) // usable + 1)
    if bound <= cap:
        return text, min(bound, cap), False
    # cap lines hold at most cap * (chars per line + a line break) characters,
    # a line holding at most usable / (narrowest visible character) of them.
    # Measuring that much of a long text decides the first cap lines; the
    # rest is never wrapped (a 9000-character cell took seconds).
    narrowest = min((w for c in set(text) if (w := pdf.get_string_width(c)) > 0), default=usable)
    prefix = text[: cap * (int(usable / narrowest) + 2) + 1]
    lines = pdf.multi_cell(w=width, text=prefix, dry_run=True, output="LINES")
    if len(lines) <= cap and len(prefix) < len(text):
        # Characters of no width (combining marks) defeat the limit: measure it all.
        lines = pdf.multi_cell(w=width, text=text, dry_run=True, output="LINES")
    if len(lines) <= cap:
        return text, len(lines), False
    kept = list(lines[:cap])
    while True:
        last = kept[-1]
        while last and pdf.get_string_width(last + _CUT_MARK) > usable:
            last = last[:-1]
        kept[-1] = last + _CUT_MARK
        fitted = "\n".join(kept)
        if len(pdf.multi_cell(w=width, text=fitted, dry_run=True, output="LINES")) <= cap:
            return fitted, cap, True
        if len(kept) == 1:
            return _CUT_MARK, 1, True
        kept = kept[:-1]


def _column_parts(pdf: FPDF, table_data: list[list[str]], page_width: float) -> list[list[int]]:
    """The column indexes of each table the pdf draws, left to right.

    A table whose columns all fit the page at a width that holds the widest
    character any cell or heading uses (heading in bold) is one table, as
    before. A wider one is split into consecutive tables of as many columns as
    fit, each repeating the first column so a row can be matched across parts:
    the rows stay in order and nothing is shrunk below a readable size.
    """
    count = len(table_data[0]) if table_data else 0
    if count <= 1:
        return [list(range(count))]
    pdf.set_font(_PDF_FONT, style="B", size=7)
    headings = set("".join(table_data[0])) | {_CUT_MARK}
    widest = max(pdf.get_string_width(c) for c in headings)
    pdf.set_font(_PDF_FONT, size=7)
    # The cut mark too: a cell cut to fit a page ends in it (_fit_part).
    body = set().union(*(set(cell) for row in table_data[1:] for cell in row), {_CUT_MARK})
    widest = max(widest, max((pdf.get_string_width(c) for c in body), default=0.0))
    # fpdf2 takes the cell margin on both sides and the border line out of a
    # column's width, and raises "Not enough horizontal space to render a
    # single character" when what is left is narrower than one character. This
    # bound is a little wider than fpdf2's own test (a page of W fits 40
    # columns by it, 42 by fpdf2), so a column it allows always renders.
    needed = widest + 2 * pdf.c_margin + pdf.line_width
    fits = max(int(page_width // needed), 1)
    if count <= fits:
        return [list(range(count))]
    if fits == 1:
        return [[col] for col in range(count)]
    step = fits - 1
    return [[0, *range(start, min(start + step, count))] for start in range(1, count, step)]
