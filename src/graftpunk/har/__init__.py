"""HAR (HTTP Archive) parsing and the run digest.

Everything that understands a HAR file: the parser, and the digest that
turns entries into a readable RunDigest.

Example usage:
    from pathlib import Path

    from graftpunk.har import parse_har_file
    from graftpunk.har.digest import DigestSource, digest
    from graftpunk.har.report import render_markdown

    result = parse_har_file("network.har")
    source = DigestSource.from_har(Path("network.har"))
    print(render_markdown(digest(source)))
"""

from graftpunk.har.digest import (
    DigestSource,
    Endpoint,
    LoginForm,
    LoginObservation,
    RunDigest,
    ShapeNode,
    TokenCandidate,
    digest,
)
from graftpunk.har.parser import (
    HAREntry,
    HARParseResult,
    HARRequest,
    HARResponse,
    ParseError,
    parse_har_file,
)

__all__ = [
    # Parser
    "HAREntry",
    "HARParseResult",
    "HARRequest",
    "HARResponse",
    "ParseError",
    "parse_har_file",
    # Digest
    "DigestSource",
    "Endpoint",
    "LoginForm",
    "LoginObservation",
    "RunDigest",
    "ShapeNode",
    "TokenCandidate",
    "digest",
]
