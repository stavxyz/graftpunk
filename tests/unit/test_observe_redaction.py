"""What an observe run is scrubbed of (#283).

Every credential is scrubbed from login and plugin-command runs. An identifier
field (by the allowlist :func:`graftpunk.observe.run.is_identifier_field`) is
replaced only as a whole value and left in the URL fields; every other
credential is a secret, replaced wherever it occurs. An HTTP Basic
credential holding a credential is replaced whole.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from graftpunk.observe.run import REDACTED, Redaction, is_identifier_field, redact_har_entries

# Every name the reviews of #286 found a heuristic leak: all are secrets.
SECRET_FIELD_NAMES = [
    "login_password",
    "user_password",
    "userPin",
    "login_pin",
    "email_code",
    "email_token",
    "login_token",
    "user_key",
    "userkey",
    "userPINCode",
    "loginMFACode",
    "emailOTPCode",
    "user2FA",
    "user_pass",
    "login_pwd",
    "user_passwd",
    "userPass",
    "login_pw",
    "user_passcode",
    "user_email",
]
IDENTIFIER_FIELD_NAMES = [
    "username",
    "user_name",
    "UserName",
    "user",
    "user-id",
    "userID",
    "email",
    "Email",
    "email_address",
    "emailAddress",
    "login",
    "login.id",
    "loginName",
    "account",
    "Account ID",
    "account_id",
]
HIDDEN = "s3cret-value-77"
USER = "bob@example.com"


class TestTheClassifier:
    @pytest.mark.parametrize("name", SECRET_FIELD_NAMES)
    def test_a_name_off_the_allowlist_is_a_secret(self, name: str) -> None:
        assert not is_identifier_field(name)
        assert Redaction.from_credentials({name: HIDDEN}) == Redaction(secrets=(HIDDEN,))

    @pytest.mark.parametrize("name", IDENTIFIER_FIELD_NAMES)
    def test_an_allowlisted_name_in_any_spelling_is_an_identifier(self, name: str) -> None:
        assert is_identifier_field(name)
        assert Redaction.from_credentials({name: USER}) == Redaction(identifiers=(USER,))


def _url_entry(value: str) -> dict[str, Any]:
    return {
        "request": {
            "url": f"https://myshop.example/api?x={value}",
            "queryString": [{"name": "x", "value": value}],
        },
        "response": {"redirectURL": f"https://myshop.example/next?x={value}"},
    }


def _login_redaction(fields: dict[str, str]) -> Redaction:
    """The redaction a nodriver login with *fields* hands ``save_observe_run``."""
    import asyncio

    from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep, SitePlugin
    from graftpunk.plugins.login_engine import generate_login_method
    from tests.unit.test_login_headless_observe import _nodriver_session_mock, _nodriver_tab

    class Shop(SitePlugin):
        site_name = "myshop"
        session_name = "myshop"
        help_text = "t"
        base_url = "https://myshop.example"
        backend = "nodriver"
        login_config = LoginConfig(
            steps=[LoginStep(fields={name: f"#{i}" for i, name in enumerate(fields)}, submit="#s")],
            url="/login",
            failure="Bad login.",
            timeout=0.05,
            settle=0.0,
        )

    mock_bs, _ = _nodriver_session_mock(_nodriver_tab())
    capture = AsyncMock()
    capture.get_header_roles = lambda: {}
    login = generate_login_method(Shop())
    with (
        patch("graftpunk.BrowserSession", mock_bs),
        patch("graftpunk.observe.capture.create_capture_backend", return_value=capture),
        patch("graftpunk.observe.run.save_observe_run", new_callable=AsyncMock) as save,
        patch("graftpunk.observe.storage.ObserveStorage"),
    ):
        asyncio.run(login(dict(fields), observe_mode="full"))
    return save.await_args.kwargs["redact"]


class TestSecretFieldsAreScrubbedFromUrls:
    @pytest.mark.parametrize("name", SECRET_FIELD_NAMES)
    def test_on_a_login_run(self, name: str) -> None:
        redaction = _login_redaction({"username": USER, name: HIDDEN})

        (entry,) = redact_har_entries([_url_entry(HIDDEN)], redaction)

        assert HIDDEN not in json.dumps(entry)

    @pytest.mark.parametrize("name", SECRET_FIELD_NAMES)
    def test_on_a_plugin_command_run(
        self, name: str, site: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import typer

        from graftpunk.cli.command_factory import synthesize_command_fn
        from graftpunk.cli.login_commands import credential_envvar
        from graftpunk.cli.plugin_runtime import run_plugin_command
        from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep, SitePlugin, command

        class Shop(SitePlugin):
            site_name = "myshop"
            session_name = "myshop"
            help_text = "t"
            requires_session = False
            base_url = site
            login_config = LoginConfig(
                steps=[LoginStep(fields={"username": "#u", name: "#s"}, submit="#b")],
                url="/login",
            )

            @command(help="Fetch")
            def fetch(self, ctx: Any) -> dict[str, Any]:
                ctx.session.get(f"{self.base_url}/home", params={"x": HIDDEN})
                return {}

        plugin = Shop()
        monkeypatch.setenv(credential_envvar(plugin, "username"), USER)
        monkeypatch.setenv(credential_envvar(plugin, name), HIDDEN)
        spec = next(s for s in plugin.get_commands() if s.name == "fetch")
        app = typer.Typer()
        app.command(name="fetch")(
            synthesize_command_fn(
                name="fetch",
                param_specs=list(spec.params),
                body=lambda ctx, **kw: run_plugin_command(plugin, spec, ctx, **kw),
            )
        )
        with (
            patch("graftpunk.observe.context.OBSERVE_BASE_DIR", tmp_path),
            patch("graftpunk.cli.plugin_runtime.format_output"),
        ):
            result = CliRunner().invoke(app, [], obj={"observe_mode": "full"})

        assert result.exit_code == 0, result.output
        (run,) = [p for p in tmp_path.glob("[!.]*/*") if p.is_dir()]
        har = (run / "network.har").read_text()
        assert HIDDEN not in har
        assert "x=***REDACTED***" in json.loads(har)["log"]["entries"][0]["request"]["url"]


class TestIdentifiersMatchWholeValues:
    def test_a_short_username_does_not_eat_into_other_words(self) -> None:
        entry = {
            "request": {
                "url": "https://myshop.example/api/data/items",
                "headers": [{"name": "Referer", "value": "https://myshop.example/metadata"}],
                "postData": {"text": json.dumps({"data": [{"metadata": 1}], "who": "data"})},
            }
        }

        (clean,) = redact_har_entries([entry], Redaction(identifiers=("data",)))

        request = clean["request"]
        assert request["url"] == "https://myshop.example/api/data/items"
        assert request["headers"][0]["value"] == "https://myshop.example/metadata"
        assert json.loads(request["postData"]["text"]) == {
            REDACTED: [{"metadata": 1}],
            "who": REDACTED,
        }

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (f"user={USER}&pin=1", f"user={REDACTED}&pin=1"),
            ("user=bob%40example.com&pin=1", f"user={REDACTED}&pin=1"),
            ("user=bob%40example.com", f"user={REDACTED}"),
            (json.dumps({"u": USER}), json.dumps({"u": REDACTED})),
            (f"x{USER}y", f"x{USER}y"),
            (f"{USER}2", f"{USER}2"),
            # An escape sequence is a delimiter, though it ends in a letter or digit.
            (
                "returnUrl=%2Faccount%3Femail%3Dbob%40example.com",
                f"returnUrl=%2Faccount%3Femail%3D{REDACTED}",
            ),
            ("%22bob%40example.com%22", f"%22{REDACTED}%22"),
            ('{"msg": "hello\\nbob@example.com"}', f'{{"msg": "hello\\n{REDACTED}"}}'),
            ('{"u": "\\u0022bob@example.com\\u0022"}', f'{{"u": "\\u0022{REDACTED}\\u0022"}}'),
            ("&quot;bob@example.com&quot;", f"&quot;{REDACTED}&quot;"),
            ("&#34;bob@example.com&#34;", f"&#34;{REDACTED}&#34;"),
            # Lower-case hex, and the other JSON escapes.
            ("x%3dbob%40example.com", f"x%3d{REDACTED}"),
            ('{"m": "\\u002fbob@example.com"}', f'{{"m": "\\u002f{REDACTED}"}}'),
            ('{"m": "a\\rbob@example.com"}', f'{{"m": "a\\r{REDACTED}"}}'),
            ('{"m": "a\\tbob@example.com"}', f'{{"m": "a\\t{REDACTED}"}}'),
            ('{"m": "a\\bbob@example.com"}', f'{{"m": "a\\b{REDACTED}"}}'),
            ('{"m": "a\\fbob@example.com"}', f'{{"m": "a\\f{REDACTED}"}}'),
        ],
    )
    def test_in_its_raw_percent_encoded_and_json_forms(self, text: str, expected: str) -> None:
        entry = {"request": {"postData": {"text": text}}}

        (clean,) = redact_har_entries([entry], Redaction(identifiers=(USER,)))

        assert clean["request"]["postData"]["text"] == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("u%253Dbob", f"u%253D{REDACTED}"),
            ("next=%252Fusers%252Fbob", f"next=%252Fusers%252F{REDACTED}"),
            (
                "next=%252Faccount%253Femail%253Dbob%2540example.com",
                f"next=%252Faccount%253Femail%253D{REDACTED}",
            ),
        ],
    )
    def test_in_a_double_encoded_url(self, text: str, expected: str) -> None:
        entry = {"request": {"headers": [{"name": "Referer", "value": text}]}}

        (clean,) = redact_har_entries([entry], Redaction(identifiers=("bob", USER)))

        assert clean["request"]["headers"][0]["value"] == expected

    @pytest.mark.parametrize(
        "text",
        [
            "%41bob",  # "Abob"
            "%61bob",  # "abob"
            "Abob",
            "bob%41",  # "bobA"
            "\\\\nbob",  # an escaped backslash, then "nbob"
            '{"m": "\\\\u0020bob"}',  # an escaped backslash, then "u0020bob"
            '{"m": "\\u0041bob"}',  # "Abob"
        ],
    )
    def test_an_escape_of_a_letter_or_digit_does_not_split_a_word(self, text: str) -> None:
        entry = {"request": {"postData": {"text": text}}}

        (clean,) = redact_har_entries([entry], Redaction(identifiers=("bob",)))

        assert clean["request"]["postData"]["text"] == text

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("欢迎bob", f"欢迎{REDACTED}"),
            ("用户bob", f"用户{REDACTED}"),
            ("こんにちはbob様", f"こんにちは{REDACTED}様"),
            ("ébob", f"é{REDACTED}"),
            ("bobé", f"{REDACTED}é"),
            ('{"m": "\\u00e9bob"}', f'{{"m": "\\u00e9{REDACTED}"}}'),
            ("%E2%82%AAbob", f"%E2%82%AA{REDACTED}"),
        ],
    )
    def test_text_in_another_script_does_not_hide_a_value(self, text: str, expected: str) -> None:
        """A word is ASCII letters and digits, in text as in bytes."""
        entry = {"response": {"content": {"text": text}}}

        (clean,) = redact_har_entries([entry], Redaction(identifiers=("bob",)))

        assert clean["response"]["content"]["text"] == expected

    def test_a_shorter_form_is_tried_where_a_longer_one_is_inside_a_word(self) -> None:
        entry = {"request": {"postData": {"text": "bob@example.comx"}}}

        (clean,) = redact_har_entries([entry], Redaction(identifiers=("bob", USER)))

        assert clean["request"]["postData"]["text"] == f"{REDACTED}@example.comx"

    def test_a_secret_is_still_replaced_inside_other_text(self) -> None:
        entry = {"request": {"postData": {"text": f"x{HIDDEN}y"}}}

        (clean,) = redact_har_entries([entry], Redaction(secrets=(HIDDEN,)))

        assert clean["request"]["postData"]["text"] == f"x{REDACTED}y"

    def test_an_identifier_inside_a_binary_body_file_is_masked_whole_only(
        self, tmp_path: Path
    ) -> None:
        from graftpunk.observe.run import redact_request_body_files

        (tmp_path / "bodies").mkdir()
        body = b"\xff" + USER.encode() + b"\x00x" + USER.encode() + b"y"
        (tmp_path / "bodies" / "r1.request.bin").write_bytes(body)
        entries = [{"request": {"_postDataFile": "bodies/r1.request.bin"}}]

        redact_request_body_files(entries, Redaction(identifiers=(USER,)), tmp_path)

        masked = (tmp_path / "bodies" / "r1.request.bin").read_bytes()
        assert masked == b"\xff" + b"*" * len(USER) + b"\x00x" + USER.encode() + b"y"


class TestBasicCredentials:
    @staticmethod
    def _token() -> str:
        return base64.b64encode(f"bobb@example.com:{HIDDEN}".encode()).decode()

    @pytest.mark.parametrize("scheme", ["Basic", "basic", "BASIC"])
    def test_any_case_of_the_scheme(self, scheme: str) -> None:
        entry = {
            "request": {
                "headers": [{"name": "Authorization", "value": f"{scheme} {self._token()}"}]
            }
        }

        (clean,) = redact_har_entries([entry], Redaction(secrets=(HIDDEN,)))

        assert clean["request"]["headers"][0]["value"] == f"{scheme} {REDACTED}"

    @pytest.mark.parametrize("space", ["%20", "+"])
    def test_a_url_encoded_echo(self, space: str) -> None:
        import urllib.parse

        # A password whose token holds "/" and "+", sent as %2F and %2B.
        hidden = "x??~~>>"
        token = base64.b64encode(f"bob@example.com:{hidden}".encode()).decode()
        assert "/" in token
        assert "+" in token
        text = f"auth=Basic{space}{urllib.parse.quote(token, safe='')}&next=1"
        assert "%2F" in text
        entry = {"request": {"postData": {"text": text}}}

        (clean,) = redact_har_entries([entry], Redaction(secrets=(hidden,)))

        assert clean["request"]["postData"]["text"] == f"auth=Basic{space}{REDACTED}&next=1"

    def test_a_json_body_that_escapes_its_slashes(self) -> None:
        # A credential whose base64 holds "/" (escaped as "\\/" by some JSON encoders).
        token = base64.b64encode(f"bobb@example.com:{'hunter2-0?'}".encode()).decode()
        assert "/" in token
        text = json.dumps({"auth": f"Basic {token}"}).replace("/", "\\/")
        entry = {"response": {"content": {"text": text}}}

        (clean,) = redact_har_entries([entry], Redaction(secrets=("hunter2-0?",)))

        assert json.loads(clean["response"]["content"]["text"]) == {"auth": f"Basic {REDACTED}"}
