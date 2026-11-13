"""Tests for multi-view rendering in output formatters."""

import csv
import datetime
import decimal
import io
import json
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console
from rich.table import Table

from graftpunk.plugins.cli_plugin import CommandResult
from graftpunk.plugins.export import json_to_pdf
from graftpunk.plugins.formatters import (
    CsvFormatter,
    JsonFormatter,
    OutputFormatter,
    PdfFormatter,
    RawFormatter,
    TableFormatter,
    XlsxFormatter,
    format_output,
)


def _parse_csv_output(console: MagicMock) -> list[list[str]]:
    """Extract and parse CSV output from a mock console."""
    output = console.file.write.call_args[0][0]
    return list(csv.reader(io.StringIO(output)))


class TestTableFormatterMultiView:
    """Tests for TableFormatter multi-view rendering."""

    def test_single_view_no_section_header(self) -> None:
        """A single view renders without a Rule separator."""
        from rich.rule import Rule

        from graftpunk.plugins import OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        data = {"results": [{"id": 1, "name": "foo"}]}
        cfg = OutputConfig(
            views=[ViewConfig(name="items", path="results", title="Items")],
        )
        TableFormatter().format(data, console, output_config=cfg)
        # Should print exactly one thing: the table
        console.print.assert_called_once()
        table = console.print.call_args[0][0]
        assert isinstance(table, Table)
        # No Rule should have been printed
        for call in console.print.call_args_list:
            args = call[0]
            for arg in args:
                assert not isinstance(arg, Rule)

    def test_multiple_views_renders_all_with_headers(self) -> None:
        """Multiple views each get a Rule header and a Table."""
        from rich.rule import Rule

        from graftpunk.plugins import OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        data = {
            "items": [{"id": 1, "name": "foo"}],
            "page": {"current": 1, "total": 5},
        }
        cfg = OutputConfig(
            views=[
                ViewConfig(name="items", path="items", title="Results"),
                ViewConfig(name="page", path="page", title="Pagination"),
            ],
        )
        TableFormatter().format(data, console, output_config=cfg)
        # Collect all printed args
        rules = []
        tables = []
        for call in console.print.call_args_list:
            args = call[0]
            for arg in args:
                if isinstance(arg, Rule):
                    rules.append(arg)
                elif isinstance(arg, Table):
                    tables.append(arg)
        assert len(rules) == 2
        assert len(tables) == 2
        assert rules[0].title == "Results"
        assert rules[1].title == "Pagination"

    def test_empty_view_data_skipped(self) -> None:
        """A view whose path yields None is silently skipped."""
        from graftpunk.plugins import OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        data = {"items": [{"id": 1}]}
        cfg = OutputConfig(
            views=[
                ViewConfig(name="items", path="items"),
                ViewConfig(name="missing", path="nonexistent"),
            ],
        )
        TableFormatter().format(data, console, output_config=cfg)
        # Only one table rendered (the "missing" view is skipped)
        tables = [
            call[0][0]
            for call in console.print.call_args_list
            if call[0] and isinstance(call[0][0], Table)
        ]
        assert len(tables) == 1

    def test_multi_view_with_column_filter(self) -> None:
        """Column filters apply per-view in multi-view mode."""
        from graftpunk.plugins import ColumnFilter, OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        data = {
            "items": [{"id": 1, "name": "foo", "desc": "long"}],
        }
        cfg = OutputConfig(
            views=[
                ViewConfig(
                    name="items",
                    path="items",
                    columns=ColumnFilter("include", ["id", "name"]),
                ),
            ],
        )
        TableFormatter().format(data, console, output_config=cfg)
        console.print.assert_called_once()
        table = console.print.call_args[0][0]
        assert isinstance(table, Table)
        assert len(table.columns) == 2

    def test_no_config_still_works(self) -> None:
        """No output_config renders same as before (backward compat)."""
        console = MagicMock(spec=Console)
        data = [{"id": 1, "name": "foo"}]
        TableFormatter().format(data, console, output_config=None)
        console.print.assert_called_once()
        table = console.print.call_args[0][0]
        assert isinstance(table, Table)
        assert len(table.columns) == 2
        assert table.row_count == 1

    def test_single_dict_view_renders_key_value_pairs(self) -> None:
        """A view that extracts a single dict renders as key-value table."""
        from graftpunk.plugins import OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        data = {"meta": {"version": "1.0", "status": "ok"}}
        cfg = OutputConfig(
            views=[ViewConfig(name="meta", path="meta")],
        )
        TableFormatter().format(data, console, output_config=cfg)
        console.print.assert_called_once()
        table = console.print.call_args[0][0]
        assert isinstance(table, Table)
        assert table.row_count == 2

    def test_title_fallback_to_name(self) -> None:
        """Multi-view Rule header falls back to view name when title is empty."""
        from rich.rule import Rule

        from graftpunk.plugins import OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        data = {
            "items": [{"id": 1}],
            "page": [{"number": 0}],
        }
        cfg = OutputConfig(
            views=[
                ViewConfig(name="items", path="items"),
                ViewConfig(name="page", path="page"),
            ],
        )
        TableFormatter().format(data, console, output_config=cfg)
        rules = [
            c[0][0] for c in console.print.call_args_list if c[0] and isinstance(c[0][0], Rule)
        ]
        assert len(rules) == 2
        assert rules[0].title == "items"
        assert rules[1].title == "page"

    def test_all_views_empty_logs_warning(self) -> None:
        """When all views yield None data, a warning is logged."""
        from graftpunk.plugins import OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        data = {"other": "value"}
        cfg = OutputConfig(
            views=[
                ViewConfig(name="a", path="missing_a"),
                ViewConfig(name="b", path="missing_b"),
            ],
        )
        with patch("graftpunk.plugins.formatters.LOG") as mock_log:
            TableFormatter().format(data, console, output_config=cfg)
            mock_log.warning.assert_called_once()
            assert mock_log.warning.call_args[0][0] == "multi_view_all_empty"


