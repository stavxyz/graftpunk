"""``gp plugin check``: a lint over the project reader's view. It never edits.

The one owner of the complete list of finding kinds (every other docstring in
this module or ``plugin_project.py`` that names a reason points back here
instead of re-enumerating). This module reports: a remaining ``GP-FILL``
marker in a plugin module or a test module; a module without exactly one
``SitePlugin`` subclass (the reader's per-plugin defect, listed with the
other findings); a requirement's file that does not parse, is not valid
UTF-8, cannot be read, or is not a regular file (or whose directory is
blocked); a ``PROJECT_REQUIREMENTS`` entry the project lacks, which
``gp plugin upgrade`` fixes; a ``policy.FIXTURES_TREE`` that is missing
(which ``gp plugin upgrade`` creates), is not a directory (an author must
move it aside), or cannot be read (an author must fix its permissions),
including when ``tests/`` itself, not the tree, is the one blocked; and,
when the reader refuses the project outright (not a plugin project, or a
``pyproject.toml`` or plugin module that cannot be read or does not parse),
that refusal as the only finding. It does not compare a declared endpoint
against the request call: the declaration is authoritative by design, and a
check that could only ever be weak would give an author a reason to drop the
keyword. It does not restate the fixtures check, which the generated suite
runs (graft skill spec, 2026-09-21). A reader: never imports ``write.py``.

``FINDING_ADVICE`` is every advice phrase a message built here can carry, each
drawn from the constant the message itself is built from (never re-typed), so
a test pinning the plugin-development guide's prose against this tuple fails
when a phrase here has no match in the guide, instead of the two drifting
silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from graftpunk.devtools.plugin_project import (
    CANNOT_BE_READ,
    DOES_NOT_PARSE,
    EXACTLY_ONE,
    NOT_A_DIRECTORY_PHRASE,
    NOT_A_REGULAR_FILE_PHRASE,
    NOT_VALID_UTF8,
    NotAPluginProjectError,
    PluginProjectError,
    require_plugin_project,
    unreadable_file_message,
)
from graftpunk.devtools.scaffold.policy import FIXTURES_TREE, GP_FILL_MARKER

__all__ = ["FINDING_ADVICE", "Finding", "check_project"]

# The two messages built entirely in this module, each holding its own advice
# phrase; unreadable_file_message's phrases live in plugin_project.py, next to
# the reasons it builds its advice from.
_CREATES_IT = "creates it"
_ADDS_IT = "adds it"
_FIXTURES_TREE_MISSING = f"missing; gp plugin upgrade {_CREATES_IT}."
_DOES_NOT_BIND = "does not bind"
_MISSING_REQUIREMENT_TAIL = f"gp plugin upgrade {_ADDS_IT}."

FINDING_ADVICE: tuple[str, ...] = (
    NOT_A_DIRECTORY_PHRASE,
    NOT_A_REGULAR_FILE_PHRASE,
    CANNOT_BE_READ,
    NOT_VALID_UTF8,
    DOES_NOT_PARSE,
    EXACTLY_ONE,
    GP_FILL_MARKER,
    _CREATES_IT,
    _ADDS_IT,
)
"""Every advice phrase a ``check_project`` finding can carry, for a test to
check the guide's prose against: not a guarantee that a future reason is
caught here automatically, since a new reason still has to be added to this
tuple by hand."""


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
    """Every finding in *root*'s plugin project, in this order: a missing or
    blocked fixtures tree, then markers in file order, then plugin defects,
    then unreadable requirement files, then missing requirements (this
    module's own docstring has the complete list of finding kinds). A
    refusal from the reader (not a plugin project, or not readable at all) is
    the one finding. The same blocked ancestor can surface through both the
    fixtures tree and a requirement file (both live under ``tests/``);
    findings are deduplicated so it is reported once."""
    try:
        view = require_plugin_project(root)
    except (NotAPluginProjectError, PluginProjectError) as exc:
        return [Finding(path=None, line=None, message=str(exc))]
    findings: list[Finding] = []
    if view.fixtures_tree_blocked is not None:
        path, reason = view.fixtures_tree_blocked
        findings.append(Finding(path=path, line=None, message=unreadable_file_message(reason)))
    elif not view.fixtures_tree_present:
        findings.append(Finding(path=FIXTURES_TREE, line=None, message=_FIXTURES_TREE_MISSING))
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
            message=f"{_DOES_NOT_BIND} {requirement.name}; {_MISSING_REQUIREMENT_TAIL}",
        )
        for requirement in view.missing_requirements()
    )
    return list(dict.fromkeys(findings))
