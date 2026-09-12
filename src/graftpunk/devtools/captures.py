"""The single owner of the 'captures never enter git' rule.

The fixtures command, the scaffold's generated ``.gitignore``, and the
scaffold's suite mode all use this module, so the default directory and the
ignore line cannot disagree (plugin tooling spec, 2026-09-11).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from graftpunk.logging import get_logger

LOG = get_logger(__name__)

CAPTURES_DIR = "tests/captures"

__all__ = ["CAPTURES_DIR", "ensure_ignored", "find_repo_root", "is_tracked"]

_GIT_TIMEOUT_SECONDS = 10


def find_repo_root(start: Path) -> Path | None:
    """The git work tree's root containing *start*, or ``None`` outside one
    (including when ``git`` is not on ``PATH``)."""
    argv = ["git", "rev-parse", "--show-toplevel"]
    try:
        result = subprocess.run(  # noqa: S603 - argv is a fixed git invocation, not untrusted input
            argv,
            cwd=start,
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
    """Add *relative* to the root ``.gitignore`` if it is not already covered.

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
