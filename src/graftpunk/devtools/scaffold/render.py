"""ScaffoldSpec -> relative-path -> file-content.

The rule for every line this module emits: a fact about the site (a URL, a
selector, a parameter name, a header name) or a call into graftpunk's public
API. Never graftpunk's own logic (plugin tooling spec, 2026-09-11, "A rule
for everything the scaffold emits").
"""

from __future__ import annotations

import json
import keyword
import re
import textwrap
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from graftpunk.devtools.captures import CAPTURES_DIR
from graftpunk.har.digest import SHAPE_UNAVAILABLE, Endpoint, LoginForm, RunDigest, TokenCandidate
from graftpunk.har.paths import template_path
from graftpunk.har.report import summarize_shape

__all__ = [
    "PLUGIN_NAME_RE",
    "ScaffoldSpec",
    "class_name_for",
    "module_name_for",
    "render",
    "validate_plugin_name",
]

_MAX_SCAFFOLD_ENDPOINTS = 12
_PY_TYPE_BY_OBSERVED: dict[str, str] = {
    "int": "int",
    "bool": "bool",
    "list": "list[str]",
    "str": "str",
}
# A generated stub's docstring is one line: shallower than report.py's own
# default (3), so a wide response shows its top-level keys without spilling
# nested detail into the docstring.
_SCAFFOLD_SHAPE_DEPTH = 1

# The line length a generated project's own [tool.ruff] declares (_render_pyproject
# below): every wrapping decision this module makes for generated content is
# against this one number, so the two cannot silently drift apart.
_GENERATED_LINE_LENGTH = 100

# Indentation levels used when a generated command stub is exploded onto
# multiple lines (a class body; a method body; a call's arguments; an
# argument dict's entries), one owner each, so the levels cannot drift.
_L1 = "    "
_L2 = "        "
_L3 = "            "
_L4 = "                "
_DOCSTRING_WRAP_WIDTH = _GENERATED_LINE_LENGTH - len(_L2)

# The three caps that bound every identifier the generator derives from site
# data or from the user's chosen name. Wrapping alone cannot keep a generated
# line inside the generated width when the line is one identifier (a def, a
# call, an assignment target): ruff format never splits an identifier and
# E501 still applies, so the identifiers are bounded at the point they are
# derived instead (validation fix round 4, Finding 4, 2026-09-12).
_MAX_PLUGIN_NAME = 40
_MAX_COMMAND_NAME = 40
_MAX_PARAM_NAME = 40

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
    """*base*, or ``{base}_{n}`` for the lowest *n* that is not in *seen*; the result is
    added to *seen*. The one uniqueness rule for every identifier this module derives."""
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
    sibling ``api_orders_2``, which says nothing about what it fetches (polish
    round 1, 2026-09-12). The counter is left for a true collision: the same
    template under another method, or two names equal after truncation.

    Truncated to ``_MAX_COMMAND_NAME`` before the uniqueness counter is applied: the
    name lands in a ``def``, a decorator, and the generated test's own ``def`` and
    call, none of which any wrapping helper can split, so a deep captured path must
    not be able to push those past the generated width (validation fix round 4,
    2026-09-12).
    """
    parts = [
        f"by_{segment[1:-1]}" if segment.startswith("{") and segment.endswith("}") else segment
        for segment in template.strip("/").split("/")
        if segment
    ]
    base = re.sub(r"[^a-z0-9_]", "_", "_".join(parts).lower())[:_MAX_COMMAND_NAME] or "root"
    return _deduped(base, seen)


def _param_identifier(site_name: str, seen: set[str]) -> str:
    """A Python identifier for the site parameter *site_name*, unique within *seen*.

    The one owner of every parameter identifier a generated stub declares: a path
    placeholder, a query parameter, a body parameter. The site's own name is kept as
    the dict key at the call site, so the sanitisation here is free to rename: every
    character outside the identifier alphabet becomes an underscore, a leading digit
    gains a ``p_`` prefix, the result is truncated to ``_MAX_PARAM_NAME``, a Python
    keyword gains a trailing underscore, and ``_deduped`` makes it unique (so two path
    segments that template to the same name, or a query parameter colliding with a
    path placeholder, cannot emit a duplicate argument).
    """
    base = re.sub(r"[^A-Za-z0-9_]", "_", site_name)
    if base and base[0].isdigit():
        base = f"p_{base}"
    base = base[:_MAX_PARAM_NAME] or "param"
    if keyword.iskeyword(base) or keyword.issoftkeyword(base):
        base = f"{base}_"
    return _deduped(base, seen)


def _redirect_target_after_credential_post(d: RunDigest) -> str | None:
    """The path a credential post redirected to, if any: a candidate for `success`."""
    posted = False
    for observation in d.login:
        if observation.kind == "credential_post":
            posted = True
            continue
        if posted and observation.kind == "redirect":
            return urlparse(observation.url).path
    return None


def _password_login_form(d: RunDigest) -> LoginForm | None:
    """The first captured login form with a password field, if any.

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
            text = (
                f"{observation.order}. {observation.method} {observation.url} ({observation.kind})"
            )
            lines.extend(_wrapped_comment_lines(text, indent=len(_L1)))
        lines.append(
            '    # login_config = LoginConfig(steps=[LoginStep(fields={...}, submit="...")])'
        )
        return lines
    hint = _redirect_target_after_credential_post(spec.digest)
    lines = ["    login_config = LoginConfig(", "        steps=["]
    lines.extend(_render_login_step(form, indent=len(_L3)))
    lines.append("        ],")
    lines.extend(_literal_lines(form.action, indent=len(_L2), prefix="url="))
    lines.append('        failure="GP-FILL: text on the page indicating login failure",')
    if hint:
        text = f"candidate success redirect target, from the run: {hint}"
        lines.extend(_wrapped_comment_lines(text, indent=len(_L2)))
    lines.append('        success="GP-FILL: CSS selector for login success",')
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
            lines.extend(_render_token_call(header, source, indent=len(_L3)))
        lines.append("        ]")
        lines.append("    )")
    else:
        lines.append(
            "    # token_config = TokenConfig(tokens=["
            'Token.from_meta_tag(name="...", header="...")])'
        )
    for candidate in unpaired:
        text = f"GP-FILL: unpaired token candidate: {candidate.kind} '{candidate.name}'"
        lines.extend(_wrapped_comment_lines(text, indent=len(_L1)))
    return lines


