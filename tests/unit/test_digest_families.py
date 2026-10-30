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
import time
from collections.abc import Callable
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from graftpunk.cli.main import app as main_app
from graftpunk.cli.observe_commands import observe_app
from graftpunk.cli.scaffold_commands import plugin_app
from graftpunk.devtools.scaffold.render import ScaffoldSpec, fixture_paths, render
from graftpunk.devtools.scaffold.selection import CommandSelection
from graftpunk.har.digest import (
    DigestSource,
    Endpoint,
    RunDigest,
    digest,
    family_values,
)
from graftpunk.har.paths import family_param_name
from graftpunk.har.report import (
    endpoints_projection,
    render_endpoints_json,
    render_json,
    render_markdown,
)
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
    tmp_path.mkdir(parents=True, exist_ok=True)
    return digest(DigestSource.from_har(_write_har(tmp_path, entries)))


def _families(tmp_path: Path, entries: list[dict]) -> list[Endpoint]:
    return [e for e in _digest_of(tmp_path, entries).endpoints if e.family_members]


_TWO_REGIONS = [_get("/search/north-region/items/"), _get("/search/south-region/items/")]
_FAMILY = "/search/{search_slug}/items/"
# Two HTML pages and two JSON endpoints under one prefix: two candidate families,
# both /account/{account_slug}.
_ACCOUNT = [
    _get("/account/profile", _PAGE, "text/html"),
    _get("/account/settings", _PAGE, "text/html"),
    _get("/account/status", json.dumps({"balance": 1})),
    _get("/account/balance", json.dumps({"balance": 2})),
]


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

    def test_members_with_two_non_json_content_types_give_none(self, tmp_path: Path) -> None:
        """Neither body is JSON, so both shapes are ``("none",)``: only the content
        type tells them apart."""
        entries = [
            _get("/search/north-region/items/", _PAGE, "text/html"),
            _get("/search/south-region/items/", "plain text", "text/plain"),
        ]
        assert _families(tmp_path, entries) == []

    def test_members_on_two_hosts_give_none(self, tmp_path: Path) -> None:
        entries = [
            _get("/search/north-region/items/"),
            _entry(
                "GET", "https://api.myshop.example.com/search/south-region/items/", body=_RESULTS
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)), all_hosts=True)
        assert len(result.endpoints) == 2
        assert not any(e.family_members for e in result.endpoints)

    def test_each_method_forms_its_own_family(self, tmp_path: Path) -> None:
        """The digest files one endpoint per method and template, so GET and POST on
        the same two paths are four endpoints and two families, one per method."""
        entries = [
            *_TWO_REGIONS,
            _entry("POST", f"{_HOST}/search/north-region/items/", body=_RESULTS),
            _entry("POST", f"{_HOST}/search/south-region/items/", body=_RESULTS),
        ]
        result = _digest_of(tmp_path, entries)
        families = [e for e in result.endpoints if e.family_members]
        assert [(e.methods, e.template) for e in families] == [
            (("GET",), _FAMILY),
            (("POST",), _FAMILY),
        ]
        for family in families:
            assert family.count == 2
        # Each family comes just before the first member of its own method.
        expected = [
            f"GET {_FAMILY}",
            "GET /search/north-region/items/",
            f"POST {_FAMILY}",
            "POST /search/north-region/items/",
            "GET /search/south-region/items/",
            "POST /search/south-region/items/",
        ]
        headers = [line for line in render_markdown(result).splitlines() if line.startswith("### ")]
        assert headers == [f"### {row}" for row in expected]
        rows = endpoints_projection(result)["endpoints"]
        assert [f"{row['method']} {row['template']}" for row in rows] == expected

    def test_an_unreadable_json_body_never_joins(self, tmp_path: Path) -> None:
        """A JSON body too large to read and not parsing has an unavailable shape,
        which matches nothing, not even another unavailable one."""
        unreadable = '{"results": "' + "x" * (300 * 1024)
        entries = [
            _get("/search/north-region/items/", unreadable),
            _get("/search/south-region/items/", unreadable),
        ]
        result = _digest_of(tmp_path, entries)
        assert [e.shape.kind if e.shape else None for e in result.endpoints] == [
            "unavailable",
            "unavailable",
        ]
        assert not any(e.family_members for e in result.endpoints)

    def test_a_truncated_object_differs_from_one_with_the_same_kept_keys(
        self, tmp_path: Path
    ) -> None:
        """Twelve keys are kept: thirteen keys share the first twelve with twelve, and
        differ only in being cut."""
        twelve = json.dumps({f"k{i:02d}": i for i in range(12)})
        thirteen = json.dumps({f"k{i:02d}": i for i in range(13)})
        entries = [
            _get("/search/north-region/items/", twelve),
            _get("/search/south-region/items/", thirteen),
        ]
        result = _digest_of(tmp_path, entries)
        assert [e.shape.truncated for e in result.endpoints if e.shape] == [False, True]
        assert not any(e.family_members for e in result.endpoints)

    def test_arrays_of_different_element_kinds_give_none(self, tmp_path: Path) -> None:
        entries = [
            _get("/search/north-region/items/", "[1]"),
            _get("/search/south-region/items/", '[{"a": 1}]'),
        ]
        assert _families(tmp_path, entries) == []
        same = [
            _get("/search/north-region/items/", "[1]"),
            _get("/search/south-region/items/", "[2]"),
        ]
        (family,) = _families(tmp_path / "same", same)
        assert family.template == _FAMILY

    def test_a_non_ascii_segment_is_not_letters_only(self, tmp_path: Path) -> None:
        entries = [_get("/shop/caf\u00e9/items"), _get("/shop/th\u00e9/items")]
        result = _digest_of(tmp_path, entries)
        assert [e.template for e in result.endpoints] == [
            "/shop/caf\u00e9/items",
            "/shop/th\u00e9/items",
        ]
        assert not any(e.family_members for e in result.endpoints)

    def test_values_differing_only_in_case_are_one_word(self, tmp_path: Path) -> None:
        assert _families(tmp_path, [_get("/Items/x/y"), _get("/items/x/y")]) == []
        entries = [
            _get("/shop/North/items"),
            _get("/shop/north/items"),
            _get("/shop/south/items"),
            _get("/shop/east/items"),
        ]
        (family,) = _families(tmp_path / "four", entries)
        assert family.family_members == ("/shop/east/items", "/shop/south/items")

    def test_two_families_with_one_method_and_template_give_none(self, tmp_path: Path) -> None:
        """HTML pages and JSON endpoints under one prefix would both be
        ``/account/{account_slug}``: the template cannot name both, so neither row
        is reported, and the four literal rows stay."""
        result = _digest_of(tmp_path, _ACCOUNT)
        assert [e.template for e in result.endpoints] == [
            "/account/balance",
            "/account/profile",
            "/account/settings",
            "/account/status",
        ]
        shapes = [
            _get("/search/north/items/"),
            _get("/search/south/items/"),
            _get("/search/east/items/", _HITS),
            _get("/search/west/items/", _HITS),
        ]
        assert _families(tmp_path / "shapes", shapes) == []

    def test_one_family_per_method_and_template(self, tmp_path: Path) -> None:
        """A family whose template no other family shares is kept beside two that
        are dropped for sharing one."""
        result = _digest_of(tmp_path, [*_ACCOUNT, *_TWO_REGIONS])
        keys = [(e.methods, e.template) for e in result.endpoints]
        assert len(keys) == len(set(keys))
        assert [e.template for e in result.endpoints if e.family_members] == [_FAMILY]

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

    def test_a_segment_quoting_would_change_never_joins(self, tmp_path: Path) -> None:
        """The generated stub quotes its parameter with ``quote(value, safe="")``,
        which turns ``$`` into ``%24``: its test would request a path no member's
        fixture is named for."""
        entries = [_get("/search/north$region/items/"), _get("/search/south$region/items/")]
        assert _families(tmp_path, entries) == []

    def test_an_empty_part_is_not_letters_only(self, tmp_path: Path) -> None:
        for values in (("north--region", "south--region"), ("-north", "-south")):
            entries = [_get(f"/r/{value}/items") for value in values]
            assert _families(tmp_path / values[0], entries) == [], values

    def test_a_dot_in_a_middle_segment_joins(self, tmp_path: Path) -> None:
        (family,) = _families(tmp_path, [_get("/docs/a.b/items"), _get("/docs/c.d/items")])
        assert family.template == "/docs/{doc_slug}/items"
        assert family_values(family) == ("a.b", "c.d")

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

    def test_a_routing_word_in_upper_case_never_joins(self, tmp_path: Path) -> None:
        entries = [_get("/x/API/items"), _get("/x/STATIC/items")]
        assert _families(tmp_path / "upper", entries) == []
        entries = [_get("/API/items"), _get("/north/items"), _get("/south/items")]
        (family,) = _families(tmp_path / "mixed", entries)
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

    def test_a_plural_whose_singular_is_not_y_keeps_a_word(self) -> None:
        assert family_param_name("series") == "series_slug"
        assert family_param_name("species") == "species_slug"
        assert family_param_name("movies") == "movie_slug"
        assert family_param_name("cookies") == "cookie_slug"
        assert family_param_name("stories") == "story_slug"

    def test_a_numeric_suffix_when_the_name_is_taken(self) -> None:
        assert family_param_name("", taken={"slug"}) == "slug2"
        assert family_param_name("", taken={"slug", "slug2"}) == "slug3"
        assert family_param_name("items", taken={"item_slug"}) == "item_slug2"


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

    def test_the_markdown_limit_counts_literal_rows_only(self, tmp_path: Path) -> None:
        """A family row is shown with the first of its members and never takes a
        literal row's place; the footer counts literal endpoints and family rows
        apart, as the summary line does."""
        text = render_markdown(_digest_of(tmp_path, _TWO_REGIONS), limit=1)
        headers = [line for line in text.splitlines() if line.startswith("### ")]
        assert headers == [f"### GET {_FAMILY}", "### GET /search/north-region/items/"]
        assert "... and 1 more endpoint(s) (raise --limit to see them)" in text

    def test_the_footer_names_family_rows_left_out(self, tmp_path: Path) -> None:
        entries = [_get("/api/orders", _HITS), _get("/api/orders", _HITS), *_TWO_REGIONS]
        text = render_markdown(_digest_of(tmp_path, entries), limit=1)
        assert "- hosts: 1, endpoints: 3, families: 1\n" in text
        headers = [line for line in text.splitlines() if line.startswith("### ")]
        assert headers == ["### GET /api/orders"]
        assert (
            "... and 2 more endpoint(s), 1 more family row(s) (raise --limit to see them)"
        ) in text

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

    def test_a_family_row_comes_just_before_its_first_member(self, tmp_path: Path) -> None:
        """``/zzz`` was recorded twice and each member once, so the family (count 2)
        and ``/zzz`` tie on count; sorting every row together would put the family
        first by template, and the rule puts it beside its members."""
        entries = [_get("/zz/north/items"), _get("/zz/south/items"), _get("/zzz"), _get("/zzz")]
        result = _digest_of(tmp_path, entries)
        expected = ["/zzz", "/zz/{zz_slug}/items", "/zz/north/items", "/zz/south/items"]
        assert [row["template"] for row in endpoints_projection(result)["endpoints"]] == expected
        headers = [line for line in render_markdown(result).splitlines() if line.startswith("### ")]
        assert headers == [f"### GET {template}" for template in expected]

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
        comment = " ".join(
            " ".join(
                line.strip().removeprefix("#").strip()
                for line in plugin.split("def region_items(", 1)[1].splitlines()
                if line.strip().startswith("#")
            ).split()
        )
        assert (
            f"{_FAMILY} stands for 2 recorded templates that differ only in the segment "
            "search_slug fills (gp observe digest lists them); search_slug is a neutral "
            "name: rename it here and in its test to what that segment names."
        ) in comment
        compile(plugin, "plugin.py", "exec")
        tests = files["tests/test_plugin.py"]
        assert 'search_slug="north-region"' in tests
        compile(tests, "test_plugin.py", "exec")

    def test_a_family_beside_an_id_placeholder_fills_its_own_argument(self, tmp_path: Path) -> None:
        entries = [_get("/stores/1001/north/items"), _get("/stores/1002/south/items")]
        result = _digest_of(tmp_path, entries)
        family = "/stores/{store_id}/{slug}/items"
        files = render(_spec(result, CommandSelection("store-items", "GET", family)))
        plugin = files["src/graftpunk_myshop/plugin.py"]
        comment = " ".join(
            line.strip().removeprefix("#").strip()
            for line in plugin.split("def store_items(", 1)[1].splitlines()
            if line.strip().startswith("#")
        )
        assert "differ only in the segment slug fills" in " ".join(comment.split())
        tests = files["tests/test_plugin.py"]
        call = " ".join(tests.split("result = plugin.store_items(", 1)[1].split(")", 1)[0].split())
        assert call == 'ctx, store_id="1001", slug="north"'

    @pytest.mark.parametrize("family_first", [True, False])
    def test_a_family_and_its_fixture_member_both_get_a_test(
        self, tmp_path: Path, family_first: bool
    ) -> None:
        """Both tests issue the member's recorded request and read its one fixture."""
        result = _digest_of(tmp_path, _TWO_REGIONS)
        commands = [
            CommandSelection("region-items", "GET", _FAMILY),
            CommandSelection("north", "GET", "/search/north-region/items/"),
        ]
        spec = _spec(result, *(commands if family_first else commands[::-1]))
        tests = render(spec)["tests/test_plugin.py"]
        assert "def test_region_items() -> None:" in tests
        assert "def test_north() -> None:" in tests
        assert "GP-FILL: no test" not in tests
        assert fixture_paths(spec) == ["tests/fixtures/get_search_north-region_items.json"]

    def test_a_family_sharing_a_stem_with_another_request_loses_its_test(
        self, tmp_path: Path
    ) -> None:
        """``/x_north/items`` names its fixture as the family's member
        ``/x/north/items`` does, but answers another request: the second test would
        read the first one's fixture, so it is left out with a note."""
        entries = [_get("/x/north/items"), _get("/x/south/items"), _get("/x_north/items")]
        result = _digest_of(tmp_path, entries)
        spec = _spec(
            result,
            CommandSelection("x-items", "GET", "/x/{x_slug}/items"),
            CommandSelection("x-north", "GET", "/x_north/items"),
        )
        tests = render(spec)["tests/test_plugin.py"]
        assert "def test_x_items() -> None:" in tests
        assert "def test_x_north() -> None:" not in tests
        assert "# GP-FILL: no test for x_north (GET /x_north/items): its fixture would" in tests

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

    def test_two_literal_commands_on_one_endpoint_render_as_before(self, tmp_path: Path) -> None:
        """Only a family command shares its member's fixture: two literal commands on
        one endpoint keep the earlier rule, one test and a note."""
        result = _digest_of(tmp_path, _TWO_REGIONS)
        north = "/search/north-region/items/"
        spec = _spec(
            result, CommandSelection("north", "GET", north), CommandSelection("n", "GET", north)
        )
        tests = render(spec)["tests/test_plugin.py"]
        assert "def test_north() -> None:" in tests
        assert "def test_n() -> None:" not in tests
        assert f"# GP-FILL: no test for n (GET {north}): its fixture would share" in tests

    def test_fixture_paths_name_the_fixture_member_s_own_file(self, tmp_path: Path) -> None:
        result = _digest_of(tmp_path, _TWO_REGIONS)
        paths = fixture_paths(_spec(result, CommandSelection("region-items", "GET", _FAMILY)))
        assert paths == ["tests/fixtures/get_search_north-region_items.json"]


