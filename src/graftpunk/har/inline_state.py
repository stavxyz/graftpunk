"""Inline JSON state: the data object a server-rendered page carries in a script.

A React or similar page often ships its data in one inline ``<script>``, either an
assignment to a dotted name (``window.App.props = {...};``) or a JSON block
(``<script id="__NEXT_DATA__" type="application/json">{...}</script>``). Without
it, the digest reports such a page as non-JSON and the page holding a site's
results reads like navigation chrome (#271).

Two forms are read, and no other:

- a script with no ``type``, or a JavaScript ``type``, and no ``src``, holding an
  assignment whose target is a dotted name of two or more parts (``window.App``,
  ``App.props``, ``window.App.props``) and whose value starts with ``{`` and is a
  JSON object. ``var``/``let``/``const`` declarations, bare names, bracketed
  names (``window["App"]``), ``JSON.parse("...")``, and JavaScript object
  literals that are not JSON are not read;
- a ``<script type="application/json">`` whose body is a JSON object, named by
  its ``id`` (``script#__NEXT_DATA__``) or, without one, as
  ``script[type="application/json"]``. ``application/ld+json`` is search-engine
  metadata, not page state, and is not read.

An array is never reported, only an object. The object must be spelled in at
least :data:`MIN_OBJECT_CHARS` characters, so a small settings object does not
stand for the page; of those that qualify, the largest is reported, the first on
a tie.

The search is bounded and linear in the page: it examines at most
:data:`MAX_SCRIPTS` scripts, skips a script body longer than
:data:`MAX_SCRIPT_CHARS`, and decodes at most :data:`MAX_DECODES` candidate
objects per page, each within its own script body. It never executes script. A
decode that fails, or that the parser refuses for nesting too deep for it
(``RecursionError``), is skipped.

A part of the target that looks like it holds an id or a value
(:func:`graftpunk.har.paths.looks_dynamic`) is reported as a placeholder, as a
path segment is (``window.{window_id}.props``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from graftpunk.har.paths import looks_dynamic, param_name_for_segment

__all__ = [
    "MAX_DECODES",
    "MAX_SCRIPTS",
    "MAX_SCRIPT_CHARS",
    "MIN_OBJECT_CHARS",
    "InlineState",
    "inline_json",
    "pattern_for",
]

MAX_SCRIPTS = 128
"""Scripts examined per page; later ones are not read."""

MAX_SCRIPT_CHARS = 4 * 1024 * 1024
"""A script body longer than this many characters is not examined."""

MAX_DECODES = 32
"""Candidate objects decoded per page, failed decodes included."""

MIN_OBJECT_CHARS = 512
"""An object spelled in fewer characters than this is not reported."""

_SCRIPT_OPEN_RE = re.compile(r"<script\b", re.IGNORECASE)
_SCRIPT_CLOSE_RE = re.compile(r"</script", re.IGNORECASE)
_ATTRIBUTE_RE = re.compile(
    r"""(?:^|\s)(type|id|src)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.IGNORECASE
)
_IDENTIFIER = r"[A-Za-z_$][A-Za-z0-9_$]*"
# A dotted name not preceded by a name character or a dot (so never the tail of a
# longer name), an "=" that is not part of "==", and a value starting with "{".
_ASSIGNMENT_RE = re.compile(
    rf"(?<![A-Za-z0-9_$.])({_IDENTIFIER}(?:\.{_IDENTIFIER})+)\s*=\s*(?=\{{)"
)
_JS_TYPES = frozenset(
    {
        "text/javascript",
        "application/javascript",
        "application/x-javascript",
        "application/ecmascript",
        "text/ecmascript",
        "module",
    }
)
_JSON_TYPE = "application/json"
_JSON_SCRIPT_TARGET = 'script[type="application/json"]'
_DECODER = json.JSONDecoder()


@dataclass(frozen=True)
class InlineState:
    """The object a page's script holds, and the target it is reported under."""

    target: str
    value: dict[str, Any]


def _attributes(tag: str) -> dict[str, str]:
    """The ``type``, ``id``, and ``src`` attributes of an opening tag's text, by
    lower-case name; the first of each wins, as in a browser."""
    found: dict[str, str] = {}
    for match in _ATTRIBUTE_RE.finditer(tag):
        name = match.group(1).lower()
        value = next(group for group in match.groups()[1:] if group is not None)
        found.setdefault(name, value)
    return found


