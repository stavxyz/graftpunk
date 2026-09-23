"""The plugin guide's commands, links, and Python blocks stay true to the code.

docs/PLUGIN_DEVELOPMENT.md is the document the plugin-development skill cites
section by section, so a renamed option, a renamed heading, or a snippet that
stops parsing has to fail here rather than in a reader's terminal. Nothing in
this module invokes a gp command or opens a socket: the CLI is inspected through
``typer.main.get_command``, and the guide's Python blocks are executed in an
isolated namespace that reaches nothing but graftpunk and the standard library.
"""

from __future__ import annotations

import ast
import builtins
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import click
import pytest
import typer.main

from graftpunk.cli.main import app
from graftpunk.testing.sidecar import Sidecar, sidecar_text


def _repo_root() -> Path:
    """The directory holding pyproject.toml, found by walking up from this file."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError("pyproject.toml not found above this test file")


REPO_ROOT = _repo_root()
GUIDE = REPO_ROOT / "docs" / "PLUGIN_DEVELOPMENT.md"
GUIDE_TEXT = GUIDE.read_text(encoding="utf-8")
# The documents that link into the guide. A heading rename there is the likeliest
# way an inbound anchor goes stale.
INBOUND_SOURCES = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "HOW_IT_WORKS.md",
    REPO_ROOT / "examples" / "README.md",
)

_FENCE_RE = re.compile(r"^```(\w*)[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
# A code span may wrap across a line break, and one of the guide's own does. A
# per-line scanner silently loses the tail (and any option in it), so the span
# regex has to tolerate a newline; a blank line ends a paragraph, so a match
# containing one is a pair of unbalanced backticks rather than a span.
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
# A plugin's own commands are registered by an installed plugin; no plugin is
# installed in the test environment, so `gp myshop ...` has nothing to walk to.
# `my-shop` is the guide's hyphenated-name example and appears only in prose,
# which is why it has to be here even though no fenced block names it.
_PLUGIN_COMMAND_PREFIXES = ("myshop", "my-shop")


def _blocks(text: str, language: str) -> list[tuple[int, str]]:
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


def _gp_invocations(text: str) -> list[tuple[int, str]]:
    """Every ``gp ...`` invocation in *text*, as (1-based line number, invocation).

    Two sources: a line of a fenced ``bash`` block, and an inline code span
    outside any fence. The guide names several options only in prose, so the
    spans matter as much as the blocks.
    """
    found: list[tuple[int, str]] = []
    for start, body in _blocks(text, "bash"):
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


def _option_names(command: click.Command) -> set[str]:
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


GP_INVOCATIONS = _gp_invocations(GUIDE_TEXT)
PYTHON_BLOCKS = _blocks(GUIDE_TEXT, "python")


def _check_invocation(invocation: str, where: str) -> None:
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

    leaf_options = _option_names(command)
    for index, word in enumerate(tokens):
        if not word.startswith("-") or set(word) == {"-"}:
            continue
        name = word.split("=", 1)[0]
        if name in leaf_options:
            continue
        allowed_here = any(
            name in _option_names(group) and index < exit_index for group, exit_index in groups
        )
        assert allowed_here, (
            f"{where}: {name} is not an option of `{command.name}`, "
            f"nor of a group it sits under at that position"
        )


def test_the_sidecar_json_example_matches_sidecar_text_byte_for_byte() -> None:
    """The guide's sidecar example is hand-written prose, and ``sidecar_text``'s
    exact layout (``indent=2`` puts every list item on its own line) is not: a
    doc edit that reflows the example without running it would drift from what
    `gp observe fixtures` actually writes. Round-trips the example's own values
    through ``Sidecar``/``sidecar_text`` rather than comparing against a second
    hand-written copy, so there is exactly one place the layout is spelled."""
    (_line_no, block) = next(
        pair for pair in _blocks(GUIDE_TEXT, "json") if "capture_sha256" in pair[1]
    )
    doc_json = json.loads(block)
    sidecar = Sidecar(
        status=doc_json["status"],
        content_type=doc_json["content_type"],
        body_params=tuple(doc_json["body_params"]),
        capture_sha256=doc_json["capture_sha256"],
        flagged_names=tuple(doc_json["flagged_names"]),
    )
    assert block.strip() == sidecar_text(sidecar).strip()


def test_the_generated_plugin_example_matches_the_generator_output() -> None:
    """The guide's "What gets filled in" block is what ``gp plugin new`` writes for
    the digest shown above it, minus the elided stubs. Rendering that digest's
    facts here (the one ``/api/orders`` endpoint the example keeps) and comparing
    whole text means a generator change that the guide does not follow fails
    here, rather than showing a reader a stub the tool no longer writes."""
    from graftpunk.devtools.scaffold.render import ScaffoldSpec, render
    from graftpunk.har.digest import (
        DigestSource,
        Endpoint,
        LoginObservation,
        RunDigest,
        ShapeNode,
    )
    from graftpunk.har.documents import LoginForm, TokenCandidate

    (_line_no, block) = next(
        pair for pair in _blocks(GUIDE_TEXT, "python") if "GP-FILL: describe api_orders" in pair[1]
    )
    orders = Endpoint(
        host="myshop.example",
        template="/api/orders",
        methods=("GET",),
        count=2,
        statuses=(200,),
        content_type="application/json",
        query_params={"archived": "bool", "page": "int", "per_page": "int"},
        body_params={},
        body_kind="none",
        shape=ShapeNode(
            kind="object",
            children={
                "orders": ShapeNode(kind="array"),
                "page": ShapeNode(kind="number"),
                "total": ShapeNode(kind="number"),
            },
        ),
        custom_headers=("X-Csrf-Token",),
        examples=("/api/orders",),
    )
    digest = RunDigest(
        source=DigestSource(
            har_path=Path("network.har"), session="myshop", run_id="20260915-100000-1"
        ),
        primary_host="myshop.example",
        hosts={"myshop.example": 5},
        endpoints=(orders,),
        login=(
            LoginObservation(
                order=2,
                method="POST",
                url="https://myshop.example/session",
                status=302,
                kind="credential_post",
                fields=("email", "password"),
                redirect_to="/dashboard",
            ),
        ),
        login_forms=(
            LoginForm(
                action="/session",
                method="POST",
                fields={"password": "#password", "username": "#email"},
                submit="#sign-in",
                hidden=(),
                source="https://myshop.example/login",
            ),
        ),
        tokens=(
            TokenCandidate(
                kind="header",
                name="X-Csrf-Token",
                seen_on=("GET /api/orders", "GET /api/orders/{order_id}"),
            ),
        ),
        cookies=("myshop_session",),
        dropped={"static": 2, "third_party": 0, "error": 0},
    )
    spec = ScaffoldSpec(
        name="myshop",
        mode="new_project",
        backend="nodriver",
        base_url="https://myshop.example",
        digest=digest,
    )
    assert block == render(spec)["src/graftpunk_myshop/plugin.py"]


def test_the_guide_has_commands_links_and_python_to_check() -> None:
    """A guide that stopped parsing would make the tests below vacuous."""
    assert GP_INVOCATIONS, "no gp invocations found in docs/PLUGIN_DEVELOPMENT.md"
    assert PYTHON_BLOCKS, "no python blocks found in docs/PLUGIN_DEVELOPMENT.md"


@pytest.mark.parametrize(("line_no", "invocation"), GP_INVOCATIONS, ids=lambda v: str(v)[:60])
def test_every_gp_invocation_names_a_real_command_and_options(
    line_no: int, invocation: str
) -> None:
    tokens = shlex.split(invocation)[1:]
    if tokens and tokens[0] in _PLUGIN_COMMAND_PREFIXES:
        pytest.skip(f"{invocation} addresses an installed plugin's own command group")
    _check_invocation(invocation, f"{GUIDE.name}:{line_no}")


def test_a_misspelled_subcommand_is_caught() -> None:
    """The walk must not fall back to the group and skip the option check."""
    with pytest.raises(AssertionError, match="digestt"):
        _check_invocation("gp observe digestt myshop", "<synthetic>")


def test_a_group_flag_after_its_subcommand_is_caught() -> None:
    """`gp observe interactive --no-session URL` is "No such option" on the real CLI."""
    with pytest.raises(AssertionError, match="--no-session"):
        _check_invocation(
            "gp observe interactive --no-session https://myshop.example/", "<synthetic>"
        )


def test_a_group_flag_before_its_subcommand_is_accepted() -> None:
    """The guide's own capture command, which the real CLI accepts."""
    _check_invocation("gp observe --no-session interactive https://myshop.example/", "<synthetic>")


