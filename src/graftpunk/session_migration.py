"""Deal with the cached sessions graftpunk 1.24.0's account-label rule named after a secret (#293).

graftpunk 1.24.0 and earlier could name a session after a PIN, a password or
a one-time code (``myshop@4417``). The label now comes only from an identifier
field, so the next login of the same credentials writes another slot
(``myshop``, or ``myshop@<identifier>``). Left alone, the old slot beside it
makes every command without ``--session`` ambiguous, and its name shows the
secret.

A login runs :func:`migrate_legacy_account_slot` before the attempt and
:func:`finish_superseded_slot` after a successful one. Only a slot whose name
this login's credentials reproduce is migrated: a static secret (a PIN, a
password) names the same slot every time, a one-time code never does. A
leftover slot is recognised by :func:`is_legacy_secret_slot`, which the login
counts and the "Several sessions cached" error masks, but it is never deleted
automatically, since nothing shows which account it belongs to.

The old name holds the secret, so it is never printed, and every storage call
on it runs under :func:`hidden_name`, which writes a placeholder in its place
in graftpunk's log lines.

Delete this module, its calls, and the ``_legacy_*`` functions of
:mod:`graftpunk.session_identity` in a later release.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from graftpunk.cache import (
    clear_session_cache,
    copy_session,
    get_session_metadata,
)
from graftpunk.exceptions import SessionNotFoundError
from graftpunk.logging import get_logger, redacted_values
from graftpunk.observe.storage import make_private_dir, session_dirname
from graftpunk.session_context import SESSION_FILE_NAME
from graftpunk.session_identity import (
    _legacy_derive_account_identity,
    _legacy_secret_field,
    is_identifier_field,
    join_session_name,
    label_for,
    split_session_name,
)
from graftpunk.storage.base import parse_datetime_iso

LOG = get_logger(__name__)

EARLIER_LABEL = "<earlier label>"


def earlier_name(base_name: str) -> str:
    """How a slot named after a secret is shown: ``<base>@<earlier label>``."""
    return f"{base_name}@{EARLIER_LABEL}"


@contextmanager
def hidden_name(name: str) -> Iterator[None]:
    """Within the block, graftpunk's log lines show *name* as :func:`earlier_name`.

    Its observe directory name (``myshop-4417``) is hidden the same way.
    """
    base, _label = split_session_name(name)
    with redacted_values(
        (name, earlier_name(base)),
        (session_dirname(name), f"{base}-{EARLIER_LABEL}"),
    ):
        yield


def is_legacy_secret_slot(name: str, metadata: Mapping[str, Any] | None) -> bool:
    """Whether *name* looks like a slot graftpunk 1.24.0 labelled from a login field.

    True when the slot is labelled, records no account rule (it was written
    by 1.24.0 or earlier, see :data:`graftpunk.session_identity.ACCOUNT_RULE`),
    and its label is the slug of the account identifier it records. Whether
    that field was a secret depends on the plugin: the caller asks only for a
    plugin whose 1.24.0 label field (:func:`plugin_secret_field`) is not an
    identifier field, and for such a plugin every slot 1.24.0 labelled itself
    came from that field. A slot labelled with ``--as`` fails the slug test.
    """
    _base, label = split_session_name(name)
    if label is None or metadata is None or metadata.get("account_rule") is not None:
        return False
    identifier = metadata.get("account_identifier")
    return bool(identifier) and label_for(str(identifier)) == label


def records_legacy_secret(metadata: Mapping[str, Any] | None) -> bool:
    """Whether a slot of a plugin with a secret label field records that secret as its account.

    True for any slot 1.24.0 or earlier wrote with an account identifier,
    ``--as`` slots included, since 1.24.0 recorded the secret field's value.
    """
    return (
        metadata is not None
        and metadata.get("account_rule") is None
        and bool(metadata.get("account_identifier"))
    )


def plugin_secret_field(field_names: Iterable[str], secret_keywords: frozenset[str]) -> str | None:
    """The field 1.24.0 labelled this plugin's sessions from, when it is not an identifier."""
    return _legacy_secret_field(field_names, secret_keywords)


