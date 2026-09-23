"""RunDigest: a HAR (and whatever a run adds to it) read into a small, readable model.

Rules are documented on :func:`digest`. The exclusion and auth-path patterns
below replace ``har/analyzer.py``'s, moved here because the digest is the
only remaining consumer (plugin tooling spec, 2026-09-11).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urljoin, urlparse

from graftpunk.har.documents import (
    LoginForm,
    TokenCandidate,
    TokenKind,
    extract_login_forms,
    extract_token_candidates,
    looks_like_token_name,
)
from graftpunk.har.parser import HAREntry, parse_har_file
from graftpunk.har.paths import (
    bare_host,
    bare_url,
    holds_an_id,
    looks_dynamic,
    param_name_for_segment,
    template_path,
)
from graftpunk.logging import get_logger

LOG = get_logger(__name__)

BodyKind = Literal["json", "form", "none"]
ObservationKind = Literal["form_page", "credential_post", "redirect", "set_cookie", "auth_api"]
DropReason = Literal["static", "third_party", "error", "other_scheme"]

__all__ = [
    "BodyKind",
    "DigestSource",
    "DropReason",
    "Endpoint",
    "INTERNAL",
    "LoginForm",
    "LoginObservation",
    "ObservationKind",
    "SHAPE_UNAVAILABLE",
    "RunDigest",
    "ShapeNode",
    "TokenCandidate",
    "TokenKind",
    "body_params",
    "digest",
    "endpoint_template",
    "flagged_names_of",
    "redacted_names_of",
]

INTERNAL = "internal"
"""The dataclass field metadata key marking a field that is a lookup for code, not
part of the digest a reader sees: ``render_json`` skips a field that carries it."""

# Thresholds, every one a named constant (plugin tooling spec, "Rules the digest applies").
# A body over this size that does not parse is reported as "shape unavailable"
# rather than as non-JSON: a capture routinely truncates a body this large, and
# claiming the endpoint returns non-JSON would be false.
_BODY_SAMPLE_THRESHOLD = 256 * 1024
_HIGH_CARDINALITY_THRESHOLD = 8  # a segment with more distinct values than this collapses too
_DYNAMIC_MAJORITY = 0.5  # ... but only when more than this fraction of them look like identifiers
_LOGIN_WINDOW = 20  # entries after a credential post that may carry a redirect/set_cookie
_SHAPE_MAX_DEPTH = 3
_SHAPE_MAX_KEYS = 12
_MAX_ENDPOINT_EXAMPLES = 3  # example paths kept per endpoint, first seen wins
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)

# The only schemes a captured entry can be an endpoint under. A capture taken
# before the first navigation holds the browser's own new-tab page: chrome://,
# chrome-untrusted://, and a data: URL, each of which parses with a netloc that
# is not a host at all (new-tab-page, resources, theme, and an empty string),
# and each of which was counted as a host in the digest (polish round 2,
# 2026-09-12).
_HTTP_SCHEMES = frozenset({"http", "https"})

# Exclusion is three rules against three parts of the URL, never one substring
# search over the whole of it: matched anywhere, "analytics" dropped the primary
# host's own /api/analytics/summary and "static." dropped /static-report
# (polish round 1, 2026-09-12).
_ASSET_EXTENSION_RE = re.compile(
    r"\.(js|css|png|jpg|jpeg|gif|svg|ico|woff|woff2|ttf|eot|map)$", re.IGNORECASE
)
# A third-party tracker or an asset host: these name a host, not a path, so a
# first-party path that happens to spell one of them stays in the digest.
_EXCLUDE_HOST_PATTERNS = [
    r"google-analytics",
    r"googletagmanager",
    r"facebook\.com",
    r"analytics",
    r"tracking",
    r"cdn\.",
    r"static\.",
    r"assets\.",
    r"fonts\.",
]
_EXCLUDE_HOST_REGEX = re.compile("|".join(_EXCLUDE_HOST_PATTERNS), re.IGNORECASE)
# A whole path segment, never a substring of one: /pixel is a tracking pixel,
# /pixelate-image is an endpoint.
_EXCLUDE_PATH_SEGMENTS = frozenset({"pixel", "beacon"})
# A response of one of these main types is an asset whatever its URL says.
_STATIC_MAIN_TYPES = frozenset({"image", "font", "audio", "video"})
# The asset types whose main type is shared with real endpoints (text/* and
# application/* both carry documents and data), so each is listed whole. An
# extension list alone missed a hashed asset with an unusual extension:
# /vendor/custom.<hash>._hs, served as text/hyperscript, became an endpoint, a
# command stub, and a generated test (polish round 2, 2026-09-12).
_STATIC_CONTENT_TYPES = frozenset(
    {
        "text/css",
        "text/javascript",
        "application/javascript",
        "application/x-javascript",
        "application/ecmascript",
        "text/hyperscript",
        "application/wasm",
        "application/font-woff",
        "application/font-woff2",
        "application/vnd.ms-fontobject",
        "image/svg+xml",
    }
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
# Each pattern ends at a segment boundary: as bare substrings they labelled
# /api/tokens/list an auth endpoint (polish round 1, 2026-09-12). Every pattern
# already begins with "/", which anchors the left side.
_AUTH_URL_REGEX = re.compile(
    "|".join(f"(?:{pattern})(?=/|$)" for pattern in _AUTH_URL_PATTERNS), re.IGNORECASE
)

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

_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
_MAX_FIELD_NAME_LEN = 64  # a form field name past this is not a field name
# The character class carries no whitespace, "<", or "{" by construction, so a
# body that is really XML, JSON, or prose cannot present itself as one enormous
# field name (polish round 1, 2026-09-12). A key in the alphabet can still be
# data: _plausible_field_name also refuses one graftpunk.har.paths.holds_an_id
# says carries an account value, the one rule every path segment and name meets.
_FORM_FIELD_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-\[\]]*")


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
    """The shape of one JSON value: its kind, and (for objects/arrays) its children.

    ``kind="unavailable"`` is the one node that is not a JSON value: it marks a
    JSON response whose shape could not be read (see
    :data:`SHAPE_UNAVAILABLE`), distinct from ``shape=None``, which means the
    endpoint does not return JSON at all.
    """

    kind: Literal["object", "array", "string", "number", "boolean", "null", "unavailable"]
    children: dict[str, ShapeNode] | None = None  # objects: key -> node, at most _SHAPE_MAX_KEYS
    item: ShapeNode | None = None  # arrays: the first element's node
    truncated: bool = False  # keys beyond the cap or depth beyond the cap were dropped


SHAPE_UNAVAILABLE = ShapeNode(kind="unavailable")
"""A JSON response too large to have been captured whole: its shape is unknown.

