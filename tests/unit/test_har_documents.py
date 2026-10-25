"""HTML extraction on inline HTML, no HAR involved (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

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


class TestLoginFieldsOutsideAnyForm:
    """A password input outside every ``<form>`` (a script-driven login modal whose
    inputs sit in a ``<div>``) yields a login form built from the formless controls
    of the element around it (#267)."""

    _MODAL = (
        '<header><a href="#" id="signin-link">Sign in</a></header>'
        '<div class="modal" id="login-modal" hidden><div class="login-form">'
        '<input type="text" id="login-username" autocomplete="username">'
        '<input type="password" id="login-password" autocomplete="current-password">'
        '<button type="submit" class="btn">Sign in</button></div></div>'
    )

    def test_roles_and_id_selectors(self) -> None:
        (form,) = extract_login_forms(self._MODAL, source="https://myshop.example/")
        assert form.fields == {"username": "#login-username", "password": "#login-password"}
        assert form.action == ""
        assert form.method == ""
        assert form.formless
        # The submit button has neither an id nor a name, and with no form scope
        # nothing else picks it.
        assert form.submit is None
        assert form.unresolved_roles == ("submit",)
        # No target of its own: a script sends it, so the digest never matches a
        # POST to it by where it posts.
        assert form.action_target == ("", "")

    def test_unique_names_are_selected_by_name_and_a_shared_one_is_unresolved(self) -> None:
        html = (
            '<div><input type="email" name="email">'
            '<input type="password" name="password">'
            '<button type="button" name="go">Sign in</button></div>'
            '<div><input type="email" name="email" placeholder="Newsletter"></div>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"password": 'input[name="password"]'}
        assert "username" in form.unresolved_roles
        assert form.absent_roles == ()
        # A type="button" button inside the element around the password is the
        # submit: a script-driven modal must not submit a form.
        assert form.submit == 'button[name="go"]'
        fields, _submit = printable_selectors(form)
        assert fields == {"password": 'input[name="password"]'}

    def test_a_submit_with_no_id_or_unique_name_is_unresolved(self) -> None:
        html = (
            '<div><input name="user" id="u"><input type="password" name="pw">'
            "<button>Sign in</button>"
            '<button name="go" id="go-btn">Go</button></div>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": "#u", "password": 'input[name="pw"]'}
        assert form.submit is None
        assert "submit" in form.unresolved_roles

    def test_a_submitting_button_wins_over_a_type_button_before_it(self) -> None:
        """A modal's show-password or close button often comes first."""
        html = (
            '<div><input id="u"><input type="password" id="p">'
            '<button type="button" id="close">Close</button>'
            '<button type="submit" id="go">Sign in</button></div>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.submit == "#go"

    def test_a_selector_never_carries_a_form_scope(self) -> None:
        html = (
            '<div><input name="username"><input type="password" name="password">'
            '<button name="signin">Sign in</button></div>'
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
            '<input type="password" name="password" id="pw"><button id="go">Go</button></form>'
            f"{self._MODAL}"
        )
        real, formless = extract_login_forms(html, source="s")
        assert real.action == "/login"
        assert real.method == "POST"
        assert not real.formless
        assert real.fields == {"username": "#email", "password": "#pw"}
        assert formless.formless
        assert formless.fields == {
            "username": "#login-username",
            "password": "#login-password",
        }
        assert real.signature != formless.signature

    def test_a_real_form_s_controls_are_never_part_of_the_formless_group(self) -> None:
        """A formless password with no username before it does not borrow a real
        form's text input."""
        html = (
            '<form action="/search"><input type="text" name="q" id="q"></form>'
            '<div><input type="password" id="pin"><button id="unlock">Unlock</button></div>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"password": "#pin"}
        assert form.absent_roles == ("username",)
        assert form.submit == "#unlock"

    def test_a_control_owned_by_a_form_through_its_form_attribute_is_not_formless(self) -> None:
        html = (
            '<form id="signin" action="/login"><input name="username" id="u"></form>'
            '<input type="password" name="password" id="p" form="signin">'
        )
        (form,) = extract_login_forms(html, source="s")
        assert not form.formless
        assert form.action == "/login"

    def test_a_form_attribute_naming_no_form_leaves_the_control_formless(self) -> None:
        html = (
            '<div><input name="username" id="u" form="gone">'
            '<input type="password" name="password" id="p" form="gone"></div>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.formless
        assert form.fields == {"username": "#u", "password": "#p"}

    def test_inside_a_form_a_form_attribute_naming_no_form_leaves_the_control_formless(
        self,
    ) -> None:
        """A ``form`` attribute naming no form gives the control no form owner, even
        inside a ``<form>``, so it belongs to no form's controls; it joins a
        formless group instead of being lost."""
        html = (
            '<form action="/x"><input name="u" id="u" form="gone">'
            '<input type="password" id="p" form="gone"></form>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.formless
        assert form.fields == {"username": "#u", "password": "#p"}

    def test_a_formless_password_with_no_submit_control(self) -> None:
        """No submit control beside the password: the role is absent and listed as
        unresolved, so the generated step carries a GP-FILL for it."""
        html = '<div><input type="email" id="e"><input type="password" id="p"></div>'
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": "#e", "password": "#p"}
        assert form.submit is None
        assert form.absent_roles == ("submit",)
        assert form.unresolved_roles == ("submit",)

    @pytest.mark.parametrize(
        "html",
        [
            '<div><input type="email" id="e">'
            '<input type="password" id="p" autocomplete="new-password"></div>',
            '<div><input type="email" id="e"><input type="password" id="p" name="password">'
            '<input type="password" id="c" name="password_confirm"></div>',
            '<div><input type="password" id="cur" autocomplete="current-password">'
            '<input type="password" id="new" name="new_password"></div>',
        ],
        ids=["new-password", "confirmation", "change-password"],
    )
    def test_a_sign_up_or_password_change_group_yields_nothing(self, html: str) -> None:
        assert extract_login_forms(html, source="s") == ()

    def test_a_lone_new_password_form_keeps_its_old_result_beside_a_formless_login(
        self,
    ) -> None:
        """A real form's result never depends on the formless controls: a lone form
        marked new-password is kept as before, and the formless login is listed
        after it."""
        real_form = (
            '<form action="/register"><input type="email" name="email" id="reg-email">'
            '<input type="password" name="password" id="reg-pw" autocomplete="new-password">'
            "</form>"
        )
        (alone,) = extract_login_forms(real_form, source="s")
        real, formless = extract_login_forms(real_form + self._MODAL, source="s")
        assert real == alone
        assert formless.formless

    def test_a_misused_new_password_login_form_survives_an_unrelated_formless_pin(
        self,
    ) -> None:
        real_form = (
            '<form action="/login" method="post"><input name="user" id="u">'
            '<input type="password" id="p" name="password" autocomplete="new-password">'
            '<button id="b">Go</button></form>'
        )
        (alone,) = extract_login_forms(real_form, source="s")
        forms = extract_login_forms(
            real_form + '<div><input type="password" id="giftpin"></div>', source="s"
        )
        assert forms[0] == alone
        assert all(form.formless for form in forms[1:])

    def test_controls_inside_noscript_are_left_out(self) -> None:
        """With script on, a ``<noscript>`` body is never in the page, and a formless
        group is sent only by script."""
        only_noscript = (
            '<noscript><input type="text" id="u"><input type="password" id="p"></noscript>'
        )
        assert extract_login_forms(only_noscript, source="s") == ()
        mixed = (
            '<div><input type="text" id="u"><input type="password" id="p">'
            '<noscript><button id="fallback">Sign in</button></noscript>'
            '<button id="go">Sign in</button></div>'
        )
        (form,) = extract_login_forms(mixed, source="s")
        assert form.submit == "#go"

    def test_hidden_inputs_outside_a_form_are_token_candidates_of_the_group(self) -> None:
        html = (
            '<input type="hidden" name="page_token" value="y">'
            '<div><input type="hidden" name="csrf_token" value="x">'
            '<input id="u"><input type="password" id="p"></div>'
        )
        (form,) = extract_login_forms(html, source="s")
        # Only the hidden inputs of the element around the password.
        assert form.hidden == ("csrf_token",)

    def test_the_same_group_on_two_pages_has_one_signature(self) -> None:
        (one,) = extract_login_forms(self._MODAL, source="https://myshop.example/")
        (two,) = extract_login_forms(self._MODAL, source="https://myshop.example/cart")
        assert one.signature == two.signature

    def test_the_signature_ignores_formless_controls_elsewhere_on_the_page(self) -> None:
        (bare,) = extract_login_forms(self._MODAL, source="s")
        elsewhere = (
            '<header><input type="search" name="q" id="q"></header>'
            f"{self._MODAL}"
            '<aside><input type="checkbox" name="color" id="c-red">'
            '<input type="hidden" name="filter_token" value="x"></aside>'
        )
        (busy,) = extract_login_forms(elsewhere, source="s")
        assert busy.signature == bare.signature
        assert busy.input_names == bare.input_names
        assert busy.hidden == bare.hidden

    def test_a_formless_signature_never_equals_a_form_s(self) -> None:
        """A ``<form>`` with no id holding the same controls is a different form."""
        controls = '<input id="u"><input type="password" id="p">'
        (formless,) = extract_login_forms(f"<div>{controls}</div>", source="s")
        (real,) = extract_login_forms(f"<form>{controls}</form>", source="s")
        assert formless.signature != real.signature

    def test_a_page_with_no_formless_password_is_unchanged(self) -> None:
        html = f'{_LOGIN_PAGE}<div><input type="text" name="q" id="search"></div>'
        (form,) = extract_login_forms(html, source="s")
        assert not form.formless
        assert extract_login_forms(_LOGIN_PAGE, source="s") == (form,)


_MODAL_BUTTON = (
    '<div id="m"><input type="text" id="lu" name="account">'
    '<input type="password" id="lp"><button type="button" id="go">Sign in</button></div>'
)


class TestAFormlessGroupStaysInsideTheElementAroundThePassword:
    """The role rules never reach controls elsewhere on the page (#267 review)."""

    @pytest.mark.parametrize(
        "after",
        [
            '<main><button id="add-to-cart">Add to cart</button></main>',
            '<footer><input type="email" id="nl" name="newsletter">'
            '<button id="sub">Subscribe</button></footer>',
            '<div class="cookies"><input type="email" name="news" id="news">'
            '<button id="accept">Accept</button></div>',
        ],
        ids=["cart", "newsletter", "cookie-banner"],
    )
    def test_a_later_button_elsewhere_is_never_the_submit(self, after: str) -> None:
        (form,) = extract_login_forms(_MODAL_BUTTON + after, source="s")
        assert form.fields == {"username": "#lu", "password": "#lp"}
        assert form.submit == "#go"

    def test_a_modal_with_no_submit_control_takes_no_later_button(self) -> None:
        html = (
            '<div><input id="u" type="email"><input type="password" id="p">'
            '<a href="#" id="go">Sign in</a></div>'
            '<main><button id="add-to-cart">Add to cart</button></main>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.submit is None
        assert "submit" in form.unresolved_roles

    def test_inputs_after_a_submit_less_modal_are_no_fields(self) -> None:
        html = (
            '<div><input id="u" type="email"><input type="password" id="p">'
            '<a href="#" id="go">Sign in</a></div>'
            '<main><input type="number" name="qty" id="qty1">'
            '<input type="text" name="coupon" id="coupon"></main>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": "#u", "password": "#p"}

    def test_a_newsletter_email_before_the_modal_is_not_the_username(self) -> None:
        html = (
            '<header><input type="email" id="nl" name="newsletter_email" '
            'autocomplete="email"></header>'
            '<div><input type="text" id="lu"><input type="password" id="lp"></div>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": "#lu", "password": "#lp"}
        assert form.neutral_roles == ()

    def test_a_search_box_before_a_password_only_modal_is_not_the_username(self) -> None:
        html = (
            '<header><input type="text" id="q" name="q"></header>'
            '<div><input type="password" id="pin"><button id="go">Go</button></div>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"password": "#pin"}
        assert form.absent_roles == ("username",)

    def test_a_password_with_nothing_around_it_is_alone_and_its_roles_unresolved(
        self,
    ) -> None:
        """Never the whole ``<body>``: with no smaller element holding a text input
        before the password or a button, the group is the password alone."""
        html = (
            '<html><body><header><input type="text" id="q" name="q"></header>'
            '<div class="pw"><input type="password" id="p"></div>'
            '<main><button id="add-to-cart">Add to cart</button></main></body></html>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"password": "#p"}
        assert form.submit is None
        assert form.absent_roles == ("username", "submit")
        assert form.unresolved_roles == ("username", "submit")
        assert form.input_names == ()

    def test_a_sign_up_modal_beside_a_login_modal_marked_new_password(self) -> None:
        html = (
            '<div id="login"><input type="email" id="le"><input type="password" id="lp">'
            '<button id="lgo">Sign in</button></div>'
            '<div id="signup"><input type="email" id="se">'
            '<input type="password" id="sp" autocomplete="new-password">'
            '<button id="sgo">Create</button></div>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": "#le", "password": "#lp"}
        assert form.submit == "#lgo"

    def test_a_sign_up_modal_before_the_login_modal_is_listed_after_it(self) -> None:
        """Neither modal is marked, so both are login-shaped; the one whose element
        or controls are named for signing up is listed last."""
        html = (
            '<div id="signup"><input type="email" id="se" name="email">'
            '<input type="password" id="sp" name="password"><button id="sgo">Create</button>'
            "</div>"
            '<div id="login"><input type="email" id="le" name="email">'
            '<input type="password" id="lp" name="password"><button id="lgo">Sign in</button>'
            "</div>"
        )
        first, second = extract_login_forms(html, source="s")
        assert first.fields == {"username": "#le", "password": "#lp"}
        assert second.fields == {"username": "#se", "password": "#sp"}

    def test_a_current_password_group_is_listed_first(self) -> None:
        html = (
            '<div><input id="a"><input type="password" id="ap"><button id="ago">Go</button></div>'
            '<div><input id="b"><input type="password" id="bp" autocomplete="current-password">'
            '<button id="bgo">Go</button></div>'
        )
        first, second = extract_login_forms(html, source="s")
        assert first.fields["password"] == "#bp"  # noqa: S105
        assert second.fields["password"] == "#ap"  # noqa: S105

    def test_two_copies_of_a_modal_are_two_groups(self) -> None:
        html = (
            '<div><input id="u1" name="username"><input type="password" id="p1" name="password">'
            '<button id="g1">Go</button></div>'
            '<div><input id="u2" name="username"><input type="password" id="p2" name="password">'
            '<button id="g2">Go</button></div>'
        )
        one, two = extract_login_forms(html, source="s")
        assert (one.fields, one.submit) == ({"username": "#u1", "password": "#p1"}, "#g1")
        assert (two.fields, two.submit) == ({"username": "#u2", "password": "#p2"}, "#g2")

    def test_a_modal_among_a_busy_page_s_formless_controls(self) -> None:
        html = (
            "<html><body>"
            '<header><input type="search" name="q" id="q"><button id="search-go">Go</button>'
            '<a href="#" id="signin-link">Sign in</a></header>'
            '<main><div class="product"><input type="number" name="qty" id="qty">'
            '<button id="add-to-cart">Add to cart</button></div></main>'
            '<div class="modal" id="login-modal" hidden><div class="modal-body">'
            '<input type="email" id="login-email" name="email">'
            '<input type="password" id="login-password" name="password">'
            '<button type="button" id="login-go">Sign in</button></div></div>'
            '<footer><input type="email" name="newsletter" id="nl">'
            '<button id="subscribe">Subscribe</button></footer>'
            '<div class="cookies"><button id="accept">Accept</button></div>'
            "</body></html>"
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": "#login-email", "password": "#login-password"}
        assert form.submit == "#login-go"
        assert form.input_names == ("email", "password")
        _assert_first_match(html, form.submit, id_="login-go")

    def test_an_unclosed_element_and_a_stray_end_tag_do_not_break_the_tree(self) -> None:
        html = (
            "</span><div><p>Sign in<input id=u><br><img src=x>"
            '<input type="password" id="p"><button id="go">Go</button></div>'
            '<main><button id="cart">Cart</button>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": "#u", "password": "#p"}
        assert form.submit == "#go"


class TestAFormlessGroupClimbsToItsDialog:
    """When the element around the password holds no submit control, the group
    widens to the nearest enclosing dialog, and never further (#267 review)."""

    @pytest.mark.parametrize(
        "opening",
        [
            '<div class="modal" role="dialog">',
            '<div class="modal" role="alertdialog">',
            '<div class="modal" aria-modal="true">',
            "<dialog>",
        ],
        ids=["role-dialog", "role-alertdialog", "aria-modal", "dialog-element"],
    )
    def test_a_submit_in_the_dialog_footer_is_found(self, opening: str) -> None:
        closing = "</dialog>" if opening == "<dialog>" else "</div>"
        html = (
            '<header><input type="search" id="q" name="q"></header>'
            f'{opening}<div class="modal-dialog"><div class="modal-content">'
            '<div class="modal-body"><input type="email" id="login-email">'
            '<input type="password" id="login-password"></div>'
            '<div class="modal-footer"><button type="button" id="close">Close</button>'
            '<button type="submit" id="login-go">Sign in</button></div>'
            f"</div></div>{closing}"
            '<main><button id="add-to-cart">Add to cart</button></main>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": "#login-email", "password": "#login-password"}
        assert form.submit == "#login-go"

    def test_a_dialog_with_only_a_sign_in_link_takes_no_button_outside_it(self) -> None:
        html = (
            '<div class="modal" role="dialog"><div class="modal-body">'
            '<input type="email" id="login-email"><input type="password" id="login-password">'
            '</div><div class="modal-footer"><a href="#" id="login-go">Sign in</a></div></div>'
            '<main><button id="add-to-cart">Add to cart</button></main>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.submit is None
        assert "submit" in form.unresolved_roles

    def test_a_dialog_holding_another_password_group_is_not_climbed_to(self) -> None:
        html = (
            '<div role="dialog">'
            '<div class="login"><input type="email" id="le"><input type="password" id="lp">'
            "</div>"
            '<div class="signup"><input type="email" id="se">'
            '<input type="password" id="sp" autocomplete="new-password"></div>'
            '<div class="footer"><button id="go">Continue</button></div></div>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.fields == {"username": "#le", "password": "#lp"}
        assert form.submit is None

    def test_no_dialog_ancestor_means_no_climb(self) -> None:
        """A page wrapper is not a dialog: the cart button stays out."""
        html = (
            '<div id="app"><div class="login-body"><input type="email" id="le">'
            '<input type="password" id="lp"></div>'
            '<div class="actions"><button id="add-to-cart">Add to cart</button></div></div>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.submit is None

    def test_a_dialog_that_is_itself_the_container_is_not_climbed_past(self) -> None:
        html = (
            '<div class="page"><div role="dialog"><input type="email" id="le">'
            '<input type="password" id="lp"><a href="#" id="go">Sign in</a></div>'
            '<button id="add-to-cart">Add to cart</button></div>'
        )
        (form,) = extract_login_forms(html, source="s")
        assert form.submit is None
