"""Answering a page's JavaScript dialogs in a headless nodriver browser (#320, #353).

A page's ``alert()``, ``confirm()``, ``prompt()`` or ``beforeunload`` dialog stops its
script until someone answers it, and while one is open every DevTools read of the
page waits too. In a headless browser nobody can answer it, so graftpunk does,
through CDP: :func:`answer_dialogs` registers a ``Page.javascriptDialogOpening``
handler on a tab and enables the Page domain, and each dialog is accepted with
``Page.handleJavaScriptDialog``. :func:`answer_dialogs_in_new_tabs` does the same for
each tab or popup the browser opens later, on a CDP connection of its own that
holds each new page before its first script until its answerer is in place. A
visible browser leaves dialogs to the user.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import Any

from graftpunk.logging import get_logger

LOG = get_logger(__name__)

# The most an answer waits for Chrome's reply: nodriver's send has no bound of its
# own, and a target that went away may never reply.
_DIALOG_ANSWER_WAIT_S = 5.0

# The tab attribute that holds its answerer, so a tab gets one handler and one
# answer per dialog however many callers ask for answering.
_TAB_ATTR = "_gp_dialog_answerer"

# The browser attribute that holds its NewPageAnswerer, so a browser gets one
# however many callers ask.
_BROWSER_ATTR = "_gp_dialog_new_tabs"

# The most one command of a NewPageAnswerer's install or resume waits for Chrome's
# reply, so a lost reply cannot hold up the answerer's other work.
_INSTALL_WAIT_S = 5.0

# The largest message the NewPageAnswerer's connection reads (websockets' own
# default is 1 MiB). It hears only target and Page events and command replies.
_MAX_MESSAGE_BYTES = 16 * 1024 * 1024


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
        from graftpunk.observe.capture import _capture_session_kwargs

        kwargs = _capture_session_kwargs(self._tab, self._session_id)
        if await _accept_dialog(event, lambda cmd: self._tab.send(cmd, **kwargs)):
            self.answered += 1
        else:
            self.failed += 1


async def _accept_dialog(event: Any, send: Callable[[Any], Awaitable[Any]]) -> bool:
    """Accept the dialog *event* announces through *send*, log the outcome, and
    return whether Chrome took the answer (see :meth:`DialogAnswerer.on_dialog_opening`)."""
    import nodriver.cdp.page as cdp_page

    kind = str(getattr(getattr(event, "type_", None), "value", "") or "unknown")
    host = urllib.parse.urlsplit(str(getattr(event, "url", "") or "")).hostname
    prompt_text = (getattr(event, "default_prompt", None) or "") if kind == "prompt" else None
    try:
        async with asyncio.timeout(_DIALOG_ANSWER_WAIT_S):
            await send(cdp_page.handle_java_script_dialog(accept=True, prompt_text=prompt_text))
    except Exception as exc:  # noqa: BLE001 - best-effort; never holds up the run
        LOG.debug(
            "nodriver_dialog_answer_failed",
            dialog_type=kind,
            host=host,
            error=str(exc),
            exc_type=type(exc).__name__,
        )
        return False
    LOG.info("nodriver_dialog_answered", dialog_type=kind, choice="accept", host=host)
    return True


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


class NewPageAnswerer:
    """Answers the JavaScript dialogs of the pages a browser gains, on a CDP
    connection of its own to the browser's endpoint (#353).

    ``Target.setAutoAttach`` with ``waitForDebuggerOnStart`` and ``flatten`` attaches
    this connection to each page target Chrome creates from then on, reported as
    waiting for the debugger. The page gets ``Page.enable`` on its session, and
    only then ``Runtime.runIfWaitingForDebugger``, so a dialog its first script
    opens is reported here and accepted. The resume is sent whatever the install
    did, within ``_INSTALL_WAIT_S`` of each command. The auto-attach asks for page
    targets only; any other target a Chrome attaches all the same is resumed at
    once and detached. This connection, not nodriver's, reads the session id
    Chrome puts on each event, which nodriver 0.50.6 drops when it parses one.

    A page that already has a :class:`DialogAnswerer` (its nodriver Tab's) is left
    to it, at attach and again at each dialog. A session's state goes when Chrome
    reports it detached (its target closed or destroyed), and everything goes when
    the connection closes, which a stopped browser does.
    """

    def __init__(self, browser: Any, url: str) -> None:
        self._browser = browser
        self._url = url
        self._socket: Any = None
        self._listener: asyncio.Task[None] | None = None
        self._ids = itertools.count(1)
        self._replies: dict[int, asyncio.Future[dict[str, Any]]] = {}
        # The sessions of the page targets answered here: session id to target id.
        self.pages: dict[str, str] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self.answered = 0
        self.failed = 0

    @property
    def closed(self) -> bool:
        """Whether the connection has ended (or never opened)."""
        return self._listener is None or self._listener.done()

    async def start(self) -> None:
        """Open the connection and turn auto-attach on; raises if either fails."""
        self._socket = await _open_socket(self._url)
        self._listener = asyncio.create_task(self._listen())
        await self._call(
            "Target.setAutoAttach",
            {
                "autoAttach": True,
                "waitForDebuggerOnStart": True,
                "flatten": True,
                # Pages only: a service worker or any other target is never held.
                "filter": [{"type": "page"}],
            },
        )

    async def aclose(self) -> None:
        """Close the connection."""
        if self._listener is not None:
            self._listener.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._listener
        if self._socket is not None:
            with contextlib.suppress(Exception):
                await self._socket.close()
        self._ended()

    def _ended(self) -> None:
        for future in self._replies.values():
            if not future.done():
                future.set_exception(ConnectionError("dialog answerer connection closed"))
        self._replies.clear()
        self.pages.clear()

    async def _listen(self) -> None:
        try:
            async for raw in self._socket:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if "id" in message:
                    future = self._replies.pop(message["id"], None)
                    if future is not None and not future.done():
                        future.set_result(message)
                elif "method" in message:
                    self._on_event(message)
        except Exception as exc:  # noqa: BLE001 - a stopped browser ends it
            LOG.debug(
                "nodriver_dialog_connection_ended", error=str(exc), exc_type=type(exc).__name__
            )
        finally:
            self._ended()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_event(self, message: dict[str, Any]) -> None:
        method = message["method"]
        params = message.get("params") or {}
        session_id = message.get("sessionId")
        if method == "Target.attachedToTarget" and session_id is None:
            self._spawn(self._on_attached(params))
        elif method == "Target.detachedFromTarget" and session_id is None:
            self.pages.pop(str(params.get("sessionId")), None)
        elif method == "Page.javascriptDialogOpening" and session_id in self.pages:
            self._spawn(self._on_dialog(session_id, message))

    async def _request(
        self, method: str, params: dict[str, Any], session_id: str | None = None
    ) -> asyncio.Future[dict[str, Any]]:
        """Send one command and return the future of its reply, without waiting for it."""
        if self.closed:
            raise ConnectionError("dialog answerer connection closed")
        call_id = next(self._ids)
        message: dict[str, Any] = {"id": call_id, "method": method, "params": params}
        if session_id is not None:
            message["sessionId"] = session_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._replies[call_id] = future
        try:
            await self._socket.send(json.dumps(message))
        except BaseException:
            self._replies.pop(call_id, None)
            raise
        return future

    async def _call(
        self, method: str, params: dict[str, Any], session_id: str | None = None
    ) -> dict[str, Any]:
        """Send one command and wait, at most ``_INSTALL_WAIT_S``, for its result."""
        future = await self._request(method, params, session_id)
        async with asyncio.timeout(_INSTALL_WAIT_S):
            reply = await future
        return _result(method, reply)

    async def _on_attached(self, params: dict[str, Any]) -> None:
        session_id = params.get("sessionId")
        info = params.get("targetInfo") or {}
        target_id = info.get("targetId")
        kind = info.get("type")
        if not isinstance(session_id, str):
            return
        answer = kind == "page" and not _answered_elsewhere(self._browser, target_id)
        try:
            try:
                if answer:
                    self.pages[session_id] = str(target_id)
                    await self._call("Page.enable", {}, session_id)
            except Exception as exc:  # noqa: BLE001 - the page goes on, unanswered
                self.pages.pop(session_id, None)
                LOG.warning(
                    "nodriver_dialog_answering_unavailable",
                    error=str(exc),
                    exc_type=type(exc).__name__,
                    hint="A page that opens an alert, confirm or prompt stops until it"
                    " is answered.",
                )
            finally:
                # A held target that nobody resumes never runs, so the resume goes
                # whatever the install did.
                if params.get("waitingForDebugger"):
                    await self._call("Runtime.runIfWaitingForDebugger", {}, session_id)
            if not answer:
                await self._call("Target.detachFromTarget", {"sessionId": session_id})
        except Exception as exc:  # noqa: BLE001 - never stops the browser
            LOG.warning(
                "nodriver_dialog_new_target_resume_failed",
                target_type=kind,
                error=str(exc),
                exc_type=type(exc).__name__,
            )

    async def _on_dialog(self, session_id: str, message: dict[str, Any]) -> None:
        from nodriver.cdp.util import parse_json_event

        if _answered_elsewhere(self._browser, self.pages.get(session_id)):
            return
        try:
            event = parse_json_event({"method": message["method"], "params": message["params"]})
        except Exception as exc:  # noqa: BLE001 - an event this nodriver cannot read
            LOG.debug("nodriver_dialog_event_unreadable", error=str(exc))
            return
        if await _accept_dialog(event, lambda cmd: self._send(cmd, session_id)):
            self.answered += 1
        else:
            self.failed += 1

    async def _send(self, cmd: Any, session_id: str) -> Any:
        """Send a nodriver CDP command generator on *session_id*, as nodriver's own
        send does, and return its parsed result."""
        request = next(cmd)
        method = request["method"]
        future = await self._request(method, request.get("params") or {}, session_id)
        result = _result(method, await future)
        try:
            cmd.send(result)
        except StopIteration as stop:
            return stop.value
        return None


def _result(method: str, reply: dict[str, Any]) -> dict[str, Any]:
    error = reply.get("error")
    if error is not None:
        detail = error.get("message", error) if isinstance(error, dict) else error
        raise RuntimeError(f"{method}: {detail}")
    return reply.get("result") or {}


async def _open_socket(url: str) -> Any:
    """A websocket connection to *url*; the tests replace it with a fake Chrome."""
    import websockets

    return await websockets.connect(url, max_size=_MAX_MESSAGE_BYTES)


async def answer_dialogs_in_new_tabs(browser: Any) -> NewPageAnswerer | None:
    """Answer the JavaScript dialogs of every page target *browser* gains from now
    on: a tab a program opens (``get(url, new_tab=True)``) and a popup a page opens
    (``window.open``, a ``target="_blank"`` link), from its first script on.

    Each new page is held before its first script until its answerer is in place
    (:class:`NewPageAnswerer`). Returns the browser's answerer, the one an earlier
    call made when there is one. Never raises: a browser whose endpoint refuses the
    connection or the auto-attach is one warning, and its first tab is still
    answered.
    """
    existing = getattr(browser, _BROWSER_ATTR, None)
    if existing is not None:
        return existing if isinstance(existing, NewPageAnswerer) else None
    url = getattr(browser, "websocket_url", None)
    if not isinstance(url, str) or not url:
        LOG.warning("nodriver_dialog_answering_new_tabs_unavailable", reason="no_websocket_url")
        return None
    answerer = NewPageAnswerer(browser, url)
    setattr(browser, _BROWSER_ATTR, answerer)
    try:
        await answerer.start()
    except Exception as exc:  # noqa: BLE001 - the first tab is answered anyway
        await answerer.aclose()
        LOG.warning(
            "nodriver_dialog_answering_new_tabs_unavailable",
            error=str(exc),
            exc_type=type(exc).__name__,
            hint="A tab or popup opened later that opens an alert, confirm or prompt"
            " stops until it is answered.",
        )
    return answerer


def new_tab_dialog_counts(browser: Any) -> tuple[int, int]:
    """(answered, failed) by *browser*'s :class:`NewPageAnswerer` so far, or (0, 0)."""
    answerer = getattr(browser, _BROWSER_ATTR, None)
    if not isinstance(answerer, NewPageAnswerer):
        return (0, 0)
    return (answerer.answered, answerer.failed)


def _answered_elsewhere(browser: Any, target_id: Any) -> bool:
    """Whether nodriver's Tab for *target_id* has an answerer of its own."""
    if target_id is None:
        return False
    return any(
        _target_id(tab) == target_id and isinstance(getattr(tab, _TAB_ATTR, None), DialogAnswerer)
        for tab in list(getattr(browser, "targets", None) or [])
    )


def _target_id(tab: Any) -> Any:
    return getattr(getattr(tab, "target", None), "target_id", None)
