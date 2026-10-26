"""The DOM snapshot taken at a credential post (#267, part 2): the sanitizer, its
redaction, the shared password-field rule, and the snapshot-only login group."""

from __future__ import annotations

import json
from collections.abc import Callable
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

    def test_a_password_with_no_button_after_it_is_a_group_alone(self) -> None:
        html = (
            '<div><input type="text" name="username" id="u">'
            '<input type="password" name="password" id="p"></div>'
        )
        (form,) = extract_login_forms(html, source=_SITE, snapshot=True)
        assert form.fields == {"password": "#p"}
        assert form.block_bound == "alone"
        assert set(form.absent_roles) == {"username", "submit"}

    def test_body_is_never_the_bound(self) -> None:
        html = (
            '<html><body><input type="text" name="username" id="u">'
            '<input type="password" name="password" id="p">'
            '<button type="submit" id="s">Go</button></body></html>'
        )
        (form,) = extract_login_forms(html, source=_SITE, snapshot=True)
        assert form.fields == {"password": "#p"}
        assert form.block_bound == "alone"

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
        """A walk stops at the first ancestor holding more than four text or password
        inputs, so no element is climbed past by more than four passwords."""

        def page(depth: int) -> str:
            nested = '<div><input type="password">' * depth + "</div>" * depth
            return f"<main><section>{nested}<button>Go</button></section></main>"

        _assert_snapshot_parses_in_linear_time(page)
        # Every password's walk stops at an element holding several passwords, none
        # marked current: a sign-up shape, so no password of it is a login.
        assert extract_login_forms(page(200), source=_SITE, snapshot=True) == ()

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

    def test_a_show_password_toggle_does_not_bound_the_group(self) -> None:
        """Review I3: the toggle's wrapper holds no input before the password, so the
        group is the login block around it, and the username is found."""
        html = (
            '<main><div class="login"><input type="email" id="u" name="email">'
            '<div class="pw-wrap"><input type="password" id="p">'
            '<button type="button" id="eye">Show</button></div>'
            '<button type="button" id="go">Sign in</button></div></main>'
        )
        (form,) = extract_login_forms(html, source=_SITE, snapshot=True)
        assert form.fields == {"username": "#u", "password": "#p"}
        assert form.plain_block
        assert form.block_bound == "inputs"

    def test_a_password_step_alone_is_bounded_by_its_block(self) -> None:
        """No element holds an input before the password: the nearest one holding a
        button after it bounds the group, as for a password-only login step."""
        html = (
            '<main><div class="step"><input type="password" id="p">'
            '<button type="submit" id="next">Next</button></div></main>'
        )
        (form,) = extract_login_forms(html, source=_SITE, snapshot=True)
        assert form.fields == {"password": "#p"}
        assert form.submit == "#next"
        assert form.absent_roles == ("username",)
        assert form.block_bound == "button"

    @pytest.mark.parametrize(
        "before",
        [
            '<header><input type="text" name="q" id="q"><button>Search</button></header>',
            '<nav><input type="text" name="zip" id="zip"><button>Find a store</button></nav>',
            '<aside><input type="email" name="email" id="nl"><button>Subscribe</button></aside>',
        ],
        ids=["header search", "store finder", "newsletter"],
    )
    def test_an_unrelated_input_above_a_password_step_is_not_its_username(
        self, before: str
    ) -> None:
        """Review round 2 (I2): a button between that input and the password says the
        input belongs to something else, so the step is a password-only group."""
        html = (
            f'<div id="app">{before}<main><div class="pw-step">'
            '<input type="password" name="password" id="pw">'
            '<button type="button" id="next">Continue</button></div></main></div>'
        )
        (form,) = extract_login_forms(html, source=_SITE, snapshot=True)
        assert form.fields == {"password": "#pw"}
        assert form.block_bound == "button"
        assert "username" in form.absent_roles

    def test_a_password_only_group_starts_at_the_password(self) -> None:
        """An input before the password with a button between them is something else,
        so the group holds no control before the password."""
        html = (
            '<main><div id="box"><input type="text" id="u" name="user">'
            '<button type="button" id="help">Help</button>'
            '<input type="password" id="p" name="password">'
            '<button type="submit" id="go">Go</button></div></main>'
        )
        (form,) = extract_login_forms(html, source=_SITE, snapshot=True)
        assert form.fields == {"password": "#p"}
        assert form.submit == "#go"
        assert form.block_bound == "button"

    def test_a_login_in_a_page_with_many_controls_elsewhere_is_found(self) -> None:
        nav = "".join(f'<input type="checkbox" name="f{i}" id="f{i}">' for i in range(12))
        html = (
            f'<div id="app"><aside>{nav}<button>Apply</button></aside>'
            '<div class="login"><input type="email" id="u" name="email">'
            '<input type="password" id="p" name="password">'
            '<button type="submit" id="go">Sign in</button></div></div>'
        )
        (form,) = extract_login_forms(html, source=_SITE, snapshot=True)
        assert form.fields == {"username": "#u", "password": "#p"}
        assert form.block_bound == "inputs"

    def test_an_element_with_more_than_four_inputs_never_bounds_a_group(self) -> None:
        """The wrapper holds the username, but five text and password inputs: a page,
        not a login."""
        extra = "".join(f'<input type="text" name="f{i}">' for i in range(3))
        html = (
            f'<div id="wrap"><input type="email" id="u" name="email">{extra}'
            '<div class="pw"><input type="password" id="p" name="password"></div>'
            '<button type="submit" id="go">Go</button></div>'
        )
        (form,) = extract_login_forms(html, source=_SITE, snapshot=True)
        assert form.fields == {"password": "#p"}
        assert form.block_bound == "alone"

    def test_an_element_with_exactly_four_inputs_bounds_its_group(self) -> None:
        extra = "".join(f'<input type="text" name="f{i}">' for i in range(2))
        html = (
            f'<div id="wrap"><input type="email" id="u" name="email">{extra}'
            '<div class="pw"><input type="password" id="p" name="password"></div>'
            '<button type="submit" id="go">Go</button></div>'
        )
        (form,) = extract_login_forms(html, source=_SITE, snapshot=True)
        assert form.fields["username"] == "#u"
        assert form.block_bound == "inputs"

    _CARD = (
        '<div class="card"><input type="email" id="u" name="email">'
        '<div class="pw"><input type="password" id="p" name="password">'
        '<button type="button" id="eye">Show</button></div>'
        '<input type="checkbox" id="rm" name="remember">'
        '<button type="submit" id="go">Sign in</button>{extra}</div>'
    )

    def test_a_card_with_remember_me_and_four_providers_keeps_its_username(self) -> None:
        """Review round 3: buttons and checkboxes do not count toward the bound."""
        providers = "".join(f'<button type="button">Continue with P{i}</button>' for i in range(4))
        html = self._CARD.format(extra=providers)
        (form,) = extract_login_forms(f"<main>{html}</main>", source=_SITE, snapshot=True)
        assert form.fields == {"username": "#u", "password": "#p"}
        assert form.block_bound == "inputs"

    def test_a_card_with_six_hidden_inputs_keeps_its_username(self) -> None:
        hidden = "".join(f'<input type="hidden" name="h{i}">' for i in range(6))
        html = self._CARD.format(extra=hidden)
        (form,) = extract_login_forms(f"<main>{html}</main>", source=_SITE, snapshot=True)
        assert form.fields == {"username": "#u", "password": "#p"}

    def test_a_sign_up_block_before_a_login_card_gives_only_the_login(self) -> None:
        """Review round 3 (I1): the sign-up block's walk stops at the block, which
        holds two passwords and none marked current, so neither is a login."""
        signup = (
            '<div class="signup"><input type="text" name="first_name" id="fn">'
            '<input type="text" name="last_name" id="ln">'
            '<input type="email" name="email" id="se">'
            '<input type="tel" name="phone" id="ph">'
            '<div class="row"><input type="password" name="password" id="np"></div>'
            '<div class="row"><input type="password" name="password_confirmation" id="cp"></div>'
            '<input type="checkbox" name="terms"><input type="checkbox" name="news">'
            '<button type="button">Create account</button></div>'
        )
        providers = "".join(f'<button type="button">Continue with P{i}</button>' for i in range(4))
        html = f"<main>{signup}{self._CARD.format(extra=providers)}</main>"
        forms = extract_login_forms(html, source=_SITE, snapshot=True)
        assert [form.fields for form in forms] == [{"username": "#u", "password": "#p"}]

    def test_a_sign_up_block_with_show_toggles_gives_none(self) -> None:
        """Review round 3 (toolkit M3): the confirm field's group starts at it, but the
        element bounding it holds the first password too."""
        html = (
            '<main><div class="signup"><input type="email" id="em" name="email">'
            '<input type="password" id="np1" name="pass1"><button type="button">Show</button>'
            '<input type="password" id="np2" name="pass2"><button type="button">Show</button>'
            '<button type="button">Register</button></div></main>'
        )
        assert extract_login_forms(html, source=_SITE, snapshot=True) == ()

    def test_a_new_password_alone_is_never_a_group(self) -> None:
        html = '<main><div><input type="password" id="np" name="new_password"></div></main>'
        assert extract_login_forms(html, source=_SITE, snapshot=True) == ()

    def test_a_change_password_block_gives_none(self) -> None:
        """The current password is marked, but the block also asks for a new one."""
        html = (
            '<main><div class="change"><input type="password" id="old" '
            'autocomplete="current-password"><input type="password" id="np" '
            'autocomplete="new-password"><button type="submit">Save</button></div></main>'
        )
        assert extract_login_forms(html, source=_SITE, snapshot=True) == ()

    def test_a_large_settings_block_asking_for_a_new_password_gives_none(self) -> None:
        """The walk stops at the settings block, which asks for a new password: the
        marked current password in it is no login either."""
        html = (
            '<main><div class="settings"><input type="text" name="name">'
            '<input type="email" name="email"><input type="tel" name="phone">'
            '<div class="row"><input type="password" id="cur" autocomplete="current-password">'
            '</div><div class="row"><input type="password" id="np" '
            'autocomplete="new-password"></div><button type="submit">Save</button></div></main>'
        )
        assert extract_login_forms(html, source=_SITE, snapshot=True) == ()

    def test_a_new_password_is_never_a_group(self) -> None:
        html = (
            '<main><div class="step"><input type="password" id="np" name="new_password">'
            '<button type="submit">Save</button></div></main>'
        )
        assert extract_login_forms(html, source=_SITE, snapshot=True) == ()

    def test_an_input_with_no_username_hint_is_not_the_username(self) -> None:
        """A buttonless header input above a password step is no username."""
        html = (
            '<div id="app"><header><input type="text" name="q" id="q"></header>'
            '<div class="pw-step"><input type="password" name="password" id="pw">'
            '<button type="button" id="next">Continue</button></div></div>'
        )
        (form,) = extract_login_forms(html, source=_SITE, snapshot=True)
        assert form.fields == {"password": "#pw"}
        assert "username" in form.absent_roles

    def test_a_hinted_input_after_a_search_box_s_button_is_the_username(self) -> None:
        """Review round 3 (superpowers M4): the group starts after the last button
        before the username, so the search box is not in it."""
        html = (
            '<main><div id="box"><input type="text" id="q" name="login_search">'
            '<button type="button">Go</button>'
            '<input type="text" id="acct" name="account">'
            '<input type="password" id="p" name="password">'
            '<button type="submit" id="go">Sign in</button></div></main>'
        )
        (form,) = extract_login_forms(html, source=_SITE, snapshot=True)
        assert form.fields == {"username": "#acct", "password": "#p"}
        assert "login_search" not in form.input_names

    def test_the_nearest_qualifying_element_bounds_a_password_only_group(self) -> None:
        """Review round 3 (superpowers M7): the inner element wins over the outer one,
        which also holds an unrelated control after the password."""
        html = (
            '<main><div id="outer"><div id="inner"><input type="password" id="p">'
            '<button type="submit" id="go">Next</button></div>'
            '<input type="text" id="promo" name="promo">'
            '<button type="button">Apply</button></div></main>'
        )
        (form,) = extract_login_forms(html, source=_SITE, snapshot=True)
        assert form.input_names == ()
        assert form.submit == "#go"
        assert form.block_bound == "button"

    def test_a_text_input_in_a_form_or_a_dialog_does_not_widen_the_bound(self) -> None:
        """Only formless text inputs outside every dialog count as a username."""
        html = (
            '<div id="app"><form><input type="text" name="q" id="q"></form>'
            '<div role="dialog"><input type="text" name="promo" id="promo"></div>'
            '<div class="pw-step"><input type="password" name="password" id="pw">'
            '<button type="button" id="next">Continue</button></div></div>'
        )
        forms = extract_login_forms(html, source=_SITE, snapshot=True)
        (block,) = [form for form in forms if form.plain_block]
        assert block.fields == {"password": "#pw"}
        assert block.block_bound == "button"

    def test_a_button_only_before_the_password_bounds_nothing(self) -> None:
        html = (
            '<main><div id="box"><input type="text" id="u" name="user">'
            '<button type="button" id="b">Help</button>'
            '<input type="password" id="p" name="password"></div></main>'
        )
        (form,) = extract_login_forms(html, source=_SITE, snapshot=True)
        assert form.fields == {"password": "#p"}
        assert form.block_bound == "alone"

    def test_two_unmarked_passwords_in_one_block_give_none(self) -> None:
        html = (
            '<main><div id="box"><input type="text" id="u" name="user">'
            '<input type="password" id="p1" name="secret">'
            '<input type="password" id="p2" name="pin">'
            '<button type="submit" id="s">Go</button></div></main>'
        )
        assert extract_login_forms(html, source=_SITE, snapshot=True) == ()


