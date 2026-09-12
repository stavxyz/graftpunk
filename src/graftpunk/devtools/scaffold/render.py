"""ScaffoldSpec -> relative-path -> file-content.

The rule for every line this module emits: a fact about the site (a URL, a
selector, a parameter name, a header name) or a call into graftpunk's public
API. Never graftpunk's own logic (plugin tooling spec, 2026-09-11, "A rule
for everything the scaffold emits").
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from graftpunk.devtools.captures import CAPTURES_DIR
from graftpunk.har.digest import Endpoint, LoginForm, RunDigest, TokenCandidate
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

# A plugin's name becomes a Python identifier fragment (the CLI command, the
# entry-point key) in more than one generated file; validated once here so
# render() can never emit a project nothing can import (validation
# Critical 1/2, 2026-09-12).
PLUGIN_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")


def validate_plugin_name(name: str) -> None:
    """Raise if *name* cannot become a valid `gp <name>` command and entry-point key."""
    if not PLUGIN_NAME_RE.fullmatch(name):
        raise ValueError(
            f"Plugin name {name!r} must start with a letter and contain only "
            "letters, digits, hyphens, and underscores"
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


def _command_name(template: str, seen: set[str]) -> str:
    segments = [s for s in template.strip("/").split("/") if s and not s.startswith("{")]
    base = re.sub(r"[^a-z0-9_]", "_", "_".join(segments).lower()) or "root"
    name = base
    counter = 1
    while name in seen:
        counter += 1
        name = f"{base}_{counter}"
    seen.add(name)
    return name


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
            lines.append(
                f"    #   {observation.order}. {observation.method} "
                f"{observation.url} ({observation.kind})"
            )
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
        lines.append(f"        # candidate success redirect target, from the run: {hint}")
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
        lines.append(
            f"    # GP-FILL: unpaired token candidate: {candidate.kind} '{candidate.name}'"
        )
    return lines


def _is_json_endpoint(endpoint: Endpoint) -> bool:
    return endpoint.shape is not None or "json" in endpoint.content_type.lower()


def _wrapped_docstring_lines(text: str) -> list[str]:
    """*text* word-wrapped to fit a generated stub's docstring at ``_L2`` indentation,
    each returned line already carrying that indentation."""
    wrapped = textwrap.wrap(text, width=_DOCSTRING_WRAP_WIDTH) or [text]
    return [f"{_L2}{line}" for line in wrapped]


def _exploded_dict_lines(keyword: str, entries: list[tuple[str, str]]) -> list[str]:
    """A ``keyword={...}`` call argument, one ``key: value,`` entry per line, with a
    magic trailing comma on the closing brace so ``ruff format`` leaves it exploded."""
    lines = [f"{_L3}{keyword}=" + "{"]
    lines.extend(f"{_L4}{key}: {value}," for key, value in entries)
    lines.append(f"{_L3}" + "},")
    return lines


def _literal_lines(value: str, *, indent: int, prefix: str = "") -> list[str]:
    """A quoted Python string literal for a captured site fact (a selector, a URL, a
    header name): one line, ``{prefix}"{value}",`` at *indent* spaces, when that fits
    the generated width. Past that width a single quoted string can still overflow on
    its own even after a call or dict has been exploded one argument per line (a
    selector or header name is one fact ruff format itself never splits), so this
    falls back to an implicit string concatenation instead, each chunk from
    ``textwrap.wrap`` with whitespace preserved so the chunks rejoin to exactly
    *value* (validation fix round 2, Finding 4, 2026-09-12).
    """
    pad = " " * indent
    single_line = f'{pad}{prefix}"{value}",'
    if len(single_line) <= _GENERATED_LINE_LENGTH:
        return [single_line]
    continuation_pad = " " * (indent + 4)
    # -2 for the quote characters wrapped around each chunk.
    chunk_width = max(1, _GENERATED_LINE_LENGTH - len(continuation_pad) - 2)
    chunks = textwrap.wrap(
        value,
        width=chunk_width,
        break_long_words=True,
        break_on_hyphens=False,
        drop_whitespace=False,
    ) or [value]
    lines = [f"{pad}{prefix}("]
    lines.extend(f'{continuation_pad}"{chunk}"' for chunk in chunks)
    lines.append(f"{pad}),")
    return lines


def _exploded_literal_dict_lines(entries: list[tuple[str, str]], *, indent: int) -> list[str]:
    """The ``fields={...}`` dict on a generated ``LoginStep``: one ``"role": "selector",``
    entry per line, each selector routed through ``_literal_lines`` since (unlike a
    stub's parameter dicts) its values are captured site facts that can themselves be
    too wide for one line."""
    pad = " " * indent
    lines = [f"{pad}fields={{"]
    for role, selector in entries:
        lines.extend(_literal_lines(selector, indent=indent + 4, prefix=f'"{role}": '))
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


def _render_command_stub(endpoint: Endpoint, seen_names: set[str], run_label: str) -> list[str]:
    method = endpoint.methods[0]
    name = _command_name(endpoint.template, seen_names)
    path_params = re.findall(r"\{([a-zA-Z0-9_]+)\}", endpoint.template)
    is_json = _is_json_endpoint(endpoint)
    call, role, return_type = (
        ("request_json", "xhr", "dict") if is_json else ("request_text", "navigation", "str")
    )

    params = ["self", "ctx: CommandContext"] + [f"{p}: str" for p in path_params]
    for extra in sorted(set(endpoint.query_params) | set(endpoint.body_params)):
        observed = endpoint.query_params.get(extra) or endpoint.body_params.get(extra, "str")
        params.append(f"{extra}: {_PY_TYPE_BY_OBSERVED.get(observed, 'str')} | None = None")

    url_expr = f'f"{endpoint.template}"' if path_params else f'"{endpoint.template}"'
    call_lines = [f'{_L3}"{method}",', f"{_L3}{url_expr},", f'{_L3}role="{role}",']
    if endpoint.query_params:
        entries = [(f'"{p}"', p) for p in sorted(endpoint.query_params)]
        call_lines.extend(_exploded_dict_lines("params", entries))
    if any(m in ("POST", "PUT", "PATCH") for m in endpoint.methods) and endpoint.body_params:
        entries = [(f'"{p}"', p) for p in sorted(endpoint.body_params)]
        call_lines.extend(_exploded_dict_lines("json", entries))
    if endpoint.custom_headers:
        entries = [(f'"{h}"', '"GP-FILL"') for h in endpoint.custom_headers]
        call_lines.extend(_exploded_dict_lines("headers", entries))

    summary = f"{method} {endpoint.template}: seen {endpoint.count} time(s) in run {run_label}."
    shape_line = f"Shape: {summarize_shape(endpoint.shape, depth=_SCAFFOLD_SHAPE_DEPTH)}."

    lines = [
        f'{_L1}@command(help="GP-FILL: describe {name}")',
        f"{_L1}def {name}(",
    ]
    lines.extend(f"{_L2}{p}," for p in params)
    lines.append(f"{_L1}) -> {return_type}:")
    lines.append(f'{_L2}"""')
    lines.extend(_wrapped_docstring_lines(summary))
    lines.append("")
    lines.extend(_wrapped_docstring_lines(shape_line))
    lines.append(f'{_L2}"""')
    lines.append(f"{_L2}return ctx.{call}(")
    lines.extend(call_lines)
    lines.append(f"{_L2})")
    lines.append("")
    return lines


def _ordered_endpoints(d: RunDigest) -> list[Endpoint]:
    return sorted(d.endpoints, key=lambda e: (not _is_json_endpoint(e), -e.count, e.template))


def _run_label(d: RunDigest) -> str:
    return f"{d.source.session}/{d.source.run_id}" if d.source.session else "the supplied HAR"


def _render_command_stubs(spec: ScaffoldSpec) -> list[str]:
    if spec.digest is None or not spec.digest.endpoints:
        return [
            '    @command(help="GP-FILL: describe this command")',
            "    def example(self, ctx: CommandContext) -> dict:",
            '        """GP-FILL: what this command does."""',
            '        return ctx.request_json("GET", "/GP-FILL/path")',
        ]
    seen_names: set[str] = set()
    lines: list[str] = []
    for endpoint in _ordered_endpoints(spec.digest)[:_MAX_SCAFFOLD_ENDPOINTS]:
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
    lines.append(f'    """Commands for {spec.base_url or "GP-FILL: base_url"}."""')
    lines.append("")
    lines.append(f'    site_name = "{spec.name}"')
    lines.append(f'    session_name = "{spec.name}"')
    lines.append(f'    help_text = "Commands for {spec.name}"')
    lines.append(f'    base_url = "{spec.base_url}"')
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
    return (
        "from graftpunk.testing.plugin import site_env_scrubber\n"
        "\n"
        'pytest_plugins = ["graftpunk.testing.plugin"]\n'
        "\n"
        f'scrub_site_env = site_env_scrubber("{_env_prefix_for(spec.name)}")\n'
    )


def _render_test_module(spec: ScaffoldSpec, *, package: str) -> str:
    klass = class_name_for(spec.name)
    # fixture_context is only used by the per-endpoint tests below: importing
    # it when there is nothing to call it with is an unused import in the
    # generated file's own ruff run (F401; validation Important 2, 2026-09-12).
    has_endpoint_tests = spec.digest is not None and bool(spec.digest.endpoints)
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
        f"from {package}.plugin import {klass}",
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
    for endpoint in _ordered_endpoints(spec.digest)[:_MAX_SCAFFOLD_ENDPOINTS]:
        name = _command_name(endpoint.template, seen)
        path_params = re.findall(r"\{([a-zA-Z0-9_]+)\}", endpoint.template)
        # "1" round-trips through the naming rule (paths.template_path treats
        # an all-digit segment as dynamic): the request this test issues
        # renames back to the endpoint's own template, so it finds the
        # fixture named for it. A literal "GP-FILL" would not: it stays a
        # literal segment and the fixture lookup would 404.
        args = ", ".join(f'{p}="1"' for p in path_params)
        call_args = f"ctx{', ' + args if args else ''}"
        lines.append(f"def test_{name}() -> None:")
        lines.append(
            f'    ctx = fixture_context(FIXTURES_DIR, plugin_name="{spec.name}", '
            f'base_url="{spec.base_url}")'
        )
        lines.append(f"    plugin = {klass}()")
        lines.append(f"    result = plugin.{name}({call_args})")
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
