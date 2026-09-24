"""ScaffoldSpec -> relative-path -> file-content.

The rule for every line this module emits: a fact about the site (a URL, a
selector, a parameter name, a header name) or a call into graftpunk's public
API. Never graftpunk's own logic (plugin tooling spec, 2026-09-11, "A rule
for everything the scaffold emits").
"""

from __future__ import annotations

import glob
import keyword
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from graftpunk.devtools.captures_rule import CAPTURES_DIR
from graftpunk.devtools.scaffold import policy
from graftpunk.devtools.scaffold.pysrc import (
    GENERATED_LINE_LENGTH,
    INDENT_STEP,
    L1,
    L2,
    L3,
    URL_PLACEHOLDER_RE,
    call_expression_lines,
    exploded_dict_lines,
    given_entries_dict_lines,
    import_lines,
    literal_dict_entry_lines,
    literal_lines,
    quoted_literal,
    url_expr_lines,
    wrapped_comment_lines,
    wrapped_docstring_block,
    wrapped_docstring_lines,
)
from graftpunk.har.digest import SHAPE_UNAVAILABLE, Endpoint, LoginForm, RunDigest, TokenCandidate
from graftpunk.har.documents import printable_selectors, printable_unresolved_roles
from graftpunk.har.naming import capture_filename, capture_slug
from graftpunk.har.paths import (
    template_path,
    templated_url,
    templates_a_segment,
)
from graftpunk.har.report import summarize_shape
from graftpunk.plugins.cli_plugin import SitePlugin

__all__ = [
    "PLUGIN_NAME_RE",
    "ScaffoldSpec",
    "class_name_for",
    "fixture_paths",
    "fixtures_root_for",
    "module_name_for",
    "render",
    "validate_plugin_name",
]

_MAX_SCAFFOLD_ENDPOINTS = 12
# The scalar labels a stub declares an option for. A label outside these, and
# outside the list labels _declaration accepts, is left undeclared (see there).
_SCALAR_LABELS = ("str", "int", "float", "bool")
# The list element types a repeatable option is typed as; any other element of a
# query or form list is sent as text (str).
_TYPED_LIST_ELEMENTS = ("int", "float")
# A generated stub's docstring is one line: shallower than report.py's own
# default (3), so a wide response shows its top-level keys without spilling
# nested detail into the docstring.
_SCAFFOLD_SHAPE_DEPTH = 1


# The three caps that bound every identifier the generator derives from site
# data or from the user's chosen name. Wrapping alone cannot keep a generated
# line inside the generated width when the line is one identifier (a def, a
# call, an assignment target): ruff format never splits an identifier and
# E501 still applies, so the identifiers are bounded at the point they are
# derived instead.
_MAX_PLUGIN_NAME = 40
_MAX_COMMAND_NAME = 40
_MAX_PARAM_NAME = 40

# Where a camelCase site name becomes a snake_case Python identifier: after a
# lower or a digit and before an upper, or between the last upper of a run and
# an upper-lower pair. Both are zero-width, so the substitution only inserts.
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# The methods whose stub carries a JSON body dict.
_MUTATING_METHODS = ("POST", "PUT", "PATCH")

# The observed types an explicit PluginParamSpec entry carries, each with the
# keywords its entry adds after the name. A stub with a parameter of one of these
# gets an explicit params= list, the one route that keeps the type under the
# generated module's future-annotations import. This is the compensation for
# #208: remove it, and the params= emission, when #208 lands.
#
# A bool option must be a flag or command_factory refuses the command at
# registration. The stub's bool is a flag with a negative (--archived and
# --no-archived, through the "flag" key command_factory reads), so the handler
# gets True, False, or None when neither is given. ctx.request_json and
# ctx.request_text send True and False in params and data as true and false,
# the only spelling the digest types as bool, and drop None; a JSON body gets a
# JSON boolean. A list is a repeatable option ("multiple"), which passes a list
# or None: requests sends a list in params or data as repeated keys, and a JSON
# body gets a JSON array.
_SPEC_TYPE_BY_OBSERVED: dict[str, tuple[str, ...]] = {
    "int": ("type=int",),
    "float": ("type=float",),
    "bool": ("type=bool",),
}

# The option names every plugin command already carries (command_factory's
# BUILTIN_OPTIONS: --format, --view, --output, --session) and --help. A site
# parameter of one of these names would fail the whole plugin's registration, or
# shadow --help, so its identifier is renamed (format_2, giving --format-2); the
# site's own name stays the dict key. A copy, since devtools does not import
# graftpunk.cli; a test holds it equal to BUILTIN_OPTIONS plus help.
_RESERVED_OPTION_IDENTIFIERS = ("format", "view", "output", "session", "help")

_ENDPOINT_COMMENT = (
    f"{L2}# This request is the endpoint= declared on @command above: change both together."
)

# A plugin's name becomes a Python identifier fragment (the CLI command, the
# entry-point key) in more than one generated file; validated once here so
# render() can never emit a project nothing can import (validation
# Critical 1/2, 2026-09-12).
PLUGIN_NAME_RE = re.compile(rf"[A-Za-z][A-Za-z0-9_-]{{0,{_MAX_PLUGIN_NAME - 1}}}")


def validate_plugin_name(name: str) -> None:
    """Raise if *name* cannot become a valid `gp <name>` command and entry-point key."""
    if not PLUGIN_NAME_RE.fullmatch(name):
        raise ValueError(
            f"Plugin name {name!r} must start with a letter and contain only "
            f"letters, digits, hyphens, and underscores, and be at most "
            f"{_MAX_PLUGIN_NAME} characters"
        )


def module_name_for(name: str) -> str:
    """*name*, lowercased with every run of non-alphanumeric characters collapsed to one
    underscore: the Python module fragment (``graftpunk_{module_name_for(name)}``).

    Total: never raises. A name reaching here through ``ScaffoldSpec`` is
    already validated by ``validate_plugin_name``, but the function makes no
    assumption of that on its own.
    """
    return re.sub(r"[^a-z0-9]+", "_", name.lower())


@dataclass(frozen=True)
class ScaffoldSpec:
    name: str
    mode: Literal["new_project", "add_to_suite"]
    backend: Literal["nodriver", "selenium"]
    base_url: str
    digest: RunDigest | None = None
    graftpunk_version: str = ""

    def __post_init__(self) -> None:
        validate_plugin_name(self.name)


def class_name_for(name: str) -> str:
    parts = re.split(r"[^a-zA-Z0-9]+", name)
    return "".join(part.capitalize() for part in parts if part) + "Plugin"


def _env_prefix_for(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()) + "_"


def _deduped(base: str, seen: set[str]) -> str:
    """*base* when it is free, else the first of ``{base}_2``, ``{base}_3``, ... that is
    not in *seen*; the result is added to *seen*. The counter starts at 2 because the
    unsuffixed name is the first of the series. The one uniqueness rule for every
    identifier this module derives."""
    name = base
    counter = 1
    while name in seen:
        counter += 1
        name = f"{base}_{counter}"
    seen.add(name)
    return name