Reporting it as non-JSON would be a false claim about the endpoint, and the
generated docstring said ``Shape: non-JSON.`` for every one of them (polish
round 1, 2026-09-12)."""


@dataclass(frozen=True)
class Endpoint:
    """One (method, templated path) observed one or more times against the primary domain."""

    host: str
    template: str
    # A tuple for hand-built endpoints; digest() keys each endpoint by one
    # (method, template) pair, so every endpoint it produces has exactly one.
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
    # Part of the login flow login_config drives (the login form's GET or the
    # credential POST): the generator renders no stub for it and a command
    # proposal drops it, both from this one flag (graft skill spec, 2026-09-21).
    login_flow: bool = False
    # How many distinct recorded names each position dropped because the name was
    # data, not a field name (graftpunk.har.paths.holds_an_id, or not a field
    # name at all): a stub says so in a GP-FILL comment, so nothing is lost
    # silently. The names themselves are never kept.
    dropped_id_query_keys: int = 0
    dropped_id_body_keys: int = 0
    dropped_id_header_names: int = 0


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
    # where a 3xx sent the client: the target's path, query stripped, on any
    # observation whose own status is a redirect. A credential post that answers
    # 302 carries its landing path here and is never a separate observation, so
    # this is the only record of where a login ended up.
    redirect_to: str = ""


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
    # Each raw template (paths.template_path of an observed path) the
    # high-cardinality collapse re-templated, to the endpoint template it now
    # belongs to; a raw template missing here is its own endpoint's template.
    # Read it through endpoint_template. A lookup, not a finding: its keys are
    # every member path of every collapsed family, far past the capped
    # examples, so it is marked INTERNAL and render_json leaves it out.
    collapsed_templates: dict[str, str] = field(
        default_factory=dict, repr=False, metadata={INTERNAL: True}
    )
    # How many distinct cookie names (``cookies``) and token candidate names
    # (``tokens``, any kind) were left out because the name held an account value
    # (graftpunk.har.paths.holds_an_id). The names are written in no form.
    dropped_id_cookie_names: int = 0
    dropped_id_token_names: int = 0


def _scope_root(primary_host: str) -> str:
    """The domain whose subtree counts as the site, derived from *primary_host*.

    *primary_host* itself when it has two labels or fewer, otherwise
    *primary_host* minus its first label: ``shop.team.example.com`` gives
    ``team.example.com`` and ``www.example.com`` gives ``example.com``. A host
    is in scope when it equals the root or is a subdomain of it
    (:func:`_in_scope`).

    Taking the last two labels instead needs a public suffix list to be
    correct, and without one a site under a two-label public suffix (a
    country-code second-level domain) made every host sharing that suffix a
    first party (polish round 1, 2026-09-12). The parent rule needs no list.
    The residual it accepts: a primary host of the form ``name.<two-label
    public suffix>`` (``mybank.co.uk``) has three labels, so it scopes to the
    bare suffix and every host under that suffix counts as first party for
    that capture. Sites normally record from a ``www`` or ``app`` subdomain,
    which scopes correctly; a bare apex under such a suffix is the one shape
    this rule gets wrong.
    """
    labels = primary_host.split(".")
    return primary_host if len(labels) <= 2 else ".".join(labels[1:])


def _primary_host(non_static_hosts: dict[str, int], document_hosts: set[str]) -> str:
    """The host the run was against: the one that answered the most non-static
    requests, with a host that served an HTML document preferred over one that
    did not. Empty when nothing survived the static rule.

    The document half of the rule decides a case count alone gets wrong. A
    page-driven site answers its own pages and serves the rest of its traffic
    as assets, while a third-party telemetry endpoint answers a handful of
    non-static POSTs; on a real recording the site served three documents and
    an error-reporting host four beacons, so count alone made the beacon host
    primary and the site third party (polish round 2, 2026-09-12). A capture
    with no HTML in it at all (an API-only run) falls back to the count, which
    is what it always was. Ties keep the first host seen, so the run's own
    first request still wins one.
    """
    return max(
        non_static_hosts,
        key=lambda host: (host in document_hosts, non_static_hosts[host]),
        default="",
    )


def _in_scope(host: str, root: str) -> bool:
    """True when *host* is *root* or a subdomain of it."""
    return bool(root) and (host == root or host.endswith(f".{root}"))


def _is_static(entry: HAREntry) -> bool:
    """True when *entry* is an asset, a tracker, or a beacon rather than an endpoint.

    The content-type rule reads the response's type rather than its file
    extension, so an asset served under a hashed name with an extension nobody
    listed is still recognised: its main type is one of
    ``_STATIC_MAIN_TYPES``, or its full type (parameters stripped) is one of
    ``_STATIC_CONTENT_TYPES``. The extension and host rules are unchanged and
    still catch an asset whose response declared no type at all.
    """
    parsed = urlparse(entry.request.url)
    if _ASSET_EXTENSION_RE.search(parsed.path):
        return True
    if _EXCLUDE_HOST_REGEX.search(parsed.netloc):
        return True
    if any(segment.lower() in _EXCLUDE_PATH_SEGMENTS for segment in parsed.path.split("/")):
        return True
    content_type = (entry.response.content_type or "").split(";")[0].strip().lower()
    if not content_type:
        return False
    return content_type.split("/")[0] in _STATIC_MAIN_TYPES or content_type in _STATIC_CONTENT_TYPES


def _observed_type(value: str) -> str:
    """The type of one recorded text value: ``bool``, ``int``, ``float``, or ``str``.

    A value gets a type only when that type's value is sent back spelled exactly
    as recorded, since a generated command sends the typed value: ``07030`` as an
    ``int`` would go out as ``7030``. So ``int`` needs ``str(int(value)) == value``,
    ``float`` a finite float whose ``str`` is *value*, and ``bool`` the lowercase
    ``true`` or ``false`` that ``ctx.request_json`` and ``ctx.request_text`` send
    for a Python bool.
    """
    if value in ("true", "false"):
        return "bool"
    try:
        if str(int(value)) == value:
            return "int"
    except ValueError:
        pass
    try:
        number = float(value)
    except ValueError:
        return "str"
    return "float" if math.isfinite(number) and str(number) == value else "str"


# The type labels a parameter carries (schema 1 of the endpoints projection):
# str, int, float, bool, object (a JSON object), mixed (JSON values no one type
# sends), and list[<element>] with element one of those, list (an array inside
# an array), or unknown (only empty arrays seen).
_MIXED = "mixed"
_UNKNOWN_ELEMENT = "unknown"


def _list_label(element: str) -> str:
    return f"list[{element}]"


def _element_of(label: str) -> str | None:
    """The element label of a ``list[...]`` label, or None for a scalar label."""
    if label.startswith("list[") and label.endswith("]"):
        return label[len("list[") : -1]
    return None


def _merged_text_type(known: str, observed: str) -> str:
    """Query and form values: text on the wire, so any disagreement is ``str``,
    which re-sends every recorded value as it was. A key seen once and repeated
    elsewhere is a list, since a repeatable option can send one value."""
    if known == observed:
        return known
    known_element, observed_element = _element_of(known), _element_of(observed)
    if known_element is None and observed_element is None:
        return "str"
    merged = _merged_text_type(known_element or known, observed_element or observed)
    return _list_label(merged)


def _merged_json_element(known: str, observed: str) -> str:
    if known == observed or observed == _UNKNOWN_ELEMENT:
        return known
    if known == _UNKNOWN_ELEMENT:
        return observed
    if {known, observed} == {"int", "float"}:
        return "float"
    return _MIXED


def _merged_json_type(known: str, observed: str) -> str:
    """JSON values keep their type on the wire: ``int`` and ``float`` merge to
    ``float``, two arrays merge their element types, and any other disagreement
    is ``mixed``, which the generator does not declare."""
    if known == observed:
        return known
    if {known, observed} == {"int", "float"}:
        return "float"
    known_element, observed_element = _element_of(known), _element_of(observed)
    if known_element is not None and observed_element is not None:
        return _list_label(_merged_json_element(known_element, observed_element))
    return _MIXED


def _merged_types(seen: dict[str, str], observed: dict[str, str], *, json_body: bool) -> None:
    """Fold *observed* into *seen* in place, by the JSON rule when *json_body* and
    by the text rule (query and form) otherwise."""
    merge = _merged_json_type if json_body else _merged_text_type
    for name, observed_type in observed.items():
        known = seen.get(name)
        seen[name] = observed_type if known is None else merge(known, observed_type)


def _text_values_type(values: list[str]) -> str:
    """The label of one query or form key's recorded values: the value's type, or
    for a repeated key ``list[<element>]`` with the elements merged as text."""
    if len(values) == 1:
        return _observed_type(values[0])
    element = _observed_type(values[0])
    for value in values[1:]:
        element = _merged_text_type(element, _observed_type(value))
    return _list_label(element)


def _json_value_type(value: Any) -> str | None:
    """The label of one JSON value, or None for ``null``, which is no observation."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, float):
        return "float"
    if isinstance(value, int):
        return "int"
    if isinstance(value, str):
        return "str"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        element = _UNKNOWN_ELEMENT
        for item in value:
            item_type = _json_value_type(item)
            if item_type is None:
                continue
            # An array inside an array is recorded as the bare element ``list``.
            item_element = "list" if _element_of(item_type) is not None else item_type
            element = _merged_json_element(element, item_element)
        return _list_label(element)
    return _MIXED


