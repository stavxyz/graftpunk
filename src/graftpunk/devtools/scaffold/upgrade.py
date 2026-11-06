"""Brings an existing plugin project up to ``PROJECT_REQUIREMENTS``, through ``write.py``.

On purpose and by name: ``gp plugin upgrade`` runs it, and nothing else does.
It applies only what the project reader's view says is missing, so it is
idempotent under the reader's own definition, and it changes nothing a project
already has (graft skill spec, 2026-09-21). The statements are added by
``pysrc.with_bindings``, the assembler the renderer uses for a new conftest, so
an upgraded conftest is byte-identical to a generated one. It also creates
``policy.FIXTURES_TREE`` when a project lacks it, reading that fact from
``ProjectView.fixtures_tree_present`` (the same field ``gp plugin check``
reads). It decides nothing about the filesystem itself: every path it might
otherwise refuse on, a blocked ``policy.FIXTURES_TREE`` or an ancestor of a
requirement's file included, is read from ``ProjectView.fixtures_tree_blocked``
and ``ProjectView.unreadable_files()``, the same two facts ``gp plugin check``
reports findings from, so the two consumers cannot disagree. Whenever it
writes, the same ``apply_changes`` call raises the project's plain
``graftpunk>=`` lower bound to the running release
(``project.planned_graftpunk_floor``), since what it adds imports
``graftpunk.testing``, which an older graftpunk lacks.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from graftpunk.devtools.errors import DevtoolsRefusal
from graftpunk.devtools.plugin_project import (
    DOES_NOT_PARSE,
    require_plugin_project,
    unreadable_file_message,
)
from graftpunk.devtools.scaffold.policy import (
    FIXTURES_PLACEHOLDER,
    FIXTURES_TREE,
    ProjectRequirement,
)
from graftpunk.devtools.scaffold.project import planned_graftpunk_floor
from graftpunk.devtools.scaffold.pyproject_edit import (
    FloorVerdict,
)
from graftpunk.devtools.scaffold.pysrc import ImportPlacementError, with_bindings
from graftpunk.devtools.scaffold.write import (
    PlannedChange,
    apply_changes,
    read_original,
    validate_python,
)

__all__ = ["UpgradeApplied", "UpgradeRefusedError", "upgrade_project"]


class UpgradeRefusedError(DevtoolsRefusal, ValueError):
    """A requirement's file, or ``tests/fixtures``, is one of the reader's
    unreadable or blocked kinds (``graftpunk.devtools.plugin_check``'s module
    docstring has the complete list), or an import is not in a shape
    ``pysrc.with_import`` places into; nothing was written."""


@dataclass(frozen=True)
class UpgradeApplied:
    """What ``upgrade_project`` wrote: the ``PROJECT_REQUIREMENTS`` entries the
    project's files were missing, in the order they were applied, and whether
    ``policy.FIXTURES_TREE`` did not exist and was created (with
    ``FIXTURES_PLACEHOLDER``, the same empty file ``gp plugin new`` writes).
    ``changed`` is whether either happened, for a caller that wants one
    truth test instead of reading both fields. ``floor`` is what a change did
    to the project's ``graftpunk`` requirement (see
    ``project.planned_graftpunk_floor``), ``None`` when nothing changed.

    ``created_roots`` holds each suite member's fixtures root it created, with
    ``FIXTURES_PLACEHOLDER`` (#370). ``plugin`` is the entry point it was scoped to,
    or ``None``. What the project still lacks that this call left alone is counted
    in two parts: ``left_in_scope``, the requirements of its scope (``plugin``, or
    the whole project) no upgrade adds (a suite's whole-tree conftest check), and
    ``left_elsewhere``, another plugin's requirements and fixtures roots under
    ``plugin``."""

    requirements: tuple[ProjectRequirement, ...]
    created_fixtures_tree: bool = False
    floor: FloorVerdict = None
    created_roots: tuple[str, ...] = ()
    plugin: str | None = None
    left_in_scope: int = 0
    left_elsewhere: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.requirements) or self.created_fixtures_tree or bool(self.created_roots)


def upgrade_project(root: Path, *, plugin: str | None = None) -> UpgradeApplied:
    """Apply every requirement *root*'s project lacks, and return what was applied.

    With *plugin* (an entry-point name, or its module name), only the requirements
    that serve it (``ProjectRequirement.plugin``) and, for a suite member, its
    fixtures root when that is missing. In a suite it never adds the shared
    conftest's check over the whole fixtures tree (``upgrade_adds``), with or
    without *plugin*: that check fails every plugin's tests on one plugin's
    fixture (#370).

    Raises:
        UpgradeRefusedError: A requirement's file, or ``tests/fixtures``, is one
            of the reader's unreadable or blocked kinds
            (``graftpunk.devtools.plugin_check``'s module docstring has the
            complete list), or an import is not in a shape
            ``pysrc.with_import`` places into.
        UpgradeRefusedError: *plugin* names no readable plugin of the project.
        NotAPluginProjectError: *root* is not a plugin project.
        PluginProjectError: See :func:`read_project`.
        ScaffoldWriteError: A write failed; every file was restored first, or the
            error names what was left changed.
    """
    view = require_plugin_project(root)
    if plugin is not None:
        found = view.entry_point_for(plugin)
        if found is None:
            names = ", ".join(p.entry_point for p in view.plugins) or "none"
            raise UpgradeRefusedError(
                f"no plugin named {plugin!r} in this project; its plugins: {names}."
            )
        plugin = found
    roots = [r for r in view.member_roots if plugin is None or r.entry_point == plugin]
    blocked: list[tuple[str, str]] = []
    if view.fixtures_tree_blocked is not None:
        blocked.append(view.fixtures_tree_blocked)
    blocked.extend(view.unreadable_files(plugin, upgrade_adds_only=True))
    blocked.extend(r.blocked for r in roots if r.blocked is not None)
    blocked = list(dict.fromkeys(blocked))
    if blocked:
        # A single space, not "; ": each entry is already a full sentence
        # ending in "." and contains its own semicolon (the advice clause),
        # so "; " between two entries reads as a third one.
        listing = " ".join(f"{path}: {unreadable_file_message(reason)}" for path, reason in blocked)
        raise UpgradeRefusedError(listing)
    in_scope = view.missing_requirements(plugin)
    missing = tuple(r for r in in_scope if r.upgrade_adds)
    created_roots = tuple(r.path for r in roots if not r.present and r.blocked is None)
    left_elsewhere = (
        len(view.missing_requirements())
        - len(in_scope)
        + sum(1 for r in view.member_roots if not r.present and r.path not in created_roots)
    )
    by_path: dict[str, list[ProjectRequirement]] = {}
    for requirement in missing:
        by_path.setdefault(requirement.path, []).append(requirement)
    changes: list[PlannedChange] = []
    for relative, requirements in by_path.items():
        path = root / relative
        status = view.requirements.get(requirements[0].key)
        present = status.present if status is not None else False
        original = read_original(path) if present else None
        try:
            content = with_bindings(
                original or "", requirements, first_party=view.first_party_packages
            )
        except ImportPlacementError as exc:
            raise UpgradeRefusedError(f"{relative}: {exc}") from exc
        except SyntaxError as exc:
            raise UpgradeRefusedError(
                f"{relative}: {DOES_NOT_PARSE} after its import is placed ({exc.msg})."
            ) from exc
        changes.append(PlannedChange(path, content, original=original, validate=validate_python))
    fixtures_tree = root / FIXTURES_TREE
    created_fixtures_tree = not view.fixtures_tree_present
    if created_fixtures_tree:
        changes.append(PlannedChange(fixtures_tree / FIXTURES_PLACEHOLDER, ""))
    changes.extend(PlannedChange(root / path / FIXTURES_PLACEHOLDER, "") for path in created_roots)
    floor = None
    if changes:
        floor, floor_changes = planned_graftpunk_floor(root)
        changes.extend(floor_changes)
    apply_changes(changes)
    return UpgradeApplied(
        requirements=missing,
        created_fixtures_tree=created_fixtures_tree,
        floor=floor,
        created_roots=created_roots,
        plugin=plugin,
        left_in_scope=len(in_scope) - len(missing),
        left_elsewhere=left_elsewhere,
    )
