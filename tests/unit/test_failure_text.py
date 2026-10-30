"""#268 part 3: the failure text a wrong-password attempt suggests, computed from the
page read after the attempt (raw HTML, in memory only) against the page at the post.

Every candidate is checked by the engine's own rule
(:func:`graftpunk.plugins.login_settle.failure_text_on_page`, a case-insensitive
substring of the raw serialized HTML), so these tests state what the engine would do
with each candidate and assert the function agrees.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from graftpunk.observe.failure_text import (
    MAX_FAILURE_CANDIDATES,
    AccountGuard,
    failed_password_values,
    failure_candidates,
    login_form_username_names,
    posted_values,
    scrub_failed_password,
    without_success_page,
)
from graftpunk.plugins.login_settle import failure_text_on_page

_BODY = json.dumps({"username": "jsmith", "password": "wrongpass1"})
_JSON = "application/json"
_BEFORE = (
    "<html><body><form><input name='username'><input type='password' name='password'>"
    "<button>Sign in</button></form></body></html>"
)


def _guard(body: str = _BODY, content_type: str = _JSON) -> AccountGuard:
    guard = AccountGuard.of(body, content_type)
    assert guard is not None
    return guard


def _after(error: str, *, extra: str = "") -> str:
    return _BEFORE.replace("<form>", f"{extra}<form>{error}")


def _found(after: str | list[str], before: str = _BEFORE, **kwargs: object) -> tuple[str, ...]:
    reads = [after] if isinstance(after, str) else after
    guard = kwargs.pop("guard", _guard())
    found = failure_candidates(reads, before, guard)  # type: ignore[arg-type]
    for text in found:
        # What the engine would do with each one: it matches every read it came from
        # (at least one) and never the page at the post.
        assert any(failure_text_on_page(read, text) for read in reads), text
        assert not failure_text_on_page(before, text), text
    return found


class TestFailureCandidates:
    def test_an_error_in_a_div_is_found(self) -> None:
        after = _after("<div>Invalid username or password.</div>")
        assert _found(after) == ("Invalid username or password.",)

    def test_whitespace_around_a_text_is_trimmed(self) -> None:
        after = _after("<div>\n    Invalid username or password.\n  </div>")
        assert _found(after) == ("Invalid username or password.",)

    @pytest.mark.parametrize("text", ["Wrong p", "x" * 201])
    def test_a_text_under_8_or_over_200_characters_is_not_found(self, text: str) -> None:
        assert _found(_after(f"<div>{text}</div>")) == ()

    def test_8_and_200_characters_are_found(self) -> None:
        after = _after("<div>Wrong pw</div><p>" + "y" * 200 + "</p>")
        assert _found(after) == ("Wrong pw", "y" * 200)

    @pytest.mark.parametrize(
        "elsewhere",
        [
            "<template><div>Invalid username or password.</div></template>",
            "<script>var t = {err: 'Invalid username or password.'};</script>",
        ],
    )
    def test_text_also_in_a_template_or_script_on_the_login_page_is_dropped(
        self, elsewhere: str
    ) -> None:
        """The engine reads raw HTML, scripts and templates included: text the login
        page already carries there would fail every login on its first tick."""
        before = _BEFORE.replace("</body>", f"{elsewhere}</body>")
        after = before.replace("<form>", "<form><div>Invalid username or password.</div>")
        assert failure_text_on_page(before, "Invalid username or password.")
        assert _found(after, before) == ()

    def test_text_only_in_a_script_or_template_of_the_read_is_no_candidate(self) -> None:
        """Not page text a person sees; the read's script and template contents give no
        text node."""
        after = _BEFORE.replace(
            "</body>",
            "<script>var e = 'Invalid username or password.';</script>"
            "<template><p>Your session has expired.</p></template></body>",
        )
        assert _found(after) == ()

    def test_text_split_by_an_element_gives_each_part_never_the_whole(self) -> None:
        """The engine matches a contiguous substring of the raw HTML, so the sentence
        "Your login details were not recognised by us" never matches as one: each
        text node does, and is the candidate."""
        after = _after("<div>Your login details were <b>not recognised</b> by us</div>")
        found = _found(after)
        assert found == ("Your login details were", "not recognised")
        assert not failure_text_on_page(after, "Your login details were not recognised by us")

    def test_text_holding_an_escaped_ampersand_is_dropped(self) -> None:
        """The serialized HTML holds ``&amp;``, so the decoded text never matches it."""
        after = _after("<div>Email &amp; password do not match</div>")
        assert not failure_text_on_page(after, "Email & password do not match")
        assert _found(after) == ()

    def test_text_already_on_the_page_at_the_post_is_dropped(self) -> None:
        before = _BEFORE.replace("<form>", "<form><p>Forgot your password?</p>")
        after = before.replace("<form>", "<form><div>Invalid username or password.</div>")
        assert _found(after, before) == ("Invalid username or password.",)

    def test_case_is_ignored_on_the_before_side(self) -> None:
        before = _BEFORE.replace(
            "</body>", "<script>ERR='INVALID USERNAME OR PASSWORD.'</script></body>"
        )
        after = before.replace("<form>", "<form><div>Invalid username or password.</div>")
        assert _found(after, before) == ()

    def test_without_the_page_at_the_post_nothing_is_suggested(self) -> None:
        after = _after("<div>Invalid username or password.</div>")
        assert failure_candidates([after], None, _guard()) == ()

    def test_without_a_guard_nothing_is_suggested(self) -> None:
        after = _after("<div>Invalid username or password.</div>")
        assert failure_candidates([after], _BEFORE, None) == ()

    @pytest.mark.parametrize(
        "error",
        [
            "No account matches jsmith here",
            "No account matches JSMITH here",
            "No account matches j_smith here",
            "The password wrongpass1 is wrong",
            "Write to alice@example.com for help",
            "Write to ALICE@EXAMPLE.COM for help",
        ],
    )
    def test_text_holding_an_account_value_or_an_email_is_dropped(self, error: str) -> None:
        after = _after(f"<div>{error}</div><p>Please try again later.</p>")
        assert _found(after) == ("Please try again later.",)

    def test_an_echoed_email_username_is_dropped_in_any_case(self) -> None:
        body = json.dumps({"email": "Alice.Smith@Example.com", "password": "wrongpass1"})
        after = _after("<div>No account for alice.smith@example.com</div><p>Try again.</p>")
        assert _found(after, guard=_guard(body)) == ("Try again.",)

    def test_an_email_local_part_is_an_account_value(self) -> None:
        body = json.dumps({"email": "alice.smith@example.com", "password": "wrongpass1"})
        after = _after("<div>Hello alice.smith, that failed</div><p>Please try again later.</p>")
        assert _found(after, guard=_guard(body)) == ("Please try again later.",)

    def test_a_value_only_its_percent_encoded_spelling_finds_is_dropped(self) -> None:
        """A page echoing the password as encodeURIComponent writes it: the compact
        needles miss it, the snapshot redaction's spellings do not."""
        body = json.dumps({"username": "jsmith", "password": "p@ss word!"})
        guard = _guard(body)
        assert "p%40ss%20word!" in guard.spellings
        after = _after("<div>Retry p%40ss%20word! later</div><p>Please try again later.</p>")
        assert _found(after, guard=guard) == ("Please try again later.",)

    @pytest.mark.parametrize(
        "body",
        [
            "username=jo&password=wrongpass1",
            json.dumps({"username": "jo", "password": "wrongpass1"}),
        ],
    )
    def test_a_short_username_is_dropped_as_a_whole_word(self, body: str) -> None:
        content_type = (
            "application/json" if body.startswith("{") else "application/x-www-form-urlencoded"
        )
        guard = _guard(body, content_type)
        after = _after(
            "<div>Hello JO, wrong password</div><p>Your job did not run.</p>"
            "<p>Please try again later.</p>"
        )
        # "job" holds "jo" only inside a longer word.
        assert _found(after, guard=guard) == ("Your job did not run.", "Please try again later.")

    def test_an_unreadable_body_gives_no_guard(self) -> None:
        assert AccountGuard.of("<xml/>", "application/xml") is None
        assert AccountGuard.of(None) is None

    def test_errors_marked_as_alerts_rank_first_then_document_order(self) -> None:
        after = _after(
            "<p>Need help signing in?</p>"
            "<span class='form-Error-text'>Check the fields below.</span>"
            "<div><p>Please try again later.</p></div>"
            "<div role='alert'><p>That did not work out.</p></div>"
            "<div aria-live='polite'>Something went wrong here.</div>"
        )
        assert _found(after) == (
            "Check the fields below.",
            "That did not work out.",
            "Something went wrong here.",
        )

    @pytest.mark.parametrize(
        "wrapper",
        [
            "<div class='invalid-feedback'>{}</div>",
            "<div class='alert alert-danger'>{}</div>",
            "<div role='alert'>{}</div>",
            "<div aria-live='assertive'>{}</div>",
        ],
    )
    def test_each_alert_marker_ranks_first(self, wrapper: str) -> None:
        after = _after("<p>Need help signing in?</p>" + wrapper.format("Sign-in failed, sorry."))
        assert _found(after)[0] == "Sign-in failed, sorry."

    def test_aria_live_off_is_no_alert(self) -> None:
        after = _after(
            "<p>Need help signing in?</p><div aria-live='off'>Sign-in failed, sorry.</div>"
        )
        assert _found(after) == ("Need help signing in?", "Sign-in failed, sorry.")

    def test_at_most_3(self) -> None:
        texts = [f"Error number {n} happened" for n in range(6)]
        after = _after("".join(f"<p>{text}</p>" for text in texts))
        assert _found(after) == tuple(texts[:MAX_FAILURE_CANDIDATES])
        assert MAX_FAILURE_CANDIDATES == 3

    def test_two_reads_each_checked_against_itself_and_merged(self) -> None:
        """The toast in the first read is gone from the second; the text in both is
        given once."""
        first = _after(
            "<div role='alert'>Invalid username or password.</div>"
            "<p class='error'>Try again soon.</p>"
        )
        second = _after(
            "<p class='error'>Try again soon.</p><p class='error'>Locked for a minute now.</p>"
        )
        assert _found([first, second]) == (
            "Invalid username or password.",
            "Try again soon.",
            "Locked for a minute now.",
        )

    def test_only_alerts_when_any_alert_text_is_kept(self) -> None:
        """A re-rendered heading or a timestamp beside a ``role="alert"`` message is
        page churn, not the error."""
        after = _after(
            "<p>Last visit at 10:42 today</p><div role='alert'>That did not work out.</div>"
        )
        assert _found(after) == ("That did not work out.",)
        # With no alert text at all, the others are still suggested.
        assert _found(_after("<p>That did not work out.</p>")) == ("That did not work out.",)

    @pytest.mark.parametrize(
        "markup",
        [
            "<title>Sign-in failed, sorry</title>",
            "<select><option>Sign-in failed, sorry</option></select>",
            "<label for='x'>Sign-in failed, sorry</label>",
            "<label><span>Sign-in failed, sorry</span></label>",
            "<button type='button'>Sign-in failed, sorry</button>",
            "<fieldset><legend>Sign-in failed, sorry</legend></fieldset>",
        ],
    )
    def test_never_text_naming_the_page_or_a_control(self, markup: str) -> None:
        after = _after(markup)
        assert failure_text_on_page(after, "Sign-in failed, sorry")
        assert _found(after) == ()

    @pytest.mark.parametrize(
        "markup",
        [
            # jQuery Validation's shape.
            "<label id='password-error' class='error' for='password'>Sign-in failed, sorry</label>",
            "<label role='alert'><span>Sign-in failed, sorry</span></label>",
            "<button type='button' aria-live='polite'>Sign-in failed, sorry</button>",
            "<legend class='is-invalid'>Sign-in failed, sorry</legend>",
        ],
    )
    def test_an_excluded_element_that_is_itself_an_alert_is_read(self, markup: str) -> None:
        after = _after("<p>Need help signing in?</p>" + markup)
        assert _found(after) == ("Sign-in failed, sorry",)

    def test_a_label_inside_an_alert_is_still_no_message(self) -> None:
        """Only the excluded element's own markers count: a plain label inside an
        alert box names a control."""
        after = _after(
            "<div class='alert'><label for='x'>Remember this device</label>"
            "<p>Sign-in failed, sorry</p></div>"
        )
        assert _found(after) == ("Sign-in failed, sorry",)

    def test_never_a_placeholder(self) -> None:
        after = _after("<input type='text' placeholder='Sign-in failed, sorry'>")
        assert failure_text_on_page(after, "Sign-in failed, sorry")
        assert _found(after) == ()

    def test_a_read_that_is_no_login_page_is_ignored(self) -> None:
        """Enter pressed after a real login: the read is the landed page, whose text
        is all new and none of it a failure."""
        landed = (
            "<html><body><div class='alert-banner'>Free shipping on orders over 50</div>"
            "<p>Your recent orders</p></body></html>"
        )
        assert _found(landed) == ()
        # The same text on a read that is still a login page counts.
        assert _found(_after("<div class='alert-banner'>Free shipping on orders over 50</div>"))

    def test_a_server_error_page_re_rendering_the_form_counts(self) -> None:
        """A form POST answered with a whole new document that shows the form again
        and the error: still a login page."""
        error_page = (
            "<html><head><title>Sign in</title></head><body><main>"
            "<p class='flash-error'>Invalid login. Please try again.</p>"
            "<form method='post' action='/session'><input name='username'>"
            "<input type='password' name='password'><button>Sign in</button></form>"
            "</main></body></html>"
        )
        assert _found(error_page) == ("Invalid login. Please try again.",)

    def test_a_password_input_inside_a_template_makes_no_login_page(self) -> None:
        read = (
            "<html><body><p class='error'>Session ended for safety</p>"
            "<template><input type='password'></template></body></html>"
        )
        assert _found(read) == ()

    def test_a_text_repeated_in_another_case_is_given_once(self) -> None:
        after = _after("<p>Invalid credentials given</p><p>INVALID CREDENTIALS GIVEN</p>")
        assert _found(after) == ("Invalid credentials given",)


