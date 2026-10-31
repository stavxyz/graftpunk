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
        "- `pref`: sent in requests and never set by a response the digest kept",
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
        "- `e`: sent in requests and never set by a response the digest kept",
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
        "- `sid`: set by POST /login: HttpOnly=no Secure=no SameSite=- Path=- expiry=session",
        "- 2 cookies whose names read as values are not shown",
    ]


def test_no_cookie_prints_none(lines_of) -> None:
    assert lines_of([_entry("GET", f"{_HOST}/account")]) == ["(none)"]


def test_a_digest_without_facts_prints_the_names(tmp_path: Path) -> None:
    """A RunDigest built by hand, with ``cookies`` and no ``cookie_facts``."""
    d = dataclasses.replace(_digest(tmp_path, _login(f"sid={_VALUE}")), cookie_facts=())
    assert _cookie_lines(d) == ["- `sid`"]


def test_json_carries_the_cookie_facts_and_the_projection_no_cookie(tmp_path: Path) -> None:
    """--json, the complete digest, carries ``cookie_facts`` with the words the
    markdown prints; --endpoints-json carries no cookie name."""
    entries = _login(f"sid={_VALUE}; HttpOnly; Path=/")
    entries.append(_entry("GET", f"{_HOST}/account", request_headers={"Cookie": f"pref={_VALUE}"}))
    d = _digest(tmp_path, entries)
    assert json.loads(render_json(d))["cookie_facts"] == [
        {
            "name": "sid",
            "settings": [
                {
                    "expiry": "session",
                    "http_only": "yes",
                    "path": "/",
                    "same_site": "-",
                    "secure": "no",
                    "set_by": ["POST /login"],
                }
            ],
        },
        {"name": "pref", "settings": []},
    ]
    assert "sid" not in render_endpoints_json(d)
    assert "pref" not in render_endpoints_json(d)


def test_json_leaves_cookie_facts_out_of_a_run_without_a_cookie(tmp_path: Path) -> None:
    d = _digest(tmp_path, [_entry("GET", f"{_HOST}/account")])
    assert "cookie_facts" not in json.loads(render_json(d))


# A short random token sent as a nameless cookie's bare value reaches a foreign
# HAR's request.cookies as a name with an empty value; holds_an_id keeps it, and
# the name gate does not.
_VALUE_AS_NAME = "SECRETVALUEq7"


def test_a_name_that_reads_as_a_value_prints_as_a_count(tmp_path: Path) -> None:
    """The name gate har-facts.py prints through (printable_name) holds back a
    name, set or only sent, from the markdown and --json, and counts it."""
    entries = _login(f"sid={_VALUE}", f"uid_SAMCOOKIE_X={_VALUE}")
    account = _entry("GET", f"{_HOST}/account")
    account["request"]["cookies"] = [{"name": _VALUE_AS_NAME, "value": ""}]
    entries.append(account)
    d = _digest(tmp_path, entries)
    assert _cookie_lines(d) == [
        "- `sid`: set by POST /login: HttpOnly=no Secure=no SameSite=- Path=- expiry=session",
        "- 2 cookies whose names read as values are not shown",
    ]
    rendered = json.loads(render_json(d))
    assert rendered["cookies"] == ["sid"]
    assert [fact["name"] for fact in rendered["cookie_facts"]] == ["sid"]
    assert rendered["cookie_names_not_shown"] == 2
    for text in (render_markdown(d), render_json(d), render_endpoints_json(d)):
        assert _VALUE_AS_NAME not in text
        assert "SAMCOOKIE" not in text
    # The fixture safety net still holds every name the digest saw.
    assert "uid_SAMCOOKIE_X" in d.cookies


def test_one_name_held_back_reads_in_the_singular(lines_of) -> None:
    entries = _login(f"sid={_VALUE}")
    account = _entry("GET", f"{_HOST}/account")
    account["request"]["cookies"] = [{"name": _VALUE_AS_NAME, "value": ""}]
    entries.append(account)
    assert lines_of(entries)[-1] == "- 1 cookie whose name reads as a value is not shown"


def test_json_leaves_the_count_out_when_every_name_prints(tmp_path: Path) -> None:
    d = _digest(tmp_path, _login(f"sid={_VALUE}"))
    assert "cookie_names_not_shown" not in json.loads(render_json(d))


# Text a Path can carry that is no part of a path: a second cookie folded into the
# header with a comma, a query, a fragment, params, a space, and quotes. Lower
# case, so the endpoint templating alone would keep it.
@pytest.mark.parametrize(
    ("path", "word"),
    [
        ("/, cart_owner=bobsmith", "bobsmith"),
        ("/cb?next=home", "home"),
        ("/x#frag", "frag"),
        ("/a=dusk", "dusk"),
        ("/a lemon", "lemon"),
        ('/"quoted"', "quoted"),
    ],
)
def test_a_path_that_is_not_a_plain_path_is_not_shown(tmp_path: Path, path: str, word: str) -> None:
    entries = _login(f"sid={_VALUE}; Path={path}")
    array_only = _entry("POST", f"{_HOST}/session")
    array_only["response"]["cookies"] = [{"name": "pref", "value": _VALUE, "path": path}]
    entries.append(array_only)
    d = _digest(tmp_path, entries)
    assert _cookie_lines(d) == [
        "- `sid`: set by POST /login: HttpOnly=no Secure=no SameSite=- Path=(not shown) "
        "expiry=session",
        "- `pref`: set by POST /session: HttpOnly=unknown Secure=unknown SameSite=unknown "
        "Path=(not shown) expiry=unknown",
    ]
    assert word not in "\n".join(_cookie_lines(d))
    assert word not in json.dumps(json.loads(render_json(d))["cookie_facts"])