def _is_json_endpoint(endpoint: Endpoint) -> bool:
    return endpoint.shape is not None or "json" in endpoint.content_type.lower()


def _escaped_for_docstring(text: str) -> str:
    """*text* made safe to sit inside a ``\"\"\"`` docstring.

    A captured site fact reaches a generated docstring verbatim: the class
    docstring carries ``base_url``, a stub's summary carries its template and run
    label, and its shape line carries captured JSON keys. Embedded raw, a backslash
    starts an escape sequence nobody wrote (a trailing one swallows the closing
    quotes) and a ``\"\"\"`` ends the docstring early, so a URL or a key holding
    either renders a module that does not parse. A trailing quote is escaped too:
    it would otherwise sit against the closing quotes of the one-line form and
    close the string one character early (final fix wave, 2026-09-12).
    """
    escaped = text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    if escaped.endswith('"'):
        trailing = escaped[:-1]
        backslashes = len(trailing) - len(trailing.rstrip("\\"))
        if backslashes % 2 == 0:
            escaped = f'{trailing}\\"'
    return escaped


def _repaired_escape_splits(lines: list[str]) -> list[str]:
    """*lines* with any escape sequence that word-wrapping cut in half put back.

    ``textwrap`` breaks an over-long word at a character boundary, which can fall
    inside the ``\\\\`` or ``\\"`` that :func:`_escaped_for_docstring` introduced;
    the lone backslash left behind would read as a line continuation inside the
    docstring. Moving it onto the next line restores the pair exactly, and
    :func:`_escaped_docstring_wrap` wraps one column short of the budget so the
    moved character still fits the generated width.
    """
    repaired = list(lines)
    for index in range(len(repaired) - 1):
        line = repaired[index]
        if (len(line) - len(line.rstrip("\\"))) % 2:
            repaired[index] = line[:-1]
            repaired[index + 1] = f"\\{repaired[index + 1]}"
    return repaired


