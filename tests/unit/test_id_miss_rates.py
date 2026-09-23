"""The id rule measured in both directions: how often it misses a random token,
by shape, on a fixed seed; and how often it reads an ordinary name as an id, over
two fixed corpora measured apart: the regression corpus the rule was tuned
against (``field_names_regression.py``) and a held-out corpus of public SDK and
API names it was not (``field_names_held_out.py``).

A ceiling per shape and per corpus, set just above the rate measured when the rule
last changed: a change that lets more random tokens through, or reads more
ordinary names as ids, fails here. A token of letters alone (no digit) reads as a
word by any lexical rule, so no shape's floor is zero; for 8-character lower-case
base36 that floor is (26/36)**8, about 7.4%.

Every sub-rule in the word tables (``_SPELLING_REJECTS``, ``_WORD_REJECTS``) and
the joined-name short-run limit must pay for itself: removing it breaches at least
one miss-rate ceiling. A rule that does not is deleted.
"""

from __future__ import annotations

import random
import string

import pytest

from graftpunk.har import paths
from graftpunk.har.paths import holds_an_id
from tests.unit.field_names_held_out import HELD_OUT_NAMES
from tests.unit.field_names_regression import REGRESSION_NAMES

_SEED = 20260923
_COUNT = 4000
_LOWER36 = string.ascii_lowercase + string.digits
_UPPER36 = string.ascii_uppercase + string.digits
_URLSAFE = string.ascii_letters + string.digits + "-_"

# shape: (alphabet, length, miss-rate ceiling). Measured at these seeds when the
# rule last changed (2026-09-23, round 7): lower36_8 0.083, lower36_12 0.023,
# upper36_10 0.036, token_urlsafe_16 0.02675, nanoid_21 0.0295. Each ceiling is
# the measured rate plus 0.001 (four tokens).
_SHAPES = {
    "lower36_8": (_LOWER36, 8, 0.084),
    "lower36_12": (_LOWER36, 12, 0.024),
    "upper36_10": (_UPPER36, 10, 0.037),
    "token_urlsafe_16": (_URLSAFE, 22, 0.02775),
    "nanoid_21": (_URLSAFE, 21, 0.0305),
}

# The share of each corpus read as ids, set just above the rate measured when the
# rule last changed (2026-09-23, round 7). Regression: 1 of 482 (add2cart). Held
# out: 19 of 259, in five classes: lower-case letters right after a digit
# (retina2x, argon2id, k8sNamespace, p2pTransfer, l10nBundle, c6gXlarge, secp256k1);
# a run of 5 or more digits (ed25519, x25519); no run of 4 or more letters
# (sha256Key, aes128Gcm); a letter run the consonant-pair check refuses
# (pbkdf2Iterations); and a 20-or-more-character camel name with a digit read as a
# base64 token (Md5OfMessageAttributes).
_REGRESSION_CEILING = 0.004
_HELD_OUT_CEILING = 0.075


def _tokens(shape: str) -> list[str]:
    alphabet, length, _ceiling = _SHAPES[shape]
    rng = random.Random(f"{_SEED}-{shape}")  # noqa: S311 - a fixed seed, not a secret
    return ["".join(rng.choice(alphabet) for _ in range(length)) for _ in range(_COUNT)]


_TOKENS = {shape: _tokens(shape) for shape in _SHAPES}


def _miss_rate(shape: str) -> float:
    return sum(1 for token in _TOKENS[shape] if not holds_an_id(token)) / _COUNT


def _breached_shapes() -> list[str]:
    return [shape for shape in _SHAPES if _miss_rate(shape) > _SHAPES[shape][2]]


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_random_tokens_are_ids_at_or_under_the_ceiling(shape: str) -> None:
    assert _miss_rate(shape) <= _SHAPES[shape][2], (shape, _miss_rate(shape))


_SUB_RULES = [
    (table, name)
    for table in ("_SPELLING_REJECTS", "_WORD_REJECTS")
    for name, _ in getattr(paths, table)
]


@pytest.mark.parametrize(("table", "rule"), _SUB_RULES)
def test_every_sub_rule_breaches_a_ceiling_when_removed(
    monkeypatch: pytest.MonkeyPatch, table: str, rule: str
) -> None:
    rules = getattr(paths, table)
    monkeypatch.setattr(paths, table, tuple(entry for entry in rules if entry[0] != rule))
    assert _breached_shapes(), f"{rule} buys nothing on the miss-rate ceilings: delete it"


def test_the_joined_short_run_limit_breaches_a_ceiling_when_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(paths, "_MAX_JOINED_SHORT_RUNS", 99)
    assert _breached_shapes()


@pytest.mark.parametrize(
    ("corpus", "minimum"),
    [(REGRESSION_NAMES, 300), (HELD_OUT_NAMES, 200)],
    ids=["regression", "held_out"],
)
def test_each_corpus_is_large_and_unique(corpus: tuple[str, ...], minimum: int) -> None:
    assert len(corpus) >= minimum
    assert len(set(corpus)) == len(corpus)


def test_the_corpora_do_not_overlap() -> None:
    assert not set(REGRESSION_NAMES) & set(HELD_OUT_NAMES)


@pytest.mark.parametrize(
    ("corpus", "ceiling"),
    [(REGRESSION_NAMES, _REGRESSION_CEILING), (HELD_OUT_NAMES, _HELD_OUT_CEILING)],
    ids=["regression", "held_out"],
)
def test_ordinary_names_are_ids_at_or_under_the_ceiling(
    corpus: tuple[str, ...], ceiling: float
) -> None:
    read_as_ids = [name for name in corpus if holds_an_id(name)]
    assert len(read_as_ids) / len(corpus) <= ceiling, read_as_ids
