"""The header capture a login runs, shared by the generated logins
(``login_engine``) and a hand-written ``login()`` through
``SitePlugin.browser_session()`` or ``browser_session_sync()`` (#401).

A leaf module: ``login_engine`` imports ``cli_plugin`` at module level, so
``cli_plugin`` could reach code kept in ``login_engine`` only through an import
back into it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from graftpunk import console as gp_console
from graftpunk.logging import get_logger

if TYPE_CHECKING:
    from graftpunk.plugins.cli_plugin import SitePlugin

LOG = get_logger(__name__)


def header_only_capture(
    backend_type: str,
    driver: Any,
    get_tab: Any,
    *,
    headless: bool = False,
    record_console: bool = True,
) -> Any:
    """A capture used only for header roles: its HAR is never written, so it keeps,
    fetches and logs about no request or response body (#308). A headless one answers JavaScript
    dialogs, as a recorded one does (#320). *record_console* False keeps it to
    what the roles need: on nodriver the tab gets ``Network.enable`` and no
    ``Runtime.enable``, and on Selenium Chrome's ``browser`` log is not read."""
    from graftpunk.observe.capture import create_capture_backend

    return create_capture_backend(
        backend_type,
        driver,
        get_tab=get_tab,
        record_request_bodies=False,
        answer_dialogs=headless,
        record_console=record_console,
    )


def start_login_capture(
    plugin: SitePlugin,
    backend_type: str,
    driver: Any,
    observe_mode: str,
    get_tab: Any = None,
    session_name: str | None = None,
    headless: bool = False,
) -> tuple[Any, Any]:
    """Create the capture backend for a login run.

    Returns ``(capture, storage)``. With ``observe_mode == "off"`` the capture
    is the lightweight header-only one used for role extraction and storage is
    None. Otherwise it is a full capture (bodies streamed to the run dir) and
    storage is an ``ObserveStorage`` under the OPERATING session name --
    ``session_name`` when the CLI computed one (``base@label``), else the
    plugin's base name -- so ``gp --observe=full <plugin> login`` files the run
    where the reader looks for that account (#151). Either capture of a
    *headless* login accepts each JavaScript dialog the pages open, as a
    headless ``gp observe go`` does (#320), through the tab's one answerer, which
    the headless browser installed before the login page loaded.
    """
    from graftpunk.observe.capture import create_capture_backend

    if observe_mode == "off":
        return header_only_capture(backend_type, driver, get_tab, headless=headless), None

    # Why the login engine owns this for nodriver instead of handing
    # observe_mode to BrowserSession like the selenium path: the nodriver
    # capture needs a get_tab callable for the tab that the login opens, and
    # its eager body fetch only runs from start_capture_async(), neither of
    # which BrowserSession's sync _start_observe can provide.
    from graftpunk.observe import OBSERVE_BASE_DIR
    from graftpunk.observe.run import make_run_id
    from graftpunk.observe.storage import ObserveStorage, session_dirname

    # Opt-in diagnostics must never break the login: an unsafe session name or
    # an unwritable base dir degrades to the header-only capture with a warning.
    session_slug = session_dirname(session_name or plugin.session_name)
    try:
        storage = ObserveStorage(OBSERVE_BASE_DIR, session_slug, make_run_id())
    except (ValueError, OSError) as exc:
        LOG.warning(
            "login_observe_unavailable",
            plugin=plugin.site_name,
            session_name=session_slug,
            error=str(exc),
            exc_type=type(exc).__name__,
        )
        gp_console.warn(f"Observability capture unavailable for this login: {exc}")
        return header_only_capture(backend_type, driver, get_tab, headless=headless), None
    capture = create_capture_backend(
        backend_type,
        driver,
        get_tab=get_tab,
        bodies_dir=storage.run_dir / "bodies",
        answer_dialogs=headless,
    )
    LOG.info(
        "login_observe_capture_started",
        plugin=plugin.site_name,
        mode=observe_mode,
        run_dir=str(storage.run_dir),
    )
    return capture, storage


def store_header_roles(session: Any, header_capture: Any, base_url: str) -> None:
    """Put the login's header roles on *session*, with the origin each came from.

    Each role is taken from a request to *base_url*'s origin where the login
    made one (``graftpunk.observe.headers.extract_header_roles``), and
    ``GraftpunkSession`` sends a captured role as captured only to the origin
    it came from (#228). A capture that found no role logs
    ``login_header_roles_empty`` with the number of requests it held; the
    login goes on.
    """
    from graftpunk.tokens import _HEADER_ROLE_ORIGINS_ATTR, _HEADER_ROLES_ATTR

    roles = header_capture.get_header_roles(site_url=base_url)
    setattr(session, _HEADER_ROLES_ATTR, roles)
    origins = header_capture.get_header_role_origins(site_url=base_url)
    setattr(session, _HEADER_ROLE_ORIGINS_ATTR, dict(origins) if isinstance(origins, dict) else {})
    if not roles:
        # Without roles the session's commands send python-requests' User-Agent,
        # and nothing else would say why.
        LOG.warning(
            "login_header_roles_empty",
            base_url=base_url,
            requests=header_capture.request_count(),
            hint="No captured request could be classified as navigation, xhr or form; "
            "commands send the registered role headers and the HTTP client's User-Agent",
        )


def plugin_set_header_roles(session: Any) -> bool:
    """Whether a hand-written ``login()`` put header roles on *session* itself,
    inside its ``browser_session()`` block. Those are kept: a plugin that set
    them did so on purpose."""
    from graftpunk.tokens import _HEADER_ROLES_ATTR

    roles = vars(session).get(_HEADER_ROLES_ATTR)
    return isinstance(roles, dict) and bool(roles)
