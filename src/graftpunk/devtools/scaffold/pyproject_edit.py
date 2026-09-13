"""The one seam that edits an existing pyproject.toml: textual, two known shapes only.

Refuses rather than guesses on anything else: a wrong edit to someone's
build configuration is worse than a refusal with instructions (plugin
tooling spec, 2026-09-11).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

__all__ = ["PyprojectEditError", "add_entry_point", "add_wheel_package"]

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


def _read_normalised(pyproject_path: Path) -> str:
    """*pyproject_path*'s text, ending in a newline.

    The entry-point table's own pattern needs its last line terminated, so a
    file whose table is last and that ends without a newline was refused as an
    unknown shape (polish round 1, 2026-09-12). The normalised text is what gets
    written back.
    """
    text = pyproject_path.read_text(encoding="utf-8")
    return text if text.endswith("\n") or not text else text + "\n"


class PyprojectEditError(Exception):
    """The shape pyproject_edit.py expected was not found; nothing was written."""


def add_entry_point(pyproject_path: Path, name: str, target: str) -> None:
    """Append ``name = "target"`` to the ``[project.entry-points."graftpunk.plugins"]`` table.

    Raises:
        PyprojectEditError: The name is already registered, or the table's
            shape cannot be located textually.
    """
    text = _read_normalised(pyproject_path)
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
    new_text = (
        text[: match.start()] + header + body + f'{name} = "{target}"\n' + text[match.end() :]
    )
    pyproject_path.write_text(new_text, encoding="utf-8")


def _append_to_array(array_text: str, item: str) -> str:
    inner = array_text[1:-1]
    stripped = inner.rstrip()
    if not stripped.strip():
        return f'["{item}"]'
    separator = "" if stripped.endswith(",") else ","
    if "\n" in inner:
        return f'[{stripped}{separator}\n    "{item}",\n]'
    return f'[{stripped}{separator} "{item}"]'


def add_wheel_package(pyproject_path: Path, package: str) -> None:
    """Append *package* to ``[tool.hatch.build.targets.wheel]``'s ``packages`` array.

    A no-op when the table has no explicit ``packages`` key at all (hatchling
    then infers packages on its own).

    Raises:
        PyprojectEditError: The wheel table uses ``include`` instead of
            ``packages``, or ``packages`` exists but its array cannot be
            located textually.
    """
    text = _read_normalised(pyproject_path)
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
        return
    if package in wheel["packages"]:
        return
    match = _WHEEL_PACKAGES_RE.search(text)
    if match is None:
        raise PyprojectEditError(
            f"{pyproject_path} has [tool.hatch.build.targets.wheel].packages but I cannot "
            f'locate its array textually. Add "{package}" to it by hand.'
        )
    prefix, array = match.group(1), match.group(2)
    new_text = (
        text[: match.start()] + prefix + _append_to_array(array, package) + text[match.end() :]
    )
    pyproject_path.write_text(new_text, encoding="utf-8")
