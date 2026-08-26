"""Centralized terminal output for graftpunk.

All user-facing output should go through this module. Plugins should prefer
these helpers over importing Rich directly.

Key principle: stderr for status/progress, stdout for data.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

# stderr console for status messages (spinners, success/error)
err_console = Console(stderr=True)

# stdout console for data output (JSON, tables)
out_console = Console()


def _stream_is_tty(console: Console) -> bool:
    """Whether the console's stream is an interactive terminal.

    Deliberately not ``console.is_terminal``: Rich reports a terminal whenever
    FORCE_COLOR is set, including for a pipe — the environment #144 came from.
    """
    isatty = getattr(console.file, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except ValueError:  # closed stream (e.g. at interpreter/pytest teardown)
        return False


def write_raw(text: str, console: Console, output_path: str = "") -> None:
    """Emit *text* exactly as given: to *output_path* if set, else to the console's stream.

    Data payloads (raw responses, CSV, JSON) must never pass through Rich's
    ``print``: it parses ``[...]`` as markup (a ``[/LB]`` pack size crashed
    ``--format raw``, #145), highlights, expands tabs, converts emoji shortcodes
    and word-wraps to the console width (a file console wrapped rows over 200
    columns, #144). Writing to the underlying stream keeps the bytes intact.
    Files are always UTF-8, independent of the locale.

    Because it bypasses ``Console.print``, the text is invisible to
    ``Console.capture()`` and ``record=True``; do not pass a capturing console.
    """
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        return
    console.file.write(text)
    console.file.flush()


def success(message: str, *, console: Console | None = None) -> None:
    """Print a success message (green checkmark) to stderr.

    The message is printed verbatim (no markup parsing): it routinely carries
    user paths, exception text and response fragments.
    """
    c = console or err_console
    c.print(f"  \u2713 {message}", style="green", markup=False, highlight=False)


def error(message: str, *, console: Console | None = None) -> None:
    """Print an error message (red X) to stderr."""
    c = console or err_console
    c.print(f"  \u2717 {message}", style="red", markup=False, highlight=False)


def warn(message: str, *, console: Console | None = None) -> None:
    """Print a warning message (yellow) to stderr."""
    c = console or err_console
    c.print(f"  \u26a0 {message}", style="yellow", markup=False, highlight=False)


def info(message: str, *, console: Console | None = None) -> None:
    """Print an info message (dim) to stderr."""
    c = console or err_console
    c.print(f"  {message}", style="dim", markup=False, highlight=False)
