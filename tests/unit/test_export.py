"""Tests for export utilities and CommandResult.export() API."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from graftpunk.client import GraftpunkClient, execute_plugin_command
from graftpunk.plugins.cli_plugin import CommandContext, CommandResult, CommandSpec
from graftpunk.plugins.export import (
    flatten_dict,
    get_downloads_dir,
    json_to_csv,
    json_to_pdf,
    ordered_keys,
)
from graftpunk.plugins.formatters import (
    CsvFormatter,
    JsonFormatter,
    OutputFormatter,
    PdfFormatter,
    RawFormatter,
    TableFormatter,
    XlsxFormatter,
)
from graftpunk.plugins.output_config import OutputConfig, ViewConfig


class TestFlattenDict:
    def test_flat_dict_unchanged(self) -> None:
        d = {"a": 1, "b": "hello", "c": 3.14}
        result = flatten_dict(d)
        assert result == {"a": 1, "b": "hello", "c": 3.14}

    def test_nested_dict_uses_dot_notation(self) -> None:
        d = {"a": {"b": 1, "c": 2}}
        result = flatten_dict(d)
        assert result == {"a.b": 1, "a.c": 2}

    def test_deeply_nested(self) -> None:
        d = {"a": {"b": {"c": {"d": 42}}}}
        result = flatten_dict(d)
        assert result == {"a.b.c.d": 42}

    def test_custom_separator(self) -> None:
        d = {"a": {"b": 1}}
        result = flatten_dict(d, sep="_")
        assert result == {"a_b": 1}

    def test_custom_parent_key(self) -> None:
        d = {"b": 1}
        result = flatten_dict(d, parent_key="a")
        assert result == {"a.b": 1}

    def test_none_values_preserved(self) -> None:
        d = {"a": None, "b": {"c": None}}
        result = flatten_dict(d)
        assert result == {"a": None, "b.c": None}

    def test_empty_dict_returns_empty(self) -> None:
        assert flatten_dict({}) == {}

    def test_list_of_primitives_joined(self) -> None:
        d = {"tags": ["red", "green", "blue"]}
        result = flatten_dict(d)
        assert result == {"tags": "red, green, blue"}

    def test_list_of_dicts_becomes_json_string(self) -> None:
        d = {"items": [{"id": 1}, {"id": 2}]}
        result = flatten_dict(d)
        assert result["items"] == json.dumps([{"id": 1}, {"id": 2}])

    def test_empty_list_becomes_empty_string(self) -> None:
        d = {"items": []}
        result = flatten_dict(d)
        assert result == {"items": ""}

    def test_bool_values_preserved(self) -> None:
        d = {"active": True, "deleted": False}
        result = flatten_dict(d)
        assert result == {"active": True, "deleted": False}

    def test_mixed_nested_and_flat(self) -> None:
        d = {
            "name": "test",
            "meta": {"version": 1, "tags": ["a", "b"]},
            "active": True,
        }
        result = flatten_dict(d)
        assert result == {
            "name": "test",
            "meta.version": 1,
            "meta.tags": "a, b",
            "active": True,
        }


class TestOrderedKeys:
    def test_single_row(self) -> None:
        rows = [{"a": 1, "b": 2}]
        assert ordered_keys(rows) == ["a", "b"]

    def test_superset_of_keys(self) -> None:
        rows = [{"a": 1, "b": 2}, {"b": 3, "c": 4}]
        assert ordered_keys(rows) == ["a", "b", "c"]

    def test_preserves_first_appearance_order(self) -> None:
        rows = [{"z": 1}, {"a": 2}, {"m": 3}]
        assert ordered_keys(rows) == ["z", "a", "m"]

    def test_empty_rows(self) -> None:
        assert ordered_keys([]) == []

    def test_empty_dicts(self) -> None:
        assert ordered_keys([{}, {}]) == []


class TestJsonToCsv:
    def test_basic_csv(self, tmp_path: Path) -> None:
        data = [{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]
        out = tmp_path / "out.csv"
        path, count = json_to_csv(data, out)
        assert count == 2
        with open(path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 2
        assert rows[0]["name"] == "Alice"
        assert rows[1]["age"] == "25"

    def test_nested_dicts_flattened(self, tmp_path: Path) -> None:
        data = [{"user": {"name": "Alice", "age": 30}}]
        out = tmp_path / "out.csv"
        path, count = json_to_csv(data, out)
        assert count == 1
        with open(path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert "user.name" in rows[0]
        assert rows[0]["user.name"] == "Alice"

    def test_flatten_disabled(self, tmp_path: Path) -> None:
        data = [{"user": {"name": "Alice"}}]
        out = tmp_path / "out.csv"
        path, count = json_to_csv(data, out, flatten=False)
        assert count == 1
        with open(path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        # Without flattening, nested dict is written as-is (string repr)
        assert "user" in rows[0]

    def test_empty_data(self, tmp_path: Path) -> None:
        out = tmp_path / "out.csv"
        path, count = json_to_csv([], out)
        assert count == 0
        assert Path(path).exists()
        assert Path(path).read_text() == ""

    def test_superset_columns_from_all_rows(self, tmp_path: Path) -> None:
        data = [{"a": 1, "b": 2}, {"b": 3, "c": 4}]
        out = tmp_path / "out.csv"
        path, _ = json_to_csv(data, out)
        with open(path) as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            rows = list(reader)
        assert fieldnames is not None
        assert "a" in fieldnames
        assert "b" in fieldnames
        assert "c" in fieldnames
        # First row has no "c", should be empty
        assert rows[0]["c"] == ""
        # Second row has no "a", should be empty
        assert rows[1]["a"] == ""

    def test_string_path_accepted(self, tmp_path: Path) -> None:
        data = [{"x": 1}]
        out = str(tmp_path / "out.csv")
        path, count = json_to_csv(data, out)
        assert count == 1
        assert Path(path).exists()

    def test_returns_absolute_path(self, tmp_path: Path) -> None:
        data = [{"x": 1}]
        out = tmp_path / "out.csv"
        path, _ = json_to_csv(data, out)
        assert Path(path).is_absolute()


class TestJsonToPdf:
    def test_basic_pdf(self, tmp_path: Path) -> None:
        data = [{"name": "Alice", "age": 30}]
        out = tmp_path / "out.pdf"
        path, pages = json_to_pdf(data, out)
        assert pages >= 1
        content = Path(path).read_bytes()
        assert content[:5] == b"%PDF-"

    def test_metadata_header_section(self, tmp_path: Path) -> None:
        data = [{"name": "Alice"}]
        out = tmp_path / "out.pdf"
        path, _ = json_to_pdf(data, out, title="Test Report", metadata={"Account": "12345"})
        assert Path(path).exists()
        # PDF was created successfully with metadata -- we verify the file
        # is a valid PDF and non-empty (content verification is visual)
        content = Path(path).read_bytes()
        assert len(content) > 100

    def test_empty_data(self, tmp_path: Path) -> None:
        out = tmp_path / "out.pdf"
        path, pages = json_to_pdf([], out)
        assert pages >= 1
        content = Path(path).read_bytes()
        assert content[:5] == b"%PDF-"

    def test_string_path_accepted(self, tmp_path: Path) -> None:
        data = [{"x": 1}]
        out = str(tmp_path / "out.pdf")
        path, pages = json_to_pdf(data, out)
        assert pages >= 1
        assert Path(path).exists()

    def test_returns_absolute_path(self, tmp_path: Path) -> None:
        data = [{"x": 1}]
        out = tmp_path / "out.pdf"
        path, _ = json_to_pdf(data, out)
        assert Path(path).is_absolute()

    def test_nested_dicts_flattened_in_table(self, tmp_path: Path) -> None:
        data = [{"user": {"name": "Alice", "age": 30}}]
        out = tmp_path / "out.pdf"
        path, pages = json_to_pdf(data, out)
        assert pages >= 1
        assert Path(path).exists()

    def test_flatten_disabled(self, tmp_path: Path) -> None:
        data = [{"user": {"name": "Alice", "age": 30}}]
        out = tmp_path / "out.pdf"
        path, pages = json_to_pdf(data, out, flatten=False)
        assert pages >= 1
        assert Path(path).exists()

    def test_many_rows_creates_multiple_pages(self, tmp_path: Path) -> None:
        data = [{"id": i, "value": f"item-{i}"} for i in range(100)]
        out = tmp_path / "out.pdf"
        path, pages = json_to_pdf(data, out)
        assert pages > 1

    def test_vendor_header(self, tmp_path: Path) -> None:
        """Vendor name appears in the PDF header."""
        data = [{"name": "Alice"}]
        out = tmp_path / "out.pdf"
        path, pages = json_to_pdf(data, out, vendor="Acme Corp")
        assert pages >= 1
        assert Path(path).exists()
        content = Path(path).read_bytes()
        assert len(content) > 100

    def test_vendor_with_info(self, tmp_path: Path) -> None:
        """Vendor name and info line both render."""
        data = [{"name": "Alice"}]
        out = tmp_path / "out.pdf"
        path, pages = json_to_pdf(data, out, vendor="Acme Corp", vendor_info="123 Main St")
        assert pages >= 1
        assert Path(path).exists()

    def test_logo_missing_file_still_renders(self, tmp_path: Path) -> None:
        """Missing logo file logs a warning but still produces a PDF."""
        data = [{"name": "Alice"}]
        out = tmp_path / "out.pdf"
        path, pages = json_to_pdf(data, out, vendor="Acme Corp", logo="/nonexistent/logo.png")
        assert pages >= 1
        assert Path(path).exists()
        content = Path(path).read_bytes()
        assert content[:5] == b"%PDF-"


class TestGetDownloadsDir:
    """Tests for the get_downloads_dir utility function."""

    def test_default_directory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default download directory is ./gp-downloads/ resolved to absolute."""
        monkeypatch.delenv("GP_DOWNLOADS_DIR", raising=False)
        result = get_downloads_dir()
        assert result == Path("gp-downloads").resolve()
        assert result.is_absolute()

    def test_env_var_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """GP_DOWNLOADS_DIR env var overrides default."""
        custom_dir = tmp_path / "my-downloads"
        monkeypatch.setenv("GP_DOWNLOADS_DIR", str(custom_dir))
        result = get_downloads_dir()
        assert result == custom_dir

    def test_creates_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Download directory is created if it doesn't exist."""
        download_dir = tmp_path / "downloads"
        monkeypatch.setenv("GP_DOWNLOADS_DIR", str(download_dir))
        result = get_downloads_dir()
        assert result == download_dir
        assert result.is_dir()


