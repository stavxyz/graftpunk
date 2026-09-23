"""How often the id rule misses a random token, by shape, on a fixed seed.

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
