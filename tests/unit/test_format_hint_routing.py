"""Which formatter a CommandResult.format_hint, -f and -o pick (#284).

Rules under test, in order:
1. an explicit -f always wins;
2. a hint naming a formatter the plugin registered (any name, a built-in one
   included) or an entry point added under a non-built-in name runs
   everywhere, and that formatter sees output_path and the console;
3. a hint naming a built-in formatter the plugin did not override applies
   only to a terminal with no -o;
4. -o with a .json, .csv, .xlsx or .pdf suffix picks that format;
5. otherwise the CLI default.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from rich.console import Console

from graftpunk import console as gp_console_module
from graftpunk.console import stream_is_tty
from graftpunk.plugins import formatters as formatters_module
from graftpunk.plugins.cli_plugin import CommandResult
from graftpunk.plugins.formatters import (
    BUILTIN_FORMATTERS,
    format_output,
    write_rendered_to_file,
)
from graftpunk.plugins.output_config import ColumnFilter, OutputConfig, ViewConfig

_ROWS = [{"id": 1, "total": "9.50"}]


class _TtyBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class _ClosedTtyBuffer(io.StringIO):
    """isatty raises as a closed stream's does; writes still land, so output is observable."""

    def isatty(self) -> bool:
        raise ValueError("I/O operation on closed file")


class _NoIsattyBuffer:
    """A stream with no isatty at all, as some embedded interpreters provide."""

    def __init__(self) -> None:
        self.text = ""

    def write(self, text: str) -> int:
        self.text += text
        return len(text)

    def flush(self) -> None:
        return None


class _PluginDefault:
    """A plugin's own formatter: reports what it was handed."""

    name = "default"
    binary = False

    def format(
        self,
        data: Any,
        console: Console,
        output_config: Any = None,
        output_path: str = "",
    ) -> None:
        report = f"plugin-default tty={stream_is_tty(console)} path={Path(output_path).name}\n"
        gp_console_module.write_raw(report, console, output_path)


def _tty_console() -> tuple[Console, _TtyBuffer]:
    buf = _TtyBuffer()
    return Console(file=buf, width=200), buf


def _pipe_console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, width=200), buf


def _is_box_table(text: str) -> bool:
    return "┏" in text and "9.50" in text


def _builtin_hint() -> CommandResult:
    return CommandResult(data=_ROWS, format_hint="table")


class TestBuiltinHintOnTheConsole:
    def test_terminal_without_output_uses_hint(self) -> None:
        console, buf = _tty_console()
        format_output(_builtin_hint(), "json", console)
        assert _is_box_table(buf.getvalue())

    def test_pipe_ignores_hint(self) -> None:
        console, buf = _pipe_console()
        format_output(_builtin_hint(), "json", console)
        assert json.loads(buf.getvalue()) == _ROWS

    def test_closed_stream_ignores_hint(self) -> None:
        buf = _ClosedTtyBuffer()
        format_output(_builtin_hint(), "json", Console(file=buf, width=200))
        assert json.loads(buf.getvalue()) == _ROWS

    def test_stream_without_isatty_ignores_hint(self) -> None:
        buf = _NoIsattyBuffer()
        format_output(_builtin_hint(), "json", Console(file=buf, width=200))  # type: ignore[arg-type]
        assert json.loads(buf.text) == _ROWS

    def test_none_stdout_ignores_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        chosen: list[str] = []

        class _Spy:
            binary = False

            def __init__(self, name: str) -> None:
                self.name = name

            def format(self, data: Any, console: Console, **_: Any) -> None:
                chosen.append(self.name)

        # Spies stand in for the built-ins themselves (not a plugin override,
        # which would run everywhere): output to a null file is unobservable.
        monkeypatch.setattr(
            formatters_module,
            "discover_formatters",
            lambda: {"table": _Spy("table"), "json": _Spy("json")},
        )
        monkeypatch.setattr(sys, "stdout", None)
        format_output(_builtin_hint(), "json", Console())
        assert chosen == ["json"]

    def test_explicit_format_wins_on_terminal(self) -> None:
        console, buf = _tty_console()
        format_output(_builtin_hint(), "raw", console, user_explicit=True)
        assert "┏" not in buf.getvalue()
        assert "9.50" in buf.getvalue()