class TestThePageAtThePost:
    def test_without_one_nothing_is_suggested(self) -> None:
        """No request-time snapshot: no page as served stands in for it, since a
        script can add text to the login page (a cookie banner) that the served HTML
        lacks, and that text would fail every login."""
        after = _after("<div role='alert'>Invalid username or password.</div>")
        assert failure_candidates([after], None, _guard()) == ()
        assert _found(after) == ("Invalid username or password.",)


class TestTheSearchCap:
    def test_texts_the_page_at_the_post_shows_take_no_place_in_the_cap(self) -> None:
        """Sixty texts the login page shows already, then a new error that is no
        alert: the shared texts are dropped before the cap of fifty searches, so the
        error is still searched for."""
        shared = "".join(f"<p>Help line number {n} for signing in</p>" for n in range(60))
        before = _BEFORE.replace("<form>", f"{shared}<form>")
        after = before.replace("<form>", "<p>That sign-in did not work.</p><form>")
        assert _found(after, before) == ("That sign-in did not work.",)


class TestValuesOfOtherRequests:
    """An identifier-first login posts the username in a step before the password
    post, under any name, so the password post's own values do not hold it."""

    _STEP = "username=jsmith"
    _FORM = "application/x-www-form-urlencoded"

    def test_the_guard_holds_them(self) -> None:
        own = _guard("password=wrong-x7k2q9", self._FORM)
        assert not own.holds("The password for jsmith is not right.")
        posted = posted_values([(self._STEP, self._FORM, "POST")])
        assert posted == ["jsmith", "jsmith"]
        guard = AccountGuard.of("password=wrong-x7k2q9", self._FORM, also=posted)
        assert guard is not None
        assert guard.holds("The password for jsmith is not right.")
        assert guard.holds("No user JSmith here")

    def test_a_short_one_is_held_as_a_whole_word(self) -> None:
        guard = AccountGuard.of("password=wrong-x7k2q9", self._FORM, also=["jo"])
        assert guard is not None
        assert guard.holds("Unknown user jo. Try again")
        assert not guard.holds("Try a job again")

    @pytest.mark.parametrize("name", ["j_username", "user_login", "loginfmt", "handle", "uid"])
    def test_a_step_field_under_any_name(self, name: str) -> None:
        posted = posted_values([(f"{name}=jsmith", self._FORM, "POST")])
        assert posted == ["jsmith"]
        guard = AccountGuard.of("j_password=wrong-x7k2q9", self._FORM, also=posted)
        assert guard is not None
        assert guard.holds("The password for jsmith is not right.")

    def test_every_value_of_a_post_but_a_token_s_short_one_or_boolean_word(self) -> None:
        bodies = [
            ("q=shoes&page=2&csrf_token=abc123xyz&remember=true&lang=en", self._FORM, "POST"),
            (json.dumps({"user": {"email": "alice@example.com"}, "note": "x"}), _JSON, "POST"),
        ]
        assert posted_values(bodies) == ["shoes", "alice@example.com", "alice@example.com"]

    def test_only_identifier_values_of_a_request_that_is_no_post(self) -> None:
        body = json.dumps({"login": "jsmith", "note": "first visit"})
        assert posted_values([(body, _JSON, "PUT")]) == ["jsmith"]
        assert posted_values([(body, _JSON, "POST")]) == ["jsmith", "jsmith", "first visit"]

    def test_a_login_form_s_username_name_counts(self) -> None:
        page = (
            "<html><body><form action='/login' method='post'><input name='handle'>"
            "<input type='password' name='password'></form></body></html>"
        )
        names = login_form_username_names(page)
        assert names == frozenset({"handle"})
        assert posted_values([("handle=jo", self._FORM, "POST")]) == []
        assert posted_values([("handle=jo", self._FORM, "POST")], names) == ["jo"]
        assert login_form_username_names(None) == frozenset()


