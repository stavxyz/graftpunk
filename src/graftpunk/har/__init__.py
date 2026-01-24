"""HAR (HTTP Archive) file parsing and analysis.

This module provides tools for importing HAR files captured from browser
developer tools and generating graftpunk plugins from them.

Example usage:
    from graftpunk.har import parse_har_file, detect_auth_flow, generate_plugin_code

    entries = parse_har_file("auth-flow.har")
    auth_flow = detect_auth_flow(entries)
    code = generate_plugin_code("mysite", "example.com", auth_flow, endpoints)
"""

from graftpunk.har.analyzer import (
    APIEndpoint,
    AuthFlow,
    AuthStep,
    detect_auth_flow,
    discover_api_endpoints,
    extract_domain,
)
from graftpunk.har.generator import generate_plugin_code
from graftpunk.har.parser import HAREntry, HARRequest, HARResponse, parse_har_file

__all__ = [
    # Parser
    "HAREntry",
    "HARRequest",
    "HARResponse",
    "parse_har_file",
    # Analyzer
    "APIEndpoint",
    "AuthFlow",
    "AuthStep",
    "detect_auth_flow",
    "discover_api_endpoints",
    "extract_domain",
    # Generator
    "generate_plugin_code",
]
