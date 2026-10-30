"""Family templates for sibling letters-only path segments (#271, part 1).

Two or more recorded paths that differ in one letters-only segment, and agree on
everything else, are reported as one more endpoint with a placeholder beside
their literal rows. Nothing is removed or renamed: a run with no family digests,
projects, and scaffolds exactly as graftpunk 1.31.1 did, which the golden file
pins.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from graftpunk.cli.main import app as main_app
from graftpunk.cli.observe_commands import observe_app
from graftpunk.cli.scaffold_commands import plugin_app
from graftpunk.devtools.scaffold.render import ScaffoldSpec, fixture_paths, render
from graftpunk.devtools.scaffold.selection import CommandSelection
from graftpunk.har.digest import DigestSource, Endpoint, RunDigest, digest, family_values
from graftpunk.har.paths import FAMILY_PLACEHOLDER_RE, family_param_name
from graftpunk.har.report import render_endpoints_json, render_json, render_markdown
from graftpunk.testing import FixtureSession
from graftpunk.testing.sidecar import load_sidecar, sidecar_text
from tests.unit.cli_harness import strip_ansi
from tests.unit.test_har_digest import _entry, _write_har
from tests.unit.test_har_report import _ENDPOINT_V1, _FAMILY_ENDPOINT_V1

_HOST = "https://myshop.example.com"
_FIXTURES = Path(__file__).parent.parent / "fixtures"
_GOLDEN = _FIXTURES / "digest_without_families.json"
_RESULTS = json.dumps({"results": [{"name": "x"}], "total": 1})
_HITS = json.dumps({"hits": [], "page": 1})
_PAGE = "<html><body><p>page</p></body></html>"


def _get(path: str, body: str = _RESULTS, content_type: str = "application/json") -> dict:
    return _entry("GET", f"{_HOST}{path}", content_type=content_type, body=body)


# The runs the golden file holds the 1.31.1 output of: each is a near miss of a
# family, or holds none at all, so none of them gains a row.
def _golden_runs(base: Path) -> dict[str, tuple[DigestSource, tuple[CommandSelection, ...]]]:
    runs: dict[str, tuple[list[dict], tuple[CommandSelection, ...]]] = {
        "shapes-differ": (
            [_get("/search/north-region/items/"), _get("/search/south-region/items/", _HITS)],
            (CommandSelection("north", "GET", "/search/north-region/items/"),),
        ),
        "content-types-differ": (
            [
                _get("/search/north-region/items/"),
                _get("/search/south-region/items/", _PAGE, "text/html"),
            ],
            (),
        ),
        "methods-differ": (
            [
                _get("/search/north-region/items/"),
                _entry("POST", f"{_HOST}/search/south-region/items/", body=_RESULTS),
            ],
            (),
        ),
        "two-segments-differ": (
            [_get("/search/north-region/items/"), _get("/browse/south-region/items/")],
            (),
        ),
        "one-member": ([_get("/search/north-region/items/"), _get("/api/orders", _HITS)], ()),
        "file-extension": (
            [
                _get("/docs/intro.html", _PAGE, "text/html"),
                _get("/docs/setup.html", _PAGE, "text/html"),
            ],
            (),
        ),
        "fixed-vocabulary": ([_get("/api/items"), _get("/search/items")], ()),
        "one-segment": (
            [_get("/about", _PAGE, "text/html"), _get("/contact", _PAGE, "text/html")],
            (),
        ),
        "digit-bearing": ([_get("/shop/page2/items"), _get("/shop/page3/items")], ()),
        "trailing-slash-differs": (
            [_get("/search/north-region/items/"), _get("/search/south-region/items")],
            (),
        ),
    }
    sources: dict[str, tuple[DigestSource, tuple[CommandSelection, ...]]] = {}
    for name, (entries, commands) in runs.items():
        run = base / name
        run.mkdir(parents=True)
        sources[name] = (DigestSource(har_path=_write_har(run, entries)), commands)
    sources["sample"] = (
        DigestSource(har_path=_FIXTURES / "sample.har"),
        (CommandSelection("user-posts", "GET", "/api/users/{user_id}/posts"),),
    )
    return sources


def golden_outputs(base: Path) -> dict[str, dict[str, str]]:
    """Every output the digest gives for each golden run, with *base* spelled
    ``<run>`` and the fixtures directory ``<fixtures>``: the markdown, the JSON, the
    endpoints projection, and the plugin and test module the generator writes, by
    default and, where a run names one, for a literal ``--command``."""
    outputs: dict[str, dict[str, str]] = {}
    for name, (source, commands) in _golden_runs(base).items():
        result = digest(source)
        files: dict[str, str] = {
            "markdown": render_markdown(result),
            "json": render_json(result),
            "endpoints": render_endpoints_json(result),
        }
        for label, selected in (("default", ()), ("command", commands)):
            if label == "command" and not selected:
                continue
            rendered = render(
                ScaffoldSpec(
                    name="myshop",
                    mode="new_project",
                    backend="nodriver",
                    base_url=_HOST,
                    digest=result,
                    graftpunk_version="1.31.1",
                    commands=selected,
                )
            )
            files[f"{label}-plugin"] = rendered["src/graftpunk_myshop/plugin.py"]
            files[f"{label}-tests"] = rendered["tests/test_plugin.py"]
        outputs[name] = {
            key: value.replace(str(base), "<run>").replace(str(_FIXTURES), "<fixtures>")
            for key, value in files.items()
        }
    return outputs


def test_a_run_without_a_family_digests_as_1_31_1_did(tmp_path: Path) -> None:
    """The golden file was written by graftpunk 1.31.1 (main at 9946864b) from these
    runs; every output must match it byte for byte."""
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    assert golden_outputs(tmp_path) == golden


def _digest_of(tmp_path: Path, entries: list[dict]) -> RunDigest:
    return digest(DigestSource.from_har(_write_har(tmp_path, entries)))


def _families(tmp_path: Path, entries: list[dict]) -> list[Endpoint]:
    return [e for e in _digest_of(tmp_path, entries).endpoints if e.family_members]


_TWO_REGIONS = [_get("/search/north-region/items/"), _get("/search/south-region/items/")]
_FAMILY = "/search/{search_slug}/items/"


class TestFamilyMembership:
    def test_the_issue_s_two_regions_give_one_family_beside_the_literal_rows(
        self, tmp_path: Path
    ) -> None:
        result = _digest_of(tmp_path, _TWO_REGIONS)
        assert [e.template for e in result.endpoints] == [
            "/search/north-region/items/",
            "/search/south-region/items/",
            _FAMILY,
        ]
        family = result.endpoints[2]
        assert family.family_members == (
            "/search/north-region/items/",
            "/search/south-region/items/",
        )
        assert family.methods == ("GET",)
        assert family.count == 2
        assert family.content_type == "application/json"
        assert family_values(family) == ("north-region", "south-region")
        assert not family.login_flow
        assert not any(e.family_members for e in result.endpoints[:2])

    def test_three_members(self, tmp_path: Path) -> None:
        (family,) = _families(tmp_path, [*_TWO_REGIONS, _get("/search/east-region/items/")])
        assert family.template == _FAMILY
        assert family_values(family) == ("east-region", "north-region", "south-region")
        assert family.count == 3

    def test_a_single_member_gives_none(self, tmp_path: Path) -> None:
        assert _families(tmp_path, [_get("/search/north-region/items/")]) == []

    def test_two_recordings_of_one_path_give_none(self, tmp_path: Path) -> None:
        entries = [_get("/search/north-region/items/"), _get("/search/north-region/items/")]
        assert _families(tmp_path, entries) == []

    def test_members_differing_in_two_segments_give_none(self, tmp_path: Path) -> None:
        entries = [_get("/search/north-region/items/"), _get("/browse/south-region/items/")]
        assert _families(tmp_path, entries) == []

    def test_members_with_different_response_shapes_give_none(self, tmp_path: Path) -> None:
        entries = [_get("/search/north-region/items/"), _get("/search/south-region/items/", _HITS)]
        assert _families(tmp_path, entries) == []

    def test_members_with_different_content_types_give_none(self, tmp_path: Path) -> None:
        entries = [
            _get("/search/north-region/items/"),
            _get("/search/south-region/items/", _PAGE, "text/html"),
        ]
        assert _families(tmp_path, entries) == []

    def test_members_with_different_methods_give_none(self, tmp_path: Path) -> None:
        entries = [
            _get("/search/north-region/items/"),
            _entry("POST", f"{_HOST}/search/south-region/items/", body=_RESULTS),
        ]
        assert _families(tmp_path, entries) == []

    def test_html_pages_form_a_family(self, tmp_path: Path) -> None:
        entries = [
            _get("/search/north-region/items/", _PAGE, "text/html"),
            _get("/search/south-region/items/", _PAGE, "text/html"),
        ]
        (family,) = _families(tmp_path, entries)
        assert family.template == _FAMILY

    def test_a_path_fitting_two_families_joins_none(self, tmp_path: Path) -> None:
        """/shop/north/items differs from /shop/south/items in its second segment and
        from /shop/north/deals in its third: it is a member of neither family, and
        each of those is left with one member, so there is no family."""
        entries = [_get("/shop/north/items"), _get("/shop/south/items"), _get("/shop/north/deals")]
        assert _families(tmp_path, entries) == []

    def test_the_rest_of_a_family_stays_when_one_path_fits_two(self, tmp_path: Path) -> None:
        entries = [
            _get("/shop/north/items"),
            _get("/shop/south/items"),
            _get("/shop/east/items"),
            _get("/shop/north/deals"),
        ]
        (family,) = _families(tmp_path, entries)
        assert family.template == "/shop/{shop_slug}/items"
        assert family.family_members == ("/shop/east/items", "/shop/south/items")

    def test_a_segment_holding_a_digit_is_not_letters_only(self, tmp_path: Path) -> None:
        assert _families(tmp_path, [_get("/shop/page2/items"), _get("/shop/page3/items")]) == []

    def test_a_last_segment_with_a_file_extension_gives_none(self, tmp_path: Path) -> None:
        entries = [
            _get("/docs/intro.html", _PAGE, "text/html"),
            _get("/docs/setup.html", _PAGE, "text/html"),
        ]
        assert _families(tmp_path, entries) == []

    def test_a_fixed_vocabulary_segment_never_joins(self, tmp_path: Path) -> None:
        assert _families(tmp_path, [_get("/api/items"), _get("/search/items")]) == []
        entries = [_get("/api/items"), _get("/north/items"), _get("/south/items")]
        (family,) = _families(tmp_path, entries)
        assert family.template == "/{slug}/items"
        assert family.family_members == ("/north/items", "/south/items")

    def test_one_segment_paths_never_form_a_family(self, tmp_path: Path) -> None:
        entries = [_get("/about", _PAGE, "text/html"), _get("/contact", _PAGE, "text/html")]
        assert _families(tmp_path, entries) == []

    def test_a_trailing_slash_must_agree(self, tmp_path: Path) -> None:
        entries = [_get("/search/north-region/items/"), _get("/search/south-region/items")]
        assert _families(tmp_path, entries) == []

    def test_a_login_flow_endpoint_never_joins(self, tmp_path: Path) -> None:
        form = (
            '<form action="/account/session" method="post"><input type="email" name="email">'
            '<input type="password" name="password"></form>'
        )
        entries = [
            _get("/account/signin", form, "text/html"),
            _get("/account/profile", _PAGE, "text/html"),
            _get("/account/settings", _PAGE, "text/html"),
            _entry(
                "POST",
                f"{_HOST}/account/session",
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
        ]
        result = _digest_of(tmp_path, entries)
        assert any(e.login_flow and e.template == "/account/signin" for e in result.endpoints)
        (family,) = (e for e in result.endpoints if e.family_members)
        assert family.family_members == ("/account/profile", "/account/settings")

    def test_a_family_beside_an_id_placeholder(self, tmp_path: Path) -> None:
        entries = [_get("/stores/1001/north/items"), _get("/stores/1002/south/items")]
        (family,) = _families(tmp_path, entries)
        assert family.template == "/stores/{store_id}/{slug}/items"
        assert family_values(family) == ("north", "south")

    def test_the_fixture_value_is_the_fixture_recording_s(self, tmp_path: Path) -> None:
        entries = [_get("/search/south-region/items/"), _get("/search/north-region/items/")]
        (family,) = _families(tmp_path, entries)
        assert family.family_fixture_value == "south-region"
        assert family.fixture_entry_index == 0


class TestFamilyParamName:
    def test_the_singular_of_a_plain_word_plus_slug(self) -> None:
        assert family_param_name("categories") == "category_slug"
        assert family_param_name("search") == "search_slug"
        assert family_param_name("regions") == "region_slug"
        assert family_param_name("status") == "status_slug"

    def test_slug_when_no_plain_word_comes_before_it(self) -> None:
        assert family_param_name("") == "slug"
        assert family_param_name("{store_id}") == "slug"
        assert family_param_name("north-region") == "slug"
        assert family_param_name("v2") == "slug"

    def test_a_numeric_suffix_when_the_name_is_taken(self) -> None:
        assert family_param_name("", taken={"slug"}) == "slug2"
        assert family_param_name("", taken={"slug", "slug2"}) == "slug3"
        assert family_param_name("items", taken={"item_slug"}) == "item_slug2"

    def test_the_placeholder_pattern_matches_exactly_these_names(self) -> None:
        for name in ("slug", "slug2", "category_slug", "item_slug3"):
            assert FAMILY_PLACEHOLDER_RE.fullmatch("{" + name + "}"), name
        for name in ("order_id", "id", "slugs", "key", "1_slug"):
            assert not FAMILY_PLACEHOLDER_RE.fullmatch("{" + name + "}"), name


class TestWhereAFamilyShows:
    def test_the_markdown_lists_the_family_before_its_members(self, tmp_path: Path) -> None:
        text = render_markdown(_digest_of(tmp_path, _TWO_REGIONS))
        assert "- hosts: 1, endpoints: 2, families: 1\n" in text
        headers = [line for line in text.splitlines() if line.startswith("### ")]
        assert headers == [
            f"### GET {_FAMILY}",
            "### GET /search/north-region/items/",
            "### GET /search/south-region/items/",
        ]
        block = text.split(f"### GET {_FAMILY}\n\n", 1)[1].split("\n\n", 1)[0]
        assert block.splitlines()[0] == (
            "- family of 2 recorded templates, search_slug: north-region, south-region"
        )

    def test_the_markdown_limit_counts_the_family_row(self, tmp_path: Path) -> None:
        text = render_markdown(_digest_of(tmp_path, _TWO_REGIONS), limit=1)
        headers = [line for line in text.splitlines() if line.startswith("### ")]
        assert headers == [f"### GET {_FAMILY}"]
        assert "... and 2 more endpoint(s)" in text

    def test_the_projection_names_the_members_on_the_family_row_only(self, tmp_path: Path) -> None:
        payload = json.loads(render_endpoints_json(_digest_of(tmp_path, _TWO_REGIONS)))
        assert payload["schema"] == 1
        rows = payload["endpoints"]
        assert [row["template"] for row in rows] == [
            _FAMILY,
            "/search/north-region/items/",
            "/search/south-region/items/",
        ]
        assert rows[0]["family_members"] == [
            "/search/north-region/items/",
            "/search/south-region/items/",
        ]
        assert set(rows[0]) == _FAMILY_ENDPOINT_V1
        for row in rows[1:]:
            assert set(row) == _ENDPOINT_V1

    def test_the_json_carries_the_members_on_the_family_only(self, tmp_path: Path) -> None:
        payload = json.loads(render_json(_digest_of(tmp_path, _TWO_REGIONS)))
        members = [e.get("family_members") for e in payload["endpoints"]]
        assert members == [
            None,
            None,
            ["/search/north-region/items/", "/search/south-region/items/"],
        ]


def _spec(result: RunDigest, *commands: CommandSelection) -> ScaffoldSpec:
    return ScaffoldSpec(
        name="myshop",
        mode="new_project",
        backend="nodriver",
        base_url=_HOST,
        digest=result,
        graftpunk_version="1.31.1",
        commands=commands,
    )


class TestScaffoldingAFamily:
    def test_the_family_command_takes_the_placeholder(self, tmp_path: Path) -> None:
        result = _digest_of(tmp_path, _TWO_REGIONS)
        files = render(_spec(result, CommandSelection("region-items", "GET", _FAMILY)))
        plugin = files["src/graftpunk_myshop/plugin.py"]
        assert f'endpoint="GET {_FAMILY}"' in plugin
        assert "search_slug: str," in plugin
        assert '_quote_path(search_slug, safe="")' in plugin
        assert 'f"/search/{search_slug}/items/"' in plugin
        assert "north-region" not in plugin.split("def region_items(", 1)[1].split('"""', 2)[2]
        compile(plugin, "plugin.py", "exec")
        tests = files["tests/test_plugin.py"]
        assert 'search_slug="north-region"' in tests
        compile(tests, "test_plugin.py", "exec")

    def test_the_default_render_proposes_no_family(self, tmp_path: Path) -> None:
        result = _digest_of(tmp_path, _TWO_REGIONS)
        without = dataclasses.replace(
            result, endpoints=tuple(e for e in result.endpoints if not e.family_members)
        )
        assert render(_spec(result)) == render(_spec(without))

    def test_a_literal_command_is_unchanged_by_a_family(self, tmp_path: Path) -> None:
        result = _digest_of(tmp_path, _TWO_REGIONS)
        without = dataclasses.replace(
            result, endpoints=tuple(e for e in result.endpoints if not e.family_members)
        )
        literal = CommandSelection("north", "GET", "/search/north-region/items/")
        assert render(_spec(result, literal)) == render(_spec(without, literal))

    def test_fixture_paths_name_the_family_template(self, tmp_path: Path) -> None:
        result = _digest_of(tmp_path, _TWO_REGIONS)
        paths = fixture_paths(_spec(result, CommandSelection("region-items", "GET", _FAMILY)))
        assert paths == ["tests/fixtures/get_search_{search_slug}_items.json"]