class TestCost:
    """CPU time for a page sixteen times as long stays near sixteen times as much,
    well under the 256 times a search of the whole page per text node would take. The
    bound is 64, halfway between the two on a log scale."""

    _ALERT = "<div role='alert'>Sign-in failed, sorry.</div>"

    @staticmethod
    def _filler(paragraphs: int) -> str:
        return "".join(f"<p>Paragraph number {n} of the help text</p>" for n in range(paragraphs))

    @staticmethod
    def _cpu(pages: Callable[[int], tuple[str, str]], paragraphs: int) -> float:
        import time

        before, after = pages(paragraphs)
        guard = _guard()
        best = float("inf")
        for _ in range(3):
            start = time.process_time()
            assert failure_candidates([after, after], before, guard) == ("Sign-in failed, sorry.",)
            best = min(best, time.process_time() - start)
        return best

    def _ratio(self, pages: Callable[[int], tuple[str, str]]) -> float:
        return self._cpu(pages, 32_000) / self._cpu(pages, 2_000)

    def test_text_the_page_at_the_post_shares(self) -> None:
        """The usual read: the login page again, with the error added."""

        def pages(paragraphs: int) -> tuple[str, str]:
            before = _BEFORE.replace("<form>", f"{self._filler(paragraphs)}<form>")
            return before, before.replace("<form>", f"{self._ALERT}<form>")

        assert self._ratio(pages) < 64

    def test_text_only_the_read_holds(self) -> None:
        """A read whose every text is new (a page the server rendered anew)."""

        def pages(paragraphs: int) -> tuple[str, str]:
            return _BEFORE, _after(self._ALERT, extra=self._filler(paragraphs))

        assert self._ratio(pages) < 64


