"""The id rules measured, each in the position it guards.

Names (keys, headers, input, cookie, and token names) are dropped only on strong
evidence (``holds_an_id``): the key-position id table in ``test_har_paths.py`` must
be caught in full, every sub-rule of the name rule must be the only catch of one of
its entries, and the share of ordinary names read as ids is held under a ceiling
on three corpora measured apart: the regression corpus of names reviewers raised
(``field_names_regression.py``), the round-7 corpus of public SDK and API names
the round-7b thresholds were set against (``field_names_round7.py``), and a fresh
corpus of field, header, and cookie names written in round 8 and not tuned
against (``field_names_fresh.py``). A response map is also keyed by ids when 3 or
more keys share one length of 12 or more and each mix letters and digits
(``paths.keys_are_ids``); no group of same-length corpus names triggers it.

Path segments fail closed (``looks_dynamic``), so random tokens are covered there:
a ceiling per random-token shape, set just above the rate measured when the rule
last changed. A token of letters alone (no digit) is literal by any lexical rule,
so no shape's floor is zero; for 8-character lower-case base36 that floor is
(26/36)**8, about 7.4%.

The name rule's own random-token miss rates are measured and reported, with no
ceiling (2026-09-23, round 7b): lower36_8 0.99975, lower36_12 0.998, upper36_10
0.998, token_urlsafe_16 0.95175, nanoid_21 0.95025. A short random token used as
a name is kept; that is the name rule's known limit.
"""

from __future__ import annotations

import random
import string

import pytest

from graftpunk.har import paths
from graftpunk.har.paths import holds_an_id, keys_are_ids, looks_dynamic
from tests.unit.field_names_fresh import FRESH_NAMES
from tests.unit.field_names_regression import REGRESSION_NAMES
from tests.unit.field_names_round7 import ROUND7_NAMES
from tests.unit.test_har_paths import KEY_POSITION_IDS

_SEED = 20260923
_COUNT = 4000
_LOWER36 = string.ascii_lowercase + string.digits
_UPPER36 = string.ascii_uppercase + string.digits
_URLSAFE = string.ascii_letters + string.digits + "-_"

# shape: (alphabet, length, path miss-rate ceiling). Measured at these seeds when
# the path rule last changed (2026-09-23, round 8): lower36_8 0.08175, lower36_12
# 0.01625, upper36_10 0.036, token_urlsafe_16 0.026, nanoid_21 0.03. Each
# ceiling is the round-7b rate plus 0.001 (four tokens).
_SHAPES = {
    "lower36_8": (_LOWER36, 8, 0.08275),
    "lower36_12": (_LOWER36, 12, 0.01725),
    "upper36_10": (_UPPER36, 10, 0.037),
    "token_urlsafe_16": (_URLSAFE, 22, 0.027),
    "nanoid_21": (_URLSAFE, 21, 0.03125),
}

# The share of each corpus read as ids, measured 2026-09-23 (round 8): 0 of 482
# regression names, 0 of 259 round-7 names, and 0 of 229 fresh names. The round-7
# ceiling is round 7b's 1%; the regression ceiling is unchanged from round 6; the
# fresh ceiling is its measured rate plus one name.
_REGRESSION_CEILING = 0.004
_ROUND7_CEILING = 0.01
_FRESH_CEILING = 0.005
_CORPORA = [
    ("regression", REGRESSION_NAMES, 300, _REGRESSION_CEILING),
    ("round7", ROUND7_NAMES, 200, _ROUND7_CEILING),
    ("fresh", FRESH_NAMES, 150, _FRESH_CEILING),
]


def _tokens(shape: str) -> list[str]:
    alphabet, length, _ceiling = _SHAPES[shape]
    rng = random.Random(f"{_SEED}-{shape}")  # noqa: S311 - a fixed seed, not a secret
    return ["".join(rng.choice(alphabet) for _ in range(length)) for _ in range(_COUNT)]


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_random_tokens_in_a_path_are_dynamic_at_or_under_the_ceiling(shape: str) -> None:
    missed = sum(1 for token in _tokens(shape) if not looks_dynamic(token)) / _COUNT
    assert missed <= _SHAPES[shape][2], (shape, missed)


_RULES = [name for name, _rule in paths._NAME_ID_RULES]


@pytest.mark.parametrize("rule", _RULES)
def test_every_name_sub_rule_is_the_only_catch_of_a_key_position_id(
    monkeypatch: pytest.MonkeyPatch, rule: str
) -> None:
    rules = paths._NAME_ID_RULES
    monkeypatch.setattr(paths, "_NAME_ID_RULES", tuple(r for r in rules if r[0] != rule))
    missed = [value for value in KEY_POSITION_IDS if not holds_an_id(value)]
    assert missed, f"{rule} is the only catch of no key-position id: delete it"


@pytest.mark.parametrize(("label", "corpus", "minimum", "ceiling"), _CORPORA)
def test_each_corpus_is_large_and_unique(
    label: str, corpus: tuple[str, ...], minimum: int, ceiling: float
) -> None:
    assert len(corpus) >= minimum, label
    assert len(set(corpus)) == len(corpus), label


def test_the_corpora_do_not_overlap() -> None:
    sets = [set(corpus) for _label, corpus, _minimum, _ceiling in _CORPORA]
    for index, first in enumerate(sets):
        for second in sets[index + 1 :]:
            assert not first & second


@pytest.mark.parametrize(("label", "corpus", "minimum", "ceiling"), _CORPORA)
def test_ordinary_names_are_ids_at_or_under_the_ceiling(
    label: str, corpus: tuple[str, ...], minimum: int, ceiling: float
) -> None:
    read_as_ids = [name for name in corpus if holds_an_id(name)]
    assert len(read_as_ids) / len(corpus) <= ceiling, (label, read_as_ids)


@pytest.mark.parametrize(("label", "corpus", "minimum", "ceiling"), _CORPORA)
def test_no_group_of_corpus_names_reads_as_an_id_keyed_map(
    label: str, corpus: tuple[str, ...], minimum: int, ceiling: float
) -> None:
    by_length: dict[int, list[str]] = {}
    for name in corpus:
        by_length.setdefault(len(name), []).append(name)
    assert not [group for group in by_length.values() if keys_are_ids(group)], label
