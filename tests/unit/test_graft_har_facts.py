"""The graft skill's har-facts script: it answers questions about a recording's HAR
with names, attributes, shapes, and counts, prints no cookie, header, query, or body
value, and prints names only when they pass a heuristic gate."""

from __future__ import annotations

import base64
import importlib.util
import json
import random
import shutil
import string
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.unit.guide_harness import REPO_ROOT

SCRIPT = REPO_ROOT / "skills" / "graft" / "scripts" / "har-facts.py"
PYTHON = shutil.which("python3")

_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlX3ZhbHVl"
_RESET = "Zq9XkLmPwR4t7Bv2Hn8Ks3Jd"
_STRIPE = "cus_NffrFeUfNV2Hib"
_UUID = "f47ac10b-58cc-43a2-a567-0e02b2c3d4e9"

# Every value below is planted in the HAR and must never be printed.
_SECRETS = [
    "PASSWORD_SECRET",
    "alice.example@mail.example",
    "alice",
    "PRE_SECRET",
    "AUTH_SECRET",
    "SID_SECRET",
    "XSID_SECRET",
    "QTOK_SECRET",
    "ORD_SECRET_1",
    "BODY_TOKEN_SECRET",
    "NAME_SECRET",
    "PW2_SECRET",
    "FILE_SECRET",
    "CSRF_SECRET_VALUE",
    "Welcome back",
    "Victim",
    "Mapleton",
    "SVG_TITLE",
    "B64_TITLE",
    "OUTSIDE_FILE",
    "OPTION_TEXT",
    "NOTE_TEXT",
    "TYPE_ATTR",
    "ACTIONJS",
    "OAUTHCODE",
    "88812345",
    "40912873",
    _JWT.split(".", 1)[0],
    "c2lnbmF0dXJl",
    _RESET,
    "NffrFeUfNV2Hib",
    "f47ac10b",
    "dXNlcjpwYXNz",
    "PATHATTR",
    "SSATTR",
    "DOMATTR",
    "SAMCOOKIE",
    "ADDRKEY",
    "SKUPARAM",
    "ACCTPATH",
    "MPFILE",
    "PORTSECRET",
    "ACTPORT",
    "DATAURL",
    "PGgx",
    "xqzkvbtrwamplhab",
    "dark",
    "12.5",
]

_LOGIN_PAGE = (
    "<html><head><title>Welcome back, Alice Victim | Orders 88812345</title></head><body>"
    "<svg><title>SVG_TITLE alice.example@mail.example</title></svg>"
    '<form method="post" action="/session?next=QTOK_SECRET">'
    '<input name="username" value="alice">'
    '<input type="password" name="password" value="PASSWORD_SECRET">'
    '<input type="hidden" name="csrf" value="CSRF_SECRET_VALUE">'
    '<input type="TYPE_ATTR_X" name="extra">'
    '<button type="submit">Sign in as alice</button>'
    '<input id="remember" type="checkbox">'
    "</form>"
    '<form action="mailto:alice.example@mail.example"></form>'
    "<form action=\"javascript:go('ACTIONJS')\"></form>"
    '<form method="post" action="/account/ACCTPATH_X/update">'
    '<input name="qty_99887766" value="3"><input name="addr[ADDRKEY_X]" value="1">'
    '<input name="user_alice.example@mail.example" value="1">'
    '<select name="ship"><option>OPTION_TEXT</option></select>'
    '<textarea name="note">NOTE_TEXT</textarea>'
    "</form>"
    '<form action="http://shop.test:ACTPORT/x"></form>'
    "</body></html>"
)

# The real recording's shapes, with invented names and values: a saved-search id in
# the path, a place in the title, a CSRF-only search form, a field sent many times,
# and a 41-box checkbox group.
_SEARCH_PAGE = (
    "<html><head><title>Mapleton County property search</title></head><body>"
    '<form method="post" action="/search/40912873/results">'
    '<input type="hidden" name="csrfmiddlewaretoken" value="CSRF_SECRET_VALUE">'
    '<input name="search_type" value="parcel"><input name="startDate" value="2026-01-01">'
    '<input type="hidden" name="xqzkvbtrwamplhab-TOTAL_FORMS" value="1">'
    '<input name="xqzkvbtrwamplhab-0-name" value="x">'
    + "".join(f'<input type="checkbox" name="types1" value="t{n}">' for n in range(41))
    + "</form></body></html>"
)


def _h(name: str, value: str) -> dict[str, str]:
    return {"name": name, "value": value}


