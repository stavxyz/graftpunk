"""The one reader of a plugin project: what a directory holds, read once.

Resolves a directory to its plugin project (``pyproject.toml``, the
``graftpunk.plugins`` entry-point table, each entry point's module), classifies
it in its own terms (``empty``, ``plugin``, ``foreign``), and reads every plugin
module with ``ast``, never importing it, so a plugin whose code does not run
still gets an answer. :func:`read_project` returns one structural view that
``gp plugin info``, the stub inserter, the migrator, and ``gp plugin check`` all
work from; none of them parses a project on its own. The view grows here when a
consumer needs more, but a need for body-level facts beyond marker locations is
the point to reconsider it rather than extend it (graft skill spec, 2026-09-21).

A reader: it imports neither ``graftpunk.devtools.scaffold.write`` nor
``graftpunk.devtools.scaffold.render``, and knows no output format (the
``gp plugin info`` payload is built in ``graftpunk.devtools.plugin_info``).
"""

from __future__ import annotations

import ast
import re
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from graftpunk.devtools.errors import DevtoolsRefusal
from graftpunk.devtools.scaffold import policy
from graftpunk.devtools.scaffold.policy import GP_FILL_MARKER, ProjectRequirement, module_name_for
from graftpunk.devtools.scaffold.pysrc import binds_name
from graftpunk.har.naming import registered_name
from graftpunk.plugins import PLUGINS_GROUP

__all__ = [
    "CommandView",
    "DirectoryKind",
    "NotAPluginProjectError",
    "PluginDefect",
    "PluginProjectError",
    "PluginView",
    "ProjectView",
    "RequirementState",
    "RequirementStatus",
    "Span",
    "classify",
    "read_project",
    "require_plugin_project",
]

DirectoryKind = Literal["empty", "plugin", "foreign"]
RequirementState = Literal["bound", "unbound", "unreadable"]

_PLUGIN_BASE = "SitePlugin"
_COMMAND_DECORATOR = "command"
# Where an entry point's module may live, relative to the project root: the src
# layout gp plugin new writes, then a flat layout.
_SOURCE_ROOTS = ("src", "")


@dataclass(frozen=True)
class Span:
    """A 1-based, inclusive line range in a module."""

    start: int
    end: int


@dataclass(frozen=True)
class CommandView:
    """One ``@command``-decorated method, or nested class (a command group): its
    name (``method`` holds the group's class name for a group), its span (first
    decorator through the end of its body), and its decorator's keywords,
    read-only, each the string literal it was given or ``None`` when it is not
    one. A group's ``endpoint`` is always ``None``: the ``command`` decorator
    only applies ``endpoint=`` to a function."""

    method: str
    span: Span
    keywords: Mapping[str, str | None]

    @property
    def endpoint(self) -> str | None:
        """The declared ``endpoint=`` literal, or ``None`` for a command that has none."""
        return self.keywords.get("endpoint")

    @property
    def cli_name(self) -> str:
        """The name the CLI registers, by the rule the ``command`` decorator applies."""
        return registered_name(self.keywords.get("name"), self.method)


@dataclass(frozen=True)
class PluginView:
    """One entry point's plugin, as its module reads. ``entry_point`` is the name of
    its line in the ``graftpunk.plugins`` table: the one identity every consumer
    addresses a plugin by, which ``site_name`` need not match.

    ``commands`` holds every ``@command``-decorated top-level member: a method, and
    also a nested class (a command group), whose entry carries the group's
    registered name and an ``endpoint`` of ``None`` (a group never declares one).
    ``class_names`` holds every name the class body binds at its top level, decorated
    or not: a method, a nested class (including a command group), or an assignment
    target. The stub inserter's collision check reads ``class_names``, not just
    ``commands``, so a plain helper or attribute is refused too."""

    entry_point: str
    module_path: str  # project-relative, forward slashes
    class_name: str
    class_span: Span
    site_name: str | None
    base_url: str | None
    commands: tuple[CommandView, ...]
    markers: tuple[int, ...]  # the line of every GP-FILL marker in the module
    fixtures_root: str  # project-relative, trailing slash
    class_names: frozenset[str]