class TestFixtureSessionAnswersAFamilyMember:
    def _write(self, directory: Path, name: str, body: str) -> None:
        (directory / name).write_text(body)

    def test_a_member_path_reads_the_family_fixture(self, tmp_path: Path) -> None:
        self._write(tmp_path, "get_search_{search_slug}_items.json", '{"total": 1}')
        session = FixtureSession(tmp_path)
        response = session.request("GET", f"{_HOST}/search/west-region/items/")
        assert response.status_code == 200
        assert response.json() == {"total": 1}

    def test_a_literal_fixture_wins_over_the_family(self, tmp_path: Path) -> None:
        self._write(tmp_path, "get_search_{search_slug}_items.json", '{"from": "family"}')
        self._write(tmp_path, "get_search_west-region_items.json", '{"from": "literal"}')
        response = FixtureSession(tmp_path).request("GET", f"{_HOST}/search/west-region/items/")
        assert response.json() == {"from": "literal"}

    def test_a_path_outside_the_family_still_answers_404(self, tmp_path: Path) -> None:
        self._write(tmp_path, "get_search_{search_slug}_items.json", "{}")
        session = FixtureSession(tmp_path)
        assert session.request("GET", f"{_HOST}/search/west-region/deals/").status_code == 404
        assert session.request("POST", f"{_HOST}/search/west-region/items/").status_code == 404
        assert session.request("GET", f"{_HOST}/search/a/b/items/").status_code == 404

    def test_an_id_placeholder_fixture_still_misses_a_slug(self, tmp_path: Path) -> None:
        """#213 is unchanged: only a family placeholder (``slug``, ``<word>_slug``)
        answers for another segment."""
        self._write(tmp_path, "get_products_{product_id}.json", "{}")
        response = FixtureSession(tmp_path).request("GET", f"{_HOST}/products/alpha-widget")
        assert response.status_code == 404