class TestFormatterBinaryProperty:
    """All formatters must declare whether they produce binary output."""

    def test_json_formatter_is_not_binary(self) -> None:
        assert JsonFormatter().binary is False

    def test_table_formatter_is_not_binary(self) -> None:
        assert TableFormatter().binary is False

    def test_raw_formatter_is_not_binary(self) -> None:
        assert RawFormatter().binary is False

    def test_csv_formatter_is_not_binary(self) -> None:
        assert CsvFormatter().binary is False

    def test_xlsx_formatter_is_binary(self) -> None:
        assert XlsxFormatter().binary is True

    def test_pdf_formatter_is_binary(self) -> None:
        assert PdfFormatter().binary is True

    def test_binary_property_in_protocol(self) -> None:
        """Custom formatter with binary property satisfies protocol."""

        class BinaryFormatter:
            name = "bin"
            binary = True

            def format(self, data: object, console: object) -> None:
                pass

        assert isinstance(BinaryFormatter(), OutputFormatter)


class TestCommandResultPluginFormatters:
    """CommandResult carries plugin-wide formatters for export()."""

    def test_plugin_formatters_defaults_to_none(self) -> None:

        result = CommandResult(data={"key": "value"})
        assert result._plugin_formatters is None

    def test_plugin_formatters_can_be_set(self) -> None:

        fmt = JsonFormatter()
        result = CommandResult(data={}, _plugin_formatters={"json": fmt})
        assert result._plugin_formatters == {"json": fmt}


