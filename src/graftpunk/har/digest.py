"""RunDigest: a HAR (and whatever a run adds to it) read into a small, readable model.

Rules are documented on :func:`digest`. The exclusion and auth-path patterns
below replace ``har/analyzer.py``'s, moved here because the digest is the
only remaining consumer (plugin tooling spec, 2026-09-11).
"""

from __future__ import annotations

import json
import math
import re
import string
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, get_args
from urllib.parse import parse_qs, unquote, unquote_plus, urljoin, urlparse, urlunparse
from urllib.parse import quote as percent_encode

from graftpunk.har.documents import (
    PASSWORD_NAME_HINTS,
    LoginForm,
    TokenCandidate,
    TokenKind,
    extract_login_forms,
    extract_token_candidates,
    form_action_targets,
    looks_like_new_password_name,
    looks_like_token_name,
)
from graftpunk.har.inline_state import inline_json
from graftpunk.har.login_signals import (
    account_needles,
    asks_for_a_code,
    could_be_an_account_value,
    holds_an_account_value,
    is_login_step_post,
    success_candidates,
)
from graftpunk.har.naming import capture_text, fixture_rank, normalize_media_type
from graftpunk.har.parser import HAREntry, LoginInteraction, cookie_header_pairs, parse_har_file
from graftpunk.har.paths import (
    bare_host,
    bare_path,
    bare_url,
    family_param_name,
    holds_an_id,
    is_placeholder,
    keys_are_ids,
    looks_dynamic,
    normal_host,
    param_name_for_segment,
    reads_as_cased_words,
    reads_as_words,
    split_parts,
    template_path,
)
from graftpunk.har.selectors import bounded_selector, selector_holds_an_id, selector_keys
from graftpunk.logging import get_logger
from graftpunk.plugins.login_settle import failure_text_on_lowered_page
from graftpunk.session_identity import identifier_key, is_identifier_field

LOG = get_logger(__name__)

BodyKind = Literal["json", "form", "none"]
BodyLayout = Literal["json", "text_plain", "multipart", "form", "none"]
ObservationKind = Literal["form_page", "credential_post", "redirect", "set_cookie", "auth_api"]
DropReason = Literal["static", "third_party", "error", "other_scheme"]

__all__ = [
    "BodyKind",
    "BodyLayout",
    "DigestSource",
    "DropReason",
    "Endpoint",
    "INTERNAL",
    "InlineJson",
    "LoginForm",
    "OMIT_EMPTY",
    "LoginObservation",
    "ObservationKind",
    "SHAPE_UNAVAILABLE",
    "RunDigest",
    "ShapeNode",
    "TokenCandidate",
    "TokenKind",
    "body_layout",
    "body_params",
    "body_values",
    "digest",
    "is_credential_body",
    "multipart_delimiter",
    "password_field_names",
    "post_account_values",
    "post_every_value",
    "post_identifier_values",
    "posted_account_values",
    "endpoint_template",
    "family_values",
    "FLAGGED_REASONS",
    "FlaggedReason",
    "flagged_names_of",
    "flagged_reasons_of",
    "flagged_values_of",
    "form_state",
    "redacted_names_of",
]

INTERNAL = "internal"
"""The dataclass field metadata key marking a field that is a lookup for code, not
part of the digest a reader sees: ``render_json`` skips a field that carries it."""

OMIT_EMPTY = "omit_empty"
"""The dataclass field metadata key marking a field ``render_json`` leaves out while
it holds an empty value: a field added for a finding most runs do not have, so a
run without that finding renders as it did before the field existed."""

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
# and each of which was counted as a host in the digest.
_HTTP_SCHEMES = frozenset({"http", "https"})

# Exclusion is three rules against three parts of the URL, never one substring
# search over the whole of it: matched anywhere, "analytics" dropped the primary
# host's own /api/analytics/summary and "static." dropped /static-report.
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
# command stub, and a generated test.
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
# /api/tokens/list an auth endpoint. Every pattern
# already begins with "/", which anchors the left side.
_AUTH_URL_REGEX = re.compile(
    "|".join(f"(?:{pattern})(?=/|$)" for pattern in _AUTH_URL_PATTERNS), re.IGNORECASE
)
# A logout's path, on the same segment-boundary rule: a redirect chain through one
# never names the page a login opens at (LoginForm.opened_from).
_LOGOUT_URL_REGEX = re.compile(r"/(?:log[-_]?out|sign[-_]?out|log[-_]?off)(?=/|$)", re.IGNORECASE)

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


