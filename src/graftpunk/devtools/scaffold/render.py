"""ScaffoldSpec -> relative-path -> file-content.

The rule for every line this module emits: a fact about the site (a URL, a
selector, a parameter name, a header name) or a call into graftpunk's public
API. Never graftpunk's own logic (plugin tooling spec, 2026-09-11, "A rule
for everything the scaffold emits").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from graftpunk.devtools.captures import CAPTURES_DIR
from graftpunk.har.digest import Endpoint, RunDigest, TokenCandidate
from graftpunk.har.report import summarize_shape

__all__ = ["ScaffoldSpec", "class_name_for", "render"]

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


@dataclass(frozen=True)
class ScaffoldSpec:
    name: str
    mode: Literal["new_project", "add_to_suite"]
    backend: Literal["nodriver", "selenium"]
    base_url: str
    digest: RunDigest | None = None
    graftpunk_version: str = ""


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


def _render_login_config(spec: ScaffoldSpec) -> list[str]:
    if spec.digest is None:
        return [
            "    # GP-FILL: no run digest available.",
            '    # login_config = LoginConfig(steps=[LoginStep(fields={...}, submit="...")])',
        ]
    form = next((f for f in spec.digest.login_forms if "password" in f.fields), None)
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
    fields_repr = ", ".join(
        f'"{role}": "{selector}"' for role, selector in sorted(form.fields.items())
    )
    submit_repr = form.submit or "GP-FILL: submit selector"
    hint = _redirect_target_after_credential_post(spec.digest)
    lines = [
        "    login_config = LoginConfig(",
        "        steps=[",
        f'            LoginStep(fields={{{fields_repr}}}, submit="{submit_repr}"),',
        "        ],",
        f'        url="{form.action}",',
        '        failure="GP-FILL: text on the page indicating login failure",',
    ]
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
        lines.append("    token_config = TokenConfig(tokens=[")
        for header, source in pairs:
            if source.kind == "meta":
                lines.append(
                    f'        Token.from_meta_tag(name="{source.name}", header="{header.name}"),'
                )
            else:
                lines.append(
                    f'        Token.from_cookie(cookie_name="{source.name}", '
                    f'header="{header.name}"),'
                )
        lines.append("    ])")
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


def _render_command_stub(endpoint: Endpoint, seen_names: set[str], run_label: str) -> list[str]:
    method = endpoint.methods[0]
    name = _command_name(endpoint.template, seen_names)
    path_params = re.findall(r"\{([a-zA-Z0-9_]+)\}", endpoint.template)
    is_json = _is_json_endpoint(endpoint)
    call, role, return_type = (
        ("request_json", "xhr", "dict") if is_json else ("request_text", "navigation", "str")
    )

    sig_parts = ["self", "ctx: CommandContext"] + [f"{p}: str" for p in path_params]
    for extra in sorted(set(endpoint.query_params) | set(endpoint.body_params)):
        observed = endpoint.query_params.get(extra) or endpoint.body_params.get(extra, "str")
        sig_parts.append(f"{extra}: {_PY_TYPE_BY_OBSERVED.get(observed, 'str')} | None = None")

    url_expr = f'f"{endpoint.template}"' if path_params else f'"{endpoint.template}"'
    call_kwargs = [f'role="{role}"']
    if endpoint.query_params:
        query_dict = ", ".join(f'"{p}": {p}' for p in sorted(endpoint.query_params))
        call_kwargs.append(f"params={{{query_dict}}}")
    if any(m in ("POST", "PUT", "PATCH") for m in endpoint.methods) and endpoint.body_params:
        body_dict = ", ".join(f'"{p}": {p}' for p in sorted(endpoint.body_params))
        call_kwargs.append(f"json={{{body_dict}}}")
    if endpoint.custom_headers:
        header_dict = ", ".join(f'"{h}": "GP-FILL"' for h in endpoint.custom_headers)
        call_kwargs.append(f"headers={{{header_dict}}}")

    return [
        f'    @command(help="GP-FILL: describe {name}")',
        f"    def {name}({', '.join(sig_parts)}) -> {return_type}:",
        (
            f'        """{method} {endpoint.template}: seen {endpoint.count} time(s) in run '
            f"{run_label}. Shape: "
            f'{summarize_shape(endpoint.shape, depth=_SCAFFOLD_SHAPE_DEPTH)}."""'
        ),
        f'        return ctx.{call}("{method}", {url_expr}, {", ".join(call_kwargs)})',
        "",
    ]


def _ordered_endpoints(d: RunDigest) -> list[Endpoint]:
    return sorted(d.endpoints, key=lambda e: (not _is_json_endpoint(e), -e.count, e.template))


def _run_label(d: RunDigest) -> str:
    return f"{d.source.session}/{d.source.run_id}" if d.source.session else "the supplied HAR"


def _render_command_stubs(spec: ScaffoldSpec) -> list[str]:
    if spec.digest is None:
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


def _render_plugin_module(spec: ScaffoldSpec) -> str:
    klass = class_name_for(spec.name)
    needs_token_import = spec.digest is not None and bool(_paired_token_candidates(spec.digest))
    lines = [
        f'"""{spec.name} plugin.',
        "",
        "Verified against a real account on: (none yet)",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from graftpunk.plugins import CommandContext, LoginConfig, LoginStep, SitePlugin, command",
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
    package = f"graftpunk_{spec.name}"
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
        "line-length = 100\n"
        "\n"
        "[tool.ruff.lint]\n"
        'select = ["E", "F", "I", "UP", "B"]\n'
    )


def _render_conftest(spec: ScaffoldSpec) -> str:
    return (
        'pytest_plugins = ["graftpunk.testing.plugin"]\n'
        "from graftpunk.testing.plugin import site_env_scrubber\n"
        "\n"
        f'scrub_site_env = site_env_scrubber("{_env_prefix_for(spec.name)}")\n'
    )


def _render_test_module(spec: ScaffoldSpec, *, package: str) -> str:
    klass = class_name_for(spec.name)
    lines = [
        f'"""Tests for the {spec.name} plugin."""',
        "",
        "from __future__ import annotations",
        "",
        "from pathlib import Path",
        "",
        f"from {package}.plugin import {klass}",
        "from graftpunk.testing import fixture_context",
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
    if spec.digest is None:
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
    package = f"graftpunk_{spec.name}"
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
        f"tests/test_{spec.name}.py": _render_test_module(spec, package=package),
        "tests/fixtures/.gitkeep": "",
    }
