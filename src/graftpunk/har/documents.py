"""HTML extraction for the digest: login forms and token candidates.

On the standard library ``html.parser``, limited to forms, inputs, and meta
tags. Inputs are HTML text (a page source or an HTML response body), never a
HAR entry, so this module knows nothing about HAR (plugin tooling spec,
2026-09-11).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urlsplit

from graftpunk.har.paths import bare_path, bare_url, templates_a_segment
from graftpunk.logging import get_logger

LOG = get_logger(__name__)

TokenKind = Literal["header", "meta", "hidden_input", "cookie"]

__all__ = [
    "LoginForm",
    "TokenCandidate",
    "TokenKind",
    "extract_login_forms",
    "extract_token_candidates",
    "is_login_document",
    "looks_like_token_name",
    "printable_selectors",
    "unscoped_selector",
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


# The forms an action that strips to nothing can be written as: absent, empty,
# or only a query or ;params.
_EMPTY_ACTION_SCOPES = (
    "form:not([action])",
    'form[action=""]',
    'form[action^="?"]',
    'form[action^=";"]',
)


def _css_string(value: str) -> str:
    """*value* escaped for the inside of a double-quoted CSS attribute value: a
    backslash and a quote are backslash-escaped, and a newline or carriage return
    becomes its hex escape, so a captured name or action cannot end the value early
    or change the selector."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return escaped.replace("\n", "\\a ").replace("\r", "\\d ")


# A CSS identifier an #id selector can spell as it is.
_CSS_IDENTIFIER_RE = re.compile(r"-?[A-Za-z_][\w-]*")


def _form_scopes(raw_action: str, action: str) -> tuple[str, ...]:
    """The CSS form selectors for the form whose attribute reads *raw_action*, spelled
    with the bare *action* (:func:`graftpunk.har.paths.bare_url`) and never with a
    query, fragment, or parameter value.

    An unchanged action is one exact match. An action stripped only at its end is
    that action exactly or followed by ``;``, ``?``, or ``#``, so the live form
    matches and a sibling such as ``/login-help`` does not. An action with
    ``;params`` in a middle segment goes to :func:`_middle_param_scopes`. An action
    that strips to nothing matches only a form whose action is absent, empty, or
    only a query or ``;params``.
    """
    if not action:
        return _EMPTY_ACTION_SCOPES
    if action == raw_action:
        return (f'form[action="{_css_string(action)}"]',)
    raw_path = raw_action.split("?", 1)[0].split("#", 1)[0]
    head, separator, rest = raw_path.partition(";")
    if separator and "/" in rest:
        return _middle_param_scopes(head, rest.partition("/")[2], raw_path.endswith("/"))
    return tuple(
        f'form[action{op}"{_css_string(action + tail)}"]' for op, tail in _STRIPPED_ENDINGS
    )


# How the raw action can continue after the bare one: nothing, or a ;param,
# query, or fragment.
_STRIPPED_ENDINGS = (("=", ""), ("^=", ";"), ("^=", "?"), ("^=", "#"))


def _middle_param_scopes(head: str, after: str, trailing_slash: bool) -> tuple[str, ...]:
    """Form selectors for an action whose first ``;params`` sit in a middle segment.

    *head* is the raw action before its first ``;``; *after* is the raw text past the
    segment that carries it. No prefix of the bare path is a prefix of the raw
    value, so the selector matches the parts the raw value keeps whatever the
    parameter values are: it starts with ``head;``, contains each later segment
    before the last as ``/segment``, and has the last segment followed by nothing,
    ``;``, ``?``, or ``#``. The limit: CSS cannot order the middle ``*=`` tests or
    anchor their ends, so a decoy that differs only in a middle segment's suffix
    (``/b`` against ``/bx``) is not told apart; the last segment and the head are.
    """
    segments = [segment.partition(";")[0] for segment in after.strip("/").split("/") if segment]
    base = f'form[action^="{_css_string(head + ";")}"]' + "".join(
        f'[action*="{_css_string("/" + s)}"]' for s in segments[:-1]
    )
    last = f"/{segments[-1]}" if segments else ""
    if trailing_slash or not segments:
        last += "/"
    return (
        f'{base}[action$="{_css_string(last)}"]',
        f'{base}[action*="{_css_string(last + ";")}"]',
        f'{base}[action*="{_css_string(last + "?")}"]',
        f'{base}[action*="{_css_string(last + "#")}"]',
    )


