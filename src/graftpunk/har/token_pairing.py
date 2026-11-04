"""Pair a request header with the source whose value it carried, by value (#369).

A CSRF header's value comes from somewhere the client read it: a cookie, a
``<meta>`` tag, or a hidden input on a page it loaded. Names are weak evidence (a
site can send ``x-csrf`` from a hidden input named ``csrf`` while a cookie named
``x-csrf-v1`` rotates on every response), so the digest compares values: each
time a token-named request header is sent, its value is compared with each cookie
that same request sent and with the latest value of each meta tag and hidden input
on a page of the primary host loaded before it. The source holding the header's
value most often pairs with it. Values are held only in memory, for the length of
one digest, and are never printed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from graftpunk.har.documents import TokenKind, looks_like_token_name, token_source_values

__all__ = ["MIN_PAIRING_VALUE_LENGTH", "TokenPairing", "TokenValueTracker"]

# A shorter header value (a flag, a version number) equals some page value by
# chance. A judgment, not a measurement: a CSRF token is far longer.
MIN_PAIRING_VALUE_LENGTH = 8

# When two sources held the header's value equally often, a cookie wins (no page
# to fetch, and the pairing a name match gave before #369), then a meta tag.
_KIND_PREFERENCE: dict[TokenKind, int] = {"cookie": 0, "meta": 1, "hidden_input": 2}


@dataclass(frozen=True)
class TokenPairing:
    """A request header and the source whose value it carried: a ``cookie``, a
    ``meta`` tag, or a ``hidden_input``, by name, and for a meta tag or a hidden
    input the path template of the page it was read from (``page``), a GET of the
    primary host. ``matched`` is how many of the ``sent`` requests carrying the
    header held that source's value."""

    header: str
    kind: TokenKind
    name: str
    page: str = ""
    matched: int = 0
    sent: int = 0


@dataclass
class _HeaderTally:
    header: str
    sent: int = 0
    # (kind, name) -> page template -> how many requests held that source's value
    # while it was last read from that page. Insertion order is first-match order.
    matches: dict[tuple[TokenKind, str], dict[str, int]] = field(default_factory=dict)
    # Every (kind, lower-cased name) compared with this header at least once.
    measured: set[tuple[TokenKind, str]] = field(default_factory=set)


class TokenValueTracker:
    """Feed it the run in order: :meth:`request` for each kept request before
    :meth:`page` for its response, then read :meth:`pairings`."""

    def __init__(self) -> None:
        # (kind, name) -> (latest value, page template it was read from).
        self._page_values: dict[tuple[TokenKind, str], tuple[str, str]] = {}
        self._tallies: dict[str, _HeaderTally] = {}

    def request(self, headers: dict[str, str], sent_cookies: Iterable[tuple[str, str]]) -> None:
        """Compare each token-named header of a request with *sent_cookies* (that
        request's ``(name, value)`` cookies) and with the page values read so far."""
        token_headers = [
            (name, value)
            for name, value in headers.items()
            if isinstance(value, str)
            and len(value) >= MIN_PAIRING_VALUE_LENGTH
            and looks_like_token_name(name)
        ]
        if not token_headers:
            return
        cookies = list(sent_cookies)
        for name, value in token_headers:
            tally = self._tallies.setdefault(name.lower(), _HeaderTally(header=name))
            tally.sent += 1
            for (kind, source), (held, page) in self._page_values.items():
                tally.measured.add((kind, source.lower()))
                if held == value:
                    by_page = tally.matches.setdefault((kind, source), {})
                    by_page[page] = by_page.get(page, 0) + 1
            for cookie, held in cookies:
                tally.measured.add(("cookie", cookie.lower()))
                if held == value:
                    by_page = tally.matches.setdefault(("cookie", cookie), {})
                    by_page[""] = by_page.get("", 0) + 1

    def page(self, html: str, template: str) -> None:
        """Read the meta tags and hidden inputs of a page loaded at *template*."""
        for kind, name, value in token_source_values(html):
            if len(value) >= MIN_PAIRING_VALUE_LENGTH:
                self._page_values[(kind, name)] = (value, template)

    def pairings(self) -> tuple[TokenPairing, ...]:
        """One pairing per header (by lower-cased name, in the order first sent)
        that held some source's value at least once: the source that held it most
        often, a cookie before a meta tag before a hidden input on a tie, then the
        source named as the header, then the first matched."""
        found: list[TokenPairing] = []
        for tally in self._tallies.values():
            best: TokenPairing | None = None
            best_key: tuple[int, int, int] | None = None
            for (kind, name), by_page in tally.matches.items():
                matched = sum(by_page.values())
                page = max(by_page, key=lambda p: by_page[p])  # first on a tie
                key = (
                    -matched,
                    _KIND_PREFERENCE[kind],
                    0 if name.lower() == tally.header.lower() else 1,
                )
                if best_key is None or key < best_key:
                    best_key = key
                    best = TokenPairing(
                        header=tally.header,
                        kind=kind,
                        name=name,
                        page=page,
                        matched=matched,
                        sent=tally.sent,
                    )
            if best is not None:
                found.append(best)
        return tuple(found)

    def mismatches(self) -> tuple[tuple[str, TokenKind, str], ...]:
        """``(header, kind, source name)``, names lower-cased, for each source
        compared with a header that never held its value, so a pairing by name
        alone does not pick a source the recording shows the header did not carry."""
        return tuple(
            (header, kind, name)
            for header, tally in self._tallies.items()
            for kind, name in sorted(tally.measured)
            if not any(kind == k and name == n.lower() for k, n in tally.matches)
        )