class TestExportTextFormats:
    """export() returns str for text formats without output path."""

    def test_export_json_returns_string(self) -> None:

        data = {"name": "Alice", "age": 30}
        result = CommandResult(data=data)
        output = result.export("json")
        assert isinstance(output, str)
        parsed = json.loads(output)
        assert parsed == data

    def test_export_raw_returns_string(self) -> None:

        result = CommandResult(data="hello world")
        output = result.export("raw")
        assert isinstance(output, str)
        assert "hello world" in output

    def test_export_csv_returns_string(self) -> None:

        data = [{"name": "Alice", "age": "30"}, {"name": "Bob", "age": "25"}]
        result = CommandResult(data=data)
        output = result.export("csv")
        assert isinstance(output, str)
        assert "Alice" in output
        assert "Bob" in output

    def test_export_table_returns_string(self) -> None:

        data = [{"col": "val"}]
        result = CommandResult(data=data)
        output = result.export("table")
        assert isinstance(output, str)
        assert "val" in output

    def test_export_json_to_file_returns_path(self, tmp_path: Path) -> None:

        data = {"key": "value"}
        result = CommandResult(data=data)
        out = tmp_path / "out.json"
        returned = result.export("json", out)
        assert isinstance(returned, Path)
        assert returned == out
        assert out.exists()

    def test_export_csv_to_file_returns_path(self, tmp_path: Path) -> None:

        data = [{"a": "1"}]
        result = CommandResult(data=data)
        out = tmp_path / "out.csv"
        returned = result.export("csv", out)
        assert isinstance(returned, Path)
        assert out.exists()
        assert "a" in out.read_text()

    def test_export_unknown_format_raises(self) -> None:

        result = CommandResult(data={})
        with pytest.raises(ValueError, match="Unknown output format"):
            result.export("nonexistent")


