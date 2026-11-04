"""#365: a login control with no usable id or name is selected by a stable attribute
unique on the page (autocomplete, a password's or a submit button's type, placeholder,
aria-label, then a class whose every part reads as a word and holds no digit), each value
exactly as written, never a redaction, and never by a button's text."""

from __future__ import annotations

import re

import pytest

from graftpunk.har.documents import extract_login_forms, is_build_hashed_class

_SITE = "https://myshop.example.com/log-in"


def _dialog(inner: str, after: str = "") -> str:
    return f'<html><body><div role="dialog">{inner}</div>{after}</body></html>'


def _only(html: str, *, snapshot: bool = False):  # noqa: ANN202
    (form,) = extract_login_forms(html, source=_SITE, snapshot=snapshot)
    return form


def _css_unescaped(spelled: str) -> str:
    """The value a double-quoted CSS string *spelled* (its inside) stands for, by the
    CSS Syntax escape rules: a backslash and one to six hex digits (one following
    space eaten) is that code point; a backslash and any other character is that
    character. An unescaped quote would end the string early, so it fails here."""
    out: list[str] = []
    at = 0
    while at < len(spelled):
        ch = spelled[at]
        assert ch != '"', f"unescaped quote in {spelled!r}"
        if ch != "\\":
            out.append(ch)
            at += 1
            continue
        hex_digits = re.match(r"[0-9a-fA-F]{1,6}", spelled[at + 1 :])
        if hex_digits:
            out.append(chr(int(hex_digits.group(), 16)))
            at += 1 + len(hex_digits.group())
            at += spelled[at : at + 1] == " "
        else:
            out.append(spelled[at + 1])
            at += 2
    return "".join(out)


class TestBuildHashedClasses:
    @pytest.mark.parametrize(
        "token",
        [
            "tY7r3Qp",
            "css-1x9k3tq",
            "Button_primary__3kR9v",
            "_3kR9v",
            "jsx-2843710157",
            "Header__QwErT",
            "header__loggedIn___abcDE",
            "a3f9c2d1e0b4",
            # styled-components: a component id and a class hash, letters only.
            "sc-bdVaJa",
            "kAzCrH",
            "jss123",
            "makeStyles-root-12",
            "svelte-1x2y3z",
            "ng-tns-c12-3",
            "css-abc123",
            "css-q7",
            # Any digit, so written names with one are not used either.
            "col-md-6",
            "h1",
            "arrow2",
            # One part in two case shapes does not read as a word.
            "loginPanel",
            "-x",
        ],
    )
    def test_hashed(self, token: str) -> None:
        assert is_build_hashed_class(token)

    @pytest.mark.parametrize(
        "token",
        [
            "sign-in-btn",
            "btn-primary",
            "form-control",
            "btn-block",
            "card__title",
            "card__title--active",
            "login-form__submit",
            "text-center",
        ],
    )
    def test_written(self, token: str) -> None:
        assert not is_build_hashed_class(token)


