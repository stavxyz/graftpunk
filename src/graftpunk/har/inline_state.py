"""Inline JSON state: the data object a server-rendered page carries in a script.

A React or similar page often ships its data in one inline ``<script>``, either an
assignment to a dotted name (``window.App.props = {...};``) or a JSON block
(``<script id="__NEXT_DATA__" type="application/json">{...}</script>``). Without
it, the digest reports such a page as non-JSON and the page holding a site's
results reads like navigation chrome (#271).

Only ``<script>`` elements a browser would parse as scripts are read: the page is
tokenized as HTML, so a script inside an HTML comment, an attribute value, or the
text of a ``<textarea>``, ``<title>``, ``<style>``, ``<noscript>``, ``<iframe>``,
``<noembed>``, ``<noframes>`` or ``<xmp>`` is text, and one inside a
``<template>`` is inert and is skipped. Two forms are read, and no other:

- a script whose ``type`` is absent, empty, or a JavaScript type
  (:data:`JAVASCRIPT_TYPES`, compared as the HTML standard compares them: without
  case, and for all but ``module`` without surrounding whitespace, and never with
  parameters, so ``text/javascript; charset=utf-8`` is a data block), with no
  ``src``, holding an assignment whose target is
  a dotted name of two or more parts (``window.App``, ``App.props``,
  ``window.App.props``) and whose value starts with ``{`` and is a JSON object.
  An assignment inside a JavaScript string, template literal, or ``//`` or
  ``/* */`` comment is skipped. ``var``/``let``/``const`` declarations, bare
  names, bracketed names (``window["App"]``), ``JSON.parse("...")``, and
  JavaScript object literals that are not JSON are not read;
- a ``<script>`` whose ``type`` is ``application/json``, with or without
  parameters (``application/json; charset=utf-8``), whose body is a JSON object,
  named by its ``id`` (``script#__NEXT_DATA__``) or, without one, as
  ``script[type="application/json"]``. ``application/ld+json`` is search-engine
  metadata, not page state, and is not read.

An array is never reported, only an object. The object must be spelled in at
least :data:`MIN_OBJECT_CHARS` characters, so a small settings object does not
stand for the page; of those that qualify, the largest is reported, the first on
a tie.

The search is bounded and linear in the page: it examines at most
:data:`MAX_SCRIPTS` scripts, skips a script body longer than
:data:`MAX_SCRIPT_CHARS`, and decodes at most :data:`MAX_DECODES` candidate
objects per page, each within its own script body. A candidate is first decoded
within its first ``MIN_OBJECT_CHARS - 1`` characters; one that ends there is under
the floor and is skipped without using a decode, so many small settings objects
never hide the state after them. Every other candidate, a malformed one included,
uses one. It never executes script. A decode that fails, or that the parser
refuses for nesting too deep for it (``RecursionError``), is skipped.

The target is reported as written only when it is a plain dotted name
(identifiers joined by ``.``) of at most :data:`MAX_TARGET_CHARS` characters. A
part of an assignment's target that looks like it holds an id or a value
(:func:`graftpunk.har.paths.looks_dynamic`) is reported as a placeholder, as a
path segment is (``window.{window_id}.props``), and a longer target is reported as
``{id}``. A JSON script's id that is not such a name, or has such a part, is
reported as ``script#{id}``. So no quote, backtick, space, or line break from the
page reaches any output through a target.

:func:`read_inline_json` is the reader a generated plugin calls: the same scan and
the same choice, so it returns the object the digest reported for the page.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from graftpunk.har.paths import looks_dynamic, param_name_for_segment

__all__ = [
    "JAVASCRIPT_TYPES",
    "JSON_TYPE",
    "MAX_DECODES",
    "MAX_SCRIPTS",
    "MAX_SCRIPT_CHARS",
    "MAX_TARGET_CHARS",
    "MIN_OBJECT_CHARS",
    "InlineState",
    "inline_json",
    "read_inline_json",
]

MAX_SCRIPTS = 128
"""Scripts examined per page; later ones are not read."""

MAX_SCRIPT_CHARS = 4 * 1024 * 1024
"""A script body longer than this many characters is not examined."""

MAX_DECODES = 32
"""Candidate objects decoded per page past the floor check, failed decodes
included; a candidate the floor check rejects does not count."""

MIN_OBJECT_CHARS = 512
"""An object spelled in fewer characters than this is not reported."""

MAX_TARGET_CHARS = 64
"""A target name longer than this many characters is reported as a placeholder."""

JAVASCRIPT_TYPES = frozenset(
    {
        "text/javascript",
        "application/javascript",
        "application/x-javascript",
        "application/ecmascript",
        "text/ecmascript",
        "module",
    }
)
"""The ``type`` values of a script whose assignments are read (with an absent or
empty ``type``), compared without case; all but ``module`` also without
surrounding whitespace."""

_CLASSIC_TYPES = JAVASCRIPT_TYPES - {"module"}

JSON_TYPE = "application/json"
"""The media type, before any parameters, of a JSON script that is read."""

_HTML_SPACE = "\t\n\f\r "
_TAG_NAME_RE = re.compile(r"[A-Za-z][^\t\n\f\r />]*")
# An attribute as the HTML tokenizer reads one: a name (whose first character may
# be "="), then optionally "=" and a double-quoted, single-quoted, or unquoted value.
# Every quantifier is possessive, so a match never backtracks. A name followed by
# "=" whose value does not match is a quoted value left open, which runs to the end
# of the page, so no match of _TAG_REST_RE ends there.
_ATTRIBUTE = (
    r"""([^\t\n\f\r />][^\t\n\f\r />=]*+)"""
    r"""(?:[\t\n\f\r ]*+=[\t\n\f\r ]*+(?:"([^"]*+)"|'([^']*+)'|(?!["'])([^\t\n\f\r >]*+))"""
    r"""|(?![\t\n\f\r ]*+=))"""
)
_ATTRIBUTE_RE = re.compile(_ATTRIBUTE)
# The rest of a tag after its name, up to and including the ">" that ends it.
_TAG_REST_RE = re.compile(rf"(?:[\t\n\f\r /]++|{_ATTRIBUTE})*+>")
_ATTRIBUTES_READ = frozenset({"type", "id", "src"})
_COMMENT_END_RE = re.compile(r"--!?>")
# Elements whose content is text up to their own end tag: scripts, the raw text
# elements, and the escapable raw text ones (noscript is raw text when scripting
# is on, as it is in a recording browser).
_TEXT_ELEMENTS = frozenset(
    {"script", "style", "xmp", "iframe", "noembed", "noframes", "noscript", "textarea", "title"}
)
_END_TAG_RES = {
    name: re.compile(rf"</{name}[\t\n\f\r />]", re.IGNORECASE) for name in _TEXT_ELEMENTS
}
_IDENTIFIER = r"[A-Za-z_$][A-Za-z0-9_$]*"
_NAME_PATH_RE = re.compile(rf"{_IDENTIFIER}(?:\.{_IDENTIFIER})*")
# A JSON script's id as printed: an HTML id commonly holds "-" (app-state), which a
# script's dotted name cannot, and nothing that could break a line or a quote.
_HTML_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
# What a script body's scan stops at: a comment, a string or template literal (each
# skipped whole), or an assignment candidate: a dotted name not preceded by a name
# character or a dot (so never the tail of a longer name), an "=" that is not part
# of "==", and a value starting with "{".
_JS_TOKEN_RE = re.compile(
    rf"(?P<line>//)|(?P<block>/\*)|(?P<quote>[\"'`])"
    rf"|(?<![A-Za-z0-9_$.])(?P<name>{_IDENTIFIER}(?:\.{_IDENTIFIER})+)\s*=\s*(?=\{{)"
)
_LINE_COMMENT_RE = re.compile(r"//[^\n\r\u2028\u2029]*")
_STRING_RES = {
    '"': re.compile(r'"[^"\\\n\r]*(?:\\[\s\S][^"\\\n\r]*)*"?'),
    "'": re.compile(r"'[^'\\\n\r]*(?:\\[\s\S][^'\\\n\r]*)*'?"),
    "`": re.compile(r"`[^`\\]*(?:\\[\s\S][^`\\]*)*`?"),
}
_JSON_SCRIPT_TARGET = 'script[type="application/json"]'
_PLACEHOLDER = "{" + param_name_for_segment("") + "}"
_DECODER = json.JSONDecoder()


