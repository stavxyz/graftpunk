"""The ``LoginConfig.failure`` text a wrong-password attempt suggests (#268, part 3).

``gp observe interactive --wrong-password-first`` reads the page after a credential
post the user marks as failed. The raw HTML of those reads is held in memory only;
what survives here is a few text strings, each one the engine would match on that
page (:func:`graftpunk.plugins.login_settle.failure_text_on_page`) and not on the page
at the post, and holding no value either post sent. Suggestions only: the generator
writes them commented out.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser

from graftpunk.har.digest import body_values, post_account_values, post_every_value
from graftpunk.har.documents import PASSWORD_NAME_HINTS
from graftpunk.har.login_signals import account_needles, holds_an_account_value
from graftpunk.observe.run import Redaction
from graftpunk.observe.snapshot import holds_an_email, snapshot_redaction
from graftpunk.plugins.login_settle import failure_text_on_page

__all__ = [
    "MAX_FAILURE_CANDIDATES",
    "MAX_FAILURE_TEXT",
    "MIN_FAILURE_TEXT",
    "AccountGuard",
    "failed_password_values",
    "failure_candidates",
    "without_success_page",
]

MAX_FAILURE_CANDIDATES = 3
# A shorter text matches too much of an ordinary page; a longer one is a paragraph,
# not a message.
MIN_FAILURE_TEXT = 8
MAX_FAILURE_TEXT = 200

# Elements whose contents are no text a person reads: the engine still matches
# them (they are in the raw HTML), so text the login page holds there is caught by
# the check against the page at the post.
_NO_TEXT = frozenset({"script", "style", "template", "noscript", "textarea"})
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
_ALERT_CLASS_WORDS = ("error", "invalid", "alert")
# Below this a value is not redacted from a run either (graftpunk.observe.run).
_MIN_SPELLING = 3


@dataclass(frozen=True)
class AccountGuard:
    """What a suggested text may not hold, from a credential post's body: each value
    the digest's guard takes for an account value
    (:func:`graftpunk.har.digest.post_account_values`, and every value but a token
    field's, :func:`graftpunk.har.digest.post_every_value`), compared as the guard
    compares a name (:func:`graftpunk.har.login_signals.holds_an_account_value`); each
    spelling the snapshot redaction knows of the post's own values
    (:func:`graftpunk.observe.snapshot.snapshot_redaction`), any letter case; and any
    email address (:func:`graftpunk.observe.snapshot.holds_an_email`)."""

    needles: frozenset[str]
    spellings: tuple[str, ...]
    redaction: Redaction

    @classmethod
    def of(cls, body_text: str | None, content_type: str = "") -> AccountGuard | None:
        """The guard for the post whose body is *body_text*; None when the body gives
        no values read by their field names, so the username cannot be seen."""
        values = post_account_values(body_text, content_type)
        if values is None:
            return None
        redaction = snapshot_redaction(body_text, content_type)
        spellings = tuple(
            dict.fromkeys(
                spelling.lower()
                for spelling in redaction.everything
                if len(spelling) >= _MIN_SPELLING
            )
        )
        return cls(
            account_needles([*values, *post_every_value(body_text, content_type)]),
            spellings,
            redaction,
        )

    def holds(self, text: str) -> bool:
        """Whether *text* holds an account value, a spelling of a posted value, or an
        email address."""
        lowered = text.lower()
        return (
            holds_an_account_value(text, self.needles)
            or any(spelling in lowered for spelling in self.spellings)
            or holds_an_email(text, self.redaction)
        )


class _TextNodes(HTMLParser):
    """Each text node outside :data:`_NO_TEXT`, whitespace-trimmed, as (text, whether
    it is inside an alert), in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.open: list[tuple[str, bool]] = []
        self.nodes: list[tuple[str, bool]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _VOID:
            self.open.append((tag, _marks_an_alert(attrs)))

    def handle_endtag(self, tag: str) -> None:
        for at in range(len(self.open) - 1, -1, -1):
            if self.open[at][0] == tag:
                del self.open[at:]
                return

    def handle_data(self, data: str) -> None:
        if any(tag in _NO_TEXT for tag, _alert in self.open):
            return
        text = data.strip()
        if text:
            self.nodes.append((text, any(alert for _tag, alert in self.open)))


def _marks_an_alert(attrs: list[tuple[str, str | None]]) -> bool:
    """``role="alert"``, an ``aria-live`` other than ``off``, or a class token holding
    ``error``, ``invalid``, or ``alert``, any letter case."""
    for name, value in attrs:
        value = (value or "").strip().lower()
        if name == "role" and "alert" in value.split():
            return True
        if name == "aria-live" and value and value != "off":
            return True
        if name == "class" and any(
            word in token for token in value.split() for word in _ALERT_CLASS_WORDS
        ):
            return True
    return False


def _text_nodes(html: str) -> list[tuple[str, bool]]:
    parser = _TextNodes()
    parser.feed(html)
    parser.close()
    return parser.nodes


def failure_candidates(
    after_reads: Sequence[str], before: str | None, guard: AccountGuard | None
) -> tuple[str, ...]:
    """The failure texts the reads of the page after a failed post suggest, best
    first, at most :data:`MAX_FAILURE_CANDIDATES`.

    Each text node of each read in *after_reads* (raw HTML), whitespace-trimmed,
    :data:`MIN_FAILURE_TEXT` to :data:`MAX_FAILURE_TEXT` characters, that the engine
    would find on that read and not on *before*, the raw page at the post
    (:func:`graftpunk.plugins.login_settle.failure_text_on_page`), and that *guard*
    does not hold. Text split by an element gives each part, never the sentence,
    and text holding a character the serializer escapes (``&`` as ``&amp;``) is not
    on the raw HTML as written, so it is dropped: the engine would never match
    either. Ranked: inside an alert (``role="alert"``, an ``aria-live``, a class
    token holding ``error``, ``invalid`` or ``alert``) first, then in the order of
    the reads and of each document; a text repeated in any letter case is given once.

    Nothing without *before* or *guard*: a text the login page already shows would
    fail every login, and a text whose account values cannot be checked may hold one.
    """
    if before is None or guard is None:
        return ()
    found: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    order = 0
    for html in after_reads:
        for text, in_alert in _text_nodes(html):
            order += 1
            key = text.lower()
            if (
                key in seen
                or not MIN_FAILURE_TEXT <= len(text) <= MAX_FAILURE_TEXT
                or not failure_text_on_page(html, text)
                or failure_text_on_page(before, text)
                or guard.holds(text)
            ):
                continue
            seen.add(key)
            found.append((0 if in_alert else 1, order, text))
    found.sort()
    return tuple(text for _rank, _order, text in found[:MAX_FAILURE_CANDIDATES])


def without_success_page(
    candidates: Iterable[str], page: str | None, guard: AccountGuard | None
) -> tuple[str, ...]:
    """*candidates* less each one the engine would find on *page*, the raw page read
    after the real login's post (it would fail every good login), and each one that
    *guard*, the real post's, holds. None for *guard* (the real post's values cannot
    be read) drops them all."""
    if guard is None:
        return ()
    return tuple(
        text
        for text in candidates
        if not guard.holds(text) and (page is None or not failure_text_on_page(page, text))
    )


def failed_password_values(body_text: str | None, content_type: str = "") -> list[str]:
    """The values of the failed post's body (:func:`graftpunk.har.digest.body_values`)
    under a password-like name (a :data:`graftpunk.har.documents.PASSWORD_NAME_HINTS`
    hint in the nearest name, the rule :func:`graftpunk.har.digest.post_account_values`
    reads a password by), for the run's redaction: an interactive run keeps what it
    posted, and a wrong password is often the real one with a typo."""
    return [
        value
        for path, value in body_values(body_text, content_type)
        if value and path and any(hint in path[-1].lower() for hint in PASSWORD_NAME_HINTS)
    ]
