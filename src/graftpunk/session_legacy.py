"""Read a saved session's state dict out of a legacy pickle.

Every release up to 1.44.0 stores a session as a dill pickle of the session
object. This module unpickles such a payload without importing the session
classes (and so without selenium, requestium, nodriver or httpie): the three
session classes are replaced by a placeholder that keeps the state dict given
to ``__setstate__``, and any other class that cannot be imported is stubbed the
same way and reported in ``unresolved``.

This is not a security boundary. ``find_class`` still resolves any importable
name, so a hostile payload can run code exactly as the stdlib unpickler would;
the only protection is that Fernet authenticates the bytes, so the key is the
trust boundary.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import dill

from graftpunk.session_riders import SESSION_CLASS_PATHS, CachedToken


class _Capture:
    """Placeholder for a class whose state is wanted but whose code is not.

    ``__setstate__`` receives a dict, or the ``(dict, slotstate)`` tuple pickle
    delivers for ``__slots__`` classes; any other shape is refused rather than
    dropped.
    """

    def __setstate__(self, state: object) -> None:
        if isinstance(state, tuple) and len(state) == 2:
            dict_state, slot_state = state
            if dict_state:
                self.__dict__.update(dict_state)
            for key, value in (slot_state or {}).items():
                setattr(self, key, value)
        elif isinstance(state, dict):
            self.__dict__.update(state)
        else:
            raise TypeError(f"cannot restore pickle state of type {type(state)!r}")


# Class paths a stored session names, mapped to what reads them: the session
# classes to the capture placeholder, CachedToken to the real class (it needs
# no browser stack, and resolving it here keeps graftpunk.tokens unimported).
FROZEN_CLASS_PATHS: Mapping[tuple[str, str], object] = {
    **dict.fromkeys(SESSION_CLASS_PATHS, _Capture),
    (CachedToken.__module__, CachedToken.__qualname__): CachedToken,
}


@dataclass(frozen=True)
class LegacyState:
    """The state dict of a pickled session and what it was pickled as."""

    kind: str | None
    class_path: str
    state: dict[str, Any]
    unresolved: tuple[str, ...]


class _Unpickler(dill.Unpickler):
    def __init__(self, file: io.BytesIO) -> None:
        super().__init__(file)
        self.unresolved: list[str] = []
        self._placeholders: dict[tuple[str, str], type] = {}

    def _placeholder(self, module: str, name: str) -> type:
        # One subclass per path, so the instance reports the class it replaces.
        key = (module, name)
        if key not in self._placeholders:
            self._placeholders[key] = type(name, (_Capture,), {"__module__": module})
            self._placeholders[key].__qualname__ = name
        return self._placeholders[key]

    def find_class(self, module: str, name: str) -> Any:
        frozen = FROZEN_CLASS_PATHS.get((module, name))
        if frozen is _Capture:
            return self._placeholder(module, name)
        if frozen is not None:
            return frozen
        try:
            return super().find_class(module, name)
        except ImportError:
            path = f"{module}.{name}"
            if path not in self.unresolved:
                self.unresolved.append(path)
            return self._placeholder(module, name)


def state_from_pickle(decrypted: bytes) -> LegacyState:
    """Return the state dict of the session pickled in *decrypted*.

    ``pickle.UnpicklingError`` propagates for bytes that are not a pickle. A
    top-level object the placeholder did not capture (a pickled ``dict`` or
    ``types.SimpleNamespace``) gives ``kind=None`` and its ``vars()``; one with
    no ``__dict__`` raises ``TypeError``.
    """
    unpickler = _Unpickler(io.BytesIO(decrypted))
    obj = unpickler.load()
    cls = type(obj)
    try:
        state = dict(vars(obj))
    except TypeError:
        raise TypeError(f"a pickled {cls.__name__} holds no session state") from None
    return LegacyState(
        kind=SESSION_CLASS_PATHS.get((cls.__module__, cls.__qualname__)),
        class_path=f"{cls.__module__}.{cls.__qualname__}",
        state=state,
        unresolved=tuple(unpickler.unresolved),
    )
