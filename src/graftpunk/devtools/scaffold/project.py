"""Decides new-project vs add-to-suite, checks for conflicts, and writes.

Generation lives in ``render.py``; this module owns the filesystem
decisions ``render.py`` has no business making (plugin tooling spec,
2026-09-11).
"""

from __future__ import annotations

import contextlib
import dataclasses
import tomllib
from dataclasses import dataclass
from pathlib import Path

from graftpunk.devtools.captures import CAPTURES_DIR, ensure_ignored
from graftpunk.devtools.scaffold.pyproject_edit import (
    PyprojectEditError,
    add_entry_point,
    add_wheel_package,
)
from graftpunk.devtools.scaffold.render import ScaffoldSpec, class_name_for, module_name_for, render
from graftpunk.logging import get_logger

LOG = get_logger(__name__)

__all__ = ["ScaffoldConflictError", "ScaffoldResult", "write_scaffold"]

PLUGINS_ENTRY_POINT_GROUP = "graftpunk.plugins"


def _missing_parents(directory: Path) -> list[Path]:
    """The ancestors of *directory*, *directory* included, that do not exist yet.

    Deepest last, which is the order ``mkdir(parents=True)`` creates them and the
    reverse of the order they have to be removed in.
    """
    missing: list[Path] = []
    current = directory
    while not current.exists() and current != current.parent:
        missing.append(current)
        current = current.parent
    return list(reversed(missing))


def _undo_writes(written: list[Path], created_dirs: list[Path]) -> None:
    """Remove what this call put on disk, best effort.

    Files first, then the directories this call created, deepest first and only
    while they are empty: a directory that already held something, or that the
    user has since filled, is not this function's to delete. Every removal is
    guarded because unwinding after an I/O failure runs in the same conditions
    that caused it, and the original error is the one the caller must see.
    """
    for path in written:
        with contextlib.suppress(OSError):
            path.unlink()
    for directory in reversed(created_dirs):
        with contextlib.suppress(OSError):
            directory.rmdir()


class ScaffoldConflictError(Exception):
    """One or more target paths already exist; nothing was written."""

    def __init__(self, conflicts: list[Path]) -> None:
        self.conflicts = conflicts
        listing = ", ".join(str(p) for p in conflicts)
        super().__init__(f"Refusing to overwrite existing file(s): {listing}")


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
        ScaffoldConflictError: A target path already exists. Nothing is
            written; the check runs before any file is touched.
        ValueError: *target_dir* has a ``pyproject.toml`` that does not
            declare the ``graftpunk.plugins`` entry-point group: a
            different kind of project.
        PyprojectEditError: In suite mode, ``pyproject.toml`` could not be
            edited (see ``pyproject_edit.py``). ``pyproject.toml`` is
            restored to its original bytes before this re-raises, and no
            rendered file has been written yet, so the suite is left
            byte-identical to how ``write_scaffold`` found it.
    """
    existing = None if force_new else _existing_pyproject(target_dir)
    if existing is not None and not _declares_plugin_group(existing):
        raise ValueError(
            f"{existing} exists but does not declare "
            f'[project.entry-points."{PLUGINS_ENTRY_POINT_GROUP}"]. '
            "This does not look like a graftpunk plugin suite. Use --new to start a fresh "
            "project in a different directory, or add the entry-point group by hand."
        )

    mode = "add_to_suite" if existing is not None else "new_project"
    resolved_spec = dataclasses.replace(spec, mode=mode)
    files = render(resolved_spec)

    # Conflict detection runs before anything else is touched, in either
    # mode: a refusal here must never have edited pyproject.toml either.
    targets = {target_dir / rel: content for rel, content in files.items()}
    conflicts = sorted(p for p in targets if p.exists())
    if conflicts:
        LOG.warning("scaffold_write_refused", conflicts=len(conflicts))
        raise ScaffoldConflictError(conflicts)

    gitignore_updated = False
    pyproject_updated = False
    original_pyproject_text: str | None = None
    if mode == "add_to_suite":
        assert existing is not None  # narrows for the type checker; mode implies it
        module = module_name_for(resolved_spec.name)
        package = f"src/graftpunk_{module}"
        target = f"graftpunk_{module}.plugin:{class_name_for(resolved_spec.name)}"
        # Both pyproject.toml edits happen (and, on failure, are undone)
        # before any rendered file is written: a PyprojectEditError from
        # add_wheel_package must not leave add_entry_point's edit behind,
        # and neither edit failing may leave a new package directory or
        # test module on disk.
        original_pyproject_text = existing.read_text(encoding="utf-8")
        try:
            add_entry_point(existing, resolved_spec.name, target)
            add_wheel_package(existing, package)
        except PyprojectEditError:
            existing.write_text(original_pyproject_text, encoding="utf-8")
            LOG.warning("scaffold_write_refused", reason="pyproject_edit_error")
            raise
        pyproject_updated = True
        gitignore_updated = ensure_ignored(target_dir, CAPTURES_DIR)

    written: list[Path] = []
    created_dirs: list[Path] = []
    try:
        for path, content in targets.items():
            created_dirs.extend(_missing_parents(path.parent))
            path.parent.mkdir(parents=True, exist_ok=True)
            written.append(path)
            path.write_text(content, encoding="utf-8")
    except OSError:
        _undo_writes(written, created_dirs)
        if existing is not None and original_pyproject_text is not None:
            try:
                existing.write_text(original_pyproject_text, encoding="utf-8")
            except OSError as restore_exc:
                LOG.warning(
                    "scaffold_pyproject_restore_failed",
                    path=str(existing),
                    error=str(restore_exc),
                )
        LOG.warning("scaffold_write_refused", reason="os_error", written=len(written))
        raise

    LOG.info("scaffold_written", mode=mode, target_dir=str(target_dir), files=len(targets))
    return ScaffoldResult(
        mode=mode,
        written=tuple(sorted(targets)),
        gitignore_updated=gitignore_updated,
        pyproject_updated=pyproject_updated,
    )
