"""The DOM snapshot ``gp observe`` takes at a credential post (#267).

A login modal a page's script renders after a click is in no recorded HTML
response, and has closed by the time the page source is saved at stop. The
recorder serializes the live page when a credential-shaped POST is sent, and
this module reduces that HTML to what the digest reads before it is written: the
tags and the few attributes a login form's selectors are made of, and the short
text of links, buttons, and labels. Everything a page renders from its data
(values, text, scripts, styles) is dropped, and the triggering post's own
values are redacted from what is left.

The read the recorder takes of the page after a credential post's response (#268)
is a logged-in page, and goes through the structure-only mode, which keeps no text
at all and redacts more of the account from ids, classes, and paths.

Redaction reads one attribute value or one text at a time, decoded and then once
more as escaped: a value the page renders across elements (``hun<b>ter22</b>``) is
kept, a documented limit. It never changes an attribute's name or a plain element
name.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from html import escape
from html.parser import HTMLParser
from urllib.parse import quote

from graftpunk.har.digest import body_values, password_field_names, post_account_values
from graftpunk.har.documents import extract_login_forms
from graftpunk.har.login_signals import could_be_an_account_value
from graftpunk.observe.run import _MIN_REDACT_LEN, REDACTED, Redaction, text_redactor
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
# The attributes whose values a structure-only read also redacts the account's words
# from (_account_scrub): ids, class tokens, an id a label or a control names, and
# paths. Never ``name`` (a field's name is the key a login form's selectors use) nor
# a fixed vocabulary (type, autocomplete, method, role, aria-modal).
_ACCOUNT_ATTRIBUTES = frozenset({"id", "class", "for", "form", "href", "action"})


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


# An email address in an attribute value (a mailto: link, a profile path). A
# structure-only read drops it whoever's it is: the page is a logged-in one, and the
# account's email need not be the one the post sent. The local part is a run of 1 to
# 64 characters (RFC 5321's limit) but whitespace, quotes, angle brackets, and URL
# delimiters ("/", ":", "?", "#", "&", "=", ";", ","), so a path or a scheme before
# it is kept and a non-ASCII local part is taken whole. The "@" may be written "%40",
# "%2540" (encoded twice), or a full-width "\uff20". The host has a dot: 2 to 10
# labels of at most 63 characters; a host without one (_email_pattern) is matched
# only when it is the posted email's host. Every repeat is bounded, so the pattern
# runs in time linear in a value's length (with an unbounded local part, a
# 100,000-character id took minutes).
_EMAIL_LOCAL = r"[^\s/:@\uff20\"'<>?#&=;,]{1,64}"
_EMAIL_AT = r"(?:@|\uff20|%40|%2540)"
_DOTTED_HOST = r"[\w-]{1,63}(?:\.[\w-]{1,63}){1,9}"
_EMAIL_RE = re.compile(f"{_EMAIL_LOCAL}{_EMAIL_AT}{_DOTTED_HOST}", re.IGNORECASE)
# An element name written as it is (HTML lower-cases it); any other is written as
# _SAFE_TAG, as is a name holding the post's password.
_PLAIN_TAG_RE = re.compile(r"[a-z][a-z0-9-]*")
_SAFE_TAG = "x-redacted"
# Every doctype is written as this one: a doctype's text is free, and the digest
# reads none of it.
_DOCTYPE = "<!DOCTYPE html>"


def _email_pattern(redaction: Redaction) -> re.Pattern[str]:
    """:data:`_EMAIL_RE`, and an address at a host with no dot when that host is the
    one an identifier of *redaction* (the posted email) has (``sybil@intranet``)."""
    hosts = sorted(
        {
            re.split(r"@|%40", value, maxsplit=1, flags=re.IGNORECASE)[1].lower()
            for value in redaction.identifiers
            if re.search(r"@|%40", value, flags=re.IGNORECASE)
        }
        - {""},
        key=len,
        reverse=True,
    )
    dotless = [host for host in hosts if "." not in host and re.fullmatch(r"[\w-]{1,63}", host)]
    if not dotless:
        return _EMAIL_RE
    named = "|".join(re.escape(host) for host in dotless)
    return re.compile(
        f"{_EMAIL_LOCAL}{_EMAIL_AT}(?:{_DOTTED_HOST}|(?:{named})(?![\\w.-]))", re.IGNORECASE
    )


class _Sanitizer(HTMLParser):
    def __init__(
        self,
        redact: Callable[[str], str],
        *,
        secrets: Callable[[str], str],
        account: Callable[[str], str] | None = None,
        emails: re.Pattern[str] | None = None,
    ) -> None:
        super().__init__(convert_charrefs=True)
        # Run on each decoded value and text, and again on it escaped: the scrub
        # looks for the escaped spellings (p&amp;ss) too.
        self._redact = redact
        # The post's password-like values alone, which no element name may hold.
        self._secrets = secrets
        # Structure only: the account's words in ids, classes, and paths
        # (_ACCOUNT_ATTRIBUTES), no text, and every email in a kept value replaced.
        self._account = account
        self._emails = emails
        self.out: list[str] = []
        # Open elements whose contents are dropped, by tag, and the text budget left
        # to each open labelling element, innermost last.
        self._dropping: list[str] = []
        self._budgets: list[list[int]] = []
        self._text_open: dict[str, int] = {}
        self._textarea = 0
        # The text since the last tag, in the chunks the parser gave (a comment
        # between two splits it), redacted as one (_flush_text).
        self._text: list[str] = []

    def _tag(self, tag: str, attrs: list[tuple[str, str | None]], close: str) -> str:
        kept = "".join(
            f' {name}="{self._kept_value(name, value or "")}"'
            for name, value in attrs
            if name in KEPT_ATTRIBUTES
        )
        return f"<{self._name(tag)}{kept}{close}>"

    def _name(self, tag: str) -> str:
        """*tag* as written when it is plain: no account value is redacted from an
        element name, so ``main`` stays ``main`` for the username ``main``. Else
        :data:`_SAFE_TAG`: broken markup can make a name of an email
        (``<ivan@x.example>``), and a name holding the post's password is never
        written."""
        if _PLAIN_TAG_RE.fullmatch(tag) and self._secrets(tag) == tag:
            return tag
        return _SAFE_TAG

    def _kept_value(self, name: str, value: str) -> str:
        """*value* of the kept attribute *name*, redacted and escaped for the markup."""
        if name in _URL_ATTRIBUTES:
            value = _bare_reference(value)
        account = self._account if name in _ACCOUNT_ATTRIBUTES else None
        value = self._redact(value)
        if account is not None:
            value = account(value)
        if self._emails is not None:
            value = self._emails.sub(REDACTED, value)
        escaped = self._redact(escape(value, quote=True))
        return account(escaped) if account is not None else escaped

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._flush_text()
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
        self._flush_text()
        if self._dropping:
            return
        if tag in _VOID:
            self.out.append(self._tag(tag, attrs, ""))
        else:
            self.out.append(self._tag(tag, attrs, " /"))

    def handle_endtag(self, tag: str) -> None:
        self._flush_text()
        if self._dropping:
            if tag == self._dropping[0]:
                self._dropping.pop()
                if not self._dropping:
                    self.out.append(f"</{self._name(tag)}>")
            return
        if tag in _VOID:
            return
        if tag == "textarea" and self._textarea:
            self._textarea -= 1
        elif tag in _TEXT_ELEMENTS and self._text_open.get(tag):
            self._text_open[tag] -= 1
            self._budgets.pop()
        self.out.append(f"</{self._name(tag)}>")

    def handle_data(self, data: str) -> None:
        self._text.append(data)

    def _flush_text(self) -> None:
        """Write the text since the last tag, when it is kept: redacted whole, so a
        value a comment splits (``hun<!-- x -->ter22``) is still found, then cut to
        the budget, escaped, and redacted once more as escaped."""
        data = "".join(self._text)
        self._text.clear()
        if (
            not data
            or self._dropping
            or self._textarea
            or not self._budgets
            or self._emails is not None
        ):
            return
        budget = self._budgets[-1]
        text = self._redact(data)[: budget[0]]
        budget[0] -= len(text)
        if text:
            self.out.append(self._redact(escape(text, quote=False)))

    def close(self) -> None:
        super().close()
        self._flush_text()

    def handle_decl(self, decl: str) -> None:
        self._flush_text()
        if decl.lower().startswith("doctype"):
            self.out.append(_DOCTYPE)


def sanitize_dom_snapshot(
    html: str,
    redaction: Redaction | None = None,
    *,
    structure_only: bool = False,
    account_values: Iterable[str] = (),
    field_names: Iterable[str] = (),
) -> str:
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
    before the text budget cuts it, identifiers included, then escaped again and
    redacted once more as escaped, so the output is markup the page did not write
    only where it was markup before. An attribute's name is never redacted. A
    doctype is written ``<!DOCTYPE html>``, and an element name that is not plain
    (``[a-z][a-z0-9-]*``) or that holds one of *redaction*'s secrets is written
    ``x-redacted``.

    With *structure_only* (the read of a logged-in page after a credential post), no
    text is kept at all, the labelling elements' included; each kept attribute
    value has every email address in it replaced, whoever's it is
    (:func:`_email_pattern`); and an ``id``, ``class``, ``for``, ``form``, ``href``,
    or ``action`` value has the account's words replaced in any letter case where
    they stand as a whole word (:func:`_account_words`: *redaction*'s identifiers
    and *account_values*, an email's local part, their separator and
    percent-encoded spellings), but never a word that is one of *field_names* (the
    post's own field names), any case aside."""
    redaction = redaction or Redaction()
    parser = _Sanitizer(
        _redactor(redaction),
        secrets=text_redactor(redaction.secrets),
        account=_account_scrub([*redaction.identifiers, *account_values], field_names)
        if structure_only
        else None,
        emails=_email_pattern(redaction) if structure_only else None,
    )
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