_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
_MAX_FIELD_NAME_LEN = 64  # a form field name past this is not a field name
# The character class carries no whitespace, "<", or "{" by construction, so a
# body that is really XML, JSON, or prose cannot present itself as one enormous
# field name. A key in the alphabet can still be
# data: _plausible_field_name also refuses one graftpunk.har.paths.holds_an_id
# says carries an account value, the one rule every path segment and name meets.
# "$" is admitted, leading and inner: OData spells query keys $filter and $top,
# and ASP.NET WebForms names its fields ctl00$Main$txtSearch.
_FORM_FIELD_NAME_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_.\-\[\]$]*")


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
        # A page source that resolves outside the run (a symlink) is not read, as
        # the HAR parser reads no body file outside the HAR's directory.
        inside = page_source.resolve().is_relative_to(run_dir.resolve())
        if not inside:
            LOG.warning("digest_page_source_outside_run_dir", run_id=run_id)
        return cls(
            har_path=run_dir / "network.har",
            bodies_dir=bodies_dir if bodies_dir.is_dir() else None,
            page_source=page_source if inside and page_source.is_file() else None,
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


@dataclass(frozen=True)
class InlineJson:
    """The inline JSON state an HTML page carries (:mod:`graftpunk.har.inline_state`):
    the target it is assigned to as written, a part holding an id shown as a
    placeholder (``window.App.props``, ``script#__NEXT_DATA__``), and the object's
    shape, read by the rule a JSON body's is. Never a value."""

    target: str
    shape: ShapeNode


SHAPE_UNAVAILABLE = ShapeNode(kind="unavailable")
"""A JSON response too large to have been captured whole: its shape is unknown.

Reporting it as non-JSON would be a false claim about the endpoint, and the
generated docstring said ``Shape: non-JSON.`` for every one of them."""


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
    # The most-recorded response media type across every recording the digest kept
    # for this endpoint (graftpunk.har.naming.normalize_media_type), whatever its
    # body: the docstring's summary, and the stub's JSON/text fallback for an
    # endpoint with no fixture recording at all (fixture_content_type empty). Not
    # necessarily the fixture's own type: fixture_content_type is that.
    content_type: str
    query_params: dict[str, str]
    body_params: dict[str, str]
    body_kind: BodyKind
    # The fixture recording's own JSON shape (fixture_content_type's recording,
    # graftpunk.har.naming.fixture_order's rule): None when that recording is not
    # JSON, or there is no fixture. The docstring's "Shape:" line reads this, so it
    # never claims a shape from a recording the generated test does not read.
    shape: ShapeNode | None
    custom_headers: tuple[str, ...]
    examples: tuple[str, ...]
    # Part of the login flow login_config drives (the login form's GET or the
    # credential POST): the generator renders no stub for it and a command
    # proposal drops it, both from this one flag (graft skill spec, 2026-09-21).
    login_flow: bool = False
    # How many distinct recorded names each position dropped, by cause: the name
    # holds an account value (graftpunk.har.paths.holds_an_id), or it is not a
    # field name at all (it starts with a digit, holds a character outside the
    # field-name alphabet, or is longer than 64 characters). A stub says so in a
    # GP-FILL comment per cause, so nothing is lost silently. The names themselves
    # are never kept.
    query_keys_dropped_as_ids: int = 0
    query_keys_dropped_as_non_names: int = 0
    body_keys_dropped_as_ids: int = 0
    body_keys_dropped_as_non_names: int = 0
    header_names_dropped_as_ids: int = 0
    # How many recordings sent a body the recorder could not capture
    # (HARRequest.post_data_not_captured): body_params holds only what the other
    # recordings showed, so the digest and a stub say the body was not captured
    # rather than present a request with no or partial parameters as complete.
    body_not_captured: int = 0
    # How many recordings sent a body the recorder left out on purpose
    # (HARRequest.post_data_omitted): over the capture's max body size, or not
    # UTF-8, with no bodies/ directory to write it to. Counted apart from
    # body_not_captured, so the digest and a stub can say the capture had no
    # bodies/ directory to keep it in.
    body_omitted: int = 0
    # How many recordings sent a body the recorder kept in bodies/ that is not
    # text (HARRequest.post_data_binary), and the first such file, relative to
    # the run directory, with its size in bytes when the HAR recorded one: the
    # digest and a stub point at it, since no field names can be read from it.
    body_binary: int = 0
    body_binary_file: str = ""
    body_binary_size: int | None = None
    # Every recorded response to this endpoint had no body (a redirect, a 204): a
    # generated test asserts the call completed, since an empty body is falsy.
    response_body_empty: bool = False
    # The fixture recording's body when it is JSON parsing to a falsy value ({},
    # [], "", 0, false, or null), spelled as canonical JSON; None otherwise, an
    # empty body included. The generated test reads that fixture, so it asserts
    # this value.
    falsy_first_response: str | None = None
    # The fixture recording's own media type, normalised
    # (graftpunk.har.naming.normalize_media_type; UNNAMED_CONTENT_TYPE when it
    # named none): among the media types of the recordings the digest kept for
    # this endpoint (in scope, not static, and graftpunk.har.naming.capture_text
    # not None), the most-recorded one that has a recording with a body, a count
    # tie going to a JSON type and then to the type whose winning recording came
    # first; only when no type has a body at all does the same rule run over
    # every recorded type. Empty when fixture_written is False (no such
    # recording). The stub's request method, the fixture's file name, and the
    # falsy decision all read this one recording. A lookup for the generator, so
    # render_json leaves it out.
    fixture_content_type: str = field(default="", metadata={INTERNAL: True})
    # Whether gp observe fixtures writes a fixture for any recording
    # (graftpunk.har.naming.capture_text): the generator writes no test for an
    # endpoint it writes none for. Internal.
    fixture_written: bool = field(default=True, metadata={INTERNAL: True})
    # The fixture recording's position in the run's HAR entries (parse_har_file's
    # own order), the stable reference gp observe fixtures matches against its own
    # entries to write exactly that recording unsuffixed, rather than re-deriving
    # "which recording is first" by content type and risking a pick the digest
    # never made (an out-of-scope or a static entry sharing the same method and
    # template). None when fixture_written is False.
    fixture_entry_index: int | None = field(default=None, metadata={INTERNAL: True})
    # The literal templates this endpoint stands for when it is a family row
    # (_family_endpoints): two or more recorded paths that differ only in one
    # letters-only segment, which this template holds as the placeholder
    # family_placeholder names. Empty on every other endpoint, and then left out of
    # render_json. A family row is a suggestion beside its members' own rows, which
    # stay exactly as they are.
    family_members: tuple[str, ...] = field(default=(), metadata={OMIT_EMPTY: True})
    # The family's placeholder name (graftpunk.har.paths.family_param_name), and the
    # varying segment's value in the member whose recording is the family's fixture:
    # the value a generated test passes, so the request it issues is a recorded one.
    # Lookups for the generator, so render_json leaves them out.
    family_placeholder: str = field(default="", metadata={INTERNAL: True})
    family_fixture_value: str = field(default="", metadata={INTERNAL: True})
    # The inline JSON state the fixture recording carries when it is an HTML page
    # (_inline_json_of), read from the same recording as shape, so it describes the
    # page the generated test reads. None otherwise, and then left out of
    # render_json, so a run without one renders as it did before the field existed.
    inline_json: InlineJson | None = field(default=None, metadata={OMIT_EMPTY: True})


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
    # Part of the login the generator uses: the promoted page of the selected login
    # form; the credential posts that went to its action (or, when none did, the
    # script posts to the first target no recorded form posts to); and each redirect
    # or set-cookie response that continues those posts' redirect chains. A password
    # change, an account edit, or a later POST answering with a redirect recorded in
    # the same run is observed but not part of it.
    login_flow: bool = True
    # On a credential post only: why where its login lands is ambiguous, as a clause
    # the generator's GP-FILL states, or empty when it is not. The chain came to
    # rest on a 200 page holding an OAuth form_post-shaped form it did not follow
    # (a same-host identity provider's, or a hidden-only logout form on the landing
    # page), or its last hop, a form_post submission, answered without a redirect,
    # leaving the client on that hop rather than on the last redirect's target. The
    # generator then takes no landing. A lookup for the generator, so render_json
    # leaves it out.
    landing_unresolved: str = field(default="", metadata={INTERNAL: True})


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
    cookie_names_dropped_as_ids: int = 0
    token_names_dropped_as_ids: int = 0
    # Selectors the recording suggests for LoginConfig.success: an element on a
    # logged-in page of the run and on none of its logged-out pages
    # (graftpunk.har.login_signals.success_candidates). Suggestions only: the
    # generator writes them commented out.
    success_candidates: tuple[str, ...] = ()
    # Text the recording suggests for LoginConfig.failure: shown on the page after
    # the credential post the user marked as a wrong password in gp observe
    # interactive --wrong-password-first (_failure_candidates). Suggestions only: the
    # generator writes them commented out.
    failure_candidates: tuple[str, ...] = ()


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
    first party. The parent rule needs no list.
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
    primary and the site third party. A capture
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


def _request_content_type(entry: HAREntry) -> str:
    """The request's ``Content-Type`` header as sent, parameters included, or HAR's
    own ``postData.mimeType`` when the capture carries no such header. Empty when
    the request declared neither."""
    for name, value in entry.request.headers.items():
        if name.lower() == "content-type":
            return value
    return entry.request.post_data_mime_type or ""


def _declared_request_content_type(entry: HAREntry) -> str:
    """What the request said its body was (:func:`_request_content_type`), lowered
    and without parameters."""
    return _request_content_type(entry).split(";")[0].strip().lower()


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
    the text carries an ``=``, and at least one key ``parse_qs`` returns is
    spelled like a field name (:func:`_field_name_shaped`). The keys that are
    not field names, or that hold an id, are then dropped and returned as the
    third element; the rest are the form's fields. ``parse_qs`` returns the
    whole text as a single key for anything else, so falling through to it put
    an XML credential post's entire body (values included) into
    ``Endpoint.body_params``, the rendered digest, the fixtures sidecar, and
    generated plugin source.

    A JSON array or scalar is still a JSON body; it just has no field names.
    """
    return _parse_body_text(entry.request.post_data, _declared_request_content_type(entry))


def _parse_body_text(
    post_data: str | None, declared: str
) -> tuple[dict[str, str], BodyKind, set[str]]:
    """:func:`_parse_body_keys` on a body's text and the media type its request
    declared (lowered, without parameters; empty when it declared none)."""
    kind, parsed = _decoded_body(post_data, declared)
    if kind == "json":
        if isinstance(parsed, dict):
            types, dropped = _json_body_types(parsed)
            return types, "json", dropped
        return {}, "json", set()
    if kind == "form":
        # A key that is not a field name, or that holds an id, is dropped and
        # counted, never the whole body.
        types = {k: _text_values_type(v) for k, v in parsed.items() if _plausible_field_name(k)}
        return types, "form", {k for k in parsed if not _plausible_field_name(k)}
    return {}, "none", set()


def _decoded_body(post_data: str | None, declared: str) -> tuple[BodyKind, Any]:
    """The one JSON-then-form reading of a body's text, for its names
    (:func:`_parse_body_text`) and its values (:func:`body_values`): ``("json",
    <the JSON value>)``, ``("form", <parse_qs of it>)`` when it reads as a form, or
    ``("none", None)``, a body nested past the JSON parser's limit among them."""
    kind, parsed = _decoded(post_data, declared)
    return ("none", None) if kind == "too_deep" else (kind, parsed)


def _decoded(post_data: str | None, declared: str) -> tuple[BodyKind | Literal["too_deep"], Any]:
    """:func:`_decoded_body`, telling a body nested past the JSON parser's limit
    (``"too_deep"``) from one that is not JSON."""
    if not post_data:
        return "none", None
    try:
        return "json", json.loads(post_data)
    except (ValueError, TypeError):
        pass
    except RecursionError:
        # Nested past the parser's limit (about 1,000 deep on Python 3.11): read as
        # no body rather than failing the digest.
        return "too_deep", None
    if declared and declared != _FORM_CONTENT_TYPE:
        return "none", None
    form = _form_fields(post_data)
    return ("none", None) if form is None else ("form", form)


def _form_fields(post_data: str) -> dict[str, list[str]] | None:
    """``parse_qs`` of *post_data* when it reads as a form, else ``None``."""
    if "=" not in post_data:
        return None
    form = parse_qs(post_data, keep_blank_values=True)
    # A form when at least one key reads as a field name: a body that is really
    # XML or prose gives parse_qs one unreadable key and is not one.
    if not form or not any(_field_name_shaped(name) for name in form):
        return None
    return form


_MULTIPART_CONTENT_TYPE = "multipart/form-data"
# A multipart part's field name, from its Content-Disposition; "filename=" holds
# "name=" after a word character, so \b keeps it out.
_MULTIPART_NAME_RE = re.compile(r'\bname="([^"]*)"', re.IGNORECASE)
_BOUNDARY_RE = re.compile(r'\bboundary=(?:"([^"]+)"|([^\s;]+))', re.IGNORECASE)


def multipart_delimiter(text: str, content_type: str) -> str | None:
    """The line a ``multipart/form-data`` body *text* splits its parts on: ``--``
    and the boundary its *content_type* names, or, when it names none, the first
    non-blank line of *text* when that starts with ``--``; else None."""
    declared = _BOUNDARY_RE.search(content_type)
    if declared:
        return "--" + (declared.group(1) or declared.group(2))
    delimiter = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return delimiter if delimiter.startswith("--") and len(delimiter) >= 3 else None


def _multipart_values(text: str, content_type: str) -> list[tuple[tuple[str, ...], str]]:
    """The (name,) and value of each text part of a ``multipart/form-data`` body,
    split on the boundary its *content_type* names, or, when it names none, on the
    first non-blank line of the body. A file part is skipped."""
    newline = "\r\n" if "\r\n" in text else "\n"
    delimiter = multipart_delimiter(text, content_type)
    if delimiter is None:
        return []
    found: list[tuple[tuple[str, ...], str]] = []
    for part in text.split(delimiter)[1:]:
        if part.startswith("--"):
            break
        head, separator, value = part.lstrip("\r\n").partition(newline * 2)
        name = _MULTIPART_NAME_RE.search(head)
        if separator and name and "filename=" not in head.lower():
            found.append(((name.group(1),), value.removesuffix(newline)))
    return found


def body_values(post_data: str | None, content_type: str = "") -> list[tuple[tuple[str, ...], str]]:
    """Each scalar value of a request body, with the field names that lead to it,
    outermost first: read as :func:`body_params` reads its names
    (:func:`_decoded_body`), a JSON value at any depth (a boolean and ``null``
    skipped, a list adding no name), each value of a form field, and, which
    :func:`body_params` does not read, each text part of a ``multipart/form-data``
    body. *content_type* is the request's ``Content-Type`` as sent, parameters
    included. The one reader of a credential post's values, for the digest's
    suggestions (:func:`_account_values`) and the recorder's snapshot redaction
    (:func:`graftpunk.observe.snapshot.snapshot_redaction`). JSON is walked without
    recursion; a body nested past the JSON parser's own limit reads as no values.

    Values feed redaction and the leak guard, so this reads more than
    :func:`body_params`: a ``text/plain`` form body field by field (one field a
    line, as a browser sends ``enctype="text/plain"``), a form-shaped body whatever
    type it declared (``multipart/form-data`` with no parts, say), any body holding
    ``=`` that is not JSON by ``parse_qs`` even when no key reads as a field name, as
    the snapshot redaction always did, and a value that is itself a JSON object or
    array, one level down, under its field's name (:func:`_read_body_values`)."""
    return [(path, value) for path, value, _named in _read_body_values(post_data, content_type)[0]]


# A body value: the names leading to it, outermost first; the value; and how many
# of those names are the field's own. That is all of them but for a value read
# inside a field holding JSON, whose own names are the outer field's only.
_BodyValue = tuple[tuple[str, ...], str, int]


def _read_body_values(post_data: str | None, content_type: str) -> tuple[list[_BodyValue], bool]:
    """:func:`body_values`, each with how many of its names are its field's own
    (:data:`_BodyValue`), and whether the body was read by its field names: as
    JSON in which every value has a name, as multipart parts, as ``text/plain``
    fields, or as a form with a key spelled like a field name. A body read only by
    ``parse_qs`` with no such key (XML with an attribute, prose with an ``=``), a
    JSON body with a value under no name (a bare string, a list of strings), and a
    value holding JSON too deep for the parser are not read by field names."""
    _layout, fields, by_names = _body_fields(post_data, content_type)
    return _with_json_values(fields, by_names)


def body_layout(post_data: str | None, content_type: str = "") -> BodyLayout:
    """How :func:`body_values` splits a request body into fields, so a writer
    (:func:`graftpunk.observe.failure_text.scrub_failed_password`) splits it the
    same way: ``"json"``; ``"text_plain"``, a field a line
    (:func:`_text_plain_fields`); ``"multipart"``, a field a part; ``"form"``, a
    field each ``&``-separated piece, by ``parse_qs``; or ``"none"`` for no fields."""
    return _body_fields(post_data, content_type)[0]


def _body_fields(
    post_data: str | None, content_type: str
) -> tuple[BodyLayout, list[tuple[tuple[str, ...], str]], bool]:
    """The one split of a body into fields, for :func:`_read_body_values` and
    :func:`body_layout`: its layout, its fields (before the values inside a field
    holding JSON), and whether they were read by their field names."""
    declared = content_type.split(";")[0].strip().lower()
    kind, parsed = _decoded(post_data, declared)
    if not post_data or kind == "too_deep":
        return "none", [], False
    if kind == "json":
        values = _scalar_values(parsed)
        return "json", values, all(path for path, _value in values)
    lines = _text_plain_fields(post_data) if declared == _TEXT_PLAIN_CONTENT_TYPE else None
    if lines:
        return "text_plain", lines, True
    if kind == "none" and declared == _MULTIPART_CONTENT_TYPE:
        parts = _multipart_values(post_data, content_type)
        if parts:
            return "multipart", parts, True
    form = parsed if kind == "form" else _form_fields(post_data)
    if form is not None:
        return "form", [((name,), value) for name, values in form.items() for value in values], True
    if "=" in post_data:
        loose = parse_qs(post_data, keep_blank_values=True)
        return (
            "form",
            [((name,), value) for name, values in loose.items() for value in values],
            False,
        )
    return "none", [], False


_TEXT_PLAIN_CONTENT_TYPE = "text/plain"


def _text_plain_fields(post_data: str) -> list[tuple[tuple[str, ...], str]] | None:
    """The fields of a ``text/plain`` form body, which a browser sends as
    ``name=value`` lines with nothing escaped: the first line starts a field when
    its part before its first ``=`` is spelled like a field name, each later line
    holding ``=`` starts one whatever its name (``loginForm:log``, ``user name``),
    and a line with no ``=`` continues the value before it. A value is read as
    written: no ``&`` splitting and no percent-decoding. A browser ends every field
    with CRLF, so one line ending in a line break is one field. ``None`` when the
    body is one line with no line break after it (a script's ``fetch`` of a string
    declares ``text/plain`` and often sends a URL-encoded form; the form read takes
    it) or its first line starts no field."""
    lines = re.split(r"\r?\n", post_data.rstrip("\r\n"))
    if len(lines) < 2 and not post_data.endswith("\n"):
        return None
    first, separator, _value = lines[0].partition("=")
    if not (separator and _field_name_shaped(first)):
        return None
    fields: list[tuple[tuple[str, ...], str]] = []
    for line in lines:
        name, separator, value = line.partition("=")
        if separator:
            fields.append(((name,), value))
        else:
            path, before = fields[-1]
            fields[-1] = (path, f"{before}\n{line}")
    return fields


def _with_json_values(
    values: list[tuple[tuple[str, ...], str]], by_names: bool
) -> tuple[list[_BodyValue], bool]:
    """*values*, each followed by the values inside it when it is itself a JSON
    object or array (a form field or a JSON string holding JSON), under its own
    names; one level only. A value inside keeps its field's names as its own
    (:data:`_BodyValue`). *by_names* turns false when such a value is nested past
    the JSON parser's limit, because the values inside it cannot be read."""
    found: list[_BodyValue] = []
    for path, value in values:
        found.append((path, value, len(path)))
        if value.lstrip()[:1] not in ("{", "["):
            continue
        try:
            inner = json.loads(value)
        except (ValueError, TypeError):
            continue
        except RecursionError:
            by_names = False
            continue
        found.extend(
            ((*path, *names), scalar, len(path)) for names, scalar in _scalar_values(inner)
        )
    return found, by_names


def _scalar_values(parsed: object) -> list[tuple[tuple[str, ...], str]]:
    """:func:`body_values` of a parsed JSON value, walked without recursion."""
    found: list[tuple[tuple[str, ...], str]] = []
    stack: list[tuple[object, tuple[str, ...]]] = [(parsed, ())]
    while stack:
        value, path = stack.pop()
        if isinstance(value, dict):
            stack.extend((inner, (*path, str(name))) for name, inner in reversed(value.items()))
        elif isinstance(value, list):
            stack.extend((inner, path) for inner in reversed(value))
        elif isinstance(value, str | int | float) and not isinstance(value, bool):
            found.append((path, str(value)))
    return found


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
        # such sibling's. A map whose keys are ids as a group (keys_are_ids) has
        # every key replaced.
        children: dict[str, ShapeNode] = {}
        map_of_ids = keys_are_ids(value)
        for key, child in value.items():
            name = _ID_KEY if map_of_ids or holds_an_id(key) else key
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
    the generated docstring said so.
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


_HTML_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})


def _inline_json_of(media_type: str, text: str) -> InlineJson | None:
    """The inline JSON state *text*, a recorded response body of normalised
    *media_type*, carries when it is an HTML page
    (:func:`graftpunk.har.inline_state.inline_json`), with its object's shape
    read by :func:`_shape_of`; None for any other body or a page holding none."""
    if media_type not in _HTML_MEDIA_TYPES or not text:
        return None
    state = inline_json(text)
    if state is None:
        return None
    return InlineJson(target=state.target, shape=_shape_of(state.value))


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
    every line of every ``Set-Cookie`` header on the raw response,
    deduplicated, in order, by the same parse that gives
    ``set_cookie_http_only``. The raw headers list is read there, not here,
    since a response can carry more than one ``Set-Cookie`` header and a
    header dict collapses duplicate names to the last value.
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


def _entry_cookie_names(entry: HAREntry) -> list[str]:
    """Cookie names this entry carries: the ones its response sets
    (:func:`_response_cookie_names`), then the ones its request sends that the
    response does not set. A recording made with a cached session sends its
    session without any response setting it (#274)."""
    names = _response_cookie_names(entry)
    for cookie in entry.request.cookies:
        name = cookie.get("name", "")
        if name and name not in names:
            names.append(name)
    return names


def _redirect_target(entry: HAREntry) -> tuple[str, str]:
    """The host and path a 3xx response sent the client to, as
    :func:`_redirect_target_path` spells the path; both empty when it names none."""
    path = _redirect_target_path(entry)
    if not path:
        return "", ""
    # _redirect_target_path already split this same URL, so this cannot raise.
    target = urlparse(bare_url(urljoin(entry.request.url, entry.response.redirect_url)))
    return normal_host(target.scheme, target.netloc), path


def _hop_form_targets(entry: HAREntry) -> dict[tuple[str, str], frozenset[str]]:
    """The OAuth ``form_post``-shaped forms on *entry*'s page, when it answered 200
    with HTML (:func:`graftpunk.har.documents.form_action_targets`): a POST to one,
    carrying only its hidden names, continues the redirect chain *entry* is part
    of."""
    content_type = (entry.response.content_type or "").lower()
    if entry.response.status != 200 or "html" not in content_type or not entry.response.body:
        return {}
    return form_action_targets(entry.response.body, _unmasked_page(entry))


def _submits_a_chain_form(
    post_host: str,
    target: tuple[str, str],
    body_names: frozenset[str],
    page: tuple[str, dict[tuple[str, str], frozenset[str]]],
) -> bool:
    """True when a POST sent to *post_host* and *target* carrying *body_names* is the
    submission of an OAuth ``form_post`` form on *page* (the host that served it,
    and its forms): it goes to that form's target, carries only its hidden names,
    and crosses hosts (the identity provider's page posts to the app), which a
    same-site logout or cart form submitted by script never does. Every host here
    is spelled by :func:`graftpunk.har.paths.normal_host`, so case and a default
    port do not tell two hosts apart."""
    page_host, forms = page
    return post_host != page_host and any(
        _target_matches(form_target, target) and body_names <= hidden
        for form_target, hidden in forms.items()
    )


def _continues_chain(host: str, path: str, expected: tuple[str, str]) -> bool:
    """True when a request to *host* and *path* is where the chain was sent: the same
    path on the same host, both hosts normalised (:func:`normal_host`). Both are
    always known: a request URL the digest reaches has a host, and so does a
    redirect target resolved against it."""
    want_host, want_path = expected
    return bool(want_path) and path == want_path and host == want_host


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


