"""Two renderers for a RunDigest: markdown for a person, JSON for a program.

Each applies its own budget on top of what ``digest()`` found (the model
itself is complete): the markdown renderer's ``limit`` caps the endpoint
list and orders JSON endpoints first, the JSON renderer is complete. Not
exported from ``graftpunk.har``'s top level, so presentation can change
without touching the package's compatibility surface (plugin tooling spec,
2026-09-11).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from graftpunk.contracts import current_schema
from graftpunk.har.digest import INTERNAL, Endpoint, RunDigest, ShapeNode
from graftpunk.har.paths import template_path

__all__ = [
    "DEFAULT_ENDPOINT_LIMIT",
    "SHAPE_UNAVAILABLE_SUMMARY",
    "endpoints_projection",
    "render_endpoints_json",
    "render_json",
    "render_markdown",
    "summarize_shape",
]

DEFAULT_ENDPOINT_LIMIT = 60
_DEFAULT_SUMMARY_DEPTH = 3

SHAPE_UNAVAILABLE_SUMMARY = "shape unavailable: body over the sampling threshold"
"""How :data:`graftpunk.har.digest.SHAPE_UNAVAILABLE` reads in a digest."""


def summarize_shape(shape: ShapeNode | None, *, depth: int = _DEFAULT_SUMMARY_DEPTH) -> str:
    """A one-line human-readable summary of *shape*, recursing up to *depth* levels.

    The one shape-summarising function: the markdown renderer below and the
    scaffold's generated docstrings (``devtools/scaffold/render.py``) both
    call this instead of keeping their own divergent formatting, so "what
    does this endpoint return" reads the same in a digest and in a generated
    plugin (plugin tooling spec, 2026-09-11; validation net-negative,
    addressed 2026-09-12).
    """
    if shape is None:
        return "non-JSON"
    if shape.kind == "unavailable":
        return SHAPE_UNAVAILABLE_SUMMARY
    if shape.kind in ("string", "number", "boolean", "null"):
        return shape.kind
    if shape.kind == "array":
        if depth <= 0:
            return "array<...>"
        return f"array<{summarize_shape(shape.item, depth=depth - 1)}>"
    if not shape.children:
        return "object{}"
    if depth <= 0:
        return "object{...}"
    keys = ", ".join(sorted(shape.children))
    suffix = ", ..." if shape.truncated else ""
    return f"object{{{keys}{suffix}}}"


def _endpoint_block(endpoint: Endpoint) -> list[str]:
    lines = [f"### {'/'.join(endpoint.methods)} {endpoint.template}", ""]
    statuses = list(endpoint.statuses)
    lines.append(f"- host: `{endpoint.host}`, count: {endpoint.count}, statuses: {statuses}")
    lines.append(f"- content type: `{endpoint.content_type or '(none)'}`")
    if endpoint.query_params:
        params = ", ".join(f"{k}: {v}" for k, v in sorted(endpoint.query_params.items()))
        lines.append(f"- query params: {params}")
    if endpoint.body_params:
        params = ", ".join(f"{k}: {v}" for k, v in sorted(endpoint.body_params.items()))
        lines.append(f"- body params ({endpoint.body_kind}): {params}")
    if endpoint.custom_headers:
        lines.append(f"- custom headers: {', '.join(endpoint.custom_headers)}")
    lines.append(f"- shape: {summarize_shape(endpoint.shape)}")
    if endpoint.examples:
        lines.append(f"- examples: {', '.join(endpoint.examples)}")
    lines.append("")
    return lines


def _endpoint_sort_key(endpoint: Endpoint) -> tuple[bool, int, str]:
    return (endpoint.shape is None, -endpoint.count, endpoint.template)


def _other_host_sort_key(item: tuple[str, int]) -> int:
    return -item[1]


def render_markdown(d: RunDigest, *, limit: int = DEFAULT_ENDPOINT_LIMIT) -> str:
    """A person-readable digest: Summary, Login, Tokens, Cookies, Endpoints, Other hosts."""
    lines: list[str] = ["# Observe digest", ""]

    lines.append("## Summary")
    source_label = (
        f"{d.source.session}/{d.source.run_id}" if d.source.session else str(d.source.har_path)
    )
    lines.append(f"- source: `{source_label}`")
    lines.append(f"- primary host: `{d.primary_host}`")
    lines.append(f"- hosts: {len(d.hosts)}, endpoints: {len(d.endpoints)}")
    lines.append(
        f"- dropped: static={d.dropped.get('static', 0)}, "
        f"third_party={d.dropped.get('third_party', 0)}, "
        f"other_scheme={d.dropped.get('other_scheme', 0)}, "
        f"error={d.dropped.get('error', 0)}"
    )
    lines.append("")

    lines.append("## Login")
    if d.login:
        for obs in d.login:
            field_note = f" ({', '.join(obs.fields)})" if obs.fields else ""
            redirect_note = f" -> {obs.redirect_to}" if obs.redirect_to else ""
            lines.append(
                f"{obs.order}. {obs.method} {obs.url} [{obs.status}] "
                f"{obs.kind}{field_note}{redirect_note}"
            )
    else:
        lines.append("(no login observations)")
    if d.login_forms:
        lines.append("")
        for form in d.login_forms:
            lines.append(f"- form at `{form.action}` ({form.method}), source: {form.source}")
            for role, selector in sorted(form.fields.items()):
                lines.append(f"    - {role}: `{selector}`")
    lines.append("")

    lines.append("## Tokens")
    if d.tokens:
        for token in d.tokens:
            lines.append(f"- {token.kind} `{token.name}`, seen on: {', '.join(token.seen_on)}")
    else:
        lines.append("(none found)")
    lines.append("")

    lines.append("## Cookies")
    lines.append(", ".join(d.cookies) if d.cookies else "(none)")
    lines.append("")

    lines.append("## Endpoints")
    ordered = sorted(d.endpoints, key=_endpoint_sort_key)
    shown, remaining = ordered[:limit], ordered[limit:]
    for endpoint in shown:
        lines.extend(_endpoint_block(endpoint))
    if remaining:
        lines.append(f"... and {len(remaining)} more endpoint(s) (raise --limit to see them)")
        lines.append("")

    other_hosts = {h: c for h, c in d.hosts.items() if h != d.primary_host}
    if other_hosts:
        lines.append("## Other hosts")
        for host, count in sorted(other_hosts.items(), key=_other_host_sort_key):
            lines.append(f"- `{host}`: {count} request(s)")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _jsonable(getattr(value, f.name))
            for f in dataclasses.fields(value)
            if not f.metadata.get(INTERNAL)
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def render_json(d: RunDigest) -> str:
    """The complete digest as JSON: nothing capped, nothing summarised. A field
    marked :data:`graftpunk.har.digest.INTERNAL` is a lookup for code and is left
    out."""
    return json.dumps(_jsonable(d), indent=2, sort_keys=True)


def _templated_url(url: str) -> str:
    """*url* with its path templated the way endpoints are: an observation carries
    the raw path, which can hold an account id or a one-time token."""
    parts = urlsplit(url)
    template, _ = template_path(parts.path or "/")
    return urlunsplit((parts.scheme, parts.netloc, template, "", ""))


def endpoints_projection(d: RunDigest) -> dict[str, Any]:
    """The declared projection ``gp observe digest --endpoints-json`` prints.

    An explicit field list, never a reflection over the digest's dataclasses, so
    renaming an internal field touches this function and no contract. It carries
    only what a command proposal consumes, and by construction no cookie name, no
    token candidate, no example path, no body, no untemplated login URL path, and
    no form action query string or ``;params``. Its field set is pinned per
    schema version; fields are added and never renamed or removed within one
    (:mod:`graftpunk.contracts`). ``render_json`` stays an unversioned dump.
    """
    source = d.source
    return {
        "schema": current_schema("endpoints"),
        "source": {
            "session": source.session,
            "run_id": source.run_id,
            "har": None if source.session else str(source.har_path),
        },
        "primary_host": d.primary_host,
        "endpoints": [
            {
                "method": method,
                "template": endpoint.template,
                "login_flow": endpoint.login_flow,
                "content_type": endpoint.content_type,
                "shape": summarize_shape(endpoint.shape),
                "query_params": dict(sorted(endpoint.query_params.items())),
                "body_params": dict(sorted(endpoint.body_params.items())),
                "custom_headers": list(endpoint.custom_headers),
            }
            for endpoint in sorted(d.endpoints, key=_endpoint_sort_key)
            for method in endpoint.methods
        ],
        "login": {
            "auth_urls": [
                {"method": o.method, "url": _templated_url(o.url), "kind": o.kind} for o in d.login
            ],
            "forms": [
                {"action": form.action, "fields": dict(sorted(form.fields.items()))}
                for form in d.login_forms
            ],
        },
    }


def render_endpoints_json(d: RunDigest) -> str:
    """:func:`endpoints_projection` as indented JSON with sorted keys."""
    return json.dumps(endpoints_projection(d), indent=2, sort_keys=True)