def test_an_array_path_holding_params_is_not_shown(lines_of) -> None:
    """A header line's ``;`` ends the Path; an array entry's ``path`` can hold one."""
    assert lines_of(_array_cookie(path="/a;theme=dark")) == [
        "- `sid`: set by POST /logout: HttpOnly=unknown Secure=unknown SameSite=unknown "
        "Path=(not shown) expiry=unknown"
    ]


def test_a_path_inside_a_collapsed_family_prints_the_familys_template(lines_of) -> None:
    """Ten slugs collapse ``/p/<slug>/reviews`` into one family; a Path that is
    a leading part of one of its members prints as the family template's."""
    slugs = ["alice2", "bobby7", "carla3", "dario5", "elena8", "fiona4", "gavin6",
             "hanna9", "ivano1", "julia2"]  # fmt: skip
    entries = [_entry("GET", f"{_HOST}/p/{slug}/reviews") for slug in slugs]
    entries.append(
        _entry("GET", f"{_HOST}/p/alice2/reviews", set_cookies=[f"pref={_VALUE}; Path=/p/alice2"])
    )
    entries.append(
        _entry("GET", f"{_HOST}/p/bobby7/reviews", set_cookies=[f"seen={_VALUE}; Path=/p/bobby7/"])
    )
    lines = lines_of(entries)
    assert lines == [
        "- `pref`: set by GET /p/{p_id}/reviews: HttpOnly=no Secure=no SameSite=- "
        "Path=/p/{p_id} expiry=session",
        "- `seen`: set by GET /p/{p_id}/reviews: HttpOnly=no Secure=no SameSite=- "
        "Path=/p/{p_id}/ expiry=session",
    ]
    assert not any(slug in line for line in lines for slug in slugs)


def _array_cookie(**fields: object) -> list[dict]:
    entry = _entry("POST", f"{_HOST}/logout")
    entry["response"]["cookies"] = [{"name": "sid", "value": "", **fields}]
    return [entry]


@pytest.mark.parametrize(
    ("expires", "expiry"),
    [
        ("1970-01-01T00:00:00.000Z", "past"),
        ("2037-10-21T07:28:00.000Z", "persistent"),
        ("Thu, 01 Jan 1970 00:00:00 GMT", "past"),
        ("Wed, 21 Oct 2037 07:28:00 GMT", "persistent"),
        ("not a date", "unknown"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_an_array_only_expires_is_compared_with_the_request(
    lines_of, expires: object, expiry: str
) -> None:
    """HAR 1.2 gives ``expires`` as ISO 8601; gp's recorder copies the header's
    HTTP date. Either is compared with when the request was sent."""
    fields = {} if expires is None else {"expires": expires}
    assert lines_of(_array_cookie(httpOnly=True, **fields)) == [
        "- `sid`: set by POST /logout: HttpOnly=yes Secure=unknown SameSite=unknown "
        f"Path=unknown expiry={expiry}"
    ]


@pytest.mark.parametrize(
    ("set_cookie", "expiry"),
    [
        ("sid=; Expires=Thu, 01 Jan 1970 00:00:00 GMT", "unknown"),
        ("sid=; Expires=Wed, 21 Oct 2037 07:28:00 GMT", "unknown"),
        ("sid=; Max-Age=0; Expires=Wed, 21 Oct 2037 07:28:00 GMT", "past"),
        ("sid=; Max-Age=60", "persistent"),
        ("sid=", "session"),
    ],
)
def test_without_a_request_time_expires_is_unknown_and_max_age_decides(
    lines_of, set_cookie: str, expiry: str
) -> None:
    entries = [_entry("POST", f"{_HOST}/logout", set_cookies=[set_cookie], started="garbled")]
    entries[0]["response"]["cookies"] = [
        {"name": "arr", "value": "", "expires": "1970-01-01T00:00:00.000Z"}
    ]
    assert lines_of(entries) == [
        "- `arr`: set by POST /logout: HttpOnly=unknown Secure=unknown SameSite=unknown "
        "Path=unknown expiry=unknown",
        f"- `sid`: set by POST /logout: HttpOnly=no Secure=no SameSite=- Path=- expiry={expiry}",
    ]


@pytest.mark.parametrize("same_site", ["SameSite=", "SameSite", "SameSite=  "])
def test_an_empty_or_bare_same_site_is_unknown(lines_of, same_site: str) -> None:
    assert lines_of(_login(f"sid={_VALUE}; {same_site}")) == [
        "- `sid`: set by POST /login: HttpOnly=no Secure=no SameSite=unknown Path=- expiry=session"
    ]


def test_an_empty_array_same_site_is_unknown(lines_of) -> None:
    assert lines_of(_array_cookie(sameSite="", path="", secure=False)) == [
        "- `sid`: set by POST /logout: HttpOnly=unknown Secure=no SameSite=unknown Path=- "
        "expiry=unknown"
    ]


def test_a_cookie_a_dropped_response_set_is_only_sent_by_a_kept_one(lines_of) -> None:
    """A static response the digest leaves out set the cookie; the line says
    what the digest knows, not that the recording never set it."""
    static = _entry(
        "GET",
        f"{_HOST}/static/app.js",
        content_type="application/javascript",
        set_cookies=[f"bm_check={_VALUE}; Path=/; HttpOnly"],
    )
    account = _entry("GET", f"{_HOST}/account", request_headers={"Cookie": f"bm_check={_VALUE}"})
    assert lines_of([static, account]) == [
        "- `bm_check`: sent in requests and never set by a response the digest kept"
    ]
