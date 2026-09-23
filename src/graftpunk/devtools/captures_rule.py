"""The single owner of the "captures never enter git" rule, as text.

The directory captures go to, and the ``.gitignore`` edit that keeps them out
of git. Nothing here touches a file, so the scaffold modules, which write only
through ``write.py``, import the rule from here. The fixtures command, the
scaffold's generated ``.gitignore``, and the scaffold's suite mode all use it,
so the default directory and the ignore line cannot disagree (plugin tooling
spec, 2026-09-11). ``graftpunk.devtools.captures`` applies the edit to disk for
``gp observe fixtures`` (graft skill spec, 2026-09-21).
"""

from __future__ import annotations

__all__ = ["CAPTURES_DIR", "with_ignored"]

CAPTURES_DIR = "tests/captures"


def with_ignored(text: str, relative: str) -> str:
    """*text*, a ``.gitignore``'s content, with ``<relative>/`` appended unless that
    exact line is there, in which case *text* itself. The rule ``ensure_ignored``
    applies, as a text function a scaffold writer can plan as a change."""
    if relative.rstrip("/") in {entry.strip().rstrip("/") for entry in text.splitlines()}:
        return text
    separator = "\n" if text and not text.endswith("\n") else ""
    return f"{text}{separator}{relative.rstrip('/')}/\n"