def test_an_unknown_option_is_caught() -> None:
    with pytest.raises(AssertionError, match="--nope"):
        _check_invocation("gp observe digest myshop --nope", "<synthetic>")


def test_the_login_options_the_guide_names_exist() -> None:
    """`--as`, `--headless` and `--headful` are named in prose on a plugin command.

    Every `gp <plugin> ...` invocation is skipped above, because no plugin is
    installed here, so these three would otherwise go unchecked. They are built
    by ``create_login_fn`` for every generated login command, so the command it
    synthesizes for a throwaway plugin is where they can be pinned.
    """
    from graftpunk.cli.login_commands import (
        create_login_fn,
        resolve_login_callable,
        resolve_login_fields,
    )
    from graftpunk.plugins import LoginConfig, LoginStep, SitePlugin

    class _GuidePlugin(SitePlugin):
        site_name = "guideprobe"
        base_url = "https://myshop.example"
        login_config = LoginConfig(
            steps=[LoginStep(fields={"username": "#email", "password": "#password"})],
        )

    plugin = _GuidePlugin()
    login_callable = resolve_login_callable(plugin)
    assert login_callable is not None, "a declarative login_config should generate a login"
    fn = create_login_fn(plugin, login_callable, resolve_login_fields(plugin))

    # Registered on a throwaway Typer app and converted the way the real CLI
    # converts it, so the spellings come from Typer's own parameter building.
    probe = typer.Typer()
    probe.command("login")(fn)
    login_command = typer.main.get_command(probe)
    if isinstance(login_command, click.Group):
        login_command = login_command.commands["login"]
    spellings = _option_names(login_command)
    for named_in_the_guide in ("--as", "--headless", "--headful"):
        assert named_in_the_guide in spellings, (
            f"the guide names {named_in_the_guide} on a login command, "
            f"but create_login_fn builds {sorted(spellings)}"
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


def _relative_links(path: Path) -> list[tuple[int, str]]:
    text = path.read_text(encoding="utf-8")
    return [
        (text[: match.start()].count("\n") + 1, match.group(1))
        for match in _LINK_RE.finditer(text)
        if not match.group(1).startswith(("http://", "https://", "mailto:"))
    ]


GUIDE_LINKS = _relative_links(GUIDE)
INBOUND_LINKS = [
    (source, line_no, target)
    for source in INBOUND_SOURCES
    for line_no, target in _relative_links(source)
    if target.partition("#")[0].endswith("PLUGIN_DEVELOPMENT.md") and "#" in target
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


def test_the_guide_is_linked_to_by_anchor_from_elsewhere() -> None:
    """Without inbound anchors the test below would be vacuous."""
    assert INBOUND_LINKS, "no anchored inbound links into the guide were found"


@pytest.mark.parametrize(("source", "line_no", "target"), INBOUND_LINKS, ids=lambda v: str(v)[:60])
def test_every_inbound_anchor_into_the_guide_resolves(
    source: Path, line_no: int, target: str
) -> None:
    anchor = target.partition("#")[2]
    assert anchor in _slugs_of(GUIDE), (
        f"{source.relative_to(REPO_ROOT)}:{line_no} links to {target}, "
        f"but no heading in {GUIDE.name} slugifies to {anchor!r}"
    )


class _Inert:
    """Stands in for a name a guide snippet expects its reader to supply."""

    def __call__(self, *args: Any, **kwargs: Any) -> _Inert:
        return self

    def __iter__(self) -> Any:
        return iter(())


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def _bound_names(tree: ast.AST) -> set[str]:
    """Every name the block binds itself: imports, assignments, defs, arguments."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
    return bound


def _free_names(tree: ast.AST) -> set[str]:
    """Names the block reads without binding them and without a builtin behind them."""
    loaded = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    return loaded - _bound_names(tree) - set(dir(builtins))


EXECUTABLE_BLOCKS: list[tuple[int, str]] = []
UNRUNNABLE_BLOCKS: list[tuple[int, str, str]] = []
for _line_no, _source in PYTHON_BLOCKS:
    try:
        _tree = ast.parse(_source)
    except SyntaxError:
        EXECUTABLE_BLOCKS.append((_line_no, _source))  # the compile test reports it
        continue
    _outside = {
        root
        for root in _imported_roots(_tree)
        if root != "graftpunk" and root not in sys.stdlib_module_names
    }
    if _outside:
        UNRUNNABLE_BLOCKS.append((_line_no, _source, ", ".join(sorted(_outside))))
    else:
        EXECUTABLE_BLOCKS.append((_line_no, _source))


@pytest.mark.parametrize(("line_no", "source"), PYTHON_BLOCKS, ids=lambda v: str(v)[:40])
def test_every_python_block_compiles(line_no: int, source: str) -> None:
    try:
        compile(source, f"{GUIDE.name}:{line_no}", "exec")
    except SyntaxError as exc:
        pytest.fail(f"{GUIDE.name}:{line_no}: python block does not compile: {exc}")


@pytest.mark.parametrize(("line_no", "source"), EXECUTABLE_BLOCKS, ids=lambda v: str(v)[:40])
def test_every_self_contained_python_block_executes(line_no: int, source: str) -> None:
    """Run the block, so an import that moved or a signature that changed fails here.

    The namespace is fresh per block and carries no entry in ``sys.modules``, so
    a ``SitePlugin`` subclass defined by a snippet cannot reach plugin discovery.
    Names the snippet expects its reader to supply are bound to inert
    stand-ins, which is what lets a fragment run without inventing a parser.
    """
    tree = ast.parse(source)
    namespace: dict[str, Any] = {
        "__name__": f"guide_block_{line_no}",
        "__file__": str(GUIDE),
        "__builtins__": builtins,
    }
    for name in _free_names(tree):
        namespace[name] = _Inert()
    first_line = source.strip().splitlines()[0]
    try:
        exec(compile(source, f"{GUIDE.name}:{line_no}", "exec"), namespace)  # noqa: S102
    except Exception as exc:  # noqa: BLE001 - any failure is the finding
        pytest.fail(
            f"{GUIDE.name}:{line_no}: python block raised "
            f"{type(exc).__name__}: {exc}\n  first line: {first_line}"
        )


@pytest.mark.parametrize(
    ("line_no", "source", "outside"), UNRUNNABLE_BLOCKS, ids=lambda v: str(v)[:40]
)
def test_blocks_that_are_only_compiled_say_why(line_no: int, source: str, outside: str) -> None:
    """Pin the reason a block is compiled but not executed, so the list cannot grow quietly.

    The one such block is the generated project's own test, which imports
    ``graftpunk_myshop``: a package `gp plugin new` writes into the reader's
    directory, which is not installed here and must not be.
    """
    assert outside == "graftpunk_myshop", (
        f"{GUIDE.name}:{line_no}: block skips execution for an unexpected import "
        f"({outside}); either it is runnable, or this test needs a new reason"
    )
