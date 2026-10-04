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
from graftpunk.devtools.scaffold.pysrc import binds_name, source_lines
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
    only applies ``endpoint=`` to a function. ``group`` is ``True`` for a
    decorated nested class and ``False`` for a decorated method, set from the
    AST node type, never from whether ``endpoint`` is set: a hand-written
    method can carry no ``endpoint=`` and still be a command, not a group."""

    method: str
    span: Span
    keywords: Mapping[str, str | None]
    group: bool = False

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
    ``class_names`` holds every name the class body binds, as ``_class_body_names``
    defines it: a method, a nested class, an assignment, an annotated assignment,
    or an unpacking target, an import, a ``for``/``with`` target, and any of these
    nested under an ``if`` or ``try``. The stub inserter's collision check reads
    ``class_names``, not just ``commands``, so a plain helper, attribute, or
    import is refused too."""

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

    ``test_markers`` holds every ``GP-FILL`` marker line in a ``*.py`` file under
    ``policy.TESTS_DIR`` (project-relative path, 1-based line), the same fact
    ``PluginView.markers`` holds for a plugin module; ``gp plugin check`` reports
    both.

    ``fixtures_tree_present`` is whether ``root / policy.FIXTURES_TREE`` is a
    directory, read once here so ``gp plugin check`` and ``gp plugin upgrade``
    take the same answer from the same place and cannot disagree.

    ``first_party_packages`` is read once here, by the same rule ruff's own
    default (``src = [".", "src"]``) uses to decide a module is first party:
    every top-level directory and ``*.py`` stem directly under the project
    root and under ``src/``, plus any name the project's own
    ``[tool.ruff.lint.isort] known-first-party`` (or the legacy
    ``[tool.ruff.isort]``) declares. ``pysrc.with_import``'s ``first_party=``
    argument, so ``add_command`` and ``gp plugin upgrade`` place a new import
    into isort's first-party section exactly when ruff itself would sort it
    there, whether the plugin lives in a package directory or is a single
    module file.

    Design note: ``requirement_set`` is snapshotted from ``policy.PROJECT_REQUIREMENTS``
    by ``read_project`` at read time, and both methods read ``self.requirement_set``
    instead of the module attribute, so a ``ProjectView`` is a value: two calls on
    the same instance always agree, and equality compares the same fields the
    methods read.
    """

    directory: DirectoryKind
    plugins: tuple[PluginView, ...]
    defects: tuple[PluginDefect, ...]
    requirements: Mapping[str, RequirementStatus]
    requirement_set: tuple[ProjectRequirement, ...]
    test_markers: tuple[tuple[str, int], ...]
    fixtures_tree_present: bool
    first_party_packages: frozenset[str] = frozenset()

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
    except OSError as exc:
        raise PluginProjectError(f"{path}: cannot be read ({exc.strerror or exc})") from exc
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
                f"pyproject.toml: entry point {key!r} must be a string, not {type(value).__name__}."
            )
        result[str(key)] = value
    return result


def classify(root: Path) -> DirectoryKind:
    """*root* in its own terms: no ``pyproject.toml`` is ``empty``; one declaring the
    ``graftpunk.plugins`` entry-point group is ``plugin``; any other is ``foreign``.

    Raises:
        PluginProjectError: ``pyproject.toml`` exists but is not valid TOML, is not
            UTF-8, or cannot be read (permissions); its ``project`` or
            ``project.entry-points`` table, or its ``"graftpunk.plugins"`` group
            table, has the wrong shape; or an entry point's value is not a string.
    """
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
        PluginProjectError: *root* does not exist or is not a directory; or the
            project cannot be read at all: ``pyproject.toml`` is not valid TOML,
            is not UTF-8, or cannot be read (permissions); its ``project`` or
            ``project.entry-points`` table, or its ``"graftpunk.plugins"`` group
            table, has the wrong shape; an entry point's value is not a string;
            or an entry point's module is missing, is not UTF-8, cannot be read
            (permissions), or does not parse.
    """
    if not root.exists():
        raise PluginProjectError(f"{root}: no such directory.")
    if not root.is_dir():
        raise PluginProjectError(f"{root}: not a directory.")
    data = _load_pyproject(root)
    fixtures_tree_present = (root / policy.FIXTURES_TREE).is_dir()
    first_party_packages = _first_party_packages(root, data)
    if data is None:
        return ProjectView(
            directory="empty",
            plugins=(),
            defects=(),
            requirements=MappingProxyType({}),
            requirement_set=policy.PROJECT_REQUIREMENTS,
            test_markers=(),
            fixtures_tree_present=fixtures_tree_present,
            first_party_packages=first_party_packages,
        )
    entry_points = _entry_points(data)
    if entry_points is None:
        return ProjectView(
            directory="foreign",
            plugins=(),
            defects=(),
            requirements=MappingProxyType({}),
            requirement_set=policy.PROJECT_REQUIREMENTS,
            test_markers=(),
            fixtures_tree_present=fixtures_tree_present,
            first_party_packages=first_party_packages,
        )
    project_name = str(data.get("project", {}).get("name", ""))
    read = [
        _read_plugin(root, key, value, project_name, len(entry_points))
        for key, value in entry_points.items()
    ]
    requirement_set = policy.PROJECT_REQUIREMENTS
    requirements = _requirement_statuses(root, requirement_set)
    return ProjectView(
        directory="plugin",
        plugins=tuple(r for r in read if isinstance(r, PluginView)),
        defects=tuple(r for r in read if isinstance(r, PluginDefect)),
        requirements=MappingProxyType(requirements),
        requirement_set=requirement_set,
        test_markers=_test_markers(root),
        fixtures_tree_present=fixtures_tree_present,
        first_party_packages=first_party_packages,
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


_SKIPPED_ROOT_DIR_NAMES = frozenset({"__pycache__", "venv", "node_modules"})


def _first_party_packages(root: Path, data: dict[str, Any] | None) -> frozenset[str]:
    """Every name ruff's own default (``src = [".", "src"]``) treats as first party
    for *root*: each top-level directory (skipping a dotdir, ``__pycache__``,
    ``venv``, ``node_modules``, or a name that is not a valid Python identifier)
    and each top-level ``*.py`` stem, directly under the project root and under
    ``src/``, plus any name *data*'s ``[tool.ruff.lint.isort] known-first-party``
    (or the legacy ``[tool.ruff.isort]``) declares. Scanning the filesystem,
    rather than deriving this from ``PluginView.module_path``, is what makes a
    single-module plugin (a ``*.py`` file with no package directory) first
    party too: its module path keeps its ``.py`` suffix, which the old
    per-plugin derivation left in the set, so ruff never matched it."""
    names: set[str] = set()
    for base in (root, root / "src"):
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                if entry.name not in _SKIPPED_ROOT_DIR_NAMES and entry.name.isidentifier():
                    names.add(entry.name)
            elif entry.suffix == ".py":
                names.add(entry.stem)
    names.update(_known_first_party(data))
    return frozenset(names)


def _known_first_party(data: dict[str, Any] | None) -> frozenset[str]:
    """The project's own declared first-party names: ``[tool.ruff.lint.isort]
    known-first-party``, or the legacy ``[tool.ruff.isort] known-first-party``
    when the project has not migrated. Either table being the wrong shape is
    not this function's business to refuse; it reads what it can and ignores
    the rest, the same way an unreadable ``pyproject.toml`` table would already
    have raised earlier in ``read_project``."""
    if data is None:
        return frozenset()
    tool = data.get("tool")
    if not isinstance(tool, dict):
        return frozenset()
    ruff = tool.get("ruff")
    if not isinstance(ruff, dict):
        return frozenset()
    lint = ruff.get("lint")
    isort = lint.get("isort") if isinstance(lint, dict) else None
    if not isinstance(isort, dict):
        isort = ruff.get("isort")
    if not isinstance(isort, dict):
        return frozenset()
    known = isort.get("known-first-party")
    if not isinstance(known, list):
        return frozenset()
    return frozenset(name for name in known if isinstance(name, str))


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
    except OSError as exc:
        raise PluginProjectError(f"{relative}: cannot be read ({exc.strerror or exc})") from exc
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


_SKIPPED_TEST_DIR_NAMES = frozenset({"__pycache__", "venv", ".venv", "node_modules"})


def _test_markers(root: Path) -> tuple[tuple[str, int], ...]:
    """Every ``GP-FILL`` marker line in a ``*.py`` file under ``policy.TESTS_DIR``,
    as (project-relative path, 1-based line), file order then line order. A file
    that cannot be decoded as UTF-8, or cannot be read at all (permissions), is
    skipped, not raised: it is not this function's business to refuse an
    unreadable test module, only to report the markers it can read. A path with
    a component that starts with ``.`` (a
    dotfile or a dotdir, which covers ``.venv``) or that is ``__pycache__``,
    ``venv``, or ``node_modules`` is never a test module and is skipped."""
    tests_dir = root / policy.TESTS_DIR
    if not tests_dir.is_dir():
        return ()
    found: list[tuple[str, int]] = []
    candidates = sorted(
        p
        for p in tests_dir.rglob("*.py")
        if p.is_file()
        and not any(
            part.startswith(".") or part in _SKIPPED_TEST_DIR_NAMES
            for part in p.relative_to(tests_dir).parts
        )
    )
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        relative = path.relative_to(root).as_posix()
        found.extend(
            (relative, number)
            for number, line in enumerate(source_lines(text), start=1)
            if GP_FILL_MARKER in line
        )
    return tuple(found)


def _read_plugin(
    root: Path, key: str, value: str, project_name: str, suite_size: int
) -> PluginView | PluginDefect:
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
            for number, line in enumerate(source_lines(text), start=1)
            if GP_FILL_MARKER in line
        ),
        fixtures_root=policy.fixtures_root(
            suite_member=suite_size > 1 and _normalised(project_name) != _normalised(package),
            module_name=module_name_for(key),
        ),
        class_names=_class_body_names(klass),
    )


