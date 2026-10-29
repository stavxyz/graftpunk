"""HTML extraction on inline HTML, no HAR involved (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import random
import time
from collections.abc import Callable

import lxml.html
import pytest

from graftpunk.har.documents import (
    extract_login_forms,
    extract_token_candidates,
    form_action_targets,
    is_login_document,
    looks_like_token_name,
    printable_selectors,
    printable_unresolved_roles,
)

_LOGIN_PAGE = """
<html><body>
<form action="/login" method="post">
  <input type="email" name="email" id="email-field">
  <input type="password" name="password" id="pw">
  <input type="hidden" name="_token" value="abc123">
  <button type="submit" id="login-btn">Sign in</button>
</form>
</body></html>
"""

_NO_LOGIN_PAGE = "<html><body><p>Welcome, alice@example.com</p></body></html>"

_MULTI_FORM_PAGE = """
<html><body>
<form action="/search"><input type="text" name="q"></form>
<form action="/login" method="POST">
  <input name="username">
  <input type="password" name="pw">
  <input type="submit" value="Go">
</form>
</body></html>
"""


def _parse_seconds(html: str) -> float:
    """The fastest of three parses of *html*, so one slow scheduling slice on a
    loaded CI runner does not decide the result."""
    best = float("inf")
    for _ in range(3):
        started = time.perf_counter()
        extract_login_forms(html, source="s")
        best = min(best, time.perf_counter() - started)
    return best


def _assert_parses_in_linear_time(page: Callable[[int], str]) -> None:
    """Four times the input takes well under sixteen times as long. A quadratic
    parse grows sixteenfold; a linear one about fourfold. A ratio, not a wall-clock
    bound, so a slow runner cannot fail it."""
    small, large = _parse_seconds(page(5000)), _parse_seconds(page(20000))
    assert large < 8 * small + 0.05, f"5000: {small:.3f}s, 20000: {large:.3f}s"


class TestExtractLoginForms:
    def test_finds_form_with_password_input(self) -> None:
        forms = extract_login_forms(_LOGIN_PAGE, source="page-source.html")
        assert len(forms) == 1
        form = forms[0]
        assert form.action == "/login"
        assert form.method == "POST"
        assert form.source == "page-source.html"

    def test_id_selector_preferred_over_name_selector(self) -> None:
        (form,) = extract_login_forms(_LOGIN_PAGE, source="s")
        assert form.fields["username"] == "#email-field"
        assert form.fields["password"] == "#pw"  # noqa: S105

    def test_email_type_guessed_as_username(self) -> None:
        (form,) = extract_login_forms(_LOGIN_PAGE, source="s")
        assert "username" in form.fields

    def test_hidden_inputs_captured(self) -> None:
        (form,) = extract_login_forms(_LOGIN_PAGE, source="s")
        assert form.hidden == ("_token",)

    def test_a_select_and_a_textarea_are_controls_not_hidden_inputs(self) -> None:
        html = _LOGIN_PAGE.replace(
            "</form>",
            '<select name="region"><option>us</option></select>'
            '<textarea name="note"></textarea></form>',
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.hidden == ("_token",)
        assert {"region", "note"} <= set(form.input_names)

    def test_a_javascript_action_keeps_its_scope_and_submit(self) -> None:
        """javascript:void(0) holds no id, so the scoped selectors are printed."""
        html = (
            '<form action="javascript:void(0)"><input name="username">'
            '<input type="password" name="password"><button>Sign in</button></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        fields, submit = printable_selectors(form)
        assert fields["username"] == 'form[action="javascript:void(0)"] input[name="username"]'
        assert submit is not None
        assert printable_unresolved_roles(form) == ()

    def test_a_javascript_action_holding_a_token_is_printed_without_its_scope(self) -> None:
        """The scope would spell the token, so each selector is printed unscoped, or
        left unresolved when that would not pick one input."""
        token = "f3a9c2e1b7d4a6f0e2c8b1d9"  # noqa: S105 - a planted token, not a secret
        html = (
            f'<form action="javascript:login(\'{token}\')"><input name="username">'
            '<input type="password" name="password"><button>Sign in</button></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert token in form.fields["username"]
        fields, submit = printable_selectors(form)
        assert fields == {
            "password": 'input[name="password"]',
            "username": 'input[name="username"]',
        }
        assert submit is None
        assert printable_unresolved_roles(form) == ("submit",)

    def test_submit_selector(self) -> None:
        (form,) = extract_login_forms(_LOGIN_PAGE, source="s")
        assert form.submit == "#login-btn"

    def test_form_without_password_input_is_not_a_login_form(self) -> None:
        assert extract_login_forms(_NO_LOGIN_PAGE, source="s") == ()

    def test_only_the_password_form_among_several_is_returned(self) -> None:
        forms = extract_login_forms(_MULTI_FORM_PAGE, source="s")
        assert len(forms) == 1
        assert forms[0].action == "/login"

    def test_field_without_id_falls_back_to_name_selector(self) -> None:
        (form,) = extract_login_forms(_MULTI_FORM_PAGE, source="s")
        assert form.fields["username"] == 'form[action="/login"] input[name="username"]'

    def test_submit_input_without_id_falls_back_to_type_selector(self) -> None:
        (form,) = extract_login_forms(_MULTI_FORM_PAGE, source="s")
        assert form.submit == 'form[action="/login"] input[type="submit"]'

    @pytest.mark.parametrize(
        ("raw_action", "action", "scopes"),
        [
            (
                "/login;jsessionid=S3CR3T?t=tok",
                "/login",
                (
                    'form[action="/login"]',
                    'form[action^="/login;"]',
                    'form[action^="/login?"]',
                    'form[action^="/login#"]',
                ),
            ),
            (
                "/login#top",
                "/login",
                (
                    'form[action="/login"]',
                    'form[action^="/login;"]',
                    'form[action^="/login?"]',
                    'form[action^="/login#"]',
                ),
            ),
            (
                "https://myshop.example.com/a;v=1/login#top",
                "https://myshop.example.com/a/login",
                (
                    'form[action^="https://myshop.example.com/a;"][action$="/login"]',
                    'form[action^="https://myshop.example.com/a;"][action*="/login;"]',
                    'form[action^="https://myshop.example.com/a;"][action*="/login?"]',
                    'form[action^="https://myshop.example.com/a;"][action*="/login#"]',
                ),
            ),
            (
                "?next=/orders",
                "",
                (
                    "form:not([action])",
                    'form[action=""]',
                    'form[action^="?"]',
                    'form[action^=";"]',
                    'form[action^="#"]',
                ),
            ),
        ],
    )
    def test_an_action_loses_its_query_fragment_and_path_params(
        self, raw_action: str, action: str, scopes: tuple[str, ...]
    ) -> None:
        html = (
            f'<form action="{raw_action}"><input name="username">'
            '<input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.action == action
        assert form.fields["username"] == ", ".join(
            f'{scope} input[name="username"]' for scope in scopes
        )

    @pytest.mark.parametrize(
        ("decoy", "live"),
        [
            ('action="/login-help"', 'action="/login;jsessionid=S3CR3T?t=tok"'),
            ('action="/login2"', 'action="/login?t=tok"'),
            ('action="/other"', 'action="?next=/orders"'),
            ('action="/other"', 'action=";jsessionid=S3CR3T"'),
            ('action="/other"', ""),
            ('action="/other"', 'action=""'),
            ('action="/login-help"', 'action="/login#top"'),
            (
                'action="/account;m=DECOYVALUE/login-help"',
                'action="/account;m=MIDVALUE/login;s=LASTVALUE?t=QUERYVALUE"',
            ),
            (
                'action="/account;m=DECOYVALUE/login-help"',
                'action="/account;m=MIDVALUE/login"',
            ),
            (
                'action="/a;m=DECOYVALUE/b/c-help"',
                'action="/a;m=MIDVALUE/b;n=SECONDVALUE/c"',
            ),
        ],
        ids=[
            "prefix-decoy",
            "digit-decoy",
            "query-only",
            "params-only",
            "no-action",
            "empty",
            "fragment-only",
            "middle-param",
            "middle-param-bare-last",
            "two-middle-params",
        ],
    )
    def test_a_stripped_action_selects_the_live_form_and_not_a_decoy_before_it(
        self, decoy: str, live: str
    ) -> None:
        html = (
            f'<html><body><form {decoy}><input name="username" class="decoy">'
            '<input type="password" name="password" class="decoy"></form>'
            f'<form {live}><input name="username" class="live">'
            '<input type="password" name="password" class="live"></form></body></html>'
        )
        forms = extract_login_forms(html, source="s")
        live_form = forms[1]
        document = lxml.html.fromstring(html)
        for selector in live_form.fields.values():
            selected = document.cssselect(selector)
            assert [element.get("class") for element in selected] == ["live"], selector
            for value in ("MIDVALUE", "LASTVALUE", "QUERYVALUE", "SECONDVALUE", "DECOYVALUE"):
                assert value not in selector, selector


class TestExtractTokenCandidates:
    def test_meta_tag_with_csrf_name_is_a_candidate(self) -> None:
        html = '<html><head><meta name="csrf-token" content="xyz"></head></html>'
        candidates = extract_token_candidates(html, source="page-source.html")
        assert len(candidates) == 1
        assert candidates[0].kind == "meta"
        assert candidates[0].name == "csrf-token"
        assert candidates[0].seen_on == ("page-source.html",)

    def test_meta_tag_without_token_substring_is_not_a_candidate(self) -> None:
        html = '<html><head><meta name="description" content="a shop"></head></html>'
        assert extract_token_candidates(html, source="s") == ()

    def test_hidden_input_named_authenticity_token(self) -> None:
        html = '<form><input type="hidden" name="authenticity_token" value="v"></form>'
        candidates = extract_token_candidates(html, source="s")
        assert len(candidates) == 1
        assert candidates[0].kind == "hidden_input"
        assert candidates[0].name == "authenticity_token"

    def test_no_candidates_in_plain_html(self) -> None:
        assert extract_token_candidates(_NO_LOGIN_PAGE, source="s") == ()


class TestIsLoginDocument:
    def test_true_for_password_form(self) -> None:
        assert is_login_document(_LOGIN_PAGE) is True

    def test_false_for_ordinary_page(self) -> None:
        assert is_login_document(_NO_LOGIN_PAGE) is False


class TestLooksLikeTokenName:
    """The one predicate digest.py's header and cookie scans call, instead of
    re-typing the hint tuple (validation net-negative, addressed 2026-09-12)."""

    @pytest.mark.parametrize(
        "name",
        ["X-CSRF-Token", "xsrf-token", "csrftoken", "Authorization-Token"],
    )
    def test_true_for_a_token_like_name(self, name: str) -> None:
        assert looks_like_token_name(name) is True

    @pytest.mark.parametrize("name", ["Content-Type", "Accept", "session_id"])
    def test_false_for_an_ordinary_name(self, name: str) -> None:
        assert looks_like_token_name(name) is False


class TestPrintedWithoutTheFormScope:
    """printable_selectors, the public path: when the form's action holds an id, a
    selector prints without its scope only when it still picks its input."""

    _PAGE = (
        '<form action="/a/40912873/session"><input type="email" name="username">'
        '<input type="password" id="email"><input type="text">'
        '<button type="submit">Go</button></form>'
    )

    def test_a_name_selector_prints_its_input_part(self) -> None:
        (form,) = extract_login_forms(self._PAGE, source="s")
        fields, _submit = printable_selectors(form)
        assert fields["username"] == 'input[name="username"]'

    def test_an_id_selector_prints_unchanged(self) -> None:
        (form,) = extract_login_forms(self._PAGE, source="s")
        fields, _submit = printable_selectors(form)
        assert fields["password"] == "#email"  # noqa: S105

    def test_a_type_selector_does_not_print(self) -> None:
        """Fails closed: without the scope it would pick the first such control."""
        (form,) = extract_login_forms(self._PAGE, source="s")
        _fields, submit = printable_selectors(form)
        assert submit is None
        assert "submit" in printable_unresolved_roles(form)


class TestSelectorsEscapeAttributeValues:
    def test_a_name_holding_a_quote_and_a_backslash_is_escaped(self) -> None:
        html = (
            '<form action="/login"><input name="ref&quot;x\\y">'
            '<input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields["username"] == 'form[action="/login"] input[name="ref\\"x\\\\y"]'
        _assert_first_match(html, form.fields["username"], name='ref"x\\y')

    def test_an_action_holding_a_quote_is_escaped(self) -> None:
        html = (
            '<form action="/log&quot;in"><input name="username">'
            '<input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields["username"] == 'form[action="/log\\"in"] input[name="username"]'

    def test_an_id_that_is_not_a_css_identifier_is_an_attribute_selector(self) -> None:
        html = (
            '<form action="/login"><input id="user email" name="username">'
            '<input type="password" id="pw" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": '[id="user email"]', "password": "#pw"}


def test_a_form_whose_action_holds_an_email_gets_unscoped_selectors() -> None:
    """The action is kept with the email masked, which no live form's action
    matches, so a scope spelled from it would never select anything."""
    html = (
        '<form action="/users/alice%40example.com/session"><input name="username">'
        '<input type="password" name="password"></form>'
    )
    (form,) = extract_login_forms(html, source="s")
    assert form.action == "/users/{user_id}/session"
    assert form.fields == {
        "username": 'input[name="username"]',
        "password": 'input[name="password"]',
    }


def test_a_brace_in_the_stripped_query_does_not_hide_a_masked_email() -> None:
    """Masking is judged path to path; a "{" the query held is not a placeholder."""
    html = (
        '<form action="/users/alice%40example.com/session?next={x}"><input name="username">'
        '<input type="password" name="password"></form>'
    )
    (form,) = extract_login_forms(html, source="s")
    assert form.action == "/users/{user_id}/session"
    assert form.fields["username"] == 'input[name="username"]'


class TestLoginFormNamesGoThroughTheIdRule:
    """A login form's element ids and input names are names like any
    other, and one that holds an account value is never printed."""

    def test_an_element_id_holding_an_id_falls_back_to_the_name(self) -> None:
        html = (
            '<form action="/login"><input id="user_40912873" name="username">'
            '<input type="password" id="pw" name="password">'
            '<button id="btn-5f1a9c2e8b1d" type="submit">Go</button></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields["username"] == 'form[action="/login"] input[name="username"]'
        assert form.submit == 'form[action="/login"] button[type="submit"]'

    def test_an_input_name_holding_an_id_gets_a_neutral_role_and_a_type_selector(self) -> None:
        html = (
            '<form action="/login"><input type="email" name="email">'
            '<input type="text" name="otp_40912873">'
            '<input type="tel" name="fld_a8f3c9e2b1d4">'
            '<input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {
            "username": 'form[action="/login"] input[name="email"]',
            "field_1": 'form[action="/login"] input[type="text"]',
            "field_2": 'form[action="/login"] input[type="tel"]',
            "password": 'form[action="/login"] input[name="password"]',
        }

    def test_a_credential_input_name_holding_an_id_keeps_its_role(self) -> None:
        html = (
            '<form action="/login"><input type="email" name="email_40912873">'
            '<input type="password" name="pw_ab12cd34ef56"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {
            "username": 'form[action="/login"] input[type="email"]',
            "password": 'form[action="/login"] input[type="password"]',
        }

    def test_a_hidden_input_name_holding_an_id_is_dropped_and_counted(self) -> None:
        html = (
            '<form action="/login"><input type="hidden" name="7f3a9c2e8b1d4f60a9e2c3b4d5f6a7b8">'
            '<input type="hidden" name="_token"><input name="username">'
            '<input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.hidden == ("_token",)
        assert form.hidden_names_dropped_as_ids == 1


class TestLoginFallbackSelectors:
    """A fallback selector (no usable id or name) is used only when it picks one
    input of the recorded form, and never printed without the form scope; a
    typeless input or button gets a selector that matches it."""

    def test_the_only_candidate_for_a_role_resolved_by_type_is_unresolved_when_not_unique(
        self,
    ) -> None:
        html = (
            '<form action="/login"><input type="text" name="otp_40912873">'
            '<input type="text" name="fld_a8f3c9e2b1d4">'
            '<input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"password": 'form[action="/login"] input[name="password"]'}
        assert form.unresolved_roles == ("username",)

    def test_a_typeless_input_and_button_get_selectors_that_match_them(self) -> None:
        html = (
            '<form action="/login"><input name="user_40912873">'
            '<input type="password" name="password"><button>Go</button></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        scope = 'form[action="/login"]'
        assert form.fields["username"] == (f'{scope} input:not([type]), {scope} input[type="text"]')
        assert form.submit == f'{scope} button:not([type]), {scope} button[type="submit"]'
        _assert_first_match(html, form.fields["username"], name="user_40912873")
        _assert_first_match(html, form.submit, text="Go")

    def test_an_empty_type_is_typeless(self) -> None:
        html = (
            '<form action="/login"><input type="" name="user_40912873">'
            '<input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        scope = 'form[action="/login"]'
        assert form.fields["username"] == (f'{scope} input:not([type]), {scope} input[type="text"]')

    def test_a_neutral_key_never_takes_a_real_input_name(self) -> None:
        html = (
            '<form action="/login"><input type="text" name="username">'
            '<input type="text" name="field_1">'
            '<input type="tel" name="otp_40912873">'
            '<input type="password" name="password"><button>Go</button></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields["field_1"] == 'form[action="/login"] input[name="field_1"]'
        assert form.fields["field_2"] == 'form[action="/login"] input[type="tel"]'
        assert form.neutral_roles == ("field_2",)

    def test_a_type_fallback_is_never_printed_unscoped(self) -> None:
        """A search form sits before a login form whose action holds an id: the
        printed selectors drop the scope, and input[type="text"] would match the
        search box first."""
        html = (
            '<form action="/search"><input type="text" name="q"><button>Search</button></form>'
            '<form action="/accounts/40912873/session"><input type="text">'
            '<input type="password"><button>Sign in</button></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        fields, submit = printable_selectors(form)
        assert fields == {"password": None, "username": None}
        assert submit is None
        assert printable_unresolved_roles(form) == ("username", "password", "submit")

    def test_a_name_printed_unscoped_must_be_unique_on_the_page(self) -> None:
        html = (
            '<form action="/newsletter"><input type="email" name="email"></form>'
            '<form action="/accounts/40912873/session"><input type="email" name="email">'
            '<input type="password" name="password"><button id="signin">Sign in</button></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        fields, submit = printable_selectors(form)
        assert fields == {"password": 'input[name="password"]', "username": None}
        assert submit == "#signin"
        assert printable_unresolved_roles(form) == ("username",)
        _assert_first_match(html, 'input[name="password"]', name="password")

    def test_an_unparseable_action_leaves_type_fallbacks_unresolved(self) -> None:
        html = (
            '<form action="http://[::1/login"><input type="text">'
            '<input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"password": 'input[name="password"]'}
        assert form.unresolved_roles == ("username",)


class TestLoginRolesFollowHtml:
    """Roles follow HTML semantics: the password input anchors them, the username is
    the text-like input nearest before it, the submit is the first submit control
    after it, and a control that is not a credential is never a role."""

    def test_the_submit_is_the_first_after_the_password(self) -> None:
        html = (
            '<form action="/login"><input type="submit" id="go" value="Go">'
            '<input name="username"><input type="password" name="password">'
            '<button id="signin">Sign in</button><button id="subscribe">Subscribe</button></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.submit == "#signin"

    def test_the_username_is_the_hinted_input_nearest_before_the_password(self) -> None:
        html = (
            '<form action="/login"><input type="text" name="q">'
            '<input type="email" name="email"><input type="text" name="company">'
            '<input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields["username"] == 'form[action="/login"] input[name="email"]'
        assert form.fields["company"] == 'form[action="/login"] input[name="company"]'
        assert "q" not in form.fields

    def test_a_text_input_with_no_hint_nearest_the_password_is_the_username(self) -> None:
        html = (
            '<form action="/login"><input type="text" name="handle">'
            '<input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields["username"] == 'form[action="/login"] input[name="handle"]'

    @pytest.mark.parametrize(
        "input_type", ["checkbox", "radio", "file", "image", "reset", "range", "hidden"]
    )
    def test_a_non_text_control_is_never_a_role(self, input_type: str) -> None:
        html = (
            f'<form action="/login"><input name="username"><input type="password" '
            f'name="password"><input type="{input_type}" name="remember_me"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert set(form.fields) == {"username", "password"}

    def test_a_later_hinted_input_never_overwrites_the_username(self) -> None:
        html = (
            '<form action="/login"><input name="username" id="user">'
            '<input type="password" name="password"><button id="signin">Sign in</button>'
            '<input type="email" name="newsletter_email"><button>Subscribe</button></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {
            "username": "#user",
            "password": 'form[action="/login"] input[name="password"]',
        }
        assert form.submit == "#signin"
        assert form.unresolved_roles == ()

    def test_a_nameless_input_takes_its_role_from_autocomplete(self) -> None:
        html = (
            '<form action="/login"><input type="text" name="q">'
            '<input autocomplete="username" id="login-id">'
            '<input type="text" name="nickname">'
            '<input type="password" autocomplete="current-password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields["username"] == "#login-id"

    def test_a_form_with_no_username_records_it_unresolved(self) -> None:
        html = '<form action="/login"><input type="password" name="password"></form>'
        (form,) = extract_login_forms(html, source="s")
        assert set(form.fields) == {"password"}
        assert form.unresolved_roles == ("username",)

    def test_a_registration_form_is_not_a_login_form(self) -> None:
        html = (
            '<form action="/register"><input type="email" name="email">'
            '<input type="password" name="password">'
            '<input type="password" name="password_confirm"><button>Create</button></form>'
            '<form action="/login"><input type="email" name="email">'
            '<input type="password" name="password"><button>Sign in</button></form>'
        )
        forms = extract_login_forms(html, source="s")
        assert [form.action for form in forms] == ["/login"]


def _assert_first_match(
    html: str,
    selector: str,
    *,
    name: str | None = None,
    text: str | None = None,
    id_: str | None = None,
) -> None:
    """The first element *selector* picks in *html* is the intended one."""
    matches = lxml.html.fromstring(f"<html><body>{html}</body></html>").cssselect(selector)
    assert matches, selector
    first = matches[0]
    if name is not None:
        assert first.get("name") == name, (selector, first.attrib)
    if text is not None:
        assert (first.text or first.get("value")) == text, (selector, first.attrib)
    if id_ is not None:
        assert first.get("id") == id_, (selector, first.attrib)


_WEBFORMS_PAGE = (
    '<form id="aspnetForm" action="./Login.aspx" method="post">'
    '<input type="hidden" name="__VIEWSTATE" value="x">'
    '<div class="header"><input type="text" name="ctl00$Header$txtSearch"'
    ' id="ctl00_Header_txtSearch">'
    '<input type="submit" name="ctl00$Header$btnGo" id="ctl00_Header_btnGo" value="Go"></div>'
    '<input type="text" id="ctl00_MainContent_LoginUser_UserName"'
    ' name="ctl00$MainContent$LoginUser$UserName">'
    '<input type="password" id="ctl00_MainContent_LoginUser_Password"'
    ' name="ctl00$MainContent$LoginUser$Password">'
    '<input type="checkbox" id="ctl00_MainContent_LoginUser_RememberMe"'
    ' name="ctl00$MainContent$LoginUser$RememberMe">'
    '<input type="submit" id="ctl00_MainContent_LoginUser_LoginButton"'
    ' name="ctl00$MainContent$LoginUser$LoginButton" value="Log In">'
    '<div class="footer"><input type="email" name="ctl00$Footer$txtNewsletter"'
    ' id="ctl00_Footer_txtNewsletter">'
    '<input type="submit" name="ctl00$Footer$btnSubscribe" id="ctl00_Footer_btnSubscribe"'
    ' value="Subscribe"></div></form>'
)

_REACT_PAGE = (
    '<form action="/api/session" method="post">'
    '<input id=":r0:" type="text" autocomplete="username">'
    '<input id=":r1:" type="password" autocomplete="current-password">'
    '<button type="submit">Continue</button></form>'
)

_SEARCH_THEN_ID_ACTION_PAGE = (
    '<form action="/search"><input type="text" name="q"><button>Search</button></form>'
    '<form action="/accounts/40912873/session"><input type="text" name="username">'
    '<input type="password" name="password"><button type="submit" name="signin">Sign in</button>'
    "</form>"
)

_REGISTER_THEN_LOGIN_PAGE = (
    '<form action="/register"><input type="email" name="email">'
    '<input type="password" name="password" autocomplete="new-password">'
    "<button>Create account</button></form>"
    '<form action="/login"><input type="email" name="email">'
    '<input type="password" name="password"><button>Sign in</button></form>'
)


class TestLoginSelectorsPickTheIntendedElement:
    """Every selector a login form prints, run against the recorded HTML, picks the
    intended element first."""

    def test_webforms_with_header_search_and_footer_newsletter(self) -> None:
        (form,) = extract_login_forms(_WEBFORMS_PAGE, source="s")
        fields, submit = printable_selectors(form)
        assert set(fields) == {"username", "password"}
        _assert_first_match(
            _WEBFORMS_PAGE, fields["username"] or "", id_="ctl00_MainContent_LoginUser_UserName"
        )
        _assert_first_match(
            _WEBFORMS_PAGE, fields["password"] or "", id_="ctl00_MainContent_LoginUser_Password"
        )
        _assert_first_match(
            _WEBFORMS_PAGE, submit or "", id_="ctl00_MainContent_LoginUser_LoginButton"
        )

    def test_react_ids_and_autocomplete(self) -> None:
        (form,) = extract_login_forms(_REACT_PAGE, source="s")
        fields, submit = printable_selectors(form)
        _assert_first_match(_REACT_PAGE, fields["username"] or "", id_=":r0:")
        _assert_first_match(_REACT_PAGE, fields["password"] or "", id_=":r1:")
        _assert_first_match(_REACT_PAGE, submit or "", text="Continue")

    def test_a_search_form_before_a_login_form_whose_action_holds_an_id(self) -> None:
        forms = extract_login_forms(_SEARCH_THEN_ID_ACTION_PAGE, source="s")
        (form,) = forms
        fields, submit = printable_selectors(form)
        assert "40912873" not in repr((fields, submit))
        _assert_first_match(_SEARCH_THEN_ID_ACTION_PAGE, fields["username"] or "", name="username")
        _assert_first_match(_SEARCH_THEN_ID_ACTION_PAGE, fields["password"] or "", name="password")
        _assert_first_match(_SEARCH_THEN_ID_ACTION_PAGE, submit or "", name="signin")

    def test_a_register_form_before_the_login_form(self) -> None:
        (form,) = extract_login_forms(_REGISTER_THEN_LOGIN_PAGE, source="s")
        fields, submit = printable_selectors(form)
        _assert_first_match(_REGISTER_THEN_LOGIN_PAGE, submit or "", text="Sign in")
        for role in ("username", "password"):
            matches = lxml.html.fromstring(
                f"<html><body>{_REGISTER_THEN_LOGIN_PAGE}</body></html>"
            ).cssselect(fields[role] or "")
            assert matches[0].getparent().get("action") == "/login", role


class TestLoginRolesFromAutocompleteAndControls:
    """Autocomplete hints by position, registration forms, image submits, fragment
    actions, a username after the password, and a button outside its form."""

    def test_a_footer_newsletter_email_hint_does_not_take_the_username(self) -> None:
        page = _WEBFORMS_PAGE.replace(
            'name="ctl00$Footer$txtNewsletter"',
            'name="ctl00$Footer$txtNewsletter" autocomplete="email"',
        )
        assert 'autocomplete="email"' in page
        (form,) = extract_login_forms(page, source="s")
        fields, _submit = printable_selectors(form)
        _assert_first_match(
            page, fields["username"] or "", id_="ctl00_MainContent_LoginUser_UserName"
        )

    def test_an_email_hint_before_the_password_is_the_username(self) -> None:
        html = (
            '<form action="/login"><input type="text" name="q" id="q">'
            '<input type="text" name="contact" id="contact" autocomplete="email">'
            '<input type="text" name="nickname"><input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields["username"] == "#contact"

    def test_a_username_hint_after_the_password_counts_anywhere(self) -> None:
        html = (
            '<form action="/login"><input type="password" name="password">'
            '<input type="text" id="who" autocomplete="username"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields["username"] == "#who"

    def test_a_literal_username_after_the_password_is_used_when_none_precedes(self) -> None:
        html = (
            '<form action="/login"><input type="password" name="password">'
            '<input type="text" name="username"><button id="go">Go</button></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields["username"] == 'form[action="/login"] input[name="username"]'
        assert form.unresolved_roles == ()
        assert form.absent_roles == ()

    def test_a_password_only_form_names_the_username_absent(self) -> None:
        html = '<form action="/login"><input type="password" name="password"></form>'
        (form,) = extract_login_forms(html, source="s")
        assert form.absent_roles == ("username",)
        assert form.unresolved_roles == ("username",)

    def test_a_registration_only_page_is_still_a_login_document(self) -> None:
        html = (
            '<form action="/register"><input type="email" name="email">'
            '<input type="password" name="password" autocomplete="new-password">'
            '<input type="password" name="password_confirm"></form>'
        )
        assert is_login_document(html)

    def test_a_password_and_pin_form_is_a_login_form(self) -> None:
        html = (
            '<form action="/login"><input type="email" name="email">'
            '<input type="password" name="password"><input type="password" name="pin"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields["password"] == 'form[action="/login"] input[name="password"]'  # noqa: S105

    def test_a_lone_login_form_marked_new_password_is_kept(self) -> None:
        html = (
            '<form action="/login"><input type="email" name="email">'
            '<input type="password" name="password" autocomplete="new-password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.action == "/login"

    def test_a_combined_login_and_register_form_uses_the_current_password(self) -> None:
        html = (
            '<form action="/account"><input type="email" name="login_email">'
            '<input type="password" name="login_password" autocomplete="current-password">'
            '<button name="signin">Sign in</button>'
            '<input type="email" name="register_email">'
            '<input type="password" name="new_password" autocomplete="new-password">'
            '<input type="password" name="new_password_confirm" autocomplete="new-password">'
            '<button name="register">Register</button></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        fields, submit = printable_selectors(form)
        _assert_first_match(html, fields["password"] or "", name="login_password")
        _assert_first_match(html, fields["username"] or "", name="login_email")
        _assert_first_match(html, submit or "", name="signin")

    def test_an_image_input_is_the_submit_and_never_a_field(self) -> None:
        html = (
            '<form action="/login"><input name="username"><input type="password" '
            'name="password"><input type="image" name="go" src="go.png"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert set(form.fields) == {"username", "password"}
        assert form.submit is not None
        _assert_first_match(html, form.submit, name="go")

    @pytest.mark.parametrize("action", ["#", "#login"])
    def test_a_fragment_action_selects_the_live_form(self, action: str) -> None:
        html = (
            '<form action="/other"><input name="username" class="decoy">'
            '<input type="password" name="password" class="decoy"></form>'
            f'<form action="{action}"><input name="username" class="live">'
            '<input type="password" name="password" class="live"></form>'
        )
        live = extract_login_forms(html, source="s")[1]
        document = lxml.html.fromstring(f"<html><body>{html}</body></html>")
        for selector in live.fields.values():
            assert [e.get("class") for e in document.cssselect(selector)] == ["live"], selector

    def test_a_button_outside_its_form_belongs_to_that_form(self) -> None:
        html = (
            '<form id="signin" action="/login"><input name="username">'
            '<input type="password" name="password"></form>'
            '<button type="submit" form="signin" id="outside">Sign in</button>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.submit == "#outside"

    def test_a_neutral_role_records_whether_its_input_had_no_name(self) -> None:
        html = (
            '<form action="/login"><input type="email" name="email">'
            '<input type="tel"><input type="text" name="otp_40912873">'
            '<input type="password" name="password"><button>Go</button></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.neutral_roles == ("field_1", "field_2")
        assert form.nameless_roles == ("field_1",)


class TestRegistrationFormsAndControlsOutsideTheirForm:
    def test_a_lone_registration_form_with_a_confirmation_is_not_a_login_form(self) -> None:
        """Only a lone form with one password (new-password misused) is kept."""
        html = (
            '<form action="/register"><input type="email" name="email">'
            '<input type="password" name="password">'
            '<input type="password" name="password_confirm"></form>'
        )
        assert extract_login_forms(html, source="s") == ()
        assert is_login_document(html)

    def test_a_scoped_name_shared_within_the_form_falls_back(self) -> None:
        """A name selector must pick one input of its form."""
        html = (
            '<form action="/login"><input type="text" name="user" id="u1" class="first">'
            '<input type="password" name="password">'
            '<input type="checkbox" name="password"><button>Go</button></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields["password"] == 'form[action="/login"] input[type="password"]'  # noqa: S105
        _assert_first_match(html, form.fields["password"], name="password")
        first = lxml.html.fromstring(f"<html><body>{html}</body></html>").cssselect(
            form.fields["password"]
        )[0]
        assert first.get("type") == "password"

    @pytest.mark.parametrize(
        ("control", "expected", "check"),
        [
            (
                '<button type="submit" form="signin" id="outside">Sign in</button>',
                "#outside",
                {"id_": "outside"},
            ),
            (
                '<button type="submit" form="signin" name="go">Sign in</button>',
                'button[form="signin"][name="go"]',
                {"name": "go"},
            ),
            (
                '<button type="submit" form="signin">Sign in</button>',
                'button[form="signin"][type="submit"]',
                {"text": "Sign in"},
            ),
        ],
        ids=["id", "name", "type"],
    )
    def test_a_control_outside_its_form_is_selected_by_its_form_attribute(
        self, control: str, expected: str, check: dict
    ) -> None:
        """A descendant selector would match nothing for a control outside its form."""
        html = (
            '<form id="signin" action="/login"><input name="username">'
            '<input type="password" name="password"></form>'
            f"{control}"
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.submit == expected
        _assert_first_match(html, form.submit, **check)
        fields, submit = printable_selectors(form)
        assert submit == expected


class TestSelectorsCountTheControlsInsideTheForm:
    def test_a_name_counted_on_what_the_form_physically_holds(self) -> None:
        """A control inside the form but owned by another still matches a
        descendant selector, so it counts against the name's uniqueness."""
        html = (
            '<form id="search" action="/search"></form>'
            '<form action="/login"><input type="search" name="username" form="search">'
            '<input name="username"><input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        selector = form.fields["username"]
        assert "name=" not in selector
        _assert_first_match(html, selector, name="username")
        first = lxml.html.fromstring(f"<html><body>{html}</body></html>").cssselect(selector)[0]
        assert first.get("type") is None


class TestTwoFormsPostingToOneAction:
    """A scoped selector matches every form with the same action, so a name is
    unique only across all of them."""

    _PAGE = (
        '<form action="/session" method="post" class="mini">'
        '<input type="email" name="login[username]">'
        '<input type="password" name="login[password]">'
        "<button>Go</button>"
        "</form>"
        '<form action="/session" method="post" class="main">'
        '<input type="email" name="login[username]" id="email">'
        '<input type="password" name="login[password]" id="pass">'
        '<button id="send2">Sign in</button>'
        "</form>"
    )

    def test_the_main_form_is_selected_by_its_ids(self) -> None:
        header, main = extract_login_forms(self._PAGE, source="s")
        assert main.fields == {"username": "#email", "password": "#pass"}
        assert main.submit == "#send2"
        for selector, id_ in (("#email", "email"), ("#pass", "pass"), ("#send2", "send2")):
            _assert_first_match(self._PAGE, selector, id_=id_)

    def test_the_header_form_s_shared_names_are_unresolved(self) -> None:
        header, _main = extract_login_forms(self._PAGE, source="s")
        assert header.fields == {}
        assert header.unresolved_roles == ("username", "password", "submit")


class TestChangePasswordForms:
    def test_current_and_new_password_with_no_username_is_not_a_login_form(self) -> None:
        html = (
            '<form action="/account/password"><input type="password" name="current_password" '
            'autocomplete="current-password"><input type="password" name="new_password" '
            'autocomplete="new-password"></form>'
        )
        assert extract_login_forms(html, source="s") == ()
        assert is_login_document(html)

    def test_a_page_wide_form_with_a_username_stays_a_login_form(self) -> None:
        html = (
            '<form action="/account"><input type="email" name="login_email">'
            '<input type="password" name="login_password" autocomplete="current-password">'
            '<input type="password" name="new_password" autocomplete="new-password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.action == "/account"


def test_a_new_password_named_but_not_marked_is_a_change_password_form() -> None:
    """A password input named for a new password, with no autocomplete mark, still
    makes a current-password form a change-password form."""
    html = (
        '<form action="/account/password"><input type="password" name="current_password" '
        'autocomplete="current-password"><input type="password" name="new_password"></form>'
    )
    assert extract_login_forms(html, source="s") == ()


def test_a_new_password_id_but_no_mark_is_a_change_password_form() -> None:
    """A password input whose id names a new password does the same."""
    html = (
        '<form action="/account/password"><input type="password" name="current" '
        'autocomplete="current-password"><input type="password" name="pw2" '
        'id="new_password"></form>'
    )
    assert extract_login_forms(html, source="s") == ()


class TestFormActionTargets:
    """The form_post-shaped forms on a page, each with its hidden input names."""

    _BASE = "https://api.myshop.example.com/authorize/resume"

    def test_a_hidden_only_post_form_resolves_against_the_base(self) -> None:
        html = (
            '<form method="post" action="../callback"><input type="hidden" name="code">'
            '<input type="hidden" name="state"><noscript><input type="submit"></noscript>'
            "</form>"
        )
        assert form_action_targets(html, self._BASE) == {
            ("api.myshop.example.com", "/callback"): frozenset({"code", "state"})
        }

    def test_an_empty_action_posts_to_the_page_itself(self) -> None:
        html = '<form method="post"><input type="hidden" name="code"></form>'
        assert form_action_targets(html, self._BASE) == {
            ("api.myshop.example.com", "/authorize/resume"): frozenset({"code"})
        }

    def test_two_forms_posting_to_one_target_keep_every_hidden_name(self) -> None:
        html = (
            '<form method="post" action="/callback"><input type="hidden" name="code"></form>'
            '<form method="post" action="/callback"><input type="hidden" name="state"></form>'
        )
        assert form_action_targets(html, self._BASE) == {
            ("api.myshop.example.com", "/callback"): frozenset({"code", "state"})
        }

    def test_the_target_host_is_lower_case_without_a_default_port(self) -> None:
        html = (
            '<form method="post" action="https://API.MyShop.example.com:443/callback">'
            '<input type="hidden" name="code"></form>'
        )
        assert form_action_targets(html, self._BASE) == {
            ("api.myshop.example.com", "/callback"): frozenset({"code"})
        }

    @pytest.mark.parametrize(
        "html",
        [
            '<form method="get" action="/search"><input type="hidden" name="q"></form>',
            '<form method="post" action="/cart"><input type="hidden" name="sku">'
            '<input type="number" name="qty"></form>',
            '<form method="post" action="/logout"><input type="hidden" name="csrf">'
            "<button>Log out</button></form>",
            '<form method="post" action="/logout"><input type="hidden" name="csrf">'
            '<input type="submit" value="Log out"></form>',
            '<form method="post" action="/empty"></form>',
            '<form method="post" action="/cart"><input type="hidden" name="sku">'
            '<select name="qty"><option>1</option></select></form>',
            '<form method="post" action="/review"><input type="hidden" name="sku">'
            '<textarea name="note"></textarea></form>',
        ],
        ids=[
            "get",
            "visible-input",
            "visible-button",
            "visible-submit",
            "no-hidden",
            "select",
            "textarea",
        ],
    )
    def test_a_form_that_is_not_form_post_shaped_is_left_out(self, html: str) -> None:
        assert form_action_targets(html, self._BASE) == {}


def _in_dialog(inner: str, opening: str = '<div class="modal" role="dialog">') -> str:
    """*inner* inside a dialog element opened by *opening*."""
    closing = "</dialog>" if opening.startswith("<dialog") else "</div>"
    return f"{opening}{inner}{closing}"


def _formless(html: str) -> list:  # noqa: ANN401
    return [form for form in extract_login_forms(html, source="s") if form.formless]


_DIALOG_MODAL = _in_dialog(
    '<div class="login-form">'
    '<input type="text" id="login-username" autocomplete="username">'
    '<input type="password" id="login-password" autocomplete="current-password">'
    '<button type="submit" class="btn">Sign in</button></div>'
)


class TestLoginFieldsInADialogOutsideAnyForm:
    """A password input outside every ``<form>`` and inside a dialog (a script-driven
    login modal) yields a login form built from the dialog's formless controls (#267)."""

    def test_roles_and_id_selectors(self) -> None:
        (form,) = extract_login_forms(_DIALOG_MODAL, source="https://myshop.example/")
        assert form.fields == {"username": "#login-username", "password": "#login-password"}
        assert form.action == ""
        assert form.method == ""
        assert form.formless
        # The submit button has neither an id nor a name, and with no form scope
        # nothing else picks it.
        assert form.submit is None
        assert form.unresolved_roles == ("submit",)
        assert form.absent_roles == ()
        # No target of its own: the digest never matches a POST to it by target.
        assert form.action_target == ("", "")

    @pytest.mark.parametrize(
        "opening",
        [
            '<div class="modal" role="dialog">',
            '<div class="modal" role="alertdialog">',
            '<div class="modal" aria-modal="true">',
            '<section role="DIALOG">',
            "<dialog open>",
        ],
        ids=["role-dialog", "role-alertdialog", "aria-modal", "role-upper-case", "dialog"],
    )
    def test_each_dialog_marker_bounds_a_group(self, opening: str) -> None:
        closing = {"<dialog open>": "</dialog>", '<section role="DIALOG">': "</section>"}
        html = (
            f'{opening}<input type="email" id="u"><input type="password" id="p">'
            f'<button type="submit" id="go">Sign in</button>{closing.get(opening, "</div>")}'
            '<main><button type="submit" id="add-to-cart">Add to cart</button></main>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert (form.fields, form.submit) == ({"username": "#u", "password": "#p"}, "#go")

    @pytest.mark.parametrize(
        "opening",
        ['<div class="modal">', '<div role="presentation">', '<div aria-modal="false">'],
        ids=["plain-div", "other-role", "aria-modal-false"],
    )
    def test_a_password_outside_every_dialog_is_in_no_group(self, opening: str) -> None:
        html = (
            f'{opening}<input type="email" id="u"><input type="password" id="p">'
            '<button type="submit" id="go">Sign in</button></div>'
        )
        assert extract_login_forms(html, source="s") == ()

    def test_unique_names_are_selected_by_name_and_a_shared_one_is_unresolved(self) -> None:
        html = (
            _in_dialog(
                '<input type="email" name="email"><input type="password" name="password">'
                '<button type="submit" name="go">Sign in</button>'
            )
            + '<footer><input type="email" name="email" placeholder="Newsletter"></footer>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"password": 'input[name="password"]'}
        assert "username" in form.unresolved_roles
        assert form.absent_roles == ()
        assert form.submit == 'button[name="go"]'

    def test_a_submit_with_no_id_or_unique_name_is_unresolved(self) -> None:
        html = _in_dialog(
            '<input name="user" id="u"><input type="password" name="pw">'
            '<button type="submit">Sign in</button>'
            '<button type="submit" name="go" id="go-btn">Go</button>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": "#u", "password": 'input[name="pw"]'}
        assert form.submit is None
        assert "submit" in form.unresolved_roles
        assert form.absent_roles == ()

    @pytest.mark.parametrize(
        "button",
        [
            '<input type="submit" id="go" value="Sign in">',
            '<input type="image" id="go" src="x.png">',
            '<button type="submit" id="go">Sign in</button>',
        ],
        ids=["input-submit", "input-image", "button-submit"],
    )
    def test_an_explicit_submit_after_the_password_is_the_submit(self, button: str) -> None:
        html = _in_dialog(f'<input id="u"><input type="password" id="p">{button}')
        (form,) = extract_login_forms(html, source="s")
        assert form.submit == "#go"

    def test_a_submit_before_the_password_is_not_the_submit(self) -> None:
        html = _in_dialog(
            '<button type="submit" id="back">Back</button><input id="u">'
            '<input type="password" id="p">'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.submit is None
        assert form.absent_roles == ("submit",)

    def test_a_type_button_is_never_the_submit(self) -> None:
        html = _in_dialog(
            '<input id="u"><input type="password" id="p">'
            '<button type="button" id="go">Sign in</button>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.submit is None
        assert form.absent_roles == ("submit",)
        assert form.unresolved_roles == ("submit",)

    def test_a_show_password_toggle_beside_the_password_is_never_the_submit(self) -> None:
        """Review round 2 (n1 T1, shapes A and A2): the toggle in the password's own
        wrapper is not the submit, and the username before it is kept. Since review
        round 5 the toggle also ends the submit search, so the sign-in after it is a
        GP-FILL rather than a guess."""
        for sign_in in (
            '<button type="submit" id="go">Sign in</button>',
            '<button type="submit" id="go">x</button>',
        ):
            html = _in_dialog(
                '<div class="row"><input type="email" id="u" name="email"></div>'
                '<div class="input-group"><input type="password" id="p" name="password">'
                '<button type="button" id="eye">show</button></div>'
                f'<div class="row">{sign_in}</div>'
            )
            (form,) = extract_login_forms(html, source="s")
            assert form.fields == {"username": "#u", "password": "#p"}
            assert form.submit is None
            assert "submit" in form.unresolved_roles

    def test_a_toggle_and_a_type_button_sign_in_leave_the_submit_to_a_gp_fill(self) -> None:
        """Review round 2 (n1 T2, shapes A3)."""
        html = _in_dialog(
            '<div class="row"><input type="email" id="u" name="email"></div>'
            '<div class="input-group"><input type="password" id="p" name="password">'
            '<button type="button" id="eye">show</button></div>'
            '<div class="row"><button type="button" id="go">Sign in</button></div>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": "#u", "password": "#p"}
        assert form.submit is None
        assert "submit" in form.unresolved_roles

    def test_a_close_button_before_the_sign_in_is_never_the_submit(self) -> None:
        """Review round 2 (n3 B1): a Bootstrap footer of two type=button buttons."""
        html = _in_dialog(
            '<div class="modal-header"><button type="button" class="btn-close" id="x">'
            '</button></div><div class="modal-body"><input type="email" id="u">'
            '<input type="password" id="p"></div><div class="modal-footer">'
            '<button type="button" id="close">Close</button>'
            '<button type="button" id="go">Sign in</button></div>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.submit is None
        assert "submit" in form.unresolved_roles

    def test_a_bootstrap_footer_s_close_button_ends_the_submit_search(self) -> None:
        """A Close ``type="button"`` before the submitting Sign in ends the search
        (review round 5): the submit is a GP-FILL, the fields are kept."""
        html = (
            '<header><input type="search" id="q" name="q"></header>'
            + _in_dialog(
                '<div class="modal-dialog"><div class="modal-content">'
                '<div class="modal-body"><input type="email" id="login-email">'
                '<input type="password" id="login-password"></div>'
                '<div class="modal-footer"><button type="button" id="close">Close</button>'
                '<button type="submit" id="login-go">Sign in</button></div></div></div>'
            )
            + '<main><button type="submit" id="add-to-cart">Add to cart</button></main>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": "#login-email", "password": "#login-password"}
        assert form.submit is None
        assert "submit" in form.unresolved_roles

    def test_a_bootstrap_footer_whose_first_button_submits(self) -> None:
        html = _in_dialog(
            '<div class="modal-body"><input type="email" id="login-email">'
            '<input type="password" id="login-password"></div>'
            '<div class="modal-footer"><button type="submit" id="login-go">Sign in</button>'
            '<button type="button" id="close">Close</button></div>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.submit == "#login-go"

    def test_a_selector_never_carries_a_form_scope(self) -> None:
        html = _in_dialog(
            '<input name="username"><input type="password" name="password">'
            '<button type="submit" name="signin">Sign in</button>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {
            "username": 'input[name="username"]',
            "password": 'input[name="password"]',
        }
        assert form.submit == 'button[name="signin"]'
        assert not any("form" in s for s in [*form.fields.values(), form.submit])
        assert printable_selectors(form) == (form.fields, form.submit)
        _assert_first_match(html, form.fields["username"], name="username")
        _assert_first_match(html, form.submit, name="signin")

    def test_beside_a_real_login_form_both_are_kept_the_real_one_first(self) -> None:
        html = (
            '<form action="/login" method="post"><input name="email" id="email">'
            '<input type="password" name="password" id="pw"><button type="submit" id="go">Go'
            "</button></form>"
            f"{_DIALOG_MODAL}"
        )
        real, formless = extract_login_forms(html, source="s")
        assert (real.action, real.method, real.formless) == ("/login", "POST", False)
        assert real.fields == {"username": "#email", "password": "#pw"}
        assert formless.formless
        assert formless.fields == {
            "username": "#login-username",
            "password": "#login-password",
        }
        assert real.signature != formless.signature

    def test_a_real_form_s_controls_are_never_part_of_the_group(self) -> None:
        html = _in_dialog(
            '<form action="/search"><input type="text" name="q" id="q"></form>'
            '<input type="password" id="pin"><button type="submit" id="unlock">Unlock</button>'
        )
        (form,) = _formless(html)
        assert form.fields == {"password": "#pin"}
        assert form.absent_roles == ("username",)
        assert form.submit == "#unlock"

    def test_a_control_owned_by_a_form_through_its_form_attribute_is_not_formless(self) -> None:
        html = '<form id="signin" action="/login"><input name="username" id="u"></form>' + (
            _in_dialog('<input type="password" name="password" id="p" form="signin">')
        )
        (form,) = extract_login_forms(html, source="s")
        assert not form.formless
        assert form.action == "/login"

    def test_a_form_attribute_naming_no_form_leaves_the_control_formless(self) -> None:
        html = _in_dialog(
            '<input name="username" id="u" form="gone">'
            '<input type="password" name="password" id="p" form="gone">'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.formless
        assert form.fields == {"username": "#u", "password": "#p"}

    def test_inside_a_form_a_form_attribute_naming_no_form_leaves_the_control_formless(
        self,
    ) -> None:
        """A ``form`` attribute naming no form leaves the control with no form owner,
        even inside a ``<form>``, so it belongs to no form's controls."""
        html = _in_dialog(
            '<form action="/x"><input name="u" id="u" form="gone">'
            '<input type="password" id="p" form="gone"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.formless
        assert form.fields == {"username": "#u", "password": "#p"}

    @pytest.mark.parametrize(
        "inner",
        [
            '<input type="email" id="e"><input type="password" id="p" autocomplete="new-password">',
            '<input type="email" id="e"><input type="password" id="p" name="password">'
            '<input type="password" id="c" name="password_confirm">',
            '<input type="password" id="cur" autocomplete="current-password">'
            '<input type="password" id="new" name="new_password">',
            '<input type="email" id="le"><input type="password" id="lp">'
            '<button type="submit" id="lgo">In</button>'
            '<input type="email" id="se"><input type="password" id="sp">'
            '<button type="submit" id="sgo">Up</button>',
        ],
        ids=["new-password", "confirmation", "change-password", "two-unmarked-passwords"],
    )
    def test_a_dialog_the_rules_cannot_read_as_one_login_yields_nothing(self, inner: str) -> None:
        assert extract_login_forms(_in_dialog(inner), source="s") == ()

    def test_two_passwords_one_marked_current_password_is_a_login(self) -> None:
        html = _in_dialog(
            '<input type="email" id="le"><input type="password" id="lp" '
            'autocomplete="current-password"><button type="submit" id="lgo">Sign in</button>'
            '<input type="password" id="pin" name="pin">'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields["password"] == "#lp"  # noqa: S105

    def test_a_lone_new_password_form_keeps_its_old_result_beside_a_dialog_login(self) -> None:
        """A real form's result never depends on the formless controls."""
        real_form = (
            '<form action="/register"><input type="email" name="email" id="reg-email">'
            '<input type="password" name="password" id="reg-pw" autocomplete="new-password">'
            "</form>"
        )
        (alone,) = extract_login_forms(real_form, source="s")
        real, formless = extract_login_forms(real_form + _DIALOG_MODAL, source="s")
        assert real == alone
        assert formless.formless

    def test_a_misused_new_password_login_form_survives_a_pin_dialog(self) -> None:
        real_form = (
            '<form action="/login" method="post"><input name="user" id="u">'
            '<input type="password" id="p" name="password" autocomplete="new-password">'
            '<button type="submit" id="b">Go</button></form>'
        )
        (alone,) = extract_login_forms(real_form, source="s")
        forms = extract_login_forms(
            real_form + _in_dialog('<input type="password" id="giftpin">'), source="s"
        )
        assert forms[0] == alone
        assert [form.fields for form in forms[1:]] == [{"password": "#giftpin"}]

    def test_controls_inside_noscript_are_left_out(self) -> None:
        only_noscript = _in_dialog(
            '<noscript><input type="text" id="u"><input type="password" id="p"></noscript>'
        )
        assert extract_login_forms(only_noscript, source="s") == ()
        mixed = _in_dialog(
            '<input type="text" id="u"><input type="password" id="p">'
            '<noscript><button type="submit" id="fallback">Sign in</button></noscript>'
            '<button type="submit" id="go">Sign in</button>'
        )
        (form,) = extract_login_forms(mixed, source="s")
        assert form.submit == "#go"

    def test_hidden_inputs_are_the_dialog_s_own(self) -> None:
        html = '<input type="hidden" name="page_token" value="y">' + _in_dialog(
            '<input type="hidden" name="csrf_token" value="x">'
            '<input id="u"><input type="password" id="p">'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.hidden == ("csrf_token",)

    def test_the_same_dialog_on_two_pages_has_one_signature(self) -> None:
        (one,) = extract_login_forms(_DIALOG_MODAL, source="https://myshop.example/")
        (two,) = extract_login_forms(_DIALOG_MODAL, source="https://myshop.example/cart")
        assert one.signature == two.signature

    def test_the_signature_ignores_formless_controls_outside_the_dialog(self) -> None:
        (bare,) = extract_login_forms(_DIALOG_MODAL, source="s")
        elsewhere = (
            '<header><input type="search" name="q" id="q"></header>'
            f"{_DIALOG_MODAL}"
            '<aside><input type="checkbox" name="color" id="c-red">'
            '<input type="hidden" name="filter_token" value="x"></aside>'
        )
        (busy,) = extract_login_forms(elsewhere, source="s")
        assert (busy.signature, busy.input_names, busy.hidden) == (
            bare.signature,
            bare.input_names,
            bare.hidden,
        )

    def test_a_formless_signature_never_equals_a_form_s(self) -> None:
        controls = '<input id="u"><input type="password" id="p">'
        (formless,) = extract_login_forms(_in_dialog(controls), source="s")
        (real,) = extract_login_forms(f"<form>{controls}</form>", source="s")
        assert formless.signature != real.signature

    def test_a_page_with_no_formless_password_is_unchanged(self) -> None:
        html = f"{_LOGIN_PAGE}" + _in_dialog('<input type="text" name="q" id="search">')
        (form,) = extract_login_forms(html, source="s")
        assert not form.formless
        assert extract_login_forms(_LOGIN_PAGE, source="s") == (form,)


class TestADialogBoundsItsLoginGroup:
    """The group is the dialog's controls and nothing else on the page (#267 review)."""

    def test_a_modal_among_a_busy_page_s_formless_controls(self) -> None:
        html = (
            "<html><body>"
            '<header><input type="search" name="q" id="q">'
            '<button type="submit" id="search-go">Go</button>'
            '<a href="#" id="signin-link">Sign in</a></header>'
            '<main><div class="product"><input type="number" name="qty" id="qty">'
            '<button type="submit" id="add-to-cart">Add to cart</button></div></main>'
            + _in_dialog(
                '<div class="modal-body"><input type="email" id="login-email" name="email">'
                '<input type="password" id="login-password" name="password">'
                '<button type="submit" id="login-go">Sign in</button></div>'
            )
            + '<footer><input type="email" name="newsletter" id="nl">'
            '<button type="submit" id="subscribe">Subscribe</button></footer>'
            '<div class="cookies"><button type="submit" id="accept">Accept</button></div>'
            "</body></html>"
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": "#login-email", "password": "#login-password"}
        assert form.submit == "#login-go"
        assert form.input_names == ("email", "password")
        _assert_first_match(html, form.submit, id_="login-go")

    @pytest.mark.parametrize(
        "html",
        [
            # n3 B2: role=dialog, password and a sign-in link, inside an app wrapper.
            '<body><div id="app"><header><input type="text" name="q" id="q"></header>'
            '<div role="dialog"><input type="password" id="pin"><a href="#" id="go">Sign in</a>'
            '</div><main><button type="submit" id="cart">Add to cart</button></main></div></body>',
            # n3 B3: <dialog> with a password and a link, a newsletter around it.
            '<body><div class="page"><footer><input type="email" id="nl" name="newsletter">'
            '</footer><dialog><input type="password" id="pin"><a id="go">Continue</a></dialog>'
            '<button type="submit" id="sub">Subscribe</button></div></body>',
        ],
        ids=["B2", "B3"],
    )
    def test_a_password_only_dialog_takes_nothing_from_around_it(self, html: str) -> None:
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"password": "#pin"}
        assert form.submit is None
        assert form.absent_roles == ("username", "submit")

    def test_nested_dialogs_each_hold_their_own_controls(self) -> None:
        html = (
            '<div role="dialog" id="login"><input type="email" id="u">'
            '<input type="password" id="p" autocomplete="current-password">'
            '<dialog id="confirm"><input type="password" id="pin">'
            '<button type="submit" id="ok">OK</button></dialog>'
            '<button type="submit" id="go">Sign in</button></div>'
        )
        outer, inner = extract_login_forms(html, source="s")
        assert (outer.fields, outer.submit) == ({"username": "#u", "password": "#p"}, "#go")
        assert (inner.fields, inner.submit) == ({"password": "#pin"}, "#ok")

    def test_a_dialog_s_end_ignores_other_tags_left_open_or_closed_stray(self) -> None:
        """Only the dialog's own tag counts: an unclosed ``<p>`` or ``<li>`` and a
        stray ``</p>`` inside it do not move its end."""
        html = (
            '<p>Welcome<div role="dialog"><p>Sign in<ul><li><input id="u">'
            '<li><input type="password" id="p"></ul></p></p><span></p></span>'
            '<button type="submit" id="go">Go</button></div>'
            '<main><input name="q" id="q"><button type="submit" id="cart">Add to cart'
            "</button></main>"
        )
        (form,) = extract_login_forms(html, source="s")
        assert (form.fields, form.submit) == ({"username": "#u", "password": "#p"}, "#go")

    def test_inner_elements_of_the_dialog_s_tag_are_counted(self) -> None:
        html = (
            '<div role="dialog"><div class="body"><div><input id="u"></div>'
            '<input type="password" id="p"></div><div class="footer">'
            '<button type="submit" id="go">Go</button></div></div>'
            '<button type="submit" id="cart">Cart</button>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.submit == "#go"

    def test_a_stray_end_tag_of_the_dialog_s_tag_ends_it_early(self) -> None:
        """What follows is left out: the group only ever shrinks."""
        html = (
            '<div role="dialog"><input id="u"><input type="password" id="p"></div></div>'
            '<button type="submit" id="go">Go</button></div>'
            '<button type="submit" id="cart">Cart</button>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.submit is None

    def test_a_stray_end_tag_before_the_dialog_does_not_end_it(self) -> None:
        html = (
            '</div></div><div role="dialog"><input id="u"><input type="password" id="p">'
            '<button type="submit" id="go">Go</button></div>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.submit == "#go"

    def test_a_void_end_tag_is_not_counted(self) -> None:
        html = (
            '<div role="dialog"><input id="u"></input><br></br><img src="x"></img>'
            '<input type="password" id="p"><button type="submit" id="go">Go</button></div>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.submit == "#go"

    def test_an_unclosed_dialog_runs_to_the_end(self) -> None:
        html = (
            '<dialog><input id="u"><input type="password" id="p">'
            '<button type="submit" id="go">Go</button>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.submit == "#go"

    def test_a_long_page_of_unclosed_elements_parses_in_linear_time(self) -> None:
        def page(n: int) -> str:
            return "<ul>" + "<li><input name='q'>" * n + "</ul>" + _DIALOG_MODAL

        _assert_parses_in_linear_time(page)
        (form,) = extract_login_forms(page(20000), source="s")
        assert form.fields["password"] == "#login-password"  # noqa: S105

    def test_two_copies_of_a_modal_are_two_groups(self) -> None:
        html = _in_dialog(
            '<input id="u1" name="username"><input type="password" id="p1" name="password">'
            '<button type="submit" id="g1">Go</button>'
        ) + _in_dialog(
            '<input id="u2" name="username"><input type="password" id="p2" name="password">'
            '<button type="submit" id="g2">Go</button>'
        )
        one, two = extract_login_forms(html, source="s")
        assert (one.fields, one.submit) == ({"username": "#u1", "password": "#p1"}, "#g1")
        assert (two.fields, two.submit) == ({"username": "#u2", "password": "#p2"}, "#g2")

    def test_a_current_password_group_is_listed_first(self) -> None:
        html = _in_dialog(
            '<input id="a"><input type="password" id="ap"><button type="submit" id="ago">Go'
            "</button>"
        ) + _in_dialog(
            '<input id="b"><input type="password" id="bp" autocomplete="current-password">'
            '<button type="submit" id="bgo">Go</button>'
        )
        first, second = extract_login_forms(html, source="s")
        assert first.fields["password"] == "#bp"  # noqa: S105
        assert second.fields["password"] == "#ap"  # noqa: S105

    @pytest.mark.parametrize(
        ("signup_opening", "signup_ids"),
        [
            ('<div role="dialog" id="signup-modal">', ("se", "sp")),
            ('<div role="dialog" class="modal">', ("register-email", "register-pw")),
        ],
        ids=["dialog-id", "control-ids"],
    )
    def test_a_sign_up_named_dialog_is_listed_after_the_login(
        self, signup_opening: str, signup_ids: tuple[str, str]
    ) -> None:
        email, password = signup_ids
        html = _in_dialog(
            f'<input type="email" id="{email}"><input type="password" id="{password}">'
            '<button type="submit" id="sgo">Create</button>',
            signup_opening,
        ) + _in_dialog(
            '<input type="email" id="le"><input type="password" id="lp">'
            '<button type="submit" id="lgo">Sign in</button>'
        )
        first, second = extract_login_forms(html, source="s")
        assert first.fields == {"username": "#le", "password": "#lp"}
        assert second.fields == {"username": f"#{email}", "password": f"#{password}"}

    def test_a_sign_up_dialog_marked_new_password_beside_a_login_dialog(self) -> None:
        html = _in_dialog(
            '<input type="email" id="le"><input type="password" id="lp">'
            '<button type="submit" id="lgo">Sign in</button>'
        ) + _in_dialog(
            '<input type="email" id="se"><input type="password" id="sp" '
            'autocomplete="new-password"><button type="submit" id="sgo">Create</button>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": "#le", "password": "#lp"}

    def test_real_forms_never_depend_on_the_formless_groups(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Random pages with forms, dialogs, stray and unclosed tags: the real forms
        are the same with formless groups built and with them switched off. This
        compares against formless groups switched off in this code, not against
        main: it shows real forms do not depend on the formless groups."""
        from graftpunk.har import documents

        rng = random.Random(267)  # noqa: S311 - a fixed sample, not a secret
        pages = [_random_page(rng) for _ in range(1500)]
        built = [
            [f for f in extract_login_forms(html, source="s") if not f.formless] for html in pages
        ]
        monkeypatch.setattr(documents, "_formless_login_groups", lambda _parsed: [])
        assert [list(extract_login_forms(html, source="s")) for html in pages] == built
        assert sum(1 for forms in built if forms) > 100


_RANDOM_TAGS = ("div", "p", "li", "ul", "span", "option", "td", "section", "dialog", "noscript")
_RANDOM_CONTROLS = (
    '<input id="u{n}" name="user">',
    '<input type="email" name="email" autocomplete="email">',
    '<input type="password" id="p{n}" name="password">',
    '<input type="password" autocomplete="new-password" name="pw2">',
    '<input type="password" autocomplete="current-password" id="cp{n}">',
    '<input type="hidden" name="csrf">',
    '<button type="submit" id="b{n}">Go</button>',
    '<button type="button" id="bb{n}">Go</button>',
    '<input type="submit" id="s{n}">',
    '<input type="password" name="password_confirm">',
    '<input form="f1" name="ext" id="e{n}">',
    '<input form="nope" type="password" id="np{n}">',
)


def _random_page(rng: random.Random) -> str:
    out = []
    in_form = False
    for n in range(rng.randint(1, 30)):
        roll = rng.random()
        if roll < 0.15:
            role = ' role="dialog"' if rng.random() < 0.3 else ""
            out.append(f"<{rng.choice(_RANDOM_TAGS)}{role}>")
        elif roll < 0.25:
            out.append(f"</{rng.choice(_RANDOM_TAGS)}>")
        elif roll < 0.32 and not in_form:
            form_id = ' id="f1"' if rng.random() < 0.5 else ""
            out.append(f'<form action="/a{rng.randint(0, 2)}"{form_id} method="post">')
            in_form = True
        elif roll < 0.38 and in_form:
            out.append("</form>")
            in_form = False
        else:
            out.append(rng.choice(_RANDOM_CONTROLS).format(n=n))
    return "".join(out)


# Every shape the two reviews probed outside a dialog, round 1 and round 2: with no
# dialog around the password, none gives a formless login form.
_REVIEW_PROBES_OUTSIDE_A_DIALOG = {
    "r1-type-button-then-footer": (
        '<div id="m"><input type="text" id="lu" name="account"><input type="password" id="lp">'
        '<button type="button" id="go">Sign in</button></div><footer><input type="email" '
        'id="nl" name="newsletter"><button id="sub">Subscribe</button></footer>'
    ),
    "r1-newsletter-before": (
        '<header><input type="email" id="nl" name="newsletter_email"></header>'
        '<div><input type="text" id="lu"><input type="password" id="lp"></div>'
    ),
    "r1-search-before-pin": (
        '<header><input type="text" id="q" name="q"></header>'
        '<div><input type="password" id="pin"><button id="go">Go</button></div>'
    ),
    "r1-signup-then-login": (
        '<div id="signup"><input type="email" id="se" name="email"><input type="password" '
        'id="sp" name="password"><button id="sgo">Create</button></div><div id="login">'
        '<input type="email" id="le" name="email"><input type="password" id="lp" '
        'name="password"><button id="lgo">Sign in</button></div>'
    ),
    "r1-qty-after": (
        '<div><input id="u" type="email"><input type="password" id="p"><a href="#" id="go">'
        'Sign in</a></div><main><input type="number" name="qty" id="qty1"><input type="text" '
        'name="coupon" id="coupon"></main>'
    ),
    "r1-cookie-banner": (
        '<div id="m"><input id="u" name="username" type="text"><input id="p" name="password" '
        'type="password"><button type="button" id="go">Sign in</button></div><div '
        'class="cookies"><input type="email" name="news" id="news"><button id="accept">'
        "Accept</button></div>"
    ),
    "r1-form-attribute-no-owner": (
        '<input type="hidden" name="csrf_token" value="x"><input name="username" form="nope">'
        '<input type="password" name="password"><button>x</button>'
    ),
    "r2-A-toggle": (
        '<div class="modal"><div class="f"><input type="email" id="u"></div><div class="f pw">'
        '<input type="password" id="p"><button type="button" id="eye">Show</button></div>'
        '<button id="signin">Sign in</button></div>'
    ),
    "r2-B-unclosed-p": (
        '<body><p>Welcome<header><input type="search" name="q" id="q"></header><div id="m">'
        '<input type="password" id="pin"></div><footer><input type="email" id="nl"><button '
        'id="sub">Subscribe</button></footer></body>'
    ),
    "r2-B2-unclosed-li": (
        '<body><ul><li><input type="search" id="q"><li><div><input type="password" id="pin">'
        '</div><li><button id="cart">Cart</button></ul></body>'
    ),
    "r2-J-body-footer": (
        '<div class="modal"><div class="body"><input id="u" type="text"><input type="password" '
        'id="p"></div><div class="footer"><button id="go">Sign in</button></div></div>'
    ),
    "r2-K-template": (
        '<template id="t"><div><input id="u" type="text"><input type="password" id="p">'
        '<button id="go">Go</button></div></template>'
    ),
    "r2-T3-app-wrapper": (
        '<body><div id="app"><header><input type="text" name="q" id="q"></header><div '
        'class="modal"><input type="password" id="pin"><a href="#" id="go">Sign in</a></div>'
        '<main><input type="number" name="qty" id="qty"><button id="cart">Add to cart</button>'
        "</main></div></body>"
    ),
    "r2-T12-body": (
        '<body><input id="u" name="user"><input type="password" id="p"><button id="go">Go'
        "</button></body>"
    ),
    "r2-D1-stray-p": (
        '<body><section><p>Welcome<div class="modal"><input id="u" name="user"><span>Sign in'
        '</p></span><input type="password" id="p"><a id="go">Go</a></div><input name="q" '
        'id="q"><button id="cart">Add to cart</button></section></body>'
    ),
    "r2-D2-stray-div": (
        '<body><div id="app"><div class="modal"><div class="body"><input id="u"></div></div>'
        '<input type="password" id="p"><a id="go">Go</a></div><button id="cart">Add to cart'
        "</button></div></body>"
    ),
}


@pytest.mark.parametrize(
    "html",
    list(_REVIEW_PROBES_OUTSIDE_A_DIALOG.values()),
    ids=list(_REVIEW_PROBES_OUTSIDE_A_DIALOG),
)
def test_a_review_probe_outside_a_dialog_gives_no_formless_form(html: str) -> None:
    assert _formless(html) == []


class TestDialogSubmitAndParsingRound3:
    """Review round 3 of #267: the submit inside a dialog, self-closing tags, role
    tokens, void elements, and many open dialogs."""

    @pytest.mark.parametrize(
        ("inner", "submit"),
        [
            # toggle.py: a typeless show-password toggle, then a type=submit sign-in.
            # The toggle ends the submit search (review round 5): a GP-FILL.
            (
                '<input type="email" id="u" name="email"><div class="pw">'
                '<input type="password" id="p" name="password">'
                '<button class="toggle" aria-label="Show password" id="eye"></button></div>'
                '<button type="submit" id="signin">Sign in</button>',
                None,
            ),
            # A typeless toggle, then a typeless sign-in: neither submits outside a form.
            (
                '<input type="email" id="u" name="email"><input type="password" id="p" '
                'name="password"><button aria-label="Show password" id="eye"></button>'
                '<button id="signin">Sign in</button>',
                None,
            ),
            # A typeless "Forgot password?" button, then a typeless sign-in.
            (
                '<input type="email" id="u" name="email"><input type="password" id="p" '
                'name="password"><button class="link" id="forgot">Forgot password?</button>'
                '<button id="signin">Sign in</button>',
                None,
            ),
        ],
        ids=["typeless-toggle-then-submit", "two-typeless", "forgot-then-typeless"],
    )
    def test_a_typeless_button_is_never_a_dialog_s_submit(
        self, inner: str, submit: str | None
    ) -> None:
        (form,) = extract_login_forms(_in_dialog(inner), source="s")
        assert form.fields == {"username": "#u", "password": "#p"}
        assert form.submit == submit
        assert ("submit" in form.unresolved_roles) is (submit is None)

    def test_a_typeless_button_still_submits_a_real_form(self) -> None:
        html = (
            '<form action="/login"><input id="u"><input type="password" id="p">'
            '<button id="go">Go</button></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.submit == "#go"

    @pytest.mark.parametrize(
        "html",
        [
            '<div role="dialog"/><input id="u"><input type="password" id="p">'
            '<input type="submit" id="go"></div>',
            '<div role="dialog"><div class="spacer"/><input id="u"><input type="password" '
            'id="p"></div><input type="submit" id="go"></div>',
        ],
        ids=["dialog-self-closing", "inner-div-self-closing"],
    )
    def test_a_self_closing_non_void_element_opens_as_a_browser_reads_it(self, html: str) -> None:
        (form,) = extract_login_forms(html, source="s")
        assert (form.fields, form.submit) == ({"username": "#u", "password": "#p"}, "#go")

    def test_a_self_closing_form_still_closes_at_once(self) -> None:
        """Unchanged for real forms: ``<form/>`` holds nothing, as before."""
        html = (
            '<form action="/login" method="post"/><input name="u" id="u">'
            '<input type="password" id="p"></form>'
        )
        assert extract_login_forms(html, source="s") == ()

    def test_a_self_closing_element_in_svg_closes_at_once(self) -> None:
        html = (
            '<svg><g role="dialog"/></svg><input id="u"><input type="password" id="p">'
            '<input type="submit" id="go">'
        )
        assert extract_login_forms(html, source="s") == ()

    @pytest.mark.parametrize(
        ("attribute", "is_dialog"),
        [
            ('role="dialog presentation"', True),
            ('role="alertdialog dialog"', True),
            ('role=" dialog "', True),
            ('role="presentation dialog"', False),
            ('role="dialogue"', False),
            ('aria-modal="TRUE"', True),
            ('aria-modal=" true "', True),
        ],
    )
    def test_role_is_read_as_a_token_list_and_aria_modal_without_case(
        self, attribute: str, is_dialog: bool
    ) -> None:
        html = (
            f'<div {attribute}><input id="u"><input type="password" id="p">'
            '<input type="submit" id="go"></div>'
        )
        assert bool(_formless(html)) is is_dialog

    def test_a_void_element_marked_as_a_dialog_opens_none(self) -> None:
        html = (
            '<img role="dialog" src="x.png"><input id="u"><input type="password" id="p">'
            '<input type="submit" id="go">'
        )
        assert extract_login_forms(html, source="s") == ()

    def test_thousands_of_nested_dialogs_parse_in_linear_time(self) -> None:
        def page(depth: int) -> str:
            return (
                '<div role="dialog"><span></span>' * depth
                + '<input id="u"><input type="password" id="p"><input type="submit" id="go">'
                + "</div>" * depth
            )

        _assert_parses_in_linear_time(page)
        forms = extract_login_forms(page(20000), source="s")
        assert [form.submit for form in forms] == ["#go"]

    def test_a_dialog_missing_its_own_end_tag_runs_on_past_its_cell(self) -> None:
        """A known limit (review round 3, A12): a browser closes the dialog's
        ``<div>`` at ``</td>``; here only a ``</div>`` closes it, so the next cell's
        input joins the group. Its typeless button is no submit, so nothing is
        clicked that the user did not mean."""
        html = (
            '<table><tr><td><div role="dialog"><input type="password" id="p"></td>'
            '<td><input id="q" name="q"><button id="cart">Cart</button></td></tr></table>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"password": "#p", "q": "#q"}
        assert form.submit is None


class TestDialogFieldBlockAndForeignContentRound4:
    """Review round 4 of #267."""

    def test_the_field_block_ends_at_a_typeless_sign_in(self) -> None:
        """A typeless "Sign in" is no submit, but the fields end there: a hidden
        reset panel's input after it is no login field."""
        html = _in_dialog(
            '<input type="email" id="u" name="email"><input type="password" id="p" '
            'name="password"><button id="signin">Sign in</button>'
            '<div class="reset" hidden><input type="email" id="r" name="reset_email">'
            '<button id="send">Send link</button></div>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": "#u", "password": "#p"}
        assert form.submit is None
        assert "submit" in form.unresolved_roles

    @pytest.mark.parametrize(
        "control",
        [
            '<button type="button" id="b">Sign in</button>',
            '<input type="button" id="b" value="Sign in">',
            '<input type="reset" id="b">',
        ],
        ids=["button-type-button", "input-button", "input-reset"],
    )
    def test_the_field_block_ends_at_any_button_like_control(self, control: str) -> None:
        html = _in_dialog(
            f'<input id="u"><input type="password" id="p">{control}'
            '<input type="text" id="promo" name="promo">'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": "#u", "password": "#p"}

    def test_a_field_before_the_first_button_stays_a_field(self) -> None:
        html = _in_dialog(
            '<input id="u"><input type="password" id="p"><input type="text" id="otp" '
            'name="otp"><button id="go">Sign in</button>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": "#u", "password": "#p", "otp": "#otp"}

    _SELF_CLOSING_DIALOG = (
        '<div role="dialog"/><input id="u"><input type="password" id="p">'
        '<input type="submit" id="go"></div>'
    )

    @pytest.mark.parametrize(
        "before",
        [
            "<svg/>",
            "<math/>",
            '<svg><path d="x"/></svg>',
            "<math><mi/></math>",
            "<noscript/>",
        ],
        ids=["svg-self-closing", "math-self-closing", "svg-closed", "math-closed", "noscript"],
    )
    def test_foreign_content_and_noscript_end_where_a_browser_ends_them(self, before: str) -> None:
        (form,) = extract_login_forms(before + self._SELF_CLOSING_DIALOG, source="s")
        assert form.formless
        assert form.submit == "#go"

    def test_a_self_closing_element_in_math_closes_at_once(self) -> None:
        html = (
            '<math><mrow role="dialog"/></math><input id="u"><input type="password" id="p">'
            '<input type="submit" id="go">'
        )
        assert extract_login_forms(html, source="s") == ()


class TestDialogSubmitBoundedByTheFieldBlockRound5:
    """Review round 5 of #267: the submit search stops where the fields end."""

    @pytest.mark.parametrize(
        "sign_in",
        ['<button id="signin">Sign in</button>', '<button type="button" id="signin">In</button>'],
        ids=["typeless", "type-button"],
    )
    def test_a_hidden_panel_s_explicit_submit_is_never_taken(self, sign_in: str) -> None:
        """Review round 5 (toolkit I1, B1 and B2)."""
        html = _in_dialog(
            f'<input type="email" id="u" name="email"><input type="password" id="p" '
            f'name="password">{sign_in}<div class="reset" hidden><input type="email" '
            'id="r" name="reset_email"><button type="submit" id="send">Send link</button>'
            "</div>"
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": "#u", "password": "#p"}
        assert form.submit is None
        assert "submit" in form.unresolved_roles

    def test_a_toggle_after_the_password_ends_the_fields_and_the_submit_search(self) -> None:
        """Review round 5 (toolkit M1, F1): the intended trade, a GP-FILL over a
        wrong selector. The OTP input after the toggle is dropped, and the submit
        after it is a GP-FILL."""
        html = _in_dialog(
            '<input type="email" id="u" name="email"><input type="password" id="p" '
            'name="password"><button type="button" id="eye">Show</button>'
            '<input id="otp" name="otp"><button type="submit" id="go">Sign in</button>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": "#u", "password": "#p"}
        assert form.submit is None
        assert "submit" in form.unresolved_roles

    @pytest.mark.parametrize(
        "submit",
        [
            '<input type="submit" id="go">',
            '<input type="image" id="go" src="x.png">',
            '<button type="submit" id="go">Go</button>',
        ],
        ids=["input-submit", "input-image", "button-submit"],
    )
    def test_an_explicit_submit_ends_the_fields_and_is_the_submit(self, submit: str) -> None:
        """Review round 5 (toolkit M2, superpowers M10)."""
        html = _in_dialog(
            f'<input id="u"><input type="password" id="p">{submit}'
            '<input type="text" id="promo" name="promo">'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": "#u", "password": "#p"}
        assert form.submit == "#go"

    def test_a_real_form_keeps_its_own_field_block_rule(self) -> None:
        """Review round 5 (toolkit M3, superpowers M11): as on main, a form's fields
        run to its submit, past a show-password button."""
        html = (
            '<form action="/login"><input name="u" id="u"><input type="password" '
            'name="p" id="p"><button type="button" id="eye">Show</button>'
            '<input name="otp" id="otp"><button type="submit" id="go">Go</button></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": "#u", "password": "#p", "otp": "#otp"}
        assert form.submit == "#go"

    def test_a_button_before_the_password_does_not_end_the_fields(self) -> None:
        """Review round 5 (toolkit M4, F2): the rule starts at the password."""
        html = _in_dialog(
            '<input id="u" name="email"><button type="button" id="next">Next</button>'
            '<input type="password" id="p"><input id="otp" name="otp">'
            '<button type="submit" id="go">Sign in</button>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": "#u", "password": "#p", "otp": "#otp"}
        assert form.submit == "#go"


def test_a_hidden_panel_s_username_input_is_never_the_username() -> None:
    """Review round 6 (toolkit M1, B): the autocomplete="username" lookup stays
    inside the dialog's field block."""
    html = _in_dialog(
        '<input type="email" id="u" name="email"><input type="password" id="p" '
        'name="password"><button id="signin">Sign in</button><div class="reset" hidden>'
        '<input type="email" id="r" name="reset_email" autocomplete="username">'
        '<button type="submit" id="send">Send link</button></div>'
    )
    (form,) = extract_login_forms(html, source="s")
    assert form.fields == {"username": "#u", "password": "#p"}


def test_an_autocomplete_username_before_the_password_still_wins_in_a_dialog() -> None:
    html = _in_dialog(
        '<input id="a" name="a"><input id="u" name="login" autocomplete="username">'
        '<input id="b" name="b"><input type="password" id="p">'
        '<input type="submit" id="go">'
    )
    (form,) = extract_login_forms(html, source="s")
    assert form.fields["username"] == "#u"


def test_a_form_s_autocomplete_username_is_found_anywhere_in_it_as_before() -> None:
    """A real form keeps main's rule: the lookup is not bounded by its submit."""
    html = (
        '<form action="/login"><input type="password" id="p"><button id="go">Go</button>'
        '<input id="u" name="login" autocomplete="username"></form>'
    )
    (form,) = extract_login_forms(html, source="s")
    assert form.fields["username"] == "#u"


def test_the_literal_name_username_fallback_stops_at_a_dialog_s_first_button() -> None:
    """Review round 7: after a show-password toggle, an input named username is no
    longer in the dialog's field block, so the username is a GP-FILL; in a real form
    the same controls give it, as on main."""
    controls = (
        '<input type="password" id="p"><button type="button" id="eye">Show</button>'
        '<input id="u" name="username"><input type="submit" id="go">'
    )
    (dialog,) = extract_login_forms(_in_dialog(controls), source="s")
    assert "username" not in dialog.fields
    assert dialog.absent_roles[0] == "username"
    (form,) = extract_login_forms(f'<form action="/login">{controls}</form>', source="s")
    assert form.fields["username"] == "#u"


class TestARealFormInsideADialog:
    """``LoginForm.in_dialog`` (#267, part 3): a real form's password input inside a
    dialog-marked element, whatever its other controls sit in."""

    def test_the_password_inside_a_dialog_marks_the_form(self) -> None:
        html = (
            '<div role="dialog"><form><input name="email">'
            '<input type="password" name="password"></form></div>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.in_dialog

    def test_another_control_inside_a_dialog_does_not(self) -> None:
        html = (
            '<form><div role="dialog"><input name="email"></div>'
            '<input type="password" name="password"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert not form.in_dialog

    def test_a_dialog_s_second_role_token_does_not(self) -> None:
        html = (
            '<div role="presentation dialog"><form><input name="email">'
            '<input type="password" name="password"></form></div>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert not form.in_dialog
