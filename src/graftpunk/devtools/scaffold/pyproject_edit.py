"""The one seam that edits an existing pyproject.toml's text: three known shapes, no file access.

Refuses rather than guesses on anything else: a wrong edit to someone's
build configuration is worse than a refusal with instructions (plugin
tooling spec, 2026-09-11). The caller writes the result through ``write.py``:
``write_scaffold`` for the entry point and the wheel package, and
``upgrade_project`` and ``add_command`` for the graftpunk floor, which they
raise because the code they add needs the running graftpunk (graft skill
spec, amended 2026-10-04).
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import Specifier
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from graftpunk.plugins import PLUGINS_GROUP

__all__ = [
    "CannotRaiseFloor",
    "DynamicDependencies",
    "PyprojectEditError",
    "RaisedFloor",
    "with_entry_point",
    "with_graftpunk_floor",
    "with_wheel_package",
]

_ENTRY_POINT_TABLE_RE = re.compile(
    r'(\[project\.entry-points\."graftpunk\.plugins"\]\n)((?:.*\n)*?)(?=\[|\Z)'
)
# The span between the wheel table's header and its packages key stops only at
# a line-initial table header. "[^\[]*?" stopped at any "[", so an array before
# packages (exclude = ["docs"]) made a known shape unlocatable.
_WHEEL_PACKAGES_RE = re.compile(
    r"(\[tool\.hatch\.build\.targets\.wheel\](?:(?!\n\s*\[)[\s\S])*?packages\s*=\s*)(\[[^\]]*\])"
)


class PyprojectEditError(Exception):
    """The suite's pyproject.toml cannot be edited as found: it is not valid TOML
    (raised by ``write_scaffold``), or the shape pyproject_edit.py expected was
    not found. Nothing was written."""


def _normalised(text: str) -> str:
    """*text* ending in a newline. The entry-point table's own pattern needs its last
    line terminated, so a file whose table is last and that ends without a newline
    was refused as an unknown shape."""
    return text if text.endswith("\n") or not text else text + "\n"


def _as_lf(text: str, pyproject_path: Path) -> tuple[str, bool]:
    """*text* with its line endings as LF, and whether they were all CRLF.

    The two patterns match LF lines. A file that is LF throughout is edited as it
    is; a file that is CRLF throughout is edited as LF and converted back, so
    every line keeps its ending.

    Raises:
        PyprojectEditError: *text* mixes CRLF and LF line endings. An edit could
            not keep every line's ending, and the writer's contract is byte-exact
            or refuse.
    """
    crlf = text.count("\r\n")
    if crlf == 0:
        return text, False
    if crlf == text.count("\n"):
        return text.replace("\r\n", "\n"), True
    raise PyprojectEditError(
        f"{pyproject_path} mixes CRLF and LF line endings, so I cannot edit it and keep "
        "every line as it was. Normalise its line endings (all LF or all CRLF) and run "
        "this again."
    )


def _as_crlf_if(text: str, crlf: bool) -> str:
    return text.replace("\n", "\r\n") if crlf else text


def with_entry_point(text: str, pyproject_path: Path, name: str, target: str) -> str:
    """*text* with ``name = "target"`` appended to its
    ``[project.entry-points."graftpunk.plugins"]`` table. *pyproject_path* names the
    file in a refusal and is never opened. An all-LF or all-CRLF *text* keeps its
    line endings.

    Raises:
        PyprojectEditError: *text* mixes CRLF and LF line endings, the name is
            already registered, or the table's shape cannot be located
            textually.
    """
    text, crlf = _as_lf(text, pyproject_path)
    text = _normalised(text)
    data = tomllib.loads(text)
    existing = data.get("project", {}).get("entry-points", {}).get(PLUGINS_GROUP, {})
    if name in existing:
        raise PyprojectEditError(f"Entry point '{name}' is already registered in {pyproject_path}.")
    match = _ENTRY_POINT_TABLE_RE.search(text)
    if match is None:
        raise PyprojectEditError(
            f'{pyproject_path} has no [project.entry-points."{PLUGINS_GROUP}"] table I can '
            f"locate textually. Add this line by hand:\n"
            f'{name} = "{target}"'
        )
    header, body = match.group(1), match.group(2)
    new_text = text[: match.start()] + header + _body_with_entry(body, name, target)
    return _as_crlf_if(new_text + text[match.end() :], crlf)


def _body_with_entry(body: str, name: str, target: str) -> str:
    """*body* with ``name = "target"`` appended after its last entry, ahead of
    whatever blank lines separated the table from what follows it.

    The pattern's body runs to the next table header, so it carries that
    separator. Appending past it glued the new entry to the next header, and a
    second add then read as part of that table.
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
    or *package* is already listed; that holds for any line endings, since
    nothing is rewritten. Only an edit normalises the trailing newline, and an
    edit keeps an all-LF or all-CRLF *text*'s line endings.

    Raises:
        PyprojectEditError: The wheel table uses ``include`` instead of
            ``packages``, ``packages`` exists but its array cannot be located
            textually, or an edit is needed and *text* mixes CRLF and LF line
            endings.
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
    text, crlf = _as_lf(text, pyproject_path)
    text = _normalised(text)
    match = _WHEEL_PACKAGES_RE.search(text)
    if match is None:
        raise PyprojectEditError(
            f"{pyproject_path} has [tool.hatch.build.targets.wheel].packages but I cannot "
            f'locate its array textually. Add "{package}" to it by hand.'
        )
    prefix, array = match.group(1), match.group(2)
    edited = text[: match.start()] + prefix + _append_to_array(array, package) + text[match.end() :]
    return _as_crlf_if(edited, crlf)


