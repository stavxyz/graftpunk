"""Suggested ``LoginConfig.success`` selectors read from a recording (#268).

A run usually holds the page it ended on logged in (``page-source.html`` at stop,
or the last page the browser loaded after the credential post), often other pages
it loaded logged in on the way, and several pages it saw logged out (the login
page as served, the DOM snapshot taken as the password was submitted, any page
loaded before the login, and the steps of the login after the post, such as an MFA
or "stay signed in" page). An element on the final logged-in page and on none of
the logged-out pages is a candidate for the ``success`` signal, which the engine
tests as element presence.

The steps of the login are the leading run of the pages loaded after the post: a
page there is a step when it asks for a one-time code or the browser submitted a
step-shaped form from it (:func:`is_login_step_post`). The first page that is
neither ends the run, and it and every later page are logged in, whatever they
submit or hold (a search, a cart, a 2FA settings page). A step with no one-time-code
input cannot be told from a logged-in page by a recording, and counts as one, when a
script sends its code, when its form posts only names and a path with no step word
(``pin``, ``passcode``), or when the run stopped on it without submitting it.

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
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlsplit

from graftpunk.har.documents import looks_like_token_name
from graftpunk.har.paths import holds_an_id
from graftpunk.har.selectors import bounded_selector

__all__ = [
    "MAX_SUCCESS_CANDIDATES",
    "could_be_an_account_value",
    "is_login_step_post",
    "success_candidates",
]

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
# A URL path segment holding one of these words (or "log out", "sign off" and the
# like as two adjacent words) makes a logout path: "/auth/sign_out", "/logout.php"
# (the split at "." leaves a file extension a word of its own), "/Account/LogOff".
_LOGOUT_WORDS = frozenset({"logout", "logoff", "signout", "signoff"})
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
# A form posted from a page after the credential post is a step of the login when a
# body field's name or a segment of its URL path holds one of these words. "code"
# and "remember" end too many ordinary names (postal_code, currencyCode,
# remember_filters) to be among them: "code" counts in a field name alone or with
# only _CODE_COMPANIONS beside it (sms_code, one_time_code, passCode), and
# "remember" with only _REMEMBER_COMPANIONS beside it (remember_me, rememberDevice,
# remember_for_30_days). A step word needs no companion, so neither set holds one
# (remember_login and auth_code are steps by "login" and "auth" alone).
_STEP_WORDS = frozenset(
    {
        "otp",
        "totp",
        "mfa",
        # "2fa" too, read as "tfa" (_TWO_FA_RE).
        "tfa",
        "twofactor",
        "verify",
        "verification",
        "challenge",
        "trust",
        "kmsi",
        "stay",
        "signin",
        "login",
        "auth",
        "sso",
        "consent",
    }
)
_REMEMBER_COMPANIONS = frozenset(
    {"me", "my", "this", "device", "browser", "computer", "for", "days"}
)
_CODE_COMPANIONS = frozenset(
    {
        "sms",
        "email",
        "security",
        "one",
        "time",
        "backup",
        "recovery",
        "pass",
        "access",
    }
)
# A token-named field (a CSRF field a framework puts in every form) is not read for
# step words unless it holds one of these: otp_token, mfaToken, totpToken.
_ONE_TIME_CODE_WORDS = frozenset({"otp", "totp", "mfa", "tfa", "twofactor"})
# "2fa" as a word. _words splits the digit from its letters, so it is read as "tfa"
# (2faCode, 2FA_token, verify-2fa).
_TWO_FA_RE = re.compile(r"(?<![A-Za-z0-9])(?:2fa|2FA|2Fa)(?![a-z])")
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
    """*text* lower-cased without ``-``, ``_``, ``.``, or whitespace: a name holds
    no whitespace, so a value is compared as a name could spell it."""
    return re.sub(r"[-_.\s]", "", text.lower())


def _words(name: str) -> list[str]:
    """*name*'s words, lower-cased: split at a case change and at anything that is
    not a letter (``-``, ``_``, ``.``, and digits among them)."""
    return [word.lower() for word in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", name)]


def _holds_word(name: str, wanted: frozenset[str]) -> bool:
    """Whether a word of *name*, or two adjacent words joined, is one of *wanted*."""
    words = _words(name)
    joined = {first + second for first, second in zip(words, words[1:], strict=False)}
    return not wanted.isdisjoint({*words, *joined})


def _step_words(name: str) -> str:
    """*name* with a ``2fa`` spelled ``tfa``, so its words (:func:`_words`) hold it."""
    return _TWO_FA_RE.sub("_tfa_", name)


def _is_step_name(name: str, *, field: bool) -> bool:
    """Whether *name* (a body field's name when *field*, else a URL path segment)
    holds a word of :data:`_STEP_WORDS` (:func:`_holds_word`), is ``remember`` with
    only :data:`_REMEMBER_COMPANIONS` beside it, or, a field name only, is ``code``
    with only :data:`_CODE_COMPANIONS` beside it (a path segment ``code``, as in
    ``/promo/code``, is no step). Digit runs are no words (``remember_for_30_days``)."""
    spelled = _step_words(name)
    if _holds_word(spelled, _STEP_WORDS):
        return True
    words = set(_words(spelled))
    if "remember" in words and words <= {"remember", *_REMEMBER_COMPANIONS}:
        return True
    return field and "code" in words and words <= {"code", *_CODE_COMPANIONS}


def _read_for_steps(name: str) -> bool:
    """Whether body field *name* is read for step words: every name but a token's
    (:func:`graftpunk.har.documents.looks_like_token_name`), which a framework puts
    in every form it renders, unless the token's name holds a word of
    :data:`_ONE_TIME_CODE_WORDS` (``otp_token``, ``mfaToken``)."""
    return not looks_like_token_name(name) or _holds_word(_step_words(name), _ONE_TIME_CODE_WORDS)


def _is_logout_path(path: str) -> bool:
    """Whether URL *path* has a logout segment: a segment holding a word of
    :data:`_LOGOUT_WORDS` or two adjacent words joining into one, without case
    (``/auth/sign_out``, ``/logout.php``, ``/Account/LogOff``,
    ``/auth/logout/confirm``)."""
    return any(_holds_word(segment, _LOGOUT_WORDS) for segment in path.split("/"))


def is_login_step_post(path: str, field_names: Iterable[str]) -> bool:
    """Whether a form posted to *path* with body fields *field_names* looks like a
    step of a login (an MFA code, "stay signed in", a consent): a field name or a
    path segment is a step name (:func:`_is_step_name`). A search or a cart form is
    not one, and neither is a post to a logout path (:func:`_is_logout_path`),
    whatever else it holds. A field named like a CSRF token
    (:func:`graftpunk.har.documents.looks_like_token_name`, ASP.NET's
    ``__RequestVerificationToken`` among them) is not read unless it names a
    one-time code (:func:`_read_for_steps`)."""
    if _is_logout_path(path):
        return False
    return any(_is_step_name(segment, field=False) for segment in path.split("/")) or any(
        _is_step_name(name, field=True) for name in field_names if _read_for_steps(name)
    )


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
    compared with them: lower-cased with ``-``, ``_``, ``.``, and whitespace
    removed (:func:`_compact`), leaving out a short one or a boolean word. Comparing the
    compact forms finds every name that holds a value as written too."""
    found: set[str] = set()
    for value in values:
        text = value.strip()
        for part in (text, text.split("@", 1)[0] if "@" in text else ""):
            if could_be_an_account_value(part):
                found.add(_compact(part))
    return frozenset(found)


def could_be_an_account_value(value: str) -> bool:
    """Whether *value*, compared as a name could spell it (:func:`_compact`), is long
    enough to say anything about an account (:data:`_MIN_VALUE_LENGTH`) and is not
    a boolean word (``true``, ``off``, ``none``)."""
    form = _compact(value)
    return len(form) >= _MIN_VALUE_LENGTH and form not in _NOT_ACCOUNT_VALUES


def _says_signed_out(name: str, element: _Element) -> bool:
    return _holds_word(name, _SIGNED_OUT) or (
        element.tag not in _SIGN_OUT_CONTROLS and _holds_word(name, _SIGN_OUT_WORDS)
    )


def _targets_logout(element: _Element) -> bool:
    """Whether *element* is a link or a form whose target is a logout path
    (:func:`_is_logout_path`)."""
    if element.tag not in ("a", "form") or not element.target:
        return False
    try:
        path = urlsplit(element.target).path
    except ValueError:
        return False
    return _is_logout_path(path)


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
    earlier: Sequence[str] = (),
    submitted_a_step: Collection[int] = (),
    account_values: Iterable[str] = (),
) -> tuple[str, ...]:
    """Up to :data:`MAX_SUCCESS_CANDIDATES` selectors for an element on the final
    logged-in page and on no logged-out page: each selects exactly one element of
    *logged_in* (the HTML of the page the run ended on) and none of any page of
    *logged_out*. Empty when *logged_in* is.

    *earlier* are the pages loaded after the credential post before *logged_in*, in
    order, and *submitted_a_step* the indexes into ``[*earlier, logged_in]`` of the
    pages the browser submitted a step-shaped form from
    (:func:`is_login_step_post`) before loading the next. The leading run of those
    pages that either submitted a step or hold an input asking for a one-time code
    (``autocomplete="one-time-code"``) are steps of the login, and count as logged
    out. The first page that is neither, and every page after it, are logged in.
    When every page is a step, the final one included, there is no logged-in page
    and nothing is suggested.

    A selector is ``#id`` or ``tag.token`` over a plain CSS identifier (the tag form
    only for a plain tag outside ``svg`` and ``math`` that the parser does not
    rename), and never one whose name looks generated (``_GENERATED_RULES``, an
    account value by :func:`graftpunk.har.paths.holds_an_id` among them), has a
    word saying signed out, signing in, or a guest (a sign-out word counts only on
    a link, a button, or a form), or holds one of *account_values* (the credential
    post's account values, :func:`graftpunk.har.digest._account_values`) or an
    email's local part, compared by :func:`_compact`. An element that renders
    nothing (in the head, a script, a template's or a noscript's content) is not
    suggested. Ids and class tokens are counted without
    case on every page; a selector keeps its element's case.

    Ranked: a selector on every logged-in page first; then a name with a word
    ``logged``, ``account``, ``signout``, ``logout``, ``user``, ``profile``, or
    ``avatar``, or a link or form whose target has a ``logout``, ``log-out``,
    ``signout``, or ``sign-out`` path segment; then an id before a class; then
    document order. Only selectors are returned, never text or attribute values."""
    if not logged_in:
        return ()
    after_post = [_read(html) for html in (*earlier, logged_in)]
    landing = next(
        (
            index
            for index, page in enumerate(after_post)
            if index not in submitted_a_step and not page.asks_for_a_code
        ),
        None,
    )
    if landing is None:
        return ()
    final = after_post[-1]
    others = [_read(html) for html in logged_out] + after_post[:landing]
    also_logged_in = after_post[landing:-1]
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