@dataclass(frozen=True)
class PluginDefect:
    """An entry point whose module parsed but does not hold exactly one ``SitePlugin``
    subclass. Recorded rather than raised, so the rest of the project still reads
    and each consumer decides whether the defect stops it."""

    entry_point: str
    module_path: str  # project-relative, forward slashes
    message: str


@dataclass(frozen=True)
class RequirementStatus:
    """Whether a project's file binds one ``PROJECT_REQUIREMENTS`` name: ``bound``;
    ``unbound`` (the file is missing, or does not bind it); or ``unreadable`` (the
    file does not parse), with ``reason`` saying why."""

    state: RequirementState
    reason: str | None = None


@dataclass(frozen=True)
class ProjectView:
    """A directory's plugin project: its classification, one view per readable entry
    point, one defect per entry point whose module is structurally wrong, and the
    status of each ``PROJECT_REQUIREMENTS`` name in its files (read-only, keyed by
    the requirement's ``key``).

    ``requirements`` and ``requirement_set`` are both computed when the project is
    read, from ``policy.PROJECT_REQUIREMENTS`` as it stood then; :meth:`missing_requirements`
    and :meth:`unreadable_files` read ``self.requirement_set``, not the module
    attribute, so both methods are pure functions of the view's own fields, not of
    whatever ``policy.PROJECT_REQUIREMENTS`` holds at call time (design note below).
    They treat a requirement the view has no status for as unbound. A view is
    short-lived: read it, act on it, and read the project again rather than keep
    one.

    Design note (2026-09-24): ``missing_requirements`` and ``unreadable_files``
    originally read ``policy.PROJECT_REQUIREMENTS`` live, at call time, rather than
    from the view's own fields; two calls on the same ``ProjectView`` could then
    disagree if a test or a caller mutated ``policy.PROJECT_REQUIREMENTS`` between
    them, even though nothing else about the view changed. ``ProjectView`` carries
    a ``requirement_set`` field, snapshotted from ``policy.PROJECT_REQUIREMENTS`` by
    ``read_project`` at read time, and both methods read ``self.requirement_set``
    instead, so a ``ProjectView`` is a value: two calls on the same instance always
    agree, and equality compares the same fields the methods read.
    """

    directory: DirectoryKind
    plugins: tuple[PluginView, ...]
    defects: tuple[PluginDefect, ...]
    requirements: Mapping[str, RequirementStatus]
    requirement_set: tuple[ProjectRequirement, ...]

    def plugin(self, entry_point: str) -> PluginView | PluginDefect | None:
        """The plugin or defect whose entry-point name is *entry_point*, or ``None``:
        the one owner of "which plugin does this name address". The reader keys
        both tuples by entry point and never records a name twice, so at most one
        matches."""
        for found in (*self.plugins, *self.defects):
            if found.entry_point == entry_point:
                return found
        return None

    def _state(self, requirement: ProjectRequirement) -> RequirementState:
        status = self.requirements.get(requirement.key)
        return "unbound" if status is None else status.state

    def missing_requirements(self) -> tuple[ProjectRequirement, ...]:
        """The ``requirement_set`` entries this project's files do not bind, in
        declared order: the one answer ``gp plugin upgrade`` applies and
        ``gp plugin check`` reports. An entry whose file does not parse is not
        here; its file is in :meth:`unreadable_files`. Empty for a directory that
        is not a plugin project, which has no requirements to lack."""
        if self.directory != "plugin":
            return ()
        return tuple(r for r in self.requirement_set if self._state(r) == "unbound")

    def unreadable_files(self) -> tuple[tuple[str, str], ...]:
        """Each requirement file that does not parse, once, as ``(path, reason)``, in
        the order ``requirement_set`` first names it, however many requirements
        it holds: ``gp plugin upgrade`` refuses on them and ``gp plugin check``
        reports one finding each, while ``gp plugin info`` and
        ``gp plugin add-command``, which never read those files, proceed."""
        if self.directory != "plugin":
            return ()
        files: dict[str, str] = {}
        for r in self.requirement_set:
            status = self.requirements.get(r.key)
            if status is not None and status.state == "unreadable":
                files.setdefault(r.path, status.reason or "does not parse")
        return tuple(files.items())