class TestFixtureSessionIsUnchanged:
    """FixtureSession answers a request only from the file named for its own
    template, as before family rows existed: a file whose name holds a family
    placeholder answers no other path."""

    def test_no_other_segment_value_reads_a_placeholder_fixture(self, tmp_path: Path) -> None:
        for name in (
            "get_products_{product_slug}.json",
            "get_products_{product_id}.json",
            "get_search_{search_slug}_items.json",
        ):
            (tmp_path / name).write_text("{}")
        session = FixtureSession(tmp_path)
        for path in (
            "/products/red-widget-99",
            "/search/1001/items/",
            "/search/api/items/",
            "/search/west-region/items/",
        ):
            assert session.request("GET", f"{_HOST}{path}").status_code == 404, path


def _run_with_a_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entries: list[dict] | None = None
) -> typer.Typer:
    monkeypatch.delenv("GRAFTPUNK_SESSION", raising=False)
    observe_base = tmp_path / "observe"
    monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
    run_dir = observe_base / "myshop" / "run-1"
    run_dir.mkdir(parents=True)
    if entries is None:
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

    def test_the_family_template_writes_each_member_under_its_own_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _run_with_a_family(tmp_path, monkeypatch)
        out = tmp_path / "captures"
        output = self._fixtures(app, out, f"GET {_FAMILY}")
        assert "No entries matched" not in output
        written = sorted(p.name for p in out.iterdir() if not p.name.endswith(".meta.json"))
        assert written == [
            "get_search_north-region_items.json",
            "get_search_south-region_items.json",
        ]
        north = json.loads((out / "get_search_north-region_items.json").read_text())
        assert north == {"results": [{"n": 1}], "total": 1}

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

    def test_the_family_and_a_member_both_named_write_each_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _run_with_a_family(tmp_path, monkeypatch)
        out = tmp_path / "captures"
        self._fixtures(app, out, "GET /search/south-region/items/", f"GET {_FAMILY}")
        written = sorted(p.name for p in out.iterdir() if not p.name.endswith(".meta.json"))
        assert written == [
            "get_search_north-region_items.json",
            "get_search_south-region_items.json",
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
        assert "tests/fixtures/get_search_north-region_items.json" in strip_ansi(result.output)
        self._fixtures(app, target / "tests" / "fixtures", f"GET {_FAMILY}")
        assert "2 passed" in _project_pytest(target)
        _assert_ruff_clean(target)

    @pytest.mark.parametrize("family_first", [True, False])
    def test_a_family_and_its_fixture_member_s_command_both_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, family_first: bool
    ) -> None:
        """A family command and a literal command for its fixture member read that
        member's one fixture, so the project runs both tests, in either order."""
        app = _run_with_a_family(tmp_path, monkeypatch)
        target = tmp_path / "out"
        commands = [f"region-items=GET {_FAMILY}", "north=GET /search/north-region/items/"]
        argv = ["plugin", "new", "myshop", "--from-run", "myshop", "--dir", str(target)]
        for command in commands if family_first else commands[::-1]:
            argv += ["--command", command]
        result = CliRunner().invoke(app, argv)
        assert result.exit_code == 0, result.output
        tests = (target / "tests" / "test_plugin.py").read_text()
        assert "GP-FILL: no test" not in tests
        fixtures_dir = target / "tests" / "fixtures"
        self._fixtures(app, fixtures_dir, f"GET {_FAMILY}")
        assert "3 passed" in _project_pytest(target)

    def test_a_dollar_value_forms_no_family_and_its_literal_flow_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``north$region`` would be quoted to ``north%24region`` by a family stub, so
        it forms no family: the family template is no endpoint, and the literal
        commands' project passes its own tests."""
        entries = [_get("/search/north$region/items/"), _get("/search/south$region/items/")]
        app = _run_with_a_family(tmp_path, monkeypatch, entries)
        projected = CliRunner().invoke(app, ["observe", "digest", "myshop", "--endpoints-json"])
        assert projected.exit_code == 0, projected.output
        assert "family_members" not in projected.output
        target = tmp_path / "out"
        new = ["plugin", "new", "myshop", "--from-run", "myshop", "--dir", str(target)]
        refused = CliRunner().invoke(app, [*new, "--command", f"fam=GET {_FAMILY}"])
        assert refused.exit_code != 0
        assert not target.exists()
        result = CliRunner().invoke(app, new)
        assert result.exit_code == 0, result.output
        self._fixtures(app, target / "tests" / "fixtures", "GET /search/*")
        assert "3 passed" in _project_pytest(target)

    def test_a_stem_collision_through_a_family_match_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entries = [_get("/x/north/items"), _get("/x/south/items"), _get("/x_north/items")]
        app = _run_with_a_family(tmp_path, monkeypatch, entries)
        out = tmp_path / "captures"
        argv = ["observe", "fixtures", "myshop", "--out", str(out)]
        argv += ["--match", "GET /x/{x_slug}/items", "--match", "GET /x_north/items"]
        result = CliRunner().invoke(app, argv)
        assert result.exit_code == 1, result.output
        assert (
            "Refusing to write get_x_north_items: GET /x/north/items and GET /x_north/items "
            "would share that fixture name"
        ) in " ".join(strip_ansi(result.output).split())

    def test_two_families_on_one_template_leave_the_literal_flow_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reviewers' reproduction: HTML pages and JSON endpoints under
        /account. No family row is reported, the members' fixtures are written
        without a collision, and the default project passes its own tests."""
        app = _run_with_a_family(tmp_path, monkeypatch, _ACCOUNT)
        projected = CliRunner().invoke(app, ["observe", "digest", "myshop", "--endpoints-json"])
        assert projected.exit_code == 0, projected.output
        assert "family_members" not in projected.output
        target = tmp_path / "out"
        new = ["plugin", "new", "myshop", "--from-run", "myshop", "--dir", str(target)]
        result = CliRunner().invoke(app, new)
        assert result.exit_code == 0, result.output
        fixtures_dir = target / "tests" / "fixtures"
        output = self._fixtures(app, fixtures_dir, "GET /account/*", "GET /account/{account_slug}")
        assert "Refusing" not in output
        assert "No entries matched --match GET /account/{account_slug}." in output
        written = sorted(
            p.name for p in fixtures_dir.glob("get_*") if not p.name.endswith(".meta.json")
        )
        assert written == [
            "get_account_balance.json",
            "get_account_profile.html",
            "get_account_settings.html",
            "get_account_status.json",
        ]
        assert "5 passed" in _project_pytest(target)

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
        assert "tests/fixtures/get_search_north-region_items.json" in strip_ansi(added.output)
        plugin = (target / "src" / "graftpunk_myshop" / "plugin.py").read_text()
        assert "def region_items(" in plugin
        assert "search_slug: str," in plugin
        _assert_ruff_clean(target)


def _entries_with_families(pairs: int) -> list[dict]:
    """*pairs* families of two HTML pages each, ``/<w>/<w>/north/items`` and
    ``/<w>/<w>/south/items``: the word repeats so that no two pairs share a
    family at either of its positions."""
    entries: list[dict] = []
    for i in range(pairs):
        word = ""
        n = i + 26 * 26
        while n:
            n, r = divmod(n, 26)
            word = "abcdefghijklmnopqrstuvwxyz"[r] + word
        for value in ("north", "south"):
            entries.append(_get(f"/{word}/{word}/{value}/items", _PAGE, "text/html"))
    return entries


def _cpu_seconds(work: Callable[[], object]) -> float:
    """The least CPU time of three runs of *work*: CPU time, never wall clock, and
    the least of three, so a busy machine slows neither side of a ratio."""
    times = []
    for _ in range(3):
        start = time.process_time()
        work()
        times.append(time.process_time() - start)
    return min(times)


def test_family_rows_cost_linear_time(tmp_path: Path) -> None:
    """Eight times the endpoints (and families) within 32 times the CPU time, for
    the digest, the markdown, and the projection together. Ordering family rows
    by a list scan was quadratic, 64 times at this ratio, and cubic in its
    equality checks."""
    sources = {}
    for pairs in (25, 200):
        run = tmp_path / str(pairs)
        run.mkdir()
        sources[pairs] = DigestSource.from_har(_write_har(run, _entries_with_families(pairs)))
    assert sum(1 for e in digest(sources[200]).endpoints if e.family_members) == 200

    def everything(pairs: int) -> Callable[[], object]:
        def work() -> object:
            result = digest(sources[pairs])
            return render_markdown(result, limit=10**6), endpoints_projection(result)

        return work

    small, large = _cpu_seconds(everything(25)), _cpu_seconds(everything(200))
    assert large < 32 * small, (small, large)


def _project_pytest(target: Path) -> str:
    """Run the generated project's own pytest on fixtures ``gp observe fixtures``
    wrote into it (their capture hashes cleared, as the existing gates do), and
    return its output; the run must pass."""
    for meta in (target / "tests" / "fixtures").glob("*.meta.json"):
        meta.write_text(sidecar_text(dataclasses.replace(load_sidecar(meta), capture_sha256=None)))
    env = {**os.environ, "PYTHONPATH": str(target / "src")}
    tested = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
        cwd=target,
        capture_output=True,
        text=True,
        env=env,
    )
    assert tested.returncode == 0, tested.stdout + tested.stderr
    return tested.stdout


def _assert_ruff_clean(project: Path) -> None:
    for argv in (["check", "."], ["format", "--check", "."]):
        linted = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [sys.executable, "-m", "ruff", *argv], cwd=project, capture_output=True, text=True
        )
        assert linted.returncode == 0, linted.stdout + linted.stderr
