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
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from graftpunk.har.paths import (
    bare_host,
    bare_path,
    bare_url,
    holds_an_id,
    is_placeholder,
    templates_a_segment,
)
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
    "printable_unresolved_roles",
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
    # Hidden inputs left out of ``hidden`` because the name held an account value
    # (graftpunk.har.paths.holds_an_id); the names are written nowhere.
    hidden_names_dropped_as_ids: int = 0
    # Roles keyed neutrally (field_1, ...) because the input's name held an account
    # value or was missing; a generated LoginStep says to rename each it declares.
    neutral_roles: tuple[str, ...] = ()
    # Roles (and "submit") the form needs but has no selector for: no username input,
    # or no selector that picks the one input of the recorded form. Each once.
    unresolved_roles: tuple[str, ...] = ()
    # The selectors safe to print without the form scope, by role: an id selector,
    # or a name selector no other input on the recorded page shares. A role missing
    # here is printed unscoped only when its selector is by id (printable_selectors).
    unscoped_fields: dict[str, str] = field(default_factory=dict)
    unscoped_submit: str | None = None
    # The neutral roles whose input had no name at all (the rest had a name that
    # held an account value), so a GP-FILL states the right cause.
    nameless_roles: tuple[str, ...] = ()
    # The roles the form has no input for at all: a password-only page of a
    # multi-step login has no username. Each is in unresolved_roles too.
    absent_roles: tuple[str, ...] = ()
    # Where the form posts, host and path, resolved against its page and NOT
    # email-masked, so the digest can match a POST to it exactly. A lookup for code:
    # marked "internal" (graftpunk.har.digest.INTERNAL) so render_json leaves it out.
    action_target: tuple[str, str] = field(default=("", ""), metadata={"internal": True})
    # The name of every control of the form, so the digest can tell which of two
    # forms posting to one place a POST's body came from. Internal like the above.
    input_names: tuple[str, ...] = field(default=(), metadata={"internal": True})


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
    # Whether the element carried a non-empty type attribute; input_type is the
    # default ("text", or "submit" for a button) when it did not.
    typed: bool = True
    autocomplete: str = ""
    # The id of the form a form="..." attribute names, and the position on the page,
    # so a control outside its form joins it in document order.
    form_owner: str = ""
    order: int = 0
    # A control whose form= names a form it does not sit inside: a selector scoped
    # to that form would match nothing, so it is selected by its form attribute.
    outside_form: bool = False
    # The form this control sits inside in the document, whatever form= says: what
    # a descendant selector scoped to that form matches.
    container: _RawForm | None = field(default=None, repr=False, compare=False)


@dataclass
class _RawForm:
    action: str = ""
    method: str = "GET"
    inputs: list[_RawInput] = field(default_factory=list)
    element_id: str = ""


