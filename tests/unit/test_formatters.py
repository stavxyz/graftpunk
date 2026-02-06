"""Tests for output formatters."""

import csv
import io
import json
from unittest.mock import MagicMock, patch

from rich.console import Console
from rich.json import JSON
from rich.table import Table

from graftpunk.plugins.cli_plugin import CommandResult
from graftpunk.plugins.formatters import (
    BUILTIN_FORMATTERS,
    CsvFormatter,
    JsonFormatter,
    OutputFormatter,
    RawFormatter,
    TableFormatter,
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

    def test_builtin_formatters_has_all_four(self) -> None:
        assert "json" in BUILTIN_FORMATTERS
        assert "table" in BUILTIN_FORMATTERS
        assert "raw" in BUILTIN_FORMATTERS
        assert "csv" in BUILTIN_FORMATTERS

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

    def test_unknown_format_falls_back_to_json(self) -> None:
        """Unknown format names fall back to the json formatter."""
        console = MagicMock(spec=Console)
        data = {"key": "value"}
        format_output(data, "nonexistent", console)
        console.print.assert_called_once()
        arg = console.print.call_args[0][0]
        assert isinstance(arg, JSON)


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
        format_output(result, "table", console)
        console.print.assert_called_once()
        arg = console.print.call_args[0][0]
        assert isinstance(arg, Table)

    def test_hint_applies_when_format_is_json_default(self) -> None:
        """When format_type is json (default), hint takes effect."""
        console = MagicMock(spec=Console)
        result = CommandResult(data={"name": "Alice", "age": 30}, format_hint="table")
        format_output(result, "json", console)
        console.print.assert_called_once()
        arg = console.print.call_args[0][0]
        # Hint says "table" and format_type is "json" (default), so table is used
        assert isinstance(arg, Table)

    def test_hint_csv_applies_when_format_is_json_default(self) -> None:
        """When format_type is json (default), csv hint takes effect."""
        console = MagicMock(spec=Console)
        result = CommandResult(data=[{"a": "1"}], format_hint="csv")
        format_output(result, "json", console)
        console.print.assert_called_once()
        rows = _parse_csv_output(console)
        assert rows[0] == ["a"]
        assert rows[1] == ["1"]

    def test_hint_ignored_when_format_is_explicit_non_json(self) -> None:
        """When user explicitly picks raw, hint is ignored even if set."""
        console = MagicMock(spec=Console)
        result = CommandResult(data="hello", format_hint="table")
        format_output(result, "raw", console)
        console.print.assert_called_once_with("hello")

    def test_no_hint_uses_requested_format(self) -> None:
        """Without a format_hint, the requested format is used as-is."""
        console = MagicMock(spec=Console)
        result = CommandResult(data={"key": "value"})
        format_output(result, "json", console)
        console.print.assert_called_once()
        arg = console.print.call_args[0][0]
        assert isinstance(arg, JSON)


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
