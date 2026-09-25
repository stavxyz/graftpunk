"""Declarative project policy: nothing here touches the filesystem
(graft skill spec, 2026-09-21, "Project policy is declarative")."""

from __future__ import annotations

import ast
from pathlib import Path

import graftpunk.devtools.scaffold.policy as policy
from graftpunk.devtools.scaffold.policy import (
    CONFTEST_PATH,
    FIXTURES_PLACEHOLDER,
    FIXTURES_TREE,
    GP_FILL_MARKER,
    PROJECT_REQUIREMENTS,
    TESTS_DIR,
    ProjectRequirement,
    fixtures_root,
    module_name_for,
)


def test_the_tree_lies_under_the_tests_directory() -> None:
    assert (TESTS_DIR, FIXTURES_TREE) == ("tests/", "tests/fixtures/")
    assert FIXTURES_TREE.startswith(TESTS_DIR)


def test_a_standalone_project_uses_the_tree_itself() -> None:
    assert fixtures_root(suite_member=False, module_name="myshop") == "tests/fixtures/"


def test_a_suite_member_owns_a_directory_under_the_tree() -> None:
    assert fixtures_root(suite_member=True, module_name="my_shop") == "tests/fixtures/my_shop/"


def test_every_root_lies_under_the_tree() -> None:
    for suite_member in (False, True):
        assert fixtures_root(suite_member=suite_member, module_name="x").startswith(FIXTURES_TREE)


# Modules that can touch the filesystem. Policy is data, so it imports none of
# them. A denylist rather than an allowlist: a later addition to policy (a
# dataclass, a regex, a constant owned elsewhere) needs no edit here, and the
# check does not loosen as policy grows.
_FILESYSTEM_MODULES = (
    "os",
    "shutil",
    "pathlib",
    "io",
    "tempfile",
    "subprocess",
    "graftpunk.devtools.captures",
)


def test_policy_imports_nothing_that_touches_the_filesystem() -> None:
    """Checks policy's own imports. graftpunk.devtools.captures is the writing side
    of the captures rule; its pure side, captures_rule, is not on the list."""
    assert policy.__file__ is not None
    tree = ast.parse(Path(policy.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported |= {f"{node.module}.{alias.name}" for alias in node.names}
    touching = sorted(
        name
        for name in imported
        for module in _FILESYSTEM_MODULES
        if name == module or name.startswith(f"{module}.")
    )
    assert touching == []


class TestProjectRequirements:
    def test_the_conftest_binds_the_tree_and_the_check(self) -> None:
        assert f"{TESTS_DIR}conftest.py" == CONFTEST_PATH
        assert [(r.path, r.name) for r in PROJECT_REQUIREMENTS] == [
            (CONFTEST_PATH, "FIXTURES_TREE"),
            (CONFTEST_PATH, "sanitised_fixtures"),
        ]

    def test_each_statement_binds_its_own_name(self) -> None:
        for requirement in PROJECT_REQUIREMENTS:
            assert requirement.statement.startswith(f"{requirement.name} = ")

    def test_the_tree_statement_names_the_policy_tree(self) -> None:
        tree = next(r for r in PROJECT_REQUIREMENTS if r.name == "FIXTURES_TREE")
        assert tree.statement == 'FIXTURES_TREE = Path(__file__).parent / "fixtures"'
        assert FIXTURES_TREE == "tests/fixtures/"

    def test_the_key(self) -> None:
        assert ProjectRequirement(path="a.py", name="x", statement="x = 1").key == "a.py:x"


def test_the_placeholder_is_the_testing_layers() -> None:
    from graftpunk.testing import sidecar

    assert FIXTURES_PLACEHOLDER is sidecar.FIXTURES_PLACEHOLDER


def _group_literals(tree: ast.Module, group: str) -> list[int]:
    """The lines where *tree* spells *group* in one of the group's two uses, outside
    docstrings: inside a TOML table header (a literal holding the group in double
    quotes), or as the key of an entry-point lookup (``.get(group)`` or
    ``group=group``). The same text as an import path is not counted."""
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
    }
    lines: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and f'"{group}"' in node.value
        ):
            lines.append(node.lineno)
        elif isinstance(node, ast.Call):
            lookup = isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            keys = [*(node.args[:1] if lookup else [])]
            keys += [k.value for k in node.keywords if k.arg == "group"]
            lines += [k.lineno for k in keys if isinstance(k, ast.Constant) and k.value == group]
    return lines


def test_the_marker_and_the_module_name_rule_live_here() -> None:
    """Policy is the rule's one home: the renderer calls it through policy and
    re-exports nothing, so no importer can reach it by a second route."""
    import graftpunk.devtools.scaffold.render as render_module

    assert GP_FILL_MARKER == "GP-FILL"
    assert module_name_for("My-Shop.v2") == "my_shop_v2"
    assert "module_name_for" not in render_module.__all__
    assert not hasattr(render_module, "module_name_for")


def test_the_entry_point_group_is_spelled_in_one_module() -> None:
    """The group is the runtime's (graftpunk.plugins.PLUGINS_GROUP), and its two uses,
    a TOML table header and an entry-point lookup, read that constant everywhere.
    The renderer's _PLUGINS_MODULE, the package a generated plugin imports from,
    is the same text as an import path, a different fact, and is not counted."""
    import graftpunk
    from graftpunk.plugins import PLUGINS_GROUP

    assert PLUGINS_GROUP == "graftpunk.plugins"
    package = Path(graftpunk.__file__).parent
    spelled = [
        f"{path.relative_to(package).as_posix()}:{line}"
        for path in sorted(package.rglob("*.py"))
        for line in _group_literals(ast.parse(path.read_text(encoding="utf-8")), PLUGINS_GROUP)
    ]
    assert spelled == []


def test_the_reserved_command_names_are_the_root_commands_registration_adds() -> None:
    """devtools does not import graftpunk.cli, so policy keeps its own copy."""
    from graftpunk.cli.plugin_commands import AUTO_ROOT_COMMAND_NAMES

    assert set(policy._AUTO_ROOT_COMMAND_NAMES) == set(AUTO_ROOT_COMMAND_NAMES)
