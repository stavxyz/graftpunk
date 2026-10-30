"""The recorder's read of the page after a credential post settles (#268, part 2).

A credential post that carries ``request._domAfter`` links a structure-only read of
the page taken a few seconds after its response (``snapshots/<rid>.after.html``).
The digest takes the last credential post's read as the logged-in page for its
success suggestions, before ``page-source.html`` and the last page in the HAR,
unless that read is still a login (a login form, or a step of one). A run without
the key digests exactly as graftpunk 1.29.0 did, which the golden file pins.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from graftpunk.devtools.scaffold.render import ScaffoldSpec, render
from graftpunk.har.digest import DigestSource, digest
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


class TestTheAfterReadIsTheLoggedInPage:
    def test_it_wins_over_the_page_the_run_stopped_on(self, tmp_path: Path) -> None:
        """The run stopped on /orders: without the read only what /orders shows is
        suggested; with it, the landing page's markers are, and the one on both
        pages ranks first."""
        entries = [_get("/login", _LOGIN), _post(tmp_path, "9.1", after=_after(_LANDING))]

        stopped = _digest(tmp_path / "plain", _without_after(entries), _ORDERS)
        assert stopped.success_candidates == ("a.signout-link", "#orders-table")

        result = _digest(tmp_path, entries, _ORDERS)
        assert result.success_candidates == ("a.signout-link", "#account-menu", "#welcome")
        payload = json.loads(render_endpoints_json(result))
        assert payload["login"]["success_candidates"] == list(result.success_candidates)

    def test_it_wins_over_the_last_page_in_the_har(self, tmp_path: Path) -> None:
        entries = [
            _get("/login", _LOGIN),
            _post(tmp_path, "9.1", after=_after(_LANDING)),
            _get("/orders", _ORDERS, started="2026-09-10T10:00:30.000Z"),
        ]
        result = _digest(tmp_path, entries)
        assert result.success_candidates == ("a.signout-link", "#account-menu", "#welcome")

    def test_it_alone_is_a_logged_in_page(self, tmp_path: Path) -> None:
        """A script login with no page load after it and no page source: 1.29.0
        suggested nothing; the read is the logged-in page."""
        entries = [_get("/login", _LOGIN), _post(tmp_path, "9.1", after=_after(_LANDING))]
        assert _digest(tmp_path / "plain", _without_after(entries)).success_candidates == ()
        result = _digest(tmp_path, entries)
        assert result.success_candidates == ("#account-menu", "a.signout-link", "#welcome")

    def test_the_read_holds_no_text_and_the_ranking_needs_none(self, tmp_path: Path) -> None:
        stored = _after(_LANDING)
        assert "Welcome" not in stored
        assert "Sign out" not in stored
        assert "alice" not in stored.lower()
        entries = [_get("/login", _LOGIN), _post(tmp_path, "9.1", after=stored)]
        assert _digest(tmp_path, entries).success_candidates == (
            "#account-menu",
            "a.signout-link",
            "#welcome",
        )

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
        entries = [_get("/login", _LOGIN), _post(tmp_path, "9.1", after=_after(_LANDING))]
        (tmp_path / "snapshots" / "9.1.after.html").unlink()
        result = _digest(tmp_path, entries, _ORDERS)
        assert result.success_candidates == ("a.signout-link", "#orders-table")

    def test_a_link_leaving_the_run_is_not_read(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside.html"
        outside.write_text(_after(_LANDING))
        post = _post(tmp_path / "run", "9.1")
        post["request"]["_domAfter"] = "../outside.html"
        result = _digest(tmp_path / "run", [_get("/login", _LOGIN), post], _ORDERS)
        assert result.success_candidates == ("a.signout-link", "#orders-table")

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
    assert golden_outputs(tmp_path) == golden