def _selector_for(raw: _RawInput, form_scopes: tuple[str, ...]) -> str:
    """One input's selector: by its id when it has one, else its tag and ``name``
    (or ``type``) scoped to each of *form_scopes*, or unscoped when there are none.
    Every attribute value is escaped (:func:`_css_string`)."""
    if raw.element_id:
        if _CSS_IDENTIFIER_RE.fullmatch(raw.element_id):
            return f"#{raw.element_id}"
        return f'[id="{_css_string(raw.element_id)}"]'
    attribute = (
        f'name="{_css_string(raw.name)}"' if raw.name else f'type="{_css_string(raw.input_type)}"'
    )
    suffix = f"{raw.tag}[{attribute}]"
    if not form_scopes:
        return suffix
    return ", ".join(f"{scope} {suffix}" for scope in form_scopes)


# The input part every alternative of a form-scoped selector ends with (see
# _selector_for): a tag and its one name= or type= attribute, whose value may hold
# backslash escapes.
_INPUT_PART_RE = re.compile(r'[A-Za-z][\w-]*\[(?:name|type)="(?:[^"\\]|\\.)*"\]$')
# A selector with no form scope: by id, as _selector_for writes one.
_ID_SELECTOR_RE = re.compile(r'#-?[A-Za-z_][\w-]*|\[id="(?:[^"\\]|\\.)*"\]')


def unscoped_selector(selector: str) -> str | None:
    """*selector*, a :class:`LoginForm` field selector, without its form scope, or
    None when it cannot be reduced to a part that holds no form action.

    An id selector has no scope and comes back as it is. A form-scoped one comes
    back as its input part (``input[name="username"]``), which matches the same
    input in any form: for printing a selector whose scope would carry the form
    action's literal path. Anything else fails closed: returning it would print the
    scope, action included.
    """
    if _ID_SELECTOR_RE.fullmatch(selector):
        return selector
    match = _INPUT_PART_RE.search(selector)
    return match.group(0) if match else None


_PLACEHOLDER_RE = re.compile(r"\{[A-Za-z0-9_]+\}")


def _placeholder_segments(path: str) -> int:
    """How many of *path*'s segments are a ``{name}`` placeholder."""
    return sum(1 for segment in path.split("/") if _PLACEHOLDER_RE.fullmatch(segment))


def printable_selectors(form: LoginForm) -> tuple[dict[str, str | None], str | None]:
    """*form*'s field selectors (by role) and submit selector as a projection or a
    generated file may print them.

    The one rule for both: when the action's path holds an id or a token
    (:func:`graftpunk.har.paths.templates_a_segment`), a scoped selector would spell
    it, so each selector is unscoped (:func:`unscoped_selector`), and one that cannot
    be is ``None``: the caller leaves it out, or writes a ``GP-FILL`` in its place.
    Otherwise every selector is printed as the digest recorded it. A form with no
    submit control has a ``None`` submit either way.
    """
    fields = dict(sorted(form.fields.items()))
    if not templates_a_segment(form.action):
        return dict[str, str | None](fields), form.submit
    unscoped = {role: unscoped_selector(selector) for role, selector in fields.items()}
    return unscoped, unscoped_selector(form.submit) if form.submit else None


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
    (:func:`graftpunk.har.paths.bare_url`), one CSS selector per input (an id
    selector when the input has one, else a selector scoped to the form's action,
    or unscoped when the action cannot be parsed, with its attribute values
    escaped), the credential role guessed from type and name, the submit control's
    selector, and hidden input names.
    """
    forms: list[LoginForm] = []
    for raw in _parse(html).forms:
        password_inputs = [i for i in raw.inputs if i.input_type == "password"]
        if not password_inputs:
            continue
        try:
            action = bare_url(raw.action)
            # bare_url masked an email segment as a placeholder: no live form's
            # action reads that way, so its selectors go unscoped. Judged path to
            # path, so a "{" the stripped query or fragment held does not count.
            masked = _placeholder_segments(urlsplit(action).path) > _placeholder_segments(
                bare_path(urlsplit(raw.action).path)
            )
            scopes = () if masked else _form_scopes(raw.action, action)
        except ValueError:
            # urlsplit refuses an action it cannot split (an unclosed IPv6
            # bracket); the form still counts. Its selectors are unscoped: a
            # scope for an empty action would never match the live form, and one
            # spelled from the raw text would print it. The page is named in the
            # warning, never the action text.
            LOG.warning("login_form_action_unparseable", source=source)
            action = ""
            scopes = ()
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
                submit = _selector_for(raw_input, scopes)
                continue
            role = _guess_role(raw_input.input_type, raw_input.name)
            if role:
                fields[role] = _selector_for(raw_input, scopes)
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