def _scripts(page: str) -> list[tuple[dict[str, str], str]]:
    """The first :data:`MAX_SCRIPTS` scripts of *page*: each one's attributes and
    body. A tag left open, or a body left unclosed, ends the search, since nothing
    after it can close it. Each step searches forward from the last, so the scan is
    linear in the page."""
    scripts: list[tuple[dict[str, str], str]] = []
    position = 0
    while len(scripts) < MAX_SCRIPTS:
        opening = _SCRIPT_OPEN_RE.search(page, position)
        if opening is None:
            break
        tag_end = page.find(">", opening.end())
        if tag_end == -1:
            break
        closing = _SCRIPT_CLOSE_RE.search(page, tag_end + 1)
        if closing is None:
            break
        attributes = _attributes(page[opening.end() : tag_end])
        scripts.append((attributes, page[tag_end + 1 : closing.start()]))
        position = closing.end()
    return scripts


def _decoded(body: str, start: int) -> tuple[dict[str, Any], int] | None:
    """The JSON object at *start* in *body*, and where it ends; None when what is
    there is not a JSON object, the parser refuses its nesting, or it is spelled in
    fewer than :data:`MIN_OBJECT_CHARS` characters."""
    try:
        value, end = _DECODER.raw_decode(body, start)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or end - start < MIN_OBJECT_CHARS:
        return None
    return value, end


def _printable_target(target: str) -> str:
    """*target* with each part that looks like it holds an id or a value replaced
    by a placeholder named as a path segment's would be."""
    parts: list[str] = []
    for part in target.split("."):
        if looks_dynamic(part):
            parts.append("{" + param_name_for_segment(parts[-1] if parts else "") + "}")
        else:
            parts.append(part)
    return ".".join(parts)


def _json_script_target(attributes: dict[str, str]) -> str:
    script_id = attributes.get("id", "")
    if not script_id:
        return _JSON_SCRIPT_TARGET
    if looks_dynamic(script_id):
        return "script#{" + param_name_for_segment("") + "}"
    return f"script#{script_id}"


def inline_json(page: str) -> InlineState | None:
    """The largest inline JSON object *page* holds in a form this module reads, or
    None when it holds none (see the module docstring for the forms and bounds)."""
    best: tuple[int, str, dict[str, Any]] | None = None
    decodes = 0
    for attributes, body in _scripts(page):
        if decodes >= MAX_DECODES:
            break
        if len(body) > MAX_SCRIPT_CHARS:
            continue
        script_type = attributes.get("type", "").strip().lower()
        candidates: list[tuple[str, dict[str, Any], int]] = []
        if script_type == _JSON_TYPE:
            start = len(body) - len(body.lstrip())
            decodes += 1
            decoded = _decoded(body, start)
            if decoded is not None:
                candidates.append((_json_script_target(attributes), decoded[0], decoded[1] - start))
        elif (not script_type or script_type in _JS_TYPES) and "src" not in attributes:
            position = 0
            while decodes < MAX_DECODES:
                match = _ASSIGNMENT_RE.search(body, position)
                if match is None:
                    break
                decodes += 1
                decoded = _decoded(body, match.end())
                if decoded is None:
                    position = match.end()
                    continue
                value, end = decoded
                candidates.append((_printable_target(match.group(1)), value, end - match.end()))
                position = end
        for target, value, size in candidates:
            if best is None or size > best[0]:
                best = (size, target, value)
    if best is None:
        return None
    return InlineState(target=best[1], value=best[2])


def pattern_for(target: str) -> str:
    """A regular expression whose match ends where *target*'s object starts in the
    page, for a generated example that reads it: the assignment's dotted name (a
    placeholder part matching any name), or the JSON script's opening tag."""
    if target == _JSON_SCRIPT_TARGET:
        return r"<script\b[^>]*\btype=[\"']?application/json[\"']?[^>]*>\s*"
    if target.startswith("script#"):
        script_id = target.removeprefix("script#")
        spelled = r"[^\"'\s>]+" if script_id.startswith("{") else re.escape(script_id)
        return rf"<script\b[^>]*\bid=[\"']?{spelled}[\"']?[^>]*>\s*"
    parts = [
        r"[A-Za-z0-9_$]+" if part.startswith("{") else re.escape(part) for part in target.split(".")
    ]
    return r"(?<![A-Za-z0-9_$.])" + r"\.".join(parts) + r"\s*=\s*(?=\{)"