def _escaped_docstring_wrap(text: str, *, width: int) -> list[str]:
    """*text* escaped for a docstring, then word-wrapped to *width*.

    Escaping comes first so the width accounting sees the characters that are
    really emitted, and the repair pass undoes the one thing that ordering can
    break.
    """
    escaped = _escaped_for_docstring(text)
    wrapped = textwrap.wrap(escaped, width=max(1, width - 1)) or [escaped]
    return _repaired_escape_splits(wrapped)


def _wrapped_docstring_lines(text: str) -> list[str]:
    """*text* escaped for a docstring and word-wrapped to fit a generated stub's
    docstring at ``_L2`` indentation, each returned line already carrying that
    indentation."""
    wrapped = _escaped_docstring_wrap(text, width=_DOCSTRING_WRAP_WIDTH)
    return [f"{_L2}{line}" for line in wrapped]


def _wrapped_comment_lines(text: str, *, indent: int) -> list[str]:
    """*text* as one or more ``#``-prefixed comment lines at *indent* spaces: a
    captured URL or candidate name is unbounded, and ``E501`` applies to a comment
    line exactly as it does to code, so every comment this module emits routes
    through here rather than risking one long line (validation fix round 3, Finding
    open in round 2, 2026-09-12). ``break_long_words`` means a URL with no spaces at
    all still cannot overflow; ``initial_indent``/``subsequent_indent`` give the
    first line ``# `` and every continuation line the hanging ``#   `` the ruling
    asked for, with ``textwrap`` doing the width accounting for both.
    """
    pad = " " * indent
    return textwrap.wrap(
        text,
        width=_GENERATED_LINE_LENGTH,
        initial_indent=f"{pad}# ",
        subsequent_indent=f"{pad}#   ",
        break_long_words=True,
        break_on_hyphens=False,
    ) or [f"{pad}# "]


def _wrapped_docstring_block(text: str, *, indent: int) -> list[str]:
    """A one-line docstring ``\"\"\"{text}\"\"\"`` at *indent* spaces when that fits the
    generated width; otherwise the same text as a multi-line docstring with the
    closing quotes on their own line (validation fix round 3, 2026-09-12). *text* is
    escaped for a docstring in both shapes (final fix wave, 2026-09-12)."""
    pad = " " * indent
    single_line = f'{pad}"""{_escaped_for_docstring(text)}"""'
    if len(single_line) <= _GENERATED_LINE_LENGTH:
        return [single_line]
    wrapped = _escaped_docstring_wrap(text, width=max(1, _GENERATED_LINE_LENGTH - indent))
    return [f'{pad}"""', *(f"{pad}{line}" for line in wrapped), f'{pad}"""']


def _quoted(value: str) -> str:
    """*value* as a complete Python string literal, quotes included.

    A captured site fact carries quoting of its own: ``har.documents`` builds a
    selector as ``form[action="/login"] input[name="user"]`` whenever the input has no
    id, and embedding that raw ends the literal at its first inner quote, so the
    generated module does not parse at all. ``json.dumps`` escapes exactly the
    characters a literal cannot hold (the quote, the backslash, the control
    characters) in a form Python reads the same way, and ``ensure_ascii=False`` leaves
    everything else as written. The quote character is the one ``ruff format`` would
    settle on (double, unless single quoting costs fewer escapes), so a generated file
    needs no second formatting pass (validation fix round 4, 2026-09-12).
    """
    quote = _quote_char(value)
    return f"{quote}{_escaped_body(value, quote)}{quote}"


def _quote_char(value: str) -> str:
    """The quote character ``ruff format`` would wrap *value* in: double, unless single
    quoting would cost fewer escapes."""
    return "'" if value.count('"') > value.count("'") else '"'


def _escaped_body(value: str, quote: str) -> str:
    """*value* as the body of a *quote*-quoted Python string literal, without the
    quotes themselves."""
    body = json.dumps(value, ensure_ascii=False)[1:-1]
    if quote == "'":
        body = body.replace('\\"', '"').replace("'", "\\'")
    return body


