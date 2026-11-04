"""#365: a JSON or GraphQL login whose password is nested (``variables.password``),
rendered by a script with no name or id on its inputs, and answering a wrong password
with a JSON ``errors[].message`` and no redirect."""

from __future__ import annotations

import json
import time
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
        # As the recorder writes it: the mark always, the texts only when any were kept.
        failed["request"]["_userMarkedFailed"] = True
        if recorded:
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

    @pytest.mark.parametrize(
        "name",
        [
            "rememberPassword",
            "passwordless",
            "hasPassword",
            "passwordLabel",
            "passwordExpiresAt",
            "passwordResetToken",
            "passwordUpdatedAt",
            "passwordMinLength",
            "showPassword",
        ],
    )
    @pytest.mark.parametrize(
        "shape",
        [
            lambda field: field,
            lambda field: {"variables": {"input": field}},
            lambda field: [{"operationName": "Settings", "variables": field}],
        ],
        ids=["top-level", "nested", "batched"],
    )
    def test_a_setting_about_the_password_makes_no_credential_post(
        self, name: str, shape: Any
    ) -> None:
        """One rule at any depth, the scrub's (``names_a_password``): a name that
        holds a password hint and a setting word names no password."""
        body = json.dumps(shape({name: "2027-01-01"}))
        assert password_field_names(body, _JSON) == []
        assert not is_credential_post(body, _JSON, _API, ())

    def test_a_batched_graphql_login_beside_a_settings_query(self) -> None:
        body = json.dumps(
            [
                {"operationName": "Prefs", "variables": {"rememberPassword": "yes"}},
                json.loads(_login_body()),
            ]
        )
        assert password_field_names(body, _JSON) == ["password"]
        assert is_credential_post(body, _JSON, _API, ())

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


# ASP.NET WebForms names: a naming container whose id holds a setting word
# (placeholder, mode, message, error) before the password's own segment.
_CONTAINER_NAMES = (
    "ctl00$MainPlaceholder$Password",
    "ctl00$ContentPlaceholder1$Password",
    "ctl00$Content$Placeholder$Password",
    "ctl00$LoginMode$Password",
    "ctl00$MessagePanel$txtPwd",
    "ctl00$Main$ErrorPanel$Password",
    "ctl00$placeholder$txtPassword",
    "main_0$placeholder_0$txtPassword",
)


class TestASettingWordCountsOnlyInThePasswordsOwnSegment:
    @pytest.mark.parametrize("name", _CONTAINER_NAMES)
    def test_a_container_s_setting_word_leaves_a_form_login(self, name: str) -> None:
        from urllib.parse import urlencode

        body = urlencode({name: "hunter22", "user": "alice"})
        assert password_field_names(body, _FORM) == [name]
        assert is_credential_body(body, _FORM)

    @pytest.mark.parametrize(
        "name",
        [
            "showPassword",
            "ctl00$Main$showPassword",
            "ctl00$Main$Password$Show",
            "settings.passwordLabel",
            "password[type]",
            "user[rememberPassword]",
        ],
    )
    def test_a_setting_in_the_password_s_segment_or_after_it_still_names_none(
        self, name: str
    ) -> None:
        from urllib.parse import urlencode

        body = urlencode({name: "1", "user": "alice"})
        assert password_field_names(body, _FORM) == []
        assert not is_credential_body(body, _FORM)

    @pytest.mark.parametrize(
        "name",
        ["user[password]", "login.password", "ctl00$ShowPanel$txtPassword", "Login1$Password"],
    )
    def test_a_password_after_any_container_is_one(self, name: str) -> None:
        from urllib.parse import urlencode

        assert is_credential_body(urlencode({name: "hunter22", "user": "alice"}), _FORM)


class TestAPasswordInAJsonList:
    def test_it_is_read_by_the_name_of_the_key_holding_the_list(self) -> None:
        """The scrub reads a list item under the key holding the list
        (``body_values``); the digest's rule reads it the same way."""
        from graftpunk.har.digest import body_values

        body = json.dumps({"v": {"password": ["hunter22"]}, "user": "alice"})
        assert password_field_names(body, _JSON) == ["password"]
        assert is_credential_body(body, _JSON)
        assert (("v", "password"), "hunter22") in body_values(body, _JSON)

    def test_a_list_under_no_key_gives_no_name(self) -> None:
        assert password_field_names(json.dumps(["hunter22", ["x"]]), _JSON) == []

    def test_a_list_of_objects_is_read_by_their_own_names(self) -> None:
        body = json.dumps({"v": {"password": [{"hint": "x"}], "items": [{"pwd": "y"}]}})
        assert password_field_names(body, _JSON) == ["pwd"]


def _large_json_body(megabytes: int, *, distinct: bool = False) -> str:
    """A JSON body of about *megabytes* MB with a login's password nowhere in it:
    records sharing a few names, as a batch sync or a catalogue upload posts, or with
    *distinct*, one object whose names all differ (a map keyed by name)."""
    record = {"id": "r0000000", "name": "Item number 0", "sku": "SKU-00000000", "price": 1.5}
    count = megabytes * 1_000_000 // len(json.dumps(record))
    if distinct:
        return json.dumps({f"item{i:07d}": f"Item number {i}" for i in range(count * 3)})
    rows = [
        {"id": f"r{i:07d}", "name": f"Item number {i}", "sku": f"SKU-{i:08d}", "price": i * 1.5}
        for i in range(count)
    ]
    return json.dumps({"items": rows, "tags": ["a", "b"]})