@dataclass(frozen=True)
class InlineState:
    """The object a page's script holds, and the target it is reported under."""

    target: str
    value: dict[str, Any]


def _matched_end(pattern: re.Pattern[str], text: str, position: int) -> int:
    """Where *pattern*, matched at *position* in *text*, ends; *position* when it
    does not match there."""
    match = pattern.match(text, position)
    return position if match is None else match.end()


def _attributes(tag: str) -> dict[str, str]:
    """The ``type``, ``id``, and ``src`` attributes of *tag*, the text of a tag
    after its name, by lower-case name; the first of each wins, as in a browser."""
    found: dict[str, str] = {}
    for match in _ATTRIBUTE_RE.finditer(tag):
        name = match.group(1).lower()
        if name in _ATTRIBUTES_READ:
            value = next((group for group in match.groups()[1:] if group is not None), "")
            found.setdefault(name, value)
    return found


def _comment_end(page: str, position: int) -> int:
    """Where the HTML comment whose text starts at *position* (after ``<!--``)
    ends, past its ``-->``; -1 when the page ends inside it."""
    if page.startswith(">", position):
        return position + 1
    if page.startswith("->", position):
        return position + 2
    closing = _COMMENT_END_RE.search(page, position)
    return -1 if closing is None else closing.end()


