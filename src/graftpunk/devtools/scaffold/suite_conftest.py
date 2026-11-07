"""A suite's shared conftest: the fixtures check over the first plugin's fixtures
alone, with every member's directory in ``SUITE_MEMBER_FIXTURES`` (#370).

Pure text in, text out, like ``pysrc``: ``gp plugin new`` in suite mode and
``gp plugin upgrade`` both call :func:`with_suite_check`, so the two write the same
conftest. It reads the conftest's module-level assignments and edits only what gp
wrote: the ``SUITE_MEMBER_FIXTURES`` tuple when it is a literal of names, and a
``sanitised_fixtures`` statement exactly as ``gp plugin new`` writes it for a
standalone project. Any other ``sanitised_fixtures`` is the author's, left alone.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass

from graftpunk.devtools.scaffold import policy
from graftpunk.devtools.scaffold.pysrc import binds_name, joined_like, source_lines, with_bindings

__all__ = ["SuiteConftestState", "has_check", "read_suite_conftest", "with_suite_check"]

_CHECK_NAME = "sanitised_fixtures"


def _value_of(statement: str) -> str:
    """The AST dump of *statement*'s assigned value, an assignment's right-hand side."""
    node = ast.parse(statement).body[0]
    assert isinstance(node, ast.Assign)
    return ast.dump(node.value)


# The standalone check gp plugin new writes, compared by AST.
_UNSCOPED_VALUE = _value_of(policy.PROJECT_REQUIREMENTS[-1].statement)


@dataclass(frozen=True)
class SuiteConftestState:
    """What a suite's conftest holds of its fixtures check: ``listed``, the names
    ``SUITE_MEMBER_FIXTURES`` binds when it is a literal tuple or list of strings
    (``None`` when it is absent or anything else); ``skip_bound``, whether the name
    is bound at all; ``unscoped``, whether ``sanitised_fixtures`` is the
    standalone whole-tree statement gp wrote, which a suite scopes; and
    ``needs_skip``, whether the conftest needs ``SUITE_MEMBER_FIXTURES`` bound: its
    check is missing (gp adds both), unscoped, or reads the name. A check the
    author wrote some other way needs nothing from gp."""

    listed: tuple[str, ...] | None
    skip_bound: bool
    unscoped: bool
    needs_skip: bool = True


def _assignment(tree: ast.Module, name: str) -> ast.Assign | None:
    """The last module-level ``name = ...`` with *name* its one target."""
    found = None
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
        ):
            found = node
    return found


def _listed(node: ast.Assign | None) -> tuple[str, ...] | None:
    if node is None or not isinstance(node.value, (ast.Tuple, ast.List)):
        return None
    names = []
    for item in node.value.elts:
        if not (isinstance(item, ast.Constant) and isinstance(item.value, str)):
            return None
        names.append(item.value)
    return tuple(names)


def _unscoped(node: ast.Assign | None) -> bool:
    return node is not None and ast.dump(node.value) == _UNSCOPED_VALUE


def _reads_skip(node: ast.Assign | None) -> bool:
    return node is not None and any(
        isinstance(n, ast.Name) and n.id == policy.SUITE_SKIP_NAME for n in ast.walk(node.value)
    )


def read_suite_conftest(tree: ast.Module) -> SuiteConftestState:
    """*tree*'s :class:`SuiteConftestState`."""
    check = _assignment(tree, _CHECK_NAME)
    return SuiteConftestState(
        listed=_listed(_assignment(tree, policy.SUITE_SKIP_NAME)),
        skip_bound=binds_name(tree, policy.SUITE_SKIP_NAME),
        unscoped=_unscoped(check),
        needs_skip=not binds_name(tree, _CHECK_NAME) or _reads_skip(check),
    )


def has_check(text: str) -> bool:
    """Whether *text*, a conftest, binds ``sanitised_fixtures``: whether it checks any
    fixtures at all. False for text that does not parse."""
    try:
        return binds_name(ast.parse(text), _CHECK_NAME)
    except SyntaxError:
        return False


def with_suite_check(
    text: str,
    owner: str,
    members: Sequence[str],
    *,
    add_check: bool = True,
    first_party: frozenset[str] = frozenset(),
) -> tuple[str, tuple[str, ...]]:
    """*text*, a suite's conftest, with the fixtures check over *owner*'s fixtures
    alone, and what changed, one line each. *members* are the member directory names
    in suite order.

    A literal ``SUITE_MEMBER_FIXTURES`` gains each name of *members* it lacks. A
    ``sanitised_fixtures`` exactly as gp writes it for a standalone project becomes
    ``policy.SUITE_CHECK_STATEMENT``, with the tuple bound just before it when the
    conftest has none, as it is before a check that reads the tuple without it. Each
    of these only narrows what is checked. With no ``sanitised_fixtures`` at all, the
    bindings of ``policy.suite_requirements`` it lacks are appended, but only with
    *add_check*: that turns on a check over the first plugin's fixtures, which can
    fail its tests, so only an upgrade of the whole project or of *owner* asks for it
    (#370). Anything else is left as it is.

    Raises:
        SyntaxError: *text* does not parse.
        ImportPlacementError: An import is not in a shape ``pysrc.with_import`` places.
    """
    tree = ast.parse(text)
    lines = source_lines(text)
    changes: list[str] = []
    # (first line index, last line index) -> replacement lines, applied bottom up.
    edits: dict[tuple[int, int], list[str]] = {}
    skip_node = _assignment(tree, policy.SUITE_SKIP_NAME)
    listed = _listed(skip_node)
    if skip_node is not None and listed is not None:
        lacking = [m for m in members if m not in listed]
        if lacking:
            span = (skip_node.lineno - 1, (skip_node.end_lineno or skip_node.lineno) - 1)
            edits[span] = policy.suite_skip_statement([*listed, *lacking]).split("\n")
            changes.append(f"{policy.SUITE_SKIP_NAME}: added {', '.join(lacking)}")
    check_node = _assignment(tree, _CHECK_NAME)
    appended = []
    if _unscoped(check_node):
        assert check_node is not None  # _unscoped is False for None
        replacement = [policy.SUITE_CHECK_STATEMENT]
        if not binds_name(tree, policy.SUITE_SKIP_NAME):
            replacement.insert(0, policy.suite_skip_statement(members))
            changes.append(f"added {policy.SUITE_SKIP_NAME}")
        span = (check_node.lineno - 1, (check_node.end_lineno or check_node.lineno) - 1)
        edits[span] = "\n".join(replacement).split("\n")
        changes.append(f"{_CHECK_NAME}: skips {policy.SUITE_SKIP_NAME}")
    elif _reads_skip(check_node) and not binds_name(tree, policy.SUITE_SKIP_NAME):
        assert check_node is not None  # _reads_skip is False for None
        span = (check_node.lineno - 1, (check_node.end_lineno or check_node.lineno) - 1)
        own = lines[span[0] : span[1] + 1]
        edits[span] = [*policy.suite_skip_statement(members).split("\n"), *own]
        changes.append(f"added {policy.SUITE_SKIP_NAME}")
    elif add_check and not binds_name(tree, _CHECK_NAME):
        appended = [
            r for r in policy.suite_requirements(owner, members) if not binds_name(tree, r.name)
        ]
        changes.extend(f"added {r.name}" for r in appended)
    for (first, last), replacement in sorted(edits.items(), reverse=True):
        lines[first : last + 1] = replacement
    if edits:
        text = joined_like(text, lines)
    if appended:
        text = with_bindings(text, appended, first_party=first_party)
    return text, tuple(changes)