@dataclass(frozen=True)
class LegacySlotMigration:
    """What :func:`migrate_legacy_account_slot` found and did.

    It names the field the old label came from and the new session name. The
    old name, which holds the secret, is kept out of ``repr`` so a log of this
    object cannot carry it.

    Attributes:
        source_field: The login field 1.24.0 labelled the session from.
        new_name: The session name this login writes.
        stale_name: The old slot's name, or None when no slot of that name is
            cached. Never print or log it.
        action: For the old slot: ``"renamed"`` (copied to *new_name*, which
            was a bare name not yet cached, and removed), ``"superseded"`` (to
            be removed by :func:`finish_superseded_slot` once the login has
            written *new_name*), ``"deleted"`` (removed: it had expired, or
            :func:`finish_superseded_slot` removed it) or ``"kept"`` (left in
            place; *problem* says why). None when there is no old slot.
        problem: A one-line account of something left undone, naming the
            field and never the old name.
        cleared_target: Whether *new_name* recorded the secret as its account
            (a 1.24.0 ``--as`` slot) and now records this login's identifier.
        leftovers: How many other slots of this plugin look named after the
            field by 1.24.0 (:func:`is_legacy_secret_slot`). They are only
            counted: nothing shows which account each belongs to.
        env_pinned: Whether ``GRAFTPUNK_SESSION`` names the old slot, which gp
            cannot change.
        pin_updated: Whether a ``.gp-session`` in the working directory named
            the old slot and now names *new_name*.
    """

    source_field: str
    new_name: str
    stale_name: str | None = field(default=None, repr=False)
    action: str | None = None
    problem: str | None = None
    cleared_target: bool = False
    leftovers: int = 0
    env_pinned: bool = False
    pin_updated: bool = False


def migrate_legacy_account_slot(
    base_name: str,
    credentials: Mapping[str, str],
    secret_keywords: frozenset[str],
    *,
    target_name: str,
    identifier: str | None,
    observe_base_dir: Path,
    environ: Mapping[str, str],
    cwd: Path,
    existing_names: Iterable[str],
) -> LegacySlotMigration | None:
    """Before the login attempt: deal with what 1.24.0's rule left for these credentials.

    The old rule runs on the same credentials. When it picks an identifier
    field, or the identifier the current rule picks, nothing is done and None
    is returned. Otherwise the field it picked holds a secret, and:

    - When *target_name* is cached, was written by 1.24.0, and records the
      secret as its account (a ``--as`` slot), that record is replaced with
      *identifier* now, so a failed login does not leave it.
    - The slot the old rule names from these credentials (the stale slot) is
      handled when it is cached and records the same secret. A slot past its
      expiry is deleted now: it could not be used. When *target_name* is the
      bare base and not cached, the stale slot is copied to it, read back, and
      only then deleted, so the session survives a failed login. Otherwise it
      is ``"superseded"``: it is deleted after the login succeeds, since a
      labelled *target_name* claims an account this login has not proved yet.
      A copy that fails leaves the stale slot in place, superseded.
    - Other slots that look named by 1.24.0 from the same field (a one-time
      code changes every login, so its slot is never the stale slot) are
      counted for a notice and left alone.

    *existing_names* are the cached session names, for that count.
    """
    old_identifier, old_label = _legacy_derive_account_identity(credentials, secret_keywords)
    if old_identifier is None or old_identifier == identifier:
        return None
    source_field = next(name for name, value in credentials.items() if value == old_identifier)
    if is_identifier_field(source_field):
        return None
    result = LegacySlotMigration(source_field, target_name)

    target_meta = get_session_metadata(target_name)
    if (
        target_meta is not None
        and records_legacy_secret(target_meta)
        and target_meta.get("account_identifier") == old_identifier
    ):
        try:
            copy_session(target_name, target_name, account_identifier=identifier)
            result = replace(result, cleared_target=True)
            LOG.info("legacy_account_identifier_cleared", session=target_name, field=source_field)
        except Exception as exc:  # noqa: BLE001 (left as it is; a successful login rewrites it)
            LOG.warning(
                "legacy_account_identifier_not_cleared",
                session=target_name,
                field=source_field,
                exc_type=type(exc).__name__,
            )

    stale_name = join_session_name(base_name, old_label) if old_label else None
    if stale_name is not None and stale_name != target_name:
        result = _handle_stale(
            result,
            stale_name,
            old_identifier,
            target_name=target_name,
            identifier=identifier,
            observe_base_dir=observe_base_dir,
            environ=environ,
            cwd=cwd,
        )

    leftovers = 0
    for name in existing_names:
        base, label = split_session_name(name)
        if label is None or base != base_name or name in (result.stale_name, target_name):
            continue
        with hidden_name(name):
            try:
                metadata = get_session_metadata(name)
            except Exception:  # noqa: BLE001, S112 (a count for a notice; a failed read is not counted)
                continue
        if is_legacy_secret_slot(name, metadata):
            leftovers += 1
    result = replace(result, leftovers=leftovers)

    if result == LegacySlotMigration(source_field, target_name):
        return None
    return result


