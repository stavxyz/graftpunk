"""Tests for output formatters."""

import csv
import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console
from rich.json import JSON
from rich.table import Table

from graftpunk.plugins.cli_plugin import CommandResult
from graftpunk.plugins.formatters import (
    BUILTIN_FORMATTERS,
    CsvFormatter,
    JsonFormatter,
    OutputFormatter,
    PdfFormatter,
    RawFormatter,
    TableFormatter,
    XlsxFormatter,
    discover_formatters,
    format_output,
)


def _parse_csv_output(console: MagicMock) -> list[list[str]]:
    """Extract and parse CSV output from a mock console."""
    output = console.print.call_args[0][0]
    return list(csv.reader(io.StringIO(output)))


class TestOutputFormatterProtocol:
    """Tests for the OutputFormatter protocol."""

    def test_json_formatter_satisfies_protocol(self) -> None:
        assert isinstance(JsonFormatter(), OutputFormatter)

    def test_table_formatter_satisfies_protocol(self) -> None:
        assert isinstance(TableFormatter(), OutputFormatter)

    def test_raw_formatter_satisfies_protocol(self) -> None:
        assert isinstance(RawFormatter(), OutputFormatter)

    def test_csv_formatter_satisfies_protocol(self) -> None:
        assert isinstance(CsvFormatter(), OutputFormatter)

    def test_pdf_formatter_satisfies_protocol(self) -> None:
        assert isinstance(PdfFormatter(), OutputFormatter)

    def test_builtin_formatters_has_all_six(self) -> None:
        assert "json" in BUILTIN_FORMATTERS
        assert "table" in BUILTIN_FORMATTERS
        assert "raw" in BUILTIN_FORMATTERS
        assert "csv" in BUILTIN_FORMATTERS
        assert "xlsx" in BUILTIN_FORMATTERS
        assert "pdf" in BUILTIN_FORMATTERS

    def test_custom_class_satisfies_protocol(self) -> None:
        class YamlFormatter:
            name = "yaml"

            def format(self, data: object, console: Console) -> None:
                console.print("yaml output")

        assert isinstance(YamlFormatter(), OutputFormatter)


class TestFormatOutput:
    def test_dispatches_to_json_formatter(self) -> None:
        console = MagicMock(spec=Console)
        data = {"key": "value"}
        format_output(data, "json", console)
        # Should have called console.print with a JSON object
        console.print.assert_called_once()
        arg = console.print.call_args[0][0]
        assert isinstance(arg, JSON)

    def test_dispatches_to_table_formatter(self) -> None:
        console = MagicMock(spec=Console)
        data = {"key": "value"}
        format_output(data, "table", console)
        console.print.assert_called_once()
        arg = console.print.call_args[0][0]
        assert isinstance(arg, Table)

    def test_dispatches_to_raw_formatter(self) -> None:
        console = MagicMock(spec=Console)
        format_output("hello", "raw", console)
        console.print.assert_called_once_with("hello")

    def test_dispatches_to_csv_formatter(self) -> None:
        console = MagicMock(spec=Console)
        data = [{"name": "Alice", "age": "30"}]
        format_output(data, "csv", console)
        console.print.assert_called_once()
        rows = _parse_csv_output(console)
        assert rows[0] == ["name", "age"]

    def test_unknown_format_raises_value_error(self) -> None:
        """Unknown format names raise ValueError instead of silent fallback."""
        console = MagicMock(spec=Console)
        data = {"key": "value"}
        with pytest.raises(ValueError, match="Unknown output format 'nonexistent'"):
            format_output(data, "nonexistent", console)


class TestJsonFormatter:
    def test_formats_dict_as_json(self) -> None:
        console = MagicMock(spec=Console)
        data = {"name": "test", "value": 42}
        JsonFormatter().format(data, console)
        console.print.assert_called_once()
        arg = console.print.call_args[0][0]
        assert isinstance(arg, JSON)

    def test_formats_list_as_json(self) -> None:
        console = MagicMock(spec=Console)
        data = [1, 2, 3]
        JsonFormatter().format(data, console)
        console.print.assert_called_once()
        arg = console.print.call_args[0][0]
        assert isinstance(arg, JSON)


