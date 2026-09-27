"""Python-source formatting for generated files: width, indentation, quoting,
escaping, and wrapping.

Every helper here turns a captured site fact into Python source that parses
and that ``ruff format`` leaves unchanged at the generated project's line
length. ``render.py`` decides what a generated file says; this module decides
how each line of it is spelled (issue #201, item 2). A name another module
imports is public and listed in ``__all__``; the rest are private to this
module.
"""

from __future__ import annotations

import ast
import json
import re
import sys
import textwrap
from collections.abc import Iterable, Sequence
from typing import Protocol

__all__ = [
    "Binding",
    "GENERATED_LINE_LENGTH",
    "INDENT_STEP",
    "ImportPlacementError",
    "L1",
    "L2",
    "L3",
    "URL_PLACEHOLDER_RE",
    "binds_name",
    "call_expression_lines",
    "exploded_dict_lines",
    "given_entries_dict_lines",
    "import_lines",
    "joined_like",
    "literal_dict_entry_lines",
    "literal_lines",
    "quoted_literal",
    "source_lines",
    "url_expr_lines",
    "with_bindings",
    "with_import",
    "wrapped_comment_lines",
    "wrapped_docstring_block",
    "wrapped_docstring_lines",
]

# The line length a generated project's own [tool.ruff] declares (render.py's
# _render_pyproject writes it from this constant): every wrapping decision this
# module makes for generated content is against this one number, so the two
# cannot silently drift apart.
GENERATED_LINE_LENGTH = 100

# Indentation levels used when a generated command stub is exploded onto
# multiple lines (a class body; a method body; a call's arguments; an
# argument dict's entries), one owner each, so the levels cannot drift.
L1 = "    "
L2 = "        "
L3 = "            "
_L4 = "                "
INDENT_STEP = 4  # the step between the levels above, and one nesting level anywhere else
_DOCSTRING_WRAP_WIDTH = GENERATED_LINE_LENGTH - len(L2)


def _escaped_for_docstring(text: str) -> str:
    """*text* made safe to sit inside a ``\"\"\"`` docstring.

    A captured site fact reaches a generated docstring verbatim: the class
    docstring carries ``base_url``, a stub's summary carries its template and run
    label, and its shape line carries captured JSON keys. Embedded raw, a backslash
    starts an escape sequence nobody wrote (a trailing one swallows the closing
    quotes) and a ``\"\"\"`` ends the docstring early, so a URL or a key holding
    either renders a module that does not parse. A trailing quote is escaped too:
    it would otherwise sit against the closing quotes of the one-line form and
    close the string one character early.
    """
    escaped = text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    if escaped.endswith('"'):
        trailing = escaped[:-1]
        backslashes = len(trailing) - len(trailing.rstrip("\\"))
        if backslashes % 2 == 0:
            escaped = f'{trailing}\\"'
    return escaped


def _repaired_escape_splits(lines: list[str]) -> list[str]:
    """*lines* with any escape sequence that word-wrapping cut in half put back.

    ``textwrap`` breaks an over-long word at a character boundary, which can fall
    inside the ``\\\\`` or ``\\"`` that :func:`_escaped_for_docstring` introduced;
    the lone backslash left behind would read as a line continuation inside the
    docstring. Moving it onto the next line restores the pair exactly, and
    :func:`_escaped_docstring_wrap` wraps one column short of the budget so the
    moved character still fits the generated width.
    """
    repaired = list(lines)
    for index in range(len(repaired) - 1):
        line = repaired[index]
        if (len(line) - len(line.rstrip("\\"))) % 2:
            repaired[index] = line[:-1]
            repaired[index + 1] = f"\\{repaired[index + 1]}"
    return repaired


def _escaped_docstring_wrap(text: str, *, width: int) -> list[str]:
    """*text* escaped for a docstring, then word-wrapped to *width*.

    Escaping comes first so the width accounting sees the characters that are
    really emitted, and the repair pass undoes the one thing that ordering can
    break. ``break_on_hyphens=False`` for the same reason the comment wrapper
    sets it: every hyphen in this text belongs to a captured fact (a session
    name, a path segment, a JSON key), and breaking at one rendered a run label
    as ``run myshop-\\nrun-1``.
    """
    escaped = _escaped_for_docstring(text)
    wrapped = textwrap.wrap(escaped, width=max(1, width - 1), break_on_hyphens=False) or [escaped]
    return _repaired_escape_splits(wrapped)


