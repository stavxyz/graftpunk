"""Tests for the console output module."""

import io

from rich.console import Console

from graftpunk.console import (
    error,
    info,
    success,
    warn,
)


class TestConsoleHelpers:
    """Tests for console helper functions."""

    def test_success_outputs_green_check(self) -> None:
        """Test success prints green checkmark to stderr."""
        buf = io.StringIO()
        test_console = Console(file=buf, stderr=True, no_color=True)
        success("Done", console=test_console)
        output = buf.getvalue()
        assert "Done" in output

    def test_error_outputs_red_x(self) -> None:
        """Test error prints red X to stderr."""
        buf = io.StringIO()
        test_console = Console(file=buf, stderr=True, no_color=True)
        error("Failed", console=test_console)
        output = buf.getvalue()
        assert "Failed" in output

    def test_warn_outputs_yellow(self) -> None:
        """Test warn prints yellow message to stderr."""
        buf = io.StringIO()
        test_console = Console(file=buf, stderr=True, no_color=True)
        warn("Careful", console=test_console)
        output = buf.getvalue()
        assert "Careful" in output

    def test_info_outputs_dim(self) -> None:
        """Test info prints dim message to stderr."""
        buf = io.StringIO()
        test_console = Console(file=buf, stderr=True, no_color=True)
        info("Note", console=test_console)
        output = buf.getvalue()
        assert "Note" in output