class TestTableFormatter:
    def test_list_of_dicts_creates_column_table(self) -> None:
        console = MagicMock(spec=Console)
        data = [
            {"name": "Alice", "age": "30"},
            {"name": "Bob", "age": "25"},
        ]
        TableFormatter().format(data, console)
        console.print.assert_called_once()
        table = console.print.call_args[0][0]
        assert isinstance(table, Table)
        assert len(table.columns) == 2
        assert table.columns[0].header == "name"
        assert table.columns[1].header == "age"
        assert table.row_count == 2

    def test_single_dict_creates_key_value_table(self) -> None:
        console = MagicMock(spec=Console)
        data = {"name": "Alice", "age": 30}
        TableFormatter().format(data, console)
        console.print.assert_called_once()
        table = console.print.call_args[0][0]
        assert isinstance(table, Table)
        assert table.row_count == 2

    def test_single_dict_with_nested_dict_value(self) -> None:
        console = MagicMock(spec=Console)
        data = {"name": "Alice", "meta": {"role": "admin"}}
        TableFormatter().format(data, console)
        console.print.assert_called_once()
        table = console.print.call_args[0][0]
        assert isinstance(table, Table)
        assert table.row_count == 2

    def test_single_dict_with_nested_list_value(self) -> None:
        console = MagicMock(spec=Console)
        data = {"name": "Alice", "tags": ["a", "b"]}
        TableFormatter().format(data, console)
        console.print.assert_called_once()
        table = console.print.call_args[0][0]
        assert isinstance(table, Table)
        assert table.row_count == 2

    def test_non_dict_non_list_falls_back_to_json(self) -> None:
        console = MagicMock(spec=Console)
        TableFormatter().format("just a string", console)
        console.print.assert_called_once()
        arg = console.print.call_args[0][0]
        assert isinstance(arg, JSON)

    def test_empty_list_falls_back_to_json(self) -> None:
        console = MagicMock(spec=Console)
        TableFormatter().format([], console)
        console.print.assert_called_once()
        arg = console.print.call_args[0][0]
        assert isinstance(arg, JSON)

    def test_list_of_non_dicts_falls_back_to_json(self) -> None:
        console = MagicMock(spec=Console)
        TableFormatter().format([1, 2, 3], console)
        console.print.assert_called_once()
        arg = console.print.call_args[0][0]
        assert isinstance(arg, JSON)

    def test_list_of_dicts_with_missing_keys(self) -> None:
        console = MagicMock(spec=Console)
        data = [
            {"name": "Alice", "age": "30"},
            {"name": "Bob"},
        ]
        TableFormatter().format(data, console)
        console.print.assert_called_once()
        table = console.print.call_args[0][0]
        assert isinstance(table, Table)
        assert table.row_count == 2


class TestRawFormatter:
    def test_string_data_printed_directly(self) -> None:
        console = MagicMock(spec=Console)
        RawFormatter().format("hello world", console)
        console.print.assert_called_once_with("hello world")

    def test_non_string_data_json_dumped(self) -> None:
        console = MagicMock(spec=Console)
        data = {"key": "value"}
        RawFormatter().format(data, console)
        console.print.assert_called_once_with(json.dumps(data, default=str))

    def test_list_data_json_dumped(self) -> None:
        console = MagicMock(spec=Console)
        data = [1, 2, 3]
        RawFormatter().format(data, console)
        console.print.assert_called_once_with(json.dumps(data, default=str))

    def test_integer_data_json_dumped(self) -> None:
        console = MagicMock(spec=Console)
        RawFormatter().format(42, console)
        console.print.assert_called_once_with(json.dumps(42, default=str))


