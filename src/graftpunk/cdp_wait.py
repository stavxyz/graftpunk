"""Bounded waits on nodriver DevTools commands that never cancel the command.

nodriver 0.48.1's ``Connection`` sets a command's result on its future with no
check that the future is still pending, outside any ``try``, in the task that
reads the connection. Cancelling a command whose answer then arrives (what
``asyncio.timeout`` and ``asyncio.wait_for`` do on expiry) raises
``InvalidStateError`` in that task, which ends, and every later command on the
same tab or browser connection waits forever. nodriver 0.50.6 checks first, but
graftpunk still supports 0.48 below Python 3.14.

So a wait here gives up on a command without cancelling it: the command keeps
running, and its late answer or error is collected and dropped when it comes.
Use these for any bounded wait on a nodriver call whose connection is used again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any, TypeVar

T = TypeVar("T")


async def answer_by(awaitable: Awaitable[T], *, end: float) -> T:
    """Await *awaitable* until the running loop's clock reaches *end*, and raise
    ``TimeoutError`` if it has not answered by then, without cancelling it.

    Even with no time left the call gets the loop pass it was scheduled for,
    ahead of the timeout, so an answer already at hand (a closed tab's error) is
    taken. If the caller is itself cancelled while waiting, the call is left
    running the same way.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(awaitable)
    try:
        done, _pending = await asyncio.wait({task}, timeout=max(0.0, end - loop.time()))
    except BaseException:
        task.add_done_callback(_drop_late_answer)
        raise
    if task in done:
        return task.result()
    task.add_done_callback(_drop_late_answer)
    raise TimeoutError


async def answer_within(awaitable: Awaitable[T], seconds: float) -> T:
    """:func:`answer_by` with an end *seconds* from now."""
    return await answer_by(awaitable, end=asyncio.get_running_loop().time() + seconds)


def _drop_late_answer(task: asyncio.Future[Any]) -> None:
    """Collect an abandoned call's outcome, so asyncio does not log it as never retrieved."""
    if not task.cancelled():
        task.exception()
