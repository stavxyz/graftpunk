"""RunDigest: a HAR (and whatever a run adds to it) read into a small, readable model.

Rules are documented on :func:`digest`. The exclusion and auth-path patterns
below replace ``har/analyzer.py``'s, moved here because the digest is the
only remaining consumer (plugin tooling spec, 2026-09-11).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from graftpunk.har.documents import (
    LoginForm,
    TokenCandidate,
    TokenKind,
    extract_login_forms,
    extract_token_candidates,
    looks_like_token_name,
)
from graftpunk.har.parser import HAREntry, parse_har_file
from graftpunk.har.paths import looks_dynamic, param_name_for_segment, template_path
from graftpunk.logging import get_logger

LOG = get_logger(__name__)

BodyKind = Literal["json", "form", "none"]
ObservationKind = Literal["form_page", "credential_post", "redirect", "set_cookie", "auth_api"]
DropReason = Literal["static", "third_party", "error"]

__all__ = [
    "BodyKind",
    "DigestSource",
    "DropReason",
    "Endpoint",
    "LoginForm",
    "LoginObservation",
    "ObservationKind",
    "RunDigest",
    "ShapeNode",
    "TokenCandidate",
    "TokenKind",
    "body_params",
    "digest",
]

# -- thresholds, every one a named constant (plugin tooling spec, "Rules the digest applies") --
_BODY_SAMPLE_THRESHOLD = 256 * 1024  # bodies over this size are sampled for shape only
_BODY_SAMPLE_SIZE = 64 * 1024
_HIGH_CARDINALITY_THRESHOLD = 8  # a segment with more distinct values than this collapses too
_DYNAMIC_MAJORITY = 0.5  # ... but only when more than this fraction of them look like identifiers
_LOGIN_WINDOW = 20  # entries after a credential post that may carry a redirect/set_cookie
_SHAPE_MAX_DEPTH = 3
_SHAPE_MAX_KEYS = 12
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)

_EXCLUDE_PATTERNS = [
    r"\.(js|css|png|jpg|jpeg|gif|svg|ico|woff|woff2|ttf|eot|map)(\?|$)",
    r"google-analytics",
    r"googletagmanager",
    r"facebook\.com",
    r"analytics",
    r"tracking",
    r"pixel",
    r"beacon",
    r"cdn\.",
    r"static\.",
    r"assets\.",
    r"fonts\.",
]
_EXCLUDE_REGEX = re.compile("|".join(_EXCLUDE_PATTERNS), re.IGNORECASE)
_STATIC_CONTENT_TYPE_PREFIXES = (
    "image/",
    "font/",
    "text/css",
    "application/javascript",
    "text/javascript",
)

_AUTH_URL_PATTERNS = [
    r"/login",
    r"/signin",
    r"/sign-in",
    r"/auth",
    r"/oauth",
    r"/authenticate",
    r"/session",
    r"/api/auth",
    r"/api/login",
    r"/api/session",
    r"/token",
    r"/callback",
    r"/sso",
]
_AUTH_URL_REGEX = re.compile("|".join(_AUTH_URL_PATTERNS), re.IGNORECASE)

_STANDARD_REQUEST_HEADERS = frozenset(
    {
        "host",
        "connection",
        "accept",
        "accept-encoding",
        "accept-language",
        "cache-control",
        "pragma",
        "content-type",
        "content-length",
        "cookie",
        "referer",
        "origin",
        "user-agent",
        "x-requested-with",
        "upgrade-insecure-requests",
    }
)
_STANDARD_REQUEST_HEADER_PREFIXES = ("sec-ch-", "sec-fetch-", "accept")

_PASSWORD_FIELD_HINTS = ("password", "passwd", "pwd")


@dataclass(frozen=True)
class DigestSource:
    """Where the digest reads a HAR from, and what else a run adds to it."""

    har_path: Path
    bodies_dir: Path | None = None
    page_source: Path | None = None
    session: str | None = None
    run_id: str | None = None

    @classmethod
    def from_run_dir(cls, run_dir: Path, *, session: str, run_id: str) -> DigestSource:
        bodies_dir = run_dir / "bodies"
        page_source = run_dir / "page-source.html"
        return cls(
            har_path=run_dir / "network.har",
            bodies_dir=bodies_dir if bodies_dir.is_dir() else None,
            page_source=page_source if page_source.is_file() else None,
            session=session,
            run_id=run_id,
        )

    @classmethod
    def from_har(cls, har_path: Path) -> DigestSource:
        return cls(har_path=har_path)


@dataclass(frozen=True)
class ShapeNode:
    """The shape of one JSON value: its kind, and (for objects/arrays) its children."""

    kind: Literal["object", "array", "string", "number", "boolean", "null"]
    children: dict[str, ShapeNode] | None = None  # objects: key -> node, at most _SHAPE_MAX_KEYS
    item: ShapeNode | None = None  # arrays: the first element's node
    truncated: bool = False  # keys beyond the cap or depth beyond the cap were dropped


@dataclass(frozen=True)
class Endpoint:
    """One (method, templated path) observed one or more times against the primary domain."""

    host: str
    template: str
    methods: tuple[str, ...]
    count: int
    statuses: tuple[int, ...]
    content_type: str
    query_params: dict[str, str]
    body_params: dict[str, str]
    body_kind: BodyKind
    shape: ShapeNode | None
    custom_headers: tuple[str, ...]
    examples: tuple[str, ...]


@dataclass(frozen=True)
class LoginObservation:
    """One entry in the credential-post-centred window that looks like part of a login."""

    order: int
    method: str
    url: str  # query stripped
    status: int
    kind: ObservationKind
    # field NAMES on a credential post, or cookie names on a set_cookie; never values
    fields: tuple[str, ...]


@dataclass(frozen=True)
class RunDigest:
    source: DigestSource
    primary_host: str
    hosts: dict[str, int]
    endpoints: tuple[Endpoint, ...]
    login: tuple[LoginObservation, ...]
    login_forms: tuple[LoginForm, ...]
    tokens: tuple[TokenCandidate, ...]
    cookies: tuple[str, ...]
    dropped: dict[DropReason, int]


def _registrable_domain(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _is_static(entry: HAREntry) -> bool:
    if _EXCLUDE_REGEX.search(entry.request.url):
        return True
    content_type = (entry.response.content_type or "").lower()
    return any(content_type.startswith(p) for p in _STATIC_CONTENT_TYPE_PREFIXES)


def _observed_type(value: str) -> str:
    lowered = value.lower()
    if lowered in ("true", "false"):
        return "bool"
    try:
        int(value)
        return "int"
    except ValueError:
        return "str"


def _query_param_types(url: str) -> dict[str, str]:
    query = urlparse(url).query
    if not query:
        return {}
    parsed = parse_qs(query, keep_blank_values=True)
    types: dict[str, str] = {}
    for name, values in parsed.items():
        types[name] = "list" if len(values) > 1 else _observed_type(values[0])
    return types


def _parse_body(entry: HAREntry) -> tuple[dict[str, str], BodyKind]:
    """The request body's field names and observed types, and which kind it was."""
    post_data = entry.request.post_data
    if not post_data:
        return {}, "none"

    try:
        parsed = json.loads(post_data)
        if isinstance(parsed, dict):
            types: dict[str, str] = {}
            for key, value in parsed.items():
                if isinstance(value, bool):
                    types[key] = "bool"
                elif isinstance(value, (int, float)):
                    types[key] = "int"
                elif isinstance(value, list):
                    types[key] = "list"
                else:
                    types[key] = "str"
            return types, "json"
    except (ValueError, TypeError):
        pass
    form = parse_qs(post_data, keep_blank_values=True)
    if form:
        types = {k: ("list" if len(v) > 1 else _observed_type(v[0])) for k, v in form.items()}
        return types, "form"
    return {}, "none"