def _split_key_lines(key: str, *, indent: int) -> list[str]:
    """The opening of a parenthesised implicit-concatenation dict key: ``(`` at *indent*
    spaces and one quoted chunk of *key* per line below it.

    The caller supplies the closing line, which always begins ``):`` but differs after
    the colon by call site (an identifier, a quoted value, another concatenation).
    A parenthesised concatenation is a valid dict key, and ``ruff format`` leaves it
    alone once the joined form no longer fits the width.
    """
    pad = " " * indent
    continuation_pad = " " * (indent + 4)
    # A chunk is quoted after it is cut, so the budget comes off the whole key's own
    # quoting overhead (its two quotes plus whatever escaping it needs), which is an
    # upper bound on any one chunk's.
    overhead = len(_quoted(key)) - len(key)
    chunk_width = max(1, _GENERATED_LINE_LENGTH - len(continuation_pad) - overhead)
    chunks = textwrap.wrap(
        key,
        width=chunk_width,
        break_long_words=True,
        break_on_hyphens=False,
        drop_whitespace=False,
    ) or [key]
    return [f"{pad}(", *(f"{continuation_pad}{_quoted(chunk)}" for chunk in chunks)]


def _dict_entry_lines(key: str, value: str, *, indent: int) -> list[str]:
    """One ``"key": value,`` dict entry at *indent* spaces, on one line when that fits
    the generated width.

    Past that width the key renders as a parenthesised implicit concatenation
    (``(\\n "par"\\n "t"\\n): value,``), which is a valid dict key and which
    ``ruff format`` leaves alone once the joined form no longer fits: a site's own
    parameter or header name is one fact, and it is the dict key, so neither the
    exploded dict nor any identifier cap can shorten it (validation fix round 4,
    Finding 4, 2026-09-12).
    """
    pad = " " * indent
    single_line = f"{pad}{_quoted(key)}: {value},"
    if len(single_line) <= _GENERATED_LINE_LENGTH:
        return [single_line]
    return [*_split_key_lines(key, indent=indent), f"{pad}): {value},"]


def _exploded_dict_lines(name: str, entries: list[tuple[str, str]]) -> list[str]:
    """A ``name={...}`` call argument, one ``"key": value,`` entry per line, with a
    magic trailing comma on the closing brace so ``ruff format`` leaves it exploded.
    Each *entries* pair is the site's own name for the key (quoted and, if it is too
    wide, split by ``_dict_entry_lines``) and the value expression."""
    lines = [f"{_L3}{name}=" + "{"]
    for key, value in entries:
        lines.extend(_dict_entry_lines(key, value, indent=len(_L4)))
    lines.append(f"{_L3}" + "},")
    return lines


def _call_lines(prefix: str, args: list[str], *, indent: int) -> list[str]:
    """``{prefix}(arg, arg)`` at *indent* spaces on one line when it fits the generated
    width, otherwise one argument per line with a magic trailing comma so
    ``ruff format`` leaves it exploded. Both shapes are stable under the formatter, so
    the generated file needs no second pass either way."""
    pad = " " * indent
    single_line = f"{pad}{prefix}({', '.join(args)})"
    if len(single_line) <= _GENERATED_LINE_LENGTH:
        return [single_line]
    continuation_pad = " " * (indent + 4)
    return [
        f"{pad}{prefix}(",
        *(f"{continuation_pad}{arg}," for arg in args),
        f"{pad})",
    ]


def _literal_lines(
    value: str, *, indent: int, prefix: str = "", trailing_comma: bool = True
) -> list[str]:
    """A quoted Python string literal for a captured site fact (a selector, a URL, a
    header name): one line, ``{prefix}"{value}",`` at *indent* spaces, when that fits
    the generated width. Past that width a single quoted string can still overflow on
    its own even after a call or dict has been exploded one argument per line (a
    selector or header name is one fact ruff format itself never splits), so this
    falls back to an implicit string concatenation instead, each chunk from
    ``textwrap.wrap`` with whitespace preserved so the chunks rejoin to exactly
    *value* (validation fix round 2, Finding 4, 2026-09-12). ``trailing_comma=False``
    renders a plain assignment statement (``name = "value"``) rather than a call
    keyword argument (``name="value",``); both shapes reuse the same wrapping.
    """
    comma = "," if trailing_comma else ""
    pad = " " * indent
    quoted = _quoted(value)
    single_line = f"{pad}{prefix}{quoted}{comma}"
    if len(single_line) <= _GENERATED_LINE_LENGTH:
        return [single_line]
    continuation_pad = " " * (indent + 4)
    # A chunk is quoted after it is cut, so the budget comes off the whole value's own
    # quoting overhead (see _split_key_lines).
    overhead = len(quoted) - len(value)
    chunk_width = max(1, _GENERATED_LINE_LENGTH - len(continuation_pad) - overhead)
    chunks = textwrap.wrap(
        value,
        width=chunk_width,
        break_long_words=True,
        break_on_hyphens=False,
        drop_whitespace=False,
    ) or [value]
    lines = [f"{pad}{prefix}("]
    lines.extend(f"{continuation_pad}{_quoted(chunk)}" for chunk in chunks)
    lines.append(f"{pad}){comma}")
    return lines


