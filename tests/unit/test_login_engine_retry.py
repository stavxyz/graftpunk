"""Tests for _select_with_retry and wait_for login engine features."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from graftpunk.exceptions import PluginError
from graftpunk.plugins.cli_plugin import LoginConfig, SitePlugin


class DeclarativeHN(SitePlugin):
    """Test plugin with declarative login (nodriver)."""

    site_name = "hn"
    session_name = "hackernews"
    help_text = "HN"
    base_url = "https://news.ycombinator.com"
    backend = "nodriver"
    login_config = LoginConfig(
        url="/login",
        fields={"username": "input[name='acct']", "password": "input[name='pw']"},
        submit="input[value='login']",
        failure="Bad login.",
    )


class DeclarativeWaitFor(SitePlugin):
    """Nodriver plugin with wait_for configured."""

    site_name = "waitfor"
    session_name = "waitfor"
    help_text = "WF"
    base_url = "https://example.com"
    backend = "nodriver"
    login_config = LoginConfig(
        url="/login",
        fields={"username": "#user"},
        submit="#btn",
        wait_for="#login-form",
    )


def _make_nodriver_mock_bs():
    """Create a mock BrowserSession that works as an async context manager."""
    mock_bs = MagicMock()
    instance = MagicMock()
    mock_bs.return_value = instance

    # Make it work as async context manager
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=False)

    return mock_bs, instance


class TestSelectWithRetry:
    """Tests for _select_with_retry helper."""

    @pytest.mark.asyncio
    async def test_returns_element_on_first_try(self) -> None:
        """Returns element immediately when select succeeds."""
        from graftpunk.plugins.login_engine import _select_with_retry

        mock_tab = AsyncMock()
        mock_element = AsyncMock()
        mock_tab.select = AsyncMock(return_value=mock_element)

        result = await _select_with_retry(mock_tab, "input#name")
        assert result is mock_element
        mock_tab.select.assert_called_once()

    @pytest.mark.asyncio
    async def test_retries_on_protocol_exception(self) -> None:
        """Retries when ProtocolException is raised, succeeds on later attempt."""
        from nodriver.core.connection import ProtocolException

        from graftpunk.plugins.login_engine import _select_with_retry

        mock_tab = AsyncMock()
        mock_element = AsyncMock()

        # Fail twice with ProtocolException, succeed on third
        exc = ProtocolException({"code": -32000, "message": "Could not find node"})
        mock_tab.select = AsyncMock(side_effect=[exc, exc, mock_element])

        result = await _select_with_retry(mock_tab, "input#name", timeout=10, interval=0.01)
        assert result is mock_element
        assert mock_tab.select.call_count == 3

    @pytest.mark.asyncio
    async def test_raises_after_timeout(self) -> None:
        """Raises last ProtocolException when timeout expires."""
        from nodriver.core.connection import ProtocolException

        from graftpunk.plugins.login_engine import _select_with_retry

        mock_tab = AsyncMock()
        exc = ProtocolException({"code": -32000, "message": "Could not find node"})
        mock_tab.select = AsyncMock(side_effect=exc)

        with pytest.raises(ProtocolException):
            await _select_with_retry(mock_tab, "input#name", timeout=0.1, interval=0.01)

    @pytest.mark.asyncio
    async def test_returns_none_when_select_returns_none(self) -> None:
        """Returns None when select returns None and timeout expires (no exception)."""
        from graftpunk.plugins.login_engine import _select_with_retry

        mock_tab = AsyncMock()
        mock_tab.select = AsyncMock(return_value=None)

        result = await _select_with_retry(mock_tab, "input#gone", timeout=0.1, interval=0.01)
        assert result is None

    @pytest.mark.asyncio
    async def test_non_protocol_exception_propagates(self) -> None:
        """Non-ProtocolException errors propagate immediately (no retry)."""
        from graftpunk.plugins.login_engine import _select_with_retry

        mock_tab = AsyncMock()
        mock_tab.select = AsyncMock(side_effect=RuntimeError("unexpected"))

        with pytest.raises(RuntimeError, match="unexpected"):
            await _select_with_retry(mock_tab, "input#name")

        # Only called once — no retry for non-protocol errors
        mock_tab.select.assert_called_once()

    @pytest.mark.asyncio
    async def test_zero_timeout_raises(self) -> None:
        """Raises ValueError when timeout is zero."""
        from graftpunk.plugins.login_engine import _select_with_retry

        with pytest.raises(ValueError, match="timeout must be positive"):
            await _select_with_retry(AsyncMock(), "input", timeout=0)

    @pytest.mark.asyncio
    async def test_negative_timeout_raises(self) -> None:
        """Raises ValueError when timeout is negative."""
        from graftpunk.plugins.login_engine import _select_with_retry

        with pytest.raises(ValueError, match="timeout must be positive"):
            await _select_with_retry(AsyncMock(), "input", timeout=-1)

    @pytest.mark.asyncio
    async def test_zero_interval_raises(self) -> None:
        """Raises ValueError when interval is zero."""
        from graftpunk.plugins.login_engine import _select_with_retry

        with pytest.raises(ValueError, match="interval must be positive"):
            await _select_with_retry(AsyncMock(), "input", interval=0)

    @pytest.mark.asyncio
    async def test_negative_interval_raises(self) -> None:
        """Raises ValueError when interval is negative."""
        from graftpunk.plugins.login_engine import _select_with_retry

        with pytest.raises(ValueError, match="interval must be positive"):
            await _select_with_retry(AsyncMock(), "input", interval=-1)

    @pytest.mark.asyncio
    async def test_per_attempt_timeout_capped(self) -> None:
        """Each select() attempt uses min(5.0, remaining) as timeout."""
        from graftpunk.plugins.login_engine import _select_with_retry

        mock_tab = AsyncMock()
        mock_element = AsyncMock()
        mock_tab.select = AsyncMock(return_value=mock_element)

        # With a 30s total timeout, the first per-attempt cap should be 5.0
        await _select_with_retry(mock_tab, "input#name", timeout=30, interval=1)

        # Verify select was called with timeout capped at 5.0, not 30
        call_kwargs = mock_tab.select.call_args
        assert call_kwargs[1]["timeout"] == pytest.approx(5.0, abs=0.5)

    @pytest.mark.asyncio
    async def test_none_then_element_on_retry(self) -> None:
        """Returns element when select returns None first, then succeeds."""
        from graftpunk.plugins.login_engine import _select_with_retry

        mock_tab = AsyncMock()
        mock_element = AsyncMock()
        mock_tab.select = AsyncMock(side_effect=[None, None, mock_element])

        result = await _select_with_retry(mock_tab, "input#name", timeout=10, interval=0.01)
        assert result is mock_element
        assert mock_tab.select.call_count == 3


class TestLoginRetryIntegration:
    """Tests for _select_with_retry integration in the login flow."""

    @pytest.mark.asyncio
    async def test_login_retries_through_protocol_exception(self) -> None:
        """Login succeeds when select fails transiently then recovers."""
        from nodriver.core.connection import ProtocolException

        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeHN()
        login_method = generate_login_method(plugin)

        mock_tab = AsyncMock()
        mock_element = AsyncMock()

        # select() fails with ProtocolException on first call, succeeds after
        exc = ProtocolException({"code": -32000, "message": "Could not find node"})
        mock_tab.select = AsyncMock(side_effect=[exc, mock_element, mock_element, mock_element])
        mock_tab.get_content = AsyncMock(return_value="<html>Welcome</html>")

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.cache_session"),
            patch("graftpunk.plugins.login_engine.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is True


class TestLoginWaitFor:
    """Tests for LoginConfig.wait_for integration."""

    @pytest.mark.asyncio
    async def test_wait_for_called_before_fields(self) -> None:
        """When wait_for is set, the selector is awaited before filling fields."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeWaitFor()
        login_method = generate_login_method(plugin)

        mock_tab = AsyncMock()
        mock_element = AsyncMock()
        select_calls: list[str] = []

        async def tracking_select(selector: str, **kwargs: object) -> AsyncMock:
            select_calls.append(selector)
            return mock_element

        mock_tab.select = tracking_select
        mock_tab.get_content = AsyncMock(return_value="<html>OK</html>")

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.cache_session"),
            patch("graftpunk.plugins.login_engine.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await login_method({"username": "user"})

        assert result is True
        # wait_for selector should be first
        assert select_calls[0] == "#login-form"
        # Then the field selector, then submit
        assert "#user" in select_calls
        assert "#btn" in select_calls

    @pytest.mark.asyncio
    async def test_wait_for_timeout_raises_plugin_error(self) -> None:
        """When wait_for times out, raises PluginError."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeWaitFor()
        login_method = generate_login_method(plugin)

        mock_tab = AsyncMock()
        # select always returns None (element never appears)
        mock_tab.select = AsyncMock(return_value=None)

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(PluginError, match="Timed out waiting for"),
        ):
            await login_method({"username": "user"})

    @pytest.mark.asyncio
    async def test_wait_for_protocol_exception_raises_plugin_error(self) -> None:
        """When wait_for encounters persistent ProtocolException, raises PluginError."""
        from nodriver.core.connection import ProtocolException

        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeWaitFor()
        login_method = generate_login_method(plugin)

        mock_tab = AsyncMock()
        exc = ProtocolException({"code": -32000, "message": "Could not find node"})
        mock_tab.select = AsyncMock(side_effect=exc)

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(PluginError, match="Timed out waiting for"),
        ):
            await login_method({"username": "user"})

    @pytest.mark.asyncio
    async def test_no_wait_for_skips_wait(self) -> None:
        """When wait_for is empty, no extra select call is made."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeHN()  # no wait_for
        login_method = generate_login_method(plugin)

        mock_tab = AsyncMock()
        mock_element = AsyncMock()
        mock_tab.select = AsyncMock(return_value=mock_element)
        mock_tab.get_content = AsyncMock(return_value="<html>Welcome</html>")

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.cache_session"),
            patch("graftpunk.plugins.login_engine.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is True
        # select should NOT have been called with wait_for selector
        for call in mock_tab.select.call_args_list:
            assert call[0][0] != "#login-form"