def _handle_stale(
    result: LegacySlotMigration,
    stale_name: str,
    old_identifier: str,
    *,
    target_name: str,
    identifier: str | None,
    observe_base_dir: Path,
    environ: Mapping[str, str],
    cwd: Path,
) -> LegacySlotMigration:
    with hidden_name(stale_name):
        stored = get_session_metadata(stale_name)
        if (
            stored is None
            or stored.get("account_rule") is not None
            or stored.get("account_identifier") != old_identifier
        ):
            return result
        result = replace(
            result,
            stale_name=stale_name,
            env_pinned=environ.get("GRAFTPUNK_SESSION", "").strip() == stale_name,
        )
        expires_at = parse_datetime_iso(stored.get("expires_at"))
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at is not None and expires_at < datetime.now(UTC):
            return _delete_stale(result, observe_base_dir=observe_base_dir, cwd=cwd)
        if split_session_name(target_name)[1] is not None or get_session_metadata(target_name):
            return replace(result, action="superseded")
        try:
            copy_session(stale_name, target_name, account_identifier=identifier)
        except SessionNotFoundError:
            # Another login moved it between the read above and this one.
            return replace(result, stale_name=None, env_pinned=False)
        except Exception as exc:  # noqa: BLE001 (the stale slot stays; the login goes on)
            LOG.warning(
                "legacy_account_slot_not_copied",
                new_session=target_name,
                field=result.source_field,
                exc_type=type(exc).__name__,
            )
            return replace(
                result,
                action="superseded",
                problem=(
                    f"Could not move this account's earlier session, named from the "
                    f"{result.source_field} field; it is kept until this login succeeds."
                ),
            )
        renamed = _delete_stale(result, observe_base_dir=observe_base_dir, cwd=cwd)
        return replace(renamed, action="renamed") if renamed.action == "deleted" else renamed


def finish_superseded_slot(
    migration: LegacySlotMigration,
    *,
    landed: bool,
    observe_base_dir: Path,
    cwd: Path,
) -> LegacySlotMigration:
    """After a successful login: delete a ``"superseded"`` stale slot.

    Only when the login wrote *new_name* (*landed*); otherwise the stale slot
    is kept and the result says so. Its observe runs and a ``.gp-session`` pin
    follow as for a rename.
    """
    if migration.action != "superseded" or migration.stale_name is None:
        return migration
    if not landed:
        return replace(
            migration,
            action="kept",
            problem=(
                f"Kept this account's earlier session, named from the "
                f"{migration.source_field} field: this login wrote nothing to "
                f"'{migration.new_name}'."
            ),
        )
    with hidden_name(migration.stale_name):
        return _delete_stale(migration, observe_base_dir=observe_base_dir, cwd=cwd)


def _delete_stale(
    migration: LegacySlotMigration, *, observe_base_dir: Path, cwd: Path
) -> LegacySlotMigration:
    """Delete the stale slot, then move what follows its name. Call under :func:`hidden_name`."""
    stale_name = migration.stale_name
    assert stale_name is not None  # noqa: S101 (callers set it)
    try:
        removed = bool(clear_session_cache(stale_name))
    except Exception as exc:  # noqa: BLE001 (reported; the login goes on)
        LOG.warning(
            "legacy_account_slot_not_removed",
            new_session=migration.new_name,
            field=migration.source_field,
            exc_type=type(exc).__name__,
        )
        removed = False
    if not removed and get_session_metadata(stale_name) is not None:
        return replace(
            migration,
            action="kept",
            problem=(
                f"Could not remove this account's earlier session, named from the "
                f"{migration.source_field} field. Find it with: gp session list, "
                "then remove it with: gp session clear <its name>"
            ),
        )
    LOG.info(
        "legacy_account_slot_removed",
        new_session=migration.new_name,
        field=migration.source_field,
    )
    _move_observe_runs(observe_base_dir, stale_name, migration.new_name)
    return replace(
        migration,
        action="deleted",
        pin_updated=_repin_directory(cwd, stale_name, migration.new_name),
    )


def _move_observe_runs(base_dir: Path, old_name: str, new_name: str) -> None:
    """Move *old_name*'s observe runs into *new_name*'s directory.

    A run whose id already exists there stays where it is, and so does the old
    directory while anything is left in it. A run that fails to move is
    logged, and the rest are still tried: the runs are a record, and the login
    must go on.
    """
    old_dir = base_dir / session_dirname(old_name)
    new_dir = base_dir / session_dirname(new_name)
    if old_dir == new_dir or not old_dir.is_dir():
        return
    try:
        make_private_dir(new_dir)
        runs = sorted(old_dir.iterdir())
    except OSError as exc:
        LOG.warning(
            "legacy_observe_runs_not_moved", new_session=new_name, error_type=type(exc).__name__
        )
        return
    for run in runs:
        if (new_dir / run.name).exists():
            continue
        try:
            run.rename(new_dir / run.name)
        except OSError as exc:
            LOG.warning(
                "legacy_observe_run_not_moved", new_session=new_name, error_type=type(exc).__name__
            )
    with contextlib.suppress(OSError):
        if not any(old_dir.iterdir()):
            old_dir.rmdir()


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