def wrapped_docstring_lines(text: str) -> list[str]:
    """*text* escaped for a docstring and word-wrapped to fit a generated stub's
    docstring at ``L2`` indentation, each returned line already carrying that
    indentation."""
    wrapped = _escaped_docstring_wrap(text, width=_DOCSTRING_WRAP_WIDTH)
    return [f"{L2}{line}" for line in wrapped]


def wrapped_comment_lines(text: str, *, indent: int) -> list[str]:
    """*text* as one or more ``#``-prefixed comment lines at *indent* spaces: a
    captured URL or candidate name is unbounded, and ``E501`` applies to a comment
    line exactly as it does to code, so every comment this module emits routes
    through here rather than risking one long line. ``break_long_words`` means a
    URL with no spaces at all still cannot overflow; ``initial_indent`` and
    ``subsequent_indent`` give the first line ``# `` and every continuation line a
    hanging ``#   ``, with ``textwrap`` doing the width accounting for both.
    """
    pad = " " * indent
    return textwrap.wrap(
        text,
        width=GENERATED_LINE_LENGTH,
        initial_indent=f"{pad}# ",
        subsequent_indent=f"{pad}#   ",
        break_long_words=True,
        break_on_hyphens=False,
    ) or [f"{pad}# "]


def wrapped_docstring_block(text: str, *, indent: int) -> list[str]:
    """A one-line docstring ``\"\"\"{text}\"\"\"`` at *indent* spaces when that fits the
    generated width; otherwise the same text as a multi-line docstring with the
    closing quotes on their own line. *text* is escaped for a docstring in both
    shapes."""
    pad = " " * indent
    single_line = f'{pad}"""{_escaped_for_docstring(text)}"""'
    if len(single_line) <= GENERATED_LINE_LENGTH:
        return [single_line]
    wrapped = _escaped_docstring_wrap(text, width=max(1, GENERATED_LINE_LENGTH - indent))
    return [f'{pad}"""', *(f"{pad}{line}" for line in wrapped), f'{pad}"""']


def quoted_literal(value: str) -> str:
    """*value* as a complete Python string literal, quotes included.

    A captured site fact carries quoting of its own: ``har.documents`` builds a
    selector as ``form[action="/login"] input[name="user"]`` whenever the input has no
    id, and embedding that raw ends the literal at its first inner quote, so the
    generated module does not parse at all. ``json.dumps`` escapes exactly the
    characters a literal cannot hold (the quote, the backslash, the control
    characters) in a form Python reads the same way, and ``ensure_ascii=False`` leaves
    everything else as written. The quote character is the one ``ruff format`` would
    settle on (double, unless single quoting costs fewer escapes), so a generated file
    needs no second formatting pass.
    """
    quote = _quote_char(value)
    return f"{quote}{_escaped_body(value, quote)}{quote}"


def _quote_char(value: str) -> str:
    """The quote character ``ruff format`` would wrap *value* in: double, unless single
    quoting would cost fewer escapes."""
    return "'" if value.count('"') > value.count("'") else '"'


def _escaped_body(value: str, quote: str) -> str:
    """*value* as the body of a *quote*-quoted Python string literal, without the
    quotes themselves."""
    body = json.dumps(value, ensure_ascii=False)[1:-1]
    if quote == "'":
        body = body.replace('\\"', '"').replace("'", "\\'")
    return body


def _split_key_lines(key: str, *, indent: int) -> list[str]:
    """The opening of a parenthesised implicit-concatenation dict key: ``(`` at *indent*
    spaces and one quoted chunk of *key* per line below it.

    The caller supplies the closing line, which always begins ``):`` but differs after
    the colon by call site (an identifier, a quoted value, another concatenation).
    A parenthesised concatenation is a valid dict key, and ``ruff format`` leaves it
    alone once the joined form no longer fits the width.
    """
    pad = " " * indent
    continuation_pad = " " * (indent + INDENT_STEP)
    # A chunk is quoted after it is cut, so the budget comes off the whole key's own
    # quoting overhead (its two quotes plus whatever escaping it needs), which is an
    # upper bound on any one chunk's.
    overhead = len(quoted_literal(key)) - len(key)
    chunk_width = max(1, GENERATED_LINE_LENGTH - len(continuation_pad) - overhead)
    chunks = textwrap.wrap(
        key,
        width=chunk_width,
        break_long_words=True,
        break_on_hyphens=False,
        drop_whitespace=False,
    ) or [key]
    return [f"{pad}(", *(f"{continuation_pad}{quoted_literal(chunk)}" for chunk in chunks)]