class TestCsvFormatter:
    def test_list_of_dicts_produces_csv(self) -> None:
        console = MagicMock(spec=Console)
        data = [{"name": "Alice", "age": "30"}, {"name": "Bob", "age": "25"}]
        CsvFormatter().format(data, console)
        console.print.assert_called_once()
        rows = _parse_csv_output(console)
        assert rows[0] == ["name", "age"]
        assert rows[1] == ["Alice", "30"]
        assert rows[2] == ["Bob", "25"]
        assert len(rows) == 3
        # Verify end="" is passed to avoid double-newlining
        assert console.print.call_args[1].get("end") == ""

    def test_single_dict_produces_single_row_csv(self) -> None:
        console = MagicMock(spec=Console)
        data = {"name": "Alice", "age": "30"}
        CsvFormatter().format(data, console)
        console.print.assert_called_once()
        rows = _parse_csv_output(console)
        assert rows[0] == ["name", "age"]
        assert rows[1] == ["Alice", "30"]
        assert len(rows) == 2

    def test_string_passthrough(self) -> None:
        console = MagicMock(spec=Console)
        csv_str = "name,age\nAlice,30\n"
        CsvFormatter().format(csv_str, console)
        console.print.assert_called_once_with(csv_str)

    def test_nested_dict_json_serialized(self) -> None:
        console = MagicMock(spec=Console)
        data = [{"name": "Alice", "meta": {"role": "admin"}}]
        CsvFormatter().format(data, console)
        rows = _parse_csv_output(console)
        assert rows[1][1] == json.dumps({"role": "admin"})

    def test_nested_list_json_serialized(self) -> None:
        console = MagicMock(spec=Console)
        data = [{"name": "Alice", "tags": ["a", "b"]}]
        CsvFormatter().format(data, console)
        rows = _parse_csv_output(console)
        assert rows[1][1] == json.dumps(["a", "b"])

    def test_empty_list_produces_empty_output(self) -> None:
        console = MagicMock(spec=Console)
        CsvFormatter().format([], console)
        console.print.assert_not_called()

    def test_non_dict_non_list_non_string_falls_back(self) -> None:
        console = MagicMock(spec=Console)
        CsvFormatter().format(42, console)
        console.print.assert_called_once_with(json.dumps(42, default=str))

    def test_list_of_non_dicts_falls_back_to_raw(self) -> None:
        console = MagicMock(spec=Console)
        CsvFormatter().format([1, 2, 3], console)
        console.print.assert_called_once_with(json.dumps([1, 2, 3], default=str))

    def test_mixed_type_list_falls_back_to_raw(self) -> None:
        """A list mixing dicts and non-dicts falls back rather than crashing."""
        console = MagicMock(spec=Console)
        data = [{"name": "Alice"}, "not a dict", {"name": "Bob"}]
        CsvFormatter().format(data, console)
        console.print.assert_called_once_with(json.dumps(data, default=str))

    def test_list_of_dicts_with_missing_keys(self) -> None:
        console = MagicMock(spec=Console)
        data = [{"name": "Alice", "age": "30"}, {"name": "Bob"}]
        CsvFormatter().format(data, console)
        rows = _parse_csv_output(console)
        assert rows[0] == ["name", "age"]
        assert rows[1] == ["Alice", "30"]
        assert rows[2] == ["Bob", ""]

    def test_union_headers_from_all_rows(self) -> None:
        """Headers include keys from all rows, not just the first."""
        console = MagicMock(spec=Console)
        data = [{"name": "Alice"}, {"name": "Bob", "age": "25", "email": "bob@x.com"}]
        CsvFormatter().format(data, console)
        rows = _parse_csv_output(console)
        assert rows[0] == ["name", "age", "email"]
        assert rows[1] == ["Alice", "", ""]
        assert rows[2] == ["Bob", "25", "bob@x.com"]

    def test_values_with_special_chars_are_escaped(self) -> None:
        """Commas and quotes in values are properly escaped by csv.writer."""
        console = MagicMock(spec=Console)
        data = [{"name": 'Alice "The Great"', "bio": "likes commas, and stuff"}]
        CsvFormatter().format(data, console)
        rows = _parse_csv_output(console)
        assert rows[1][0] == 'Alice "The Great"'
        assert rows[1][1] == "likes commas, and stuff"


class TestCommandResultUnwrapping:
    """Tests that format_output unwraps CommandResult before formatting."""

    def test_unwraps_command_result_for_json(self) -> None:
        """CommandResult.data is extracted before JSON formatting."""
        console = MagicMock(spec=Console)
        result = CommandResult(data={"key": "value"}, metadata={"page": 1})
        format_output(result, "json", console)
        console.print.assert_called_once()
        arg = console.print.call_args[0][0]
        assert isinstance(arg, JSON)

    def test_unwraps_command_result_for_table(self) -> None:
        """CommandResult.data is extracted before table formatting."""
        console = MagicMock(spec=Console)
        result = CommandResult(data={"name": "Alice", "age": 30})
        format_output(result, "table", console)
        console.print.assert_called_once()
        arg = console.print.call_args[0][0]
        assert isinstance(arg, Table)

    def test_unwraps_command_result_for_csv(self) -> None:
        """CommandResult.data is extracted before CSV formatting."""
        console = MagicMock(spec=Console)
        result = CommandResult(data=[{"name": "Alice"}])
        format_output(result, "csv", console)
        console.print.assert_called_once()
        rows = _parse_csv_output(console)
        assert rows[0] == ["name"]
        assert rows[1] == ["Alice"]

    def test_unwraps_command_result_for_raw(self) -> None:
        """CommandResult.data is extracted before raw formatting."""
        console = MagicMock(spec=Console)
        result = CommandResult(data="hello world")
        format_output(result, "raw", console)
        console.print.assert_called_once_with("hello world")

    def test_raw_data_still_works(self) -> None:
        """Non-CommandResult data passes through unchanged."""
        console = MagicMock(spec=Console)
        format_output({"key": "value"}, "json", console)
        console.print.assert_called_once()
        arg = console.print.call_args[0][0]
        assert isinstance(arg, JSON)


