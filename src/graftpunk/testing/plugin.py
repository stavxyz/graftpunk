"""The pytest half of :mod:`graftpunk.testing`. Load only as a pytest plugin.

A generated ``tests/conftest.py`` declares
``pytest_plugins = ["graftpunk.testing.plugin"]`` and nothing else:
everything the generated test suite needs from this module is a fixture, so
the generated file carries no framework logic of its own to drift from
graftpunk's (the scaffold rule, plugin tooling spec, 2026-09-11).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

__all__ = ["site_env_scrubber"]


def site_env_scrubber(prefix: str) -> Any:
    """An autouse fixture that removes every env var starting with *prefix*
    for the duration of each test, and restores them afterward.

    A generated plugin's ``tests/conftest.py`` sets
    ``scrub_site_env = site_env_scrubber("<NAME>_")`` so a developer's own
    ``MYSHOP_USERNAME``/``MYSHOP_PASSWORD`` in the ambient environment never
    leaks into a test asserting on the plugin's behaviour without them.
    """

    @pytest.fixture(autouse=True)
    def _scrub() -> Iterator[None]:
        saved = {k: v for k, v in os.environ.items() if k.startswith(prefix)}
        for key in saved:
            del os.environ[key]
        try:
            yield
        finally:
            os.environ.update(saved)

    return _scrub