class TestRedactionBeforeEscaping:
    """Review I1: a value holding ``&``, ``<``, ``>``, ``"`` or ``'`` is redacted on its
    decoded text, before the sanitizer escapes it again."""

    PASSWORD = "p&ss<w>\"o'rd9"  # noqa: S105 - a test value, not a credential
    EMAIL = "o'brien&co@example.com"

    def _stored(self) -> str:
        from html import escape

        pw, em = escape(self.PASSWORD), escape(self.EMAIL)
        page = (
            f"<label>{pw}</label><button>Not {em}?</button>"
            f'<a href="/u/{em}">me</a><a href="/forgot/{em.replace("&#x27;", chr(39))}">x</a>'
            f'<div id="{em}" class="x {pw}" data-x="{pw}"></div>'
        )
        body = json.dumps({"email": self.EMAIL, "password": self.PASSWORD})
        return stored_dom_snapshot(page, body, "application/json")

    def test_no_spelling_of_either_value_is_stored(self) -> None:
        from html import escape

        out = self._stored()
        for value in (self.PASSWORD, self.EMAIL):
            for spelled in (value, escape(value), escape(value, quote=False)):
                assert spelled not in out
        assert "p&amp;ss" not in out
        assert "brien" not in out
        assert "***REDACTED***" in out

    def test_the_escaped_forms_are_a_second_net(self) -> None:
        from html import escape

        body = json.dumps({"email": self.EMAIL, "password": self.PASSWORD})
        redaction = snapshot_redaction(body, "application/json")
        assert {escape(self.PASSWORD), escape(self.PASSWORD, quote=False)} <= set(redaction.secrets)
        assert escape(self.EMAIL) in redaction.identifiers


