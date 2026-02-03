"""Tests for graftpunk logging utilities."""

from __future__ import annotations

import logging

import pytest

from graftpunk.logging import suppress_asyncio_noise


class TestSuppressAsyncioNoise:
    """Tests for suppress_asyncio_noise context manager."""

    def test_suppresses_asyncio_warnings(self) -> None:
        asyncio_logger = logging.getLogger("asyncio")
        original_level = asyncio_logger.level

        with suppress_asyncio_noise():
            assert asyncio_logger.level == logging.CRITICAL

        assert asyncio_logger.level == original_level

    def test_restores_level_on_exception(self) -> None:
        asyncio_logger = logging.getLogger("asyncio")
        original_level = asyncio_logger.level

        with pytest.raises(RuntimeError), suppress_asyncio_noise():
            raise RuntimeError("test")

        assert asyncio_logger.level == original_level

    def test_restores_custom_level(self) -> None:
        asyncio_logger = logging.getLogger("asyncio")
        asyncio_logger.setLevel(logging.DEBUG)

        with suppress_asyncio_noise():
            assert asyncio_logger.level == logging.CRITICAL

        assert asyncio_logger.level == logging.DEBUG
        # Reset
        asyncio_logger.setLevel(logging.WARNING)
