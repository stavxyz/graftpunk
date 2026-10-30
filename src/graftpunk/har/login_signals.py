"""Suggested ``LoginConfig.success`` selectors read from a recording (#268).

A run usually holds the page it ended on logged in (``page-source.html`` at stop,
or the last page the browser loaded after the credential post), often other pages
it loaded logged in on the way, and several pages it saw logged out (the login
page as served, the DOM snapshot taken as the password was submitted, any page
loaded before the login, and any step of the login after the post, such as an MFA
or "stay signed in" page). An element on the final logged-in page and on none of
the logged-out pages is a candidate for the ``success`` signal, which the engine
tests as element presence.

Only ever a suggestion. One recording cannot show that an element is absent from
the login page the engine opens on a fresh visit (a modal on the home page in the
recording, a dedicated ``/login`` page at replay), and single-recording inference
picked wrong elements on real sites in #267, so the generator writes these
commented out for the author to confirm.

The matcher here understands only the two selector forms it emits, ``#id`` and
``tag.token``, both over plain CSS identifiers, and counts each id and each
(tag, class) pair once a page, so it needs no CSS engine at run time and its cost
grows with the page's size. It follows a browser's parser where the two could
disagree (the first of a duplicated attribute wins, class tokens split on ASCII
whitespace only), and it counts ids and class tokens without case on every page.
That is how a quirks-mode document matches them, and in a standards-mode one it
only rejects more, so a selector it keeps (written in its element's own case)
holds in either mode. ``tests/unit/test_login_signals.py`` checks every emitted
selector against lxml and cssselect where they can tell.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlsplit

from graftpunk.har.paths import holds_an_id
from graftpunk.har.selectors import bounded_selector

__all__ = ["MAX_SUCCESS_CANDIDATES", "success_candidates"]

MAX_SUCCESS_CANDIDATES = 3

# A plain CSS identifier: the only ids and class tokens suggested, so a selector
# needs no escaping and the matcher below agrees with a browser's.
_IDENTIFIER_RE = re.compile(r"-?[A-Za-z_][A-Za-z0-9_-]*")
# A tag a selector can name as written: no namespace prefix, ASCII only.
_PLAIN_TAG_RE = re.compile(r"[a-z][a-z0-9-]*")
# HTML splits a class attribute on ASCII whitespace only.
_ASCII_WHITESPACE_RE = re.compile(r"[\t\n\f\r ]+")
# The word sets below are matched against a name's words (_words), a word alone or
# two adjacent words joined: "sign-out" and "signOut" hold "signout", and
# "catalog-outlet" holds no "logout".
# What a logged-in marker's name tends to hold.
_KEYWORDS = frozenset({"logged", "account", "signout", "logout", "user", "profile", "avatar"})
# A name holding one of these says signed out, signing in, or a guest.
_SIGNED_OUT = frozenset(
    {"loggedout", "signedout", "login", "signin", "signup", "guest", "anonymous"}
)
# A name holding one of these marks a logged-in page only on a control that signs
# out (a link, a button, or a form such as a framework's "logout-form"); on
# anything else it is as likely a "you have signed out" message.
_SIGN_OUT_WORDS = frozenset({"signout", "logout"})
_SIGN_OUT_CONTROLS = frozenset({"a", "button", "form"})
_LOGOUT_SEGMENTS = frozenset({"logout", "log-out", "signout", "sign-out"})
# Elements that render nothing; the engine finds them, but they mark no page.
_NOT_RENDERED = frozenset(
    {"head", "title", "meta", "link", "base", "script", "style", "noscript", "template"}
)
# Elements whose content a script-enabled browser does not put in the document.
# The HTML parser ignores a self-closing flag on these, so <template/> opens one.
_INERT_CONTAINERS = frozenset({"template", "noscript"})
# Foreign content: a tag selector's case rules differ there, so these elements and
# everything inside them are suggested by id only. A self-closing flag is honoured.
_FOREIGN = frozenset({"svg", "math"})
# Tags the HTML parser renames as it builds the document (<image> becomes <img>),
# so a selector naming the tag as written matches nothing there: id only.
_RENAMED_TAGS = frozenset({"image"})
# The autocomplete token of an input asking for an MFA or emailed code.
_ONE_TIME_CODE = "one-time-code"
# A credential value this short, or one of these words, is in too many names to
# say anything about the account.
_MIN_VALUE_LENGTH = 3
_NOT_ACCOUNT_VALUES = frozenset({"true", "false", "yes", "off", "null", "none"})

# Generated names, each rule on its own (the tests pin that each catches a name no
# other does): not a plain identifier; a CSS-module class (Header_loggedIn__x7Q2a,
# Header-module__loggedIn___abcDE); any "___"; a "_<word>_<5 characters>" class; a
# CSS-in-JS class; a run of three or more digits (ember1234); a part of four or
# more characters mixing letters and digits (a build hash); and an account value.
_CSS_MODULE_RE = re.compile(r"[A-Za-z0-9-]+_{1,2}[A-Za-z0-9]+_{2,3}[A-Za-z0-9_-]{4,}")
_UNDERSCORE_HASH_RE = re.compile(r"_[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_-]{5}")
_CSS_IN_JS_PREFIXES = ("css-", "sc-", "jsx-", "emotion-", "svelte-", "styled-")
_DIGIT_RUN_RE = re.compile(r"[0-9]{3,}")
_MIXED_PART_RE = re.compile(r"(?=[A-Za-z0-9]*[0-9])(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{4,}")

_GENERATED_RULES: tuple[tuple[str, Callable[[str], bool]], ...] = (
    ("not_identifier", lambda name: not _IDENTIFIER_RE.fullmatch(name)),
    ("css_module", lambda name: bool(_CSS_MODULE_RE.fullmatch(name))),
    ("triple_underscore", lambda name: "___" in name),
    ("underscore_hash", lambda name: bool(_UNDERSCORE_HASH_RE.fullmatch(name))),
    ("css_in_js", lambda name: name.lower().startswith(_CSS_IN_JS_PREFIXES)),
    ("digit_run", lambda name: bool(_DIGIT_RUN_RE.search(name))),
    (
        "mixed_part",
        lambda name: any(_MIXED_PART_RE.fullmatch(part) for part in re.split(r"[-_]", name)),
    ),
    ("account_value", holds_an_id),
)


def _compact(text: str) -> str:
    return re.sub(r"[-_.]", "", text.lower())


def _words(name: str) -> list[str]:
    """*name*'s words, lower-cased: split at a case change and at anything that is
    not a letter (``-``, ``_``, ``.``, and digits among them)."""
    return [word.lower() for word in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", name)]


def _holds_word(name: str, wanted: frozenset[str]) -> bool:
    """Whether a word of *name*, or two adjacent words joined, is one of *wanted*."""
    words = _words(name)
    joined = {first + second for first, second in zip(words, words[1:], strict=False)}
    return not wanted.isdisjoint({*words, *joined})


@dataclass(frozen=True)
class _Element:
    tag: str
    element_id: str
    classes: tuple[str, ...]
    # A link's href, or a form's action.
    target: str
    # Inside a template or a noscript: not in a script-enabled document.
    inert: bool
    # An svg or math element, or inside one.
    foreign: bool


@dataclass
class _Page:
    elements: list[_Element] = field(default_factory=list)
    # Lower-cased ids and (tag, class token) pairs, each counted once an element.
    ids: Counter[str] = field(default_factory=Counter)
    pairs: Counter[tuple[str, str]] = field(default_factory=Counter)
    # Whether an input asks for a one-time code: a step of the login.
    asks_for_a_code: bool = False

    def count(self, key: tuple[str, ...]) -> int:
        """How many elements the selector *key* (``("id", name)`` or ``("class", tag,
        token)``) selects on this page, the name compared without case."""
        if key[0] == "id":
            return self.ids[key[1].lower()]
        return self.pairs[(key[1], key[2].lower())]


class _ElementReader(HTMLParser):
    """Every element's tag, id, class tokens, and link or form target, in document
    order, and whether an input asks for a one-time code."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[_Element] = []
        self.asks_for_a_code = False
        self._inert_depth = 0
        self._foreign_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values: dict[str, str] = {}
        for name, value in attrs:
            # A browser keeps the first of a duplicated attribute.
            values.setdefault(name, value or "")
        if tag == "input" and _ONE_TIME_CODE in values.get("autocomplete", "").lower().split():
            self.asks_for_a_code = True
        self.elements.append(
            _Element(
                tag=tag,
                element_id=values.get("id", ""),
                classes=tuple(
                    token for token in _ASCII_WHITESPACE_RE.split(values.get("class", "")) if token
                ),
                target=values.get("href" if tag == "a" else "action", ""),
                inert=self._inert_depth > 0,
                foreign=self._foreign_depth > 0 or tag in _FOREIGN,
            )
        )
        if tag in _INERT_CONTAINERS:
            self._inert_depth += 1
        if tag in _FOREIGN:
            self._foreign_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag in _FOREIGN:
            self._foreign_depth -= 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _INERT_CONTAINERS and self._inert_depth > 0:
            self._inert_depth -= 1
        if tag in _FOREIGN and self._foreign_depth > 0:
            self._foreign_depth -= 1


