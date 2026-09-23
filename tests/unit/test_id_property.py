"""The id rule end to end: every must-be-id value in every position a recording
carries a name or a path segment in, and every must-be-kept name surviving.

G2: no account value from a recording reaches a generated file, the
``--endpoints-json`` projection, or a fixture sidecar. ``--json`` and the markdown
digest keep real URL paths by design (the example paths, the login observations'
URLs, the form's action and page, the redirect target), with only an email masked
there, so they are held to G2 for the name positions and for emails alone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from graftpunk.devtools.scaffold.render import ScaffoldSpec, render
from graftpunk.har.digest import (
    DigestSource,
    body_params,
    digest,
    flagged_names_of,
    redacted_names_of,
)
from graftpunk.har.parser import parse_har_file
from graftpunk.har.report import render_endpoints_json, render_json, render_markdown
from graftpunk.testing.sidecar import Sidecar, sidecar_text
from tests.unit.test_har_paths import (
    KEY_POSITION_IDS,
    MUST_BE_KEPT,
    MUST_STAY_LITERAL_SEGMENT,
    PATH_ID_SHAPES,
    SHORT_DIGIT_GROUPS,
)

_HOST = "https://myshop.example.com"

# The URL positions --json and markdown keep as recorded (emails masked).
_URL_POSITIONS = ("api_path_middle", "api_path_last", "login_page", "form_action", "redirect")
_NAME_POSITIONS = (
    "query_key",
    "json_key",
    "form_key",
    "response_key",
    "header_name",
    "cookie_name",
    "login_input_id",
    "login_input_name",
    "hidden_input_name",
)


def _entry(
    method: str,
    url: str,
    *,
    status: int = 200,
    content_type: str = "application/json",
    body: str = "{}",
    request_headers: dict[str, str] | None = None,
    post_data: tuple[str, str] | None = None,
    location: str | None = None,
    set_cookie: str | None = None,
) -> dict[str, Any]:
    headers = [{"name": "Content-Type", "value": content_type}]
    if location is not None:
        headers.append({"name": "Location", "value": location})
    if set_cookie is not None:
        headers.append({"name": "Set-Cookie", "value": set_cookie})
    entry: dict[str, Any] = {
        "startedDateTime": "2026-09-10T10:00:00.000Z",
        "time": 5,
        "request": {
            "method": method,
            "url": url,
            "headers": [{"name": k, "value": v} for k, v in (request_headers or {}).items()],
            "cookies": [],
            "queryString": [],
        },
        "response": {
            "status": status,
            "statusText": "OK",
            "headers": headers,
            "cookies": [],
            "content": {"mimeType": content_type, "text": body, "size": len(body)},
            "redirectURL": location or "",
        },
    }
    if post_data is not None:
        mime_type, text = post_data
        entry["request"]["postData"] = {"mimeType": mime_type, "text": text}
    return entry


def _run(tmp_path: Path, position: str, value: str) -> Path:
    """A recording with a login and three API calls, *value* planted at *position*."""
    page = f"/signin/{value}" if position == "login_page" else "/signin"
    action = f"/accounts/{value}/session" if position == "form_action" else "/session"
    landing = f"/accounts/{value}/dashboard" if position == "redirect" else "/dashboard"
    list_path = f"/api/accounts/{value}/orders" if position == "api_path_middle" else "/api/orders"
    item_path = f"/api/orders/{value}" if position == "api_path_last" else "/api/orders/latest"
    query = f"?{value}=1&page=1" if position == "query_key" else "?page=1"
    json_body = {value: 1, "note": "x"} if position == "json_key" else {"note": "x"}
    form_body = f"{value}=1&name=x" if position == "form_key" else "name=x"
    response = {value: {"total": 1}, "ok": True} if position == "response_key" else {"ok": True}
    headers = {value: "1", "X-Shop-Client": "web"} if position == "header_name" else {}
    cookie = f"{value}=v; Path=/" if position == "cookie_name" else "shop_session=v; Path=/"
    user_id = value if position == "login_input_id" else "email"
    extra = f'<input type="text" name="{value}">' if position == "login_input_name" else ""
    hidden = f'<input type="hidden" name="{value}">' if position == "hidden_input_name" else ""
    entries = [
        _entry(
            "GET",
            f"{_HOST}{page}",
            content_type="text/html",
            body=(
                f'<form action="{action}" method="post">{hidden}{extra}'
                f'<input id="{user_id}" name="username">'
                '<input type="password" name="password"></form>'
            ),
        ),
        _entry(
            "POST",
            f"{_HOST}{action}",
            status=302,
            content_type="text/html",
            body="",
            post_data=("application/x-www-form-urlencoded", "username=alice&password=x"),
            location=landing,
            set_cookie=cookie,
        ),
        _entry(
            "GET",
            f"{_HOST}{list_path}{query}",
            body=json.dumps(response),
            request_headers=headers,
        ),
        _entry(
            "POST",
            f"{_HOST}{item_path}",
            post_data=("application/json", json.dumps(json_body)),
        ),
        _entry(
            "POST",
            f"{_HOST}/api/notes",
            post_data=("application/x-www-form-urlencoded", form_body),
        ),
    ]
    har = tmp_path / "network.har"
    har.write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}))
    return har


def _sidecars(har: Path) -> list[str]:
    result = digest(DigestSource.from_har(har))
    entries = parse_har_file(har).entries
    flagged = flagged_names_of(result, entries)
    redacted = redacted_names_of(result, entries)
    return [
        sidecar_text(
            Sidecar(
                status=entry.response.status,
                content_type=entry.response.content_type or "",
                body_params=tuple(body_params(entry)),
                flagged_names=flagged,
                redacted_names=redacted,
            )
        )
        for entry in entries
    ]


@pytest.mark.parametrize("position", _URL_POSITIONS + _NAME_POSITIONS)
@pytest.mark.parametrize("value", KEY_POSITION_IDS)
def test_no_id_reaches_a_generated_file_the_projection_or_a_sidecar(
    tmp_path: Path, value: str, position: str
) -> None:
    har = _run(tmp_path, position, value)
    result = digest(DigestSource.from_har(har))
    files = render(
        ScaffoldSpec(
            name="myshop", mode="new_project", backend="nodriver", base_url=_HOST, digest=result
        )
    )
    assert "login_config = LoginConfig(" in files["src/graftpunk_myshop/plugin.py"]
    for path, content in files.items():
        assert value not in content, path
        assert value not in path, path
    assert value not in render_endpoints_json(result)
    for text in _sidecars(har):
        assert value not in text
    # Not even hashed: a hash of a short id is reversed by brute force.
    for text in (
        *files.values(),
        render_endpoints_json(result),
        render_json(result),
        *_sidecars(har),
    ):
        assert "sha256:" not in text
    if position in _NAME_POSITIONS or "@" in value or "%40" in value:
        assert value not in render_json(result)
        assert value not in render_markdown(result)


# A name starting with a digit is not a field name by the key alphabet (a separate
# rule from holds_an_id), so the all-digit entries are tested as path segments only.
@pytest.mark.parametrize("name", [n for n in MUST_BE_KEPT if not n[0].isdigit()])
def test_an_ordinary_name_survives_in_every_name_position(tmp_path: Path, name: str) -> None:
    """The must-be-kept table, end to end: none is dropped or hashed."""
    for position in ("query_key", "json_key", "form_key", "response_key", "header_name"):
        run_dir = tmp_path / position
        run_dir.mkdir()
        result = digest(DigestSource.from_har(_run(run_dir, position, name)))
        projection = json.loads(render_endpoints_json(result))
        if position == "query_key":
            found = any(name in e["query_params"] for e in projection["endpoints"])
        elif position in ("json_key", "form_key"):
            found = any(name in e["body_params"] for e in projection["endpoints"])
        elif position == "header_name":
            found = any(name in e["custom_headers"] for e in projection["endpoints"])
        else:
            found = any(e.shape is not None and name in e.shape.children for e in result.endpoints)
        assert found, position


# Digit groups (card, national id, phone numbers), dates, short digit groups, and
# the path rule's own id shapes, which a path fails closed on.
_DIGIT_GROUPS_AND_DATES = (
    "4111-1111-1111-1111",
    "123-45-6789",
    "512-555-0100",
    "1-800-555-0199",
    "1987-03-14",
    "2026-09-23",
    *SHORT_DIGIT_GROUPS,
    *PATH_ID_SHAPES,
)


@pytest.mark.parametrize("position", _URL_POSITIONS)
@pytest.mark.parametrize("value", _DIGIT_GROUPS_AND_DATES)
def test_no_digit_group_or_date_in_a_path_reaches_any_output(
    tmp_path: Path, value: str, position: str
) -> None:
    har = _run(tmp_path, position, value)
    result = digest(DigestSource.from_har(har))
    files = render(
        ScaffoldSpec(
            name="myshop", mode="new_project", backend="nodriver", base_url=_HOST, digest=result
        )
    )
    for path, content in files.items():
        assert value not in content, path
        assert value not in path, path
    # --json and markdown keep recorded paths by design (module docstring).
    for text in (render_endpoints_json(result), *_sidecars(har)):
        assert value not in text


@pytest.mark.parametrize("name", [n for n in MUST_STAY_LITERAL_SEGMENT if "{" not in n])
def test_an_ordinary_path_segment_stays_literal(tmp_path: Path, name: str) -> None:
    result = digest(DigestSource.from_har(_run(tmp_path, "api_path_middle", name)))
    assert f"/api/accounts/{name}/orders" in {e.template for e in result.endpoints}


def test_an_ordinary_cookie_name_is_flagged_as_itself(tmp_path: Path) -> None:
    har = _run(tmp_path, "cookie_name", "billing_address2")
    result = digest(DigestSource.from_har(har))
    assert "billing_address2" in flagged_names_of(result, parse_har_file(har).entries)
