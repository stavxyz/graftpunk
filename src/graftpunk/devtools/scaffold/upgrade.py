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
    CannotRaiseFloor,
    DynamicDependencies,
    RaisedFloor,
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
    ``project.planned_graftpunk_floor``), ``None`` when nothing changed."""

    requirements: tuple[ProjectRequirement, ...]
    created_fixtures_tree: bool = False
    floor: RaisedFloor | CannotRaiseFloor | DynamicDependencies | None = None

    @property
    def changed(self) -> bool:
        return bool(self.requirements) or self.created_fixtures_tree


def upgrade_project(root: Path) -> UpgradeApplied:
    """Apply every requirement *root*'s project lacks, and return what was applied.

    Raises:
        UpgradeRefusedError: A requirement's file, or ``tests/fixtures``, is one
            of the reader's unreadable or blocked kinds
            (``graftpunk.devtools.plugin_check``'s module docstring has the
            complete list), or an import is not in a shape
            ``pysrc.with_import`` places into.
        NotAPluginProjectError: *root* is not a plugin project.
        PluginProjectError: See :func:`read_project`.
        ScaffoldWriteError: A write failed; every file was restored first, or the
            error names what was left changed.
    """
    view = require_plugin_project(root)
    blocked: list[tuple[str, str]] = []
    if view.fixtures_tree_blocked is not None:
        blocked.append(view.fixtures_tree_blocked)
    blocked.extend(view.unreadable_files())
    blocked = list(dict.fromkeys(blocked))
    if blocked:
        # A single space, not "; ": each entry is already a full sentence
        # ending in "." and contains its own semicolon (the advice clause),
        # so "; " between two entries reads as a third one.
        listing = " ".join(f"{path}: {unreadable_file_message(reason)}" for path, reason in blocked)
        raise UpgradeRefusedError(listing)
    missing = view.missing_requirements()
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
    floor = None
    if changes:
        floor, floor_changes = planned_graftpunk_floor(root)
        changes.extend(floor_changes)
    apply_changes(changes)
    return UpgradeApplied(
        requirements=missing, created_fixtures_tree=created_fixtures_tree, floor=floor
    )