_PROJECT_TABLE_RE = re.compile(r"^[ \t]*\[project\][ \t]*(?:#[^\n]*)?\r?$", re.MULTILINE)
_NEXT_TABLE_RE = re.compile(r"\n[ \t]*\[")
_DEPENDENCIES_KEY_RE = re.compile(r"^[ \t]*dependencies[ \t]*=[ \t]*\[", re.MULTILINE)
_REQUIREMENT_NAME_RE = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


@dataclass(frozen=True)
class RaisedFloor:
    """The pyproject *text* with its ``graftpunk`` requirement's lower bound
    raised to the floor; *previous* is that bound's version as it was written."""

    text: str
    previous: str


@dataclass(frozen=True)
class CannotRaiseFloor:
    """The project's ``graftpunk`` requirement is in a form a lower-bound rewrite
    cannot raise. *requirement* is that requirement as written, or ``None`` when
    ``[project] dependencies`` holds none."""

    requirement: str | None


@dataclass(frozen=True)
class DynamicDependencies:
    """``[project] dependencies`` is itself listed in ``[project] dynamic``: a
    build backend supplies this project's dependencies, not the array (if any)
    in this file, so there is no ``graftpunk`` literal here to raise or name
    (graft skill spec, amended 2026-10-04)."""


def _array_literals(text: str, open_bracket: int) -> list[tuple[int, int]] | None:
    """The spans of the string literals in the one-level array opening at
    *open_bracket*, or ``None`` when the array holds anything but one-line basic
    and literal strings (a multi-line string, a nested array or table) or never
    closes."""
    spans: list[tuple[int, int]] = []
    i = open_bracket + 1
    while i < len(text):
        char = text[i]
        if char == "]":
            return spans
        if char == "#":
            end = text.find("\n", i)
            i = len(text) if end == -1 else end
            continue
        if text.startswith('"""', i) or text.startswith("'''", i) or char in "[{":
            return None
        if char in "\"'":
            j = i + 1
            while j < len(text) and text[j] not in (char, "\n"):
                j += 2 if char == '"' and text[j] == "\\" else 1
            if j >= len(text) or text[j] != char:
                return None
            spans.append((i, j + 1))
            i = j + 1
            continue
        i += 1
    return None


def _dependency_literals(text: str) -> list[tuple[int, int]] | None:
    """The spans of ``[project] dependencies``'s string literals in *text*, or
    ``None`` when that array cannot be located textually."""
    table = _PROJECT_TABLE_RE.search(text)
    if table is None:
        return None
    following = _NEXT_TABLE_RE.search(text, table.end())
    end = len(text) if following is None else following.start()
    key = _DEPENDENCIES_KEY_RE.search(text, table.end(), end)
    if key is None:
        return None
    return _array_literals(text, key.end() - 1)


def _is_graftpunk(requirement: str) -> bool:
    match = _REQUIREMENT_NAME_RE.match(requirement)
    return match is not None and canonicalize_name(match.group(1)) == "graftpunk"


