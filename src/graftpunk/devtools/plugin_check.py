"""``gp plugin check``: a lint over the project reader's view. It never edits.

The one owner of the complete list of finding kinds (every other docstring in
this module or ``plugin_project.py`` that names a reason points back here
instead of re-enumerating). This module reports: a remaining ``GP-FILL``
marker in a plugin module or a test module; a module without exactly one
``SitePlugin`` subclass (the reader's per-plugin defect, listed with the
other findings); a requirement's file that does not parse, is not valid
UTF-8, cannot be read, or is not a regular file (or whose directory is
blocked); a requirement the project lacks, which ``gp plugin upgrade``
fixes (in a suite, each member's own in each of its test modules, fixed by
``gp plugin upgrade --plugin <name>``, and the shared conftest's check over the
first plugin's fixtures); in a suite, a conftest check that still walks every
member's directory, or a literal ``SUITE_MEMBER_FIXTURES`` that does not list a
member, each fixed by ``gp plugin upgrade``, or a ``SUITE_MEMBER_FIXTURES`` that
is not a literal tuple, which an author rewrites as one; a ``policy.FIXTURES_TREE``, or in a
suite a member's own fixtures root, that is missing (which
``gp plugin upgrade``, with ``--plugin <name>`` for a member's, creates), is
not a directory (an author must move it aside), or cannot be read (an author
must fix its permissions), including when ``tests/`` itself, not the tree, is
the one blocked; a plugin's ``aliases`` that is not a literal tuple of strings,
and each alias that is not a valid plugin name, is listed twice, is the plugin's
own ``site_name``, names a core ``gp`` command or another of the project's
plugins, or is also another of its plugins' alias (#374); and,
when the reader refuses the project outright (for example, not a plugin
project, or a ``pyproject.toml`` or plugin module that cannot be read or
does not parse), that refusal as the only finding. It does not compare a
declared endpoint against the request call: the declaration is authoritative
by design, and a check that could only ever be weak would give an author a
reason to drop the keyword. It does not restate the fixtures check, which the
generated suite runs (graft skill spec, 2026-09-21). A reader: never imports
``write.py``.

``FINDING_ADVICE`` is every advice phrase a message built here can carry, each
drawn from the constant the message itself is built from (never re-typed), so
a test pinning the plugin-development guide's prose against this tuple fails
when a phrase here has no match in the guide, instead of the two drifting
silently.
"""

from __future__ import annotations

from collections.abc import Sequence
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
    ProjectView,
    require_plugin_project,
    unreadable_file_message,
)
from graftpunk.devtools.scaffold.policy import (
    CONFTEST_PATH,
    FIXTURES_TREE,
    GP_FILL_MARKER,
    SUITE_SKIP_NAME,
    ProjectRequirement,
    module_name_for,
)
from graftpunk.devtools.scaffold.render import validate_plugin_name
from graftpunk.devtools.scaffold.suite_conftest import SuiteConftestState
from graftpunk.plugins import infer_site_name
from graftpunk.plugins.names import plugin_names

__all__ = ["FINDING_ADVICE", "Finding", "check_project", "suite_tuple_problems"]

# The two messages built entirely in this module, each holding its own advice
# phrase; unreadable_file_message's phrases live in plugin_project.py, next to
# the reasons it builds its advice from.
_CREATES_IT = "creates it"
_ADDS_IT = "adds it"
_FIXTURES_TREE_MISSING = f"missing; gp plugin upgrade {_CREATES_IT}."
_SCOPES_IT = "scopes it"
_WRITE_IT_LITERAL = "write it as a literal tuple of directory names"
_DOES_NOT_BIND = "does not bind"
_RENAME_OR_REMOVE = "rename or remove the alias"
_ALIASES_NOT_LITERAL = (
    "aliases is not a literal tuple of strings, so gp plugin check cannot check it; "
    "write it as one, such as ('shop',)."
)


def _upgrade_advice(requirement: ProjectRequirement) -> str:
    """The advice for a missing *requirement*: the upgrade that adds it and, in a
    suite, nothing another plugin lacks (#370)."""
    scope = f" --plugin {requirement.plugin}" if requirement.plugin is not None else ""
    return f"gp plugin upgrade{scope} {_ADDS_IT}."


def _left_to_the_author(view: ProjectView, requirement: ProjectRequirement) -> bool:
    """Whether *requirement* is a suite conftest's ``SUITE_MEMBER_FIXTURES`` that is
    not wanted: the check is gp's standalone one (its own finding says the upgrade
    adds both) or one the author wrote without the tuple."""
    state = view.suite_conftest
    return (
        state is not None
        and requirement.path == CONFTEST_PATH
        and requirement.name == SUITE_SKIP_NAME
        and (state.unscoped or not state.needs_skip)
    )


@dataclass(frozen=True)
class _AliasedView:
    """A plugin as :func:`graftpunk.plugins.names.plugin_names` reads it: the name
    the runtime would give it and the aliases the check has not already refused."""

    site_name: str
    session_name: str
    aliases: tuple[str, ...]
    module_path: str
    line: int | None


