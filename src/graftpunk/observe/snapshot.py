"""The DOM snapshot ``gp observe`` takes at a credential post (#267).

A login modal a page's script renders after a click is in no recorded HTML
response, and has closed by the time the page source is saved at stop. The
recorder serializes the live page when a credential-shaped POST is sent, and
this module reduces that HTML to what the digest reads before it is written: the
tags and the few attributes a login form's selectors are made of, and the short
text of links, buttons, and labels. Everything a page renders from its data
(values, text, scripts, styles) is dropped, and the triggering post's own
values are redacted from what is left.

Redaction reads one attribute value or one text at a time, and the stored markup
once more as a whole: a value the page renders across elements
(``hun<b>ter22</b>``) is kept, a documented limit.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from html import escape
from html.parser import HTMLParser
from urllib.parse import quote

from graftpunk.har.digest import body_values, password_field_names
from graftpunk.observe.run import REDACTED, Redaction, text_redactor
from graftpunk.session_identity import identifier_key, is_identifier_field

__all__ = [
    "KEPT_ATTRIBUTES",
    "TEXT_LIMIT",
    "sanitize_dom_snapshot",
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


# The attributes that hold a URL: kept without their query or fragment.
_URL_ATTRIBUTES = frozenset({"href", "action"})


# What a browser's URL parser removes before reading a scheme: ASCII tab and
# newline anywhere, and C0 control characters and spaces at either end.
_URL_TABS_RE = re.compile(r"[\t\n\r]")
_URL_EDGES = "".join(chr(code) for code in range(0x21))
_SCHEME_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*):")
# Schemes whose whole text is data or code, not a place: only the scheme is kept.
_OPAQUE_SCHEMES = frozenset({"javascript", "data", "vbscript"})
_PATH_PARAMS_RE = re.compile(r";[^/]*")


def _bare_reference(value: str) -> str:
    """*value* (an ``href`` or ``action``) with no query, fragment, or ``;params``
    value: ``/login;jsessionid=AB/next?x=1#top`` becomes ``/login;/next?``, so the
    digest's scoped selectors still match the live form, whose action starts with or
    holds those markers. A ``javascript:``, ``data:``, or ``vbscript:`` URL keeps only
    its scheme, read as a browser reads it (tabs and newlines removed, controls and
    spaces trimmed, any case). The path is kept, and can hold a token or an account
    value (a ``/reset/<token>`` link)."""
    cleaned = _URL_TABS_RE.sub("", value).strip(_URL_EDGES)
    scheme = _SCHEME_RE.match(cleaned)
    if scheme and scheme.group(1).lower() in _OPAQUE_SCHEMES:
        return scheme.group(1).lower() + ":"
    cut = min((i for i in (value.find("?"), value.find("#")) if i != -1), default=-1)
    head, tail = (value, "") if cut == -1 else (value[:cut], value[cut])
    return _PATH_PARAMS_RE.sub(";", head) + tail


class _Sanitizer(HTMLParser):
    def __init__(self, redact: Callable[[str], str]) -> None:
        super().__init__(convert_charrefs=True)
        # Run on each decoded value and text before it is escaped again: an escaped
        # value (p&amp;ss) is not a spelling the scrub looks for.
        self._redact = redact
        self.out: list[str] = []
        # Open elements whose contents are dropped, by tag, and the text budget left
        # to each open labelling element, innermost last.
        self._dropping: list[str] = []
        self._budgets: list[list[int]] = []
        self._text_open: dict[str, int] = {}
        self._textarea = 0

    def _tag(self, tag: str, attrs: list[tuple[str, str | None]], close: str) -> str:
        kept = "".join(
            f' {name}="{escape(self._kept_value(name, value or ""), quote=True)}"'
            for name, value in attrs
            if name in KEPT_ATTRIBUTES
        )
        return f"<{tag}{kept}{close}>"

    def _kept_value(self, name: str, value: str) -> str:
        if name in _URL_ATTRIBUTES:
            value = _bare_reference(value)
        return self._redact(value)

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
        text = self._redact(data)[: budget[0]]
        budget[0] -= len(text)
        if text:
            self.out.append(escape(text, quote=False))

    def handle_decl(self, decl: str) -> None:
        if decl.lower().startswith("doctype"):
            self.out.append(f"<!{escape(decl, quote=False)}>")


def sanitize_dom_snapshot(html: str, redaction: Redaction | None = None) -> str:
    """*html* (a page's serialized DOM) with only what the digest reads: every
    element and its kept attributes (``KEPT_ATTRIBUTES``: never ``value``,
    ``checked``, ``selected``, ``style``, a ``data-`` attribute, or an event
    handler), the contents of ``<script>``, ``<style>``, and ``<template>`` dropped,
    a ``<textarea>``'s text dropped, and any other text dropped unless it sits in an
    ``a``, ``button``, ``label``, ``summary``, ``legend``, or ``option``, which keeps
    at most ``TEXT_LIMIT`` characters. Comments and processing instructions are
    dropped. An ``href`` or ``action`` keeps no query, fragment, or ``;params``
    value, and a ``javascript:``, ``data:``, or ``vbscript:`` URL only its scheme
    (:func:`_bare_reference`). With *redaction*, each kept value and text is
    redacted on its decoded text (:func:`_redactor`, an email in any letter case),
    before the text budget cuts it, identifiers included. Every kept value and text
    is then escaped again, so the output is markup the page did not write only
    where it was markup before."""
    parser = _Sanitizer(_redactor(redaction or Redaction()))
    parser.feed(html)
    parser.close()
    return "".join(parser.out)


def _redactor(redaction: Redaction) -> Callable[[str], str]:
    """:func:`graftpunk.observe.run.text_redactor` for *redaction*, and then each
    identifier spelling that holds an email (``@`` or ``%40``) replaced in any letter
    case: a page often shows an email lowercased, or as the user typed it."""
    scrub = text_redactor(redaction)
    emails = sorted(
        {value for value in redaction.identifiers if "@" in value or "%40" in value.lower()},
        key=len,
        reverse=True,
    )
    if not emails:
        return scrub
    pattern = re.compile("|".join(re.escape(email) for email in emails), re.IGNORECASE)
    return lambda text: pattern.sub(REDACTED, scrub(text))


def _spellings(value: str) -> list[str]:
    """*value* and the spellings a page can show it in that the run's scrub does not
    look for itself: as ``encodeURIComponent`` writes it (``'`` and ``!*()~`` left
    raw), and each of the two HTML-escaped (``html.escape`` with and without
    quotes)."""
    encoded = quote(value, safe="-_.!~*'()")
    return list(
        dict.fromkeys(
            spelled
            for base in (value, encoded)
            for spelled in (base, escape(base, quote=True), escape(base, quote=False))
        )
    )


def snapshot_redaction(body_text: str | None, content_type: str = "") -> Redaction:
    """What a snapshot is scrubbed of: the triggering post's own values, its
    password-like fields' (:func:`graftpunk.har.digest.password_field_names`) as
    secrets, replaced wherever they occur, and its identifier fields'
    (:func:`graftpunk.session_identity.is_identifier_field`, an allowlist) as
    identifiers, replaced where they stand as a whole value. Another field's value
    (``remember=1``) is left alone: replacing it everywhere would mangle the
    markup. The values are read by :func:`graftpunk.har.digest.body_values` (a
    nested JSON value under its nearest name, a multipart text part), and a
    Rails-style name by its last bracketed part
    (:func:`graftpunk.session_identity.identifier_key`). Each value's other
    spellings (:func:`_spellings`) are added beside it, a second net under the
    sanitizer's own redaction of decoded text."""
    password_names = set(password_field_names(body_text, content_type))
    secrets: list[str] = []
    identifiers: list[str] = []
    for path, value in body_values(body_text, content_type):
        name = path[-1] if path else ""
        if not value:
            continue
        if name in password_names:
            secrets.extend(_spellings(value))
        elif is_identifier_field(identifier_key(name)):
            identifiers.extend(_spellings(value))
    return Redaction(tuple(secrets), tuple(identifiers))


def stored_dom_snapshot(html: str, body_text: str | None, content_type: str = "") -> str:
    """*html* as a run stores it: sanitized (:func:`sanitize_dom_snapshot`) with the
    triggering post's values (:func:`snapshot_redaction`) redacted from each decoded
    value and text, then scrubbed of them once more as markup (:func:`_redactor`, the
    scrub the rest of a run uses, emails in any letter case)."""
    redaction = snapshot_redaction(body_text, content_type)
    return _redactor(redaction)(sanitize_dom_snapshot(html, redaction))
