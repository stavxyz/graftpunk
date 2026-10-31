"""The forms a fixture is read in besides as written, shared by the fixtures check
and by ``gp observe fixtures``.

Pytest-free, so the writer side (``graftpunk.cli.observe_commands``), which never
imports :mod:`graftpunk.testing.plugin`, reads a capture body in the same forms the
check reads its fixture in. Kept in step with
``skills/graft/scripts/fixture-leaks.py``'s ``_escape_decodings`` (a test compares
the two): that script runs with the standard library alone and cannot import this.
"""

from __future__ import annotations

import html
import re
from urllib.parse import unquote_plus

__all__ = ["decoded_forms"]

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
