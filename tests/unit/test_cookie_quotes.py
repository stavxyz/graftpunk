"""A cookie value with quotes is kept exactly as the browser held it wherever
graftpunk copies cookies from one jar to another: the login's transfer from the
browser, the write-back, and the HTTPie export.

``requests.cookies.RequestsCookieJar.set_cookie`` deletes every ``\\"`` from a
value that starts and ends with ``"``, and its ``update`` and ``copy`` go
through it; Chrome keeps and sends such a value as it is.
"""

from __future__ import annotations

import asyncio
import json
import types
from http import cookiejar
from pathlib import Path
from typing import Any

import pytest
import requests

from graftpunk import cache
from graftpunk.cookies import _set_cookie_as_is, _update_jar_as_is, cookie_from_browser
from graftpunk.graftpunk_session import GraftpunkSession
from tests.unit.session_compat_cases import TEST_KEY
from tests.unit.test_session_record import QUOTED_COOKIE_VALUES


def _cookie(name: str, value: str) -> cookiejar.Cookie:
    return cookie_from_browser(
        {"name": name, "value": value, "domain": ".myshop.example", "path": "/", "secure": True}
    )


def _values(jar: Any) -> dict[str, str]:
    return {c.name: c.value for c in jar}


def _expected() -> dict[str, str]:
    return {f"c{i}": v for i, v in enumerate(QUOTED_COOKIE_VALUES)}


def test_the_helpers_keep_each_value() -> None:
    source = requests.cookies.RequestsCookieJar()
    for name, value in _expected().items():
        _set_cookie_as_is(source, _cookie(name, value))
    dest = requests.cookies.RequestsCookieJar()
    _update_jar_as_is(dest, source)

    assert _values(source) == _expected()
    assert _values(dest) == _expected()
    # Copies: the destination never holds the source's Cookie objects.
    assert not {id(c) for c in dest} & {id(c) for c in source}


@pytest.mark.parametrize("fmt", ["pickle", "record"])
def test_a_write_back_keeps_each_value(fmt: str, session_store: Any, session_format: Any) -> None:
    session_format(fmt)
    stored = GraftpunkSession()
    for name, value in _expected().items():
        _set_cookie_as_is(stored.cookies, _cookie(name, value))
    cache.cache_session(stored, "myshop", key=TEST_KEY)

    api = cache.load_session_for_api("myshop", key=TEST_KEY)
    assert _values(api.cookies) == _expected()
    cache.update_session_cookies(api, "myshop", key=TEST_KEY)

    assert session_store.format_of("myshop") == fmt
    assert _values(cache.load_session_for_api("myshop", key=TEST_KEY).cookies) == _expected()


def _browser_session() -> Any:
    from graftpunk.session import BrowserSession

    session = BrowserSession.__new__(BrowserSession)
    requests.Session.__init__(session)
    session._give_new_jar_the_browser_cookie_policy()
    return session


def _records() -> list[dict[str, Any]]:
    return [
        {"name": name, "value": value, "domain": ".myshop.example", "path": "/", "secure": True}
        for name, value in _expected().items()
    ]


def test_the_nodriver_transfer_keeps_each_value() -> None:
    cookies = [
        types.SimpleNamespace(
            name=r["name"],
            value=r["value"],
            domain=r["domain"],
            path=r["path"],
            secure=True,
            http_only=False,
            session=True,
            expires=-1,
        )
        for r in _records()
    ]

    async def get_all() -> list[Any]:
        return cookies

    session = _browser_session()
    browser = types.SimpleNamespace(cookies=types.SimpleNamespace(get_all=get_all))
    session._backend_instance = types.SimpleNamespace(_browser=browser)
    asyncio.run(session.transfer_nodriver_cookies_to_session())

    assert _values(session.cookies) == _expected()


def test_the_selenium_transfer_keeps_each_value() -> None:
    session = _browser_session()
    session._backend_type = "selenium"
    session._webdriver = types.SimpleNamespace(
        get_cookies=_records, execute_script=lambda _script: "BrowserUA/1.0"
    )
    session.transfer_driver_cookies_to_session()

    assert _values(session.cookies) == _expected()


def test_the_httpie_export_keeps_each_value(tmp_path: Path) -> None:
    session = _browser_session()
    session._backend_type = "nodriver"
    session._session_name = "myshop"
    session.current_url = "https://myshop.example/"
    for name, value in _expected().items():
        _set_cookie_as_is(session.cookies, _cookie(name, value))

    target = session.save_httpie_session(output=tmp_path / "myshop.json")

    saved = {c["name"]: c["value"] for c in json.loads(Path(target).read_text())["cookies"]}
    assert saved == _expected()


def test_the_cookie_header_requests_sends_is_still_requests_own() -> None:
    """The Cookie header is built by requests' merge (psf/requests' rule), which
    strips each \\" from a quoted value. Pinned so that a change to what goes on
    the wire is a decision, not a side effect."""
    session = GraftpunkSession()
    _set_cookie_as_is(session.cookies, _cookie("q", '"a\\"b"'))

    prepared = session.prepare_request(requests.Request("GET", "https://myshop.example/x"))

    assert prepared.headers["Cookie"] == 'q="ab"'
    assert _values(session.cookies) == {"q": '"a\\"b"'}
