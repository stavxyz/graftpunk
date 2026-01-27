"""Tests for NoDriverBackend property accessors and cookie operations."""

from unittest.mock import MagicMock, patch

from graftpunk.backends.nodriver import NoDriverBackend


class TestNoDriverBackendProperties:
    """Tests for NoDriverBackend property accessors."""

    def test_current_url_empty_when_not_running(self) -> None:
        """current_url returns empty string when not running."""
        backend = NoDriverBackend()
        assert backend.current_url == ""

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_current_url_returns_value_when_running(self, mock_run: MagicMock) -> None:
        """current_url returns actual URL when running."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()
        mock_run.return_value = "https://example.com/page"

        assert backend.current_url == "https://example.com/page"
        mock_run.assert_called()

    def test_page_title_empty_when_not_running(self) -> None:
        """page_title returns empty string when not running."""
        backend = NoDriverBackend()
        assert backend.page_title == ""

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_page_title_returns_value_when_running(self, mock_run: MagicMock) -> None:
        """page_title returns actual title when running."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()
        mock_run.return_value = "Example Page Title"

        assert backend.page_title == "Example Page Title"
        mock_run.assert_called()

    def test_page_source_empty_when_not_running(self) -> None:
        """page_source returns empty string when not running."""
        backend = NoDriverBackend()
        assert backend.page_source == ""

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_page_source_returns_value_when_running(self, mock_run: MagicMock) -> None:
        """page_source returns actual HTML when running."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()
        mock_run.return_value = "<html><body>Test</body></html>"

        assert backend.page_source == "<html><body>Test</body></html>"
        mock_run.assert_called()

    def test_get_user_agent_empty_when_not_running(self) -> None:
        """get_user_agent() returns empty string when not running."""
        backend = NoDriverBackend()
        assert backend.get_user_agent() == ""

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_get_user_agent_returns_value_when_running(self, mock_run: MagicMock) -> None:
        """get_user_agent() returns actual UA when running."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()
        mock_run.return_value = "Mozilla/5.0 Test Browser"

        assert backend.get_user_agent() == "Mozilla/5.0 Test Browser"
        mock_run.assert_called()


class TestNoDriverBackendCookies:
    """Tests for NoDriverBackend cookie operations."""

    def test_get_cookies_empty_when_not_running(self) -> None:
        """get_cookies() returns empty list when not running."""
        backend = NoDriverBackend()
        assert backend.get_cookies() == []

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_set_cookies_starts_if_not_running(self, mock_run: MagicMock) -> None:
        """set_cookies() starts browser if not running."""
        backend = NoDriverBackend()
        result = backend.set_cookies([{"name": "test", "value": "value"}])

        # Should have called start and then set_cookies
        assert mock_run.call_count >= 1
        # Returns count of cookies (all succeed when no error)
        assert result == 1

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_set_cookies_returns_count(self, mock_run: MagicMock) -> None:
        """set_cookies() returns number of cookies successfully set."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()

        result = backend.set_cookies(
            [
                {"name": "cookie1", "value": "val1"},
                {"name": "cookie2", "value": "val2"},
            ]
        )

        assert result == 2

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_set_cookies_returns_zero_on_failure(self, mock_run: MagicMock) -> None:
        """set_cookies() returns 0 when CDP call fails."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()
        mock_run.side_effect = RuntimeError("CDP error")

        result = backend.set_cookies([{"name": "test", "value": "value"}])

        assert result == 0

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_delete_all_cookies_returns_true_on_success(self, mock_run: MagicMock) -> None:
        """delete_all_cookies() returns True when successful."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()
        mock_run.return_value = True  # Async method returns True on success

        result = backend.delete_all_cookies()

        assert result is True
        mock_run.assert_called()

    @patch("graftpunk.backends.nodriver.asyncio.run")
    def test_delete_all_cookies_returns_false_on_failure(self, mock_run: MagicMock) -> None:
        """delete_all_cookies() returns False when CDP call fails."""
        backend = NoDriverBackend()
        backend._started = True
        backend._browser = MagicMock()
        backend._page = MagicMock()
        mock_run.side_effect = RuntimeError("CDP error")

        result = backend.delete_all_cookies()

        assert result is False

    def test_delete_all_cookies_returns_true_when_not_running(self) -> None:
        """delete_all_cookies() returns True when not running (no cookies to delete)."""
        backend = NoDriverBackend()
        result = backend.delete_all_cookies()
        assert result is True