class PluginProjectError(DevtoolsRefusal, ValueError):
    """The project cannot be read at all: the message names the file and the reason.
    A plugin module that parses but holds the wrong number of plugin classes is a
    :class:`PluginDefect` in the view, and a requirement's file that does not parse
    is an ``unreadable`` :class:`RequirementStatus`, not this."""


class NotAPluginProjectError(DevtoolsRefusal, ValueError):
    """An operation that needs a plugin project was pointed at a directory that is
    not one; the message names the directory and its classification."""


def _load_pyproject(root: Path) -> dict[str, Any] | None:
    path = root / "pyproject.toml"
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise PluginProjectError(f"{path}: not valid UTF-8 ({exc})") from exc
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PluginProjectError(f"{path}: not valid TOML ({exc})") from exc


def _table(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PluginProjectError(
            f"pyproject.toml: [{where}] must be a table, not {type(value).__name__}."
        )
    return value


def _entry_points(data: dict[str, Any]) -> dict[str, str] | None:
    project = _table(data.get("project", {}), "project")
    entry_points = _table(project.get("entry-points", {}), "project.entry-points")
    group = entry_points.get(PLUGINS_GROUP)
    if group is None:
        return None
    group = _table(group, f'project.entry-points."{PLUGINS_GROUP}"')
    result: dict[str, str] = {}
    for key, value in group.items():
        if not isinstance(value, str):
            raise PluginProjectError(
                f"pyproject.toml: entry point {key!r} must be a string, not "
                f"{type(value).__name__}."
            )
        result[str(key)] = value
    return result


def classify(root: Path) -> DirectoryKind:
    """*root* in its own terms: no ``pyproject.toml`` is ``empty``; one declaring the
    ``graftpunk.plugins`` entry-point group is ``plugin``; any other is ``foreign``."""
    data = _load_pyproject(root)
    if data is None:
        return "empty"
    return "plugin" if _entry_points(data) is not None else "foreign"


def read_project(root: Path) -> ProjectView:
    """Read *root* into its structural view.

    A module that parses but does not hold exactly one ``SitePlugin`` subclass is
    recorded in ``defects`` and read no further; every other plugin still reads.
    A requirement's file that does not parse is recorded as ``unreadable``; each
    distinct requirement file is parsed once.

    Raises:
        PluginProjectError: The project cannot be read at all: ``pyproject.toml``
            is not valid TOML, or an entry point's module is missing or does not
            parse.
    """
    data = _load_pyproject(root)
    if data is None:
        return ProjectView(
            directory="empty",
            plugins=(),
            defects=(),
            requirements=MappingProxyType({}),
            requirement_set=policy.PROJECT_REQUIREMENTS,
        )
    entry_points = _entry_points(data)
    if entry_points is None:
        return ProjectView(
            directory="foreign",
            plugins=(),
            defects=(),
            requirements=MappingProxyType({}),
            requirement_set=policy.PROJECT_REQUIREMENTS,
        )
    project_name = str(data.get("project", {}).get("name", ""))
    read = [_read_plugin(root, key, value, project_name) for key, value in entry_points.items()]
    requirement_set = policy.PROJECT_REQUIREMENTS
    requirements = _requirement_statuses(root, requirement_set)
    return ProjectView(
        directory="plugin",
        plugins=tuple(r for r in read if isinstance(r, PluginView)),
        defects=tuple(r for r in read if isinstance(r, PluginDefect)),
        requirements=MappingProxyType(requirements),
        requirement_set=requirement_set,
    )


def require_plugin_project(root: Path) -> ProjectView:
    """*root*'s view, when *root* is a plugin project: the one owner of "this
    directory must be a plugin project", which the stub inserter, the migrator,
    and the lint all ask.

    Raises:
        NotAPluginProjectError: *root* is ``empty`` or ``foreign``.
        PluginProjectError: See :func:`read_project`.
    """
    view = read_project(root)
    if view.directory != "plugin":
        raise NotAPluginProjectError(
            f"{root} is not a graftpunk plugin project ({view.directory})."
        )
    return view


def _normalised(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def _module_file(root: Path, module: str) -> str | None:
    relative = module.replace(".", "/")
    for base in _SOURCE_ROOTS:
        for candidate in (f"{relative}.py", f"{relative}/__init__.py"):
            path = Path(base) / candidate
            if (root / path).is_file():
                return path.as_posix()
    return None


def _parse_error_message(exc: SyntaxError | ValueError) -> str:
    """The "does not parse (...)" refusal text, with a line clause only when the
    exception carries one: a NUL byte in the source raises ``ValueError`` on
    Python 3.11 (no ``lineno``) and ``SyntaxError`` with ``lineno=None`` on later
    versions."""
    msg = getattr(exc, "msg", None) or str(exc)
    lineno = getattr(exc, "lineno", None)
    clause = f", line {lineno}" if lineno is not None else ""
    return f"does not parse ({msg}{clause})"


def _parse(root: Path, relative: str) -> tuple[str, ast.Module]:
    path = root / relative
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise PluginProjectError(f"{relative}: not valid UTF-8 ({exc})") from exc
    try:
        return text, ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        raise PluginProjectError(f"{relative}: {_parse_error_message(exc)}") from exc


def _last_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _read_plugin(root: Path, key: str, value: str, project_name: str) -> PluginView | PluginDefect:
    module = value.partition(":")[0]
    relative = _module_file(root, module)
    if relative is None:
        path = module.replace(".", "/") + ".py"
        raise PluginProjectError(
            f"entry point {key!r} names {value!r}, but neither src/{path} nor {path} exists."
        )
    text, tree = _parse(root, relative)
    classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and any(_last_name(b) == _PLUGIN_BASE for b in node.bases)
    ]
    if len(classes) != 1:
        found = ", ".join(c.name for c in classes) or "none"
        return PluginDefect(
            entry_point=key,
            module_path=relative,
            message=(
                f"{relative}: expected exactly one SitePlugin subclass, found {len(classes)} "
                f"({found}). A plugin module holds one plugin class."
            ),
        )
    (klass,) = classes
    package = module.split(".")[0]
    start = min([klass.lineno, *(d.lineno for d in klass.decorator_list)])
    return PluginView(
        entry_point=key,
        module_path=relative,
        class_name=klass.name,
        class_span=Span(start, klass.end_lineno or klass.lineno),
        site_name=_class_string(klass, "site_name"),
        base_url=_class_string(klass, "base_url"),
        commands=tuple(_commands(klass)),
        markers=tuple(
            number
            for number, line in enumerate(text.splitlines(), start=1)
            if GP_FILL_MARKER in line
        ),
        fixtures_root=policy.fixtures_root(
            suite_member=_normalised(project_name) != _normalised(package),
            module_name=module_name_for(key),
        ),
        class_names=_class_body_names(klass),
    )


# Design note (2026-09-24): whether a plugin is a suite member is decided
# twice, by two different facts. The renderer knows it directly:
# ScaffoldSpec.mode is "add_to_suite" or "new_project", set by whoever calls
# it. The reader has no such field to read back; mode is a choice the writer
# made at generation time, not a fact gp plugin new or gp plugin add-command
# persists anywhere on disk (no project file records it), so _read_plugin
# above re-derives the same answer from the one fact that IS on disk and
# matches the generator's own decision: a suite member's package name differs
# from the project's [project].name.
# test_the_rule_gives_the_same_answer_from_the_spec_and_from_the_project is
# the check that these two independent derivations still agree; it is the
# owner of that agreement, not a coincidence to be relied on silently.
# Recording mode as an on-disk marker would remove the need for the
# heuristic, but that is a new generated-project file format, outside this
# plan's scope; the parity test is the cheaper fix for the actual risk (the
# two derivations drifting apart), and this plan keeps it.


def _class_string(klass: ast.ClassDef, name: str) -> str | None:
    """The string literal *klass*'s body assigns to *name*, or ``None``."""
    for node in klass.body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets, value = [node.target.id], node.value
        else:
            continue
        if name in targets and isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return None


def _commands(klass: ast.ClassDef) -> Iterator[CommandView]:
    """Every ``@command``-decorated top-level member: a method, and also a nested
    class (a command group, which the ``command`` decorator also accepts)."""
    for node in klass.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        decorator = next(
            (
                d
                for d in node.decorator_list
                if _last_name(d.func if isinstance(d, ast.Call) else d) == _COMMAND_DECORATOR
            ),
            None,
        )
        if decorator is None:
            continue
        keywords: dict[str, str | None] = {}
        if isinstance(decorator, ast.Call):
            for keyword in decorator.keywords:
                if keyword.arg is None:
                    continue
                value = keyword.value
                keywords[keyword.arg] = (
                    value.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str)
                    else None
                )
        start = min(d.lineno for d in node.decorator_list)
        yield CommandView(
            node.name, Span(start, node.end_lineno or node.lineno), MappingProxyType(keywords)
        )


