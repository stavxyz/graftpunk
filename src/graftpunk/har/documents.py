"""HTML extraction for the digest: login forms and token candidates.

On the standard library ``html.parser``, limited to forms, inputs, and meta
tags. Inputs are HTML text (a page source or an HTML response body), never a
HAR entry, so this module knows nothing about HAR (plugin tooling spec,
2026-09-11).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Literal

from graftpunk.har.paths import bare_url

TokenKind = Literal["header", "meta", "hidden_input", "cookie"]

__all__ = [
    "LoginForm",
    "TokenCandidate",
    "TokenKind",
    "extract_login_forms",
    "extract_token_candidates",
    "is_login_document",
    "looks_like_token_name",
]

_USERNAME_HINTS = ("user", "email", "login", "account")
_TOKEN_NAME_HINTS = ("csrf", "xsrf", "token")
_HIDDEN_TOKEN_NAME_HINTS = ("_token", "csrf", "authenticity_token")


def looks_like_token_name(name: str) -> bool:
    """True when *name* (a header, meta, or cookie name) contains a CSRF-token hint.

    The one predicate over ``_TOKEN_NAME_HINTS``: ``digest.py``'s header and
    cookie scans call this instead of re-typing the substring tuple, so the
    three-way "csrf, xsrf, token" rule has a single owner (plugin tooling
    spec, 2026-09-11, "Token candidates").
    """
    lowered = name.lower()
    return any(hint in lowered for hint in _TOKEN_NAME_HINTS)


@dataclass(frozen=True)
class LoginForm:
    """A ``<form>`` containing a password input, from a page source or an HTML response."""

    action: str
    method: str
    fields: dict[str, str]  # credential role -> CSS selector
    submit: str | None
    hidden: tuple[str, ...]  # hidden input names (token candidates)
    source: str


@dataclass(frozen=True)
class TokenCandidate:
    """A header, meta tag, hidden input, or cookie whose name looks like a CSRF token."""

    kind: TokenKind
    name: str
    seen_on: tuple[str, ...]


@dataclass
class _RawInput:
    tag: str
    input_type: str
    name: str
    element_id: str


@dataclass
class _RawForm:
    action: str = ""
    method: str = "GET"
    inputs: list[_RawInput] = field(default_factory=list)


class _DocumentParser(HTMLParser):
    """Collects every ``<form>`` (with its inputs) and every ``<meta>`` in one pass."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_RawForm] = []
        self.metas: list[tuple[str, str]] = []
        self._current: _RawForm | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: (v or "") for k, v in attrs}
        if tag == "form":
            self._current = _RawForm(
                action=values.get("action", ""),
                method=(values.get("method") or "GET").upper(),
            )
        elif tag in ("input", "button") and self._current is not None:
            self._current.inputs.append(
                _RawInput(
                    tag=tag,
                    input_type=(
                        values.get("type") or ("submit" if tag == "button" else "text")
                    ).lower(),
                    name=values.get("name", ""),
                    element_id=values.get("id", ""),
                )
            )
        elif tag == "meta":
            name, content = values.get("name"), values.get("content")
            if name and content:
                self.metas.append((name, content))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None


def _parse(html: str) -> _DocumentParser:
    parser = _DocumentParser()
    parser.feed(html)
    if parser._current is not None:  # an unclosed <form>: keep what was seen
        parser.forms.append(parser._current)
    return parser


def _form_scope(raw_action: str, action: str) -> str:
    """The CSS selector for the form whose attribute reads *raw_action*, spelled
    with the bare *action* only: a prefix match when anything was stripped, so
    the selector still finds the form on the live page."""
    if action == raw_action:
        return f'form[action="{action}"]'
    if action:
        return f'form[action^="{action}"]'
    return "form"


def _selector_for(raw: _RawInput, form_scope: str) -> str:
    if raw.element_id:
        return f"#{raw.element_id}"
    if raw.name:
        return f'{form_scope} {raw.tag}[name="{raw.name}"]'
    return f'{form_scope} {raw.tag}[type="{raw.input_type}"]'


def _guess_role(input_type: str, name: str) -> str:
    if input_type == "password":
        return "password"
    if input_type == "email":
        return "username"
    lowered = name.lower()
    if any(hint in lowered for hint in _USERNAME_HINTS):
        return "username"
    return name


def extract_login_forms(html: str, source: str) -> tuple[LoginForm, ...]:
    """Every ``<form>`` in *html* that contains a password input.

    Each yields a :class:`LoginForm` with the form's action stripped of its
    query string, fragment, and ``;params``
    (:func:`graftpunk.har.paths.bare_url`), one CSS
    selector per input (an ``#id`` selector when the input has one, else a
    selector scoped to the form's action), the credential role guessed from type and name, the
    submit control's selector, and hidden input names.
    """
    forms: list[LoginForm] = []
    for raw in _parse(html).forms:
        password_inputs = [i for i in raw.inputs if i.input_type == "password"]
        if not password_inputs:
            continue
        action = bare_url(raw.action)
        scope = _form_scope(raw.action, action)
        fields: dict[str, str] = {}
        hidden: list[str] = []
        submit: str | None = None
        for raw_input in raw.inputs:
            if raw_input.input_type == "hidden":
                if raw_input.name:
                    hidden.append(raw_input.name)
                continue
            if raw_input.input_type == "submit" or (
                raw_input.tag == "button" and raw_input.input_type != "button"
            ):
                submit = _selector_for(raw_input, scope)
                continue
            role = _guess_role(raw_input.input_type, raw_input.name)
            if role:
                fields[role] = _selector_for(raw_input, scope)
        forms.append(
            LoginForm(
                action=action,
                method=raw.method,
                fields=fields,
                submit=submit,
                hidden=tuple(hidden),
                source=source,
            )
        )
    return tuple(forms)


def extract_token_candidates(html: str, source: str) -> tuple[TokenCandidate, ...]:
    """Meta tags and hidden inputs in *html* whose name looks like a CSRF token.

    Header and cookie candidates are HAR-entry concerns and are collected by
    ``digest.py`` directly; this function only ever sees document HTML.
    """
    parsed = _parse(html)
    candidates: list[TokenCandidate] = []
    for name, _content in parsed.metas:
        if looks_like_token_name(name):
            candidates.append(TokenCandidate(kind="meta", name=name, seen_on=(source,)))
    for raw_form in parsed.forms:
        for raw_input in raw_form.inputs:
            if raw_input.input_type != "hidden" or not raw_input.name:
                continue
            lowered = raw_input.name.lower()
            if any(hint in lowered for hint in _HIDDEN_TOKEN_NAME_HINTS):
                candidates.append(
                    TokenCandidate(kind="hidden_input", name=raw_input.name, seen_on=(source,))
                )
    return tuple(candidates)


def is_login_document(html: str) -> bool:
    """True when *html* contains a form with a password input.

    Used by :mod:`graftpunk.plugins.site_requests` to tell a real login page
    (a stale session gets one back as a 200) apart from any other
    unexpectedly non-JSON response.
    """
    return bool(extract_login_forms(html, source=""))