class TestFieldFallbacks:
    def test_autocomplete_first(self) -> None:
        form = _only(
            _dialog(
                '<input type="text" autocomplete="username" placeholder="Email">'
                '<input type="password" autocomplete="current-password">'
                '<button type="submit">Go</button>'
            )
        )
        assert form.fields == {
            "username": 'input[autocomplete="username"]',
            "password": 'input[autocomplete="current-password"]',
        }
        assert form.unscoped_fields == form.fields

    def test_autocomplete_off_tells_nothing(self) -> None:
        form = _only(
            _dialog(
                '<input type="text" autocomplete="off" placeholder="Email">'
                '<input type="password" autocomplete="off" placeholder="Password">'
                '<button type="submit">Go</button>'
            )
        )
        assert form.fields == {
            "username": 'input[placeholder="Email"]',
            "password": 'input[type="password"]',
        }

    def test_a_password_s_type_before_its_placeholder_unless_shared(self) -> None:
        other = (
            "<form action='/signup' method='post'><input type='password' name='pw'>"
            "<input type='password' name='pw_confirm'></form>"
        )
        form = _only(
            _dialog(
                '<input type="email" placeholder="Email"><input type="password" '
                'placeholder="Your password"><button type="submit">Go</button>',
                after=other,
            )
        )
        assert form.fields == {
            "username": 'input[placeholder="Email"]',
            "password": 'input[placeholder="Your password"]',
        }

    def test_aria_label_when_the_placeholder_is_shared(self) -> None:
        form = _only(
            _dialog(
                '<input type="text" placeholder="Required" aria-label="Email address">'
                '<input type="password" placeholder="Required"><button type="submit">Go</button>'
            )
        )
        assert form.fields["username"] == 'input[aria-label="Email address"]'

    def test_a_written_class_last_and_never_a_hashed_one(self) -> None:
        form = _only(
            _dialog(
                '<input type="text" class="tY7r3Qp login-email form-control">'
                '<input type="text" class="tY7r3Qp form-control">'
                '<input type="password" class="tY7r3Qp form-control">'
                '<button type="submit">Go</button>'
            )
        )
        # The username is the text input nearest the password: only hashed or shared
        # classes, so it has no selector.
        assert "username" in form.unresolved_roles
        assert all("tY7r3Qp" not in selector for selector in form.fields.values())
        first = _only(
            _dialog(
                '<input type="text" class="tY7r3Qp form-control">'
                '<input type="text" class="tY7r3Qp login-email form-control">'
                '<input type="password" class="tY7r3Qp form-control">'
                '<button type="submit">Go</button>'
            )
        )
        assert first.fields["username"] == "input.login-email"

    def test_a_value_holding_an_account_value_is_never_used(self) -> None:
        form = _only(
            _dialog(
                '<input type="text" placeholder="acct_40912873">'
                '<input type="password"><button type="submit">Go</button>'
            )
        )
        assert "username" in form.unresolved_roles
        assert "40912873" not in str(form.fields)

    def test_an_id_or_a_name_still_comes_first(self) -> None:
        form = _only(
            _dialog(
                '<input type="text" name="email" placeholder="Email">'
                '<input type="password" id="pw" placeholder="Password">'
                '<button type="submit">Go</button>'
            )
        )
        assert form.fields == {"username": 'input[name="email"]', "password": "#pw"}

    def test_a_form_whose_type_selector_is_not_unique_falls_back(self) -> None:
        """Inside a ``<form>`` a control with no id or name is picked by its type
        scoped to the form, as before; when two of the form's controls share that
        type, by a stable attribute unique on the page instead of nothing."""
        form = _only(
            "<form><input type='text' placeholder='Company'>"
            "<input type='text' placeholder='Email'>"
            "<input type='password'><button type='submit'>Go</button></form>"
        )
        scoped = ", ".join(
            f'{scope} input[type="password"]'
            for scope in (
                "form:not([action])",
                'form[action=""]',
                'form[action^="?"]',
                'form[action^=";"]',
                'form[action^="#"]',
            )
        )
        assert form.fields == {"username": 'input[placeholder="Email"]', "password": scoped}


class TestAttributeValuesAsWritten:
    """An attribute selector matches the value exactly, so the value is spelled as
    written (padding included) and CSS-escaped; a stored redaction is never one."""

    @pytest.mark.parametrize(
        ("attribute", "value"),
        [
            ("placeholder", " Enter your email "),
            ("aria-label", "Email "),
            ("autocomplete", " username"),
            ("placeholder", 'Your "email"'),
            ("placeholder", "Email\tor phone"),
        ],
    )
    def test_a_padded_or_escaped_value_matches_the_control(
        self, attribute: str, value: str
    ) -> None:
        from html import escape

        html = _dialog(
            f'<input type="text" {attribute}="{escape(value, quote=True)}">'
            '<input type="password"><button type="submit">Go</button>'
        )
        selector = _only(html).fields["username"]
        prefix = f'input[{attribute}="'
        assert selector.startswith(prefix)
        assert selector.endswith('"]')
        assert _css_unescaped(selector[len(prefix) : -2]) == value
        # And a CSS engine (lxml's cssselect) picks the control with it.
        import lxml.html

        (match,) = lxml.html.fromstring(html).cssselect(selector)
        assert match.get(attribute) == value

    def test_a_value_of_whitespace_is_never_used(self) -> None:
        form = _only(
            _dialog(
                '<input type="text" placeholder="   "><input type="password">'
                '<button type="submit">Go</button>'
            )
        )
        assert "username" in form.unresolved_roles

    @pytest.mark.parametrize("value", ["***REDACTED***", "***redacted***", "Hi ***REDACTED***"])
    def test_a_redaction_is_never_a_selector(self, value: str) -> None:
        form = _only(
            _dialog(
                f'<input type="email" placeholder="{value}"><input type="password">'
                '<button type="submit">Go</button>'
            ),
            snapshot=True,
        )
        assert "REDACTED" not in str(form.fields).upper()
        assert "username" not in form.fields

    def test_a_redacted_placeholder_from_a_stored_snapshot(self) -> None:
        """The snapshot redaction writes ``***REDACTED***`` in place of a posted email
        in a placeholder; the username then falls to the next attribute."""
        import json

        from graftpunk.observe.snapshot import stored_dom_snapshot

        page = (
            '<html><body><div role="dialog"><input placeholder="jsmith@example.com" '
            'type="email" aria-label="Email"><input type="password">'
            '<button type="submit">Go</button></div></body></html>'
        )
        body = json.dumps({"variables": {"email": "jsmith@example.com", "password": "pw123456"}})
        stored = stored_dom_snapshot(page, body, "application/json")
        assert "***REDACTED***" in stored
        form = _only(stored, snapshot=True)
        assert form.fields["username"] == 'input[aria-label="Email"]'


