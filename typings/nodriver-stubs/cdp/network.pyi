"""Partial stub for nodriver.cdp.network.

nodriver ships this generated module with a Latin-1 byte in a comment
(``JSON (\xb1Inf)``), so type checkers cannot read it: ty reports
"stream did not contain valid UTF-8" and every member becomes unresolved.
The package is a PEP 561 *partial* stub (see ``py.typed``), so only this one
module is replaced; the rest of nodriver still resolves from the installed
package. Delete this directory once upstream ships the file as UTF-8.
"""

from typing import Any

def __getattr__(name: str) -> Any: ...
