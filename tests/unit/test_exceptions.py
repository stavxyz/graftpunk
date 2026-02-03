"""Tests for graftpunk.exceptions module."""

from __future__ import annotations

import pytest

from graftpunk.exceptions import CommandError, GraftpunkError, PluginError


class TestCommandError:
    """Tests for CommandError exception."""

    def test_inherits_from_plugin_error(self) -> None:
        """CommandError is a subclass of PluginError."""
        assert issubclass(CommandError, PluginError)

    def test_inherits_from_graftpunk_error(self) -> None:
        """CommandError is a subclass of GraftpunkError."""
        assert issubclass(CommandError, GraftpunkError)

    def test_user_message_stored(self) -> None:
        """CommandError stores user_message attribute."""
        err = CommandError("Amount must be positive")
        assert err.user_message == "Amount must be positive"

    def test_str_representation(self) -> None:
        """CommandError string representation matches user_message."""
        err = CommandError("Invalid input")
        assert str(err) == "Invalid input"

    def test_catchable_as_plugin_error(self) -> None:
        """CommandError can be caught as PluginError."""
        with pytest.raises(PluginError):
            raise CommandError("test error")

    def test_catchable_as_command_error(self) -> None:
        """CommandError can be caught specifically."""
        with pytest.raises(CommandError) as exc_info:
            raise CommandError("specific error")
        assert exc_info.value.user_message == "specific error"

    def test_importable_from_plugins_package(self) -> None:
        """CommandError is importable from graftpunk.plugins."""
        from graftpunk.plugins import CommandError as PluginsCommandError

        assert PluginsCommandError is CommandError

    def test_in_plugins_all(self) -> None:
        """CommandError is listed in graftpunk.plugins.__all__."""
        import graftpunk.plugins

        assert "CommandError" in graftpunk.plugins.__all__
