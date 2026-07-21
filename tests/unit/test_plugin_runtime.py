"""Integration: factory-synthesized commands driving the runtime pipeline."""

from __future__ import annotations

from unittest.mock import patch

import typer
from typer.testing import CliRunner

from graftpunk.cli.command_factory import synthesize_command_fn
from graftpunk.cli.plugin_runtime import run_plugin_command
from graftpunk.plugins.cli_plugin import SitePlugin, command


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
