"""The one capture/fixture filename rule.

``gp observe fixtures``, :mod:`graftpunk.testing`'s ``FixtureSession``, and
the scaffold's generated tests all name a file the same way, so this module
depends on ``paths.py`` and nothing else (plugin tooling spec, 2026-09-11).
"""

from __future__ import annotations

from graftpunk.har.paths import template_path

__all__ = [
    "EndpointSpecError",
    "HTTP_METHODS",
    "capture_filename",
    "capture_slug",
    "parse_command_spec",
    "parse_endpoint",
]

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


HTTP_METHODS: frozenset[str] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"}
)

_ENDPOINT_EXAMPLE = '"GET /orders/{order_id}"'
_COMMAND_EXAMPLE = '"order=GET /orders/{order_id}"'


class EndpointSpecError(ValueError):
    """A value that does not read as an endpoint or a command spec. The message says
    how to write one, and every consumer prints it unchanged."""


def parse_endpoint(value: str) -> tuple[str, str]:
    """``"<METHOD> <template>"``, the way the digest prints an endpoint, as its pair.

    The one reader of the grammar: ``gp observe fixtures --match``, both
    ``--command`` options, and the skill's own composition all take the halves
    from here. The method is one of :data:`HTTP_METHODS`, in capitals; the
    template is everything after the first space, stripped, and may be a glob
    where the consumer accepts one. A value that splits wrong would match nothing
    and look like an empty result, so it is refused instead (final fix wave,
    2026-09-12).

    Raises:
        EndpointSpecError: No space, a method that is not a capitalised HTTP
            method, an empty template, a template with whitespace inside it, or
            one that starts with neither ``/`` nor ``*`` (a path always starts
            with ``/``, so such a template could only ever match nothing).
    """
    method, separator, template = value.strip().partition(" ")
    template = template.strip()
    if not separator or not template or method not in HTTP_METHODS:
        raise EndpointSpecError(
            f'{value!r} is not a "METHOD template" pair. Write the method in capitals, '
            f"a space, then the template, as in {_ENDPOINT_EXAMPLE}."
        )
    if any(character.isspace() for character in template):
        raise EndpointSpecError(
            f"{value!r}: the template {template!r} has whitespace inside it. Write one "
            f"method and one path, as in {_ENDPOINT_EXAMPLE}."
        )
    if not template.startswith(("/", "*")):
        raise EndpointSpecError(
            f"{value!r}: the template {template!r} starts with neither '/' nor '*'. "
            f"Write the path as the digest prints it, as in {_ENDPOINT_EXAMPLE}."
        )
    return method, template


def parse_command_spec(value: str) -> tuple[str, str, str]:
    """``"<name>=<METHOD> <template>"`` as its (name, method, template) triple.

    A command name cannot contain ``=``, so the split on the first one is
    unambiguous; the endpoint half goes through :func:`parse_endpoint`. Whether
    the name is a usable command name is the generator's rule, not this one's.

    Raises:
        EndpointSpecError: No ``=``, an empty name, nothing after the ``=``, or
            an endpoint half :func:`parse_endpoint` refuses.
    """
    name, separator, endpoint = value.partition("=")
    name = name.strip()
    if not separator:
        raise EndpointSpecError(
            f"{value!r} has no '='. Write the command name, '=', then the endpoint, "
            f"as in {_COMMAND_EXAMPLE}."
        )
    if not name:
        raise EndpointSpecError(
            f"{value!r} has no command name before '='. Write one, as in {_COMMAND_EXAMPLE}."
        )
    if not endpoint.strip():
        raise EndpointSpecError(
            f"{value!r} has nothing after '='. Write the endpoint the way the digest "
            f"prints it, as in {_COMMAND_EXAMPLE}."
        )
    method, template = parse_endpoint(endpoint)
    return name, method, template
