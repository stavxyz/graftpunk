"""Suggested ``success`` selectors read from a recording (#268): an element on the
run's final logged-in page and on none of its logged-out pages. Each emitted
selector is checked with lxml and cssselect against both sides where they can tell,
so the stdlib matcher the module uses at run time cannot drift from what a CSS
engine selects. lxml matches ids and classes with case in every document, so the
cases where the module counts them without case are asserted on the module
alone."""

from __future__ import annotations

import time

import lxml.html
import pytest

from graftpunk.har.login_signals import _GENERATED_RULES, _words, success_candidates

_DOCTYPE = "<!DOCTYPE html>"
_LOGIN = (
    f"{_DOCTYPE}<html><body><header><nav class='site-nav'>"
    "<a href='/login' class='nav-link'>Sign in</a></nav></header><main>"
    "<form action='/session' method='post'>"
    "<input type='email' name='email'><input type='password' name='password'>"
    "<button class='btn'>Sign in</button></form><p class='intro'>Welcome</p></main>"
    "</body></html>"
)


def _page(header: str, main: str = "<p class='intro'>Welcome</p>") -> str:
    return (
        f"{_DOCTYPE}<html><body><header><nav class='site-nav'>"
        f"{header}</nav></header><main>{main}</main></body></html>"
    )


def _matches(selector: str, html: str) -> int:
    return len(lxml.html.fromstring(html).cssselect(selector))


def _checked(before: list[str], after: str, **kwargs: object) -> tuple[str, ...]:
    """``success_candidates``, with every selector it emits checked by cssselect: on
    exactly one element of the logged-in page and on none of any logged-out page."""
    found = success_candidates(before, after, **kwargs)  # type: ignore[arg-type]
    assert len(found) <= 3
    for selector in found:
        assert _matches(selector, after) == 1, selector
        for page in before:
            assert _matches(selector, page) == 0, selector
    return found


