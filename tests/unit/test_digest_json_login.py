"""#365: a JSON or GraphQL login whose password is nested (``variables.password``),
rendered by a script with no name or id on its inputs, and answering a wrong password
with a JSON ``errors[].message`` and no redirect."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from graftpunk.devtools.scaffold.render import ScaffoldSpec, render
from graftpunk.har.digest import (
    DigestSource,
    RunDigest,
    digest,
    is_credential_body,
    is_credential_post,
    password_field_names,
)
from graftpunk.observe.snapshot import stored_dom_snapshot
from tests.unit.test_har_digest import _entry, _write_har

_HOST = "https://myshop.example.com"
_API = f"{_HOST}/GraphQLController"
_JSON = "application/json"
_FORM = "application/x-www-form-urlencoded"
_EMAIL = "alice@example.com"
_PASSWORD = "hunter2hunter2"  # noqa: S105 - placeholder, not a credential
_MUTATION = (
    "mutation LoginMutation($emailOrPhoneNumber: String!, $password: String!) "
    "{ LoginMutation(emailOrPhoneNumber: $emailOrPhoneNumber, password: $password) { id } }"
)
_SHELL = (
    '<!DOCTYPE html><html><body><input type="hidden" name="csrf" value="t0k3n">'
    '<div id="root">{nav}<main class="content"></main></div></body></html>'
)
_LOGGED_OUT = _SHELL.format(nav="")
_LOGGED_IN = _SHELL.format(
    nav='<nav id="navbar"><a href="/orders">Orders</a></nav>'
    '<div id="sideNavMenuOptions"><a href="/account">Account</a></div>'
)
# The login form the page's script draws: no name or id, build-hashed classes.
_FORM_HTML = (
    '<div class="xK9q2Lm login-card">'
    '<div class="mb-3"><input placeholder="Enter your email or mobile number" type="text" '
    'class="tY7r3Qp form-control"></div>'
    '<div class="mb-3"><input placeholder="Enter your password" type="password" '
    'class="tY7r3Qp form-control"></div>'
    '<button type="submit" class="bN4w8Zx sign-in-btn btn btn-primary btn-block">Sign In'
    "</button></div>"
)
_RENDERED = (
    '<!DOCTYPE html><html><body><div id="root"><main class="content">'
    f"{_FORM_HTML}</main></div></body></html>"
)
_ERRORS = {
    "errors": [{"message": "Invalid Credentials", "extensions": {"category": "auth"}}],
    "details": "",
    "custom": True,
    "custom_error_code": "Invalid Credentials",
}


def _login_body(password: str = _PASSWORD, **variables: Any) -> str:
    return json.dumps(
        {
            "operationName": "LoginMutation",
            "variables": {"emailOrPhoneNumber": _EMAIL, "password": password, **variables},
            "extensions": {"clientLibrary": {"name": "client"}},
            "query": _MUTATION,
        }
    )


def _post(body: str, response: dict[str, Any], started: str) -> dict[str, Any]:
    return _entry(
        "POST",
        _API,
        post_data=body,
        body=json.dumps(response),
        request_headers={"Content-Type": _JSON, "Referer": f"{_HOST}/log-in"},
        started=started,
    )


def _snapshot(run: Path, post: dict[str, Any], name: str, html: str, body: str) -> None:
    """Store *html* as the recorder stores a credential post's DOM snapshot."""
    (run / "snapshots").mkdir(exist_ok=True)
    (run / "snapshots" / f"{name}.html").write_text(stored_dom_snapshot(html, body, _JSON))
    post["request"]["_domSnapshot"] = f"snapshots/{name}.html"
    post["request"]["_domSnapshotUrl"] = f"{_HOST}/log-in"


def _graphql_run(run: Path, *, failed_step: bool = True, recorded: tuple[str, ...] = ()) -> Path:
    """The shape of a GraphQL login run: the shell pages, the logged-out ``user``
    query, (a wrong password marked failed,) the login mutation, and the logged-in
    ``user`` query; ``page-source.html`` is the logged-in shell."""
    run.mkdir(parents=True, exist_ok=True)
    user_query = json.dumps({"operationName": "user", "variables": {}, "query": "query user"})
    entries = [
        _entry("GET", f"{_HOST}/", content_type="text/html", body=_LOGGED_OUT),
        _entry("GET", f"{_HOST}/log-in", content_type="text/html", body=_LOGGED_OUT),
        _post(user_query, {"data": {"user": None}}, "2026-11-04T10:00:02.000Z"),
    ]
    if failed_step:
        wrong = _login_body("***REDACTED***")
        failed = _post(wrong, _ERRORS, "2026-11-04T10:00:05.000Z")
        _snapshot(run, failed, "41.1", _RENDERED, _login_body("wrongpass1"))
        failed["request"]["_userMarkedFailed"] = True
        failed["request"]["_failureTextCandidates"] = list(recorded)
        entries.append(failed)
    real = _post(
        _login_body(), {"data": {"LoginMutation": {"id": "u-1"}}}, "2026-11-04T10:00:09.000Z"
    )
    _snapshot(run, real, "52.1", _RENDERED, _login_body())
    (run / "snapshots" / "52.1.after.html").write_text(
        stored_dom_snapshot(_LOGGED_IN, _login_body(), _JSON, structure_only=True)
    )
    real["request"]["_domAfter"] = "snapshots/52.1.after.html"
    entries.append(real)
    entries.append(_post(user_query, {"data": {"user": {"id": "u-1"}}}, "2026-11-04T10:00:10.000Z"))
    _write_har(run, entries)
    (run / "page-source.html").write_text(_LOGGED_IN)
    return run


