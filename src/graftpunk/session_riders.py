"""Shared constants of the stored session format, free of any browser stack.

Holds the rider registry (attributes that ride a session object beside its
cookies and headers), :class:`CachedToken`, and the three session class paths.
Importing this module pulls in no session class and none of selenium,
requestium, nodriver or httpie.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from graftpunk.session_identity import GP_ACCOUNT_ATTR, GP_SESSION_NAME_ATTR


@dataclass(frozen=True)
class CachedToken:
    """An extracted token value with TTL."""

    name: str
    value: str
    extracted_at: float
    ttl: float

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("CachedToken.name must be non-empty")
        if self.ttl <= 0:
            raise ValueError("CachedToken.ttl must be positive")

    @property
    def is_expired(self) -> bool:
        """Check if the cached token has exceeded its TTL."""
        return (time.time() - self.extracted_at) > self.ttl


# The class lives here but pickles as ``graftpunk.tokens.CachedToken``: stored
# sessions, and the releases that read them, name the old path.
CachedToken.__module__ = "graftpunk.tokens"

_CACHE_ATTR = "_gp_cached_tokens"
_CSRF_TOKENS_ATTR = "_gp_csrf_tokens"
# The base URL prepare_session() obtained the CSRF tokens for: a token goes only
# to that site (GraftpunkSession._inject_csrf_tokens).
_CSRF_ORIGIN_ATTR = "_gp_csrf_origin"
_HEADER_ROLES_ATTR = "_gp_header_roles"
# The origin each captured header role was recorded on (role -> "https://host"):
# GraftpunkSession sends a captured role as captured to that origin only.
_HEADER_ROLE_ORIGINS_ATTR = "_gp_header_role_origins"

# The one registry of attributes that ride a session object beside its cookies
# and headers. Every place that moves state from one session to another
# (BrowserSession's pickle hooks, cache._api_session_from_session) iterates
# this instead of hand-listing, so a new rider is added in one place and
# cannot be forgotten by one copier (#151).
SESSION_RIDER_ATTRS: tuple[str, ...] = (
    _CACHE_ATTR,
    _CSRF_TOKENS_ATTR,
    _CSRF_ORIGIN_ATTR,
    _HEADER_ROLES_ATTR,
    _HEADER_ROLE_ORIGINS_ATTR,
    GP_SESSION_NAME_ATTR,
    GP_ACCOUNT_ATTR,
)

# The value a rider takes on a session that never had one: the dict-valued
# riders restore as a fresh empty dict (callers mutate them in place), the
# account identifier and the loaded slot name as None. Keyed by the same
# registry above.
SESSION_RIDER_DEFAULTS: dict[str, Callable[[], Any]] = {
    _CACHE_ATTR: dict,
    _CSRF_TOKENS_ATTR: dict,
    _CSRF_ORIGIN_ATTR: str,
    _HEADER_ROLES_ATTR: dict,
    _HEADER_ROLE_ORIGINS_ATTR: dict,
    GP_SESSION_NAME_ATTR: lambda: None,
    GP_ACCOUNT_ATTR: lambda: None,
}

# Three riders live in memory only, and the pickled subset is DERIVED by
# excluding them, never hand-listed. CSRF tokens are per-request and go stale
# the moment a session is put away, so a restored session re-extracts them, and
# the origin they were obtained for goes with them.
# The loaded slot name is stamped by the cache at load and save time, so it
# always describes the key this copy actually came from or went to; pickling it
# would let a blob cached under a second name carry the first one (#174).
_MEMORY_ONLY_SESSION_RIDER_ATTRS: frozenset[str] = frozenset(
    {_CSRF_TOKENS_ATTR, _CSRF_ORIGIN_ATTR, GP_SESSION_NAME_ATTR}
)
PICKLED_SESSION_RIDER_ATTRS: tuple[str, ...] = tuple(
    attr for attr in SESSION_RIDER_ATTRS if attr not in _MEMORY_ONLY_SESSION_RIDER_ATTRS
)

# (module, class name) of each session class a stored session may be, mapped to
# the kind the stored format calls it.
SESSION_CLASS_PATHS: Mapping[tuple[str, str], str] = {
    ("graftpunk.session", "BrowserSession"): "browser",
    ("graftpunk.graftpunk_session", "GraftpunkSession"): "api",
    ("requests.sessions", "Session"): "requests",
}