def _dict_entry_lines(key: str, value: str, *, indent: int) -> list[str]:
    """One ``"key": value,`` dict entry at *indent* spaces, on one line when that fits
    the generated width.

    Past that width the key renders as a parenthesised implicit concatenation
    (``(\\n "par"\\n "t"\\n): value,``), which is a valid dict key and which
    ``ruff format`` leaves alone once the joined form no longer fits: a site's own
    parameter or header name is one fact, and it is the dict key, so neither the
    exploded dict nor any identifier cap can shorten it.
    """
    pad = " " * indent
    single_line = f"{pad}{quoted_literal(key)}: {value},"
    if len(single_line) <= GENERATED_LINE_LENGTH:
        return [single_line]
    return [*_split_key_lines(key, indent=indent), f"{pad}): {value},"]


def exploded_dict_lines(name: str, entries: list[tuple[str, str]]) -> list[str]:
    """A ``name={...}`` call argument, one ``"key": value,`` entry per line, with a
    magic trailing comma on the closing brace so ``ruff format`` leaves it exploded.
    Each *entries* pair is the site's own name for the key (quoted and, if it is too
    wide, split by ``_dict_entry_lines``) and the value expression."""
    lines = [f"{L3}{name}=" + "{"]
    for key, value in entries:
        lines.extend(_dict_entry_lines(key, value, indent=len(_L4)))
    lines.append(f"{L3}" + "},")
    return lines


def given_entries_dict_lines(name: str, entries: list[tuple[str, str]]) -> list[str]:
    """A ``name={...}`` call argument like :func:`exploded_dict_lines`, wrapped in a
    comprehension that leaves out every entry whose value is ``None``, in the shape
    ``ruff format`` gives it::

        name={
            key: value
            for key, value in {
                "field": field,
            }.items()
            if value is not None
        },
    """
    inner = " " * (len(L3) + INDENT_STEP)
    lines = [f"{L3}{name}=" + "{", f"{inner}key: value", f"{inner}for key, value in " + "{"]
    for key, value in entries:
        lines.extend(_dict_entry_lines(key, value, indent=len(inner) + INDENT_STEP))
    lines.extend([f"{inner}" + "}.items()", f"{inner}if value is not None", f"{L3}" + "},"])
    return lines


def call_expression_lines(prefix: str, args: list[str], *, indent: int) -> list[str]:
    """``{prefix}(arg, arg)`` at *indent* spaces on one line when it fits the generated
    width, otherwise one argument per line with a magic trailing comma so
    ``ruff format`` leaves it exploded. Both shapes are stable under the formatter, so
    the generated file needs no second pass either way."""
    pad = " " * indent
    single_line = f"{pad}{prefix}({', '.join(args)})"
    if len(single_line) <= GENERATED_LINE_LENGTH:
        return [single_line]
    continuation_pad = " " * (indent + INDENT_STEP)
    return [
        f"{pad}{prefix}(",
        *(f"{continuation_pad}{arg}," for arg in args),
        f"{pad})",
    ]


def literal_lines(
    value: str, *, indent: int, prefix: str = "", trailing_comma: bool = True
) -> list[str]:
    """A quoted Python string literal for a captured site fact (a selector, a URL, a
    header name): one line, ``{prefix}"{value}",`` at *indent* spaces, when that fits
    the generated width. Past that width a single quoted string can still overflow on
    its own even after a call or dict has been exploded one argument per line (a
    selector or header name is one fact ruff format itself never splits), so this
    falls back to an implicit string concatenation instead, each chunk from
    ``textwrap.wrap`` with whitespace preserved so the chunks rejoin to exactly
    *value*. ``trailing_comma=False``
    renders a plain assignment statement (``name = "value"``) rather than a call
    keyword argument (``name="value",``); both shapes reuse the same wrapping.
    """
    comma = "," if trailing_comma else ""
    pad = " " * indent
    quoted = quoted_literal(value)
    single_line = f"{pad}{prefix}{quoted}{comma}"
    if len(single_line) <= GENERATED_LINE_LENGTH:
        return [single_line]
    continuation_pad = " " * (indent + INDENT_STEP)
    # A chunk is quoted after it is cut, so the budget comes off the whole value's own
    # quoting overhead (see _split_key_lines).
    overhead = len(quoted) - len(value)
    chunk_width = max(1, GENERATED_LINE_LENGTH - len(continuation_pad) - overhead)
    chunks = textwrap.wrap(
        value,
        width=chunk_width,
        break_long_words=True,
        break_on_hyphens=False,
        drop_whitespace=False,
    ) or [value]
    lines = [f"{pad}{prefix}("]
    lines.extend(f"{continuation_pad}{quoted_literal(chunk)}" for chunk in chunks)
    lines.append(f"{pad}){comma}")
    return lines


