"""The recorder's read of the page after a credential post settles (#268, part 2).

A credential post that carries ``request._domAfter`` links a structure-only read of
the page taken a few seconds after its response (``snapshots/<rid>.after.html``).
The digest keeps 1.29.0's final logged-in page (``page-source.html``, else the
last page in the HAR) and, when the last credential post's read is usable, suggests
only what that read also holds. A read that is still a login (a login form, a step
of one, or one a step-shaped POST followed) is not used. A run without the key
digests exactly as graftpunk 1.29.0 did, which the golden file pins.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from graftpunk.devtools.scaffold.render import ScaffoldSpec, render
from graftpunk.har.digest import DigestSource, digest
from graftpunk.har.documents import extract_login_forms
from graftpunk.har.report import render_endpoints_json, render_json, render_markdown
from graftpunk.observe.snapshot import stored_dom_snapshot
from tests.unit.test_har_digest import _entry, _write_har

_HOST = "https://myshop.example.com"
_CREDS = {"email": "alice@example.com", "password": "hunter2hunter2"}
_LOGIN = (
    "<html><body><header><nav class='site-nav'></nav></header><main>"
    "<form action='/session' method='post'><input type='email' id='email' name='email'>"
    "<input type='password' id='password' name='password'><button id='go'>Sign in</button>"
    "</form></main></body></html>"
)
# The page the login landed on, as the browser rendered it: the after-read's page.
_LANDING = (
    "<html><body><header><nav class='site-nav'><div id='account-menu'>"
    "<span class='who'>alice@example.com</span>"
    "<a class='signout-link' href='/bye'>Sign out</a></div></nav></header>"
    "<main><div id='welcome'><p>Welcome back, Alice</p></div></main></body></html>"
)
# Where the user went next and stopped: page-source.html.
_ORDERS = (
    "<html><body><header><nav class='site-nav'>"
    "<a class='signout-link' href='/bye'>Sign out</a></nav></header>"
    "<main><div id='orders-table'></div></main></body></html>"
)
_MFA = (
    "<html><body><div id='verify-box'><input id='otp' name='otp' "
    "autocomplete='one-time-code'></div></body></html>"
)
_FIXTURES = Path(__file__).parent.parent / "fixtures"
_GOLDEN = _FIXTURES / "digest_without_dom_after.json"


def _after(html: str) -> str:
    """*html* as the recorder stores an after-read: structure only, redacted of the
    post's own values."""
    return stored_dom_snapshot(html, json.dumps(_CREDS), "application/json", structure_only=True)


def _get(path: str, html: str, started: str = "2026-09-10T10:00:00.000Z") -> dict:
    entry = _entry("GET", f"{_HOST}{path}", content_type="text/html", body=html, started=started)
    entry["request"]["_resourceType"] = "Document"
    return entry


def _post(run: Path, rid: str, *, after: str | None = None, path: str = "/session") -> dict:
    entry = _entry(
        "POST",
        f"{_HOST}{path}",
        post_data=json.dumps(_CREDS),
        started="2026-09-10T10:00:05.000Z",
    )
    if after is not None:
        (run / "snapshots").mkdir(exist_ok=True)
        (run / "snapshots" / f"{rid}.after.html").write_text(after)
        entry["request"]["_domAfter"] = f"snapshots/{rid}.after.html"
    return entry


def _document_post(path: str, payload: dict) -> dict:
    entry = _entry("POST", f"{_HOST}{path}", status=302, post_data=json.dumps(payload))
    entry["request"]["_resourceType"] = "Document"
    return entry


def _digest(run: Path, entries: list[dict], page_source: str = ""):  # noqa: ANN202
    run.mkdir(parents=True, exist_ok=True)
    har = _write_har(run, entries)
    source = None
    if page_source:
        source = run / "page-source.html"
        source.write_text(page_source)
    return digest(DigestSource(har_path=har, page_source=source))


def _without_after(entries: list[dict]) -> list[dict]:
    stripped = json.loads(json.dumps(entries))
    for entry in stripped:
        entry["request"].pop("_domAfter", None)
    return stripped


