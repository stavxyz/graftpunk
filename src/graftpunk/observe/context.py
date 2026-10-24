"""Plugin-facing observability context."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from graftpunk.logging import get_logger

if TYPE_CHECKING:
    import requests

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

    def start(self) -> None:
        """Start the capture, when there is one.

        A capture that fails to start is logged and dropped, the run directory
        is removed while it is still empty, and the command still runs, with
        nothing recorded.
        """
        if self._mode == "off" or self._capture is None:
            return
        try:
            self._capture.start_capture()
        except Exception as exc:  # noqa: BLE001 - opt-in diagnostics are best-effort
            from graftpunk import console as gp_console

            LOG.error("observe_start_capture_failed", error=str(exc), exc_type=type(exc).__name__)
            gp_console.warn(f"Observability capture unavailable: could not start it ({exc}).")
            self._capture = None
            if self._storage is not None and self._storage.remove_if_empty():
                self._storage = None

    def finish(self, redact: Any = ()) -> None:
        """Stop the capture and write its HAR, bodies and console logs to the run.

        Safe to call when observe is off or nothing was captured. Nothing here
        raises; a failed write is logged (see
        :func:`graftpunk.observe.run.write_capture_data`).

        Args:
            redact: What to scrub from the HAR and the request bodies: a
                :class:`graftpunk.observe.run.Redaction`, or values that are all secrets.
        """
        if self._mode == "off" or self._capture is None or self._storage is None:
            return
        from graftpunk.observe.run import write_capture_data

        try:
            self._capture.stop_capture()
        except Exception as exc:  # noqa: BLE001 - still write what was captured
            LOG.error("observe_stop_capture_failed", error=str(exc), exc_type=type(exc).__name__)
        entries = write_capture_data(self._storage, self._capture, redact)
        LOG.info("observe_run_saved", run_dir=str(self._storage.run_dir), har_entries=entries)


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
    http_session: requests.Session | None = None,
    redact: Any = (),
) -> ObservabilityContext:
    """Build an observability context for a plugin command or session.

    The capture is not started here: call :meth:`ObservabilityContext.start`
    before the work and :meth:`ObservabilityContext.finish` after it.

    Args:
        site_name: Plugin site name, normalized via
            :func:`graftpunk.observe.storage.session_dirname` before use as
            a subdirectory (the same transformation every other writer
            applies, so readers like ``gp observe show`` find the run).
        backend_type: Browser backend type ("selenium" or "nodriver").
        driver: Browser driver instance, or None.
        mode: Observe mode ("off" or "full").
        http_session: With no driver, the ``requests`` session whose traffic
            the run records (:class:`graftpunk.observe.http_capture.HttpCaptureBackend`).
        redact: Secret values the HTTP capture scrubs from each entry as it
            records it; ``finish()`` should be given the same values.

    Returns:
        Configured ObservabilityContext, or NoOpObservabilityContext if mode is
        "off" or the run directory cannot be created.
    """
    if mode == "off":
        return NoOpObservabilityContext()

    import datetime

    from graftpunk.observe.storage import ObserveStorage, session_dirname

    run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    try:
        storage = ObserveStorage(OBSERVE_BASE_DIR, session_dirname(site_name), run_id)
    except (ValueError, OSError) as exc:
        from graftpunk import console as gp_console

        LOG.warning(
            "observe_start_skipped_storage_error",
            site_name=site_name,
            error=str(exc),
            exc_type=type(exc).__name__,
        )
        gp_console.warn(f"Observability capture unavailable: {exc}")
        return NoOpObservabilityContext()
    bodies_dir = storage.run_dir / "bodies"

    if driver is not None:
        from graftpunk.observe.capture import create_capture_backend

        capture = create_capture_backend(backend_type, driver, bodies_dir=bodies_dir)
        return ObservabilityContext(capture=capture, storage=storage, mode=mode)

    if http_session is not None:
        from graftpunk.observe.http_capture import SPOOL_NAME, HttpCaptureBackend

        return ObservabilityContext(
            capture=HttpCaptureBackend(
                http_session,
                bodies_dir=bodies_dir,
                spool_path=storage.run_dir / SPOOL_NAME,
                redact=redact,
            ),
            storage=storage,
            mode=mode,
        )

    LOG.warning(
        "observe_capture_unavailable",
        site_name=site_name,
        reason="no browser driver",
    )
    return ObservabilityContext(capture=None, storage=storage, mode=mode)