def _query_param_types(url: str) -> tuple[dict[str, str], set[str]]:
    """*url*'s query parameter names and observed types, and the keys dropped
    because they do not read as a field name (:func:`_plausible_field_name`)."""
    query = urlparse(url).query
    if not query:
        return {}, set()
    parsed = parse_qs(query, keep_blank_values=True)
    types: dict[str, str] = {}
    dropped: set[str] = set()
    for name, values in parsed.items():
        if not _plausible_field_name(name):
            dropped.add(name)
            continue
        types[name] = _text_values_type(values)
    return types, dropped


def _field_name_shaped(name: str) -> bool:
    """True when *name* is spelled like a field name (the field-name alphabet, no
    longer than ``_MAX_FIELD_NAME_LEN``): what tells a form body from body text."""
    return len(name) <= _MAX_FIELD_NAME_LEN and bool(_FORM_FIELD_NAME_RE.fullmatch(name))


def _plausible_field_name(name: str) -> bool:
    """True when *name* reads as a field name rather than as body text or an id: a
    key that does not match the field-name alphabet (an email address, a key
    starting with a digit) is not one, and neither is a key that
    :func:`graftpunk.har.paths.holds_an_id` says carries an account value
    (``u_40912873``, ``cus_NffrFeUfNV2Hib``)."""
    return _field_name_shaped(name) and not holds_an_id(name)