def literal_dict_entry_lines(key: str, value: str, *, indent: int) -> list[str]:
    """One ``"key": "value",`` dict entry where both halves are captured site facts and
    either can be too wide for a line.

    The key keeps the whole line when it leaves room for the value's opening
    parenthesis; past that, the key splits too and the value follows on the ``):``
    line, which ``literal_lines`` renders by taking ``"): "`` as its prefix.
    """
    pad = " " * indent
    if len(f"{pad}{quoted_literal(key)}: (") <= GENERATED_LINE_LENGTH:
        return literal_lines(value, indent=indent, prefix=f"{quoted_literal(key)}: ")
    return [
        *_split_key_lines(key, indent=indent),
        *literal_lines(value, indent=indent, prefix="): "),
    ]


URL_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_]+)\}")


def _quoted_fstring(text: str) -> str:
    """*text* as a complete f-string literal: each literal span quoted the way
    ``quoted_literal`` quotes it and each brace outside a ``{placeholder}`` doubled, so a
    captured path holding a stray brace or a quote cannot emit a file that will not
    parse. The quote character is chosen once, for the whole literal, from the text
    outside the placeholders (a placeholder is an identifier and holds neither quote).
    """
    spans: list[str] = []
    parts: list[str] = []
    position = 0
    for match in URL_PLACEHOLDER_RE.finditer(text):
        spans.append(text[position : match.start()])
        parts.append(match.group(0))
        position = match.end()
    spans.append(text[position:])
    quote = _quote_char("".join(spans))
    bodies = [_escaped_body(span, quote).replace("{", "{{").replace("}", "}}") for span in spans]
    woven = [bodies[0]]
    for placeholder, body in zip(parts, bodies[1:], strict=True):
        woven.extend([placeholder, body])
    return f"f{quote}{''.join(woven)}{quote}"


def _url_atoms(text: str, *, width: int) -> list[str]:
    """*text* as the smallest pieces a URL may be split between: a whole
    ``{placeholder}``, and each ``/``-introduced literal segment (itself hard-split at
    *width* when one segment is wider than a line can hold)."""
    atoms: list[str] = []
    for piece in (p for p in re.split(r"(?=/)", text) if p):
        atoms.extend(piece[i : i + width] for i in range(0, len(piece), width))
    return atoms


def _url_chunks(text: str, *, width: int) -> list[str]:
    """*text* split into chunks that each fit *width*, preferring ``/`` boundaries and
    never splitting inside a ``{placeholder}``. The chunks rejoin to exactly *text*."""
    atoms: list[str] = []
    position = 0
    for match in URL_PLACEHOLDER_RE.finditer(text):
        atoms.extend(_url_atoms(text[position : match.start()], width=width))
        atoms.append(match.group(0))
        position = match.end()
    atoms.extend(_url_atoms(text[position:], width=width))
    chunks: list[str] = []
    for atom in atoms:
        if chunks and len(chunks[-1]) + len(atom) <= width:
            chunks[-1] += atom
        else:
            chunks.append(atom)
    return chunks or [text]


