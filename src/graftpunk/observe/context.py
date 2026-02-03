"""Plugin-facing observability context."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from graftpunk.logging import get_logger

if TYPE_CHECKING:
    from graftpunk.observe.capture import CaptureBackend
    from graftpunk.observe.storage import ObserveStorage

LOG = get_logger(__name__)


class ObservabilityContext:
    """Plugin-facing observability handle.

    All methods are safe to call regardless of mode. In 'off' mode, they're no-ops.
    """

    def __init__(
        self,
        capture: CaptureBackend | None,
        storage: ObserveStorage | None,
        mode: Literal["off", "full"],
    ) -> None:
        self._capture = capture
        self._storage = storage
        self._mode = mode
        self._counter = 0

    def screenshot(self, label: str) -> Path | None:
        """Take a screenshot and save it with the given label.

        Args:
            label: Descriptive label for the screenshot file.

        Returns:
            Path to saved screenshot, or None if observability is off or capture failed.
        """
        if self._mode == "off" or self._capture is None or self._storage is None:
            return None
        self._counter += 1
        png_data = self._capture.take_screenshot_sync()
        if png_data is None:
            return None
        return self._storage.save_screenshot(self._counter, label, png_data)

    async def screenshot_async(self, label: str) -> Path | None:
        """Take a screenshot asynchronously (works with nodriver).

        Args:
            label: Descriptive label for the screenshot file.

        Returns:
            Path to saved screenshot, or None if observability is off or capture failed.
        """
        if self._mode == "off" or self._capture is None or self._storage is None:
            return None
        self._counter += 1
        data = await self._capture.take_screenshot()
        if data is None:
            return None
        return self._storage.save_screenshot(self._counter, label, data)

    def log(self, event: str, data: dict[str, Any] | None = None) -> None:
        """Write a structured event to the observability log.

        Args:
            event: Event name/type.
            data: Optional key-value data associated with the event.
        """
        if self._mode == "off" or self._storage is None:
            return
        self._storage.write_event(event, data or {})

    def mark(self, label: str) -> None:
        """Record a named timing mark.

        Args:
            label: Descriptive label for the mark.
        """
        if self._mode == "off" or self._storage is None:
            return
        self._storage.write_event("mark", {"label": label, "timestamp": time.time()})


class NoOpObservabilityContext(ObservabilityContext):
    """No-op implementation for when observability is off."""

    def __init__(self) -> None:
        super().__init__(capture=None, storage=None, mode="off")


#: Default base directory for observability data.
OBSERVE_BASE_DIR = Path.home() / ".local" / "share" / "graftpunk" / "observe"


def build_observe_context(
    site_name: str,
    backend_type: str,
    driver: Any,
    mode: Literal["off", "full"],
) -> ObservabilityContext:
    """Build an observability context for a plugin command or session.

    Args:
        site_name: Plugin site name (used as subdirectory).
        backend_type: Browser backend type ("selenium" or "nodriver").
        driver: Browser driver instance, or None.
        mode: Observe mode ("off" or "full").

    Returns:
        Configured ObservabilityContext, or NoOpObservabilityContext if mode is "off".
    """
    if mode == "off":
        return NoOpObservabilityContext()

    import datetime

    from graftpunk.observe.capture import create_capture_backend
    from graftpunk.observe.storage import ObserveStorage

    run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    storage = ObserveStorage(OBSERVE_BASE_DIR, site_name, run_id)

    if driver is not None:
        capture = create_capture_backend(
            backend_type, driver, bodies_dir=storage.run_dir / "bodies"
        )
        return ObservabilityContext(capture=capture, storage=storage, mode=mode)

    LOG.warning(
        "observe_capture_unavailable",
        site_name=site_name,
        reason="no browser driver",
    )
    return ObservabilityContext(capture=None, storage=storage, mode=mode)