class TestWithoutSuccessPage:
    def test_a_candidate_also_on_the_logged_in_page_is_dropped(self) -> None:
        landed = "<html><body><p>Try again soon.</p><div id='account'></div></body></html>"
        kept = without_success_page(
            ("Invalid username or password.", "TRY AGAIN SOON."), landed, _guard()
        )
        assert kept == ("Invalid username or password.",)

    def test_a_candidate_holding_the_real_post_s_value_is_dropped(self) -> None:
        real = _guard(json.dumps({"username": "jsmith2024", "password": "realsecret9"}))
        kept = without_success_page(("Unknown user jsmith2024", "Bad credentials"), None, real)
        assert kept == ("Bad credentials",)

    def test_no_guard_for_the_real_post_drops_everything(self) -> None:
        assert without_success_page(("Bad credentials",), None, None) == ()


class TestFailedPasswordValues:
    def test_the_password_field_s_values(self) -> None:
        body = "username=jsmith&password=wrongpass1&remember=1"
        assert failed_password_values(body, "application/x-www-form-urlencoded") == ["wrongpass1"]

    def test_a_hint_only_in_a_parent_name_is_no_password(self) -> None:
        """The nearest name decides: ``note`` under ``password_policy`` is no
        password, and scrubbing it would lose a value that is not secret."""
        body = json.dumps(
            {
                "username": "jsmith",
                "password": "wrongpass1",
                "password_policy": {"note": "x9y8z7w6"},
            }
        )
        assert failed_password_values(body, _JSON) == ["wrongpass1"]

    def test_a_nested_json_password(self) -> None:
        body = json.dumps({"user": {"login": "jsmith", "password": "wrongpass1"}})
        assert failed_password_values(body, _JSON) == ["wrongpass1"]


