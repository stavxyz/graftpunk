"""render_markdown and render_json over synthetic runs (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from graftpunk.har.digest import SHAPE_UNAVAILABLE, DigestSource, Endpoint, digest
from graftpunk.har.documents import LoginForm
from graftpunk.har.report import (
    SHAPE_UNAVAILABLE_SUMMARY,
    endpoints_projection,
    render_endpoints_json,
    render_json,
    render_markdown,
    summarize_shape,
)

_EXPECTED_MAX_MARKDOWN_LINES = 400
_LARGE_RUN_ENTRY_COUNT = 700


def _entry(
    method: str, url: str, *, content_type: str = "application/json", body: str = "{}"
) -> dict:
    return {
        "startedDateTime": "2026-09-10T10:00:00.000Z",
        "time": 5,
        "request": {"method": method, "url": url, "headers": [], "cookies": [], "queryString": []},
        "response": {
            "status": 200,
            "statusText": "OK",
            "headers": [{"name": "Content-Type", "value": content_type}],
            "cookies": [],
            "content": {"mimeType": content_type, "text": body, "size": len(body)},
        },
    }


def _write_har(tmp_path: Path, entries: list[dict]) -> Path:
    har_path = tmp_path / "network.har"
    har_path.write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}))
    return har_path


class TestRenderMarkdown:
    def test_sections_present_in_order(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/orders")]
        text = render_markdown(digest(DigestSource.from_har(_write_har(tmp_path, entries))))
        for heading in ("## Summary", "## Login", "## Tokens", "## Cookies", "## Endpoints"):
            assert heading in text
        assert text.index("## Summary") < text.index("## Login") < text.index("## Tokens")
        assert text.index("## Tokens") < text.index("## Cookies") < text.index("## Endpoints")

    def test_a_redirecting_login_observation_shows_where_it_went(self, tmp_path: Path) -> None:
        """The landing path is what a reader needs for success_url, so it is on the line."""
        credential_post = {
            "startedDateTime": "2026-09-10T10:00:00.000Z",
            "time": 5,
            "request": {
                "method": "POST",
                "url": "https://api.myshop.example.com/login",
                "headers": [],
                "cookies": [],
                "queryString": [],
                "postData": {
                    "mimeType": "application/json",
                    "text": json.dumps({"password": "x"}),
                },
            },
            "response": {
                "status": 302,
                "statusText": "Found",
                "headers": [{"name": "Location", "value": "/dashboard?welcome=1"}],
                "cookies": [],
                "content": {"mimeType": "text/html", "text": "", "size": 0},
            },
        }
        text = render_markdown(
            digest(DigestSource.from_har(_write_har(tmp_path, [credential_post])))
        )
        login_line = next(line for line in text.splitlines() if "credential_post" in line)
        assert login_line.endswith("-> /dashboard")

    def test_json_endpoints_ordered_before_non_json(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/page",
                content_type="text/html",
                body="<html></html>",
            ),
            _entry("GET", "https://api.myshop.example.com/orders", body="{}"),
        ]
        text = render_markdown(digest(DigestSource.from_har(_write_har(tmp_path, entries))))
        assert text.index("/orders") < text.index("/page")

    def test_dropped_line_reports_non_http_entries(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "chrome://new-tab-page/", content_type="text/html", body="<html>"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        text = render_markdown(result)
        assert "other_scheme=1" in text
        assert "## Other hosts" not in text

    def test_limit_caps_endpoints_shown(self, tmp_path: Path) -> None:
        entries = [_entry("GET", f"https://api.myshop.example.com/item-{i}") for i in range(10)]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        text = render_markdown(result, limit=3)
        assert text.count("### GET /item-") == 3
        assert "more endpoint(s)" in text

    def test_large_run_stays_under_400_lines_with_default_limit(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", f"https://api.myshop.example.com/orders/{i}", body=json.dumps({"id": i}))
            for i in range(_LARGE_RUN_ENTRY_COUNT)
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        text = render_markdown(result)
        assert len(text.splitlines()) < _EXPECTED_MAX_MARKDOWN_LINES

    def test_no_planted_secret_appears_in_rendered_output(self, tmp_path: Path) -> None:
        planted_cookie = "session_id=s3cr3t-cookie-value; Path=/"
        planted_password = "hunter2superSecret"  # noqa: S105 (test fixture, not a credential)
        entries = [
            {
                "startedDateTime": "2026-09-10T10:00:00.000Z",
                "time": 5,
                "request": {
                    "method": "POST",
                    "url": "https://api.myshop.example.com/login",
                    "headers": [{"name": "X-Api-Key", "value": "unshown-header-value-999"}],
                    "cookies": [],
                    "queryString": [],
                    "postData": {
                        "mimeType": "application/json",
                        "text": json.dumps(
                            {"email": "alice@example.com", "password": planted_password}
                        ),
                    },
                },
                "response": {
                    "status": 200,
                    "statusText": "OK",
                    "headers": [
                        {"name": "Content-Type", "value": "application/json"},
                        {"name": "Set-Cookie", "value": planted_cookie},
                    ],
                    "cookies": [],
                    "content": {"mimeType": "application/json", "text": "{}", "size": 2},
                },
            }
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        markdown = render_markdown(result)
        rendered_json = render_json(result)
        for planted in (planted_password, "s3cr3t-cookie-value", "unshown-header-value-999"):
            assert planted not in markdown
            assert planted not in rendered_json

    @pytest.mark.parametrize(
        ("mime_type", "body_text"),
        [
            (
                "application/xml",
                "<login><account>SECRET-ACCT-99</account>"
                "<password>hunter2superSecret</password></login>",
            ),
            ("application/json", '["SECRET-ACCT-99", "hunter2superSecret"]'),
            ("text/plain", "SECRET-ACCT-99 hunter2superSecret"),
        ],
    )
    def test_a_non_form_request_body_never_reaches_the_output(
        self, tmp_path: Path, mime_type: str, body_text: str
    ) -> None:
        """parse_qs returns the whole body as one key for anything that is not a
        form, so the body itself used to render as a 'body param' name."""
        entry = _entry("POST", "https://api.myshop.example.com/login")
        entry["request"]["postData"] = {"mimeType": mime_type, "text": body_text}
        result = digest(DigestSource.from_har(_write_har(tmp_path, [entry])))
        markdown = render_markdown(result)
        rendered_json = render_json(result)
        for planted in ("SECRET-ACCT-99", "hunter2superSecret"):
            assert planted not in markdown
            assert planted not in rendered_json


class TestUnavailableShape:
    def test_summarize_shape_names_it_rather_than_calling_it_non_json(self) -> None:
        assert summarize_shape(SHAPE_UNAVAILABLE) == SHAPE_UNAVAILABLE_SUMMARY
        assert summarize_shape(None) == "non-JSON"

    def test_the_markdown_digest_prints_the_message(self, tmp_path: Path) -> None:
        truncated = '{"orders": [' + '{"id": 1},' * 40000
        entries = [_entry("GET", "https://api.myshop.example.com/big", body=truncated)]
        text = render_markdown(digest(DigestSource.from_har(_write_har(tmp_path, entries))))
        assert f"- shape: {SHAPE_UNAVAILABLE_SUMMARY}" in text


class TestRenderJson:
    def test_output_is_valid_json_with_expected_top_level_keys(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/orders")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        parsed = json.loads(render_json(result))
        assert set(parsed) == {
            "source",
            "primary_host",
            "hosts",
            "endpoints",
            "login",
            "login_forms",
            "tokens",
            "cookies",
            "dropped",
            "cookie_names_dropped_as_ids",
            "token_names_dropped_as_ids",
        }

    def test_dropped_carries_every_reason(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "data:text/html,hello", content_type="text/html", body="hello"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        parsed = json.loads(render_json(result))
        assert set(parsed["dropped"]) == {"static", "third_party", "error", "other_scheme"}
        assert parsed["dropped"]["other_scheme"] == 1

    def test_is_complete_regardless_of_endpoint_count(self, tmp_path: Path) -> None:
        entries = [_entry("GET", f"https://api.myshop.example.com/item-{i}") for i in range(80)]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        parsed = json.loads(render_json(result))
        assert len(parsed["endpoints"]) == len(result.endpoints) == 80

    def test_is_complete_for_two_segment_paths_too(self, tmp_path: Path) -> None:
        """A single-segment path never reaches the high-cardinality collapse
        (the rule skips a segment count of one), so the case above cannot show
        that eighty word-like siblings survive it: this one can."""
        names = [f"{chr(97 + i // 26)}{chr(97 + i % 26)}-listing" for i in range(80)]
        assert len(set(names)) == 80
        entries = [_entry("GET", f"https://api.myshop.example.com/api/{name}") for name in names]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        parsed = json.loads(render_json(result))
        assert len(parsed["endpoints"]) == len(result.endpoints) == 80


_SAMPLE_HAR = Path(__file__).resolve().parents[1] / "fixtures" / "sample.har"

# The projection's field set at schema 1, frozen here so a rename fails the suite.
_PROJECTION_V1 = {"schema", "source", "primary_host", "endpoints", "login"}
_SOURCE_V1 = {"session", "run_id", "har"}
_ENDPOINT_V1 = {
    "method",
    "template",
    "login_flow",
    "content_type",
    "shape",
    "query_params",
    "body_params",
    "custom_headers",
}
_LOGIN_V1 = {"auth_urls", "forms"}
_AUTH_URL_V1 = {"method", "url", "kind"}
_FORM_V1 = {"action", "fields"}


_PLANTED_RESET_SEGMENT = "reset-7f3a9c2e8b1d4f60a9e2c3b4d5f6a7b8"


def _planted_run(tmp_path: Path) -> Path:
    """A run holding a cookie name, a token candidate, an example path, and a body."""
    account = _entry(
        "GET",
        "https://myshop.example.com/account",
        content_type="text/html",
        body=(
            '<html><head><meta name="csrf-token" content="planted-token-value"></head>'
            '<form action="/session" method="post"><input type="email" id="email" name="email">'
            '<input type="password" id="password" name="password"></form></html>'
        ),
    )
    order = _entry(
        "GET",
        "https://myshop.example.com/api/orders/12345",
        body='{"id": "planted-body-value"}',
    )
    order["response"]["cookies"] = [{"name": "shop_session_cookie", "value": "planted-cookie"}]
    # A form whose action carries a session id and a query token, with inputs that
    # have no id, so every selector is built from the action.
    signin = _entry(
        "GET",
        "https://myshop.example.com/signin",
        content_type="text/html",
        body=(
            '<form action="/login;jsessionid=SECRETSESSION123?t=tok999" method="post">'
            '<input type="text" name="username"><input type="password" name="password">'
            "</form>"
        ),
    )
    # A form page served at a token-bearing path.
    reset = _entry(
        "GET",
        f"https://myshop.example.com/signin/{_PLANTED_RESET_SEGMENT}",
        content_type="text/html",
        body=(
            '<form action="/password/reset" method="post">'
            '<input type="password" name="password"></form>'
        ),
    )
    return _write_har(tmp_path, [account, order, signin, reset])


class TestEndpointsProjection:
    def test_schema_one_and_its_field_set(self, tmp_path: Path) -> None:
        payload = endpoints_projection(digest(DigestSource.from_har(_planted_run(tmp_path))))
        assert payload["schema"] == 1
        assert set(payload) == _PROJECTION_V1
        assert set(payload["source"]) == _SOURCE_V1
        assert payload["endpoints"]
        for entry in payload["endpoints"]:
            assert set(entry) == _ENDPOINT_V1
        assert set(payload["login"]) == _LOGIN_V1
        for url in payload["login"]["auth_urls"]:
            assert set(url) == _AUTH_URL_V1
        assert payload["login"]["forms"]
        for form in payload["login"]["forms"]:
            assert set(form) == _FORM_V1

    def test_no_cookie_name_token_candidate_example_path_or_body(self, tmp_path: Path) -> None:
        result = digest(DigestSource.from_har(_planted_run(tmp_path)))
        # The digest itself holds all four, so the assertions below are not vacuous.
        assert "shop_session_cookie" in result.cookies
        assert any(token.name == "csrf-token" for token in result.tokens)
        assert any("/api/orders/12345" in e.examples for e in result.endpoints)
        text = render_endpoints_json(result)
        for planted in (
            "shop_session_cookie",
            "planted-cookie",
            "csrf-token",
            "planted-token-value",
            "/api/orders/12345",
            "planted-body-value",
            "SECRETSESSION123",
            "jsessionid",
            "tok999",
            _PLANTED_RESET_SEGMENT,
        ):
            assert planted not in text, planted

    def test_a_raw_form_action_and_a_token_bearing_form_page_path_are_not_printed(
        self, tmp_path: Path
    ) -> None:
        result = digest(DigestSource.from_har(_planted_run(tmp_path)))
        # The run observed both, so the assertions below are not vacuous.
        assert any(_PLANTED_RESET_SEGMENT in o.url for o in result.login)
        payload = endpoints_projection(result)
        urls = [o["url"] for o in payload["login"]["auth_urls"]]
        assert "https://myshop.example.com/signin/{signin_id}" in urls
        login_form = next(f for f in payload["login"]["forms"] if f["action"] == "/login")
        assert login_form["fields"]["username"] == (
            'form[action="/login"] input[name="username"], '
            'form[action^="/login;"] input[name="username"], '
            'form[action^="/login?"] input[name="username"], '
            'form[action^="/login#"] input[name="username"]'
        )

    def test_the_sample_har_leaks_no_cookie_name_or_example_path(self) -> None:
        result = digest(DigestSource.from_har(_SAMPLE_HAR))
        assert "sessionId" in result.cookies
        text = render_endpoints_json(result)
        assert "sessionId" not in text
        assert "/api/users/123/posts" not in text

    def test_the_projection_is_uncapped(self, tmp_path: Path) -> None:
        # Letters only: a segment with a digit is eligible for the high-cardinality
        # collapse, which would fold these seventy routes into one.
        words = [f"{chr(97 + i // 26)}{chr(97 + i % 26)}route" for i in range(70)]
        entries = [
            _entry("GET", f"https://myshop.example.com/api/{word}", body='{"id": 1}')
            for word in words
        ]
        payload = endpoints_projection(digest(DigestSource.from_har(_write_har(tmp_path, entries))))
        assert len(payload["endpoints"]) == 70

    def test_a_pathless_form_action_is_printed_as_it_is_and_stays_scoped(
        self, tmp_path: Path
    ) -> None:
        """templated_url gives a pathless URL a "/", which is not an account value."""
        result = digest(DigestSource.from_har(_write_har(tmp_path, [])))
        scoped = 'form[action="https://myshop.example.com"] input[name="username"]'
        form = LoginForm(
            action="https://myshop.example.com",
            method="POST",
            fields={"username": scoped},
            submit=None,
            hidden=(),
            source="https://myshop.example.com/signin",
        )
        payload = endpoints_projection(dataclasses.replace(result, login_forms=(form,)))
        assert payload["login"]["forms"] == [
            {"action": "https://myshop.example.com", "fields": {"username": scoped}}
        ]

    def test_a_selector_that_cannot_be_unscoped_is_left_out(self, tmp_path: Path) -> None:
        result = digest(DigestSource.from_har(_write_har(tmp_path, [])))
        form = LoginForm(
            action="/accounts/12345/session",
            method="POST",
            fields={
                "username": 'form[action="/accounts/12345/session"] input.odd',
                "password": "#pw",
            },
            submit=None,
            hidden=(),
            source="https://myshop.example.com/signin",
        )
        payload = endpoints_projection(dataclasses.replace(result, login_forms=(form,)))
        assert payload["login"]["forms"] == [
            {"action": "/accounts/{account_id}/session", "fields": {"password": "#pw"}}
        ]

    def test_a_form_action_holding_an_id_is_templated_and_its_selectors_unscoped(
        self, tmp_path: Path
    ) -> None:
        """The action is printed through the auth URLs' templating. A selector
        scoped to the templated action would match no live form, and one scoped to
        the literal action would print the id, so each prints its input part."""
        page = _entry(
            "GET",
            "https://myshop.example.com/signin",
            content_type="text/html",
            body=(
                f'<form action="/accounts/{_PLANTED_RESET_SEGMENT}/session" method="post">'
                '<input type="text" name="username"><input type="password" id="pw" '
                'name="password"></form>'
            ),
        )
        result = digest(DigestSource.from_har(_write_har(tmp_path, [page])))
        (form,) = endpoints_projection(result)["login"]["forms"]
        assert form == {
            "action": "/accounts/{account_id}/session",
            "fields": {"password": "#pw", "username": 'input[name="username"]'},
        }
        assert _PLANTED_RESET_SEGMENT not in render_endpoints_json(result)

    def test_login_flow_and_shape_come_through(self, tmp_path: Path) -> None:
        payload = endpoints_projection(digest(DigestSource.from_har(_planted_run(tmp_path))))
        order = next(e for e in payload["endpoints"] if e["template"] == "/api/orders/{order_id}")
        assert order["method"] == "GET"
        assert order["login_flow"] is False
        assert order["shape"] == "object{id}"

    def test_the_projection_survives_a_rename_of_an_internal_endpoint_field(
        self, tmp_path: Path
    ) -> None:
        """Built by an explicit function, not by reflection: renaming a field the
        projection does not carry changes render_json and leaves this unchanged."""
        result = digest(DigestSource.from_har(_planted_run(tmp_path)))
        renames = {"examples": "example_paths", "statuses": "status_codes"}
        fields = dataclasses.fields(Endpoint)
        renamed_cls = dataclasses.make_dataclass(
            "Endpoint", [(renames.get(f.name, f.name), f.type) for f in fields], frozen=True
        )
        renamed = tuple(
            renamed_cls(**{renames.get(f.name, f.name): getattr(e, f.name) for f in fields})
            for e in result.endpoints
        )
        patched = dataclasses.replace(result, endpoints=renamed)
        assert endpoints_projection(patched) == endpoints_projection(result)
        assert render_json(patched) != render_json(result)


# A distinct marker in the query, the fragment, a ;param, and the userinfo of
# every URL a digest reads: none of them is a path, so none may be retained.
_URL_MARKERS = (
    "QVALUEPAGE",
    "FRAGPAGE",
    "SEMIPAGE",
    "USERINFOPW",
    "QVALUEACTION",
    "FRAGACTION",
    "SEMIACTION",
    "QVALUEPOST",
    "SEMIPOST",
    "QVALUEREDIR",
    "FRAGREDIR",
    "SEMIREDIR",
    "QVALUEORDERS",
    "SEMIMIDDLE",
)


def _url_planted_run(tmp_path: Path) -> Path:
    page = _entry(
        "GET",
        "https://alice:USERINFOPW@myshop.example.com/signin;s=SEMIPAGE/page?q=QVALUEPAGE#FRAGPAGE",
        content_type="text/html",
        body=(
            '<html><head><meta name="csrf-token" content="t"></head>'
            '<form action="/login;jsessionid=SEMIACTION?q=QVALUEACTION#FRAGACTION" method="post">'
            '<input type="hidden" name="authenticity_token" value="t">'
            '<input name="username"><input type="password" name="password"></form></html>'
        ),
    )
    post = _entry(
        "POST",
        "https://myshop.example.com/login;s=SEMIPOST?q=QVALUEPOST",
        content_type="text/html",
        body="",
    )
    post["request"]["postData"] = {
        "mimeType": "application/x-www-form-urlencoded",
        "text": "username=a&password=b",
    }
    post["response"]["status"] = 302
    redirect = "/dashboard;s=SEMIREDIR?q=QVALUEREDIR#FRAGREDIR"
    post["response"]["redirectURL"] = redirect
    post["response"]["headers"].append({"name": "Location", "value": redirect})
    orders = _entry(
        "GET",
        "https://myshop.example.com/api;s=SEMIMIDDLE/orders?q=QVALUEORDERS",
        body='{"id": 1}',
    )
    return _write_har(tmp_path, [page, post, orders])


_EMAILS = ("alice@example.com", "alice%40example.com")


def _email_planted_run(tmp_path: Path) -> Path:
    """An email in an API path, a login page path, a form action, and a redirect."""
    page = _entry(
        "GET",
        "https://myshop.example.com/signin/alice%40example.com",
        content_type="text/html",
        body=(
            '<form action="/users/alice@example.com/session" method="post">'
            '<input name="username"><input type="password" name="password"></form>'
        ),
    )
    post = _entry(
        "POST",
        "https://myshop.example.com/users/alice@example.com/session",
        content_type="text/html",
        body="",
    )
    post["request"]["postData"] = {
        "mimeType": "application/x-www-form-urlencoded",
        "text": "username=a&password=b",
    }
    post["response"]["status"] = 302
    redirect = "/users/alice%40example.com/dashboard"
    post["response"]["redirectURL"] = redirect
    post["response"]["headers"].append({"name": "Location", "value": redirect})
    orders = _entry(
        "GET", "https://myshop.example.com/api/users/alice@example.com/orders", body='{"id": 1}'
    )
    return _write_har(tmp_path, [page, post, orders])


def test_no_output_carries_an_email_from_a_recorded_path(tmp_path: Path) -> None:
    result = digest(DigestSource.from_har(_email_planted_run(tmp_path)))
    # The digest saw the page, the form, the login, and the endpoint.
    assert result.login_forms
    assert any(o.kind == "credential_post" for o in result.login)
    assert "/api/users/{user_id}/orders" in {e.template for e in result.endpoints}
    for text in (render_json(result), render_endpoints_json(result), render_markdown(result)):
        for email in _EMAILS:
            assert email not in text, email


class TestNoUrlPartBeyondThePathIsRetained:
    def test_neither_json_output_carries_a_query_fragment_param_or_userinfo(
        self, tmp_path: Path
    ) -> None:
        result = digest(DigestSource.from_har(_url_planted_run(tmp_path)))
        # The digest saw the form, the login, the redirect, and the token, so the
        # absence below is not vacuous.
        assert result.login_forms
        assert any(o.kind == "credential_post" for o in result.login)
        assert any(t.name == "csrf-token" for t in result.tokens)
        for text in (render_json(result), render_endpoints_json(result), render_markdown(result)):
            for marker in _URL_MARKERS:
                assert marker not in text, marker

    def test_a_middle_segment_param_leaves_the_endpoint_template(self, tmp_path: Path) -> None:
        payload = endpoints_projection(digest(DigestSource.from_har(_url_planted_run(tmp_path))))
        assert "/api/orders" in {e["template"] for e in payload["endpoints"]}


def test_render_json_carries_no_collapsed_member_beyond_the_capped_examples(
    tmp_path: Path,
) -> None:
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
    words += ["golf", "hotel", "india", "juliet", "kilo", "lima"]
    entries = [
        _entry("GET", f"https://myshop.example.com/products/{word}-widget-2024") for word in words
    ]
    result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
    (endpoint,) = result.endpoints
    assert endpoint.template == "/products/{product_id}"
    text = render_json(result)
    assert "collapsed_templates" not in json.loads(text)
    retained = [w for w in words if any(w in example for example in endpoint.examples)]
    assert len(retained) == len(endpoint.examples) < len(words)
    for word in words:
        assert (word in text) == (word in retained), word