def _command_name(template: str, seen: set[str]) -> str:
    """The command method name for *template*, unique within *seen*.

    A placeholder becomes a ``by_<param>`` part rather than being dropped, so
    sibling endpoints read as what they are: ``/api/orders`` is ``api_orders``,
    ``/api/orders/{order_id}`` is ``api_orders_by_order_id``, and
    ``/a/{x}/b/{y}`` is ``a_by_x_b_by_y``. Dropping them named the second
    sibling ``api_orders_2``, which says nothing about what it fetches. The
    counter is left for a true collision: the same
    template under another method, or two names equal after truncation.

    Truncated to ``_MAX_COMMAND_NAME`` before the uniqueness counter is applied: the
    name lands in a ``def``, a decorator, and the generated test's own ``def`` and
    call, none of which any wrapping helper can split, so a deep captured path must
    not be able to push those past the generated width.
    """
    parts = [
        f"by_{segment[1:-1]}" if segment.startswith("{") and segment.endswith("}") else segment
        for segment in template.strip("/").split("/")
        if segment
    ]
    base = re.sub(r"[^a-z0-9_]", "_", "_".join(parts).lower())[:_MAX_COMMAND_NAME] or "root"
    return _deduped(_safe_identifier(base, digit_prefix="n_"), seen)


def _safe_identifier(base: str, *, digit_prefix: str) -> str:
    """*base*, already in the identifier alphabet, made a Python identifier: a leading
    digit gains *digit_prefix*, and a keyword or soft keyword gains a trailing
    underscore (``import`` gives ``import_``). The one rule for command names, their
    test names, and parameter identifiers."""
    if base and base[0].isdigit():
        base = f"{digit_prefix}{base}"
    if keyword.iskeyword(base) or keyword.issoftkeyword(base):
        base = f"{base}_"
    return base


# The root commands graftpunk.cli.plugin_commands adds to a plugin itself (login, for
# a plugin with login_config). A copy, since devtools does not import graftpunk.cli;
# a test holds it equal to AUTO_ROOT_COMMAND_NAMES.
_AUTO_ROOT_COMMAND_NAMES = ("login",)
# The names a generated command may not take: every public attribute of
# SitePlugin, the class the generated plugin subclasses, read from the class itself
# so a new framework attribute is covered without an edit here, and those root
# commands.
_TAKEN_COMMAND_NAMES = frozenset(
    {name for name in dir(SitePlugin) if not name.startswith("_")} | set(_AUTO_ROOT_COMMAND_NAMES)
)
# The generated test module's fixed tests, whose names a per-command test may not take.
_FIXED_TEST_NAMES = frozenset({"plugin_instantiates"})


def _snake_cased(text: str) -> str:
    """*text* with each camelCase boundary replaced by an underscore, lowercased.

    A boundary is a lower-or-digit followed by an upper (``keywordSearch``), or the
    last upper of a run followed by an upper-lower pair (``HTTPServer`` gives
    ``http_server``). A token that is all upper with no such pair simply lowercases,
    so ``ID`` gives ``id`` rather than ``i_d``.
    """
    return _CAMEL_BOUNDARY_RE.sub("_", text).lower()


def _param_identifier(site_name: str, seen: set[str]) -> str:
    """A Python identifier for the site parameter *site_name*, unique within *seen*.

    The one owner of every parameter identifier a generated stub declares: a path
    placeholder, a query parameter, a body parameter. The site's own name is kept as
    the dict key at the call site, so the sanitisation here is free to rename: every
    character outside the identifier alphabet becomes an underscore, camelCase becomes
    snake_case (``keywordSearch`` gives ``keyword_search``, ``recorded-date-range``
    gives ``recorded_date_range``), a leading digit gains a ``p_`` prefix, the result
    is truncated to ``_MAX_PARAM_NAME``, a Python keyword gains a trailing underscore,
    and ``_deduped`` makes it unique (so two path segments that template to the same
    name, or a query parameter colliding with a path placeholder, cannot emit a
    duplicate argument).

    Keeping the site's spelling verbatim put ``keywordSearch`` and
    ``recordedDateRange`` in a Python signature and on the command line, where
    neither reads as this project's own code.
    """
    # Stripped of leading and trailing underscores, so $filter gives filter and
    # __VIEWSTATE gives viewstate, not an option spelled with leading hyphens.
    base = _snake_cased(re.sub(r"[^A-Za-z0-9_]", "_", site_name)).strip("_")
    base = _safe_identifier(base, digit_prefix="p_")[:_MAX_PARAM_NAME] or "param"
    base = _safe_identifier(base, digit_prefix="p_")
    return _deduped(base, seen)


def _login_landing_path(d: RunDigest) -> str:
    """The path the login's redirect chain came to rest on, or an empty string.

    Every observation whose own status is a 3xx carries the path it sent the
    client to, the credential post included: a login whose POST answers 302 is
    one observation, not a post plus a redirect, so reading a later observation's
    own URL would name the page that redirected rather than the landing page. Only
    the login's own observations count (``LoginObservation.login_flow``): the
    credential post and each hop that continues its chain, a request to where the
    previous hop sent the client. The last such target is the end of the chain,
    and a later POST answering with a redirect is never part of it.

    "The window" is the digest's: it classifies the entries after a credential post
    up to its own ``_LOGIN_WINDOW`` limit, so a login whose redirect chain runs
    longer than that ends with an intermediate hop as its last classified target,
    and the pattern rendered from it names a page the login passes through rather
    than the one it rests on. No captured login has come close to that limit, so
    this is stated rather than bounded.
    """
    landing = ""
    posted = False
    # Only the login the generator uses: a password change's redirect is not it.
    for observation in (o for o in d.login if o.login_flow):
        if observation.kind == "credential_post":
            posted = True
        if posted and observation.redirect_to:
            landing = observation.redirect_to
    return landing


def _success_url_pattern(redirect_path: str) -> str | None:
    """*redirect_path* as a ``success_url`` glob, or None when it says nothing useful.

    The login engine matches ``success_url`` against the whole URL, so the observed
    path gets a leading wildcard for the host (the login often lands on a different
    one than it started from) and a trailing wildcard for the query the site adds.
    A redirect to the site root is every URL's prefix and would match the login page
    itself, so it yields no pattern and the caller emits a comment instead; so does
    a landing path whose every segment templates, which would match any URL.

    The path is templated first (``template_path``), and each ``{placeholder}``
    becomes ``*``: a landing path such as ``/accounts/12345/dashboard`` holds an
    account id, which a committed file must not spell, and which differs per account
    anyway. The literal parts are escaped: ``[``, ``*`` and ``?`` are glob syntax,
    and a site that puts one in a path (``/a[b]/c``) would otherwise widen or break
    the pattern the engine matches with. ``glob.escape`` leaves a bare ``]`` alone
    and needs to: once the ``[`` before it is escaped, nothing opens a bracket
    expression for it to close, so it is a literal already.
    """
    path = redirect_path.rstrip("/")
    if not path.startswith("/"):
        return None
    template, _ = template_path(path)
    literal_segments = [
        segment for segment in template.split("/") if segment and not segment.startswith("{")
    ]
    if not literal_segments:
        # Every segment is an id (/40912873, /1/2): the glob would be */**, which
        # matches every URL, so a failed login that navigates would count as
        # success. No pattern, as for the root.
        return None
    parts = URL_PLACEHOLDER_RE.split(template)
    # split() with one group alternates literal text and placeholder names.
    return "*" + "".join("*" if i % 2 else glob.escape(part) for i, part in enumerate(parts)) + "*"