class _DocumentParser(HTMLParser):
    """Collects every ``<form>`` (with its inputs) and every ``<meta>`` in one pass."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_RawForm] = []
        self.metas: list[tuple[str, str]] = []
        # Every input and button on the page, in a form or not, in document order.
        self.inputs: list[_RawInput] = []
        self._current: _RawForm | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: (v or "") for k, v in attrs}
        if tag == "form":
            self._current = _RawForm(
                action=values.get("action", ""),
                method=(values.get("method") or "GET").upper(),
                element_id=values.get("id", ""),
            )
        elif tag in ("input", "button"):
            raw_input = _RawInput(
                tag=tag,
                input_type=(
                    values.get("type") or ("submit" if tag == "button" else "text")
                ).lower(),
                name=values.get("name", ""),
                element_id=values.get("id", ""),
                typed=bool(values.get("type")),
                autocomplete=values.get("autocomplete", "").strip().lower(),
                form_owner=values.get("form", ""),
                order=len(self.inputs),
            )
            raw_input.container = self._current
            inside = self._current.element_id if self._current is not None else ""
            raw_input.outside_form = bool(raw_input.form_owner) and raw_input.form_owner != inside
            self.inputs.append(raw_input)
            if self._current is not None and not raw_input.form_owner:
                self._current.inputs.append(raw_input)
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
    # A control with form="id" belongs to that form wherever it sits on the page.
    owners = {form.element_id: form for form in parser.forms if form.element_id}
    for raw_input in parser.inputs:
        owner = owners.get(raw_input.form_owner) if raw_input.form_owner else None
        if owner is not None:
            owner.inputs.append(raw_input)
            owner.inputs.sort(key=lambda control: control.order)
    return parser


# The forms an action that strips to nothing can be written as: absent, empty,
# or only a query, ;params, or fragment.
_EMPTY_ACTION_SCOPES = (
    "form:not([action])",
    'form[action=""]',
    'form[action^="?"]',
    'form[action^=";"]',
    'form[action^="#"]',
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


def _type_selectors(raw: _RawInput) -> tuple[str, ...]:
    """The selectors that pick *raw* by its type alone: a typeless input matches
    ``input:not([type])`` (and ``input[type="text"]``, its type by default), a
    typeless button ``button:not([type])`` (and ``button[type="submit"]``)."""
    if raw.typed:
        return (f'{raw.tag}[type="{_css_string(raw.input_type)}"]',)
    return (f"{raw.tag}:not([type])", f'{raw.tag}[type="{_css_string(raw.input_type)}"]')


def _same_type(raw: _RawInput, other: _RawInput) -> bool:
    """Whether a type selector for *raw* would also pick *other*: same tag and same
    type, a typeless element counted as its default type."""
    return other.tag == raw.tag and other.input_type == raw.input_type


def _id_selector(raw: _RawInput) -> str | None:
    """*raw*'s selector by id, or None when it has none or its id holds an account
    value (graftpunk.har.paths.holds_an_id)."""
    if not raw.element_id or holds_an_id(raw.element_id):
        return None
    if _CSS_IDENTIFIER_RE.fullmatch(raw.element_id):
        return f"#{raw.element_id}"
    return f'[id="{_css_string(raw.element_id)}"]'


def _name_selector(raw: _RawInput) -> str | None:
    """*raw*'s tag and ``name`` selector, unscoped, or None when it has no name or
    its name holds an account value."""
    if not raw.name or holds_an_id(raw.name):
        return None
    return f'{raw.tag}[name="{_css_string(raw.name)}"]'


def _scoped(suffixes: tuple[str, ...], form_scopes: tuple[str, ...]) -> str:
    return ", ".join(f"{scope} {suffix}" for suffix in suffixes for scope in form_scopes)


def _selectors_for(
    raw: _RawInput,
    form_scopes: tuple[str, ...],
    contained: list[_RawInput],
    page_inputs: list[_RawInput],
) -> tuple[str | None, str | None]:
    """One input's selector as recorded, and the one safe to print without the
    form scope; each None when there is none.

    By its id when it has one, else its tag and ``name``, else its type; an id or a
    name that holds an account value is never used, and every attribute value is
    escaped (:func:`_css_string`). The recorded selector is scoped to each of
    *form_scopes*; a selector by name or by type is used only when it picks this one
    of the controls the form physically contains (*contained*, what a descendant
    selector matches, a control another form owns by its form attribute included).
    The unscoped one is the id selector, or the
    name selector when no other input on the recorded page (*page_inputs*) has that
    tag and name; a selector by type is never printed unscoped, since it would pick
    the first input of that type on the page. With no form scope (an action that is
    masked or cannot be parsed) the recorded selector is the unscoped one."""
    by_id = _id_selector(raw)
    if by_id is not None:
        return by_id, by_id
    if raw.outside_form:
        return _outside_selector(raw, page_inputs)
    by_name = _name_selector(raw)
    if by_name is not None and (
        sum(1 for other in contained if other.tag == raw.tag and other.name == raw.name) > 1
    ):
        # A name two inputs of the form share picks the first of them, which may
        # not be this one: fall back as for any selector that is not unique.
        by_name = None
    unscoped = None
    if by_name is not None and (
        sum(1 for other in page_inputs if other.tag == raw.tag and other.name == raw.name) == 1
    ):
        unscoped = by_name
    if not form_scopes:
        return (unscoped, unscoped) if by_name is not None else (None, None)
    if by_name is not None:
        return _scoped((by_name,), form_scopes), unscoped
    if sum(1 for other in contained if _same_type(raw, other)) > 1:
        return None, None
    return _scoped(_type_selectors(raw), form_scopes), None


def _outside_selector(
    raw: _RawInput, page_inputs: list[_RawInput]
) -> tuple[str | None, str | None]:
    """The selector of a control outside the form its ``form`` attribute names:
    ``tag[form="id"][name="..."]``, else ``tag[form="id"][type="..."]`` (with
    ``:not([type])`` for a typeless one), each only when it picks this one control
    on the page, else none. It spells no action, so it prints as it is."""
    owner = f'[form="{_css_string(raw.form_owner)}"]'
    siblings = [other for other in page_inputs if other.form_owner == raw.form_owner]
    by_name = _name_selector(raw)
    if by_name is not None and (
        sum(1 for other in siblings if other.tag == raw.tag and other.name == raw.name) == 1
    ):
        selector = f'{raw.tag}{owner}[name="{_css_string(raw.name)}"]'
        return selector, selector
    if sum(1 for other in siblings if _same_type(raw, other)) == 1:
        suffixes = tuple(
            suffix.replace(raw.tag, f"{raw.tag}{owner}", 1) for suffix in _type_selectors(raw)
        )
        selector = ", ".join(suffixes)
        return selector, selector
    return None, None


# A selector with no form scope: by id, as _selectors_for writes one.
_ID_SELECTOR_RE = re.compile(r'#-?[A-Za-z_][\w-]*|\[id="(?:[^"\\]|\\.)*"\]')


def _placeholder_segments(path: str) -> int:
    """How many of *path*'s segments are a ``{name}`` placeholder."""
    return sum(1 for segment in path.split("/") if is_placeholder(segment))


