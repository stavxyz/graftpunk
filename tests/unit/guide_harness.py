"""The plugin guide, read, and gp invocations checked against the real CLI.

Shared by the guide's own test (tests/unit/test_plugin_development_guide.py),
the project gate test (tests/unit/test_project_gate.py), and the graft skill's
tests, so no test module imports another. Nothing here is a test and nothing
here runs a gp command: the CLI is inspected through typer.main.get_command.
"""

from __future__ import annotations

import re
import shlex
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

import typer.main

from graftpunk.cli.main import app


def _repo_root() -> Path:
    """The directory holding pyproject.toml, found by walking up from this file."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError("pyproject.toml not found above this test file")


REPO_ROOT = _repo_root()
GUIDE = REPO_ROOT / "docs" / "PLUGIN_DEVELOPMENT.md"
GUIDE_TEXT = GUIDE.read_text(encoding="utf-8")


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")

# CommonMark 0.31.2, section 2.1 "Characters and lines": a line ends at a line
# feed, a carriage return, or the two together, and at nothing else.
_LINE_ENDING_RE = re.compile(r"\r\n|\r|\n")


def _lines(text: str) -> list[str]:
    """The lines of *text*, split only where CommonMark ends a line.

    ``str.splitlines`` also splits on a vertical tab, a form feed, the file, group,
    and record separators, U+0085, U+2028, and U+2029, so one of those pasted into a document would shift every later
    line number and could end a code span or a fence early. As with
    ``splitlines``, a line ending at the very end adds no empty last line.
    """
    lines = _LINE_ENDING_RE.split(text)
    if lines[-1] == "":
        lines.pop()
    return lines

# CommonMark 0.31.2, section 4.5 "Fenced code blocks"
# (https://spec.commonmark.org/0.31.2/#fenced-code-blocks).
_FENCE_OPEN_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")


@dataclass(frozen=True)
class Fence:
    """One fenced code block: its fence lines (1-based, inclusive), info string, and
    content lines. An unclosed block runs to the end of the text, so ``close`` is
    then its last line."""

    open: int
    close: int
    info: str
    lines: tuple[str, ...]

    @property
    def language(self) -> str:
        """The info string's first word, the conventional language name."""
        return self.info.split()[0] if self.info else ""