class TestUrlsKeepNoQueryOrFragment:
    def test_href_and_action_lose_their_query_and_fragment(self) -> None:
        out = sanitize_dom_snapshot(
            '<a href="/account?token=abc123SECRET&amp;x=1#top">a</a>'
            '<a href="#reviews">b</a><a href="javascript:go(40912873)">c</a>'
            '<form action="https://myshop.example/login?next=%2Forders#x"></form>'
        )
        assert "abc123SECRET" not in out
        assert "40912873" not in out
        assert 'href="/account?"' in out
        assert 'href="#"' in out
        assert 'href="javascript:"' in out
        assert 'action="https://myshop.example/login?"' in out

    def test_a_stripped_action_still_scopes_to_the_live_form(self) -> None:
        html = sanitize_dom_snapshot(
            '<main><form action="/login?next=%2F" method="post"><input name="user">'
            '<input type="password" name="pw"><button>Go</button></form></main>'
        )
        (form,) = extract_login_forms(html, source=_SITE, snapshot=True)
        assert 'form[action^="/login?"] input[name="pw"]' in form.fields["password"]


class TestSanitizerGuards:
    def test_a_textarea_in_a_label_keeps_no_text(self) -> None:
        out = sanitize_dom_snapshot("<label>Note <textarea>typed by alice</textarea></label>")
        assert "typed by alice" not in out
        assert ">Note <textarea></textarea></label>" in out

    def test_nested_labelling_elements_share_one_budget(self) -> None:
        out = sanitize_dom_snapshot(f"<label>{'x' * 60}<a href='/'>{'y' * 60}</a></label>")
        assert out.count("x") + out.count("y") == TEXT_LIMIT

    def test_the_sanitizer_redacts_decoded_values_itself(self) -> None:
        """The first net on its own, without stored_dom_snapshot's second pass."""
        from graftpunk.observe.run import Redaction

        out = sanitize_dom_snapshot(
            '<label>p&amp;ss&lt;9</label><div class="a p&amp;ss&lt;9"></div>',
            Redaction(secrets=("p&ss<9",)),
        )
        assert "p&amp;ss" not in out
        assert out.count("***REDACTED***") == 2

    def test_nested_logins_each_with_their_own_block_parse_in_linear_time(self) -> None:
        """Each block bounds its own password, and no walk climbs past an element
        holding more than four text or password inputs."""

        def page(depth: int) -> str:
            html = '<div><input type="text"><input type="password"><button>Go</button>' * depth
            return f"<main>{html}{'</div>' * depth}</main>"

        _assert_snapshot_parses_in_linear_time(page)

    def test_a_value_past_the_text_budget_is_redacted_before_the_cut(self) -> None:
        """Redact, then truncate: cut first, the budget keeps the value's start."""
        password = "hunter22"  # noqa: S105 - a test value, not a credential
        out = stored_dom_snapshot(
            f"<label>{'x' * 75}{password}</label>",
            json.dumps({"password": password}),
            "application/json",
        )
        assert "hunte" not in out

    def test_a_value_split_by_a_comment_is_caught_by_the_second_net(self) -> None:
        """The chunks around a comment are redacted one at a time and miss it; joined
        in the stored markup, the second pass finds it."""
        password = "hunter22"  # noqa: S105 - a test value, not a credential
        out = stored_dom_snapshot(
            "<label>hun<!-- x -->ter22</label>",
            json.dumps({"password": password}),
            "application/json",
        )
        assert password not in out

    def test_a_value_split_across_elements_is_the_documented_limit(self) -> None:
        """Redaction reads one text at a time: a value the page renders across
        elements is kept. HOW_IT_WORKS names this limit."""
        out = stored_dom_snapshot(
            "<label>hun<b>ter22</b></label>",
            json.dumps({"password": "hunter22"}),
            "application/json",
        )
        assert "<label>hun<b>ter22</b></label>" in out