def _normal_url(url: str) -> str:
    """*url*, one :func:`graftpunk.har.paths.bare_url` gave, with its host spelled by
    :func:`graftpunk.har.paths.normal_host`. The digest already split it, so this
    cannot raise."""
    parts = urlparse(url)
    return urlunparse(parts._replace(netloc=normal_host(parts.scheme, parts.netloc)))


def _host_of(url: str) -> str:
    """*url*'s host, spelled by :func:`graftpunk.har.paths.normal_host`. *url* is one
    the digest already split, so this cannot raise."""
    parts = urlparse(url)
    return normal_host(parts.scheme, parts.netloc)


def _request_target(url: str) -> tuple[str, str]:
    """Where a request to *url* goes, as host and path (``;params`` dropped, never
    email-masked), the way a form's ``action_target`` is spelled: the host through
    :func:`graftpunk.har.paths.normal_host`."""
    try:
        parts = urlparse(url)
    except ValueError:
        return "", ""
    return normal_host(parts.scheme, parts.netloc), unquote(bare_path(parts.path)) or "/"


def _unmasked_page(entry: HAREntry) -> str:
    """*entry*'s URL as scheme, host, and path with no email masked: the base a form
    on the page resolves its action against. Never printed."""
    return _unmasked_url(entry.request.url)


def _unmasked_url(url: str) -> str:
    """:func:`_unmasked_page` for a page's URL."""
    try:
        parts = urlparse(url)
    except ValueError:
        return ""
    return urlunparse((parts.scheme, bare_host(parts.netloc), bare_path(parts.path), "", "", ""))


def _target_matches(form_target: tuple[str, str], post_target: tuple[str, str]) -> bool:
    """True when a POST to *post_target* goes where a form with *form_target* posts:
    the same host (any, when the form's is unknown) and the same path, or, for a
    slash-less target from a saved page source (``session``), a path ending in
    ``/`` and that target."""
    form_host, form_path = form_target
    post_host, post_path = post_target
    if not form_path or not post_path or (form_host and form_host != post_host):
        return False
    if form_path.startswith("/"):
        return form_path == post_path
    return post_path.endswith("/" + form_path)


def _posts_to_a_login_form(target: tuple[str, str], targets: set[tuple[str, str]]) -> bool:
    """True when a POST to *target* goes where a recorded login form posts."""
    return any(_target_matches(form_target, target) for form_target in targets)


@dataclass(frozen=True)
class _CredentialPost:
    """One credential post, as the ordering of login forms needs it: where it went,
    the names its body carried, and the form page it was promoted from."""

    target: tuple[str, str]
    body_names: frozenset[str]
    page_step: int | None
    # Found by its field names alone: no recorded login form's target matched it.
    by_field_names: bool = False
    # The formless group it was credited to by its input names (_formless_credit).
    formless_key: tuple[object, ...] | None = None


def _script_driven_forms(page_url: str, forms: tuple[LoginForm, ...]) -> list[LoginForm]:
    """Those of *forms*, on the page at *page_url*, with no target of their own: an
    empty, ``#``, or ``javascript:`` action, or one resolving to the page itself.
    Script decides where such a form is sent, so a credential post found by its field
    names alone may be its post. A formless group (``LoginForm.formless``) is never
    one, as its action is empty and its target ``("", "")``: a post is credited to
    it only by its input names (:func:`_formless_credit`)."""
    page = _request_target(page_url)
    return [
        form
        for form in forms
        if form.action_target == page or form.action.lower().startswith("javascript:")
    ]


def _used_forms_first(
    forms: list[LoginForm],
    page_of: dict[int, int],
    posts: list[_CredentialPost],
    script_driven: set[int],
) -> list[LoginForm]:
    """*forms* in the order the generator should prefer them:

    0. a real form a credential post went to (by its target, or found by its field
       names on the page it promoted), then a formless group a post was credited to
       (:func:`_formless_credit`), then any other real form, then any other formless
       group. A credited group ranks below every real form a post went to (a later
       change-email form included) and above a real form no post went to, which
       then shares no visible name with any credential post, since no group is
       credited in a run where a real form does (:func:`digest`); with no formless
       group this term orders nothing the terms below do not;
    1. one on the page that a credential post it went to promoted;
    2. the one the earliest credential post went to (a form no post went to ranks
       after every form one did); a post found by its field names alone went to
       the script-driven form (:func:`_script_driven_forms`) on the page it
       promoted, so a script login ranks its form ahead of a later form posting to
       its own page (a change-email form);
    3. the one whose control names cover the most of that post's body;
    4. one that sits on no page a credential post did not promote (not a site-wide
       header form);
    5. the one with fewer unresolved roles;
    6. document order.

    *page_of* maps a form's ``id`` to its page's step; *script_driven* holds the
    ``id`` of each script-driven form."""
    promoted = {post.page_step for post in posts if post.page_step is not None}
    pages_of_key: dict[tuple[object, ...], set[int]] = {}
    for form in forms:
        if id(form) in page_of:
            pages_of_key.setdefault(_form_key(form), set()).add(page_of[id(form)])

    def rank(form: LoginForm) -> tuple[int, bool, int, int, bool, int]:
        page = page_of.get(id(form))
        matching = [
            (order, post)
            for order, post in enumerate(posts)
            if _target_matches(form.action_target, post.target)
            or (post.by_field_names and id(form) in script_driven and page == post.page_step)
            # Every copy of the credited group matches: the copies are one form,
            # listed once (_unique_forms), so which copy matched orders nothing.
            or (post.formless_key is not None and _form_key(form) == post.formless_key)
        ]
        on_its_page = any(page == post.page_step for _o, post in matching)
        # A login precedes a password change: the form the earliest post went to wins
        # over a change-password form that covers more of its own post's body.
        earliest = min((order for order, _post in matching), default=len(posts))
        coverage = max(
            (len(set(form.input_names) & post.body_names) for _o, post in matching), default=0
        )
        site_wide = bool(pages_of_key.get(_form_key(form), set()) - promoted)
        # No "not matching" term: a form no post went to has no page of its own and
        # the latest possible earliest post, so the terms below already rank it last.
        kind = (1 if matching else 3) if form.formless else (0 if matching else 2)
        return (
            kind,
            not on_its_page,
            earliest,
            -coverage,
            site_wide,
            len(form.unresolved_roles),
        )

    return sorted(forms, key=rank)


def _form_key(form: LoginForm) -> tuple[object, ...]:
    """What makes two recorded forms the same form: its structure, never its
    selectors, which depend on the page (a name unique on one page is shared with a
    same-action form on another). Not action_target either: an empty-action form
    resolves to each page it is on."""
    return (form.action, form.method, form.signature, form.hidden)


def _formless_credit(
    post_step: int,
    body_names: frozenset[str],
    forms_on_page: dict[int, list[LoginForm]],
) -> tuple[int, tuple[object, ...]] | None:
    """The page and the formless group a credential post found by its field names
    alone is credited to, when no earlier page holds a real form with no target of
    its own: among the pages recorded before the post (step below *post_step*; only
    a GET's page holds one, as the digest drops a formless group from a POST's HTML),
    one holding a formless group (``LoginForm.formless``) that shares at least one
    input name with the post's body (*body_names*), as a real form is matched by
    the names it covers. The group sharing the most names wins, then the page whose
    path has the fewest segments (``/`` when recorded), then the earliest recorded.
    None when no formless group shares a name: a group whose inputs have no names
    is never credited."""
    candidates = [
        (-len(set(form.input_names) & body_names), _path_segments(form.source), at, form)
        for at, forms in forms_on_page.items()
        if at < post_step
        for form in forms
        if form.formless and set(form.input_names) & body_names
    ]
    if not candidates:
        return None
    _shared, _segments, at, form = min(candidates, key=lambda c: c[:3])
    return at, _form_key(form)


def _printable_selector(selector: str | None) -> str:
    """*selector* when it is usable and safe to print: within ``MAX_SELECTOR_LENGTH``
    and holding no account value; else empty."""
    bounded = bounded_selector(selector)
    return bounded if bounded is not None and not selector_holds_an_id(bounded) else ""


def _login_opener(
    form: LoginForm,
    interactions: tuple[LoginInteraction, ...],
    post_at: datetime,
    page_loads: list[datetime],
) -> str | None:
    """The click the recording suggests opened *form*: a selector from the
    *interactions* recorded before its credential post (sent at *post_at*), or None.
    It is only ever a suggestion: a single recording does not tell reliably which
    click opened a login, so the generator writes it commented out for the author to
    confirm.

    The candidate is the last click on a control (``control``), not flagged inside
    the login (``in_login``), before the first focus on one of the form's field
    selectors (the recorded ones and their unscoped forms, compared by
    :func:`graftpunk.har.selectors.selector_keys`). There is no suggestion when there
    is no candidate, or when it picks one of the form's fields or its submit; a click
    on no control came after it and before that focus (a ``div`` or a menu item may
    have opened the login); a
    navigation of the run (*page_loads*, see :func:`_is_page_load`) started between it
    and that focus, both ends included; one of the form's fields was among the fields
    shown at it (the login was already on screen); its page path differs from the
    first focus's (when both are known); the form has no username and a field was
    focused before it, or a field was shown at it (the login has an earlier step); it
    was inside a dialog; it was not drawn when the page was last read before it
    (``revealed``); or it has no usable
    selector (over ``MAX_SELECTOR_LENGTH`` characters, or holding an account value). A
    focus or a shown field whose selector is over that length is not read."""
    roles: set[tuple[str, ...]] = set()
    for selector in (*form.fields.values(), *form.unscoped_fields.values()):
        roles |= selector_keys(selector)
    controls = set(roles)
    for selector in (form.submit, form.unscoped_submit):
        if selector:
            controls |= selector_keys(selector)

    def names_a_field(selector: str | None) -> bool:
        bounded = bounded_selector(selector)
        return bounded is not None and bool(selector_keys(bounded) & roles)

    first_focus = next(
        (
            at
            for at, event in enumerate(interactions)
            if event.kind == "focus" and names_a_field(event.selector)
        ),
        None,
    )
    if first_focus is None:
        return None
    at = next(
        (
            at
            for at in range(first_focus - 1, -1, -1)
            if interactions[at].kind == "click"
            and interactions[at].control
            and not interactions[at].in_login
        ),
        None,
    )
    if at is None:
        return None
    click = interactions[at]
    focus = interactions[first_focus]
    clicked = bounded_selector(click.selector)
    clicked_at = post_at - timedelta(milliseconds=click.ms_before)
    focused_at = post_at - timedelta(milliseconds=focus.ms_before)
    no_username = "username" not in form.fields
    disqualified = (
        (clicked is not None and bool(selector_keys(clicked) & controls))
        # A click on no control after it (a div or a menu item with a handler) may be
        # what opened the login.
        or any(
            event.kind == "click" and not event.control
            for event in interactions[at + 1 : first_focus]
        )
        or any(clicked_at <= loaded <= focused_at for loaded in page_loads)
        or any(names_a_field(shown) for shown in click.shown_fields)
        or (click.path is not None and focus.path is not None and click.path != focus.path)
        or (no_username and any(event.kind == "focus" for event in interactions[:at]))
        or (no_username and bool(click.shown_fields))
        or click.in_dialog
        or click.revealed
    )
    printable = _printable_selector(clicked)
    return None if disqualified or not printable else printable


def _is_page_load(entry: HAREntry, method: str, content_type: str) -> bool:
    """Whether *entry* is a navigation: by the recorder's ``_resourceType``
    (``Document``) when the HAR has it; else by ``Sec-Fetch-Mode: navigate`` or
    ``Sec-Fetch-Dest`` ``document`` or ``iframe``, which Chrome sends only to a
    secure origin; else, for a HAR with neither, a GET answered 2xx with HTML (which
    takes an HTML fragment a script fetched for one too). An HTML fragment fetched
    by script on a click is no page load, so a modal whose markup is fetched keeps
    its opener."""
    if entry.request.resource_type is not None:
        return entry.request.resource_type == "Document"
    headers = {name.lower(): value.strip().lower() for name, value in entry.request.headers.items()}
    mode, dest = headers.get("sec-fetch-mode"), headers.get("sec-fetch-dest")
    if mode or dest:
        return mode == "navigate" or dest in ("document", "iframe")
    return method == "GET" and "html" in content_type and 200 <= entry.response.status < 300


# A navigation into one of these is a frame's document, never the page the engine
# queries.
_FRAME_DESTINATIONS = frozenset({"iframe", "frame", "embed", "object"})


def _is_top_level_page_load(entry: HAREntry, method: str, content_type: str) -> bool:
    """Whether *entry* is a navigation of the top-level page (:func:`_is_page_load`),
    not of a frame. A frame's document is told by every signal a recording holds for
    it: a ``Sec-Fetch-Dest`` of ``iframe``, ``frame``, ``embed``, or ``object``, or
    a ``_resourceType`` other than ``Document`` (which :func:`_is_page_load` reads
    as no navigation). A frame navigation recorded with ``_resourceType``
    ``Document``, as the recorder writes every navigation, and no ``Sec-Fetch-Dest``
    cannot be told apart."""
    dest = next(
        (
            value.strip().lower()
            for name, value in entry.request.headers.items()
            if name.lower() == "sec-fetch-dest"
        ),
        "",
    )
    return dest not in _FRAME_DESTINATIONS and _is_page_load(entry, method, content_type)


# What a run's redaction writes in place of a value (graftpunk.observe.run.REDACTED,
# which this module cannot import): a text holding it can never match a page.
REDACTED_MARK = "***REDACTED***"
# An email address inside a value.
_EMAIL_IN_VALUE_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def _after_read(credential_entries: Sequence[HAREntry], stepped_since: bool) -> str | None:
    """The page the recorder read after the run's last credential post settled
    (``request._domAfter``, structure only), which narrows the success suggestions
    (``also_on`` of :func:`graftpunk.har.login_signals.success_candidates`); None
    when that post has none, or when the read may still be a login: it holds a
    login form, asks for a one-time code
    (:func:`graftpunk.har.login_signals.asks_for_a_code`), or *stepped_since* (the
    browser sent a step-shaped POST after that post, a document form or a script's,
    :func:`graftpunk.har.login_signals.is_login_step_post`)."""
    if not credential_entries:
        return None
    html = credential_entries[-1].request.dom_after
    if not html or stepped_since or asks_for_a_code(html):
        return None
    if extract_login_forms(html, source="", snapshot=True):
        return None
    return html