class TestCsvFormatterMultiView:
    """Tests for CsvFormatter behavior with multi-view OutputConfig."""

    def test_warns_when_multiple_views_unfiltered(self) -> None:
        """CsvFormatter warns to stderr when OutputConfig has >1 view."""
        from graftpunk.plugins import OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        data = {
            "items": [{"id": 1, "name": "foo"}],
            "page": [{"number": 0}],
        }
        cfg = OutputConfig(
            views=[
                ViewConfig(name="items", path="items", title="Items"),
                ViewConfig(name="page", path="page", title="Page Info"),
            ],
            default_view="items",
        )
        with patch("graftpunk.plugins.formatters.gp_console") as mock_gp:
            CsvFormatter().format(data, console, output_config=cfg)
            mock_gp.warn.assert_called_once()
            warning_msg = mock_gp.warn.call_args[0][0]
            assert "items" in warning_msg
            assert "page" in warning_msg
            assert "--view" in warning_msg

    def test_no_warning_for_single_view(self) -> None:
        """CsvFormatter does not warn when OutputConfig has only 1 view."""
        from graftpunk.plugins import OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        data = [{"id": 1, "name": "foo"}]
        cfg = OutputConfig(views=[ViewConfig(name="items", title="Items")])
        with patch("graftpunk.plugins.formatters.gp_console") as mock_gp:
            CsvFormatter().format(data, console, output_config=cfg)
            mock_gp.warn.assert_not_called()

    def test_renders_default_view_when_multiple_views(self) -> None:
        """CsvFormatter renders the default view data, not all views."""
        from graftpunk.plugins import OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        data = {
            "items": [{"id": 1, "name": "foo"}],
            "page": [{"number": 0}],
        }
        cfg = OutputConfig(
            views=[
                ViewConfig(name="items", path="items", title="Items"),
                ViewConfig(name="page", path="page", title="Page Info"),
            ],
            default_view="items",
        )
        with patch("graftpunk.plugins.formatters.gp_console"):
            CsvFormatter().format(data, console, output_config=cfg)
        rows = _parse_csv_output(console)
        assert rows[0] == ["id", "name"]
        assert rows[1] == ["1", "foo"]


