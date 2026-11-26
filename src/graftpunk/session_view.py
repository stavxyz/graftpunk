"""A saved session as a plain object, for callers that need no browser.

:class:`StoredSession` holds a stored session's state dict and answers the
attribute reads and writes the API loaders make on a session, without the
session classes and so without selenium, requestium, nodriver or httpie.
"""

from __future__ import annotations

import copy
from typing import Any, NoReturn

from requests.cookies import RequestsCookieJar
from requests.structures import CaseInsensitiveDict

from graftpunk.cookies import _set_cookie_as_is
from graftpunk.session_record import FIELDS, Record, record_to_state
from graftpunk.session_riders import SESSION_RIDER_ATTRS, SESSION_RIDER_DEFAULTS

_RIDERS = frozenset(SESSION_RIDER_ATTRS)


class StoredSession:
    """A stored session's state, readable and settable like a session object.

    *kind* is the stored kind (``browser``, ``api`` or ``requests``), or None
    for a legacy pickle of a subclass or another type: such a view can be read
    and never written (:meth:`stored_state` refuses it). A value set on an
    attribute the table stores for the kind goes into the state; any other
    value is kept in memory for this view only and is absent from
    :meth:`stored_state`.

    The view takes ownership of the *state* and *extra* dicts it is given: it
    keeps them without copying, writes to *state* when a stored attribute is
    set, and the caller must not use them afterwards.

    A stored key missing from the state reads as a default made on first read.
    :meth:`stored_state` leaves it out while it is empty, so a key nobody
    touched is not invented, and includes it once it has been edited in place.
    """

    kind: str | None
    extra: dict[str, Any]

    def __init__(
        self, kind: str | None, state: dict[str, Any], extra: dict[str, Any] | None = None
    ) -> None:
        self.kind = kind
        self.extra = extra if extra is not None else {}
        self._state = state
        self._memory: dict[str, Any] = {}
        # Keys in _memory that are defaults made on read, not values set.
        self._made: set[str] = set()

    @classmethod
    def from_record(cls, record: Record) -> StoredSession:
        """The view of a validated record, carrying its unknown top-level keys."""
        return cls(record.kind, record_to_state(record), copy.deepcopy(record.extra))

    def _stored_keys(self) -> frozenset[str]:
        return frozenset(row.state_key for row in FIELDS if self.kind in row.kinds)

    def _read(self, key: str, default: Any) -> Any:
        if key in self._memory:
            return self._memory[key]
        if key in self._state:
            return self._state[key]
        # An absent value is made once and kept, so an in-place change to it
        # (a rider dict) is seen by the next read.
        self._memory[key] = default()
        self._made.add(key)
        return self._memory[key]

    def _write(self, key: str, value: Any) -> None:
        self._made.discard(key)
        if key in self._stored_keys():
            self._state[key] = value
            self._memory.pop(key, None)
        else:
            self._memory[key] = value

    def stored_state(self) -> dict[str, Any]:
        """The keys of the state the table lists for the kind, no defaults added."""
        if self.kind is None:
            raise TypeError("a session with no stored kind cannot be written")
        keys = self._stored_keys()
        stored = {key: value for key, value in self._state.items() if key in keys}
        for key in self._made & keys:
            # A default made on read is stored only once it holds something.
            if self._memory[key]:
                stored[key] = self._memory[key]
        return stored

    @property
    def cookies(self) -> RequestsCookieJar:
        jar = self._read("cookies", RequestsCookieJar)
        if not isinstance(jar, RequestsCookieJar):
            # A legacy pickle may hold another jar type; the view reads the same cookies.
            rebuilt = RequestsCookieJar()
            for cookie in jar:
                _set_cookie_as_is(rebuilt, cookie)
            self._write("cookies", rebuilt)
            return rebuilt
        return jar

    @cookies.setter
    def cookies(self, value: Any) -> None:
        self._write("cookies", value)

    @property
    def headers(self) -> Any:
        return self._read("headers", CaseInsensitiveDict)

    @headers.setter
    def headers(self, value: Any) -> None:
        self._write("headers", value)

    @property
    def session_name(self) -> str:
        return self._read("session_name", str)

    @session_name.setter
    def session_name(self, value: str) -> None:
        self._write("session_name", value)

    @property
    def current_url(self) -> str:
        return self._read("current_url", str)

    @current_url.setter
    def current_url(self, value: str) -> None:
        self._write("current_url", value)

    def __getattr__(self, name: str) -> Any:
        # Called only when normal lookup fails: the registered riders.
        if name in _RIDERS:
            return self._read(name, SESSION_RIDER_DEFAULTS[name])
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _RIDERS:
            self._write(name, value)
        else:
            object.__setattr__(self, name, value)

    def __reduce_ex__(self, protocol: Any) -> NoReturn:
        # A pickle naming this class is a class path no older release can read.
        raise TypeError("a StoredSession is never pickled or copied; use stored_state()")
