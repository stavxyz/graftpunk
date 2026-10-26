"""HTML extraction for the digest: login forms and token candidates.

On the standard library ``html.parser``, limited to forms, inputs, and meta
tags. Inputs are HTML text (a page source or an HTML response body), never a
HAR entry, so this module knows nothing about HAR (plugin tooling spec,
2026-09-11).
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from graftpunk.har.paths import (
    bare_host,
    bare_path,
    bare_url,
    holds_an_id,
    is_placeholder,
    normal_host,
    templates_a_segment,
)
from graftpunk.logging import get_logger

LOG = get_logger(__name__)

TokenKind = Literal["header", "meta", "hidden_input", "cookie"]

__all__ = [
    "PASSWORD_NAME_HINTS",
    "LoginForm",
    "TokenCandidate",
    "TokenKind",
    "extract_login_forms",
    "extract_token_candidates",
    "form_action_targets",
    "is_login_document",
    "looks_like_new_password_name",
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
    """A ``<form>`` containing a password input, or a dialog's login controls that sit
    in no form (``formless``), from a page source or an HTML response."""

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
    # The form's structure: its element id and each control's (tag, type, name,
    # element id) in order. The same form on several pages has one signature even
    # when its selectors differ by page, so the digest lists it once. Internal.
    signature: tuple[object, ...] = field(default=(), metadata={"internal": True})
    # The URL (graftpunk.har.paths.bare_url) of the in-scope GET on another host
    # whose redirect chain led to this form's page: an identity provider's form page
    # opened directly lacks the state the app's redirect gave it, so the login is
    # opened there. The last such GET of the chain that is not a logout (opening one
    # would log out); empty when none. Set by the digest. Internal.
    opened_from: str = field(default="", metadata={"internal": True})
    # Built from the controls of a dialog that sit in no <form> (a script-driven
    # login modal), not from a <form>: no browser submits it, so its action and
    # method are empty, its action_target is ("", ""), and the digest credits a
    # credential post to it only by its input names. Internal.
    formless: bool = field(default=False, metadata={"internal": True})
    # Read from a DOM snapshot the recorder took at a credential post (the live page
    # as the user submitted it), not from a recorded HTML response. Internal.
    from_snapshot: bool = field(default=False, metadata={"internal": True})
    # A formless group bounded by an ancestor element of the password rather than
    # by a dialog: read from a DOM snapshot only. Internal.
    plain_block: bool = field(default=False, metadata={"internal": True})
    # How a plain_block group was bounded (graftpunk.har.documents._block_login_groups):
    # "inputs", "button" (a password-only group), or "alone". Internal.
    block_bound: str = field(default="", metadata={"internal": True})
    # The names of the form's controls that are not hidden inputs, whatever the name
    # holds (hidden lists only names that hold no account value). Internal.
    visible_names: tuple[str, ...] = field(default=(), metadata={"internal": True})


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
    # Inside a <noscript> element: shown only when script is off, as an OAuth
    # form_post page's fallback submit button is.
    in_noscript: bool = False
    # The innermost dialog this control sits in, as its index in
    # _DocumentParser.dialog_hints, or None: what bounds a formless login group.
    dialog: int | None = None
    # The innermost element this control sits in, as its index in
    # _DocumentParser.blocks, or None; tracked for a DOM snapshot only.
    block: int | None = None


@dataclass
class _Block:
    """An element of a DOM snapshot, for bounding a login group outside any dialog:
    its tag, its lowered id and class, the element it sits in, and the controls
    inside it, as the range [start, end) of _DocumentParser.inputs."""

    tag: str
    hints: str
    parent: int | None
    start: int
    end: int | None = None


@dataclass
class _RawForm:
    action: str = ""
    method: str = "GET"
    inputs: list[_RawInput] = field(default_factory=list)
    element_id: str = ""


# The elements that never hold content, so never open: neither their start nor their
# end tag counts toward a dialog's end.
_VOID_TAGS = frozenset(
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


class _DocumentParser(HTMLParser):
    """Collects every ``<form>`` (with its inputs) and every ``<meta>`` in one pass,
    and the dialog each control sits in."""

    def __init__(self, *, track_blocks: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        # For a DOM snapshot, which the browser serialized with every element
        # closed: each element as a _Block, and the open ones, innermost last, with
        # how many of each tag are open, so an end tag pops to its own element and a
        # stray one pops nothing.
        self._track_blocks = track_blocks
        self.blocks: list[_Block] = []
        self._block_stack: list[int] = []
        self._block_open: dict[str, int] = {}
        self.forms: list[_RawForm] = []
        self.metas: list[tuple[str, str]] = []
        # Every input, button, select, and textarea on the page, in a form or not, in
        # document order.
        self.inputs: list[_RawInput] = []
        self._current: _RawForm | None = None
        self._noscript = 0
        # Each dialog (a <dialog>, or an element whose role's first token is dialog
        # or alertdialog, or with aria-modal="true"), in document order, as its
        # lowered id and class attributes; a control's dialog indexes into it.
        self.dialog_hints: list[str] = []
        # How many elements of each tag are open, counting only that tag's own start
        # and end tags. A void element never opens, and the count never falls below
        # zero, so neither a void end tag nor a stray one counts, and an implied end
        # tag of another element cannot move a dialog's end.
        self._open_count: dict[str, int] = {}
        # Per tag, the open dialogs of that tag, innermost last, as (the tag's open
        # count when the dialog opened, index into dialog_hints). A dialog closes when
        # its tag's count falls below that level; one never closed runs to the end.
        self._dialog_levels: dict[str, list[tuple[int, int]]] = {}
        # Every dialog opened and not yet seen closed, innermost last, and the ones
        # closed: a closed one is dropped when it reaches the end of the list.
        self._dialogs: list[int] = []
        self._closed: set[int] = set()
        # Inside <svg> or <math>, where "/>" closes an element; elsewhere a browser
        # ignores it on an element that is not void.
        self._foreign = 0

    def _innermost_dialog(self) -> int | None:
        while self._dialogs and self._dialogs[-1] in self._closed:
            self._dialogs.pop()
        return self._dialogs[-1] if self._dialogs else None

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        # An <svg/> or <math/> itself counts: its start tag has just entered foreign
        # content, so its end tag here leaves it.
        if self._foreign:
            self.handle_endtag(tag)
        elif tag in ("form", "noscript"):
            # As before: a <form/> or <noscript/> holds nothing. Its open count is
            # left alone, as a browser leaves the element open.
            self._end_form_or_noscript(tag)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: (v or "") for k, v in attrs}
        if tag in ("svg", "math"):
            self._foreign += 1
        if tag not in _VOID_TAGS:
            level = self._open_count.get(tag, 0) + 1
            self._open_count[tag] = level
            role = values.get("role", "").lower().split()[:1]
            if (
                tag == "dialog"
                or role in (["dialog"], ["alertdialog"])
                or values.get("aria-modal", "").strip().lower() == "true"
            ):
                index = len(self.dialog_hints)
                self._dialog_levels.setdefault(tag, []).append((level, index))
                self._dialogs.append(index)
                hints = f"{values.get('id', '')} {values.get('class', '')}".lower()
                self.dialog_hints.append(hints)
        if tag == "noscript":
            self._noscript += 1
        if tag == "form":
            self._current = _RawForm(
                action=values.get("action", ""),
                method=(values.get("method") or "GET").upper(),
                element_id=values.get("id", ""),
            )
        elif tag in ("input", "button", "select", "textarea"):
            # A select or a textarea takes no type attribute: its tag is its type, and
            # it is always a visible control.
            if tag in ("select", "textarea"):
                input_type = tag
            else:
                input_type = values.get("type") or ("submit" if tag == "button" else "text")
            raw_input = _RawInput(
                tag=tag,
                input_type=input_type.lower(),
                name=values.get("name", ""),
                element_id=values.get("id", ""),
                typed=bool(values.get("type")),
                autocomplete=values.get("autocomplete", "").strip().lower(),
                form_owner=values.get("form", ""),
                order=len(self.inputs),
            )
            raw_input.container = self._current
            raw_input.in_noscript = self._noscript > 0
            raw_input.dialog = self._innermost_dialog()
            raw_input.block = self._block_stack[-1] if self._block_stack else None
            inside = self._current.element_id if self._current is not None else ""
            raw_input.outside_form = bool(raw_input.form_owner) and raw_input.form_owner != inside
            self.inputs.append(raw_input)
            if self._current is not None and not raw_input.form_owner:
                self._current.inputs.append(raw_input)
        elif tag == "meta":
            name, content = values.get("name"), values.get("content")
            if name and content:
                self.metas.append((name, content))
        if self._track_blocks and tag not in _VOID_TAGS:
            parent = self._block_stack[-1] if self._block_stack else None
            hints = f"{values.get('id', '')} {values.get('class', '')}".lower()
            self._block_stack.append(len(self.blocks))
            self.blocks.append(_Block(tag, hints, parent, start=len(self.inputs)))
            self._block_open[tag] = self._block_open.get(tag, 0) + 1

    def _close_blocks(self, tag: str) -> None:
        """Close the open elements down to the innermost *tag*, when one is open."""
        if not self._block_open.get(tag):
            return
        while self._block_stack:
            block = self.blocks[self._block_stack.pop()]
            block.end = len(self.inputs)
            self._block_open[block.tag] -= 1
            if block.tag == tag:
                return

    def handle_endtag(self, tag: str) -> None:
        if self._track_blocks:
            self._close_blocks(tag)
        if self._open_count.get(tag, 0) > 0:
            count = self._open_count[tag] - 1
            self._open_count[tag] = count
            levels = self._dialog_levels.get(tag)
            while levels and count < levels[-1][0]:
                self._closed.add(levels.pop()[1])
        if tag in ("svg", "math") and self._foreign:
            self._foreign -= 1
        self._end_form_or_noscript(tag)

    def _end_form_or_noscript(self, tag: str) -> None:
        if tag == "noscript" and self._noscript:
            self._noscript -= 1
        if tag == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None


def _parse(html: str, *, track_blocks: bool = False) -> _DocumentParser:
    parser = _DocumentParser(track_blocks=track_blocks)
    parser.feed(html)
    if parser._current is not None:  # an unclosed <form>: keep what was seen
        parser.forms.append(parser._current)
    for index in parser._block_stack:  # elements still open run to the end
        parser.blocks[index].end = len(parser.inputs)
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


@dataclass
class _Counts:
    """How many of a set of controls share a tag and name, or a tag and type (what a
    type selector picks: a typeless element counted as its default type), and the
    same for each ``form`` attribute's
    controls: counted once, so checking that a selector picks one control is a
    lookup, not a pass over the page."""

    by_name: Counter[tuple[str, str]] = field(default_factory=Counter)
    by_type: Counter[tuple[str, str]] = field(default_factory=Counter)
    by_owner_name: Counter[tuple[str, str, str]] = field(default_factory=Counter)
    by_owner_type: Counter[tuple[str, str, str]] = field(default_factory=Counter)

    @classmethod
    def of(cls, controls: Iterable[_RawInput]) -> _Counts:
        counts = cls()
        for raw in controls:
            counts.by_name[(raw.tag, raw.name)] += 1
            counts.by_type[(raw.tag, raw.input_type)] += 1
            counts.by_owner_name[(raw.form_owner, raw.tag, raw.name)] += 1
            counts.by_owner_type[(raw.form_owner, raw.tag, raw.input_type)] += 1
        return counts

    def named(self, raw: _RawInput) -> int:
        return self.by_name[(raw.tag, raw.name)]

    def typed(self, raw: _RawInput) -> int:
        return self.by_type[(raw.tag, raw.input_type)]


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
    contained: _Counts,
    page_inputs: _Counts,
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
    if by_name is not None and contained.named(raw) > 1:
        # A name two inputs of the form share picks the first of them, which may
        # not be this one: fall back as for any selector that is not unique.
        by_name = None
    unscoped = None
    if by_name is not None and page_inputs.named(raw) == 1:
        unscoped = by_name
    if not form_scopes:
        return (unscoped, unscoped) if by_name is not None else (None, None)
    if by_name is not None:
        return _scoped((by_name,), form_scopes), unscoped
    if contained.typed(raw) > 1:
        return None, None
    return _scoped(_type_selectors(raw), form_scopes), None


def _outside_selector(raw: _RawInput, page_inputs: _Counts) -> tuple[str | None, str | None]:
    """The selector of a control outside the form its ``form`` attribute names:
    ``tag[form="id"][name="..."]``, else ``tag[form="id"][type="..."]`` (with
    ``:not([type])`` for a typeless one), each only when it picks this one control
    on the page, else none. It spells no action, so it prints as it is."""
    owner = f'[form="{_css_string(raw.form_owner)}"]'
    by_name = _name_selector(raw)
    if by_name is not None and (
        page_inputs.by_owner_name[(raw.form_owner, raw.tag, raw.name)] == 1
    ):
        selector = f'{raw.tag}{owner}[name="{_css_string(raw.name)}"]'
        return selector, selector
    if page_inputs.by_owner_type[(raw.form_owner, raw.tag, raw.input_type)] == 1:
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


def _is_button_like(raw: _RawInput) -> bool:
    """Any ``<button>``, or a submit, image, button, or reset input: what a user
    presses, and what ends a formless group's fields."""
    return raw.tag == "button" or (
        raw.tag == "input" and raw.input_type in ("submit", "image", "button", "reset")
    )


