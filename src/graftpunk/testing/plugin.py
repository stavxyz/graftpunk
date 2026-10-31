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

From sidecar schema 4 a sidecar names the observe run its capture came from, and
when that run is on this machine (a developer's checkout, never CI) the check
also compares the fixture with every value the run recorded for its flagged
names (:mod:`graftpunk.testing.recorded`). The run is found by the lookup
``gp observe`` uses (:func:`graftpunk.observe.storage.run_dir_of`) under
``graftpunk.observe.OBSERVE_BASE_DIR``, and only that run's own files are read.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest

import graftpunk.observe
from graftpunk.contracts import current_schema
from graftpunk.observe.storage import run_dir_of
from graftpunk.testing.decoding import decoded_forms, whole_name_regex
from graftpunk.testing.recorded import (
    RunUnreadableError,
    read_run_har,
    recorded_leaks,
    recorded_values,
)
from graftpunk.testing.sidecar import (
    FIXTURES_PLACEHOLDER,
    ObserveRun,
    Sidecar,
    SidecarError,
    is_sidecar,
    load_sidecar,
    sidecar_path,
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
    """What :func:`check_fixtures_tree` found: every problem, and the counts."""

    problems: tuple[str, ...]
    verified: int
    declared: int
    # Fixtures also compared with the values their observe run recorded: the run
    # was on this machine. Zero in CI, where no run is.
    run_checked: int = 0

    @property
    def summary(self) -> str:
        """The line the check prints on every run, so the declared count shows in
        review, and whether the local value check ran."""
        return (
            f"fixtures_are_sanitised: {self.verified} fixture(s) verified against their "
            f"capture, {self.declared} accepted on declaration (no capture hash: "
            f"trusted, not checked), {self.run_checked} compared with the values their "
            f"observe run recorded on this machine"
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
        f'"body_params": [], "capture_sha256": null, "flagged_names": [], '
        f'"flagged_reasons": {{}}, "checked_names": [], "observe_run": null, and '
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
                # the listing and the stat: not a fixture. A sidecar in this
                # shape is reported once, by its fixture's sidecar probe below.
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


def check_fixtures_tree(tree: Path, *, observe_dir: Path | None = None) -> FixturesTreeReport:
    """Check every fixture anywhere under *tree* against its sidecar.

    Catches a missing tree, a directory under it that cannot be listed or
    whose entries cannot be stated, a fixture with no sidecar, a sidecar
    outside its declared format (through the owner,
    :mod:`graftpunk.testing.sidecar`), a fixture that cannot be read, a
    fixture that is byte for byte its capture, and a flagged cookie or token
    name in a fixture, a cookie name only where the capture held its value or held
    the name other than in a name-value pair holding nothing (the sidecar's ``checked_names``;
    every flagged name for a sidecar from before schema 3).
    It does not judge
    whether invented content is invented well. A sidecar with no capture hash
    is the author's declaration that the file came off no account (or gp's,
    for an empty capture, which holds nothing to sanitise); it is trusted,
    not checked, and counted. A dotfile (any path component starting
    with ``.``, which covers ``FIXTURES_PLACEHOLDER`` itself, a macOS
    ``.DS_Store``, and an editor swap file) is never a fixture and is
    skipped.

    A sidecar that names its observe run (schema 4) has the fixture also compared
    with every value that run recorded for its flagged names, when the run is
    under *observe_dir* (``graftpunk.observe.OBSERVE_BASE_DIR`` when None): a
    match is a problem naming the flagged name and the fixture's lines, never the
    value. A run that is not there (CI) is skipped without a word, beyond the
    summary's count. A run that is there but cannot be read is one problem, since
    a check that quietly did not run reads as one that passed; the name check
    still applies to its fixtures. Only the run's own files are read
    (:meth:`_RunValues.leaks`).
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
    runs = _RunValues(
        observe_dir if observe_dir is not None else graftpunk.observe.OBSERVE_BASE_DIR
    )
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
        problems.extend(_flagged_name_problems(relative, sidecar, body))
        if sidecar.observe_run is not None:
            problems.extend(runs.leaks(relative, sidecar.observe_run, sidecar, body))
    problems.extend(runs.unreadable_problems())
    return FixturesTreeReport(
        problems=tuple(problems), verified=verified, declared=declared, run_checked=runs.checked
    )


class _RunValues:
    """The local value check: each observe run a sidecar names, looked up once
    under *base* by :func:`graftpunk.observe.storage.run_dir_of` and read once,
    and the fixtures compared with it."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.checked = 0
        # Per run: (its directory, its parsed HAR); None when it is not on this
        # machine; the reason, when it is there and cannot be read.
        self._runs: dict[ObserveRun, tuple[Path, object] | str | None] = {}
        self._values: dict[tuple[ObserveRun, tuple[str, ...]], dict[str, set[str]]] = {}
        # Per unreadable run: the first fixture whose sidecar names it, and how many do.
        self._unreadable: dict[ObserveRun, tuple[Path, int]] = {}

    def _run(self, run: ObserveRun) -> tuple[Path, object] | str | None:
        if run in self._runs:
            return self._runs[run]
        run_dir = run_dir_of(run.session, run.run, base_dir=self.base)
        detail: list[str] = []
        kind = _stat_kind(run_dir, detail)
        found: tuple[Path, object] | str | None
        if kind == "absent":
            found = None
        elif kind == "dir":
            try:
                found = (run_dir, read_run_har(run_dir))
            except RunUnreadableError as exc:
                found = str(exc)
        elif detail:
            found = detail[0]
        elif kind == "dangling":
            found = "a symlink to nothing"
        else:
            found = "not a directory"
        self._runs[run] = found
        return found

    def leaks(self, relative: Path, run: ObserveRun, sidecar: Sidecar, body: bytes) -> list[str]:
        """One problem per flagged name whose value *run* recorded is in *body*,
        or none when the run is not on this machine. An unreadable run is
        reported once, by :meth:`unreadable_problems`."""
        found = self._run(run)
        if found is None:
            return []
        if isinstance(found, str):
            first, count = self._unreadable.get(run, (relative, 0))
            self._unreadable[run] = (first, count + 1)
            return []
        run_dir, har = found
        names = tuple(name for name in sidecar.flagged_names if name)
        key = (run, names)
        if key not in self._values:
            self._values[key] = recorded_values(har, names, run_dir)
        self.checked += 1
        text = body.decode("utf-8", errors="replace")
        return [
            f"{relative}: {line}, as its observe run {run.session}/{run.run} on this "
            f"machine recorded it; invent a value in its place."
            for line in recorded_leaks(text, self._values[key])
        ]

    def unreadable_problems(self) -> list[str]:
        """One problem per run a sidecar names that is on this machine and cannot
        be read."""
        problems: list[str] = []
        for run, (first, count) in self._unreadable.items():
            reason = self._runs[run]
            others = f" and {count - 1} more" if count > 1 else ""
            problems.append(
                f"{first}{others}: the observe run {run.session}/{run.run} its sidecar names "
                f"is under {self.base} but cannot be read ({reason}), so the fixture was not "
                f"compared with the values the run recorded. Make the run readable, or move "
                f"its directory out of {self.base} to have the fixture checked by its names "
                f"alone, as in CI."
            )
        return problems


def _flagged_name_pattern(name: str) -> re.Pattern[str]:
    """*name*, casefolded, as a whole name
    (:func:`graftpunk.testing.decoding.whole_name_regex`): a flagged ``text``
    matches ``"text":``, ``text=``, ``text.`` at a sentence's end, and ``$text``,
    but not ``context``, ``text-align`` or ``text.v2``."""
    return re.compile(whole_name_regex(name))


def _flagged_name_problems(relative: Path, sidecar: Sidecar, body: bytes) -> list[str]:
    """One problem per name to check (:attr:`Sidecar.names_to_check`) found in the
    fixture's body. A schema 3 sidecar names every flagged token name, and each
    flagged cookie name whose recorded value the capture held, that the capture
    held anywhere but in a name-value pair whose value holds nothing, or whose
    values gp did not know in full
    (:func:`graftpunk.devtools.captures.checked_names_of`). So an honest
    ``"id": 7`` key passes when a recording sent a cookie named ``id`` and its
    capture held ``id`` only as keys of numbers or form state. A schema 1 or 2 sidecar
    records no such list, so every flagged name is looked for, as before, and the
    problem says how to regenerate it.

    Found as a whole name (:func:`_flagged_name_pattern`), in the body as written
    or in any of its decoded forms
    (:func:`graftpunk.testing.decoding.decoded_forms`), so a name beside an
    escaped ``&`` or ``"`` still counts. Matched case-insensitively: an HTTP
    header name is case-insensitive by the protocol, and a capture recorded over
    HTTP/2 lowercases every header name, so a flagged ``X-Csrf-Token`` copied
    into a fixture as ``x-csrf-token`` still counts.

    No sidecar field is scanned. ``status`` and ``content_type`` are written by
    gp from the response and hold no cookie value (a media type such as
    ``text/html`` is not a leak of a cookie named ``text``); ``body_params``
    holds request field names, which legitimately include a posted form's own
    CSRF field; and the rest are the names themselves, their reasons, a digest,
    a count, and the schema number. A schema 2 sidecar's reasons are named in
    the problem, so the author can tell a form-state cookie flagged by its name
    from a session value without the recording."""
    problems: list[str] = []
    raw = body.decode("utf-8", errors="replace")
    texts = {form.casefold() for form in (raw, *decoded_forms(raw))}
    if sidecar.checked_names is None:
        regenerate = (
            " Or regenerate the sidecar with gp observe fixtures (sidecar schema 4), "
            "which leaves a flagged cookie name out where the capture held none of its "
            "values and held the name only in name-value pairs holding nothing, and "
            "names its observe run, so on a machine holding the run the check also "
            "compares the fixture with the values the run recorded."
        )
        source = "a cookie or token name the capture's digest recorded"
    else:
        regenerate = ""
        source = (
            "a token name, or a cookie name whose recorded value the capture held, "
            "that the capture held other than in a name-value pair holding nothing, "
            "or whose value the recording did not keep"
        )
    for name in sidecar.names_to_check:
        if not name:
            # An empty name would match between every two characters.
            continue
        pattern = _flagged_name_pattern(name)
        if any(pattern.search(text) for text in texts):
            why = sidecar.flagged_reasons.get(name)
            flagged_by = f" (flagged by: {', '.join(why)})" if why else ""
            problems.append(
                f"{relative}: contains the flagged name {name!r}, {source}{flagged_by}; "
                f"rename it in the fixture.{regenerate}"
            )
    return problems


def fixtures_are_sanitised(tree: Path | str, *, observe_dir: Path | str | None = None) -> Any:
    """An autouse, session-scoped fixture that fails the run unless every fixture under
    *tree* passes :func:`check_fixtures_tree`.

    A generated ``tests/conftest.py`` sets
    ``sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)``; the assignment is
    what registers it. It prints :attr:`FixturesTreeReport.summary` once per run,
    and on a problem it fails every test with the list of problems. *observe_dir*
    is where the local value check looks for a sidecar's observe run
    (:func:`check_fixtures_tree`); a generated conftest leaves it out.
    """
    root = Path(tree)
    runs = Path(observe_dir) if observe_dir is not None else None

    @pytest.fixture(scope="session", autouse=True)
    def _sanitised(request: pytest.FixtureRequest) -> None:
        report = check_fixtures_tree(root, observe_dir=runs)
        reporter = request.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(report.summary)
        if report.problems:
            listing = "\n".join(f"  {problem}" for problem in report.problems)
            pytest.fail(f"fixtures_are_sanitised:\n{listing}", pytrace=False)

    return _sanitised
