"""Generic data-to-file export utilities.

Provides ``flatten_dict``, ``json_to_csv``, and ``json_to_pdf`` for
converting lists of flat or nested dicts into CSV and PDF files
respectively. Intended for use by plugin download/export commands.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import cast


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
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(cast("dict[str, object]", v), new_key, sep=sep).items())
        elif isinstance(v, list):
            if not v:
                items.append((new_key, ""))
            elif isinstance(v[0], dict):
                items.append((new_key, json.dumps(v)))
            else:
                items.append((new_key, ", ".join(str(i) for i in v)))
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


def json_to_csv(
    data: list[dict[str, object]],
    output_path: str | Path,
    *,
    flatten: bool = True,
) -> tuple[str, int]:
    """Convert a list of dicts to a CSV file.

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

    rows = [flatten_dict(row) for row in data] if flatten else data
    columns = ordered_keys(rows)

    with open(output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

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
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    page_width = pdf.w - pdf.l_margin - pdf.r_margin

    # Vendor header with optional logo
    if vendor or logo:
        logo_w = 0
        if logo and Path(logo).exists():
            logo_w = 25
            pdf.image(str(logo), x=pdf.l_margin, y=pdf.get_y(), w=logo_w)

        x_after_logo = pdf.l_margin + logo_w + (3 if logo_w else 0)
        pdf.set_x(x_after_logo)

        if vendor:
            pdf.set_font("Helvetica", style="B", size=18)
            pdf.cell(text=vendor, new_x="LMARGIN", new_y="NEXT")
        if vendor_info:
            pdf.set_x(x_after_logo)
            pdf.set_font("Helvetica", size=8)
            pdf.cell(text=vendor_info, new_x="LMARGIN", new_y="NEXT")

        # Ensure we're below the logo
        if logo_w:
            pdf.set_y(max(pdf.get_y(), pdf.get_y() + 2))
        pdf.ln(2)

        # Horizontal rule
        y = pdf.get_y()
        pdf.line(pdf.l_margin, y, pdf.l_margin + page_width, y)
        pdf.ln(4)

    # Title
    pdf.set_font("Helvetica", style="B", size=14)
    pdf.cell(text=title, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # Metadata section
    if metadata:
        pdf.set_font("Helvetica", size=9)
        for key, value in metadata.items():
            pdf.set_font("Helvetica", style="B", size=9)
            pdf.cell(text=f"{key}: ", new_x="END")
            pdf.set_font("Helvetica", size=9)
            pdf.cell(text=str(value), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)

    if not data:
        pdf.set_font("Helvetica", size=10)
        pdf.cell(text="No data.")
        pdf.output(str(output))
        return str(output), pdf.pages_count

    rows = [flatten_dict(row) for row in data] if flatten else data
    columns = ordered_keys(rows)

    # Build table data: header row + data rows
    table_data: list[list[str]] = [columns]
    for row in rows:
        table_data.append([str(row.get(col, "")) for col in columns])

    # Render table
    pdf.set_font("Helvetica", size=7)
    headings_style = FontFace(emphasis="BOLD", fill_color=(220, 220, 220))

    col_width = (pdf.w - pdf.l_margin - pdf.r_margin) / max(len(columns), 1)

    with pdf.table(
        headings_style=headings_style,
        col_widths=(col_width,) * len(columns),
        first_row_as_headings=True,
    ) as table:
        for row_data in table_data:
            row_obj = table.row()
            for cell_value in row_data:
                row_obj.cell(cell_value)

    pdf.output(str(output))
    return str(output), pdf.pages_count
