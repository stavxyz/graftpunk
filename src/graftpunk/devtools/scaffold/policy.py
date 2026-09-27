"""What a generated plugin project must look like, as data.

Knows nothing about the filesystem and imports nothing that touches it, so a
reader, a lint, and a writer can all import it without importing each other
(graft skill spec, 2026-09-21, "Project policy is declarative and lives apart
from the writer").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from graftpunk.plugins.cli_plugin import SitePlugin
from graftpunk.testing.sidecar import FIXTURES_PLACEHOLDER

__all__ = [
    "CONFTEST_PATH",
    "FIXTURES_PLACEHOLDER",
    "FIXTURES_TREE",
    "GENERATED_MODULE_NAMES",
    "GP_FILL_MARKER",
    "PROJECT_GATE",
    "PROJECT_REQUIREMENTS",
    "RESERVED_COMMAND_NAMES",
    "TESTS_DIR",
    "ProjectRequirement",
    "fixtures_root",
    "module_name_for",
]

TESTS_DIR: Final = "tests/"
"""The directory a generated project's tests live in, project-relative: the one
spelling every other tests-relative path here is built from."""

FIXTURES_TREE: Final = f"{TESTS_DIR}fixtures/"
"""The one directory every plugin's fixtures live under, project-relative.

The same in a standalone project and in a suite, and true for every member a
suite ever gains, which is why a generated conftest can carry it although it
is written once."""


def fixtures_root(*, suite_member: bool, module_name: str) -> str:
    """Where one plugin's fixtures live, project-relative, with a trailing slash.

    A standalone project uses the tree itself. A suite member owns a directory
    under it named for its module, because fixture names come from endpoint paths
    and two plugins in one suite can share a path.
    The generator supplies the two facts from its spec; a reader supplies them
    from the project on disk.
    """
    if suite_member:
        return f"{FIXTURES_TREE}{module_name}/"
    return FIXTURES_TREE


CONFTEST_PATH: Final = f"{TESTS_DIR}conftest.py"
"""The generated project's shared conftest, project-relative. Written once, for the
first plugin of a project; a suite member added later shares it."""


@dataclass(frozen=True)
class ProjectRequirement:
    """A module-level name a project file must bind, and the statement that binds it.

    Presence is decided structurally by the project reader, with
    ``pysrc.binds_name``: the file binds the name at module level. The renderer
    emits every requirement into the conftest it generates, ``gp plugin upgrade``
    applies the ones a project lacks, and ``gp plugin check`` reports them; the
    renderer and the migrator both add the statement through
    ``pysrc.with_bindings``. Every requirement's ``path`` is ``CONFTEST_PATH``
    today (a test in ``test_scaffold_policy.py`` holds that), so a requirement
    for another file would need the renderer's conftest-only filter extended
    first.

    Only a Python file and a module-level binding. A ``pyproject.toml`` key is not
    this format's business (it needs a TOML edit through ``pyproject_edit.py``),
    and neither is a gate entry (that is the README regenerated from the gate).
    """

    path: str
    name: str
    statement: str
    # (module, name) pairs the statement reads, merged into the file's imports.
    imports: tuple[tuple[str, str], ...] = ()

    @property
    def key(self) -> str:
        """``"<path>:<name>"``: how the project view reports this requirement."""
        return f"{self.path}:{self.name}"


# The fixtures tree as the conftest sees it: the conftest lives in TESTS_DIR.
_TREE_FROM_CONFTEST = FIXTURES_TREE.removeprefix(TESTS_DIR).strip("/")

PROJECT_REQUIREMENTS: Final[tuple[ProjectRequirement, ...]] = (
    ProjectRequirement(
        path=CONFTEST_PATH,
        name="FIXTURES_TREE",
        statement=f'FIXTURES_TREE = Path(__file__).parent / "{_TREE_FROM_CONFTEST}"',
        imports=(("pathlib", "Path"),),
    ),
    ProjectRequirement(
        path=CONFTEST_PATH,
        name="sanitised_fixtures",
        statement="sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)",
        imports=(("graftpunk.testing.plugin", "fixtures_are_sanitised"),),
    ),
)
"""What every generated project's files must bind, in the order they are applied."""


def module_name_for(name: str) -> str:
    """*name*, lowercased with every run of non-alphanumeric characters collapsed to one
    underscore: the Python module fragment (``graftpunk_{module_name_for(name)}``).

    Total: never raises. A name reaching here through ``ScaffoldSpec`` is
    already validated by ``validate_plugin_name``, but the function makes no
    assumption of that on its own.
    """
    return re.sub(r"[^a-z0-9]+", "_", name.lower())


GP_FILL_MARKER: Final = "GP-FILL"
"""The marker the generator writes wherever the digest could not decide a value.
The renderer writes it, the project reader finds it, and ``gp plugin check``
reports every one left; all three take it from here."""

_AUTO_ROOT_COMMAND_NAMES = ("login",)
"""The root commands graftpunk.cli.plugin_commands adds to a plugin itself (login,
for a plugin with login_config). A copy, since devtools does not import
graftpunk.cli; a test holds it equal to AUTO_ROOT_COMMAND_NAMES."""

RESERVED_COMMAND_NAMES: Final[frozenset[str]] = frozenset(
    {name for name in dir(SitePlugin) if not name.startswith("_")} | set(_AUTO_ROOT_COMMAND_NAMES)
)
"""The names a generated or inserted command may not take: every public attribute of
SitePlugin, the class every plugin subclasses, read from the class itself so a new
framework attribute is covered without an edit here, and the root commands
registration adds. render.py, selection.py, and insert.py all read it."""

GENERATED_MODULE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "command",
        "CommandContext",
        "PluginParamSpec",
        "SitePlugin",
        "LoginConfig",
        "LoginStep",
        "Token",
        "TokenConfig",
        "_quote_path",
        "int",
        "float",
        "bool",
        "str",
        "list",
        "dict",
    }
)
"""The names a generated plugin module binds or reads at its own module level: every
name ``render.py`` imports into it (``command``, ``CommandContext``,
``PluginParamSpec``, ``SitePlugin``, ``LoginConfig``, ``LoginStep``, ``Token``,
``TokenConfig``, and ``_quote_path``, the private alias it gives ``urllib.parse.quote``),
plus the builtins a stub's ``@command(..., type=int)`` keyword evaluates in the class
body. A command stub named one of these binds the name inside the class namespace, so
a later ``@command(...)`` in the same class calls the stub instead of the real
decorator or type (issue graft-package-project-tools polish-r1 P1).
``selection.plan_command`` refuses a selection naming one, ``render._default_commands``
seeds its dedupe with it, and ``insert.add_command`` separately refuses a name the
target module already binds at top level, for a name outside this set. A test in
``test_scaffold_policy.py`` pins this set against a rendered module's top-level
bindings, so a new renderer import cannot silently fall out of step."""


PROJECT_GATE: Final[tuple[str, ...]] = (
    "pytest",
    "ruff check .",
    "ruff format --check .",
    "gp plugin check",
)
"""The commands a generated project's gate runs, in order.

Reproduced in exactly one rendered form, the checks block of the generated
README; the guide's "The gate" section and its CI example quote the same
lines, and a test pins all three to this constant."""
