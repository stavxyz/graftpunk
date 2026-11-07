"""The request headers a recording holds that neither plugin source nor a replayed
header profile ever sends: the HTTP client or the transport supplies them.

Its own module, importing nothing, so the digest, the scaffold, gp observe's
header profiles and ``GraftpunkSession`` all read one rule without the session
importing ``graftpunk.har``.
"""

from __future__ import annotations

__all__ = ["CLIENT_SET_HEADERS", "client_sets_header"]

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
