"""CSV and xlsx output never turns a value from a site into a formula (#288)."""

import csv
import decimal
import io
from pathlib import Path
from unittest.mock import MagicMock

import openpyxl
import pytest
from rich.console import Console

from graftpunk.plugins.export import json_to_csv
from graftpunk.plugins.formatters import CsvFormatter, XlsxFormatter
from graftpunk.plugins.output_config import ColumnFilter, OutputConfig, ViewConfig

# The leading characters OWASP's CSV Injection page lists, full-width forms included.
TRIGGERS = ["=", "+", "-", "@", "\t", "\r", "\n", "＝", "＋", "－", "＠"]


def _csv_rows(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.reader(f))


def _write_csv(tmp_path: Path, data: object, output_config: OutputConfig | None = None) -> Path:
    out = tmp_path / "out.csv"
    CsvFormatter().format(data, MagicMock(spec=Console), output_config, output_path=str(out))
    return out


class TestCsvFormatter:
    @pytest.mark.parametrize("lead", TRIGGERS)
    def test_string_cell_starting_with_a_trigger_gets_a_quote(
        self, tmp_path: Path, lead: str
    ) -> None:
        value = f'{lead}HYPERLINK("http://x.test","y")'
        rows = _csv_rows(_write_csv(tmp_path, [{"a": value}]))
        assert rows[1] == ["'" + value]

    def test_command_payload_is_neutralised(self, tmp_path: Path) -> None:
        rows = _csv_rows(_write_csv(tmp_path, [{"a": "=cmd|' /C calc'!A0"}]))
        assert rows[1] == ["'=cmd|' /C calc'!A0"]

    def test_ordinary_strings_are_unchanged(self, tmp_path: Path) -> None:
        data = [{"a": "Alice", "b": "a=b", "c": "", "d": "'quoted"}]
        rows = _csv_rows(_write_csv(tmp_path, data))
        assert rows[1] == ["Alice", "a=b", "", "'quoted"]

    def test_numbers_are_written_without_a_quote(self, tmp_path: Path) -> None:
        data = [{"i": -5, "f": -2.5, "d": decimal.Decimal("-1.10"), "b": True}]
        rows = _csv_rows(_write_csv(tmp_path, data))
        assert rows[1] == ["-5", "-2.5", "-1.10", "True"]

    def test_a_negative_number_held_as_a_string_gets_a_quote(self, tmp_path: Path) -> None:
        rows = _csv_rows(_write_csv(tmp_path, [{"a": "-5"}]))
        assert rows[1] == ["'-5"]

    def test_header_starting_with_a_trigger_gets_a_quote(self, tmp_path: Path) -> None:
        rows = _csv_rows(_write_csv(tmp_path, [{"=SUM(A1)": "x"}]))
        assert rows[0] == ["'=SUM(A1)"]

    def test_empty_result_header_row_is_neutralised(self, tmp_path: Path) -> None:
        config = OutputConfig(
            views=[ViewConfig(name="v", columns=ColumnFilter("include", ("ok", "@evil")))]
        )
        rows = _csv_rows(_write_csv(tmp_path, [], config))
        assert rows == [["ok", "'@evil"]]

    def test_stdout_is_neutralised_too(self) -> None:
        console = MagicMock(spec=Console)
        CsvFormatter().format([{"a": "=1+1"}], console)
        text = console.file.write.call_args[0][0]
        assert list(csv.reader(io.StringIO(text))) == [["a"], ["'=1+1"]]


class TestJsonToCsv:
    @pytest.mark.parametrize("lead", TRIGGERS)
    def test_string_cell_starting_with_a_trigger_gets_a_quote(
        self, tmp_path: Path, lead: str
    ) -> None:
        out = tmp_path / "x.csv"
        json_to_csv([{"a": f"{lead}1+1"}], out)
        assert _csv_rows(out)[1] == [f"'{lead}1+1"]

    def test_numbers_none_and_headers(self, tmp_path: Path) -> None:
        out = tmp_path / "x.csv"
        path, count = json_to_csv([{"n": -5, "=h": "ok"}, {"n": -1.5, "other": None}], out)
        assert count == 2
        assert path == str(out.resolve())
        assert _csv_rows(out) == [["n", "'=h", "other"], ["-5", "ok", ""], ["-1.5", "", ""]]

    def test_nested_values_flatten_as_before(self, tmp_path: Path) -> None:
        out = tmp_path / "x.csv"
        json_to_csv([{"a": {"b": "-x"}}], out)
        assert _csv_rows(out) == [["a.b"], ["'-x"]]


def _xlsx_cells(path: Path, sheet: str | None = None) -> list[list[openpyxl.cell.Cell]]:
    wb = openpyxl.load_workbook(path)
    ws = wb[sheet] if sheet else wb.active
    return [list(row) for row in ws.iter_rows()]


class TestXlsxFormatter:
    def test_list_rows_write_formulas_as_text(self, tmp_path: Path) -> None:
        out = tmp_path / "x.xlsx"
        data = [{"f": "=1+1", "arr": "{=SUM(A1:A2)}", "n": -5, "s": "-5", "x": 2.5}]
        XlsxFormatter().format(data, MagicMock(spec=Console), output_path=str(out))
        row = _xlsx_cells(out)[1]
        assert [c.data_type for c in row] == ["s", "s", "n", "s", "n"]
        assert [c.value for c in row] == ["=1+1", "{=SUM(A1:A2)}", -5, "-5", 2.5]

    def test_urls_are_text_not_hyperlinks(self, tmp_path: Path) -> None:
        out = tmp_path / "x.xlsx"
        data = [{"u": "https://evil.test/a", "m": "mailto:a@b.test"}]
        XlsxFormatter().format(data, MagicMock(spec=Console), output_path=str(out))
        row = _xlsx_cells(out)[1]
        assert [c.value for c in row] == ["https://evil.test/a", "mailto:a@b.test"]
        assert [c.hyperlink for c in row] == [None, None]

    def test_header_formula_is_text(self, tmp_path: Path) -> None:
        out = tmp_path / "x.xlsx"
        XlsxFormatter().format([{"=1+1": "v"}], MagicMock(spec=Console), output_path=str(out))
        header = _xlsx_cells(out)[0][0]
        assert (header.data_type, header.value) == ("s", "=1+1")

    def test_dict_keys_and_values_are_text(self, tmp_path: Path) -> None:
        out = tmp_path / "x.xlsx"
        XlsxFormatter().format({"=k": "=v", "n": -3}, MagicMock(spec=Console), output_path=str(out))
        rows = _xlsx_cells(out)
        assert [(c.data_type, c.value) for c in rows[1]] == [("s", "=k"), ("s", "=v")]
        assert [(c.data_type, c.value) for c in rows[2]] == [("s", "n"), ("n", -3)]

    def test_scalar_is_text(self, tmp_path: Path) -> None:
        out = tmp_path / "x.xlsx"
        XlsxFormatter().format("=1+1", MagicMock(spec=Console), output_path=str(out))
        cell = _xlsx_cells(out)[0][0]
        assert (cell.data_type, cell.value) == ("s", "=1+1")

    def test_view_sheets_are_text(self, tmp_path: Path) -> None:
        out = tmp_path / "x.xlsx"
        config = OutputConfig(views=[ViewConfig(name="items", path="items")])
        XlsxFormatter().format(
            {"items": [{"a": "@SUM(1)"}]}, MagicMock(spec=Console), config, str(out)
        )
        cell = _xlsx_cells(out, "items")[1][0]
        assert (cell.data_type, cell.value) == ("s", "@SUM(1)")