def _literal_dict_entry_lines(key: str, value: str, *, indent: int) -> list[str]:
    """One ``"key": "value",`` dict entry where both halves are captured site facts and
    either can be too wide for a line.

    The key keeps the whole line when it leaves room for the value's opening
    parenthesis; past that, the key splits too and the value follows on the ``):``
    line, which ``_literal_lines`` renders by taking ``"): "`` as its prefix.
    """
    pad = " " * indent
    if len(f"{pad}{_quoted(key)}: (") <= _GENERATED_LINE_LENGTH:
        return _literal_lines(value, indent=indent, prefix=f"{_quoted(key)}: ")
    return [
        *_split_key_lines(key, indent=indent),
        *_literal_lines(value, indent=indent, prefix="): "),
    ]


def _exploded_literal_dict_lines(entries: list[tuple[str, str]], *, indent: int) -> list[str]:
    """The ``fields={...}`` dict on a generated ``LoginStep``: one ``"role": "selector",``
    entry per line. Unlike a stub's parameter dicts, both halves here are captured site
    facts (a credential role is the form input's own name when it is neither the
    username nor the password field), so both route through
    ``_literal_dict_entry_lines``."""
    pad = " " * indent
    lines = [f"{pad}fields={{"]
    for role, selector in entries:
        lines.extend(_literal_dict_entry_lines(role, selector, indent=indent + 4))
    lines.append(f"{pad}}},")
    return lines


def _render_login_step(form: LoginForm, *, indent: int) -> list[str]:
    """A generated ``LoginStep(...)``, exploded one keyword argument per line so a
    long selector cannot push the whole call over the generated width."""
    pad = " " * indent
    submit_value = form.submit or "GP-FILL: submit selector"
    lines = [f"{pad}LoginStep("]
    lines.extend(_exploded_literal_dict_lines(sorted(form.fields.items()), indent=indent + 4))
    lines.extend(_literal_lines(submit_value, indent=indent + 4, prefix="submit="))
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
    lines.extend(_literal_lines(source.name, indent=indent + 4, prefix=f"{value_keyword}="))
    lines.extend(_literal_lines(header.name, indent=indent + 4, prefix="header="))
    lines.append(f"{pad}),")
    return lines


_URL_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_]+)\}")


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

    return _URL_PLACEHOLDER_RE.sub(rename, template), identifiers


def _quoted_fstring(text: str) -> str:
    """*text* as a complete f-string literal: each literal span quoted the way
    ``_quoted`` quotes it and each brace outside a ``{placeholder}`` doubled, so a
    captured path holding a stray brace or a quote cannot emit a file that will not
    parse. The quote character is chosen once, for the whole literal, from the text
    outside the placeholders (a placeholder is an identifier and holds neither quote).
    """
    spans: list[str] = []
    parts: list[str] = []
    position = 0
    for match in _URL_PLACEHOLDER_RE.finditer(text):
        spans.append(text[position : match.start()])
        parts.append(match.group(0))
        position = match.end()
    spans.append(text[position:])
    quote = _quote_char("".join(spans))
    bodies = [_escaped_body(span, quote).replace("{", "{{").replace("}", "}}") for span in spans]
    woven = [bodies[0]]
    for placeholder, body in zip(parts, bodies[1:], strict=True):
        woven.extend([placeholder, body])
    return f"f{quote}{''.join(woven)}{quote}"


def _url_atoms(text: str, *, width: int) -> list[str]:
    """*text* as the smallest pieces a URL may be split between: a whole
    ``{placeholder}``, and each ``/``-introduced literal segment (itself hard-split at
    *width* when one segment is wider than a line can hold)."""
    atoms: list[str] = []
    for piece in (p for p in re.split(r"(?=/)", text) if p):
        atoms.extend(piece[i : i + width] for i in range(0, len(piece), width))
    return atoms