def body_params(entry: HAREntry) -> dict[str, str]:
    """Request body field names to their observed type: JSON object fields,
    or form-encoded fields when the body is not JSON. Empty when there is no
    body.

    The one public entry point for "what are this entry's body param
    names": ``digest()``'s endpoint accumulation and the fixtures command's
    sidecar (``cli/observe_commands.py``) both call this instead of
    reimplementing the JSON-then-form parse (validation net-negative,
    addressed 2026-09-12).
    """
    types, _kind = _parse_body(entry)
    return types


def _shape_of(value: Any, depth: int = 0) -> ShapeNode:
    if isinstance(value, dict):
        if depth >= _SHAPE_MAX_DEPTH:
            return ShapeNode(kind="object", truncated=bool(value))
        keys = list(value.keys())
        truncated = len(keys) > _SHAPE_MAX_KEYS
        children = {k: _shape_of(value[k], depth + 1) for k in keys[:_SHAPE_MAX_KEYS]}
        return ShapeNode(kind="object", children=children, truncated=truncated)
    if isinstance(value, list):
        if depth >= _SHAPE_MAX_DEPTH:
            return ShapeNode(kind="array", truncated=bool(value))
        item = _shape_of(value[0], depth + 1) if value else None
        return ShapeNode(kind="array", item=item)
    if isinstance(value, bool):
        return ShapeNode(kind="boolean")
    if isinstance(value, (int, float)):
        return ShapeNode(kind="number")
    if isinstance(value, str):
        return ShapeNode(kind="string")
    return ShapeNode(kind="null")


