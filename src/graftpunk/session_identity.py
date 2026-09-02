"""Session identity: the ``name@label`` grammar, derivation, and resolution policy.

This module owns everything about what a session name *means*. It imports
nothing from ``graftpunk.cache`` (callers hand it data), so naming/identity
policy sits above storage mechanism and stays cycle-free (#151).

**Contract for future contributors:** This is a pure-policy core plus ONE
composition function that reads ambient state (:func:`compute_operating_session_name`);
new functions here take data as arguments. If the precedence chain ever grows a
second axis — another tier, another skip-flag — split it into named entry points
or have callers pass the ambient value in, rather than adding a second boolean.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from slugify import slugify

from graftpunk.exceptions import AmbiguousSessionError

# The session attribute that carries the account identifier. Owned here so no
# other module hard-codes the string (precedent: tokens.py's _CACHE_ATTR).
GP_ACCOUNT_ATTR = "_gp_account_identifier"

# One side of a session name: today's rule, unchanged.
_PART_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Identifier keywords, checked keyword-major (for each keyword, fields in
# declaration order). The user⊂username overlap is intentional: username is
# checked first, so it wins.
_IDENTIFIER_KEYWORDS = ("username", "email", "login", "identifier", "user")


def split_session_name(name: str) -> tuple[str, str | None]:
    """Split ``base@label`` into its halves; a bare name has ``label=None``."""
    base, sep, label = name.partition("@")
    return (base, label if sep else None)


def join_session_name(base: str, label: str | None) -> str:
    """Inverse of :func:`split_session_name`."""
    return f"{base}@{label}" if label else base


def validate_session_name(name: str) -> None:
    """Validate a session name (``base`` or ``base@label``).

    Each side must match ``[a-z0-9][a-z0-9_-]*``; at most one ``@``, never
    first or last; dots are forbidden (they indicate domains in
    ``gp session clear``).

    Raises:
        ValueError: If the name is invalid.
    """
    if not name:
        raise ValueError("Session name must be non-empty")
    if "." in name:
        raise ValueError(
            f"Session name {name!r} cannot contain dots. "
            "Dots are reserved for domain matching in 'gp session clear'."
        )
    if name.count("@") > 1:
        raise ValueError(f"Session name {name!r} may contain at most one '@'")
    base, label = split_session_name(name)
    for part in (base, label) if label is not None else (base,):
        if not _PART_RE.match(part):
            raise ValueError(
                f"Session name {name!r} must match [a-z0-9][a-z0-9_-]* on each "
                "side of an optional '@' (lowercase alphanumeric, hyphens, underscores)"
            )


def validate_account_label(label: str) -> None:
    """Validate one account label (the ``@label`` half), on its own terms.

    A label-specific entry point so a bad ``gp <site> login --as <label>``
    names the label the user typed rather than a synthetic session name.

    Raises:
        ValueError: If the label is empty or malformed.
    """
    if not label:
        raise ValueError("Account label must be non-empty")
    if not _PART_RE.match(label):
        raise ValueError(f"Account label {label!r} must match [a-z0-9][a-z0-9_-]*")


def resolve_account_session(base_name: str, existing_names: Iterable[str]) -> str:
    """Pick the session for *base_name* among *existing_names*.

    Candidates are ``base_name`` itself plus any name whose split base equals
    ``base_name``. Exactly one candidate is returned; zero returns
    ``base_name`` (the caller's not-found path handles absence, as today);
    several raise :class:`AmbiguousSessionError` — never "most recent wins".
    """
    candidates = [
        n for n in existing_names if n == base_name or split_session_name(n)[0] == base_name
    ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        return base_name
    raise AmbiguousSessionError(base_name, sorted(candidates))


def derive_account_identity(
    fields: Mapping[str, str], secret_keywords: frozenset[str]
) -> tuple[str | None, str | None]:
    """Derive ``(identifier, label)`` from resolved login fields.

    The identifier is the value of the first non-secret field whose name
    contains an identifier keyword (keyword-major order); failing that, the
    first field that is not secret per *secret_keywords*. A field name
    matching a secret keyword is never picked as the identifier — even when
    it also matches an identifier keyword (e.g. ``login_token`` matches both
    "login" and "token") — because the identifier becomes a persisted,
    human-visible session label. The label is the slugified identifier.
    Returns ``(None, None)`` when nothing usable exists — the session then
    keeps its bare legacy name.
    """

    def is_secret(field_name: str) -> bool:
        lowered = field_name.lower()
        return any(k in lowered for k in secret_keywords)

    def pick() -> str | None:
        for keyword in _IDENTIFIER_KEYWORDS:
            for field_name, value in fields.items():
                if keyword in field_name.lower() and value and not is_secret(field_name):
                    return value
        for field_name, value in fields.items():
            if value and not is_secret(field_name):
                return value
        return None

    identifier = pick()
    if identifier is None:
        return (None, None)
    label = slugify(identifier)
    return (identifier, label or None)


def compute_operating_session_name(
    explicit: str | None,
    base_name: str,
    existing_names: Iterable[str],
    *,
    use_ambient: bool = True,
) -> str:
    """The one home of the precedence chain: flag > env > .gp-session > resolution.

    ``use_ambient=False`` (the Python API's deliberate mode) skips the env and
    per-directory tiers: library code is not steered by ambient shell state.
    Raises :class:`AmbiguousSessionError` when nothing selects and several
    sessions are cached for *base_name*.
    """
    if explicit:
        return explicit
    if use_ambient:
        from graftpunk.session_context import get_active_session

        ambient = get_active_session()
        if ambient:
            return ambient
    return resolve_account_session(base_name, existing_names)
