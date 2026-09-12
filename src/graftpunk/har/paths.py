"""Pure path templating, no HAR involved.

The one rule for collapsing a dynamic path segment into a parameter, shared
by the digest's endpoint modelling and the fixtures/naming rule below it
(plugin tooling spec, 2026-09-11).
"""

from __future__ import annotations

import re

# A segment collapses to a parameter when it looks like an opaque
# identifier rather than a word: all digits, a UUID, 16+ hex characters, or
# 20+ URL-safe-base64-like characters. The base64-like check additionally
# requires at least one digit, so an ordinary long slug ("administrator-
# dashboard") is not mistaken for an encoded token.
_MIN_HEX_LEN = 16
_MIN_BASE64_LEN = 20

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_BASE64_RE = re.compile(r"^[A-Za-z0-9_-]+=*$")

__all__ = ["looks_dynamic", "param_name_for_segment", "template_path"]


def looks_dynamic(segment: str) -> bool:
    """True when *segment* reads as an opaque identifier rather than a word.

    The one owner of that judgement: ``template_path`` collapses on it, and the
    digest's high-cardinality collapse gates on it (in a relaxed form) so a run
    of distinct word-like sibling routes is not mistaken for one parameterised
    family.
    """
    if not segment:
        return False
    if segment.isdigit():
        return True
    if _UUID_RE.match(segment):
        return True
    if len(segment) >= _MIN_HEX_LEN and _HEX_RE.match(segment):
        return True
    return (
        len(segment) >= _MIN_BASE64_LEN
        and bool(_BASE64_RE.match(segment))
        and any(ch.isdigit() for ch in segment)
    )


def param_name_for_segment(prev_segment: str) -> str:
    """The parameter name for a segment collapsed at ``prev_segment``'s position.

    The singular of the preceding literal segment (``orders`` -> ``order_id``),
    or the bare ``id`` when there is no preceding segment or it is itself a
    collapsed parameter (``{order_id}``).
    """
    if not prev_segment or prev_segment.startswith("{"):
        return "id"
    is_plural = prev_segment.endswith("s") and len(prev_segment) > 1
    singular = prev_segment[:-1] if is_plural else prev_segment
    return f"{singular}_id"


def template_path(path: str) -> tuple[str, dict[str, str]]:
    """Collapse a URL path's dynamic segments into named parameters.

    ``/orders/123`` becomes ``/orders/{order_id}``. A leading and/or
    trailing slash is preserved exactly as given.

    Returns:
        The templated path, and a dict mapping each collapsed parameter
        name to the literal segment value it replaced.
    """
    leading = "/" if path.startswith("/") else ""
    trailing = "/" if len(path) > 1 and path.endswith("/") else ""
    segments = path.strip("/").split("/") if path.strip("/") else []

    params: dict[str, str] = {}
    result_segments: list[str] = []
    for segment in segments:
        if looks_dynamic(segment):
            prev = result_segments[-1] if result_segments else ""
            name = param_name_for_segment(prev)
            params[name] = segment
            result_segments.append(f"{{{name}}}")
        else:
            result_segments.append(segment)

    body = "/".join(result_segments)
    return f"{leading}{body}{trailing}" if body else (leading or "/"), params
