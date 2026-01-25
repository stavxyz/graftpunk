"""HAR analysis for auth flow detection and API discovery.

Analyzes HAR entries to identify:
- Authentication flows (login forms, OAuth, etc.)
- Session cookies that indicate logged-in state
- API endpoints suitable for plugin commands
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlparse

from graftpunk.har.parser import HAREntry
from graftpunk.logging import get_logger

LOG = get_logger(__name__)

# URL patterns that indicate authentication-related requests
AUTH_URL_PATTERNS = [
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

# Pre-compile for performance when checking many URLs
AUTH_URL_REGEX = re.compile("|".join(AUTH_URL_PATTERNS), re.IGNORECASE)

# URL patterns indicating successful login destination
POST_LOGIN_PATTERNS = [
    r"/dashboard",
    r"/home",
    r"/account",
    r"/profile",
    r"/app",
    r"/main",
    r"/welcome",
]

POST_LOGIN_REGEX = re.compile("|".join(POST_LOGIN_PATTERNS), re.IGNORECASE)

# HTTP redirect status codes
REDIRECT_STATUS_CODES = (301, 302, 303, 307, 308)

# Patterns to exclude from API discovery
EXCLUDE_PATTERNS = [
    r"\.(js|css|png|jpg|jpeg|gif|svg|ico|woff|woff2|ttf|eot|map)(\?|$)",
    r"google-analytics",
    r"googletagmanager",
    r"facebook\.com",
    r"analytics",
    r"tracking",
    r"pixel",
    r"beacon",
    r"cdn\.",
    r"static\.",
    r"assets\.",
    r"fonts\.",
]

EXCLUDE_REGEX = re.compile("|".join(EXCLUDE_PATTERNS), re.IGNORECASE)


@dataclass
class AuthStep:
    """Single step in an authentication flow."""

    entry: HAREntry
    step_type: str  # "form_page", "login_submit", "redirect", "authenticated", "oauth"
    cookies_set: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class AuthFlow:
    """Detected authentication flow from HAR entries."""

    steps: list[AuthStep]
    session_cookies: list[str]  # Cookies that indicate logged-in state
    auth_type: str = "form"  # "form", "oauth", "api_key", "unknown"


@dataclass
class APIEndpoint:
    """Discovered API endpoint."""

    method: str
    url: str
    path: str  # URL path without domain
    params: list[str] = field(default_factory=list)  # Detected path parameters
    description: str = ""


def extract_domain(entries: list[HAREntry]) -> str:
    """Extract primary domain from HAR entries.

    Uses the most common domain across all requests.

    Args:
        entries: List of HAR entries.

    Returns:
        Primary domain string, or empty string if none found.
    """
    if not entries:
        return ""

    domains = [urlparse(e.request.url).netloc for e in entries]
    domains = [d for d in domains if d]  # Filter empty
    if not domains:
        return ""

    return Counter(domains).most_common(1)[0][0]


def _is_auth_url(url: str) -> bool:
    """Check if URL appears to be authentication-related."""
    parsed = urlparse(url)
    return bool(AUTH_URL_REGEX.search(parsed.path))


def _is_post_login_url(url: str) -> bool:
    """Check if URL appears to be a post-login destination."""
    parsed = urlparse(url)
    return bool(POST_LOGIN_REGEX.search(parsed.path))


def _get_set_cookies(entry: HAREntry) -> list[str]:
    """Get names of cookies set in response."""
    cookies = [c.get("name", "") for c in entry.response.cookies if c.get("name")]

    # Also check Set-Cookie headers
    for header_name, header_value in entry.response.headers.items():
        if header_name.lower() == "set-cookie" and "=" in header_value:
            # Extract cookie name from "name=value; ..."
            cookie_name = header_value.split("=")[0].strip()
            if cookie_name and cookie_name not in cookies:
                cookies.append(cookie_name)

    return cookies


def _detect_step_type(entry: HAREntry) -> str:
    """Determine the type of auth step for an entry."""
    url = entry.request.url
    method = entry.request.method.upper()
    status = entry.response.status

    # OAuth callback detection
    if "callback" in url.lower() or "code=" in url.lower():
        return "oauth"

    # POST to auth URL = login submission
    if method == "POST" and _is_auth_url(url):
        return "login_submit"

    # GET to auth URL = form page or redirect
    if method == "GET" and _is_auth_url(url):
        if status in REDIRECT_STATUS_CODES:
            return "redirect"
        return "form_page"

    # Redirect after auth
    if status in REDIRECT_STATUS_CODES:
        return "redirect"

    # Has new session cookies (whether on post-login page or not)
    if _get_set_cookies(entry):
        return "authenticated"

    return "unknown"


def detect_auth_flow(entries: list[HAREntry]) -> AuthFlow | None:
    """Detect authentication flow from HAR entries.

    Looks for patterns like:
    - GET /login -> POST /login -> redirect -> dashboard
    - OAuth flows with callbacks
    - API token exchanges

    Args:
        entries: List of HAR entries in chronological order.

    Returns:
        AuthFlow if detected, None otherwise.
    """
    if not entries:
        return None

    auth_steps: list[AuthStep] = []
    all_cookies_set: list[str] = []

    for entry in entries:
        url = entry.request.url
        cookies = _get_set_cookies(entry)  # Get once per entry

        # Skip if not auth-related
        if not _is_auth_url(url) and not _is_post_login_url(url):
            # But check if this sets cookies after an auth step
            if auth_steps and cookies:
                all_cookies_set.extend(cookies)
                auth_steps.append(
                    AuthStep(
                        entry=entry,
                        step_type="authenticated",
                        cookies_set=cookies,
                        description=f"Session established at {urlparse(url).path}",
                    )
                )
            continue

        step_type = _detect_step_type(entry)
        all_cookies_set.extend(cookies)

        parsed = urlparse(url)
        description = f"{entry.request.method} {parsed.path}"
        if cookies:
            description += f" (cookies: {', '.join(cookies)})"

        auth_steps.append(
            AuthStep(
                entry=entry,
                step_type=step_type,
                cookies_set=cookies,
                description=description,
            )
        )

    if not auth_steps:
        return None

    # Determine auth type
    auth_type = "form"
    for step in auth_steps:
        if step.step_type == "oauth":
            auth_type = "oauth"
            break

    # Deduplicate session cookies
    session_cookies = list(dict.fromkeys(all_cookies_set))

    LOG.info(
        "auth_flow_detected",
        steps=len(auth_steps),
        cookies=len(session_cookies),
        auth_type=auth_type,
    )

    return AuthFlow(
        steps=auth_steps,
        session_cookies=session_cookies,
        auth_type=auth_type,
    )


def _should_exclude(url: str) -> bool:
    """Check if URL should be excluded from API discovery."""
    return bool(EXCLUDE_REGEX.search(url))


def _is_api_response(entry: HAREntry) -> bool:
    """Check if response appears to be an API response."""
    content_type = entry.response.content_type or ""

    # JSON responses are API calls
    if "application/json" in content_type:
        return True

    # Check for API-like paths
    parsed = urlparse(entry.request.url)
    path = parsed.path.lower()

    api_indicators = ["/api/", "/v1/", "/v2/", "/v3/", "/graphql", "/rest/"]
    return any(indicator in path for indicator in api_indicators)


def _extract_path_params(path: str) -> tuple[str, list[str]]:
    """Extract path parameters and create template.

    Converts /users/123/posts/456 to /users/{id}/posts/{post_id}

    Args:
        path: URL path.

    Returns:
        Tuple of (template_path, param_names).
    """
    params: list[str] = []
    segments = path.split("/")
    result_segments: list[str] = []

    # Track param names to avoid duplicates
    param_counts: dict[str, int] = {}

    for segment in segments:
        if segment.isdigit():
            # This is likely an ID parameter
            # Try to infer name from previous segment
            if result_segments:
                prev = result_segments[-1].rstrip("s")  # users -> user
                base_name = f"{prev}_id" if prev and prev != "{" else "id"
            else:
                base_name = "id"

            # Handle duplicates
            count = param_counts.get(base_name, 0)
            param_name = f"{base_name}_{count}" if count > 0 else base_name
            param_counts[base_name] = count + 1

            params.append(param_name)
            result_segments.append(f"{{{param_name}}}")
        else:
            result_segments.append(segment)

    return "/".join(result_segments), params


def discover_api_endpoints(
    entries: list[HAREntry],
    domain: str | None = None,
) -> list[APIEndpoint]:
    """Discover API endpoints from HAR entries.

    Args:
        entries: List of HAR entries.
        domain: Optional domain to filter by.

    Returns:
        List of discovered API endpoints.
    """
    if not entries:
        return []

    if domain is None:
        domain = extract_domain(entries)

    seen_paths: set[str] = set()
    endpoints: list[APIEndpoint] = []

    for entry in entries:
        url = entry.request.url
        parsed = urlparse(url)

        # Filter by domain if specified
        if domain and parsed.netloc != domain:
            continue

        # Skip excluded URLs
        if _should_exclude(url):
            continue

        # Only include API-like responses
        if not _is_api_response(entry):
            continue

        # Skip failed requests
        if entry.response.status >= 400:
            continue

        method = entry.request.method.upper()
        path = parsed.path

        # Extract path parameters
        template_path, params = _extract_path_params(path)

        # Create unique key
        key = f"{method}:{template_path}"
        if key in seen_paths:
            continue
        seen_paths.add(key)

        # Generate description from path
        segments = [s for s in template_path.split("/") if s and not s.startswith("{")]
        if segments:
            description = " ".join(segments[-2:]).replace("-", " ").replace("_", " ")
            description = description.title()
        else:
            description = f"{method} request"

        endpoints.append(
            APIEndpoint(
                method=method,
                url=url,
                path=template_path,
                params=params,
                description=description,
            )
        )

    LOG.info("api_endpoints_discovered", count=len(endpoints))
    return endpoints
