"""Tests for export utilities and CommandResult.export() API."""

from __future__ import annotations

import csv
import datetime
import decimal
import json
import random
import re
import time
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any
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
        ctx._operating_session_name = ""
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
        ctx._operating_session_name = ""
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


def _pdf_code_points(pdf: bytes) -> set[int]:
    """The Unicode code points the PDF's ToUnicode maps name: what a reader
    extracts from the text drawn in an embedded TTF (fpdf2 writes these
    maps uncompressed)."""
    return {int(m, 16) for m in re.findall(rb"<[0-9A-F]{4}> <([0-9A-F]{4,8})>", pdf)}


class TestPdfUnicode:
    """#290: the core Helvetica font encodes Latin-1 only, and a curly quote,
    an en dash or a letter past Latin-1 raised FPDFUnicodeEncodingException."""

    _TEXT = "O\u2019BRIEN \u2013 caf\u00e9 \u2026 \u041c\u043e\u0441\u043a\u0432\u0430 \u03a9"

    def test_text_past_latin_1_renders_as_written(self, tmp_path: Path) -> None:
        path, pages = json_to_pdf(
            [{"name": self._TEXT, "№": "x"}],
            tmp_path / "out.pdf",
            title="R\u00e9sum\u00e9 \u2013 Q1",
            vendor="Caf\u00e9 \u2019",
            vendor_info="№ 1",
            metadata={"№": "€ 5"},
        )
        assert pages == 1
        points = _pdf_code_points(Path(path).read_bytes())
        wanted = {ord(c) for c in self._TEXT + "№€" if not c.isspace()}
        assert wanted <= points, sorted(map(hex, wanted - points))

    def test_the_bold_heading_row_renders_past_latin_1(self, tmp_path: Path) -> None:
        path, _ = json_to_pdf([{"Łódź": 1}], tmp_path / "out.pdf")
        assert {0x141, 0x17A} <= _pdf_code_points(Path(path).read_bytes())

    def test_a_character_the_font_lacks_is_drawn_as_the_replacement_mark(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """fpdf2 left such a character out without a mark: "A\u4e2d\u6587B"
        drew "AB". It is now drawn as U+FFFD, in cells, headings and the title,
        and one warning names it."""
        from graftpunk.logging import configure_logging

        configure_logging(level="WARNING")
        path, pages = json_to_pdf(
            [{"name": "A\u4e2d\u6587B", "\u4e2d": 1}], tmp_path / "out.pdf", title="T\u4e2d"
        )
        assert pages == 1
        points = _pdf_code_points(Path(path).read_bytes())
        assert 0xFFFD in points
        assert {0x41, 0x42} <= points
        err = capsys.readouterr().err
        assert err.count("pdf_characters_not_in_font") == 1
        assert "U+4E2D" in err
        assert "U+6587" in err

    def test_line_breaks_and_tabs_are_not_replaced(self, tmp_path: Path) -> None:
        path, _ = json_to_pdf([{"note": "a\nb\tc"}], tmp_path / "out.pdf")
        assert 0xFFFD not in _pdf_code_points(Path(path).read_bytes())

    def test_command_result_export_writes_the_row(self) -> None:
        content = CommandResult(data=[{"name": "O\u2019BRIEN"}]).export("pdf")
        assert isinstance(content, bytes)
        assert 0x2019 in _pdf_code_points(content)

    def test_the_font_and_its_licence_ship_inside_the_package(self) -> None:
        import graftpunk.plugins

        fonts = Path(graftpunk.plugins.__file__).parent / "fonts"
        assert {p.name for p in fonts.iterdir()} >= {
            "DejaVuSans.ttf",
            "DejaVuSans-Bold.ttf",
            "LICENSE-DejaVu.txt",
        }
        assert "Bitstream Vera Fonts Copyright" in (fonts / "LICENSE-DejaVu.txt").read_text()


def _heading_fills(pdf: bytes) -> int:
    """How often the pdf sets the table heading's grey fill, in its
    Flate-compressed page content streams: a fixed number of times per heading
    cell, which a one-cell table measures."""
    count = 0
    for match in re.finditer(rb"stream\r?\n", pdf):
        try:
            content = zlib.decompressobj().decompress(pdf[match.end() :])
        except zlib.error:
            continue
        count += content.count(b"0.8627 0.8627 0.8627 rg")
    return count


class TestPdfWideTables:
    """At about 45 columns a column was narrower than one character and fpdf2
    raised "Not enough horizontal space to render a single character"."""

    @pytest.mark.parametrize("count", [70, 150, 400])
    def test_a_wide_table_renders_in_parts(self, tmp_path: Path, count: int) -> None:
        data = [{f"column_{i}": "W" * 5 for i in range(count)}] * 3
        path, pages = json_to_pdf(data, tmp_path / "out.pdf")
        assert pages >= 1
        one = json_to_pdf([{"c0": "W"}], tmp_path / "one.pdf")[0]
        per_cell = _heading_fills(Path(one).read_bytes())
        assert _heading_fills(Path(path).read_bytes()) // per_cell > count

    @pytest.mark.parametrize(
        ("cell", "count"),
        [("W", 43), ("1", 55), ("a", 55), ("12", 50)],
    )
    def test_no_fewer_columns_in_one_table_than_1_42_0(
        self, tmp_path: Path, cell: str, count: int
    ) -> None:
        """1.42.0 (Helvetica) drew these as one table: 43 columns of ``W`` and
        55 of ``1`` or ``a``, with headings ``c0`` to ``cN``. DejaVu Sans is
        wider, so such a table is drawn with a smaller cell margin instead of
        being split; nothing is cut, so no column makes room for the cut mark."""
        one = json_to_pdf([{"c0": "W"}], tmp_path / "one.pdf")[0]
        per_cell = _heading_fills(Path(one).read_bytes())
        path, _ = json_to_pdf([{f"c{i}": cell for i in range(count)}] * 2, tmp_path / "out.pdf")
        assert _heading_fills(Path(path).read_bytes()) == count * per_cell

    def test_a_table_that_fits_is_one_table_as_before(self, tmp_path: Path) -> None:
        one = json_to_pdf([{"c0": "W"}], tmp_path / "one.pdf")[0]
        per_cell = _heading_fills(Path(one).read_bytes())
        data = [{f"column_{i}": "W" * 5 for i in range(20)}]
        path, _ = json_to_pdf(data, tmp_path / "out.pdf")
        assert _heading_fills(Path(path).read_bytes()) == 20 * per_cell

    def test_each_part_repeats_the_first_column(self, tmp_path: Path) -> None:
        """Split in parts, every part after the first adds its own copy of the
        first column's heading: more heading cells than columns."""
        one = json_to_pdf([{"c0": "W"}], tmp_path / "one.pdf")[0]
        per_cell = _heading_fills(Path(one).read_bytes())
        path, _ = json_to_pdf([{f"c{i}": "W" * 5 for i in range(150)}], tmp_path / "out.pdf")
        cells = _heading_fills(Path(path).read_bytes()) // per_cell
        assert cells > 150
        parts = cells - 150 + 1
        assert parts >= 2

    def test_a_wide_glyph_never_raises(self, tmp_path: Path) -> None:
        data = [{f"c{i}": "\ufdfd\u2014W" for i in range(80)}]
        _, pages = json_to_pdf(data, tmp_path / "out.pdf")
        assert pages >= 1


def _pdf_text_bytes(pdf: bytes) -> bytes:
    """The pdf's Flate-compressed content streams, inflated."""
    out = b""
    for match in re.finditer(rb"stream\r?\n", pdf):
        try:
            out += zlib.decompressobj().decompress(pdf[match.end() :])
        except zlib.error:
            continue
    return out


class TestPdfCellsTooTallForAPage:
    """fpdf2 cannot split a table row across pages, and raised ``The row with
    index N is too high`` for a cell taller than a page; DejaVu Sans, wider
    than Helvetica, reached that with shorter text than 1.42.0 did."""

    @pytest.mark.parametrize(
        ("columns", "length"),
        [(1, 12000), (5, 1800), (12, 600), (20, 300), (40, 150), (200, 400)],
    )
    def test_a_long_cell_is_cut_with_one_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], columns: int, length: int
    ) -> None:
        from graftpunk.logging import configure_logging

        configure_logging(level="WARNING")
        row = {f"c{i}": "x" for i in range(columns)}
        row["c0"] = ("abc de " * length)[:length]
        path, pages = json_to_pdf([row, row], tmp_path / "out.pdf")
        assert pages >= 1
        # Cut cells end in U+2026, named in the ToUnicode map.
        assert 0x2026 in _pdf_code_points(Path(path).read_bytes())
        err = capsys.readouterr().err
        assert err.count("pdf_cells_truncated") == 1
        # Two rows; a table split into parts repeats the first column, cut in each.
        (count,) = re.findall(r"count=(\d+)", err)
        assert int(count) == 2 if columns < 40 else int(count) % 2 == 0

    def test_a_cell_of_line_breaks_is_cut(self, tmp_path: Path) -> None:
        _, pages = json_to_pdf([{"a": "\n" * 3000, "b": 1}], tmp_path / "out.pdf")
        assert pages >= 1

    def test_a_row_of_many_tall_cells_and_a_long_heading(self, tmp_path: Path) -> None:
        heading = "h" * 4000
        row = {heading: "word " * 2000, **{f"c{i}": "word " * 800 for i in range(11)}}
        _, pages = json_to_pdf([row] * 3, tmp_path / "out.pdf")
        assert pages >= 3

    def test_a_flattened_list_of_dicts_in_a_wide_row(self, tmp_path: Path) -> None:
        row = {f"c{i}": "v" for i in range(11)}
        row["items"] = [{"sku": f"SKU-{n:05d}", "qty": n} for n in range(25)]
        _, pages = json_to_pdf([row], tmp_path / "out.pdf")
        assert pages >= 1

    def test_a_cell_that_fits_is_not_cut(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from graftpunk.logging import configure_logging

        configure_logging(level="WARNING")
        path, _ = json_to_pdf([{"a": "abc de " * 40, "b": 1}], tmp_path / "out.pdf")
        assert 0x2026 not in _pdf_code_points(Path(path).read_bytes())
        assert "pdf_cells_truncated" not in capsys.readouterr().err


class TestPdfEmptyTexts:
    """fpdf2's ``cell`` raises on empty text, so an empty title or metadata
    value raised ``'text_line' must have fragments``."""

    def test_an_empty_title_draws_no_title(self, tmp_path: Path) -> None:
        path, pages = json_to_pdf([{"a": 1}], tmp_path / "out.pdf", title="")
        assert pages == 1
        assert 0x52 not in _pdf_code_points(Path(path).read_bytes())  # no "Report"

    def test_an_empty_metadata_value_is_left_out(self, tmp_path: Path) -> None:
        path, pages = json_to_pdf(
            [{"a": 1}], tmp_path / "out.pdf", metadata={"Customer": "", "Zone": "5"}
        )
        assert pages == 1
        points = _pdf_code_points(Path(path).read_bytes())
        assert 0x5A in points  # Zone
        assert 0x43 not in points  # Customer

    def test_empty_vendor_texts(self, tmp_path: Path) -> None:
        _, pages = json_to_pdf([{"a": 1}], tmp_path / "out.pdf", vendor="", vendor_info="")
        assert pages == 1

    @pytest.mark.parametrize("blank", ["\r", "\r\r", "\n", "\x0c", "​" * 40])
    def test_texts_that_are_empty_once_laid_out(self, tmp_path: Path, blank: str) -> None:
        """fpdf2 deletes every carriage return, so a metadata value of only
        ``\\r`` reached ``cell()`` empty and raised (found by the fuzz)."""
        _, pages = json_to_pdf(
            [{"a": 1}],
            tmp_path / "out.pdf",
            title=blank,
            vendor=blank,
            vendor_info=blank,
            metadata={"k": blank, blank: "v"},
        )
        assert pages == 1


_FUZZ_CHARS = [
    "W",
    "a",
    " ",
    "\n",
    "\t",
    "\r",
    "中",
    "\U0001f600",
    "\ud800",
    "\x00",
    "‏",
    "́",
    "Ж",
    "\u2014",
    "﷽",
    "\x0b",
    "­",
    "﻿",
    "…",
    "abc de ",
]


def _fuzz_value(rnd: random.Random, depth: int = 0) -> Any:
    kind = rnd.randint(0, 10 if depth < 2 else 5)
    if kind in (0, 1):
        n = rnd.choice([0, 1, 4, 40, 300, 1500])
        return "".join(rnd.choice(_FUZZ_CHARS) for _ in range(n))
    if kind == 2:
        return rnd.choice([rnd.randint(-(10**30), 10**30), float("nan"), float("inf"), -0.0])
    if kind == 3:
        return rnd.choice([None, True, datetime.date(2026, 1, 1), decimal.Decimal("NaN")])
    if kind == 4:
        return rnd.choice([(1, "x"), {1, 2}, object(), b"\xff"])
    if kind == 5:
        return rnd.choice(["word " * 900, "\n" * 400])
    if kind in (6, 7):
        return [_fuzz_value(rnd, depth + 1) for _ in range(rnd.randint(0, 8))]
    return {
        rnd.choice(["a", "中", (1, 2), 3, None, ""]): _fuzz_value(rnd, depth + 1)
        for _ in range(rnd.randint(0, 3))
    }


@pytest.mark.parametrize("seed", range(8))
def test_the_pdf_export_never_raises_on_fuzzed_input(tmp_path: Path, seed: int) -> None:
    """Mixed types, unicode, nested values, long cells, 1 to 120 columns, and
    odd titles and metadata: a seeded sample of the fuzz run on this branch."""
    rnd = random.Random(seed)  # noqa: S311 - a fixed sample, not a secret
    for case in range(6):
        columns = rnd.choice([1, 2, 5, 12, 30, 45, 80, 120])
        keys = [
            rnd.choice([f"c{j}", f"c{j}", 1, None, (1, 2), "", "W" * 40]) for j in range(columns)
        ]
        rows: list[Any] = [
            {key: _fuzz_value(rnd) for key in keys} if rnd.random() < 0.8 else _fuzz_value(rnd)
            for _ in range(rnd.randint(0, 4))
        ]
        json_to_pdf(
            rows,
            tmp_path / f"out{case}.pdf",
            title=rnd.choice(["", "T", "\n", " ", "\ud800"]),
            vendor=rnd.choice([None, "", "V\u2014\ud800"]),
            vendor_info=rnd.choice([None, "", "\U0001f600"]),
            metadata={rnd.choice(["k", "", "中"]): rnd.choice(["", "v", "5"]) for _ in range(2)},
            flatten=rnd.random() < 0.8,
        )


def _least_cpu(work: Callable[[], object], runs: int = 3) -> float:
    """The least CPU time of *runs* calls of *work*: CPU time, never wall clock,
    and the least, so a busy machine slows neither side of a ratio."""
    times = []
    for _ in range(runs):
        start = time.process_time()
        work()
        times.append(time.process_time() - start)
    return min(times)


def _plain_fpdf_table(rows: list[dict[str, str]], out: Path) -> None:
    """The same table drawn by fpdf2 alone, in the bundled font, with no
    fitting: what json_to_pdf's own work is measured against."""
    from fpdf import FPDF
    from fpdf.fonts import FontFace

    import graftpunk.plugins

    fonts = Path(graftpunk.plugins.__file__).parent / "fonts"
    pdf = FPDF()
    pdf.add_font("DejaVuSans", fname=fonts / "DejaVuSans.ttf")
    pdf.add_font("DejaVuSans", style="B", fname=fonts / "DejaVuSans-Bold.ttf")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("DejaVuSans", size=7)
    columns = list(rows[0])
    width = (pdf.w - pdf.l_margin - pdf.r_margin) / len(columns)
    with pdf.table(
        headings_style=FontFace(emphasis="BOLD", fill_color=(220, 220, 220)),
        col_widths=(width,) * len(columns),
        first_row_as_headings=True,
    ) as table:
        for row in [dict(zip(columns, columns, strict=True)), *rows]:
            cells = table.row()
            for column in columns:
                cells.cell(row[column])
    pdf.output(str(out))


class TestPdfSpeed:
    """Fitting every cell up front made an ordinary table two to three times
    slower to export, and a cell of 50,000 combining marks took 100 s."""

    def test_an_ordinary_table_costs_little_more_than_fpdf2_drawing_it(
        self, tmp_path: Path
    ) -> None:
        # Cells of up to about 35 lines in 20 columns: within a page, but past
        # the cheap bound that let round 2 skip measuring a cell.
        words = "lorem ipsum dolor sit amet "
        rows = [{f"c{c}": (words * 10)[: 140 + c] for c in range(20)} for _ in range(20)]
        ours: list[float] = []
        plain: list[float] = []
        # Interleaved, so a burst of load on a shared CI runner slows both sides;
        # the least of each, so it slows neither. Round 2's up-front fitting
        # measured 1.68 times fpdf2 alone; a loaded runner measured 1.41 times
        # for this release with three runs of each taken one side after the other.
        for _ in range(5):
            ours.append(_least_cpu(lambda: json_to_pdf(rows, tmp_path / "ours.pdf"), runs=1))
            plain.append(_least_cpu(lambda: _plain_fpdf_table(rows, tmp_path / "p.pdf"), runs=1))
        assert min(ours) <= 1.5 * min(plain) + 0.05, (ours, plain)

    @pytest.mark.parametrize("filler", ["́", "​"], ids=["combining", "zero-width-space"])
    def test_a_cell_of_characters_with_no_width_fits_in_near_linear_time(
        self, tmp_path: Path, filler: str
    ) -> None:
        def export(n: int) -> Callable[[], object]:
            return lambda: json_to_pdf([{"a": "a" + filler * n, "b": 1}], tmp_path / f"{n}.pdf")

        small = _least_cpu(export(5000))
        large = _least_cpu(export(50000))
        # Ten times the characters: linear is 10x; fpdf2's quadratic line
        # breaking made it about 100x.
        assert large <= 15 * small + 0.2, (small, large)