def _has_username_hint(raw: _RawInput) -> bool:
    if raw.autocomplete in _USERNAME_AUTOCOMPLETE or raw.input_type == "email":
        return True
    lowered = raw.name.lower()
    return any(hint in lowered for hint in _USERNAME_HINTS)


_LITERAL_USERNAME_NAMES = frozenset({"username", "email", "login", "user"})
_CONFIRMATION_HINTS = ("confirm", "repeat", "verify", "again", "retype")
# A password field's name that asks for a new password, not the current one.
_NEW_PASSWORD_HINTS = ("new", *_CONFIRMATION_HINTS)
# The one owner of the password-field hints: the digest reads a credential post's
# body names through it too.
PASSWORD_NAME_HINTS = ("password", "passwd", "pwd")


def looks_like_new_password_name(name: str) -> bool:
    """True when *name* is a password field's name that asks for a new or repeated
    password (``new_password``, ``password_confirm``): the hints a registration
    form's confirmation input is recognised by, plus ``new``."""
    lowered = name.lower()
    return any(hint in lowered for hint in PASSWORD_NAME_HINTS) and any(
        hint in lowered for hint in _NEW_PASSWORD_HINTS
    )


def _password_inputs(inputs: list[_RawInput]) -> list[_RawInput]:
    return [i for i in inputs if i.tag == "input" and i.input_type == "password"]