def _body_missing(entry: HAREntry) -> bool:
    return entry.response.body_file is not None and entry.response.body is None


def _response_shape(entry: HAREntry) -> ShapeNode | None:
    content_type = (entry.response.content_type or "").lower()
    if "json" not in content_type or not entry.response.body:
        return None

    text = entry.response.body
    if len(text.encode("utf-8", errors="ignore")) > _BODY_SAMPLE_THRESHOLD:
        text = text[:_BODY_SAMPLE_SIZE]
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return _shape_of(parsed)


def _custom_headers(entry: HAREntry) -> tuple[str, ...]:
    names: list[str] = []
    for name in entry.request.headers:
        lowered = name.lower()
        if lowered in _STANDARD_REQUEST_HEADERS:
            continue
        if any(lowered.startswith(p) for p in _STANDARD_REQUEST_HEADER_PREFIXES):
            continue
        names.append(name)
    return tuple(names)


def _response_cookie_names(entry: HAREntry) -> list[str]:
    """Cookie names this response sets: the union of HAR's own
    ``response.cookies`` array and ``response.set_cookie_names``.

    ``set_cookie_names`` is parsed at parse time in ``har/parser.py`` from
    every ``Set-Cookie`` header on the raw response, deduplicated, in
    order; that is the only place header text is read, since a response can
    carry more than one ``Set-Cookie`` header and a header dict collapses
    duplicate names to the last value.
    """
    names: list[str] = []
    for cookie in entry.response.cookies:
        name = cookie.get("name", "")
        if name and name not in names:
            names.append(name)
    for name in entry.response.set_cookie_names:
        if name not in names:
            names.append(name)
    return names


def _has_password_field(entry: HAREntry) -> list[str]:
    """Field names on a POST whose body has a password-like field."""
    field_types = body_params(entry)
    return [
        name for name in field_types if any(hint in name.lower() for hint in _PASSWORD_FIELD_HINTS)
    ]


class _EndpointAccumulator:
    def __init__(self, host: str) -> None:
        self.host = host
        self.methods: list[str] = []
        self.count = 0
        self.statuses: list[int] = []
        self.content_types: dict[str, int] = {}
        self.query_params: dict[str, str] = {}
        self.body_params: dict[str, str] = {}
        self.body_kind: BodyKind = "none"
        self.shape: ShapeNode | None = None
        self.custom_headers: set[str] = set()
        self.examples: list[str] = []

    def record(self, entry: HAREntry, path: str) -> None:
        method = entry.request.method.upper()
        if method not in self.methods:
            self.methods.append(method)
        self.count += 1
        self.statuses.append(entry.response.status)
        content_type = entry.response.content_type or ""
        self.content_types[content_type] = self.content_types.get(content_type, 0) + 1
        self.query_params.update(_query_param_types(entry.request.url))
        field_types, body_kind = _parse_body(entry)
        if field_types:
            self.body_params.update(field_types)
        if body_kind != "none":
            self.body_kind = body_kind
        if self.shape is None:
            self.shape = _response_shape(entry)
        self.custom_headers.update(_custom_headers(entry))
        if path not in self.examples and len(self.examples) < 3:
            self.examples.append(path)

    def finish(self, template: str) -> Endpoint:
        primary_content_type = max(
            self.content_types, key=lambda ct: self.content_types[ct], default=""
        )
        return Endpoint(
            host=self.host,
            template=template,
            methods=tuple(self.methods),
            count=self.count,
            statuses=tuple(sorted(set(self.statuses))),
            content_type=primary_content_type,
            query_params=dict(self.query_params),
            body_params=dict(self.body_params),
            body_kind=self.body_kind,
            shape=self.shape,
            custom_headers=tuple(sorted(self.custom_headers)),
            examples=tuple(self.examples),
        )


def _collapse_eligible(segment: str) -> bool:
    """True when *segment* may stand in for a parameter in a collapsed family.

    ``looks_dynamic`` relaxed by one case: any segment carrying a digit. A
    captured family is often slugs rather than bare ids (``/products/red-widget-
    2024``), which ``looks_dynamic`` rightly refuses on its own but which a run
    of many siblings identifies as a parameter position.
    """
    return looks_dynamic(segment) or any(ch.isdigit() for ch in segment)


