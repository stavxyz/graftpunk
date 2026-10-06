"""Print every value from a capture that is still in the fixture derived from it.

Usage: fixture-leaks.py CAPTURE FIXTURE [SIDECAR]

The graft skill writes each fixture from a capture on the developer's workstation,
inventing every value. This reports what survived, so the skill can rewrite it: each
string leaf of a JSON capture (each text node and attribute value of any other text
capture) at least three characters long, and each run of three or more digits, that
appears anywhere in the fixture's text. A string the command branches or selects on
(a status, a currency code, a class name) may be kept on purpose; the skill decides,
so this reports and never edits. With SIDECAR, it also prints the names the sidecar
lists, since a sidecar is committed and the guide asks for both lists to be read.

Exit 0 when nothing survived, 1 when something did, 2 on a usage or read error.
Standard library only, so it runs under any Python 3.9 or later.
"""

from __future__ import annotations

import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

_DIGITS = re.compile(r"\d{3,}")
_MIN_STRING = 3


class _TextAndAttributes(HTMLParser):
    """Text nodes and attribute values of a non-JSON capture, the parts a site fills in."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[str] = []

    def handle_data(self, data: str) -> None:
        self.values.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.values.extend(value for _name, value in attrs if value)


def _json_leaves(node: object) -> list[str]:
    if isinstance(node, dict):
        return [leaf for value in node.values() for leaf in _json_leaves(value)]
    if isinstance(node, list):
        return [leaf for value in node for leaf in _json_leaves(value)]
    if isinstance(node, bool) or node is None:
        return []
    return [str(node)]


def captured_values(text: str) -> list[str]:
    """The values in *text* worth looking for in a fixture, in first-seen order."""
    try:
        raw = _json_leaves(json.loads(text))
    except ValueError:
        parser = _TextAndAttributes()
        parser.feed(text)
        raw = parser.values
    found: dict[str, None] = {}
    for value in raw:
        value = value.strip()
        if len(value) >= _MIN_STRING and not value.isdigit():
            found[value] = None
        for run in _DIGITS.findall(value):
            found[run] = None
    return list(found)


def survivors(capture: str, fixture: str) -> list[str]:
    """Each captured value that still appears in *fixture*."""
    return [value for value in captured_values(capture) if value in fixture]


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print("usage: fixture-leaks.py CAPTURE FIXTURE [SIDECAR]", file=sys.stderr)
        return 2
    try:
        capture = Path(argv[1]).read_text(encoding="utf-8")
        fixture = Path(argv[2]).read_text(encoding="utf-8")
        sidecar = json.loads(Path(argv[3]).read_text(encoding="utf-8")) if len(argv) == 4 else None
    except (OSError, ValueError) as exc:
        print(f"fixture-leaks: cannot read the inputs: {exc}", file=sys.stderr)
        return 2
    left = survivors(capture, fixture)
    for value in left:
        print(f"still in the fixture: {value!r}")
    if sidecar is not None:
        for key in ("body_params", "flagged_names"):
            names = sidecar.get(key) or []
            print(f"sidecar {key}: {', '.join(map(str, names)) if names else '(none)'}")
    if not left:
        print("fixture-leaks: no captured value is left in the fixture.")
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
