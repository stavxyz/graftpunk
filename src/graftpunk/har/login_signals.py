"""Suggested ``LoginConfig.success`` selectors read from a recording (#268).

A run usually holds a logged-in page (``page-source.html`` at stop, or a page the
browser loaded after the credential post) and one or more logged-out pages (the
login page as served, the DOM snapshot taken as the password was submitted, and any
page loaded before the login). An element on the logged-in page and on none of the
logged-out pages is a candidate for the ``success`` signal, which the engine tests
as element presence.

Only ever a suggestion. One recording cannot show that an element is absent from
the login page the engine opens on a fresh visit (a modal on the home page in the
recording, a dedicated ``/login`` page at replay), and single-recording inference
picked wrong elements on real sites in #267, so the generator writes these
commented out for the author to confirm.

The matcher here understands only the two selector forms it emits, ``#id`` and
``tag.token``, both restricted to plain CSS identifiers, so it needs no CSS engine
at run time; ``tests/unit/test_login_signals.py`` checks every emitted selector
against lxml and cssselect.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser

from graftpunk.har.paths import holds_an_id
from graftpunk.har.selectors import bounded_selector, selector_holds_an_id

__all__ = ["MAX_SUCCESS_CANDIDATES", "success_candidates"]

MAX_SUCCESS_CANDIDATES = 3

# A plain CSS identifier: the only ids and class tokens suggested, so a selector
# needs no escaping and the matcher below agrees with a browser's.
_IDENTIFIER_RE = re.compile(r"-?[A-Za-z_][A-Za-z0-9_-]*")
# What a logged-in marker's id or class tends to hold, compared with "-" and "_"
# removed and in lower case.
_KEYWORDS = ("logged", "account", "signout", "logout", "user", "profile", "avatar")
# A marker of the logged-out state, never suggested.
_LOGGED_OUT = ("loggedout", "signedout")
_LOGOUT_HREF_RE = re.compile(r"log-?out|sign-?out", re.IGNORECASE)
# Elements that render nothing; the engine finds them, but they mark no page.
_NOT_RENDERED = frozenset(
    {"head", "title", "meta", "link", "base", "script", "style", "noscript", "template"}
)
# Elements whose descendants a script-enabled browser does not put in the document
# (a template's content, a noscript's markup).
_INERT_CONTAINERS = frozenset({"template", "noscript"})
# Generated names: a CSS-module class (Header_loggedIn__x7Q2a), a CSS-in-JS class
# (css-1x2y3z, sc-bdVaJa, jsx-2738465, emotion-0, svelte-1abc2de), a part mixing
# letters and digits (build hashes), and a run of three or more digits (ember1234,
# react-select-12-input).
_CSS_MODULE_RE = re.compile(r"[A-Za-z0-9]+_[A-Za-z0-9]+__[A-Za-z0-9_-]{4,}")
_CSS_IN_JS_PREFIXES = ("css-", "sc-", "jsx-", "emotion-", "svelte-", "styled-")
_DIGIT_RUN_RE = re.compile(r"[0-9]{3,}")
_MIXED_PART_RE = re.compile(r"(?=[A-Za-z0-9]*[0-9])(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{4,}")


@dataclass(frozen=True)
class _Element:
    tag: str
    element_id: str
    classes: tuple[str, ...]
    href: str
    # Inside a template or a noscript: not in a script-enabled document.
    inert: bool


class _ElementReader(HTMLParser):
    """Every element's tag, id, class tokens, and href, in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[_Element] = []
        self._inert_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        self.elements.append(
            _Element(
                tag=tag,
                element_id=values.get("id", ""),
                classes=tuple(values.get("class", "").split()),
                href=values.get("href", ""),
                inert=self._inert_depth > 0,
            )
        )
        if tag in _INERT_CONTAINERS:
            self._inert_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # A self-closing <template/> opens nothing.
        self.handle_starttag(tag, attrs)
        if tag in _INERT_CONTAINERS:
            self._inert_depth -= 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _INERT_CONTAINERS and self._inert_depth > 0:
            self._inert_depth -= 1


def _elements(html: str) -> list[_Element]:
    reader = _ElementReader()
    try:
        reader.feed(html)
        reader.close()
    except (AssertionError, ValueError):
        # HTMLParser raises on very little; what was read before stands.
        pass
    return reader.elements