def _fetch_post(path: str, payload: dict) -> dict:
    """A POST a page's script sent (fetch), after the credential post."""
    entry = _entry(
        "POST", f"{_HOST}{path}", post_data=json.dumps(payload), started="2026-09-10T10:00:40.000Z"
    )
    entry["request"]["_resourceType"] = "Fetch"
    return entry


# An MFA page with no one-time-code input and no password: no rule of part 1 knows
# it for a login step.
_PLAIN_MFA = (
    "<html><body><header><nav class='site-nav'></nav></header><main>"
    "<div id='mfa-challenge'><form id='mfa-form'><input id='code' name='code' "
    "inputmode='numeric'><button id='verify'>Verify</button></form></div></main></body></html>"
)
# The live probe's case 1: a script login swaps the header in place; the user then
# browses to a help page.
_SIGNED_OUT_HEADER = "<header id='site-header'><a id='brand' href='/'>Shop</a></header>"
_SIGNED_IN_HEADER = (
    "<header id='site-header' class='logged-in'><a id='brand' href='/'>Shop</a>"
    "<nav id='account-nav'><a id='account-link' href='/account'>My account</a>"
    "<a id='logout-link' href='/logout'>Sign out</a></nav></header>"
)
_SCRIPT_LOGIN = (
    f"<html><body>{_SIGNED_OUT_HEADER}<main id='login-main'><div id='login-box'>"
    "<input id='email' name='email'><input id='password' name='password' type='password'>"
    "<button id='go' type='button'>Sign in</button></div></main></body></html>"
)
_SWAPPED = (
    f"<html><body>{_SIGNED_IN_HEADER}<main id='dashboard'><h1 class='welcome'>Welcome"
    "</h1></main></body></html>"
)
_HELP_ARTICLE = (
    "<main id='help-article' class='article'><section id='account-faq'></section></main>"
)