def url_expr_lines(url_text: str, *, is_fstring: bool, indent: int) -> list[str]:
    """The URL argument of a generated stub's request call: ``f"/a/{id}/b",`` on one
    line at *indent* spaces when that fits the generated width, otherwise the same
    string as a parenthesised implicit concatenation split at ``/`` boundaries. A
    captured path template is one fact ``ruff format`` cannot split for itself, and a
    deeply nested one is past the width on its own."""
    pad = " " * indent
    render = _quoted_fstring if is_fstring else quoted_literal
    single_line = f"{pad}{render(url_text)},"
    if len(single_line) <= GENERATED_LINE_LENGTH:
        return [single_line]
    continuation_pad = " " * (indent + INDENT_STEP)
    # A chunk is quoted after it is cut, so the budget comes off the whole template's
    # own literal overhead (its quotes, its `f`, and any escaping or brace doubling),
    # which is an upper bound on any one chunk's.
    overhead = len(render(url_text)) - len(url_text)
    chunk_width = max(1, GENERATED_LINE_LENGTH - len(continuation_pad) - overhead)
    lines = [f"{pad}("]
    lines.extend(
        f"{continuation_pad}{render(chunk)}" for chunk in _url_chunks(url_text, width=chunk_width)
    )
    lines.append(f"{pad}),")
    return lines


def import_lines(module: str, *names: str) -> list[str]:
    """``from {module} import {names}`` on one line when it fits the generated width,
    otherwise the parenthesised form, one name per line with a magic trailing comma,
    which is the shape ``ruff format`` would give it."""
    single_line = f"from {module} import {', '.join(names)}"
    if len(single_line) <= GENERATED_LINE_LENGTH:
        return [single_line]
    return [f"from {module} import (", *(f"{L1}{name}," for name in names), ")"]


def _is_stdlib(module: str) -> bool:
    """Whether *module* is in the standard library, the first isort section."""
    return module.split(".")[0] in sys.stdlib_module_names


# isort's default section order this module places into, after __future__ (handled
# on its own): the standard library, everything else, then the project's own
# top-level packages, each ``with_import`` caller supplies as ``first_party=``.
_STDLIB_SECTION, _THIRD_PARTY_SECTION, _FIRST_PARTY_SECTION = range(3)


def _section(module: str, first_party: frozenset[str]) -> int:
    """Which of the three isort sections *module* sorts into, given the project's
    own top-level package names in *first_party*."""
    if _is_stdlib(module):
        return _STDLIB_SECTION
    if module.split(".")[0] in first_party:
        return _FIRST_PARTY_SECTION
    return _THIRD_PARTY_SECTION


def _isort_name_key(name: str) -> tuple[int, str]:
    """isort's order-by-type within one import: CONSTANTS, then Classes, then the rest.
    An aliased name (``a as b``) sorts by the imported name."""
    bare = name.split(" as ")[0]
    if bare.isupper():
        return (0, bare)
    if bare[:1].isupper():
        return (1, bare)
    return (2, bare)


def _import_block_lines(imports: Iterable[tuple[str, str]]) -> list[str]:
    """``from module import names`` lines for (module, name) pairs, grouped the way isort
    groups them: the standard library first, then the rest, a blank line between
    the two, modules sorted and each module's names in isort's order."""
    by_module: dict[str, set[str]] = {}
    for module, name in imports:
        by_module.setdefault(module, set()).add(name)
    sections = (
        sorted(m for m in by_module if _is_stdlib(m)),
        sorted(m for m in by_module if not _is_stdlib(m)),
    )
    lines: list[str] = []
    for section in sections:
        if not section:
            continue
        if lines:
            lines.append("")
        for module in section:
            lines.extend(import_lines(module, *sorted(by_module[module], key=_isort_name_key)))
    return lines


