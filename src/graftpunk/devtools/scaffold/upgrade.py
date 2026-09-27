"""Brings an existing plugin project up to ``PROJECT_REQUIREMENTS``, through ``write.py``.

On purpose and by name: ``gp plugin upgrade`` runs it, and nothing else does.
It applies only what the project reader's view says is missing, so it is
idempotent under the reader's own definition, and it changes nothing a project
already has (graft skill spec, 2026-09-21). The statements are added by
``pysrc.with_bindings``, the assembler the renderer uses for a new conftest, so
an upgraded conftest is byte-identical to a generated one. It also creates
``policy.FIXTURES_TREE`` when a project lacks it, the one thing here the
reader's view does not decide on its own (the view has no missing-directory
fact; the filesystem check is this module's).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from graftpunk.devtools.errors import DevtoolsRefusal
from graftpunk.devtools.plugin_project import require_plugin_project
from graftpunk.devtools.scaffold.policy import (
    FIXTURES_PLACEHOLDER,
    FIXTURES_TREE,
    ProjectRequirement,
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
    """A requirement's file does not parse, is not a regular file, or its imports
    are not in a shape the migrator places into; nothing was written."""


@dataclass(frozen=True)
class UpgradeApplied:
    """What ``upgrade_project`` wrote: the ``PROJECT_REQUIREMENTS`` entries the
    project's files were missing, in the order they were applied, and whether
    ``policy.FIXTURES_TREE`` did not exist and was created (with
    ``FIXTURES_PLACEHOLDER``, the same empty file ``gp plugin new`` writes).
    Iterating or truth-testing an instance reads ``requirements`` and
    ``created_fixtures_tree`` together, so a caller that only checked
    "was anything applied" before this field existed still gets the right
    answer."""

    requirements: tuple[ProjectRequirement, ...]
    created_fixtures_tree: bool = False

    def __bool__(self) -> bool:
        return bool(self.requirements) or self.created_fixtures_tree

    def __iter__(self) -> Iterator[ProjectRequirement]:
        return iter(self.requirements)


def _refuse_unless_directory(path: Path, root: Path) -> None:
    """Refuse in one line when *path* exists but is not a directory: this
    function is about to write inside it (a requirement's parent directory, or
    the fixtures tree itself)."""
    if path.exists() and not path.is_dir():
        raise UpgradeRefusedError(
            f"{path.relative_to(root)}: exists but is not a directory. gp plugin "
            f"upgrade writes there; move it aside, then run it again."
        )


def upgrade_project(root: Path) -> UpgradeApplied:
    """Apply every requirement *root*'s project lacks, and return what was applied.

    Raises:
        UpgradeRefusedError: A requirement's file does not parse, is not a regular
            file, or its imports are not in a shape ``pysrc.with_import`` places
            into.
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
        _refuse_unless_directory(path.parent, root)
        if path.exists() and not path.is_file():
            raise UpgradeRefusedError(
                f"{relative}: exists but is not a regular file. gp plugin upgrade writes "
                f"a Python module there; move it aside, then run it again."
            )
        original = read_original(path) if path.is_file() else None
        try:
            content = with_bindings(
                original or "", requirements, first_party=view.first_party_packages
            )
        except ImportPlacementError as exc:
            raise UpgradeRefusedError(f"{relative}: {exc}") from exc
        except SyntaxError as exc:
            raise UpgradeRefusedError(
                f"{relative}: does not parse after its import is placed ({exc.msg})."
            ) from exc
        changes.append(PlannedChange(path, content, original=original, validate=validate_python))
    fixtures_tree = root / FIXTURES_TREE
    _refuse_unless_directory(fixtures_tree, root)
    created_fixtures_tree = not fixtures_tree.is_dir()
    if created_fixtures_tree:
        changes.append(PlannedChange(fixtures_tree / FIXTURES_PLACEHOLDER, ""))
    apply_changes(changes)
    return UpgradeApplied(requirements=missing, created_fixtures_tree=created_fixtures_tree)