def _printed_unscoped(form: LoginForm, role: str, selector: str) -> str | None:
    if role in form.unscoped_fields:
        return form.unscoped_fields[role]
    return selector if _ID_SELECTOR_RE.fullmatch(selector) else None


def printable_selectors(form: LoginForm) -> tuple[dict[str, str | None], str | None]:
    """*form*'s field selectors (by role) and submit selector as a projection or a
    generated file may print them.

    The one rule for both: when the action's path holds an id or a token
    (:func:`graftpunk.har.paths.templates_a_segment`), a scoped selector would spell
    it, so each is printed without its scope, and only when that still picks the
    intended input: an id selector, or a name selector no other input on the
    recorded page shares (``LoginForm.unscoped_fields``). A selector by type, or a
    shared name, is ``None``: the caller leaves it out, or writes a ``GP-FILL`` in
    its place (:func:`printable_unresolved_roles` names them). Otherwise every
    selector is printed as the digest recorded it. A form with no submit control
    has a ``None`` submit either way.
    """
    fields = dict(sorted(form.fields.items()))
    if not templates_a_segment(form.action):
        return dict[str, str | None](fields), form.submit
    printed = {role: _printed_unscoped(form, role, selector) for role, selector in fields.items()}
    if form.submit is None:
        return printed, None
    submit = form.unscoped_submit
    if submit is None and _ID_SELECTOR_RE.fullmatch(form.submit):
        submit = form.submit
    return printed, submit


def printable_unresolved_roles(form: LoginForm) -> tuple[str, ...]:
    """The roles (and ``submit``) a printed login step has no selector for: the ones
    the digest left unresolved (``LoginForm.unresolved_roles``), then the ones
    :func:`printable_selectors` cannot print without the form scope, each once."""
    fields, submit = printable_selectors(form)
    roles = list(form.unresolved_roles)
    roles += [role for role in form.fields if fields.get(role) is None and role not in roles]
    if form.submit is not None and submit is None and "submit" not in roles:
        roles.append("submit")
    return tuple(roles)


_TEXT_LIKE_TYPES = frozenset({"text", "email", "tel", "number", "url"})
_USERNAME_AUTOCOMPLETE = frozenset({"username", "email"})


def _is_password(raw: _RawInput) -> bool:
    return raw.tag == "input" and (
        raw.input_type == "password" or raw.autocomplete == "current-password"
    )


def _is_text_like(raw: _RawInput) -> bool:
    return raw.tag == "input" and raw.input_type in _TEXT_LIKE_TYPES


def _is_submit(raw: _RawInput) -> bool:
    """An ``<input type="submit">``, an ``<input type="image">``, or a submitting
    ``<button>`` (typeless included)."""
    return raw.input_type == "submit" or (raw.tag == "input" and raw.input_type == "image")


def _has_username_hint(raw: _RawInput) -> bool:
    if raw.autocomplete in _USERNAME_AUTOCOMPLETE or raw.input_type == "email":
        return True
    lowered = raw.name.lower()
    return any(hint in lowered for hint in _USERNAME_HINTS)


_LITERAL_USERNAME_NAMES = frozenset({"username", "email", "login", "user"})
_CONFIRMATION_HINTS = ("confirm", "repeat", "verify", "again", "retype")