def fences(text: str) -> list[Fence]:
    """Every fenced code block in *text*, by CommonMark 0.31.2 section 4.5
    (https://spec.commonmark.org/0.31.2/#fenced-code-blocks).

    A fence is a run of at least three backticks or at least three tildes, never
    mixed, indented by at most three spaces. After a backtick fence the info
    string may not hold a backtick (such a line is prose with code spans in it).
    The block closes at the next line holding only a fence of the same character
    at least as long as the opening one (indented by at most three spaces, and
    followed only by spaces or tabs), or else at the end of the text. Up to as
    many spaces as indent the opening fence are removed from each content line.

    Container blocks (block quotes, list items) are not modeled: a fence inside
    one is found only when its lines start the way a top-level fence's would.
    """
    found: list[Fence] = []
    lines = _lines(text)
    index = 0
    while index < len(lines):
        opening = _FENCE_OPEN_RE.match(lines[index])
        if opening is None or (opening.group(2)[0] == "`" and "`" in opening.group(3)):
            index += 1
            continue
        indent, fence, info = len(opening.group(1)), opening.group(2), opening.group(3)
        closing = re.compile(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$")
        end = next((i for i in range(index + 1, len(lines)) if closing.match(lines[i])), len(lines))
        content = tuple(
            line[min(indent, len(line) - len(line.lstrip(" "))) :]
            for line in lines[index + 1 : end]
        )
        close = min(end, len(lines) - 1)
        found.append(Fence(index + 1, close + 1, info.strip(" \t"), content))
        index = end + 1
    return found


def blocks(text: str, language: str) -> list[tuple[int, str]]:
    """Every fenced block of *language* in *text*, as (1-based opening fence line,
    body). Body line *k* (1-based) sits on line ``start + k`` of *text*."""
    return [
        (fence.open, "".join(f"{line}\n" for line in fence.lines))
        for fence in fences(text)
        if fence.language == language
    ]


def _fenced_line_numbers(text: str) -> set[int]:
    """Every 1-based line number inside a fenced block, the fence lines included."""
    return {line_no for fence in fences(text) for line_no in range(fence.open, fence.close + 1)}


# ASCII punctuation: the characters a backslash escapes (CommonMark 0.31.2, 2.4).
_ESCAPABLE = frozenset(string.punctuation)


def code_spans(text: str) -> list[tuple[int, str]]:
    """Every inline code span in *text* outside fenced blocks, as (1-based line of
    its opening backticks, content), by CommonMark 0.31.2 section 6.1 "Code spans"
    (https://spec.commonmark.org/0.31.2/#code-spans).

    A backtick string (a maximal run of backticks) of length N opens a span that
    closes at the next backtick string of exactly length N; a longer or shorter
    run in between is part of the content. An opening string with no such closer
    is literal backticks, and the scan resumes right after it. Line endings in
    the content become spaces, and when the result both begins and ends with a
    space and is not all spaces, one space comes off each end. Outside a span a
    backslash escapes the ASCII punctuation after it (section 2.4, "Backslash
    escapes"), so an escaped backtick opens nothing; inside a span a backslash is
    literal.

    Fenced blocks are found by ``fences`` (section 4.5) and hold no spans. A span
    stays inside one paragraph, so a blank line ends any search for a closer;
    each line's leading spaces and tabs are dropped first, as a paragraph's
    continuation lines drop theirs (section 4.8, "Paragraphs"). Other block
    structure (headings, list markers, HTML blocks, indented code) is not
    modeled: the documents read here keep their code spans in prose.
    """
    fenced = _fenced_line_numbers(text)
    paragraphs: list[tuple[int, list[str]]] = []
    for line_no, line in enumerate(_lines(text), start=1):
        if line_no in fenced or not line.strip(" \t"):
            paragraphs.append((line_no + 1, []))
            continue
        if not paragraphs:
            paragraphs.append((line_no, []))
        paragraphs[-1][1].append(line.lstrip(" \t"))

    found: list[tuple[int, str]] = []
    for first_line, para_lines in paragraphs:
        para = "\n".join(para_lines)
        found.extend((first_line + line, content) for line, content in _spans_in(para))
    return found


def _spans_in(para: str) -> list[tuple[int, str]]:
    """The code spans of one paragraph, as (0-based line within it, content)."""
    found: list[tuple[int, str]] = []
    index = 0
    while index < len(para):
        char = para[index]
        if char == "\\" and index + 1 < len(para) and para[index + 1] in _ESCAPABLE:
            index += 2
            continue
        if char != "`":
            index += 1
            continue
        opener_end = _run_end(para, index)
        width = opener_end - index
        cursor = opener_end
        while cursor < len(para):
            if para[cursor] != "`":
                cursor += 1
                continue
            run_end = _run_end(para, cursor)
            if run_end - cursor == width:
                content = para[opener_end:cursor].replace("\n", " ")
                if content.startswith(" ") and content.endswith(" ") and content.strip(" "):
                    content = content[1:-1]
                found.append((para.count("\n", 0, index), content))
                index = run_end
                break
            cursor = run_end
        else:
            index = opener_end
    return found


def _run_end(text: str, start: int) -> int:
    """The index just past the run of backticks starting at *start*."""
    end = start
    while end < len(text) and text[end] == "`":
        end += 1
    return end


def _split(raw: str) -> list[str]:
    """*raw* as shell tokens, or an empty list when it does not lex.

    ``comments=True`` drops a trailing ``# comment`` without truncating a value
    that contains a ``#`` of its own (a CSS selector, a URL fragment).
    """
    try:
        return shlex.split(raw, comments=True)
    except ValueError:
        return []


def gp_invocations(text: str) -> list[tuple[int, str]]:
    """Every ``gp ...`` invocation in *text*, as (1-based line number, invocation).

    Two sources: a line of a fenced ``bash`` block, and an inline code span
    outside any fence. The guide names several options only in prose, so the
    spans matter as much as the blocks.
    """
    found: list[tuple[int, str]] = []
    for start, body in blocks(text, "bash"):
        for offset, raw in enumerate(_lines(body), start=1):
            tokens = _split(raw)
            if tokens and tokens[0] == "gp":
                found.append((start + offset, shlex.join(tokens)))

    for line_no, span in code_spans(text):
        if not span.startswith("gp "):
            continue
        # A prose span often trails an ellipsis standing in for the rest of the
        # command line; it is not a token the CLI would ever see.
        tokens = [word for word in _split(span) if word != "..."]
        if tokens and tokens[0] == "gp":
            found.append((line_no, shlex.join(tokens)))
    return found


# typer>=0.26 builds its commands on a vendored Click (typer._click) whose
# classes are not the external click's: a TyperGroup there is not a
# click.Group. So a group is told apart by behavior, and every context comes
# from the command's own context_class, so the two Clicks never mix.
Command: TypeAlias = Any


def is_group(command: Command) -> bool:
    """Whether *command* is a group, on either Click typer builds with.

    Checked by the group protocol rather than the ``commands`` dict, so a group
    that loads its subcommands lazily still counts as one."""
    return callable(getattr(command, "list_commands", None))


def subcommand(group: Command, name: str) -> Command | None:
    """The subcommand of *group* called *name*, or None when it has none."""
    return group.get_command(group.context_class(group), name)


# Built once: typer.main.get_command(app) returns a fresh Click object on every
# call, so an identity check against a second call can never match.
ROOT_COMMAND: Command = typer.main.get_command(app)


def option_names(command: Command) -> set[str]:
    """Every option spelling (``--limit``, ``-s``, ``--help``) the command accepts.

    The help option is not in ``command.params``: Click builds it from the
    context's ``help_option_names``, so a check reading ``params`` alone reports
    ``gp --help`` as an unknown option.
    """
    names: set[str] = set()
    for param in command.params:
        names.update(param.opts)
        names.update(param.secondary_opts)
    help_option = command.get_help_option(command.context_class(command))
    if help_option is not None:
        names.update(help_option.opts)
    return names


def _takes_a_value(command: Command, spelling: str) -> bool:
    """Whether *spelling* on *command* consumes the token after it."""
    for param in command.params:
        if spelling in param.opts or spelling in param.secondary_opts:
            return not getattr(param, "is_flag", False) and param.nargs != 0
    return False


def _walk_to_command(tokens: list[str]) -> tuple[Command, list[tuple[Command, int]]]:
    """The command *tokens* addresses, and each group it descended out of.

    Walks the Typer app's Click tree by name. An option token is stepped over,
    along with its value when it takes one, so a group-level flag written before
    the subcommand (``gp observe --no-session interactive URL``) does not end the
    walk early. The walk stops at any leaf command, so an option's value can
    never be mistaken for a subcommand name.

    Each group is returned with the token index at which the walk left it,
    because a group-level flag is legal only *before* its subcommand: the real
    CLI answers ``gp observe interactive --no-session URL`` with "No such
    option". No ``gp`` group takes a positional argument, so a non-option token
    that is not a subcommand of the current group is a typo, and raises.

    Raises:
        AssertionError: A token under a group names no subcommand of it.
    """
    current: Command = ROOT_COMMAND
    groups: list[tuple[Command, int]] = [(ROOT_COMMAND, len(tokens))]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not is_group(current):
            break
        if token.startswith("-"):
            known = [group for group, _exit in groups]
            consumes = any(_takes_a_value(command, token) for command in known)
            index += 2 if consumes else 1
            continue
        child = subcommand(current, token)
        if child is None:
            raise AssertionError(f"`{token}` is not a subcommand of `{current.name}`")
        groups[-1] = (groups[-1][0], index)
        current = child
        index += 1
        if is_group(current):
            groups.append((current, len(tokens)))
    return current, groups


def check_invocation(invocation: str, where: str) -> None:
    """Assert *invocation* names a real gp command and only real options.

    Shared by the guide test's positive and negative invocation cases
    (``test_plugin_development_guide.py``) and by the gate test
    (``test_project_gate.py``), each driving it with its own invocation: the
    guide's own, a synthetic one the guide does not contain, and
    ``PROJECT_GATE``'s.
    """
    tokens = shlex.split(invocation)[1:]
    command, groups = _walk_to_command(tokens)
    # `gp --help` names no subcommand and is still a real invocation, so the
    # check only applies once a non-option token is present.
    if [word for word in tokens if not word.startswith("-")]:
        assert command is not ROOT_COMMAND, f"{where} names no gp command"

    leaf_options = option_names(command)
    for index, word in enumerate(tokens):
        if not word.startswith("-") or set(word) == {"-"}:
            continue
        name = word.split("=", 1)[0]
        if name in leaf_options:
            continue
        allowed_here = any(
            name in option_names(group) and index < exit_index for group, exit_index in groups
        )
        assert allowed_here, (
            f"{where}: {name} is not an option of `{command.name}`, "
            f"nor of a group it sits under at that position"
        )


def slug(title: str) -> str:
    """The GitHub anchor for a heading whose text is *title*."""
    title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", title).replace("`", "")
    return re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", title.strip().lower()))


def outside_fences(text: str) -> str:
    """*text* without its fenced blocks, the fence lines included: its prose."""
    fenced = _fenced_line_numbers(text)
    return "\n".join(
        line for line_no, line in enumerate(_lines(text), start=1) if line_no not in fenced
    )


def section(text: str, heading: str) -> str:
    """*text* from the line *heading* up to the next heading of the same or a higher
    level, or to its end: the one rule for where a markdown section ends. A line
    inside a fenced block is never a heading (the guide quotes a digest whose body
    has ``##`` lines)."""
    level = len(heading) - len(heading.lstrip("#"))
    lines = _lines(text)
    fenced = _fenced_line_numbers(text)
    start = lines.index(heading)
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if i + 1 not in fenced and re.match(rf"#{{1,{level}}}\s", lines[i])
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def slugs_of(path: Path) -> set[str]:
    """GitHub heading anchors for *path*, skipping fenced blocks.

    The guide quotes a digest whose own body carries ``##`` lines; those are
    content, not headings, and GitHub does not make anchors from them.
    """
    slugs: set[str] = set()
    for line in _lines(outside_fences(path.read_text(encoding="utf-8"))):
        heading = _HEADING_RE.match(line)
        if heading is not None:
            slugs.add(slug(heading.group(2)))
    return slugs
