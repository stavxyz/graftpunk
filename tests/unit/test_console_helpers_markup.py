"""gp_console helpers and CommandResult.export must treat their input as data (#145/#144)."""

from __future__ import annotations

import importlib
import io

import pytest
from rich.console import Console

from graftpunk import console as gp_console
from graftpunk.plugins.cli_plugin import CommandResult


@pytest.mark.parametrize(
    "helper", [gp_console.info, gp_console.warn, gp_console.error, gp_console.success]
)
def test_helpers_print_bracketed_text_verbatim(helper) -> None:
    buf = Console(file=io.StringIO(), width=200)
    helper("Saved: out [1/25 LB].csv", console=buf)
    assert "out [1/25 LB].csv" in buf.file.getvalue()


def test_export_table_has_no_ansi_under_force_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORCE_COLOR", "3")
    monkeypatch.delenv("NO_COLOR", raising=False)
    text = CommandResult(data=[{"name": "BRAND [/LB]", "qty": 1}]).export(format="table")
    assert "\x1b[" not in text
    assert "BRAND [/LB]" in text


@pytest.mark.parametrize(
    "helper", [gp_console.info, gp_console.warn, gp_console.error, gp_console.success]
)
def test_helpers_print_emoji_shortcodes_verbatim(helper) -> None:
    """#291: a ``:word:`` token is text a site or a user wrote, never an emoji."""
    buf = Console(file=io.StringIO(), width=200)
    helper("Name: :smile: LOT:1:BLOCK", console=buf)
    assert "Name: :smile: LOT:1:BLOCK" in buf.file.getvalue()


def test_the_ambiguous_session_list_prints_emoji_shortcodes_verbatim() -> None:
    buf = Console(file=io.StringIO(), width=200)
    gp_console.render_ambiguous_session("myshop", [("myshop:smile:", ":thumbs_up:")], console=buf)
    out = buf.file.getvalue()
    assert "myshop:smile:" in out
    assert ":thumbs_up:" in out


@pytest.mark.parametrize(
    "module",
    [
        "graftpunk.console",
        "graftpunk.cli.config_commands",
        "graftpunk.cli.keepalive_commands",
        "graftpunk.cli.main",
        "graftpunk.cli.observe_browser",
        "graftpunk.cli.observe_commands",
        "graftpunk.cli.plugin_runtime",
        "graftpunk.cli.scaffold_shared",
        "graftpunk.cli.session_commands",
    ],
)
def test_graftpunk_consoles_print_emoji_shortcodes_verbatim(module: str) -> None:
    """A path or a site's text interpolated into a markup string (escaped, so
    no markup) still went through Rich's emoji substitution."""
    mod = importlib.import_module(module)
    consoles = [
        c
        for name in ("console", "err_console", "out_console", "_format_console")
        if isinstance(c := getattr(mod, name, None), Console)
    ]
    assert consoles
    for c in consoles:
        with c.capture() as captured:
            c.print("Digest written: /tmp/:smile:/x")
        assert captured.get().strip() == "Digest written: /tmp/:smile:/x"


def test_export_table_keeps_emoji_shortcodes_in_headers_and_cells() -> None:
    text = CommandResult(data=[{":smile:": "a :thumbs_up: b"}]).export(format="table")
    assert ":smile:" in text
    assert "a :thumbs_up: b" in text
