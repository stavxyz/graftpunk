"""The graft skill's har-facts script: it answers questions about a recording's HAR
with names, attributes, shapes, and counts, and never prints a value from it."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.unit.guide_harness import REPO_ROOT

SCRIPT = REPO_ROOT / "skills" / "graft" / "scripts" / "har-facts.py"
PYTHON = shutil.which("python3")

# Every value below is one the script must never print.
_SECRETS = [
    "CSRF_SECRET_VALUE",
    "PASSWORD_SECRET",
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
    "dark",
    "12.5",
]


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


_LOGIN_PAGE = (
    "<html><head><title>Sign in</title></head><body>"
    '<form method="post" action="/session?next=QTOK_SECRET">'
    '<input name="username" value="alice">'
    '<input type="password" name="password" value="PASSWORD_SECRET">'
    '<input type="hidden" name="csrf" value="CSRF_SECRET_VALUE">'
    "</form></body></html>"
)


def _har(tmp_path: Path) -> Path:
    joined = (
        "sid=SID_SECRET; Path=/; HttpOnly; Secure; SameSite=Lax\n"
        "xsid=XSID_SECRET; Path=/; SameSite=Strict"
    )
    (tmp_path / "bodies").mkdir()
    (tmp_path / "bodies" / "big.json").write_text(json.dumps({"rows": ["FILE_SECRET"]}))
    entries = [
        _entry(
            "GET",
            "https://shop.test/login",
            content={"mimeType": "text/html", "text": _LOGIN_PAGE},
        ),
        _entry(
            "POST",
            "https://shop.test/session",
            request_headers=[
                _h("Content-Type", "application/x-www-form-urlencoded"),
                _h("Cookie", "pre=PRE_SECRET"),
                _h("Authorization", "Bearer AUTH_SECRET"),
            ],
            request_cookies=[{"name": "pre", "value": "PRE_SECRET"}],
            post={
                "mimeType": "application/x-www-form-urlencoded",
                "text": "username=alice%40example.com&password=PASSWORD_SECRET&remember=1",
            },
            status=302,
            response_headers=[_h("set-cookie", joined)],
            # The array lacks httpOnly for sid, which the header carries.
            response_cookies=[
                {"name": "sid", "value": "SID_SECRET", "path": "/", "secure": True},
                {"name": "xsid", "value": "XSID_SECRET", "path": "/"},
            ],
        ),
        _entry(
            "GET",
            "https://shop.test/api/orders?page=2&token=QTOK_SECRET",
            request_headers=[_h("Cookie", "sid=SID_SECRET; xsid=XSID_SECRET")],
            response_headers=[_h("Set-Cookie", "sid=SID_SECRET2; Path=/; Secure")],
            response_cookies=[{"name": "sid", "value": "SID_SECRET2", "path": "/", "secure": True}],
            content={
                "mimeType": "application/json",
                "text": json.dumps(
                    {
                        "orders": [
                            {"id": "ORD_SECRET_1", "total": 12.5, "gift": False, "note": None}
                        ],
                        "page": 2,
                        "auth_token": "BODY_TOKEN_SECRET",
                        "alice@example.com": {"x": 1},
                    }
                ),
            },
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
    ]
    path = tmp_path / "network.har"
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


def _block(stdout: str, name: str) -> list[str]:
    """The lines of the ``cookie <name>`` block."""
    lines = stdout.splitlines()
    start = lines.index(f"cookie {name}")
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if not line.startswith("  "):
            break
        block.append(line)
    return block


_EVERY_RUN = [
    ("cookies",),
    ("request", "/"),
    ("response", "/"),
    ("request", "/session"),
    ("response", "/login"),
    ("response", "/api/orders"),
    ("request", "/api/orders"),
    ("request", "/api/profile"),
    ("response", "/api/export"),
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
    result = _run("cookies", str(_har(tmp_path)))
    sid = _block(result.stdout, "sid")
    login = [line for line in sid if "POST https://shop.test/session" in line]
    assert len(login) == 1
    assert "HttpOnly=yes Secure=yes SameSite=Lax Path=/ Domain=- expiry=no" in login[0]
    assert "the response.cookies array differs: HttpOnly is no there" in login[0]


def test_a_joined_header_gives_each_cookie_its_own_attributes(tmp_path: Path) -> None:
    result = _run("cookies", str(_har(tmp_path)))
    xsid = _block(result.stdout, "xsid")
    assert len(xsid) == 2
    assert "HttpOnly=no Secure=no SameSite=Strict" in xsid[1]
    assert "differs" not in xsid[1]


def test_a_name_that_ends_another_is_its_own_cookie(tmp_path: Path) -> None:
    """sid's block holds only sid's settings, not xsid's."""
    result = _run("cookies", str(_har(tmp_path)))
    assert not [line for line in _block(result.stdout, "sid") if "SameSite=Strict" in line]


