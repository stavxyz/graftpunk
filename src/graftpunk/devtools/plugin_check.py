"""``gp plugin check``: a lint over the project reader's view. It never edits.

Reports a missing or blocked ``policy.FIXTURES_TREE`` (the first case
``gp plugin upgrade`` creates, the second an author must move aside), a
remaining ``GP-FILL`` marker in a plugin module or a test module, a module
without exactly one ``SitePlugin`` subclass (the reader's per-plugin defect,
listed with the other findings), a requirement's file that does not parse or
is not a regular file, and a ``PROJECT_REQUIREMENTS`` entry the project lacks,
which ``gp plugin upgrade`` fixes. It does not compare a declared endpoint against
the request call: the declaration is authoritative by design, and a check
that could only ever be weak would give an author a reason to drop the
keyword. It does not restate the fixtures check, which the generated suite
runs (graft skill spec, 2026-09-21). A reader: never imports ``write.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from graftpunk.devtools.plugin_project import (
    NOT_A_DIRECTORY,
    NotAPluginProjectError,
    PluginProjectError,
    require_plugin_project,
    unreadable_file_message,
)
from graftpunk.devtools.scaffold.policy import FIXTURES_TREE, GP_FILL_MARKER

__all__ = ["Finding", "check_project"]


@dataclass(frozen=True)
class Finding:
    """One thing the lint found: the message alone when the message already
    names its own path or names none (a reader refusal, a plugin defect), or
    at a project-relative path and, when it has one, a line."""

    path: str | None
    line: int | None
    message: str

    def __str__(self) -> str:
        if self.path is None:
            return self.message
        where = f"{self.path}:{self.line}" if self.line is not None else self.path
        return f"{where}: {self.message}"


def check_project(root: Path) -> list[Finding]:
    """Every finding in *root*'s plugin project: a missing or blocked fixtures
    tree, then markers in file order, then plugin defects, then requirement
    files that do not parse or are not regular files, then missing
    requirements. A refusal from the reader (not a plugin project, or not
    readable at all) is the one finding. The same blocked ancestor can surface
    through both the fixtures tree and a requirement file (both live under
    ``tests/``); findings are deduplicated so it is reported once."""
    try:
        view = require_plugin_project(root)
    except (NotAPluginProjectError, PluginProjectError) as exc:
        return [Finding(path=None, line=None, message=str(exc))]
    findings: list[Finding] = []
    if view.fixtures_tree_blocked is not None:
        findings.append(
            Finding(path=view.fixtures_tree_blocked, line=None, message=NOT_A_DIRECTORY)
        )
    elif not view.fixtures_tree_present:
        findings.append(
            Finding(path=FIXTURES_TREE, line=None, message="missing; gp plugin upgrade creates it.")
        )
    findings.extend(
        Finding(
            path=plugin.module_path, line=line, message=f"{GP_FILL_MARKER} marker left to fill in."
        )
        for plugin in view.plugins
        for line in plugin.markers
    )
    findings.extend(
        Finding(path=path, line=line, message=f"{GP_FILL_MARKER} marker left to fill in.")
        for path, line in view.test_markers
    )
    findings.extend(
        # defect.message already starts with "{module_path}: " (PluginDefect's
        # own contract); a path here too would print it twice.
        Finding(path=None, line=None, message=defect.message)
        for defect in view.defects
    )
    findings.extend(
        Finding(path=path, line=None, message=unreadable_file_message(reason))
        for path, reason in view.unreadable_files()
    )
    findings.extend(
        Finding(
            path=requirement.path,
            line=None,
            message=f"does not bind {requirement.name}; gp plugin upgrade adds it.",
        )
        for requirement in view.missing_requirements()
    )
    return list(dict.fromkeys(findings))