def _failure_candidates(
    credential_entries: Sequence[HAREntry],
    account_values: list[str] | None,
    logged_in_pages: Sequence[str],
    posted_values: Sequence[str] = (),
) -> tuple[str, ...]:
    """The failure texts the last credential post the user marked failed carries
    (``request._failureTextCandidates``), less each one holding an account value of
    any credential post of the run (*account_values*) or a value any request
    posted up to the real login (*posted_values*, :func:`posted_account_values`:
    the username of an identifier-first login, under any name), by the rule the
    success suggestions are kept by
    (:func:`graftpunk.har.login_signals.holds_an_account_value`), an email address,
    or a redaction, and each one the engine would find
    (:func:`graftpunk.plugins.login_settle.failure_text_on_page`) on one of
    *logged_in_pages*, the run's pages after the real login, since it would fail
    every good login. Nothing when no post is marked, when *account_values* is None
    (a credential post's values cannot be read), or when a real login followed the
    marked post and nothing shows the page it landed on: no logged-in page and no
    after-read of the real login (``request._domAfter``, the recorder's sign that it
    read the landed page and dropped the texts on it). The recorder checked them
    already, but a stop soon after the real login cancels its read of the landed
    page, a HAR can be edited, and the run's redaction changed it since."""
    marked = [at for at, entry in enumerate(credential_entries) if entry.request.user_marked_failed]
    if not marked or account_values is None:
        return ()
    real_login = credential_entries[-1]
    if (
        marked[-1] < len(credential_entries) - 1
        and not logged_in_pages
        and real_login.request.dom_after is None
    ):
        return ()
    needles = account_needles([*account_values, *posted_values])
    lowered_pages = [page.lower() for page in logged_in_pages]
    return tuple(
        text
        for text in credential_entries[marked[-1]].request.failure_text_candidates
        if REDACTED_MARK not in text
        and not _EMAIL_IN_VALUE_RE.search(text)
        and not holds_an_account_value(text, needles)
        and not any(failure_text_on_lowered_page(page, text) for page in lowered_pages)
    )


def _pages_after_the_real_login(
    credential_entries: Sequence[HAREntry],
    after_pages_at_post: Sequence[int],
    after_post_pages: Sequence[str],
) -> list[str]:
    """The last recorded page after the real login (the last credential post after
    the last one the user marked failed), as a list of none or one: a page loaded
    after it holding no login form (*after_post_pages*, each credential post's count
    of them beside it in *after_pages_at_post*)."""
    marked = [at for at, entry in enumerate(credential_entries) if entry.request.user_marked_failed]
    if not marked or marked[-1] == len(credential_entries) - 1:
        return []
    pages = after_post_pages[after_pages_at_post[-1] :]
    return [pages[-1]] if pages else []


def _account_values(entry: HAREntry, username_names: frozenset[str]) -> list[str] | None:
    """:func:`post_account_values` of credential post *entry*; ``None`` also when the
    recording does not hold the body as readable text (no body, an empty one, one the
    recorder did not capture, left out, or found not to be UTF-8)."""
    request = entry.request
    if request.post_data_not_captured or request.post_data_omitted or request.post_data_binary:
        return None
    return post_account_values(request.post_data, _request_content_type(entry), username_names)


def post_account_values(
    post_data: str | None, content_type: str = "", username_names: Iterable[str] = ()
) -> list[str] | None:
    """The values of a credential post's body *post_data* (read by
    :func:`body_values`) that no suggested name may hold, for
    :func:`graftpunk.har.login_signals.success_candidates`, and that the recorder's
    read of the page after the post redacts
    (:func:`graftpunk.observe.snapshot.stored_dom_snapshot` with ``structure_only``):
    the password field's (a name with a :data:`PASSWORD_NAME_HINTS` hint); the
    username's, a field the recorded login form takes as its username
    (*username_names*, ``LoginForm.username_name``) or one
    :func:`graftpunk.session_identity.is_identifier_field` accepts, by any name on
    the way to a nested value; a value read inside a field holding JSON under a key
    either check accepts; and each email address inside any value. When no
    field is a username by those rules (a key inside a field holding JSON never
    counts here), every value is one, but a token field's
    (:func:`graftpunk.har.documents.looks_like_token_name`). A field counts as the
    username only when its value could name an account
    (:func:`graftpunk.har.login_signals.could_be_an_account_value`) and is not one
    of the names leading to it, case aside (``email=``, ``login=Login``, and
    ``account=1`` leave every value an account value).

    ``None`` when the body gives no values read by their field names
    (:func:`_read_body_values`: no body, nested past the JSON parser's limit, XML, a
    JSON value under no name, or any other shape), so the guard cannot see the
    username, and nothing may be suggested. Held in memory only, never written."""
    values, by_names = _read_body_values(post_data, content_type)
    if not (values and by_names):
        return None
    usernames_by_form = frozenset(username_names)

    def names_a_username(path: tuple[str, ...], value: str) -> bool:
        if not could_be_an_account_value(value) or value.strip().lower() in {
            name.lower() for name in path
        }:
            return False
        return any(
            name in usernames_by_form or is_identifier_field(identifier_key(name)) for name in path
        )

    usernames = [value for path, value, named in values if names_a_username(path[:named], value)]
    # A key inside a field holding JSON never turns the fallback off, but what it
    # names is still the account's.
    inner = [
        value
        for path, value, named in values
        if named < len(path) and names_a_username(path, value)
    ]
    if not usernames:
        usernames = _every_value_but_tokens(values)
    passwords = [value for path, value, _named in values if path and _password_like([path[-1]])]
    emails = [
        email for _path, value, _named in values for email in _EMAIL_IN_VALUE_RE.findall(value)
    ]
    return [*usernames, *inner, *passwords, *emails]


def _every_value_but_tokens(values: list[_BodyValue]) -> list[str]:
    """The every-value fallback of :func:`post_account_values`: each of *values* but
    one under a token field's name (:func:`graftpunk.har.documents.looks_like_token_name`)
    anywhere on its way."""
    return [
        value for path, value, _named in values if not any(looks_like_token_name(n) for n in path)
    ]


def post_every_value(post_data: str | None, content_type: str = "") -> list[str]:
    """The values of a credential post's body *post_data* that the every-value
    fallback of :func:`post_account_values` takes, whether or not a field of the post
    is a username by its rules: every value but a token field's. The recorder's read
    of the page after the post redacts these beside :func:`post_account_values`
    (:func:`graftpunk.observe.snapshot.stored_dom_snapshot` with ``structure_only``),
    because a username posted under a field neither rule knows (``j_username`` beside
    an ``email``) is still the account's. Empty when the body gives no values read by
    their field names, as :func:`post_account_values` gives ``None``. Held in memory
    only, never written."""
    values, by_names = _read_body_values(post_data, content_type)
    return _every_value_but_tokens(values) if values and by_names else []


def post_identifier_values(
    post_data: str | None, content_type: str = "", username_names: Iterable[str] = ()
) -> list[str]:
    """Each non-empty value of a request body *post_data* (read by
    :func:`body_values`) under a name that is an identifier field's
    (:func:`graftpunk.session_identity.is_identifier_field`) or that a recorded login
    form takes as its username (*username_names*), by any name on the way to it: the
    username an identifier-first login posts in a step before the password post,
    which that post's own values do not hold. Held in memory only, never written."""
    by_form = frozenset(username_names)
    return [
        value
        for path, value in body_values(post_data, content_type)
        if value.strip()
        and any(name in by_form or is_identifier_field(identifier_key(name)) for name in path)
    ]


def posted_account_values(
    post_data: str | None,
    content_type: str = "",
    method: str = "POST",
    username_names: Iterable[str] = (),
) -> list[str]:
    """The values a request of a login run sent that a failure suggestion may not
    hold: its identifier values (:func:`post_identifier_values`) and, for a POST,
    each value the every-value rule takes (:func:`post_every_value`) that could name
    an account (:func:`graftpunk.har.login_signals.could_be_an_account_value`: three
    or more characters, not a boolean word). An identifier-first login posts the
    username in a step under any name (``j_username``, ``loginfmt``), which neither
    the password post's values nor an identifier name finds. Held in memory only,
    never written."""
    values = post_identifier_values(post_data, content_type, username_names)
    if method.upper() == "POST":
        values += [
            value
            for value in post_every_value(post_data, content_type)
            if could_be_an_account_value(value)
        ]
    return values


def _path_segments(url: str) -> int:
    """How many non-empty path segments *url* has."""
    return len([segment for segment in urlparse(url).path.split("/") if segment])


def _used_login(
    ranked: list[LoginForm],
    recorded: list[LoginForm],
    page_of: dict[int, int],
    post_steps: list[int],
    posts: list[_CredentialPost],
) -> tuple[set[int], set[int]]:
    """The steps of the credential posts and the form pages of the login the
    generator uses: the first ranked form with a password field (the one
    ``login_config`` is built from), and the pages the owned posts promoted. Owned
    are the posts that went to its target; the posts a recorded form's target
    matched that promoted a page it is on; and, only when no post went to its
    target, the posts found by their field names alone to the first target no
    recorded form posts to (a script posting elsewhere than the form says), whether
    they promoted its page or not. A field-name-only post that promoted its page but
    went elsewhere than that first target is not owned. With no such form, or when
    it is a formless group (a dialog's post is found by its names alone, so a later
    password post cannot be told from the login's), every credential post and its
    page."""
    selected = next((form for form in ranked if "password" in form.fields), None)
    if selected is None or selected.formless:
        # A dialog's post is found by its field names alone, so a later password post
        # (a re-authentication) cannot be told from the login's: every credential
        # post stays the login's, as when no form was recorded.
        owned = set(post_steps)
    else:
        key = _form_key(selected)
        pages = {
            page_of[id(form)] for form in recorded if id(form) in page_of and _form_key(form) == key
        }
        steps_and_posts = list(zip(post_steps, posts, strict=True))
        direct = {
            step
            for step, post in steps_and_posts
            if _target_matches(selected.action_target, post.target)
        }
        # A login script can post elsewhere than its form's action. Only when no post
        # went to the form's action is a post found by its field names alone, to no
        # recorded form's action, the login's; and only the first such target, so a
        # later password-confirmed action (change email, delete account) stays its
        # own command. The same rule holds for a field-name-only post that reached
        # the form's page through the scripted fallback (a self-posting or actionless
        # login form), so no such post is owned by the page alone.
        script_posts = [
            (step, post)
            for step, post in steps_and_posts
            if post.by_field_names
            and not any(_target_matches(form.action_target, post.target) for form in recorded)
        ]
        first_target = script_posts[0][1].target if script_posts and not direct else None
        by_page = {
            step
            for step, post in steps_and_posts
            if post.page_step in pages and (not post.by_field_names or post.target == first_target)
        }
        by_script = {step for step, post in script_posts if post.target == first_target}
        owned = direct | by_page | by_script
    owned_pages = {
        post.page_step
        for step, post in zip(post_steps, posts, strict=True)
        if step in owned and post.page_step is not None
    }
    return owned, owned_pages


def _opened_from(via: tuple[str, ...], here_host: str) -> str:
    """The last GET of a redirect chain *via* on another host than *here_host* that is
    not a logout (opening one would log out): where a form on the chain's page is
    opened (``LoginForm.opened_from``). Empty when there is none."""
    return next(
        (
            hop
            for hop in reversed(via)
            if _host_of(hop) != here_host and not _LOGOUT_URL_REGEX.search(urlparse(hop).path)
        ),
        "",
    )


def _visible_shape(form: LoginForm) -> tuple[object, ...]:
    """What a user sees of a login form, for telling a DOM snapshot's copy of a page's
    form from the recorded page's: where it posts (a formless group's target is
    ``("", "")``, which no form's is), the names of its controls that are not hidden
    inputs (``LoginForm.visible_names``, a hidden name holding an id included among
    the hidden), and its roles. Not its hidden inputs, attributes, or ids, which a
    page's script adds or changes after load (a device fingerprint field, an
    ``autocomplete``)."""
    visible = tuple(sorted(set(form.visible_names)))
    return (form.action_target, visible, tuple(sorted(form.fields)))


def _snapshot_page_forms(
    html: str,
    page_url: str,
    recorded_on_page: dict[tuple[str, str], set[tuple[object, ...]]],
    redirected_via: dict[tuple[str, str], tuple[str, ...]],
    dialog_shapes: set[tuple[tuple[str, str], tuple[object, ...]]] | None = None,
) -> tuple[str, tuple[LoginForm, ...]]:
    """The page a credential post's DOM snapshot shows (``request._domSnapshot``), as
    its URL the way the digest keeps one, and its login forms
    (:func:`graftpunk.har.documents.extract_login_forms` with ``snapshot``): each
    with that URL as its ``source``, and without the forms a recorded GET of the
    same page already held (by :func:`_visible_shape`), so a recorded copy is never
    doubled and never takes the snapshot's ``GP-FILL``. A left-out copy the snapshot
    shows inside a dialog-marked element (a server-rendered modal its script marks as
    one when it opens) adds its page and shape to *dialog_shapes*, so the recorded
    copy takes ``in_dialog``. Empty when *page_url* is not an http or https URL."""
    try:
        if urlparse(page_url).scheme.lower() not in _HTTP_SCHEMES:
            return "", ()
        source = _normal_url(bare_url(page_url))
    except ValueError:
        return "", ()
    here = urlparse(source)
    page = (normal_host(here.scheme, here.netloc), here.path or "/")
    shown = recorded_on_page.get(page, set())
    read = extract_login_forms(html, source=source, base=_unmasked_url(page_url), snapshot=True)
    forms = tuple(form for form in read if _visible_shape(form) not in shown)
    if dialog_shapes is not None:
        dialog_shapes.update(
            (page, _visible_shape(form))
            for form in read
            if form.in_dialog and _visible_shape(form) in shown
        )
    opened_from = _opened_from(redirected_via.get(page, ()), page[0])
    if opened_from:
        forms = tuple(replace(form, opened_from=opened_from) for form in forms)
    return source, forms


def _unique_forms(forms: list[LoginForm]) -> list[LoginForm]:
    """*forms* with a form recorded again (the same site-wide form on several pages)
    kept once, at its first position, as the copy whose selectors resolve best
    (fewest unresolved roles; then one from a recorded page over one from a DOM
    snapshot, whose step says a click may open it; the first of those)."""
    best: dict[tuple[object, ...], LoginForm] = {}
    order: list[tuple[object, ...]] = []
    for form in forms:
        key = _form_key(form)
        if key not in best:
            order.append(key)
            best[key] = form
        elif (len(form.unresolved_roles), form.from_snapshot) < (
            len(best[key].unresolved_roles),
            best[key].from_snapshot,
        ):
            best[key] = form
    return [best[key] for key in order]


def _has_password_field(entry: HAREntry) -> list[str]:
    """Field names on a POST whose body has a password-like field."""
    return _password_like(body_params(entry))


def _password_like(names: Iterable[str]) -> list[str]:
    return [name for name in names if any(hint in name.lower() for hint in PASSWORD_NAME_HINTS)]


def _asks_for_a_new_password(names: Iterable[str]) -> bool:
    return any(looks_like_new_password_name(name) for name in names)