def _declared_request_content_type(entry: HAREntry) -> str:
    """What the request said its body was: the ``Content-Type`` request header,
    or HAR's own ``postData.mimeType`` when the capture carries no such header.
    Empty when the request declared neither."""
    for name, value in entry.request.headers.items():
        if name.lower() == "content-type":
            return value.split(";")[0].strip().lower()
    declared = entry.request.post_data_mime_type or ""
    return declared.split(";")[0].strip().lower()


def _json_body_types(parsed: dict[str, Any]) -> tuple[dict[str, str], set[str]]:
    """*parsed*'s field names and observed types, and the keys dropped by the rule
    every key is held to (:func:`_plausible_field_name`): an email address, a key
    starting with a digit, or a key holding an id (``acct_40912873``).

    A ``null`` value is no type observation, so a field seen only as ``null`` is
    left out."""
    types: dict[str, str] = {}
    dropped: set[str] = set()
    for key, value in parsed.items():
        if not _plausible_field_name(key):
            dropped.add(key)
            continue
        observed = _json_value_type(value)
        if observed is not None:
            types[key] = observed
    return types, dropped


def _parse_body(entry: HAREntry) -> tuple[dict[str, str], BodyKind]:
    """The request body's field names and observed types, and which kind it was
    (:func:`_parse_body_keys` without the dropped keys)."""
    types, kind, _dropped = _parse_body_keys(entry)
    return types, kind


def _parse_body_keys(entry: HAREntry) -> tuple[dict[str, str], BodyKind, set[str]]:
    """The request body's field names and observed types, which kind it was, and
    the keys dropped because they are data rather than field names.

    A body is read as a form only when it really looks like one: the request
    declared no content type other than ``application/x-www-form-urlencoded``,
    the text carries an ``=``, and every key ``parse_qs`` returns is a plausible
    field name. ``parse_qs`` returns the whole text as a single key for anything
    else, so falling through to it put an XML credential post's entire body
    (values included) into ``Endpoint.body_params``, the rendered digest, the
    fixtures sidecar, and generated plugin source (polish round 1, 2026-09-12).

    A JSON array or scalar is still a JSON body; it just has no field names.
    """
    post_data = entry.request.post_data
    if not post_data:
        return {}, "none", set()

    try:
        parsed = json.loads(post_data)
    except (ValueError, TypeError):
        pass
    else:
        if isinstance(parsed, dict):
            types, dropped = _json_body_types(parsed)
            return types, "json", dropped
        return {}, "json", set()

    declared = _declared_request_content_type(entry)
    if declared and declared != _FORM_CONTENT_TYPE:
        return {}, "none", set()
    if "=" not in post_data:
        return {}, "none", set()
    form = parse_qs(post_data, keep_blank_values=True)
    if not form or not all(_field_name_shaped(name) for name in form):
        return {}, "none", set()
    # Still a form when a key holds an id; that key alone is dropped.
    types = {k: _text_values_type(v) for k, v in form.items() if not holds_an_id(k)}
    return types, "form", {k for k in form if holds_an_id(k)}