def _url_chunks(text: str, *, width: int) -> list[str]:
    """*text* split into chunks that each fit *width*, preferring ``/`` boundaries and
    never splitting inside a ``{placeholder}``. The chunks rejoin to exactly *text*."""
    atoms: list[str] = []
    position = 0
    for match in _URL_PLACEHOLDER_RE.finditer(text):
        atoms.extend(_url_atoms(text[position : match.start()], width=width))
        atoms.append(match.group(0))
        position = match.end()
    atoms.extend(_url_atoms(text[position:], width=width))
    chunks: list[str] = []
    for atom in atoms:
        if chunks and len(chunks[-1]) + len(atom) <= width:
            chunks[-1] += atom
        else:
            chunks.append(atom)
    return chunks or [text]


def _url_expr_lines(url_text: str, *, is_fstring: bool, indent: int) -> list[str]:
    """The URL argument of a generated stub's request call: ``f"/a/{id}/b",`` on one
    line at *indent* spaces when that fits the generated width, otherwise the same
    string as a parenthesised implicit concatenation split at ``/`` boundaries. A
    captured path template is one fact ``ruff format`` cannot split for itself, and a
    deeply nested one is past the width on its own (validation fix round 4, Finding 4,
    2026-09-12)."""
    pad = " " * indent
    render = _quoted_fstring if is_fstring else _quoted
    single_line = f"{pad}{render(url_text)},"
    if len(single_line) <= _GENERATED_LINE_LENGTH:
        return [single_line]
    continuation_pad = " " * (indent + 4)
    # A chunk is quoted after it is cut, so the budget comes off the whole template's
    # own literal overhead (its quotes, its `f`, and any escaping or brace doubling),
    # which is an upper bound on any one chunk's.
    overhead = len(render(url_text)) - len(url_text)
    chunk_width = max(1, _GENERATED_LINE_LENGTH - len(continuation_pad) - overhead)
    lines = [f"{pad}("]
    lines.extend(
        f"{continuation_pad}{render(chunk)}" for chunk in _url_chunks(url_text, width=chunk_width)
    )
    lines.append(f"{pad}),")
    return lines


def _render_command_stub(endpoint: Endpoint, seen_names: set[str], run_label: str) -> list[str]:
    method = endpoint.methods[0]
    name = _command_name(endpoint.template, seen_names)
    # "self" and "ctx" are taken before any site parameter is named, so a site
    # parameter called either cannot shadow the stub's own arguments.
    seen_params = {"self", "ctx"}
    url_text, path_params = _templated_url(endpoint.template, seen_params)
    is_json = _is_json_endpoint(endpoint)
    call, role, return_type = (
        ("request_json", "xhr", "dict") if is_json else ("request_text", "navigation", "str")
    )

    params = ["self", "ctx: CommandContext"] + [f"{p}: str" for p in path_params]
    identifier_for: dict[str, str] = {}
    for extra in sorted(set(endpoint.query_params) | set(endpoint.body_params)):
        observed = endpoint.query_params.get(extra) or endpoint.body_params.get(extra, "str")
        identifier_for[extra] = _param_identifier(extra, seen_params)
        annotation = _PY_TYPE_BY_OBSERVED.get(observed, "str")
        params.append(f"{identifier_for[extra]}: {annotation} | None = None")

    call_lines = [f'{_L3}"{method}",']
    call_lines.extend(_url_expr_lines(url_text, is_fstring=bool(path_params), indent=len(_L3)))
    call_lines.append(f'{_L3}role="{role}",')
    if endpoint.query_params:
        entries = [(p, identifier_for[p]) for p in sorted(endpoint.query_params)]
        call_lines.extend(_exploded_dict_lines("params", entries))
    if any(m in ("POST", "PUT", "PATCH") for m in endpoint.methods) and endpoint.body_params:
        entries = [(p, identifier_for[p]) for p in sorted(endpoint.body_params)]
        call_lines.extend(_exploded_dict_lines("json", entries))
    if endpoint.custom_headers:
        entries = [(h, '"GP-FILL"') for h in endpoint.custom_headers]
        call_lines.extend(_exploded_dict_lines("headers", entries))

    summary = f"{method} {endpoint.template}: seen {endpoint.count} time(s) in run {run_label}."
    # An unavailable shape is not a fact about the site, so the docstring says
    # nothing rather than guessing (polish round 1, 2026-09-12).
    shape_known = endpoint.shape is None or endpoint.shape != SHAPE_UNAVAILABLE

    lines = _call_lines("@command", [f'help="GP-FILL: describe {name}"'], indent=len(_L1))
    lines.append(f"{_L1}def {name}(")
    lines.extend(f"{_L2}{p}," for p in params)
    lines.append(f"{_L1}) -> {return_type}:")
    lines.append(f'{_L2}"""')
    lines.extend(_wrapped_docstring_lines(summary))
    if shape_known:
        shape_line = f"Shape: {summarize_shape(endpoint.shape, depth=_SCAFFOLD_SHAPE_DEPTH)}."
        lines.append("")
        lines.extend(_wrapped_docstring_lines(shape_line))
    lines.append(f'{_L2}"""')
    lines.append(f"{_L2}return ctx.{call}(")
    lines.extend(call_lines)
    lines.append(f"{_L2})")
    lines.append("")
    return lines


