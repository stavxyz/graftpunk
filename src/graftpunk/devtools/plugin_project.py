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
import glob
import os
import re
import stat
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
from graftpunk.devtools.scaffold.suite_conftest import SuiteConftestState, read_suite_conftest
from graftpunk.har.naming import registered_name
from graftpunk.plugins import PLUGINS_GROUP

__all__ = [
    "CANNOT_BE_READ",
    "DOES_NOT_PARSE",
    "EXACTLY_ONE",
    "NOT_A_DIRECTORY",
    "NOT_A_DIRECTORY_PHRASE",
    "NOT_A_REGULAR_FILE",
    "NOT_A_REGULAR_FILE_PHRASE",
    "NOT_VALID_UTF8",
    "CommandView",
    "DirectoryKind",
    "MemberRoot",
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
    "unreadable_file_message",
]

DirectoryKind = Literal["empty", "plugin", "foreign"]
RequirementState = Literal["bound", "unbound", "unreadable"]
PathKind = Literal["absent", "file", "dir", "other", "unreadable", "unfollowable"]

# Shared between gp plugin check (a Finding's message) and gp plugin upgrade
# (a refusal), so the two consumers of the one reader fact never describe the
# same blocked path differently. The short *_PHRASE constants are each
# reason's one identifying substring, built into its full constant below and
# also used by plugin_check.FINDING_ADVICE, so the guide's prose and the
# messages themselves are checked against the same words.
NOT_A_DIRECTORY_PHRASE = "is not a directory"
NOT_A_REGULAR_FILE_PHRASE = "is not a regular file"
CANNOT_BE_READ = "cannot be read"
NOT_VALID_UTF8 = "not valid UTF-8"
DOES_NOT_PARSE = "does not parse"
EXACTLY_ONE = "exactly one"

NOT_A_DIRECTORY = f"exists but {NOT_A_DIRECTORY_PHRASE}; move it aside, then run gp plugin upgrade."
NOT_A_REGULAR_FILE = f"exists but {NOT_A_REGULAR_FILE_PHRASE}"


def unreadable_file_message(reason: str) -> str:
    """*reason* (one of this module's unreadable-file reasons, or a parse/decode
    message) with the advice that fits it, built once so ``gp plugin check``'s
    finding and ``gp plugin upgrade``'s refusal can never word the same reason
    differently. ``NOT_A_DIRECTORY`` already carries its own advice (an ancestor
    blocking the path, not the path itself): used as given. A permissions
    problem and an encoding problem each get advice of their own, since
    neither is fixed by waiting for the file to parse."""
    if reason == NOT_A_DIRECTORY:
        return reason
    if reason == NOT_A_REGULAR_FILE:
        return f"{reason}; move it aside, then run gp plugin upgrade."
    if reason.startswith(CANNOT_BE_READ):
        return f"{reason}; fix its permissions, then run gp plugin upgrade."
    if reason.startswith(NOT_VALID_UTF8):
        return f"{reason}; save it as UTF-8, then run gp plugin upgrade."
    return f"{reason}; gp plugin upgrade can add its wiring once it parses."


def _probe(path: Path, detail: list[str] | None = None) -> PathKind:
    """What is at *path*, from ``path.lstat()``/``path.stat()`` inside
    ``try``/``except OSError``: the one place in this module allowed to ask
    the filesystem what is at a path or what kind of thing it is. That
    covers any ``is_*`` predicate call (``.is_dir()``, ``.is_fifo()``,
    ``.is_mount()``, and every other one pathlib has or ever adds), the
    exact calls ``.exists()``, ``.lexists()``, ``.stat()``, ``.lstat()``,
    ``.samefile()``, and ``.access()``, and any ``os.path`` function other
    than the pure string ones (``join``, ``basename``, ``dirname``,
    ``splitext``, ``split``, ``normpath``, ``relpath``, ``sep``), whether
    reached as ``os.path.whatever(...)`` or imported by name and called bare
    (``test_only_probe_calls_exists_is_dir_is_file_is_symlink_lstat_or_stat``
    enforces this by AST), so every other "what is at this path" question in
    this module goes through this function.

    ``"absent"``: *path*, or a component of it, plainly does not exist
    (``FileNotFoundError`` or ``NotADirectoryError``). ``"dir"`` or
    ``"file"``: a directory or a regular file, a symlink to one included.
    ``"other"``: a symlink whose follow-up ``stat()`` finds nothing there at
    all (``FileNotFoundError`` or ``NotADirectoryError``, a dangling target)
    or whose own kind is neither a directory nor a regular file: the symlink
    itself is what is wrong, never the path that led to it.
    ``"unfollowable"``: a symlink whose follow-up ``stat()`` raises any other
    ``OSError`` (a loop, or a permission error reaching the target): the
    symlink itself is again what is wrong, not the path that led to it, but
    unlike ``"other"`` the reason is worth keeping. When *detail* is given
    and the result is ``"unfollowable"``, ``exc.strerror`` (or the exception
    itself when the platform gives none) is appended to it, the same as for
    ``"unreadable"`` below.
    ``"unreadable"``: an ``OSError`` from ``lstat()`` on *path* itself, which
    can only mean an ancestor of *path* is untraversable (most often a
    permission error on a directory without its execute bit); ``lstat()``
    does not follow a symlink at *path*, so *path*'s own kind is never why
    this happens. When *detail* is given and the result is ``"unreadable"``,
    ``exc.strerror`` (or the exception itself when the platform gives none)
    is appended to it, so a caller that wants the reason for a message does
    not stat *path* again."""
    try:
        linked = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    except OSError as exc:
        if detail is not None:
            detail.append(exc.strerror or str(exc))
        return "unreadable"
    if stat.S_ISLNK(linked.st_mode):
        try:
            found = path.stat()
        except (FileNotFoundError, NotADirectoryError):
            return "other"
        except OSError as exc:
            if detail is not None:
                detail.append(exc.strerror or str(exc))
            return "unfollowable"
    else:
        found = linked
    if stat.S_ISDIR(found.st_mode):
        return "dir"
    if stat.S_ISREG(found.st_mode):
        return "file"
    return "other"


