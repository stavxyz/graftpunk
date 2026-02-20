"""Tests for three-level format override mechanism.

Verifies the resolution order: per-command > plugin-wide > core.
"""

from unittest.mock import MagicMock

import pytest
from rich.console import Console

from graftpunk.plugins.cli_plugin import CommandResult
from graftpunk.plugins.formatters import OutputFormatter, format_output


class MockFormatter:
    """Test formatter that records calls."""

    binary = False

    def __init__(self, name_val: str) -> None:
        self._name = name_val
        self.called = False
        self.call_args: tuple | None = None

    @property
    def name(self) -> str:
        return self._name

    def format(
        self,
        data: object,
        console: Console,
        output_config: object = None,
        output_path: str = "",
    ) -> None:
        self.called = True
        self.call_args = (data, console, output_config, output_path)


class TestMockFormatterProtocol:
    """Verify MockFormatter satisfies OutputFormatter protocol."""

    def test_mock_formatter_is_output_formatter(self) -> None:
        assert isinstance(MockFormatter("test"), OutputFormatter)


class TestFormatOverrides:
    """Tests for the three-level format override resolution."""

    def test_core_formatter_used_when_no_overrides(self) -> None:
        """Core json formatter used by default."""
        console = MagicMock(spec=Console)
        # Should not raise — core json formatter handles it
        format_output({"key": "value"}, "json", console)

    def test_plugin_wide_override_replaces_core(self) -> None:
        """Plugin-wide formatter takes precedence over core."""
        mock_fmt = MockFormatter("json")
        console = MagicMock(spec=Console)
        format_output(
            {"key": "value"},
            "json",
            console,
            plugin_formatters={"json": mock_fmt},
        )
        assert mock_fmt.called

    def test_per_command_override_replaces_plugin_wide(self) -> None:
        """Per-command formatter takes precedence over plugin-wide."""
        plugin_fmt = MockFormatter("json")
        command_fmt = MockFormatter("json")
        result = CommandResult(
            data={"key": "value"},
            format_overrides={"json": command_fmt},
        )
        console = MagicMock(spec=Console)
        format_output(
            result,
            "json",
            console,
            plugin_formatters={"json": plugin_fmt},
        )
        assert command_fmt.called
        assert not plugin_fmt.called

    def test_per_command_override_replaces_core(self) -> None:
        """Per-command formatter takes precedence over core."""
        command_fmt = MockFormatter("json")
        result = CommandResult(
            data={"key": "value"},
            format_overrides={"json": command_fmt},
        )
        console = MagicMock(spec=Console)
        format_output(result, "json", console)
        assert command_fmt.called

    def test_per_command_can_add_new_format(self) -> None:
        """Per-command override can register a format unknown to core."""
        custom_fmt = MockFormatter("bekentree")
        result = CommandResult(
            data="csv content",
            format_overrides={"bekentree": custom_fmt},
        )
        console = MagicMock(spec=Console)
        format_output(result, "bekentree", console)
        assert custom_fmt.called

    def test_plugin_wide_can_add_new_format(self) -> None:
        """Plugin-wide override can register a format unknown to core."""
        custom_fmt = MockFormatter("custom-report")
        console = MagicMock(spec=Console)
        format_output(
            {"key": "value"},
            "custom-report",
            console,
            plugin_formatters={"custom-report": custom_fmt},
        )
        assert custom_fmt.called

    def test_unknown_format_raises_value_error(self) -> None:
        """Unknown format with no override raises ValueError."""
        console = MagicMock(spec=Console)
        with pytest.raises(ValueError, match="Unknown output format"):
            format_output({"key": "value"}, "nonexistent_format", console)

    def test_format_hint_works_with_per_command_overrides(self) -> None:
        """format_hint can reference a per-command override format."""
        custom_fmt = MockFormatter("custom")
        result = CommandResult(
            data={"key": "value"},
            format_hint=None,
            format_overrides={"custom": custom_fmt},
        )
        console = MagicMock(spec=Console)
        format_output(result, "custom", console)
        assert custom_fmt.called

    def test_format_hint_uses_overridden_formatter(self) -> None:
        """format_hint 'json' uses overridden json formatter, not core."""
        overridden_json = MockFormatter("json")
        result = CommandResult(
            data={"key": "value"},
            format_hint="json",
            format_overrides={"json": overridden_json},
        )
        console = MagicMock(spec=Console)
        # user_explicit=False so format_hint applies
        format_output(result, "table", console)
        assert overridden_json.called

    def test_plugin_formatters_none_is_safe(self) -> None:
        """Passing plugin_formatters=None does not break anything."""
        console = MagicMock(spec=Console)
        format_output({"key": "value"}, "json", console, plugin_formatters=None)

    def test_command_result_format_overrides_none_is_safe(self) -> None:
        """CommandResult with format_overrides=None works fine."""
        result = CommandResult(data={"key": "value"}, format_overrides=None)
        console = MagicMock(spec=Console)
        format_output(result, "json", console)

    def test_empty_plugin_formatters_is_safe(self) -> None:
        """Passing plugin_formatters={} does not break anything."""
        console = MagicMock(spec=Console)
        format_output({"key": "value"}, "json", console, plugin_formatters={})

    def test_empty_command_format_overrides_is_safe(self) -> None:
        """CommandResult with format_overrides={} works fine."""
        result = CommandResult(data={"key": "value"}, format_overrides={})
        console = MagicMock(spec=Console)
        format_output(result, "json", console)

    def test_override_receives_unwrapped_data(self) -> None:
        """Override formatter receives unwrapped data (not CommandResult)."""
        custom_fmt = MockFormatter("json")
        inner_data = {"key": "value"}
        result = CommandResult(
            data=inner_data,
            format_overrides={"json": custom_fmt},
        )
        console = MagicMock(spec=Console)
        format_output(result, "json", console)
        assert custom_fmt.call_args is not None
        assert custom_fmt.call_args[0] == inner_data

    def test_user_explicit_overrides_format_hint(self) -> None:
        """When user_explicit=True, format_hint is ignored even with overrides."""
        hint_fmt = MockFormatter("table")
        requested_fmt = MockFormatter("json")
        result = CommandResult(
            data={"key": "value"},
            format_hint="table",
            format_overrides={"table": hint_fmt, "json": requested_fmt},
        )
        console = MagicMock(spec=Console)
        format_output(result, "json", console, user_explicit=True)
        # User explicitly requested json, so table hint is ignored
        assert requested_fmt.called
        assert not hint_fmt.called

    def test_three_levels_all_present(self) -> None:
        """With all three levels providing 'json', per-command wins."""
        plugin_fmt = MockFormatter("json")
        command_fmt = MockFormatter("json")
        result = CommandResult(
            data={"key": "value"},
            format_overrides={"json": command_fmt},
        )
        console = MagicMock(spec=Console)
        format_output(
            result,
            "json",
            console,
            plugin_formatters={"json": plugin_fmt},
        )
        assert command_fmt.called
        assert not plugin_fmt.called


class TestCommandResultFormatOverridesField:
    """Tests for the format_overrides field on CommandResult."""

    def test_default_is_none(self) -> None:
        result = CommandResult(data="test")
        assert result.format_overrides is None

    def test_can_set_overrides(self) -> None:
        fmt = MockFormatter("custom")
        result = CommandResult(data="test", format_overrides={"custom": fmt})
        assert result.format_overrides == {"custom": fmt}

    def test_frozen_dataclass(self) -> None:
        """CommandResult is frozen — cannot reassign format_overrides."""
        result = CommandResult(data="test", format_overrides={"a": 1})
        with pytest.raises(AttributeError):
            result.format_overrides = {}  # type: ignore[misc]