_LOGIN_FLOW_KINDS = ("form_page", "credential_post")


def _login_flow_endpoints(d: RunDigest) -> set[tuple[str, str]]:
    """The ``(method, templated path)`` pairs ``login_config`` owns.

    The login form's own GET and the credential POST are the login flow, which
    the generated ``login_config`` drives. Rendered as command stubs they were
    wrong for the developer and their generated tests could only fail (polish
    round 1, 2026-09-12). The digest's own endpoint list is unchanged; only the
    scaffold skips them.
    """
    owned: set[tuple[str, str]] = set()
    for observation in d.login:
        if observation.kind not in _LOGIN_FLOW_KINDS:
            continue
        template, _ = template_path(urlparse(observation.url).path or "/")
        owned.add((observation.method.upper(), template))
    return owned


def _ordered_endpoints(d: RunDigest) -> list[Endpoint]:
    """The endpoints the scaffold renders a stub for, most useful first."""
    owned = _login_flow_endpoints(d)
    kept = [e for e in d.endpoints if not all((m, e.template) in owned for m in e.methods)]
    return sorted(kept, key=lambda e: (not _is_json_endpoint(e), -e.count, e.template))


def _run_label(d: RunDigest) -> str:
    return f"{d.source.session}/{d.source.run_id}" if d.source.session else "the supplied HAR"


def _stub_endpoints(spec: ScaffoldSpec) -> list[Endpoint]:
    """The endpoints this spec renders stubs and generated tests for."""
    if spec.digest is None:
        return []
    return _ordered_endpoints(spec.digest)[:_MAX_SCAFFOLD_ENDPOINTS]


def _render_command_stubs(spec: ScaffoldSpec) -> list[str]:
    endpoints = _stub_endpoints(spec)
    if spec.digest is None or not endpoints:
        return [
            '    @command(help="GP-FILL: describe this command")',
            "    def example(self, ctx: CommandContext) -> dict:",
            '        """GP-FILL: what this command does."""',
            '        return ctx.request_json("GET", "/GP-FILL/path")',
        ]
    seen_names: set[str] = set()
    lines: list[str] = []
    for endpoint in endpoints:
        lines.extend(_render_command_stub(endpoint, seen_names, _run_label(spec.digest)))
    return lines


def _needs_login_import(spec: ScaffoldSpec) -> bool:
    return spec.digest is not None and _password_login_form(spec.digest) is not None


def _plugins_import_names(*, needs_login_import: bool) -> list[str]:
    """The names to import from ``graftpunk.plugins``, ordered the way this project's
    own isort setting (classes, then functions, each alphabetical) expects, so the
    generated line never needs a second reformatting pass."""
    classes = ["CommandContext", "SitePlugin"]
    if needs_login_import:
        classes += ["LoginConfig", "LoginStep"]
    return sorted(classes) + ["command"]


