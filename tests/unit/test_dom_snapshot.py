"""The DOM snapshot taken at a credential post (#267, part 2): the sanitizer, its
redaction, the shared password-field rule, and the snapshot-only login group."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from graftpunk.har.digest import (
    DigestSource,
    digest,
    is_credential_body,
    password_field_names,
)
from graftpunk.har.documents import extract_login_forms
from graftpunk.observe.snapshot import (
    TEXT_LIMIT,
    sanitize_dom_snapshot,
    snapshot_redaction,
    stored_dom_snapshot,
)

_PASSWORD = "hunter2hunter2"  # noqa: S105 - a test value, not a credential
_EMAIL = "alice@example.com"


def _modal(extra: str = "") -> str:
    return (
        "<html><head><title>My shop, signed out</title>"
        "<style>.modal { color: red }</style>"
        '<script>window.__STATE__ = {"user": "alice@example.com", "csrf": "s3cr3t"};</script>'
        "</head><body>"
        '<header><a href="/help" class="nav" data-track="x1">Help and support</a></header>'
        '<div id="login-modal" class="modal" style="display:block" data-user="u-40912873">'
        '<label for="login-username">Email address</label>'
        f'<input type="email" id="login-username" name="username" value="{_EMAIL}" '
        'autocomplete="username" placeholder="you@example.com">'
        f'<input type="password" id="login-password" name="password" value="{_PASSWORD}">'
        '<input type="checkbox" name="remember" checked>'
        '<select name="region"><option value="us" selected>United States</option></select>'
        f"<textarea name='note'>{_EMAIL} wrote this</textarea>"
        '<button type="submit" id="login-submit" aria-label="Sign in now">Sign in</button>'
        f"<p>Welcome back, {_EMAIL}. Your last order shipped.</p>"
        f"<template><input name='tpl' value='{_PASSWORD}'></template>"
        f"{extra}</div></body></html>"
    )


class TestSanitizer:
    def test_no_value_attribute_and_no_selection_state_is_kept(self) -> None:
        out = sanitize_dom_snapshot(_modal())
        assert "value=" not in out
        assert "checked" not in out
        assert "selected" not in out
        assert _PASSWORD not in out

    def test_script_style_and_template_contents_are_dropped(self) -> None:
        out = sanitize_dom_snapshot(_modal())
        assert "__STATE__" not in out
        assert "s3cr3t" not in out
        assert "color: red" not in out
        assert "tpl" not in out
        assert "<script></script>" in out
        assert "<template></template>" in out

    def test_text_is_kept_only_inside_the_labelling_elements(self) -> None:
        out = sanitize_dom_snapshot(_modal())
        assert ">Email address</label>" in out
        assert ">Sign in</button>" in out
        assert ">Help and support</a>" in out
        assert ">United States</option>" in out
        # A paragraph's text, the title, and a textarea's text are not kept.
        assert "Welcome back" not in out
        assert "signed out" not in out
        assert "wrote this" not in out

    def test_the_attributes_the_digest_reads_are_kept_and_no_other(self) -> None:
        out = sanitize_dom_snapshot(_modal())
        for kept in (
            'id="login-modal"',
            'class="modal"',
            'type="email"',
            'name="username"',
            'autocomplete="username"',
            'for="login-username"',
            'href="/help"',
        ):
            assert kept in out
        for dropped in ("style=", "data-user", "data-track", "placeholder", "aria-label"):
            assert dropped not in out

    def test_kept_text_is_truncated(self) -> None:
        long_label = "x" * 200
        out = sanitize_dom_snapshot(f"<a href='/'>{long_label}<span>{long_label}</span></a>")
        assert out.count("x") == TEXT_LIMIT

    def test_the_output_is_still_readable_by_the_digest(self) -> None:
        (form,) = extract_login_forms(
            sanitize_dom_snapshot(
                _modal().replace('id="login-modal"', 'id="login-modal" role="dialog"')
            ),
            source="https://myshop.example/",
        )
        assert form.fields == {"username": "#login-username", "password": "#login-password"}
        assert form.submit == "#login-submit"

    def test_markup_in_a_kept_attribute_is_escaped(self) -> None:
        out = sanitize_dom_snapshot('<a href="/x&quot;><script>alert(1)</script>">go</a>')
        assert "<script>alert" not in out
        assert 'href="/x&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"' in out


class TestRedaction:
    def test_the_post_s_own_values_are_redacted(self) -> None:
        body = json.dumps({"username": _EMAIL, "password": _PASSWORD, "remember": "1"})
        html = '<a href="/u/alice@example.com">alice@example.com</a>'
        html += f"<button>{_PASSWORD}</button>"
        out = stored_dom_snapshot(html, body, "application/json")
        assert _EMAIL not in out
        assert _PASSWORD not in out

    def test_secrets_are_password_fields_and_identifiers_by_the_allowlist(self) -> None:
        body = "user_name=alice&login_password=pa55word&remember=yes"
        redaction = snapshot_redaction(body, "application/x-www-form-urlencoded")
        assert redaction.secrets == ("pa55word",)
        assert redaction.identifiers == ("alice",)

    def test_a_body_with_no_fields_redacts_nothing(self) -> None:
        assert not snapshot_redaction("not a body", "text/plain")
        assert not snapshot_redaction(None, "")


def _credential_posts(tmp_path: Path, body: str, content_type: str) -> list[str]:
    """The digest's credential posts in a run of one GET and one POST of *body*."""
    entries = [
        {
            "startedDateTime": "2026-10-24T10:00:00.000Z",
            "time": 1,
            "request": {
                "method": method,
                "url": f"https://myshop.example/{path}",
                "headers": [{"name": "Content-Type", "value": content_type}],
                "cookies": [],
                **({"postData": {"mimeType": content_type, "text": body}} if post else {}),
            },
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "cookies": [],
                "content": {"mimeType": "application/json", "text": "{}"},
            },
        }
        for method, path, post in (("GET", "api/me", False), ("POST", "api/session", True))
    ]
    har = tmp_path / "network.har"
    har.write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}))
    result = digest(DigestSource.from_har(har))
    return [o.url for o in result.login if o.kind == "credential_post"]