def _is_registration(inputs: list[_RawInput]) -> bool:
    """A sign-up form without ``current-password``: a password marked
    ``new-password``, or a second password input whose name or id asks for a
    confirmation (``password_confirm``). A form with a ``current-password`` input is
    never a registration form: it is a change-password form when it also asks for a
    new password and holds no username-like input (:func:`_is_change_password`),
    and a login form otherwise (a page-wide form with both login and register
    fields). A second password input with no confirmation hint (a PIN) does not make
    a form a registration form.
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


def _is_change_password(inputs: list[_RawInput]) -> bool:
    """A change-password form: an input marked ``current-password``, a new password
    (a password input marked ``new-password``, or one whose name or id asks for a new
    or repeated password), and no username-like text input. It is not a login form.
    A page-wide form that also holds a username-like input (a combined login and
    register form) stays a login form."""
    if not any(i.autocomplete == "current-password" for i in inputs):
        return False
    new_password = any(
        i.autocomplete == "new-password"
        or looks_like_new_password_name(i.name)
        or looks_like_new_password_name(i.element_id)
        for i in _password_inputs(inputs)
    )
    username_like = any(_is_text_like(i) and _has_username_hint(i) for i in inputs)
    return new_password and not username_like


def _password_index(inputs: list[_RawInput]) -> int | None:
    """The input marked ``current-password``, else the first password input."""
    for index, raw in enumerate(inputs):
        if raw.tag == "input" and raw.autocomplete == "current-password":
            return index
    return next((i for i, raw in enumerate(inputs) if _is_password(raw)), None)


def _username_index(
    inputs: list[_RawInput], password_index: int, end: int | None = None
) -> int | None:
    """The username input: one ``autocomplete="username"`` names anywhere in the form;
    else the input nearest before the password that ``autocomplete="email"`` names;
    else the hinted text-like input nearest before the password; else the text-like
    input nearest before it; else, when nothing text-like precedes the password,
    the first input after it literally named ``username``, ``email``, ``login``, or
    ``user``. None when the form has none. With *end*, no input at or after that
    index is looked at (a dialog's field block: a hidden reset panel's input after
    it is never the username)."""
    end = len(inputs) if end is None else end
    for index, raw in enumerate(inputs[:end]):
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
            for index in range(password_index + 1, end)
            if _is_text_like(inputs[index])
            and inputs[index].name.lower() in _LITERAL_USERNAME_NAMES
        ),
        None,
    )


def _bare_action(raw: _RawForm) -> str | None:
    """*raw*'s action as a scope spells it (:func:`graftpunk.har.paths.bare_url`), or
    None when it cannot be parsed."""
    try:
        return bare_url(raw.action)
    except ValueError:
        return None


def _action_target(raw_action: str, source: str) -> tuple[str, str]:
    """Where a form posts, as host and path (``;params``, query, and fragment
    dropped, never email-masked), resolved against its page when *source* is a URL;
    the host is empty when neither names one, and otherwise spelled by
    :func:`graftpunk.har.paths.normal_host`, as the digest spells a request's."""
    try:
        parts = urlsplit(raw_action)
        action = urlunsplit((parts.scheme, bare_host(parts.netloc), bare_path(parts.path), "", ""))
        if source.startswith(("http://", "https://")):
            resolved = urlsplit(urljoin(source, action))
            host = normal_host(resolved.scheme, resolved.netloc)
            return host, unquote(bare_path(resolved.path)) or "/"
        path = unquote(bare_path(parts.path))
        # A document-relative action with no page URL to resolve it against stays
        # relative, without its "./", and matches a POST path ending in it.
        return normal_host(parts.scheme, parts.netloc), path.removeprefix("./")
    except ValueError:
        return "", ""