class TestExportBinaryFormats:
    """export() returns bytes for binary formats without output path."""

    def test_export_xlsx_returns_bytes(self) -> None:
        data = [{"name": "Alice", "age": 30}]
        result = CommandResult(data=data)
        output = result.export("xlsx")
        assert isinstance(output, bytes)
        # XLSX files start with PK (ZIP magic bytes)
        assert output[:2] == b"PK"

    def test_export_xlsx_to_file_returns_path(self, tmp_path: Path) -> None:
        data = [{"name": "Alice"}]
        result = CommandResult(data=data)
        out = tmp_path / "out.xlsx"
        returned = result.export("xlsx", out)
        assert isinstance(returned, Path)
        assert out.exists()
        assert out.read_bytes()[:2] == b"PK"

    @patch("graftpunk.plugins.formatters.gp_console")
    def test_export_pdf_returns_bytes(self, mock_console: object) -> None:
        data = [{"name": "Alice", "age": "30"}]
        result = CommandResult(data=data)
        output = result.export("pdf")
        assert isinstance(output, bytes)
        # PDF files start with %PDF
        assert output[:4] == b"%PDF"

    @patch("graftpunk.plugins.formatters.gp_console")
    def test_export_pdf_to_file_returns_path(self, mock_console: object, tmp_path: Path) -> None:
        data = [{"name": "Alice"}]
        result = CommandResult(data=data)
        out = tmp_path / "out.pdf"
        returned = result.export("pdf", out)
        assert isinstance(returned, Path)
        assert out.exists()


