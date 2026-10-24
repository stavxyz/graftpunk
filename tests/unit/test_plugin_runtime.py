"""Integration: factory-synthesized commands driving the runtime pipeline."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import typer
from rich.console import Console
from typer.testing import CliRunner

from graftpunk.cli.command_factory import synthesize_command_fn
from graftpunk.cli.plugin_runtime import run_plugin_command
from graftpunk.plugins.cli_plugin import CommandResult, SitePlugin, command


class _EchoPlugin(SitePlugin):
    site_name = "echosite"
    session_name = "echosession"
    help_text = "Echo plugin"
    requires_session = False

    @command(help="Echo params back")
    def echo(self, ctx, **kw):
        return {"echo": kw}


class TestFactoryPlusRuntime:
    def test_end_to_end_command_executes_pipeline(self) -> None:
        plugin = _EchoPlugin()
        spec = next(s for s in plugin.get_commands() if s.name == "echo")

        def body(ctx: typer.Context, **kwargs) -> None:
            run_plugin_command(plugin, spec, ctx, **kwargs)

        fn = synthesize_command_fn(name="echo", param_specs=list(spec.params), body=body)
        app = typer.Typer()
        app.command(name="echo")(fn)
        with patch("graftpunk.cli.plugin_runtime.format_output") as mock_fmt:
            result = CliRunner().invoke(app, ["--kw", "test"])
        assert result.exit_code == 0, result.output
        mock_fmt.assert_called_once()
        assert mock_fmt.call_args.kwargs["user_explicit"] is False


class _TtyBuffer(io.StringIO):
    """A console stream that reports itself as a terminal."""

    def isatty(self) -> bool:
        return True


_ROWS = [{"id": 1, "total": "9.50"}]


class _DefaultFormatter:
    """A plugin's own ``default`` formatter, choosing the format at format time."""

    name = "default"
    binary = False

    def format(
        self,
        data: Any,
        console: Console,
        output_config: Any = None,
        output_path: str = "",
    ) -> None:
        from graftpunk.console import stream_is_tty, write_raw

        if output_path:
            write_raw(f"plugin-default file {Path(output_path).name}\n", console, output_path)
        elif stream_is_tty(console):
            write_raw("default-table\n", console)
        else:
            write_raw(json.dumps(data) + "\n", console)


class _HintPlugin(SitePlugin):
    site_name = "myshop"
    session_name = "myshop"
    help_text = "Hinting plugin"
    requires_session = False
    format_overrides = {"default": _DefaultFormatter()}

    @command(help="List orders with a built-in hint")
    def orders(self, ctx):
        return CommandResult(data=_ROWS, format_hint="table")

    @command(help="List orders with the plugin's own hint")
    def mine(self, ctx):
        return CommandResult(data=_ROWS, format_hint="default")


def _invoke(name: str, args: list[str]) -> tuple[Any, _TtyBuffer]:
    """Run ``gp myshop <name> <args>`` with a terminal as stdout."""
    plugin = _HintPlugin()
    spec = next(s for s in plugin.get_commands() if s.name == name)

    def body(ctx: typer.Context, **kwargs) -> None:
        run_plugin_command(plugin, spec, ctx, **kwargs)

    fn = synthesize_command_fn(name=name, param_specs=list(spec.params), body=body)
    app = typer.Typer()
    app.command(name=name)(fn)
    terminal = _TtyBuffer()
    console = Console(file=terminal, width=200)
    with patch("graftpunk.cli.plugin_runtime._format_console", console):
        result = CliRunner().invoke(app, args)
    return result, terminal


class TestFormatHintAndOutputFile:
    """#284: a built-in hint typed in a terminal must not decide what -o writes."""

    def test_table_hint_does_not_reach_output_json_file(self, tmp_path: Path) -> None:
        out = tmp_path / "out.json"
        result, terminal = _invoke("orders", ["-o", str(out)])
        assert result.exit_code == 0, result.output
        assert json.loads(out.read_text(encoding="utf-8")) == _ROWS
        assert terminal.getvalue() == ""

    def test_table_hint_with_csv_suffix_writes_csv(self, tmp_path: Path) -> None:
        out = tmp_path / "rows.csv"
        result, _ = _invoke("orders", ["-o", str(out)])
        assert result.exit_code == 0, result.output
        rows = list(csv.reader(io.StringIO(out.read_text(encoding="utf-8"))))
        assert rows == [["id", "total"], ["1", "9.50"]]

    def test_table_hint_reaches_the_terminal(self) -> None:
        result, terminal = _invoke("orders", [])
        assert result.exit_code == 0, result.output
        assert "9.50" in terminal.getvalue()
        assert "─" in terminal.getvalue()

    def test_explicit_format_wins_over_hint_and_suffix(self, tmp_path: Path) -> None:
        out = tmp_path / "rows.csv"
        result, _ = _invoke("orders", ["-f", "json", "-o", str(out)])
        assert result.exit_code == 0, result.output
        assert json.loads(out.read_text(encoding="utf-8")) == _ROWS

    def test_plugin_hint_still_decides_the_file(self, tmp_path: Path) -> None:
        out = tmp_path / "rows.csv"
        result, _ = _invoke("mine", ["-o", str(out)])
        assert result.exit_code == 0, result.output
        assert out.read_text(encoding="utf-8") == "plugin-default file rows.csv\n"

    def test_plugin_hint_reaches_the_terminal(self) -> None:
        result, terminal = _invoke("mine", [])
        assert result.exit_code == 0, result.output
        assert terminal.getvalue() == "default-table\n"