def _dynamic_majority(values: set[str]) -> bool:
    """True when more than ``_DYNAMIC_MAJORITY`` of *values* look like identifiers.

    The eligibility half of the collapse rule. Without it, nine word-like
    sibling routes (``/api/orders``, ``/api/products``, ...) are one
    high-cardinality family by count alone and collapse into a single
    ``/api/{api_id}``, hiding eight endpoints from the digest and eight stubs
    from the scaffold.
    """
    eligible = sum(1 for value in values if _collapse_eligible(value))
    return eligible > len(values) * _DYNAMIC_MAJORITY


def _collapse_high_cardinality(templates: list[str]) -> dict[str, str]:
    """Map each raw template to its high-cardinality-collapsed form.

    Best-effort: groups templates sharing a segment count, and within a
    group compares each literal position against the first template's other
    positions to approximate "the same family of endpoints". This is a
    heuristic, not an exhaustive path-family miner: a run whose family has no
    common representative at position 0 is not detected. Adequate for a
    digest tool; documented rather than perfected (Task 3 implementation
    note).

    A position collapses only when it passes both halves of the rule: more
    than ``_HIGH_CARDINALITY_THRESHOLD`` distinct values, and a
    ``_DYNAMIC_MAJORITY`` of those values eligible under ``_collapse_eligible``
    (final fix wave, 2026-09-12).
    """
    by_count: dict[int, list[list[str]]] = {}
    for template in templates:
        segments = template.strip("/").split("/") if template.strip("/") else []
        by_count.setdefault(len(segments), []).append(segments)

    collapse_positions: dict[int, set[int]] = {}
    for count, rows in by_count.items():
        if count <= 1:
            # A single segment has no other position to match, so the family
            # check below is vacuously true for every row; skip it, or nine
            # or more distinct root routes (/orders, /products, ...) would
            # collapse into one {id}.
            continue
        for i in range(count):
            if rows[0][i].startswith("{"):
                continue
            family = [r for r in rows if all(r[j] == rows[0][j] for j in range(count) if j != i)]
            distinct = {r[i] for r in family}
            if len(distinct) > _HIGH_CARDINALITY_THRESHOLD and _dynamic_majority(distinct):
                collapse_positions.setdefault(count, set()).add(i)

    result: dict[str, str] = {}
    for template in templates:
        segments = template.strip("/").split("/") if template.strip("/") else []
        positions = collapse_positions.get(len(segments), set())
        if not positions:
            continue
        new_segments = list(segments)
        for i in sorted(positions):
            if new_segments[i].startswith("{"):
                continue
            prev = new_segments[i - 1] if i > 0 else ""
            new_segments[i] = "{" + param_name_for_segment(prev) + "}"
        new_template = "/" + "/".join(new_segments)
        if new_template != template:
            result[template] = new_template
    return result


