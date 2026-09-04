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
from collections.abc import Callable, Iterable, Mapping
from typing import cast

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

# Derivation-only exclusions. Deliberately NOT SECRET_KEYWORDS (that set drives
# prompt masking, where masking is deliberately over-eager and matches by
# substring). These match by whole TOKEN instead — the field name split on
# "_"/"-"/"." — so "pin" excludes "otp_pin" but not "shipping_email" or
# "pinnacle_user" (a substring match would wrongly skip both). A field whose
# name contains one of these as a token is never derived from — even when it
# also matches an identifier keyword, so ``login_code`` is skipped despite
# matching "login" (#151).
_NON_IDENTIFIER_HINTS = (
    "code",
    "otp",
    "pin",
    "passphrase",
    "answer",
    "captcha",
    "mfa",
    "2fa",
    "totp",
)


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

    The identifier is the value of the first field whose name contains an
    identifier keyword (keyword-major order: username, email, login,
    identifier, user) and is neither secret per *secret_keywords* nor
    non-identifier-shaped per :data:`_NON_IDENTIFIER_HINTS`. The set of
    derivable fields is therefore closed by construction: there is no
    "first non-secret field" fallback, so a one-time code, a PIN or a
    security answer can never become a persisted, human-visible session
    label. Returns ``(None, None)`` when no field qualifies — the session
    then keeps its bare legacy name, and ``--as`` remains available to name
    it explicitly.

    The label is the slugified identifier, validated with
    :func:`validate_account_label`; an identifier that cannot produce a legal
    label yields ``(identifier, None)`` — the identifier is still recorded in
    metadata while the session keeps its bare name.
    """

    def is_derivable(field_name: str) -> bool:
        lowered = field_name.lower()
        if any(k in lowered for k in secret_keywords):
            return False
        tokens = re.split(r"[_.-]", lowered)
        return not any(hint in tokens for hint in _NON_IDENTIFIER_HINTS)

    def pick() -> str | None:
        for keyword in _IDENTIFIER_KEYWORDS:
            for field_name, value in fields.items():
                if keyword in field_name.lower() and value and is_derivable(field_name):
                    return value
        return None

    identifier = pick()
    if identifier is None:
        return (None, None)
    label = slugify(identifier)
    if not label:
        return (identifier, None)
    try:
        validate_account_label(label)
    except ValueError:
        return (identifier, None)
    return (identifier, label)


class _AmbientUnread:
    """Sentinel: the caller did not read ambient state, so read it here."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<ambient unread>"


AMBIENT_UNREAD = _AmbientUnread()


def compute_operating_session_name(
    explicit: str | None,
    base_name: str,
    existing_names: Iterable[str] | Callable[[], Iterable[str]],
    *,
    use_ambient: bool = True,
    ambient: str | None | _AmbientUnread = AMBIENT_UNREAD,
) -> str:
    """The one home of the precedence chain: flag > env > .gp-session > resolution.

    The ambient tier (env ``GRAFTPUNK_SESSION`` / ``.gp-session``, read via
    :func:`graftpunk.session_context.get_active_session`) is base-scoped: it
    applies only when the ambient name belongs to *base_name*
    (``split_session_name(ambient)[0] == base_name``). An ambient name for a
    different base is ignored and resolution falls through to
    :func:`resolve_account_session`. The *explicit* argument is NOT
    base-scoped: an explicit ``--session`` always wins outright, whatever its
    base.

    ``use_ambient=False`` (the Python API's deliberate mode) skips the env and
    per-directory tiers: library code is not steered by ambient shell state.

    **The pin contract.** A pin is returned as given — this function neither
    resolves nor rejects it — because a BARE pin names a BASE, not a slot, and
    is resolved at LOAD time by
    :func:`graftpunk.cache.load_session_for_api_resolved`: a slot cached under
    the bare name itself wins, else the single labelled account under that
    base, else ``AmbiguousSessionError``. A LABELLED pin is exact. Whichever
    slot the load lands on is the operating name for every write-back of that
    invocation, so callers replace the pin with the name the load reports
    (#182) rather than keeping the one they passed in here.

    Both pins are validated before anything is listed or loaded: a name that
    cannot exist ("../../x", "MyShop@Alice") is a caller/config error, not a
    lookup miss, and must never reach storage.

    Args:
        explicit: The ``--session`` value, or None.
        base_name: The plugin's base session name.
        existing_names: The cached session names, or a zero-argument callable
            returning them. A callable is invoked ONLY when the resolution
            tier is reached, so a pinned invocation costs no listing (a
            network round-trip on S3/Supabase).
        use_ambient: Whether the env and per-directory tiers apply.
        ambient: The already-read ambient session name, for callers that read
            :func:`~graftpunk.session_context.get_active_session` themselves
            (e.g. to log a decision). Left unset, this function reads it —
            once, and only when the ambient tier is reached.

    Raises:
        ValueError: The explicit or ambient name is not a legal session name.
        AmbiguousSessionError: Nothing selects and several sessions are cached
            for *base_name*.
    """
    if explicit:
        validate_session_name(explicit)
        return explicit
    if use_ambient:
        if isinstance(ambient, _AmbientUnread):
            from graftpunk.session_context import get_active_session

            ambient = get_active_session()
        if ambient:
            # A garbage GRAFTPUNK_SESSION or .gp-session is a config error:
            # surface it loudly rather than silently falling through.
            validate_session_name(ambient)
            if split_session_name(ambient)[0] == base_name:
                return ambient
    if callable(existing_names):
        # Only here — the resolution tier — does a listing actually happen.
        names: Iterable[str] = cast("Callable[[], Iterable[str]]", existing_names)()
    else:
        names = existing_names
    return resolve_account_session(base_name, names)
