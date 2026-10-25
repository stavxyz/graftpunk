"""The DOM snapshot ``gp observe`` takes at a credential post (#267).

A login modal a page's script renders after a click is in no recorded HTML
response, and has closed by the time the page source is saved at stop. The
recorder serializes the live page when a credential-shaped POST is sent, and
this module reduces that HTML to what the digest reads before it is written: the
tags and the few attributes a login form's selectors are made of, and the short
text of links, buttons, and labels. Everything a page renders from its data
(values, text, scripts, styles) is dropped, and the triggering post's own
values are redacted from what is left.
"""

from __future__ import annotations

import json
from html import escape
from html.parser import HTMLParser
from urllib.parse import parse_qsl

from graftpunk.har.digest import password_field_names
from graftpunk.observe.run import Redaction, redact_text
from graftpunk.session_identity import is_identifier_field

__all__ = [
    "KEPT_ATTRIBUTES",
    "TEXT_LIMIT",
    "sanitize_dom_snapshot",
    "redact_dom_snapshot",
    "snapshot_redaction",
    "stored_dom_snapshot",
]

# The attributes the digest reads a login form from (graftpunk.har.documents), plus
# href and for, which say what a link opens and which input a label names.
KEPT_ATTRIBUTES = frozenset(
    {
        "id",
        "name",
        "type",
        "autocomplete",
        "form",
        "role",
        "aria-modal",
        "action",
        "method",
        "class",
        "href",
        "for",
    }
)
# The elements whose text is kept: what a user reads to find and press the login.
_TEXT_ELEMENTS = frozenset({"a", "button", "label", "summary", "legend", "option"})
# The elements whose contents are dropped whole, tags included.
_DROPPED_CONTENT = frozenset({"script", "style", "template"})
# How many characters of text one labelling element keeps, counted across its
# descendants' text too.
TEXT_LIMIT = 80
_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        # Open elements whose contents are dropped, by tag, and the text budget left
        # to each open labelling element, innermost last.
        self._dropping: list[str] = []
        self._budgets: list[list[int]] = []
        self._text_open: dict[str, int] = {}
        self._textarea = 0

    def _tag(self, tag: str, attrs: list[tuple[str, str | None]], close: str) -> str:
        kept = "".join(
            f' {name}="{escape(value or "", quote=True)}"'
            for name, value in attrs
            if name in KEPT_ATTRIBUTES
        )
        return f"<{tag}{kept}{close}>"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._dropping:
            if tag == self._dropping[0]:
                self._dropping.append(tag)
            return
        self.out.append(self._tag(tag, attrs, ""))
        if tag in _DROPPED_CONTENT:
            self._dropping.append(tag)
        elif tag == "textarea":
            self._textarea += 1
        elif tag in _TEXT_ELEMENTS:
            # A nested labelling element shares its ancestor's budget.
            self._budgets.append(self._budgets[-1] if self._budgets else [TEXT_LIMIT])
            self._text_open[tag] = self._text_open.get(tag, 0) + 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._dropping:
            return
        if tag in _VOID:
            self.out.append(self._tag(tag, attrs, ""))
        else:
            self.out.append(self._tag(tag, attrs, " /"))

    def handle_endtag(self, tag: str) -> None:
        if self._dropping:
            if tag == self._dropping[0]:
                self._dropping.pop()
                if not self._dropping:
                    self.out.append(f"</{tag}>")
            return
        if tag in _VOID:
            return
        if tag == "textarea" and self._textarea:
            self._textarea -= 1
        elif tag in _TEXT_ELEMENTS and self._text_open.get(tag):
            self._text_open[tag] -= 1
            self._budgets.pop()
        self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._dropping or self._textarea or not self._budgets:
            return
        budget = self._budgets[-1]
        text = data[: budget[0]]
        budget[0] -= len(text)
        if text:
            self.out.append(escape(text, quote=False))

    def handle_decl(self, decl: str) -> None:
        if decl.lower().startswith("doctype"):
            self.out.append(f"<!{escape(decl, quote=False)}>")


def sanitize_dom_snapshot(html: str) -> str:
    """*html* (a page's serialized DOM) with only what the digest reads: every
    element and its kept attributes (``KEPT_ATTRIBUTES``: never ``value``,
    ``checked``, ``selected``, ``style``, a ``data-`` attribute, or an event
    handler), the contents of ``<script>``, ``<style>``, and ``<template>`` dropped,
    a ``<textarea>``'s text dropped, and any other text dropped unless it sits in an
    ``a``, ``button``, ``label``, ``summary``, ``legend``, or ``option``, which keeps
    at most ``TEXT_LIMIT`` characters. Comments and processing instructions are
    dropped. Every kept value and text is escaped again, so the output is markup
    the page did not write only where it was markup before."""
    parser = _Sanitizer()
    parser.feed(html)
    parser.close()
    return "".join(parser.out)


def _body_values(body_text: str | None) -> list[tuple[str, str]]:
    """The top-level field names and values of a JSON object or form-encoded body;
    a number or a boolean as its text. Empty for any other body."""
    if not body_text:
        return []
    try:
        parsed = json.loads(body_text)
    except ValueError:
        return parse_qsl(body_text, keep_blank_values=False) if "=" in body_text else []
    if not isinstance(parsed, dict):
        return []
    return [
        (str(name), value if isinstance(value, str) else json.dumps(value))
        for name, value in parsed.items()
        if isinstance(value, str | int | float) and not isinstance(value, bool)
    ]


def snapshot_redaction(body_text: str | None, content_type: str = "") -> Redaction:
    """What a snapshot is scrubbed of: the triggering post's own values, its
    password-like fields' (:func:`graftpunk.har.digest.password_field_names`) as
    secrets, replaced wherever they occur, and its identifier fields'
    (:func:`graftpunk.session_identity.is_identifier_field`, an allowlist) as
    identifiers, replaced where they stand as a whole value. Another field's value
    (``remember=1``) is left alone: replacing it everywhere would mangle the
    markup."""
    password_names = set(password_field_names(body_text, content_type))
    secrets: list[str] = []
    identifiers: list[str] = []
    for name, value in _body_values(body_text):
        if not value:
            continue
        if name in password_names:
            secrets.append(value)
        elif is_identifier_field(name):
            identifiers.append(value)
    return Redaction(tuple(secrets), tuple(identifiers))


def redact_dom_snapshot(sanitized: str, body_text: str | None, content_type: str = "") -> str:
    """*sanitized* (:func:`sanitize_dom_snapshot`'s output) with the triggering post's
    values redacted (:func:`snapshot_redaction`) by
    :func:`graftpunk.observe.run.redact_text`, the scrub the rest of a run uses."""
    return redact_text(sanitized, snapshot_redaction(body_text, content_type))


def stored_dom_snapshot(html: str, body_text: str | None, content_type: str = "") -> str:
    """*html* as a run stores it: sanitized (:func:`sanitize_dom_snapshot`), then
    redacted of the triggering post's values (:func:`redact_dom_snapshot`)."""
    return redact_dom_snapshot(sanitize_dom_snapshot(html), body_text, content_type)