class TestExportViewFiltering:
    """export() supports view filtering via the views parameter."""

    def test_csv_with_view_filter(self) -> None:
        data = {
            "items": [{"name": "A", "price": "10"}, {"name": "B", "price": "20"}],
            "summary": {"total": 30},
        }
        config = OutputConfig(
            views=(
                ViewConfig(name="items", path="items"),
                ViewConfig(name="summary", path="summary"),
            )
        )
        result = CommandResult(data=data, output_config=config)
        output = result.export("csv", views=("items",))
        assert isinstance(output, str)
        assert "A" in output
        assert "total" not in output

    def test_csv_with_column_filter(self) -> None:
        data = {"items": [{"name": "A", "price": "10", "qty": "5"}]}
        config = OutputConfig(views=(ViewConfig(name="items", path="items"),))
        result = CommandResult(data=data, output_config=config)
        output = result.export("csv", views=("items:name,price",))
        assert isinstance(output, str)
        assert "name" in output
        assert "price" in output
        assert "qty" not in output


def _make_test_spec(name: str = "test", handler: object = None, **kw: object) -> CommandSpec:
    return CommandSpec(
        name=name,
        handler=handler or (lambda ctx, **k: {"data": "ok"}),
        **kw,
    )


def _make_test_plugin(
    commands: list[CommandSpec] | None = None,
    format_overrides: dict | None = None,
) -> MagicMock:
    plugin = MagicMock()
    plugin.site_name = "test"
    plugin.session_name = "test"
    plugin.requires_session = False
    plugin.token_config = None
    plugin.base_url = ""
    plugin.backend = "selenium"
    plugin.api_version = 1
    plugin._plugin_config = None
    plugin.get_commands.return_value = commands or []
    plugin.format_overrides = format_overrides
    return plugin


class TestPluginFormattersThreading:
    """_plugin_formatters is populated during result normalization."""

    def test_execute_plugin_command_threads_formatters(self) -> None:
        fake_fmt = MagicMock()
        fake_fmt.name = "custom"
        spec = _make_test_spec()
        ctx = MagicMock(spec=CommandContext)
        ctx.plugin_name = "test"
        result = execute_plugin_command(
            spec,
            ctx,
            plugin_formatters={"custom": fake_fmt},
        )
        assert result._plugin_formatters == {"custom": fake_fmt}

    def test_execute_plugin_command_none_by_default(self) -> None:
        spec = _make_test_spec()
        ctx = MagicMock(spec=CommandContext)
        ctx.plugin_name = "test"
        result = execute_plugin_command(spec, ctx)
        assert result._plugin_formatters is None

    @patch("graftpunk.client.get_plugin")
    def test_client_execute_threads_formatters(
        self,
        mock_get: MagicMock,
    ) -> None:
        fake_fmt = MagicMock()
        fake_fmt.name = "custom"
        spec = _make_test_spec(handler=lambda ctx, **kw: {"ok": True})
        plugin = _make_test_plugin(
            commands=[spec],
            format_overrides={"custom": fake_fmt},
        )
        mock_get.return_value = plugin
        client = GraftpunkClient("test")
        result = client.execute("test")
        assert result._plugin_formatters == {"custom": fake_fmt}

    @patch("graftpunk.client.get_plugin")
    def test_client_threads_formatters_on_handler_returned_result(
        self,
        mock_get: MagicMock,
    ) -> None:
        """When handler returns CommandResult, plugin formatters are merged."""
        fake_fmt = MagicMock()
        handler_result = CommandResult(data={"x": 1})
        spec = _make_test_spec(handler=lambda ctx, **kw: handler_result)
        plugin = _make_test_plugin(
            commands=[spec],
            format_overrides={"custom": fake_fmt},
        )
        mock_get.return_value = plugin
        client = GraftpunkClient("test")
        result = client.execute("test")
        assert result._plugin_formatters == {"custom": fake_fmt}
