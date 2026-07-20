"""One-time generator for the browser-free deserialize fixture.

Produces a Fernet-encrypted dill pickle of a BrowserSession carrying DUMMY
credentials, plus the test key used to encrypt it. Commit both outputs.

IMPORTANT: this does NOT construct BrowserSession via its normal __init__
(which launches a browser through create_stealth_driver/requestium). Instead
it builds the instance with BrowserSession.__new__ and sets only the plain
attributes __getstate__'s nodriver branch reads (session.py "backend_type ==
'nodriver'" block). Because graftpunk.session is importable in THIS process
(we import BrowserSession above), dill pickles the class BY REFERENCE — it
records the module + qualname and does not embed the class body. That is
what makes the committed fixture meaningful: the browser-free A4 guard test
forces graftpunk.session out of sys.modules before loading, so the unpickler
must fail to import it and fall back to cache._Stub. If dill had embedded the
class by value instead, the fixture would defeat the whole point of Task 5's
guard (it would "work" even with the browser stack genuinely absent, without
ever exercising the stub path for the REAL class name).
"""

import pathlib

import dill
import requests
from cryptography.fernet import Fernet

from graftpunk.session import BrowserSession
from graftpunk.tokens import _CACHE_ATTR

FIX = pathlib.Path(__file__).parent.parent / "tests" / "fixtures"


def main() -> None:
    key = Fernet.generate_key()

    sess = BrowserSession.__new__(BrowserSession)  # no __init__ -> no browser launch
    sess._backend_type = "nodriver"  # force the browser-free __getstate__ branch
    sess._use_stealth = False
    sess.current_url = "https://example.com"  # non-empty -> __getstate__ won't touch self.driver
    sess._session_name = "fixture"

    jar = requests.cookies.RequestsCookieJar()
    jar.set("User", "dummyuser", domain="example.com")
    jar.set("Password", "dummypass", domain="example.com")
    sess.cookies = jar
    sess.headers = {"User-Agent": "Mozilla/5.0 (dummy)"}
    sess._gp_header_roles = {"api": {"X-Api-Key": "dummy"}}
    setattr(sess, _CACHE_ATTR, {"cached": "dummy"})
    # NOTE: BrowserSession.__getstate__ adds only _gp_header_roles + _gp_cached_tokens
    # (session.py's nodriver branch); it does NOT serialize _gp_csrf_tokens, so a csrf
    # attr would be dropped on pickling — intentionally omitted here and not asserted
    # in the A4 guard test.

    blob = dill.dumps(sess)  # by-reference: names graftpunk.session.BrowserSession
    (FIX / "browserfree_session.enc").write_bytes(Fernet(key).encrypt(blob))
    (FIX / "browserfree_session.key").write_bytes(key)
    print("wrote fixture + key to", FIX)


if __name__ == "__main__":
    main()