def form_action_targets(html: str, base: str) -> dict[tuple[str, str], frozenset[str]]:
    """The OAuth ``form_post``-shaped forms in *html*, each as its target (host and
    path, resolved against *base*, the unmasked URL of the page) and its hidden
    input names.

    A form has that shape when it posts (``method="post"``), holds at least one
    hidden input, and every other control is hidden too, a submit control inside
    ``<noscript>`` (the fallback shown when script is off) excepted; a ``<select>``
    and a ``<textarea>`` are visible controls. A cart form with a visible field, a
    logout form with a visible button, and a GET search form do not. Two such forms
    posting to one target give it the union of their hidden names. The digest
    follows a login's redirect chain through such a page when a POST to one of these
    targets, on another host than the page's, carries only its hidden names; a chain
    resting on a page with such a form it did not follow gives the login no
    landing.
    """
    targets: dict[tuple[str, str], frozenset[str]] = {}
    for raw in _parse(html).forms:
        if raw.method != "POST":
            continue
        hidden = [i for i in raw.inputs if i.tag == "input" and i.input_type == "hidden"]
        visible = [
            i
            for i in raw.inputs
            if not (i.tag == "input" and i.input_type == "hidden")
            and not (i.in_noscript and i.input_type == "submit")
        ]
        target = _action_target(raw.action, base)
        if hidden and not visible and target[1]:
            names = frozenset(i.name for i in hidden if i.name)
            targets[target] = targets.get(target, frozenset()) | names
    return targets


