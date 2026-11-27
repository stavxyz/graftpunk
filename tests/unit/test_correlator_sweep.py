"""The correlator sweep's comparison: what it counts as worse, fewer wrong, and gained."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "correlator_sweep.py"
_spec = importlib.util.spec_from_file_location("correlator_sweep", _SCRIPT)
assert _spec is not None and _spec.loader is not None
sweep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweep)


def _snap(entries: list[str], roles: dict[str, list[str]]) -> dict[str, Any]:
    return {"entries": entries, "roles": roles}


def _run(phase_pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    base: dict[str, Any] = {}
    head: dict[str, Any] = {}
    for i, (a, b) in enumerate(phase_pairs):
        key = f"chain | order {i}"
        base[key] = {"mid": a, "final": a}
        head[key] = {"mid": b, "final": b}
    return sweep.compare(base, head)["chain"]


def test_wrong_counts_entries_holding_another_requests_headers():
    assert sweep.wrong(_snap(["A<-A", "B<-None", "C<-A"], {})) == 1


def test_a_role_the_ref_never_built_is_gained_not_fewer_wrong():
    ref = _snap(["A<-B", "B<-A"], {})
    checkout = _snap(["A<-B", "B<-A"], {"navigation": ["User-Agent"]})
    counts = _run([(ref, checkout)])
    assert counts["gained_role"] == 2
    assert counts["fewer_wrong"] == 0
    assert counts["worse"] == 0
    assert counts["base_wrong"] == counts["head_wrong"] == 2


def test_fewer_wrong_attributions_are_counted_as_such():
    ref = _snap(["A<-B", "B<-A"], {"form": ["Accept"]})
    checkout = _snap(["A<-A", "B<-B"], {"form": ["Accept"]})
    counts = _run([(ref, checkout)])
    assert counts["fewer_wrong"] == 2
    assert counts["gained_role"] == 0
    assert counts["worse"] == 0


def test_more_wrong_attributions_are_worse_with_an_example():
    ref = _snap(["A<-A", "B<-B"], {"form": ["Accept"]})
    checkout = _snap(["A<-B", "B<-A"], {"form": ["Accept"]})
    counts = _run([(ref, checkout)])
    assert counts["worse"] == 2
    assert counts["fewer_wrong"] == 0
    assert len(counts["examples"]) == 2


def test_a_lost_role_where_the_ref_attributed_rightly_is_worse():
    ref = _snap(["A<-A"], {"form": ["Accept"]})
    checkout = _snap(["A<-A"], {})
    counts = _run([(ref, checkout)])
    assert counts["worse"] == 2
    assert counts["dropped_misattributed"] == 0


def test_dropping_a_role_the_ref_built_from_wrong_headers_is_counted_apart():
    ref = _snap(["A<-B"], {"form": ["Accept"]})
    checkout = _snap(["A<-None"], {})
    counts = _run([(ref, checkout)])
    assert counts["dropped_misattributed"] == 2
    assert counts["worse"] == 0


def test_a_lost_role_is_not_counted_as_gained():
    ref = _snap(["A<-A"], {"form": ["Accept"], "navigation": ["Accept"]})
    checkout = _snap(["A<-A"], {"navigation": ["Accept"]})
    counts = _run([(ref, checkout)])
    assert counts["gained_role"] == 0
    assert counts["worse"] == 2


def test_a_role_missing_a_header_the_ref_had_is_worse():
    ref = _snap(["A<-A"], {"form": ["Accept", "Content-Type"]})
    checkout = _snap(["A<-A"], {"form": ["Accept"]})
    counts = _run([(ref, checkout)])
    assert counts["worse"] == 2
    assert counts["dropped_misattributed"] == 0


def test_equal_wrongness_with_a_dropped_role_is_counted_apart():
    ref = _snap(["A<-B", "B<-A"], {"form": ["Accept"]})
    checkout = _snap(["A<-B", "B<-A"], {})
    counts = _run([(ref, checkout)])
    assert counts["dropped_misattributed"] == 2
    assert counts["worse"] == 0


def test_base_and_head_wrong_count_their_own_side():
    ref = _snap(["A<-B", "B<-A"], {"form": ["Accept"]})
    checkout = _snap(["A<-A", "B<-B"], {"form": ["Accept"]})
    counts = _run([(ref, checkout)])
    assert counts["base_wrong"] == 2
    assert counts["head_wrong"] == 0


def test_at_most_three_worse_examples_are_kept():
    ref = _snap(["A<-A", "B<-B"], {"form": ["Accept"]})
    checkout = _snap(["A<-B", "B<-A"], {"form": ["Accept"]})
    counts = _run([(ref, checkout), (ref, checkout)])
    assert counts["worse"] == 4
    assert len(counts["examples"]) == 3


def test_equal_wrongness_with_the_same_roles_counts_nothing():
    ref = _snap(["A<-B"], {"form": ["Accept"]})
    counts = _run([(ref, ref)])
    assert counts["orders"] == 2
    assert counts["dropped_misattributed"] == 0
    assert counts["worse"] == 0


def test_a_header_dropped_from_a_role_the_ref_built_wrongly_is_counted_apart():
    ref = _snap(["A<-B"], {"form": ["Accept", "Content-Type"]})
    checkout = _snap(["A<-B"], {"form": ["Accept"]})
    counts = _run([(ref, checkout)])
    assert counts["dropped_misattributed"] == 2
    assert counts["worse"] == 0


def test_more_wrong_with_a_dropped_role_is_worse():
    ref = _snap(["A<-B", "B<-B"], {"form": ["Accept"]})
    checkout = _snap(["A<-B", "B<-A"], {})
    counts = _run([(ref, checkout)])
    assert counts["worse"] == 2
    assert counts["dropped_misattributed"] == 0


def test_two_wrong_entries_down_to_one_is_fewer_wrong():
    ref = _snap(["A<-B", "B<-A"], {"form": ["Accept"]})
    checkout = _snap(["A<-B", "B<-B"], {"form": ["Accept"]})
    counts = _run([(ref, checkout)])
    assert counts["fewer_wrong"] == 2
    assert counts["base_wrong"] == counts["head_wrong"] == 2
    assert counts["worse"] == 0


def test_a_worse_example_shows_both_sides():
    ref = _snap(["A<-A", "B<-B"], {"form": ["Accept"]})
    checkout = _snap(["A<-B", "B<-A"], {"form": ["Accept"]})
    counts = _run([(ref, checkout)])
    assert counts["examples"][0] == "mid chain | order 0: ['A<-A', 'B<-B'] -> ['A<-B', 'B<-A']"
