"""Suggested ``success`` selectors read from a recording (#268): an element on the
logged-in page and on no recorded copy of the login page. Each emitted selector is
checked with lxml and cssselect against both sides, so the stdlib matcher the
module uses at run time cannot drift from what a browser's CSS engine selects."""

from __future__ import annotations

import lxml.html
import pytest

from graftpunk.har.login_signals import success_candidates

_LOGIN = (
    "<html><body><header><nav class='site-nav'><a href='/login' class='nav-link'>Sign in</a>"
    "</nav></header><main><form action='/session' method='post'>"
    "<input type='email' name='email'><input type='password' name='password'>"
    "<button class='btn'>Sign in</button></form><p class='intro'>Welcome</p></main>"
    "</body></html>"
)


def _page(header: str, main: str = "<p class='intro'>Welcome</p>") -> str:
    return (
        "<html><body><header><nav class='site-nav'>"
        f"{header}</nav></header><main>{main}</main></body></html>"
    )


def _matches(selector: str, html: str) -> int:
    return len(lxml.html.fromstring(html).cssselect(selector))


def _checked(before: list[str], after: list[str]) -> tuple[str, ...]:
    """``success_candidates``, with every selector it emits checked by cssselect: on
    exactly one element of each logged-in page holding it, on at least one of them,
    and on no element of any login copy."""
    found = success_candidates(before, after)
    assert len(found) <= 3
    for selector in found:
        counts = [_matches(selector, page) for page in after]
        assert max(counts) == 1, (selector, counts)
        for page in before:
            assert _matches(selector, page) == 0, selector
    return found


class TestSuccessCandidates:
    def test_an_id_on_the_logged_in_page_only_is_suggested(self) -> None:
        after = _page("<div id='account-menu'><a href='/orders'>Orders</a></div>")
        assert _checked([_LOGIN], [after]) == ("#account-menu",)

    def test_a_class_token_is_suggested_with_its_tag(self) -> None:
        after = _page("<span class='badge user-chip'>A</span>")
        assert _checked([_LOGIN], [after]) == ("span.user-chip", "span.badge")

    def test_keyword_first_then_id_then_class_in_document_order(self) -> None:
        after = _page(
            "<div id='cart-count'>2</div><span class='header-tools'></span>"
            "<a class='signout-link' href='/bye'>Sign out</a><div id='main-panel'></div>"
        )
        assert _checked([_LOGIN], [after]) == ("a.signout-link", "#cart-count", "#main-panel")

    def test_a_link_to_logout_ranks_first(self) -> None:
        after = _page("<div id='main-panel'></div><a class='nav-exit' href='/logout'>Bye</a>")
        assert _checked([_LOGIN], [after])[0] == "a.nav-exit"

    def test_an_element_also_on_the_login_page_is_not_suggested(self) -> None:
        """A hidden account menu the logged-out page carries too would pass on the
        first tick of a failed login."""
        login = _LOGIN.replace("<main>", "<main><div class='account-menu' hidden></div>")
        after = _page("<div class='account-menu'></div><div id='welcome'></div>")
        assert _checked([login], [after]) == ("#welcome",)

    @pytest.mark.parametrize(
        "marker",
        [
            "<div class='Header_loggedIn__x7Q2a'></div>",
            "<div class='css-1x2y3z'></div>",
            "<div class='sc-bdVaJa'></div>",
            "<div id='ember1234'></div>",
            "<div id='user_40912873'></div>",
            "<div id='alice@example.com'></div>",
            "<div class='a3f9c2d1e0b4'></div>",
            "<div id='1st'></div>",
            "<div id='odd:id'></div>",
            "<div class='logged-out'></div>",
        ],
    )
    def test_a_generated_id_or_class_or_an_account_value_is_not_suggested(
        self, marker: str
    ) -> None:
        assert _checked([_LOGIN], [_page(marker)]) == ()

    def test_a_selector_matching_twice_is_not_suggested(self) -> None:
        after = _page("<span class='user-chip'></span><span class='user-chip'></span>")
        assert _checked([_LOGIN], [after]) == ()

    def test_an_id_on_two_elements_is_not_suggested(self) -> None:
        after = _page("<div id='account'></div><p id='account'></p>")
        assert _checked([_LOGIN], [after]) == ()

    def test_the_snapshot_at_the_post_counts_as_a_login_copy(self) -> None:
        """The DOM as the user submitted the password, sanitized: a modal's account
        hint there is no logged-in marker."""
        snapshot = (
            "<html><head></head><body><header><div id='account-hint'></div></header>"
            "<div class='modal'><input type='password' name='password'></div></body></html>"
        )
        after = _page("<div id='account-hint'></div><div id='account-menu'></div>")
        assert _checked([_LOGIN, snapshot], [after]) == ("#account-menu",)

    def test_at_most_three(self) -> None:
        after = _page("".join(f"<div id='panel-{name}'></div>" for name in "abcdef"))
        assert _checked([_LOGIN], [after]) == ("#panel-a", "#panel-b", "#panel-c")

    def test_no_logged_in_page_gives_nothing(self) -> None:
        assert success_candidates([_LOGIN], []) == ()

    def test_head_and_template_elements_are_not_suggested(self) -> None:
        after = (
            "<html><head><meta id='user-meta'><script id='user-data'></script></head>"
            "<body><template><div id='user-tpl'></div></template>"
            "<noscript><div id='user-nojs'></div></noscript></body></html>"
        )
        assert _checked([_LOGIN], [after]) == ()

    def test_a_marker_on_more_logged_in_pages_ranks_first(self) -> None:
        first = _page("<div id='order-list'></div><div id='session-bar'></div>")
        second = _page("<div id='session-bar'></div>")
        assert _checked([_LOGIN], [first, second]) == ("#session-bar", "#order-list")

    def test_matching_twice_on_any_logged_in_page_rejects_it(self) -> None:
        first = _page("<div class='user-chip'></div>")
        second = _page("<div class='user-chip'></div><div class='user-chip'></div>")
        assert _checked([_LOGIN], [first, second]) == ()

    def test_a_class_matched_case_sensitively_like_the_browser(self) -> None:
        """CSS class selectors are case-sensitive in a standards-mode document."""
        login = _LOGIN.replace("<main>", "<main><div class='UserChip'></div>")
        after = _page("<div class='userchip'></div>")
        assert _checked([login], [after]) == ("div.userchip",)

    def test_unparseable_markup_does_not_raise(self) -> None:
        assert success_candidates(["<div <<"], ["<div id='account-menu'><<<"]) == ("#account-menu",)
