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
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from graftpunk.contracts import ENDPOINTS_SCHEMA_WITHOUT_OPERATIONS, current_schema
from graftpunk.har.digest import (
    INTERNAL,
    OMIT_EMPTY,
    CookieSetting,
    Endpoint,
    RunDigest,
    ShapeNode,
    family_values,
    header_names_not_shown,
    header_spellings,
    held_back_token_count,
    meta_names_not_shown,
    printable_custom_headers,
    printable_login_fields,
    printable_token_pairs,
    printable_tokens,
    run_header_names_not_shown,
)
from graftpunk.har.documents import LoginForm, printable_selectors, printable_unresolved_roles
from graftpunk.har.graphql import operation_kind
from graftpunk.har.paths import printable_name, templated_url, templates_a_segment

__all__ = [
    "DEFAULT_ENDPOINT_LIMIT",
    "SHAPE_TOO_DEEP_SUMMARY",
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

SHAPE_TOO_DEEP_SUMMARY = "shape unavailable: body nested too deep to parse"
"""How :data:`graftpunk.har.digest.SHAPE_TOO_DEEP` reads in a digest."""


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
        return SHAPE_TOO_DEEP_SUMMARY if shape.too_deep else SHAPE_UNAVAILABLE_SUMMARY
    if shape.kind in ("string", "number", "boolean", "null"):
        return shape.kind
    if shape.kind == "array":
        # An item of None is an empty array, or a non-empty one cut at the
        # digest's depth cap (truncated); neither is a non-JSON element.
        if depth <= 0 or (shape.item is None and shape.truncated):
            return "array<...>"
        if shape.item is None:
            return "array<empty>"
        return f"array<{summarize_shape(shape.item, depth=depth - 1)}>"
    if not shape.children:
        return "object{...}" if shape.truncated else "object{}"
    if depth <= 0:
        return "object{...}"
    keys = ", ".join(sorted(shape.children))
    suffix = ", ..." if shape.truncated else ""
    return f"object{{{keys}{suffix}}}"


def _size_note(size: int | None) -> str:
    """`` (N bytes)`` for a size the HAR recorded, and nothing for one it did not."""
    return "" if size is None else f" ({size} bytes)"


def _cookie_setting(setting: CookieSetting) -> str:
    """One attribute set, in the words and order the graft skill's
    ``har-facts.py cookies`` prints them, with the expiry as a word."""
    return (
        f"set by {', '.join(setting.set_by)}: HttpOnly={setting.http_only} "
        f"Secure={setting.secure} SameSite={setting.same_site} Path={setting.path} "
        f"expiry={setting.expiry}"
    )


_ONLY_SENT = "sent in requests and never set by a response the digest kept"


def _cookie_lines(d: RunDigest) -> list[str]:
    """The Cookies section: one line per cookie, by name, never a value. A cookie a
    response set lists each distinct attribute set with the endpoints that set it
    so, separated by ``;``; one only requests carried says so (a static,
    third-party, or failed response the digest left out may have set it). A name
    in ``d.cookies`` with no facts (a digest built without them) prints alone. A
    name that reads as a value (:func:`graftpunk.har.paths.printable_name`) is
    left out and counted on a line of its own."""
    facts = {fact.name: fact for fact in d.cookie_facts}
    names = [name for name in dict.fromkeys([*d.cookies, *facts]) if printable_name(name)]
    hidden = max(d.cookie_names_not_shown, sum(1 for name in d.cookies if not printable_name(name)))
    lines: list[str] = []
    for name in names:
        fact = facts.get(name)
        if fact is None:
            lines.append(f"- `{name}`")
        elif fact.settings:
            lines.append(f"- `{name}`: {'; '.join(map(_cookie_setting, fact.settings))}")
        else:
            lines.append(f"- `{name}`: {_ONLY_SENT}")
    if hidden:
        lines.append(
            "- 1 cookie whose name reads as a value is not shown"
            if hidden == 1
            else f"- {hidden} cookies whose names read as values are not shown"
        )
    return lines or ["(none)"]


def _endpoint_block(endpoint: Endpoint, spellings: Mapping[str, str]) -> list[str]:
    lines = [f"### {endpoint.key}", ""]
    if endpoint.operation:
        lines.append(f"- GraphQL operation: {_operation_kind(endpoint)}")
    if endpoint.family_members:
        values = ", ".join(family_values(endpoint))
        lines.append(
            f"- family of {len(endpoint.family_members)} recorded templates, "
            f"{endpoint.family_placeholder}: {values}"
        )
    statuses = list(endpoint.statuses)
    lines.append(f"- host: `{endpoint.host}`, count: {endpoint.count}, statuses: {statuses}")
    lines.append(f"- content type: `{endpoint.content_type or '(none)'}`")
    if endpoint.query_params:
        params = ", ".join(f"{k}: {v}" for k, v in sorted(endpoint.query_params.items()))
        lines.append(f"- query params: {params}")
    if endpoint.body_params:
        params = ", ".join(f"{k}: {v}" for k, v in sorted(endpoint.body_params.items()))
        label = "variables" if endpoint.operation else f"body params ({endpoint.body_kind})"
        lines.append(f"- {label}: {params}")
    if endpoint.operation_batched:
        lines.append(
            f"- batched: {endpoint.operation_batched} of {endpoint.count} recording(s) sent it "
            "in a batch, which is never a fixture"
        )
    if endpoint.body_not_captured:
        lines.append(
            f"- body not captured: {endpoint.body_not_captured} of {endpoint.count} "
            "recording(s) sent a body the recorder could not get"
        )
    if endpoint.body_omitted:
        lines.append(
            f"- body left out: {endpoint.body_omitted} of {endpoint.count} recording(s) sent "
            "a body over the capture's max body size, or not text, and the capture had no "
            "bodies/ directory to keep it in (gp's own recorders all have one; a capture "
            "backend built without bodies_dir does not)"
        )
    if endpoint.body_binary:
        lines.append(
            f"- body not text: {endpoint.body_binary} of {endpoint.count} recording(s) sent "
            f"a binary body, kept as `{endpoint.body_binary_file}`"
            f"{_size_note(endpoint.body_binary_size)}"
        )
    headers = printable_custom_headers(endpoint, spellings)
    if headers:
        lines.append(f"- custom headers: {', '.join(headers)}")
    hidden = header_names_not_shown(endpoint.custom_headers, spellings)
    if hidden:
        lines.append(
            "- 1 custom header whose name reads as a value is not shown"
            if hidden == 1
            else f"- {hidden} custom headers whose names read as values are not shown"
        )
    if endpoint.operation and endpoint.operation_batched == endpoint.count:
        lines.append("- shape: unknown (every recording was in a batch)")
    else:
        lines.append(f"- shape: {summarize_shape(endpoint.shape)}")
    if endpoint.inline_json is not None:
        lines.append(
            f"- inline JSON state: `{endpoint.inline_json.target}`, "
            f"{summarize_shape(endpoint.inline_json.shape)}"
        )
    if endpoint.examples:
        lines.append(f"- examples: {', '.join(endpoint.examples)}")
    lines.append("")
    return lines


def _operation_kind(endpoint: Endpoint) -> str:
    """``query``, ``mutation``, ``subscription``, or ``persisted query (no document
    recorded)`` for an operation row."""
    kind = operation_kind(endpoint.graphql_document)
    return "persisted query (no document recorded)" if kind == "persisted" else kind


def _endpoint_sort_key(endpoint: Endpoint) -> tuple[bool, int, str, str]:
    return (endpoint.shape is None, -endpoint.count, endpoint.template, endpoint.operation)


def _ordered_endpoints(d: RunDigest) -> list[Endpoint]:
    """*d*'s endpoints in the order both renderers list them: by
    :func:`_endpoint_sort_key`, each family row just before the first of its
    members, so it reads beside the rows it stands for. A run with no family is
    listed exactly as it was before family rows existed. Linear in the endpoint
    count: each family is found by its members' method and template."""
    literal = sorted((e for e in d.endpoints if not e.family_members), key=_endpoint_sort_key)
    by_member: dict[tuple[tuple[str, ...], str], list[Endpoint]] = {}
    for family in (e for e in d.endpoints if e.family_members):
        for member in family.family_members:
            by_member.setdefault((family.methods, member), []).append(family)
    emitted: set[int] = set()
    ordered: list[Endpoint] = []
    for endpoint in literal:
        for family in by_member.get((endpoint.methods, endpoint.template), ()):
            if id(family) not in emitted:
                emitted.add(id(family))
                ordered.append(family)
        ordered.append(endpoint)
    return ordered


def _limited(ordered: list[Endpoint], limit: int) -> list[Endpoint]:
    """The head of *ordered* the markdown digest shows: its first *limit* literal
    rows, each with the family rows listed before it. A family row never takes a
    literal row's place under the limit."""
    shown: list[Endpoint] = []
    literal = 0
    for endpoint in ordered:
        if literal == limit:
            break
        if not endpoint.family_members:
            literal += 1
        shown.append(endpoint)
    return shown


def _other_host_sort_key(item: tuple[str, int]) -> int:
    return -item[1]


def render_markdown(d: RunDigest, *, limit: int = DEFAULT_ENDPOINT_LIMIT) -> str:
    """A person-readable digest: Summary, Login, Tokens, Cookies, Endpoints, Other hosts."""
    lines: list[str] = ["# Observe digest", ""]
    spellings = header_spellings(d)

    lines.append("## Summary")
    source_label = (
        f"{d.source.session}/{d.source.run_id}" if d.source.session else str(d.source.har_path)
    )
    lines.append(f"- source: `{source_label}`")
    lines.append(f"- primary host: `{d.primary_host}`")
    families = sum(1 for e in d.endpoints if e.family_members)
    family_note = f", families: {families}" if families else ""
    lines.append(f"- hosts: {len(d.hosts)}, endpoints: {len(d.endpoints) - families}{family_note}")
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
            fields = printable_login_fields(obs)
            field_note = f" ({', '.join(fields)})" if fields else ""
            redirect_note = f" -> {obs.redirect_to}" if obs.redirect_to else ""
            # A login-shaped observation outside the login the generator uses (a
            # password change, a later redirect) says so.
            outside = (
                " (not part of the login)"
                if not obs.login_flow and obs.kind in _LOGIN_SHAPED_KINDS
                else ""
            )
            lines.append(
                f"{obs.order}. {obs.method} {obs.url} [{obs.status}] "
                f"{obs.kind}{field_note}{redirect_note}{outside}"
            )
    else:
        lines.append("(no login observations)")
    if d.login_forms:
        lines.append("")
        for form in d.login_forms:
            if form.formless:
                lines.append(f"- inputs outside a form (sent by script), source: {form.source}")
            else:
                lines.append(f"- form at `{form.action}` ({form.method}), source: {form.source}")
            for role, selector in sorted(form.fields.items()):
                lines.append(f"    - {role}: `{selector}`")
    lines.append("")

    lines.append("## Tokens")
    tokens = printable_tokens(d.tokens, spellings)
    for token in tokens:
        lines.append(f"- {token.kind} `{token.name}`, seen on: {', '.join(token.seen_on)}")
    for pair in printable_token_pairs(d, spellings):
        where = f" on GET {pair.page}" if pair.page else ""
        lines.append(
            f"- header `{pair.header}` carried the value of {_TOKEN_KIND_WORDS[pair.kind]} "
            f"`{pair.name}`{where} on {pair.matched} of {pair.sent} requests"
        )
    hidden = held_back_token_count(d.tokens, spellings)
    if hidden:
        lines.append(
            "- 1 token candidate whose name reads as a value is not shown"
            if hidden == 1
            else f"- {hidden} token candidates whose names read as values are not shown"
        )
    if not tokens and not hidden:
        lines.append("(none found)")
    lines.append("")

    lines.append("## Cookies")
    lines.extend(_cookie_lines(d))
    lines.append("")

    lines.append("## Endpoints")
    ordered = _ordered_endpoints(d)
    shown = _limited(ordered, limit)
    for endpoint in shown:
        lines.extend(_endpoint_block(endpoint, spellings))
    # Counted as the summary line counts: literal endpoints, and family rows apart.
    more_families = sum(1 for e in ordered[len(shown) :] if e.family_members)
    more_endpoints = len(ordered) - len(shown) - more_families
    if more_endpoints:
        family_note = f", {more_families} more family row(s)" if more_families else ""
        lines.append(
            f"... and {more_endpoints} more endpoint(s){family_note} (raise --limit to see them)"
        )
        lines.append("")

    if d.shared_values:
        lines.append("## Shared values")
        lines.append(
            "GraphQL variables that sent a value an earlier response field held, by name "
            "only (the values are not shown):"
        )
        for shared in d.shared_values:
            lines.append(
                f"- {shared.source_operation} `{shared.source_path}` -> "
                f"{shared.target_operation} `variables.{shared.target_variable}` "
                f"({shared.count} request(s))"
            )
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
            and not (f.metadata.get(OMIT_EMPTY) and not getattr(value, f.name))
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


_TOKEN_KIND_WORDS = {"cookie": "cookie", "meta": "meta tag", "hidden_input": "hidden input"}


def render_json(d: RunDigest) -> str:
    """The complete digest as JSON: nothing capped, nothing summarised. A field
    marked :data:`graftpunk.har.digest.INTERNAL` is a lookup for code and is left
    out, and so is a cookie name that reads as a value
    (:func:`graftpunk.har.paths.printable_name`), which
    ``cookie_names_not_shown`` counts: in ``cookies``, in ``login`` fields
    (:func:`graftpunk.har.digest.printable_login_fields`), and in ``tokens``
    (:func:`graftpunk.har.digest.printable_tokens`). A header name the run prints
    nowhere (:func:`graftpunk.har.digest.header_spellings`) is left out of
    ``tokens`` and of each endpoint's ``custom_headers``
    (:func:`graftpunk.har.digest.printable_custom_headers`), which then carries
    ``custom_headers_not_shown``; ``header_names_not_shown`` counts them for the
    run, computed here from the digest's tokens and endpoints. A meta tag token
    candidate whose name reads as a value is left out of ``tokens``, and
    ``meta_names_not_shown``, computed here from the digest's tokens, counts
    them. ``token_pairs`` holds only the pairings
    :func:`graftpunk.har.digest.printable_token_pairs` prints."""
    rendered = _jsonable(d)
    rendered["cookies"] = [name for name in d.cookies if printable_name(name)]
    spellings = header_spellings(d)
    rendered.pop("header_names_not_shown", None)
    if hidden := run_header_names_not_shown(d):
        rendered["header_names_not_shown"] = hidden
    rendered["tokens"] = _jsonable(printable_tokens(d.tokens, spellings))
    rendered.pop("meta_names_not_shown", None)
    if meta_hidden := meta_names_not_shown(d.tokens):
        rendered["meta_names_not_shown"] = meta_hidden
    rendered.pop("token_pairs", None)
    if pairs := printable_token_pairs(d, spellings):
        rendered["token_pairs"] = _jsonable(pairs)
    for observation, login in zip(d.login, rendered["login"], strict=True):
        login["fields"] = list(printable_login_fields(observation))
    for endpoint, projected in zip(d.endpoints, rendered["endpoints"], strict=True):
        _put_custom_headers(projected, endpoint, spellings)
    return json.dumps(rendered, indent=2, sort_keys=True)


_LOGIN_SHAPED_KINDS = frozenset({"form_page", "credential_post", "redirect", "set_cookie"})


def _projected_form(form: LoginForm) -> dict[str, Any]:
    """*form* as the projection prints it. When the action's path holds an id or a
    token (:func:`graftpunk.har.paths.templates_a_segment`), the action is printed
    through :func:`templated_url`, as the auth URLs are, and each field selector
    without its form scope (:func:`graftpunk.har.documents.printable_selectors`,
    the rule the generator applies too); one that cannot be unscoped is left out.
    Otherwise both are printed as recorded. The submit selector follows the same
    rule; ``neutral_roles`` are the placeholder role keys (``field_N``), and
    ``unresolved_roles`` the roles the printed step has no selector for
    (:func:`graftpunk.har.documents.printable_unresolved_roles`). ``opener_candidate``
    is the selector of the click the recording suggests opened the form
    (``LoginForm.opener_candidate``) or null."""
    action = templated_url(form.action) if templates_a_segment(form.action) else form.action
    selectors, submit = printable_selectors(form)
    # A selector that cannot be unscoped is left out, never printed scoped.
    fields = {role: selector for role, selector in selectors.items() if selector is not None}
    return {
        "action": action,
        "fields": fields,
        "submit": submit,
        "neutral_roles": list(form.neutral_roles),
        "unresolved_roles": list(printable_unresolved_roles(form)),
        # The click the recording suggests opened the form; null when none
        # qualifies. Only a suggestion: a generated login never clicks it.
        "opener_candidate": form.opener_candidate or None,
    }


def _put_custom_headers(
    projected: dict[str, Any], endpoint: Endpoint, spellings: Mapping[str, str]
) -> None:
    """Set *projected*'s ``custom_headers`` to *endpoint*'s printable ones
    (:func:`graftpunk.har.digest.printable_custom_headers`), and
    ``custom_headers_not_shown`` to how many it leaves out, only when any."""
    projected["custom_headers"] = list(printable_custom_headers(endpoint, spellings))
    hidden = header_names_not_shown(endpoint.custom_headers, spellings)
    if hidden:
        projected["custom_headers_not_shown"] = hidden


def _projected_endpoint(
    endpoint: Endpoint, method: str, spellings: Mapping[str, str]
) -> dict[str, Any]:
    """One ``endpoints`` entry of the projection: *endpoint* under *method*. A family
    row (``Endpoint.family_members``) also carries ``family_members``, its members'
    templates, and an HTML page holding inline JSON state (``Endpoint.inline_json``)
    carries ``inline_json``, its target and shape summary; every other entry leaves
    them out."""
    projected: dict[str, Any] = {
        "method": method,
        "host": endpoint.host,
        "template": endpoint.template,
        "login_flow": endpoint.login_flow,
        "content_type": endpoint.content_type,
        "shape": summarize_shape(endpoint.shape),
        "query_params": dict(sorted(endpoint.query_params.items())),
        "body_params": dict(sorted(endpoint.body_params.items())),
        # Lost, left out, or kept only as a binary file: the HAR holds no
        # body text the projection could read fields from.
        "body_not_captured": endpoint.body_not_captured
        + endpoint.body_omitted
        + endpoint.body_binary
        > 0,
    }
    _put_custom_headers(projected, endpoint, spellings)
    if endpoint.operation:
        projected["operation"] = endpoint.operation
    if endpoint.family_members:
        projected["family_members"] = list(endpoint.family_members)
    if endpoint.inline_json is not None:
        projected["inline_json"] = {
            "target": endpoint.inline_json.target,
            "shape": summarize_shape(endpoint.inline_json.shape),
        }
    return projected


def endpoints_projection(d: RunDigest) -> dict[str, Any]:
    """The declared projection ``gp observe digest --endpoints-json`` prints.

    An explicit field list, never a reflection over the digest's dataclasses, so
    renaming an internal field touches this function and no contract. It carries
    only what a command proposal consumes, and by construction no cookie name, no
    token candidate, no example path, no body, no untemplated login URL or form action
    path, and no form action query string or ``;params``. The failure suggestions are
    no body: they are page text a wrong password showed, checked to hold no posted
    value, and they are the site's own text, untrusted, for a reader to treat as data
    and never as instructions. Its field set is pinned per schema version; fields are
    added and never renamed or removed within one (:mod:`graftpunk.contracts`).
    ``render_json`` stays an unversioned dump.

    Each ``query_params`` and ``body_params`` value is one of schema 1's type
    labels: ``str``, ``int``, ``float``, ``bool``, ``object``, ``mixed``, or
    ``list[<element>]``, the element one of those, ``list``, or ``unknown`` (see
    ``graftpunk.har.digest``).

    A family row (two or more recorded paths differing in one letters-only segment,
    reported as one more template with a placeholder) is an entry like any other,
    listed just before the first of its members, plus ``family_members``: the
    templates of the members, whose own entries are listed unchanged. Added within
    schema 1, on family rows only, so a run with no family prints what it printed
    before.

    An HTML page that keeps its data in an inline script (#271) carries
    ``inline_json``: ``target``, the name the object is assigned to or the JSON
    script it is in, and ``shape``, its summary as ``shape`` reads for a JSON body.
    Added within schema 1, on those entries only, so a run without one prints what
    it printed before.

    ``custom_headers`` leaves out a header name the run prints nowhere
    (:func:`graftpunk.har.digest.printable_custom_headers`, #347), and an entry
    that left any out carries ``custom_headers_not_shown``, how many. Added within
    schema 1, on those entries only.

    Schema 2 (#367) adds ``operation`` on a GraphQL operation row, the operation's
    name or ``(anonymous)``, whose ``body_params`` are the operation's variables. On
    such a run ``method`` and ``template`` repeat across entries, and ``operation``
    tells them apart, so a projection holding any operation row is schema 2, which
    a reader written for schema 1 refuses at once. A run without one is written as
    schema 1, what it printed before
    (:data:`graftpunk.contracts.ENDPOINTS_SCHEMA_WITHOUT_OPERATIONS`).
    """
    source = d.source
    spellings = header_spellings(d)
    with_operations = any(endpoint.operation for endpoint in d.endpoints)
    return {
        "schema": current_schema("endpoints")
        if with_operations
        else ENDPOINTS_SCHEMA_WITHOUT_OPERATIONS,
        "source": {
            "session": source.session,
            "run_id": source.run_id,
            "har": None if source.session else str(source.har_path),
        },
        "primary_host": d.primary_host,
        "endpoints": [
            _projected_endpoint(endpoint, method, spellings)
            for endpoint in _ordered_endpoints(d)
            for method in endpoint.methods
        ],
        "login": {
            # The login's own observations only (LoginObservation.login_flow): a
            # logout or a cart redirect recorded in the same window is not its URL.
            "auth_urls": [
                {"method": o.method, "url": templated_url(o.url), "kind": o.kind}
                for o in d.login
                if o.login_flow
            ],
            "forms": [_projected_form(form) for form in d.login_forms],
            # Selectors the recording suggests for LoginConfig.success, best first;
            # never text. Only suggestions: a generated login never sets one.
            "success_candidates": list(d.success_candidates),
            # Text the recording suggests for LoginConfig.failure, best first: shown on
            # the page after the post the user marked as a wrong password. Page text,
            # never a body; only suggestions, which a generated login never sets.
            "failure_candidates": list(d.failure_candidates),
        },
    }


def render_endpoints_json(d: RunDigest) -> str:
    """:func:`endpoints_projection` as indented JSON with sorted keys."""
    return json.dumps(endpoints_projection(d), indent=2, sort_keys=True)
