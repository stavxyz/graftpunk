"""Read the token values a page carries: each ``<meta name content>`` and each
named hidden input.

One reader serves both sides of a token pairing (#369): the digest compares a
request header with these values, and ``Token.from_meta_tag`` and
``Token.from_hidden_input`` extract them at runtime, so a pairing the digest makes
is the value the generated ``Token`` sends. It is built on ``html.parser``:
attributes in any order and case, quoted or not, character references decoded,
and nothing read inside ``<script>``, ``<style>``, ``<template>``, ``<textarea>``,
or a comment, none of which a browser renders as an element there.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Literal

__all__ = ["PageValueKind", "page_values"]

PageValueKind = Literal["meta", "hidden_input"]

# Elements whose content is text or inert markup, never a meta tag or input on the
# page. html.parser treats script and style as raw text already; the others it
# would parse as tags.
_INERT_CONTENT = frozenset({"script", "style", "template", "textarea"})


class _PageValueParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[tuple[PageValueKind, str], str] = {}
        self._inert = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _INERT_CONTENT:
            self._inert += 1
            return
        if self._inert:
            return
        values: dict[str, str] = {}
        for key, value in attrs:
            # A repeated attribute keeps its first value, as a browser does.
            values.setdefault(key, value or "")
        found: tuple[PageValueKind, str, str] | None = None
        if tag == "meta":
            found = ("meta", values.get("name", ""), values.get("content", ""))
        elif tag == "input" and values.get("type", "").strip().lower() == "hidden":
            found = ("hidden_input", values.get("name", ""), values.get("value", ""))
        if found is not None and found[1] and found[2]:
            self.values.setdefault((found[0], found[1]), found[2])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # A self-closed <template/> or <textarea/> holds nothing to skip.
        if tag not in _INERT_CONTENT:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _INERT_CONTENT and self._inert:
            self._inert -= 1


def page_values(html: str) -> dict[tuple[PageValueKind, str], str]:
    """``{(kind, name): value}`` for *html*: kind ``meta`` for each ``<meta>`` with
    a ``name`` and a non-empty ``content``, kind ``hidden_input`` for each
    ``<input type="hidden">`` with a ``name`` and a non-empty ``value``, in a form
    or not. When several carry one name, the first in document order wins, as
    ``document.querySelector`` would find it. Names are as written (case kept);
    values are decoded."""
    parser = _PageValueParser()
    parser.feed(html)
    parser.close()
    return parser.values