def _password_login_form(d: RunDigest) -> LoginForm | None:
    """The first captured login form with a password field, if any. The digest
    lists the form a credential post went to first, so this is the form the
    recording used when there was one.

    The one place that decides "did we capture a usable login form":
    ``_render_login_config`` and the plugin module's import list
    (``_needs_login_import``) both call this instead of repeating the same
    ``"password" in f.fields`` scan (validation Important 1, 2026-09-12).
    """
    return next((f for f in d.login_forms if "password" in f.fields), None)


def _render_login_config(spec: ScaffoldSpec) -> list[str]:
    if spec.digest is None:
        return [
            "    # GP-FILL: no run digest available.",
            '    # login_config = LoginConfig(steps=[LoginStep(fields={...}, submit="...")])',
        ]
    form = _password_login_form(spec.digest)
    if form is None:
        lines = ["    # No login form detected. Observations:"]
        for observation in spec.digest.login:
            # Templated as the projection prints it: the observed path can hold an
            # account id or a one-time token, and this file is committed.
            url = templated_url(observation.url)
            text = f"{observation.order}. {observation.method} {url} ({observation.kind})"
            lines.extend(wrapped_comment_lines(text, indent=len(L1)))
        lines.append(
            '    # login_config = LoginConfig(steps=[LoginStep(fields={...}, submit="...")])'
        )
        return lines
    landing_path = _login_landing_path(spec.digest)
    pattern = _success_url_pattern(landing_path) if landing_path else None
    lines = ["    login_config = LoginConfig(", "        steps=["]
    lines.extend(_render_login_step(form, indent=len(L3)))
    lines.append("        ],")
    login_url = _login_page_url(form, spec.base_url)
    if login_url is None:
        # LoginConfig.url is the page the engine opens, never the action the form
        # posts to. A GP-FILL literal would be a configured page the engine then
        # opens, so the hint is a comment and the field stays unset.
        lines.extend(
            wrapped_comment_lines(
                "GP-FILL: url, the path of the login page. "
                + (
                    "The recorded login page path holds an account value."
                    if form.source.startswith(("http://", "https://"))
                    else "The login form was read from a saved page source, not a URL."
                ),
                indent=len(L2),
            )
        )
    else:
        lines.extend(literal_lines(login_url, indent=len(L2), prefix="url="))
    lines.append('        failure="GP-FILL: text on the page indicating login failure",')
    # Nothing observed says which element marks the landing page, and a GP-FILL
    # literal here would be a configured signal: the engine would poll for that
    # selector until the timeout and fail naming it, as a GP-FILL success_url
    # literal once did. The hint is a comment and the field stays unset, so a fresh
    # scaffold whose success_url was pre-filled has exactly one signal, which is
    # the intended state.
    lines.extend(
        wrapped_comment_lines(
            "GP-FILL: success, a CSS selector for an element that is on the page this "
            "login lands on and not on the login form itself.",
            indent=len(L2),
        )
    )
    if pattern:
        # What the run saw the credential post redirect to: the engine polls for
        # this URL after submit, and an element check is still worth filling in.
        lines.extend(literal_lines(pattern, indent=len(L2), prefix="success_url="))
    else:
        # No landing URL was observed, so there is nothing to copy. A GP-FILL string
        # here would be a configured signal: the engine would poll for that literal
        # until the timeout and fail naming it, even for an author who filled in
        # success instead. The hint is a comment, and the field stays unset.
        lines.extend(
            wrapped_comment_lines(
                "GP-FILL: success_url, a glob matched against the whole URL this login "
                "lands on, e.g. */dashboard*. "
                + (
                    "The redirect this run observed after the credential post has no "
                    "literal path segment to match on."
                    if landing_path
                    else "This run observed no redirect after the credential post."
                ),
                indent=len(L2),
            )
        )
    lines.append("    )")
    return lines


def _paired_token_candidates(d: RunDigest) -> list[tuple[TokenCandidate, TokenCandidate]]:
    by_name: dict[str, list[TokenCandidate]] = {}
    for candidate in d.tokens:
        by_name.setdefault(candidate.name.lower(), []).append(candidate)
    pairs: list[tuple[TokenCandidate, TokenCandidate]] = []
    for candidates in by_name.values():
        headers = [c for c in candidates if c.kind == "header"]
        sources = [c for c in candidates if c.kind in ("meta", "cookie")]
        if headers and sources:
            pairs.append((headers[0], sources[0]))
    return pairs


def _render_token_config(spec: ScaffoldSpec) -> list[str]:
    if spec.digest is None:
        return [
            "    # token_config = TokenConfig(tokens=["
            'Token.from_meta_tag(name="...", header="...")])'
        ]
    pairs = _paired_token_candidates(spec.digest)
    paired = {candidate for pair in pairs for candidate in pair}
    unpaired = [c for c in spec.digest.tokens if c not in paired]
    lines: list[str] = []
    if pairs:
        lines.append("    token_config = TokenConfig(")
        lines.append("        tokens=[")
        for header, source in pairs:
            lines.extend(_render_token_call(header, source, indent=len(L3)))
        lines.append("        ]")
        lines.append("    )")
    else:
        lines.append(
            "    # token_config = TokenConfig(tokens=["
            'Token.from_meta_tag(name="...", header="...")])'
        )
    if spec.digest.token_names_dropped_as_ids:
        text = (
            f"GP-FILL: {spec.digest.token_names_dropped_as_ids} token candidate(s) were left out "
            "because their names held an account value; configure any this plugin needs "
            "by hand."
        )
        lines.extend(wrapped_comment_lines(text, indent=len(L1)))
    for candidate in unpaired:
        text = f"GP-FILL: unpaired token candidate: {candidate.kind} '{candidate.name}'"
        lines.extend(wrapped_comment_lines(text, indent=len(L1)))
    return lines


def _is_json_endpoint(endpoint: Endpoint) -> bool:
    return endpoint.shape is not None or "json" in endpoint.content_type.lower()


