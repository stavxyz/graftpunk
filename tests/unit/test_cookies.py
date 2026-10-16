"""graftpunk sessions send the cookies Chrome would send (#253)."""

from __future__ import annotations

import os
import pathlib
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from http import cookiejar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dill
import pytest
import requests
from cryptography.fernet import Fernet
from typer.testing import CliRunner

from graftpunk import cache
from graftpunk.cli.session_commands import session_app
from graftpunk.cookies import (
    LOCALHOST_SUFFIX,
    LOOPBACK_HOSTS,
    BrowserCookiePolicy,
    BrowserCookieSession,
    is_secure_loopback_host,
    use_browser_cookie_policy,
)
from graftpunk.graftpunk_session import GraftpunkSession
from tests.unit.test_lite_cli_hints import _cache_fixture_session

_FIX = pathlib.Path(__file__).parent.parent / "fixtures"


def test_the_secure_hosts_are_the_ones_chrome_was_measured_on() -> None:
    """Chrome 155 sent a Secure cookie over plain HTTP to exactly these
    (2026-10-16). Widening the set needs a new measurement."""
    assert frozenset({"localhost", "127.0.0.1", "::1"}) == LOOPBACK_HOSTS
    assert LOCALHOST_SUFFIX == ".localhost"


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
    assert is_secure_loopback_host(host) is secure


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

    http = pathlib.Path(sys.executable).parent / "http"
    sent = subprocess.run(  # noqa: S603
        [
            str(http),
            "--offline",
            "--ignore-stdin",
            f"--session={target}",
            "GET",
            "http://localhost/x",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "HOME": str(tmp_path / "home")},
    )

    assert sent.returncode == 0, sent.stderr
    assert "Cookie: User=dummyuser; Password=dummypass" in sent.stdout.splitlines(), sent.stdout


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
