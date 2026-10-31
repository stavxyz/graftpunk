"""Answering a page's JavaScript dialogs in a headless nodriver browser (#320).

A page's ``alert()``, ``confirm()``, ``prompt()`` or ``beforeunload`` dialog stops its
script until someone answers it, and while one is open every DevTools read of the
page waits too. In a headless browser nobody can answer it, so graftpunk does,
through CDP: :func:`answer_dialogs` registers a ``Page.javascriptDialogOpening``
handler on a tab and enables the Page domain, and each dialog is accepted with
``Page.handleJavaScriptDialog``. A visible browser leaves dialogs to the user.
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
    answerer = DialogAnswerer(tab)
    try:
        tab.add_handler(event_cls, answerer.on_dialog_opening)
        setattr(tab, _TAB_ATTR, answerer)
        await tab.send(cdp_page.enable())
        session_id = getattr(tab, "session_id", None)
        answerer._session_id = session_id if isinstance(session_id, str) else None
    except Exception as exc:  # noqa: BLE001 - the browser goes on without it
        LOG.warning(
            "nodriver_dialog_answering_unavailable",
            error=str(exc),
            exc_type=type(exc).__name__,
            hint="A page that opens an alert, confirm or prompt stops until it is answered.",
        )
    return answerer
