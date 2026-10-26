"""Reading the CSS selectors a recording carries: the digest's own login-form selectors
(:mod:`graftpunk.har.documents`) and the ones ``gp observe``'s page listener computed
for a click or a focus move (:mod:`graftpunk.observe.dom_listener`).

Only the shapes those two write are understood: ``#id`` (CSS-escaped),
``[id="..."]``, ``tag[name="..."]``, a structural ``tag:nth-of-type(n)`` path, and
selector lists and descendant scopes built from them. Anything else is compared as
text.
"""

from __future__ import annotations

import re

from graftpunk.har.paths import holds_an_id

__all__ = ["css_unescape", "selector_holds_an_id", "selector_keys"]

_ESCAPE_RE = re.compile(r"\\([0-9a-fA-F]{1,6}) ?|\\(.)", re.DOTALL)
_HEX_ESCAPE_RE = re.compile(r"[0-9a-fA-F]{1,6} ?")
_ID_RE = re.compile(r"#((?:\\[0-9a-fA-F]{1,6} ?|\\.|[\w-]|[^\x00-\x7f])+)", re.DOTALL)
_QUOTED_RE = re.compile(r'"((?:\\.|[^"\\])*)"', re.DOTALL)
_ID_ATTRIBUTE_RE = re.compile(r'\[id="((?:\\.|[^"\\])*)"\]', re.DOTALL)
_NAME_ATTRIBUTE_RE = re.compile(r'([A-Za-z][\w-]*)\[name="((?:\\.|[^"\\])*)"\]', re.DOTALL)


def _code_point(match: re.Match[str]) -> str:
    if match.group(2) is not None:
        return match.group(2)
    value = int(match.group(1), 16)
    # CSS Syntax: zero, a surrogate, or past the last code point reads as U+FFFD.
    if value == 0 or 0xD800 <= value <= 0xDFFF or value > 0x10FFFF:
        return "\ufffd"
    return chr(value)


def css_unescape(text: str) -> str:
    """*text* with its CSS escapes undone: ``\\31 st`` is ``1st``, ``\\:`` is ``:``, and
    ``\\"`` is ``"``."""
    return _ESCAPE_RE.sub(_code_point, text)


def _split_top(text: str, separators: str) -> list[str]:
    """*text* split at each of *separators* outside quotes, brackets, parentheses, and
    CSS escapes (a hex escape's one trailing space included)."""
    parts: list[str] = []
    depth = 0
    quote = ""
    start = 0
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\":
            hex_escape = _HEX_ESCAPE_RE.match(text, i + 1)
            i += 1 + (len(hex_escape.group(0)) if hex_escape else 1)
            continue
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in "[(":
            depth += 1
        elif char in "])":
            depth -= 1
        elif depth == 0 and char in separators:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return parts


def selector_holds_an_id(selector: str) -> bool:
    """Whether *selector* spells an account value (:func:`graftpunk.har.paths.holds_an_id`)
    anywhere: in its whole text, in an ``#id`` with its escapes undone, or in a quoted
    attribute value."""
    parts = [selector]
    parts += [css_unescape(found) for found in _ID_RE.findall(selector)]
    parts += [css_unescape(found) for found in _QUOTED_RE.findall(selector)]
    return any(holds_an_id(part) for part in parts)


def selector_keys(selector: str) -> frozenset[tuple[str, ...]]:
    """What each alternative of *selector* picks, comparable across the ways the digest
    and the page's listener spell it. An alternative whose last compound is an id
    (``#id`` or ``[id="..."]``) is ``("id", id)``, and one whose last compound is
    ``tag[name="..."]`` is ``("name", tag, name)``, whatever scope comes before it:
    both writers use such a selector only when it picks one element on the page, so
    the scope adds nothing. Any other alternative is ``("text", alternative)``, its
    combinators spaced one way."""
    keys: set[tuple[str, ...]] = set()
    for alternative in _split_top(selector, ","):
        compounds = [part for part in _split_top(alternative, " \t\n\r\f>") if part]
        if not compounds:
            continue
        last = compounds[-1]
        found = _ID_RE.fullmatch(last) or _ID_ATTRIBUTE_RE.fullmatch(last)
        if found is not None:
            keys.add(("id", css_unescape(found.group(1))))
        elif (found := _NAME_ATTRIBUTE_RE.fullmatch(last)) is not None:
            keys.add(("name", found.group(1).lower(), css_unescape(found.group(2))))
        else:
            keys.add(("text", " ".join(alternative.replace(">", " > ").split())))
    return frozenset(keys)