def _scripts(page: str) -> list[tuple[dict[str, str], str]]:
    """The first :data:`MAX_SCRIPTS` scripts a browser would parse in *page*, outside
    any ``<template>``: each one's attributes and body. The page is tokenized as
    HTML: comments, doctypes, and the text of the elements in ``_TEXT_ELEMENTS`` are
    skipped, and a tag's quoted attribute values are read whole. A tag, comment, or
    element left open ends the search, since nothing after it is markup. Each step
    searches forward from the last, so the scan is linear in the page."""
    scripts: list[tuple[dict[str, str], str]] = []
    position = 0
    templates = 0
    while len(scripts) < MAX_SCRIPTS:
        opening = page.find("<", position)
        if opening == -1:
            break
        if page.startswith("<!--", opening):
            position = _comment_end(page, opening + 4)
            if position == -1:
                break
            continue
        after = page[opening + 1 : opening + 2]
        closing_tag = after == "/"
        name_match = _TAG_NAME_RE.match(page, opening + (2 if closing_tag else 1))
        if name_match is None:
            if after in ("!", "?") or closing_tag:
                # A doctype, a processing instruction, or a malformed end tag: a
                # bogus comment up to the next ">".
                end = page.find(">", opening + 2)
                if end == -1:
                    break
                position = end + 1
            else:
                position = opening + 1
            continue
        rest = _TAG_REST_RE.match(page, name_match.end())
        if rest is None:
            # The page ends inside the tag, or inside a quoted value left open.
            break
        position = rest.end()
        name = name_match.group().lower()
        if closing_tag:
            if name == "template" and templates:
                templates -= 1
            continue
        if name == "template":
            templates += 1
        elif name == "plaintext":
            break
        elif name in _TEXT_ELEMENTS:
            end_tag = _END_TAG_RES[name].search(page, position)
            if end_tag is None:
                break
            if name == "script" and not templates:
                attributes = _attributes(page[name_match.end() : rest.end() - 1])
                scripts.append((attributes, page[position : end_tag.start()]))
            position = end_tag.start()
    return scripts


class _Budget:
    """The decodes a page has left (:data:`MAX_DECODES`)."""

    def __init__(self) -> None:
        self.used = 0

    @property
    def spent(self) -> bool:
        return self.used >= MAX_DECODES


def _object_at(text: str, start: int, budget: _Budget) -> tuple[dict[str, Any] | None, int]:
    """The JSON object at *start* in *text* that is spelled in at least
    :data:`MIN_OBJECT_CHARS` characters, or None, and where the scan resumes: past
    a value that decoded, at *start* after a failed decode, and at the end of
    *text* when too little of it is left to hold a qualifying object.

    The value is first decoded within its first ``MIN_OBJECT_CHARS - 1``
    characters; one that ends there is under the floor and is skipped without using
    a decode from *budget*. Only a candidate that does not end there is decoded
    whole, which uses one."""
    if len(text) - start < MIN_OBJECT_CHARS:
        return None, len(text)
    try:
        _small, end = _DECODER.raw_decode(text[start : start + MIN_OBJECT_CHARS - 1])
    except (ValueError, RecursionError):
        pass
    else:
        return None, start + end
    budget.used += 1
    try:
        value, end = _DECODER.raw_decode(text, start)
    except (ValueError, RecursionError):
        return None, start
    return (value if isinstance(value, dict) else None), end


def _plain_name(name: str) -> bool:
    return len(name) <= MAX_TARGET_CHARS and _NAME_PATH_RE.fullmatch(name) is not None


