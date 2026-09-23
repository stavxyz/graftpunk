"""pysrc.py owns the Python-source formatting helpers and render.py keeps the
per-artifact renderers (graft skill spec, 2026-09-21; issue #201, item 2)."""

from __future__ import annotations

import ast
import warnings
from pathlib import Path
from types import ModuleType

import graftpunk.devtools.scaffold.pysrc as pysrc
import graftpunk.devtools.scaffold.render as render
from graftpunk.devtools.scaffold.pysrc import (
    _DOCSTRING_WRAP_WIDTH,
    GENERATED_LINE_LENGTH,
    _dict_entry_lines,
    _url_chunks,
    wrapped_docstring_block,
    wrapped_docstring_lines,
)

_TESTS = Path(__file__).parents[1]
# The packages whose private names no other module's test may import. Tests
# elsewhere in the suite predate this rule and still reach across modules for
# privates; they are not held to it here.
_GUARDED_PACKAGES = (
    "graftpunk.contracts",
    "graftpunk.devtools",
    "graftpunk.har",
    "graftpunk.testing",
)

_FORMATTING_HELPERS = frozenset(
    {
        "GENERATED_LINE_LENGTH",
        "L1",
        "L2",
        "L3",
        "_L4",
        "INDENT_STEP",
        "_DOCSTRING_WRAP_WIDTH",
        "_escaped_for_docstring",
        "_repaired_escape_splits",
        "_escaped_docstring_wrap",
        "wrapped_docstring_lines",
        "wrapped_comment_lines",
        "wrapped_docstring_block",
        "quoted_literal",
        "_quote_char",
        "_escaped_body",
        "_split_key_lines",
        "_dict_entry_lines",
        "exploded_dict_lines",
        "call_expression_lines",
        "literal_lines",
        "literal_dict_entry_lines",
        "URL_PLACEHOLDER_RE",
        "_quoted_fstring",
        "_url_atoms",
        "_url_chunks",
        "url_expr_lines",
        "import_lines",
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


def test_every_name_another_module_imports_is_public_and_exported() -> None:
    """No module imports a private name from pysrc: what it shares is its __all__."""
    package = Path(pysrc.__file__).parents[2]
    imported: set[str] = set()
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported |= {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "graftpunk.devtools.scaffold.pysrc"
            for alias in node.names
        }
    assert imported
    assert imported <= set(pysrc.__all__)
    assert not [name for name in pysrc.__all__ if name.startswith("_")]


def _is_own_subject(test_path: Path, module: str) -> bool:
    """True when *module* is the subject of the test module at *test_path*: the
    module's dotted path, dots read as underscores, ends with the test file's name
    after ``test_`` (``test_har_digest.py`` for ``graftpunk.har.digest``,
    ``test_contracts.py`` for ``graftpunk.contracts``)."""
    return module.replace(".", "_").endswith(test_path.stem.removeprefix("test_"))


def test_no_test_imports_another_modules_private_names() -> None:
    """A test reaches a module's private names only when that module is its own
    subject (the exemption rule in _is_own_subject, so test_contracts.py may import
    contracts._CURRENT); any other module's test goes through public names."""
    offenders = []
    for path in sorted(_TESTS.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not node.module.startswith(_GUARDED_PACKAGES):
                continue
            if _is_own_subject(path, node.module):
                continue
            private = [
                alias.name
                for alias in node.names
                if alias.name.startswith("_") and not alias.name.startswith("__")
            ]
            if private:
                offenders.append(
                    f"{path.relative_to(_TESTS)}:{node.lineno}: {node.module} {private}"
                )
    assert offenders == []


def test_the_subject_rule_exempts_only_a_modules_own_test() -> None:
    assert _is_own_subject(Path("test_contracts.py"), "graftpunk.contracts")
    assert _is_own_subject(Path("test_scaffold_pysrc.py"), "graftpunk.devtools.scaffold.pysrc")
    assert not _is_own_subject(Path("test_scaffold_render.py"), "graftpunk.devtools.scaffold.pysrc")
    assert not _is_own_subject(Path("test_scaffold_render.py"), "graftpunk.har.digest")


class TestDictEntryLines:
    def test_short_key_renders_on_one_line(self) -> None:
        assert _dict_entry_lines("page", "page", indent=16) == ['                "page": page,']

    def test_long_key_splits_and_rejoins_exactly(self) -> None:
        key = "x" * 120
        indent = 16
        lines = _dict_entry_lines(key, "identifier", indent=indent)
        pad = " " * indent
        continuation_pad = " " * (indent + 4)
        assert lines[0] == f"{pad}("
        assert lines[-1] == f"{pad}): identifier,"
        chunks = [line[len(continuation_pad) + 1 : -1] for line in lines[1:-1]]
        assert "".join(chunks) == key
        for line in lines:
            assert len(line) <= GENERATED_LINE_LENGTH


class TestUrlChunks:
    def test_chunks_rejoin_exactly(self) -> None:
        text = "/api/v2/customer-accounts/{account_id}/payment-methods/default-billing-address"
        chunks = _url_chunks(text, width=30)
        assert len(chunks) > 1
        assert "".join(chunks) == text

    def test_no_chunk_boundary_falls_inside_a_placeholder(self) -> None:
        text = "/a/{account_id}/b/{payment_method_id}/c"
        for width in range(4, 40):
            chunks = _url_chunks(text, width=width)
            assert "".join(chunks) == text
            for chunk in chunks:
                assert chunk.count("{") == chunk.count("}")

    def test_a_single_segment_wider_than_the_width_is_hard_split(self) -> None:
        text = "/" + "s" * 250
        chunks = _url_chunks(text, width=40)
        assert "".join(chunks) == text
        for chunk in chunks:
            assert len(chunk) <= 40

    def test_a_chunk_starts_at_a_slash_when_it_can(self) -> None:
        text = "/alpha/beta/gamma/delta"
        chunks = _url_chunks(text, width=12)
        assert "".join(chunks) == text
        for chunk in chunks[1:]:
            assert chunk.startswith("/")


class TestDocstringEscaping:
    """Text that reaches a generated docstring reads back unchanged."""

    @staticmethod
    def _read_back(lines: list[str]) -> str:
        source = "\n".join(["def f():", '    """', *lines, '    """', "    pass"])
        with warnings.catch_warnings():
            warnings.simplefilter("error", SyntaxWarning)
            module = ast.parse(source)
        function = module.body[0]
        assert isinstance(function, ast.FunctionDef)
        return ast.get_docstring(function) or ""

    def test_a_triple_quote_and_a_backslash_survive_the_round_trip(self) -> None:
        text = 'GET /a\\b: shape object{"""k", tail\\}'
        read_back = self._read_back(wrapped_docstring_lines(text))
        assert " ".join(read_back.split()) == " ".join(text.split())

    def test_an_escape_cut_in_half_by_wrapping_is_put_back(self) -> None:
        # One unbroken word long enough that textwrap breaks it mid-character,
        # with the backslash sitting exactly on the break: the half left behind
        # would otherwise read as a line continuation inside the docstring.
        text = "x" * (_DOCSTRING_WRAP_WIDTH - 2) + "\\" + "y" * 60
        lines = wrapped_docstring_lines(text)
        assert len(lines) > 1, "the input must actually wrap for this test to mean anything"
        assert self._read_back(lines).replace("\n", "") == text

    def test_a_trailing_quote_does_not_close_the_one_line_form_early(self) -> None:
        block = wrapped_docstring_block('Commands for https://myshop.example.com/"', indent=0)
        assert len(block) == 1
        source = "\n".join(["class C:", f"    {block[0]}", "    pass"])
        module = ast.parse(source)
        klass = module.body[0]
        assert isinstance(klass, ast.ClassDef)
        assert ast.get_docstring(klass) == 'Commands for https://myshop.example.com/"'

    def test_a_backslash_before_the_trailing_quote_still_gets_escaped(self) -> None:
        text = 'a\\"'
        block = wrapped_docstring_block(text, indent=0)
        assert len(block) == 1
        source = "\n".join(["class C:", f"    {block[0]}", "    pass"])
        module = ast.parse(source)
        klass = module.body[0]
        assert isinstance(klass, ast.ClassDef)
        assert ast.get_docstring(klass) == text