class TestDiscoverFormatters:
    """Tests for discover_formatters with mocked entry points."""

    def test_returns_builtins_when_no_entry_points(self) -> None:
        formatters = discover_formatters()
        assert "json" in formatters
        assert "table" in formatters
        assert "raw" in formatters

    def test_entry_point_formatter_is_merged(self) -> None:
        """A third-party formatter registered via entry points is discovered."""

        class YamlFormatter:
            name = "yaml"

            def format(self, data: object, console: Console) -> None:
                console.print("yaml")

        mock_ep = MagicMock()
        mock_ep.name = "yaml"
        mock_ep.load.return_value = YamlFormatter

        with patch(
            "graftpunk.plugins.formatters.importlib.metadata.entry_points",
            return_value=[mock_ep],
        ):
            formatters = discover_formatters()

        assert "yaml" in formatters
        assert formatters["yaml"].name == "yaml"
        # Builtins still present
        assert "json" in formatters

    def test_entry_point_failure_is_logged_not_raised(self) -> None:
        """A failing entry point does not prevent other formatters from loading."""
        mock_ep = MagicMock()
        mock_ep.name = "broken"
        mock_ep.load.side_effect = ImportError("nope")

        with patch(
            "graftpunk.plugins.formatters.importlib.metadata.entry_points",
            return_value=[mock_ep],
        ):
            formatters = discover_formatters()

        # Builtins still present, broken one is absent
        assert "json" in formatters
        assert "broken" not in formatters

    def test_entry_point_can_override_builtin(self) -> None:
        """An entry-point formatter can override a built-in by name."""

        class CustomJson:
            name = "json"

            def format(self, data: object, console: Console) -> None:
                console.print("custom json")

        mock_ep = MagicMock()
        mock_ep.name = "json"
        mock_ep.load.return_value = CustomJson

        with patch(
            "graftpunk.plugins.formatters.importlib.metadata.entry_points",
            return_value=[mock_ep],
        ):
            formatters = discover_formatters()

        assert isinstance(formatters["json"], CustomJson)

    def test_entry_point_non_protocol_skipped(self) -> None:
        """An entry-point formatter that doesn't match OutputFormatter is skipped."""

        class NotAFormatter:
            """Missing name property and format method."""

            pass

        mock_ep = MagicMock()
        mock_ep.name = "bad"
        mock_ep.load.return_value = NotAFormatter

        with patch(
            "graftpunk.plugins.formatters.importlib.metadata.entry_points",
            return_value=[mock_ep],
        ):
            formatters = discover_formatters()

        assert "bad" not in formatters
        # Builtins still present
        assert "json" in formatters


class TestFormatPrecedence:
    """Tests for format selection precedence: explicit flag > hint > default."""

    def test_explicit_format_overrides_hint(self) -> None:
        """When user specifies --format table, format_hint is ignored."""
        console = MagicMock(spec=Console)
        result = CommandResult(data={"key": "value"}, format_hint="raw")
        format_output(result, "table", console, user_explicit=True)
        console.print.assert_called_once()
        arg = console.print.call_args[0][0]
        assert isinstance(arg, Table)

    def test_explicit_json_overrides_table_hint(self) -> None:
        """When user explicitly passes -f json, format_hint='table' is ignored."""
        console = MagicMock(spec=Console)
        result = CommandResult(data={"key": "value"}, format_hint="table")
        format_output(result, "json", console, user_explicit=True)
        console.print.assert_called_once()
        arg = console.print.call_args[0][0]
        assert isinstance(arg, JSON)

    def test_hint_applies_when_format_is_default(self) -> None:
        """When user did not pass --format, hint takes effect."""
        console = MagicMock(spec=Console)
        result = CommandResult(data={"name": "Alice", "age": 30}, format_hint="table")
        format_output(result, "json", console, user_explicit=False)
        console.print.assert_called_once()
        arg = console.print.call_args[0][0]
        assert isinstance(arg, Table)

    def test_hint_csv_applies_when_format_is_default(self) -> None:
        """When user did not pass --format, csv hint takes effect."""
        console = MagicMock(spec=Console)
        result = CommandResult(data=[{"a": "1"}], format_hint="csv")
        format_output(result, "json", console, user_explicit=False)
        console.print.assert_called_once()
        rows = _parse_csv_output(console)
        assert rows[0] == ["a"]
        assert rows[1] == ["1"]

    def test_hint_ignored_when_format_is_explicit(self) -> None:
        """When user explicitly picks raw, hint is ignored even if set."""
        console = MagicMock(spec=Console)
        result = CommandResult(data="hello", format_hint="table")
        format_output(result, "raw", console, user_explicit=True)
        console.print.assert_called_once_with("hello")

    def test_no_hint_uses_requested_format(self) -> None:
        """Without a format_hint, the requested format is used as-is."""
        console = MagicMock(spec=Console)
        result = CommandResult(data={"key": "value"})
        format_output(result, "json", console)
        console.print.assert_called_once()
        arg = console.print.call_args[0][0]
        assert isinstance(arg, JSON)

    def test_unknown_hint_falls_through_safely(self) -> None:
        """An unrecognized format_hint does not crash — falls through to default."""
        console = MagicMock(spec=Console)
        result = CommandResult(data={"key": "value"}, format_hint="nonexistent")  # type: ignore[arg-type]
        format_output(result, "json", console, user_explicit=False)
        console.print.assert_called_once()
        arg = console.print.call_args[0][0]
        assert isinstance(arg, JSON)

    def test_backward_compat_user_explicit_kwarg_optional(self) -> None:
        """Calling format_output without user_explicit still applies hint."""
        console = MagicMock(spec=Console)
        result = CommandResult(data={"key": "value"}, format_hint="table")
        # Omit user_explicit entirely — defaults to False, so hint applies
        format_output(result, "json", console)
        console.print.assert_called_once()
        arg = console.print.call_args[0][0]
        assert isinstance(arg, Table)