def _printable_target(target: str) -> str:
    """*target*, an assignment's dotted name, as it is reported: each part that
    looks like it holds an id or a value replaced by a placeholder named as a path
    segment's would be, and the whole as ``{id}`` when it is not a plain name of at
    most :data:`MAX_TARGET_CHARS` characters, or its placeholders make it longer."""
    if not _plain_name(target):
        return _PLACEHOLDER
    parts: list[str] = []
    for part in target.split("."):
        if looks_dynamic(part):
            parts.append("{" + param_name_for_segment(parts[-1] if parts else "") + "}")
        else:
            parts.append(part)
    printed = ".".join(parts)
    return printed if len(printed) <= MAX_TARGET_CHARS else _PLACEHOLDER


def _json_script_target(attributes: dict[str, str]) -> str:
    """A JSON script's target: ``script#`` and its id when the id is a plain HTML id
    (letters, digits, ``_``, ``.`` and ``-``) with no part that looks like it holds
    an id, ``script#{id}`` for any other id, and ``script[type="application/json"]``
    without one."""
    script_id = attributes.get("id", "")
    if not script_id:
        return _JSON_SCRIPT_TARGET
    if (
        len(script_id) <= MAX_TARGET_CHARS
        and _HTML_ID_RE.fullmatch(script_id)
        and not any(looks_dynamic(part) for part in re.split(r"[-_.]", script_id) if part)
    ):
        return f"script#{script_id}"
    return "script#" + _PLACEHOLDER


def _media_type(value: str) -> str:
    return value.split(";", 1)[0].strip(_HTML_SPACE).lower()


def _runs_as_javascript(script_type: str) -> bool:
    """True when a script whose ``type`` is *script_type* (empty when absent) runs as
    JavaScript, as the HTML standard's "prepare the script element" decides: an
    empty type, a JavaScript MIME type compared without case or surrounding
    whitespace and with no parameters, or ``module`` compared without case only."""
    stripped = script_type.strip(_HTML_SPACE).lower()
    return not stripped or stripped in _CLASSIC_TYPES or script_type.lower() == "module"


def _assignments(body: str, budget: _Budget) -> Iterator[tuple[str, dict[str, Any], int]]:
    """Each qualifying object *body*, a JavaScript script's, assigns to a dotted
    name outside its strings and comments: the printable target, the object, and
    its spelled length."""
    position = 0
    while not budget.spent:
        token = _JS_TOKEN_RE.search(body, position)
        if token is None:
            return
        if token.group("line"):
            position = _matched_end(_LINE_COMMENT_RE, body, token.start())
        elif token.group("block"):
            closing = body.find("*/", token.end())
            if closing == -1:
                return
            position = closing + 2
        elif token.group("quote"):
            position = _matched_end(_STRING_RES[token.group("quote")], body, token.start())
        else:
            value, position = _object_at(body, token.end(), budget)
            if value is not None:
                yield _printable_target(token.group("name")), value, position - token.end()


def _candidates(page: str) -> Iterator[tuple[str, dict[str, Any], int]]:
    """Every qualifying object *page* holds in a form this module reads, in page
    order: its printable target, the object, and its spelled length."""
    budget = _Budget()
    for attributes, body in _scripts(page):
        if budget.spent:
            return
        if len(body) > MAX_SCRIPT_CHARS:
            continue
        script_type = attributes.get("type", "")
        if _media_type(script_type) == JSON_TYPE:
            start = len(body) - len(body.lstrip())
            value, end = _object_at(body, start, budget)
            if value is not None:
                yield _json_script_target(attributes), value, end - start
        elif "src" not in attributes and _runs_as_javascript(script_type):
            yield from _assignments(body, budget)


def _largest(
    candidates: Iterator[tuple[str, dict[str, Any], int]],
) -> InlineState | None:
    best: tuple[int, str, dict[str, Any]] | None = None
    for target, value, size in candidates:
        if best is None or size > best[0]:
            best = (size, target, value)
    return None if best is None else InlineState(target=best[1], value=best[2])


def inline_json(page: str) -> InlineState | None:
    """The largest inline JSON object *page* holds in a form this module reads, and
    its printable target, or None when it holds none (see the module docstring for
    the forms and bounds)."""
    return _largest(_candidates(page))


def read_inline_json(page: str, target: str | None = None) -> dict[str, Any] | None:
    """The inline JSON object the digest reports for *page* (:func:`inline_json`'s),
    or None when it holds none. With *target*, a target the digest reported, the
    largest object reported under that target instead, the first on a tie, so a
    page whose other objects have grown still gives the one the digest named; the
    same scan and bounds apply."""
    candidates = _candidates(page)
    if target is not None:
        candidates = (c for c in candidates if c[0] == target)
    found = _largest(candidates)
    return None if found is None else found.value