def _snapshot_parse_seconds(html: str) -> float:
    """The parse's CPU time, the best of three with the cyclic collector off: wall
    time on a busy runner, or a collection landing in one run, is not the parse."""
    import gc
    import time

    best = float("inf")
    collecting = gc.isenabled()
    gc.disable()
    try:
        for _ in range(3):
            started = time.process_time()
            extract_login_forms(html, source=_SITE, snapshot=True)
            best = min(best, time.process_time() - started)
    finally:
        if collecting:
            gc.enable()
    return best


def _assert_snapshot_parses_in_linear_time(page: Callable[[int], str]) -> None:
    """Four times the input takes well under sixteen times as long, as part 1's
    check in test_har_documents.py does, timed in CPU time as the best of three runs
    (:func:`_snapshot_parse_seconds`). A heavily loaded runner can still slow the two
    sizes unevenly."""
    small = _snapshot_parse_seconds(page(1000))
    large = _snapshot_parse_seconds(page(4000))
    assert large < 8 * small + 0.05, f"1000: {small:.3f}s, 4000: {large:.3f}s"


class TestSnapshotParseGrowth:
    """A snapshot's parse is linear in the page's length, whatever its shape."""

    def test_a_wide_page_of_login_blocks(self) -> None:
        block = (
            '<div class="row"><div><input type="text"><input type="password">'
            "<button>Go</button></div></div>"
        )
        _assert_snapshot_parses_in_linear_time(lambda n: f"<main>{block * n}</main>")

    def test_a_wide_page_whose_blocks_share_input_names(self) -> None:
        """Each name is checked for uniqueness against counts taken once."""
        block = (
            '<div class="row"><div><input type="text" name="username">'
            '<input type="password" name="password"><button>Go</button></div></div>'
        )
        _assert_snapshot_parses_in_linear_time(lambda n: f"<main>{block * n}</main>")

    def test_a_wide_page_of_uniquely_named_blocks(self) -> None:
        _assert_snapshot_parses_in_linear_time(
            lambda n: (
                "<main>"
                + "".join(
                    f'<div class="row"><div><input type="text" name="u{i}">'
                    f'<input type="password" name="p{i}"><button>Go</button></div></div>'
                    for i in range(n)
                )
                + "</main>"
            )
        )

    def test_a_page_of_many_login_forms_sharing_one_action(self) -> None:
        """Forms sharing an action are counted together once, not once per form."""
        form = (
            '<form action="/login" method="post"><input name="username">'
            '<input type="password" name="password"><button>Go</button></form>'
        )
        _assert_snapshot_parses_in_linear_time(lambda n: f"<main>{form * n}</main>")


