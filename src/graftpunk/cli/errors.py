"""CLI error-boundary helpers: enrich, render, exit."""

from typing import NoReturn

from graftpunk import console as gp_console
from graftpunk.cache import get_session_metadata
from graftpunk.exceptions import AmbiguousSessionError


def exit_ambiguous_session(exc: AmbiguousSessionError) -> NoReturn:
    """Enrich the candidates with account identifiers, render, exit(1).

    Identifiers are fetched lazily — for the candidates only, best-effort
    (a candidate with no metadata degrades to its bare name) — so the
    console renderer stays a pure formatter.
    """
    candidates: list[tuple[str, str | None]] = []
    for name in exc.candidates:
        identifier: str | None = None
        try:
            meta = get_session_metadata(name)
            identifier = meta.get("account_identifier") if meta else None
        except Exception:  # noqa: BLE001 — display is best-effort
            identifier = None
        candidates.append((name, identifier))
    gp_console.render_ambiguous_session(exc.base_name, candidates)
    raise SystemExit(1)
