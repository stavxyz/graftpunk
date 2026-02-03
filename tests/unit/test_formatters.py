"""Tests for output formatters."""

import json
from unittest.mock import MagicMock, patch

from rich.console import Console
from rich.json import JSON
from rich.table import Table

from graftpunk.plugins.cli_plugin import CommandResult
from graftpunk.plugins.formatters import (
    BUILTIN_FORMATTERS,
    JsonFormatter,
    OutputFormatter,
    RawFormatter,
    TableFormatter,
    discover_formatters,
    format_output,
)


class TestOutputFormatterProtocol:
    """Tests for the OutputFormatter protocol."""

    def test_json_formatter_satisfies_protocol(self) -> None:
        assert isinstance(JsonFormatter(), OutputFormatter)

    def test_table_formatter_satisfies_protocol(self) -> None:
        assert isinstance(TableFormatter(), OutputFormatter)

    def test_raw_formatter_satisfies_protocol(self) -> None:
        assert isinstance(RawFormatter(), OutputFormatter)

    def test_builtin_formatters_has_all_three(self) -> None:
        assert "json" in BUILTIN_FORMATTERS
        assert "table" in BUILTIN_FORMATTERS
        assert "raw" in BUILTIN_FORMATTERS

    def test_custom_class_satisfies_protocol(self) -> None:
        class CsvFormatter:
            name = "csv"

            def format(self, data: object, console: Console) -> None:
                console.print("csv output")

        assert isinstance(CsvFormatter(), OutputFormatter)


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