def digest(source: DigestSource, *, all_hosts: bool = False) -> RunDigest:
    """Read *source* into a :class:`RunDigest`.

    Never raises on a malformed entry (the parser already records per-entry
    errors) or on a body file the HAR references but that is missing on
    disk; both count under ``dropped["error"]`` and the digest still
    completes.
    """
    parse_result = parse_har_file(source.har_path)
    entries = parse_result.entries
    dropped: dict[DropReason, int] = {
        "static": 0,
        "third_party": 0,
        "error": len(parse_result.errors),
    }

    hosts: dict[str, int] = {}
    non_static_hosts: dict[str, int] = {}
    classified: list[tuple[HAREntry, str, bool]] = []
    for entry in entries:
        host = urlparse(entry.request.url).netloc.lower()
        hosts[host] = hosts.get(host, 0) + 1
        static = _is_static(entry)
        if not static:
            non_static_hosts[host] = non_static_hosts.get(host, 0) + 1
        classified.append((entry, host, static))

    primary_host = max(non_static_hosts, key=lambda h: non_static_hosts[h], default="")
    primary_domain = _registrable_domain(primary_host) if primary_host else ""

    accumulators: dict[tuple[str, str], _EndpointAccumulator] = {}
    login_forms: list[LoginForm] = []
    token_seen: dict[tuple[TokenKind, str], list[str]] = {}
    cookies_seen: dict[str, None] = {}
    login: list[LoginObservation] = []
    credential_post_indexes: list[int] = []
    order = 0

    for index, (entry, host, static) in enumerate(classified):
        if static:
            dropped["static"] += 1
            continue
        in_scope = all_hosts or (_registrable_domain(host) == primary_domain)
        if not in_scope:
            dropped["third_party"] += 1
            continue
        if _body_missing(entry):
            dropped["error"] += 1
            LOG.warning("digest_body_file_missing", url=entry.request.url)
            continue

        path = urlparse(entry.request.url).path or "/"
        method = entry.request.method.upper()
        raw_template, _ = template_path(path)
        key = (method, raw_template)
        acc = accumulators.setdefault(key, _EndpointAccumulator(host=host))
        acc.record(entry, path)

        for name in entry.request.headers:
            if looks_like_token_name(name):
                token_seen.setdefault(("header", name), []).append(f"{method} {raw_template}")
        for cookie in entry.request.cookies + entry.response.cookies:
            cname = cookie.get("name", "")
            if cname and looks_like_token_name(cname):
                token_seen.setdefault(("cookie", cname), []).append(f"{method} {raw_template}")

        if host == primary_host:
            for cookie_name in _response_cookie_names(entry):
                cookies_seen.setdefault(cookie_name, None)

        content_type = (entry.response.content_type or "").lower()
        forms_in_entry: tuple[LoginForm, ...] = ()
        if "html" in content_type and entry.response.body:
            document_source = entry.request.url
            forms_in_entry = extract_login_forms(entry.response.body, source=document_source)
            login_forms.extend(forms_in_entry)
            for candidate in extract_token_candidates(entry.response.body, source=document_source):
                token_key = (candidate.kind, candidate.name)
                token_seen.setdefault(token_key, []).extend(candidate.seen_on)

        credential_hint_fields = _has_password_field(entry) if method == "POST" else []
        kind: ObservationKind | None = None
        fields: tuple[str, ...] = ()
        if method == "GET" and forms_in_entry:
            kind = "form_page"
        elif method == "POST" and credential_hint_fields:
            # Report every body field name, not only the password-hinted
            # ones: a credential post's username/email field is part of the
            # observation too, and the field's own tests require it
            # (deviation from the brief's credential_fields-only draft,
            # documented in task-3-report.md).
            kind, fields = "credential_post", tuple(sorted(body_params(entry)))
        elif credential_post_indexes and index - credential_post_indexes[-1] <= _LOGIN_WINDOW:
            if entry.response.status in _REDIRECT_STATUSES:
                kind = "redirect"
            elif _response_cookie_names(entry):
                kind, fields = "set_cookie", tuple(_response_cookie_names(entry))
        if kind is None and _AUTH_URL_REGEX.search(path):
            kind = "auth_api"

        if kind is not None:
            order += 1
            login.append(
                LoginObservation(
                    order=order,
                    method=method,
                    url=f"{urlparse(entry.request.url).scheme}://{host}{path}",
                    status=entry.response.status,
                    kind=kind,
                    fields=fields,
                )
            )
            if kind == "credential_post":
                credential_post_indexes.append(index)

    if source.page_source is not None and source.page_source.is_file():
        page_html = source.page_source.read_text(encoding="utf-8", errors="replace")
        page_label = str(source.page_source)
        login_forms.extend(extract_login_forms(page_html, source=page_label))
        for candidate in extract_token_candidates(page_html, source=page_label):
            token_seen.setdefault((candidate.kind, candidate.name), []).extend(candidate.seen_on)

    collapse_map = _collapse_high_cardinality([template for _method, template in accumulators])
    merged: dict[tuple[str, str], _EndpointAccumulator] = {}
    for (method, raw_template), acc in accumulators.items():
        final_template = collapse_map.get(raw_template, raw_template)
        merged_key = (method, final_template)
        if merged_key not in merged:
            merged[merged_key] = acc
        else:
            target = merged[merged_key]
            target.count += acc.count
            target.statuses.extend(acc.statuses)
            for ct, n in acc.content_types.items():
                target.content_types[ct] = target.content_types.get(ct, 0) + n
            target.query_params.update(acc.query_params)
            target.body_params.update(acc.body_params)
            target.custom_headers.update(acc.custom_headers)
            for example in acc.examples:
                if example not in target.examples and len(target.examples) < 3:
                    target.examples.append(example)

    endpoints = tuple(
        sorted(
            (acc.finish(template) for (_method, template), acc in merged.items()),
            key=lambda e: (-e.count, e.template),
        )
    )

    tokens = tuple(
        TokenCandidate(kind=kind, name=name, seen_on=tuple(dict.fromkeys(seen_on)))
        for (kind, name), seen_on in token_seen.items()
    )

    return RunDigest(
        source=source,
        primary_host=primary_host,
        hosts=hosts,
        endpoints=endpoints,
        login=tuple(login),
        login_forms=tuple(login_forms),
        tokens=tokens,
        cookies=tuple(cookies_seen),
        dropped=dropped,
    )
