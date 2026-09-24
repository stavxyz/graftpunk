"""Declarative project policy: nothing here touches the filesystem
(graft skill spec, 2026-09-21, "Project policy is declarative")."""

from __future__ import annotations

import ast
from pathlib import Path

import graftpunk.devtools.scaffold.policy as policy
from graftpunk.devtools.scaffold.policy import FIXTURES_TREE, TESTS_DIR, fixtures_root


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
