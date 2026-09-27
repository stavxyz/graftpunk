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

from graftpunk.testing.sidecar import Sidecar, sidecar_text
from tests.unit.guide_harness import (
    GUIDE,
    GUIDE_TEXT,
    REPO_ROOT,
    blocks,
    check_invocation,
    gp_invocations,
    option_names,
    slugs_of,
)

# The documents that link into the guide. A heading rename there is the likeliest
# way an inbound anchor goes stale.
INBOUND_SOURCES = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "HOW_IT_WORKS.md",
    REPO_ROOT / "examples" / "README.md",
)

_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
# A plugin's own commands are registered by an installed plugin; no plugin is
# installed in the test environment, so `gp myshop ...` has nothing to walk to.
# `my-shop` is the guide's hyphenated-name example and appears only in prose,
# which is why it has to be here even though no fenced block names it.
_PLUGIN_COMMAND_PREFIXES = ("myshop", "my-shop")


GP_INVOCATIONS = gp_invocations(GUIDE_TEXT)
PYTHON_BLOCKS = blocks(GUIDE_TEXT, "python")


def test_the_sidecar_json_example_matches_sidecar_text_byte_for_byte() -> None:
    """The guide's sidecar example is hand-written prose, and ``sidecar_text``'s
    exact layout (``indent=2`` puts every list item on its own line) is not: a
    doc edit that reflows the example without running it would drift from what
    `gp observe fixtures` actually writes. Round-trips the example's own values
    through ``Sidecar``/``sidecar_text`` rather than comparing against a second
    hand-written copy, so there is exactly one place the layout is spelled."""
    (_line_no, block) = next(
        pair for pair in blocks(GUIDE_TEXT, "json") if "capture_sha256" in pair[1]
    )
    doc_json = json.loads(block)
    sidecar = Sidecar(
        status=doc_json["status"],
        content_type=doc_json["content_type"],
        body_params=tuple(doc_json["body_params"]),
        capture_sha256=doc_json["capture_sha256"],
        flagged_names=tuple(doc_json["flagged_names"]),
        redacted_names=doc_json["redacted_names"],
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
        pair for pair in blocks(GUIDE_TEXT, "python") if "GP-FILL: describe api-orders" in pair[1]
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
    check_invocation(invocation, f"{GUIDE.name}:{line_no}")


def test_a_misspelled_subcommand_is_caught() -> None:
    """The walk must not fall back to the group and skip the option check."""
    with pytest.raises(AssertionError, match="digestt"):
        check_invocation("gp observe digestt myshop", "<synthetic>")


def test_a_group_flag_after_its_subcommand_is_caught() -> None:
    """`gp observe interactive --no-session URL` is "No such option" on the real CLI."""
    with pytest.raises(AssertionError, match="--no-session"):
        check_invocation(
            "gp observe interactive --no-session https://myshop.example/", "<synthetic>"
        )


def test_a_group_flag_before_its_subcommand_is_accepted() -> None:
    """The guide's own capture command, which the real CLI accepts."""
    check_invocation("gp observe --no-session interactive https://myshop.example/", "<synthetic>")


def test_an_unknown_option_is_caught() -> None:
    with pytest.raises(AssertionError, match="--nope"):
        check_invocation("gp observe digest myshop --nope", "<synthetic>")


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
    spellings = option_names(login_command)
    for named_in_the_guide in ("--as", "--headless", "--headful"):
        assert named_in_the_guide in spellings, (
            f"the guide names {named_in_the_guide} on a login command, "
            f"but create_login_fn builds {sorted(spellings)}"
        )


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
        assert anchor in slugs_of(resolved), (
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
    assert anchor in slugs_of(GUIDE), (
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
    ``__file__`` is one of ``_free_names``' own free names (a block reads it
    without binding it) but is already seeded below with a real path, which a
    snippet building ``Path(__file__)`` needs; ``setdefault`` keeps that seed
    rather than overwriting it with a stand-in.
    """
    tree = ast.parse(source)
    namespace: dict[str, Any] = {
        "__name__": f"guide_block_{line_no}",
        "__file__": str(GUIDE),
        "__builtins__": builtins,
    }
    for name in _free_names(tree):
        namespace.setdefault(name, _Inert())
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