def _exploded_literal_dict_lines(entries: list[tuple[str, str]], *, indent: int) -> list[str]:
    """The ``fields={...}`` dict on a generated ``LoginStep``: one ``"role": "selector",``
    entry per line. Unlike a stub's parameter dicts, both halves here are captured site
    facts (a credential role is the form input's own name when it is neither the
    username nor the password field), so both route through
    ``literal_dict_entry_lines``."""
    pad = " " * indent
    lines = [f"{pad}fields={{"]
    for role, selector in entries:
        lines.extend(literal_dict_entry_lines(role, selector, indent=indent + INDENT_STEP))
    lines.append(f"{pad}}},")
    return lines


def _login_page_url(form: LoginForm, base_url: str) -> str | None:
    """What ``LoginConfig.url`` is set to: the page *form* was read from (its
    ``source``), as a path when it is on *base_url*'s host and absolute otherwise.
    None when there is no such page to name (the form came from a saved page source)
    or when its path holds an id or a token (``templates_a_segment``)."""
    parts = urlsplit(form.source)
    if parts.scheme not in ("http", "https") or templates_a_segment(form.source):
        return None
    if parts.netloc == urlsplit(base_url).netloc:
        return parts.path or "/"
    return form.source


def _render_login_step(form: LoginForm, *, indent: int) -> list[str]:
    """A generated ``LoginStep(...)``, exploded one keyword argument per line so a
    long selector cannot push the whole call over the generated width. Selectors
    are the ones ``printable_selectors`` allows: unscoped when the form's action
    holds an id, and a ``GP-FILL`` where one cannot be."""
    pad = " " * indent
    fields, submit = printable_selectors(form)
    submit_value = submit or "GP-FILL: submit selector"
    entries = [(role, selector or f"GP-FILL: {role} selector") for role, selector in fields.items()]
    lines: list[str] = []
    for role in form.neutral_roles:
        if role not in form.fields:
            continue
        cause = (
            "the recorded input had no name"
            if role in form.nameless_roles
            else "the recorded input's name held an account value"
        )
        lines.extend(
            wrapped_comment_lines(
                f"GP-FILL: {role} is a placeholder role: {cause}; rename it to the field it is.",
                indent=indent,
            )
        )
    for role in printable_unresolved_roles(form):
        if role in form.absent_roles:
            text = (
                f"GP-FILL: {role} is not on the recorded form: a multi-step login asks for "
                "it on another page; add a LoginStep for that page by hand."
            )
        else:
            reason = (
                "none picks one input of the recorded form"
                if role in form.unresolved_roles
                else "the form's action holds an id, and without it none picks one input "
                "of the recorded page"
            )
            text = f"GP-FILL: {role} has no selector: {reason}; write one by hand."
        lines.extend(wrapped_comment_lines(text, indent=indent))
    lines.append(f"{pad}LoginStep(")
    lines.extend(_exploded_literal_dict_lines(entries, indent=indent + INDENT_STEP))
    lines.extend(literal_lines(submit_value, indent=indent + INDENT_STEP, prefix="submit="))
    lines.append(f"{pad}),")
    return lines


def _render_token_call(header: TokenCandidate, source: TokenCandidate, *, indent: int) -> list[str]:
    """A generated ``Token.from_meta_tag(...)`` or ``Token.from_cookie(...)``, exploded
    one keyword argument per line so a long header or cookie name cannot push the
    whole call over the generated width."""
    pad = " " * indent
    if source.kind == "meta":
        call_name, value_keyword = "Token.from_meta_tag", "name"
    else:
        call_name, value_keyword = "Token.from_cookie", "cookie_name"
    lines = [f"{pad}{call_name}("]
    lines.extend(
        literal_lines(source.name, indent=indent + INDENT_STEP, prefix=f"{value_keyword}=")
    )
    lines.extend(literal_lines(header.name, indent=indent + INDENT_STEP, prefix="header="))
    lines.append(f"{pad}),")
    return lines


def _templated_url(template: str, seen: set[str]) -> tuple[str, list[str]]:
    """*template* with each ``{placeholder}`` renamed to the bounded identifier the stub
    declares for it, plus those identifiers in the order they appear.

    Renaming is positional, not by name: a path that templates the same placeholder
    twice (``/orders/1/orders/2`` -> ``/orders/{order_id}/orders/{order_id}``) must
    still produce two distinct arguments.
    """
    identifiers: list[str] = []

    def rename(match: re.Match[str]) -> str:
        identifier = _param_identifier(match.group(1), seen)
        identifiers.append(identifier)
        return f"{{{identifier}}}"

    return URL_PLACEHOLDER_RE.sub(rename, template), identifiers


def _emits_body(endpoint: Endpoint) -> bool:
    """Whether the stub sends a request body (``json=``, or ``data=`` for a recorded
    form): only for a mutating method, so a GET that happened to record a body
    declares no body arguments."""
    return bool(endpoint.body_params) and any(m in _MUTATING_METHODS for m in endpoint.methods)


@dataclass(frozen=True)
class _Declaration:
    """How a stub declares one query or body parameter: the handler annotation (without
    ``| None``), the ``PluginParamSpec.option`` keywords after the name, and whether
    it is a repeatable option. A bool's flag keywords depend on the other options of
    the stub, so the stub adds them (see ``_bool_flag_kwargs``)."""

    label: str
    annotation: str
    keywords: tuple[str, ...]
    multiple: bool = False

    @property
    def needs_spec(self) -> bool:
        """Whether this parameter needs an explicit ``PluginParamSpec`` entry: a
        typed scalar (#208) or a repeatable option."""
        return self.multiple or self.label in _SPEC_TYPE_BY_OBSERVED


# Why a JSON body label is left undeclared, for the stub's GP-FILL comment.
_UNDECLARED_REASONS = {
    "object": "a JSON object",
    "mixed": "values of more than one JSON type",
    "list[mixed]": "a JSON array whose elements have more than one type",
    "list[object]": "a JSON array of objects",
    "list[unknown]": "only empty JSON arrays",
    "list[bool]": "a JSON array of booleans",
    "list[list]": "a JSON array of arrays",
}


def _declaration(label: str, *, json_body: bool) -> _Declaration | str:
    """How a stub declares a parameter the digest labelled *label*, or, when no
    command-line option can send the recorded type and shape, the reason it is left
    undeclared. Query and form values are text on the wire, so a list of anything
    but numbers is a list of ``str``; a JSON body sends its own types, so an object,
    a mixed value, or an array of anything but text and numbers is undeclared."""
    if label in _SCALAR_LABELS:
        return _Declaration(label, label, _SPEC_TYPE_BY_OBSERVED.get(label, ()))
    element = label[len("list[") : -1] if label.startswith("list[") else None
    if element is not None:
        if element in _TYPED_LIST_ELEMENTS:
            return _Declaration(label, f"list[{element}]", (f"type={element}",), multiple=True)
        if element == "str" or (not json_body and element in _SCALAR_LABELS):
            return _Declaration(label, "list[str]", (), multiple=True)
    return _UNDECLARED_REASONS.get(label, f"a value the digest labelled {label}")


