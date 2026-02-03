"""Tests for _run_browser_extraction sync/async context dispatch."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import requests

from graftpunk.tokens import Token


class TestRunBrowserExtraction:
    """Tests for _run_browser_extraction context dispatch."""

    def test_sync_context_uses_asyncio_run(self) -> None:
        """In sync context, _run_browser_extraction uses asyncio.run()."""
        from graftpunk.tokens import _run_browser_extraction

        session = requests.Session()
        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf="([^"]+)"',
            page_url="/app",
        )

        with (
            patch(
                "graftpunk.tokens._extract_tokens_browser", new_callable=AsyncMock
            ) as mock_extract,
            patch("graftpunk.logging.suppress_asyncio_noise") as mock_suppress,
        ):
            mock_extract.return_value = {"X-CSRF": "token123"}
            mock_suppress.return_value.__enter__ = MagicMock()
            mock_suppress.return_value.__exit__ = MagicMock(return_value=False)
            result = _run_browser_extraction(session, [token], "https://example.com")

        assert result == {"X-CSRF": "token123"}
        mock_suppress.assert_called_once()
        mock_extract.assert_awaited_once_with(session, [token], "https://example.com")

    def test_async_context_uses_thread_pool(self) -> None:
        """In async context, _run_browser_extraction uses ThreadPoolExecutor."""
        from graftpunk.tokens import _run_browser_extraction

        session = requests.Session()
        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf="([^"]+)"',
            page_url="/app",
        )

        async def run_in_async():
            with (
                patch(
                    "graftpunk.tokens._extract_tokens_browser",
                    new_callable=AsyncMock,
                ) as mock_extract,
                patch("graftpunk.tokens.asyncio.get_running_loop") as mock_loop,
            ):
                mock_extract.return_value = {"X-CSRF": "token123"}
                mock_loop.return_value = asyncio.get_event_loop()
                result = _run_browser_extraction(session, [token], "https://example.com")
            return result

        result = asyncio.run(run_in_async())
        assert result == {"X-CSRF": "token123"}