def _target_names(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return set().union(*(_target_names(e) for e in target.elts))
    return set()


def binds_name(tree: ast.Module, name: str) -> bool:
    """Whether *tree* binds *name* at module level: the one binding predicate.

    A plain import, an aliased import, an assignment (tuple targets included), an
    annotated assignment with a value, a ``def``, and a ``class`` bind; a line
    wrapped in parentheses binds like any other. A star import does not, and
    neither does a conditional import or anything inside an ``if``, a ``try``,
    or a function: only the module's top-level statements count.
    """
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name != "*" and (alias.asname or alias.name.split(".")[0]) == name:
                    return True
        elif isinstance(node, ast.Assign):
            if any(name in _target_names(t) for t in node.targets):
                return True
        elif isinstance(node, ast.AnnAssign):
            if node.value is not None and name in _target_names(node.target):
                return True
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and (
            node.name == name
        ):
            return True
    return False


def _imported_module(node: ast.Import | ast.ImportFrom) -> str:
    if isinstance(node, ast.ImportFrom):
        return node.module or ""
    return node.names[0].name


def _joined(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")


def source_lines(text: str) -> list[str]:
    """*text* split into physical lines the way the tokenizer (and so ``ast`` line
    numbers) see them: on ``"\\r\\n"``, ``"\\r"``, and ``"\\n"`` only.

    ``str.splitlines()`` also breaks on characters the tokenizer does not (a form
    feed, a vertical tab, the file/group/record separators, NEL, and the Unicode
    line and paragraph separators), so indexing a ``str.splitlines()`` result by an
    ast line number desyncs by one for every such character above the target line:
    a stub lands mid-line, and the re-parse that follows can raise a bare
    ``SyntaxError`` past the caller (polish-r1 P2). No trailing empty element for a
    text ending in a line break, matching ``str.splitlines()``'s own convention, so
    every existing caller keeps its line count.
    """
    lines = _LINE_BREAK_RE.split(text)
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _line_ending(text: str) -> str:
    """The line ending *text* already uses: ``"\\r\\n"`` when it holds one, else
    ``"\\n"``."""
    return "\r\n" if "\r\n" in text else "\n"


def joined_like(original: str, lines: Sequence[str]) -> str:
    """*lines* rejoined as *original* was written: ``"\\r\\n"`` when *original* holds
    one, else ``"\\n"``, and a trailing newline only when *original* ends with one.

    ``write.read_original`` reads a file's bytes without universal-newline
    translation, so a CRLF file is compared and restored as CRLF; every writer
    that rebuilds a module from :func:`source_lines` (which discards the line
    ending each line had) rejoins through here instead of assuming LF, so the
    round trip keeps faith with what was actually on disk.
    """
    ending = _line_ending(original)
    joined = ending.join(lines)
    return joined + ending if original.endswith("\n") else joined


class ImportPlacementError(ValueError):
    """An import :func:`with_import` will not place: the module's imports are not in a
    shape it knows. Nothing is changed; the message says what to do instead."""


def with_import(
    text: str, module: str, name: str, *, first_party: frozenset[str] = frozenset()
) -> str:
    """*text* with ``from {module} import {name}`` in its module-level imports, placed
    the way isort places a from-import.

    Its contract is the shapes generated files have and nothing wider: the merge
    below, which a hand-written module reaches too (a test runs the project's own
    ruff over one), and a new line in the layout ``gp plugin new`` writes. It is
    not a general import sorter. Widening the shapes it places into is the point
    to stop placing imports here and run ``ruff check --fix --select I`` over the
    edited text instead.

    Unchanged when :func:`binds_name` says the module already binds *name*'s bound
    identifier (the alias, for ``"a as b"``; *name* itself otherwise). Otherwise
    merged into an existing ``from {module} import ...``, re-rendered in isort's
    name order, whatever the rest of the module looks like, unless a source line
    of that statement holds a comment: re-rendering it would drop the comment, so
    this refuses instead (below). Failing that, placed
    only into the shapes generated files have: module-level imports contiguous at
    the top (after a docstring and any ``__future__`` import) and in at most three
    isort sections, the standard library, everything else, and, for a module
    whose top-level package is in *first_party*, that package (isort's own
    default order; ``case-sensitive = false``, ``force-sort-within-sections``
    off). Within a section isort puts every plain ``import x`` before any
    ``from x import y``; the new line, always a from-import, goes before the
    first same-section from-import whose module sorts after it
    case-insensitively, else after the last same-section from-import, else
    after the last same-section plain import. For a section with no import yet,
    the new line's block goes before the first import of the next section that
    already has one, else after the last import of the previous section that
    does, else, for a module with no imports at all, after its docstring.

    Raises:
        ImportPlacementError: An import follows other code, a relative import is
            present, or a matching existing import's source lines hold a comment;
            the message says to add the import by hand (and, for the first two,
            to run ``ruff check --fix``).
    """
    tree = ast.parse(text)
    if binds_name(tree, name.split(" as ")[-1]):
        return text
    lines = source_lines(text)
    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    for node in imports:
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module == module
            and all(alias.name != "*" for alias in node.names)
        ):
            node_lines = lines[node.lineno - 1 : node.end_lineno or node.lineno]
            if any("#" in line for line in node_lines):
                raise ImportPlacementError(
                    f"cannot merge {name!r} into the existing 'from {module} import ...': its "
                    f"line holds a comment, which re-rendering the statement would drop. Add "
                    f"the name to it by hand."
                )
            names = [
                alias.name if alias.asname is None else f"{alias.name} as {alias.asname}"
                for alias in node.names
            ]
            merged = import_lines(module, *sorted([*names, name], key=_isort_name_key))
            return joined_like(
                text, lines[: node.lineno - 1] + merged + lines[node.end_lineno or node.lineno :]
            )
    new_line = f"from {module} import {name}"
    first = next(
        (i for i, n in enumerate(tree.body) if isinstance(n, (ast.Import, ast.ImportFrom))), 0
    )
    # At the top: nothing but a docstring before the first import, and no other
    # statement between the imports.
    at_top = first <= 1 and all(
        isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) for n in tree.body[:first]
    )
    contiguous = tree.body[first : first + len(imports)] == imports
    relative = any(isinstance(n, ast.ImportFrom) and n.level for n in imports)
    if not (at_top and contiguous) or relative:
        raise ImportPlacementError(
            f"cannot place {new_line!r}: the module's imports are not all at its top in "
            f"the shape generated files have. Add the line by hand, then run "
            f"`ruff check --fix` to sort it."
        )
    body = [n for n in imports if _imported_module(n) != "__future__"]
    section = _section(module, first_party)
    same_section = [n for n in body if _section(_imported_module(n), first_party) == section]
    # isort (force-sort-within-sections off) puts every plain "import x" before a
    # section's from-imports; the new line is always a from-import, so only a
    # same-section from-import can be "later" than it, and a same-section-with-
    # no-from-imports falls back to going after the plain imports.
    same_section_from = [n for n in same_section if isinstance(n, ast.ImportFrom)]
    later = [n for n in same_section_from if _imported_module(n).lower() > module.lower()]
    if later:
        at, insert = later[0].lineno - 1, [new_line]
    elif same_section_from:
        at, insert = same_section_from[-1].end_lineno or same_section_from[-1].lineno, [new_line]
    elif same_section:
        at, insert = same_section[-1].end_lineno or same_section[-1].lineno, [new_line]
    elif body:
        # This section has no import yet: place its new block before the first
        # import of a later section that already has one, else after the last
        # import of an earlier section that does (source order == section order,
        # the "generated files have" shape this function's contract is limited to).
        higher = [n for n in body if _section(_imported_module(n), first_party) > section]
        if higher:
            at, insert = higher[0].lineno - 1, [new_line, ""]
        else:
            lower = [n for n in body if _section(_imported_module(n), first_party) < section]
            at, insert = lower[-1].end_lineno or lower[-1].lineno, ["", new_line]
    else:
        leading = [
            n
            for n in tree.body[:2]
            if (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
            or (isinstance(n, ast.ImportFrom) and n.module == "__future__")
        ]
        at = (leading[-1].end_lineno or leading[-1].lineno) if leading else 0
        insert = ["", new_line] if at else [new_line, ""]
    return joined_like(text, lines[:at] + insert + lines[at:])


class Binding(Protocol):
    """A module-level statement and the ``(module, name)`` imports it reads.

    ``policy.ProjectRequirement`` is one; this module imports nothing from
    graftpunk, so it names the shape rather than the class."""

    @property
    def statement(self) -> str: ...

    @property
    def imports(self) -> tuple[tuple[str, str], ...]: ...


def with_bindings(
    text: str, bindings: Sequence[Binding], *, first_party: frozenset[str] = frozenset()
) -> str:
    """*text* with each binding's imports merged and its statement appended, in order.

    The one assembler for statements a generator or a migrator adds to a module,
    so the two cannot disagree about layout. The blank-line rule, stated once: a
    statement follows the one before it on the next line, and follows a ``def``
    or a ``class`` after two blank lines, which is what ``ruff format`` keeps. Text
    with nothing in it gets the imports as one isort block, a blank line, and then
    the statements. *first_party* is :func:`with_import`'s argument of the same
    name, passed through to every import it merges or places.
    """
    if not text.strip():
        imports = [pair for binding in bindings for pair in binding.imports]
        head = [*_import_block_lines(imports), ""] if imports else []
        return _joined([*head, *(binding.statement for binding in bindings)])
    for binding in bindings:
        for module, name in binding.imports:
            text = with_import(text, module, name, first_party=first_party)
        body = ast.parse(text).body
        after_definition = bool(body) and isinstance(
            body[-1], (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        )
        ending = _line_ending(text)
        separator = ending * 3 if after_definition else ending
        text = text.rstrip("\r\n") + separator + binding.statement + ending
    return text