def _account_scrub(
    values: Iterable[str], field_names: Iterable[str]
) -> Callable[[str], str] | None:
    """Each of :func:`_account_words` of *values* replaced in any letter case where it
    stands as a whole word (no ASCII letter or digit beside it): a profile path or a
    class often spells the username lower-cased (``/users/alice``), or only the
    email's local part (``menu-alice.smith``). None when there is no such word."""
    words = _account_words(values, field_names)
    if not words:
        return None
    whole = re.compile(
        "(?<![A-Za-z0-9])(?:" + "|".join(re.escape(word) for word in words) + ")(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    return lambda text: whole.sub(REDACTED, text)


_AT_RE = re.compile(r"@|%40", re.IGNORECASE)
_SEPARATORS_RE = re.compile(r"[._-]+")


def _account_words(values: Iterable[str], field_names: Iterable[str]) -> list[str]:
    """The words a structure-only read redacts, longest first: each of *values* and
    each email's local part among them, each also with its ``.``, ``-``, and ``_``
    written as any one of them or as nothing (``alice.smith`` as ``alice-smith``,
    ``alice_smith``, ``alicesmith``) and each of those percent-encoded
    (``alice%2Esmith``); those that could name an account
    (:func:`graftpunk.har.login_signals.could_be_an_account_value`: three or more
    characters, not a boolean word), and never one of *field_names*, any case
    aside, so a field the page names as the account (``user=user%40example.com``,
    ``<input name="user" id="user">``) keeps its selector."""
    names = {name.casefold() for name in field_names}
    found: set[str] = set()
    for value in values:
        text = value.strip()
        for word in (text, _AT_RE.split(text, maxsplit=1)[0] if _AT_RE.search(text) else ""):
            pieces = [piece for piece in _SEPARATORS_RE.split(word) if piece]
            spelled = {word, *(sep.join(pieces) for sep in (".", "-", "_", ""))}
            found |= spelled | {_percent_encoded(each) for each in spelled}
    return sorted(
        (
            word
            for word in found
            if len(word) >= _MIN_REDACT_LEN
            and could_be_an_account_value(word)
            and word.casefold() not in names
        ),
        key=len,
        reverse=True,
    )


def _percent_encoded(word: str) -> str:
    """*word* with every character but an ASCII letter or digit percent-encoded as
    UTF-8 (``alice.smith`` as ``alice%2Esmith``); matched in any letter case."""
    return "".join(
        char
        if char.isascii() and char.isalnum()
        else "".join(f"%{byte:02X}" for byte in char.encode())
        for char in word
    )


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


def stored_dom_snapshot(
    html: str,
    body_text: str | None,
    content_type: str = "",
    *,
    structure_only: bool = False,
    login_page: str | None = None,
) -> str:
    """*html* as a run stores it: sanitized (:func:`sanitize_dom_snapshot`, with no
    text at all when *structure_only*) with the triggering post's values
    (:func:`snapshot_redaction`) redacted from each value and text, decoded and as
    escaped (:func:`_redactor`, the scrub the rest of a run uses, emails in any
    letter case).

    With *structure_only* (the read of the page after the post), the words of every
    value the digest's username guard takes for an account value are redacted too
    (:func:`graftpunk.har.digest.post_account_values`, by the same rule: the field
    the login form on *login_page*, the post's stored snapshot, takes as its
    username, an identifier field, or, when neither is in the post, every value but
    a token field's), but never the post's own field names."""
    redaction = snapshot_redaction(body_text, content_type)
    if not structure_only:
        return sanitize_dom_snapshot(html, redaction)
    usernames = (
        {
            form.username_name
            for form in extract_login_forms(login_page, source="", snapshot=True)
            if form.username_name
        }
        if login_page
        else set()
    )
    names = {
        each
        for path, _value in body_values(body_text, content_type)
        for name in path
        for each in (name, identifier_key(name))
    }
    return sanitize_dom_snapshot(
        html,
        redaction,
        structure_only=True,
        account_values=post_account_values(body_text, content_type, usernames) or (),
        field_names=names | usernames,
    )