def _body_declarations(endpoint: Endpoint) -> dict[str, _Declaration | str]:
    """Each body field the stub sends, with its declaration or undeclared reason."""
    if not _emits_body(endpoint):
        return {}
    json_body = endpoint.body_kind != "form"
    return {
        name: _declaration(label, json_body=json_body)
        for name, label in endpoint.body_params.items()
    }


def _query_declarations(endpoint: Endpoint) -> dict[str, _Declaration]:
    """Each query parameter's declaration. Every query label is declarable
    (``_declaration`` refuses only JSON shapes); one that is not is a digest the
    generator does not understand, and is refused by name."""
    declared: dict[str, _Declaration] = {}
    for name, label in endpoint.query_params.items():
        found = _declaration(label, json_body=False)
        if not isinstance(found, _Declaration):
            raise ValueError(
                f"query parameter {name!r} is labelled {label!r}, which a query cannot carry"
            )
        declared[name] = found
    return declared


_BODY_KEY_PREFIX = "body_"


def _body_field_keys(endpoint: Endpoint) -> dict[str, str]:
    """For each body field the stub sends, the key of its declaration in
    ``_declared_extras``: the field's own name, or ``body_<name>`` when a query
    parameter of the same name is declared differently, so each is sent with its
    own recorded type (an option ``--body-<name>``), deduped against every query
    and body name of the endpoint (``body_limit_2`` beside a site ``body_limit``).
    A field one option serves for both keeps the shared name."""
    query = _query_declarations(endpoint)
    bodies = _body_declarations(endpoint)
    # A generated key is deduped against every site name on the endpoint, so a
    # site parameter literally named body_<name> keeps its own key.
    taken = set(endpoint.query_params) | set(endpoint.body_params)
    keys: dict[str, str] = {}
    for name in sorted(bodies):
        found = bodies[name]
        if not isinstance(found, _Declaration):
            continue
        if query.get(name, found) == found:
            keys[name] = name
        else:
            keys[name] = _deduped(f"{_BODY_KEY_PREFIX}{name}", taken)
    return keys


def _declared_extras(endpoint: Endpoint) -> dict[str, _Declaration]:
    """The query and body parameters a stub declares as keyword arguments, each with
    its declaration, keyed by the name the stub's identifier is derived from: the
    site's own name, or ``body_<name>`` for a body field whose query namesake is
    declared differently (see ``_body_field_keys``). A body field no option can send
    is not among them (see ``_undeclared_body_fields``)."""
    declared = dict(_query_declarations(endpoint))
    bodies = _body_declarations(endpoint)
    for name, key in _body_field_keys(endpoint).items():
        found = bodies[name]
        if isinstance(found, _Declaration):
            declared[key] = found
    return declared


def _undeclared_body_fields(endpoint: Endpoint) -> dict[str, str]:
    """The body fields the stub leaves out, each with why (G1: a command sends the
    recorded type and shape, or does not declare the field). A field whose query
    namesake is declared says so, since that option sends the query value only."""
    undeclared: dict[str, str] = {}
    for name, found in _body_declarations(endpoint).items():
        if not isinstance(found, str):
            continue
        query_label = endpoint.query_params.get(name)
        if query_label is not None:
            found = (
                f'{found} (the query parameter "{name}" is {query_label}, and its '
                "option sends the query value only)"
            )
        undeclared[name] = found
    return undeclared


def _needs_param_specs(endpoint: Endpoint) -> bool:
    """Whether the stub declares its parameters explicitly (see _Declaration.needs_spec)."""
    return any(d.needs_spec for d in _declared_extras(endpoint).values())


def _option_name(identifier: str) -> str:
    """The option a parameter declares, without its ``--``: the name hyphenated."""
    return identifier.replace("_", "-")


def _negatable_flag(identifier: str, option_names: set[str]) -> str:
    """The option declaration of a bool parameter: ``--name/--no-name``, or
    ``--name/--name-false`` when ``no-name`` is already one of the stub's
    *option_names* (a ``no_cache`` beside a ``cache``), so each option keeps its own
    value. When both are taken, the positive ``--name`` alone: the option can send
    ``true`` or nothing, and the stub says in a ``GP-FILL`` comment why ``false``
    cannot be sent."""
    flag = _option_name(identifier)
    for negative in (f"no-{flag}", f"{flag}-false"):
        if negative not in option_names:
            return f"--{flag}/--{negative}"
    return f"--{flag}"


ClickKwargs = tuple[tuple[str, "bool | str"], ...]


def _click_kwargs_value(value: bool | str) -> str:
    return str(value) if isinstance(value, bool) else quoted_literal(value)


def _param_spec(
    identifier: str, keywords: tuple[str, ...], *, click_kwargs: ClickKwargs = ()
) -> str:
    """One ``PluginParamSpec.option(...)`` entry of a stub's ``params=`` list, as the
    expression ``_decorator_lines`` places at ``L3``: on one line when that line,
    its trailing comma included, fits the generated width, otherwise exploded one
    argument per line with a magic trailing comma, the shape ``ruff format`` gives
    it. The exploded form's continuation lines carry their own indentation.

    *click_kwargs* is ``(key, value)`` pairs, a bool value written as ``True`` or
    ``False`` and a str value as a string literal. A ``click_kwargs`` too wide for
    its line is exploded one key per line, a string value wrapped by
    ``literal_dict_entry_lines``."""
    args = [quoted_literal(identifier), *keywords]
    inline = ", ".join(f"{quoted_literal(k)}: {_click_kwargs_value(v)}" for k, v in click_kwargs)
    click_arg = f"click_kwargs={{{inline}}}"
    single_args = [*args, click_arg] if click_kwargs else args
    single = f"PluginParamSpec.option({', '.join(single_args)})"
    if len(f"{L3}{single},") <= GENERATED_LINE_LENGTH:
        return single
    arg_pad = f"{L3}{' ' * INDENT_STEP}"
    exploded = ["PluginParamSpec.option(", *(f"{arg_pad}{a}," for a in args)]
    if click_kwargs:
        if len(f"{arg_pad}{click_arg},") <= GENERATED_LINE_LENGTH:
            exploded.append(f"{arg_pad}{click_arg},")
        else:
            entry_indent = len(arg_pad) + INDENT_STEP
            exploded.append(f"{arg_pad}click_kwargs={{")
            for key, value in click_kwargs:
                if isinstance(value, bool):
                    exploded.append(f"{' ' * entry_indent}{quoted_literal(key)}: {value},")
                else:
                    exploded.extend(literal_dict_entry_lines(key, value, indent=entry_indent))
            exploded.append(f"{arg_pad}}},")
    return "\n".join([*exploded, f"{L3})"])


