"""Partial stub for nodriver.cdp.network.

nodriver ships this generated module with a Latin-1 byte in a comment
(``JSON (\xb1Inf)``), so type checkers cannot read it: ty reports
"stream did not contain valid UTF-8" and every member becomes unresolved.
Only this module is stubbed; the rest of nodriver resolves from the installed
package (ty 0.0.75 falls back regardless; the ``partial`` marker in ``py.typed``
is kept for mypy/pyright). Delete this directory once upstream ships the file
as UTF-8.
"""

from typing import Any

def __getattr__(name: str) -> Any: ...