def _password_inputs(inputs: list[_RawInput]) -> list[_RawInput]:
    return [i for i in inputs if i.tag == "input" and i.input_type == "password"]


def _is_registration(inputs: list[_RawInput]) -> bool:
    """A sign-up or change-password form: no input marked ``current-password``, and
    either a password marked ``new-password`` or a second password input whose name
    or id asks for a confirmation (``password_confirm``). A form with a
    ``current-password`` input is a login form whatever else it holds (a page-wide
    form with both login and register fields), and a second password input with no
    confirmation hint (a PIN) does not make a form a registration form.
    :func:`extract_login_forms` leaves a registration form out, except a lone form
    with one password (``new-password`` misused on a login form);
    ``is_login_document`` ignores the test."""
    if any(i.autocomplete == "current-password" for i in inputs):
        return False
    passwords = _password_inputs(inputs)
    if any(i.autocomplete == "new-password" for i in passwords):
        return True
    confirmations = [
        i
        for i in passwords[1:]
        if any(hint in f"{i.name} {i.element_id}".lower() for hint in _CONFIRMATION_HINTS)
    ]
    return bool(confirmations)


def _password_index(inputs: list[_RawInput]) -> int | None:
    """The input marked ``current-password``, else the first password input."""
    for index, raw in enumerate(inputs):
        if raw.tag == "input" and raw.autocomplete == "current-password":
            return index
    return next((i for i, raw in enumerate(inputs) if _is_password(raw)), None)


def _username_index(inputs: list[_RawInput], password_index: int) -> int | None:
    """The username input: one ``autocomplete="username"`` names anywhere in the form;
    else the input nearest before the password that ``autocomplete="email"`` names;
    else the hinted text-like input nearest before the password; else the text-like
    input nearest before it; else, when nothing text-like precedes the password,
    the first input after it literally named ``username``, ``email``, ``login``, or
    ``user``. None when the form has none."""
    for index, raw in enumerate(inputs):
        if _is_text_like(raw) and raw.autocomplete == "username":
            return index
    before = [index for index in range(password_index) if _is_text_like(inputs[index])]
    by_email_hint = [index for index in before if inputs[index].autocomplete == "email"]
    if by_email_hint:
        return by_email_hint[-1]
    hinted = [index for index in before if _has_username_hint(inputs[index])]
    if hinted:
        return hinted[-1]
    if before:
        return before[-1]
    return next(
        (
            index
            for index in range(password_index + 1, len(inputs))
            if _is_text_like(inputs[index])
            and inputs[index].name.lower() in _LITERAL_USERNAME_NAMES
        ),
        None,
    )


def _action_target(raw_action: str, source: str) -> tuple[str, str]:
    """Where a form posts, as host and path (``;params``, query, and fragment
    dropped, never email-masked), resolved against its page when *source* is a URL;
    the host is empty when neither names one."""
    try:
        parts = urlsplit(raw_action)
        action = urlunsplit((parts.scheme, bare_host(parts.netloc), bare_path(parts.path), "", ""))
        if source.startswith(("http://", "https://")):
            resolved = urlsplit(urljoin(source, action))
            return bare_host(resolved.netloc), unquote(bare_path(resolved.path)) or "/"
        path = unquote(bare_path(parts.path))
        # A document-relative action with no page URL to resolve it against stays
        # relative, without its "./", and matches a POST path ending in it.
        return bare_host(parts.netloc), path.removeprefix("./")
    except ValueError:
        return "", ""


