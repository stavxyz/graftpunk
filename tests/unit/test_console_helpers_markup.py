"""gp_console helpers and CommandResult.export must treat their input as data (#145/#144)."""

from __future__ import annotations

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