def body_params(entry: HAREntry) -> dict[str, str]:
    """Request body field names to their observed type: JSON object fields,
    or form-encoded fields when the body is a form. Empty when there is no
    body, and empty for every other shape (a JSON array or scalar, XML, plain
    text), which has no field names to report.

    The one public entry point for "what are this entry's body param
    names": ``digest()``'s endpoint accumulation and the fixtures command's
    sidecar (``cli/observe_commands.py``) both call this instead of
    reimplementing the JSON-then-form parse (validation net-negative,
    addressed 2026-09-12).
    """
    types, _kind = _parse_body(entry)
    return types


_ID_KEY = "{key}"


def _shape_of(value: Any, depth: int = 0) -> ShapeNode:
    if isinstance(value, dict):
        if depth >= _SHAPE_MAX_DEPTH:
            return ShapeNode(kind="object", truncated=bool(value))
        # A key holding an id (a map keyed by account or order ids) is data, not
        # a field: every such key becomes one "{key}", whose shape is the first
        # such sibling's.
        children: dict[str, ShapeNode] = {}
        for key, child in value.items():
            name = _ID_KEY if holds_an_id(key) else key
            if name not in children:
                children[name] = _shape_of(child, depth + 1)
        truncated = len(children) > _SHAPE_MAX_KEYS
        kept = dict(list(children.items())[:_SHAPE_MAX_KEYS])
        return ShapeNode(kind="object", children=kept, truncated=truncated)
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
    """The shape of *entry*'s JSON response body, ``SHAPE_UNAVAILABLE`` when a
    body over ``_BODY_SAMPLE_THRESHOLD`` does not parse, or ``None`` when the
    endpoint does not return JSON.

    The body is parsed whole; it is already in memory. Parsing a fixed-size
    prefix of it could never succeed, so every large body reported non-JSON and
    the generated docstring said so (polish round 1, 2026-09-12).
    """
    content_type = (entry.response.content_type or "").lower()
    if "json" not in content_type or not entry.response.body:
        return None

    text = entry.response.body
    try:
        parsed = json.loads(text)
    except ValueError:
        if len(text.encode("utf-8", errors="ignore")) > _BODY_SAMPLE_THRESHOLD:
            return SHAPE_UNAVAILABLE
        return None
    return _shape_of(parsed)


def _custom_headers(entry: HAREntry) -> tuple[tuple[str, ...], set[str]]:
    """*entry*'s non-standard request header names, and the ones dropped because
    the name holds an id (:func:`graftpunk.har.paths.holds_an_id`)."""
    names: list[str] = []
    dropped: set[str] = set()
    for name in entry.request.headers:
        lowered = name.lower()
        if lowered in _STANDARD_REQUEST_HEADERS:
            continue
        if any(lowered.startswith(p) for p in _STANDARD_REQUEST_HEADER_PREFIXES):
            continue
        if holds_an_id(name):
            dropped.add(name)
            continue
        names.append(name)
    return tuple(names), dropped


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


def _redirect_target_path(entry: HAREntry) -> str:
    """The path a 3xx response sent the client to, query stripped.

    Empty for any response that is not a redirect, and for a redirect whose
    recorder kept neither ``redirectURL`` nor a ``Location`` header. A relative
    target is resolved against the request's own URL, so a ``Location: /dashboard``
    and an absolute one both reduce to ``/dashboard``.
    """
    if entry.response.status not in _REDIRECT_STATUSES:
        return ""
    target = entry.response.redirect_url
    if not target:
        return ""
    try:
        # Through bare_url, the one rule for a URL the digest keeps: it also masks
        # an email segment, which bare_path alone does not.
        return urlparse(bare_url(urljoin(entry.request.url, target))).path
    except ValueError:
        # A target urljoin cannot split (an unclosed IPv6 bracket) names no path.
        LOG.warning("digest_redirect_unparseable", url=bare_url(entry.request.url))
        return ""


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
        # Dropped names, held only to count them distinctly; never kept past finish.
        self.dropped_query: set[str] = set()
        self.dropped_body: set[str] = set()
        self.dropped_headers: set[str] = set()

    def record(self, entry: HAREntry, path: str) -> None:
        method = entry.request.method.upper()
        if method not in self.methods:
            self.methods.append(method)
        self.count += 1
        self.statuses.append(entry.response.status)
        content_type = entry.response.content_type or ""
        self.content_types[content_type] = self.content_types.get(content_type, 0) + 1
        query_types, query_dropped = _query_param_types(entry.request.url)
        _merged_types(self.query_params, query_types, json_body=False)
        self.dropped_query |= query_dropped
        field_types, body_kind, body_dropped = _parse_body_keys(entry)
        _merged_types(self.body_params, field_types, json_body=body_kind == "json")
        self.dropped_body |= body_dropped
        if body_kind != "none":
            self.body_kind = body_kind
        # A real shape supersedes an unavailable one: within a family the first
        # member that was captured whole answers for the rest.
        if self.shape is None or self.shape == SHAPE_UNAVAILABLE:
            observed = _response_shape(entry)
            if observed is not None:
                self.shape = observed
        header_names, headers_dropped = _custom_headers(entry)
        self.custom_headers.update(header_names)
        self.dropped_headers |= headers_dropped
        if path not in self.examples and len(self.examples) < _MAX_ENDPOINT_EXAMPLES:
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
            dropped_id_query_keys=len(self.dropped_query),
            dropped_id_body_keys=len(self.dropped_body),
            dropped_id_header_names=len(self.dropped_headers),
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


