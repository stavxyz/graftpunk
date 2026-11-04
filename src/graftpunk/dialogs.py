"""Answering a page's JavaScript dialogs in a headless nodriver browser (#320, #353).

A page's ``alert()``, ``confirm()``, ``prompt()`` or ``beforeunload`` dialog stops its
script until someone answers it, and while one is open every DevTools read of the
page waits too. In a headless browser nobody can answer it, so graftpunk does,
through CDP: :func:`answer_dialogs` registers a ``Page.javascriptDialogOpening``
handler on a tab and enables the Page domain, and each dialog is accepted with
``Page.handleJavaScriptDialog``. :func:`answer_dialogs_in_new_tabs` does the same for
each tab or popup the browser opens later, on a CDP connection of its own. Chrome
holds a popup (and any other page the browser opens itself) before its first
script until that answerer is in place, so a dialog the popup's first script opens
is answered. A tab a program opens with ``get(url, new_tab=True)`` is not held: its
first-script dialog is answered only when the answerer's install reaches Chrome
before that script runs, a race (#363). A visible browser leaves dialogs to the
user.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import urllib.parse
from collections.abc import Awaitable, Callable, Sequence
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
        # Whether Page.enable went through: until then Chrome may report the tab's
        # dialogs to no session of this answerer, so a page is left to it only once
        # this is True (see _answered_elsewhere).
        self.ready = False
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
        answerer.ready = True
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
    this connection to each page target Chrome creates from then on. A popup (and
    any other page the browser opens itself) is reported waiting for the debugger
    and held before its first script: it gets ``Page.enable`` on its session, and
    only then ``Runtime.runIfWaitingForDebugger``, so a dialog its first script
    opens is reported here and accepted. A tab made with ``Target.createTarget``
    (``get(url, new_tab=True)``) is not held, so its first-script dialog is answered
    only when ``Page.enable`` reaches Chrome before that script runs (#363). The
    resume is sent whatever the install did, within ``_INSTALL_WAIT_S`` of each
    command. The auto-attach asks for page targets only; any other target a Chrome
    attaches all the same is resumed at once and detached. This connection, not
    nodriver's, reads the session id Chrome puts on each event, which nodriver
    0.50.6 drops when it parses one.

    A page whose nodriver Tab has a :class:`DialogAnswerer` that has enabled the
    Page domain is left to it, at attach and again at each dialog. A session's
    state goes when Chrome reports it detached (its target closed or destroyed),
    and everything goes when the connection ends. The connection sends no
    keepalive pings, as nodriver's own does not, so it ends only when
    :meth:`aclose` closes it, its listener is cancelled, or Chrome closes it. An
    end :meth:`aclose` did not ask for is a ``nodriver_dialog_answerer_lost``
    warning, and the next :func:`answer_dialogs_in_new_tabs` call for the browser
    replaces the answerer. A popup held when a connection ends stays paused until
    then, since its resume cannot be sent. The auto-attach of the replacement
    attaches every existing page too, reporting such a popup not waiting, so every
    page attached gets the resume, held or not, after its install: measured on
    2026-11-03, that released a popup a lost client had left held, its
    first-script ``alert()`` was answered, and a resume on pages that were not held
    changed nothing (docs/HOW_IT_WORKS.md).
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
        # Every session attached and not yet reported detached, answered here or not.
        self._attached: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        # Set once the end of the connection is expected (aclose, a browser stop),
        # so that end is not reported as lost.
        self._closing = False
        self._ended_once = False
        self.answered = 0
        self.failed = 0

    @property
    def closed(self) -> bool:
        """Whether the connection has ended (or never opened)."""
        return self._listener is None or self._listener.done()

    @property
    def ended(self) -> bool:
        """Whether this answerer is finished: its connection ended or its start failed."""
        return self._ended_once

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

    def expect_end(self) -> None:
        """Note that the connection is about to end with its browser, so the end is
        not reported as lost. Safe from any thread."""
        self._closing = True

    async def aclose(self) -> None:
        """Close the connection, on the loop it was opened on."""
        self._closing = True
        if self._listener is not None and not self._listener.done():
            # The listener closes the socket as it ends.
            self._listener.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._listener
        await self._close_socket()
        self._ended()

    async def _close_socket(self) -> None:
        if self._socket is None:
            return
        with contextlib.suppress(Exception):
            async with asyncio.timeout(_INSTALL_WAIT_S):
                await self._socket.close()

    def _ended(self) -> None:
        self._ended_once = True
        for future in self._replies.values():
            if not future.done():
                future.set_exception(ConnectionError("dialog answerer connection closed"))
        self._replies.clear()
        self.pages.clear()
        self._attached.clear()

    async def _listen(self) -> None:
        reason = "closed_by_chrome"
        error: BaseException | None = None
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
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - reported below; the browser goes on
            reason, error = "error", exc
        finally:
            self._ended()
            if not self._closing:
                LOG.warning(
                    "nodriver_dialog_answerer_lost",
                    reason=reason,
                    error=None if error is None else str(error),
                    exc_type=None if error is None else type(error).__name__,
                    hint="A tab or popup opened from now on that opens an alert, confirm or"
                    " prompt stops until it is answered, until the answerer is started again.",
                )
            # Auto-attach is in force while the socket is open: a page Chrome held
            # for this connection after its listener ended would never be resumed.
            await self._close_socket()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_event(self, message: dict[str, Any]) -> None:
        method = message["method"]
        params = message.get("params") or {}
        session_id = message.get("sessionId")
        if method == "Target.attachedToTarget" and session_id is None:
            attached = params.get("sessionId")
            if isinstance(attached, str):
                self._attached.add(attached)
            self._spawn(self._on_attached(params))
        elif method == "Target.detachedFromTarget" and session_id is None:
            detached = str(params.get("sessionId"))
            self._attached.discard(detached)
            self.pages.pop(detached, None)
        elif method == "Page.javascriptDialogOpening" and session_id in self.pages:
            self._spawn(self._on_dialog(session_id, message))

    async def _request(
        self, method: str, params: dict[str, Any], session_id: str | None = None
    ) -> tuple[int, asyncio.Future[dict[str, Any]]]:
        """Send one command and return its id and the future of its reply, without
        waiting for it."""
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
        return call_id, future

    async def _reply(
        self, method: str, params: dict[str, Any], session_id: str | None = None
    ) -> dict[str, Any]:
        """Send one command and wait for its result, with no bound of its own. The
        reply's slot goes however the wait ends (a timeout around it included)."""
        call_id, future = await self._request(method, params, session_id)
        try:
            reply = await future
        finally:
            self._replies.pop(call_id, None)
        return _result(method, reply)

    async def _call(
        self, method: str, params: dict[str, Any], session_id: str | None = None
    ) -> dict[str, Any]:
        """Send one command and wait, at most ``_INSTALL_WAIT_S``, for its result."""
        async with asyncio.timeout(_INSTALL_WAIT_S):
            return await self._reply(method, params, session_id)

    def _gone(self, session_id: str, errors: Sequence[BaseException]) -> bool:
        """Whether a failed command failed because the page closed: Chrome reported
        the session detached, or refused the command for a session or target it no
        longer has. A connection that ended is not a page that closed."""
        if self.closed:
            return False
        if session_id not in self._attached:
            return True
        return bool(errors) and all("not found" in str(error).lower() for error in errors)

    async def _on_attached(self, params: dict[str, Any]) -> None:
        session_id = params.get("sessionId")
        info = params.get("targetInfo") or {}
        target_id = info.get("targetId")
        kind = info.get("type")
        if not isinstance(session_id, str):
            return
        answer = kind == "page" and not _answered_elsewhere(self._browser, target_id)
        waiting = bool(params.get("waitingForDebugger"))
        install_error: Exception | None = None
        resume_error: Exception | None = None
        try:
            if answer:
                self.pages[session_id] = str(target_id)
                try:
                    await self._call("Page.enable", {}, session_id)
                except Exception as exc:  # noqa: BLE001 - the page goes on, unanswered
                    self.pages.pop(session_id, None)
                    install_error = exc
        finally:
            # A held target that nobody resumes never runs, so the resume goes
            # whatever the install did. Every page gets it, held or not: a page a
            # lost connection left held is reported not waiting when a new
            # connection's auto-attach attaches it, and only a resume releases it.
            if waiting or kind == "page":
                try:
                    await self._call("Runtime.runIfWaitingForDebugger", {}, session_id)
                except Exception as exc:  # noqa: BLE001 - reported below
                    resume_error = exc
        errors = [e for e in (install_error, resume_error) if e is not None]
        if errors and self._gone(session_id, errors):
            LOG.debug("nodriver_dialog_new_target_gone", target_type=kind)
            return
        if install_error is not None:
            LOG.warning(
                "nodriver_dialog_answering_unavailable",
                error=str(install_error),
                exc_type=type(install_error).__name__,
                hint="A page that opens an alert, confirm or prompt stops until it is answered.",
            )
        if resume_error is not None and not waiting:
            # Most pages attached not waiting were never held.
            LOG.debug(
                "nodriver_dialog_page_resume_failed",
                error=str(resume_error),
                exc_type=type(resume_error).__name__,
            )
        elif resume_error is not None:
            LOG.warning(
                "nodriver_dialog_new_target_resume_failed",
                target_type=kind,
                error=str(resume_error),
                exc_type=type(resume_error).__name__,
            )
        if answer:
            return
        try:
            await self._call("Target.detachFromTarget", {"sessionId": session_id})
        except Exception as exc:  # noqa: BLE001 - never stops the browser
            if not self._gone(session_id, [exc]):
                LOG.warning(
                    "nodriver_dialog_new_target_detach_failed",
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
        result = await self._reply(request["method"], request.get("params") or {}, session_id)
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
    """A websocket connection to *url*; the tests replace it with a fake Chrome.

    No keepalive pings: websockets' default (a ping every 20 s, the connection
    closed when a pong is 20 s late) would end the answerer whenever the browser's
    loop is busy for that long, and nodriver's own connection waits 900 s.
    """
    import websockets

    return await websockets.connect(
        url, max_size=_MAX_MESSAGE_BYTES, ping_interval=None, ping_timeout=None
    )


async def answer_dialogs_in_new_tabs(browser: Any) -> NewPageAnswerer | None:
    """Answer the JavaScript dialogs of every page target *browser* gains from now
    on: a popup a page opens (``window.open``, a ``target="_blank"`` link) from its
    first script on, since Chrome holds it until its answerer is in place, and a tab
    a program opens (``get(url, new_tab=True)``), which Chrome does not hold, so a
    dialog its first script opens is answered only if the install wins that race
    (#363). See :class:`NewPageAnswerer`.

    Returns the browser's answerer: the one an earlier call made while it is still
    connected, or a new one, which carries the earlier one's counts, when that one
    has ended. Never raises (a cancellation excepted, which leaves nothing running):
    a browser whose endpoint refuses the connection or the auto-attach is one
    warning, and its first tab is still answered.
    """
    existing = getattr(browser, _BROWSER_ATTR, None)
    if existing is not None and not isinstance(existing, NewPageAnswerer):
        return None
    if existing is not None and not existing.ended:
        return existing
    url = getattr(browser, "websocket_url", None)
    if not isinstance(url, str) or not url:
        LOG.warning("nodriver_dialog_answering_new_tabs_unavailable", reason="no_websocket_url")
        return None
    answerer = NewPageAnswerer(browser, url)
    if existing is not None:
        answerer.answered, answerer.failed = existing.answered, existing.failed
    setattr(browser, _BROWSER_ATTR, answerer)
    try:
        await answerer.start()
    except BaseException as exc:
        # Shielded: a second cancellation must not leave the listener or the
        # socket, with auto-attach on, behind.
        await asyncio.shield(answerer.aclose())
        if not isinstance(exc, Exception):
            raise
        LOG.warning(
            "nodriver_dialog_answering_new_tabs_unavailable",
            error=str(exc),
            exc_type=type(exc).__name__,
            hint="A tab or popup opened later that opens an alert, confirm or prompt"
            " stops until it is answered.",
        )
    return answerer


async def close_dialogs_in_new_tabs(browser: Any) -> None:
    """Close *browser*'s :class:`NewPageAnswerer`, if it has one, before the browser
    stops. Never raises."""
    answerer = getattr(browser, _BROWSER_ATTR, None)
    if isinstance(answerer, NewPageAnswerer):
        with contextlib.suppress(Exception):
            await answerer.aclose()


def expect_new_tabs_end(browser: Any) -> None:
    """Note that *browser*'s :class:`NewPageAnswerer` connection is about to end
    because the browser is being ended without its loop. Safe from any thread."""
    answerer = getattr(browser, _BROWSER_ATTR, None)
    if isinstance(answerer, NewPageAnswerer):
        answerer.expect_end()


def new_tab_dialog_counts(browser: Any) -> tuple[int, int]:
    """(answered, failed) by *browser*'s :class:`NewPageAnswerer` so far, or (0, 0)."""
    answerer = getattr(browser, _BROWSER_ATTR, None)
    if not isinstance(answerer, NewPageAnswerer):
        return (0, 0)
    return (answerer.answered, answerer.failed)


def _answered_elsewhere(browser: Any, target_id: Any) -> bool:
    """Whether nodriver's Tab for *target_id* has an answerer of its own that has
    enabled the Page domain."""
    if target_id is None:
        return False
    for tab in list(getattr(browser, "targets", None) or []):
        answerer = getattr(tab, _TAB_ATTR, None)
        if _target_id(tab) == target_id and isinstance(answerer, DialogAnswerer) and answerer.ready:
            return True
    return False


def _target_id(tab: Any) -> Any:
    return getattr(getattr(tab, "target", None), "target_id", None)
