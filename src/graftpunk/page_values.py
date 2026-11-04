"""Read the token values a page carries: each ``<meta name content>`` and each
named hidden input.

One reader serves both sides of a token pairing (#369): the digest compares a
request header with these values, and ``Token.from_meta_tag`` and
``Token.from_hidden_input`` extract them at runtime, so a pairing the digest makes
is the value the generated ``Token`` sends.

The reader is a small tokenizer written to the WHATWG HTML parsing rules
(https://html.spec.whatwg.org/multipage/parsing.html, section 13.2.5), so its
answer does not depend on the Python release: ``html.parser`` changed how it
decodes attribute values and which elements it treats as raw text in CPython
patch releases. It follows the spec for what decides which tags a browser sees:

- tags and attributes (13.2.5.8 to 13.2.5.40): any order and case, single,
  double or no quotes, a repeated attribute keeping its first value, a tag cut off
  by the end of the page dropped, and a self-closing slash ignored;
- character references in an attribute value (13.2.5.77 to 13.2.5.84), including
  the rule that a named reference without ``;`` followed by a letter, a digit or
  ``=`` stays literal;
- comments (13.2.5.41 to 13.2.5.52), including ``<!-->`` and ``--!>``, and
  doctypes, processing instructions and bogus comments, each ending at the next
  ``>``;
- raw text (13.2.5.2 to 13.2.5.31): nothing inside ``<script>`` (with its
  ``<!--`` escape states),
  ``<style>``, ``<textarea>``, ``<title>``, ``<xmp>``, ``<iframe>``,
  ``<noembed>``, ``<noframes>``, ``<noscript>`` (scripting is on in a browser that
  runs the page) or ``<plaintext>`` is a tag, and only that element's own end tag
  closes it;
- ``<template>`` content, which is not part of the document: skipped until the
  matching end tag, nested templates counted.

It does not build a tree, so it does not model ``<svg>`` or ``<math>`` content
or the tree builder's reordering. What differs from a browser, as far as known:

- inside ``<svg>`` or ``<math>`` a browser reads every element's content as
  markup and ``<![CDATA[`` as a CDATA section, so the raw-text and template
  skipping above does not apply there: a ``<meta>`` inside an svg
  ``<textarea>`` or ``<template>`` or a math ``<noscript>`` is in a browser's
  document and is not read here, and an ``<input type="hidden">`` inside
  ``<svg>`` is an SVG element in a browser and is read here as an HTML input;
- it reads in source order, where a browser can move an element ahead of a
  ``<table>`` (foster parenting), so of two inputs with one name it can take the
  other one first;
- a value a script writes into the page after it loads is not in the text this
  reader is given;
- of several elements with one name it takes the first with a non-empty value.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache
from html.entities import html5
from types import MappingProxyType
from typing import Literal

__all__ = ["PageValueKind", "page_values", "skipped_region_mentioning"]

PageValueKind = Literal["meta", "hidden_input"]

# A start or end tag after its "<" or "</" (13.2.5.8 tag name state to 13.2.5.40
# self-closing start tag state): the name, then each attribute, then "/>" or ">".
# A quote opens a value only right after "=", "/" not before ">" separates like
# whitespace, and every quantifier is possessive, so a tag cut off by the end of
# the text fails in linear time instead of backtracking. The match fails only
# when the text ends inside the tag (a browser drops that tag).
_TAG = re.compile(
    r"([A-Za-z][^\t\n\f />]*+)"
    r"((?:[\t\n\f ]++|/(?!>)"
    r"|[^\t\n\f />][^\t\n\f />=]*+"
    r"(?:[\t\n\f ]*+=[\t\n\f ]*+(?:\"[^\"]*+\"|'[^']*+'|(?![\"'])[^\t\n\f >]*+)"
    r"|(?![\t\n\f ]*+=))"
    r")*+)"
    r"/?>"
)
# One attribute of a section _TAG matched: name, then the value double-quoted,
# single-quoted, or unquoted.
_ATTRIBUTE = re.compile(
    r"[\t\n\f /]*+([^\t\n\f />][^\t\n\f />=]*+)"
    r"(?:[\t\n\f ]*+=[\t\n\f ]*+(?:\"([^\"]*+)\"|'([^']*+)'|([^\t\n\f >]*+)))?"
)
# 13.2.5.45 to 13.2.5.52: a comment ends at "-->" or "--!>".
_COMMENT_END = re.compile(r"--!?>")
# Elements whose content the tokenizer reads as text up to their own end tag
# (13.2.6.4.7 "in body": RAWTEXT and RCDATA elements; noscript with scripting on).
_RAW_TEXT = frozenset(
    {"style", "textarea", "title", "xmp", "iframe", "noembed", "noframes", "noscript"}
)
_RAW_TEXT_END = {name: re.compile(rf"</{name}(?=[\t\n\f />])", re.IGNORECASE) for name in _RAW_TEXT}
# What changes state inside a script (13.2.5.4 and 13.2.5.15 to 13.2.5.31).
_SCRIPT_MARK = re.compile(r"<!--|-->|<(/?)script(?=[\t\n\f />])", re.IGNORECASE)
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_ALPHANUMERIC_OR_EQUALS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789="
)
_NAMED_REFERENCE = re.compile(r"[A-Za-z0-9]+;?")
_LONGEST_NAMED_REFERENCE = max(len(name) for name in html5)
_NUMERIC_REFERENCE = re.compile(r"#(?:[xX]([0-9A-Fa-f]+)|([0-9]+));?")
# 13.2.5.84: a reference to a C1 control reads as the windows-1252 character.
_C1_REPLACEMENTS = {
    0x80: 0x20AC, 0x82: 0x201A, 0x83: 0x0192, 0x84: 0x201E, 0x85: 0x2026, 0x86: 0x2020,
    0x87: 0x2021, 0x88: 0x02C6, 0x89: 0x2030, 0x8A: 0x0160, 0x8B: 0x2039, 0x8C: 0x0152,
    0x8E: 0x017D, 0x91: 0x2018, 0x92: 0x2019, 0x93: 0x201C, 0x94: 0x201D, 0x95: 0x2022,
    0x96: 0x2013, 0x97: 0x2014, 0x98: 0x02DC, 0x99: 0x2122, 0x9A: 0x0161, 0x9B: 0x203A,
    0x9C: 0x0153, 0x9E: 0x017E, 0x9F: 0x0178,
}  # fmt: skip


def _numeric_reference(code: int) -> str:
    if code == 0 or code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
        return "�"
    return chr(_C1_REPLACEMENTS.get(code, code))


def _decode_attribute_value(raw: str) -> str:
    """*raw* with its character references decoded as in an attribute value."""
    if "&" not in raw:
        return raw
    out: list[str] = []
    pos = 0
    while (amp := raw.find("&", pos)) >= 0:
        out.append(raw[pos:amp])
        pos = amp + 1
        numeric = _NUMERIC_REFERENCE.match(raw, pos)
        if numeric is not None:
            hexadecimal, decimal = numeric.groups()
            digits = (hexadecimal if hexadecimal is not None else decimal).lstrip("0") or "0"
            # Past eight digits it is out of range whatever they are (and int()
            # refuses a few thousand).
            code = 0x110000 if len(digits) > 8 else int(digits, 16 if hexadecimal else 10)
            out.append(_numeric_reference(code))
            pos = numeric.end()
            continue
        named = _NAMED_REFERENCE.match(raw, pos)
        if named is None:  # "&" then neither "#" with digits nor a name: literal
            out.append("&")
            continue
        # The longest name in the table that the text starts with (13.2.5.78).
        candidate = named.group(0)[:_LONGEST_NAMED_REFERENCE]
        while candidate and candidate not in html5:
            candidate = candidate[:-1]
        after = pos + len(candidate)
        if not candidate or (
            not candidate.endswith(";") and raw[after : after + 1] in _ALPHANUMERIC_OR_EQUALS
        ):
            # No such name, or a name without ";" running into a letter, a digit or
            # "=", which an attribute keeps as written.
            out.append("&")
            continue
        out.append(html5[candidate])
        pos = after
    out.append(raw[pos:])
    return "".join(out)


def _attributes(section: str) -> dict[str, str]:
    """The attributes of a tag's attribute *section*, names lower-cased, the first
    of a repeated name kept, values decoded."""
    found: dict[str, str] = {}
    for match in _ATTRIBUTE.finditer(section):
        name = match.group(1).translate(_ASCII_LOWER).replace("\0", "�")
        if name in found:
            continue
        raw = next((v for v in match.group(2, 3, 4) if v is not None), "")
        found[name] = _decode_attribute_value(raw).replace("\0", "�")
    return found


def _script_end(text: str, pos: int) -> int:
    """Where the script whose content starts at *pos* ends: the start of its end
    tag, or the end of *text*. Inside ``<!--``, a ``<script>`` begins a region
    where ``</script>`` does not end the script (13.2.5.15 to 13.2.5.31)."""
    escaped = double_escaped = False
    while (mark := _SCRIPT_MARK.search(text, pos)) is not None:
        found = mark.group(0)
        if found == "<!--":
            escaped = escaped or not double_escaped
            # Its dashes can end it at once: "<!-->" leaves the escape.
            pos = mark.start() + 2
        elif found == "-->":
            escaped = double_escaped = False
            pos = mark.end()
        elif mark.group(1):  # </script
            if not double_escaped:
                return mark.start()
            double_escaped = False
            pos = mark.end()
        else:  # <script
            double_escaped = double_escaped or escaped
            pos = mark.end()
    return len(text)


def _scan(
    html: str, regions: list[tuple[str, int, int]] | None = None
) -> tuple[dict[tuple[PageValueKind, str], str], str]:
    """The page values of *html*, and the text read (line endings normalized as
    13.2.3.5 does). When *regions* is given, append to it ``(what, start, end)``
    for each stretch of that text no tag is read in: a raw-text element's content,
    a comment, or a template's content."""
    text = html.replace("\r\n", "\n").replace("\r", "\n")
    values: dict[tuple[PageValueKind, str], str] = {}
    templates = 0  # open <template> elements
    template_start = 0
    end = len(text)
    pos = 0
    while (lt := text.find("<", pos)) >= 0 and lt + 1 < end:
        after = text[lt + 1]
        pos = lt + 1
        if after.isascii() and after.isalpha():
            tag = _TAG.match(text, lt + 1)
            if tag is None:
                break  # the text ends inside this tag
            name = tag.group(1).translate(_ASCII_LOWER)
            pos = tag.end()
            if name == "script" or name in _RAW_TEXT or name == "plaintext":
                if name == "script":
                    close = _script_end(text, pos)
                elif name in _RAW_TEXT:
                    found = _RAW_TEXT_END[name].search(text, pos)
                    close = found.start() if found is not None else end
                else:
                    close = end
                if regions is not None:
                    regions.append((name, pos, close))
                pos = close
            elif name == "template":
                if not templates:
                    template_start = pos
                templates += 1
            elif templates:
                continue
            elif name == "meta" or name == "input":
                attrs = _attributes(tag.group(2))
                kind: PageValueKind
                if name == "meta":
                    kind, value = "meta", attrs.get("content", "")
                elif attrs.get("type", "").translate(_ASCII_LOWER) == "hidden":
                    kind, value = "hidden_input", attrs.get("value", "")
                else:
                    continue
                element_name = attrs.get("name", "")
                if element_name and value:
                    values.setdefault((kind, element_name), value)
        elif after == "/":
            if lt + 2 >= end:
                break
            if text[lt + 2].isascii() and text[lt + 2].isalpha():
                tag = _TAG.match(text, lt + 2)
                if tag is None:
                    break
                pos = tag.end()
                if templates and tag.group(1).translate(_ASCII_LOWER) == "template":
                    templates -= 1
                    if not templates and regions is not None:
                        regions.append(("template", template_start, lt))
            elif text[lt + 2] == ">":
                pos = lt + 3
            else:  # a bogus comment, to the next ">"
                close = text.find(">", lt + 2)
                if regions is not None:
                    regions.append(("comment", lt, end if close < 0 else close))
                if close < 0:
                    break
                pos = close + 1
        elif after == "!" and text.startswith("<!--", lt):
            if text.startswith(">", lt + 4):
                pos = lt + 5
            elif text.startswith("->", lt + 4):
                pos = lt + 6
            else:
                found = _COMMENT_END.search(text, lt + 4)
                if regions is not None:
                    regions.append(("comment", lt, end if found is None else found.start()))
                if found is None:
                    break
                pos = found.end()
        elif after == "!" or after == "?":  # a doctype or a bogus comment, to ">"
            close = text.find(">", lt + 2)
            if regions is not None:
                regions.append(("comment", lt, end if close < 0 else close))
            if close < 0:
                break
            pos = close + 1
    if templates and regions is not None:
        regions.append(("template", template_start, end))
    return values, text