class TestXlsxFormatter:
    """Tests for the XlsxFormatter."""

    def test_satisfies_protocol(self) -> None:
        """XlsxFormatter satisfies the OutputFormatter protocol."""
        assert isinstance(XlsxFormatter(), OutputFormatter)

    def test_writes_xlsx_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """XlsxFormatter creates a .xlsx file in the downloads directory."""
        console = MagicMock(spec=Console)
        data = [{"id": 1, "name": "foo"}, {"id": 2, "name": "bar"}]
        monkeypatch.setenv("GP_DOWNLOADS_DIR", str(tmp_path))
        with patch("graftpunk.plugins.formatters.gp_console") as mock_gp:
            XlsxFormatter().format(data, console)
            mock_gp.info.assert_called_once()
            output_msg = mock_gp.info.call_args[0][0]
            assert str(tmp_path) in output_msg
            assert ".xlsx" in output_msg
        xlsx_files = list(tmp_path.glob("*.xlsx"))
        assert len(xlsx_files) == 1

    def test_single_view_creates_named_worksheet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A single view creates one worksheet with the view title."""
        from graftpunk.plugins import OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        data = {"items": [{"id": 1, "name": "foo"}]}
        cfg = OutputConfig(
            views=[ViewConfig(name="items", path="items", title="Products")],
        )
        monkeypatch.setenv("GP_DOWNLOADS_DIR", str(tmp_path))
        with patch("graftpunk.plugins.formatters.gp_console"):
            XlsxFormatter().format(data, console, output_config=cfg)
        xlsx_files = list(tmp_path.glob("*.xlsx"))
        assert len(xlsx_files) == 1

    def test_multiple_views_creates_multiple_worksheets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multiple views each become a separate worksheet."""
        from graftpunk.plugins import OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        data = {
            "items": [{"id": 1, "name": "foo"}],
            "page": [{"number": 0, "totalPages": 5}],
        }
        cfg = OutputConfig(
            views=[
                ViewConfig(name="items", path="items", title="Products"),
                ViewConfig(name="page", path="page", title="Page Info"),
            ],
        )
        monkeypatch.setenv("GP_DOWNLOADS_DIR", str(tmp_path))
        with patch("graftpunk.plugins.formatters.gp_console"):
            XlsxFormatter().format(data, console, output_config=cfg)
        xlsx_files = list(tmp_path.glob("*.xlsx"))
        assert len(xlsx_files) == 1

    def test_empty_data_still_creates_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty list still produces a valid .xlsx file."""
        console = MagicMock(spec=Console)
        monkeypatch.setenv("GP_DOWNLOADS_DIR", str(tmp_path))
        with patch("graftpunk.plugins.formatters.gp_console") as mock_gp:
            XlsxFormatter().format([], console)
            mock_gp.info.assert_called_once()
            output_msg = mock_gp.info.call_args[0][0]
            assert ".xlsx" in output_msg

    def test_single_dict_renders_as_key_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plain dict is rendered as key-value pairs."""
        console = MagicMock(spec=Console)
        data = {"name": "Alice", "age": 30}
        monkeypatch.setenv("GP_DOWNLOADS_DIR", str(tmp_path))
        with patch("graftpunk.plugins.formatters.gp_console"):
            XlsxFormatter().format(data, console)
        xlsx_files = list(tmp_path.glob("*.xlsx"))
        assert len(xlsx_files) == 1

    def test_xlsx_in_builtin_formatters(self) -> None:
        """XlsxFormatter is registered in BUILTIN_FORMATTERS."""
        from graftpunk.plugins.formatters import BUILTIN_FORMATTERS

        assert "xlsx" in BUILTIN_FORMATTERS

    def test_column_filter_applied(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Column filters from OutputConfig are applied to xlsx output."""
        from graftpunk.plugins import ColumnFilter, OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        data = [{"id": 1, "name": "foo", "desc": "long"}]
        cfg = OutputConfig(
            views=[
                ViewConfig(
                    name="default",
                    columns=ColumnFilter("include", ["id", "name"]),
                ),
            ],
        )
        monkeypatch.setenv("GP_DOWNLOADS_DIR", str(tmp_path))
        with patch("graftpunk.plugins.formatters.gp_console"):
            XlsxFormatter().format(data, console, output_config=cfg)
        xlsx_files = list(tmp_path.glob("*.xlsx"))
        assert len(xlsx_files) == 1

    def test_list_of_dicts_content(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify XLSX cell content for a list of dicts."""
        import openpyxl

        console = MagicMock(spec=Console)
        data = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
        monkeypatch.setenv("GP_DOWNLOADS_DIR", str(tmp_path))
        with patch("graftpunk.plugins.formatters.gp_console"):
            XlsxFormatter().format(data, console)
        xlsx_file = next(tmp_path.glob("*.xlsx"))
        wb = openpyxl.load_workbook(xlsx_file)
        ws = wb.active
        assert ws.cell(1, 1).value == "id"
        assert ws.cell(1, 2).value == "name"
        assert ws.cell(2, 1).value == 1
        assert ws.cell(2, 2).value == "Alice"
        assert ws.cell(3, 1).value == 2
        assert ws.cell(3, 2).value == "Bob"
        wb.close()

    def test_multi_view_worksheet_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multiple views create worksheets with correct names and content."""
        import openpyxl

        from graftpunk.plugins import OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        data = {
            "items": [{"id": 1, "name": "foo"}],
            "meta": {"version": "2.0"},
        }
        cfg = OutputConfig(
            views=[
                ViewConfig(name="items", path="items", title="Products"),
                ViewConfig(name="meta", path="meta", title="Metadata"),
            ],
        )
        monkeypatch.setenv("GP_DOWNLOADS_DIR", str(tmp_path))
        with patch("graftpunk.plugins.formatters.gp_console"):
            XlsxFormatter().format(data, console, output_config=cfg)
        xlsx_file = next(tmp_path.glob("*.xlsx"))
        wb = openpyxl.load_workbook(xlsx_file)
        assert wb.sheetnames == ["Products", "Metadata"]
        ws_items = wb["Products"]
        assert ws_items.cell(1, 1).value == "id"
        assert ws_items.cell(2, 1).value == 1
        ws_meta = wb["Metadata"]
        assert ws_meta.cell(1, 1).value == "Key"
        assert ws_meta.cell(2, 1).value == "version"
        assert ws_meta.cell(2, 2).value == "2.0"
        wb.close()

    def test_sheet_name_truncated_to_31_chars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Worksheet name is truncated to Excel's 31-character limit."""
        import openpyxl

        from graftpunk.plugins import OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        long_title = "A" * 50
        data = {"items": [{"id": 1}]}
        cfg = OutputConfig(
            views=[ViewConfig(name="items", path="items", title=long_title)],
        )
        monkeypatch.setenv("GP_DOWNLOADS_DIR", str(tmp_path))
        with patch("graftpunk.plugins.formatters.gp_console"):
            XlsxFormatter().format(data, console, output_config=cfg)
        xlsx_file = next(tmp_path.glob("*.xlsx"))
        wb = openpyxl.load_workbook(xlsx_file)
        assert wb.sheetnames == ["A" * 31]
        wb.close()

    def test_empty_view_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Views whose path yields None are skipped (no empty worksheet)."""
        import openpyxl

        from graftpunk.plugins import OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        data = {"items": [{"id": 1}]}
        cfg = OutputConfig(
            views=[
                ViewConfig(name="items", path="items", title="Items"),
                ViewConfig(name="missing", path="nonexistent", title="Missing"),
            ],
        )
        monkeypatch.setenv("GP_DOWNLOADS_DIR", str(tmp_path))
        with patch("graftpunk.plugins.formatters.gp_console"):
            XlsxFormatter().format(data, console, output_config=cfg)
        xlsx_file = next(tmp_path.glob("*.xlsx"))
        wb = openpyxl.load_workbook(xlsx_file)
        assert wb.sheetnames == ["Items"]
        wb.close()


class TestFormatOutputViewArgs:
    """Tests for format_output with --view filtering."""

    def test_view_args_filters_to_single_view(self) -> None:
        """format_output with view_args filters OutputConfig to one view."""
        from graftpunk.plugins import OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        data = {
            "items": [{"id": 1, "name": "foo"}],
            "page": [{"number": 0}],
        }
        cfg = OutputConfig(
            views=[
                ViewConfig(name="items", path="items", title="Items"),
                ViewConfig(name="page", path="page", title="Page Info"),
            ],
        )
        result = CommandResult(data=data, output_config=cfg, format_hint="table")
        format_output(result, "table", console, view_args=("items",))
        # Should render only one table (items), not two
        printed_args = [c[0][0] for c in console.print.call_args_list]
        tables = [a for a in printed_args if isinstance(a, Table)]
        assert len(tables) == 1

    def test_view_args_with_column_override(self) -> None:
        """format_output with view_args applies column override."""
        from graftpunk.plugins import OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        data = [{"id": 1, "name": "foo", "desc": "long"}]
        cfg = OutputConfig(
            views=[ViewConfig(name="default")],
        )
        result = CommandResult(data=data, output_config=cfg, format_hint="table")
        format_output(result, "table", console, view_args=("default:id,name",))
        printed_args = [c[0][0] for c in console.print.call_args_list]
        tables = [a for a in printed_args if isinstance(a, Table)]
        assert len(tables) == 1
        assert len(tables[0].columns) == 2  # Only id, name

    def test_view_args_empty_tuple_renders_all(self) -> None:
        """Empty view_args renders all views (default behavior)."""
        from graftpunk.plugins import OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        data = {
            "items": [{"id": 1}],
            "page": [{"number": 0}],
        }
        cfg = OutputConfig(
            views=[
                ViewConfig(name="items", path="items", title="Items"),
                ViewConfig(name="page", path="page", title="Page Info"),
            ],
        )
        result = CommandResult(data=data, output_config=cfg, format_hint="table")
        format_output(result, "table", console, view_args=())
        printed_args = [c[0][0] for c in console.print.call_args_list]
        tables = [a for a in printed_args if isinstance(a, Table)]
        assert len(tables) == 2

    def test_view_args_no_output_config_warns(self) -> None:
        """view_args with no output_config warns and still renders."""
        console = MagicMock(spec=Console)
        result = CommandResult(data={"key": "value"})
        with patch("graftpunk.plugins.formatters.gp_console") as mock_gp:
            format_output(result, "table", console, view_args=("items",))
            mock_gp.warn.assert_called_once()
        console.print.assert_called()

    def test_nonexistent_view_name_logs_warning(self) -> None:
        """format_output with a nonexistent view name triggers a warning log."""
        from graftpunk.plugins import OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        cfg = OutputConfig(views=[ViewConfig(name="items", path="items")])
        result = CommandResult(
            data={"items": [{"id": 1}]},
            output_config=cfg,
            format_hint="table",
        )
        with patch("graftpunk.plugins.output_config.LOG") as mock_log:
            format_output(result, "table", console, view_args=("nonexistent",))
            mock_log.warning.assert_called()
            assert mock_log.warning.call_args[0][0] == "filter_views_unknown"

    def test_backward_compat_no_view_args(self) -> None:
        """format_output without view_args still works (backward compat)."""
        console = MagicMock(spec=Console)
        result = CommandResult(data={"key": "value"})
        format_output(result, "json", console)
        console.print.assert_called_once()


class TestXlsxNonStringKeysAndValues:
    """#310: a key that is not a string, or a value xlsxwriter cannot store,
    raised TypeError and wrote nothing."""

    def _write(self, tmp_path: Path, data: object) -> Any:
        import openpyxl

        out = tmp_path / "out.xlsx"
        with patch("graftpunk.plugins.formatters.gp_console"):
            XlsxFormatter().format(data, MagicMock(spec=Console), output_path=str(out))
        return openpyxl.load_workbook(out).active

    def test_a_list_of_dicts_keyed_by_number_none_and_tuple(self, tmp_path: Path) -> None:
        ws = self._write(tmp_path, [{1: "a", None: "b", (1, 2): "c"}, {1: "d", None: "e"}])
        assert [ws.cell(1, col).value for col in (1, 2, 3)] == ["1", "None", "(1, 2)"]
        assert [ws.cell(2, col).value for col in (1, 2, 3)] == ["a", "b", "c"]
        assert [ws.cell(3, col).value for col in (1, 2)] == ["d", "e"]

    def test_a_tuple_set_or_object_value_writes_as_text(self, tmp_path: Path) -> None:
        class Opaque:
            def __str__(self) -> str:
                return "opaque"

        ws = self._write(tmp_path, [{"t": (1, 2), "s": {3}, "o": Opaque(), "n": 2.5}])
        assert [ws.cell(2, col).value for col in (1, 2, 3, 4)] == ["[1, 2]", "{3}", "opaque", 2.5]

    def test_the_single_dict_path_writes_the_same_values(self, tmp_path: Path) -> None:
        ws = self._write(tmp_path, {1: (1, 2), "s": {3}})
        assert [ws.cell(2, col).value for col in (1, 2)] == ["1", "[1, 2]"]
        assert [ws.cell(3, col).value for col in (1, 2)] == ["s", "{3}"]


def test_pdf_renders_a_list_of_dicts_keyed_by_number_none_and_tuple(tmp_path: Path) -> None:
    """#310's sibling: fpdf2 takes only text, and a non-string key raised."""
    out = tmp_path / "out.pdf"
    path, pages = json_to_pdf([{1: "a", None: "b", (1, 2): "c"}], out)  # type: ignore[dict-item]
    assert pages == 1
    assert Path(path).read_bytes().startswith(b"%PDF")


def _table_text(data: object, output_config: Any = None) -> str:
    buf = io.StringIO()
    TableFormatter().format(data, Console(file=buf, width=200), output_config=output_config)
    return buf.getvalue()


def _xlsx_rows(tmp_path: Path, data: object, output_config: Any = None) -> list[list[object]]:
    import openpyxl

    out = tmp_path / "out.xlsx"
    with patch("graftpunk.plugins.formatters.gp_console"):
        XlsxFormatter().format(
            data, MagicMock(spec=Console), output_config=output_config, output_path=str(out)
        )
    ws = openpyxl.load_workbook(out).active
    return [[cell.value for cell in row] for row in ws.iter_rows()]


def _csv_rows(data: object) -> list[list[str]]:
    console = MagicMock(spec=Console)
    CsvFormatter().format(data, console)
    return _parse_csv_output(console)


class TestEveryRowsColumns:
    """The table and xlsx formatters took their columns from the first row only,
    so a key that first appears in a later row was dropped; csv and pdf took
    every row's, in first-seen order. All four now agree."""

    _DATA: ClassVar[list[dict[str, int]]] = [{"a": 1}, {"a": 2, "b": 3}, {"c": 4}]

    def test_table(self) -> None:
        header = _table_text(self._DATA).splitlines()[1]
        assert [cell.strip() for cell in header.strip("┃").split("┃")] == ["a", "b", "c"]

    def test_xlsx(self, tmp_path: Path) -> None:
        assert _xlsx_rows(tmp_path, self._DATA) == [
            ["a", "b", "c"],
            [1, None, None],
            [2, 3, None],
            [None, None, 4],
        ]

    def test_csv(self) -> None:
        assert _csv_rows(self._DATA)[0] == ["a", "b", "c"]

    def test_pdf(self, tmp_path: Path) -> None:
        from graftpunk.plugins.export import ordered_keys, table_rows

        assert ordered_keys(table_rows(self._DATA)) == ["a", "b", "c"]
        _, pages = json_to_pdf(self._DATA, tmp_path / "out.pdf")
        assert pages == 1


class TestNonDictRows:
    """A list mixing dicts and other values raised from the table, xlsx and pdf
    formatters (and from a view's column filter), the pdf formatter raised on
    a list of strings, and the xlsx formatter wrote an empty sheet for one.
    One rule now: a non-dict row is the one-column row ``{"value": item}``."""

    _MIXED: ClassVar[list[object]] = [{"a": 1}, "loose", 7]

    def test_table_writes_the_value_column(self) -> None:
        text = _table_text(self._MIXED)
        assert "value" in text
        assert "loose" in text
        assert "│ 7 " in text

    def test_xlsx_writes_the_value_column(self, tmp_path: Path) -> None:
        assert _xlsx_rows(tmp_path, self._MIXED) == [
            ["a", "value"],
            [1, None],
            [None, "loose"],
            [None, 7],
        ]

    def test_xlsx_writes_a_list_of_strings(self, tmp_path: Path) -> None:
        assert _xlsx_rows(tmp_path, ["x", "y"]) == [["value"], ["x"], ["y"]]

    def test_csv_writes_the_value_column(self) -> None:
        assert _csv_rows(self._MIXED) == [["a", "value"], ["1", ""], ["", "loose"], ["", "7"]]

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param([{"a": 1}, "loose", 7], id="mixed"),
            pytest.param(["x", "y"], id="strings"),
            pytest.param([1, [2, 3], None], id="scalars-and-a-list"),
        ],
    )
    def test_pdf_renders(self, tmp_path: Path, data: list[object]) -> None:
        out = tmp_path / "out.pdf"
        with patch("graftpunk.plugins.formatters.gp_console"):
            PdfFormatter().format(data, MagicMock(spec=Console), output_path=str(out))
        assert out.read_bytes().startswith(b"%PDF-")

    def test_a_view_filter_keeps_or_drops_the_value_column(self, tmp_path: Path) -> None:
        from graftpunk.plugins import ColumnFilter, OutputConfig, ViewConfig

        keep = OutputConfig(
            views=[ViewConfig(name="v", columns=ColumnFilter("include", ("value",)))]
        )
        assert _xlsx_rows(tmp_path, self._MIXED, keep) == [["value"], [None], ["loose"], [7]]
        drop = OutputConfig(views=[ViewConfig(name="v", columns=ColumnFilter("include", ("a",)))])
        assert "loose" not in _table_text(self._MIXED, drop)

    def test_a_list_with_no_dict_still_prints_as_json_in_the_table_format(self) -> None:
        assert json.loads(_table_text(["x", "y"])) == ["x", "y"]


