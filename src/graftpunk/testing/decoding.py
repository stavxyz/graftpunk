"""The forms a fixture is read in besides as written, shared by the fixtures check
and by ``gp observe fixtures``, and the forms a recorded value is looked for in.

Pytest-free, so the writer side (``graftpunk.cli.observe_commands``), which never
imports :mod:`graftpunk.testing.plugin`, reads a capture body in the same forms the
check reads its fixture in. Kept in step with
``skills/graft/scripts/fixture-leaks.py`` (``_escape_decodings``,
``_combined_decodings`` and ``_value_forms``; tests compare each pair): that
script runs with the standard library alone and cannot import this.
"""

from __future__ import annotations

import html
import re
from urllib.parse import unquote, unquote_plus

__all__ = ["combined_forms", "decoded_forms", "value_forms", "whole_name_regex"]

_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
_SURROGATE_PAIR = re.compile(r"\\u(d[89ab][0-9a-f]{2})\\u(d[c-f][0-9a-f]{2})", re.IGNORECASE)
_HEX_ESCAPE = re.compile(r"\\x([0-9a-fA-F]{2})")
# A one-letter control escape (\n, \r, \t, \b, \f), so a name on the line after
# a JSON or Python string's \n is read as starting that line.
_CONTROL_ESCAPE = re.compile(r"\\([nrtbf])")
_CONTROL_CHARACTERS = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}
_PERCENT_ROUNDS = 3


def _backslash_unescaped(text: str) -> str:
    """*text* with its ``\\uXXXX`` escapes decoded (a surrogate pair rejoined into
    the one character it spells), its ``\\xXX`` escapes and its ``\\n``, ``\\r``,
    ``\\t``, ``\\b`` and ``\\f`` escapes decoded, and ``\\/`` read as ``/``."""

    def pair(m: re.Match[str]) -> str:
        high, low = int(m.group(1), 16), int(m.group(2), 16)
        return chr(0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00))

    decoded = _SURROGATE_PAIR.sub(pair, text)
    decoded = _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), decoded)
    decoded = _HEX_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), decoded)
    decoded = _CONTROL_ESCAPE.sub(lambda m: _CONTROL_CHARACTERS[m.group(1)], decoded)
    return decoded.replace("\\/", "/")


def decoded_forms(text: str) -> list[str]:
    """*text* with its backslash escapes decoded, with its HTML character
    references decoded, and percent-decoded (``+`` as a space) once per round
    until it stops changing, at most three rounds, so a URL nested in another
    URL's query is read as written."""
    forms = [_backslash_unescaped(text), html.unescape(text)]
    current = text
    for _ in range(_PERCENT_ROUNDS):
        decoded = unquote_plus(current)
        if decoded == current:
            break
        forms.append(decoded)
        current = decoded
    return forms


def combined_forms(text: str) -> list[str]:
    """Each of :func:`decoded_forms`, and each of those decoded again the same
    ways, without repeats and in order: a value JSON-escaped inside an HTML
    attribute (``&quot;a&amp;b\\/c&quot;``) is read once both decodings have
    run. Only the side that compares recorded values with a body reads these;
    the fixtures check reads :func:`decoded_forms`, so a regenerated sidecar can
    only look for more names, never fewer."""
    forms: dict[str, None] = {}
    for form in decoded_forms(text):
        forms.setdefault(form)
        for twice in decoded_forms(form):
            forms.setdefault(twice)
    return list(forms)


def _without_quotes(value: str) -> str:
    """*value* without one pair of surrounding double quotes (RFC 6265 lets a
    server quote a cookie value, and a browser keeps the quotes)."""
    if len(value) > 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def value_forms(value: str) -> frozenset[str]:
    """The forms a recorded *value* is looked for in a body: as recorded,
    percent-decoded (``+`` read as a plus and as a space, since a base64 value
    holds ``+``), and each of those without one pair of surrounding double
    quotes, so a quote sent percent-encoded (``%22``) is taken off too."""
    forms = {value, unquote(value), unquote_plus(value)}
    return frozenset(forms | {_without_quotes(form) for form in forms})


# Where a flagged name ends in a fixture. RFC 6265 makes a cookie name an RFC 2616
# token: letters, digits, and !#$%&'*+-.^_`|~. Letters, digits, _ and - always
# continue a name. The token's other punctuation continues a name only between two
# of those, as in connect.sid or text.v2; anywhere else it is text around the name:
# a full stop ending a sentence, Markdown's backticks and asterisks, an exclamation
# mark, or a $ starting a variable. & ' % + # and | always end a name: in a
# fixture the first four separate query pairs, quote strings, and encode text, #
# starts a URL fragment (cb#access_token=), | separates fields, and no common
# cookie name holds # or |. Anything outside the token, US-ASCII or not, ends a
# name.
_NAME_CORE = r"A-Za-z0-9_\-"
_NAME_PUNCTUATION = r"!$*.^`~"


def whole_name_regex(name: str) -> str:
    """The regular expression for *name*, casefolded, as a whole name in a
    casefolded text, by the rule the comment above gives. The fixtures check
    finds a flagged name with it, and ``gp observe fixtures`` finds the values a
    capture holds under a flagged cookie name with it, so both read a name at
    the same places."""
    core, punctuation = _NAME_CORE, _NAME_PUNCTUATION
    return (
        rf"(?<![{core}])(?<![{core}][{punctuation}])"
        rf"{re.escape(name.casefold())}"
        rf"(?![{core}])(?![{punctuation}][{core}])"
    )
