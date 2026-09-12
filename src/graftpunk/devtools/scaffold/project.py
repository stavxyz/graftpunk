"""Decides new-project vs add-to-suite, checks for conflicts, and writes.

Generation lives in ``render.py``; this module owns the filesystem
decisions ``render.py`` has no business making (plugin tooling spec,
2026-09-11).
"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass
from pathlib import Path

from graftpunk.devtools.captures import CAPTURES_DIR, ensure_ignored
from graftpunk.devtools.scaffold.pyproject_edit import add_entry_point, add_wheel_package
from graftpunk.devtools.scaffold.render import ScaffoldSpec, class_name_for, module_name_for, render
from graftpunk.logging import get_logger

LOG = get_logger(__name__)

__all__ = ["ScaffoldConflictError", "ScaffoldResult", "write_scaffold"]

PLUGINS_ENTRY_POINT_GROUP = "graftpunk.plugins"


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

    targets = {target_dir / rel: content for rel, content in files.items()}
    conflicts = sorted(p for p in targets if p.exists())
    if conflicts:
        LOG.warning("scaffold_write_refused", conflicts=len(conflicts))
        raise ScaffoldConflictError(conflicts)

    for path, content in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    gitignore_updated = False
    pyproject_updated = False
    if mode == "add_to_suite":
        assert existing is not None  # narrows for the type checker; mode implies it
        gitignore_updated = ensure_ignored(target_dir, CAPTURES_DIR)
        module = module_name_for(resolved_spec.name)
        package = f"src/graftpunk_{module}"
        target = f"graftpunk_{module}.plugin:{class_name_for(resolved_spec.name)}"
        add_entry_point(existing, resolved_spec.name, target)
        add_wheel_package(existing, package)
        pyproject_updated = True

    LOG.info("scaffold_written", mode=mode, target_dir=str(target_dir), files=len(targets))
    return ScaffoldResult(
        mode=mode,
        written=tuple(sorted(targets)),
        gitignore_updated=gitignore_updated,
        pyproject_updated=pyproject_updated,
    )