def _read(html: str) -> _Page:
    reader = _ElementReader()
    try:
        reader.feed(html)
        reader.close()
    except (AssertionError, ValueError):
        # HTMLParser raises on very little; what was read before stands.
        pass
    page = _Page(elements=reader.elements, asks_for_a_code=reader.asks_for_a_code)
    for element in page.elements:
        if element.element_id:
            page.ids[element.element_id.lower()] += 1
        for token in {token.lower() for token in element.classes}:
            page.pairs[(element.tag, token)] += 1
    return page


def _needles(values: Iterable[str]) -> frozenset[str]:
    """The credential post's *values*, and an email's local part, as a name is
    compared with them: lower-cased with ``-``, ``_``, and ``.`` removed
    (:func:`_compact`), leaving out a short one or a boolean word. Comparing the
    compact forms finds every name that holds a value as written too."""
    found: set[str] = set()
    for value in values:
        text = value.strip()
        for part in (text, text.split("@", 1)[0] if "@" in text else ""):
            form = _compact(part)
            if len(form) >= _MIN_VALUE_LENGTH and form not in _NOT_ACCOUNT_VALUES:
                found.add(form)
    return frozenset(found)


def _says_signed_out(name: str, element: _Element) -> bool:
    return _holds_word(name, _SIGNED_OUT) or (
        element.tag not in _SIGN_OUT_CONTROLS and _holds_word(name, _SIGN_OUT_WORDS)
    )


