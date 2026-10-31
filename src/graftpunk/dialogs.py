"""Answering a page's JavaScript dialogs in a headless nodriver browser (#320).

A page's ``alert()``, ``confirm()``, ``prompt()`` or ``beforeunload`` dialog stops its
script until someone answers it, and while one is open every DevTools read of the
page waits too. In a headless browser nobody can answer it, so graftpunk does,
through CDP: :func:`answer_dialogs` registers a ``Page.javascriptDialogOpening``
handler on a tab and enables the Page domain, and each dialog is accepted with
``Page.handleJavaScriptDialog``. :func:`answer_dialogs_in_new_tabs` does the same for
each tab or popup the browser opens later. A visible browser leaves dialogs to the
user.
"""

from __future__ import annotations

import asyncio
import urllib.parse
from typing import Any

from graftpunk.logging import get_logger

LOG = get_logger(__name__)

# The most an answer waits for Chrome's reply: nodriver's send has no bound of its
# own, and a target that went away may never reply.
_DIALOG_ANSWER_WAIT_S = 5.0

# The tab attribute that holds its answerer, so a tab gets one handler and one
# answer per dialog however many callers ask for answering.
_TAB_ATTR = "_gp_dialog_answerer"

# The browser attribute that marks its new tabs as answered, so the handler is
# registered once however many callers ask.
_BROWSER_ATTR = "_gp_dialog_new_tabs"


class DialogAnswerer:
    """Accepts each JavaScript dialog one tab's pages open, and counts them."""

    def __init__(self, tab: Any) -> None:
        self._tab = tab
        # The CDP session the Page domain was enabled on: Chrome reports the dialog
        # there, and nodriver 0.50.6's Browser.get(url) opens another session on the
        # tab afterwards (see graftpunk.observe.capture._capture_session_kwargs).
        self._session_id: str | None = None
        self.answered = 0
        self.failed = 0

    async def on_dialog_opening(self, event: Any) -> None:
        """Accept the dialog *event* announces: an alert, a confirm, a prompt with its
        default text, and a beforeunload (refusing would keep the browser on the
        page the page or the user asked to leave). One line logs the dialog's type,
        the choice and the page's host, never the message, the default text, or the
        URL's path or query, any of which can hold account data. An answer that
        fails (the dialog closed already, the target went away) or gets no reply
        within ``_DIALOG_ANSWER_WAIT_S`` is a debug line and nothing else."""
        import nodriver.cdp.page as cdp_page

        from graftpunk.observe.capture import _capture_session_kwargs

        kind = str(getattr(getattr(event, "type_", None), "value", "") or "unknown")
        host = urllib.parse.urlsplit(str(getattr(event, "url", "") or "")).hostname
        prompt_text = (getattr(event, "default_prompt", None) or "") if kind == "prompt" else None
        try:
            async with asyncio.timeout(_DIALOG_ANSWER_WAIT_S):
                await self._tab.send(
                    cdp_page.handle_java_script_dialog(accept=True, prompt_text=prompt_text),
                    **_capture_session_kwargs(self._tab, self._session_id),
                )
        except Exception as exc:  # noqa: BLE001 - best-effort; never holds up the run
            self.failed += 1
            LOG.debug(
                "nodriver_dialog_answer_failed",
                dialog_type=kind,
                host=host,
                error=str(exc),
                exc_type=type(exc).__name__,
            )
            return
        self.answered += 1
        LOG.info("nodriver_dialog_answered", dialog_type=kind, choice="accept", host=host)


async def answer_dialogs(tab: Any) -> DialogAnswerer | None:
    """Answer each JavaScript dialog *tab*'s pages open, from now until the tab goes.

    Returns the tab's answerer, the one an earlier call installed when there is
    one. Never raises: a nodriver without the event class, or a target that
    refuses ``Page.enable``, is one warning, and the caller goes on without it
    (None in the first case; the answerer, which then hears no dialog, in the
    second).
    """
    existing = getattr(tab, _TAB_ATTR, None)
    if isinstance(existing, DialogAnswerer):
        return existing
    try:
        import nodriver.cdp.page as cdp_page
    except ImportError:
        LOG.warning("nodriver_dialog_answering_unavailable", reason="no_nodriver")
        return None
    event_cls = getattr(cdp_page, "JavascriptDialogOpening", None)
    if event_cls is None:
        LOG.warning("nodriver_dialog_answering_unavailable", reason="no_event_class")
        return None
    from graftpunk.observe.capture import _capture_session_kwargs

    answerer = DialogAnswerer(tab)
    try:
        tab.add_handler(event_cls, answerer.on_dialog_opening)
        setattr(tab, _TAB_ATTR, answerer)
        if getattr(tab, "session_id", "") is None and callable(getattr(tab, "attach", None)):
            # A tab nodriver 0.50.6 has not attached yet (a popup): its first send
            # would attach it, and a Browser.get(new_tab=True) running beside this
            # attaches it again, so the session Page is enabled on is fixed first.
            await tab.attach()
        session_id = getattr(tab, "session_id", None)
        session_id = session_id if isinstance(session_id, str) else None
        await tab.send(cdp_page.enable(), **_capture_session_kwargs(tab, session_id))
        answerer._session_id = session_id
    except Exception as exc:  # noqa: BLE001 - the browser goes on without it
        LOG.warning(
            "nodriver_dialog_answering_unavailable",
            error=str(exc),
            exc_type=type(exc).__name__,
            hint="A page that opens an alert, confirm or prompt stops until it is answered.",
        )
    return answerer