def _blocked_path(
    root: Path, relative: str, *, kind: Literal["directory", "file"]
) -> tuple[str, str] | None:
    """Whether project-relative *relative* can be read as *kind*: ``None`` when
    nothing blocks it (every ancestor is a directory, and *relative* itself is
    either missing outright or matches *kind*), or ``(path, reason)`` for the
    first project-relative path component (no trailing slash) that blocks it:
    an ancestor that exists but is not a directory, including one that is a
    symlink that cannot be followed (*reason* is ``NOT_A_DIRECTORY``,
    blaming the symlink itself, never the directory it sits in), *relative*
    itself when it exists but is not *kind* (the same is true of a symlink
    of any kind that does not resolve; *reason* is ``NOT_A_DIRECTORY`` or
    ``NOT_A_REGULAR_FILE``), or an ancestor whose own ``lstat()`` raises an
    ``OSError`` other than "missing", which only an ancestor further up can
    cause (*reason* names ``exc.strerror``). ``stat`` needs no execute bit on
    *current* itself, only on each directory above it, so a probe of
    ``root/a/b`` that fails this way blames ``a`` (the last component already
    confirmed a directory), never ``a/b`` (which was never reached): the one
    component shallower than whichever probe first raised."""
    parts = [part for part in relative.split("/") if part]
    current = root
    consumed: list[str] = []
    for index, part in enumerate(parts):
        current = current / part
        consumed.append(part)
        detail: list[str] = []
        found = _probe(current, detail)
        if found == "absent":
            return None
        if found == "unreadable":
            blamed = consumed[:-1] or consumed
            return "/".join(blamed), f"{CANNOT_BE_READ} ({detail[0]})"
        if index < len(parts) - 1:
            if found != "dir":
                return "/".join(consumed), NOT_A_DIRECTORY
        else:
            wanted: PathKind = "dir" if kind == "directory" else "file"
            if found != wanted:
                reason = NOT_A_DIRECTORY if kind == "directory" else NOT_A_REGULAR_FILE
                return "/".join(consumed), reason
    return None


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
    # The literal strings the class assigns to ``aliases``: an empty tuple when it
    # assigns none (aliases are never inherited, so the class body is the whole
    # answer), ``None`` when it assigns anything other than a literal tuple or
    # list of strings. ``aliases_line`` is the assignment's line, when there is one.
    aliases: tuple[str, ...] | None = ()
    aliases_line: int | None = None
    # The string literal the class assigns to ``session_name``, or ``None``.
    session_name: str | None = None


@dataclass(frozen=True)
class PluginDefect:
    """An entry point whose module parsed but does not hold exactly one ``SitePlugin``
    subclass. Recorded rather than raised, so the rest of the project still reads
    and each consumer decides whether the defect stops it. ``message`` begins with
    ``module_path`` and a colon, so a consumer that prints it (``gp plugin check``'s
    ``Finding``) names no path of its own: doing so would print the path twice."""

    entry_point: str
    module_path: str  # project-relative, forward slashes
    message: str


@dataclass(frozen=True)
class RequirementStatus:
    """Whether a project's file binds one ``PROJECT_REQUIREMENTS`` name: ``bound``;
    ``unbound`` (the file is missing, or does not bind it); or ``unreadable``, with
    ``reason`` saying why (see ``graftpunk.devtools.plugin_check``'s module docstring
    for the complete list of reasons). ``blocking`` is the project-relative ancestor
    path an "unreadable" finding should name instead of the requirement's own path,
    when an ancestor (not the file itself) is what is wrong; ``None`` for every other
    reason, where the requirement's own path is the right one to name. ``present`` is
    whether the file itself exists, defined for every state: true for ``bound``; true
    for ``unbound`` when the file exists but does not bind the name, false when it is
    plainly absent; true for an ``unreadable`` file that is wrong in its own right
    (does not parse, is not valid UTF-8, cannot be read, or is not a regular file),
    since the file does exist; false for an ``unreadable`` status whose ``blocking``
    names an ancestor, since the file past that ancestor was never reached and its
    own existence is unknown. ``gp plugin upgrade`` reads it instead of asking the
    filesystem the same question a second time."""

    state: RequirementState
    reason: str | None = None
    blocking: str | None = None
    present: bool = True


@dataclass(frozen=True)
class MemberRoot:
    """A suite member's own fixtures root (``PluginView.fixtures_root``), as
    ``ProjectView.member_roots`` reads it: whether it is a directory, and, when
    something blocks it, ``(path, reason)`` as ``fixtures_tree_blocked`` gives
    them for the tree (#370)."""

    entry_point: str
    path: str  # project-relative, trailing slash
    present: bool
    blocked: tuple[str, str] | None = None