class TestBuiltinHintWithOutputFile:
    """With -o, a core hint is ignored on a terminal; the suffix or the CLI default decides."""

    def test_json_suffix_writes_json(self, tmp_path: Path) -> None:
        out = tmp_path / "out.json"
        console, buf = _tty_console()
        format_output(_builtin_hint(), "json", console, output_path=str(out))
        assert json.loads(out.read_text(encoding="utf-8")) == _ROWS
        assert buf.getvalue() == ""

    def test_upper_case_csv_suffix_writes_csv(self, tmp_path: Path) -> None:
        out = tmp_path / "ROWS.CSV"
        console, _ = _tty_console()
        format_output(_builtin_hint(), "json", console, output_path=str(out))
        rows = list(csv.reader(io.StringIO(out.read_text(encoding="utf-8"))))
        assert rows == [["id", "total"], ["1", "9.50"]]

    def test_dotfile_csv_name_writes_csv(self, tmp_path: Path) -> None:
        out = tmp_path / ".csv"
        console, _ = _tty_console()
        format_output(_builtin_hint(), "json", console, output_path=str(out))
        rows = list(csv.reader(io.StringIO(out.read_text(encoding="utf-8"))))
        assert rows == [["id", "total"], ["1", "9.50"]]

    def test_csv_suffix_writes_csv(self, tmp_path: Path) -> None:
        out = tmp_path / "rows.csv"
        console, _ = _tty_console()
        format_output(_builtin_hint(), "json", console, output_path=str(out))
        rows = list(csv.reader(io.StringIO(out.read_text(encoding="utf-8"))))
        assert rows == [["id", "total"], ["1", "9.50"]]

    def test_xlsx_suffix_writes_a_workbook(self, tmp_path: Path) -> None:
        out = tmp_path / "rows.xlsx"
        console, _ = _tty_console()
        format_output(_builtin_hint(), "json", console, output_path=str(out))
        sheet = openpyxl.load_workbook(out).active
        assert [c.value for c in sheet[1]] == ["id", "total"]

    def test_pdf_suffix_writes_a_pdf(self, tmp_path: Path) -> None:
        out = tmp_path / "rows.pdf"
        console, _ = _tty_console()
        format_output(_builtin_hint(), "json", console, output_path=str(out))
        assert out.read_bytes().startswith(b"%PDF")

    def test_unknown_suffix_gets_cli_default(self, tmp_path: Path) -> None:
        out = tmp_path / "rows.txt"
        console, _ = _tty_console()
        format_output(_builtin_hint(), "json", console, output_path=str(out))
        assert json.loads(out.read_text(encoding="utf-8")) == _ROWS

    def test_no_suffix_gets_cli_default(self, tmp_path: Path) -> None:
        out = tmp_path / "rows"
        console, _ = _tty_console()
        format_output(_builtin_hint(), "json", console, output_path=str(out))
        assert json.loads(out.read_text(encoding="utf-8")) == _ROWS

    def test_pipe_with_output_file_ignores_hint(self, tmp_path: Path) -> None:
        out = tmp_path / "rows.txt"
        console, _ = _pipe_console()
        format_output(_builtin_hint(), "json", console, output_path=str(out))
        assert json.loads(out.read_text(encoding="utf-8")) == _ROWS

    def test_explicit_format_wins_over_suffix(self, tmp_path: Path) -> None:
        out = tmp_path / "rows.csv"
        console, _ = _tty_console()
        format_output(_builtin_hint(), "json", console, user_explicit=True, output_path=str(out))
        assert json.loads(out.read_text(encoding="utf-8")) == _ROWS

    def test_explicit_table_reaches_the_file(self, tmp_path: Path) -> None:
        out = tmp_path / "rows.json"
        console, _ = _tty_console()
        format_output(_builtin_hint(), "table", console, user_explicit=True, output_path=str(out))
        assert _is_box_table(out.read_text(encoding="utf-8"))

    def test_suffix_applies_without_any_hint(self, tmp_path: Path) -> None:
        out = tmp_path / "rows.csv"
        console, _ = _tty_console()
        format_output(CommandResult(data=_ROWS), "json", console, output_path=str(out))
        assert out.read_text(encoding="utf-8").splitlines()[0] == "id,total"

    def test_suffix_applies_to_plain_data(self, tmp_path: Path) -> None:
        out = tmp_path / "rows.csv"
        console, _ = _tty_console()
        format_output(_ROWS, "json", console, output_path=str(out))
        assert out.read_text(encoding="utf-8").splitlines()[0] == "id,total"

    def test_suffix_uses_a_plugin_override_of_that_format(self, tmp_path: Path) -> None:
        class _Csv:
            name = "csv"
            binary = False

            def format(self, data: Any, console: Console, **kwargs: Any) -> None:
                gp_console_module.write_raw("plugin-csv\n", console, kwargs["output_path"])

        out = tmp_path / "rows.csv"
        console, _ = _tty_console()
        format_output(
            _builtin_hint(),
            "json",
            console,
            output_path=str(out),
            plugin_formatters={"csv": _Csv()},
        )
        assert out.read_text(encoding="utf-8") == "plugin-csv\n"