class TestRedactionSpellings:
    """Review round 2: the spellings a page can show a post's own value in."""

    def _stored(self, page: str, body: str, content_type: str = "application/json") -> str:
        return stored_dom_snapshot(page, body, content_type)

    def test_an_email_in_another_letter_case_is_redacted(self) -> None:
        out = self._stored(
            '<label>ALICE@EXAMPLE.COM</label><a href="/u/Alice@Example.Com/x">me</a>'
            '<div id="alice@EXAMPLE.com"></div><a href="/v/ALICE%40EXAMPLE.COM/x">p</a>',
            json.dumps({"email": "alice@example.com", "password": "hunter2hunter2"}),
        )
        assert "example.com" not in out.lower()

    def test_an_email_in_another_case_is_redacted_before_the_text_budget(self) -> None:
        """The sanitizer's own pass matches any case too: cut first, the budget
        keeps the start of the email, which the markup pass cannot find."""
        out = self._stored(
            f"<label>{'x' * 75}ALICE@EXAMPLE.COM</label>",
            json.dumps({"email": "alice@example.com", "password": "hunter2hunter2"}),
        )
        assert "alice" not in out.lower()

    def test_a_nested_rails_name_is_read_by_its_last_part(self) -> None:
        body = "form%5Bprofile%5D%5Blogin%5D=alice&form%5Bprofile%5D%5Bpassword%5D=hunter2hunter2"
        redaction = snapshot_redaction(body, "application/x-www-form-urlencoded")
        assert "alice" in redaction.identifiers

    def test_an_encode_uri_component_spelling_is_redacted(self) -> None:
        out = self._stored(
            '<a href="/u/o\'brien%40example.com/x">x</a><a href="/v/o\'brien%40example.com">y</a>',
            json.dumps({"email": "o'brien@example.com", "password": "hunter2hunter2"}),
        )
        assert "brien" not in out

    def test_a_rails_style_identifier_is_redacted(self) -> None:
        body = "user%5Bemail%5D=alice%40example.com&user%5Bpassword%5D=hunter2hunter2"
        redaction = snapshot_redaction(body, "application/x-www-form-urlencoded")
        assert "alice@example.com" in redaction.identifiers
        out = self._stored(
            "<button>Not alice@example.com?</button>", body, "application/x-www-form-urlencoded"
        )
        assert "alice@example.com" not in out

    @pytest.mark.parametrize(
        ("href", "kept"),
        [
            (" JavaScript:go('tok_8812')", "javascript:"),
            ("java&#9;script:open(&quot;tok_8812&quot;)", "javascript:"),
            ("\x01javascript:go('tok_8812')", "javascript:"),
            ("DATA:text/html,tok_8812", "data:"),
            ("VBScript:msgbox(tok_8812)", "vbscript:"),
        ],
    )
    def test_script_and_data_urls_keep_only_their_scheme(self, href: str, kept: str) -> None:
        out = sanitize_dom_snapshot(f'<a href="{href}">x</a>')
        assert "tok_8812" not in out
        assert f'href="{kept}"' in out

    def test_path_params_keep_only_their_marker(self) -> None:
        out = sanitize_dom_snapshot(
            '<a href="/shop;jsessionid=9F2A71C0DEADBEEF/cart?x=1">a</a>'
            '<form action="/login;jsessionid=ABCDEF123"></form>'
        )
        assert "9F2A71C0" not in out
        assert "ABCDEF123" not in out
        assert 'href="/shop;/cart?"' in out
        assert 'action="/login;"' in out

    def test_a_param_stripped_action_still_scopes_to_the_live_form(self) -> None:
        html = sanitize_dom_snapshot(
            '<main><form action="/login;jsessionid=AB12" method="post"><input name="user">'
            '<input type="password" name="pw"><button>Go</button></form></main>'
        )
        (form,) = extract_login_forms(html, source=_SITE, snapshot=True)
        assert 'form[action^="/login;"] input[name="pw"]' in form.fields["password"]
