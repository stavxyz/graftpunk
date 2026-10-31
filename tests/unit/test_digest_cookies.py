"""The markdown digest's Cookies section: one line per cookie with the attributes
the recording shows, never a value (#315)."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from graftpunk.har.digest import DigestSource, RunDigest, digest
from graftpunk.har.report import render_endpoints_json, render_json, render_markdown
from tests.unit.test_har_digest import _entry, _write_har

_HOST = "https://myshop.example"
# Distinctive, so its absence from every output is meaningful.
_VALUE = "Zq9vLm2xKeptOut7"


def _digest(tmp_path: Path, entries: list[dict]) -> RunDigest:
    return digest(DigestSource.from_har(_write_har(tmp_path, entries)))


def _cookie_lines(d: RunDigest) -> list[str]:
    text = render_markdown(d)
    section = text.split("## Cookies\n", 1)[1].split("\n\n", 1)[0]
    return section.split("\n")


@pytest.fixture
def lines_of(tmp_path: Path) -> Callable[[list[dict]], list[str]]:
    """The Cookies section's lines for a recording of *entries*, after holding
    that no output of its digest carries the cookie value."""

    def run(entries: list[dict]) -> list[str]:
        d = _digest(tmp_path, entries)
        for text in (render_markdown(d), render_json(d), render_endpoints_json(d)):
            assert _VALUE not in text
        return _cookie_lines(d)

    return run


def _login(*set_cookies: str) -> list[dict]:
    return [_entry("POST", f"{_HOST}/login", set_cookies=list(set_cookies))]


@pytest.mark.parametrize(
    ("set_cookie", "attributes"),
    [
        (
            f"sid={_VALUE}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=3600",
            "HttpOnly=yes Secure=yes SameSite=Lax Path=/ expiry=persistent",
        ),
        (f"sid={_VALUE}", "HttpOnly=no Secure=no SameSite=- Path=- expiry=session"),
        (
            f"sid={_VALUE}; SameSite=Strict",
            "HttpOnly=no Secure=no SameSite=Strict Path=- expiry=session",
        ),
        (
            f"sid={_VALUE}; SameSite=None; Secure",
            "HttpOnly=no Secure=yes SameSite=None Path=- expiry=session",
        ),
        (
            f"sid={_VALUE}; Path=/account",
            "HttpOnly=no Secure=no SameSite=- Path=/account expiry=session",
        ),
        (
            f"sid={_VALUE}; Expires=Wed, 21 Oct 2037 07:28:00 GMT",
            "HttpOnly=no Secure=no SameSite=- Path=- expiry=persistent",
        ),
        (f"sid={_VALUE}; Max-Age=0", "HttpOnly=no Secure=no SameSite=- Path=- expiry=past"),
        (
            "sid=; Expires=Thu, 01 Jan 1970 00:00:00 GMT",
            "HttpOnly=no Secure=no SameSite=- Path=- expiry=past",
        ),
        # Max-Age wins over Expires (RFC 6265).
        (
            f"sid={_VALUE}; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=60",
            "HttpOnly=no Secure=no SameSite=- Path=- expiry=persistent",
        ),
    ],
    ids=[
        "all-set",
        "none-set",
        "same-site-strict",
        "same-site-none",
        "path",
        "expires-later",
        "max-age-zero",
        "expires-past",
        "max-age-wins",
    ],
)
def test_each_attribute_is_printed_in_its_words(lines_of, set_cookie: str, attributes: str) -> None:
    assert lines_of(_login(set_cookie)) == [f"- `sid`: set by POST /login: {attributes}"]


def test_attribute_names_are_matched_in_any_case(lines_of) -> None:
    lines = lines_of(_login(f"sid={_VALUE}; httponly; SECURE; samesite=lax; PATH=/; max-AGE=5"))
    assert lines == [
        "- `sid`: set by POST /login: HttpOnly=yes Secure=yes SameSite=Lax Path=/ expiry=persistent"
    ]


def test_a_path_holding_an_id_is_templated_as_an_endpoint_path_is(lines_of) -> None:
    lines = lines_of(_login(f"sid={_VALUE}; Path=/orders/48213"))
    assert lines == [
        "- `sid`: set by POST /login: HttpOnly=no Secure=no SameSite=- "
        "Path=/orders/{order_id} expiry=session"
    ]


@pytest.mark.parametrize(
    "set_cookie",
    [
        f"sid={_VALUE}; SameSite=Sometimes; Path=account; Max-Age=soon",
        f"sid={_VALUE}; SameSite; Path=relative/; Expires=not a date",
    ],
)
def test_a_malformed_attribute_reads_as_unknown(lines_of, set_cookie: str) -> None:
    assert lines_of(_login(set_cookie)) == [
        "- `sid`: set by POST /login: HttpOnly=no Secure=no SameSite=unknown Path=unknown "
        "expiry=unknown"
    ]


def test_a_malformed_set_cookie_line_does_not_fail_the_digest(lines_of) -> None:
    """A line with no ``=`` before its first ``;`` sets no cookie, a nameless one
    is skipped, and empty attribute parts are ignored."""
    lines = lines_of(_login(f";;; =x; HttpOnly\nnoequals; Secure\n=v{_VALUE}\nsid={_VALUE};;; ;=;"))
    assert lines == [
        "- `sid`: set by POST /login: HttpOnly=no Secure=no SameSite=- Path=- expiry=session"
    ]


def test_a_cookie_only_sent_says_so(lines_of) -> None:
    entries = _login(f"sid={_VALUE}; HttpOnly")
    entries.append(
        _entry(
            "GET", f"{_HOST}/account", request_headers={"Cookie": f"pref={_VALUE}; sid={_VALUE}"}
        )
    )
    assert lines_of(entries) == [
        "- `sid`: set by POST /login: HttpOnly=yes Secure=no SameSite=- Path=- expiry=session",
        "- `pref`: sent in requests and never set in this recording",
    ]


def test_a_cookie_set_by_several_endpoints_names_each_once(lines_of) -> None:
    entries = [
        *_login(f"sid={_VALUE}; HttpOnly; Path=/"),
        _entry("GET", f"{_HOST}/account", set_cookies=[f"sid={_VALUE}; HttpOnly; Path=/"]),
        *_login(f"sid={_VALUE}; HttpOnly; Path=/"),
    ]
    assert lines_of(entries) == [
        "- `sid`: set by POST /login, GET /account: HttpOnly=yes Secure=no SameSite=- "
        "Path=/ expiry=session"
    ]


def test_conflicting_attribute_sets_are_each_printed_with_their_endpoints(lines_of) -> None:
    entries = [
        _entry("GET", f"{_HOST}/", set_cookies=[f"cart={_VALUE}; Path=/"]),
        _entry("POST", f"{_HOST}/login", set_cookies=[f"cart={_VALUE}; Path=/; HttpOnly; Secure"]),
        _entry("POST", f"{_HOST}/logout", set_cookies=["cart=; Path=/; Max-Age=0"]),
        _entry("GET", f"{_HOST}/cart", set_cookies=[f"cart={_VALUE}; Path=/"]),
    ]
    assert lines_of(entries) == [
        "- `cart`: set by GET /, GET /cart: HttpOnly=no Secure=no SameSite=- Path=/ "
        "expiry=session; set by POST /login: HttpOnly=yes Secure=yes SameSite=- Path=/ "
        "expiry=session; set by POST /logout: HttpOnly=no Secure=no SameSite=- Path=/ "
        "expiry=past"
    ]


def test_a_foreign_har_shape_is_read(lines_of) -> None:
    """Another recorder's HAR: several ``set-cookie`` headers in lower case on one
    entry, one of them two lines joined, and a ``response.cookies`` array that
    holds the header's cookies (paired by position, so the header wins) and one
    the headers do not carry, whose missing flags are unknown."""
    entry = _entry("POST", f"{_HOST}/login")
    entry["response"]["headers"] += [
        {"name": "set-cookie", "value": f"a={_VALUE}; secure; httponly"},
        {"name": "set-cookie", "value": f"b={_VALUE}; samesite=strict\nc={_VALUE}; path=/"},
    ]
    entry["response"]["cookies"] = [
        {"name": "a", "value": _VALUE, "httpOnly": False, "secure": False},
        {"name": "b", "value": _VALUE},
        {"name": "c", "value": _VALUE},
        {"name": "d", "value": _VALUE, "secure": True, "sameSite": "Lax", "path": "/"},
    ]
    entry["request"]["cookies"] = [{"name": "e", "value": _VALUE}]
    assert lines_of([entry]) == [
        "- `a`: set by POST /login: HttpOnly=yes Secure=yes SameSite=- Path=- expiry=session",
        "- `b`: set by POST /login: HttpOnly=no Secure=no SameSite=Strict Path=- expiry=session",
        "- `c`: set by POST /login: HttpOnly=no Secure=no SameSite=- Path=/ expiry=session",
        "- `d`: set by POST /login: HttpOnly=unknown Secure=yes SameSite=Lax Path=/ expiry=unknown",
        "- `e`: sent in requests and never set in this recording",
    ]


def test_a_name_holding_an_account_value_is_left_out(lines_of) -> None:
    entries = _login(f"sid={_VALUE}", f"pref_alice@example.com={_VALUE}")
    entries.append(
        _entry(
            "GET",
            f"{_HOST}/account",
            request_headers={"Cookie": f"seen_4f9c2a7e1b3d5f60a8c4={_VALUE}"},
        )
    )
    lines = lines_of(entries)
    assert lines == [
        "- `sid`: set by POST /login: HttpOnly=no Secure=no SameSite=- Path=- expiry=session"
    ]


def test_no_cookie_prints_none(lines_of) -> None:
    assert lines_of([_entry("GET", f"{_HOST}/account")]) == ["(none)"]


def test_a_digest_without_facts_prints_the_names(tmp_path: Path) -> None:
    """A RunDigest built by hand, with ``cookies`` and no ``cookie_facts``."""
    d = dataclasses.replace(_digest(tmp_path, _login(f"sid={_VALUE}")), cookie_facts=())
    assert _cookie_lines(d) == ["- `sid`"]


def test_the_json_outputs_carry_no_cookie_facts(tmp_path: Path) -> None:
    """--json and --endpoints-json are as they were: ``cookie_facts`` is the
    markdown section's alone, and the projection carries no cookie names."""
    d = _digest(tmp_path, _login(f"sid={_VALUE}; HttpOnly"))
    assert "cookie_facts" not in json.loads(render_json(d))
    assert "sid" not in render_endpoints_json(d)