class TestSubmitFallbacks:
    _FIELDS = '<input type="email" placeholder="Email"><input type="password">'

    def test_the_page_s_one_submit_button_by_its_type(self) -> None:
        form = _only(_dialog(f'{self._FIELDS}<button type="submit" class="bN4w8Zx">Go</button>'))
        assert form.submit == 'button[type="submit"]'

    def test_a_written_class_when_the_type_is_shared(self) -> None:
        form = _only(
            _dialog(
                f'{self._FIELDS}<button type="submit" class="bN4w8Zx sign-in-btn btn">Sign In'
                "</button>",
                after='<button type="submit" class="btn">Search</button>',
            )
        )
        assert form.submit == "button.sign-in-btn"

    def test_a_hashed_class_is_never_the_submit(self) -> None:
        for classes in ("sc-bdVaJa kAzCrH", "jss123"):
            form = _only(
                _dialog(
                    f'{self._FIELDS}<button type="submit" class="{classes}">Sign In</button>',
                    after='<button type="submit" class="btn">Search</button>',
                )
            )
            assert form.submit is None
            assert "submit" in form.unresolved_roles

    def test_its_text_is_not_kept(self) -> None:
        """A button's text can name the account ("Continue as Alice Smith"), and a
        CSS selector cannot match text, so the form keeps none of it (the generated
        GP-FILL is pinned in test_scaffold_render.py)."""
        form = _only(
            _dialog(
                f'{self._FIELDS}<button type="submit" class="bN4w8Zx btn">Continue as Alice '
                "Smith</button>",
                after='<button type="submit" class="btn">Search</button>',
            )
        )
        assert form.submit is None
        assert "submit" in form.unresolved_roles
        assert "Alice" not in repr(form)


class TestTheScriptRenderedLoginInASnapshot:
    """A snapshot's login outside any form and dialog whose username is named only by
    its placeholder: the placeholder's hint makes it the username."""

    _HTML = (
        '<html><body><div class="xK9q2Lm login-card">'
        '<input placeholder="Enter your email or mobile number" type="text" '
        'class="tY7r3Qp form-control">'
        '<input placeholder="Enter your password" type="password" class="tY7r3Qp form-control">'
        '<button type="submit" class="bN4w8Zx sign-in-btn btn">Sign In</button></div>'
        "</body></html>"
    )

    def test_its_roles_and_selectors(self) -> None:
        form = _only(self._HTML, snapshot=True)
        assert form.plain_block
        assert form.block_bound == "inputs"
        assert form.fields == {
            "username": 'input[placeholder="Enter your email or mobile number"]',
            "password": 'input[type="password"]',
        }
        assert form.submit == 'button[type="submit"]'
        assert form.unresolved_roles == ()

    def test_an_aria_label_hint_names_the_username_too(self) -> None:
        html = self._HTML.replace(
            'placeholder="Enter your email or mobile number"', 'aria-label="Login ID"'
        )
        form = _only(html, snapshot=True)
        assert form.fields["username"] == 'input[aria-label="Login ID"]'

    def test_with_no_hint_the_group_is_the_password_and_its_button(self) -> None:
        html = self._HTML.replace("Enter your email or mobile number", "Phone")
        form = _only(html, snapshot=True)
        assert form.block_bound == "button"
        assert "username" in form.absent_roles
