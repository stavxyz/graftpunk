"""CPU timing for the cost-ratio tests."""

from __future__ import annotations

import gc
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager


@contextmanager
def gc_paused() -> Iterator[None]:
    """The cyclic collector off inside the block and back as it was afterwards, so a
    collection pause cannot land inside a timed region."""
    collecting = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if collecting:
            gc.enable()


def best_cpu_seconds(work: Callable[[], object], runs: int = 3) -> float:
    """The least ``time.process_time()`` of *runs* calls of *work*, with the cyclic
    collector off for the measurement and back as it was afterwards.

    ``process_time`` counts a generation-2 collection that lands inside a run (up to
    0.57 s in a full ``-n 4`` run), which is not the cost of *work* and swamps a
    ratio between two sizes. The least of several runs drops the slices another
    xdist worker's load adds.
    """
    best = float("inf")
    with gc_paused():
        for _ in range(runs):
            start = time.process_time()
            work()
            best = min(best, time.process_time() - start)
    return best