def test_a_nested_list_of_dicts_holding_a_date_exports(tmp_path: Path) -> None:
    """flatten_dict wrote such a list with json.dumps and no default, which
    raised TypeError from json_to_csv and the pdf export."""
    import datetime

    from graftpunk.plugins.export import json_to_csv

    data = [{"a": [{"t": datetime.date(2026, 1, 1)}]}]
    path, _ = json_to_csv(data, tmp_path / "out.csv")
    assert "2026-01-01" in Path(path).read_text()
    json_to_pdf(data, tmp_path / "out.pdf")


_NESTED_KEYS: list[Any] = [
    [{"a": {(1, 2): 1}}],
    [{"a": [{(1, 2): 1}]}],
    {"a": {(1, 2): 1}},
    {"a": [{frozenset({"x"}): {(3,): 2}}]},
]


class TestNestedNonStringKeys:
    """A dict key JSON cannot write (a tuple, a frozenset) nested in a value
    raised ``TypeError: keys must be str...`` wherever a value was written as
    JSON text: ``default=`` is never asked about a key."""

    @pytest.mark.parametrize("data", _NESTED_KEYS)
    def test_json(self, data: Any) -> None:
        buf = io.StringIO()
        JsonFormatter().format(data, Console(file=buf))
        text = buf.getvalue()
        assert json.loads(text)
        assert "(1, 2)" in text or "frozenset" in text

    @pytest.mark.parametrize("data", _NESTED_KEYS)
    def test_raw(self, data: Any) -> None:
        console = MagicMock(spec=Console)
        RawFormatter().format(data, console)
        assert json.loads(console.file.write.call_args[0][0])

    @pytest.mark.parametrize("data", _NESTED_KEYS)
    def test_csv(self, data: Any) -> None:
        text = "\n".join(",".join(row) for row in _csv_rows(data))
        assert "(1, 2)" in text or "frozenset" in text

    @pytest.mark.parametrize("data", _NESTED_KEYS)
    def test_table(self, data: Any) -> None:
        assert "(1, 2)" in _table_text(data) or "frozenset" in _table_text(data)

    @pytest.mark.parametrize("data", _NESTED_KEYS)
    def test_xlsx(self, tmp_path: Path, data: Any) -> None:
        cells = [str(c) for row in _xlsx_rows(tmp_path, data) for c in row]
        assert any("(1, 2)" in c or "frozenset" in c for c in cells)

    @pytest.mark.parametrize("data", _NESTED_KEYS)
    def test_pdf_and_json_to_csv(self, tmp_path: Path, data: Any) -> None:
        from graftpunk.plugins.export import json_to_csv

        rows = data if isinstance(data, list) else [data]
        json_to_pdf(rows, tmp_path / "out.pdf")
        path, _ = json_to_csv(rows, tmp_path / "out.csv")
        assert "(1, 2)" in Path(path).read_text() or "frozenset" in Path(path).read_text()

    def test_two_keys_that_read_alike_are_both_kept(self) -> None:
        from graftpunk.plugins.export import json_text

        assert json.loads(json_text({(1, 2): "a", "(1, 2)": "b"})) == {
            "(1, 2)": "a",
            "(1, 2) (2)": "b",
        }