class TestScrubFailedPassword:
    """Only the failed post's own body, only a whole value under a password-like
    name, and the rest written back character for character."""

    _FORM = "application/x-www-form-urlencoded"
    _R = "***REDACTED***"

    def test_a_form_value_and_nothing_else(self) -> None:
        body = "grant_type=password&username=jsmith&password=password&next=password"
        assert scrub_failed_password(body, self._FORM, ["password"]) == (
            f"grant_type=password&username=jsmith&password={self._R}&next=password"
        )

    def test_a_percent_encoded_form_value_is_compared_decoded(self) -> None:
        body = "username=jsmith&password=p%40ss+word%21"
        assert scrub_failed_password(body, self._FORM, ["p@ss word!"]) == (
            f"username=jsmith&password={self._R}"
        )

    def test_a_text_plain_body(self) -> None:
        body = "username=jsmith\r\npassword=wrong-x7k2q9\r\n"
        assert scrub_failed_password(body, "text/plain", ["wrong-x7k2q9"]) == (
            f"username=jsmith\r\npassword={self._R}\r\n"
        )

    def test_a_text_plain_value_is_one_line_holding_an_ampersand(self) -> None:
        body = "username=jsmith\r\npassword=pa&ss9\r\n"
        assert failed_password_values(body, "text/plain") == ["pa&ss9"]
        assert scrub_failed_password(body, "text/plain", ["pa&ss9"]) == (
            f"username=jsmith\r\npassword={self._R}\r\n"
        )

    def test_a_one_line_text_plain_body_loses_the_whole_value(self) -> None:
        """An identifier-first password page with ``enctype="text/plain"`` sends one
        field and its CRLF; the part after an ``&`` is the password too."""
        body = "password=pa&ss9\r\n"
        assert failed_password_values(body, "text/plain") == ["pa&ss9"]
        assert scrub_failed_password(body, "text/plain", ["pa&ss9"]) == (f"password={self._R}\r\n")

    def test_json_in_a_text_plain_field(self) -> None:
        body = 'user=jsmith\r\ncreds={"password":"wrong-abc123"}\r\n'
        assert failed_password_values(body, "text/plain") == ["wrong-abc123"]
        assert scrub_failed_password(body, "text/plain", ["wrong-abc123"]) == (
            f'user=jsmith\r\ncreds={{"password":"{self._R}"}}\r\n'
        )

    def test_a_text_plain_value_under_a_password_name_only(self) -> None:
        body = "action=login\r\nuser=jsmith\r\npassword=login\r\n"
        assert scrub_failed_password(body, "text/plain", ["login"]) == (
            f"action=login\r\nuser=jsmith\r\npassword={self._R}\r\n"
        )

    def test_a_text_plain_value_is_compared_as_written(self) -> None:
        body = "username=jsmith\npassword=pa+ss%9\n"
        assert failed_password_values(body, "text/plain") == ["pa+ss%9"]
        assert scrub_failed_password(body, "text/plain", ["pa+ss%9"]) == (
            f"username=jsmith\npassword={self._R}\n"
        )

    def test_a_text_plain_value_runs_on_over_a_line_without_an_equals_sign(self) -> None:
        body = "username=jsmith\r\npassword=ab\r\ncd\r\nremember=1"
        assert failed_password_values(body, "text/plain") == ["ab\ncd"]
        assert scrub_failed_password(body, "text/plain", ["ab\ncd"]) == (
            f"username=jsmith\r\npassword={self._R}\r\nremember=1"
        )

    def test_a_one_line_form_body_ending_in_a_line_break(self) -> None:
        body = "username=jsmith&password=wrong-abc123\n"
        values = failed_password_values(body, self._FORM)
        assert values == ["wrong-abc123\n"]
        assert scrub_failed_password(body, self._FORM, values) == (
            f"username=jsmith&password={self._R}\n"
        )

    @pytest.mark.parametrize(
        ("body", "content_type"),
        [
            ("username=jsmith\r\npassword=pa&ss9\r\n", "text/plain"),
            ("username=jsmith\npassword=pa+ss%9\n", "text/plain"),
            ("username=jsmith\r\npassword=ab\r\ncd\r\nremember=1", "text/plain"),
            ("username=jsmith&password=wrong-abc123\n", _FORM),
            ("password=pa&ss9", "text/plain"),
            ("password=pa&ss9\r\n", "text/plain"),
            ("username=jsmith&password=p%26ss+9&password=x%0Ay", _FORM),
            (json.dumps({"user": {"password": "wrong-abc123"}}), _JSON),
            ('{"username": "jsmith", "password": 12345678}', _JSON),
            ('{"pin": 1, "password": -1.5e3}', _JSON),
            (
                '--b\r\nContent-Disposition: form-data; name="password"\r\n\r\n'
                "ab\n--cd\r\n--b--\r\n",
                "multipart/form-data; boundary=b",
            ),
        ],
    )
    def test_every_value_it_reads_it_scrubs(self, body: str, content_type: str) -> None:
        values = failed_password_values(body, content_type)
        assert values
        scrubbed = scrub_failed_password(body, content_type, values)
        assert set(failed_password_values(scrubbed, content_type)) <= {self._R, f"{self._R}\n"}

    def test_json_in_a_form_field_written_with_plus_for_a_space(self) -> None:
        body = "data=%7B%22password%22%3A+%22a+b%22%7D"
        assert scrub_failed_password(body, self._FORM, ["a b"]) == (
            "data=%7B%22password%22%3A+%22***REDACTED***%22%7D"
        )

    def test_json_values_under_a_password_name_only(self) -> None:
        body = (
            '{"action": "login", "user": "jsmith", "password": "login", '
            '"creds": {"pwd": "login"}, "passwords": ["login"], "note": "login"}'
        )
        assert scrub_failed_password(body, _JSON, ["login"]) == (
            '{"action": "login", "user": "jsmith", "password": "***REDACTED***", '
            '"creds": {"pwd": "***REDACTED***"}, "passwords": ["***REDACTED***"], '
            '"note": "login"}'
        )

    def test_a_json_number_under_a_password_name(self) -> None:
        """The reader reads a number as its text, so a typed all-digit password is
        replaced, by a string, keeping the JSON valid; a number under another name
        stays."""
        body = '{"username": "jsmith", "id": 12345678, "password": 12345678}'
        assert failed_password_values(body, _JSON) == ["12345678"]
        scrubbed = scrub_failed_password(body, _JSON, ["12345678"])
        assert scrubbed == '{"username": "jsmith", "id": 12345678, "password": "***REDACTED***"}'
        assert json.loads(scrubbed)["password"] == self._R

    def test_a_key_spelled_as_the_password_is_kept(self) -> None:
        body = '{"wrong-x7k2q9": 1, "password": "wrong-x7k2q9"}'
        assert scrub_failed_password(body, _JSON, ["wrong-x7k2q9"]) == (
            '{"wrong-x7k2q9": 1, "password": "***REDACTED***"}'
        )

    def test_only_a_whole_value(self) -> None:
        body = '{"password": "password", "pwd_hint": "password1", "pwd_note": "my password"}'
        assert scrub_failed_password(body, _JSON, ["password"]) == (
            '{"password": "***REDACTED***", "pwd_hint": "password1", "pwd_note": "my password"}'
        )

    def test_json_in_a_json_string(self) -> None:
        inner = json.dumps({"user": "jsmith", "password": "wrong-abc123"})
        body = json.dumps({"data": inner, "kind": "login"})
        out = json.loads(scrub_failed_password(body, _JSON, ["wrong-abc123"]))
        assert json.loads(out["data"]) == {"user": "jsmith", "password": self._R}
        assert out["kind"] == "login"

    def test_json_in_a_json_string_keeps_its_escapes(self) -> None:
        """Raw non-ASCII stays raw, and an escaped one stays escaped."""
        raw = '{"data": "{\\"password\\": \\"wrong-abc123\\", \\"name\\": \\"Zoë\\"}"}'
        assert scrub_failed_password(raw, _JSON, ["wrong-abc123"]) == raw.replace(
            "wrong-abc123", self._R
        )
        escaped = raw.replace("ë", "\\\\u00eb")
        assert scrub_failed_password(escaped, _JSON, ["wrong-abc123"]) == escaped.replace(
            "wrong-abc123", self._R
        )

    def test_json_in_a_percent_encoded_form_field(self) -> None:
        body = "data=%7B%22user%22%3A%22jsmith%22%2C%22password%22%3A%22wrong-abc123%22%7D"
        assert scrub_failed_password(body, self._FORM, ["wrong-abc123"]) == (
            "data=%7B%22user%22%3A%22jsmith%22%2C%22password%22%3A%22***REDACTED***%22%7D"
        )

    def test_json_in_a_form_field_written_unencoded(self) -> None:
        body = 'data={"user":"jsmith","password":"wrong-abc123"}&lang=en'
        assert scrub_failed_password(body, "text/plain", ["wrong-abc123"]) == (
            'data={"user":"jsmith","password":"***REDACTED***"}&lang=en'
        )

    def test_a_multipart_part_and_json_in_one(self) -> None:
        def part(name: str, value: str) -> str:
            head = f'Content-Disposition: form-data; name="{name}"'
            return f"--b\r\n{head}\r\n\r\n{value}\r\n"

        body = (
            part("username", "jsmith")
            + part("password", "password")
            + part("mode", "password")
            + part("creds", '{"password":"password"}')
            + "--b--\r\n"
        )
        assert scrub_failed_password(body, "multipart/form-data; boundary=b", ["password"]) == (
            part("username", "jsmith")
            + part("password", self._R)
            + part("mode", "password")
            + part("creds", f'{{"password":"{self._R}"}}')
            + "--b--\r\n"
        )

    def test_no_values_or_no_body_leaves_it(self) -> None:
        assert scrub_failed_password("password=x1y2z3", self._FORM, []) == "password=x1y2z3"
        assert scrub_failed_password("", self._FORM, ["x1y2z3"]) == ""
