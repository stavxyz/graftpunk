"""Layout rule for ``graftpunk/cli/``: ``main.py`` is the sole composition
root, so no other module in the package may import it back."""

from __future__ import annotations

import ast
from pathlib import Path

import graftpunk


class TestObserveBrowserStaysALeaf:
    """observe_browser's docstring claims nothing here imports a command module back."""

    def test_it_imports_no_cli_module(self) -> None:
        source = Path(graftpunk.__file__).parent.joinpath("cli", "observe_browser.py").read_text()
        tree = ast.parse(source)
        cli_imports = sorted(
            {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                and node.module is not None
                and node.module.startswith("graftpunk.cli")
            }
            | {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
                if alias.name.startswith("graftpunk.cli")
            }
            | {
                # A relative import (from . import observe_commands) has no
                # module prefix to match on, so it is caught by its level.
                "." * node.level + (node.module or "")
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.level > 0
            }
        )

        assert cli_imports == []


def _imports_cli_main(path: Path) -> bool:
    """True if the module at *path* imports ``graftpunk.cli.main``, absolute or relative."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "graftpunk.cli.main" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module == "graftpunk.cli.main":
                return True
            if node.level > 0 and node.module == "main":
                return True
            if (
                node.level > 0
                and node.module is None
                and any(alias.name == "main" for alias in node.names)
            ):
                return True
    return False


class TestMainStaysTheCompositionRoot:
    """`main.py` composes every sub-app; nothing else in the package may import it back."""

    def test_no_cli_module_imports_main(self) -> None:
        package = Path(graftpunk.__file__).parent / "cli"
        offenders = {
            path.name
            for path in sorted(package.glob("*.py"))
            if path.name != "__init__.py" and _imports_cli_main(path)
        }

        assert offenders == set()