def _targets_logout(element: _Element) -> bool:
    """Whether *element* is a link or a form whose target has a logout path segment."""
    if element.tag not in ("a", "form") or not element.target:
        return False
    try:
        path = urlsplit(element.target).path
    except ValueError:
        return False
    return any(segment.lower() in _LOGOUT_SEGMENTS for segment in path.split("/"))


def _usable(name: str, element: _Element, needles: frozenset[str]) -> bool:
    if any(check(name) for _label, check in _GENERATED_RULES):
        return False
    if _says_signed_out(name, element):
        return False
    compact = _compact(name)
    return not any(needle in compact for needle in needles)


def _selectors(
    element: _Element, needles: frozenset[str]
) -> list[tuple[str, tuple[str, ...], str, bool]]:
    """Each selector *element* could be suggested by, as (selector, its count key,
    the name it is built on, whether it is an id), id first."""
    found: list[tuple[str, tuple[str, ...], str, bool]] = []
    if element.element_id and _usable(element.element_id, element, needles):
        found.append(
            (f"#{element.element_id}", ("id", element.element_id), element.element_id, True)
        )
    if element.foreign or element.tag in _RENAMED_TAGS or not _PLAIN_TAG_RE.fullmatch(element.tag):
        return found
    for token in dict.fromkeys(element.classes):
        if _usable(token, element, needles):
            found.append((f"{element.tag}.{token}", ("class", element.tag, token), token, False))
    return found


def success_candidates(
    logged_out: Sequence[str],
    logged_in: str,
    *,
    earlier_logged_in: Sequence[str] = (),
    account_values: Iterable[str] = (),
) -> tuple[str, ...]:
    """Up to :data:`MAX_SUCCESS_CANDIDATES` selectors for an element on the final
    logged-in page and on no logged-out page: each selects exactly one element of
    *logged_in* (the HTML of the page the run ended on logged in) and none of any
    page of *logged_out*. Empty when *logged_in* is.

    *earlier_logged_in* are the pages loaded after the credential post, before the
    final one, that the caller did not find to be a step of the login. One of them
    with an input for a one-time code (``autocomplete="one-time-code"``) is a step
    all the same and counts as logged out; the rest are logged in.

    A selector is ``#id`` or ``tag.token`` over a plain CSS identifier (the tag form
    only for a plain tag outside ``svg`` and ``math`` that the parser does not
    rename), and never one whose name looks generated (``_GENERATED_RULES``, an
    account value by :func:`graftpunk.har.paths.holds_an_id` among them), has a
    word saying signed out, signing in, or a guest (a sign-out word counts only on
    a link, a button, or a form), or holds one of *account_values* (the credential
    post's username, password, and email values) or an email's local part, without
    case. An element that renders nothing (in the head, a script, a template's or a
    noscript's content) is not suggested. Ids and class tokens are counted without
    case on every page; a selector keeps its element's case.

    Ranked: a selector on every logged-in page first; then a name with a word
    ``logged``, ``account``, ``signout``, ``logout``, ``user``, ``profile``, or
    ``avatar``, or a link or form whose target has a ``logout``, ``log-out``,
    ``signout``, or ``sign-out`` path segment; then an id before a class; then
    document order. Only selectors are returned, never text or attribute values."""
    if not logged_in:
        return ()
    final = _read(logged_in)
    others = [_read(html) for html in logged_out]
    also_logged_in: list[_Page] = []
    for html in earlier_logged_in:
        page = _read(html)
        (others if page.asks_for_a_code else also_logged_in).append(page)
    needles = _needles(account_values)
    ranked: dict[str, tuple[int, int, int, int]] = {}
    seen: set[str] = set()
    for order, element in enumerate(final.elements):
        if element.inert or element.tag in _NOT_RENDERED:
            continue
        for selector, key, name, is_id in _selectors(element, needles):
            if selector in seen:
                continue
            seen.add(selector)
            if (
                bounded_selector(selector) is None
                or final.count(key) != 1
                or any(page.count(key) for page in others)
            ):
                continue
            everywhere = all(page.count(key) for page in also_logged_in)
            keyword = _holds_word(name, _KEYWORDS) or _targets_logout(element)
            ranked[selector] = (
                0 if everywhere else 1,
                0 if keyword else 1,
                0 if is_id else 1,
                order,
            )
    return tuple(sorted(ranked, key=ranked.__getitem__)[:MAX_SUCCESS_CANDIDATES])
