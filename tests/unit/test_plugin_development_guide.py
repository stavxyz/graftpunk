"""The plugin guide's commands, links, and Python blocks stay true to the code.

docs/PLUGIN_DEVELOPMENT.md is the document the plugin-development skill cites
section by section, so a renamed option, a renamed heading, or a snippet that
stops parsing has to fail here rather than in a reader's terminal. Nothing in
this module invokes a command, opens a socket, or starts a browser: the CLI is
inspected through ``typer.main.get_command``, and the Python blocks are
compiled, never executed.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import click
import pytest
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
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
# A plugin's own commands are registered by an installed plugin; no plugin is
# installed in the test environment, so `gp myshop ...` has nothing to walk to.
_PLUGIN_COMMAND_PREFIXES = ("myshop", "my-shop", "mybank")


def _blocks(text: str, language: str) -> list[tuple[int, str]]:
    """Every fenced block of *language* in *text*, as (1-based start line, body)."""
    found: list[tuple[int, str]] = []
    for match in _FENCE_RE.finditer(text):
        if match.group(1) == language:
            found.append((text[: match.start()].count("\n") + 1, match.group(2)))
    return found


def _gp_invocations(text: str) -> list[tuple[int, str]]:
    """Every ``gp ...`` line in a fenced bash block, as (1-based line number, line).

    A trailing ``# comment`` is stripped. A continuation-free single line is all
    the guide ever writes, so no line joining is needed.
    """
    found: list[tuple[int, str]] = []
    for start, body in _blocks(text, "bash"):
        for offset, raw in enumerate(body.splitlines(), start=1):
            line = raw.split("#", 1)[0].strip()
            if line.startswith("gp "):
                found.append((start + offset, line))
    return found


# Built once: typer.main.get_command(app) returns a fresh Click object on every
# call, so an identity check against a second call can never match.
ROOT_COMMAND: click.Command = typer.main.get_command(app)


def _walk_to_command(tokens: list[str]) -> tuple[click.Command, list[click.Command]]:
    """The command *tokens* addresses, and every group passed through on the way.

    Walks the Typer app's Click tree by name. Option tokens are stepped over, so
    a group-level flag written before the subcommand (``gp observe
    --no-session interactive URL``) does not end the walk early. The walk stops
    at the first non-option token that is not a subcommand of the current group
    (an argument such as a URL or a session name), and at any leaf command, so
    an option's value can never be mistaken for a subcommand name.

    The groups are returned too, because a group-level flag is legal on the
    invocation of its subcommand.
    """
    current: click.Command = ROOT_COMMAND
    groups: list[click.Command] = [current]
    for token in tokens:
        if not isinstance(current, click.Group):
            break
        if token.startswith("-"):
            continue
        child = current.commands.get(token)
        if child is None:
            break
        current = child
        if isinstance(current, click.Group):
            groups.append(current)
    return current, groups


def _option_names(command: click.Command) -> set[str]:
    """Every option spelling (``--limit``, ``-s``) the command declares."""
    names: set[str] = set()
    for param in command.params:
        names.update(param.opts)
        names.update(param.secondary_opts)
    return names


GP_INVOCATIONS = _gp_invocations(GUIDE_TEXT)
PYTHON_BLOCKS = _blocks(GUIDE_TEXT, "python")


def test_the_guide_has_commands_links_and_python_to_check() -> None:
    """A guide that stopped parsing would make the three tests below vacuous."""
    assert GP_INVOCATIONS, "no gp invocations found in docs/PLUGIN_DEVELOPMENT.md"
    assert PYTHON_BLOCKS, "no python blocks found in docs/PLUGIN_DEVELOPMENT.md"


@pytest.mark.parametrize(("line_no", "invocation"), GP_INVOCATIONS, ids=lambda v: str(v)[:60])
def test_every_gp_invocation_names_a_real_command_and_options(
    line_no: int, invocation: str
) -> None:
    tokens = shlex.split(invocation)[1:]
    where = f"{GUIDE.name}:{line_no}: {invocation}"
    if tokens and tokens[0] in _PLUGIN_COMMAND_PREFIXES:
        pytest.skip(f"{where} addresses an installed plugin's own command group")

    command, groups = _walk_to_command(tokens)
    assert command is not ROOT_COMMAND, f"{where} names no gp command"
    named = [word for word in tokens if not word.startswith("-")]
    assert named and named[0] in ROOT_COMMAND.commands, (
        f"{where}: `gp {named[0] if named else ''}` is not a gp command"
    )

    allowed = _option_names(command)
    for group in groups:
        allowed |= _option_names(group)
    for word in tokens:
        if word.startswith("-") and set(word) != {"-"}:
            name = word.split("=", 1)[0]
            assert name in allowed, (
                f"{where}: {name} is not an option of `{command.name}` or of a group it sits under"
            )


def _slugs_of(path: Path) -> set[str]:
    """GitHub heading anchors for *path*, skipping fenced blocks.

    The guide quotes a digest whose own body carries ``##`` lines; those are
    content, not headings, and GitHub does not make anchors from them.
    """
    slugs: set[str] = set()
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        heading = _HEADING_RE.match(line)
        if heading is None:
            continue
        title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading.group(2)).replace("`", "")
        slug = re.sub(r"[^\w\s-]", "", title.strip().lower())
        slugs.add(re.sub(r"\s+", "-", slug))
    return slugs


GUIDE_LINKS = [
    (GUIDE_TEXT[: match.start()].count("\n") + 1, match.group(1))
    for match in _LINK_RE.finditer(GUIDE_TEXT)
    if not match.group(1).startswith(("http://", "https://", "mailto:"))
]


@pytest.mark.parametrize(("line_no", "target"), GUIDE_LINKS, ids=lambda v: str(v)[:60])
def test_every_relative_link_and_anchor_resolves(line_no: int, target: str) -> None:
    where = f"{GUIDE.name}:{line_no}: {target}"
    file_part, _, anchor = target.partition("#")
    resolved = GUIDE if not file_part else (GUIDE.parent / file_part).resolve()
    assert resolved.exists(), f"{where} points at a file that does not exist"
    if anchor:
        assert resolved.suffix == ".md", f"{where} anchors into a non-markdown file"
        assert anchor in _slugs_of(resolved), (
            f"{where}: no heading in {resolved.name} slugifies to {anchor!r}"
        )


@pytest.mark.parametrize(("line_no", "source"), PYTHON_BLOCKS, ids=lambda v: str(v)[:40])
def test_every_python_block_compiles(line_no: int, source: str) -> None:
    try:
        compile(source, f"{GUIDE.name}:{line_no}", "exec")
    except SyntaxError as exc:
        pytest.fail(f"{GUIDE.name}:{line_no}: python block does not compile: {exc}")