def test_the_same_cookie_set_by_two_responses_is_listed_per_response(tmp_path: Path) -> None:
    result = _run("cookies", str(_har(tmp_path)))
    sid = _block(result.stdout, "sid")
    assert len(sid) == 3
    orders = [line for line in sid if "GET https://shop.test/api/orders " in line]
    assert len(orders) == 1
    assert "(1 time)" in orders[0]
    assert "HttpOnly=no Secure=yes" in orders[0]


def test_cookies_only_sent_are_named(tmp_path: Path) -> None:
    result = _run("cookies", str(_har(tmp_path)))
    assert result.stdout.splitlines()[-1] == "sent in requests and never set in this recording: pre"


def test_a_form_body_gives_field_names_and_types_and_hides_a_secret_field(tmp_path: Path) -> None:
    result = _run("request", str(_har(tmp_path)), "/session", "--method", "post")
    lines = result.stdout.splitlines()
    assert lines[0] == "POST https://shop.test/session"
    assert lines[1] == "  request headers: Content-Type, Cookie, Authorization"
    assert lines[2] == "  request cookies: pre"
    assert "  body: application/x-www-form-urlencoded" in lines
    assert "    username: string (medium)" in lines
    assert "    password: (name reads as a secret; not described)" in lines
    assert "    remember: string (short)" in lines


def test_a_json_body_gives_its_shape(tmp_path: Path) -> None:
    result = _run("request", str(_har(tmp_path)), "/api/profile")
    lines = result.stdout.splitlines()
    assert "    JSON object of 3 keys" in lines
    assert "      name: string (short)" in lines
    assert "      password: (name reads as a secret; not described)" in lines
    assert "      prefs: object of 1 key" in lines
    assert "        theme: string (short)" in lines


def test_the_query_string_gives_its_keys_only(tmp_path: Path) -> None:
    result = _run("request", str(_har(tmp_path)), "/api/orders")
    assert result.stdout.splitlines()[0] == "GET https://shop.test/api/orders?page&token"


def test_a_json_response_gives_keys_and_types_and_counts_value_like_keys(tmp_path: Path) -> None:
    result = _run("response", str(_har(tmp_path)), "/api/orders")
    lines = result.stdout.splitlines()
    assert lines[1] == "  status 200, content type application/json"
    assert lines[2] == "  sets cookies: sid"
    for expected in (
        "    JSON object of 4 keys",
        "      orders: array of 1 item",
        "        [first item]: object",
        "          id: string (short)",
        "          total: number",
        "          gift: bool",
        "          note: null",
        "      page: number",
        "      auth_token: (name reads as a secret; not described)",
        "      (1 more keys that read as values, such as ids, not listed)",
    ):
        assert expected in lines, result.stdout


def test_an_html_response_gives_its_title_and_form_fields(tmp_path: Path) -> None:
    result = _run("response", str(_har(tmp_path)), "/login")
    lines = result.stdout.splitlines()
    assert "    HTML page, title: Sign in" in lines
    assert "    form POST https://shop.test/session (holds a password-like field)" not in lines
    assert "    form POST /session (holds a password-like field)" in lines
    assert "      username: text" in lines
    assert "      password: password" in lines
    assert "      csrf: hidden" in lines


def test_a_body_streamed_to_bodies_is_read_from_there(tmp_path: Path) -> None:
    result = _run("response", str(_har(tmp_path)), "/api/export")
    assert "      rows: array of 1 item" in result.stdout.splitlines()


def test_a_url_is_matched_on_the_request_url_not_a_referer(tmp_path: Path) -> None:
    result = _run("request", str(_har(tmp_path)), "/session")
    assert [line for line in result.stdout.splitlines() if not line.startswith(" ") and line] == [
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


@pytest.mark.parametrize("content", [None, "not json", json.dumps({"log": {}})])
def test_a_missing_or_unreadable_har_exits_2(tmp_path: Path, content: str | None) -> None:
    path = tmp_path / "network.har"
    if content is not None:
        path.write_text(content)
    for args in (("cookies", str(path)), ("request", str(path), "/"), ("response", str(path), "/")):
        result = _run(*args)
        assert result.returncode == 2
        assert result.stderr.startswith("har-facts: ")
        assert result.stdout == ""


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
        ("api_key", True),
        ("cardNumber", True),
        ("csrf_token", True),
        ("otp", True),
        ("username", False),
        ("email", False),
        ("shipping", False),
        ("keyword", False),
        ("page", False),
    ],
)
def test_a_secret_looking_name_is_recognised(name: str, secret: bool) -> None:
    assert _load_script().looks_secret(name) is secret
