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

import json
import re
import textwrap

__all__ = [
    "GENERATED_LINE_LENGTH",
    "INDENT_STEP",
    "L1",
    "L2",
    "L3",
    "URL_PLACEHOLDER_RE",
    "call_expression_lines",
    "exploded_dict_lines",
    "import_lines",
    "literal_dict_entry_lines",
    "literal_lines",
    "quoted_literal",
    "url_expr_lines",
    "wrapped_comment_lines",
    "wrapped_docstring_block",
    "wrapped_docstring_lines",
]

# The line length a generated project's own [tool.ruff] declares (_render_pyproject
# below): every wrapping decision this module makes for generated content is
# against this one number, so the two cannot silently drift apart.
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
    close the string one character early (final fix wave, 2026-09-12).
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
    as ``run myshop-\\nrun-1`` (polish round 2, 2026-09-12).
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
    through here rather than risking one long line (validation fix round 3, Finding
    open in round 2, 2026-09-12). ``break_long_words`` means a URL with no spaces at
    all still cannot overflow; ``initial_indent``/``subsequent_indent`` give the
    first line ``# `` and every continuation line the hanging ``#   `` the ruling
    asked for, with ``textwrap`` doing the width accounting for both.
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
    closing quotes on their own line (validation fix round 3, 2026-09-12). *text* is
    escaped for a docstring in both shapes (final fix wave, 2026-09-12)."""
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
    needs no second formatting pass (validation fix round 4, 2026-09-12).
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
    exploded dict nor any identifier cap can shorten it (validation fix round 4,
    Finding 4, 2026-09-12).
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
    *value* (validation fix round 2, Finding 4, 2026-09-12). ``trailing_comma=False``
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
    deeply nested one is past the width on its own (validation fix round 4, Finding 4,
    2026-09-12)."""
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


def import_lines(module: str, name: str) -> list[str]:
    """``from {module} import {name}`` on one line when it fits the generated width,
    otherwise the parenthesised form with a magic trailing comma. The generated
    package name and class name are both plugin-name derivatives, so together they
    can still pass the width even with the name capped at ``_MAX_PLUGIN_NAME``, and
    ``ruff format`` would split the single line for itself."""
    single_line = f"from {module} import {name}"
    if len(single_line) <= GENERATED_LINE_LENGTH:
        return [single_line]
    return [f"from {module} import (", f"{L1}{name},", ")"]
