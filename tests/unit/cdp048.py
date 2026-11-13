"""A stand-in for nodriver 0.48.1's DevTools ``Connection``, for tests of bounded
waits on DevTools commands (``graftpunk.cdp_wait``).

What it keeps of the real one is the part that decides what an abandoned command
does: ``send`` registers a future and awaits it, and the listener task sets each
answer's result with no check that the future is still pending, so an answer to
a cancelled command raises ``InvalidStateError`` there and ends the listener
(``nodriver/core/connection.py``, 0.48.1). After that, no command on the
connection is ever answered.
"""

from __future__ import annotations

import asyncio
from typing import Any


class Connection048:
    """``send(answer, after=...)`` answers *answer* after *after* seconds, or raises
    it when it is an exception."""

    def __init__(self) -> None:
        self._futures: dict[int, asyncio.Future[Any]] = {}
        self._answers: asyncio.Queue[tuple[int, Any]] = asyncio.Queue()
        self._ids = 0
        self.listener = asyncio.ensure_future(self._listen())

    async def send(self, answer: Any, *, after: float = 0.0) -> Any:
        loop = asyncio.get_running_loop()
        self._ids += 1
        future: asyncio.Future[Any] = loop.create_future()
        self._futures[self._ids] = future
        loop.call_later(after, self._answers.put_nowait, (self._ids, answer))
        return await future

    async def _listen(self) -> None:
        while True:
            command_id, answer = await self._answers.get()
            future = self._futures.pop(command_id)
            if isinstance(answer, BaseException):
                future.set_exception(answer)
            else:
                future.set_result(answer)

    async def still_answers(self) -> bool:
        """Whether the listener is alive and a fresh command gets its answer."""
        if self.listener.done():
            return False
        try:
            return await asyncio.wait_for(self.send("pong"), timeout=1.0) == "pong"
        except TimeoutError:
            return False

    def close(self) -> None:
        self.listener.cancel()
