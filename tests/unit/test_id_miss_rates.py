"""The id rule measured in both directions: how often it misses a random token,
by shape, on a fixed seed; and how often it reads an ordinary field name as an id,
over a fixed corpus (``field_name_corpus.py``).

A ceiling per shape, set just above the rate measured when the rule last
changed: a change that lets more random tokens through fails here. A token of
letters alone (no digit) reads as a word by any lexical rule, so no shape's
floor is zero; for 8-character lower-case base36 that floor is (26/36)**8, about
7.4%.
"""

from __future__ import annotations

import random
import string

import pytest

from graftpunk.har.paths import holds_an_id
from tests.unit.field_name_corpus import FIELD_NAMES

_SEED = 20260923
_COUNT = 4000
_LOWER36 = string.ascii_lowercase + string.digits
_UPPER36 = string.ascii_uppercase + string.digits
_URLSAFE = string.ascii_letters + string.digits + "-_"

# shape: (alphabet, length, miss-rate ceiling). Measured at these seeds when the
# rule last changed (2026-09-23): 0.0765, 0.0165, 0.036, 0.026, 0.02875.
_SHAPES = {
    "lower36_8": (_LOWER36, 8, 0.080),
    "lower36_12": (_LOWER36, 12, 0.020),
    "upper36_10": (_UPPER36, 10, 0.040),
    "token_urlsafe_16": (_URLSAFE, 22, 0.030),
    "nanoid_21": (_URLSAFE, 21, 0.030),
}


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_random_tokens_are_ids_at_or_under_the_ceiling(shape: str) -> None:
    alphabet, length, ceiling = _SHAPES[shape]
    rng = random.Random(f"{_SEED}-{shape}")  # noqa: S311 - a fixed seed, not a secret
    tokens = ["".join(rng.choice(alphabet) for _ in range(length)) for _ in range(_COUNT)]
    missed = sum(1 for token in tokens if not holds_an_id(token))
    assert missed / _COUNT <= ceiling, (shape, missed / _COUNT)


# The share of the corpus read as ids, set just above the rate measured when the
# rule last changed (2026-09-23: 1 of 483, add2cart, an acronym-like 3-letter run
# before a digit and a lower-case word, which the rule reads as random).
_FALSE_POSITIVE_CEILING = 0.004


def test_the_corpus_is_large_and_unique() -> None:
    assert len(FIELD_NAMES) >= 300
    assert len(set(FIELD_NAMES)) == len(FIELD_NAMES)


def test_ordinary_field_names_are_ids_at_or_under_the_ceiling() -> None:
    read_as_ids = [name for name in FIELD_NAMES if holds_an_id(name)]
    assert len(read_as_ids) / len(FIELD_NAMES) <= _FALSE_POSITIVE_CEILING, read_as_ids
