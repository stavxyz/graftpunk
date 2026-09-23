"""The one seam that edits an existing pyproject.toml's text: two known shapes, no file access.

Refuses rather than guesses on anything else: a wrong edit to someone's
build configuration is worse than a refusal with instructions (plugin
tooling spec, 2026-09-11). The caller writes the result; ``write_scaffold``
does, through ``write.py``.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

__all__ = ["PyprojectEditError", "with_entry_point", "with_wheel_package"]

_ENTRY_POINT_TABLE_RE = re.compile(
    r'(\[project\.entry-points\."graftpunk\.plugins"\]\n)((?:.*\n)*?)(?=\[|\Z)'
)
# The span between the wheel table's header and its packages key stops only at
# a line-initial table header. "[^\[]*?" stopped at any "[", so an array before
# packages (exclude = ["docs"]) made a known shape unlocatable (polish round 1,
# 2026-09-12).
_WHEEL_PACKAGES_RE = re.compile(
    r"(\[tool\.hatch\.build\.targets\.wheel\](?:(?!\n\s*\[)[\s\S])*?packages\s*=\s*)(\[[^\]]*\])"
)


class PyprojectEditError(Exception):
    """The shape pyproject_edit.py expected was not found; nothing was written."""


def _normalised(text: str) -> str:
    """*text* ending in a newline. The entry-point table's own pattern needs its last
    line terminated, so a file whose table is last and that ends without a newline
    was refused as an unknown shape (polish round 1, 2026-09-12)."""
    return text if text.endswith("\n") or not text else text + "\n"


def with_entry_point(text: str, pyproject_path: Path, name: str, target: str) -> str:
    """*text* with ``name = "target"`` appended to its
    ``[project.entry-points."graftpunk.plugins"]`` table. *pyproject_path* names the
    file in a refusal and is never opened.

    Raises:
        PyprojectEditError: The name is already registered, or the table's
            shape cannot be located textually.
    """
    text = _normalised(text)
    data = tomllib.loads(text)
    existing = data.get("project", {}).get("entry-points", {}).get("graftpunk.plugins", {})
    if name in existing:
        raise PyprojectEditError(f"Entry point '{name}' is already registered in {pyproject_path}.")
    match = _ENTRY_POINT_TABLE_RE.search(text)
    if match is None:
        raise PyprojectEditError(
            f'{pyproject_path} has no [project.entry-points."graftpunk.plugins"] table I can '
            f"locate textually. Add this line by hand:\n"
            f'{name} = "{target}"'
        )
    header, body = match.group(1), match.group(2)
    new_text = text[: match.start()] + header + _body_with_entry(body, name, target)
    return new_text + text[match.end() :]


def _body_with_entry(body: str, name: str, target: str) -> str:
    """*body* with ``name = "target"`` appended after its last entry, ahead of
    whatever blank lines separated the table from what follows it.

    The pattern's body runs to the next table header, so it carries that
    separator. Appending past it glued the new entry to the next header, and a
    second add then read as part of that table (polish round 2, 2026-09-12).
    """
    entry = f'{name} = "{target}"\n'
    entries = body.rstrip("\n")
    blank_lines = body[len(entries) :]
    if not entries:
        return f"{entry}{blank_lines}"
    # The first newline of blank_lines terminates the last existing entry, so
    # it is re-emitted before the new entry rather than kept as a separator.
    return f"{entries}\n{entry}{blank_lines[1:]}"


def _append_to_array(array_text: str, item: str) -> str:
    inner = array_text[1:-1]
    stripped = inner.rstrip()
    if not stripped.strip():
        return f'["{item}"]'
    separator = "" if stripped.endswith(",") else ","
    if "\n" in inner:
        return f'[{stripped}{separator}\n    "{item}",\n]'
    return f'[{stripped}{separator} "{item}"]'


def with_wheel_package(text: str, pyproject_path: Path, package: str) -> str:
    """*text* with *package* appended to ``[tool.hatch.build.targets.wheel]``'s
    ``packages`` array. *pyproject_path* names the file in a refusal and is never
    opened.

    Returns *text* itself, byte for byte, when there is nothing to add: the table
    has no explicit ``packages`` key (hatchling then infers packages on its own),
    or *package* is already listed. Only an edit normalises the trailing newline.

    Raises:
        PyprojectEditError: The wheel table uses ``include`` instead of
            ``packages``, or ``packages`` exists but its array cannot be
            located textually.
    """
    data = tomllib.loads(text)
    wheel = (
        data.get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {})
    )
    if "packages" not in wheel:
        if "include" in wheel:
            raise PyprojectEditError(
                f"{pyproject_path} uses [tool.hatch.build.targets.wheel].include instead of "
                f'packages. Add "{package}" to it by hand.'
            )
        return text
    if package in wheel["packages"]:
        return text
    text = _normalised(text)
    match = _WHEEL_PACKAGES_RE.search(text)
    if match is None:
        raise PyprojectEditError(
            f"{pyproject_path} has [tool.hatch.build.targets.wheel].packages but I cannot "
            f'locate its array textually. Add "{package}" to it by hand.'
        )
    prefix, array = match.group(1), match.group(2)
    return text[: match.start()] + prefix + _append_to_array(array, package) + text[match.end() :]