class TestTheAfterReadNarrowsTheSuggestions:
    """The final logged-in page stays what 1.29.0 chose; a usable after-read of the
    last credential post only removes the candidates it does not also hold."""

    def test_it_narrows_the_page_the_run_stopped_on(self, tmp_path: Path) -> None:
        """The run stopped on /orders: only what /orders and the landing page share
        is suggested; /orders' own table is not."""
        entries = [_get("/login", _LOGIN), _post(tmp_path, "9.1", after=_after(_LANDING))]

        stopped = _digest(tmp_path / "plain", _without_after(entries), _ORDERS)
        assert stopped.success_candidates == ("a.signout-link", "#orders-table")

        result = _digest(tmp_path, entries, _ORDERS)
        assert result.success_candidates == ("a.signout-link",)
        payload = json.loads(render_endpoints_json(result))
        assert payload["login"]["success_candidates"] == ["a.signout-link"]

    def test_it_narrows_the_last_page_in_the_har(self, tmp_path: Path) -> None:
        entries = [
            _get("/login", _LOGIN),
            _post(tmp_path, "9.1", after=_after(_LANDING)),
            _get("/orders", _ORDERS, started="2026-09-10T10:00:30.000Z"),
        ]
        plain = _digest(tmp_path / "plain", _without_after(entries))
        assert plain.success_candidates == ("a.signout-link", "#orders-table")
        assert _digest(tmp_path, entries).success_candidates == ("a.signout-link",)

    def test_a_name_holding_another_posted_value_is_not_suggested(self, tmp_path: Path) -> None:
        """#330: the after-read replaces a name holding any value of the post but a
        token's (``next=dashboard``), even with a username field in the post, so the
        digest no longer suggests ``#dashboard``; the HAR alone still does."""
        creds = {**_CREDS, "next": "dashboard"}
        landing = (
            "<html><body><header><nav class='site-nav'>"
            "<a class='signout-link' href='/bye'>Sign out</a></nav></header>"
            "<main><div id='dashboard'></div></main></body></html>"
        )
        after = stored_dom_snapshot(
            landing, json.dumps(creds), "application/json", structure_only=True
        )
        assert '<div id="***REDACTED***">' in after
        post = _entry(
            "POST",
            f"{_HOST}/session",
            post_data=json.dumps(creds),
            started="2026-09-10T10:00:05.000Z",
        )
        (tmp_path / "snapshots").mkdir()
        (tmp_path / "snapshots" / "9.1.after.html").write_text(after)
        post["request"]["_domAfter"] = "snapshots/9.1.after.html"
        entries = [_get("/login", _LOGIN), post]
        plain = _digest(tmp_path / "plain", _without_after(entries), landing)
        assert plain.success_candidates == ("a.signout-link", "#dashboard")
        assert _digest(tmp_path, entries, landing).success_candidates == ("a.signout-link",)

    def test_it_never_replaces_the_final_page(self, tmp_path: Path) -> None:
        """A script login with no page load after it and no page source: 1.29.0
        suggested nothing, and the read alone suggests nothing either."""
        entries = [_get("/login", _LOGIN), _post(tmp_path, "9.1", after=_after(_LANDING))]
        assert _digest(tmp_path / "plain", _without_after(entries)).success_candidates == ()
        assert _digest(tmp_path, entries).success_candidates == ()

    def test_a_read_sharing_nothing_with_the_final_page_gives_none(self, tmp_path: Path) -> None:
        """The live probe's case 1 as recorded: the help page the run stopped on holds
        no header, so it and the read share nothing."""
        help_page = f"<html><body>{_HELP_ARTICLE}</body></html>"
        entries = [_get("/login", _SCRIPT_LOGIN), _post(tmp_path, "9.1", after=_after(_SWAPPED))]
        plain = _digest(tmp_path / "plain", _without_after(entries), help_page)
        assert plain.success_candidates == ("#account-faq", "#help-article", "main.article")
        assert _digest(tmp_path, entries, help_page).success_candidates == ()

    def test_a_script_login_then_a_help_page_gives_the_header(self, tmp_path: Path) -> None:
        """The probe's case 1 on a site whose help page carries the signed-in header:
        the header's selectors, and none of the help page's own. The article comes
        first in document order, so 1.29.0 ranks its keyword id ahead of the header."""
        help_page = f"<html><body>{_HELP_ARTICLE}{_SIGNED_IN_HEADER}</body></html>"
        entries = [_get("/login", _SCRIPT_LOGIN), _post(tmp_path, "9.1", after=_after(_SWAPPED))]
        plain = _digest(tmp_path / "plain", _without_after(entries), help_page)
        assert plain.success_candidates == ("#account-faq", "#account-nav", "#account-link")
        result = _digest(tmp_path, entries, help_page)
        assert result.success_candidates == ("#account-nav", "#account-link", "#logout-link")

    def test_the_read_holds_no_text_and_the_ranking_needs_none(self, tmp_path: Path) -> None:
        stored = _after(_LANDING)
        assert "Welcome" not in stored
        assert "Sign out" not in stored
        assert "alice" not in stored.lower()
        entries = [_get("/login", _LOGIN), _post(tmp_path, "9.1", after=stored)]
        assert _digest(tmp_path, entries, _LANDING).success_candidates == (
            "#account-menu",
            "a.signout-link",
            "#welcome",
        )

    def test_an_undetected_mfa_page_in_the_read_gives_no_mfa_selector(self, tmp_path: Path) -> None:
        """The read caught an MFA page no rule knows for a step, and no step POST
        followed it in the HAR (a push approval): the run stopped on the landing page,
        which shares nothing with the MFA page outside the logged-out header."""
        entries = [_get("/login", _LOGIN), _post(tmp_path, "9.1", after=_after(_PLAIN_MFA))]
        plain = _digest(tmp_path / "plain", _without_after(entries), _LANDING)
        assert plain.success_candidates == ("#account-menu", "a.signout-link", "#welcome")
        assert _digest(tmp_path, entries, _LANDING).success_candidates == ()

    def test_a_push_approval_page_promotes_no_shared_element(self, tmp_path: Path) -> None:
        """Toolkit round 2 I2: the read caught a push-approval page (no one-time-code
        input, no step POST, only a GET poll) that shares a generic toast container
        with the landing page. None of 1.29.0's three is on the read, and the shared
        container has no login marker, so nothing takes their places."""
        push = (
            "<html><body><main><div id='push-wait'><p>Approve the sign-in on your phone</p>"
            "<div id='toast-root'></div></div></main></body></html>"
        )
        landing = (
            "<html><body><header><nav class='site-nav'><div id='account-menu'>"
            "<a class='signout-link' href='/bye'>Sign out</a></div></nav></header>"
            "<main><div id='welcome'></div><div id='toast-root'></div></main></body></html>"
        )
        poll = _entry(
            "GET",
            f"{_HOST}/api/push/status",
            content_type="application/json",
            body="{}",
            started="2026-09-10T10:00:20.000Z",
        )
        entries = [_get("/login", _LOGIN), _post(tmp_path, "9.1", after=_after(push)), poll]
        plain = _digest(tmp_path / "plain", _without_after(entries), landing)
        assert plain.success_candidates == ("#account-menu", "a.signout-link", "#welcome")
        assert _digest(tmp_path, entries, landing).success_candidates == ()

    def test_a_selector_the_read_holds_twice_is_kept(self, tmp_path: Path) -> None:
        """The read only has to hold the element; the once-only rule is the final
        page's."""
        twice = _LANDING.replace(
            "<a class='signout-link' href='/bye'>Sign out</a>",
            "<a class='signout-link' href='/bye'>Sign out</a>"
            "<a class='signout-link' href='/bye'>Sign out</a>",
        )
        entries = [_get("/login", _LOGIN), _post(tmp_path, "9.1", after=_after(twice))]
        assert _digest(tmp_path, entries, _LANDING).success_candidates == (
            "#account-menu",
            "a.signout-link",
            "#welcome",
        )

    def test_a_step_sent_by_fetch_after_the_post_falls_back(self, tmp_path: Path) -> None:
        """The reviewers' SPA MFA: the code is verified by a fetch POST after the
        credential post, so the read may show that step and is not used."""
        entries = [
            _get("/login", _LOGIN),
            _post(tmp_path, "9.1", after=_after(_PLAIN_MFA)),
            _fetch_post("/api/mfa/verify", {"code": "123456"}),
        ]
        result = _digest(tmp_path, entries, _LANDING)
        assert result.success_candidates == ("#account-menu", "a.signout-link", "#welcome")
        assert not {"#mfa-challenge", "#code", "#verify"} & set(result.success_candidates)

    def test_a_fetch_post_that_is_no_step_keeps_the_read(self, tmp_path: Path) -> None:
        entries = [
            _get("/login", _LOGIN),
            _post(tmp_path, "9.1", after=_after(_LANDING)),
            _fetch_post("/api/cart", {"item": "42"}),
        ]
        assert _digest(tmp_path, entries, _ORDERS).success_candidates == ("a.signout-link",)

    @pytest.mark.parametrize(
        "after",
        [
            pytest.param(_LANDING.replace("<main>", "<main>" + _LOGIN), id="login-form"),
            pytest.param(_MFA, id="one-time-code"),
            pytest.param(_LOGIN, id="login-page"),
        ],
    )
    def test_a_read_that_is_still_a_login_falls_back(self, tmp_path: Path, after: str) -> None:
        entries = [_get("/login", _LOGIN), _post(tmp_path, "9.1", after=_after(after))]
        result = _digest(tmp_path, entries, _ORDERS)
        plain = _digest(tmp_path / "plain", _without_after(entries), _ORDERS)
        assert result.success_candidates == plain.success_candidates
        assert result.success_candidates == ("a.signout-link", "#orders-table")

    @pytest.mark.parametrize(
        ("creds", "login"),
        [
            pytest.param(
                {"j_username": "jsmith", "j_password": "hunter2hunter2"},
                "<form id='jsmith-login' action='/j_security_check' method='post'>"
                "<input id='j_username' name='j_username'>"
                "<input type='password' id='j_password' name='j_password'></form>",
                id="j_username",
            ),
            pytest.param(
                {"login": "login@example.com", "password": "hunter2hunter2"},
                "<form id='login-form' action='/login' method='post'>"
                "<input type='email' id='login' name='login' autocomplete='username'>"
                "<input type='password' id='password' name='password'></form>",
                id="local-part-is-the-field-name",
            ),
            pytest.param(
                {"user": "user@example.com", "password": "hunter2hunter2"},
                "<div role='dialog' aria-modal='true'><form action='/session'>"
                "<input type='email' id='user' name='user'>"
                "<input type='password' name='password'></form></div>",
                id="user",
            ),
        ],
    )
    def test_a_login_page_stored_by_the_read_is_still_a_login(
        self, tmp_path: Path, creds: dict[str, str], login: str
    ) -> None:
        """Ruling 2 of round 4: the read replaces every name that holds an account
        value, but a login page it stores is still found to be a login, so the digest
        falls back exactly as without the read."""
        body = json.dumps(creds)
        stored = stored_dom_snapshot(
            f"<html><body><main>{login}</main></body></html>",
            body,
            "application/json",
            structure_only=True,
        )
        assert extract_login_forms(stored, source="", snapshot=True)
        post = _entry(
            "POST", f"{_HOST}/session", post_data=body, started="2026-09-10T10:00:05.000Z"
        )
        (tmp_path / "snapshots").mkdir()
        (tmp_path / "snapshots" / "9.1.after.html").write_text(stored)
        post["request"]["_domAfter"] = "snapshots/9.1.after.html"
        entries = [_get("/login", _LOGIN), post]
        result = _digest(tmp_path, entries, _ORDERS)
        plain = _digest(tmp_path / "plain", _without_after(entries), _ORDERS)
        assert result.success_candidates == plain.success_candidates
        assert result.success_candidates

    def test_a_step_submitted_after_the_post_falls_back(self, tmp_path: Path) -> None:
        """The browser posted a step-shaped form after the credential post (an MFA code
        whose input names no one-time code): the read may be that step's page."""
        mfa = "<html><body><div id='account-verify'><input name='pin'></div></body></html>"
        entries = [
            _get("/login", _LOGIN),
            _post(tmp_path, "9.1", after=_after(mfa)),
            _get("/verify", mfa),
            _document_post("/verify", {"otp": "123456"}),
            _get("/orders", _ORDERS),
        ]
        result = _digest(tmp_path, entries)
        plain = _digest(tmp_path / "plain", _without_after(entries))
        assert result.success_candidates == plain.success_candidates
        assert "#account-verify" not in result.success_candidates

    def test_a_step_before_the_last_post_does_not_count(self, tmp_path: Path) -> None:
        """A failed attempt, a step-shaped post, then the good attempt: the step came
        before the last credential post, so that post's read still narrows."""
        entries = [
            _get("/login", _LOGIN),
            _post(tmp_path, "9.1"),
            _fetch_post("/api/mfa/verify", {"code": "123456"}),
            _post(tmp_path, "9.2", after=_after(_LANDING)),
        ]
        assert _digest(tmp_path, entries, _ORDERS).success_candidates == ("a.signout-link",)

    def test_only_the_last_credential_post_s_read_counts(self, tmp_path: Path) -> None:
        """A failed attempt, then a good one with no read: the first post's read is the
        page the failed attempt left, never the logged-in page."""
        failed_page = "<html><body><div id='account-error'></div></body></html>"
        entries = [
            _get("/login", _LOGIN),
            _post(tmp_path, "9.1", after=_after(failed_page)),
            _post(tmp_path, "9.2"),
        ]
        result = _digest(tmp_path, entries, _ORDERS)
        plain = _digest(tmp_path / "plain", _without_after(entries), _ORDERS)
        assert result.success_candidates == plain.success_candidates
        assert "#account-error" not in result.success_candidates

    def test_a_link_to_a_missing_file_is_no_read(self, tmp_path: Path) -> None:
        from graftpunk.har.parser import parse_har_file

        entries = [_get("/login", _LOGIN), _post(tmp_path, "9.1", after=_after(_LANDING))]
        (tmp_path / "snapshots" / "9.1.after.html").unlink()
        result = _digest(tmp_path, entries, _ORDERS)
        assert result.success_candidates == ("a.signout-link", "#orders-table")
        post = parse_har_file(tmp_path / "network.har").entries[1]
        assert (post.request.dom_after_file, post.request.dom_after) == (None, None)

    def test_a_link_leaving_the_run_is_not_read(self, tmp_path: Path) -> None:
        from graftpunk.har.parser import parse_har_file

        outside = tmp_path / "outside.html"
        outside.write_text(_after(_LANDING))
        post = _post(tmp_path / "run", "9.1")
        post["request"]["_domAfter"] = "../outside.html"
        result = _digest(tmp_path / "run", [_get("/login", _LOGIN), post], _ORDERS)
        assert result.success_candidates == ("a.signout-link", "#orders-table")
        parsed = parse_har_file(tmp_path / "run" / "network.har").entries[1]
        assert (parsed.request.dom_after_file, parsed.request.dom_after) == (None, None)

    def test_the_parser_reads_the_link(self, tmp_path: Path) -> None:
        from graftpunk.har.parser import parse_har_file

        stored = _after(_LANDING)
        har = _write_har(tmp_path, [_post(tmp_path, "9.1", after=stored)])
        (entry,) = parse_har_file(har).entries
        assert entry.request.dom_after_file == "snapshots/9.1.after.html"
        assert entry.request.dom_after == stored