class TestXlsxValuesExcelCannotHold:
    """xlsxwriter raised on NaN, an infinity, ``Decimal("NaN")``, a datetime or
    time with a UTC offset, and an int too large for a float, and a lone
    surrogate raised ``UnicodeEncodeError``. Each is written as text now."""

    _CASES: ClassVar[list[tuple[Any, Any]]] = [
        (float("nan"), "nan"),
        (float("inf"), "inf"),
        (float("-inf"), "-inf"),
        (decimal.Decimal("NaN"), "NaN"),
        (decimal.Decimal("1e400"), "1E+400"),
        (10**400, str(10**400)),
        (
            datetime.datetime(2026, 1, 2, 3, 4, tzinfo=datetime.UTC),
            "2026-01-02T03:04:00+00:00",
        ),
        (datetime.time(3, 4, tzinfo=datetime.UTC), "03:04:00+00:00"),
        ("a\ud800b", "a�b"),
    ]

    @pytest.mark.parametrize(("value", "written"), _CASES)
    def test_the_list_path(self, tmp_path: Path, value: Any, written: Any) -> None:
        assert _xlsx_rows(tmp_path, [{"v": value}]) == [["v"], [written]]

    @pytest.mark.parametrize(("value", "written"), _CASES)
    def test_the_single_dict_path(self, tmp_path: Path, value: Any, written: Any) -> None:
        assert _xlsx_rows(tmp_path, {"v": value})[1] == ["v", written]

    def test_a_finite_number_and_a_naive_datetime_stay_native(self, tmp_path: Path) -> None:
        rows = _xlsx_rows(
            tmp_path,
            [{"n": 2.5, "d": decimal.Decimal("1.25"), "t": datetime.datetime(2026, 1, 2)}],
        )
        # A naive datetime stays an Excel date serial number, not text.
        assert rows[1][:2] == [2.5, 1.25]
        assert not isinstance(rows[1][2], str)