@dataclass(frozen=True)
class ProjectView:
    """A directory's plugin project: its classification, one view per readable entry
    point, one defect per entry point whose module is structurally wrong, and the
    status of each requirement name in its files (read-only, keyed by the
    requirement's ``key``).

    ``requirements`` and ``requirement_set`` are both computed when the project is
    read, from ``policy.PROJECT_REQUIREMENTS`` as it stood then and, in a suite,
    each member's ``policy.member_requirements``; :meth:`missing_requirements`
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
    ``fixtures_tree_blocked`` is ``None`` when nothing blocks it, or
    ``(path, reason)`` for the project-relative path (no trailing slash) that
    does: the tree itself (a regular file, or a symlink that cannot be
    followed to a directory), or an ancestor (``tests`` itself being a file,
    or unreadable, most likely). *reason* is ``NOT_A_DIRECTORY`` or "cannot be
    read (<strerror>)", the same text :func:`unreadable_file_message` turns
    into a finding or a refusal. A reader fact distinct from "missing", so
    neither consumer tells an author gp plugin upgrade will create a path it
    is actually going to refuse. ``member_roots`` holds the same two facts for
    each suite member's own fixtures root, and ``suite_conftest`` what a suite's
    shared conftest holds of its fixtures check (``SUITE_MEMBER_FIXTURES``, and
    whether its check is still the standalone whole-tree one), which
    ``gp plugin check`` reports and ``gp plugin upgrade`` fixes (#370).

    ``first_party_packages`` is read once here, by the same rule ruff uses to
    decide a module is first party: every top-level directory and ``*.py`` or
    ``*.pyi`` stem directly under each directory of the project's ``[tool.ruff]
    src`` (ruff's default ``[".", "src"]`` when unset; a root ``.ruff.toml`` or
    ``ruff.toml`` wins over ``pyproject.toml``, the nearest ancestor's settings
    file counts when the project has none, and ``extend`` is followed, as in
    ruff), plus any name the ``[lint.isort] known-first-party`` (or, when that
    table is absent, the legacy ``[isort]``) of those settings declares. It is
    what ``add_command`` and ``gp plugin upgrade`` pass as
    ``pysrc.with_import``'s ``first_party=`` argument, so a new import lands in
    isort's first-party section exactly when ruff itself would sort it there,
    whether the plugin lives in a package directory or is a single module file.

    Design note: ``requirement_set`` is computed by ``read_project`` at read time,
    and both methods read ``self.requirement_set`` instead of the module attribute,
    so a ``ProjectView`` is a value: two calls on the same instance always agree,
    and equality compares the same fields the methods read.
    """

    directory: DirectoryKind
    plugins: tuple[PluginView, ...]
    defects: tuple[PluginDefect, ...]
    requirements: Mapping[str, RequirementStatus]
    requirement_set: tuple[ProjectRequirement, ...]
    test_markers: tuple[tuple[str, int], ...]
    fixtures_tree_present: bool
    fixtures_tree_blocked: tuple[str, str] | None = None
    first_party_packages: frozenset[str] = frozenset()
    member_roots: tuple[MemberRoot, ...] = ()
    suite_conftest: SuiteConftestState | None = None

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

    def _serving(self, plugin: str | None) -> tuple[ProjectRequirement, ...]:
        """The ``requirement_set`` entries that serve *plugin* (an entry-point name):
        its own and the whole project's. Every entry when *plugin* is ``None``."""
        if plugin is None:
            return self.requirement_set
        return tuple(r for r in self.requirement_set if r.plugin in (None, plugin))

    def missing_requirements(self, plugin: str | None = None) -> tuple[ProjectRequirement, ...]:
        """The ``requirement_set`` entries this project's files do not bind, in
        declared order: the one answer ``gp plugin upgrade`` applies and
        ``gp plugin check`` reports. With *plugin*, only those that serve it
        (``ProjectRequirement.plugin``). An entry whose file does not parse is not
        here; its file is in :meth:`unreadable_files`. Empty for a directory that
        is not a plugin project, which has no requirements to lack."""
        if self.directory != "plugin":
            return ()
        return tuple(r for r in self._serving(plugin) if self._state(r) == "unbound")

    def entry_point_for(self, name: str) -> str | None:
        """The entry point of the readable plugin *name* addresses, by its entry-point
        name or its module name (``policy.module_name_for``, the name its fixtures
        root and test module carry), or ``None``."""
        for found in self.plugins:
            if name in (found.entry_point, module_name_for(found.entry_point)):
                return found.entry_point
        return None

    def unreadable_files(self, plugin: str | None = None) -> tuple[tuple[str, str], ...]:
        """Each requirement file whose status is ``unreadable``, once, as ``(path,
        reason)``, in the order ``requirement_set`` first names it, however many
        requirements it holds (``graftpunk.devtools.plugin_check``'s module
        docstring has the complete list of reasons): ``gp plugin upgrade`` refuses
        on them and ``gp plugin check`` reports one finding each, while
        ``gp plugin info`` and ``gp plugin add-command``, which never read those
        files, proceed. *path* is the requirement's own file, unless an ancestor
        blocks it (``status.blocking``), in which case it is that ancestor: the
        finding then names the same path ``gp plugin upgrade`` refuses on. With
        *plugin*, only the files of the requirements that serve it."""
        if self.directory != "plugin":
            return ()
        files: dict[str, str] = {}
        for r in self._serving(plugin):
            status = self.requirements.get(r.key)
            if status is not None and status.state == "unreadable":
                files.setdefault(status.blocking or r.path, status.reason or DOES_NOT_PARSE)
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
    detail: list[str] = []
    kind = _probe(path, detail)
    if kind == "unreadable":
        raise PluginProjectError(f"{path}: {CANNOT_BE_READ} ({detail[0]}).")
    if kind == "unfollowable":
        raise PluginProjectError(f"{path}: cannot be followed ({detail[0]}).")
    if kind == "absent":
        return None
    if kind != "file":
        raise PluginProjectError(f"{path}: {NOT_A_REGULAR_FILE}.")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise PluginProjectError(f"{path}: {NOT_VALID_UTF8} ({exc})") from exc
    except OSError as exc:
        raise PluginProjectError(f"{path}: {CANNOT_BE_READ} ({exc.strerror or exc})") from exc
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


