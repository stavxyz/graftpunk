"""graftpunk sessions send the cookies Chrome would send (#253)."""

from __future__ import annotations

import asyncio
import copy
import email.message
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
import types
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from http import cookiejar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import dill
import pytest
import requests
from cryptography.fernet import Fernet
from typer.testing import CliRunner

from graftpunk import cache
from graftpunk.cli.session_commands import session_app
from graftpunk.cookies import (
    _LOCALHOST_SUFFIX,
    _LOOPBACK_HOSTS,
    BrowserCookiePolicy,
    BrowserCookieSession,
    _is_secure_loopback_host,
    importing_httpie,
    use_browser_cookie_policy,
)
from graftpunk.graftpunk_session import GraftpunkSession
from tests.unit.test_lite_cli_hints import _cache_fixture_session

_FIX = pathlib.Path(__file__).parent.parent / "fixtures"


def test_the_secure_hosts_are_the_ones_chrome_was_measured_on() -> None:
    """Chrome 155 sent a Secure cookie over plain HTTP to exactly these
    (2026-10-16). Widening the set needs a new measurement."""
    assert frozenset({"localhost", "127.0.0.1", "::1"}) == _LOOPBACK_HOSTS
    assert _LOCALHOST_SUFFIX == ".localhost"


@pytest.mark.parametrize(
    ("host", "secure"),
    [
        ("localhost", True),
        ("LOCALHOST", True),
        ("app.localhost", True),
        ("127.0.0.1", True),
        ("::1", True),
        ("127.0.0.2", False),
        ("notlocalhost", False),
        ("localhost.example.com", False),
        ("example.com", False),
    ],
)
def test_is_secure_loopback_host(host: str, secure: bool) -> None:
    assert _is_secure_loopback_host(host) is secure


def _cookie_header(session: requests.Session, url: str, **kwargs: object) -> str | None:
    return session.prepare_request(requests.Request("GET", url, **kwargs)).headers.get("Cookie")


def _with_secure_cookies(session: requests.Session) -> requests.Session:
    session.cookies.set("lh", "1", domain="localhost.local", secure=True)
    session.cookies.set("v4", "1", domain="127.0.0.1", secure=True)
    # The stdlib jar files a dotless host's cookies under "<host>.local".
    session.cookies.set("v6", "1", domain="[::1].local", secure=True)
    session.cookies.set("ex", "1", domain="example.com", secure=True)
    return session


@pytest.mark.parametrize("session_class", [GraftpunkSession, BrowserCookieSession])
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://localhost/", "lh=1"),
        ("http://localhost:8080/", "lh=1"),
        ("http://127.0.0.1/", "v4=1"),
        ("http://[::1]:8080/", "v6=1"),
        ("http://example.com/", None),
        ("https://example.com/", "ex=1"),
    ],
)
def test_a_graftpunk_session_sends_a_secure_cookie_where_chrome_does(
    session_class: type[requests.Session], url: str, expected: str | None
) -> None:
    session = _with_secure_cookies(session_class())

    assert isinstance(session.cookies.get_policy(), BrowserCookiePolicy)
    assert _cookie_header(session, url) == expected


def test_a_cookie_header_the_caller_set_is_kept() -> None:
    session = _with_secure_cookies(GraftpunkSession())

    assert _cookie_header(session, "http://localhost/", headers={"Cookie": "mine=1"}) == "mine=1"
    session.headers["Cookie"] = "session-level=1"
    assert _cookie_header(session, "http://localhost/") == "session-level=1"


def test_request_cookies_are_sent_with_the_session_policy() -> None:
    session = GraftpunkSession()
    extra = requests.cookies.RequestsCookieJar()
    extra.set("req", "1", domain="localhost.local", secure=True)

    assert _cookie_header(session, "http://localhost/", cookies=extra) == "req=1"


def test_a_reassigned_jar_gets_the_policy_at_the_next_request() -> None:
    session = GraftpunkSession()
    jar = requests.cookies.RequestsCookieJar()
    jar.set("tok", "1", domain="localhost.local", secure=True)
    session.cookies = jar

    assert _cookie_header(session, "http://localhost/") == "tok=1"
    assert isinstance(jar.get_policy(), BrowserCookiePolicy)


def test_a_standard_policys_settings_are_kept() -> None:
    session = GraftpunkSession()
    session.cookies.set_policy(cookiejar.DefaultCookiePolicy(blocked_domains=["localhost.local"]))
    session.cookies.set("tok", "1", domain="localhost.local", secure=True)
    session.cookies.set("v4", "1", domain="127.0.0.1", secure=True)

    assert _cookie_header(session, "http://localhost/") is None
    assert _cookie_header(session, "http://127.0.0.1/") == "v4=1"
    assert type(session.cookies.get_policy()) is BrowserCookiePolicy
    assert session.cookies.get_policy().blocked_domains() == ("localhost.local",)


def test_a_policy_class_the_caller_chose_is_kept() -> None:
    class NoCookies(cookiejar.DefaultCookiePolicy):
        def return_ok(self, cookie: cookiejar.Cookie, request: object) -> bool:
            return False

    session = GraftpunkSession()
    mine = NoCookies()
    session.cookies.set_policy(mine)
    session.cookies.set("tok", "1", domain="example.com")

    assert _cookie_header(session, "https://example.com/") is None
    assert session.cookies.get_policy() is mine