def _entry(
    method: str,
    url: str,
    *,
    request_headers: list[dict[str, str]] | None = None,
    request_cookies: list[dict[str, str]] | None = None,
    post: dict[str, Any] | None = None,
    status: int = 200,
    response_headers: list[dict[str, str]] | None = None,
    response_cookies: list[dict[str, Any]] | None = None,
    content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "method": method,
        "url": url,
        "headers": request_headers or [],
        "cookies": request_cookies or [],
        "queryString": [],
    }
    if post is not None:
        request["postData"] = post
    return {
        "request": request,
        "response": {
            "status": status,
            "headers": response_headers or [],
            "cookies": response_cookies or [],
            "content": content or {"mimeType": "", "text": None},
        },
    }


def _json(value: Any) -> dict[str, Any]:
    return {"mimeType": "application/json", "text": json.dumps(value)}


def _har(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    (run / "bodies").mkdir(parents=True)
    (run / "bodies" / "big.json").write_text(json.dumps({"rows": ["FILE_SECRET"]}))
    (run / "bodies" / "blob.bin").write_bytes(b"\xff\xfe\x00FILE_SECRET")
    (tmp_path / "outside.html").write_text("<title>OUTSIDE_FILE</title>")
    joined = (
        "sid=SID_SECRET; Path=/; HttpOnly; Secure; SameSite=Lax\n"
        "xsid=XSID_SECRET; Path=/; SameSite=Strict"
    )
    entries = [
        _entry(
            "GET", "https://shop.test/login", content={"mimeType": "text/html", "text": _LOGIN_PAGE}
        ),
        _entry(
            "POST",
            "https://shop.test/session",
            request_headers=[
                _h("Content-Type", "application/x-www-form-urlencoded"),
                _h("Cookie", "pre=PRE_SECRET; uid_SAMCOOKIE_X=1"),
                _h("Authorization", "Bearer AUTH_SECRET"),
                _h("X-For-alice.example@mail.example", "1"),
            ],
            request_cookies=[{"name": "pre", "value": "PRE_SECRET"}],
            post={
                "mimeType": "application/x-www-form-urlencoded",
                "text": "username=alice.example%40mail.example&password=PASSWORD_SECRET&remember=1",
            },
            status=302,
            response_headers=[_h("set-cookie", joined), _h("Location", "/orders/88812345")],
            # The array lacks httpOnly for sid, which the header carries.
            response_cookies=[
                {"name": "sid", "value": "SID_SECRET", "path": "/", "secure": True},
                {"name": "xsid", "value": "XSID_SECRET", "path": "/"},
            ],
        ),
        _entry(
            "GET",
            f"https://shop.test/api/orders?page=2&token=QTOK_SECRET&{_RESET}&code=OAUTHCODE",
            request_headers=[_h("Cookie", "sid=SID_SECRET; xsid=XSID_SECRET")],
            response_headers=[_h("Set-Cookie", "sid=SID_SECRET2; Path=/; Secure")],
            response_cookies=[{"name": "sid", "value": "SID_SECRET2", "path": "/", "secure": True}],
            content=_json(
                {
                    "orders": [{"id": "ORD_SECRET_1", "total": 12.5, "gift": False, "note": None}],
                    "page": 2,
                    "auth_token": "BODY_TOKEN_SECRET",
                    "alice.example@mail.example": {"x": 1},
                    _STRIPE: {"a": 1},
                    _UUID: 4,
                    _RESET: 3,
                    "xqzkvbtrwamplhab-TOTAL_FORMS": 1,
                }
            ),
        ),
        _entry(
            "POST",
            "https://shop.test/api/profile",
            request_headers=[_h("Referer", "https://shop.test/session")],
            post={
                "mimeType": "application/json",
                "text": json.dumps(
                    {"name": "NAME_SECRET", "password": "PW2_SECRET", "prefs": {"theme": "dark"}}
                ),
            },
        ),
        _entry(
            "GET",
            "https://shop.test/api/export",
            content={"mimeType": "application/json", "text": None, "_bodyFile": "bodies/big.json"},
        ),
        _entry(
            "GET",
            "https://shop.test/api/binary",
            content={"mimeType": "application/pdf", "_bodyFile": "bodies/blob.bin"},
        ),
        _entry(
            "GET",
            "https://shop.test/trav",
            content={"mimeType": "text/html", "_bodyFile": "../outside.html"},
        ),
        _entry(
            "GET",
            "https://shop.test/abs",
            content={"mimeType": "text/html", "_bodyFile": str(tmp_path / "outside.html")},
        ),
        _entry("GET", f"https://shop.test/users/alice.example@mail.example/reset/{_RESET}"),
        _entry("GET", f"https://shop.test/verify/{_JWT}"),
        _entry("GET", f"https://shop.test/acct/{_STRIPE}/orders/88812345"),
        _entry("GET", "https://shop.test/reset/ACCTPATH_X/confirm"),
        _entry("GET", "https://shop.test:PORTSECRET/x"),
        _entry(
            "GET",
            "data:text/html;base64,PGgxPkRBVEFVUkw8L2gxPg==",
            content={"mimeType": "text/html", "text": "<h1>DATAURL</h1>"},
        ),
        _entry(
            "GET",
            "https://shop.test/b64",
            content={
                "mimeType": "text/html",
                "encoding": "base64",
                "text": base64.b64encode(b"<title>B64_TITLE</title>").decode(),
            },
        ),
        _entry(
            "POST",
            "https://shop.test/api/blob",
            post={"mimeType": "application/x-www-form-urlencoded", "text": "dXNlcjpwYXNzQkxPQg"},
        ),
        _entry(
            "POST",
            "https://shop.test/api/params",
            post={
                "mimeType": "application/x-www-form-urlencoded",
                "params": [
                    {"name": "alice.example@mail.example", "value": "x"},
                    {"name": "cart[SKUPARAM_X]", "value": "1"},
                    {"name": "quantity", "value": "1"},
                ],
            },
        ),
        _entry(
            "POST",
            "https://shop.test/api/upload",
            post={
                "mimeType": "multipart/form-data; boundary=b",
                "text": (
                    "--b\r\nContent-Disposition: form-data;"
                    ' name="doc_for_alice.example@mail.example";'
                    ' filename="MPFILE.pdf"\r\n\r\nFILE_SECRET\r\n'
                    '--b\r\nContent-Disposition: form-data; name="title"\r\n\r\n'
                    "NAME_SECRET\r\n--b--"
                ),
            },
        ),
        _entry(
            "GET",
            "https://shop.test/setck",
            response_headers=[
                _h(
                    "Set-Cookie",
                    "a=1; Path=/acct/PATHATTR_X/88812345; Domain=DOMATTR.example; SameSite=SSATTR",
                ),
                _h("Set-Cookie", "uid_SAMCOOKIE_X=1; Path=/"),
                _h("Set-Cookie", "parent=1; Domain=.shop.test; Max-Age=60"),
                _h("Set-Cookie", "dup=1; Path=/a; HttpOnly\ndup=2; Path=/b"),
            ],
            response_cookies=[
                {"name": "dup", "value": "1", "path": "/a", "httpOnly": True},
                {"name": "dup", "value": "2", "path": "/b"},
            ],
        ),
        _entry(
            "POST",
            "https://shop.test/search/40912873/results",
            post={
                "mimeType": "application/x-www-form-urlencoded",
                "text": "csrfmiddlewaretoken=CSRF_SECRET_VALUE&"
                + "&".join(["search_type=parcel"] * 11)
                + "&startDate=2026-01-01&form-TOTAL_FORMS=1&form-0-book=7"
                + "&xqzkvbtrwamplhab-0-name=x&xqzkvbtrwamplhab-TOTAL_FORMS=1",
            },
            content={"mimeType": "text/html", "text": _SEARCH_PAGE},
        ),
    ]
    path = run / "network.har"
    path.write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}, indent=2))
    return path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    assert PYTHON is not None
    return subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [PYTHON, "-I", str(SCRIPT), *args], capture_output=True, text=True
    )


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("har_facts", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _lines(*args: str) -> list[str]:
    result = _run(*args)
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def _block(lines: list[str], start: str) -> list[str]:
    """The lines from the one equal to *start* to the next blank or unindented line."""
    index = lines.index(start)
    block = [lines[index]]
    for line in lines[index + 1 :]:
        if not line.startswith("  "):
            break
        block.append(line)
    return block


_EVERY_RUN = [
    ("cookies",),
    ("request", "/"),
    ("response", "/"),
    ("request", "data"),
    ("response", "data"),
]


@pytest.mark.parametrize("args", _EVERY_RUN, ids=lambda a: " ".join(a))
def test_no_value_from_the_har_is_printed(tmp_path: Path, args: tuple[str, ...]) -> None:
    har = _har(tmp_path)
    result = _run(args[0], str(har), *args[1:])
    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    for secret in _SECRETS:
        assert secret not in output, f"{secret} printed by {args}:\n{output}"


def test_cookie_flags_come_from_the_header_and_name_the_array_disagreement(tmp_path: Path) -> None:
    sid = _block(_lines("cookies", str(_har(tmp_path))), "cookie sid")
    login = [line for line in sid if "POST https://shop.test/session" in line]
    assert len(login) == 1
    assert "HttpOnly=yes Secure=yes SameSite=Lax Path=/ Domain=- expiry=no" in login[0]
    assert "the response.cookies array differs: HttpOnly is no there" in login[0]


def test_a_joined_header_gives_each_cookie_its_own_attributes(tmp_path: Path) -> None:
    xsid = _block(_lines("cookies", str(_har(tmp_path))), "cookie xsid")
    assert len(xsid) == 2
    assert "HttpOnly=no Secure=no SameSite=Strict" in xsid[1]
    assert "differs" not in xsid[1]


def test_a_name_that_ends_another_is_its_own_cookie(tmp_path: Path) -> None:
    sid = _block(_lines("cookies", str(_har(tmp_path))), "cookie sid")
    assert not [line for line in sid if "SameSite=Strict" in line]


def test_the_same_cookie_set_by_two_responses_is_listed_per_response(tmp_path: Path) -> None:
    sid = _block(_lines("cookies", str(_har(tmp_path))), "cookie sid")
    assert len(sid) == 3
    orders = [line for line in sid if "GET https://shop.test/api/orders " in line]
    assert len(orders) == 1
    assert "(1 time)" in orders[0]
    assert "HttpOnly=no Secure=yes" in orders[0]


def test_a_cookie_set_twice_by_one_response_pairs_with_its_own_array_entry(tmp_path: Path) -> None:
    dup = _block(_lines("cookies", str(_har(tmp_path))), "cookie dup")
    assert len(dup) == 3
    assert not [line for line in dup if "differs" in line]
    assert "HttpOnly=yes Secure=no SameSite=- Path=/a" in dup[1]
    assert "HttpOnly=no Secure=no SameSite=- Path=/b" in dup[2]


def test_cookie_attributes_print_as_placeholders_unless_they_are_site_properties(
    tmp_path: Path,
) -> None:
    lines = _lines("cookies", str(_har(tmp_path)))
    (a,) = _block(lines, "cookie a")[1:]
    assert "SameSite=an unknown value Path=/acct/{}/{id}" in a
    assert "Domain=a domain outside the response's host" in a
    (parent,) = _block(lines, "cookie parent")[1:]
    assert "Domain=shop.test expiry=yes" in parent


def test_cookie_names_that_read_as_values_are_counted(tmp_path: Path) -> None:
    lines = _lines("cookies", str(_har(tmp_path)))
    assert "set: 1 cookie name that reads as a value, not shown" in lines
    # uid_SAMCOOKIE_X is also sent, and is counted once, with the names set.
    assert lines[-1] == "sent in requests and never set in this recording: pre"


def test_a_form_body_gives_field_names_and_types_and_hides_a_secret_field(tmp_path: Path) -> None:
    lines = _lines("request", str(_har(tmp_path)), "/session", "--method", "post")
    assert lines[0] == "POST https://shop.test/session"
    assert lines[1] == (
        "  request headers: Content-Type, Cookie, Authorization; "
        "1 name that reads as a value, not shown"
    )
    assert lines[2] == "  request cookies: pre; 1 name that reads as a value, not shown"
    assert "  body: application/x-www-form-urlencoded" in lines
    assert "    username: string (medium)" in lines
    assert "    password: (name reads as a secret; not described)" in lines
    assert "    remember: string (short)" in lines


def test_a_field_sent_many_times_is_one_line_with_a_count(tmp_path: Path) -> None:
    lines = _lines("request", str(_har(tmp_path)), "/search/", "--method", "POST")
    assert lines[0] == "POST https://shop.test/search/{id}/results"
    assert "    search_type: string (short) (sent 11 times)" in lines
    assert "    csrfmiddlewaretoken: (name reads as a secret; not described)" in lines
    assert "    startDate: string (short)" in lines


def test_a_json_body_gives_its_shape(tmp_path: Path) -> None:
    lines = _lines("request", str(_har(tmp_path)), "/api/profile")
    assert "    JSON object of 3 keys" in lines
    assert "      name: string (short)" in lines
    assert "      password: (name reads as a secret; not described)" in lines
    assert "      prefs: object of 1 key" in lines
    assert "        theme: string (short)" in lines


def test_the_query_string_gives_its_named_keys_and_counts_the_rest(tmp_path: Path) -> None:
    lines = _lines("request", str(_har(tmp_path)), "/api/orders")
    assert lines[0] == "GET https://shop.test/api/orders?page&token&code (1 piece with no name)"


def test_a_body_with_no_equals_sign_is_an_unnamed_blob(tmp_path: Path) -> None:
    lines = _lines("request", str(_har(tmp_path)), "/api/blob")
    assert "    an unnamed blob of 18 bytes" in lines


def test_params_and_multipart_names_go_through_the_gate(tmp_path: Path) -> None:
    har = str(_har(tmp_path))
    params = _lines("request", har, "/api/params")
    assert "    quantity: string" in params
    assert "    (2 names that read as values, not shown)" in params
    upload = _lines("request", har, "/api/upload")
    assert "    title: string" in upload
    assert "    (1 name that reads as a value, not shown)" in upload


@pytest.mark.parametrize(
    ("part", "heading"),
    [
        ("/users/", "GET https://shop.test/users/{id}/reset/{id}"),
        ("/verify/", "GET https://shop.test/verify/{token}"),
        ("/acct/", "GET https://shop.test/acct/{id}/orders/{id}"),
        ("/confirm", "GET https://shop.test/reset/{}/confirm"),
        ("/x", "GET https://shop.test/x"),
        ("data", "GET data:"),
    ],
)
def test_a_url_prints_with_its_value_segments_as_placeholders(
    tmp_path: Path, part: str, heading: str
) -> None:
    assert _lines("request", str(_har(tmp_path)), part)[0] == heading


def test_a_templated_path_can_be_matched(tmp_path: Path) -> None:
    assert _lines("request", str(_har(tmp_path)), "/acct/{id}")[0].startswith("GET")


def test_a_json_response_gives_keys_and_types_and_counts_value_like_keys(tmp_path: Path) -> None:
    lines = _lines("response", str(_har(tmp_path)), "/api/orders")
    assert lines[1] == "  status 200, content type application/json"
    assert lines[2] == "  response headers: Set-Cookie"
    assert lines[3] == "  sets cookies: sid"
    for expected in (
        "    JSON object of 8 keys",
        "      orders: array of 1 item",
        "        [first item]: object of 4 keys",
        "          id: string (short)",
        "          total: number",
        "          gift: bool",
        "          note: null",
        "      page: number",
        "      auth_token: (name reads as a secret; not described)",
        "      (5 keys that read as values, not shown)",
    ):
        assert expected in lines, "\n".join(lines)


def test_an_html_response_gives_title_length_and_forms_never_text(tmp_path: Path) -> None:
    lines = _lines("response", str(_har(tmp_path)), "/login")
    assert "    HTML page with a medium title" in lines
    assert "    form POST /session (holds a password input; holds a token-named field)" in lines
    for expected in (
        "      username: text",
        "      password: password",
        "      csrf: hidden",
        "      extra: an input of another type",
        "      (no name) submit button, control 5 of the form",
        "      (no name) checkbox, control 6 of the form, id remember",
        "    form GET mailto:",
        "    form GET javascript:",
        "    form POST /account/{}/update",
        "      ship: select",
        "      note: textarea",
        "      (name not shown) text, control 1 of the form",
        "      (name not shown) text, control 3 of the form",
        "    form GET http://shop.test/x",
    ):
        assert expected in lines, "\n".join(lines)


def test_the_real_recording_shapes_read_cleanly(tmp_path: Path) -> None:
    """A CSRF-only search form is not a login form, and 41 checkboxes are one line."""
    lines = _lines("response", str(_har(tmp_path)), "/search/")
    assert lines[0] == "POST https://shop.test/search/{id}/results"
    assert "    HTML page with a medium title" in lines
    assert "    form POST /search/{id}/results (holds a token-named field)" in lines
    assert "      types1: checkbox (41 controls)" in lines
    assert "      search_type: text" in lines


@pytest.mark.parametrize(
    ("part", "says"),
    [
        ("/api/export", "      rows: array of 1 item"),
        ("/api/binary", "    a body that is not text"),
        ("/trav", "    a body file outside the recording, not read"),
        ("/abs", "    a body file outside the recording, not read"),
        ("/b64", "    HTML page with a short title"),
    ],
)
def test_a_body_is_read_only_from_the_recording(tmp_path: Path, part: str, says: str) -> None:
    assert says in _lines("response", str(_har(tmp_path)), part)


def test_a_url_is_matched_on_the_request_url_not_a_referer(tmp_path: Path) -> None:
    lines = _lines("request", str(_har(tmp_path)), "/session")
    assert [line for line in lines if line and not line.startswith(" ")] == [
        "POST https://shop.test/session"
    ]


def test_the_query_string_is_not_matched(tmp_path: Path) -> None:
    result = _run("request", str(_har(tmp_path)), "QTOK")
    assert result.returncode == 1
    assert result.stdout == ""


def test_the_method_narrows_the_match(tmp_path: Path) -> None:
    result = _run("request", str(_har(tmp_path)), "/session", "--method", "GET")
    assert result.returncode == 1
    assert "no request URL holds '/session' with method GET" in result.stderr


@pytest.mark.parametrize(
    "content",
    [None, "not json", json.dumps({"log": {}}), json.dumps({"log": "x"}), json.dumps([1])],
)
def test_a_missing_or_malformed_har_exits_2(tmp_path: Path, content: str | None) -> None:
    path = tmp_path / "network.har"
    if content is not None:
        path.write_text(content)
    for args in (("cookies", str(path)), ("request", str(path), "/"), ("response", str(path), "/")):
        result = _run(*args)
        assert result.returncode == 2
        assert result.stderr.startswith("har-facts: ")
        assert result.stdout == ""


def test_a_body_too_deep_to_parse_is_counted_not_quoted(tmp_path: Path) -> None:
    deep = "[" * 100_000 + "]" * 100_000
    path = tmp_path / "network.har"
    path.write_text(
        json.dumps(
            {
                "log": {
                    "entries": [
                        _entry(
                            "GET",
                            "https://s.test/d",
                            content={"mimeType": "application/json", "text": deep},
                        )
                    ]
                }
            }
        )
    )
    result = _run("response", str(path), "/d")
    assert result.returncode == 0, result.stderr
    expected = "    200000 characters of JSON nested deeper than 64 levels, not parsed"
    assert expected in result.stdout.splitlines()
    assert "[[[" not in result.stdout + result.stderr


def test_an_unexpected_failure_names_only_its_type(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Any exception past loading exits 2 with its type name, never HAR text."""
    path = tmp_path / "network.har"
    path.write_text(json.dumps({"log": {"entries": [{"request": {"url": "https://s.test/x"}}]}}))
    script = _load_script()

    def explode(*_args: object) -> list[str]:
        raise ValueError("SECRET_IN_TRACEBACK")

    script.cookies = explode
    assert script.main(["har-facts.py", "cookies", str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.err == "har-facts: could not read an entry (ValueError).\n"
    assert captured.out == ""


def test_a_usage_error_exits_2(tmp_path: Path) -> None:
    assert _run("cookies").returncode == 2
    assert _run("headers", str(_har(tmp_path))).returncode == 2


@pytest.mark.parametrize(
    ("name", "secret"),
    [
        ("password", True),
        ("user_password", True),
        ("currentPassword", True),
        ("pwd", True),
        ("pw", True),
        ("api_key", True),
        ("cardNumber", True),
        ("csrf_token", True),
        ("otp", True),
        ("username", False),
        ("email", False),
        ("shipping", False),
        ("keyword", False),
        ("zipCode", False),
        ("countryCode", False),
        ("page", False),
    ],
)
def test_a_secret_looking_name_is_recognised(name: str, secret: bool) -> None:
    assert _load_script().looks_secret(name) is secret


# The gate's measured rates: each kind of random value passes it at most the
# number of times in _MAX_PASSES out of 10,000 (seed 277), 0 for a kind not listed;
# letter-only tokens are the known weak spot. Real field names pass.
_RNG_SEED = 277
_B62 = string.ascii_letters + string.digits


def _samples() -> dict[str, Callable[[random.Random], str]]:
    def text(alphabet: str, length: int) -> Callable[[random.Random], str]:
        return lambda rng: "".join(rng.choices(alphabet, k=length))

    def b64url(nbytes: int) -> Callable[[random.Random], str]:
        return lambda rng: base64.urlsafe_b64encode(rng.randbytes(nbytes)).decode().rstrip("=")

    def jwt_piece(rng: random.Random) -> str:
        claims = rng.choice(
            [
                {"alg": rng.choice(["HS256", "RS256"]), "typ": "JWT"},
                {"sub": str(rng.randrange(10**9)), "iat": rng.randrange(10**10)},
            ]
        )
        raw = json.dumps(claims).encode() if rng.random() < 0.5 else rng.randbytes(32)
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def email(rng: random.Random) -> str:
        local = "".join(rng.choices(string.ascii_lowercase, k=rng.randint(3, 12)))
        return f"{local}@{''.join(rng.choices(string.ascii_lowercase, k=6))}.example"

    def stripe(rng: random.Random) -> str:
        prefix = rng.choice(["cus", "acct", "pi", "ch", "sub", "price", "tok"])
        return f"{prefix}_{''.join(rng.choices(_B62, k=rng.randint(14, 24)))}"

    return {
        "base62 12": text(_B62, 12),
        "base62 16": text(_B62, 16),
        "base62 32": text(_B62, 32),
        "hex 16": text("0123456789abcdef", 16),
        "hex 40": text("0123456789abcdef", 40),
        "base64url 16": b64url(12),
        "base64url 22": b64url(16),
        "base64url 43": b64url(32),
        "JWT piece": jwt_piece,
        "email": email,
        "Stripe-style id": stripe,
        "UUID": lambda rng: str(uuid.UUID(int=rng.getrandbits(128), version=4)),
        "base62 8": text(_B62, 8),
        "base64url 8": text(_B62 + "-_", 8),
        "hex 8": text("0123456789abcdef", 8),
        "lowercase 8": text(string.ascii_lowercase, 8),
        "lowercase 9": text(string.ascii_lowercase, 9),
        "lowercase 12": text(string.ascii_lowercase, 12),
        "lowercase 16": text(string.ascii_lowercase, 16),
        "capitals 9": text(string.ascii_uppercase, 9),
        "capitals 12": text(string.ascii_uppercase, 12),
        "capitals 16": text(string.ascii_uppercase, 16),
        "base36 12": text(string.ascii_lowercase + string.digits, 12),
        "base36 32": text(string.ascii_lowercase + string.digits, 32),
    }


# How many of 10,000 samples of each kind pass the gate, at most: measured with
# seed 277 on 2026-10-19. Letter-only tokens are the gate's known weak spot, since
# spelling is the only rule that can tell them from words.
_MAX_PASSES = {
    "base62 8": 17,
    "base64url 8": 38,
    "hex 8": 5,
    "lowercase 8": 1459,
    "lowercase 9": 1084,
    "lowercase 12": 520,
    "lowercase 16": 197,
    "capitals 9": 1084,
    "capitals 12": 520,
    "capitals 16": 197,
    "base36 12": 25,
    "base36 32": 0,
}


@pytest.mark.parametrize("kind", list(_samples()))
def test_the_gate_fails_random_values(kind: str) -> None:
    gate = _load_script().printable_name
    rng = random.Random(_RNG_SEED)  # noqa: S311 - a fixed sample, not a secret
    make = _samples()[kind]
    passed = [value for value in (make(rng) for _ in range(10_000)) if gate(value)]
    assert len(passed) <= _MAX_PASSES.get(kind, 0), passed[:10]


@pytest.mark.parametrize(
    "name",
    [
        "username",
        "csrfmiddlewaretoken",
        "search_type",
        "startDate",
        "name1",
        "types1",
        "auth_token",
        "orders",
        "page",
        "Content-Type",
        "X-Requested-With",
        "sec-ch-ua-platform",
        "PHPSESSID",
        "JSESSIONID",
        "__Host-sid",
        "_ga",
        "userId",
        "orderID",
        "user[password]",
        "addressLine2",
        "zipCode",
        "XSRF-TOKEN",
        "__RequestVerificationToken",
        # Framework, CDN, and bot-manager names, which no account varies.
        "Upgrade-Insecure-Requests",
        "WWW-Authenticate",
        "ETag",
        "cf-ray",
        "x-amz-cf-id",
        "x-amzn-requestid",
        ".ASPXAUTH",
        "ASP.NET_SessionId",
        ".AspNetCore.Session",
        "__cf_bm",
        "cf_clearance",
        "_abck",
        "bm_sz",
        "_fbp",
        "_gcl_au",
        "__VIEWSTATEGENERATOR",
        "__EVENTVALIDATION",
        # ASP.NET WebForms controls and Django formset fields.
        "txtUserName",
        "btnLogin",
        "chkRememberMe",
        "ctl00$MainContent$txtUserName",
        "ctl00_ContentPlaceHolder1_txtPassword",
        "form-TOTAL_FORMS",
        "form-MAX_NUM_FORMS",
        "form-0-book",
        "pwdConfirm",
        "bvp-0-book",
        "bvp-TOTAL_FORMS",
        "txtPwd",
        "form-12-title",
    ],
)
def test_the_gate_passes_field_names(name: str) -> None:
    assert _load_script().printable_name(name)


@pytest.mark.parametrize(
    "value",
    [
        "alice.example@mail.example",
        "user:name",
        "/path",
        "88812345",
        "order_88812345",
        "uid_SAMCOOKIE_X",
        "addr[ADDRKEY_X]",
        _STRIPE,
        _UUID,
        _RESET,
        "",
        "a" * 49,
        # A framework shape does not carry an account piece through.
        "bvp-0-alice.example@mail.example",
        "txt88812345",
        "ctl00$MainContent$kqzvbxmtrplwyd",
        "form-123-title",
        # Letter-only tokens of the length the old shortcut let through.
        "xqzkvbtrwamplh",
        "HFCENCSGAYRCXOII",
        # A formset prefix is gated like the field after it.
        "xqzkvbtrwamplhab-0-name",
        "xqzkvbtrwamplhab-TOTAL_FORMS",
    ],
)
def test_the_gate_fails_values(value: str) -> None:
    assert not _load_script().printable_name(value)


@pytest.mark.parametrize(
    ("segment", "printed"),
    [
        ("graphql", "graphql"),
        ("jsonrpc", "jsonrpc"),
        ("xmlrpc.php", "xmlrpc.php"),
        ("cgi-bin", "cgi-bin"),
        ("2fa", "2fa"),
        ("Login.aspx", "Login.aspx"),
        ("index.php", "index.php"),
        ("88812345.aspx", "{id}"),
        ("xqzkvbtrwamplh", "{}"),
    ],
)
def test_framework_route_segments_print(segment: str, printed: str) -> None:
    assert _load_script().templated_path(f"/{segment}/x") == f"/{printed}/x"


def test_cookies_by_name_prints_a_name_the_gate_would_hide(tmp_path: Path) -> None:
    """The caller already holds the name, so echoing it adds nothing."""
    har = str(_har(tmp_path))
    lines = _lines("cookies", har, "--name", "uid_SAMCOOKIE_X")
    assert lines == [
        "cookie uid_SAMCOOKIE_X",
        "  set by GET https://shop.test/setck (1 time); header: HttpOnly=no Secure=no "
        "SameSite=- Path=/ Domain=- expiry=no; not in the response.cookies array",
    ]
    assert _lines("cookies", har, "--name", "pre") == [
        "cookie pre: sent in requests and never set in this recording"
    ]
    missing = _run("cookies", har, "--name", "absent")
    assert missing.returncode == 1
    assert missing.stderr == "har-facts: no cookie named 'absent' is set or sent.\n"


def _one_entry_har(tmp_path: Path, body_file: str) -> Path:
    path = tmp_path / "network.har"
    entries = [
        _entry(
            "GET", "https://s.test/a", content={"mimeType": "text/html", "_bodyFile": body_file}
        ),
        _entry("GET", "https://s.test/b", content={"mimeType": "text/html", "text": "<p></p>"}),
    ]
    path.write_text(json.dumps({"log": {"entries": entries}}))
    return path


def test_a_body_file_name_with_a_nul_fails_that_entry_alone(tmp_path: Path) -> None:
    lines = _lines("response", str(_one_entry_har(tmp_path, "bodies/a\x00b.html")), "/")
    assert "    a body file that cannot be read" in lines
    assert "GET https://s.test/b" in lines


def test_a_symlinked_bodies_directory_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "page.html").write_text("<title>OUTSIDE_FILE</title>")
    (tmp_path / "bodies").symlink_to(outside, target_is_directory=True)
    lines = _lines("response", str(_one_entry_har(tmp_path, "bodies/page.html")), "/a")
    assert "    a body file outside the recording, not read" in lines


def test_a_formset_prefix_that_reads_as_a_value_is_withheld(tmp_path: Path) -> None:
    """A real formset prefix shows; a token in the prefix position is counted, in a
    request body, an HTML form, and a JSON key alike."""
    har = str(_har(tmp_path))
    body = _lines("request", har, "/search/", "--method", "POST")
    assert "    form-TOTAL_FORMS: string (short)" in body
    assert "    form-0-book: string (short)" in body
    assert "    (2 names that read as values, not shown)" in body
    page = _lines("response", har, "/search/")
    assert "      (name not shown) hidden, control 4 of the form" in page
    assert "      (name not shown) text, control 5 of the form" in page
    orders = _lines("response", har, "/api/orders")
    assert "      (5 keys that read as values, not shown)" in orders
