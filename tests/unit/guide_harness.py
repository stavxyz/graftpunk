"""The plugin guide, read, and gp invocations checked against the real CLI.

Shared by the guide's own test (tests/unit/test_plugin_development_guide.py),
the project gate test (tests/unit/test_project_gate.py), and the graft skill's
tests, so no test module imports another. Nothing here is a test and nothing
here runs a gp command: the CLI is inspected through typer.main.get_command.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import click
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


_FENCE_RE = re.compile(r"^```(\w*)[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


# A code span may wrap across a line break, and one of the guide's own does. A
# per-line scanner silently loses the tail (and any option in it), so the span
# regex has to tolerate a newline; a blank line ends a paragraph, so a match
# containing one is a pair of unbalanced backticks rather than a span.
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")


def blocks(text: str, language: str) -> list[tuple[int, str]]:
    """Every fenced block of *language* in *text*, as (1-based start line, body)."""
    found: list[tuple[int, str]] = []
    for match in _FENCE_RE.finditer(text):
        if match.group(1) == language:
            found.append((text[: match.start()].count("\n") + 1, match.group(2)))
    return found


def _fenced_line_numbers(text: str) -> set[int]:
    """Every 1-based line number inside a fenced block, the fence lines included."""
    inside: set[int] = set()
    in_fence = False
    for line_no, line in enumerate(text.splitlines(), start=1):
        if line.startswith("```"):
            in_fence = not in_fence
            inside.add(line_no)
            continue
        if in_fence:
            inside.add(line_no)
    return inside


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
        for offset, raw in enumerate(body.splitlines(), start=1):
            tokens = _split(raw)
            if tokens and tokens[0] == "gp":
                found.append((start + offset, shlex.join(tokens)))

    # Fenced lines are blanked rather than dropped, so an offset in the masked
    # text still maps to the real line number.
    fenced = _fenced_line_numbers(text)
    prose = "\n".join(
        "" if line_no in fenced else line for line_no, line in enumerate(text.splitlines(), start=1)
    )
    for match in _CODE_SPAN_RE.finditer(prose):
        span = match.group(1)
        if "\n\n" in span or not span.startswith("gp "):
            continue
        line_no = prose[: match.start()].count("\n") + 1
        # A prose span often trails an ellipsis standing in for the rest of the
        # command line; it is not a token the CLI would ever see.
        tokens = [word for word in _split(" ".join(span.split())) if word != "..."]
        if tokens and tokens[0] == "gp":
            found.append((line_no, shlex.join(tokens)))
    return found


# Built once: typer.main.get_command(app) returns a fresh Click object on every
# call, so an identity check against a second call can never match.
ROOT_COMMAND: click.Command = typer.main.get_command(app)


def option_names(command: click.Command) -> set[str]:
    """Every option spelling (``--limit``, ``-s``, ``--help``) the command accepts.

    The help option is not in ``command.params``: Click builds it from the
    context's ``help_option_names``, so a check reading ``params`` alone reports
    ``gp --help`` as an unknown option.
    """
    names: set[str] = set()
    for param in command.params:
        names.update(param.opts)
        names.update(param.secondary_opts)
    help_option = command.get_help_option(click.Context(command))
    if help_option is not None:
        names.update(help_option.opts)
    return names


def _takes_a_value(command: click.Command, spelling: str) -> bool:
    """Whether *spelling* on *command* consumes the token after it."""
    for param in command.params:
        if spelling in param.opts or spelling in param.secondary_opts:
            return not getattr(param, "is_flag", False) and param.nargs != 0
    return False


def _walk_to_command(tokens: list[str]) -> tuple[click.Command, list[tuple[click.Command, int]]]:
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
    current: click.Command = ROOT_COMMAND
    groups: list[tuple[click.Command, int]] = [(ROOT_COMMAND, len(tokens))]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not isinstance(current, click.Group):
            break
        if token.startswith("-"):
            known = [group for group, _exit in groups]
            consumes = any(_takes_a_value(command, token) for command in known)
            index += 2 if consumes else 1
            continue
        child = current.commands.get(token)
        if child is None:
            raise AssertionError(f"`{token}` is not a subcommand of `{current.name}`")
        groups[-1] = (groups[-1][0], index)
        current = child
        index += 1
        if isinstance(current, click.Group):
            groups.append((current, len(tokens)))
    return current, groups


def check_invocation(invocation: str, where: str) -> None:
    """Assert *invocation* names a real gp command and only real options.

    The body of :func:`test_every_gp_invocation_names_a_real_command_and_options`,
    lifted out so the negative tests below can drive the same code with a
    synthetic invocation the guide does not contain.
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
        line for line_no, line in enumerate(text.splitlines(), start=1) if line_no not in fenced
    )


def section(text: str, heading: str) -> str:
    """*text* from the line *heading* up to the next heading of the same or a higher
    level, or to its end: the one rule for where a markdown section ends. A line
    inside a fenced block is never a heading (the guide quotes a digest whose body
    has ``##`` lines)."""
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
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
    for line in outside_fences(path.read_text(encoding="utf-8")).splitlines():
        heading = _HEADING_RE.match(line)
        if heading is not None:
            slugs.add(slug(heading.group(2)))
    return slugs