# The runs the golden file holds the 1.29.0 output of: none carries _domAfter.
def _golden_runs(base: Path) -> dict[str, DigestSource]:
    runs: dict[str, tuple[list[dict], str]] = {
        "stopped-on-orders": ([_get("/login", _LOGIN), _post(base, "9.1")], _ORDERS),
        "landing-in-the-har": (
            [_get("/login", _LOGIN), _post(base, "9.1"), _get("/account", _LANDING)],
            "",
        ),
        "mfa-then-landing": (
            [
                _get("/login", _LOGIN),
                _post(base, "9.1"),
                _get("/verify", _MFA),
                _document_post("/verify", {"otp": "123456"}),
                _get("/account", _LANDING),
            ],
            _ORDERS,
        ),
        "stopped-on-a-login": ([_get("/login", _LOGIN), _post(base, "9.1")], _LOGIN),
        "no-logged-in-page": ([_get("/login", _LOGIN), _post(base, "9.1")], ""),
    }
    sources: dict[str, DigestSource] = {}
    for name, (entries, page_source) in runs.items():
        run = base / name
        run.mkdir(parents=True)
        har = _write_har(run, entries)
        source = None
        if page_source:
            source = run / "page-source.html"
            source.write_text(page_source)
        sources[name] = DigestSource(har_path=har, page_source=source)
    sources["sample"] = DigestSource(har_path=_FIXTURES / "sample.har")
    return sources


