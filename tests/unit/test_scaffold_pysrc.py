"""pysrc.py owns the Python-source formatting helpers and render.py keeps the
per-artifact renderers (graft skill spec, 2026-09-21; issue #201, item 2)."""

from __future__ import annotations

import ast
import subprocess
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

import graftpunk.devtools.scaffold.pysrc as pysrc
import graftpunk.devtools.scaffold.render as render
from graftpunk.devtools.scaffold.pysrc import (
    _DOCSTRING_WRAP_WIDTH,
    GENERATED_LINE_LENGTH,
    ImportPlacementError,
    _dict_entry_lines,
    _url_chunks,
    binds_name,
    source_lines,
    with_bindings,
    with_import,
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


class TestBindsName:
    """The one binding predicate: the project reader decides a requirement's presence
    with it, and with_import decides whether an import is needed."""

    @pytest.mark.parametrize(
        "source",
        [
            "from pathlib import Path\n",
            "from elsewhere import thing as Path\n",
            "import Path\n",
            "Path = 1\n",
            "Path, other = 1, 2\n",
            "Path: type = object\n",
            "Path = (\n    object\n)\n",
            "def Path() -> None:\n    pass\n",
            "class Path:\n    pass\n",
        ],
    )
    def test_a_module_level_binding_binds(self, source: str) -> None:
        assert binds_name(ast.parse(source), "Path")

    @pytest.mark.parametrize(
        "source",
        [
            "",
            "from pathlib import *\n",
            "import pathlib.Path\n",
            "Path: type\n",
            "if True:\n    from pathlib import Path\n",
            "try:\n    from pathlib import Path\nexcept ImportError:\n    pass\n",
            "def f() -> None:\n    Path = 1\n",
            "class C:\n    Path = 1\n",
        ],
    )
    def test_a_star_a_conditional_or_a_nested_binding_does_not(self, source: str) -> None:
        assert not binds_name(ast.parse(source), "Path")


class TestSourceLines:
    """The one line splitter every ast-line-number consumer must use: str.splitlines()
    also breaks on characters the tokenizer does not, which desyncs an ast line
    number from a str.splitlines() index (polish-r1 P2)."""

    @pytest.mark.parametrize("break_char", ["\x0c", "\x0b", "\x1c", "\x1d", "\x1e", "\x85", " "])
    def test_a_character_str_splitlines_treats_as_a_break_does_not_split(
        self, break_char: str
    ) -> None:
        text = f"a{break_char}b\n"
        assert source_lines(text) == [f"a{break_char}b"]
        assert len(text.splitlines()) == 2  # the desync source_lines exists to avoid

    @pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
    def test_every_real_line_break_splits(self, newline: str) -> None:
        assert source_lines(f"a{newline}b{newline}") == ["a", "b"]

    def test_matches_str_splitlines_on_ordinary_text(self) -> None:
        text = "a\nb\n\nc"
        assert source_lines(text) == text.splitlines()

    def test_empty_text_is_no_lines(self) -> None:
        assert source_lines("") == []


_OLD_CONFTEST = (
    "from graftpunk.testing.plugin import site_env_scrubber\n"
    "\n"
    'scrub_site_env = site_env_scrubber("MYSHOP_")\n'
)


class TestWithImport:
    def test_a_name_merges_into_the_same_modules_import(self) -> None:
        result = with_import(_OLD_CONFTEST, "graftpunk.testing.plugin", "fixtures_are_sanitised")
        assert result.splitlines()[0] == (
            "from graftpunk.testing.plugin import fixtures_are_sanitised, site_env_scrubber"
        )

    def test_a_stdlib_import_goes_first_with_a_blank_line_after(self) -> None:
        result = with_import(_OLD_CONFTEST, "pathlib", "Path")
        assert result.splitlines()[:3] == [
            "from pathlib import Path",
            "",
            "from graftpunk.testing.plugin import site_env_scrubber",
        ]

    def test_a_name_already_bound_leaves_the_text_alone(self) -> None:
        assert (
            with_import(_OLD_CONFTEST, "graftpunk.testing.plugin", "site_env_scrubber")
            == _OLD_CONFTEST
        )
        aliased = "from elsewhere import thing as Path\n"
        assert with_import(aliased, "pathlib", "Path") == aliased

    def test_an_aliased_name_already_bound_leaves_the_text_alone(self) -> None:
        """The name with_import is asked to place can itself be "a as b" (render.py
        asks for "quote as _quote_path"); the module already binds it exactly the
        same way, so nothing is appended."""
        text = "from urllib.parse import quote as _quote_path\n"
        assert with_import(text, "urllib.parse", "quote as _quote_path") == text

    def test_a_module_with_no_imports_gets_one_after_its_docstring(self) -> None:
        result = with_import('"""Doc."""\n\nx = 1\n', "pathlib", "Path")
        assert result == '"""Doc."""\n\nfrom pathlib import Path\n\nx = 1\n'

    @pytest.mark.parametrize(
        "text",
        [
            "x = 1\nfrom os import sep\n",
            "from . import sibling\n",
        ],
    )
    def test_a_shape_it_does_not_place_into_is_refused_with_the_ruff_hint(self, text: str) -> None:
        with pytest.raises(ImportPlacementError, match="ruff check --fix"):
            with_import(text, "pathlib", "Path")

    def test_a_new_from_import_goes_after_a_plain_import_in_the_same_section(
        self, tmp_path: Path
    ) -> None:
        """isort (force-sort-within-sections off) puts every plain "import x" before
        a section's from-imports; the new line is always a from-import and must not
        jump ahead of one."""
        result = with_import("import sys\n", "pathlib", "Path")
        assert result == "import sys\nfrom pathlib import Path\n"
        (tmp_path / "pyproject.toml").write_text(_PROJECT_RUFF)
        (tmp_path / "mod.py").write_text(result)
        for argv in (["check", "--select", "I", "mod.py"], ["format", "--check", "mod.py"]):
            check = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
                [sys.executable, "-m", "ruff", *argv], cwd=tmp_path, capture_output=True, text=True
            )
            assert check.returncode == 0, check.stdout + check.stderr

    def test_a_merge_needs_no_known_shape(self) -> None:
        """Merging into an existing same-module import is always safe, so an odd
        layout elsewhere does not stop it."""
        text = "from pathlib import PurePath\nx = 1\nfrom os import sep\n"
        assert with_import(text, "pathlib", "Path").splitlines()[0] == (
            "from pathlib import Path, PurePath"
        )

    def test_a_form_feed_in_a_comment_above_the_import_does_not_desync_the_splice(self) -> None:
        """str.splitlines() also breaks on a form feed, which ast does not count as
        a line; with the old splitter this desync shifted the new import onto the
        wrong line and could corrupt the form-feed line itself (polish-r1 P2, A4)."""
        text = "# page\x0c break\n\nfrom pathlib import PurePath\n\nx = 1\n"
        result = with_import(text, "pathlib", "Path")
        assert result == "# page\x0c break\n\nfrom pathlib import Path, PurePath\n\nx = 1\n"
        ast.parse(result)