def _digest_run(run: Path) -> RunDigest:
    return digest(DigestSource.from_run_dir(run, session="myshop", run_id="r1"))


def _plugin(result: RunDigest) -> str:
    return render(
        ScaffoldSpec(
            name="myshop", mode="new_project", backend="nodriver", base_url=_HOST, digest=result
        )
    )["src/graftpunk_myshop/plugin.py"]


def _login_config(code: str) -> Any:
    from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep

    start = code.index("login_config = LoginConfig(") + len("login_config = ")
    end = code.index("\n    )\n", start) + 6
    return eval(code[start:end], {"LoginConfig": LoginConfig, "LoginStep": LoginStep})  # noqa: S307


class TestTheNestedPasswordRule:
    @pytest.mark.parametrize(
        ("body", "names"),
        [
            (_login_body(), ["password"]),
            ('{"user": {"login": "a", "pwd": "b"}}', ["pwd"]),
            ('{"a": {"b": {"c": {"userPassword": "x"}}}}', ["userPassword"]),
            # A batched GraphQL request is a JSON array of operations.
            (f"[{_login_body()}]", ["password"]),
            ('{"password": "x", "variables": {"password": "y"}}', ["password"]),
        ],
    )
    def test_a_password_named_field_at_any_depth_of_a_json_body(
        self, body: str, names: list[str]
    ) -> None:
        assert password_field_names(body, _JSON) == names
        assert is_credential_body(body, "application/graphql+json; charset=utf-8")

    @pytest.mark.parametrize(
        "body",
        [
            # Null and booleans are no password (the top-level rule skips null too).
            '{"variables": {"password": null}}',
            '{"variables": {"rememberPassword": true}}',
            # A value is never a name: GraphQL's query text spells "$password".
            json.dumps({"query": _MUTATION, "variables": {}}),
            # A key holding an account value is data, not a field name.
            '{"byUser": {"alice@example.com": {"x": "password"}}}',
        ],
    )
    def test_no_password_named_field(self, body: str) -> None:
        assert password_field_names(body, _JSON) == []
        assert not is_credential_body(body, _JSON)

    def test_a_form_body_keeps_its_top_level_names_only(self) -> None:
        """A form field holding JSON is read by its own name, as before: its keys are
        never field names of the form."""
        body = "state=%7B%22password%22%3A%22x%22%7D&user=alice"
        assert password_field_names(body, _FORM) == []
        assert password_field_names("user=alice&pwd=x", _FORM) == ["pwd"]
        assert is_credential_post(body, _FORM, f"{_HOST}/session", ()) is False

    def test_a_nested_password_change_is_no_credential_post(self) -> None:
        body = json.dumps(
            {
                "operationName": "ChangePassword",
                "variables": {"currentPassword": "a1b2c3", "newPassword": "d4e5f6"},
            }
        )
        assert password_field_names(body, _JSON) == ["currentPassword", "newPassword"]
        assert not is_credential_post(body, _JSON, _API, ())

    def test_a_nested_sign_up_with_one_password_is_a_credential_post(self) -> None:
        """Pinned limit: a sign-up asking for one password and nothing that names it
        new reads as a login, as a top-level one did before; the digest has no rule
        that tells them apart by the body alone."""
        body = json.dumps(
            {
                "operationName": "SignUpMutation",
                "variables": {"email": _EMAIL, "name": "Alice", "password": "a1b2c3"},
            }
        )
        assert is_credential_post(body, _JSON, _API, ())