# Design note: whether a plugin is a suite member is decided twice, by two
# different facts. The renderer knows it directly: ScaffoldSpec.mode is
# "add_to_suite" or "new_project", set by whoever calls it. The reader has no
# such field to read back; mode is a choice the writer made at generation
# time, not a fact gp plugin new or gp plugin add-command persists anywhere
# on disk (no project file records it), so _read_plugin above re-derives the
# same answer from the facts that ARE on disk and match the generator's own
# decision: more than one entry point, and a suite member's package name
# differs from the project's [project].name. gp plugin new never produces a
# one-entry-point project whose package differs from the project name (the
# first plugin is always new_project, whose package matches), so the count
# guard changes nothing for a generated project; it only stops a hand-written
# one-plugin project, whose name happens to differ from its package, from
# misreading as a suite member (polish-r1 P8).
# test_the_rule_gives_the_same_answer_from_the_spec_and_from_the_project is
# the check that these two independent derivations still agree; it is the
# owner of that agreement, not a coincidence to be relied on silently.


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
            node.name,
            Span(start, node.end_lineno or node.lineno),
            MappingProxyType(keywords),
            group=isinstance(node, ast.ClassDef),
        )


def _assignment_targets(target: ast.expr) -> Iterator[str]:
    """Every name *target* binds: a plain name, or, recursively, each element of
    a tuple/list unpacking (including a starred element)."""
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, ast.Starred):
        yield from _assignment_targets(target.value)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _assignment_targets(element)