# Operators whose version is a lower bound (or the one version an exact pin or
# compatible-release clause allows): a specifier using one of these, at or
# above the floor, excludes everything below the floor by itself, regardless
# of any other specifier alongside it (an upper bound included).
_LOWER_BOUND_OPERATORS = frozenset({">=", ">", "==", "===", "~="})


def _excludes_everything_below(specifiers: list[Specifier], floor: Version) -> bool:
    """Whether some specifier in *specifiers* already guarantees every version
    it allows is at or above *floor*, so raising the requirement would be
    nothing extra (graft skill spec, amended 2026-10-04). A specifier whose
    version :mod:`packaging.version` cannot parse (only ``===``, arbitrary
    equality, permits that) does not count: it answers nothing, it does not
    refuse the requirement, which is for the caller to decide."""
    for spec in specifiers:
        if spec.operator not in _LOWER_BOUND_OPERATORS:
            continue
        version = spec.version
        if spec.operator == "==" and version.endswith(".*"):
            version = version.removesuffix(".*")
        try:
            if Version(version) >= floor:
                return True
        except InvalidVersion:
            continue
    return False


def with_graftpunk_floor(
    text: str, floor: str
) -> RaisedFloor | CannotRaiseFloor | DynamicDependencies | None:
    """*text* with its ``[project] dependencies`` ``graftpunk`` requirement's lower
    bound raised to *floor*, when that requirement's only version specifier is a
    ``>=`` below it. Only the version is rewritten: the name, extras, spacing, and
    environment marker stay byte for byte, and so do the file's line endings.

    Returns ``None`` when the requirement already allows nothing below *floor*:
    any specifier with operator ``>=``, ``>``, ``==``, ``===``, or ``~=`` whose
    version (for ``==X.*``, ``X``) is at or above *floor*, however many other
    specifiers (an upper bound included) sit alongside it. Returns
    :class:`DynamicDependencies` when ``"dependencies"`` is listed in
    ``[project] dynamic``: there is no array here to hold a literal to raise
    or name, whatever ``[project] dependencies`` itself happens to say.
    Returns :class:`CannotRaiseFloor` for any other form that allows a release
    below *floor*: no ``graftpunk`` requirement, several of them not all of
    which exclude everything below the floor, a bare upper bound, several
    specifiers none of which excludes everything below the floor, a URL, or a
    literal this module cannot locate textually.
    """
    project = tomllib.loads(text).get("project", {})
    dynamic = project.get("dynamic", [])
    if isinstance(dynamic, list) and "dependencies" in dynamic:
        return DynamicDependencies()
    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list):
        return CannotRaiseFloor(requirement=None)
    found = [
        (index, raw)
        for index, raw in enumerate(dependencies)
        if isinstance(raw, str) and _is_graftpunk(raw)
    ]
    if not found:
        return CannotRaiseFloor(requirement=None)
    if len(found) > 1:
        joined = " and ".join(raw for _, raw in found)
        try:
            requirements = [Requirement(raw) for _, raw in found]
        except InvalidRequirement:
            return CannotRaiseFloor(requirement=joined)
        floor_version = Version(floor)
        if all(_excludes_everything_below(list(r.specifier), floor_version) for r in requirements):
            return None
        return CannotRaiseFloor(requirement=joined)
    index, raw = found[0]
    cannot = CannotRaiseFloor(requirement=raw)
    try:
        requirement = Requirement(raw)
    except InvalidRequirement:
        return cannot
    specifiers = list(requirement.specifier)
    if requirement.url is not None:
        return cannot
    if _excludes_everything_below(specifiers, Version(floor)):
        return None
    if len(specifiers) != 1 or specifiers[0].operator != ">=":
        return cannot
    previous = specifiers[0].version
    spans = _dependency_literals(text)
    if spans is None or len(spans) != len(dependencies):
        return cannot
    start, end = spans[index]
    literal = text[start:end]
    if tomllib.loads(f"value = {literal}")["value"] != raw:
        return cannot
    # A marker's own ">=" compares a quoted value, so it never matches here.
    pattern = r">=\s*(" + re.escape(previous) + r")(?![\w.+!*-])"
    bounds = list(re.finditer(pattern, literal))
    if len(bounds) != 1:
        return cannot
    version_start, version_end = (start + offset for offset in bounds[0].span(1))
    return RaisedFloor(text=text[:version_start] + floor + text[version_end:], previous=previous)