class TestXlsxSheetNames:
    """A view title Excel refuses as a sheet name, or two titles alike in their
    first 31 characters, made xlsxwriter raise."""

    def test_refused_characters_and_repeats(self, tmp_path: Path) -> None:
        import openpyxl

        from graftpunk.plugins import OutputConfig, ViewConfig

        cfg = OutputConfig(
            views=[
                ViewConfig(name="a", path="a", title="Orders [2026/01]: *?"),
                ViewConfig(name="b", path="b", title="x" * 40),
                ViewConfig(name="c", path="c", title="X" * 35),
                ViewConfig(name="d", path="d", title="'"),
            ]
        )
        out = tmp_path / "out.xlsx"
        data = {"a": [{"k": 1}], "b": [{"k": 2}], "c": [{"k": 3}], "d": [{"k": 4}]}
        with patch("graftpunk.plugins.formatters.gp_console"):
            XlsxFormatter().format(data, MagicMock(spec=Console), cfg, str(out))
        assert openpyxl.load_workbook(out).sheetnames == [
            "Orders _2026_01__ __",
            "x" * 31,
            "X" * 27 + " (2)",
            "Sheet",
        ]

    @pytest.mark.parametrize(
        ("titles", "names"),
        [
            pytest.param(["a" * 30 + "'b"], ["a" * 30], id="apostrophe-at-the-31st"),
            pytest.param(["a" * 28 + "'''x"], ["a" * 28], id="apostrophes-at-the-cut"),
            pytest.param(
                ["x" * 26 + "'yyyy", "x" * 26 + "'yyyy"],
                ["x" * 26 + "'yyyy", "x" * 26 + " (2)"],
                id="apostrophe-before-a-numbered-suffix",
            ),
            pytest.param(
                ["z" * 26 + "'", "z" * 26 + "'"], ["z" * 26, "z" * 26 + " (2)"], id="numbered"
            ),
            pytest.param(["'''"], ["Sheet"], id="only-apostrophes"),
        ],
    )
    def test_a_cut_never_leaves_an_apostrophe_at_either_end(
        self, tmp_path: Path, titles: list[str], names: list[str]
    ) -> None:
        """Stripping ``'`` before the cut to 31 characters let the cut leave one
        at the end, and xlsxwriter raised."""
        import openpyxl

        from graftpunk.plugins import OutputConfig, ViewConfig

        cfg = OutputConfig(
            views=[ViewConfig(name=f"v{i}", path=f"v{i}", title=t) for i, t in enumerate(titles)]
        )
        out = tmp_path / "out.xlsx"
        data = {f"v{i}": [{"k": i}] for i in range(len(titles))}
        with patch("graftpunk.plugins.formatters.gp_console"):
            XlsxFormatter().format(data, MagicMock(spec=Console), cfg, str(out))
        assert openpyxl.load_workbook(out).sheetnames == names