def _decorator_lines(name: str, endpoint_literal: str, param_specs: list[str]) -> list[str]:
    """A stub's ``@command(...)``, always exploded one keyword per line. The
    ``endpoint=`` keyword starts a line of its own; a value too wide for that line
    wraps as a parenthesised implicit concatenation, which Python reads back as
    one string. Each *param_specs* entry is an expression placed at ``L3`` (see
    ``_param_spec``)."""
    lines = [f"{L1}@command("]
    lines.extend(literal_lines(f"GP-FILL: describe {name}", indent=len(L2), prefix="help="))
    if param_specs:
        lines.append(f"{L2}params=[")
        lines.extend(f"{L3}{spec}," for spec in param_specs)
        lines.append(f"{L2}],")
    lines.extend(literal_lines(endpoint_literal, indent=len(L2), prefix="endpoint="))
    lines.append(f"{L1})")
    return lines


def _render_command_stub(endpoint: Endpoint, seen_names: set[str], run_label: str) -> list[str]:
    method = endpoint.methods[0]
    name = _command_name(endpoint.template, seen_names)
    # "self" and "ctx" are taken before any site parameter is named, so a site
    # parameter called either cannot shadow the stub's own arguments; so are the
    # CLI's own option names (_RESERVED_OPTION_IDENTIFIERS).
    seen_params = {"self", "ctx", *_RESERVED_OPTION_IDENTIFIERS}
    url_text, path_params = _templated_url(endpoint.template, seen_params)
    is_json = _is_json_endpoint(endpoint)
    call, role, return_type = (
        ("request_json", "xhr", "dict") if is_json else ("request_text", "navigation", "str")
    )
    extras = _declared_extras(endpoint)
    undeclared = _undeclared_body_fields(endpoint)
    body_keys = _body_field_keys(endpoint)

    params = ["self", "ctx: CommandContext"] + [f"{p}: str" for p in path_params]
    param_specs = [_param_spec(p, ("required=True",)) for p in path_params]
    identifier_for = {extra: _param_identifier(extra, seen_params) for extra in sorted(extras)}
    option_names = {_option_name(i) for i in [*path_params, *identifier_for.values()]}
    flag_notes: list[str] = []
    for extra in sorted(extras):
        declaration = extras[extra]
        params.append(f"{identifier_for[extra]}: {declaration.annotation} | None = None")
        click_kwargs: ClickKwargs = ()
        if declaration.multiple:
            click_kwargs = (("multiple", True),)
        elif declaration.label == "bool":
            flag = _negatable_flag(identifier_for[extra], option_names)
            click_kwargs = (("is_flag", True), ("flag", flag))
            if "/" not in flag:
                positive = flag.removeprefix("--")
                flag_notes.append(
                    f'GP-FILL: "{extra}" can send true but not false: --no-{positive} and '
                    f"--{positive}-false are both other options of this command."
                )
        param_specs.append(
            _param_spec(identifier_for[extra], declaration.keywords, click_kwargs=click_kwargs)
        )

    # Through quoted_literal like every other captured value: the method comes from
    # the capture, so it is not this module's to assume is quote-free.
    call_lines = [f"{L3}{quoted_literal(method)},"]
    call_lines.extend(url_expr_lines(url_text, is_fstring=bool(path_params), indent=len(L3)))
    call_lines.append(f"{L3}role={quoted_literal(role)},")
    if endpoint.query_params:
        entries = [(p, identifier_for[p]) for p in sorted(endpoint.query_params)]
        call_lines.extend(exploded_dict_lines("params", entries))
    if body_keys:
        entries = [(p, identifier_for[body_keys[p]]) for p in sorted(body_keys)]
        if endpoint.body_kind == "form":
            # Sent as the recording sent it. ctx.request_* drops a None value
            # from data= and spells a bool true/false, as it does for params=.
            call_lines.extend(exploded_dict_lines("data", entries))
        else:
            # json= is sent as given, so an option left off is left out here,
            # rather than sent as null.
            call_lines.extend(given_entries_dict_lines("json", entries))
    if endpoint.custom_headers:
        entries = [(h, '"GP-FILL"') for h in endpoint.custom_headers]
        call_lines.extend(exploded_dict_lines("headers", entries))

    summary = f"{method} {endpoint.template}: seen {endpoint.count} time(s) in run {run_label}."
    # An unavailable shape is not a fact about the site, so the docstring says
    # nothing rather than guessing.
    shape_known = endpoint.shape is None or endpoint.shape != SHAPE_UNAVAILABLE

    lines = _decorator_lines(
        name,
        f"{method} {endpoint.template}",
        param_specs if _needs_param_specs(endpoint) else [],
    )
    lines.append(f"{L1}def {name}(")
    lines.extend(f"{L2}{p}," for p in params)
    lines.append(f"{L1}) -> {return_type}:")
    lines.append(f'{L2}"""')
    lines.extend(wrapped_docstring_lines(summary))
    if shape_known:
        shape_line = f"Shape: {summarize_shape(endpoint.shape, depth=_SCAFFOLD_SHAPE_DEPTH)}."
        lines.append("")
        lines.extend(wrapped_docstring_lines(shape_line))
    lines.append(f'{L2}"""')
    for note in [*_dropped_name_notes(endpoint), *flag_notes]:
        lines.extend(wrapped_comment_lines(note, indent=len(L2)))
    for field_name, reason in sorted(undeclared.items()):
        lines.extend(
            wrapped_comment_lines(
                f'GP-FILL: body field "{field_name}" is not an option: the recording sent '
                f"{reason}, which no command-line option sends as recorded. Add it to the "
                "body by hand if this command needs it.",
                indent=len(L2),
            )
        )
    if path_params:
        lines.append(
            f"{L2}# Each path value is percent-encoded, so a / ? or # in it stays in its segment."
        )
        lines.extend(f'{L2}{p} = _quote_path({p}, safe="")' for p in path_params)
    lines.append(_ENDPOINT_COMMENT)
    lines.append(f"{L2}return ctx.{call}(")
    lines.extend(call_lines)
    lines.append(f"{L2})")
    lines.append("")
    return lines


def _ordered_endpoints(d: RunDigest) -> list[Endpoint]:
    """The endpoints the scaffold renders a stub for, most useful first. The login
    flow's own endpoints are skipped by the digest's ``login_flow`` flag."""
    kept = [e for e in d.endpoints if not e.login_flow]
    return sorted(kept, key=lambda e: (not _is_json_endpoint(e), -e.count, e.template))


def _run_label(d: RunDigest) -> str:
    return f"{d.source.session}/{d.source.run_id}" if d.source.session else "the supplied HAR"


def _stub_endpoints(spec: ScaffoldSpec) -> list[Endpoint]:
    """The endpoints this spec renders stubs and generated tests for."""
    if spec.digest is None:
        return []
    return _ordered_endpoints(spec.digest)[:_MAX_SCAFFOLD_ENDPOINTS]