def golden_outputs(base: Path) -> dict[str, dict[str, str]]:
    """Every output the digest gives for each golden run, with *base* spelled
    ``<run>`` and the fixtures directory ``<fixtures>``: the markdown, the JSON, the
    endpoints projection, the success candidates, and the nodriver plugin the
    generator writes."""
    outputs: dict[str, dict[str, str]] = {}
    for name, source in _golden_runs(base).items():
        result = digest(source)
        plugin = render(
            ScaffoldSpec(
                name="myshop",
                mode="new_project",
                backend="nodriver",
                base_url=_HOST,
                digest=result,
            )
        )["src/graftpunk_myshop/plugin.py"]
        outputs[name] = {
            key: value.replace(str(base), "<run>").replace(str(_FIXTURES), "<fixtures>")
            for key, value in {
                "markdown": render_markdown(result),
                "json": render_json(result),
                "endpoints": render_endpoints_json(result),
                "success_candidates": json.dumps(list(result.success_candidates)),
                "plugin": plugin,
            }.items()
        }
    return outputs


def test_a_run_without_an_after_read_digests_as_1_29_0_did(tmp_path: Path) -> None:
    """The golden file was written by graftpunk 1.29.0 (main at cd8b06be) from these
    runs; every output must match it byte for byte."""
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    assert _without_failure_candidates(golden_outputs(tmp_path)) == golden


def _without_failure_candidates(outputs: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """*outputs* with the one key added since 1.29.0 (#268 part 3, within schema 1)
    taken out of the JSON (top level) and the endpoints projection (``login``) as
    text: its one line, an empty list, which a key sorted after it always follows.
    No golden run suggests any failure text, and every other byte, the JSON's
    formatting included, is compared as rendered."""
    for outputs_of_run in outputs.values():
        for key, indent in (("json", 2), ("endpoints", 4)):
            line = " " * indent + '"failure_candidates": [],\n'
            assert outputs_of_run[key].count(line) == 1, key
            outputs_of_run[key] = outputs_of_run[key].replace(line, "")
    return outputs
