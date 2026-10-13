"""Partial stub for nodriver.cdp.network.

nodriver ships this generated module with a Latin-1 byte in a comment
(``JSON (\xb1Inf)``), so type checkers cannot read it: ty reports
"stream did not contain valid UTF-8" and every member becomes unresolved.
Only this module is stubbed; the rest of nodriver resolves from the installed
package (ty 0.0.75 falls back regardless; the ``partial`` marker in ``py.typed``
is kept for mypy/pyright). nodriver 0.50.6 ships the file as UTF-8, but the
dependency floor is still 0.48 below Python 3.14; delete this directory once the
floor reaches 0.50.6 everywhere.

Trade-off: every member resolves to ``Any``, so a typo in a member name on
this one module is not caught by ty; the tests exercise the real calls.
"""

from typing import Any

def __getattr__(name: str) -> Any: ...
