"""Move a cached session off the name graftpunk 1.24.0's account-label rule gave it (#293).

graftpunk 1.24.0 and earlier could name a session after a PIN or a one-time
code (``myshop@4417``). The account label now comes only from an allowlisted
identifier field, so the next login of the same credentials writes another slot
(``myshop``, or ``myshop@<real identifier>``). Left alone, the old slot beside
it makes every command without ``--session`` ambiguous. A login therefore runs
:func:`migrate_legacy_account_slot` first.

Delete this module, its call in :mod:`graftpunk.cli.login_commands` and
:func:`graftpunk.session_identity._legacy_derive_account_identity` in a later
release, once logins have had time to migrate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from graftpunk.cache import (
    clear_session_cache,
    get_session_metadata,
    move_session,
)
from graftpunk.logging import get_logger
from graftpunk.observe.storage import make_private_dir, session_dirname
from graftpunk.session_context import SESSION_FILE_NAME
from graftpunk.session_identity import (
    _legacy_derive_account_identity,
    join_session_name,
)

LOG = get_logger(__name__)


@dataclass(frozen=True)
class LegacySlotMigration:
    """A stale slot :func:`migrate_legacy_account_slot` found, and what it did.

    It names the field the old label came from and the new session name. The
    old name, which may hold the secret, is kept out of ``repr`` so a log of
    this object cannot carry it.

    Attributes:
        action: ``"renamed"`` (the old slot now lives under *new_name*),
            ``"deleted"`` (the old slot could not be read and was removed),
            ``"superseded"`` (*new_name* is already cached; the old slot is
            removed by :func:`finish_superseded_slot` once the login has
            written *new_name*) or ``"cleared"`` (the slot kept its name, and
            its recorded account identifier was replaced).
        source_field: The login field the old label was derived from, or None
            when the old rule derived none.
        new_name: The session name the login writes.
        stale_name: The old slot's name. Never print or log it.
        pin_updated: Whether a ``.gp-session`` file in the working directory
            named the old slot and now names *new_name*.
        env_pinned: Whether ``GRAFTPUNK_SESSION`` names the old slot, which gp
            cannot change.
    """

    action: str
    source_field: str | None
    new_name: str
    stale_name: str = field(repr=False)
    pin_updated: bool = False
    env_pinned: bool = False


def migrate_legacy_account_slot(
    base_name: str,
    credentials: Mapping[str, str],
    secret_keywords: frozenset[str],
    *,
    target_name: str,
    identifier: str | None,
    explicit_label: bool,
    observe_base_dir: Path,
    environ: Mapping[str, str],
    cwd: Path,
) -> LegacySlotMigration | None:
    """Move the slot the 1.24.0 rule named for *credentials* onto *target_name*.

    Called before the login attempt. The old rule is run on the same
    credentials. When it picks the same identifier the current rule picks,
    nothing changes and None is returned. Otherwise the slot it named
    (``<base>@<old label>``, or the bare base when it derived no label) is the
    stale slot. It is migrated when it exists and records the old identifier
    or none; one recording another identifier belongs to another account and
    is left alone.

    - *target_name* is not cached: the stale slot is renamed to it, so the
      cached session survives even if this login then fails. One that cannot
      be read (expired, corrupt, or needing the browser stack) is deleted.
    - *target_name* is cached: nothing is removed yet. The login writes
      *target_name*, and :func:`finish_superseded_slot` then deletes the
      stale slot, so a failed login loses no session.
    - The stale slot is *target_name* itself: its recorded identifier, when
      it is the old one, is replaced with *identifier*.

    A bare stale slot is left alone when the login names its label with
    ``--as`` (*explicit_label*): that login would not have written the bare
    slot under either rule.

    When the slot was renamed or deleted, its observe runs move to the new
    session's directory under *observe_base_dir* (a run whose id is already
    there is left in place), and a ``.gp-session`` file in *cwd* that names the
    old slot is rewritten to name *target_name*. ``GRAFTPUNK_SESSION`` in
    *environ* is only reported.

    Logs name fields and the new session, never the old name or any value.
    """
    old_identifier, old_label = _legacy_derive_account_identity(credentials, secret_keywords)
    if old_identifier == identifier:
        return None
    if old_label is None and explicit_label:
        return None
    stale_name = join_session_name(base_name, old_label)
    stored = get_session_metadata(stale_name)
    if stored is None:
        return None
    if stored.get("account_identifier") not in (None, old_identifier):
        return None
    source_field = _field_holding(credentials, old_identifier)

    if stale_name == target_name:
        if old_identifier is None or stored.get("account_identifier") != old_identifier:
            return None
        move_session(stale_name, stale_name, account_identifier=identifier)
        LOG.info(
            "legacy_account_identifier_cleared", session=target_name, source_field=source_field
        )
        return LegacySlotMigration("cleared", source_field, target_name, stale_name)

    if get_session_metadata(target_name) is not None:
        return LegacySlotMigration("superseded", source_field, target_name, stale_name)

    action = "renamed"
    try:
        move_session(stale_name, target_name, account_identifier=identifier)
    except Exception as exc:  # noqa: BLE001 (an unreadable stale slot is deleted instead)
        LOG.warning(
            "legacy_account_slot_unreadable",
            new_session=target_name,
            source_field=source_field,
            exc_type=type(exc).__name__,
        )
        clear_session_cache(stale_name)
        action = "deleted"
    return _settled(
        LegacySlotMigration(action, source_field, target_name, stale_name),
        observe_base_dir=observe_base_dir,
        environ=environ,
        cwd=cwd,
    )


def finish_superseded_slot(
    migration: LegacySlotMigration,
    *,
    observe_base_dir: Path,
    environ: Mapping[str, str],
    cwd: Path,
) -> LegacySlotMigration:
    """Delete a ``"superseded"`` stale slot, after the login wrote the new one.

    Its observe runs and a ``.gp-session`` pin follow as for a rename. Returns
    the migration with the action ``"deleted"``.
    """
    clear_session_cache(migration.stale_name)
    return _settled(
        replace(migration, action="deleted"),
        observe_base_dir=observe_base_dir,
        environ=environ,
        cwd=cwd,
    )


def _settled(
    migration: LegacySlotMigration,
    *,
    observe_base_dir: Path,
    environ: Mapping[str, str],
    cwd: Path,
) -> LegacySlotMigration:
    """Move what follows the slot's name, log the move, and record the pins."""
    stale_name, target_name = migration.stale_name, migration.new_name
    LOG.info(
        "legacy_account_slot_migrated",
        action=migration.action,
        new_session=target_name,
        source_field=migration.source_field,
    )
    _move_observe_runs(observe_base_dir, stale_name, target_name)
    return replace(
        migration,
        pin_updated=_repin_directory(cwd, stale_name, target_name),
        env_pinned=environ.get("GRAFTPUNK_SESSION", "").strip() == stale_name,
    )