def password_field_names(body_text: str | None, content_type: str = "") -> list[str]:
    """The password-like field names (a ``PASSWORD_NAME_HINTS`` name) of a request
    body, read as the digest reads a POST's body (:func:`body_params`) from its text
    and the request's ``Content-Type``. The one rule for a credential post's fields:
    the digest reads a recorded POST with it, and the recorder reads a request's body
    with it while the request is in flight (the DOM snapshot of #267)."""
    declared = content_type.split(";")[0].strip().lower()
    types, _kind, _dropped = _parse_body_text(body_text, declared)
    return _password_like(types)


def is_credential_body(body_text: str | None, content_type: str = "") -> bool:
    """Whether a POST body makes its request a credential post by its field names: it
    has a password-like field (:func:`password_field_names`) and none asks for a new
    password (``new_password``, ``password_confirm``: a sign-up or a password
    change)."""
    names = password_field_names(body_text, content_type)
    return bool(names) and not _asks_for_a_new_password(names)


def _count_ids(names: set[str]) -> int:
    """How many of the dropped *names* were dropped because they hold an id; the
    rest were dropped for not being field names."""
    return sum(1 for name in names if holds_an_id(name))


# One fixture candidate: the best (lowest) (rank, entry_index) key seen for its
# media type so far (graftpunk.har.naming.fixture_rank, then HAR entry order), the
# falsy JSON value of that recording's body when the type reads as JSON, its JSON
# shape under the same condition, and the raw index (parse_har_file's own order)
# of the HAR entry that recording came from.
_FixtureCandidate = tuple[tuple[int, int], str | None, ShapeNode | None, int, InlineJson | None]


def _is_json_media_type(media_type: str) -> bool:
    """True when *media_type* (already normalised: lower case, no parameters) is a
    JSON type: matches every other JSON check in this module
    (:func:`_response_shape`, ``devtools/scaffold/render.py``'s
    ``_is_json_endpoint``)."""
    return "json" in media_type


def _fixture_type_of(
    candidates: dict[str, _FixtureCandidate], content_types: dict[str, int]
) -> str | None:
    """Which of *candidates*' normalised media types is the endpoint's fixture type
    (:func:`graftpunk.har.naming.fixture_order`'s rule, run once per endpoint): the
    most-recorded type (*content_types*, every recording of that type, body or not)
    among those with a recording that carries a body (a candidate of rank 0); a
    count tie goes to a JSON type, then to the type whose winning candidate came
    first. Only when no candidate type has a body at all does the same rule run
    over every candidate type instead. None when there are no candidates (the
    endpoint's fixture_written is False)."""
    if not candidates:
        return None
    body_types = {media_type for media_type, (key, *_rest) in candidates.items() if key[0] == 0}
    pool = body_types or set(candidates)

    def rank(media_type: str) -> tuple[int, bool, int]:
        return (
            -content_types.get(media_type, 0),
            not _is_json_media_type(media_type),
            candidates[media_type][0][1],
        )

    return min(pool, key=rank)


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
        # Whether any recorded response carried a body: equivalent to "no candidate
        # type has a body" (a candidate's text is the response body itself whenever
        # the body is not None), so response_body_empty also says whether the
        # fixture recording finish() picks has one.
        self.bodied = False
        self.fixture_candidates: dict[str, _FixtureCandidate] = {}
        self.custom_headers: set[str] = set()
        self.examples: list[str] = []
        # Dropped names, held only to count them distinctly; never kept past finish.
        self.dropped_query: set[str] = set()
        self.dropped_body: set[str] = set()
        self.dropped_headers: set[str] = set()
        self.body_not_captured = 0
        self.body_omitted = 0
        self.body_binary = 0
        self.body_binary_file = ""
        self.body_binary_size: int | None = None

    def record(self, entry: HAREntry, path: str, step: int, entry_index: int) -> None:
        text = capture_text(entry.response.body, entry.response.status)
        # A recording gp observe fixtures writes nothing for is never the fixture.
        if text is not None:
            recorded_type = normalize_media_type(entry.response.content_type or "")
            key = (fixture_rank(text), step)
            best = self.fixture_candidates.get(recorded_type)
            if best is None or key < best[0]:
                is_json = _is_json_media_type(recorded_type)
                falsy = _falsy_value(text) if is_json else None
                shape = _response_shape(entry) if is_json else None
                state = _inline_json_of(recorded_type, text)
                self.fixture_candidates[recorded_type] = (key, falsy, shape, entry_index, state)
        method = entry.request.method.upper()
        if method not in self.methods:
            self.methods.append(method)
        self.count += 1
        self.statuses.append(entry.response.status)
        content_type = normalize_media_type(entry.response.content_type or "")
        self.content_types[content_type] = self.content_types.get(content_type, 0) + 1
        query_types, query_dropped = _query_param_types(entry.request.url)
        _merged_types(self.query_params, query_types, json_body=False)
        self.dropped_query |= query_dropped
        field_types, body_kind, body_dropped = _parse_body_keys(entry)
        _merged_types(self.body_params, field_types, json_body=body_kind == "json")
        self.dropped_body |= body_dropped
        if entry.request.post_data_binary and not entry.request.post_data:
            self.body_binary += 1
            if not self.body_binary_file:
                self.body_binary_file = entry.request.post_data_file or ""
                self.body_binary_size = entry.request.post_data_size
        elif entry.request.post_data_not_captured and not entry.request.post_data:
            if entry.request.post_data_omitted:
                self.body_omitted += 1
            else:
                self.body_not_captured += 1
        if body_kind != "none":
            self.body_kind = body_kind
        if entry.response.body:
            self.bodied = True
        header_names, headers_dropped = _custom_headers(entry)
        self.custom_headers.update(header_names)
        self.dropped_headers |= headers_dropped
        if path not in self.examples and len(self.examples) < _MAX_ENDPOINT_EXAMPLES:
            self.examples.append(path)

    def absorb(self, acc: _EndpointAccumulator) -> None:
        """Add *acc*'s recordings to this one's: a member joining a family (the
        high-cardinality collapse's, or a family row's). *acc* is read and never
        changed."""
        self.count += acc.count
        self.statuses.extend(acc.statuses)
        for ct, n in acc.content_types.items():
            self.content_types[ct] = self.content_types.get(ct, 0) + n
        _merged_types(self.query_params, acc.query_params, json_body=False)
        json_body = "json" in (self.body_kind, acc.body_kind)
        _merged_types(self.body_params, acc.body_params, json_body=json_body)
        self.custom_headers.update(acc.custom_headers)
        self.dropped_query |= acc.dropped_query
        self.dropped_body |= acc.dropped_body
        self.bodied = self.bodied or acc.bodied
        self.body_not_captured += acc.body_not_captured
        self.body_omitted += acc.body_omitted
        self.body_binary += acc.body_binary
        if not self.body_binary_file:
            self.body_binary_file = acc.body_binary_file
            self.body_binary_size = acc.body_binary_size
        # The family's fixture candidates are its members' first recordings of
        # each type, whichever member recorded them.
        for recorded_type, candidate in acc.fixture_candidates.items():
            best = self.fixture_candidates.get(recorded_type)
            if best is None or candidate[0] < best[0]:
                self.fixture_candidates[recorded_type] = candidate
        self.dropped_headers |= acc.dropped_headers
        # The first member of a collapsed family answers for the family, so a
        # member that happened to redirect or return HTML must not cost the
        # merged endpoint its request body kind. Its fixture recording (shape
        # included, merged into fixture_candidates above) is decided the same
        # way as any other endpoint's, from the family's combined candidates.
        if self.body_kind == "none" and acc.body_kind != "none":
            self.body_kind = acc.body_kind
        for example in acc.examples:
            if example not in self.examples and len(self.examples) < _MAX_ENDPOINT_EXAMPLES:
                self.examples.append(example)

    def finish(self, template: str) -> Endpoint:
        primary_content_type = max(
            self.content_types, key=lambda ct: self.content_types[ct], default=""
        )
        candidates = self.fixture_candidates
        fixture_type = _fixture_type_of(candidates, self.content_types)
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
            shape=None if fixture_type is None else candidates[fixture_type][2],
            custom_headers=tuple(sorted(self.custom_headers)),
            examples=tuple(self.examples),
            query_keys_dropped_as_ids=_count_ids(self.dropped_query),
            query_keys_dropped_as_non_names=len(self.dropped_query)
            - _count_ids(self.dropped_query),
            body_keys_dropped_as_ids=_count_ids(self.dropped_body),
            body_keys_dropped_as_non_names=len(self.dropped_body) - _count_ids(self.dropped_body),
            header_names_dropped_as_ids=len(self.dropped_headers),
            body_not_captured=self.body_not_captured,
            body_omitted=self.body_omitted,
            body_binary=self.body_binary,
            body_binary_file=self.body_binary_file,
            body_binary_size=self.body_binary_size,
            response_body_empty=not self.bodied,
            falsy_first_response=None if fixture_type is None else candidates[fixture_type][1],
            fixture_content_type="" if fixture_type is None else fixture_type,
            fixture_written=bool(candidates),
            fixture_entry_index=None if fixture_type is None else candidates[fixture_type][3],
            inline_json=None if fixture_type is None else candidates[fixture_type][4],
        )


def _falsy_value(body: str | None) -> str | None:
    """*body*, a JSON response's, as :attr:`Endpoint.falsy_first_response` records
    it: the falsy JSON value it parses to (``{}``, ``[]``, ``""``, ``0``,
    ``false``, or ``null``) as canonical JSON, and None for anything else, an empty
    body included (``Endpoint.response_body_empty`` records that)."""
    if not body:
        return None
    try:
        value = json.loads(body)
    except ValueError:
        return None
    return None if value else json.dumps(value)