def _cpu_seconds(work: Any) -> float:
    """The least CPU time of three runs of *work*: CPU time, never wall clock, and
    the least of three, so a busy machine slows neither side of a ratio."""
    times = []
    for _ in range(3):
        start = time.process_time()
        work()
        times.append(time.process_time() - start)
    return min(times)


class TestTheCostOfTheNestedRule:
    """The recorder runs the rule in its CDP request handler for every POST while
    snapshots are on, so the nested read must stay linear in the body: four times
    the body within 12 times the CPU time (three times linear, the margin of the
    repo's other growth tests). Compared size to size, never to ``json.loads``: a
    coverage tracer slows the Python walk and not the C parse, so a ratio to the
    parse failed on CI's Python 3.11 job. A growth ratio cannot tell a linear walk
    with a larger constant (one that judges each value, not each distinct name) from
    this one; it catches a walk that grows faster than the body."""

    def test_a_body_of_shared_names_is_read_in_linear_time(self) -> None:
        small, large = _large_json_body(1), _large_json_body(4)
        assert len(large) >= 4_000_000
        ratio = _cpu_seconds(lambda: password_field_names(large, _JSON)) / max(
            _cpu_seconds(lambda: password_field_names(small, _JSON)), 1e-4
        )
        assert ratio < 12, ratio

    def test_a_body_of_distinct_names_is_judged_in_linear_time(self) -> None:
        small = _large_json_body(1, distinct=True)
        large = _large_json_body(4, distinct=True)
        assert len(large) >= 4_000_000
        ratio = _cpu_seconds(lambda: is_credential_body(large, _JSON)) / max(
            _cpu_seconds(lambda: is_credential_body(small, _JSON)), 1e-4
        )
        assert ratio < 12, ratio


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


class TestAPostedValueNeverBecomesASelector:
    """A login page served as HTML (no DOM snapshot to redact it) whose placeholder
    and button label show the username the login then posts: neither reaches the
    digest's selectors or the generated plugin."""

    _PAGE = (
        "<!DOCTYPE html><html><body>"
        '<form action="/search"><input name="q"><button type="submit">Go</button></form>'
        '<div role="dialog"><input placeholder="jsmith" type="text">'
        '<input type="password" placeholder="Password">'
        '<button type="submit" aria-label="Continue as jsmith">Go</button></div>'
        "</body></html>"
    )

    def _result(self, tmp_path: Path, username: str) -> RunDigest:
        body = json.dumps(
            {
                "operationName": "LoginMutation",
                "variables": {"username": username, "password": _PASSWORD},
                "query": _MUTATION,
            }
        )
        entries = [
            _entry("GET", f"{_HOST}/log-in", content_type="text/html", body=self._PAGE),
            _post(body, {"data": {"LoginMutation": {"id": "u-1"}}}, "2026-11-04T10:00:09.000Z"),
            _entry("GET", f"{_HOST}/account", content_type="text/html", body=_LOGGED_IN),
        ]
        tmp_path.mkdir(exist_ok=True)
        return digest(DigestSource(har_path=_write_har(tmp_path, entries)))

    def test_a_placeholder_holding_the_posted_username_is_dropped(self, tmp_path: Path) -> None:
        result = self._result(tmp_path, "jsmith")
        (form,) = result.login_forms
        assert "username" in form.unresolved_roles
        assert "submit" in form.unresolved_roles
        assert form.fields == {"password": 'input[type="password"]'}
        assert "jsmith" not in repr(result.login_forms)
        assert "jsmith" not in _plugin(result)

    def test_the_placeholder_is_used_when_the_login_posts_another_value(
        self, tmp_path: Path
    ) -> None:
        """The same page with a login that posts another username: the placeholder
        holds no posted value, so it is the username's selector (the button's label
        is still never one)."""
        (form,) = self._result(tmp_path, "alice.w").login_forms
        assert form.fields["username"] == 'input[placeholder="jsmith"]'
        assert form.submit is None


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


class TestTheSnapshotRedaction:
    """A login-time snapshot is scrubbed of the post's email address by the rule the
    run's scrub uses: an address under any name of a credential post."""

    @pytest.mark.parametrize("name", ["emailOrPhoneNumber", "email", "loginId"])
    def test_an_email_in_a_stored_placeholder_is_redacted(self, name: str) -> None:
        page = (
            f'<html><body><input placeholder="{_EMAIL}" type="text">'
            '<input type="password"></body></html>'
        )
        body = json.dumps({"variables": {name: _EMAIL, "password": _PASSWORD}})
        stored = stored_dom_snapshot(page, body, _JSON)
        assert _EMAIL not in stored
        assert 'placeholder="***REDACTED***"' in stored


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
