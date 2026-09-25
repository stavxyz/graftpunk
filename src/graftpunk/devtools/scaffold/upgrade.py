"""Brings an existing plugin project up to ``PROJECT_REQUIREMENTS``, through ``write.py``.

On purpose and by name: ``gp plugin upgrade`` runs it, and nothing else does.
It applies only what the project reader's view says is missing, so it is
idempotent under the reader's own definition, and it changes nothing a project
already has (graft skill spec, 2026-09-21). The statements are added by
``pysrc.with_bindings``, the assembler the renderer uses for a new conftest, so
an upgraded conftest is byte-identical to a generated one.
"""

from __future__ import annotations

from pathlib import Path

from graftpunk.devtools.errors import DevtoolsRefusal
from graftpunk.devtools.plugin_project import require_plugin_project
from graftpunk.devtools.scaffold.policy import ProjectRequirement
from graftpunk.devtools.scaffold.pysrc import ImportPlacementError, with_bindings
from graftpunk.devtools.scaffold.write import (
    PlannedChange,
    apply_changes,
    read_original,
    validate_python,
)

__all__ = ["UpgradeRefusedError", "upgrade_project"]


class UpgradeRefusedError(DevtoolsRefusal, ValueError):
    """A requirement's file does not parse, or its imports are not in a shape the
    migrator places into; nothing was written."""


def upgrade_project(root: Path) -> tuple[ProjectRequirement, ...]:
    """Apply every requirement *root*'s project lacks, and return them.

    Raises:
        UpgradeRefusedError: A requirement's file does not parse, or its imports
            are not in a shape ``pysrc.with_import`` places into.
        NotAPluginProjectError: *root* is not a plugin project.
        PluginProjectError: See :func:`read_project`.
        ScaffoldWriteError: A write failed; every file was restored first, or the
            error names what was left changed.
    """
    view = require_plugin_project(root)
    unreadable = view.unreadable_files()
    if unreadable:
        listing = "; ".join(f"{path}: {reason}" for path, reason in unreadable)
        raise UpgradeRefusedError(
            f"{listing}. gp plugin upgrade edits only a file it can parse; fix it by "
            f"hand, then run it again."
        )
    missing = view.missing_requirements()
    by_path: dict[str, list[ProjectRequirement]] = {}
    for requirement in missing:
        by_path.setdefault(requirement.path, []).append(requirement)
    changes: list[PlannedChange] = []
    for relative, requirements in by_path.items():
        path = root / relative
        original = read_original(path) if path.is_file() else None
        try:
            content = with_bindings(original or "", requirements)
        except ImportPlacementError as exc:
            raise UpgradeRefusedError(f"{relative}: {exc}") from exc
        changes.append(PlannedChange(path, content, original=original, validate=validate_python))
    apply_changes(changes)
    return missing