def _import_names(aliases: list[ast.alias]) -> Iterator[str]:
    """The name each ``import``/``from ... import`` alias binds: the ``as`` name,
    or, for a plain ``import a.b.c``, the top-level package ``a``."""
    for alias in aliases:
        yield alias.asname or alias.name.split(".")[0]


def _class_body_names(klass: ast.ClassDef) -> frozenset[str]:
    """Every name the class body binds at its top level: a method (decorated or
    not), a nested class (including a command group), an assignment target
    (including tuple/starred unpacking and an annotated assignment), an import,
    a ``for``/``with`` target, or a definition nested under an ``if``, a
    ``try``, or a ``try``/``except*`` at class-body level (walked recursively,
    without entering a nested function or class body). ``PluginView.class_names``:
    the stub inserter's
    collision check reads this, not just the decorated commands, so a plain
    helper method, attribute, or import is refused too."""
    names: set[str] = set()
    _collect_body_names(klass.body, names)
    return frozenset(names)


def _collect_body_names(body: list[ast.stmt], names: set[str]) -> None:
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_assignment_targets(target))
        elif isinstance(node, ast.AnnAssign):
            names.update(_assignment_targets(node.target))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(_import_names(node.names))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            names.update(_assignment_targets(node.target))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    names.update(_assignment_targets(item.optional_vars))
        elif isinstance(node, ast.If):
            _collect_body_names(node.body, names)
            _collect_body_names(node.orelse, names)
        elif isinstance(node, (ast.Try, ast.TryStar)):
            # ast.TryStar (a "try: ... except* E:" block) parses this way on every
            # Python this project supports (requires-python >=3.11, which is when
            # ast.TryStar was added), so it is not conditional on the running
            # interpreter.
            _collect_body_names(node.body, names)
            for handler in node.handlers:
                _collect_body_names(handler.body, names)
            _collect_body_names(node.orelse, names)
            _collect_body_names(node.finalbody, names)


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
    except OSError as exc:
        return RequirementStatus("unreadable", f"cannot be read ({exc.strerror or exc})")
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        return RequirementStatus("unreadable", _parse_error_message(exc))