def _family_key(segments: list[str], position: int) -> tuple[str, ...]:
    """The segments of *segments* other than *position*: what makes two paths
    members of the same endpoint family for the collapse rule."""
    return tuple(segment for index, segment in enumerate(segments) if index != position)


def _collapse_high_cardinality(templates: list[str]) -> dict[str, str]:
    """Map each raw template to its high-cardinality-collapsed form.

    A family is the set of templates of one segment count that agree on every
    segment but one; that one position collapses when it passes both halves of
    the rule: more than ``_HIGH_CARDINALITY_THRESHOLD`` distinct values across
    the family, and a ``_DYNAMIC_MAJORITY`` of those values eligible under
    ``_collapse_eligible``.

    A qualifying family names the position and its own key, and only a template
    belonging to that family, whose own segment there is eligible, is
    re-templated. Keyed by segment count alone, one slug family turned every
    sibling route of the same depth into a parameter (``/account/profile`` and
    ``/account/settings`` merged into ``/account/{account_id}``) (polish round
    1, 2026-09-12).
    """
    by_count: dict[int, list[list[str]]] = {}
    for template in templates:
        segments = template.strip("/").split("/") if template.strip("/") else []
        by_count.setdefault(len(segments), []).append(segments)

    # segment count -> the (position, family key) pairs that qualify.
    collapse_families: dict[int, set[tuple[int, tuple[str, ...]]]] = {}
    for count, rows in by_count.items():
        if count <= 1:
            # A single segment has no other position to match, so every row
            # would share one empty family key; skip it, or nine or more
            # distinct root routes (/orders, /products, ...) would collapse
            # into one {id}.
            continue
        for position in range(count):
            families: dict[tuple[str, ...], set[str]] = {}
            for row in rows:
                if row[position].startswith("{"):
                    continue
                families.setdefault(_family_key(row, position), set()).add(row[position])
            for key, distinct in families.items():
                if len(distinct) > _HIGH_CARDINALITY_THRESHOLD and _dynamic_majority(distinct):
                    collapse_families.setdefault(count, set()).add((position, key))

    result: dict[str, str] = {}
    for template in templates:
        stripped = template.strip("/")
        segments = stripped.split("/") if stripped else []
        families_here = collapse_families.get(len(segments), set())
        if not families_here:
            continue
        new_segments = list(segments)
        for position, key in sorted(families_here):
            if new_segments[position].startswith("{"):
                continue
            if _family_key(segments, position) != key:
                continue
            if not _collapse_eligible(segments[position]):
                continue
            prev = new_segments[position - 1] if position > 0 else ""
            new_segments[position] = "{" + param_name_for_segment(prev) + "}"
        # A trailing slash is preserved the way paths.template_path preserves
        # it, so a collapsed template still names the same route as the one
        # the accumulator was keyed on (polish round 1, 2026-09-12).
        trailing = "/" if len(template) > 1 and template.endswith("/") else ""
        new_template = "/" + "/".join(new_segments) + trailing
        if new_template != template:
            result[template] = new_template
    return result


_LOGIN_FLOW_KINDS: tuple[ObservationKind, ...] = ("form_page", "credential_post")


def _template_covers_path(template: str, path: str) -> bool:
    """True when *path* is one of the paths *template* stands for: the same number
    of segments, each of the template's either a ``{placeholder}`` or that segment
    spelled exactly."""
    template_segments = template.strip("/").split("/") if template.strip("/") else []
    path_segments = path.strip("/").split("/") if path.strip("/") else []
    if len(template_segments) != len(path_segments):
        return False
    return all(
        (segment.startswith("{") and segment.endswith("}")) or segment == observed
        for segment, observed in zip(template_segments, path_segments, strict=True)
    )


