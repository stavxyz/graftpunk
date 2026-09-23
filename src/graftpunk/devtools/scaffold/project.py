"""Decides new-project vs add-to-suite, checks for conflicts, and plans the writes.

Generation lives in ``render.py``; this module owns the filesystem
decisions ``render.py`` has no business making (plugin tooling spec,
2026-09-11). The writes themselves go through ``write.py``.
"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass
from pathlib import Path

from graftpunk.devtools.captures_rule import CAPTURES_DIR, with_ignored
from graftpunk.devtools.errors import ScaffoldWriteError
from graftpunk.devtools.scaffold.pyproject_edit import (
    PyprojectEditError,
    with_entry_point,
    with_wheel_package,
)
from graftpunk.devtools.scaffold.render import ScaffoldSpec, class_name_for, module_name_for, render
from graftpunk.devtools.scaffold.write import (
    ChangeConflictError,
    InvalidChangeError,
    PlannedChange,
    Validator,
    apply_changes,
    find_conflicts,
    validate_python,
    validate_toml,
)
from graftpunk.logging import get_logger

LOG = get_logger(__name__)

__all__ = [
    "NotAPluginSuiteError",
    "ScaffoldConflictError",
    "ScaffoldResult",
    "write_scaffold",
]

PLUGINS_ENTRY_POINT_GROUP = "graftpunk.plugins"


ScaffoldConflictError = ChangeConflictError
"""A target path already exists; nothing was written. The writer's own error, under
the name the CLI and callers of ``write_scaffold`` already catch."""


def _validator_for(relative: str) -> Validator | None:
    """The grammar a rendered file must satisfy before it is written."""
    if relative.endswith(".py"):
        return validate_python
    if relative.endswith(".toml"):
        return validate_toml
    return None


class NotAPluginSuiteError(ValueError):
    """*target_dir* holds a ``pyproject.toml`` that is a different kind of project.

    Its own class so the CLI can tell it apart from the ``ValueError`` an
    invalid plugin name raises: both used to log ``reason="invalid_name"``
    (polish round 1, 2026-09-12). A ``ValueError`` subclass, so a caller that
    only cares that the call refused still catches it.
    """


@dataclass(frozen=True)
class ScaffoldResult:
    mode: str
    written: tuple[Path, ...]
    gitignore_updated: bool
    pyproject_updated: bool


def _existing_pyproject(target_dir: Path) -> Path | None:
    candidate = target_dir / "pyproject.toml"
    return candidate if candidate.is_file() else None


def _declares_plugin_group(pyproject_path: Path) -> bool:
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    return PLUGINS_ENTRY_POINT_GROUP in data.get("project", {}).get("entry-points", {})


def write_scaffold(
    target_dir: Path, spec: ScaffoldSpec, *, force_new: bool = False
) -> ScaffoldResult:
    """Render *spec* and write it under *target_dir*, refusing on any conflict.

    Args:
        target_dir: A new project's root, or an existing suite's root.
        spec: What to generate; its ``mode`` is overwritten by this
            function's own decision (new project vs add to suite).
        force_new: ``--new``: force a new project in *target_dir* even when
            a suite ``pyproject.toml`` is present.

    Raises:
        ScaffoldConflictError: A target path already exists, or the suite's
            ``pyproject.toml`` or ``.gitignore`` changed after it was read.
            Nothing is written; the check runs before any file is touched.
        NotAPluginSuiteError: *target_dir* has a ``pyproject.toml`` that does
            not declare the ``graftpunk.plugins`` entry-point group: a
            different kind of project.
        PyprojectEditError: In suite mode, the ``pyproject.toml`` edit could
            not be computed (see ``pyproject_edit.py``). ``pyproject.toml`` is
            never written and no rendered file is either, so the suite is
            left byte-identical to how ``write_scaffold`` found it.
        InvalidChangeError: A rendered file fails its own grammar check.
            Nothing is written.
        ScaffoldWriteError: A write failed; every change already applied,
            the ``pyproject.toml`` edit included, is undone first where it can
            be, and the error names the file, the OS error, and any path left
            changed (see ``write.py``). A bare ``OSError`` now means a read
            before anything was written failed.
    """
    existing = None if force_new else _existing_pyproject(target_dir)
    if existing is not None and not _declares_plugin_group(existing):
        raise NotAPluginSuiteError(
            f"{existing} exists but does not declare "
            f'[project.entry-points."{PLUGINS_ENTRY_POINT_GROUP}"]. '
            "This does not look like a graftpunk plugin suite. Use --new to start a fresh "
            "project in a different directory, or add the entry-point group by hand."
        )

    mode = "add_to_suite" if existing is not None else "new_project"
    resolved_spec = dataclasses.replace(spec, mode=mode)
    files = render(resolved_spec)

    # A .gitkeep only exists to put an empty directory in git, so one whose
    # directory is already there is nothing to write. render() stays pure and
    # does not know the filesystem; this module does. Without it, adding a
    # second plugin to a suite refused on the .gitkeep the first add created
    # (polish round 1, 2026-09-12).
    if mode == "add_to_suite":
        files = {
            relative: content
            for relative, content in files.items()
            if not (relative.endswith("/.gitkeep") and (target_dir / relative).parent.is_dir())
        }

    rendered = [
        PlannedChange(target_dir / rel, content, validate=_validator_for(rel))
        for rel, content in files.items()
    ]
    # Deliberately before apply_changes runs its own check: a refusal here never
    # computes the pyproject.toml edit.
    conflicts = find_conflicts(rendered)
    if conflicts:
        LOG.debug("scaffold_write_refused", conflicts=len(conflicts))
        raise ScaffoldConflictError(conflicts)

    changes: list[PlannedChange] = []
    pyproject_updated = False
    if mode == "add_to_suite":
        assert existing is not None  # narrows for the type checker; mode implies it
        module = module_name_for(resolved_spec.name)
        package = f"src/graftpunk_{module}"
        target = f"graftpunk_{module}.plugin:{class_name_for(resolved_spec.name)}"
        original = existing.read_text(encoding="utf-8")
        try:
            edited = with_wheel_package(
                with_entry_point(original, existing, resolved_spec.name, target), existing, package
            )
        except PyprojectEditError:
            LOG.debug("scaffold_write_refused", reason="pyproject_edit_error")
            raise
        # The pyproject.toml edit is applied first and restored with everything
        # else if any rendered file then fails to write.
        changes.append(PlannedChange(existing, edited, original=original, validate=validate_toml))
        pyproject_updated = True
    changes.extend(rendered)

    gitignore_updated = False
    if mode == "add_to_suite":
        # Last in the batch: the ignore line protects files this call writes, so
        # a failed write restores it with everything else (polish round 1,
        # 2026-09-12).
        gitignore = target_dir / ".gitignore"
        before = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else None
        after = with_ignored(before or "", CAPTURES_DIR)
        if after != (before or ""):
            changes.append(PlannedChange(gitignore, after, original=before))
            gitignore_updated = True

    try:
        apply_changes(changes)
    except ChangeConflictError as exc:
        LOG.debug("scaffold_write_refused", conflicts=len(exc.conflicts))
        raise
    except InvalidChangeError:
        LOG.debug("scaffold_write_refused", reason="invalid_change")
        raise
    except ScaffoldWriteError:
        LOG.debug("scaffold_write_refused", reason="os_error")
        raise

    LOG.info("scaffold_written", mode=mode, target_dir=str(target_dir), files=len(rendered))
    return ScaffoldResult(
        mode=mode,
        written=tuple(sorted(c.path for c in rendered)),
        gitignore_updated=gitignore_updated,
        pyproject_updated=pyproject_updated,
    )