def _run_with_a_family(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> typer.Typer:
    monkeypatch.delenv("GRAFTPUNK_SESSION", raising=False)
    observe_base = tmp_path / "observe"
    monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
    run_dir = observe_base / "myshop" / "run-1"
    run_dir.mkdir(parents=True)
    entries = [
        _get("/search/north-region/items/", json.dumps({"results": [{"n": 1}], "total": 1})),
        _get("/search/south-region/items/", json.dumps({"results": [], "total": 0})),
    ]
    (run_dir / "network.har").write_text(
        json.dumps({"log": {"version": "1.2", "entries": entries}})
    )
    app = typer.Typer()
    app.add_typer(plugin_app)
    app.add_typer(observe_app)
    return app


class TestFixturesForAFamily:
    def _fixtures(self, app: typer.Typer, out: Path, *matches: str) -> str:
        argv = ["observe", "fixtures", "myshop", "--out", str(out)]
        for match in matches:
            argv += ["--match", match]
        result = CliRunner().invoke(app, argv)
        assert result.exit_code == 0, result.output
        return strip_ansi(result.output)

    def test_the_family_template_names_the_family_fixture(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _run_with_a_family(tmp_path, monkeypatch)
        out = tmp_path / "captures"
        self._fixtures(app, out, f"GET {_FAMILY}")
        written = sorted(p.name for p in out.iterdir() if not p.name.endswith(".meta.json"))
        assert written == [
            "get_search_{search_slug}_items#1.json",
            "get_search_{search_slug}_items.json",
        ]
        unsuffixed = json.loads((out / "get_search_{search_slug}_items.json").read_text())
        assert unsuffixed == {"results": [{"n": 1}], "total": 1}

    def test_a_glob_or_a_literal_template_names_literal_fixtures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _run_with_a_family(tmp_path, monkeypatch)
        out = tmp_path / "captures"
        self._fixtures(app, out, "GET /search/*", "GET /search/north-region/items/")
        written = sorted(p.name for p in out.iterdir() if not p.name.endswith(".meta.json"))
        assert written == [
            "get_search_north-region_items.json",
            "get_search_south-region_items.json",
        ]

    def test_the_family_and_a_member_both_named_write_both(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _run_with_a_family(tmp_path, monkeypatch)
        out = tmp_path / "captures"
        self._fixtures(app, out, "GET /search/south-region/items/", f"GET {_FAMILY}")
        written = sorted(p.name for p in out.iterdir() if not p.name.endswith(".meta.json"))
        assert written == [
            "get_search_south-region_items.json",
            "get_search_{search_slug}_items#1.json",
            "get_search_{search_slug}_items.json",
        ]

    def test_a_generated_family_command_passes_its_own_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gp plugin new --command <family template>, gp observe fixtures --match
        <family template> into the project, then the project's own pytest and ruff."""
        app = _run_with_a_family(tmp_path, monkeypatch)
        target = tmp_path / "out"
        result = CliRunner().invoke(
            app,
            [
                "plugin",
                "new",
                "myshop",
                "--from-run",
                "myshop",
                "--command",
                f"region-items=GET {_FAMILY}",
                "--dir",
                str(target),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "tests/fixtures/get_search_{search_slug}_items.json" in strip_ansi(result.output)
        fixtures_dir = target / "tests" / "fixtures"
        self._fixtures(app, fixtures_dir, f"GET {_FAMILY}")
        for meta in fixtures_dir.glob("*.meta.json"):
            meta.write_text(
                sidecar_text(dataclasses.replace(load_sidecar(meta), capture_sha256=None))
            )
        env = {**os.environ, "PYTHONPATH": str(target / "src")}
        tested = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
            cwd=target,
            capture_output=True,
            text=True,
            env=env,
        )
        assert tested.returncode == 0, tested.stdout + tested.stderr
        assert "2 passed" in tested.stdout, tested.stdout
        _assert_ruff_clean(target)

    def test_add_command_adds_a_family_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _run_with_a_family(tmp_path, monkeypatch)
        target = tmp_path / "out"
        new = ["plugin", "new", "myshop", "--from-run", "myshop", "--dir", str(target)]
        result = CliRunner().invoke(main_app, new)
        assert result.exit_code == 0, result.output
        added = CliRunner().invoke(
            main_app,
            [
                "plugin",
                "add-command",
                "myshop",
                "--from-run",
                "myshop",
                "--command",
                f"region-items=GET {_FAMILY}",
                "--dir",
                str(target),
            ],
        )
        assert added.exit_code == 0, added.output
        assert "tests/fixtures/get_search_{search_slug}_items.json" in strip_ansi(added.output)
        plugin = (target / "src" / "graftpunk_myshop" / "plugin.py").read_text()
        assert "def region_items(" in plugin
        assert "search_slug: str," in plugin
        _assert_ruff_clean(target)


def _assert_ruff_clean(project: Path) -> None:
    for argv in (["check", "."], ["format", "--check", "."]):
        linted = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [sys.executable, "-m", "ruff", *argv], cwd=project, capture_output=True, text=True
        )
        assert linted.returncode == 0, linted.stdout + linted.stderr