def _require_directory(root: Path) -> None:
    """Refuse *root* in one line when it is not a readable directory: the one
    guard both of the reader's public entry points, :func:`classify` and
    :func:`read_project`, apply first, so a mistyped ``--dir`` cannot read as
    an empty project through one of them and a refusal through the other."""
    detail: list[str] = []
    kind = _probe(root, detail)
    if kind == "absent":
        raise PluginProjectError(f"{root}: no such directory.")
    if kind == "unreadable":
        raise PluginProjectError(f"{root}: {CANNOT_BE_READ} ({detail[0]}).")
    if kind == "unfollowable":
        raise PluginProjectError(f"{root}: cannot be followed ({detail[0]}).")
    if kind != "dir":
        raise PluginProjectError(f"{root}: not a directory.")


def classify(root: Path) -> DirectoryKind:
    """*root* in its own terms: no ``pyproject.toml`` is ``empty``; one declaring the
    ``graftpunk.plugins`` entry-point group is ``plugin``; any other is ``foreign``.

    Raises:
        PluginProjectError: *root* does not exist, is not a directory, or is a
            symlink that cannot be followed; or ``pyproject.toml`` exists but
            is not a regular file, is not valid TOML, is not UTF-8, cannot be
            read (permissions), or is a symlink that cannot be followed; its
            ``project`` or ``project.entry-points`` table, or its
            ``"graftpunk.plugins"`` group table, has the wrong shape; or an
            entry point's value is not a string.
    """
    _require_directory(root)
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
        PluginProjectError: *root* does not exist, is not a directory, or is a
            symlink that cannot be followed; or the project cannot be read at
            all: ``pyproject.toml`` exists but is not a regular file, is not
            valid TOML, is not UTF-8, cannot be read (permissions), or is a
            symlink that cannot be followed; its ``project`` or
            ``project.entry-points`` table, or its ``"graftpunk.plugins"``
            group table, has the wrong shape; an entry point's value is not a
            string; or an entry point's module is missing, is not a regular
            file (a directory or a dangling symlink), is blocked by a
            non-directory ancestor, is not UTF-8, cannot be read (permissions
            or a symlink that cannot be followed), or does not parse.
    """
    _require_directory(root)
    data = _load_pyproject(root)
    fixtures_tree = root / policy.FIXTURES_TREE
    fixtures_tree_blocked = _blocked_path(root, policy.FIXTURES_TREE, kind="directory")
    fixtures_tree_present = fixtures_tree_blocked is None and _probe(fixtures_tree) == "dir"
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
            fixtures_tree_blocked=fixtures_tree_blocked,
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
            fixtures_tree_blocked=fixtures_tree_blocked,
            first_party_packages=first_party_packages,
        )
    project_name = str(data.get("project", {}).get("name", ""))
    read = [
        _read_plugin(root, key, value, project_name, len(entry_points))
        for key, value in entry_points.items()
    ]
    plugins = tuple(r for r in read if isinstance(r, PluginView))
    requirement_set = _requirement_set(plugins, _test_files(root))
    requirements = _requirement_statuses(root, requirement_set)
    return ProjectView(
        directory="plugin",
        plugins=plugins,
        defects=tuple(r for r in read if isinstance(r, PluginDefect)),
        requirements=MappingProxyType(requirements),
        requirement_set=requirement_set,
        test_markers=_test_markers(root),
        fixtures_tree_present=fixtures_tree_present,
        fixtures_tree_blocked=fixtures_tree_blocked,
        first_party_packages=first_party_packages,
        member_roots=_member_roots(root, plugins),
        suite_conftest=_suite_conftest(root, plugins),
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
    """Every name ruff treats as first party for *root*: each top-level
    directory (skipping a dotdir, ``__pycache__``, ``venv``, ``node_modules``,
    or a name that is not a valid Python identifier) and each top-level
    ``*.py`` or ``*.pyi`` stem directly under each of ruff's ``src`` roots
    (:func:`_ruff_settings`), plus any name the ruff settings'
    ``known-first-party`` declares. Scanning the filesystem, rather than
    deriving this from ``PluginView.module_path``, is what makes a
    single-module plugin (a ``*.py`` file with no package directory) first
    party too: its module path keeps its ``.py`` suffix, which the old
    per-plugin derivation left in the set, so ruff never matched it."""
    settings = _ruff_settings(root, data)
    names: set[str] = set()
    for base in settings.roots:
        if _probe(base) != "dir":
            continue
        try:
            entries = tuple(base.iterdir())
        except OSError:
            # A directory ruff itself cannot stat into contributes no names
            # (ruff's own source matching treats an unreadable path as "no
            # match"); a refusal must still be one line, which this is not.
            continue
        for entry in entries:
            if entry.name.startswith("."):
                continue
            kind = _probe(entry)
            if kind == "unreadable":
                # A path ruff itself cannot stat contributes no names, the
                # same rule the directory-level OSError above follows.
                continue
            if kind == "dir":
                if entry.name not in _SKIPPED_ROOT_DIR_NAMES and entry.name.isidentifier():
                    names.add(entry.name)
            elif entry.suffix in (".py", ".pyi"):
                # ruff's isort match_sources accepts either extension.
                names.add(entry.stem)
    names.update(settings.known_first_party)
    return frozenset(names)


# The settings files ruff looks for in a directory, in the order it takes the
# first one present (ruff_workspace::pyproject::settings_toml, ruff 0.17.5):
# a pyproject.toml counts only when it has a [tool.ruff] table.
_RUFF_SETTINGS_FILES = (".ruff.toml", "ruff.toml")


@dataclass(frozen=True)
class _RuffSettings:
    """What ruff resolves for first-party detection: the ``src`` roots, or
    ``None`` for ruff's default (``project_root`` and its ``src/``), and
    ``known-first-party``. ``project_root`` is the directory of the settings
    file ruff found (``resolve_scoped_settings`` in ruff_workspace::resolver),
    or the plugin project root when it found none."""

    src: tuple[Path, ...] | None
    known_first_party: frozenset[str]
    project_root: Path

    @property
    def roots(self) -> tuple[Path, ...]:
        """The directories first-party modules are looked for in."""
        if self.src is not None:
            return self.src
        return (self.project_root, self.project_root / "src")


_NO_RUFF_FILE = object()


def _read_ruff_file(path: Path, *, discovering: bool = False) -> dict[str, Any] | object | None:
    """*path*'s ruff settings table (the top level of a ``ruff.toml``, the
    ``[tool.ruff]`` of a ``pyproject.toml``, empty when it has none);
    ``_NO_RUFF_FILE`` when nothing ruff reads is there (absent, a symlink
    ruff's own ``is_file()`` cannot follow, or, while *discovering* a settings
    file, a ``pyproject.toml`` without ``[tool.ruff]``, which ruff's
    ``settings_toml`` passes over); ``None`` when it is there but cannot be
    read or parsed, where ruff itself refuses to run."""
    if _probe(path) != "file":
        return _NO_RUFF_FILE
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    if path.name != "pyproject.toml":
        return parsed
    tool = parsed.get("tool")
    ruff = tool.get("ruff") if isinstance(tool, dict) else None
    if isinstance(ruff, dict):
        return ruff
    return _NO_RUFF_FILE if discovering else {}


class _ShellExpandError(Exception):
    """A value ruff's ``shellexpand::full`` refuses: an unset variable with no
    default. ruff then refuses to run."""


# A ${...} reference up to the first "}", a bare $NAME (shellexpand's
# is_valid_var_name_char: Unicode alphanumeric or "_"), "$$", or a lone "$".
_SHELL_REFERENCE = re.compile(r"\$(?:\{(?P<braced>[^}]*)\}|(?P<bare>[^\W]+)|(?P<dollar>\$)?)")


def _shell_expand(value: str) -> str:
    """*value* expanded the way ruff expands ``src`` entries and ``extend``
    (``shellexpand::full``, shellexpand 3.1.2 ``src/funcs.rs``, the version
    ruff 0.17.5 locks):

    - ``${NAME}`` and ``$NAME`` take the variable's value, an empty value
      included; unset, they raise :class:`_ShellExpandError`.
    - ``${NAME:-default}`` takes the default only when ``NAME`` is unset
      (``env_with_context``: a set value, even empty, wins); the default is
      not expanded again. ``${:-x}`` names the variable ``:-x``.
    - ``$$`` is one ``$``; a ``$`` before anything else, and an unclosed
      ``${``, stay as they are.
    - A leading ``~`` followed by ``/`` or nothing becomes the home directory,
      unless the ``~`` came from a variable (``full_with_context``).

    Raises:
        _ShellExpandError: A reference names an unset variable with no default.
    """

    def substitute(match: re.Match[str]) -> str:
        braced, bare = match.group("braced"), match.group("bare")
        if braced is None and bare is None:
            return "$"
        if bare is not None:
            name, default = bare, None
        else:
            split = braced.find(":-")
            if split > 0:
                name, default = braced[:split], braced[split + 2 :]
            else:
                name, default = braced, None
        found = os.environ.get(name)
        if found is not None:
            return found
        if default is not None:
            return default
        raise _ShellExpandError(name)

    expanded = _SHELL_REFERENCE.sub(substitute, value)
    if value.startswith("~") and (expanded == "~" or expanded.startswith("~/")):
        expanded = os.path.expanduser("~") + expanded[1:]
    return expanded


def _discover_ruff_file(root: Path, data: dict[str, Any] | None) -> tuple[Any, Path]:
    """The settings file ruff uses for a file under *root*, with its table,
    found the way ruff finds it: ``settings_toml`` (ruff_workspace::pyproject:
    a ``.ruff.toml``, then a ``ruff.toml``, then a ``pyproject.toml`` with
    ``[tool.ruff]``) in *root*, then in each ancestor in turn
    (``find_settings_toml``, pyproject.rs:109). *data* is *root*'s own
    ``pyproject.toml``, already read. The table is ``None`` when the file
    found cannot be read or parsed, and ``{}`` at *root*'s ``pyproject.toml``
    when no directory has a settings file. ruff's next fallback, a user-level
    settings file, is not read. *root* is made absolute first: the CLI's
    default ``--dir .`` has no ``parents``, and a relative ``../proj`` would
    walk the current directory instead of the project's ancestors."""
    root = Path(os.path.abspath(root))
    for directory in (root, *root.parents):
        for name in _RUFF_SETTINGS_FILES:
            table = _read_ruff_file(directory / name)
            if table is not _NO_RUFF_FILE:
                return table, directory / name
        if directory == root:
            tool = data.get("tool") if data is not None else None
            ruff = tool.get("ruff") if isinstance(tool, dict) else None
            if isinstance(ruff, dict):
                return ruff, root / "pyproject.toml"
            continue
        table = _read_ruff_file(directory / "pyproject.toml", discovering=True)
        if table is not _NO_RUFF_FILE:
            return table, directory / "pyproject.toml"
    return {}, root / "pyproject.toml"


def _ruff_settings(root: Path, data: dict[str, Any] | None) -> _RuffSettings:
    """The settings ruff itself resolves for *root*, by ruff 0.17.5's rules:

    - The settings file is the one :func:`_discover_ruff_file` finds: in
      *root*, or in the nearest ancestor that has one.
    - ``extend`` is followed the way ``resolve_configuration_with_cache``
      (ruff_workspace::resolver) follows it: expanded by
      :func:`_shell_expand` (``Configuration::from_options``), a relative
      path taken from the directory of the file that names it, and so on
      down the chain.
    - Each file's ``src`` is resolved against that file's own directory, and
      the first file in the chain that sets ``src`` wins
      (``Configuration::combine``: ``self.src.or(config.src)``). With none,
      ruff's default is the found settings file's directory and its
      ``src/`` (configuration.rs:395).
    - ``known-first-party`` likewise comes from the first file that sets it:
      across ``extend`` the isort keys combine one by one
      (``impl CombinePluginOptions for Option<T>``, configuration.rs:1445).
      Within one file, a present ``[lint.isort]`` table replaces the legacy
      ``[isort]`` table whole: ``lint.common.combine(lint_top_level)``
      (configuration.rs:536) is the ``CombineOptions`` derive, which joins
      each field as ``self.field.or(other.field)``
      (ruff_macros/src/combine_options.rs:53), and the field is the whole
      ``isort`` table.

    A file in the chain that cannot be read or parsed, an ``extend`` naming a
    missing file, a cycle, or an unset variable with no default in any
    file's ``extend`` or ``src`` makes ruff refuse to run, so no placement
    could agree with it; the defaults at *root* are the closest guess then."""
    root = Path(os.path.abspath(root))
    defaults = _RuffSettings(src=None, known_first_party=frozenset(), project_root=root)
    table, path = _discover_ruff_file(root, data)
    if not isinstance(table, dict):
        return defaults
    chain: list[tuple[dict[str, Any], Path]] = [(table, path)]
    seen = {os.path.normpath(path)}
    try:
        while True:
            table, path = chain[-1]
            extend = table.get("extend")
            if not isinstance(extend, str):
                break
            target = Path(os.path.normpath(path.parent / _shell_expand(extend)))
            if str(target) in seen:
                return defaults
            seen.add(str(target))
            extended = _read_ruff_file(target)
            if not isinstance(extended, dict):
                return defaults
            chain.append((extended, target))
        # Every file's src is resolved, as each from_options call does, so an
        # unset variable in a src an extending file overrides still refuses.
        resolved = [_ruff_src_roots(path.parent, table.get("src")) for table, path in chain]
    except _ShellExpandError:
        return defaults
    src = next((roots for roots in resolved if roots is not None), None)
    known: frozenset[str] = frozenset()
    for table, _path in chain:
        declared = _known_first_party(table)
        if declared is not None:
            known = declared
            break
    return _RuffSettings(src=src, known_first_party=known, project_root=chain[0][1].parent)


def _ruff_src_roots(base: Path, src: Any) -> tuple[Path, ...] | None:
    """One settings file's ``src`` resolved the way ruff resolves it
    (``resolve_src`` in ruff_workspace::configuration, ruff 0.17.5): each
    entry expanded by :func:`_shell_expand`, a relative entry taken from
    *base* (the settings file's directory), and each entry expanded as a
    glob, so an entry matching nothing contributes nothing. ``None`` when
    *src* is not a list of strings, which leaves the choice to the next file
    in the chain. ruff has no ``src`` under ``lint``; it refuses the key
    there.

    Raises:
        _ShellExpandError: An entry names an unset variable with no default.
    """
    if not isinstance(src, list) or not all(isinstance(entry, str) for entry in src):
        return None
    roots: list[Path] = []
    for entry in src:
        pattern = os.path.normpath(base / _shell_expand(entry))
        # The Rust glob crate ruff uses lets "*" match a leading dot and "**"
        # cross directories, so both flags are on to match it.
        matches = glob.glob(pattern, recursive=True, include_hidden=True)
        roots.extend(Path(match) for match in sorted(matches))
    return tuple(roots)


def _known_first_party(table: dict[str, Any]) -> frozenset[str] | None:
    """One settings table's ``known-first-party``: ``[lint.isort]``'s when
    that table exists, else the legacy ``[isort]``'s (see
    :func:`_ruff_settings` for ruff's rule); ``None`` when the table that
    counts does not set it. A wrong shape is not this function's business to
    refuse; it reads what it can and ignores the rest."""
    lint = _subtable(table, "lint")
    isort = _subtable(lint, "isort") if isinstance(lint.get("isort"), dict) else None
    if isort is None:
        isort = _subtable(table, "isort")
    known = isort.get("known-first-party")
    if not isinstance(known, list):
        return None
    return frozenset(name for name in known if isinstance(name, str))


def _subtable(table: dict[str, Any], key: str) -> dict[str, Any]:
    value = table.get(key)
    return value if isinstance(value, dict) else {}


def _module_file(root: Path, module: str) -> str | None:
    """The first candidate path (src layout, then flat) that is a regular
    file, or ``None`` when none is and no candidate exists at all (the caller
    then refuses, naming the entry point). A candidate that exists but is not
    a regular file does not read as "not here" either, in any of three ways:
    a directory on the way that cannot even be traversed (no execute bit),
    raised naming that directory (the one to fix) with its reason once found,
    or a symlink on the way that cannot be followed, raised the same way; a
    candidate path that plainly exists but is a directory or a
    dangling symlink rather than a file, raised as ``NOT_A_REGULAR_FILE`` once
    no candidate is ever a file; or a non-directory ancestor (found by
    :func:`_blocked_path`) blocking a candidate, raised naming that ancestor
    when no candidate was found and nothing else was wrong. An unreadable or
    unfollowable candidate is reported over a wrong-kind one wherever each
    falls in the search order, since a file may lie past the untraversable or
    unfollowable ancestor, while the wrong-kind path cannot be the module."""
    relative = module.replace(".", "/")
    first_error: tuple[str, str, Literal["unreadable", "unfollowable"]] | None = None
    first_wrong_kind: str | None = None
    first_blocked_ancestor: str | None = None
    for base in _SOURCE_ROOTS:
        for candidate in (f"{relative}.py", f"{relative}/__init__.py"):
            path = Path(base) / candidate
            posix = path.as_posix()
            blocked = _blocked_path(root, posix, kind="file")
            if blocked is not None:
                component, reason = blocked
                if reason.startswith(CANNOT_BE_READ):
                    # An untraversable ancestor: name it, as the fixtures tree
                    # and the requirement files do, not the candidate past it,
                    # whose own permissions may well be fine.
                    if first_error is None:
                        detail_text = reason.removeprefix(f"{CANNOT_BE_READ} (").removesuffix(")")
                        first_error = (component, detail_text, "unreadable")
                    continue
                if component != posix and reason == NOT_A_DIRECTORY:
                    # _blocked_path folds an unfollowable ancestor into
                    # NOT_A_DIRECTORY (a symlink that cannot be followed reads
                    # as the wrong kind, like a dangling one), which loses the
                    # reason; re-probe the ancestor itself so an
                    # unfollowable one still reports why, instead of a false
                    # "exists but is not a directory".
                    component_detail: list[str] = []
                    if _probe(root / component, component_detail) == "unfollowable":
                        if first_error is None:
                            first_error = (component, component_detail[0], "unfollowable")
                    elif first_blocked_ancestor is None:
                        first_blocked_ancestor = component
                    continue
            detail: list[str] = []
            kind = _probe(root / path, detail)
            if kind == "file":
                return posix
            if kind in ("unreadable", "unfollowable"):
                if first_error is None:
                    first_error = (posix, detail[0], kind)
            elif kind != "absent" and first_wrong_kind is None:
                first_wrong_kind = posix
    if first_error is not None:
        candidate_path, reason, error_kind = first_error
        phrase = CANNOT_BE_READ if error_kind == "unreadable" else "cannot be followed"
        raise PluginProjectError(f"{candidate_path}: {phrase} ({reason}).")
    if first_wrong_kind is not None:
        raise PluginProjectError(f"{first_wrong_kind}: {NOT_A_REGULAR_FILE}.")
    if first_blocked_ancestor is not None:
        raise PluginProjectError(f"{first_blocked_ancestor}: exists but {NOT_A_DIRECTORY_PHRASE}.")
    return None


def _parse_error_message(exc: SyntaxError | ValueError) -> str:
    """The "does not parse (...)" refusal text, with a line clause only when the
    exception carries one: a NUL byte in the source raises ``ValueError`` on
    Python 3.11 (no ``lineno``) and ``SyntaxError`` with ``lineno=None`` on later
    versions."""
    msg = getattr(exc, "msg", None) or str(exc)
    lineno = getattr(exc, "lineno", None)
    clause = f", line {lineno}" if lineno is not None else ""
    return f"{DOES_NOT_PARSE} ({msg}{clause})"


def _parse(root: Path, relative: str) -> tuple[str, ast.Module]:
    path = root / relative
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise PluginProjectError(f"{relative}: {NOT_VALID_UTF8} ({exc})") from exc
    except OSError as exc:
        raise PluginProjectError(f"{relative}: {CANNOT_BE_READ} ({exc.strerror or exc})") from exc
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
    if _probe(tests_dir) != "dir":
        return ()
    found: list[tuple[str, int]] = []
    candidates = sorted(
        p
        for p in tests_dir.rglob("*.py")
        if _probe(p) == "file"
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
                f"{relative}: expected {EXACTLY_ONE} SitePlugin subclass, found {len(classes)} "
                f"({found}). A plugin module holds one plugin class."
            ),
        )
    (klass,) = classes
    package = module.split(".")[0]
    aliases, aliases_line = _class_aliases(klass)
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
        aliases=aliases,
        aliases_line=aliases_line,
        session_name=_class_string(klass, "session_name"),
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
# misreading as a suite member.
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