class TestTableFormatterWithOutputConfig:
    """Tests for TableFormatter with OutputConfig column filtering."""

    def test_applies_column_filter(self) -> None:
        from graftpunk.plugins import ColumnFilter, OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        data = [{"id": 1, "name": "foo", "desc": "long"}]
        cfg = OutputConfig(
            views=[ViewConfig(name="default", columns=ColumnFilter("include", ["id", "name"]))],
        )
        TableFormatter().format(data, console, output_config=cfg)
        console.print.assert_called_once()
        table = console.print.call_args[0][0]
        assert isinstance(table, Table)
        assert len(table.columns) == 2

    def test_no_config_uses_all_columns(self) -> None:
        console = MagicMock(spec=Console)
        data = [{"id": 1, "name": "foo", "desc": "long"}]
        TableFormatter().format(data, console, output_config=None)
        console.print.assert_called_once()
        table = console.print.call_args[0][0]
        assert len(table.columns) == 3

    def test_applies_path_extraction(self) -> None:
        from graftpunk.plugins import OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        data = {"results": [{"id": 1, "name": "foo"}]}
        cfg = OutputConfig(
            views=[ViewConfig(name="default", path="results")],
        )
        TableFormatter().format(data, console, output_config=cfg)
        console.print.assert_called_once()
        table = console.print.call_args[0][0]
        assert isinstance(table, Table)
        assert len(table.columns) == 2  # id, name


class TestCsvFormatterWithOutputConfig:
    """Tests for CsvFormatter with OutputConfig column filtering."""

    def test_applies_column_filter(self) -> None:
        from graftpunk.plugins import ColumnFilter, OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        data = [{"id": 1, "name": "foo", "desc": "long"}]
        cfg = OutputConfig(
            views=[ViewConfig(name="default", columns=ColumnFilter("include", ["id", "name"]))],
        )
        CsvFormatter().format(data, console, output_config=cfg)
        rows = _parse_csv_output(console)
        assert rows[0] == ["id", "name"]

    def test_no_config_uses_all_columns(self) -> None:
        console = MagicMock(spec=Console)
        data = [{"id": 1, "name": "foo", "desc": "long"}]
        CsvFormatter().format(data, console, output_config=None)
        rows = _parse_csv_output(console)
        assert rows[0] == ["id", "name", "desc"]

    def test_applies_path_extraction(self) -> None:
        from graftpunk.plugins import OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        data = {"results": {"items": [{"id": 1, "name": "foo"}]}}
        cfg = OutputConfig(
            views=[ViewConfig(name="default", path="results.items")],
        )
        CsvFormatter().format(data, console, output_config=cfg)
        rows = _parse_csv_output(console)
        assert rows[0] == ["id", "name"]
        assert rows[1] == ["1", "foo"]

    def test_uses_default_view(self) -> None:
        from graftpunk.plugins import ColumnFilter, OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        data = [{"id": 1, "name": "foo", "type": "bar"}]
        cfg = OutputConfig(
            views=[
                ViewConfig(name="first", columns=ColumnFilter("include", ["id"])),
                ViewConfig(name="second", columns=ColumnFilter("include", ["name"])),
            ],
            default_view="second",
        )
        CsvFormatter().format(data, console, output_config=cfg)
        rows = _parse_csv_output(console)
        assert rows[0] == ["name"]
        assert rows[1] == ["foo"]