def _formless_selectors(raw: _RawInput, page_inputs: _Counts) -> tuple[str | None, str | None]:
    """The selector of a control outside every form, recorded and unscoped alike:
    its id selector, else its tag and ``name`` selector when no other input on the
    page (*page_inputs*) has that tag and name, else none. No form scope can pick it,
    and a selector by type would pick the first input of that type on the page."""
    by_id = _id_selector(raw)
    if by_id is not None:
        return by_id, by_id
    by_name = _name_selector(raw)
    if by_name is not None and page_inputs.named(raw) == 1:
        return by_name, by_name
    return None, None


def _formless_controls(parsed: _DocumentParser) -> list[_RawInput]:
    """The page's controls with no form owner, in document order: in no ``<form>``
    and with no ``form`` attribute, or with a ``form`` attribute that names no form
    on the page (which leaves a control with no owner even inside a ``<form>``).
    One inside ``<noscript>`` is left out: with script on it is never in the page,
    and only script sends a formless group."""
    owners = {form.element_id for form in parsed.forms if form.element_id}
    return [
        raw
        for raw in parsed.inputs
        if not raw.in_noscript
        and (
            (raw.form_owner and raw.form_owner not in owners)
            or (not raw.form_owner and raw.container is None)
        )
    ]


# Words in a dialog's id or class, or its controls' ids and names, that name a sign-up.
_SIGN_UP_HINTS = ("signup", "sign-up", "sign_up", "register", "registration", "create", "join")


def _formless_login_groups(parsed: _DocumentParser) -> list[list[_RawInput]]:
    """The page's formless login groups: for each dialog (``_DocumentParser``
    tracks a ``<dialog>``, ``role="dialog"``, ``role="alertdialog"``, and
    ``aria-modal="true"``), the formless controls (:func:`_formless_controls`) whose
    innermost dialog it is. A password outside every dialog is in no group: nothing
    else on a page bounds a login's inputs reliably.

    Each dialog's controls are judged as a form's are: none when they hold no
    password, when they are a change-password or a registration shape
    (:func:`_is_change_password`, :func:`_is_registration`), or when they hold more
    than one password input and none marked ``current-password`` (two logins, or a
    login and a sign-up, the rules cannot tell apart). Ordered with a group holding
    a ``current-password`` input first, then one whose dialog's id or class and
    controls' ids and names do not name a sign-up (``_SIGN_UP_HINTS``), then
    document order."""
    by_dialog: dict[int, list[_RawInput]] = {}
    for raw in _formless_controls(parsed):
        if raw.dialog is not None:
            by_dialog.setdefault(raw.dialog, []).append(raw)
    return _ranked_login_groups(
        [(parsed.dialog_hints[dialog], group) for dialog, group in by_dialog.items()]
    )


def _ranked_login_groups(groups: list[tuple[str, list[_RawInput]]]) -> list[list[_RawInput]]:
    """The formless *groups* (each with its bounding element's lowered id and class)
    that are a login, judged and ordered as :func:`_formless_login_groups` states."""

    def sign_up_named(hints: str, group: list[_RawInput]) -> bool:
        words = " ".join(f"{raw.element_id} {raw.name}" for raw in group).lower()
        words += " " + hints
        return any(hint in words for hint in _SIGN_UP_HINTS)

    kept = [
        (hints, group)
        for hints, group in groups
        if _password_index(group) is not None
        and not _is_change_password(group)
        and not _is_registration(group)
        and (
            len(_password_inputs(group)) == 1
            or any(raw.autocomplete == "current-password" for raw in group)
        )
    ]
    kept.sort(
        key=lambda pair: (
            not any(raw.autocomplete == "current-password" for raw in pair[1]),
            sign_up_named(pair[0], pair[1]),
            pair[1][0].order,
        )
    )
    return [group for _hints, group in kept]


# The elements that never bound a snapshot's login group: one bounded by the whole
# page would take any input before the password as its username.
_PAGE_ELEMENTS = frozenset({"html", "body"})
# The most text-like and password inputs with no form owner and in no dialog an
# element may hold to bound a snapshot's login group (buttons, checkboxes, radios,
# and hidden inputs do not count, and neither do a form's or a dialog's own): a login
# block asks for a username and a password, and a page's wrapper, holding a search
# box, a newsletter field, or a sign-up beside the login, asks for more.
_MAX_BLOCK_FIELDS = 4