def _collapse_eligible(segment: str) -> bool:
    """True when *segment* may stand in for a parameter in a collapsed family.

    ``looks_dynamic`` relaxed by one case: any segment carrying a digit. A
    segment ``looks_dynamic`` keeps literal can still carry one (``red-widget-2``,
    ``page2``), and a run of many such siblings identifies a parameter position.
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
    ``/account/settings`` merged into ``/account/{account_id}``).
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
        # the accumulator was keyed on.
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
        if observation.kind not in _LOGIN_FLOW_KINDS or not observation.login_flow:
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
    classified: list[tuple[HAREntry, str, bool, int]] = []
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
        # One spelling per host, so an entry recorded as API.example.com:443 counts
        # with, and is in scope with, api.example.com.
        host = normal_host(parsed_url.scheme, parsed_url.netloc)
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
        classified.append((entry, host, static, index))

    primary_host = _primary_host(non_static_hosts, document_hosts)
    scope_root = _scope_root(primary_host) if primary_host else ""

    accumulators: dict[tuple[str, str], _EndpointAccumulator] = {}
    login_forms: list[LoginForm] = []
    token_seen: dict[tuple[TokenKind, str], list[str]] = {}
    cookies_seen: dict[str, None] = {}
    id_cookie_names: set[str] = set()
    login: list[LoginObservation] = []
    # Each observation with the step it was classified at, the kind it falls back
    # to when it is a form page no credential post claims, its forms' targets, and
    # whether one of its forms has no target of its own (an empty, #, or javascript:
    # action, or one naming the page itself).
    pending: list[
        tuple[int, ObservationKind | None, LoginObservation, set[tuple[str, str]], bool]
    ] = []
    # Each credential post: its step, its target, its body names, and whether it was
    # found by its field names alone (no recorded form's target matched it).
    posts: list[tuple[int, tuple[str, str], frozenset[str], bool]] = []
    # Beside each of posts: when it was sent, and the clicks and focus moves gp
    # observe recorded before it (request._loginInteractions).
    post_interactions: list[tuple[datetime, tuple[LoginInteraction, ...]]] = []
    # When each in-scope navigation (_is_page_load) was sent: a page load.
    page_loads: list[datetime] = []
    # The (page, visible shape) of each recorded form a snapshot showed inside a
    # dialog-marked element (_snapshot_page_forms).
    dialog_shapes: set[tuple[tuple[str, str], tuple[object, ...]]] = set()
    # For the suggested success selectors (graftpunk.har.login_signals): every page
    # the run saw logged out (a response holding a login form, a credential post's
    # DOM snapshot, any page loaded before the first credential post); each page
    # loaded after one that holds no login form, in order, the last of which is the
    # final page when page-source.html is not; the indexes of those after which the
    # browser sent a step-shaped top-level document POST (login_signals.
    # is_login_step_post: an MFA or "stay signed in" form) before loading another
    # page; the index of the after-post page that is the latest page load, if one
    # is; and the credential posts, whose account values (_account_values) no
    # suggested name may hold.
    logged_out_pages: list[str] = []
    after_post_pages: list[str] = []
    submitted_a_step: set[int] = set()
    latest_after_post: int | None = None
    credential_entries: list[HAREntry] = []
    # Beside each of credential_entries: how many after_post_pages there were when it
    # was seen, so the pages after the real login are known (_failure_candidates).
    after_pages_at_post: list[int] = []
    # Every request with a body, in order, and how many of them were sent up to and
    # including the last credential post: an identifier-first login posts the
    # username in a step before the password post (_failure_candidates).
    bodied_entries: list[HAREntry] = []
    bodied_through_login = 0
    # Whether the browser sent a step-shaped POST (a document form or a script's)
    # after the last credential post: its after-read (_after_read) may show that step.
    stepped_since_last_post = False
    page_of: dict[int, int] = {}
    # The id of each recorded form with no target of its own (_script_driven_forms).
    script_driven: set[int] = set()
    # Where a GET's redirect sent the client (host and path), to the URLs of the GETs
    # of that redirect chain so far, in order.
    redirected_via: dict[tuple[str, str], tuple[str, ...]] = {}
    # The forms (by _visible_shape) each recorded GET page held, by host and path: a
    # DOM snapshot of that page adds only the forms its recorded HTML lacked.
    recorded_on_page: dict[tuple[str, str], set[tuple[object, ...]]] = {}
    credential_post_steps: list[int] = []
    # The credential post a redirect or set-cookie observation followed, by step:
    # only a hop that continues that post's redirect chain.
    follows_post: dict[int, int] = {}
    # For each credential post, the host and path its redirect chain goes to next,
    # and the targets of the forms on its last hop's page (an OAuth form_post page
    # whose form a script submits to the app's callback).
    chain_next: dict[int, tuple[str, str]] = {}
    # With the host of the page that served them: an OAuth form_post crosses hosts.
    chain_forms: dict[int, tuple[str, dict[tuple[str, str], frozenset[str]]]] = {}
    # A credential post whose chain's last hop was a form_post submission answering
    # without a redirect, to that hop's status.
    chain_ended_on_submission: dict[int, int] = {}
    # Counts only the entries that reach classification: static and out-of-scope
    # entries between a credential post and its redirect do not use up the window.
    step = 0
    order = 0
    page_forms: tuple[LoginForm, ...] = ()
    page_html = ""
    if source.page_source is not None and source.page_source.is_file():
        page_html = source.page_source.read_text(encoding="utf-8", errors="replace")
        page_forms = extract_login_forms(page_html, source=str(source.page_source))
    # The paths a recorded login form posts to: a POST to one is the credential
    # post whatever its password field is named, since the form's type="password"
    # input already names it. No assignment expression here: one inside a
    # comprehension binds in this function and rebound the loop's `path` below.
    login_action_targets = {form.action_target for form in page_forms if form.action_target[1]}

    for entry, host, static, entry_index in classified:
        if entry.request.post_data:
            bodied_entries.append(entry)
        if static:
            dropped["static"] += 1
            continue
        in_scope = all_hosts or _in_scope(host, scope_root)
        if not in_scope:
            dropped["third_party"] += 1
            continue
        if _body_missing(entry):
            dropped["error"] += 1
            if entry.response.body_file_outside:
                LOG.warning("digest_body_file_outside_har_dir", url=bare_url(entry.request.url))
            else:
                LOG.warning("digest_body_file_missing", url=bare_url(entry.request.url))
            continue

        method = entry.request.method.upper()
        credential_hint_fields = _has_password_field(entry) if method == "POST" else []
        # Kept before the new-password exclusion below: a POST carrying any password
        # field is never a redirect hop of another post.
        carries_a_password = bool(credential_hint_fields)
        # A body asking for a new password is a password change or a sign-up, never
        # a login found by its field names.
        if _asks_for_a_new_password(credential_hint_fields):
            credential_hint_fields = []
        post_target = _request_target(entry.request.url) if method == "POST" else ("", "")
        snapshot_html = entry.request.dom_snapshot if method == "POST" else None
        if snapshot_html is not None and (
            credential_hint_fields or _posts_to_a_login_form(post_target, login_action_targets)
        ):
            # The page the credential post was sent from, as the recorder saved it
            # (#267): read as a GET page one step before the post, so the post finds
            # its forms the way it finds a recorded page's.
            page_url = entry.request.dom_snapshot_url or ""
            logged_out_pages.append(snapshot_html)
            snapshot_source, snapshot_forms = _snapshot_page_forms(
                snapshot_html, page_url, recorded_on_page, redirected_via, dialog_shapes
            )
            if snapshot_forms:
                step += 1
                login_forms.extend(snapshot_forms)
                page_of.update((id(form), step) for form in snapshot_forms)
                scripted_here = _script_driven_forms(page_url, snapshot_forms)
                script_driven.update(id(form) for form in scripted_here)
                pending.append(
                    (
                        step,
                        None,
                        LoginObservation(
                            order=0,
                            method="GET",
                            url=snapshot_source,
                            status=200,
                            kind="form_page",
                            fields=(),
                        ),
                        {form.action_target for form in snapshot_forms},
                        bool(scripted_here),
                    )
                )
                login_action_targets.update(
                    form.action_target for form in snapshot_forms if form.action_target[1]
                )

        step += 1
        # Path only: every URL the digest keeps goes through paths.bare_url, so
        # no query, fragment, ;params, or userinfo reaches an example, a
        # template, a login observation, a form source, or a token's seen_on. Its
        # host is spelled as the endpoint's (normal_host), so a login observation
        # and the endpoint it belongs to name one host.
        url = _normal_url(bare_url(entry.request.url))
        path = urlparse(url).path or "/"
        raw_template, _ = template_path(path)
        key = (method, raw_template)
        acc = accumulators.setdefault(key, _EndpointAccumulator(host=host))
        acc.record(entry, path, step, entry_index)

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

        here = urlparse(url)
        here_host = normal_host(here.scheme, here.netloc)
        via = redirected_via.get((here_host, path), ()) if method == "GET" else ()
        if method == "GET" and entry.response.status in _REDIRECT_STATUSES:
            redirected_via[_redirect_target(entry)] = (*via, url)
        content_type = (entry.response.content_type or "").lower()
        if _is_page_load(entry, method, content_type):
            page_loads.append(entry.timestamp)
        # A step-shaped POST after a credential post: a document form, or one a
        # script sent (an SPA's MFA code by fetch), which only the after-read's rule
        # reads; the per-page rule (submitted_a_step) reads document forms only.
        step_shaped = (
            method == "POST"
            and bool(credential_post_steps)
            and is_login_step_post(
                path,
                {
                    *body_params(entry),
                    *(
                        name
                        for names, _value in body_values(
                            entry.request.post_data, _request_content_type(entry)
                        )
                        for name in names
                    ),
                },
            )
        )
        stepped_since_last_post = stepped_since_last_post or step_shaped
        if _is_top_level_page_load(entry, method, content_type):
            if step_shaped and latest_after_post is not None:
                submitted_a_step.add(latest_after_post)
            latest_after_post = None
        forms_in_entry: tuple[LoginForm, ...] = ()
        if "html" in content_type and entry.response.body:
            document_source = url
            forms_in_entry = extract_login_forms(
                entry.response.body, source=document_source, base=_unmasked_page(entry)
            )
            # Any response holding a login form shows the site logged out, a failed
            # attempt re-rendered by a POST included.
            holds_a_login_form = bool(forms_in_entry)
            if method != "GET":
                # Only a page the run opened shows a dialog login: a POST's HTML (a
                # failed attempt re-rendered, a search result) is never its page, its
                # url, or a second page making it site-wide.
                forms_in_entry = tuple(form for form in forms_in_entry if not form.formless)
            opened_from = _opened_from(via, here_host)
            if opened_from:
                forms_in_entry = tuple(
                    replace(form, opened_from=opened_from) for form in forms_in_entry
                )
            login_forms.extend(forms_in_entry)
            page_of.update((id(form), step) for form in forms_in_entry)
            if method == "GET":
                recorded_on_page.setdefault((here_host, path), set()).update(
                    _visible_shape(form) for form in forms_in_entry
                )
            script_driven.update(
                id(form) for form in _script_driven_forms(entry.request.url, forms_in_entry)
            )
            for candidate in extract_token_candidates(entry.response.body, source=document_source):
                token_key = (candidate.kind, candidate.name)
                token_seen.setdefault(token_key, []).extend(candidate.seen_on)
            if holds_a_login_form:
                logged_out_pages.append(entry.response.body)
            elif (
                method == "GET"
                and 200 <= entry.response.status < 300
                and _is_top_level_page_load(entry, method, content_type)
            ):
                if credential_post_steps:
                    latest_after_post = len(after_post_pages)
                    after_post_pages.append(entry.response.body)
                else:
                    logged_out_pages.append(entry.response.body)

        by_target = method == "POST" and _posts_to_a_login_form(post_target, login_action_targets)
        kind: ObservationKind | None = None
        fields: tuple[str, ...] = ()
        # A page holding only dialog logins that a login's redirect chain lands on (a
        # layout carrying the modal on every page) is that chain's hop, as it was
        # before dialogs were read, not a form page.
        lands_a_chain = (
            bool(forms_in_entry)
            and all(form.formless for form in forms_in_entry)
            and bool(credential_post_steps)
            and step - credential_post_steps[-1] <= _LOGIN_WINDOW
            and _continues_chain(
                here_host, path, chain_next.get(credential_post_steps[-1], ("", ""))
            )
        )
        if method == "GET" and forms_in_entry and not lands_a_chain:
            kind = "form_page"
        elif method == "POST" and (credential_hint_fields or by_target):
            # Report every body field name, not only the password-hinted
            # ones: a credential post's username/email field is part of the
            # observation too.
            kind, fields = "credential_post", tuple(sorted(body_params(entry)))
        elif (
            credential_post_steps
            and step - credential_post_steps[-1] <= _LOGIN_WINDOW
            and not carries_a_password
        ):
            # A hop of the login's redirect chain only when it continues it: its path
            # is where the previous hop sent the client. Anything else in the window
            # (a later POST answering 302, say) is observed but not the login's.
            last_post = credential_post_steps[-1]
            hop_url = urlparse(url)
            hop_host = normal_host(hop_url.scheme, hop_url.netloc)
            by_path = _continues_chain(hop_host, path, chain_next.get(last_post, ("", "")))
            by_form = (
                not by_path
                and method == "POST"
                and _submits_a_chain_form(
                    hop_host,
                    post_target,
                    frozenset(body_params(entry)),
                    chain_forms.get(last_post, ("", {})),
                )
            )
            if by_path or by_form:
                follows_post[step] = last_post
                chain_next[last_post] = _redirect_target(entry)
                chain_forms[last_post] = (hop_host, _hop_form_targets(entry))
                # A submission answering a page leaves the client on it, a URL no
                # redirect named.
                if by_form and entry.response.status not in _REDIRECT_STATUSES:
                    chain_ended_on_submission[last_post] = entry.response.status
                else:
                    chain_ended_on_submission.pop(last_post, None)
            if entry.response.status in _REDIRECT_STATUSES:
                kind = "redirect"
            elif _response_cookie_names(entry):
                kind = "set_cookie"
                fields = tuple(n for n in _response_cookie_names(entry) if not holds_an_id(n))
        fallback: ObservationKind | None = "auth_api" if _AUTH_URL_REGEX.search(path) else None
        if kind is None:
            kind = fallback

        if kind is not None:
            pending.append(
                (
                    step,
                    fallback,
                    LoginObservation(
                        order=0,
                        method=method,
                        url=url,
                        status=entry.response.status,
                        kind=kind,
                        fields=fields,
                        redirect_to=_redirect_target_path(entry),
                    ),
                    {form.action_target for form in forms_in_entry},
                    bool(_script_driven_forms(entry.request.url, forms_in_entry)),
                )
            )
            if kind == "credential_post":
                credential_post_steps.append(step)
                # Seeded from the post's redirect only: a credential post answering
                # 200 starts no chain, whatever its page holds.
                chain_next[step] = _redirect_target(entry)
                posts.append((step, post_target, frozenset(body_params(entry)), not by_target))
                post_interactions.append((entry.timestamp, entry.request.login_interactions))
                credential_entries.append(entry)
                after_pages_at_post.append(len(after_post_pages))
                bodied_through_login = len(bodied_entries)
                stepped_since_last_post = False
        # Recorded after this entry is classified, and only from a page a GET
        # served: a form in a POST's own response (a site-wide header form) must
        # not make that POST, or a later one to the same page, the credential post.
        if method == "GET":
            login_action_targets.update(
                form.action_target for form in forms_in_entry if form.action_target[1]
            )

    # Each credential post promotes one form page. Among the earlier pages whose form
    # posts where it went: the one whose matching form is on the fewest recorded
    # pages, then the one whose form covers more of the post's body names, then the
    # nearest. Else, for a post found by its field names alone, the nearest earlier
    # page whose form has no target of its own (an empty, #, or javascript: action,
    # or one naming the page itself), and none when every earlier form posts
    # somewhere real. Every other page carrying a login form is an ordinary page (a
    # site-wide header form) and keeps its stub. By target, not distance, so the
    # assets a login page loads cannot push it out of reach.
    form_pages = [
        (at, targets, scripted) for at, _f, o, targets, scripted in pending if o.kind == "form_page"
    ]
    credential_posts: list[_CredentialPost] = []
    # Per credential post: credited to its page through a formless group shown on
    # several recorded pages (_formless_credit), which makes no page a login page.
    site_wide: list[bool] = []
    pages_of_key: dict[tuple[object, ...], set[int]] = {}
    forms_on_page: dict[int, list[LoginForm]] = {}
    for form in login_forms:
        if id(form) in page_of:
            pages_of_key.setdefault(_form_key(form), set()).add(page_of[id(form)])
            forms_on_page.setdefault(page_of[id(form)], []).append(form)

    def page_rank(at: int, target: tuple[str, str], body_names: frozenset[str]) -> tuple[int, int]:
        """How a page whose form posts to *target* ranks for a post carrying
        *body_names*: by its best matching form, each form ranked whole, first by how
        many recorded pages it is on (a dedicated login page's form is on fewer than
        a site-wide header form), then by how many of the body names it covers (more
        first). One form's page count is never paired with another form's coverage.
        *at* is a page ``matching`` found by one of its forms' targets, so it always
        has at least one such form."""
        return min(
            (len(pages_of_key[_form_key(form)]), -len(set(form.input_names) & body_names))
            for form in forms_on_page.get(at, [])
            if _target_matches(form.action_target, target)
        )

    # No dialog is credited at all when a real login form recorded anywhere in the
    # run (a saved page source's included) shares a visible input name with any
    # credential post of the run; a hidden input (a site-wide CSRF field) is no
    # shared name. In such a run the login, the login flow, and login_config are as
    # before dialogs were read: a post not credited to a dialog goes to whatever the
    # rules above give, often no form, and the real form is the login by ranking.
    # The dialog is still listed among the login forms.
    real_form_shares_a_post_name = bool(
        {
            name
            for form in (*login_forms, *page_forms)
            if not form.formless
            for name in set(form.input_names) - set(form.hidden)
        }
        & {name for _step, _target, names, _by_names in posts for name in names}
    )
    for post_step, went_to, body_names, by_field_names in posts:
        earlier = [page for page in form_pages if page[0] < post_step]
        matching = [at for at, targets, _s in earlier if _posts_to_a_login_form(went_to, targets)]
        scripted = [at for at, _targets, is_scripted in earlier if is_scripted]
        # Among pages whose form posts where the post went, the page-specific form's
        # page wins over a nearer page with only a site-wide header form; then the
        # page whose form covers more of the post's body names; then the nearest.
        # Only a post found by its field names alone falls back to a scripted page;
        # one matched by a recorded form (a saved page source's included) does not.
        if matching:
            chosen: int | None = min(
                matching,
                key=lambda at, to=went_to, names=body_names: (*page_rank(at, to, names), -at),
            )
        else:
            chosen = max(scripted if by_field_names else [], default=None)
        credit = (
            _formless_credit(post_step, body_names, forms_on_page)
            if by_field_names and chosen is None and not real_form_shares_a_post_name
            else None
        )
        formless_key = None
        if credit is not None:
            chosen, formless_key = credit
        # Several pages, not several loads: a reload, or a failed attempt landing
        # back on the page, keeps one page.
        site_wide.append(
            credit is not None
            and len(
                {
                    _request_target(form.source)
                    for form in login_forms
                    if id(form) in page_of and _form_key(form) == credit[1]
                }
            )
            > 1
        )
        credential_posts.append(
            _CredentialPost(went_to, body_names, chosen, by_field_names, formless_key)
        )
    promoted = {post.page_step for post in credential_posts}

    logged_in_pages = _pages_after_the_real_login(
        credential_entries, after_pages_at_post, after_post_pages
    )
    if source.page_source is not None and source.page_source.is_file():
        logged_in_pages.append(page_html)
        # A run stopped on a page holding a login form stopped logged out, or on
        # another login.
        (logged_out_pages if page_forms else after_post_pages).append(page_html)
        page_label = str(source.page_source)
        login_forms.extend(page_forms)
        for candidate in extract_token_candidates(page_html, source=page_label):
            token_seen.setdefault((candidate.kind, candidate.name), []).extend(candidate.seen_on)
    # The body field names any recorded login form takes as its username, so a
    # credential post's username is known whatever the field is named.
    username_names = frozenset(form.username_name for form in login_forms if form.username_name)

    ranked_forms = _unique_forms(
        _used_forms_first(login_forms, page_of, credential_posts, script_driven)
    )
    owned_posts, owned_pages = _used_login(
        ranked_forms, login_forms, page_of, [step for step, *_rest in posts], credential_posts
    )

    # Each form on a page a credential post was credited to takes its opener from
    # the first such post that gives one (_login_opener).
    def _marked_by_a_snapshot(form: LoginForm) -> bool:
        try:
            here = urlparse(form.source)
        except ValueError:
            return False
        page = (normal_host(here.scheme, here.netloc), here.path or "/")
        return (page, _visible_shape(form)) in dialog_shapes

    marked_keys = {_form_key(form) for form in login_forms if _marked_by_a_snapshot(form)}
    openers: dict[tuple[object, ...], str] = {}
    for (post_at, interactions), post in zip(post_interactions, credential_posts, strict=True):
        if post.page_step is None or not interactions:
            continue
        for form in forms_on_page.get(post.page_step, []):
            # Only a login a click can open: a formless group (a dialog's, or a
            # snapshot's), or a real <form> inside a dialog-marked element. Any other
            # real <form> is the login as its page shows it, and a click before it
            # (a consent banner) need not come back at replay.
            if not (form.formless or form.in_dialog or _marked_by_a_snapshot(form)):
                continue
            key = _form_key(form)
            found = (
                None if key in openers else _login_opener(form, interactions, post_at, page_loads)
            )
            if found is not None:
                openers[key] = found
    ranked_forms = [
        replace(form, in_dialog=True) if _form_key(form) in marked_keys else form
        for form in ranked_forms
    ]
    ranked_forms = [
        replace(form, opener_candidate=openers[_form_key(form)])
        if _form_key(form) in openers
        else form
        for form in ranked_forms
    ]
    # A page a post was credited to only through a formless group shown on several
    # pages is a content page showing a site-wide modal, so its GET keeps its command.
    owned_pages -= {
        post.page_step for post, home in zip(credential_posts, site_wide, strict=True) if home
    } - {post.page_step for post, home in zip(credential_posts, site_wide, strict=True) if not home}
    for at, fallback, observation, _targets, _scripted in pending:
        kind = observation.kind
        if kind == "form_page" and at not in promoted:
            kind = fallback
        if kind is None:
            continue
        order += 1
        in_login = (
            (kind == "form_page" and at in owned_pages)
            or (kind == "credential_post" and at in owned_posts)
            or (kind in ("redirect", "set_cookie") and follows_post.get(at) in owned_posts)
        )
        # The chain's last hop is a 200 page whose form_post-shaped forms it did not
        # follow (that hop's forms are still the chain's own), or a submission that
        # answered a page.
        unresolved = ""
        if kind == "credential_post" and chain_forms.get(at, ("", {}))[1]:
            unresolved = "came to rest on a page holding a form it did not follow"
        elif kind == "credential_post" and at in chain_ended_on_submission:
            unresolved = f"ended when the last hop answered {chain_ended_on_submission[at]}"
        login.append(
            replace(
                observation,
                order=order,
                kind=kind,
                login_flow=in_login,
                landing_unresolved=unresolved,
            )
        )

    collapse_map = _collapse_high_cardinality([template for _method, template in accumulators])
    merged: dict[tuple[str, str], _EndpointAccumulator] = {}
    for (method, raw_template), acc in accumulators.items():
        final_template = collapse_map.get(raw_template, raw_template)
        merged_key = (method, final_template)
        if merged_key not in merged:
            merged[merged_key] = acc
        else:
            merged[merged_key].absorb(acc)

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
    # One credential post whose body the guard cannot read (None) suggests nothing.
    read_values = [_account_values(entry, username_names) for entry in credential_entries]
    account_values = (
        None
        if any(values is None for values in read_values)
        else [value for values in read_values if values is not None for value in values]
    )

    literal_endpoints = _with_login_flow(endpoints, tuple(login))
    return RunDigest(
        source=source,
        primary_host=primary_host,
        hosts=hosts,
        endpoints=literal_endpoints + _family_endpoints(literal_endpoints, merged),
        login=tuple(login),
        login_forms=tuple(ranked_forms),
        tokens=tokens,
        cookies=tuple(cookies_seen),
        dropped=dropped,
        collapsed_templates=collapse_map,
        cookie_names_dropped_as_ids=len(id_cookie_names),
        token_names_dropped_as_ids=len(id_token_names),
        # Only for a run that recorded a login. The final page is the last of
        # after_post_pages (page-source.html when it went there); success_candidates
        # finds the leading run of login steps among them. The last credential post's
        # after-read, when usable, only narrows what that page gives.
        success_candidates=success_candidates(
            logged_out_pages,
            after_post_pages[-1],
            earlier=after_post_pages[:-1],
            submitted_a_step=submitted_a_step,
            account_values=account_values,
            also_on=_after_read(credential_entries, stepped_since_last_post),
        )
        if credential_post_steps and after_post_pages and account_values is not None
        else (),
        failure_candidates=_failure_candidates(
            credential_entries,
            account_values,
            logged_in_pages,
            [
                value
                for entry in bodied_entries[:bodied_through_login]
                for value in posted_account_values(
                    entry.request.post_data,
                    _request_content_type(entry),
                    entry.request.method,
                    username_names,
                )
            ],
        ),
    )


# A segment that names a fixed part of a site's routing rather than a value: when
# one of these is what differs between two paths, they are two routes, not one
# route and a parameter. Compared case-insensitively. A version (v1, v2beta) holds
# a digit, so it is never letters-only and needs no entry here.
_FIXED_VOCABULARY = frozenset(
    {
        "admin",
        "ajax",
        "api",
        "apis",
        "assets",
        "auth",
        "graphql",
        "internal",
        "oauth",
        "public",
        "rest",
        "rpc",
        "search",
        "static",
        "www",
    }
)


def _letters_only(segment: str) -> bool:
    """True when every part of *segment* (:func:`graftpunk.har.paths.split_parts`) is
    one or more ASCII letters: ``north-region``, ``sale_items``. A placeholder, a
    percent-escape, an empty part, and a digit anywhere each make it something else.
    The segment comes from a template, so ``template_path`` has already turned one the
    path rule reads as dynamic into a placeholder."""
    return all(part.isascii() and part.isalpha() for part in split_parts(segment))


def _family_eligible(segment: str, *, last: bool) -> bool:
    """True when *segment* may be the one that varies across a family's members:
    letters-only, unchanged by ``quote(segment, safe="")``, not in the fixed
    vocabulary, and, as a path's last segment, not a file name (holding a ``.``:
    ``intro.html`` and ``setup.html`` are two documents). The generated stub quotes
    its parameter that way, so a value it would change (``north$region`` becomes
    ``north%24region``) would request a path no member's fixture is named for."""
    if not _letters_only(segment) or segment.lower() in _FIXED_VOCABULARY:
        return False
    if percent_encode(segment, safe="") != segment:
        return False
    return not (last and "." in segment)


