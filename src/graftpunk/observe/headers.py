"""Header profile classification and extraction from CDP request data."""

from __future__ import annotations

from typing import Any

# Headers excluded from profiles — managed by requests or request-specific
EXCLUDED_HEADERS: frozenset[str] = frozenset(
    {
        "cookie",
        "host",
        "content-length",
        "content-type",
        "referer",
        "origin",
        ":authority",
        ":method",
        ":path",
        ":scheme",
    }
)


def classify_request(headers: dict[str, str]) -> str | None:
    """Classify a request into a header profile based on its headers.

    Classification priority:
    1. sec-fetch-mode (most reliable — Chrome always sends it)
    2. Content-Type (for form detection)
    3. X-Requested-With / Accept heuristics (for XHR detection)

    Args:
        headers: Request headers dict (case-insensitive keys expected from CDP).

    Returns:
        Profile name ("navigation", "xhr", or "form"), or None if not classifiable.
    """
    # Normalize header keys to lowercase for comparison
    lower = {k.lower(): v for k, v in headers.items()}

    # 1. sec-fetch-mode (highest priority)
    sec_fetch = lower.get("sec-fetch-mode", "")
    if sec_fetch == "navigate":
        return "navigation"
    if sec_fetch == "cors":
        return "xhr"

    # 2. Content-Type for form detection
    content_type = lower.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        return "form"

    # 3. XHR heuristics
    if lower.get("x-requested-with", "").lower() == "xmlhttprequest":
        return "xhr"
    accept = lower.get("accept", "")
    if "application/json" in accept:
        return "xhr"

    # 4. Navigation heuristic (Accept contains text/html without sec-fetch-mode)
    if "text/html" in accept:
        return "navigation"

    return None


def extract_header_profiles(
    request_map: dict[str, dict[str, Any]],
) -> dict[str, dict[str, str]]:
    """Extract header profiles from a capture backend's request map.

    Iterates all captured requests, classifies each, and stores the full
    header set (minus excluded headers) from the first matching request
    per profile.

    Args:
        request_map: The capture backend's _request_map dict. Each entry
            has at least a "headers" key with the request header dict.

    Returns:
        Dict mapping profile name to header dict, e.g.
        {"navigation": {"User-Agent": "...", ...}, "xhr": {...}}.
    """
    profiles: dict[str, dict[str, str]] = {}

    for _request_id, data in request_map.items():
        headers = data.get("headers", {})
        if not headers:
            continue

        profile = classify_request(headers)
        if profile is None or profile in profiles:
            continue

        # Store all headers except excluded ones
        filtered = {k: v for k, v in headers.items() if k.lower() not in EXCLUDED_HEADERS}
        profiles[profile] = filtered

    return profiles
