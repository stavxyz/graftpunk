"""The graft skill's har-facts.py reads ISO 8601 times as Python 3.11's
``datetime.fromisoformat`` does, on every Python the script runs on (3.9 on).

This module imports only the standard library and pytest, so it also runs under
Python 3.9 and 3.10, which CI does not run::

    uv run --no-project -p 3.9 --with pytest pytest -p no:cacheprovider \\
        --noconftest -o addopts= tests/unit/test_har_facts_iso.py
"""

from __future__ import annotations

import importlib.util
import itertools
import random
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "graft" / "scripts" / "har-facts.py"


def _load_har_facts() -> ModuleType:
    spec = importlib.util.spec_from_file_location("har_facts_iso", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _corpus() -> list[str]:
    """Every joining of the review's dates, separators, times, fractions, and
    offsets, valid or not, and seeded random strings after a date."""
    texts: set[str] = set()
    dates = [
        "1970-01-01",
        "19700101",
        "2099-12-31",
        "2099-02-29",
        "2099-13-01",
        "1970-0101",
        "2026-W44-6",
        "2026W446",
        "2026-W44",
        "2026W44",
        "2020-W53-7",
        "2026-W53-1",
        "2099-001",
    ]
    separators = ["T", " ", "t", "_", "X", "", "0", "+", ".", "é", "€", "\ud800"]
    times = [
        "",
        "00",
        "24",
        "1",
        "000",
        "00:",
        "00:00",
        "0000",
        "00:0000",
        "00:00:00",
        "000000",
        "0000000",
        "23:59:60",
        "24:00:00",
        "12:30:45",
    ]
    fractions = ["", ".", ",", ".5", ",5", ".123", ".1234567", ".1234567x", ".5."]
    offsets = [
        "",
        "Z",
        "z",
        "+00",
        "-05",
        "+0",
        "+000",
        "+0530",
        "+05:30",
        "+05:3",
        "+053015",
        "+05:30:15",
        "+05.5",
        "+05:30.5",
        "+05:30:15.5",
        "+05:30:15,5",
        "+05:30:15.",
        "+00:00.5",
        "+24:00",
        "-00:00",
        " +00:00",
        "UTC",
    ]
    for parts in itertools.product(dates, separators, times, fractions, offsets):
        texts.add("".join(parts))
    rng = random.Random(315)  # noqa: S311 - a fixed sample, not a secret
    alphabet = "0123456789-:T.,+Z tzW_é"
    for _ in range(40_000):
        date = rng.choice(["1970-01-01", "19700101", "2026-W44-6", "2026W446", "2026W44"])
        texts.add(date + "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 20))))
    return sorted(texts)


def _fromisoformat(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _same(got: datetime | None, expected: datetime | None) -> bool:
    if got is None or expected is None:
        return got is expected
    return got == expected and got.utcoffset() == expected.utcoffset()


@pytest.mark.skipif(
    sys.version_info >= (3, 14),
    reason="3.14's fromisoformat reads an hour of 24 and rejects strings 3.11 reads",
)
def test_the_har_reader_reads_what_fromisoformat_reads_on_3_11() -> None:
    """On 3.11 to 3.13 (one reader, measured), the script's own reading gives the
    same time and offset as ``fromisoformat`` for every string, and no time where
    it raises. Before 3.11, where ``fromisoformat`` reads only its own output, the
    script reads every string it reads, with the same time and offset."""
    by_hand = _load_har_facts()._iso_by_hand
    reads_only_its_own_output = sys.version_info < (3, 11)
    read = 0
    for text in _corpus():
        expected = _fromisoformat(text)
        if expected is None and reads_only_its_own_output:
            continue
        assert _same(by_hand(text), expected), ascii(text)
        read += expected is not None
    assert read > (1_000 if reads_only_its_own_output else 20_000)


def test_the_har_reader_falls_back_only_before_3_11() -> None:
    """From 3.11 on the script reads with the running Python's reader, as the
    digest does; before 3.11 it reads what 3.11 reads."""
    facts = _load_har_facts()
    for text in _corpus():
        expected = facts._iso_by_hand(text) if sys.version_info < (3, 11) else _fromisoformat(text)
        assert _same(facts._iso_datetime(text), expected), ascii(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-31t12:00:00.12Z", "2026-10-31T12:00:00.120000+00:00"),
        ("1970-01-01_00:00:00.12Z", "1970-01-01T00:00:00.120000+00:00"),
        ("1970-01-01T00:00.5Z", "1970-01-01T00:00:00.500000+00:00"),
        ("1970-01-01T00.5Z", "1970-01-01T00:00:00.500000+00:00"),
        ("1970-01-01T00:00:00+05:30:15.5", "1970-01-01T00:00:00+05:30:15.500000"),
        ("1970-01-01t00:00:00+0000", "1970-01-01T00:00:00+00:00"),
        ("2099-W01-1", "2098-12-29T00:00:00"),
        ("2026W446T12", "2026-10-31T12:00:00"),
        ("1970-01-01é00", "1970-01-01T00:00:00"),
        ("1970-01-01T00,+00", "1970-01-01T00:00:00+00:00"),
        ("1970-01-01T", None),
        ("1970-01-01T24:00:00", None),
        ("2099-001", None),
        ("1970-01-01T00:00:00+05:30:15.", None),
    ],
)
def test_the_har_reader_reads_these_as_3_11_does(text: str, expected: str | None) -> None:
    """Shapes the review found read ``unknown`` on 3.9 and 3.10, and some 3.11
    rejects, pinned so a run on any Python checks them."""
    got = _load_har_facts()._iso_by_hand(text)
    assert (None if got is None else got.isoformat()) == expected