def _counts_as_field(raw: _RawInput) -> bool:
    """A text-like input, or one the role rules take for a password (a
    ``current-password`` autocomplete included, :func:`_is_password`)."""
    return _is_text_like(raw) or _is_password(raw)


def _username_hinted(raw: _RawInput) -> bool:
    """A text-like input the digest's username rule names: a ``username`` or
    ``email`` autocomplete, an email type, or a name or id holding a username hint
    (``user``, ``email``, ``login``, ``account``)."""
    if _has_username_hint(raw):
        return True
    lowered = raw.element_id.lower()
    return any(hint in lowered for hint in _USERNAME_HINTS)


def _username_hint_strength(raw: _RawInput) -> int:
    """How strongly a username-like input (:func:`_username_hinted`) says it is the
    username: a ``username`` autocomplete, then an ``email`` autocomplete or an
    email type, then a name or id hint."""
    if raw.autocomplete == "username":
        return 3
    if raw.autocomplete == "email" or raw.input_type == "email":
        return 2
    return 1


def _asks_for_a_new_password(raw: _RawInput) -> bool:
    return _is_password(raw) and (
        raw.autocomplete == "new-password"
        or looks_like_new_password_name(raw.name)
        or looks_like_new_password_name(raw.element_id)
    )


def _block_login_groups(parsed: _DocumentParser) -> list[tuple[list[_RawInput], str]]:
    """A DOM snapshot's login group outside every form and dialog, with how it was
    bounded, when the snapshot holds exactly one password input (:func:`_is_password`:
    a password input, or any input marked ``current-password``, whatever its type, a
    hidden one included) with no form owner and in no dialog, and that one asks for
    no new password (:func:`_asks_for_a_new_password`). With none, or with two or
    more, there is none: nothing on the page tells a sign-up's passwords from a
    login's reliably. With no other login form, the scaffold then writes "No login
    form detected" with the observations and a commented-out ``login_config``. A
    form's or a dialog's inputs belong to it and never count here.

    The walk up the password's ancestors looks only at elements holding at most
    ``_MAX_BLOCK_FIELDS`` text-like and password inputs with no form owner and in no
    dialog, never ``<html>`` or ``<body>``, and stops at the first larger one. The
    group is, in this order:

    - ``"inputs"``: the controls with no form owner and in no dialog inside the
      nearest such element holding a username-like input before the password
      (:func:`_username_hinted`), a button-like control after it
      (:func:`_is_button_like`), and no button-like control between that input and
      the password, starting after the last button-like control before that input
      (an input with a button of its own, a search box or a newsletter field, is
      something else);
    - ``"button"``: else, those from the password on inside the nearest such element
      holding a button-like control after it (a password-only step);
    - ``"alone"``: else, the password by itself.

    The snapshot is taken as the user submits, so the control they pressed is likely
    near the password, and a show-password toggle's wrapper, which holds no input
    before the password, does not stop the walk. The group needs no further
    judging: with one password that asks for no new one, the rules a dialog's group is
    judged by (:func:`_ranked_login_groups`) always pass it. One walk up one password
    is linear in the page's length."""
    controls = [raw for raw in _formless_controls(parsed) if raw.dialog is None]
    passwords = [raw for raw in controls if _is_password(raw)]
    if len(passwords) != 1 or _asks_for_a_new_password(passwords[0]):
        return []
    (raw,) = passwords
    in_group = {id(control) for control in controls}
    # Prefix counts over the page: buttons anywhere (any button is one the user may
    # have pressed), fields of the controls above only.
    buttons_before, fields_before = [0], [0]
    # The order of the last username-like input, and of the last button-like
    # control, before each position; -1 for none.
    last_field_before, last_button_before = [-1], [-1]
    for control in parsed.inputs:
        button = _is_button_like(control)
        counted = id(control) in in_group
        buttons_before.append(buttons_before[-1] + button)
        fields_before.append(fields_before[-1] + (counted and _counts_as_field(control)))
        is_field = counted and _is_text_like(control) and _username_hinted(control)
        last_field_before.append(control.order if is_field else last_field_before[-1])
        last_button_before.append(control.order if button else last_button_before[-1])

    def span(block: int) -> tuple[int, int]:
        element = parsed.blocks[block]
        return element.start, element.end if element.end is not None else len(parsed.inputs)

    field = last_field_before[raw.order]
    field_usable = field >= 0 and buttons_before[raw.order] == buttons_before[field + 1]
    with_inputs: int | None = None
    with_button: int | None = None
    block = raw.block
    while block is not None:
        start, end = span(block)
        if fields_before[end] - fields_before[start] > _MAX_BLOCK_FIELDS:
            break
        if (
            parsed.blocks[block].tag not in _PAGE_ELEMENTS
            and buttons_before[end] > buttons_before[raw.order + 1]
        ):
            with_button = block if with_button is None else with_button
            if field_usable and field >= start:
                with_inputs = block
                break
        block = parsed.blocks[block].parent
    chosen = with_inputs if with_inputs is not None else with_button
    if chosen is None:
        # One password that asks for no new one: the rules a dialog's group is
        # judged by (_ranked_login_groups) always pass it.
        return [([raw], "alone")]
    kind = "inputs" if with_inputs is not None else "button"
    start, end = span(chosen)
    # An "inputs" group starts after the last button before its username, a
    # "button" group at the password.
    start = max(start, last_button_before[field] + 1) if kind == "inputs" else raw.order
    orders = [control.order for control in controls]
    group = controls[bisect_left(orders, start) : bisect_left(orders, end)]
    return [(group, kind)]