def _looks_generated(name: str) -> bool:
    """Whether *name*, an id or a class token, looks built rather than chosen: not a
    plain CSS identifier, a generated name, an account value, or a logged-out marker."""
    if not _IDENTIFIER_RE.fullmatch(name):
        return True
    compact = name.replace("-", "").replace("_", "").lower()
    return (
        bool(_CSS_MODULE_RE.fullmatch(name))
        or name.lower().startswith(_CSS_IN_JS_PREFIXES)
        or bool(_DIGIT_RUN_RE.search(name))
        or any(_MIXED_PART_RE.fullmatch(part) for part in re.split(r"[-_]", name))
        or any(marker in compact for marker in _LOGGED_OUT)
        or holds_an_id(name)
    )


def _selectors(element: _Element) -> list[tuple[str, str, bool]]:
    """Each selector *element* could be suggested by, as (selector, the name it is
    built on, whether it is an id), id first."""
    found: list[tuple[str, str, bool]] = []
    if element.element_id and not _looks_generated(element.element_id):
        found.append((f"#{element.element_id}", element.element_id, True))
    for token in dict.fromkeys(element.classes):
        if not _looks_generated(token):
            found.append((f"{element.tag}.{token}", token, False))
    return found


def _count(selector: str, elements: Sequence[_Element]) -> int:
    """How many of *elements* *selector*, one :func:`_selectors` gives, selects."""
    if selector.startswith("#"):
        return sum(element.element_id == selector[1:] for element in elements)
    tag, _, token = selector.partition(".")
    return sum(element.tag == tag and token in element.classes for element in elements)


def _is_keyword(name: str, element: _Element) -> bool:
    compact = name.replace("-", "").replace("_", "").lower()
    return any(word in compact for word in _KEYWORDS) or (
        element.tag == "a" and bool(_LOGOUT_HREF_RE.search(element.href))
    )


def success_candidates(before: Sequence[str], after: Sequence[str]) -> tuple[str, ...]:
    """Up to :data:`MAX_SUCCESS_CANDIDATES` selectors for an element on a logged-in
    page and on no login page: each selects exactly one element on every page of
    *after* (the logged-in pages, as HTML) that holds it, and none on any page of
    *before* (the logged-out pages: every recorded copy of the login page, the DOM
    snapshot taken at the credential post, and any page loaded before the login).
    Empty when *after* is.

    A selector is ``#id`` or ``tag.token`` over a plain CSS identifier, and never one
    that looks generated (a CSS-module or CSS-in-JS class, a part mixing letters and
    digits, a run of three or more digits), that names a logged-out state, or that
    holds an account value (:func:`graftpunk.har.paths.holds_an_id`,
    :func:`graftpunk.har.selectors.selector_holds_an_id`). An element that renders
    nothing (in the head, a script, a template's content) is not suggested.

    Ranked: an id or class holding ``logged``, ``account``, ``signout``, ``logout``,
    ``user``, ``profile``, or ``avatar`` (``-`` and ``_`` ignored), or the id or a class
    of a link whose href holds ``logout`` or ``signout``, first; then an id before a
    class; then the selector found on more of *after*; then document order. Only
    selectors are returned, never text or attribute values."""
    before_pages = [_elements(html) for html in before]
    after_pages = [_elements(html) for html in after]
    # selector -> (rank key without its order, first seen order)
    ranked: dict[str, tuple[tuple[int, int], int]] = {}
    pages_holding: dict[str, int] = {}
    rejected: set[str] = set()
    order = 0
    for page in after_pages:
        seen_here: set[str] = set()
        for element in page:
            order += 1
            if element.inert or element.tag in _NOT_RENDERED:
                continue
            for selector, name, is_id in _selectors(element):
                if selector in rejected or selector in seen_here:
                    continue
                seen_here.add(selector)
                printable = bounded_selector(selector)
                if (
                    printable is None
                    or selector_holds_an_id(printable)
                    or _count(selector, page) != 1
                    or any(_count(selector, copy) for copy in before_pages)
                ):
                    rejected.add(selector)
                    ranked.pop(selector, None)
                    pages_holding.pop(selector, None)
                    continue
                pages_holding[selector] = pages_holding.get(selector, 0) + 1
                if selector not in ranked:
                    key = (0 if _is_keyword(name, element) else 1, 0 if is_id else 1)
                    ranked[selector] = (key, order)
    best = sorted(ranked, key=lambda s: (*ranked[s][0], -pages_holding[s], ranked[s][1]))
    return tuple(best[:MAX_SUCCESS_CANDIDATES])
