"""The one capture/fixture filename rule.

``gp observe fixtures``, :mod:`graftpunk.testing`'s ``FixtureSession``, and
the scaffold's generated tests all name a file the same way, so this module
depends on ``paths.py`` and nothing else (plugin tooling spec, 2026-09-11).
"""

from __future__ import annotations

from graftpunk.har.paths import template_path

__all__ = ["capture_filename", "capture_slug"]

_EXTENSION_BY_MIME: dict[str, str] = {
    "application/json": "json",
    "text/html": "html",
    "text/xml": "xml",
    "application/xml": "xml",
    "text/css": "css",
    "application/javascript": "js",
    "text/javascript": "js",
    "application/pdf": "pdf",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/svg+xml": "svg",
}
_TEXT_MIME_KEYWORDS = ("text", "xml")


def capture_slug(method: str, path: str) -> str:
    """The method+templated-path stem shared by a capture, a fixture, and its sidecar."""
    template, _ = template_path(path)
    body = template.strip("/").replace("/", "_")
    return f"{method.lower()}_{body or 'root'}"


def _extension_for_content_type(content_type: str) -> str:
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime in _EXTENSION_BY_MIME:
        return _EXTENSION_BY_MIME[mime]
    if any(keyword in mime for keyword in _TEXT_MIME_KEYWORDS):
        return "txt"
    return "bin"


def capture_filename(method: str, path: str, content_type: str) -> str:
    """``<method>_<slug>.<ext>``, the one name a capture, fixture, and test share."""
    return f"{capture_slug(method, path)}.{_extension_for_content_type(content_type)}"