_HAND_WRITTEN_PLUGIN = '''\
"""myshop plugin, written by hand."""

from __future__ import annotations

from graftpunk.plugins import SitePlugin, command


class MyshopPlugin(SitePlugin):
    site_name = "myshop"

    @command(help="List orders", params=[PluginParamSpec.option("page", type=int)])
    def orders(self, ctx: CommandContext, page: int | None = None) -> dict:
        return ctx.request_json("GET", "/api/orders", params={"page": page})
'''

# The lint a generated project's own pyproject.toml declares.
_PROJECT_RUFF = (
    '[tool.ruff]\nline-length = 100\n\n[tool.ruff.lint]\nselect = ["E", "F", "I", "UP", "B"]\n'
)


def test_a_merge_into_a_hand_written_module_passes_the_projects_ruff(tmp_path: Path) -> None:
    """The merge path re-renders an existing from-import in isort's order, and a
    hand-written module reaches it through gp plugin add-command; the project's
    own ruff has to agree with the result."""
    text = with_import(_HAND_WRITTEN_PLUGIN, "graftpunk.plugins", "CommandContext")
    text = with_import(text, "graftpunk.plugins", "PluginParamSpec")
    (tmp_path / "pyproject.toml").write_text(_PROJECT_RUFF)
    (tmp_path / "plugin.py").write_text(text)
    for argv in (["check", "plugin.py"], ["format", "--check", "plugin.py"]):
        result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [sys.executable, "-m", "ruff", *argv], cwd=tmp_path, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stdout + result.stderr


@dataclass(frozen=True)
class _Statement:
    statement: str
    imports: tuple[tuple[str, str], ...] = ()


class TestWithBindings:
    """The one assembler: the renderer and gp plugin upgrade both add statements
    through it, so there is one blank-line rule."""

    def test_empty_text_gets_one_import_block_then_the_statements(self) -> None:
        result = with_bindings(
            "", [_Statement("x = Path()", (("pathlib", "Path"),)), _Statement("y = 1")]
        )
        assert result == "from pathlib import Path\n\nx = Path()\ny = 1\n"

    def test_empty_text_and_no_imports_is_just_the_statements(self) -> None:
        assert with_bindings("", [_Statement("y = 1")]) == "y = 1\n"

    def test_a_statement_after_a_definition_gets_two_blank_lines(self) -> None:
        result = with_bindings(
            "def f() -> int:\n    return 1\n", [_Statement("y = 1"), _Statement("z = 2")]
        )
        assert result == "def f() -> int:\n    return 1\n\n\ny = 1\nz = 2\n"

    def test_imports_merge_into_the_existing_block(self) -> None:
        pair = ("graftpunk.testing.plugin", "fixtures_are_sanitised")
        result = with_bindings(_OLD_CONFTEST, [_Statement("x = fixtures_are_sanitised", (pair,))])
        assert result.splitlines()[0] == (
            "from graftpunk.testing.plugin import fixtures_are_sanitised, site_env_scrubber"
        )
        assert result.endswith(
            'scrub_site_env = site_env_scrubber("MYSHOP_")\nx = fixtures_are_sanitised\n'
        )