class TestLoneSurrogatesInCsvAndRaw:
    """``json.loads('"\\\\ud800"')`` makes a lone surrogate, which no UTF-8 file
    or stream can hold: csv and raw output raised ``UnicodeEncodeError``."""

    def test_csv_formatter_to_a_file(self, tmp_path: Path) -> None:
        out = tmp_path / "out.csv"
        with patch("graftpunk.plugins.formatters.gp_console"):
            CsvFormatter().format(
                [{"k\ud800": "a\ud800b"}], MagicMock(spec=Console), None, str(out)
            )
        assert out.read_text(encoding="utf-8") == "k�\na�b\n"

    def test_raw_formatter_to_a_stream(self) -> None:
        buf = io.BytesIO()
        stream = io.TextIOWrapper(buf, encoding="utf-8")
        RawFormatter().format("a\ud800b", Console(file=stream))
        stream.flush()
        assert buf.getvalue() == "a�b\n".encode()

    def test_json_to_csv(self, tmp_path: Path) -> None:
        from graftpunk.plugins.export import json_to_csv

        path, _ = json_to_csv([{"k\ud800": "a\ud800b"}], tmp_path / "out.csv")
        assert Path(path).read_text(encoding="utf-8").splitlines() == ["k�", "a�b"]

    def test_a_surrogate_pair_becomes_its_character(self) -> None:
        from graftpunk.console import visible_text

        assert visible_text("😀") == "\U0001f600"
        assert visible_text("plain") == "plain"


class TestViewFilterOnAListWithNoDict:
    """A view's column filter was skipped for a list with no dict, so xlsx and
    pdf wrote the value column a filter excluded; it now applies everywhere."""

    def _cfg(self, mode: str, cols: tuple[str, ...]) -> Any:
        from graftpunk.plugins import ColumnFilter, OutputConfig, ViewConfig

        return OutputConfig(views=[ViewConfig(name="v", columns=ColumnFilter(mode, cols))])

    def test_xlsx_drops_the_value_column_an_include_filter_does_not_name(
        self, tmp_path: Path
    ) -> None:
        assert _xlsx_rows(tmp_path, ["x", "y"], self._cfg("include", ("a",))) == []

    def test_xlsx_keeps_it_when_named(self, tmp_path: Path) -> None:
        rows = _xlsx_rows(tmp_path, ["x", "y"], self._cfg("include", ("value",)))
        assert rows == [["value"], ["x"], ["y"]]

    def test_csv_and_table_apply_it_too(self) -> None:
        console = MagicMock(spec=Console)
        CsvFormatter().format(["x", "y"], console, self._cfg("include", ("value",)))
        assert _parse_csv_output(console) == [["value"], ["x"], ["y"]]
        text = _table_text(["x", "y"], self._cfg("exclude", ("value",)))
        assert "value" not in text
        assert "y" not in text

    def test_pdf_applies_it(self, tmp_path: Path) -> None:
        out = tmp_path / "out.pdf"
        with patch("graftpunk.plugins.formatters.gp_console"):
            PdfFormatter().format(
                ["xЖ"], MagicMock(spec=Console), self._cfg("exclude", ("value",)), str(out)
            )
        # U+0416 would be named in the pdf's ToUnicode map had the cell been drawn.
        assert b"<0416>" not in out.read_bytes()