def _alias_findings(view: ProjectView) -> list[Finding]:
    """Each alias the CLI would ignore, through the resolver the CLI registers
    with, and each one ``gp plugin new`` would refuse as a plugin name."""
    findings: list[Finding] = []
    named: list[_AliasedView] = []
    for plugin in view.plugins:
        site_name = plugin.site_name or infer_site_name(plugin.base_url or "") or plugin.entry_point
        session_name = plugin.session_name or site_name
        where = (plugin.module_path, plugin.aliases_line)
        if plugin.aliases is None:
            findings.append(Finding(*where, _ALIASES_NOT_LITERAL))
            # Its own names still hold against every other plugin's aliases.
            named.append(_AliasedView(site_name, session_name, (), *where))
            continue
        kept: list[str] = []
        for alias in plugin.aliases:
            try:
                validate_plugin_name(alias)
            except ValueError as exc:
                message = f"alias '{alias}': {exc}; {_RENAME_OR_REMOVE}."
                findings.append(Finding(plugin.module_path, plugin.aliases_line, message))
                continue
            kept.append(alias)
        named.append(_AliasedView(site_name, session_name, tuple(kept), *where))
    owners = {n.site_name: n for n in named}
    for conflict in plugin_names(named).conflicts:
        owner = owners[conflict.site_name]
        message = f"alias '{conflict.alias}' {conflict.reason}; {_RENAME_OR_REMOVE}."
        findings.append(Finding(owner.module_path, owner.line, message))
    return findings


def suite_tuple_problems(state: SuiteConftestState, member_modules: Sequence[str]) -> list[str]:
    """What keeps gp from listing *member_modules* in a suite conftest's
    ``SUITE_MEMBER_FIXTURES``, one message each: a tuple that is not a literal (a
    tuple built some other way can skip less than every member), and each literal
    entry ``skip`` refuses (the conftest's import fails on it). ``gp plugin check``
    reports them, and ``gp plugin new`` and ``gp plugin upgrade`` print them when
    they leave such a tuple as it is (#370)."""
    if not state.skip_bound or not state.needs_skip or state.unscoped:
        return []
    example = ", ".join(f'"{module}"' for module in member_modules)
    example += "," if len(member_modules) == 1 else ""
    fix = f"{_WRITE_IT_LITERAL}, such as ({example})."
    if state.refused:
        return [
            f"{SUITE_SKIP_NAME} holds {entry}, which skip refuses, since it is not one "
            f"directory name; {fix}"
            for entry in state.refused
        ]
    if state.listed is None:
        return [f"{SUITE_SKIP_NAME} is not a literal tuple, so gp cannot list members in it; {fix}"]
    return []


def _suite_conftest_findings(view: ProjectView) -> list[Finding]:
    """A suite conftest's check that still walks every member's directory, a
    ``SUITE_MEMBER_FIXTURES`` the check reads that is not a literal tuple, and each
    member a literal one does not list (#370)."""
    state = view.suite_conftest
    if state is None:
        return []
    owner = next((r.plugin for r in view.requirement_set if r.path == CONFTEST_PATH), None)
    findings: list[Finding] = []
    if state.unscoped:
        message = (
            "sanitised_fixtures also checks every member's fixtures; gp plugin upgrade "
            f"--plugin {owner} {_SCOPES_IT} to {owner}'s with skip={SUITE_SKIP_NAME}."
        )
        findings.append(Finding(path=CONFTEST_PATH, line=None, message=message))
    modules = [module_name_for(m.entry_point) for m in view.member_roots]
    findings.extend(
        Finding(path=CONFTEST_PATH, line=None, message=message)
        for message in suite_tuple_problems(state, modules)
    )
    if state.listed is not None:
        for member in view.member_roots:
            module = module_name_for(member.entry_point)
            if module not in state.listed:
                message = (
                    f"{SUITE_SKIP_NAME} does not list {module}; gp plugin upgrade "
                    f"--plugin {member.entry_point} {_ADDS_IT}."
                )
                findings.append(Finding(path=CONFTEST_PATH, line=None, message=message))
    return findings


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
    _SCOPES_IT,
    _WRITE_IT_LITERAL,
    _RENAME_OR_REMOVE,
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
    refusal from the reader (for example, not a plugin project, or not
    readable at all) is the one finding. The same blocked ancestor can surface through both the
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
    for member in view.member_roots:
        if member.blocked is not None:
            path, reason = member.blocked
            findings.append(Finding(path=path, line=None, message=unreadable_file_message(reason)))
        elif not member.present:
            message = f"missing; gp plugin upgrade --plugin {member.entry_point} {_CREATES_IT}."
            findings.append(Finding(path=member.path, line=None, message=message))
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
            message=f"{_DOES_NOT_BIND} {requirement.name}; {_upgrade_advice(requirement)}",
        )
        for requirement in view.missing_requirements()
        if not _left_to_the_author(view, requirement)
    )
    findings.extend(_suite_conftest_findings(view))
    findings.extend(_alias_findings(view))
    return list(dict.fromkeys(findings))