class _CookieServer(BaseHTTPRequestHandler):
    """/set sets a Secure and a plain cookie; /redirect 302s to /echo, which
    answers with the Cookie header it received."""

    def do_GET(self) -> None:
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/echo")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = (self.headers.get("Cookie") or "").encode()
        self.send_response(200)
        if self.path == "/set":
            self.send_header("Set-Cookie", "sec=1; Secure; Path=/")
            self.send_header("Set-Cookie", "plain=1; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


class _V6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def _serve(server: ThreadingHTTPServer) -> Iterator[int]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def cookie_server() -> Iterator[int]:
    yield from _serve(ThreadingHTTPServer(("127.0.0.1", 0), _CookieServer))


@pytest.fixture
def cookie_server_v6() -> Iterator[int]:
    try:
        server = _V6Server(("::1", 0), _CookieServer)
    except OSError as exc:
        pytest.skip(f"no IPv6 loopback here: {exc}")
    yield from _serve(server)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_a_secure_cookie_survives_a_redirect_over_http(cookie_server: int, host: str) -> None:
    _assert_secure_cookie_survives_a_redirect(f"http://{host}:{cookie_server}")


def test_a_secure_cookie_survives_a_redirect_over_http_to_ipv6_loopback(
    cookie_server_v6: int,
) -> None:
    _assert_secure_cookie_survives_a_redirect(f"http://[::1]:{cookie_server_v6}")


def _assert_secure_cookie_survives_a_redirect(base: str) -> None:
    session = GraftpunkSession()
    session.get(f"{base}/set", timeout=10)

    response = session.get(f"{base}/redirect", timeout=10)

    assert response.history, "the server did not redirect"
    assert response.url == f"{base}/echo"
    assert sorted(response.text.split("; ")) == ["plain=1", "sec=1"]


def _fixture_bytes() -> tuple[bytes, bytes]:
    return (_FIX / "browserfree_session.enc").read_bytes(), (
        _FIX / "browserfree_session.key"
    ).read_bytes()


def test_the_committed_fixture_was_pickled_with_httpies_policy() -> None:
    """The fixture is a session cached by an earlier release, so the load tests
    below exercise the replacement rather than a jar that never needed it."""
    enc, key = _fixture_bytes()

    assert b"HTTPieCookiePolicy" in Fernet(key).decrypt(enc)


def test_an_api_session_from_an_old_cache_uses_the_browser_policy() -> None:
    enc, key = _fixture_bytes()

    api = cache.load_session_for_api_from_bytes(enc, key=key)

    assert type(api.cookies.get_policy()) is BrowserCookiePolicy


def test_an_old_cached_browser_session_loads_with_the_browser_policy() -> None:
    enc, key = _fixture_bytes()

    session = dill.loads(Fernet(key).decrypt(enc))  # noqa: S301

    assert type(session.cookies.get_policy()) is BrowserCookiePolicy


def _httpie_policy() -> cookiejar.CookiePolicy:
    # httpie.cookies alone leaves the global policy alone; httpie.compat replaces it.
    from httpie.cookies import HTTPieCookiePolicy

    return HTTPieCookiePolicy()


def test_an_old_pickled_session_loads_with_the_browser_policy() -> None:
    old = BrowserCookieSession()
    old.cookies.set_policy(_httpie_policy())
    old.cookies.set("tok", "1", domain="localhost.local", secure=True)

    loaded = dill.loads(dill.dumps(old))  # noqa: S301

    assert type(loaded.cookies.get_policy()) is BrowserCookiePolicy
    assert _cookie_header(loaded, "http://localhost/") == "tok=1"


@pytest.mark.parametrize("backend_type", ["nodriver", "selenium"])
def test_an_old_browser_session_state_restores_with_the_browser_policy(
    backend_type: str,
) -> None:
    from graftpunk.session import BrowserSession

    jar = requests.cookies.RequestsCookieJar()
    jar.set_policy(_httpie_policy())
    session = BrowserSession.__new__(BrowserSession)
    state = {"_backend_type": backend_type, "cookies": jar, "headers": {}, "_driver": None}
    if backend_type == "selenium":
        state.update(requests.Session().__getstate__())
        state["cookies"] = jar

    session.__setstate__(state)

    assert type(session.cookies.get_policy()) is BrowserCookiePolicy


def test_a_stubbed_httpie_policy_is_replaced() -> None:
    """On graftpunk-lite without HTTPie the browser-free unpickler stands a stub
    in for HTTPieCookiePolicy; it has none of a policy's methods."""
    stub = type("HTTPieCookiePolicy", (cache._Stub,), {})()
    jar = requests.cookies.RequestsCookieJar()
    jar._policy = stub

    assert type(use_browser_cookie_policy(jar).get_policy()) is BrowserCookiePolicy


def test_an_exported_session_sends_its_cookies_through_httpie(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _cache_fixture_session(tmp_path, monkeypatch)
    target = tmp_path / "export.json"
    result = CliRunner().invoke(session_app, ["export", "myshop", "-o", str(target)])
    assert result.exit_code == 0, result.output

    # The fixture's cookies are for example.com.
    assert _httpie_cookie(target, "http://example.com/x", tmp_path) == (
        "User=dummyuser; Password=dummypass"
    )
    assert _httpie_cookie(target, "http://localhost/x", tmp_path) is None


def _httpie_cookie(session_file: pathlib.Path, url: str, tmp_path: pathlib.Path) -> str | None:
    """The Cookie header HTTPie sends to *url* with *session_file*, offline."""
    http = pathlib.Path(sys.executable).parent / "http"
    sent = subprocess.run(  # noqa: S603
        [
            str(http),
            "--offline",
            "--ignore-stdin",
            f"--session-read-only={session_file}",
            "GET",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            "HOME": str(tmp_path / "home"),
            "HTTPIE_CONFIG_DIR": str(tmp_path / "httpie"),
        },
    )
    assert sent.returncode == 0, sent.stderr
    cookies = [line for line in sent.stdout.splitlines() if line.startswith("Cookie: ")]
    assert len(cookies) <= 1, sent.stdout
    return cookies[0].removeprefix("Cookie: ") if cookies else None


def test_an_exported_session_keeps_each_cookies_domain_path_and_secure_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """#260: the export used to keep only names and values."""
    with importing_httpie():
        import httpie.context
    monkeypatch.setattr(httpie.context.Environment, "config_dir", tmp_path / "httpie")
    session = _browser_session()
    session._session_name = "myshop"
    session._webdriver = types.SimpleNamespace(current_url="https://myshop.example/")
    session.cookies.set("tok", "1", domain="myshop.example")
    session.cookies.set("sec", "1", domain="myshop.example", secure=True)
    session.cookies.set("api", "1", domain="myshop.example", path="/api")
    target = session.save_httpie_session(output=tmp_path / "myshop.json")

    assert _httpie_cookie(target, "http://other.example/", tmp_path) is None
    assert _httpie_cookie(target, "http://myshop.example/", tmp_path) == "tok=1"
    assert _httpie_cookie(target, "https://myshop.example/", tmp_path) == "tok=1; sec=1"
    assert _httpie_cookie(target, "https://myshop.example/api/x", tmp_path) == (
        "api=1; tok=1; sec=1"
    )


class _CookieAuth(requests.auth.AuthBase):
    """An auth handler that authenticates with a cookie of its own."""

    def __call__(self, r: requests.PreparedRequest) -> requests.PreparedRequest:
        r.headers["Cookie"] = "token=abc"
        return r


@pytest.mark.parametrize("auth_on", ["request", "session"])
@pytest.mark.parametrize("session_class", [GraftpunkSession, BrowserCookieSession])
def test_a_cookie_header_an_auth_handler_set_is_kept_as_stock_requests_keeps_it(
    session_class: type[requests.Session], auth_on: str
) -> None:
    def header(session: requests.Session) -> str | None:
        session.cookies.set("sid", "1", domain="example.com")
        request = requests.Request("GET", "http://example.com/")
        if auth_on == "request":
            request.auth = _CookieAuth()
        else:
            session.auth = _CookieAuth()
        return session.prepare_request(request).headers.get("Cookie")

    assert header(requests.Session()) == "token=abc"
    assert header(session_class()) == "token=abc"


def test_an_auth_handler_sees_the_cookie_header_the_browser_policy_computed() -> None:
    seen: list[str | None] = []

    def auth(r: requests.PreparedRequest) -> requests.PreparedRequest:
        seen.append(r.headers.get("Cookie"))
        return r

    session = _with_secure_cookies(GraftpunkSession())
    session.prepare_request(requests.Request("GET", "http://localhost/", auth=auth))

    assert seen == ["lh=1"]


def test_overlapping_importing_httpie_blocks_leave_the_global_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thread B starts its block while thread A's block has another class
    installed. Without a lock B saves that class and restores it last."""
    from graftpunk.cookies import importing_httpie

    class Before(cookiejar.DefaultCookiePolicy):
        pass

    class During(cookiejar.DefaultCookiePolicy):
        pass

    monkeypatch.setattr(cookiejar, "DefaultCookiePolicy", Before)
    a_installed = threading.Event()
    b_entered = threading.Event()
    a_exited = threading.Event()

    def thread_a() -> None:
        with importing_httpie():
            cookiejar.DefaultCookiePolicy = During
            a_installed.set()
            # With the lock, B cannot enter until this block ends.
            b_entered.wait(timeout=1)
        a_exited.set()

    def thread_b() -> None:
        a_installed.wait(timeout=5)
        with importing_httpie():
            b_entered.set()
            a_exited.wait(timeout=5)

    threads = [threading.Thread(target=thread_a), threading.Thread(target=thread_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert cookiejar.DefaultCookiePolicy is Before


def test_a_caller_inside_importing_httpie_can_save_an_httpie_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """save_httpie_session opens the block again inside the caller's; a
    plain Lock would deadlock here."""
    from graftpunk.cookies import importing_httpie

    _cache_fixture_session(tmp_path, monkeypatch)
    session = cache.load_session("myshop")
    target = tmp_path / "export.json"
    errors: list[BaseException] = []

    def nested() -> None:
        try:
            with importing_httpie():
                session.save_httpie_session("myshop", output=target)  # ty: ignore[unresolved-attribute]
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=nested, daemon=True)
    thread.start()
    thread.join(timeout=30)

    assert not thread.is_alive()
    assert errors == []
    assert target.exists()


def _session_with_settings(session_class: type[requests.Session]) -> requests.Session:
    session = session_class()
    session.cookies.get_policy().set_blocked_domains(["blocked.example"])
    session.cookies.set("tok", "1", domain="localhost.local", secure=True)
    return session


@pytest.mark.parametrize("session_class", [GraftpunkSession, BrowserCookieSession])
def test_the_browser_policy_pickles_as_the_standard_librarys_class(
    session_class: type[requests.Session],
) -> None:
    """Releases through 1.21.0 have no graftpunk.cookies, so a cache written by
    this release must not name it."""
    session = _session_with_settings(session_class)

    jar = dill.dumps(session.cookies)
    assert b"graftpunk.cookies" not in jar
    assert b"BrowserCookiePolicy" not in jar
    # BrowserCookieSession is itself defined in graftpunk.cookies; the cached
    # BrowserSession is not.
    assert b"BrowserCookiePolicy" not in dill.dumps(session)


_LOAD_WITHOUT_GRAFTPUNK_COOKIES = """
import base64, json, sys


class _Block:
    def find_spec(self, name, path=None, target=None):
        if name == "graftpunk.cookies":
            raise ImportError("graftpunk.cookies is absent here")
        return None


sys.meta_path.insert(0, _Block())
import dill

jar = dill.loads(base64.b64decode(sys.argv[1]))
policy = jar.get_policy()
print(json.dumps({
    "policy": f"{type(policy).__module__}.{type(policy).__qualname__}",
    "blocked": list(policy.blocked_domains()),
    "cookies": [c.name for c in jar],
    "graftpunk_cookies_loaded": "graftpunk.cookies" in sys.modules,
}))
"""


def test_a_pickled_jar_loads_where_graftpunk_cookies_cannot_be_imported() -> None:
    import base64
    import json

    raw = dill.dumps(_session_with_settings(GraftpunkSession).cookies)

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", _LOAD_WITHOUT_GRAFTPUNK_COOKIES, base64.b64encode(raw)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "policy": "http.cookiejar.DefaultCookiePolicy",
        "blocked": ["blocked.example"],
        "cookies": ["tok"],
        "graftpunk_cookies_loaded": False,
    }


def test_a_session_this_release_pickled_loads_with_the_browser_policy_and_its_settings() -> None:
    loaded = dill.loads(dill.dumps(_session_with_settings(BrowserCookieSession)))  # noqa: S301
    api = cache._api_session_from_session(loaded)

    for jar in (loaded.cookies, api.cookies):
        assert type(jar.get_policy()) is BrowserCookiePolicy
        assert jar.get_policy().blocked_domains() == ("blocked.example",)
    assert _cookie_header(api, "http://localhost/") == "tok=1"


def test_copying_the_browser_policy_keeps_its_class_and_settings() -> None:
    policy = BrowserCookiePolicy(blocked_domains=["blocked.example"])

    for copied in (copy.copy(policy), copy.deepcopy(policy)):
        assert type(copied) is BrowserCookiePolicy
        assert copied.blocked_domains() == ("blocked.example",)


class _Stricter(BrowserCookiePolicy):
    """A program's own rule on top of the browser's: never send ``tracking``."""

    def return_ok(self, cookie: cookiejar.Cookie, request: object) -> bool:
        return cookie.name != "tracking" and super().return_ok(cookie, request)


def _session_with_stricter_policy() -> requests.Session:
    # BrowserCookieSession: a GraftpunkSession is built at load, not pickled.
    session = BrowserCookieSession()
    session.cookies.set_policy(_Stricter(blocked_domains=["blocked.example"]))
    session.cookies.set("tracking", "1", domain="localhost.local", secure=True)
    session.cookies.set("ok", "1", domain="localhost.local", secure=True)
    return session


def _copy_with_copied_policy(session: requests.Session) -> requests.Session:
    other = BrowserCookieSession()
    other.cookies = requests.cookies.RequestsCookieJar(
        policy=copy.copy(session.cookies.get_policy())
    )
    other.cookies.update(session.cookies)
    return other


@pytest.mark.parametrize(
    "duplicate",
    [
        pytest.param(lambda s: dill.loads(dill.dumps(s)), id="pickle"),  # noqa: S301
        pytest.param(_copy_with_copied_policy, id="copy"),
        pytest.param(copy.deepcopy, id="deepcopy"),
    ],
)
def test_a_subclass_of_the_browser_policy_keeps_its_class_and_rules(
    duplicate: Callable[[requests.Session], requests.Session],
) -> None:
    session = _session_with_stricter_policy()
    assert _cookie_header(session, "http://localhost/") == "ok=1"

    other = duplicate(session)

    policy = other.cookies.get_policy()
    assert type(policy) is _Stricter
    assert policy.blocked_domains() == ("blocked.example",)
    assert _cookie_header(other, "http://localhost/") == "ok=1"


@pytest.mark.parametrize("jar_class", [cookiejar.CookieJar, cookiejar.MozillaCookieJar])
@pytest.mark.parametrize("session_class", [GraftpunkSession, BrowserCookieSession])
def test_a_standard_library_jar_sends_as_it_does_with_stock_requests(
    session_class: type[requests.Session], jar_class: type[cookiejar.CookieJar]
) -> None:
    """requests accepts any http.cookiejar.CookieJar as Session.cookies."""

    def with_jar(session: requests.Session) -> requests.Session:
        jar = jar_class()
        jar.set_cookie(requests.cookies.create_cookie("plain", "1", domain="example.com"))
        jar.set_cookie(
            requests.cookies.create_cookie("lh", "1", domain="localhost.local", secure=True)
        )
        session.cookies = jar  # ty: ignore[invalid-assignment]
        return session

    stock = with_jar(requests.Session())
    ours = with_jar(session_class())

    assert _cookie_header(stock, "http://example.com/") == "plain=1"
    assert _cookie_header(ours, "http://example.com/") == "plain=1"
    assert _cookie_header(ours, "http://localhost/") == "lh=1"
    assert type(ours.cookies._policy) is BrowserCookiePolicy


def test_a_graftpunk_sessions_own_jar_does_not_take_a_replaced_globals_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loaded or assigned jar keeps its default policy's settings; a jar the
    session creates starts from the browser policy's own, whatever class the
    global names."""

    class BlocksLocalhost(cookiejar.DefaultCookiePolicy):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(blocked_domains=["localhost.local"], **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(cookiejar, "DefaultCookiePolicy", BlocksLocalhost)

    from graftpunk.session import BrowserSession

    def nodriver_session() -> requests.Session:
        return BrowserSession(backend="nodriver")

    def nodriver_session_restored_without_cookies() -> requests.Session:
        session = BrowserSession.__new__(BrowserSession)
        session.__setstate__(
            {"_backend_type": "nodriver", "cookies": requests.cookies.RequestsCookieJar()}
        )
        return session

    for make_session in (
        GraftpunkSession,
        BrowserCookieSession,
        nodriver_session,
        nodriver_session_restored_without_cookies,
    ):
        session = _with_secure_cookies(make_session())
        policy = session.cookies.get_policy()
        assert type(policy) is BrowserCookiePolicy, make_session
        assert policy.blocked_domains() == (), make_session
        assert _cookie_header(session, "http://localhost/") == "lh=1", make_session


def _session_hook(r: requests.Response, *args: object, **kwargs: object) -> None:
    return None


def _request_hook(r: requests.Response, *args: object, **kwargs: object) -> None:
    return None


_DIGEST = requests.auth.HTTPDigestAuth("u", "p")
_URL = "http://example.com/a"

_REQUEST_KINDS = {
    "params": lambda: requests.Request("GET", f"{_URL}?q=1", params={"rp": "2"}),
    "json": lambda: requests.Request("POST", _URL, json={"a": 1}),
    "form, lowercase method": lambda: requests.Request("post", _URL, data={"a": "1"}),
    "files": lambda: requests.Request(
        "POST", _URL, data={"a": "1"}, files={"f": ("f.txt", b"x", "text/plain")}
    ),
    "auth in url": lambda: requests.Request("GET", "http://user:pw@example.com/a"),
    "tuple auth": lambda: requests.Request("GET", _URL, auth=("u", "p")),
    "digest auth": lambda: requests.Request("GET", _URL, auth=_DIGEST),
    "netrc": lambda: requests.Request("GET", "http://netrc.example/a"),
    "request cookies": lambda: requests.Request("GET", _URL, cookies={"sid": "2", "rc": "1"}),
    "caller cookie header": lambda: requests.Request("GET", _URL, headers={"Cookie": "mine=1"}),
    "header set to None": lambda: requests.Request("GET", _URL, headers={"X-Drop": None}),
    "request hooks": lambda: requests.Request("GET", _URL, hooks={"response": [_request_hook]}),
}


@pytest.mark.parametrize("kind", sorted(_REQUEST_KINDS))
def test_the_mixin_prepares_every_field_as_stock_requests_does(
    kind: str, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The mixin copies requests.Session.prepare_request; a requests release
    that changes what the stock method does for one of these request kinds
    fails this test rather than drifting."""
    import urllib3.filepost

    netrc = tmp_path / "netrc"
    netrc.write_text("machine netrc.example login nu password np\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    monkeypatch.setattr(urllib3.filepost, "choose_boundary", lambda: "boundary")

    def prepare(session: requests.Session) -> requests.PreparedRequest:
        session.headers["X-Session"] = "s"
        session.headers["X-Drop"] = "d"
        session.params = {"sp": "1"}
        session.hooks["response"].append(_session_hook)
        session.cookies.set("sid", "1", domain="example.com")
        return session.prepare_request(_REQUEST_KINDS[kind]())

    stock = prepare(requests.Session())
    ours = prepare(BrowserCookieSession())

    assert ours.method == stock.method
    assert ours.url == stock.url
    assert dict(ours.headers) == dict(stock.headers)
    assert ours.body == stock.body
    assert ours.hooks == stock.hooks


# Cookies as a browser reports them at capture: a Secure persistent domain
# cookie, a path-scoped one, a host-only one and a localhost one.
_EXPIRY = 2_000_000_000
_CAPTURED: list[dict[str, Any]] = [
    {"name": "sid", "domain": ".example.com", "path": "/", "secure": True, "expiry": _EXPIRY},
    {"name": "app", "domain": ".example.com", "path": "/app", "secure": False, "expiry": None},
    {"name": "host", "domain": "example.com", "path": "/", "secure": False, "expiry": None},
    {"name": "lh", "domain": "localhost", "path": "/", "secure": True, "expiry": None},
]


def _cdp_cookie(record: dict[str, Any]) -> Any:
    network = pytest.importorskip("nodriver.cdp.network")
    expiry = record["expiry"]
    return network.Cookie(
        name=record["name"],
        value="1",
        domain=record["domain"],
        path=record["path"],
        size=0,
        http_only=record["name"] == "sid",
        secure=record["secure"],
        session=expiry is None,
        priority=network.CookiePriority.MEDIUM,
        source_scheme=network.CookieSourceScheme.SECURE,
        source_port=443,
        # CDP reports seconds as a float, and -1 for a session cookie.
        expires=-1.0 if expiry is None else expiry + 0.25,
    )


def _webdriver_cookie(record: dict[str, Any]) -> dict[str, Any]:
    cookie = {
        "name": record["name"],
        "value": "1",
        "domain": record["domain"],
        "path": record["path"],
        "secure": record["secure"],
        "httpOnly": record["name"] == "sid",
        "sameSite": "Lax",
    }
    # WebDriver leaves expiry out of a session cookie.
    if record["expiry"] is not None:
        cookie["expiry"] = record["expiry"]
    return cookie


def _browser_session() -> Any:
    from graftpunk.session import BrowserSession

    session = BrowserSession.__new__(BrowserSession)
    requests.Session.__init__(session)
    session._give_new_jar_the_browser_cookie_policy()
    return session


def _captured_by_nodriver(records: list[dict[str, Any]] = _CAPTURED) -> requests.Session:
    cookies = [_cdp_cookie(record) for record in records]

    async def get_all() -> list[Any]:
        return cookies

    session = _browser_session()
    browser = types.SimpleNamespace(cookies=types.SimpleNamespace(get_all=get_all))
    session._backend_instance = types.SimpleNamespace(_browser=browser)
    asyncio.run(session.transfer_nodriver_cookies_to_session())
    return session


def _captured_by_selenium(records: list[dict[str, Any]] = _CAPTURED) -> requests.Session:
    session = _browser_session()
    session._backend_type = "selenium"
    session._webdriver = types.SimpleNamespace(
        get_cookies=lambda: [_webdriver_cookie(record) for record in records],
        execute_script=lambda script: "BrowserUA/1.0",
    )
    session.transfer_driver_cookies_to_session()
    assert session.headers["User-Agent"] == "BrowserUA/1.0"
    return session


_CAPTURE_PATHS: dict[str, Callable[..., requests.Session]] = {
    "nodriver": _captured_by_nodriver,
    "selenium": _captured_by_selenium,
}


@pytest.mark.parametrize("capture", sorted(_CAPTURE_PATHS))
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/", "sid=1; host=1"),
        ("http://example.com/", "host=1"),
        ("https://example.com/app/x", "app=1; sid=1; host=1"),
        ("https://example.com/other", "sid=1; host=1"),
        ("https://sub.example.com/", "sid=1"),
        ("http://localhost:8080/", "lh=1"),
        ("http://sub.localhost/", None),
    ],
)
def test_a_captured_cookie_is_sent_where_the_browser_sends_it(
    capture: str, url: str, expected: str | None
) -> None:
    session = _CAPTURE_PATHS[capture]()

    assert _cookie_header(session, url) == expected


def test_a_host_only_cookie_a_server_sets_is_not_sent_to_its_subdomains() -> None:
    headers = email.message.Message()
    headers["Set-Cookie"] = "host=1; Path=/"
    headers["Set-Cookie"] = "dom=1; Domain=example.com; Path=/"
    response = types.SimpleNamespace(info=lambda: headers)
    session = GraftpunkSession()
    session.cookies.extract_cookies(response, urllib.request.Request("https://example.com/"))

    assert _cookie_header(session, "https://example.com/") == "host=1; dom=1"
    assert _cookie_header(session, "https://sub.example.com/") == "dom=1"


@pytest.mark.parametrize("capture", sorted(_CAPTURE_PATHS))
def test_a_captured_cookie_keeps_its_expiry_and_httponly_flag(capture: str) -> None:
    by_name = {cookie.name: cookie for cookie in _CAPTURE_PATHS[capture]().cookies}

    assert by_name["sid"].expires == _EXPIRY
    assert not by_name["sid"].discard
    assert by_name["sid"].has_nonstandard_attr("HttpOnly")
    assert by_name["host"].expires is None
    assert by_name["host"].discard
    assert not by_name["host"].has_nonstandard_attr("HttpOnly")


@pytest.mark.parametrize("capture", sorted(_CAPTURE_PATHS))
def test_captured_cookies_keep_their_attributes_through_the_cache(capture: str) -> None:
    """The jar pickles with the session; an API session loaded from the cache
    gets it as is."""
    session = GraftpunkSession()
    session.cookies = dill.loads(dill.dumps(_CAPTURE_PATHS[capture]().cookies))  # noqa: S301

    assert _cookie_header(session, "http://example.com/") == "host=1"
    assert _cookie_header(session, "https://sub.example.com/") == "sid=1"


def _dotless(name: str, domain: str) -> dict[str, Any]:
    return {"name": name, "domain": domain, "path": "/", "secure": False, "expiry": None}


# Hosts without a dot, as CDP reports them (Chrome 155, measured 2026-10-16).
_DOTLESS_HOSTS = [("localhost", "http://localhost:8080/"), ("[::1]", "http://[::1]:8080/")]


@pytest.mark.parametrize("capture", sorted(_CAPTURE_PATHS))
@pytest.mark.parametrize(("host", "url"), [*_DOTLESS_HOSTS, ("intranet", "http://intranet/")])
def test_a_servers_refresh_replaces_a_captured_cookie_for_a_host_without_a_dot(
    capture: str, host: str, url: str
) -> None:
    """http.cookiejar files the refresh under "<host>.local"; a captured cookie
    stored under the bare host would be sent beside it, stale value first."""
    session = _CAPTURE_PATHS[capture]([_dotless("tok", host)])
    assert _cookie_header(session, url) == "tok=1"

    headers = email.message.Message()
    headers["Set-Cookie"] = "tok=NEW; Path=/"
    response = types.SimpleNamespace(info=lambda: headers)
    session.cookies.extract_cookies(response, urllib.request.Request(url))  # noqa: S310

    assert _cookie_header(session, url) == "tok=NEW"
    assert session.cookies.get("tok") == "NEW"


@pytest.mark.parametrize("capture", sorted(_CAPTURE_PATHS))
def test_a_captured_ipv6_host_without_brackets_is_sent_to_that_host(capture: str) -> None:
    session = _CAPTURE_PATHS[capture]([_dotless("v6", "::1")])

    assert _cookie_header(session, "http://[::1]:8080/") == "v6=1"


def test_an_exported_session_sends_captured_cookies_for_hosts_without_a_dot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    with importing_httpie():
        import httpie.context
    monkeypatch.setattr(httpie.context.Environment, "config_dir", tmp_path / "httpie")
    session = _captured_by_nodriver([_dotless("lh", "localhost"), _dotless("v6", "[::1]")])
    session._session_name = "local"
    session._webdriver = types.SimpleNamespace(current_url="http://localhost:8080/")
    target = session.save_httpie_session(output=tmp_path / "local.json")

    assert _httpie_cookie(target, "http://localhost:8080/", tmp_path) == "lh=1"
    assert _httpie_cookie(target, "http://[::1]:8080/", tmp_path) == "v6=1"
    assert _httpie_cookie(target, "http://example.com/", tmp_path) is None


def _set_through_credentialed_url(session: requests.Session) -> None:
    headers = email.message.Message()
    headers["Set-Cookie"] = "tok=1; Path=/"
    response = types.SimpleNamespace(info=lambda: headers)
    request = urllib.request.Request("http://u:secretpw@localhost:8080/")  # noqa: S310
    session.cookies.extract_cookies(response, request)


def test_an_export_keeps_credentials_from_the_url_out_of_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    with importing_httpie():
        import httpie.context
    monkeypatch.setattr(httpie.context.Environment, "config_dir", tmp_path / "httpie")
    session = _captured_by_nodriver([])
    _set_through_credentialed_url(session)
    assert {c.domain for c in session.cookies} == {"u:secretpw@localhost.local"}
    session._session_name = "local"
    session._webdriver = types.SimpleNamespace(current_url="http://localhost:8080/")
    target = session.save_httpie_session(output=tmp_path / "local.json")

    assert "secretpw" not in target.read_text()
    assert _httpie_cookie(target, "http://localhost:8080/", tmp_path) == "tok=1"


def test_inject_logs_name_the_host_without_credentials_from_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graftpunk import session as session_module
    from graftpunk.session import inject_cookies_to_nodriver

    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        session_module.LOG, "debug", lambda event, **kw: events.append({"event": event, **kw})
    )
    session = _captured_by_nodriver([])
    _set_through_credentialed_url(session)
    session.cookies.set_cookie(
        cookiejar.Cookie(
            0,
            "ak_bmsc",
            "1",
            None,
            False,
            "u:secretpw@localhost.local",
            False,
            False,
            "/",
            True,
            False,
            None,
            True,
            None,
            None,
            {},
        )
    )
    session.cookies.set_cookie(
        cookiejar.Cookie(
            0,
            "empty",
            None,
            None,
            False,
            "u:secretpw@localhost.local",
            False,
            False,
            "/",
            True,
            False,
            None,
            True,
            None,
            None,
            {},
        )
    )

    class _Tab:
        async def send(self, command: Any) -> None:
            next(command)

    asyncio.run(inject_cookies_to_nodriver(_Tab(), session.cookies))

    skipped = {e["event"]: e["domain"] for e in events if e["event"].startswith("inject_skipping")}
    assert skipped == {
        "inject_skipping_bot_cookie": "localhost",
        "inject_skipping_malformed_cookie": "localhost",
    }
    assert "secretpw" not in repr(events)


def test_injecting_cookies_gives_the_browser_their_secure_httponly_and_expiry() -> None:
    """Chrome 155 stored every injected cookie as a non-Secure, script-readable
    session cookie when these were left out (measured 2026-10-18, #261). The
    capture helper marks the cookie named sid HttpOnly."""
    from graftpunk.session import inject_cookies_to_nodriver

    later = 2_000_000_000
    session = _captured_by_nodriver(
        [
            {
                "name": "sid",
                "value": "1",
                "domain": "example.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expiry": later,
            },
            {
                "name": "plain",
                "value": "1",
                "domain": ".example.com",
                "path": "/app",
                "secure": False,
                "expiry": None,
            },
        ]
    )
    for header in ("srv=1; Secure; httponly; Max-Age=3600", "lower=1; HTTPONLY"):
        message = email.message.Message()
        message["Set-Cookie"] = header
        response = types.SimpleNamespace(info=lambda m=message: m)
        request = urllib.request.Request("https://example.com/")  # noqa: S310
        session.cookies.extract_cookies(response, request)
    sent: list[dict[str, Any]] = []

    class _Tab:
        async def send(self, command: Any) -> None:
            sent.extend(next(command)["params"]["cookies"])

    asyncio.run(inject_cookies_to_nodriver(_Tab(), session.cookies))

    by_name = {cookie["name"]: cookie for cookie in sent}
    assert by_name["sid"]["secure"] is True
    assert by_name["sid"]["httpOnly"] is True
    assert by_name["sid"]["expires"] == later
    assert by_name["plain"]["secure"] is False
    assert by_name["plain"]["httpOnly"] is False
    assert "expires" not in by_name["plain"]
    assert by_name["plain"]["path"] == "/app"
    assert by_name["srv"]["secure"] is True
    assert by_name["srv"]["httpOnly"] is True
    assert abs(by_name["srv"]["expires"] - (time.time() + 3600)) < 60
    assert by_name["lower"]["httpOnly"] is True
    assert "expires" not in by_name["lower"]


def _injected(jar: Any) -> dict[str, dict[str, Any]]:
    from graftpunk.session import inject_cookies_to_nodriver

    sent: list[dict[str, Any]] = []

    class _Tab:
        async def send(self, command: Any) -> None:
            sent.extend(next(command)["params"]["cookies"])

    asyncio.run(inject_cookies_to_nodriver(_Tab(), jar))
    return {cookie["name"]: cookie for cookie in sent}


def test_a_cookie_requests_created_is_not_injected_httponly() -> None:
    """requests gives every cookie it creates an HttpOnly key, so a session
    cached by 1.21.0 or earlier holds one on each cookie; Chrome must not get
    those back as script-unreadable."""
    jar = requests.cookies.RequestsCookieJar()
    jar.set("legacy", "1", domain="example.com")
    jar.set_cookie(requests.cookies.create_cookie("made", "1", domain="example.com"))
    reloaded = dill.loads(dill.dumps(jar))  # noqa: S301

    assert _injected(jar)["legacy"]["httpOnly"] is False
    assert _injected(jar)["made"]["httpOnly"] is False
    assert _injected(reloaded)["legacy"]["httpOnly"] is False


def test_only_the_set_cookie_header_decides_httponly() -> None:
    session = GraftpunkSession()
    for header in ("real=1; HttpOnly", "forged=1; graftpunk-http-only"):
        message = email.message.Message()
        message["Set-Cookie"] = header
        response = types.SimpleNamespace(info=lambda m=message: m)
        session.cookies.extract_cookies(
            response,
            urllib.request.Request("https://example.com/"),  # noqa: S310
        )
    reloaded = dill.loads(dill.dumps(session.cookies))  # noqa: S301

    assert _injected(session.cookies)["real"]["httpOnly"] is True
    assert _injected(session.cookies)["forged"]["httpOnly"] is False
    assert _injected(reloaded)["real"]["httpOnly"] is True


def _set_cookie(session: requests.Session, header: str) -> None:
    message = email.message.Message()
    message["Set-Cookie"] = header
    response = types.SimpleNamespace(info=lambda: message)
    session.cookies.extract_cookies(
        response,
        urllib.request.Request("https://example.com/"),  # noqa: S310
    )


def test_a_cookie_captured_by_1_22_0_is_injected_without_httponly() -> None:
    """1.22.0's capture wrote the HttpOnly key and graftpunk-host, not the mark."""
    jar = requests.cookies.RequestsCookieJar()
    jar.set_cookie(
        cookiejar.Cookie(
            0,
            "sid",
            "1",
            None,
            False,
            "example.com",
            False,
            False,
            "/",
            True,
            True,
            None,
            True,
            None,
            None,
            {"HttpOnly": None, "graftpunk-host": "example.com"},
        )
    )

    sent = _injected(jar)["sid"]
    assert (sent["secure"], sent["httpOnly"]) == (True, False)


def test_when_every_cookie_is_expired_the_warning_says_to_log_in_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graftpunk import session as session_module

    warnings: list[dict[str, Any]] = []
    monkeypatch.setattr(
        session_module.LOG, "warning", lambda event, **kw: warnings.append({"event": event, **kw})
    )
    jar = requests.cookies.RequestsCookieJar()
    jar.set("old", "1", domain="example.com", expires=int(time.time()) - 60)

    assert _injected(jar) == {}
    assert [w["event"] for w in warnings] == ["inject_all_cookies_filtered"]
    assert "log in again" in warnings[0]["hint"]
    assert "skip_bot_cookies" not in warnings[0]["hint"]

    warnings.clear()
    jar.set("bm_sv", "1", domain="example.com")
    _injected(jar)
    assert "log in again" in warnings[0]["hint"]
    assert "skip_bot_cookies=False" in warnings[0]["hint"]

    warnings.clear()
    bots = requests.cookies.RequestsCookieJar()
    bots.set("bm_sv", "1", domain="example.com")
    _injected(bots)
    assert "skip_bot_cookies=False" in warnings[0]["hint"]
    assert "log in again" not in warnings[0]["hint"]


def test_injecting_skips_expired_cookies() -> None:
    from graftpunk.session import inject_cookies_to_nodriver

    jar = requests.cookies.RequestsCookieJar()
    jar.set("live", "1", domain="example.com", expires=int(time.time()) + 3600)
    jar.set("past", "1", domain="example.com", expires=int(time.time()) - 3600)
    jar.set("zero", "1", domain="example.com", expires=0)
    jar.set("session", "1", domain="example.com")
    sent: list[dict[str, Any]] = []

    class _Tab:
        async def send(self, command: Any) -> None:
            sent.extend(next(command)["params"]["cookies"])

    injected, skipped = asyncio.run(inject_cookies_to_nodriver(_Tab(), jar))

    assert sorted(cookie["name"] for cookie in sent) == ["live", "session"]
    assert (injected, skipped) == (2, 2)


def test_injecting_captured_cookies_gives_the_browser_the_host_that_set_them() -> None:
    from graftpunk.session import inject_cookies_to_nodriver

    session = _captured_by_nodriver(
        [
            _dotless("lh", "localhost"),
            _dotless("v6", "[::1]"),
            _dotless("dotless", "printer"),
            _dotless("mdns", "printer.local"),
            _dotless("dom", ".example.com"),
        ]
    )
    sent: list[dict[str, Any]] = []

    class _Tab:
        async def send(self, command: Any) -> None:
            sent.extend(next(command)["params"]["cookies"])

    asyncio.run(inject_cookies_to_nodriver(_Tab(), session.cookies))

    assert {cookie["name"]: cookie["domain"] for cookie in sent} == {
        "lh": "localhost",
        "v6": "[::1]",
        "dotless": "printer",
        "mdns": "printer.local",
        "dom": ".example.com",
    }


def test_a_cached_sessions_cookie_domains_name_the_hosts_that_set_them() -> None:
    session = _captured_by_nodriver(
        [_dotless("lh", "localhost"), _dotless("v6", "[::1]"), _dotless("p", "printer")]
    )

    metadata = cache._extract_session_metadata(session, "local")

    assert metadata["cookie_domains"] == ["[::1]", "localhost", "printer"]
    assert metadata["domain"] == "[::1]"


def test_gp_session_show_names_the_dotless_host_a_cookie_was_captured_from(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    from tests.unit.test_cache import _setup_local_env

    _setup_local_env(tmp_path, monkeypatch)
    session = GraftpunkSession()
    session.cookies = _captured_by_nodriver([_dotless("tok", "printer")]).cookies
    cache.cache_session(session, "printer")

    result = CliRunner().invoke(session_app, ["show", "printer"], terminal_width=200)

    assert result.exit_code == 0, result.output
    shown = {
        label: value.split()[0]
        for line in result.output.splitlines()
        if ":" in line
        for label, value in [line.strip("│ ").split(":", 1)]
    }
    assert shown["Domain"] == "printer"
    assert shown["Domains"] == "printer"


# A host without a dot and the mDNS name with ".local" added are both stored
# under "<host>.local"; the host each was captured from keeps them apart.
_DOTLESS_AND_MDNS = [("printer", "http://printer/"), ("printer.local", "http://printer.local/")]


@pytest.mark.parametrize("capture", sorted(_CAPTURE_PATHS))
@pytest.mark.parametrize(("host", "url"), _DOTLESS_AND_MDNS)
def test_a_captured_cookie_for_a_dotless_host_or_its_local_name_goes_to_that_host_alone(
    capture: str, host: str, url: str
) -> None:
    session = _CAPTURE_PATHS[capture]([_dotless("c", host)])

    assert {u: _cookie_header(session, u) for _, u in _DOTLESS_AND_MDNS} == {
        u: "c=1" if u == url else None for _, u in _DOTLESS_AND_MDNS
    }


@pytest.mark.parametrize("capture", sorted(_CAPTURE_PATHS))
@pytest.mark.parametrize(("host", "url"), _DOTLESS_AND_MDNS)
def test_a_servers_refresh_keeps_a_dotless_or_local_cookie_to_its_own_host(
    capture: str, host: str, url: str
) -> None:
    session = _CAPTURE_PATHS[capture]([_dotless("c", host)])
    headers = email.message.Message()
    headers["Set-Cookie"] = "c=NEW; Path=/"
    response = types.SimpleNamespace(info=lambda: headers)
    session.cookies.extract_cookies(response, urllib.request.Request(url))  # noqa: S310

    assert [cookie.value for cookie in session.cookies] == ["NEW"]
    assert {u: _cookie_header(session, u) for _, u in _DOTLESS_AND_MDNS} == {
        u: "c=NEW" if u == url else None for _, u in _DOTLESS_AND_MDNS
    }
    sent: list[dict[str, Any]] = []

    class _Tab:
        async def send(self, command: Any) -> None:
            sent.extend(next(command)["params"]["cookies"])

    from graftpunk.session import inject_cookies_to_nodriver

    asyncio.run(inject_cookies_to_nodriver(_Tab(), session.cookies))
    assert [cookie["domain"] for cookie in sent] == [host]


def test_a_captured_host_is_stored_lowercase_and_matched_whatever_its_case() -> None:
    session = _captured_by_nodriver([_dotless("c", "PrinTer")])

    assert [cookie.domain for cookie in session.cookies] == ["printer.local"]
    assert _cookie_header(session, "http://PRINTER/") == "c=1"
    assert _cookie_header(session, "http://printer.local/") is None


_JAR_COPIES: dict[str, Callable[[Any], Any]] = {
    "pickle": lambda jar: dill.loads(dill.dumps(jar)),  # noqa: S301
    "RequestsCookieJar.copy": lambda jar: jar.copy(),
}


@pytest.mark.parametrize("copy_jar", sorted(_JAR_COPIES))
def test_a_cookies_setting_host_survives_the_cache_and_a_jar_copy(copy_jar: str) -> None:
    session = GraftpunkSession()
    session.cookies = _JAR_COPIES[copy_jar](
        _captured_by_nodriver([_dotless("c", "printer")]).cookies
    )

    assert _cookie_header(session, "http://printer/") == "c=1"
    assert _cookie_header(session, "http://printer.local/") is None


def test_a_host_only_cookie_without_a_recorded_host_keeps_the_earlier_rule() -> None:
    """A session cached by an earlier release holds such cookies, stored by
    the standard library under "<host>.local"."""
    from graftpunk.session import inject_cookies_to_nodriver

    session = GraftpunkSession()
    for name, domain in (("lh", "localhost.local"), ("p", "printer.local")):
        session.cookies.set_cookie(
            cookiejar.Cookie(
                0, name, "1", None, False, domain, False, False, "/", True,
                False, None, True, None, None, {},
            )
        )  # fmt: skip

    assert _cookie_header(session, "http://localhost:8080/") == "lh=1"
    assert _cookie_header(session, "http://printer/") == "p=1"
    assert _cookie_header(session, "http://printer.local/") == "p=1"
    sent: list[dict[str, Any]] = []

    class _Tab:
        async def send(self, command: Any) -> None:
            sent.extend(next(command)["params"]["cookies"])

    asyncio.run(inject_cookies_to_nodriver(_Tab(), session.cookies))
    assert {cookie["name"]: cookie["domain"] for cookie in sent} == {
        "lh": "localhost",
        "p": "printer.local",
    }


def test_a_cookie_a_server_sets_through_requests_goes_to_that_host_alone(
    cookie_server: int,
) -> None:
    session = GraftpunkSession()
    session.get(f"http://localhost:{cookie_server}/set", timeout=10)

    assert session.get(f"http://localhost:{cookie_server}/echo", timeout=10).text == (
        "sec=1; plain=1"
    )
    # localhost.local is where the standard library files localhost's cookies.
    assert _cookie_header(session, "http://localhost.local/") is None
    assert session.get(f"http://127.0.0.1:{cookie_server}/echo", timeout=10).text == ""


class _ForgingProxy(BaseHTTPRequestHandler):
    """An HTTP proxy that answers every request itself, with a host-only cookie
    whose graftpunk-host attribute names the host in the ``forge`` query."""

    def do_GET(self) -> None:
        forged = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)["forge"][0]
        self.send_response(200)
        self.send_header("Set-Cookie", f"h=1; Path=/; graftpunk-host={forged}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def forging_proxy() -> Iterator[int]:
    yield from _serve(ThreadingHTTPServer(("127.0.0.1", 0), _ForgingProxy))


def _forged_response(proxy: int, session: requests.Session, url: str, forged: str) -> Any:
    session.trust_env = False
    proxies = {"http": f"http://127.0.0.1:{proxy}"}
    return session.get(f"{url}?forge={forged}", proxies=proxies, timeout=10)


def _forged_from_response_cookies(proxy: int, url: str, forged: str) -> requests.Session:
    # response.cookies always has the standard library's policy, which keeps
    # an attribute it does not know; merging it is a plugin idiom.
    response = _forged_response(proxy, requests.Session(), url, forged)
    session = GraftpunkSession()
    session.cookies.update(response.cookies)
    return session


def _forged_from_an_old_cache(proxy: int, url: str, forged: str) -> requests.Session:
    # A session cached by 1.21.0 extracted its cookies under HTTPie's policy.
    old = requests.Session()
    old.cookies.set_policy(_httpie_policy())
    _forged_response(proxy, old, url, forged)
    key = Fernet.generate_key()
    return cache.load_session_for_api_from_bytes(Fernet(key).encrypt(dill.dumps(old)), key=key)


_FORGED_PATHS: dict[str, Callable[[int, str, str], requests.Session]] = {
    "response.cookies": _forged_from_response_cookies,
    "1.21.0 cache": _forged_from_an_old_cache,
}


def _injected_domains(jar: Any) -> list[str]:
    from graftpunk.session import inject_cookies_to_nodriver

    sent: list[dict[str, Any]] = []

    class _Tab:
        async def send(self, command: Any) -> None:
            sent.extend(next(command)["params"]["cookies"])

    asyncio.run(inject_cookies_to_nodriver(_Tab(), jar))
    return [cookie["domain"] for cookie in sent]


@pytest.mark.parametrize("path", sorted(_FORGED_PATHS))
def test_a_graftpunk_host_a_server_sent_does_not_move_a_cookie_to_a_subdomain(
    forging_proxy: int, path: str
) -> None:
    session = _FORGED_PATHS[path](forging_proxy, "http://example.com/", "sub.example.com")

    assert [c.get_nonstandard_attr("graftpunk-host") for c in session.cookies] == [
        "sub.example.com"
    ]
    assert _cookie_header(session, "http://example.com/") == "h=1"
    assert _cookie_header(session, "http://sub.example.com/") is None


@pytest.mark.parametrize("path", sorted(_FORGED_PATHS))
def test_a_graftpunk_host_a_server_sent_names_neither_the_browsers_nor_the_caches_domain(
    forging_proxy: int, path: str
) -> None:
    session = _FORGED_PATHS[path](forging_proxy, "http://evil.example/", "bank.example")

    assert _injected_domains(session.cookies) == ["evil.example"]
    metadata = cache._extract_session_metadata(session, "forged")
    assert metadata["cookie_domains"] == ["evil.example"]
    assert metadata["domain"] == "evil.example"


def test_a_graftpunk_host_a_server_sent_can_pick_only_between_a_host_and_its_local_name(
    forging_proxy: int,
) -> None:
    """Both are stored as printer.local, the ambiguity the attribute settles."""
    session = _forged_from_response_cookies(forging_proxy, "http://printer/", "printer.local")

    assert _cookie_header(session, "http://printer.local/") == "h=1"
    assert _cookie_header(session, "http://printer/") is None


def test_credentials_in_the_url_stay_out_of_the_recorded_host_and_the_domains_graftpunk_gives(
    cookie_server: int,
) -> None:
    session = GraftpunkSession()
    base = f"http://user:pw@localhost:{cookie_server}"
    session.get(f"{base}/set", timeout=10)

    # The standard library stores the domain with the credentials in it.
    assert {c.domain for c in session.cookies} == {"user:pw@localhost.local"}
    assert {c.get_nonstandard_attr("graftpunk-host") for c in session.cookies} == {"localhost"}
    assert session.get(f"{base}/echo", timeout=10).text == "sec=1; plain=1"
    assert _injected_domains(session.cookies) == ["localhost", "localhost"]
    assert cache._extract_session_metadata(session, "creds")["cookie_domains"] == ["localhost"]

    # Cookies a stock requests session extracted record no host.
    stock = requests.Session()
    stock.get(f"{base}/set", timeout=10)
    unrecorded = GraftpunkSession()
    unrecorded.cookies.update(stock.cookies)
    assert _injected_domains(unrecorded.cookies) == ["localhost", "localhost"]
    assert cache._extract_session_metadata(unrecorded, "c")["cookie_domains"] == ["localhost"]


def test_cache_metadata_takes_a_custom_sessions_cookie_domains_as_they_are() -> None:
    session = types.SimpleNamespace(cookies=[types.SimpleNamespace(name="a", domain="x.example")])

    assert cache._extract_session_metadata(session, "custom")["cookie_domains"] == ["x.example"]


def test_the_public_names_are_the_ones_the_changelog_lists() -> None:
    namespace: dict[str, Any] = {}
    exec("from graftpunk.cookies import *", namespace)  # noqa: S102

    assert sorted(name for name in namespace if name != "__builtins__") == [
        "BrowserCookiePolicy",
        "BrowserCookieSession",
        "BrowserCookiesMixin",
        "cookie_from_browser",
        "importing_httpie",
        "use_browser_cookie_policy",
    ]