def extract_login_forms(
    html: str, source: str, *, base: str | None = None, snapshot: bool = False
) -> tuple[LoginForm, ...]:
    """Every login ``<form>`` in *html*: one with a password input that is neither a
    change-password form (:func:`_is_change_password`, never kept) nor a
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

    The page's controls outside every form yield more, after the forms and without
    changing them: one per formless login group (:func:`_formless_login_groups`), a
    script-driven login modal marked as a dialog. The same role rules run over each
    group's controls, with one difference: its username lookup, its extra fields,
    and the search for its submit all end at the first button-like control after
    the password (any ``<button>``, or a submit, image, button, or reset input), so
    a hidden reset panel's input after it is never the username. That control is the
    submit only when it is an explicit one (an ``<input type="submit">`` or
    ``<input type="image">``, or a ``<button type="submit">``), never a typeless or
    ``type="button"`` button, which submits nothing outside a form; otherwise the
    submit is absent and unresolved like a missing username, so the generated step
    carries a ``GP-FILL``. Its selectors are unscoped
    (:func:`_formless_selectors`), its ``action`` and ``method`` are empty (no
    browser submits it, a script sends it with a method of its own), its
    ``action_target`` is ``("", "")``, and ``formless`` is set.

    With *snapshot*, *html* is a DOM snapshot the recorder took at a credential post
    (the page as the user submitted it), every form it gives has ``from_snapshot``
    set, and it gives one more kind of group, listed last: a password outside every
    form and every dialog, grouped with the controls of a small element around it as
    :func:`_block_login_groups` states, with ``plain_block`` set and ``block_bound``
    naming how it was bounded (``"inputs"``, ``"button"``, or ``"alone"``). That
    widening is for a snapshot only: in a recorded HTML response such a block is as
    likely a page's layout as a login, and nothing says the user pressed a button
    there.
    """
    parsed = _parse(html, track_blocks=snapshot)
    candidates = [
        raw
        for raw in parsed.forms
        if _password_index(raw.inputs) is not None and not _is_change_password(raw.inputs)
    ]
    # A registration form is left out only beside a login form: a lone form marked
    # new-password is still the page's login form. Judged on the forms alone, so a
    # formless login never changes what a real form gives.
    logins = [raw for raw in candidates if not _is_registration(raw.inputs)]
    if not logins:
        # A lone form with one password is kept (new-password misused on a login
        # form); a lone form with a confirmation password is a registration form.
        logins = [raw for raw in candidates if len(_password_inputs(raw.inputs)) == 1]
    forms: list[LoginForm] = []
    page_counts = _Counts.of(parsed.inputs)
    # The controls each form physically contains, and each form's bare action, read
    # once: a scope's counts are built once per action, however many forms share it.
    contained_in: dict[int, list[_RawInput]] = {}
    for control in parsed.inputs:
        if control.container is not None:
            contained_in.setdefault(id(control.container), []).append(control)
    bare_actions = {id(other): _bare_action(other) for other in parsed.forms}
    forms_by_action: dict[str | None, list[_RawForm]] = {}
    for other in parsed.forms:
        forms_by_action.setdefault(bare_actions[id(other)], []).append(other)
    scope_counts: dict[str | None, _Counts] = {}
    for raw in logins:
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
        # A scoped selector matches every form with this action (a header mini-login
        # and the main form can share one), so uniqueness is counted across the
        # controls inside all of them.
        if not scopes:
            # No form scope (an action that could not be split, or one masked): a
            # selector is printed only when the page holds no other control it
            # picks, so the page's counts decide, and the scope's could add nothing.
            contained = page_counts
        else:
            if action not in scope_counts:
                scope_counts[action] = _Counts.of(
                    control
                    for other in forms_by_action.get(action, [])
                    for control in contained_in.get(id(other), [])
                )
            contained = scope_counts[action]
        form = _login_form(
            raw.inputs,
            lambda control, scopes=scopes, contained=contained: _selectors_for(
                control, scopes, contained, page_counts
            ),
            action=action,
            method=raw.method,
            source=source,
            action_target=_action_target(raw.action, base or source),
            element_id=raw.element_id,
        )
        if form is not None:
            forms.append(form)
    dialog_groups = _formless_login_groups(parsed)
    block_groups = _block_login_groups(parsed) if snapshot else []
    for group, block_bound in [
        *((group, "") for group in dialog_groups),
        *block_groups,
    ]:
        form = _login_form(
            group,
            lambda control: _formless_selectors(control, page_counts),
            action="",
            method="",
            source=source,
            action_target=("", ""),
            # A real form's element id is a string, so None keeps the two
            # signatures apart even when the controls are the same.
            element_id=None,
            formless=True,
            hinted_username=bool(block_bound),
        )
        if form is not None:
            forms.append(
                replace(form, plain_block=True, block_bound=block_bound) if block_bound else form
            )
    if snapshot:
        return tuple(replace(form, from_snapshot=True) for form in forms)
    return tuple(forms)