async def answer_dialogs_in_new_tabs(browser: Any) -> None:
    """Answer the JavaScript dialogs of every page target *browser* gains from now
    on: a tab a program opens (``get(url, new_tab=True)``) and a popup a page opens
    (``window.open``, a ``target="_blank"`` link).

    Chrome reports each new target (``Target.targetCreated``, with target discovery
    turned on), and the target gets its own answerer through :func:`answer_dialogs`
    once, a moment after it is created. A dialog its page opens before that answerer
    has enabled the Page domain stays open: measured on 2026-10-31 (nodriver 0.50.6,
    headless, a local page), a popup that called ``alert()`` as it loaded never
    finished ``Page.enable``, nothing answered it, and every DevTools call to the
    popup and to the page that opened it waited until the browser stopped; a popup
    whose alert came 1.5 s after it opened was answered and went on. Registered once
    per browser. Never raises: a browser that cannot take the handler is one
    warning, and its first tab is still answered.
    """
    if getattr(browser, _BROWSER_ATTR, None) is not None:
        return
    try:
        import nodriver.cdp.target as cdp_target
    except ImportError:
        LOG.warning("nodriver_dialog_answering_new_tabs_unavailable", reason="no_nodriver")
        return
    # nodriver 0.48 reads the browser's own events on browser.connection; on 0.50
    # the Browser is that connection itself, and its connection attribute is None.
    connection = getattr(browser, "connection", None) or browser
    # The targets that have an answerer, so a target Chrome reports again (target
    # discovery reports every existing target when it is turned on) gets no second.
    answered = {
        _target_id(tab)
        for tab in list(getattr(browser, "targets", None) or [])
        if isinstance(getattr(tab, _TAB_ATTR, None), DialogAnswerer)
    }

    async def on_target_created(event: Any) -> None:
        info = getattr(event, "target_info", None)
        target_id = getattr(info, "target_id", None)
        if getattr(info, "type_", None) != "page" or target_id in answered:
            return
        answered.add(target_id)
        try:
            if connection is browser:
                tab = _own_tab(browser, info)
            else:
                # nodriver 0.48's own handler made the target's Tab, one websocket
                # per target with no session to lose.
                tab = _tab_for(browser, target_id)
                if tab is None:
                    await browser.update_targets()
                    tab = _tab_for(browser, target_id)
            if tab is None:
                LOG.debug("nodriver_dialog_new_tab_not_found")
                return
            await answer_dialogs(tab)
        except Exception as exc:  # noqa: BLE001 - never stops the browser
            LOG.debug("nodriver_dialog_new_tab_failed", error=str(exc), exc_type=type(exc).__name__)

    try:
        connection.add_handler(cdp_target.TargetCreated, on_target_created)
        setattr(browser, _BROWSER_ATTR, answered)
        await connection.send(cdp_target.set_discover_targets(discover=True))
    except Exception as exc:  # noqa: BLE001 - the first tab is answered anyway
        LOG.warning(
            "nodriver_dialog_answering_new_tabs_unavailable",
            error=str(exc),
            exc_type=type(exc).__name__,
            hint="A tab or popup opened later that opens an alert, confirm or prompt"
            " stops until it is answered.",
        )


def _own_tab(browser: Any, info: Any) -> Any:
    """A nodriver 0.50 Tab of the answerer's own on the target *info* names.

    Not the Tab in ``browser.targets``: a program's ``get(url, new_tab=True)``
    attaches that one while the answerer would, and on nodriver 0.50.6 two attaches
    at once on one Tab each open a websocket and the second loses the first's
    session (measured on 2026-10-31: ``Target.getTargetInfo`` failed with "Session
    with given id not found", and the tab's dialog was never answered). Its own Tab
    has a websocket and session of its own; nothing closes that websocket before
    the browser stops.
    """
    from nodriver.core.tab import Tab

    return Tab(target=info, parent=browser)


def _target_id(tab: Any) -> Any:
    return getattr(getattr(tab, "target", None), "target_id", None)


def _tab_for(browser: Any, target_id: Any) -> Any:
    """The browser's Tab object for *target_id*, or None."""
    for tab in list(getattr(browser, "targets", None) or []):
        if _target_id(tab) == target_id:
            return tab
    return None
