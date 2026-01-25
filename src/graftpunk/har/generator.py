"""Python plugin code generator from HAR analysis.

Generates SitePlugin subclasses from detected auth flows and API endpoints.
"""

from __future__ import annotations

import re
from textwrap import dedent

from graftpunk.har.analyzer import APIEndpoint, AuthFlow
from graftpunk.logging import get_logger

LOG = get_logger(__name__)


def _sanitize_name(name: str) -> str:
    """Convert string to valid Python identifier.

    Args:
        name: Input string.

    Returns:
        Valid Python identifier.
    """
    # Replace hyphens and spaces with underscores
    result = re.sub(r"[-\s]+", "_", name)
    # Remove invalid characters
    result = re.sub(r"[^a-zA-Z0-9_]", "", result)
    # Ensure doesn't start with number
    if result and result[0].isdigit():
        result = "_" + result
    return result.lower()


def _to_class_name(name: str) -> str:
    """Convert string to PascalCase class name.

    Args:
        name: Input string.

    Returns:
        PascalCase class name.
    """
    # Split on non-alphanumeric
    parts = re.split(r"[^a-zA-Z0-9]+", name)
    # Capitalize each part
    return "".join(part.capitalize() for part in parts if part)


def _generate_command_name(endpoint: APIEndpoint) -> str:
    """Generate command name from endpoint.

    Args:
        endpoint: API endpoint.

    Returns:
        Command name string.
    """
    # Use last non-parameter path segment
    segments = [s for s in endpoint.path.split("/") if s and not s.startswith("{")]
    if segments:
        name = segments[-1]
        # If parent is meaningful, include it
        if len(segments) >= 2:
            parent = segments[-2]
            # Avoid redundancy like "users_users"
            if parent.rstrip("s") != name.rstrip("s"):
                name = f"{parent}_{name}"
    else:
        name = endpoint.method.lower()

    # Add method prefix for non-GET
    if endpoint.method != "GET":
        method_prefix = endpoint.method.lower()
        if not name.startswith(method_prefix):
            name = f"{method_prefix}_{name}"

    return _sanitize_name(name)


def _generate_method_signature(endpoint: APIEndpoint) -> str:
    """Generate method signature for endpoint.

    Args:
        endpoint: API endpoint.

    Returns:
        Method signature parameters.
    """
    params = ["self", "session: requests.Session"]

    for param in endpoint.params:
        # Use str type for path params
        params.append(f"{param}: str")

    return ", ".join(params)


def _generate_url_format(endpoint: APIEndpoint, domain: str) -> str:
    """Generate URL format string for endpoint.

    Args:
        endpoint: API endpoint.
        domain: Target domain.

    Returns:
        URL format string with f-string interpolation.
    """
    # Path already contains {param} placeholders from analyzer
    return f"https://{domain}{endpoint.path}"


def _unique_name(base_name: str, seen: set[str]) -> str:
    """Generate unique name by appending counter if needed.

    Args:
        base_name: Base name to make unique.
        seen: Set of already-used names. Will be updated with the result.

    Returns:
        Unique name not in seen set.
    """
    name = base_name
    counter = 1
    while name in seen:
        name = f"{base_name}_{counter}"
        counter += 1
    seen.add(name)
    return name