def _login_form(
    inputs: list[_RawInput],
    selectors_for: Callable[[_RawInput], tuple[str | None, str | None]],
    *,
    action: str,
    method: str,
    source: str,
    action_target: tuple[str, str],
    element_id: str | None,
    formless: bool = False,
    hinted_username: bool = False,
) -> LoginForm | None:
    """The :class:`LoginForm` the controls *inputs* make, by the role rules
    :func:`extract_login_forms` states, each selector from *selectors_for* (the
    recorded one and the one safe to print unscoped). None when *inputs* hold no
    password input."""
    password_index = _password_index(inputs)
    if password_index is None:
        return None
    hidden: list[str] = []
    dropped_hidden = 0
    for raw_input in inputs:
        if raw_input.input_type != "hidden" or not raw_input.name:
            continue
        if holds_an_id(raw_input.name):
            dropped_hidden += 1
        else:
            hidden.append(raw_input.name)

    if formless:
        # Outside a <form> a button submits nothing unless the page's script makes
        # it, and a typeless or type="button" one there is as likely a show-password
        # toggle or a Close button. So the fields, and the submit search, end at the
        # first button-like control after the password: it is the submit only when
        # it is an explicit one, else the submit is a GP-FILL. A hidden reset panel's
        # submit after a typeless "Sign in" is never taken, and a toggle right after
        # the password ends the fields there (a GP-FILL over a wrong selector).
        block_end = next(
            (i for i in range(password_index + 1, len(inputs)) if _is_button_like(inputs[i])),
            len(inputs),
        )
        submit_index = (
            block_end
            if block_end < len(inputs)
            and _is_submit(inputs[block_end])
            and (inputs[block_end].tag != "button" or inputs[block_end].typed)
            else None
        )
    else:
        # A form keeps its rule: the first submit after the password, a typeless
        # button included, ends the fields.
        submit_index = next(
            (i for i in range(password_index + 1, len(inputs)) if _is_submit(inputs[i])), None
        )
        block_end = submit_index if submit_index is not None else len(inputs)
    # A form's username is looked for among all its controls, as before; a dialog's
    # only within its field block.
    if hinted_username:
        # A snapshot's plain block: only an input a username hint names is the
        # username (graftpunk.har.documents._block_login_groups bounds the group by
        # one), never an unhinted one beside it.
        hinted = [
            (_username_hint_strength(inputs[index]), index)
            for index in range(password_index)
            if _is_text_like(inputs[index]) and _username_hinted(inputs[index])
        ]
        # The strongest hint, and of equal ones the last before the password.
        username_index = max(hinted)[1] if hinted else None
    else:
        username_index = _username_index(inputs, password_index, block_end if formless else None)
    block_start = (username_index if username_index is not None else password_index) + 1
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
    for role, raw_input in roles:
        if role in fields or role in unresolved:
            continue
        selector, unscoped = selectors_for(raw_input)
        if selector is None:
            unresolved.append(role)
            continue
        fields[role] = selector
        if unscoped is not None:
            unscoped_fields[role] = unscoped
    submit: str | None = None
    unscoped_submit: str | None = None
    if submit_index is not None:
        submit, unscoped_submit = selectors_for(inputs[submit_index])
        if submit is None:
            unresolved.append("submit")
    elif formless:
        # Nothing beside the password submits: a GP-FILL, never a guess.
        absent.append("submit")
        unresolved.append("submit")
    return LoginForm(
        action=action,
        method=method,
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
        action_target=action_target,
        input_names=tuple(i.name for i in inputs if i.name),
        visible_names=tuple(i.name for i in inputs if i.name and i.input_type != "hidden"),
        signature=(
            element_id,
            tuple((i.tag, i.input_type, i.name, i.element_id) for i in inputs),
        ),
        formless=formless,
    )


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
    unexpectedly non-JSON response. Any password input counts, a registration or a
    change-password form's included: those exclusions are the digest's, not this
    test's.
    """
    return any(_password_index(raw.inputs) is not None for raw in _parse(html).forms)