class _StrRaises:
    """A value whose ``str`` raises and whose ``repr`` works."""

    def __str__(self) -> str:
        raise RuntimeError("no str")

    def __repr__(self) -> str:
        return "StrRaises()"


class _NoText:
    """A value whose ``str`` and ``repr`` both raise."""

    def __repr__(self) -> str:
        raise RuntimeError("no repr")

    __str__ = __repr__


_BIG = 10**5000
_HARD_VALUES: list[Any] = [
    pytest.param(_BIG, "<unprintable int>", id="int-past-4300-digits"),
    pytest.param(_StrRaises(), "StrRaises()", id="str-raises"),
    pytest.param(_NoText(), "<unprintable _NoText>", id="no-text"),
    pytest.param("a\ud800b", "a�b", id="lone-surrogate"),
]


def _strict_utf8_console() -> tuple[Console, io.BytesIO]:
    """A console writing to a strict UTF-8 stream, as Python's stdout is."""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="utf-8", errors="strict")
    return Console(file=stream, width=200), raw


class TestEverySiteWritesAnyValue:
    """``str()`` raised for an int past Python's 4300-digit limit and for a
    value whose ``__str__`` raises, and a lone surrogate raised writing to a
    UTF-8 file or stream, at each formatter's cell, heading and value sites.
    One helper, ``safe_text``, gives each its text now."""

    @pytest.mark.parametrize(("value", "shown"), _HARD_VALUES)
    def test_table_cells_headings_and_the_single_dict_path_to_a_pipe(
        self, value: Any, shown: str
    ) -> None:
        for data in ([{"k": value}], {"k": value}, [{_StrKey(value): 1}]):
            console, raw = _strict_utf8_console()
            TableFormatter().format(data, console)
            console.file.flush()
            text = raw.getvalue().decode("utf-8")
            # A key whose own str and repr fail is its stand-in, named for its type.
            assert shown in text or "<unprintable _StrKey>" in text

    @pytest.mark.parametrize(("value", "shown"), _HARD_VALUES)
    def test_table_to_a_file(self, tmp_path: Path, value: Any, shown: str) -> None:
        out = tmp_path / "out.txt"
        TableFormatter().format([{"k": value}], MagicMock(spec=Console), output_path=str(out))
        assert shown in out.read_text(encoding="utf-8")

    @pytest.mark.parametrize(("value", "shown"), _HARD_VALUES)
    def test_csv_formatter(self, value: Any, shown: str) -> None:
        assert _csv_rows([{"k": value}])[1] == [shown]

    @pytest.mark.parametrize(("value", "shown"), _HARD_VALUES)
    def test_json_to_csv(self, tmp_path: Path, value: Any, shown: str) -> None:
        from graftpunk.plugins.export import json_to_csv

        path, _ = json_to_csv([{"k": value}], tmp_path / "out.csv")
        assert Path(path).read_text(encoding="utf-8").splitlines()[1] == shown

    @pytest.mark.parametrize(("value", "shown"), _HARD_VALUES)
    def test_xlsx(self, tmp_path: Path, value: Any, shown: str) -> None:
        assert _xlsx_rows(tmp_path, [{"k": value}])[1] == [shown]
        assert _xlsx_rows(tmp_path, {"k": value})[1] == ["k", shown]

    @pytest.mark.parametrize(("value", "shown"), _HARD_VALUES)
    def test_pdf_cells_headings_and_metadata(self, tmp_path: Path, value: Any, shown: str) -> None:
        path, pages = json_to_pdf(
            [{"k": value, _StrKey(value): 1}], tmp_path / "out.pdf", metadata={"m": value}
        )
        assert pages == 1

    @pytest.mark.parametrize(("value", "shown"), _HARD_VALUES)
    def test_json_and_raw(self, value: Any, shown: str) -> None:
        from graftpunk.plugins.export import json_text

        # JSON escapes a lone surrogate (\\ud800), which reads back as written.
        expected = value if isinstance(value, str) else shown
        assert json.loads(json_text({"k": [value]})) == {"k": [expected]}
        buf = io.StringIO()
        RawFormatter().format({"k": value}, Console(file=buf))
        assert json.loads(buf.getvalue()) == {"k": expected}

    def test_a_container_that_holds_itself_is_written_as_its_loop_mark(self) -> None:
        from graftpunk.plugins.export import json_text

        loop: list[Any] = [1]
        loop.append(loop)
        node: dict[str, Any] = {}
        node["self"] = node
        assert json.loads(json_text({"l": loop, "d": node})) == {
            "l": [1, "[...]"],
            "d": {"self": "{...}"},
        }


class _StrKey:
    """A hashable key whose text is the given value's."""

    def __init__(self, value: Any) -> None:
        self.value = value

    def __hash__(self) -> int:
        return 7

    def __str__(self) -> str:
        return str(self.value)

    def __repr__(self) -> str:
        return repr(self.value)