def _top_level_shape(shape: ShapeNode | None) -> tuple[object, ...] | None:
    """What two family members must share of their response shape: its kind, an
    object's top-level keys (and whether they were cut at the cap), and an array's
    element kind; ``("none",)`` for an endpoint that returns no JSON. None for a
    shape that could not be read (``SHAPE_UNAVAILABLE``), which matches nothing."""
    if shape is None:
        return ("none",)
    if shape.kind == "unavailable":
        return None
    if shape.kind == "object":
        return ("object", frozenset(shape.children or {}), shape.truncated)
    if shape.kind == "array":
        return ("array", None if shape.item is None else shape.item.kind)
    return (shape.kind,)


def _segments(template: str) -> list[str]:
    stripped = template.strip("/")
    return stripped.split("/") if stripped else []


def _family_groups(
    endpoints: tuple[Endpoint, ...],
) -> dict[tuple[int, tuple[object, ...]], list[Endpoint]]:
    """*endpoints* grouped by the varying segment's position and everything else a
    family's members share; see :func:`_family_endpoints`."""
    groups: dict[tuple[int, tuple[object, ...]], list[Endpoint]] = {}
    for endpoint in endpoints:
        # One method each: the digest files an endpoint per method and template.
        if endpoint.login_flow:
            continue
        shape = _top_level_shape(endpoint.shape)
        segments = _segments(endpoint.template)
        if shape is None or len(segments) < 2:
            continue
        trailing = endpoint.template.endswith("/")
        for position, segment in enumerate(segments):
            if not _family_eligible(segment, last=position == len(segments) - 1):
                continue
            shared = (
                endpoint.methods[0],
                endpoint.host,
                endpoint.content_type,
                shape,
                trailing,
                _family_key(segments, position),
            )
            groups.setdefault((position, shared), []).append(endpoint)
    return groups


def _without_case_variants(members: list[Endpoint], position: int) -> list[Endpoint]:
    """*members* less every one whose value at *position* equals another member's
    there but for letter case: ``Items`` and ``items`` are one word spelled two ways,
    not two values of a parameter."""
    folded: dict[str, int] = {}
    for member in members:
        value = _segments(member.template)[position].casefold()
        folded[value] = folded.get(value, 0) + 1
    return [m for m in members if folded[_segments(m.template)[position].casefold()] == 1]


def _family_endpoints(
    endpoints: tuple[Endpoint, ...],
    accumulators: dict[tuple[str, str], _EndpointAccumulator],
) -> tuple[Endpoint, ...]:
    """One more endpoint for each family among *endpoints*, sorted by template.

    A family is two or more endpoints, none of them part of the login flow, that
    share their method, host, content type, top-level response shape
    (:func:`_top_level_shape`), segment count of two or more, and trailing slash, and
    whose templates agree in every segment but one, that one passing
    :func:`_family_eligible` in each. A single segment is no family: every root route
    (``/about``, ``/contact``) would join one. Members whose values differ only in
    letter case are one word, so none of them joins (:func:`_without_case_variants`).
    An endpoint that fits two families (it differs from some siblings in one segment
    and from others in another) joins neither, and a family left with fewer than two
    members by that is dropped. The family's template holds
    :func:`graftpunk.har.paths.family_param_name`'s placeholder at the varying
    position. Two families that would have the same method and template (an HTML and
    a JSON family under one prefix) are both dropped: the template would name either,
    and a row a command could take for the wrong one is worse than no row. Its
    recordings are its members', merged as the high-cardinality collapse merges a
    family's, from *accumulators* (keyed by method and template, and only read). The
    members' own endpoints stay exactly as they are.
    """
    qualifying = []
    for key, members in _family_groups(endpoints).items():
        distinct = _without_case_variants(members, key[0])
        if len(distinct) > 1:
            qualifying.append((key[0], distinct))
    fits: dict[tuple[str, str], int] = {}
    for _position, members in qualifying:
        for member in members:
            ident = (member.methods[0], member.template)
            fits[ident] = fits.get(ident, 0) + 1

    planned: list[tuple[str, str, int, str, list[Endpoint]]] = []
    for position, members in qualifying:
        method = members[0].methods[0]
        kept = sorted(
            (m for m in members if fits[(method, m.template)] == 1), key=lambda m: m.template
        )
        if len(kept) < 2:
            continue
        segments = _segments(kept[0].template)
        taken = {segment[1:-1] for segment in segments if is_placeholder(segment)}
        name = family_param_name(segments[position - 1] if position else "", taken)
        segments[position] = "{" + name + "}"
        template = "/" + "/".join(segments) + ("/" if kept[0].template.endswith("/") else "")
        planned.append((method, template, position, name, kept))
    templates: dict[tuple[str, str], int] = {}
    for method, template, *_rest in planned:
        templates[(method, template)] = templates.get((method, template), 0) + 1

    families: list[Endpoint] = []
    for method, template, position, name, kept in planned:
        if templates[(method, template)] > 1:
            continue
        family = _EndpointAccumulator(kept[0].host)
        family.methods.append(method)
        for member in kept:
            family.absorb(accumulators[(method, member.template)])
        finished = family.finish(template)
        fixture_member = next(
            (
                member
                for member in kept
                if any(
                    candidate[3] == finished.fixture_entry_index
                    for candidate in accumulators[
                        (method, member.template)
                    ].fixture_candidates.values()
                )
            ),
            None,
        )
        families.append(
            replace(
                finished,
                family_members=tuple(member.template for member in kept),
                family_placeholder=name,
                family_fixture_value=""
                if fixture_member is None
                else _segments(fixture_member.template)[position],
            )
        )
    return tuple(sorted(families, key=lambda e: (e.template, e.methods)))