def _field_holding(credentials: Mapping[str, str], value: str | None) -> str | None:
    """The first credential field whose value is *value*."""
    if value is None:
        return None
    return next((name for name, held in credentials.items() if held == value), None)


def _move_observe_runs(base_dir: Path, old_name: str, new_name: str) -> None:
    """Move *old_name*'s observe runs into *new_name*'s directory.

    A run whose id already exists there stays where it is, and so does the old
    directory while anything is left in it. A failure is logged, never raised:
    the runs are a record, and the login must go on.
    """
    old_dir = base_dir / session_dirname(old_name)
    new_dir = base_dir / session_dirname(new_name)
    if old_dir == new_dir or not old_dir.is_dir():
        return
    try:
        make_private_dir(new_dir)
        for run in sorted(old_dir.iterdir()):
            if not (new_dir / run.name).exists():
                run.rename(new_dir / run.name)
        if not any(old_dir.iterdir()):
            old_dir.rmdir()
    except OSError as exc:
        LOG.warning(
            "legacy_observe_runs_not_moved",
            new_session=new_name,
            error_type=type(exc).__name__,
        )


def _repin_directory(cwd: Path, old_name: str, new_name: str) -> bool:
    """Rewrite a ``.gp-session`` in *cwd* that names *old_name* to name *new_name*."""
    pin = cwd / SESSION_FILE_NAME
    try:
        if not pin.is_file() or pin.read_text().strip() != old_name:
            return False
        pin.write_text(new_name)
    except OSError as exc:
        LOG.warning("legacy_session_pin_not_updated", error_type=type(exc).__name__)
        return False
    return True
