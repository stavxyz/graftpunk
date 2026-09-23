"""Test doubles for plugin authors: pytest-free and unconditional.

Never imports pytest, so a generated project's runtime code (and any
consumer importing graftpunk without pytest installed) can import this
module safely. The pytest-dependent half is :mod:`graftpunk.testing.plugin`,
imported only from a test suite's ``conftest.py``, so the import boundary is
legible from the import paths themselves (plugin tooling spec, 2026-09-11,
design note).
"""

from __future__ import annotations

import glob
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from graftpunk.graftpunk_session import GraftpunkSession
from graftpunk.har.naming import capture_slug
from graftpunk.plugins.cli_plugin import CommandContext, PluginConfig
from graftpunk.testing.sidecar import is_sidecar, load_sidecar, sidecar_path

__all__ = ["FixtureSession", "fixture_context", "make_context"]

_CONTENT_TYPE_BY_SUFFIX: dict[str, str] = {
    ".json": "application/json",
    ".html": "text/html",
    ".txt": "text/plain",
    ".xml": "application/xml",
}
_DEFAULT_FIXTURE_CONTENT_TYPE = "application/octet-stream"
_PREFERRED_FIXTURE_SUFFIX = ".json"


def _fixture_preference(path: Path) -> tuple[int, str]:
    """Sort key for the fixture files sharing one stem: ``.json`` first, then name order."""
    return (0 if path.suffix == _PREFERRED_FIXTURE_SUFFIX else 1, path.name)


def make_context(
    session: requests.Session | None = None,
    *,
    plugin_name: str = "test",
    command_name: str = "test",
    base_url: str = "",
    config: PluginConfig | None = None,
) -> CommandContext:
    """A valid CommandContext for testing a command handler directly.

    Args:
        session: The session the context carries. Defaults to a fresh
            ``GraftpunkSession`` with no captured header roles.
        plugin_name: The plugin identity the context reports.
        command_name: The command identity the context reports.
        base_url: The plugin's base URL, for relative-URL resolution in
            ``ctx.request_json``/``ctx.request_text``.
        config: The plugin's ``PluginConfig``, or ``None``.
    """
    return CommandContext(
        session=session if session is not None else GraftpunkSession(),
        plugin_name=plugin_name,
        command_name=command_name,
        api_version=1,
        base_url=base_url,
        config=config,
    )


class FixtureSession(GraftpunkSession):
    """A GraftpunkSession that never opens a socket.

    Every request is answered from the file in *fixtures_dir* named the way
    :func:`graftpunk.har.naming.capture_slug` names a capture for that
    method and path, the same rule ``gp observe fixtures`` uses to write
    files, so a fixture copied from a capture keeps its name. The fixtures
    command also applies the digest's high-cardinality collapse, which this
    lookup does not know: a fixture named for a collapsed family
    (``get_products_{product_id}.json``) answers a request whose segment
    templates the same way (an id such as ``1``), not a request for one of
    the recorded slugs. A sidecar
    ``<filename>.meta.json`` beside a fixture supplies its status and content type
    when present, read through :mod:`graftpunk.testing.sidecar`; without one, the
    status is 200 and the type is guessed from the file's extension. No matching
    file answers 404.

    The lookup matches the base stem only, escaped so a ``[`` in a path is a
    character and not a pattern, so the ``#1``, ``#2`` files ``gp observe
    fixtures`` writes for repeated captures of one template are never consulted:
    a second recorded response becomes a fixture by being copied onto the base
    name. When a stem has several extensions, ``.json``
    wins and the rest follow in sorted order: an endpoint whose fixture
    directory holds both a ``.html`` and a ``.json`` for one stem is a JSON
    endpoint with an error page beside it, and plain sorted order served the
    error page (polish round 1, 2026-09-12).
    """

    def __init__(self, fixtures_dir: Path | str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fixtures_dir = Path(fixtures_dir)

    def request(  # ty: ignore[invalid-method-override]
        self, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        path = urlparse(url).path or "/"
        stem = capture_slug(method, path)
        matches = sorted(
            (
                p
                for p in self._fixtures_dir.glob(f"{glob.escape(stem)}.*")
                if p.is_file() and not is_sidecar(p)
            ),
            key=_fixture_preference,
        )
        response = requests.Response()
        response.request = requests.PreparedRequest()
        response.request.method = method.upper()
        response.request.url = url
        response.url = url
        if not matches:
            response.status_code = 404
            response._content = b""
            return response
        return self._respond_from_file(matches[0], response)

    def _respond_from_file(self, path: Path, response: requests.Response) -> requests.Response:
        """Answer from *path*; its sidecar, when present, is read through the owner,
        which refuses a sidecar outside its declared format rather than half-reading it."""
        meta_path = sidecar_path(path)
        status = 200
        content_type = _CONTENT_TYPE_BY_SUFFIX.get(path.suffix, _DEFAULT_FIXTURE_CONTENT_TYPE)
        if meta_path.exists():
            sidecar = load_sidecar(meta_path)
            status = sidecar.status
            content_type = sidecar.content_type
        response.status_code = status
        response.headers["Content-Type"] = content_type
        response._content = path.read_bytes()
        return response


def fixture_context(fixtures_dir: Path | str, **kwargs: Any) -> CommandContext:
    """``make_context(session=FixtureSession(fixtures_dir), **kwargs)`` and nothing more."""
    return make_context(session=FixtureSession(fixtures_dir), **kwargs)