@lru_cache(maxsize=8)
def page_values(html: str) -> Mapping[tuple[PageValueKind, str], str]:
    """``{(kind, name): value}`` for *html*: kind ``meta`` for each ``<meta>`` with
    a ``name`` and a non-empty ``content``, kind ``hidden_input`` for each
    ``<input type="hidden">`` with a ``name`` and a non-empty ``value``, in a form
    or not. When several carry one name, the first in document order wins
    (``document.querySelector`` would return an earlier one with an empty value,
    which this skips). Names are as written (case kept);
    values are decoded. The result is read-only and cached for the last few page
    texts, so every token and every poll attempt on one page parses it once."""
    return MappingProxyType(_scan(html)[0])


def skipped_region_mentioning(html: str, kind: PageValueKind, name: str) -> str | None:
    """The element (``"script"``, ``"template"``, ...) or ``"comment"`` whose
    content in *html* holds text that looks like the *kind* element called *name*
    which :func:`page_values` does not read there, or None. For an error message
    only: it explains why a tag visible in the page text was not read."""
    regions: list[tuple[str, int, int]] = []
    _values, text = _scan(html, regions)
    element = "meta" if kind == "meta" else "input"
    # Loose on purpose: inside a script string the quotes may be escaped.
    looks_like = re.compile(
        rf"<{element}\b[^<]*?\bname\s*=\s*\\?[\"']?{re.escape(name)}(?![\w-])",
        re.IGNORECASE,
    )
    for what, start, stop in regions:
        if looks_like.search(text, start, stop):
            return what
    return None