def generate_plugin_code(
    site_name: str,
    domain: str,
    auth_flow: AuthFlow | None,
    endpoints: list[APIEndpoint],
) -> str:
    """Generate Python plugin code from analysis results.

    Args:
        site_name: Name for the plugin (used as CLI command group).
        domain: Target domain.
        auth_flow: Detected authentication flow, or None.
        endpoints: List of discovered API endpoints.

    Returns:
        Python source code for a SitePlugin subclass.
    """
    class_name = _to_class_name(site_name)
    safe_name = _sanitize_name(site_name)

    # Build auth flow comment
    auth_comment = ""
    if auth_flow:
        auth_lines = ["# Detected authentication flow:"]
        for i, step in enumerate(auth_flow.steps, 1):
            auth_lines.append(f"#   {i}. {step.description}")
        if auth_flow.session_cookies:
            auth_lines.append(f"# Session cookies: {', '.join(auth_flow.session_cookies)}")
        auth_comment = "\n".join(auth_lines) + "\n\n"

    # Generate command methods
    command_methods = []
    seen_names: set[str] = set()

    for endpoint in endpoints:
        cmd_name = _unique_name(_generate_command_name(endpoint), seen_names)

        signature = _generate_method_signature(endpoint)
        url = _generate_url_format(endpoint, domain)

        # Build method
        method_lines = [
            f'    @command(help="{endpoint.description}")',
            f"    def {cmd_name}({signature}) -> dict:",
        ]

        # Add docstring with endpoint info
        method_lines.append(f'        """{endpoint.method} {endpoint.path}"""')

        # Generate request call
        if endpoint.params:
            method_lines.append(f'        url = f"{url}"')
            method_lines.append(f"        return session.{endpoint.method.lower()}(url).json()")
        else:
            method_lines.append(f'        return session.{endpoint.method.lower()}("{url}").json()')

        command_methods.append("\n".join(method_lines))

    # If no endpoints, add a placeholder
    if not command_methods:
        command_methods.append(
            dedent("""
            @command(help="Example command - replace with actual API call")
            def example(self, session: requests.Session) -> dict:
                \"\"\"Placeholder command.\"\"\"
                return session.get(f"https://{domain}/api/example").json()
            """)
            .strip()
            .replace("{domain}", domain)
        )

    methods_code = "\n\n".join(command_methods)

    # Generate full plugin code
    code = (
        dedent(f'''
        """Plugin for {domain}.

        Generated from HAR file by graftpunk.
        Review and customize before use.
        """

        import requests

        from graftpunk.plugins import SitePlugin, command


        {auth_comment}class {class_name}Plugin(SitePlugin):
            """Commands for {domain} API."""

            site_name = "{safe_name}"
            session_name = "{safe_name}"
            help_text = "Commands for {domain}"

        {methods_code}
    ''').strip()
        + "\n"
    )

    LOG.info(
        "plugin_code_generated",
        site_name=site_name,
        endpoints=len(endpoints),
        has_auth_flow=auth_flow is not None,
    )

    return code


def generate_yaml_plugin(
    site_name: str,
    domain: str,
    auth_flow: AuthFlow | None,
    endpoints: list[APIEndpoint],
) -> str:
    """Generate YAML plugin from analysis results.

    Args:
        site_name: Name for the plugin.
        domain: Target domain.
        auth_flow: Detected authentication flow, or None.
        endpoints: List of discovered API endpoints.

    Returns:
        YAML plugin content.
    """
    safe_name = _sanitize_name(site_name)

    lines = [
        f"# Plugin for {domain}",
        "# Generated from HAR file by graftpunk.",
        "# Review and customize before use.",
        "",
        f"site_name: {safe_name}",
        f"session_name: {safe_name}",
        f'help: "Commands for {domain}"',
        f"base_url: https://{domain}",
        "",
    ]

    # Add auth flow comment
    if auth_flow:
        lines.append("# Detected authentication flow:")
        for i, step in enumerate(auth_flow.steps, 1):
            lines.append(f"#   {i}. {step.description}")
        if auth_flow.session_cookies:
            lines.append(f"# Session cookies: {', '.join(auth_flow.session_cookies)}")
        lines.append("")

    lines.append("commands:")

    seen_names: set[str] = set()
    for endpoint in endpoints:
        cmd_name = _unique_name(_generate_command_name(endpoint), seen_names)

        lines.append(f"  {cmd_name}:")
        lines.append(f'    help: "{endpoint.description}"')
        lines.append(f"    method: {endpoint.method}")
        lines.append(f'    url: "{endpoint.path}"')

        # Add params if any
        if endpoint.params:
            lines.append("    params:")
            for param in endpoint.params:
                lines.append(f"      - name: {param}")
                lines.append("        type: str")
                lines.append("        required: true")
                lines.append("        is_option: false")
                lines.append(f'        help: "The {param.replace("_", " ")}"')

        lines.append("")

    # If no endpoints, add placeholder
    if not endpoints:
        lines.extend(
            [
                "  example:",
                '    help: "Example command - replace with actual API call"',
                "    method: GET",
                '    url: "/api/example"',
                "",
            ]
        )

    return "\n".join(lines)