def _dropped_name_notes(endpoint: Endpoint) -> list[str]:
    """The GP-FILL notes for names the digest dropped, one per cause, so a stub never
    loses a recorded field without saying so and why."""
    notes: list[str] = []
    ids = (endpoint.query_keys_dropped_as_ids, endpoint.body_keys_dropped_as_ids)
    if any(ids):
        notes.append(
            f"GP-FILL: {ids[0]} recorded query field(s) and {ids[1]} body field(s) were left "
            "out because their names held an account value; add any this command needs "
            "by hand."
        )
    non_names = (endpoint.query_keys_dropped_as_non_names, endpoint.body_keys_dropped_as_non_names)
    if any(non_names):
        notes.append(
            f"GP-FILL: {non_names[0]} recorded query field(s) and {non_names[1]} body field(s) "
            "were left out because their names are not field names (a name starting with "
            "a digit, holding a character a field name does not, or longer than 64 "
            "characters); add any this command needs by hand."
        )
    if endpoint.header_names_dropped_as_ids:
        notes.append(
            f"GP-FILL: {endpoint.header_names_dropped_as_ids} recorded header name(s) were left "
            "out because they held an account value; add any this command needs by hand."
        )
    return notes


def _render_command_stubs(spec: ScaffoldSpec) -> list[str]:
    endpoints = _stub_endpoints(spec)
    if spec.digest is None or not endpoints:
        return [
            '    @command(help="GP-FILL: describe this command")',
            "    def example(self, ctx: CommandContext) -> dict:",
            '        """GP-FILL: what this command does."""',
            '        return ctx.request_json("GET", "/GP-FILL/path")',
        ]
    seen_names: set[str] = set(_TAKEN_COMMAND_NAMES)
    lines: list[str] = []
    for endpoint in endpoints:
        lines.extend(_render_command_stub(endpoint, seen_names, _run_label(spec.digest)))
    return lines


def _needs_login_import(spec: ScaffoldSpec) -> bool:
    return spec.digest is not None and _password_login_form(spec.digest) is not None


def _plugins_import_names(*, needs_login_import: bool, needs_param_spec: bool) -> list[str]:
    """The names to import from ``graftpunk.plugins``, ordered the way this project's
    own isort setting (classes, then functions, each alphabetical) expects, so the
    generated line never needs a second reformatting pass."""
    classes = ["CommandContext", "SitePlugin"]
    if needs_login_import:
        classes += ["LoginConfig", "LoginStep"]
    if needs_param_spec:
        classes.append("PluginParamSpec")
    return sorted(classes) + ["command"]


def _render_plugin_module(spec: ScaffoldSpec) -> str:
    klass = class_name_for(spec.name)
    needs_login_import = _needs_login_import(spec)
    needs_token_import = spec.digest is not None and bool(_paired_token_candidates(spec.digest))
    needs_param_spec = any(_needs_param_specs(e) for e in _stub_endpoints(spec))
    needs_quote = any(URL_PLACEHOLDER_RE.search(e.template) for e in _stub_endpoints(spec))
    plugins_names = _plugins_import_names(
        needs_login_import=needs_login_import, needs_param_spec=needs_param_spec
    )
    lines = [
        f'"""{spec.name} plugin.',
        "",
        "Verified against a real account on: (none yet)",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        # Under a private alias: a site parameter may be named quote.
        *(["from urllib.parse import quote as _quote_path", ""] if needs_quote else []),
        *import_lines("graftpunk.plugins", *plugins_names),
    ]
    if needs_token_import:
        lines.append("from graftpunk.tokens import Token, TokenConfig")
    lines += ["", "", f"class {klass}(SitePlugin):"]
    class_docstring = f"Commands for {spec.base_url or 'GP-FILL: base_url'}."
    lines.extend(wrapped_docstring_block(class_docstring, indent=len(L1)))
    lines.append("")
    lines.append(f'    site_name = "{spec.name}"')
    lines.append(f'    session_name = "{spec.name}"')
    lines.append(f'    help_text = "Commands for {spec.name}"')
    base_url_lines = literal_lines(
        spec.base_url, indent=len(L1), prefix="base_url = ", trailing_comma=False
    )
    if not spec.base_url:
        # Neither --url nor --from-run: the class docstring already says
        # GP-FILL, but the attribute the author has to edit carried no marker,
        # so a grep for GP-FILL missed the one line that matters (final fix
        # wave, 2026-09-12).
        base_url_lines[-1] += "  # GP-FILL: base URL"
    lines.extend(base_url_lines)
    lines.append(f'    backend = "{spec.backend}"')
    lines.append("    api_version = 1")
    lines.append("")
    lines.extend(_render_login_config(spec))
    lines.append("")
    lines.extend(_render_token_config(spec))
    lines.append("")
    lines.extend(_render_command_stubs(spec))
    return "\n".join(lines).rstrip() + "\n"


def _render_pyproject(spec: ScaffoldSpec) -> str:
    package = f"graftpunk_{module_name_for(spec.name)}"
    klass = class_name_for(spec.name)
    floor = spec.graftpunk_version or "0.0.0"
    return (
        "[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n'
        "\n"
        "[project]\n"
        f'name = "{package}"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.11"\n'
        "dependencies = [\n"
        f'    "graftpunk[browser]>={floor}",\n'
        "]\n"
        "\n"
        "[project.optional-dependencies]\n"
        'dev = ["pytest>=8.0.0", "ruff>=0.5.0"]\n'
        "\n"
        '[project.entry-points."graftpunk.plugins"]\n'
        f'{spec.name} = "{package}.plugin:{klass}"\n'
        "\n"
        "[tool.hatch.build.targets.wheel]\n"
        f'packages = ["src/{package}"]\n'
        "\n"
        "[tool.ruff]\n"
        f"line-length = {GENERATED_LINE_LENGTH}\n"
        "\n"
        "[tool.ruff.lint]\n"
        'select = ["E", "F", "I", "UP", "B"]\n'
    )


def _render_conftest(spec: ScaffoldSpec) -> str:
    # The import alone: naming the module in pytest_plugins as well makes pytest
    # try to rewrite assertions in a module the import has already loaded, which
    # it reports as a PytestAssertRewriteWarning on every run of the generated
    # suite. graftpunk.testing.plugin defines no hooks or fixtures of its own, so
    # loading it as a plugin buys nothing: site_env_scrubber returns the fixture
    # object, and the assignment below is what registers it (final fix wave,
    # 2026-09-12).
    return (
        "from graftpunk.testing.plugin import site_env_scrubber\n"
        "\n"
        f'scrub_site_env = site_env_scrubber("{_env_prefix_for(spec.name)}")\n'
    )


def fixtures_root_for(spec: ScaffoldSpec) -> str:
    """Where *spec*'s generated tests look for fixtures: the policy's rule, from the
    spec's two facts. Also the directory ``gp plugin new``'s ``Next:`` line names."""
    return policy.fixtures_root(
        suite_member=spec.mode == "add_to_suite", module_name=module_name_for(spec.name)
    )


