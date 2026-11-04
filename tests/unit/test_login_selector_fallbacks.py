"""#365: a login control with no usable id or name is selected by a stable attribute
unique on the page (autocomplete, a password's or a submit button's type, placeholder,
aria-label, then a class that is not build-hashed), never by a build-hashed class."""

from __future__ import annotations

import pytest

from graftpunk.har.documents import extract_login_forms, is_build_hashed_class

_SITE = "https://myshop.example.com/log-in"


def _dialog(inner: str, after: str = "") -> str:
    return f'<html><body><div role="dialog">{inner}</div>{after}</body></html>'


def _only(html: str, *, snapshot: bool = False):  # noqa: ANN202
    (form,) = extract_login_forms(html, source=_SITE, snapshot=snapshot)
    return form


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
        ],
    )
    def test_hashed(self, token: str) -> None:
        assert is_build_hashed_class(token)

    @pytest.mark.parametrize(
        "token",
        [
            "sign-in-btn",
            "form-control",
            "btn-block",
            "col-md-6",
            "h1",
            "arrow2",
            "card__title",
            "loginPanel",
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
        assert form.submit_text == ""

    def test_its_text_when_nothing_picks_it(self) -> None:
        form = _only(
            _dialog(
                f'{self._FIELDS}<button type="submit" class="bN4w8Zx btn">  Sign\n In </button>',
                after='<button type="submit" class="btn">Search</button>',
            )
        )
        assert form.submit is None
        assert "submit" in form.unresolved_roles
        assert form.submit_text == "Sign In"


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