def _login_flow_pairs(
    login: tuple[LoginObservation, ...], endpoints: tuple[Endpoint, ...]
) -> set[tuple[str, str]]:
    """The ``(method, template)`` pairs ``login_config`` owns, as endpoints are keyed.

    The login form's own GET and the credential POST are the login flow. An
    observation carries the raw path, and the endpoint it belongs to may have
    been re-templated by the high-cardinality collapse, so each observation
    claims every endpoint of its method whose final template covers its path,
    plus its own templated path for a run whose login flow produced no endpoint
    (moved from ``devtools/scaffold/render.py``).
    """
    owned: set[tuple[str, str]] = set()
    for observation in login:
        if observation.kind not in _LOGIN_FLOW_KINDS:
            continue
        method = observation.method.upper()
        path = urlparse(observation.url).path or "/"
        template, _ = template_path(path)
        owned.add((method, template))
        for endpoint in endpoints:
            if method in endpoint.methods and _template_covers_path(endpoint.template, path):
                owned.add((method, endpoint.template))
    return owned


def _with_login_flow(
    endpoints: tuple[Endpoint, ...], login: tuple[LoginObservation, ...]
) -> tuple[Endpoint, ...]:
    """*endpoints* with ``login_flow`` set on each one that has a method and every
    method of which the login flow owns; an endpoint with no method owns nothing."""
    owned = _login_flow_pairs(login, endpoints)
    return tuple(
        replace(
            endpoint,
            login_flow=bool(endpoint.methods)
            and all((m, endpoint.template) in owned for m in endpoint.methods),
        )
        for endpoint in endpoints
    )