def fixture_paths(spec: ScaffoldSpec) -> list[str]:
    """The fixture file each generated endpoint test looks for, project-relative.

    One per rendered stub, named by ``har.naming`` from the same method,
    template, and content type the stub's own request carries, so the list the
    CLI prints is the list ``FixtureSession`` will go looking for.
    """
    paths: list[str] = []
    stems: set[str] = set()
    for endpoint in _stub_endpoints(spec):
        # Case-folded, as a case-insensitive filesystem compares them.
        stem = capture_slug(endpoint.methods[0], endpoint.template).casefold()
        if stem in stems:
            continue  # no generated test reads it (see _render_test_module)
        stems.add(stem)
        paths.append(
            f"{fixtures_root_for(spec)}"
            f"{capture_filename(endpoint.methods[0], endpoint.template, endpoint.content_type)}"
        )
    return paths


def _fixtures_dir_expression(spec: ScaffoldSpec) -> str:
    """The generated ``FIXTURES_DIR`` assignment's right-hand side: the fixtures root,
    relative to the test module, which lives in ``policy.TESTS_DIR``. Every root
    lies under that directory by the policy's own rule, which its tests pin."""
    parts = fixtures_root_for(spec).removeprefix(policy.TESTS_DIR).strip("/").split("/")
    return "Path(__file__).parent" + "".join(f' / "{part}"' for part in parts)


def _render_test_module(spec: ScaffoldSpec, *, package: str) -> str:
    klass = class_name_for(spec.name)
    # fixture_context is only used by the per-endpoint tests below: importing
    # it when there is nothing to call it with is an unused import in the
    # generated file's own ruff run (F401; validation Important 2, 2026-09-12).
    endpoints = _stub_endpoints(spec)
    has_endpoint_tests = bool(endpoints)
    lines = [
        f'"""Tests for the {spec.name} plugin."""',
        "",
        "from __future__ import annotations",
        "",
        "from pathlib import Path",
        "",
    ]
    if has_endpoint_tests:
        # graftpunk.testing (third-party) sorts before the generated package
        # (first-party, "src" layout) under the generated project's own isort
        # settings.
        lines += ["from graftpunk.testing import fixture_context", ""]
    lines += [
        *import_lines(f"{package}.plugin", klass),
        "",
        f"FIXTURES_DIR = {_fixtures_dir_expression(spec)}",
        "",
        "",
        "def test_plugin_instantiates() -> None:",
        f"    plugin = {klass}()",
        f'    assert plugin.site_name == "{spec.name}"',
        "",
        "",
    ]
    if not has_endpoint_tests:
        marker = (
            f"# GP-FILL: add a test per command, against a fixture in {fixtures_root_for(spec)}"
        )
        lines.append(marker)
        return "\n".join(lines).rstrip() + "\n"
    seen: set[str] = set(_TAKEN_COMMAND_NAMES)
    seen_tests: set[str] = set(_FIXED_TEST_NAMES)
    stems: dict[str, str] = {}
    for endpoint in endpoints:
        name = _command_name(endpoint.template, seen)
        method = endpoint.methods[0]
        stem = capture_slug(method, endpoint.template)
        if stem.casefold() in stems:
            # FixtureSession looks a fixture up by stem, so this endpoint's test
            # would read the other endpoint's fixture.
            note = (
                f"GP-FILL: no test for {name} ({method} {endpoint.template}): its fixture "
                f"would share the stem {stem} with {stems[stem.casefold()]}; write its test "
                "against a fixture of its own."
            )
            lines.extend(wrapped_comment_lines(note, indent=0))
            lines.append("")
            lines.append("")
            continue
        stems[stem.casefold()] = f"{method} {endpoint.template}"
        test_name = _deduped(name, seen_tests)
        # The same seeding as _render_command_stub, so the identifiers here are
        # the ones the stub actually declares.
        _, path_params = _templated_url(
            endpoint.template, {"self", "ctx", *_RESERVED_OPTION_IDENTIFIERS}
        )
        # "1001" round-trips through the naming rule (paths.looks_dynamic
        # treats a digit run of 3 or more as dynamic; "1" stays literal, like
        # /page/1): the request this test issues
        # renames back to the endpoint's own template, so it finds the
        # fixture named for it. A literal "GP-FILL" would not: it stays a
        # literal segment and the fixture lookup would 404.
        lines.append(f"def test_{test_name}() -> None:")
        # base_url is a captured site fact and plugin_name is the user's own
        # name, so this call is exploded one keyword argument per line with
        # both values through literal_lines.
        lines.append(f"{L1}ctx = fixture_context(")
        lines.append(f"{L2}FIXTURES_DIR,")
        lines.extend(literal_lines(spec.name, indent=len(L2), prefix="plugin_name="))
        lines.extend(literal_lines(spec.base_url, indent=len(L2), prefix="base_url="))
        lines.append(f"{L1})")
        lines.append(f"{L1}plugin = {klass}()")
        lines.extend(
            call_expression_lines(
                f"result = plugin.{name}",
                ["ctx", *(f'{p}="1001"' for p in path_params)],
                indent=len(L1),
            )
        )
        lines.append("    assert result  # GP-FILL: assert on the shape you expect")
        lines.append("")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_gitignore() -> str:
    return f"{CAPTURES_DIR}/\n__pycache__/\n*.egg-info/\n.venv/\n"


def _render_readme(spec: ScaffoldSpec) -> str:
    return (
        f"# {spec.name}\n\n"
        "A graftpunk plugin.\n\n"
        "## Install\n\n"
        "```bash\npip install -e .\n```\n\n"
        "## Log in\n\n"
        f"```bash\ngp {spec.name} login\n```\n\n"
        "## Run a command\n\n"
        f"```bash\ngp {spec.name} --help\n```\n\n"
        "## Tests\n\n"
        "```bash\npytest\n```\n\n"
        f"Fixtures under `{policy.FIXTURES_TREE}` are hand-derived from captures in "
        f"`{CAPTURES_DIR}/` (captures are never committed; a fixture copies the "
        "structure and invents the content). See `gp observe fixtures --help`.\n"
    )


def render(spec: ScaffoldSpec) -> dict[str, str]:
    """Render *spec* into relative-path -> file-content, per its ``mode``."""
    package = f"graftpunk_{module_name_for(spec.name)}"
    plugin_module = _render_plugin_module(spec)
    if spec.mode == "new_project":
        return {
            "pyproject.toml": _render_pyproject(spec),
            f"src/{package}/__init__.py": f'"""{spec.name}: a graftpunk plugin."""\n',
            f"src/{package}/plugin.py": plugin_module,
            f"{policy.TESTS_DIR}conftest.py": _render_conftest(spec),
            f"{policy.TESTS_DIR}test_plugin.py": _render_test_module(spec, package=package),
            f"{fixtures_root_for(spec)}.gitkeep": "",
            ".gitignore": _render_gitignore(),
            "README.md": _render_readme(spec),
        }
    return {
        f"src/{package}/__init__.py": f'"""{spec.name}: a graftpunk plugin."""\n',
        f"src/{package}/plugin.py": plugin_module,
        f"{policy.TESTS_DIR}test_{module_name_for(spec.name)}.py": _render_test_module(
            spec, package=package
        ),
        f"{fixtures_root_for(spec)}.gitkeep": "",
    }
