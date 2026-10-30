"""Inline JSON state: the data object a server-rendered page carries in a script.

A React or similar page often ships its data in one inline ``<script>``, either an
assignment to a dotted name (``window.App.props = {...};``) or a JSON block
(``<script id="__NEXT_DATA__" type="application/json">{...}</script>``). Without
it, the digest reports such a page as non-JSON and the page holding a site's
results reads like navigation chrome (#271).

Only ``<script>`` elements a browser would parse as scripts are read, within the
limits below: the page is tokenized as HTML, so a script inside an HTML comment,
an attribute value, or the text of a ``<textarea>``, ``<title>``, ``<style>``,
``<noscript>``, ``<iframe>``, ``<noembed>``, ``<noframes>`` or ``<xmp>`` is text,
one inside a ``<template>`` is inert and is skipped, and nothing after a
``<plaintext>`` tag is read. Two forms are read, and no other:

- a script with no ``src`` that a browser runs as JavaScript, by the HTML
  standard's "prepare the script element" (:func:`_runs_as_javascript`: a
  ``type`` or ``language`` naming a JavaScript MIME type essence, all of
  :data:`JAVASCRIPT_TYPES` but ``module``, or ``module``; no ``nomodule`` on a
  classic script), holding an assignment whose target is a dotted name of two or
  more parts (``window.App``, ``App.props``, ``window.App.props``) and whose
  value starts with ``{`` and is a JSON object. An assignment inside a
  JavaScript string, template literal, or ``//`` or ``/* */`` comment is
  skipped. ``var``/``let``/``const`` declarations, bare names, bracketed names
  (``window["App"]``), ``JSON.parse("...")``, and JavaScript object literals
  that are not JSON are not read;
- a ``<script>`` whose ``type`` is ``application/json``, with or without
  parameters (``application/json; charset=utf-8``), whose body is a JSON object,
  named by its ``id`` (``script#__NEXT_DATA__``) or, without one, as
  ``script[type="application/json"]``. ``application/ld+json`` is search-engine
  metadata, not page state, and is not read.

Two limits remain. A regular expression literal is read as code, so a quote,
backtick, ``//``, or ``/*`` inside one changes what is read after it: a quote
(``/'/``) opens a string the next quote closes, so a later string's text is read
as code; a backtick hides the text up to the next backtick; ``//`` hides the rest
of its line; and ``/*`` hides the rest of the script when no ``*/`` follows. The
HTML tokenizer's "script data escaped" states are not modelled: every script body
ends at the first ``</script`` followed by whitespace, ``/``, or ``>``, though a
browser keeps a script open past one that follows ``<!--`` and ``<script`` inside
it, so the rest of such a script is read as markup.

An array is never reported, only an object. The object must be spelled in at
least :data:`MIN_OBJECT_CHARS` characters, so a small settings object does not
stand for the page; of those that qualify, the largest is reported, the first on
a tie.

The search is bounded and linear in the page: it examines at most
:data:`MAX_SCRIPTS` scripts, skips a script body longer than
:data:`MAX_SCRIPT_CHARS`, and decodes at most :data:`MAX_DECODES` candidate
objects per page, each within its own script body. A candidate is first decoded
within its first ``2 * MIN_OBJECT_CHARS`` characters. One that ends under the
floor, and one that fails before reading :data:`MIN_OBJECT_CHARS` characters
(``{on: true}``), use no decode, so many small settings objects, JSON or
JavaScript, never hide the state after them. An object that qualifies, a decode
that fails after reading the floor's characters, and a candidate those first
characters cut short (then decoded whole) each use one. It never executes script.
A decode that fails, or that the parser refuses for nesting too deep for it
(``RecursionError``), is skipped.

An assignment's target is reported as written only when it is a plain dotted name
(identifiers joined by ``.``) of at most :data:`MAX_TARGET_CHARS` characters. A
part of it that looks like it holds an id or a value
(:func:`graftpunk.har.paths.looks_dynamic`) is reported as a placeholder, as a
path segment is (``window.{window_id}.props``), and a target that looks like one
only as a whole, or is longer, as ``{id}``. A JSON script's id is reported as
written (``script#app-state``) only when it is a plain HTML id (letters, digits,
``_``, ``.``, and ``-``) of at most :data:`MAX_TARGET_CHARS` characters that looks
like an id neither as a whole (``k3-m9-p2-q7``) nor in a part between dots, and
as ``script#{id}`` otherwise, so an id and a target spelled the same get the same
verdict. So no quote, backtick, space, or line break from the page reaches any
output through a target.

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
"""Candidate objects decoded per page that qualify, or that fail after reading
:data:`MIN_OBJECT_CHARS` characters; a smaller object or an earlier failure does
not count."""

MIN_OBJECT_CHARS = 512
"""An object spelled in fewer characters than this is not reported."""

MAX_TARGET_CHARS = 64
"""A target name longer than this many characters is reported as a placeholder."""

JAVASCRIPT_TYPES = frozenset(
    {
        "application/ecmascript",
        "application/javascript",
        "application/x-ecmascript",
        "application/x-javascript",
        "text/ecmascript",
        "text/javascript",
        "text/javascript1.0",
        "text/javascript1.1",
        "text/javascript1.2",
        "text/javascript1.3",
        "text/javascript1.4",
        "text/javascript1.5",
        "text/jscript",
        "text/livescript",
        "text/x-ecmascript",
        "text/x-javascript",
        "module",
    }
)
"""The script types whose assignments are read: the MIME Sniffing standard's
JavaScript MIME type essences (https://mimesniff.spec.whatwg.org/, "JavaScript MIME
type", fetched 2026-10-30) and ``module``, compared as :func:`_runs_as_javascript`
says."""

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
_ATTRIBUTES_READ = frozenset({"type", "language", "id", "src", "nomodule"})
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
    """The attributes in ``_ATTRIBUTES_READ`` of *tag*, the text of a tag
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


# The first decode of a candidate reads at most this many characters of it.
_WINDOW_CHARS = 2 * MIN_OBJECT_CHARS
# How far past an error's position the decoder may have read to raise it: the
# longest literal it compares whole is "-Infinity", and a "\uXXXX" escape is six.
_DECODE_LOOKAHEAD = 16


def _chars_read(error: json.JSONDecodeError) -> int:
    """How far into its text the decode that raised *error* read: to the end for a
    string left unterminated (the error names where the string starts), else to
    the error."""
    if error.msg.startswith("Unterminated string"):
        return len(error.doc)
    return error.pos


def _object_at(text: str, start: int, budget: _Budget) -> tuple[dict[str, Any] | None, int]:
    """The JSON object at *start* in *text* that is spelled in at least
    :data:`MIN_OBJECT_CHARS` characters, or None, and where the scan resumes: past
    a value that decoded, at *start* after a failed decode, and at the end of
    *text* when too little of it is left to hold a qualifying object.

    The candidate is first decoded within its first ``2 * MIN_OBJECT_CHARS``
    characters. A value that ends under the floor, and a decode that fails before
    reading the floor's :data:`MIN_OBJECT_CHARS` characters (``{on: true}``), use no
    decode from *budget*, so many small objects, JavaScript ones included, never
    hide the state after them. A value that qualifies, a decode that fails past the
    floor, and a candidate the window cuts short (then decoded whole) use one."""
    if len(text) - start < MIN_OBJECT_CHARS:
        return None, len(text)
    window = text[start : start + _WINDOW_CHARS]
    whole = start + _WINDOW_CHARS >= len(text)
    try:
        value, end = _DECODER.raw_decode(window)
    except json.JSONDecodeError as error:
        read = _chars_read(error)
        # An error the decoder raised within reach of the cut may be the cut's.
        if whole or read < len(window) - _DECODE_LOOKAHEAD:
            if read >= MIN_OBJECT_CHARS:
                budget.used += 1
            return None, start
    except (ValueError, RecursionError):
        pass
    else:
        if end < MIN_OBJECT_CHARS:
            return None, start + end
        budget.used += 1
        return (value if isinstance(value, dict) else None), start + end
    budget.used += 1
    try:
        value, end = _DECODER.raw_decode(text, start)
    except (ValueError, RecursionError):
        return None, start
    return (value if isinstance(value, dict) else None), end


def _plain_name(name: str) -> bool:
    return len(name) <= MAX_TARGET_CHARS and _NAME_PATH_RE.fullmatch(name) is not None


def _looks_like_an_id(script_id: str) -> bool:
    """True when *script_id* looks like it holds an id or a value
    (:func:`graftpunk.har.paths.looks_dynamic`) as a whole (``k3-m9-p2-q7``) or in
    a part between dots (``App.a1_b2_c3``), as an assignment's target spelled the
    same would. ``looks_dynamic`` judges each part between ``-``, ``_``, and ``.``
    itself, so a part holding an id (``state-40912873``) makes the whole look
    like one."""
    return any(looks_dynamic(part) for part in (script_id, *script_id.split(".")) if part)


def _printable_target(target: str) -> str:
    """*target*, an assignment's dotted name, as it is reported: each part that
    looks like it holds an id or a value replaced by a placeholder named as a path
    segment's would be, and the whole as ``{id}`` when it is not a plain name of at
    most :data:`MAX_TARGET_CHARS` characters, its placeholders make it longer, or
    only the whole looks like it holds an id (``j0.Cg.wg.n_8``)."""
    if not _plain_name(target):
        return _PLACEHOLDER
    parts: list[str] = []
    for part in target.split("."):
        if looks_dynamic(part):
            parts.append("{" + param_name_for_segment(parts[-1] if parts else "") + "}")
        else:
            parts.append(part)
    printed = ".".join(parts)
    if printed == target and looks_dynamic(target):
        return _PLACEHOLDER
    return printed if len(printed) <= MAX_TARGET_CHARS else _PLACEHOLDER


def _json_script_target(attributes: dict[str, str]) -> str:
    """A JSON script's target: ``script#`` and its id when the id is a plain HTML id
    (letters, digits, ``_``, ``.`` and ``-``) of at most :data:`MAX_TARGET_CHARS`
    characters that does not look like it holds an id, ``script#{id}`` for any
    other id, and ``script[type="application/json"]`` without one."""
    script_id = attributes.get("id", "")
    if not script_id:
        return _JSON_SCRIPT_TARGET
    if (
        len(script_id) <= MAX_TARGET_CHARS
        and _HTML_ID_RE.fullmatch(script_id)
        and not _looks_like_an_id(script_id)
    ):
        return f"script#{script_id}"
    return "script#" + _PLACEHOLDER


def _media_type(value: str) -> str:
    return value.split(";", 1)[0].strip(_HTML_SPACE).lower()


def _runs_as_javascript(attributes: dict[str, str]) -> bool:
    """True when a browser that supports modules runs a script with *attributes* as
    JavaScript, as the HTML standard's "prepare the script element" decides
    (https://html.spec.whatwg.org/multipage/scripting.html, fetched 2026-10-30).

    The script's type string is "text/javascript" when ``type`` is empty, or absent
    with ``language`` absent or empty; else the ``type`` value, or, when ``type``
    is absent, "text/" and the ``language`` value. A classic script is one whose
    type string, or whose ``type`` stripped of surrounding whitespace, is a
    JavaScript MIME type essence (:data:`JAVASCRIPT_TYPES` but ``module``) without
    case, so a parameter or a whitespace-only ``type`` makes a data block; a
    classic script with ``nomodule`` is not run. A module script is one whose
    type string is ``module`` without case, unstripped."""
    script_type = attributes.get("type")
    if script_type is None:
        language = attributes.get("language", "")
        classic = not language or f"text/{language}".lower() in _CLASSIC_TYPES
    elif script_type.lower() == "module":
        return True
    else:
        classic = not script_type or script_type.strip(_HTML_SPACE).lower() in _CLASSIC_TYPES
    return classic and "nomodule" not in attributes


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
        elif "src" not in attributes and _runs_as_javascript(attributes):
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
