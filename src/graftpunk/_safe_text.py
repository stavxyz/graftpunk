"""Display text for any value, never raising: the one place graftpunk turns a
value into text for a table cell, a csv or xlsx cell, a pdf, a JSON fallback or
a log line.

``str()`` raises for an int past Python's 4300-digit limit and for any object
whose ``__str__`` raises, and a lone surrogate cannot be written to a UTF-8
file or stream. Each site that wrote ``str(value)`` raised on those.
"""

from __future__ import annotations


def visible_text(text: str) -> str:
    """*text* with each lone surrogate as U+FFFD, and unchanged otherwise.

    ``json.loads('"\\\\ud800"')`` makes such a string. No UTF-8 file or stream
    can hold a lone surrogate, so writing one raised ``UnicodeEncodeError``. A
    high and low surrogate in order become the character they encode.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    return text


def unprintable(value: object) -> str:
    """The fixed stand-in for a value with no text: ``<unprintable TYPE>``."""
    try:
        name = type(value).__name__
    except Exception:  # noqa: BLE001 - a type whose name cannot be read
        name = "object"
    return f"<unprintable {name}>"


def safe_text(value: object) -> str:
    """``str(value)``, else ``repr(value)``, else :func:`unprintable`, with each
    lone surrogate as U+FFFD. Never raises."""
    if type(value) is str:
        return visible_text(value)
    for render in (str, repr):
        try:
            text = render(value)
        except Exception:  # noqa: BLE001, S112 - the next form, then the stand-in
            continue
        if isinstance(text, str):
            return visible_text(text)
    return unprintable(value)


def safe_repr(value: object) -> str:
    """``repr(value)``, else :func:`unprintable`, with each lone surrogate as
    U+FFFD. Never raises."""
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001 - the stand-in
        return unprintable(value)
    return visible_text(text) if isinstance(text, str) else unprintable(value)