def digest(source: DigestSource, *, all_hosts: bool = False) -> RunDigest:
    """Read *source* into a :class:`RunDigest`.

    An entry whose URL scheme is neither ``http`` nor ``https`` is dropped
    first, under ``dropped["other_scheme"]``, and never reaches the host
    counts or any later rule.

    Never raises on a malformed entry (the parser already records per-entry
    errors), on a request URL ``urlparse`` cannot split, or on a body file the
    HAR references but that is missing on disk; each counts under
    ``dropped["error"]`` and the digest still completes. A redirect target or
    a form action that cannot be split is recorded as empty.
    """
    parse_result = parse_har_file(source.har_path)
    entries = parse_result.entries
    dropped: dict[DropReason, int] = {
        "static": 0,
        "third_party": 0,
        "error": len(parse_result.errors),
        "other_scheme": 0,
    }

    hosts: dict[str, int] = {}
    non_static_hosts: dict[str, int] = {}
    document_hosts: set[str] = set()
    classified: list[tuple[HAREntry, str, bool]] = []
    for index, entry in enumerate(entries):
        try:
            parsed_url = urlparse(entry.request.url)
        except ValueError:
            # urlparse refuses a URL it cannot split (an unclosed IPv6 bracket).
            # Every later parse of this entry's URL would raise too, so it goes
            # no further. The URL itself is not logged: it could not be reduced.
            dropped["error"] += 1
            LOG.warning("digest_url_unparseable", entry_index=index)
            continue
        # Before every other classification: a non-HTTP entry has no host to
        # count and no endpoint to derive.
        if parsed_url.scheme.lower() not in _HTTP_SCHEMES:
            dropped["other_scheme"] += 1
            continue
        host = bare_host(parsed_url.netloc).lower()
        hosts[host] = hosts.get(host, 0) + 1
        static = _is_static(entry)
        if not static:
            non_static_hosts[host] = non_static_hosts.get(host, 0) + 1
            # Only a page that was actually served counts as a document: a
            # third-party host answering one HTML error page must not join
            # the document group and then win the election on count.
            served_html = "html" in (entry.response.content_type or "").lower()
            if served_html and 200 <= entry.response.status < 300:
                document_hosts.add(host)
        classified.append((entry, host, static))

    primary_host = _primary_host(non_static_hosts, document_hosts)
    scope_root = _scope_root(primary_host) if primary_host else ""

    accumulators: dict[tuple[str, str], _EndpointAccumulator] = {}
    login_forms: list[LoginForm] = []
    token_seen: dict[tuple[TokenKind, str], list[str]] = {}
    cookies_seen: dict[str, None] = {}
    id_cookie_names: set[str] = set()
    login: list[LoginObservation] = []
    credential_post_indexes: list[int] = []
    order = 0

    for index, (entry, host, static) in enumerate(classified):
        if static:
            dropped["static"] += 1
            continue
        in_scope = all_hosts or _in_scope(host, scope_root)
        if not in_scope:
            dropped["third_party"] += 1
            continue
        if _body_missing(entry):
            dropped["error"] += 1
            LOG.warning("digest_body_file_missing", url=bare_url(entry.request.url))
            continue

        # Path only: every URL the digest keeps goes through paths.bare_url, so
        # no query, fragment, ;params, or userinfo reaches an example, a
        # template, a login observation, a form source, or a token's seen_on.
        url = bare_url(entry.request.url)
        path = urlparse(url).path or "/"
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

        # Every host reaching this point already passed the in-scope check
        # above, not just the primary one: a second first-party host (a
        # cookie-setting auth subdomain, say) must still show up in
        # flagged_names, which reads this field.
        for cookie_name in _response_cookie_names(entry):
            if holds_an_id(cookie_name):
                id_cookie_names.add(cookie_name)
                continue
            cookies_seen.setdefault(cookie_name, None)

        content_type = (entry.response.content_type or "").lower()
        forms_in_entry: tuple[LoginForm, ...] = ()
        if "html" in content_type and entry.response.body:
            document_source = url
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
                kind = "set_cookie"
                fields = tuple(n for n in _response_cookie_names(entry) if not holds_an_id(n))
        if kind is None and _AUTH_URL_REGEX.search(path):
            kind = "auth_api"

        if kind is not None:
            order += 1
            login.append(
                LoginObservation(
                    order=order,
                    method=method,
                    url=url,
                    status=entry.response.status,
                    kind=kind,
                    fields=fields,
                    redirect_to=_redirect_target_path(entry),
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
            _merged_types(target.query_params, acc.query_params, json_body=False)
            json_body = "json" in (target.body_kind, acc.body_kind)
            _merged_types(target.body_params, acc.body_params, json_body=json_body)
            target.custom_headers.update(acc.custom_headers)
            target.dropped_query |= acc.dropped_query
            target.dropped_body |= acc.dropped_body
            target.dropped_headers |= acc.dropped_headers
            # The first member of a collapsed family answers for the family, so
            # a member that happened to redirect or return HTML must not cost
            # the merged endpoint its response shape or its request body kind
            # (final fix wave, 2026-09-12).
            if (
                target.shape is None or target.shape == SHAPE_UNAVAILABLE
            ) and acc.shape is not None:
                target.shape = acc.shape
            if target.body_kind == "none" and acc.body_kind != "none":
                target.body_kind = acc.body_kind
            for example in acc.examples:
                if example not in target.examples and len(target.examples) < _MAX_ENDPOINT_EXAMPLES:
                    target.examples.append(example)

    endpoints = tuple(
        sorted(
            (acc.finish(template) for (_method, template), acc in merged.items()),
            key=lambda e: (-e.count, e.template),
        )
    )

    # A token name holding an id is left out and counted, never written.
    tokens = tuple(
        TokenCandidate(kind=kind, name=name, seen_on=tuple(dict.fromkeys(seen_on)))
        for (kind, name), seen_on in token_seen.items()
        if not holds_an_id(name)
    )
    id_token_names = {key for key in token_seen if holds_an_id(key[1])}

    return RunDigest(
        source=source,
        primary_host=primary_host,
        hosts=hosts,
        endpoints=_with_login_flow(endpoints, tuple(login)),
        login=tuple(login),
        login_forms=tuple(login_forms),
        tokens=tokens,
        cookies=tuple(cookies_seen),
        dropped=dropped,
        collapsed_templates=collapse_map,
        dropped_id_cookie_names=len(id_cookie_names),
        dropped_id_token_names=len(id_token_names),
    )


def endpoint_template(d: RunDigest, path: str) -> str:
    """The template *d* files a request for *path* under: the path templated by
    :func:`graftpunk.har.paths.template_path`, then carried through the digest's
    own high-cardinality collapse, so twelve product slugs answer
    ``/products/{product_id}`` exactly as the digest prints the endpoint. The
    method does not enter into it: the collapse is decided per path.
    """
    raw, _ = template_path(path)
    return d.collapsed_templates.get(raw, raw)


def flagged_names_of(d: RunDigest, entries: Iterable[HAREntry] = ()) -> tuple[str, ...]:
    """The names a committed fixture must never contain, sorted and deduplicated:
    every cookie name the digest recorded on any in-scope host (not the primary
    host alone: a second first-party host can set its own session cookie), every
    token candidate's name, and every cookie name any of *entries* sets, whatever
    its host and whether or not the digest counted it as static.

    ``d.cookies`` stays the digest's own scoped list; *entries* widens only this
    safety net. ``gp observe fixtures`` passes every entry in the HAR, since a
    ``--match`` glob can write a capture from a static response or an
    out-of-scope host, and a cookie that entry set must still be looked for. It
    lives beside the digest that records the names, so the sidecar writer takes
    plain strings and imports nothing from the digest.

    A name that holds an account value (graftpunk.har.paths.holds_an_id) is not
    among them, in any form: :func:`redacted_names_of` counts those."""
    entry_cookies = {
        name for entry in entries for name in _response_cookie_names(entry) if not holds_an_id(name)
    }
    return tuple(sorted(set(d.cookies) | {token.name for token in d.tokens} | entry_cookies))


def redacted_names_of(d: RunDigest, entries: Iterable[HAREntry] = ()) -> int:
    """How many names :func:`flagged_names_of` leaves out because they hold an
    account value: the distinct cookie names any of *entries* sets that do, plus
    the token candidate names the digest dropped (a cookie that is also a token
    candidate counts once as each). The fixture sidecar records this count, never
    the names."""
    entry_id_cookies = {
        name for entry in entries for name in _response_cookie_names(entry) if holds_an_id(name)
    }
    return len(entry_id_cookies) + d.dropped_id_token_names