class TestSharedPasswordFieldRule:
    @pytest.mark.parametrize(
        ("body", "content_type"),
        [
            ('{"username": "a", "password": "b"}', "application/json"),
            ("login=a&passwd=b", "application/x-www-form-urlencoded"),
            ('{"q": "password"}', "application/json"),
            ("<xml password='x'/>", "text/xml"),
            ('{"email": "a", "new_password": "b"}', "application/json"),
            ("user=a&pwd=b", "text/plain"),
        ],
    )
    def test_the_digest_and_the_recorder_read_one_rule(
        self, tmp_path: Path, body: str, content_type: str
    ) -> None:
        """A body the recorder snapshots for is one the digest calls a credential
        post by its field names, and no other."""
        posts = _credential_posts(tmp_path, body, content_type)
        assert bool(posts) == is_credential_body(body, content_type)

    def test_the_names_are_the_password_like_ones(self) -> None:
        body = '{"login_pwd": "x", "q": "y", "password": null}'
        assert password_field_names(body, "application/json; charset=utf-8") == ["login_pwd"]
        # A form body declared as something else is no form, as in the digest.
        assert password_field_names("password=x", "text/plain") == []

    def test_a_login_body_is_a_credential_body(self) -> None:
        assert is_credential_body('{"email": "a", "password": "b"}', "application/json")
        assert is_credential_body("user=a&pwd=b", "application/x-www-form-urlencoded")

    def test_a_new_password_body_and_a_body_without_one_are_not(self) -> None:
        assert not is_credential_body(
            '{"email": "a", "new_password": "b", "password_confirm": "b"}', "application/json"
        )
        assert not is_credential_body('{"query": "alice"}', "application/json")
        assert not is_credential_body(None, "")


_SITE = "https://myshop.example/"