class TestFormatOutputWithOutputConfig:
    """Tests for format_output extracting and passing OutputConfig."""

    def test_extracts_output_config_from_command_result(self) -> None:
        from graftpunk.plugins import ColumnFilter, OutputConfig, ViewConfig

        console = MagicMock(spec=Console)
        cfg = OutputConfig(
            views=[ViewConfig(name="default", columns=ColumnFilter("include", ["id"]))],
        )
        result = CommandResult(
            data=[{"id": 1, "name": "foo"}],
            output_config=cfg,
        )
        format_output(result, "table", console)
        console.print.assert_called_once()
        table = console.print.call_args[0][0]
        assert len(table.columns) == 1  # Only "id"

    def test_raw_data_without_config_works(self) -> None:
        console = MagicMock(spec=Console)
        format_output([{"id": 1, "name": "foo"}], "table", console)
        console.print.assert_called_once()
        table = console.print.call_args[0][0]
        assert len(table.columns) == 2


class TestPdfFormatter:
    """Tests for PdfFormatter."""

    def test_name_is_pdf(self) -> None:
        assert PdfFormatter().name == "pdf"

    def test_satisfies_output_formatter_protocol(self) -> None:
        assert isinstance(PdfFormatter(), OutputFormatter)

    def test_registered_in_builtin_formatters(self) -> None:
        assert "pdf" in BUILTIN_FORMATTERS
        assert isinstance(BUILTIN_FORMATTERS["pdf"], PdfFormatter)

    def test_discovered_by_discover_formatters(self) -> None:
        formatters = discover_formatters()
        assert "pdf" in formatters
        assert formatters["pdf"].name == "pdf"

    def test_list_of_dicts_produces_pdf_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Formatting a list of dicts creates a valid PDF file."""
        monkeypatch.setenv("GP_DOWNLOADS_DIR", str(tmp_path))
        console = MagicMock(spec=Console)
        data = [{"name": "Alice", "age": "30"}, {"name": "Bob", "age": "25"}]

        PdfFormatter().format(data, console)

        pdf_files = list(tmp_path.glob("*.pdf"))
        assert len(pdf_files) == 1
        content = pdf_files[0].read_bytes()
        assert content[:5] == b"%PDF-"

    def test_single_dict_produces_pdf_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Formatting a single dict wraps it into a list and creates a PDF."""
        monkeypatch.setenv("GP_DOWNLOADS_DIR", str(tmp_path))
        console = MagicMock(spec=Console)
        data = {"name": "Alice", "age": "30"}

        PdfFormatter().format(data, console)

        pdf_files = list(tmp_path.glob("*.pdf"))
        assert len(pdf_files) == 1
        content = pdf_files[0].read_bytes()
        assert content[:5] == b"%PDF-"

    def test_empty_list_produces_pdf_with_no_data(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Formatting an empty list still creates a PDF (with 'No data' text)."""
        monkeypatch.setenv("GP_DOWNLOADS_DIR", str(tmp_path))
        console = MagicMock(spec=Console)

        PdfFormatter().format([], console)

        pdf_files = list(tmp_path.glob("*.pdf"))
        assert len(pdf_files) == 1
        content = pdf_files[0].read_bytes()
        assert content[:5] == b"%PDF-"

    def test_non_dict_non_list_falls_back_to_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-dict/list data falls back to JSON output, no PDF file created."""
        monkeypatch.setenv("GP_DOWNLOADS_DIR", str(tmp_path))
        console = MagicMock(spec=Console)

        PdfFormatter().format("just a string", console)

        pdf_files = list(tmp_path.glob("*.pdf"))
        assert len(pdf_files) == 0
        # Should have printed JSON to console instead
        console.print.assert_called_once()
        arg = console.print.call_args[0][0]
        assert isinstance(arg, JSON)

    def test_integer_falls_back_to_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Integer data falls back to JSON output, no PDF file created."""
        monkeypatch.setenv("GP_DOWNLOADS_DIR", str(tmp_path))
        console = MagicMock(spec=Console)

        PdfFormatter().format(42, console)

        pdf_files = list(tmp_path.glob("*.pdf"))
        assert len(pdf_files) == 0
        console.print.assert_called_once()

    def test_applies_view_data_extraction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OutputConfig view path extraction is applied before rendering."""
        from graftpunk.plugins import OutputConfig, ViewConfig

        monkeypatch.setenv("GP_DOWNLOADS_DIR", str(tmp_path))
        console = MagicMock(spec=Console)
        data = {"results": [{"id": 1, "name": "foo"}]}
        cfg = OutputConfig(views=[ViewConfig(name="default", path="results")])

        PdfFormatter().format(data, console, output_config=cfg)

        pdf_files = list(tmp_path.glob("*.pdf"))
        assert len(pdf_files) == 1
        content = pdf_files[0].read_bytes()
        assert content[:5] == b"%PDF-"


class TestOutputPath:
    """Tests for the --output/-o file output functionality."""

    def test_json_to_file(self, tmp_path: Path) -> None:
        """JsonFormatter with output_path writes JSON to file."""
        outfile = tmp_path / "out.json"
        console = MagicMock(spec=Console)
        data = {"name": "Alice", "age": 30}

        JsonFormatter().format(data, console, output_path=str(outfile))

        assert outfile.exists()
        content = outfile.read_text()
        # The file should contain the JSON data (Rich JSON rendering
        # includes syntax highlighting markup, but the data is present)
        assert "Alice" in content
        assert "30" in content
        # Console mock should NOT have been called (file console used instead)
        console.print.assert_not_called()

    def test_table_to_file(self, tmp_path: Path) -> None:
        """TableFormatter with output_path writes table to file."""
        outfile = tmp_path / "out.txt"
        console = MagicMock(spec=Console)
        data = [{"name": "Alice", "age": "30"}, {"name": "Bob", "age": "25"}]

        TableFormatter().format(data, console, output_path=str(outfile))

        assert outfile.exists()
        content = outfile.read_text()
        assert "Alice" in content
        assert "Bob" in content
        console.print.assert_not_called()

    def test_csv_to_file(self, tmp_path: Path) -> None:
        """CsvFormatter with output_path writes CSV to file."""
        outfile = tmp_path / "out.csv"
        console = MagicMock(spec=Console)
        data = [{"name": "Alice", "age": "30"}, {"name": "Bob", "age": "25"}]

        CsvFormatter().format(data, console, output_path=str(outfile))

        assert outfile.exists()
        content = outfile.read_text()
        rows = list(csv.reader(io.StringIO(content)))
        assert rows[0] == ["name", "age"]
        assert rows[1] == ["Alice", "30"]
        assert rows[2] == ["Bob", "25"]
        # Console should not be used for output
        console.print.assert_not_called()

    def test_raw_to_file(self, tmp_path: Path) -> None:
        """RawFormatter with output_path writes text to file."""
        outfile = tmp_path / "out.txt"
        console = MagicMock(spec=Console)

        RawFormatter().format("hello world", console, output_path=str(outfile))

        assert outfile.exists()
        content = outfile.read_text()
        assert "hello world" in content
        console.print.assert_not_called()

    def test_xlsx_uses_output_path(self, tmp_path: Path) -> None:
        """XlsxFormatter with output_path uses the provided path."""
        outfile = tmp_path / "custom.xlsx"
        console = MagicMock(spec=Console)
        data = [{"name": "Alice", "age": "30"}]

        XlsxFormatter().format(data, console, output_path=str(outfile))

        assert outfile.exists()
        assert outfile.stat().st_size > 0

    def test_pdf_uses_output_path(self, tmp_path: Path) -> None:
        """PdfFormatter with output_path uses the provided path."""
        outfile = tmp_path / "custom.pdf"
        console = MagicMock(spec=Console)
        data = [{"name": "Alice", "age": "30"}]

        PdfFormatter().format(data, console, output_path=str(outfile))

        assert outfile.exists()
        content = outfile.read_bytes()
        assert content[:5] == b"%PDF-"

    def test_xlsx_auto_generates_without_output_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """XlsxFormatter without output_path auto-generates in downloads dir."""
        monkeypatch.setenv("GP_DOWNLOADS_DIR", str(tmp_path))
        console = MagicMock(spec=Console)
        data = [{"name": "Alice", "age": "30"}]

        XlsxFormatter().format(data, console)

        xlsx_files = list(tmp_path.glob("*.xlsx"))
        assert len(xlsx_files) == 1

    def test_pdf_auto_generates_without_output_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PdfFormatter without output_path auto-generates in downloads dir."""
        monkeypatch.setenv("GP_DOWNLOADS_DIR", str(tmp_path))
        console = MagicMock(spec=Console)
        data = [{"name": "Alice", "age": "30"}]

        PdfFormatter().format(data, console)

        pdf_files = list(tmp_path.glob("*.pdf"))
        assert len(pdf_files) == 1

    def test_format_output_passes_output_path(self, tmp_path: Path) -> None:
        """format_output passes output_path through to the formatter."""
        outfile = tmp_path / "out.csv"
        console = MagicMock(spec=Console)
        data = [{"name": "Alice"}]

        format_output(data, "csv", console, output_path=str(outfile))

        assert outfile.exists()
        content = outfile.read_text()
        rows = list(csv.reader(io.StringIO(content)))
        assert rows[0] == ["name"]
        assert rows[1] == ["Alice"]

    def test_output_path_creates_parent_dirs(self, tmp_path: Path) -> None:
        """output_path creates parent directories if they don't exist."""
        outfile = tmp_path / "subdir" / "nested" / "out.json"
        console = MagicMock(spec=Console)
        data = {"key": "value"}

        JsonFormatter().format(data, console, output_path=str(outfile))

        assert outfile.exists()
        assert "value" in outfile.read_text()

    def test_output_path_empty_string_uses_console(self) -> None:
        """Empty output_path string means output goes to console (default)."""
        console = MagicMock(spec=Console)
        data = {"key": "value"}

        JsonFormatter().format(data, console, output_path="")

        console.print.assert_called_once()
        arg = console.print.call_args[0][0]
        assert isinstance(arg, JSON)

    def test_csv_string_passthrough_to_file(self, tmp_path: Path) -> None:
        """CsvFormatter string passthrough with output_path writes to file."""
        outfile = tmp_path / "out.csv"
        console = MagicMock(spec=Console)
        csv_str = "name,age\nAlice,30\n"

        CsvFormatter().format(csv_str, console, output_path=str(outfile))

        assert outfile.exists()
        assert "Alice" in outfile.read_text()
        console.print.assert_not_called()

    def test_csv_fallback_to_file(self, tmp_path: Path) -> None:
        """CsvFormatter non-dict data with output_path falls back to file."""
        outfile = tmp_path / "out.txt"
        console = MagicMock(spec=Console)

        CsvFormatter().format(42, console, output_path=str(outfile))

        assert outfile.exists()
        assert "42" in outfile.read_text()
        console.print.assert_not_called()

    def test_pdf_fallback_to_file(self, tmp_path: Path) -> None:
        """PdfFormatter non-list data with output_path falls back to JSON file."""
        outfile = tmp_path / "out.json"
        console = MagicMock(spec=Console)

        PdfFormatter().format("just a string", console, output_path=str(outfile))

        assert outfile.exists()
        content = outfile.read_text()
        assert "just a string" in content
        # No PDF file should be created
        assert not list(tmp_path.glob("*.pdf"))
        console.print.assert_not_called()


class TestFormatOverrides:
    """Tests for the three-level format override hierarchy."""

    def test_command_result_format_overrides(self) -> None:
        """Per-command format_overrides on CommandResult take highest priority."""

        class CustomTable:
            name = "table"

            def format(
                self,
                data: object,
                console: Console,
                output_config: object = None,
                output_path: str = "",
            ) -> None:
                console.print("custom-table-output")

        console = MagicMock(spec=Console)
        result = CommandResult(
            data={"key": "value"},
            format_overrides={"table": CustomTable()},
        )
        format_output(result, "table", console)
        console.print.assert_called_once_with("custom-table-output")

    def test_plugin_formatters_override_core(self) -> None:
        """Plugin-wide formatters override core formatters."""

        class CustomRaw:
            name = "raw"

            def format(
                self,
                data: object,
                console: Console,
                output_config: object = None,
                output_path: str = "",
            ) -> None:
                console.print("plugin-raw-output")

        console = MagicMock(spec=Console)
        format_output(
            "hello",
            "raw",
            console,
            plugin_formatters={"raw": CustomRaw()},
        )
        console.print.assert_called_once_with("plugin-raw-output")

    def test_command_overrides_beat_plugin_overrides(self) -> None:
        """Per-command overrides beat plugin-wide overrides."""

        class PluginJson:
            name = "json"

            def format(
                self,
                data: object,
                console: Console,
                output_config: object = None,
                output_path: str = "",
            ) -> None:
                console.print("plugin-json")

        class CommandJson:
            name = "json"

            def format(
                self,
                data: object,
                console: Console,
                output_config: object = None,
                output_path: str = "",
            ) -> None:
                console.print("command-json")

        console = MagicMock(spec=Console)
        result = CommandResult(
            data={"key": "value"},
            format_overrides={"json": CommandJson()},
        )
        format_output(
            result,
            "json",
            console,
            plugin_formatters={"json": PluginJson()},
        )
        console.print.assert_called_once_with("command-json")

    def test_format_hint_with_override_only_format(self) -> None:
        """format_hint can reference a format only available via overrides."""

        class MarkdownFormatter:
            name = "markdown"

            def format(
                self,
                data: object,
                console: Console,
                output_config: object = None,
                output_path: str = "",
            ) -> None:
                console.print("# markdown output")

        console = MagicMock(spec=Console)
        result = CommandResult(
            data={"key": "value"},
            format_hint="markdown",
            format_overrides={"markdown": MarkdownFormatter()},
        )
        format_output(result, "json", console, user_explicit=False)
        console.print.assert_called_once_with("# markdown output")
