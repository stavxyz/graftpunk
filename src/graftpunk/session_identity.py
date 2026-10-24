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

# The session attribute that carries the cache slot a session was last loaded
# from or saved to. It lives here beside the identifier because it is naming
# policy, not token machinery: it is the name of a slot, and it is what lets a
# write-back keyed off a bare base name find the slot in that base (#174).
GP_SESSION_NAME_ATTR = "_gp_session_name"

# One side of a session name: today's rule, unchanged.
_PART_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

#: A credential field is an account identifier only when its name, lower-cased
#: with "_", "-", "." and spaces removed, is exactly one of these. Every other
#: field is a secret: substring and token heuristics each let a secret spelling
#: through (``user_pass``, ``userPINCode``, ``loginCode``), and a secret taken
#: for an identifier becomes a session name or is left unredacted in an observe
#: run (#293). The tuple order is the label preference: when several fields
#: qualify, the one whose name comes first here is the account label. It keeps
#: the keyword order the earlier rule used (username, email, login, identifier,
#: user; ``useremail`` and ``loginemail`` matched it as email), so a plugin with
#: several identifier fields keeps the label it had; ``account`` names come last
#: because the earlier rule never derived from them.
IDENTIFIER_FIELD_NAMES: tuple[str, ...] = (
    "username",
    "email",
    "emailaddress",
    "useremail",
    "loginemail",
    "login",
    "loginid",
    "loginname",
    "identifier",
    "user",
    "userid",
    "account",
    "accountid",
)

_FIELD_NAME_NOISE_RE = re.compile(r"[_\-. ]")


def normalize_field_name(field_name: str) -> str:
    """*field_name* lower-cased with ``_``, ``-``, ``.`` and spaces removed."""
    return _FIELD_NAME_NOISE_RE.sub("", field_name.lower())


def is_identifier_field(field_name: str) -> bool:
    """Whether a credential field holds an account identifier rather than a secret.

    By the allowlist :data:`IDENTIFIER_FIELD_NAMES` only, on the name
    normalized by :func:`normalize_field_name`: ``user_name``,
    ``Email-Address`` and ``loginID`` are identifiers, and ``login_password``,
    ``user_pass``, ``userPINCode`` and anything else are secrets. The one
    definition shared by the account label (:func:`derive_account_identity`)
    and observe redaction (:class:`graftpunk.observe.run.Redaction`).
    """
    return normalize_field_name(field_name) in IDENTIFIER_FIELD_NAMES


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


def derive_account_identity(fields: Mapping[str, str]) -> tuple[str | None, str | None]:
    """Derive ``(identifier, label)`` from resolved login fields.

    The identifier is the value of the field :func:`is_identifier_field`
    accepts, and when several are accepted, the one whose normalized name
    comes first in :data:`IDENTIFIER_FIELD_NAMES`; among fields with the same
    normalized name, the first declared. A field with an empty value is
    skipped. Any other field, whatever it is called, is never derived from, so
    a one-time code, a PIN or a security answer cannot become a persisted,
    human-visible session label (#293). Returns ``(None, None)`` when no field
    qualifies: the session then keeps its bare name, and ``--as`` remains
    available to name it explicitly.

    The label is the slugified identifier, validated with
    :func:`validate_account_label`; an identifier that cannot produce a legal
    label yields ``(identifier, None)``: the identifier is still recorded in
    metadata while the session keeps its bare name.
    """
    candidates = [
        (IDENTIFIER_FIELD_NAMES.index(normalize_field_name(name)), position, value)
        for position, (name, value) in enumerate(fields.items())
        if value and is_identifier_field(name)
    ]
    if not candidates:
        return (None, None)
    identifier = min(candidates)[2]
    return (identifier, _label_for(identifier))


def _label_for(identifier: str) -> str | None:
    """The account label *identifier* slugifies to, or None when it is not a legal one."""
    label = slugify(identifier)
    if not label:
        return None
    try:
        validate_account_label(label)
    except ValueError:
        return None
    return label


# graftpunk 1.24.0 and earlier derived the label by keyword substring with a
# deny-list, which let a PIN or a 2FA code through (#293). Kept only so a login
# can find the slot that rule named and migrate it
# (graftpunk.cli.login_commands); delete it, with the migration, in a later
# release.
_LEGACY_IDENTIFIER_KEYWORDS = ("username", "email", "login", "identifier", "user")
_LEGACY_NON_IDENTIFIER_HINTS = (
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


def _legacy_derive_account_identity(
    fields: Mapping[str, str], secret_keywords: frozenset[str]
) -> tuple[str | None, str | None]:
    """``(identifier, label)`` by the rule graftpunk 1.24.0 and earlier used.

    For migration only: never name a new session with it.
    """

    def is_derivable(field_name: str) -> bool:
        lowered = field_name.lower()
        if any(k in lowered for k in secret_keywords):
            return False
        tokens = re.split(r"[_.-]", lowered)
        return not any(hint in tokens for hint in _LEGACY_NON_IDENTIFIER_HINTS)

    def pick() -> str | None:
        for keyword in _LEGACY_IDENTIFIER_KEYWORDS:
            for field_name, value in fields.items():
                if keyword in field_name.lower() and value and is_derivable(field_name):
                    return value
        return None

    identifier = pick()
    if identifier is None:
        return (None, None)
    return (identifier, _label_for(identifier))


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