class TestSuccessCandidates:
    def test_an_id_on_the_logged_in_page_only_is_suggested(self) -> None:
        after = _page("<div id='account-menu'><a href='/orders'>Orders</a></div>")
        assert _checked([_LOGIN], after) == ("#account-menu",)

    def test_a_class_token_is_suggested_with_its_tag(self) -> None:
        after = _page("<span class='badge user-chip'>A</span>")
        assert _checked([_LOGIN], after) == ("span.user-chip", "span.badge")

    def test_keyword_first_then_id_then_class_in_document_order(self) -> None:
        after = _page(
            "<div id='cart-count'>2</div><span class='header-tools'></span>"
            "<a class='signout-link' href='/bye'>Sign out</a><div id='main-panel'></div>"
        )
        assert _checked([_LOGIN], after) == ("a.signout-link", "#cart-count", "#main-panel")

    @pytest.mark.parametrize(
        "href", ["/logout", "/account/log-out", "/signout?next=/", "/sign-out/"]
    )
    def test_a_link_to_a_logout_path_segment_ranks_first(self, href: str) -> None:
        after = _page(f"<div id='main-panel'></div><a class='nav-exit' href='{href}'>Bye</a>")
        assert _checked([_LOGIN], after)[0] == "a.nav-exit"

    @pytest.mark.parametrize("href", ["/catalog-outlet", "/blog-outdoors", "/designout"])
    def test_a_logout_substring_in_another_word_is_no_boost(self, href: str) -> None:
        after = _page(f"<div id='main-panel'></div><a class='promo' href='{href}'>Sale</a>")
        assert _checked([_LOGIN], after) == ("#main-panel", "a.promo")

    def test_an_element_also_on_a_logged_out_page_is_not_suggested(self) -> None:
        """A hidden account menu the logged-out page carries too would pass on the
        first tick of a failed login."""
        login = _LOGIN.replace("<main>", "<main><div class='account-menu' hidden></div>")
        after = _page("<div class='account-menu'></div><div id='welcome'></div>")
        assert _checked([login], after) == ("#welcome",)

    @pytest.mark.parametrize(
        ("rule", "name"),
        [
            ("not_identifier", "1st"),
            ("css_module", "Header_loggedIn__abcde"),
            ("css_module", "Header-module__loggedIn__abcde"),
            ("triple_underscore", "user___x"),
            ("underscore_hash", "_loggedIn_abcde"),
            ("css_in_js", "sc-bdVaJa"),
            ("digit_run", "panel-1234"),
            ("mixed_part", "nav-x7q2a"),
            ("account_value", "a-12-12-12-1"),
        ],
    )
    def test_each_generated_name_rule_alone(self, rule: str, name: str) -> None:
        """Each name is caught by its rule and by no other, so every rule is needed."""
        assert [label for label, check in _GENERATED_RULES if check(name)] == [rule]
        for markup in (f"<div id='{name}'></div>", f"<div class='{name}'></div>"):
            assert success_candidates([_LOGIN], _page(markup)) == ()

    @pytest.mark.parametrize(
        "marker",
        [
            "<div class='Header_loggedIn__x7Q2a'></div>",
            "<div class='Header-module__loggedIn___abcDE'></div>",
            "<div class='header__loggedIn___QwErT'></div>",
            "<div class='css-abcdef'></div>",
            "<div id='ember1234'></div>",
            "<div id='user_40912873'></div>",
            "<div id='alice@example.com'></div>",
            "<div class='a3f9c2d1e0b4'></div>",
            "<div id='odd:id'></div>",
            "<div id='" + "a" * 300 + "'></div>",
        ],
    )
    def test_a_generated_name_or_an_account_value_is_not_suggested(self, marker: str) -> None:
        assert _checked([_LOGIN], _page(marker)) == ()

    @pytest.mark.parametrize(
        "marker",
        [
            "<a class='login-link' href='/login'>Log in</a>",
            "<button class='log-in-button'>Log in</button>",
            "<div class='loginPanel'></div>",
            "<button id='signin-button'>Sign in</button>",
            "<a class='sign-in' href='/x'>Sign in</a>",
            "<div class='guest-banner'></div>",
            "<div id='signout-message'>You have signed out</div>",
            "<p class='logout-note'>Signed out</p>",
            "<div class='logged-out'></div>",
            "<div class='signed_out'></div>",
            "<div class='signup-cta'></div>",
            "<div class='anonymous-user'></div>",
        ],
    )
    def test_a_name_saying_signed_out_or_guest_is_not_suggested(self, marker: str) -> None:
        assert _checked([_LOGIN], _page(marker)) == ()

    def test_a_sign_out_control_is_suggested(self) -> None:
        after = _page("<button class='logout'>Sign out</button><a id='sign-out-link'>x</a>")
        assert _checked([_LOGIN], after) == ("#sign-out-link", "button.logout")

    def test_a_name_holding_a_credential_value_is_not_suggested(self) -> None:
        after = _page(
            "<div id='user-jsmith'></div><img class='avatar-jdoe' src='/a.png'>"
            "<span class='greeting-JSmith'></span><div id='account-menu'></div>"
        )
        values = ("jsmith@example.com", "jdoe")
        assert _checked([_LOGIN], after, account_values=values) == ("#account-menu",)
        # Without the values each of them would have been suggested.
        assert set(_checked([_LOGIN], after)) >= {"#user-jsmith", "img.avatar-jdoe"}

    @pytest.mark.parametrize(
        "value", ["true", "false", "yes", "off", "null", "none", "on", "1", "ab", ""]
    )
    def test_a_short_or_boolean_credential_value_rejects_nothing(self, value: str) -> None:
        after = _page(f"<div id='account-menu'></div><div id='badge-{value}-row'></div>")
        assert _checked([_LOGIN], after, account_values=(value, value.upper())) == (
            "#account-menu",
            f"#badge-{value}-row",
        )

    def test_a_credential_value_matches_without_case_dots_or_dashes(self) -> None:
        after = _page("<div id='user-jsmith'></div><div id='account-menu'></div>")
        values = ("j.smith@example.com",)
        assert _checked([_LOGIN], after, account_values=values) == ("#account-menu",)
        after = _page("<div id='user-j-smith'></div><div id='account-menu'></div>")
        assert _checked([_LOGIN], after, account_values=("J-Smith",)) == ("#account-menu",)

    def test_a_name_is_also_matched_without_its_dots_and_dashes(self) -> None:
        after = _page("<div id='user-j_smith'></div><div id='account-menu'></div>")
        assert _checked([_LOGIN], after, account_values=("jsmith",)) == ("#account-menu",)

    def test_a_selector_matching_twice_is_not_suggested(self) -> None:
        after = _page("<span class='user-chip'></span><span class='user-chip'></span>")
        assert _checked([_LOGIN], after) == ()

    def test_an_id_on_two_elements_is_not_suggested(self) -> None:
        after = _page("<div id='account'></div><p id='account'></p>")
        assert _checked([_LOGIN], after) == ()

    def test_the_snapshot_at_the_post_counts_as_a_login_copy(self) -> None:
        snapshot = (
            f"{_DOCTYPE}<html><head></head><body><header><div id='account-hint'></div>"
            "</header><div class='modal'><input type='password' name='password'></div>"
            "</body></html>"
        )
        after = _page("<div id='account-hint'></div><div id='account-menu'></div>")
        assert _checked([_LOGIN, snapshot], after) == ("#account-menu",)

    def test_at_most_three(self) -> None:
        after = _page("".join(f"<div id='panel-{name}'></div>" for name in "abcdef"))
        assert _checked([_LOGIN], after) == ("#panel-a", "#panel-b", "#panel-c")

    def test_no_logged_in_page_gives_nothing(self) -> None:
        assert success_candidates([_LOGIN], "") == ()

    def test_head_and_template_elements_are_not_suggested(self) -> None:
        after = (
            f"{_DOCTYPE}<html><head><meta id='user-meta'><script id='user-data'></script>"
            "</head><body><template><div id='user-tpl'></div></template>"
            "<noscript><div id='user-nojs'></div></noscript></body></html>"
        )
        assert _checked([_LOGIN], after) == ()

    def test_a_self_closing_template_still_opens_one(self) -> None:
        """An HTML parser ignores the self-closing flag on a template, so what follows
        is the template's content, which no selector matches in a browser."""
        after = _page("<template/><div id='user-panel'></div>")
        assert success_candidates([_LOGIN], after) == ()
        closed = _page("<template></template><div id='user-panel'></div>")
        assert _checked([_LOGIN], closed) == ("#user-panel",)

    def test_a_logged_out_page_is_matched_without_case(self) -> None:
        """A quirks-mode browser matches ids and classes without case, so a name on a
        logged-out page in any case rejects the selector, in any document."""
        login = _LOGIN.replace("<main>", "<main><div id='USERMENU'></div>")
        assert success_candidates([login], _page("<div id='userMenu'></div>")) == ()
        login = _LOGIN.replace("<main>", "<main><div class='UserChip'></div>")
        assert success_candidates([login], _page("<div class='userchip'></div>")) == ()

    def test_the_logged_in_page_counts_without_case(self) -> None:
        after = _page("<div class='userchip'></div><div class='UserChip'></div>")
        assert success_candidates([_LOGIN], after) == ()
        after = _page("<div id='userMenu'></div><p id='USERMENU'></p>")
        assert success_candidates([_LOGIN], after) == ()

    def test_a_selector_keeps_its_element_s_case(self) -> None:
        after = _page("<div id='userMenu'></div><span class='ProfileChip'></span>")
        assert _checked([_LOGIN], after) == ("#userMenu", "span.ProfileChip")

    def test_the_first_of_duplicated_attributes_wins(self) -> None:
        after = _page("<div id='account-menu' id='other'></div><div class='user-a' class='user-b'>")
        assert _checked([_LOGIN], after) == ("#account-menu", "div.user-a")
        login = _LOGIN.replace("<main>", "<main><div id='account' id='x'></div>")
        assert _checked([login], _page("<div id='account'></div>")) == ()

    def test_class_tokens_split_on_ascii_whitespace_only(self) -> None:
        after = _page("<div class='user-chip\xa0big'></div><div class='a\tuser-tab'></div>")
        assert _checked([_LOGIN], after) == ("div.user-tab", "div.a")

    def test_a_tag_that_is_not_plain_gives_no_tag_form(self) -> None:
        after = _page("<fb:like class='user-like'></fb:like><fb:share id='user-share'></fb:share>")
        assert _checked([_LOGIN], after) == ("#user-share",)

    def test_svg_and_mathml_elements_give_only_an_id(self) -> None:
        after = _page(
            "<svg id='user-avatar' class='user-icon'><path class='user-path'/>"
            "<foreignObject class='user-x'></foreignObject></svg>"
            "<math class='user-math'></math><div class='user-after'></div>"
        )
        assert _checked([_LOGIN], after) == ("#user-avatar", "div.user-after")

    def test_a_self_closing_svg_closes_itself(self) -> None:
        after = _page("<svg class='user-icon'/><div class='account-box'></div>")
        assert _checked([_LOGIN], after) == ("div.account-box",)

    def test_an_image_tag_gives_only_an_id(self) -> None:
        """The HTML parser turns <image> into <img>, so image.<token> matches
        nothing in a browser."""
        after = _page("<image id='user-pic' class='avatar-pic'><div class='user-row'></div>")
        assert success_candidates([_LOGIN], after) == ("#user-pic", "div.user-row")

    def test_a_repeated_class_token_counts_once(self) -> None:
        after = _page("<div class='user-chip user-chip'></div>")
        assert _checked([_LOGIN], after) == ("div.user-chip",)

    @pytest.mark.parametrize(
        "name", ["designing-panel", "design-intro", "guestbook", "assigning-tasks", "dialogOutline"]
    )
    def test_a_marker_inside_another_word_rejects_nothing(self, name: str) -> None:
        assert _checked([_LOGIN], _page(f"<div class='{name}'></div>")) == (f"div.{name}",)

    @pytest.mark.parametrize("name", ["catalog-outlet", "dialog-outline", "blogout"])
    def test_a_sign_out_word_inside_another_word_is_no_boost(self, name: str) -> None:
        after = _page(f"<div id='main-panel'></div><a class='{name}' href='/sale'>Sale</a>")
        assert _checked([_LOGIN], after) == ("#main-panel", f"a.{name}")

    @pytest.mark.parametrize(
        ("markup", "selector"),
        [
            ("<div id='signedOut'></div>", ""),
            ("<a class='signin-link' href='/x'>x</a>", ""),
            ("<div class='guest2-banner'></div>", ""),
            ("<div class='user-menu'></div>", "div.user-menu"),
            ("<div class='myAccount'></div>", "div.myAccount"),
            ("<div class='is-logged-in'></div>", "div.is-logged-in"),
        ],
    )
    def test_words_split_at_case_changes_and_digits(self, markup: str, selector: str) -> None:
        after = _page(f"<div id='main-panel'></div>{markup}")
        expected = (selector, "#main-panel") if selector else ("#main-panel",)
        assert _checked([_LOGIN], after) == expected

    def test_a_name_s_words(self) -> None:
        """Digits split words too, though a name holding a word next to digits is
        mostly rejected as generated before its words are read."""
        assert _words("signIn2fa_user.XMLMenu-x") == [
            "sign",
            "in",
            "fa",
            "user",
            "xml",
            "menu",
            "x",
        ]

    def test_a_logout_form_is_a_sign_out_control(self) -> None:
        after = _page(
            "<div id='main-panel'></div>"
            "<form id='logout-form' action='/logout' method='post'></form>"
            "<form class='exit' action='/account/sign-out' method='post'></form>"
        )
        assert _checked([_LOGIN], after) == ("#logout-form", "form.exit", "#main-panel")

    def test_a_sign_out_name_on_a_list_item_is_not_suggested(self) -> None:
        after = _page("<li class='nav-logout'>Signed out</li>")
        assert _checked([_LOGIN], after) == ()

    def test_a_marker_on_every_logged_in_page_ranks_first(self) -> None:
        """The run browsed on after landing: what every logged-in page carries is the
        header the engine will see on the page the login lands on."""
        landing = _page("<div id='nav-shell'></div><div id='welcome'></div>")
        after = _page("<div id='nav-shell'></div>", "<div id='user-orders'></div>")
        assert _checked([_LOGIN], after, earlier_logged_in=[landing]) == (
            "#nav-shell",
            "#user-orders",
        )

    def test_an_earlier_page_asking_for_a_one_time_code_is_logged_out(self) -> None:
        mfa = _page(
            "<div id='account-menu'></div>",
            "<input name='code' autocomplete='One-Time-Code'>",
        )
        after = _page("<div id='account-menu'></div><div id='main-panel'></div>")
        assert _checked([_LOGIN, mfa], after) == ("#main-panel",)
        assert success_candidates([_LOGIN], after, earlier_logged_in=[mfa]) == ("#main-panel",)

    def test_unparseable_markup_does_not_raise(self) -> None:
        assert success_candidates(["<div <<"], "<div id='account-menu'><<<") == ("#account-menu",)

    def test_time_grows_linearly_with_the_page(self) -> None:
        """Every id and (tag, class) pair is counted once a page, so four times the
        elements costs about four times the CPU time, not sixteen."""

        def letters(i: int) -> str:
            word = ""
            while True:
                i, digit = divmod(i, 26)
                word += chr(ord("a") + digit)
                if not i:
                    return word

        def page(n: int) -> str:
            return _page(
                "".join(
                    f"<div id='panel-{letters(i)}' class='box-{letters(i)} c{i % 7}'></div>"
                    for i in range(n)
                )
            )

        def cost(n: int) -> float:
            before = [page(n).replace("panel-", "other-")] * 3
            after = page(n)
            best = float("inf")
            for _ in range(3):
                start = time.process_time()
                success_candidates(before, after)
                best = min(best, time.process_time() - start)
            return best

        small, large = cost(1500), cost(6000)
        assert large / small < 8, (small, large)
