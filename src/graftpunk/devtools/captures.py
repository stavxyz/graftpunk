"""The single owner of the 'captures never enter git' rule.

The fixtures command, the scaffold's generated ``.gitignore``, and the
scaffold's suite mode all use this module, so the default directory and the
ignore line cannot disagree (plugin tooling spec, 2026-09-11). It also writes
the committable sidecar beside each capture, through the format
:mod:`graftpunk.testing.sidecar` owns.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Iterable
from pathlib import Path

from graftpunk.logging import get_logger
from graftpunk.testing.sidecar import Sidecar, sidecar_path, sidecar_text

LOG = get_logger(__name__)

CAPTURES_DIR = "tests/captures"

__all__ = ["CAPTURES_DIR", "ensure_ignored", "find_repo_root", "is_tracked", "write_sidecar"]

_GIT_TIMEOUT_SECONDS = 10


def _nearest_existing(start: Path) -> Path:
    """The closest ancestor of *start* (*start* itself when it exists) that is on disk.

    ``git`` needs a directory that exists to run in. The fixtures command's
    default target (``./tests/captures``) does not exist on a first run, and
    running git there raises ``FileNotFoundError``, which read as "not inside a
    git work tree" and let the command write unscrubbed bodies into a repo with
    nothing ignoring them (polish round 1, 2026-09-12).
    """
    probe = start
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe


def find_repo_root(start: Path) -> Path | None:
    """The git work tree's root containing *start*, or ``None`` outside one
    (including when ``git`` is not on ``PATH``).

    *start* need not exist yet: the search runs from its nearest existing
    ancestor, so a target directory this call is about to create is still
    resolved against the repository that will contain it.
    """
    argv = ["git", "rev-parse", "--show-toplevel"]
    try:
        result = subprocess.run(  # noqa: S603 - argv is a fixed git invocation, not untrusted input
            argv,
            cwd=_nearest_existing(start),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def ensure_ignored(repo_root: Path, relative: str) -> bool:
    """Add ``<relative>/`` to the root ``.gitignore`` unless that exact line is there.

    The check is exact-line only, against each line stripped of whitespace and
    of a trailing slash. It does not ask git whether the path is already
    ignored, so a pattern that covers *relative* some other way (a parent
    directory, a glob, an exclude file) still gets the explicit line.

    Returns:
        True when the line was added, False when it was already present.
    """
    gitignore = repo_root / ".gitignore"
    line = relative.rstrip("/") + "/"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    existing_lines = {entry.strip().rstrip("/") for entry in existing.splitlines()}
    if relative.rstrip("/") in existing_lines:
        return False
    with gitignore.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write(line + "\n")
    LOG.info("captures_gitignore_updated", path=str(gitignore), line=line)
    return True


def is_tracked(path: Path) -> bool:
    """True when *path* is tracked by git (``git ls-files --error-unmatch``)."""
    directory = path.parent if path.parent.exists() else Path.cwd()
    argv = ["git", "ls-files", "--error-unmatch", str(path)]
    try:
        result = subprocess.run(  # noqa: S603 - argv is a fixed git invocation, not untrusted input
            argv,
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def write_sidecar(
    fixture: Path,
    *,
    status: int,
    content_type: str,
    body_params: Iterable[str],
    flagged_names: Iterable[str],
) -> Path:
    """Write *fixture*'s committable sidecar beside it and return its path.

    *fixture* must already be on disk: ``capture_sha256`` is the hash of its bytes
    as written, which is what the in-suite check compares a derived fixture to.
    The sort and dedupe of ``body_params`` and ``flagged_names`` is
    ``Sidecar``'s own job, not this writer's.
    """
    sidecar = Sidecar(
        status=status,
        content_type=content_type,
        body_params=tuple(body_params),
        capture_sha256=hashlib.sha256(fixture.read_bytes()).hexdigest(),
        flagged_names=tuple(flagged_names),
    )
    path = sidecar_path(fixture)
    path.write_text(sidecar_text(sidecar), encoding="utf-8")
    return path
