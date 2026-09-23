"""pysrc.py owns the Python-source formatting helpers and render.py keeps the
per-artifact renderers (graft skill spec, 2026-09-21; issue #201, item 2)."""

from __future__ import annotations

import ast
from pathlib import Path
from types import ModuleType

import graftpunk.devtools.scaffold.pysrc as pysrc
import graftpunk.devtools.scaffold.render as render

_FORMATTING_HELPERS = frozenset(
    {
        "_GENERATED_LINE_LENGTH",
        "_L1",
        "_L2",
        "_L3",
        "_L4",
        "_INDENT_STEP",
        "_DOCSTRING_WRAP_WIDTH",
        "_escaped_for_docstring",
        "_repaired_escape_splits",
        "_escaped_docstring_wrap",
        "_wrapped_docstring_lines",
        "_wrapped_comment_lines",
        "_wrapped_docstring_block",
        "_quoted",
        "_quote_char",
        "_escaped_body",
        "_split_key_lines",
        "_dict_entry_lines",
        "_exploded_dict_lines",
        "_call_lines",
        "_literal_lines",
        "_literal_dict_entry_lines",
        "_URL_PLACEHOLDER_RE",
        "_quoted_fstring",
        "_url_atoms",
        "_url_chunks",
        "_url_expr_lines",
        "_import_lines",
    }
)


def _defined_at_module_level(module: ModuleType) -> set[str]:
    assert module.__file__ is not None
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_pysrc_defines_every_formatting_helper() -> None:
    assert _defined_at_module_level(pysrc) >= _FORMATTING_HELPERS


def test_render_defines_none_of_the_formatting_helpers() -> None:
    assert _FORMATTING_HELPERS & _defined_at_module_level(render) == set()


def test_pysrc_imports_nothing_from_graftpunk() -> None:
    """The helpers format text; they know nothing about digests or specs."""
    assert pysrc.__file__ is not None
    tree = ast.parse(Path(pysrc.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert {name for name in imported if name.startswith("graftpunk")} == set()
