"""The pytest half of :mod:`graftpunk.testing`: fixture factories a generated
``tests/conftest.py`` calls.

A generated conftest imports the factories here and assigns each one's result
to a module-level name, which is what registers that fixture with pytest.
:func:`site_env_scrubber` keeps the developer's own site variables out of the
tests; :func:`fixtures_are_sanitised` checks every committed fixture against
its sidecar, and :func:`check_fixtures_tree` is that check without pytest, for
a caller that wants the report itself. The generated file carries no framework
logic of its own to drift from graftpunk's (the scaffold rule, plugin tooling
spec, 2026-09-11).

It deliberately does not name this module in ``pytest_plugins``: that would ask
pytest to rewrite assertions in a module the import has already loaded, which
it warns about, and this module has no hooks of its own to register that way.
``fixtures_are_sanitised`` is handed the fixtures tree by the conftest, which
declares it as ``FIXTURES_TREE``; nothing here imports ``graftpunk.devtools``
(graft skill spec, 2026-09-21).
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest

from graftpunk.contracts import current_schema
from graftpunk.testing.sidecar import (
    FIXTURES_PLACEHOLDER,
    Sidecar,
    SidecarError,
    is_sidecar,
    load_sidecar,
    sidecar_path,
    sidecar_scannable_text,
)

__all__ = [
    "FixturesTreeReport",
    "check_fixtures_tree",
    "fixtures_are_sanitised",
    "site_env_scrubber",
]


def site_env_scrubber(prefix: str) -> Any:
    """An autouse fixture that removes every env var starting with *prefix*
    for the duration of each test, and restores them afterward.

    A generated plugin's ``tests/conftest.py`` sets
    ``scrub_site_env = site_env_scrubber("<NAME>_")`` so a developer's own
    ``MYSHOP_USERNAME``/``MYSHOP_PASSWORD`` in the ambient environment never
    leaks into a test asserting on the plugin's behaviour without them.
    """

    @pytest.fixture(autouse=True)
    def _scrub() -> Iterator[None]:
        saved = {k: v for k, v in os.environ.items() if k.startswith(prefix)}
        for key in saved:
            del os.environ[key]
        try:
            yield
        finally:
            os.environ.update(saved)

    return _scrub


@dataclass(frozen=True)
class FixturesTreeReport:
    """What :func:`check_fixtures_tree` found: every problem, and the two counts."""

    problems: tuple[str, ...]
    verified: int
    declared: int

    @property
    def summary(self) -> str:
        """The line the check prints on every run, so the declared count shows in review."""
        return (
            f"fixtures_are_sanitised: {self.verified} fixture(s) verified against their "
            f"capture, {self.declared} accepted on declaration (no capture hash: "
            f"trusted, not checked)"
        )


PathKind = Literal["absent", "file", "dir", "other", "unreadable", "dangling"]


def _stat_kind(
    path: Path,
    detail: list[str] | None = None,
    stat_result: list[os.stat_result] | None = None,
) -> PathKind:
    """What is at *path*, from ``path.stat()`` (and ``path.lstat()`` where a
    symlink must be told apart from its unsearchable directory) inside
    ``try``/``except OSError``: the one place in this module allowed to ask
    the filesystem what is at a path or what kind of thing it is. That
    covers any ``is_*`` predicate call (``.is_dir()``, ``.is_fifo()``,
    ``.is_mount()``, and every other one pathlib has or ever adds), the
    exact calls ``.exists()``, ``.lexists()``, ``.stat()``, ``.lstat()``,
    ``.samefile()``, and ``.access()``, and any ``os.path`` function other
    than the pure string ones (``join``, ``basename``, ``dirname``,
    ``splitext``, ``split``, ``normpath``, ``relpath``, ``sep``), whether
    reached as ``os.path.whatever(...)`` or imported by name and called bare
    (``test_only_stat_kind_calls_exists_is_dir_is_file_is_symlink_lstat_or_stat``
    enforces this by AST), so every other "what is at this path" question in
    the fixtures check goes through this function instead of a
    version-dependent pathlib predicate (before Python 3.14, ``is_dir()`` and
    ``is_file()`` raise on a permission error past the path; from 3.14 they
    swallow it and answer ``False``, which reads as "missing").

    ``"absent"``: *path* plainly does not exist (``FileNotFoundError`` or
    ``NotADirectoryError``, which also covers a race between a caller's
    listing and this stat), and is not a symlink either (its own
    ``lstat()`` fails the same way). ``"dangling"``: *path* is a symlink
    whose own ``lstat()`` succeeds but whose target cannot be reached
    (``FileNotFoundError`` from ``stat()``, a dangling target, or
    ``ELOOP``, a loop): there is something at *path*, just not a directory
    or a file, which a caller walking entries skips the same way as
    ``"absent"`` but a caller checking a single path (the fixtures tree
    itself) reports apart from a plain "nothing there" so its advice does
    not ask for a directory to be created where a symlink already sits.
    ``"dir"`` or ``"file"``: a directory or a regular file, a symlink to
    one included. ``"other"``: *path* resolves to neither a directory nor
    a regular file, or is a symlink whose target raised some other
    ``OSError`` while *path* itself could still be ``lstat``'d: the
    symlink's target is what is wrong, not *path*'s own directory.
    ``"unreadable"``: ``stat()`` and ``lstat()`` both raised, which can
    only mean an ancestor of *path* is untraversable (most often a
    permission error on a directory without its execute bit). When *detail*
    is given and the result is the symlink-target form of ``"other"`` or is
    ``"unreadable"``, ``exc.strerror`` (or the exception itself when the
    platform gives none) is appended to it, so a caller that wants the
    reason for a message does not stat *path* again. When *stat_result* is
    given and the result is ``"dir"``, the ``os.stat_result`` itself is
    appended to it, so a caller building a visited-directory set for cycle
    detection (``(st_dev, st_ino)``) does not stat *path* again either."""
    try:
        found = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        try:
            path.lstat()
        except OSError:
            # path itself (or a parent) is gone between a caller's listing
            # and this stat: not a problem, just not there.
            return "absent"
        # lstat succeeded where stat did not: path is a symlink with
        # nothing at the far end.
        return "dangling"
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            try:
                path.lstat()
            except OSError:
                return "absent"
            # lstat succeeded: path is a symlink whose target cannot be
            # resolved because it loops back on itself.
            return "dangling"
        try:
            path.lstat()
        except OSError:
            # path's own directory entry cannot even be examined: an
            # ancestor (most often the containing directory's search
            # permission) is what is wrong, not path's own target.
            if detail is not None:
                detail.append(exc.strerror or str(exc))
            return "unreadable"
        # lstat succeeded where stat did not: path itself is reachable, so
        # it is a symlink whose target could not be reached. The symlink
        # itself is what is wrong, not the directory it sits in.
        if detail is not None:
            detail.append(exc.strerror or str(exc))
        return "other"
    if stat.S_ISDIR(found.st_mode):
        if stat_result is not None:
            stat_result.append(found)
        return "dir"
    if stat.S_ISREG(found.st_mode):
        return "file"
    return "other"


def _missing_sidecar(relative: Path, sidecar_name: str) -> str:
    schema = current_schema("sidecar")
    return (
        f"{relative}: no sidecar ({sidecar_name}). For a fixture derived from a capture, "
        f"copy the sidecar gp observe fixtures wrote beside the capture. For a hand-made "
        f'fixture, write {sidecar_name} with "schema": {schema}, a status, a content type, '
        f'"body_params": [], "capture_sha256": null, "flagged_names": [], and '
        f'"redacted_names": 0, which declares that the file came off no account.'
    )


def _label(tree: Path, relative: Path) -> Path:
    """The path to name in a problem line: *tree* itself, as the caller gave
    it, when *relative* is its own root (``Path(".")``, which otherwise
    reads as the working directory rather than the fixtures tree); the
    relative path for anything under it."""
    return tree if relative == Path(".") else relative


def _collect_fixtures(tree: Path, tree_stat: os.stat_result) -> tuple[list[Path], list[str]]:
    """The committed fixtures under *tree*, sorted, and one problem line for
    each directory that cannot be listed (no read permission) or whose
    entries cannot be told apart from a subdirectory (listed, but no search
    permission to stat them): ``os.walk``'s own classification of an entry
    as a file or a directory needs neither, so the failure surfaces only
    when this function stats an entry itself. A directory-wide failure (the
    directory, or an ancestor, is untraversable) stops the walk of that
    directory's remaining entries; no fixture under it is trusted. A single
    symlink entry whose own target cannot be reached, with the directory
    itself still searchable, is that one entry's problem instead, and the
    walk goes on to its siblings. A dotfile (any path component starting
    with ``.``) is pruned before it is recursed into, so it and everything
    under it are skipped.

    The walk follows a symlink to a directory (``followlinks=True``), so a
    fixtures directory committed elsewhere and symlinked in (or a single
    plugin's fixtures directory symlinked at a shared location) is still
    walked, the same as a plain subdirectory. *tree_stat* (the caller's own
    stat of *tree*, already taken to confirm it is a directory) seeds a set
    of every directory's ``(st_dev, st_ino)`` visited so far; a dirnames
    entry whose identity is already in that set, reached a second time
    through a different symlink or a symlink cycle, is pruned before the
    walk would recurse into it again."""
    problems: list[str] = []
    fixtures: list[Path] = []
    visited = {(tree_stat.st_dev, tree_stat.st_ino)}

    def onerror(exc: OSError) -> None:
        relative = Path(exc.filename).relative_to(tree)
        problems.append(f"{_label(tree, relative)}: cannot be read ({exc.strerror or exc}).")

    for dirpath, dirnames, filenames in os.walk(tree, onerror=onerror, followlinks=True):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        directory = Path(dirpath)
        relative_dir = directory.relative_to(tree)
        kept_dirnames: list[str] = []
        blocked = False
        for name in dirnames:
            child_stat: list[os.stat_result] = []
            child_detail: list[str] = []
            child_kind = _stat_kind(directory / name, child_detail, child_stat)
            if child_kind == "unreadable":
                # directory itself (not the child, which os.walk would try
                # to descend into next) lacks the search permission needed
                # to tell its entries apart: one line for it, and nothing
                # under it is trusted.
                problems.append(
                    f"{_label(tree, relative_dir)}: cannot be read ({child_detail[0]})."
                )
                blocked = True
                break
            if child_kind == "dir" and child_stat:
                identity = (child_stat[0].st_dev, child_stat[0].st_ino)
                if identity in visited:
                    continue
                visited.add(identity)
            kept_dirnames.append(name)
        if blocked:
            dirnames[:] = []
            continue
        dirnames[:] = kept_dirnames
        for name in filenames:
            if name.startswith("."):
                continue
            candidate = directory / name
            detail: list[str] = []
            kind = _stat_kind(candidate, detail)
            if kind == "absent" or kind == "dangling":
                # A dangling or looping symlink, or an entry gone between
                # the listing and the stat: not a fixture, not a problem.
                continue
            if kind == "unreadable":
                problems.append(f"{_label(tree, relative_dir)}: cannot be read ({detail[0]}).")
                break
            if kind == "other" and detail:
                # The entry is a symlink whose own target could not be
                # reached; the directory itself is fine. A sidecar in this
                # shape is the owning fixture's problem to report, once,
                # when it is probed for its sidecar below; anything else is
                # this entry's own line, and the walk continues either way.
                if not is_sidecar(candidate):
                    problems.append(f"{relative_dir / name}: cannot be read ({detail[0]}).")
                continue
            if kind == "file" and not is_sidecar(candidate):
                fixtures.append(candidate)
    return sorted(fixtures), problems


def check_fixtures_tree(tree: Path) -> FixturesTreeReport:
    """Check every fixture anywhere under *tree* against its sidecar.

    Catches a missing tree, a directory under it that cannot be listed or
    whose entries cannot be stated, a fixture with no sidecar, a sidecar
    outside its declared format (through the owner,
    :mod:`graftpunk.testing.sidecar`), a fixture that cannot be read, a
    fixture that is byte for byte its capture, and a flagged cookie or token
    name in a fixture or in its sidecar's other fields. It does not judge
    whether invented content is invented well. A sidecar with no capture hash
    is the author's declaration that the file came off no account; it is
    trusted, not checked, and counted. A dotfile (any path component starting
    with ``.``, which covers ``FIXTURES_PLACEHOLDER`` itself, a macOS
    ``.DS_Store``, and an editor swap file) is never a fixture and is
    skipped.
    """
    tree_detail: list[str] = []
    tree_stat: list[os.stat_result] = []
    tree_kind = _stat_kind(tree, tree_detail, tree_stat)
    if tree_kind != "dir":
        if tree_kind == "absent":
            message = (
                f"{tree}: the fixtures tree does not exist. The generated conftest names "
                f"it as FIXTURES_TREE; gp plugin new creates it with a {FIXTURES_PLACEHOLDER}, and "
                f"gp plugin upgrade creates it for a project that lacks it. Run "
                f"gp plugin upgrade, or recreate the directory by hand."
            )
        elif tree_detail:
            message = f"{tree}: cannot be read ({tree_detail[0]})."
        else:
            message = f"{tree}: not a directory. Move it aside, then run gp plugin upgrade."
        return FixturesTreeReport(problems=(message,), verified=0, declared=0)
    fixtures, problems = _collect_fixtures(tree, tree_stat[0])
    verified = declared = 0
    for fixture in fixtures:
        relative = fixture.relative_to(tree)
        meta = sidecar_path(fixture)
        meta_detail: list[str] = []
        meta_kind = _stat_kind(meta, meta_detail)
        if meta_kind != "file":
            sidecar_relative = relative.parent / meta.name
            if meta_detail:
                problems.append(f"{sidecar_relative}: cannot be read ({meta_detail[0]}).")
            elif meta_kind in ("dir", "other", "dangling"):
                problems.append(
                    f"{sidecar_relative}: not a regular file. Move it aside and write the sidecar."
                )
            else:
                problems.append(_missing_sidecar(relative, meta.name))
            continue
        try:
            sidecar = load_sidecar(meta)
        except SidecarError as exc:
            problems.append(f"{relative}: {exc}")
            continue
        try:
            body = fixture.read_bytes()
        except OSError as exc:
            problems.append(f"{relative}: cannot be read ({exc.strerror or exc}).")
            continue
        if sidecar.declared:
            declared += 1
        else:
            verified += 1
            if hashlib.sha256(body).hexdigest() == sidecar.capture_sha256:
                problems.append(
                    f"{relative}: an unchanged copy of its capture. A fixture keeps the "
                    f"capture's structure and invents every value."
                )
        problems.extend(_flagged_name_problems(relative, meta.name, sidecar, body))
    return FixturesTreeReport(problems=tuple(problems), verified=verified, declared=declared)


def _flagged_name_problems(
    relative: Path, meta_name: str, sidecar: Sidecar, body: bytes
) -> list[str]:
    """One problem per flagged name found in the fixture's body or in a sidecar field
    other than ``flagged_names``. Matched case-insensitively: an HTTP header name is
    case-insensitive by the protocol, and a capture recorded over HTTP/2 lowercases
    every header name, so a flagged ``X-Csrf-Token`` copied into a fixture as
    ``x-csrf-token`` still counts."""
    problems: list[str] = []
    text = body.decode("utf-8", errors="replace").casefold()
    rest = sidecar_scannable_text(sidecar).casefold()
    for name in sidecar.flagged_names:
        if not name:
            # An empty string is a substring of every fixture; a sidecar
            # holding one would flag every file it checks.
            continue
        folded = name.casefold()
        if folded in text:
            problems.append(
                f"{relative}: contains the flagged name {name!r}, a cookie or token "
                f"name the capture's digest recorded; rename it in the fixture."
            )
        if folded in rest:
            problems.append(
                f"{relative.parent / meta_name}: carries the flagged name {name!r} "
                f"outside flagged_names."
            )
    return problems


def fixtures_are_sanitised(tree: Path | str) -> Any:
    """An autouse, session-scoped fixture that fails the run unless every fixture under
    *tree* passes :func:`check_fixtures_tree`.

    A generated ``tests/conftest.py`` sets
    ``sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)``; the assignment is
    what registers it. It prints :attr:`FixturesTreeReport.summary` once per run,
    and on a problem it fails every test with the list of problems.
    """
    root = Path(tree)

    @pytest.fixture(scope="session", autouse=True)
    def _sanitised(request: pytest.FixtureRequest) -> None:
        report = check_fixtures_tree(root)
        reporter = request.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(report.summary)
        if report.problems:
            listing = "\n".join(f"  {problem}" for problem in report.problems)
            pytest.fail(f"fixtures_are_sanitised:\n{listing}", pytrace=False)

    return _sanitised