def _render_plugin_module(spec: ScaffoldSpec) -> str:
    klass = class_name_for(spec.name)
    needs_login_import = _needs_login_import(spec)
    needs_token_import = spec.digest is not None and bool(_paired_token_candidates(spec.digest))
    plugins_import = ", ".join(_plugins_import_names(needs_login_import=needs_login_import))
    lines = [
        f'"""{spec.name} plugin.',
        "",
        "Verified against a real account on: (none yet)",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        f"from graftpunk.plugins import {plugins_import}",
    ]
    if needs_token_import:
        lines.append("from graftpunk.tokens import Token, TokenConfig")
    lines += ["", "", f"class {klass}(SitePlugin):"]
    class_docstring = f"Commands for {spec.base_url or 'GP-FILL: base_url'}."
    lines.extend(_wrapped_docstring_block(class_docstring, indent=len(_L1)))
    lines.append("")
    lines.append(f'    site_name = "{spec.name}"')
    lines.append(f'    session_name = "{spec.name}"')
    lines.append(f'    help_text = "Commands for {spec.name}"')
    base_url_lines = _literal_lines(
        spec.base_url, indent=len(_L1), prefix="base_url = ", trailing_comma=False
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
        f"line-length = {_GENERATED_LINE_LENGTH}\n"
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


def _import_lines(module: str, name: str) -> list[str]:
    """``from {module} import {name}`` on one line when it fits the generated width,
    otherwise the parenthesised form with a magic trailing comma. The generated
    package name and class name are both plugin-name derivatives, so together they
    can still pass the width even with the name capped at ``_MAX_PLUGIN_NAME``, and
    ``ruff format`` would split the single line for itself."""
    single_line = f"from {module} import {name}"
    if len(single_line) <= _GENERATED_LINE_LENGTH:
        return [single_line]
    return [f"from {module} import (", f"{_L1}{name},", ")"]


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
        *_import_lines(f"{package}.plugin", klass),
        "",
        'FIXTURES_DIR = Path(__file__).parent / "fixtures"',
        "",
        "",
        "def test_plugin_instantiates() -> None:",
        f"    plugin = {klass}()",
        f'    assert plugin.site_name == "{spec.name}"',
        "",
        "",
    ]
    if not has_endpoint_tests:
        lines.append("# GP-FILL: add a test per command, against a fixture in tests/fixtures/")
        return "\n".join(lines).rstrip() + "\n"
    seen: set[str] = set()
    for endpoint in endpoints:
        name = _command_name(endpoint.template, seen)
        # The same seeding as _render_command_stub, so the identifiers here are
        # the ones the stub actually declares.
        _, path_params = _templated_url(endpoint.template, {"self", "ctx"})
        # "1" round-trips through the naming rule (paths.template_path treats
        # an all-digit segment as dynamic): the request this test issues
        # renames back to the endpoint's own template, so it finds the
        # fixture named for it. A literal "GP-FILL" would not: it stays a
        # literal segment and the fixture lookup would 404.
        lines.append(f"def test_{name}() -> None:")
        # base_url is a captured site fact and plugin_name is the user's own
        # name, so this call is exploded one keyword argument per line with
        # both values through _literal_lines (validation fix round 4,
        # 2026-09-12).
        lines.append(f"{_L1}ctx = fixture_context(")
        lines.append(f"{_L2}FIXTURES_DIR,")
        lines.extend(_literal_lines(spec.name, indent=len(_L2), prefix="plugin_name="))
        lines.extend(_literal_lines(spec.base_url, indent=len(_L2), prefix="base_url="))
        lines.append(f"{_L1})")
        lines.append(f"{_L1}plugin = {klass}()")
        lines.extend(
            _call_lines(
                f"result = plugin.{name}",
                ["ctx", *(f'{p}="1"' for p in path_params)],
                indent=len(_L1),
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
        "Fixtures under `tests/fixtures/` are hand-derived from captures in "
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
            "tests/conftest.py": _render_conftest(spec),
            "tests/test_plugin.py": _render_test_module(spec, package=package),
            "tests/fixtures/.gitkeep": "",
            ".gitignore": _render_gitignore(),
            "README.md": _render_readme(spec),
        }
    return {
        f"src/{package}/__init__.py": f'"""{spec.name}: a graftpunk plugin."""\n',
        f"src/{package}/plugin.py": plugin_module,
        f"tests/test_{module_name_for(spec.name)}.py": _render_test_module(spec, package=package),
        "tests/fixtures/.gitkeep": "",
    }