class TestASnapshotLoginGroupOutsideAnyDialog:
    """In a snapshot, a password outside every form and every dialog makes a group
    bounded by its nearest ancestor holding a button-like control after it."""

    _DIV_LOGIN = (
        '<div class="page"><input type="search" name="q" id="q">'
        '<div class="login-box" id="login-box">'
        '<input type="text" id="login-username" name="username">'
        '<input type="password" id="login-password" name="password">'
        '<button type="submit" id="login-submit">Sign in</button></div></div>'
    )

    def test_recorded_html_still_gives_no_group(self) -> None:
        assert extract_login_forms(self._DIV_LOGIN, source=_SITE) == ()

    def test_a_snapshot_gives_one_group_bounded_by_the_block(self) -> None:
        (form,) = extract_login_forms(self._DIV_LOGIN, source=_SITE, snapshot=True)
        assert form.formless
        assert form.plain_block
        assert form.from_snapshot
        assert form.fields == {"username": "#login-username", "password": "#login-password"}
        assert form.submit == "#login-submit"
        assert "q" not in form.input_names

    def test_a_typeless_sign_in_bounds_the_group_and_its_submit_is_a_gp_fill(self) -> None:
        html = self._DIV_LOGIN.replace('<button type="submit"', "<button")
        (form,) = extract_login_forms(html, source=_SITE, snapshot=True)
        assert form.fields["username"] == "#login-username"
        assert form.submit is None
        assert "submit" in form.unresolved_roles

    def test_a_password_with_no_button_after_it_gives_no_group(self) -> None:
        html = (
            '<div><input type="text" name="username" id="u">'
            '<input type="password" name="password" id="p"></div>'
        )
        assert extract_login_forms(html, source=_SITE, snapshot=True) == ()

    def test_body_is_never_the_bound(self) -> None:
        html = (
            '<html><body><input type="text" name="username" id="u">'
            '<input type="password" name="password" id="p">'
            '<button type="submit" id="s">Go</button></body></html>'
        )
        assert extract_login_forms(html, source=_SITE, snapshot=True) == ()

    def test_a_sign_up_block_gives_none(self) -> None:
        html = (
            '<div id="signup"><input type="email" name="email" id="e">'
            '<input type="password" name="password" id="p" autocomplete="new-password">'
            '<button type="submit" id="s">Join</button></div>'
        )
        assert extract_login_forms(html, source=_SITE, snapshot=True) == ()

    def test_a_dialog_in_a_snapshot_stays_a_dialog_group(self) -> None:
        html = self._DIV_LOGIN.replace('class="login-box"', 'class="login-box" role="dialog"')
        (form,) = extract_login_forms(html, source=_SITE, snapshot=True)
        assert form.formless
        assert not form.plain_block
        assert form.from_snapshot

    def test_many_nested_passwords_parse_in_linear_time(self) -> None:
        """Each walk up skips what an earlier password's walk found buttonless, and
        a group with several unmarked passwords is never gathered."""
        import time

        depth = 20000
        html = '<div><input type="password">' * depth + "</div>" * depth
        html = f"<main><section>{html}<button>Go</button></section></main>"
        started = time.perf_counter()
        assert extract_login_forms(html, source=_SITE, snapshot=True) == ()
        assert time.perf_counter() - started < 2.0

    def test_two_passwords_one_marked_current_give_one_group(self) -> None:
        html = (
            '<div id="reauth"><input type="password" id="old" autocomplete="current-password">'
            '<input type="password" id="pin"><button type="submit" id="s">Go</button></div>'
        )
        (form,) = extract_login_forms(html, source=_SITE, snapshot=True)
        assert form.fields == {"password": "#old"}

    def test_a_real_form_in_a_snapshot_is_read_as_a_form(self) -> None:
        html = (
            '<form id="login"><input name="username" id="u">'
            '<input type="password" name="password" id="p"><button>Go</button></form>'
        )
        (form,) = extract_login_forms(html, source=_SITE, snapshot=True)
        assert not form.formless
        assert form.from_snapshot
