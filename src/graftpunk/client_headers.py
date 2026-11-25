"""The request headers a recording holds that neither plugin source nor a replayed
header profile ever sends: the HTTP client or the transport supplies them.

Its own module, importing nothing, so the digest, the scaffold, gp observe's
header profiles and ``GraftpunkSession`` all read one rule without the session
importing ``graftpunk.har``.
"""

from __future__ import annotations

__all__ = ["CLIENT_SET_HEADERS", "client_sets_header", "is_library_default"]

# GraftpunkSession's HTTP client sends Host, Connection, Accept-Encoding and
# Content-Length on its own (read off the wire of a GET, a json= POST and a data=
# POST), and Priority is an HTTP/2 transport signal. A browser's Accept-Encoding
# can offer an encoding the client cannot decode (Chrome offers zstd, which
# urllib3 decodes only with zstandard installed). Pseudo-headers (":authority",
# ":method", ...) are matched by their leading colon in client_sets_header.
CLIENT_SET_HEADERS: frozenset[str] = frozenset(
    {"host", "connection", "accept-encoding", "content-length", "priority"}
)


def client_sets_header(name: str) -> bool:
    """Whether request header *name* is one the HTTP client or the transport
    supplies: an HTTP/2 pseudo-header (a name starting with ``:``) or one of
    :data:`CLIENT_SET_HEADERS`, compared without regard to case."""
    lowered = name.strip().lower()
    return lowered.startswith(":") or lowered in CLIENT_SET_HEADERS


# What ``requests.utils.default_headers()`` holds apart from the two client-set
# names (Accept-Encoding and Connection, whose value varies with the installed
# decoders and the Python version): Accept is the same in every release, and
# User-Agent names the release, so it is matched by prefix.
_LIBRARY_DEFAULT_USER_AGENT_PREFIX = "python-requests/"
_LIBRARY_DEFAULT_ACCEPT = "*/*"


def is_library_default(name: str, value: str) -> bool:
    """Whether the saved header *name*: *value* is a default of the HTTP library
    (of any ``requests`` release) rather than a value a browser or a plugin set.

    A session saved under one ``requests`` release and loaded under another
    carries the saving release's ``User-Agent: python-requests/<version>``, which
    equals no default of the running one, so equality with the running defaults
    cannot tell it from a captured value (#399, #52). A client-set header
    (:func:`client_sets_header`) is always the library's: its saved value is never
    restored.
    """
    if client_sets_header(name):
        return True
    lowered = name.strip().lower()
    if lowered == "user-agent":
        return value.startswith(_LIBRARY_DEFAULT_USER_AGENT_PREFIX)
    return lowered == "accept" and value == _LIBRARY_DEFAULT_ACCEPT