def _class_aliases(klass: ast.ClassDef) -> tuple[tuple[str, ...] | None, int | None]:
    """The literal strings *klass*'s body assigns to ``aliases`` and the line it
    does so on: ``((), None)`` when it assigns none, and ``None`` for the strings
    when the value is not a literal tuple or list of strings."""
    for node in klass.body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets, value = [node.target.id], node.value
        else:
            continue
        if "aliases" not in targets or value is None:
            # A bare annotation (``aliases: tuple[str, ...]``) assigns nothing.
            continue
        if not isinstance(value, (ast.Tuple, ast.List)):
            return None, node.lineno
        strings = [
            e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
        return (tuple(strings) if len(strings) == len(value.elts) else None), node.lineno
    return (), None


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
    blind the consumers that never read it. The caller computes the set once
    (:func:`_requirement_set`) and passes it in, so it cannot drift from the view's
    own ``requirement_set`` within one ``read_project`` call."""
    paths = dict.fromkeys(r.path for r in requirement_set)
    parsed = {relative: _parse_requirement_file(root, relative) for relative in paths}
    statuses: dict[str, RequirementStatus] = {}
    for r in requirement_set:
        tree = parsed[r.path]
        if isinstance(tree, RequirementStatus):
            statuses[r.key] = tree
        else:
            statuses[r.key] = RequirementStatus("bound" if binds_name(tree, r.name) else "unbound")
    return statuses


def _test_files(root: Path) -> tuple[str, ...]:
    """The names of the ``*.py`` entries directly under ``policy.TESTS_DIR``, sorted;
    empty when it cannot be listed (``fixtures_tree_blocked`` reports that)."""
    try:
        with os.scandir(root / policy.TESTS_DIR) as entries:
            return tuple(sorted(e.name for e in entries if e.name.endswith(".py")))
    except OSError:
        return ()


def _suite_members(plugins: tuple[PluginView, ...]) -> tuple[PluginView, ...]:
    """The plugins with a fixtures root of their own under the tree: a suite's
    members, every plugin but the first one ``gp plugin new`` wrote."""
    return tuple(p for p in plugins if p.fixtures_root != policy.FIXTURES_TREE)


def _suite_owner(plugins: tuple[PluginView, ...]) -> PluginView | None:
    """In a suite, the plugin whose fixtures are the tree's own files (the first one
    ``gp plugin new`` wrote), or ``None``; ``None`` outside a suite."""
    if not _suite_members(plugins):
        return None
    return next((p for p in plugins if p.fixtures_root == policy.FIXTURES_TREE), None)


def _requirement_set(
    plugins: tuple[PluginView, ...], test_files: tuple[str, ...]
) -> tuple[ProjectRequirement, ...]:
    """What *plugins*' project must bind, each requirement attributed to the plugin it
    serves: ``policy.PROJECT_REQUIREMENTS`` for a project with no suite member, read
    from ``policy`` on each call. In a suite (#370), the conftest holds
    ``policy.suite_requirements`` for the plugin whose fixtures root is the tree
    itself (none when no plugin's is): a check over its fixtures alone, every
    member's directory skipped. Each member's test modules, by
    ``policy.test_module_owner`` among *test_files* over every plugin's module name
    (``policy.suite_test_module`` when it has none), each hold its own
    ``policy.member_requirements``; a file the first plugin's name wins is its own."""
    members = _suite_members(plugins)
    if not members:
        return policy.PROJECT_REQUIREMENTS
    owner = _suite_owner(plugins)
    member_modules = [module_name_for(m.entry_point) for m in members]
    shared = (
        policy.suite_requirements(owner.entry_point, member_modules) if owner is not None else ()
    )
    modules: dict[str, str | None] = {
        module_name_for(m.entry_point): m.entry_point for m in members
    }
    if owner is not None:
        modules[module_name_for(owner.entry_point)] = None
    owned: dict[str, list[str]] = {}
    for name in test_files:
        module = policy.test_module_owner(name, modules)
        entry_point = modules[module] if module is not None else None
        if entry_point is not None:
            owned.setdefault(entry_point, []).append(f"{policy.TESTS_DIR}{name}")
    member_requirements = tuple(
        r
        for m in members
        for path in owned.get(m.entry_point)
        or [policy.suite_test_module(module_name_for(m.entry_point))]
        for r in policy.member_requirements(m.entry_point, path)
    )
    return shared + member_requirements


def _member_roots(root: Path, plugins: tuple[PluginView, ...]) -> tuple[MemberRoot, ...]:
    """Each suite member's fixtures root, whether it is a directory, and what blocks
    it, the facts ``fixtures_tree_present`` and ``fixtures_tree_blocked`` hold for the
    tree (#370)."""
    found: list[MemberRoot] = []
    for member in _suite_members(plugins):
        blocked = _blocked_path(root, member.fixtures_root, kind="directory")
        present = blocked is None and _probe(root / member.fixtures_root) == "dir"
        found.append(MemberRoot(member.entry_point, member.fixtures_root, present, blocked))
    return tuple(found)


def _suite_conftest(root: Path, plugins: tuple[PluginView, ...]) -> SuiteConftestState | None:
    """In a suite with a first plugin, what its conftest holds of the fixtures check
    (``suite_conftest.read_suite_conftest``); ``None`` outside one, or when the
    conftest is missing or does not parse (the requirements report that)."""
    if _suite_owner(plugins) is None:
        return None
    tree = _parse_requirement_file(root, policy.CONFTEST_PATH)
    return read_suite_conftest(tree) if isinstance(tree, ast.Module) else None


def _parse_requirement_file(root: Path, relative: str) -> ast.Module | RequirementStatus:
    """*relative*'s tree, or the status every requirement in it takes when there is
    no tree: ``unbound`` with ``present=False`` for a path that is plainly missing,
    or ``unreadable`` with the reason (``graftpunk.devtools.plugin_check``'s module
    docstring has the complete list)."""
    blocked = _blocked_path(root, relative, kind="file")
    if blocked is not None:
        component, reason = blocked
        if component == relative:
            return RequirementStatus("unreadable", reason)
        return RequirementStatus("unreadable", reason, blocking=component, present=False)
    path = root / relative
    if _probe(path) == "absent":
        return RequirementStatus("unbound", present=False)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return RequirementStatus("unreadable", f"{NOT_VALID_UTF8} ({exc})")
    except OSError as exc:
        return RequirementStatus("unreadable", f"{CANNOT_BE_READ} ({exc.strerror or exc})")
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        return RequirementStatus("unreadable", _parse_error_message(exc))