class TestEmptyAndUnrenderableFiles:
    """-o never leaves an old file behind, and says when a file holds another format."""

    def test_empty_result_truncates_an_existing_csv(self, tmp_path: Path) -> None:
        out = tmp_path / "rows.csv"
        out.write_text("STALE FROM YESTERDAY\n", encoding="utf-8")
        console, _ = _tty_console()
        format_output(CommandResult(data=[]), "json", console, output_path=str(out))
        assert out.read_text(encoding="utf-8") == ""

    def test_empty_result_with_explicit_csv_truncates(self, tmp_path: Path) -> None:
        out = tmp_path / "rows.txt"
        out.write_text("STALE FROM YESTERDAY\n", encoding="utf-8")
        console, _ = _tty_console()
        format_output([], "csv", console, user_explicit=True, output_path=str(out))
        assert out.read_text(encoding="utf-8") == ""

    def test_empty_result_writes_known_columns_as_header(self, tmp_path: Path) -> None:
        out = tmp_path / "rows.csv"
        out.write_text("STALE FROM YESTERDAY\n", encoding="utf-8")
        cfg = OutputConfig(
            views=[
                ViewConfig(
                    name="orders",
                    path="orders",
                    columns=ColumnFilter(mode="include", columns=("id", "total")),
                )
            ]
        )
        result = CommandResult(data={"orders": []}, output_config=cfg)
        console, _ = _tty_console()
        format_output(result, "json", console, output_path=str(out))
        assert out.read_text(encoding="utf-8") == "id,total\n"

    def test_empty_result_on_the_console_prints_nothing(self) -> None:
        console, buf = _pipe_console()
        format_output([], "csv", console, user_explicit=True)
        assert buf.getvalue() == ""

    def test_pdf_suffix_with_unrenderable_data_warns_and_writes_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "rows.pdf"
        console, _ = _tty_console()
        format_output(CommandResult(data="hello"), "json", console, output_path=str(out))
        assert json.loads(out.read_text(encoding="utf-8")) == "hello"
        err = " ".join(capsys.readouterr().err.split())
        assert "cannot be written as a PDF table; wrote JSON" in err

    def test_csv_suffix_with_mixed_list_warns_and_writes_raw(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "rows.csv"
        console, _ = _tty_console()
        format_output(CommandResult(data=[1, 2]), "json", console, output_path=str(out))
        assert "1" in out.read_text(encoding="utf-8")
        err = " ".join(capsys.readouterr().err.split())
        assert "cannot be written as CSV; wrote raw text" in err

    def test_pdf_on_console_fallback_does_not_warn(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        console, buf = _pipe_console()
        format_output("hello", "pdf", console, user_explicit=True)
        assert json.loads(buf.getvalue()) == "hello"
        assert "cannot be written" not in capsys.readouterr().err


class _Markdown:
    """Stands in for a formatter a package installs through the entry-point group."""

    binary = False

    def __init__(self, name: str = "markdown", marker: str = "entry-point-markdown") -> None:
        self.name = name
        self.marker = marker

    def format(self, data: Any, console: Console, **kwargs: Any) -> None:
        gp_console_module.write_raw(f"{self.marker}\n", console, kwargs.get("output_path", ""))


@pytest.fixture
def entry_point_formatters(monkeypatch: pytest.MonkeyPatch) -> None:
    """discover_formatters() as if packages installed ``markdown`` and ``md``."""

    def discover() -> dict[str, Any]:
        found: dict[str, Any] = dict(BUILTIN_FORMATTERS)
        found["markdown"] = _Markdown()
        found["md"] = _Markdown("md", "entry-point-md")
        return found

    monkeypatch.setattr(formatters_module, "discover_formatters", discover)


@pytest.mark.usefixtures("entry_point_formatters")
class TestEntryPointHint:
    def test_entry_point_hint_piped_prints_its_output(self) -> None:
        console, buf = _pipe_console()
        format_output(CommandResult(data=_ROWS, format_hint="markdown"), "json", console)
        assert buf.getvalue() == "entry-point-markdown\n"

    def test_entry_point_hint_reaches_output_file(self, tmp_path: Path) -> None:
        out = tmp_path / "notes.csv"
        console, _ = _tty_console()
        result = CommandResult(data=_ROWS, format_hint="markdown")
        format_output(result, "json", console, output_path=str(out))
        assert out.read_text(encoding="utf-8") == "entry-point-markdown\n"

    def test_plugin_override_shadowing_an_entry_point_runs_when_piped(self) -> None:
        console, buf = _pipe_console()
        format_output(
            CommandResult(data=_ROWS, format_hint="md"),
            "json",
            console,
            plugin_formatters={"md": _Markdown("md", "plugin-md")},
        )
        assert buf.getvalue() == "plugin-md\n"

    def test_plugin_override_shadowing_an_entry_point_runs_for_output(self, tmp_path: Path) -> None:
        out = tmp_path / "rows.csv"
        console, _ = _tty_console()
        result = CommandResult(
            data=_ROWS,
            format_hint="md",
            format_overrides={"md": _Markdown("md", "plugin-md")},
        )
        format_output(result, "json", console, output_path=str(out))
        assert out.read_text(encoding="utf-8") == "plugin-md\n"


class TestPluginRegisteredHint:
    """A hint naming the plugin's own formatter is the plugin's default everywhere."""

    @staticmethod
    def _run(console: Console, *, output_path: str = "", via_result: bool = False) -> None:
        if via_result:
            result = CommandResult(
                data=_ROWS, format_hint="default", format_overrides={"default": _PluginDefault()}
            )
            format_output(result, "json", console, output_path=output_path)
        else:
            result = CommandResult(data=_ROWS, format_hint="default")
            format_output(
                result,
                "json",
                console,
                output_path=output_path,
                plugin_formatters={"default": _PluginDefault()},
            )

    @pytest.mark.parametrize("via_result", [False, True])
    def test_terminal(self, via_result: bool) -> None:
        console, buf = _tty_console()
        self._run(console, via_result=via_result)
        assert buf.getvalue() == "plugin-default tty=True path=\n"

    @pytest.mark.parametrize("via_result", [False, True])
    def test_pipe(self, via_result: bool) -> None:
        console, buf = _pipe_console()
        self._run(console, via_result=via_result)
        assert buf.getvalue() == "plugin-default tty=False path=\n"

    @pytest.mark.parametrize("name", ["out.json", "rows.csv", "rows.xlsx", "rows.pdf", "rows.txt"])
    def test_output_file_of_any_suffix(self, tmp_path: Path, name: str) -> None:
        out = tmp_path / name
        console, buf = _tty_console()
        self._run(console, output_path=str(out))
        assert out.read_text(encoding="utf-8") == f"plugin-default tty=True path={name}\n"
        assert buf.getvalue() == ""

    def test_explicit_format_wins(self, tmp_path: Path) -> None:
        out = tmp_path / "rows.csv"
        console, _ = _tty_console()
        result = CommandResult(data=_ROWS, format_hint="default")
        format_output(
            result,
            "json",
            console,
            user_explicit=True,
            output_path=str(out),
            plugin_formatters={"default": _PluginDefault()},
        )
        assert json.loads(out.read_text(encoding="utf-8")) == _ROWS

    @pytest.mark.parametrize("piped", [False, True])
    def test_override_of_a_built_in_name_runs_for_output(self, tmp_path: Path, piped: bool) -> None:
        class _Table:
            name = "table"
            binary = False

            def format(self, data: Any, console: Console, **kwargs: Any) -> None:
                gp_console_module.write_raw("plugin-table\n", console, kwargs["output_path"])

        out = tmp_path / "out.json"
        console, _ = _pipe_console() if piped else _tty_console()
        format_output(
            _builtin_hint(),
            "json",
            console,
            output_path=str(out),
            plugin_formatters={"table": _Table()},
        )
        assert out.read_text(encoding="utf-8") == "plugin-table\n"

    def test_override_of_a_built_in_name_runs_when_piped(self) -> None:
        class _Table:
            name = "table"
            binary = False

            def format(self, data: Any, console: Console, **kwargs: Any) -> None:
                gp_console_module.write_raw("plugin-table\n", console, kwargs["output_path"])

        console, buf = _pipe_console()
        result = CommandResult(
            data=_ROWS, format_hint="table", format_overrides={"table": _Table()}
        )
        format_output(result, "json", console)
        assert buf.getvalue() == "plugin-table\n"


class TestPublicHelpers:
    def test_stream_is_tty_answers_for_each_stream(self) -> None:
        assert stream_is_tty(Console(file=_TtyBuffer())) is True
        assert stream_is_tty(Console(file=io.StringIO())) is False
        assert stream_is_tty(Console(file=_ClosedTtyBuffer())) is False
        assert stream_is_tty(Console(file=_NoIsattyBuffer())) is False  # type: ignore[arg-type]

    def test_stream_is_tty_with_none_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        console = Console()
        monkeypatch.setattr(sys, "stdout", None)
        assert stream_is_tty(console) is False

    def test_private_names_are_aliases(self) -> None:
        assert gp_console_module._stream_is_tty is stream_is_tty
        assert formatters_module._stream_is_tty is stream_is_tty
        assert formatters_module._write_to_file is write_rendered_to_file

    def test_write_rendered_to_file_writes_plain_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FORCE_COLOR", "1")
        out = tmp_path / "nested" / "table.txt"
        write_rendered_to_file(str(out), lambda c: c.print("[bold]9.50[/bold]"))
        assert out.read_text(encoding="utf-8") == "9.50\n"

    def test_exported_from_graftpunk_plugins(self) -> None:
        import graftpunk.plugins as plugins

        assert plugins.write_rendered_to_file is write_rendered_to_file
        assert "write_rendered_to_file" in plugins.__all__
