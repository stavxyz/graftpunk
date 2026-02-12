"""Shared test helpers for unit tests."""

from typing import Any


def close_coro_and_return(return_value: Any = None) -> Any:
    """Create a side_effect function that closes coroutines to avoid RuntimeWarnings.

    When mocking asyncio.run(), the coroutine passed to it is created but never
    awaited. This creates RuntimeWarnings about unawaited coroutines. This helper
    creates a side_effect that properly closes the coroutine before returning.

    Args:
        return_value: Value to return from the mock.

    Returns:
        A function suitable for use as side_effect on a Mock.
    """

    def _side_effect(coro: Any) -> Any:
        # Close the coroutine to prevent "never awaited" warnings
        if hasattr(coro, "close"):
            coro.close()
        return return_value

    return _side_effect


def close_coro_and_raise(exc: Exception) -> Any:
    """Create a side_effect function that closes coroutines then raises an exception.

    Similar to close_coro_and_return, but raises an exception after closing
    the coroutine. Used for tests that verify error handling paths.

    Args:
        exc: Exception instance to raise.

    Returns:
        A function suitable for use as side_effect on a Mock.
    """

    def _side_effect(coro: Any) -> Any:
        if hasattr(coro, "close"):
            coro.close()
        raise exc

    return _side_effect
