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
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from graftpunk.exceptions import GraftpunkError
from graftpunk.graftpunk_session import GraftpunkSession
from graftpunk.har.graphql import request_operation
from graftpunk.har.naming import capture_slug, parse_endpoint_key
from graftpunk.plugins.cli_plugin import CommandContext, PluginConfig
from graftpunk.testing.sidecar import is_sidecar, load_sidecar, sidecar_path

__all__ = [
    "FixtureAnswer",
    "FixtureNotFoundError",
    "FixtureRequest",
    "FixtureSession",
    "fixture_context",
    "make_context",
]

_CONTENT_TYPE_BY_SUFFIX: dict[str, str] = {
    ".json": "application/json",
    ".html": "text/html",
    ".txt": "text/plain",
    ".xml": "application/xml",
}
_DEFAULT_FIXTURE_CONTENT_TYPE = "application/octet-stream"
_PREFERRED_FIXTURE_SUFFIX = ".json"


class FixtureNotFoundError(GraftpunkError, LookupError):
    """A GraphQL request ``FixtureSession`` has no fixture or answer for. Raised, not
    answered 404, so a command sending an operation nobody recorded fails its test."""


@dataclass(frozen=True)
class FixtureRequest:
    """One request a ``FixtureSession`` answered: its method, URL path, GraphQL
    operation (None for a request that carried none, a batch included) and that
    operation's variables, and the decoded body (``json=``, or ``data=`` read as
    JSON when it parses, else as given; None when it sent none)."""

    method: str
    path: str
    operation: str | None
    variables: dict[str, Any] | None
    body: Any


@dataclass(frozen=True)
class FixtureAnswer:
    """A response ``FixtureSession.answer`` registered for a row."""

    status: int
    content_type: str
    body: bytes


def _request_body(kwargs: Mapping[str, Any]) -> Any:
    """The body a request call sends, decoded: ``json=`` as given, ``data=`` text
    or bytes parsed as JSON when it is JSON, else as given."""
    if kwargs.get("json") is not None:
        return kwargs["json"]
    data = kwargs.get("data")
    if isinstance(data, bytes):
        try:
            data = data.decode("utf-8")
        except UnicodeDecodeError:
            return data
    if isinstance(data, str):
        try:
            return json.loads(data)
        except ValueError:
            return data
    return data


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

    The lookup matches a file that is the base stem plus exactly one extension
    (``get_api_users.csv.txt`` is not ``get_api_users``'s), the stem escaped so a
    ``[`` in a path is a character and not a pattern, so the ``#1``, ``#2`` files ``gp observe
    fixtures`` writes for repeated captures of one template are never consulted:
    a second recorded response becomes a fixture by being copied onto the base
    name. When a stem has several extensions, ``.json``
    wins and the rest follow in sorted order: an endpoint whose fixture
    directory holds both a ``.html`` and a ``.json`` for one stem is a JSON
    endpoint with an error page beside it, and plain sorted order served the
    error page.

    A request whose body (``json=``, or ``data=`` that parses as JSON) is one
    GraphQL operation (:func:`graftpunk.har.graphql.request_operation`) is
    answered from the stem with the operation added, ``post_graphql(OrdersByCustomer)``,
    the name ``gp observe fixtures`` writes for that operation, and from nothing
    else: with no such file it raises :class:`FixtureNotFoundError`, so a command
    sending an operation no fixture holds fails its test (#367). A batch is looked
    up by the plain stem.

    ``requests`` lists every request answered, in order, as :class:`FixtureRequest`
    (method, path, operation, variables, decoded body), for a test to assert what a
    command sent. :meth:`answer` registers a response for one row (an error status,
    an empty or HTML body) that wins over the directory.
    """

    def __init__(self, fixtures_dir: Path | str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fixtures_dir = Path(fixtures_dir)
        self._answers: dict[tuple[str, str, str], FixtureAnswer] = {}
        self.requests: list[FixtureRequest] = []

    def answer(
        self,
        key: str,
        *,
        status: int = 200,
        content_type: str = "application/json",
        body: str | bytes = b"",
    ) -> None:
        """Answer every later request for *key* with this response instead of a
        fixture file: an error, an empty, or an HTML response a test needs beside
        the recorded fixtures, with no file to write. *key* is a row as the digest
        prints it, ``"POST /graphql (OrdersByCustomer)"`` or ``"GET /api/orders"``,
        the path literal (no placeholder, no glob). A later call for the same key
        replaces the earlier one.

        Raises:
            graftpunk.har.naming.EndpointSpecError: *key* does not read as a row.
        """
        method, path, operation = parse_endpoint_key(key)
        content = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        self._answers[(method, capture_slug(method, path), operation)] = FixtureAnswer(
            status=status, content_type=content_type, body=content
        )

    def request(  # ty: ignore[invalid-method-override]
        self, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        path = urlparse(url).path or "/"
        body = _request_body(kwargs)
        operation = request_operation(body)
        name = "" if operation is None else operation.name
        self.requests.append(
            FixtureRequest(
                method=method.upper(),
                path=path,
                operation=operation.name if operation is not None else None,
                variables=dict(operation.variables or {}) if operation is not None else None,
                body=body,
            )
        )
        response = requests.Response()
        response.request = requests.PreparedRequest()
        response.request.method = method.upper()
        response.request.url = url
        response.url = url
        override = self._answers.get((method.upper(), capture_slug(method, path), name))
        if override is not None:
            response.status_code = override.status
            response.headers["Content-Type"] = override.content_type
            response._content = override.body
            return response
        stem = capture_slug(method, path, name)
        matches = sorted(
            (
                p
                for p in self._fixtures_dir.glob(f"{glob.escape(stem)}.*")
                # The stem plus exactly one extension: get_api_users.csv.txt is
                # another endpoint's fixture, not get_api_users'.
                if p.is_file() and not is_sidecar(p) and p.stem == stem
            ),
            key=_fixture_preference,
        )
        if not matches and operation is not None:
            # Loud, never a 404 or another operation's file: a test of a command
            # sending an operation it has no fixture for must fail (#367).
            raise FixtureNotFoundError(
                f"No fixture answers {method.upper()} {path} operation {operation.name}: "
                f"{self._fixtures_dir / stem}.<ext> does not exist. Write it with gp observe "
                f'fixtures --match "{method.upper()} {path} ({operation.name.strip("()")})", '
                "or answer it with FixtureSession.answer."
            )
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