def family_values(endpoint: Endpoint) -> tuple[str, ...]:
    """The varying segment's value in each of *endpoint*'s family members, in member
    order; empty for an endpoint that is no family."""
    if not endpoint.family_members:
        return ()
    position = _segments(endpoint.template).index("{" + endpoint.family_placeholder + "}")
    return tuple(_segments(member)[position] for member in endpoint.family_members)


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
    token candidate's name, and every cookie name any of *entries* sets or sends,
    whatever its host and whether or not the digest counted it as static, except
    a cookie that only ever holds words (below). A sent cookie counts because a
    recording made with a cached session never sees the session set: the
    browser holds it before the recording starts, and only requests carry it.

    ``d.cookies`` stays the digest's own scoped list; *entries* widens only this
    safety net. ``gp observe fixtures`` passes every entry in the HAR, since a
    ``--match`` glob can write a capture from a static response or an
    out-of-scope host, and a cookie that entry set must still be looked for. It
    lives beside the digest that records the names, so the sidecar writer takes
    plain strings and imports nothing from the digest.

    The list stands in for a leak the suite cannot see: a copied cookie or token
    whose value sits next to its name. A cookie whose every recorded value is
    form state holds no credential, and its name is often an ordinary word that
    an invented fixture keeps as a key or in prose (``text``, ``lot``), so it is
    left out. A value is form state when, with one pair of surrounding double
    quotes taken off, it reads as words (graftpunk.har.paths.reads_as_words: a
    search term, a lot number, a date) or as cased words
    (graftpunk.har.paths.reads_as_cased_words: ``LANDOWNER``, ``currentTab``,
    ``SMITH, JOHN``), or when it is ASCII punctuation and spaces alone (``""``,
    a field cleared to a quoted empty string). Every other cookie stays:

    - one whose name reads as a token name (``csrftoken``) or names a session
      (:func:`_names_a_session`: ``sessionid``, ``PHPSESSID``, ``connect.sid``),
      whatever its values;
    - one any response in *entries* set with ``HttpOnly``, whatever its values:
      a server hides a cookie from scripts to protect it, and form state written
      by script never carries the flag. The flag counts when the HAR's
      ``response.cookies`` entry says ``httpOnly`` is true or when a
      ``Set-Cookie`` header carries the attribute: the array often leaves it out
      when the header has it, so an absent flag there is unknown, not false;
    - one with any value that is not form state: a value holding an id
      (graftpunk.har.paths.holds_an_id) or any other value neither rule reads
      as words (a session id);
    - one any of *entries* records with no value at all: a ``Set-Cookie`` the
      recorder kept only as a name, or a cookie whose HAR entry has no
      ``value`` field (parsed as ``None``), whatever its other values;
    - one with no non-empty value *entries* show, which covers a cookie a
      response only ever set empty and a call without *entries*, where every
      cookie stays, unless no response sets it and a request sent it empty.
      (``gp observe digest`` does not call this; its cookie list,
      ``d.cookies``, names every cookie.)

    An empty value beside a non-empty one is a cleared field or an expired
    cookie, and holds nothing. A cookie a response only ever set empty stays
    flagged, since a recorder can write a ``Set-Cookie`` it did not fully parse
    as an empty ``response.cookies`` entry (the header can hold the value the
    array lost). When no response sets a cookie, a request sending it empty is
    evidence it held nothing: that is what the browser sent. A value of punctuation alone
    (``""``) is a value the server did send. Values are read from every entry's
    request and response cookies, whatever its host. ``Secure`` is not a guard:
    a site served only over HTTPS can mark every cookie Secure.

    A name that holds an account value (graftpunk.har.paths.holds_an_id) is not
    among them, in any form: :func:`redacted_names_of` counts those."""
    return tuple(sorted(flagged_reasons_of(d, entries)))


FlaggedReason = Literal[
    "token_candidate",
    "token_name",
    "session_name",
    "http_only",
    "value_not_form_state",
    "value_not_kept",
    "no_value_seen",
    "sent_only",
]
"""Why :func:`flagged_reasons_of` flags a name. Every reason but ``sent_only`` is
enough on its own to flag a name; ``sent_only`` says no response in the
recording set the cookie (a recording made with a cached session) and is only
ever written beside a reason that flagged it."""

FLAGGED_REASONS: tuple[FlaggedReason, ...] = get_args(FlaggedReason)


def flagged_reasons_of(
    d: RunDigest, entries: Iterable[HAREntry] = ()
) -> dict[str, tuple[FlaggedReason, ...]]:
    """Each name :func:`flagged_names_of` returns, with the reasons it is flagged,
    names and reasons sorted (the order the sidecar writes them in). The rule is
    :func:`flagged_names_of`'s; this spells it as reasons:

    - ``token_candidate``: the digest recorded the name as a token candidate;
    - ``token_name``: a cookie whose name reads as a token name;
    - ``session_name``: a cookie whose name has a session part
      (:func:`_names_a_session`);
    - ``http_only``: a response in *entries* set the cookie with ``HttpOnly``;
    - ``value_not_form_state``: a value *entries* record for the cookie is not
      form state;
    - ``value_not_kept``: *entries* carry the cookie with no value kept (a
      ``Set-Cookie`` kept only as a name, or a HAR entry with no ``value``);
    - ``no_value_seen``: *entries* show no non-empty value for the cookie, and no
      request sent it empty that no response sets, which is every cookie when
      *entries* is empty;
    - ``sent_only``: alongside another reason, no response in *entries* set the
      cookie and the digest's own list does not name it."""
    entries = tuple(entries)
    seen = _cookies_seen(entries)
    entry_cookies = {
        name for entry in entries for name in _entry_cookie_names(entry) if not holds_an_id(name)
    }
    reasons: dict[str, set[FlaggedReason]] = {}
    for name in set(d.cookies) | entry_cookies:
        values = seen.values.get(name, set())
        sent_empty = name in seen.sent_empty and name not in seen.set_by_response
        why: set[FlaggedReason] = set()
        if looks_like_token_name(name):
            why.add("token_name")
        if _names_a_session(name):
            why.add("session_name")
        if name in seen.http_only:
            why.add("http_only")
        if name in seen.unseen:
            why.add("value_not_kept")
        if any(not form_state(value) for value in values):
            why.add("value_not_form_state")
        if not values and not sent_empty:
            why.add("no_value_seen")
        if why:
            # d.cookies stands in for responses a partial *entries* leaves out.
            if name not in seen.set_by_response and name not in d.cookies:
                why.add("sent_only")
            reasons[name] = why
    for token in d.tokens:
        reasons.setdefault(token.name, set()).add("token_candidate")
    return {name: tuple(sorted(why)) for name, why in sorted(reasons.items())}


# The parts of a cookie name that say it holds a session or a credential, so the
# cookie is flagged whatever its values. Each is a default session or auth cookie
# name of a common framework, or a part of one (Django's sessionid, PHP's
# PHPSESSID, Java's JSESSIONID, Express's connect.sid, ASP.NET's
# ASP.NET_SessionId and .ASPXAUTH, Rails' _<app>_session, Laravel's
# laravel_session and remember_web_<hash>), or a word sites use for the same
# (sess, auth, jwt, login, token, csrf, xsrf). Matched as whole parts, never as
# substrings, so sidebar, author and inside are not session names.
_SESSION_NAME_PARTS = frozenset(
    {
        "sess",
        "session",
        "sessid",
        "sessionid",
        "phpsessid",
        "jsessionid",
        "sid",
        "auth",
        "aspxauth",
        "jwt",
        "token",
        "csrf",
        "xsrf",
        "login",
        "remember",
    }
)
_NAME_CHUNK_RE = re.compile(r"[^_\-.\s]+")
_NAME_PART_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")


def _names_a_session(name: str) -> bool:
    """True when a part of *name*, split on ``_ - .`` and whitespace and then at
    each change of case and between letters and digits, is in
    ``_SESSION_NAME_PARTS``, case-insensitively (``ASP.NET_SessionId`` holds
    ``Session``, ``rememberMe`` holds ``remember``)."""
    return any(
        part.lower() in _SESSION_NAME_PARTS
        for chunk in _NAME_CHUNK_RE.findall(name)
        for part in (chunk, *_NAME_PART_RE.findall(chunk))
    )


@dataclass
class _CookiesSeen:
    """What a recording shows of each cookie name: its non-empty values, the names
    a request sent empty, the names any response set, the names a request or
    response carried without a value the recorder kept, and the names a
    response set with ``HttpOnly``."""

    values: dict[str, set[str]] = field(default_factory=dict)
    sent_empty: set[str] = field(default_factory=set)
    set_by_response: set[str] = field(default_factory=set)
    unseen: set[str] = field(default_factory=set)
    http_only: set[str] = field(default_factory=set)


_PUNCTUATION_AND_SPACE = frozenset(string.punctuation + " ")


def form_state(value: str) -> bool:
    """True when *value*, a non-empty cookie value, holds no credential by
    :func:`flagged_names_of`'s rule: once decoded it is ASCII punctuation and
    spaces alone, or, with one pair of surrounding double quotes taken off (RFC
    6265 lets a server quote a value, and a browser keeps the quotes), it reads
    as words or as cased words.

    Only punctuation and spaces count as holding nothing: percent-encoded bytes
    decode to U+FFFD where they are not UTF-8, and an ASCII control character
    (``%00``) is a byte too, so a value of either stays a value. A value
    masked with punctuation alone (``****``) also holds nothing, so a
    recorder that masked every cookie value so would leave its plain-named
    cookies out; graftpunk's own redaction writes ``***REDACTED***``, which
    holds letters."""
    if all(ch in _PUNCTUATION_AND_SPACE for ch in unquote_plus(value)):
        return True
    unquoted = _without_quotes(value)
    return reads_as_words(unquoted) or reads_as_cased_words(unquoted)


def _without_quotes(value: str) -> str:
    """*value* without one pair of surrounding double quotes, written as ``"`` or
    percent-encoded as ``%22``."""
    for quote in ('"', "%22"):
        if len(value) > 2 * len(quote) and value.startswith(quote) and value.endswith(quote):
            return value[len(quote) : -len(quote)]
    return value


def _cookies_seen(entries: Iterable[HAREntry]) -> _CookiesSeen:
    """Every non-empty value *entries* record for each cookie name, in requests and
    responses; the names a request sent empty; the names any response set (its
    ``response.cookies`` or a ``Set-Cookie`` header); the names carried with no
    value kept (a ``Set-Cookie`` only in ``set_cookie_names``, or a request or
    response cookie whose value is not a string, such as ``None`` for a missing
    ``value`` field); and the names a response set with ``HttpOnly``, by its
    ``response.cookies`` entry's ``httpOnly`` being true or by a ``Set-Cookie``
    header carrying the attribute (``set_cookie_http_only``). An empty value is
    skipped, so a cookie whose only values are empty has no entry in
    ``values``."""
    seen = _CookiesSeen()
    for entry in entries:
        for cookie in entry.request.cookies + entry.response.cookies:
            name = cookie.get("name", "")
            value = cookie.get("value", "")
            if not name:
                continue
            if not isinstance(value, str):
                seen.unseen.add(name)
            elif value:
                seen.values.setdefault(name, set()).add(value)
        # A request's empty value is what the browser sent, so it is evidence the
        # cookie held nothing; a response's may be a value the recorder dropped.
        # A missing value field parses as None, never as "", so it is not this.
        seen.sent_empty.update(
            cookie.get("name", "")
            for cookie in entry.request.cookies
            if cookie.get("name") and cookie.get("value") == ""
        )
        seen.set_by_response.update(cookie.get("name", "") for cookie in entry.response.cookies)
        seen.set_by_response.update(entry.response.set_cookie_names)
        # Either source is enough: the response.cookies array often leaves
        # httpOnly out when the Set-Cookie header carries it, so an absent
        # flag there is unknown, not false.
        for cookie in entry.response.cookies:
            if cookie.get("httpOnly") is True and cookie.get("name"):
                seen.http_only.add(cookie["name"])
        seen.http_only.update(entry.response.set_cookie_http_only)
        with_values = {cookie.get("name", "") for cookie in entry.response.cookies}
        seen.unseen.update(
            name for name in entry.response.set_cookie_names if name not in with_values
        )
    return seen


def redacted_names_of(d: RunDigest, entries: Iterable[HAREntry] = ()) -> int:
    """How many names :func:`flagged_names_of` leaves out because they hold an
    account value: the cookie names, plus the token candidate names the digest
    dropped (a cookie that is also a token candidate counts once as each). The
    cookie count is the larger of the distinct id cookie names any of *entries*
    sets or sends and the digest's own ``d.cookie_names_dropped_as_ids``, so it
    is never low: called without the run's entries, or with only some of them,
    it still holds every one the digest dropped. The fixture sidecar records
    this count, never the names."""
    entry_id_cookies = {
        name for entry in entries for name in _entry_cookie_names(entry) if holds_an_id(name)
    }
    cookies = max(len(entry_id_cookies), d.cookie_names_dropped_as_ids)
    return cookies + d.token_names_dropped_as_ids


def _sent_cookie_values(entry: HAREntry) -> Iterable[tuple[str, str]]:
    """``(name, value)`` for each cookie *entry*'s request sends, by its
    ``request.cookies`` array and by the text of every ``Cookie`` header it
    carries, in any case: a value the array leaves out or spells otherwise is
    still a sent value. Empty values included."""
    for cookie in entry.request.cookies:
        name, value = cookie.get("name"), cookie.get("value")
        if isinstance(name, str) and name and isinstance(value, str):
            yield name, value
    texts = list(entry.request.cookie_headers)
    texts += [
        value
        for name, value in entry.request.headers.items()
        if name.casefold() == "cookie" and isinstance(value, str)
    ]
    for cookie in cookie_header_pairs(texts):
        yield cookie["name"], cookie["value"]


def _set_cookie_values(entry: HAREntry) -> Iterable[tuple[str, str]]:
    """``(name, value)`` for each cookie the text of every ``Set-Cookie`` header of
    *entry*'s response sets, in any case, every line of a joined value."""
    texts = list(entry.response.set_cookie_headers)
    texts += [
        value
        for name, value in entry.response.headers.items()
        if name.casefold() == "set-cookie" and isinstance(value, str)
    ]
    for text in texts:
        for line in re.split(r"\r\n|\r|\n", text):
            cookie_name, sep, cookie_value = line.split(";", 1)[0].partition("=")
            if sep and cookie_name.strip():
                yield cookie_name.strip(), cookie_value.strip()


def flagged_values_of(
    d: RunDigest, entries: Iterable[HAREntry] = ()
) -> dict[str, frozenset[str] | None]:
    """Each name :func:`flagged_reasons_of` returns, with the non-empty values the
    recording's cookie headers carry for it when it is a cookie's name alone and
    a request sent it, or None.

    A cookie's values are the ones :func:`_cookies_seen` reads from the HAR's
    cookie arrays, sent and set, and the ones the text of every ``Cookie`` and
    ``Set-Cookie`` header of *entries* carries. They are what the recording
    knows, not every value the cookie held: a value a script set after the last
    request that sent the cookie, a value a later response rotated it to, or a
    header the recorder did not keep is in no recorded header. So
    :func:`graftpunk.devtools.captures.checked_names_of` also keeps a name
    checked when the capture holds a value under it that is not form state.

    None, the values not all known, for any name the digest found as a meta tag,
    hidden input, or header token: such a value can come from a source the
    recording does not hold in full (a page a script changed, a request body, a
    response header), so the fixtures check looks for that name as it did before
    sidecar schema 3. None too for a cookie no recorded request sent (in its
    ``Cookie`` header or its cookie array), since a value seen only in a
    ``Set-Cookie`` header says nothing of what the browser held later; for a
    cookie the recording carried without a value kept (``value_not_kept``) or
    with none seen (``no_value_seen``); and for a cookie token candidate
    *entries* show neither a value nor an empty send of. A cookie a request only
    ever sent empty, flagged by its name alone, is known to have held nothing
    the recording saw: an empty set.

    ``gp observe fixtures`` reads this to record which flagged names a capture
    holds the value of (the sidecar's ``checked_names``). The values never leave
    the process."""
    entries = tuple(entries)
    reasons = flagged_reasons_of(d, entries)
    seen = _cookies_seen(entries)
    values: dict[str, set[str]] = {name: set(found) for name, found in seen.values.items()}
    sent: set[str] = set()
    for entry in entries:
        for name, value in _sent_cookie_values(entry):
            sent.add(name)
            if value:
                values.setdefault(name, set()).add(value)
        for name, value in _set_cookie_values(entry):
            if value:
                values.setdefault(name, set()).add(value)
    not_cookies = {token.name for token in d.tokens if token.kind != "cookie"}
    result: dict[str, frozenset[str] | None] = {}
    for name, why in reasons.items():
        known = (
            name not in not_cookies
            and name in sent
            and name not in seen.unseen
            and "no_value_seen" not in why
        )
        result[name] = frozenset(values.get(name, ())) if known else None
    return result