class TestTheDigestOfAGraphqlLogin:
    def test_the_posts_the_form_and_the_suggestions(self, tmp_path: Path) -> None:
        result = _digest_run(_graphql_run(tmp_path / "run"))

        posts = [o for o in result.login if o.kind == "credential_post"]
        assert [(o.method, o.url, o.status) for o in posts] == [("POST", _API, 200)] * 2
        assert posts[0].fields == ("extensions", "operationName", "query", "variables")
        (form,) = result.login_forms
        assert form.fields == {
            "username": 'input[placeholder="Enter your email or mobile number"]',
            "password": 'input[type="password"]',
        }
        assert form.submit == 'button[type="submit"]'
        assert form.unresolved_roles == ()
        assert form.from_snapshot
        assert result.success_candidates == ("#navbar", "#sideNavMenuOptions")
        assert result.failure_candidates == ("Invalid Credentials",)

    def test_without_the_wrong_password_step_no_failure_text(self, tmp_path: Path) -> None:
        result = _digest_run(_graphql_run(tmp_path / "run", failed_step=False))
        assert result.failure_candidates == ()
        assert result.success_candidates == ("#navbar", "#sideNavMenuOptions")

    def test_the_pre_login_user_query_is_no_credential_post(self, tmp_path: Path) -> None:
        result = _digest_run(_graphql_run(tmp_path / "run"))
        assert len([o for o in result.login if o.kind == "credential_post"]) == 2

    def test_gp_plugin_new_writes_a_login_config(self, tmp_path: Path) -> None:
        code = _plugin(_digest_run(_graphql_run(tmp_path / "run")))

        assert "No login form detected" not in code
        config = _login_config(code)
        (step,) = config.steps
        assert step.fields == {
            "password": 'input[type="password"]',
            "username": 'input[placeholder="Enter your email or mobile number"]',
        }
        assert step.submit == 'button[type="submit"]'
        assert config.url == "/log-in"
        assert '# failure="Invalid Credentials",' in code
        assert '# success="#navbar",' in code
        assert '# success="#sideNavMenuOptions",' in code
        assert _PASSWORD not in code
        assert _EMAIL not in code


class TestFailureTextFromAJsonResponse:
    def test_the_recorder_s_texts_come_first_and_one_text_is_given_once(
        self, tmp_path: Path
    ) -> None:
        run = _graphql_run(tmp_path / "run", recorded=("INVALID CREDENTIALS", "Please try again"))
        assert _digest_run(run).failure_candidates == ("INVALID CREDENTIALS", "Please try again")

    def test_at_most_three(self, tmp_path: Path) -> None:
        run = _graphql_run(
            tmp_path / "run", recorded=("Text one here", "Text two here", "Text 3 here")
        )
        assert _digest_run(run).failure_candidates == (
            "Text one here",
            "Text two here",
            "Text 3 here",
        )

    @pytest.mark.parametrize(
        ("response", "content_type"),
        [
            ({"errors": [{"message": f"No account for {_EMAIL}"}]}, _JSON),
            ({"errors": [{"message": "No account for Alice"}]}, _JSON),
            ({"errors": [{"message": "short"}]}, _JSON),
            ({"errors": [{"message": 401}, "Invalid Credentials"]}, _JSON),
            ({"errors": {"message": "Invalid Credentials"}}, _JSON),
            ({"message": "Invalid Credentials"}, _JSON),
            (_ERRORS, "text/plain"),
        ],
    )
    def test_a_message_that_is_not_kept(
        self, tmp_path: Path, response: dict[str, Any], content_type: str
    ) -> None:
        """An account value or an email, a text outside 8 to 200 characters, an error
        that is no object with a string message, and a body that is no JSON response
        give nothing."""
        run = _graphql_run(tmp_path / "run")
        har = json.loads((run / "network.har").read_text())
        failed = har["log"]["entries"][3]
        failed["response"]["content"] = {"mimeType": content_type, "text": json.dumps(response)}
        failed["response"]["headers"] = [{"name": "Content-Type", "value": content_type}]
        (run / "network.har").write_text(json.dumps(har))
        assert _digest_run(run).failure_candidates == ()

    def test_an_unmarked_post_s_errors_are_never_read(self, tmp_path: Path) -> None:
        run = _graphql_run(tmp_path / "run")
        har = json.loads((run / "network.har").read_text())
        del har["log"]["entries"][3]["request"]["_userMarkedFailed"]
        (run / "network.har").write_text(json.dumps(har))
        assert _digest_run(run).failure_candidates == ()


class TestAFormLoginIsReadAsBefore:
    _LOGIN = (
        "<html><body><form action='/session' method='post'>"
        "<input type='email' id='email' name='email'>"
        "<input type='password' id='password' name='password'>"
        "<button id='go'>Sign in</button></form></body></html>"
    )

    def test_the_form_post_its_fields_and_selectors(self, tmp_path: Path) -> None:
        post = _entry(
            "POST",
            f"{_HOST}/session",
            status=302,
            post_data="email=alice%40example.com&password=hunter2hunter2&state=%7B%7D",
            response_headers={"Location": "/account"},
        )
        post["request"]["postData"]["mimeType"] = _FORM
        post["request"]["headers"] = [{"name": "Content-Type", "value": _FORM}]
        entries = [
            _entry("GET", f"{_HOST}/login", content_type="text/html", body=self._LOGIN),
            post,
            _entry("GET", f"{_HOST}/account", content_type="text/html", body=_LOGGED_IN),
        ]
        tmp_path.mkdir(exist_ok=True)
        result = digest(DigestSource(har_path=_write_har(tmp_path, entries)))

        assert [(o.kind, o.fields) for o in result.login] == [
            ("form_page", ()),
            ("credential_post", ("email", "password", "state")),
        ]
        (form,) = result.login_forms
        assert form.fields == {"username": "#email", "password": "#password"}
        assert form.submit == "#go"
