"""Guard: no external-Click imports under graftpunk/cli/.

The typer>=0.26 breakage happened because externally-built click objects
entered Typer's vendored-Click runtime (see the 2026-07-19 RFC). The CLI is
now Typer-native; this test keeps the boundary from silently regrowing.
"""

from __future__ import annotations

import re
from pathlib import Path

import graftpunk.cli

CLI_DIR = Path(graftpunk.cli.__file__).parent
_IMPORT_CLICK = re.compile(r"^\s*(import click\b|from click(\.|\s+import\b))", re.MULTILINE)


def test_no_external_click_imports_in_cli() -> None:
    offenders = {
        str(path.relative_to(CLI_DIR)): matches
        for path in sorted(CLI_DIR.rglob("*.py"))
        if (matches := _IMPORT_CLICK.findall(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        f"external Click import(s) found under graftpunk/cli/: {sorted(offenders)}. "
        "Use typer-native declarations (typer.Option/Argument/Context/prompt); "
        "see docs/rfcs/2026-07-19-typer-native-plugin-commands.md."
    )