def extract_login_forms(
    html: str, source: str, *, base: str | None = None
) -> tuple[LoginForm, ...]:
    """Every login ``<form>`` in *html*: one with a password input that is not a
    registration form (:func:`_is_registration`). When the page has no such form, a
    form with exactly one password input is kept (``new-password`` misused on a
    login form); a registration form with a confirmation password never is.

    Each yields a :class:`LoginForm` with the form's action stripped of its query
    string, fragment, and ``;params`` (:func:`graftpunk.har.paths.bare_url`), and
    roles by HTML semantics, anchored on the password input (the one marked
    ``current-password``, else the first): ``password``; ``username``, the input
    :func:`_username_index` picks (absent and unresolved when there is none); the
    submit, the first submit control after the password (an image input counts);
    and each other text-like input between the username and that submit, keyed by
    its name (a neutral ``field_N`` when the name holds an account value or is
    missing). A control whose ``form`` attribute names the form belongs to it
    wherever it sits. A checkbox, radio, file, image, reset, range, or hidden input
    is never a field role, and each role is assigned once. Selectors come from
    :func:`_selectors_for`; a role with none is recorded in ``unresolved_roles``.
    Hidden input names are kept as token candidates. *base*, when given, is the
    unmasked URL of the page the form came from (*source* is the masked one the
    digest prints), and the form's ``action_target`` resolves against it.
    """
    parsed = _parse(html)
    candidates = [raw for raw in parsed.forms if _password_index(raw.inputs) is not None]
    # A registration form is left out only beside a login form: a lone form marked
    # new-password is still the page's login form.
    logins = [raw for raw in candidates if not _is_registration(raw.inputs)]
    if not logins:
        # A lone form with one password is kept (new-password misused on a login
        # form); a lone form with a confirmation password is a registration form.
        logins = [raw for raw in candidates if len(_password_inputs(raw.inputs)) == 1]
    forms: list[LoginForm] = []
    for raw in logins:
        inputs = raw.inputs
        password_index = _password_index(inputs)
        if password_index is None:
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

        hidden: list[str] = []
        dropped_hidden = 0
        for raw_input in inputs:
            if raw_input.input_type != "hidden" or not raw_input.name:
                continue
            if holds_an_id(raw_input.name):
                dropped_hidden += 1
            else:
                hidden.append(raw_input.name)

        username_index = _username_index(inputs, password_index)
        submit_index = next(
            (i for i in range(password_index + 1, len(inputs)) if _is_submit(inputs[i])), None
        )
        block_start = (username_index if username_index is not None else password_index) + 1
        block_end = submit_index if submit_index is not None else len(inputs)
        roles: list[tuple[str, _RawInput]] = []
        if username_index is not None:
            roles.append(("username", inputs[username_index]))
        roles.append(("password", inputs[password_index]))
        taken = {i.name for i in inputs if i.name} | {"username", "password"}
        neutral: list[str] = []
        nameless: list[str] = []
        for index in range(block_start, block_end):
            raw_input = inputs[index]
            if index in (username_index, password_index) or not _is_text_like(raw_input):
                continue
            if (
                raw_input.name
                and not holds_an_id(raw_input.name)
                and raw_input.name
                not in (
                    "username",
                    "password",
                )
            ):
                roles.append((raw_input.name, raw_input))
                continue
            # A name that holds an account value, or none, gets a neutral key in
            # document order, never one a real input of the form is named.
            number = len(neutral) + 1
            while f"field_{number}" in taken:
                number += 1
            key = f"field_{number}"
            taken.add(key)
            neutral.append(key)
            if not raw_input.name:
                nameless.append(key)
            roles.append((key, raw_input))

        fields: dict[str, str] = {}
        unscoped_fields: dict[str, str] = {}
        absent: list[str] = [] if username_index is not None else ["username"]
        unresolved: list[str] = list(absent)
        contained = [control for control in parsed.inputs if control.container is raw]
        for role, raw_input in roles:
            if role in fields or role in unresolved:
                continue
            selector, unscoped = _selectors_for(raw_input, scopes, contained, parsed.inputs)
            if selector is None:
                unresolved.append(role)
                continue
            fields[role] = selector
            if unscoped is not None:
                unscoped_fields[role] = unscoped
        submit: str | None = None
        unscoped_submit: str | None = None
        if submit_index is not None:
            submit, unscoped_submit = _selectors_for(
                inputs[submit_index], scopes, contained, parsed.inputs
            )
            if submit is None:
                unresolved.append("submit")
        forms.append(
            LoginForm(
                action=action,
                method=raw.method,
                fields=fields,
                submit=submit,
                hidden=tuple(hidden),
                source=source,
                hidden_names_dropped_as_ids=dropped_hidden,
                neutral_roles=tuple(neutral),
                unresolved_roles=tuple(unresolved),
                unscoped_fields=unscoped_fields,
                unscoped_submit=unscoped_submit,
                nameless_roles=tuple(nameless),
                absent_roles=tuple(absent),
                action_target=_action_target(raw.action, base or source),
                input_names=tuple(i.name for i in inputs if i.name),
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
    unexpectedly non-JSON response. Any password input counts, a registration
    form's included: the registration exclusion is the digest's, not this test's.
    """
    return any(_password_index(raw.inputs) is not None for raw in _parse(html).forms)