def _class_body_names(klass: ast.ClassDef) -> frozenset[str]:
    """Every name the class body binds at its top level: a method (decorated or
    not), a nested class (including a command group), or an assignment target.
    ``PluginView.class_names``: the stub inserter's collision check reads this,
    not just the decorated commands, so a plain helper method or attribute is
    refused too."""
    names: set[str] = set()
    for node in klass.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return frozenset(names)


def _requirement_statuses(
    root: Path, requirement_set: tuple[ProjectRequirement, ...]
) -> dict[str, RequirementStatus]:
    """Each entry in *requirement_set*'s status, keyed by its ``key``: whether its
    file binds its name, by the one predicate, ``pysrc.binds_name``. Each distinct
    file is parsed once. A file that does not parse is ``unreadable`` for every entry
    it holds, with its one reason, rather than raised: one broken conftest must not
    blind the consumers that never read it. The caller reads ``policy.PROJECT_REQUIREMENTS``
    once and passes the snapshot in, so it cannot drift from the view's own
    ``requirement_set`` within one ``read_project`` call."""
    paths = dict.fromkeys(r.path for r in requirement_set)
    parsed = {relative: _parse_requirement_file(root / relative) for relative in paths}
    statuses: dict[str, RequirementStatus] = {}
    for r in requirement_set:
        tree = parsed[r.path]
        if isinstance(tree, RequirementStatus):
            statuses[r.key] = tree
        else:
            statuses[r.key] = RequirementStatus("bound" if binds_name(tree, r.name) else "unbound")
    return statuses


def _parse_requirement_file(path: Path) -> ast.Module | RequirementStatus:
    """*path*'s tree, or the status every requirement in it takes when there is no
    tree: ``unbound`` for a missing file, ``unreadable`` with the reason for one that
    does not parse."""
    if not path.is_file():
        return RequirementStatus("unbound")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return RequirementStatus("unreadable", f"not valid UTF-8 ({exc})")
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        return RequirementStatus("unreadable", _parse_error_message(exc))
