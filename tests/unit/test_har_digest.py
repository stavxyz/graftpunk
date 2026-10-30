"""RunDigest over synthetic HARs built in tmp_path (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import random
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import pytest
from structlog.testing import capture_logs

from graftpunk.devtools.scaffold.render import ScaffoldSpec, render
from graftpunk.har.digest import (
    _BODY_SAMPLE_THRESHOLD,
    _DYNAMIC_MAJORITY,
    _FORM_CONTENT_TYPE,
    _HIGH_CARDINALITY_THRESHOLD,
    _LOGIN_WINDOW,
    _MAX_ENDPOINT_EXAMPLES,
    _MAX_FIELD_NAME_LEN,
    _SHAPE_MAX_DEPTH,
    _SHAPE_MAX_KEYS,
    SHAPE_UNAVAILABLE,
    DigestSource,
    Endpoint,
    LoginObservation,
    ObservationKind,
    _account_values,
    _form_state,
    _login_opener,
    _parse_body,
    _scalar_values,
    _with_login_flow,
    body_params,
    body_values,
    digest,
    endpoint_template,
    flagged_names_of,
    flagged_reasons_of,
    redacted_names_of,
)
from graftpunk.har.documents import LoginForm
from graftpunk.har.parser import LoginInteraction, parse_har_file
from graftpunk.har.paths import reads_as_words
from graftpunk.har.report import render_endpoints_json, render_json, render_markdown
from tests.unit.guide_harness import REPO_ROOT


def _entry(
    method: str,
    url: str,
    *,
    status: int = 200,
    content_type: str = "application/json",
    body: str = "{}",
    request_headers: dict[str, str] | None = None,
    response_headers: dict[str, str] | None = None,
    post_data: str | None = None,
    set_cookies: list[str] | None = None,
    started: str = "2026-09-10T10:00:00.000Z",
) -> dict:
    resp_headers = [{"name": "Content-Type", "value": content_type}]
    for name, value in (response_headers or {}).items():
        resp_headers.append({"name": name, "value": value})
    for cookie in set_cookies or []:
        resp_headers.append({"name": "Set-Cookie", "value": cookie})
    entry = {
        "startedDateTime": started,
        "time": 5,
        "request": {
            "method": method,
            "url": url,
            "headers": [{"name": k, "value": v} for k, v in (request_headers or {}).items()],
            "cookies": [],
            "queryString": [],
        },
        "response": {
            "status": status,
            "statusText": "OK",
            "headers": resp_headers,
            "cookies": [],
            "content": {"mimeType": content_type, "text": body, "size": len(body)},
        },
    }
    if post_data is not None:
        entry["request"]["postData"] = {"mimeType": "application/json", "text": post_data}
    return entry


def _write_har(tmp_path: Path, entries: list[dict], name: str = "network.har") -> Path:
    har_path = tmp_path / name
    har_path.write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}))
    return har_path


def _nested_dict(levels: int) -> dict[str, Any]:
    """A dict nested `levels` deep: {"level0": {"level1": {...: "too deep"}}}."""
    value: Any = "too deep"
    for i in reversed(range(levels)):
        value = {f"level{i}": value}
    return value


class TestPrimaryHostAndHosts:
    def test_primary_host_is_most_common_non_static(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://api.myshop.example.com/orders/1"),
            _entry(
                "GET",
                "https://ads.tracker.example.net/pixel.gif",
                content_type="image/gif",
                body="",
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.primary_host == "api.myshop.example.com"

    def test_a_document_host_outranks_a_busier_beacon_host(self, tmp_path: Path) -> None:
        """A page-driven site answers its own pages and serves the rest as
        assets, so a telemetry endpoint can out-count it on non-static requests
        alone."""
        entries = [
            _entry(
                "GET",
                "https://myshop.example.com/records/search",
                content_type="text/html; charset=utf-8",
                body="<html><body>results</body></html>",
            ),
            _entry(
                "GET",
                "https://myshop.example.com/records/1",
                content_type="text/html; charset=utf-8",
                body="<html><body>one</body></html>",
            ),
        ]
        entries += [_entry("POST", f"https://sessions.telemetry.example.net/{i}") for i in range(5)]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.primary_host == "myshop.example.com"
        assert result.dropped["third_party"] == 5

    def test_an_html_error_page_does_not_make_a_host_a_document_host(self, tmp_path: Path) -> None:
        """Only a served page counts: a beacon host answering one HTML 500
        must not join the document group and then win on count."""
        entries = [
            _entry(
                "GET",
                "https://myshop.example.com/records/search",
                content_type="text/html; charset=utf-8",
                body="<html><body>results</body></html>",
            ),
            _entry(
                "POST",
                "https://sessions.telemetry.example.net/collect",
                status=500,
                content_type="text/html; charset=utf-8",
                body="<html><body>server error</body></html>",
            ),
        ]
        entries += [_entry("POST", f"https://sessions.telemetry.example.net/{i}") for i in range(4)]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.primary_host == "myshop.example.com"

    def test_an_api_only_run_still_elects_on_count_alone(self, tmp_path: Path) -> None:
        """Nothing served HTML, so the document half of the rule decides
        nothing and the busiest non-static host wins as it always did."""
        entries = [_entry("GET", "https://api.myshop.example.com/orders")]
        entries += [_entry("GET", f"https://api.other.example.org/t/{i}") for i in range(3)]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.primary_host == "api.other.example.org"

    def test_hosts_dict_counts_every_host(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry(
                "GET", "https://cdn.myshop.example.com/logo.png", content_type="image/png", body=""
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.hosts["api.myshop.example.com"] == 1
        assert result.hosts["cdn.myshop.example.com"] == 1


class TestStaticAndThirdPartyExclusion:
    def test_static_extension_is_dropped(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry(
                "GET",
                "https://api.myshop.example.com/app.js",
                content_type="application/javascript",
                body="",
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["static"] == 1
        assert not any(e.template == "/app.js" for e in result.endpoints)

    def test_image_content_type_is_static_regardless_of_url(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry(
                "GET",
                "https://api.myshop.example.com/generated-thumb",
                content_type="image/jpeg",
                body="",
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["static"] == 1

    def test_a_hashed_asset_with_an_unlisted_extension_is_static(self, tmp_path: Path) -> None:
        """The extension list caught .js and .css but not ._hs, so a hashed
        hyperscript asset became an endpoint, a command stub, and a generated
        test."""
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry(
                "GET",
                "https://api.myshop.example.com/vendor/custom.4a0ba46ee0b6b964b44b2909b6._hs",
                content_type="text/hyperscript",
                body="on click log 'hi'",
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["static"] == 1
        assert all("vendor" not in e.template for e in result.endpoints)

    def test_a_content_type_parameter_does_not_hide_a_static_type(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry(
                "GET",
                "https://api.myshop.example.com/bundle",
                content_type="text/css; charset=utf-8",
                body="body{}",
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["static"] == 1

    def test_a_json_response_under_an_unusual_path_is_kept(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry(
                "GET",
                "https://api.myshop.example.com/vendor/custom.4a0ba46ee0b6b964b44b2909b6._hs",
                content_type="application/json",
                body='{"ok": true}',
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["static"] == 0
        assert any("vendor" in e.template for e in result.endpoints)

    def test_an_html_page_is_kept(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry(
                "GET",
                "https://api.myshop.example.com/records/search",
                content_type="text/html; charset=utf-8",
                body="<html><body>results</body></html>",
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["static"] == 0
        templates = {e.template for e in result.endpoints}
        assert "/records/search" in templates

    def test_a_first_party_analytics_path_is_kept(self, tmp_path: Path) -> None:
        """The word analytics names a tracker host, not a path: matched anywhere
        in the URL it dropped the primary host's own reporting endpoint."""
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://api.myshop.example.com/api/analytics/summary"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert "/api/analytics/summary" in templates
        assert result.dropped["static"] == 0

    def test_an_asset_host_is_still_dropped(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://cdn.example.com/app.js", content_type="text/plain", body="x=1"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["static"] == 1
        assert all("app.js" not in e.template for e in result.endpoints)

    def test_a_tracker_host_is_still_dropped(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://analytics.example.net/collect"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["static"] == 1

    def test_a_tracking_pixel_path_segment_is_dropped(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://api.myshop.example.com/pixel"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["static"] == 1

    def test_a_segment_merely_containing_an_excluded_word_is_kept(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://api.myshop.example.com/pixelate-image"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert "/pixelate-image" in templates
        assert result.dropped["static"] == 0

    def test_third_party_host_dropped_without_all_hosts(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://api.myshop.example.com/orders/2"),
            _entry("GET", "https://other.example.org/data"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["third_party"] == 1
        assert all(e.host == "api.myshop.example.com" for e in result.endpoints)

    def test_a_deep_primary_host_does_not_widen_the_scope(self, tmp_path: Path) -> None:
        """Taking the last two labels made every host sharing them a first
        party, which under a two-label public suffix pulls in the whole
        country-code domain. The scope root is the primary host's parent
        instead: team.example.com here, so other.example.com is third party."""
        entries = [
            _entry("GET", "https://shop.team.example.com/orders"),
            _entry("GET", "https://shop.team.example.com/orders/2"),
            _entry("GET", "https://api.team.example.com/data"),
            _entry("GET", "https://other.example.com/data"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.primary_host == "shop.team.example.com"
        hosts = {e.host for e in result.endpoints}
        assert hosts == {"shop.team.example.com", "api.team.example.com"}
        assert result.dropped["third_party"] == 1

    def test_a_two_label_primary_host_keeps_its_own_subtree(self, tmp_path: Path) -> None:
        # "other." rather than "cdn."/"assets.": those are static-exclusion
        # patterns, and this test is about the scope rule, not that one.
        entries = [
            _entry("GET", "https://www.example.com/orders"),
            _entry("GET", "https://www.example.com/orders/2"),
            _entry("GET", "https://api.example.com/data"),
            _entry("GET", "https://other.example.net/data"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.primary_host == "www.example.com"
        hosts = {e.host for e in result.endpoints}
        assert hosts == {"www.example.com", "api.example.com"}
        assert result.dropped["third_party"] == 1

    def test_all_hosts_models_every_host(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://api.myshop.example.com/orders/2"),
            _entry("GET", "https://other.example.org/data"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)), all_hosts=True)
        assert any(e.host == "other.example.org" for e in result.endpoints)
        assert result.dropped["third_party"] == 0


class TestNonHttpSchemes:
    """A capture taken before the first navigation holds the browser's own
    new-tab page, whose entries are not HTTP at all."""

    def _mixed_har(self, tmp_path: Path) -> Path:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "chrome://new-tab-page/", content_type="text/html", body="<html>"),
            _entry("GET", "chrome-untrusted://theme/colors.css", content_type="text/css", body=""),
            _entry("GET", "data:image/png;base64,AAAA", content_type="image/png", body=""),
            _entry("GET", "blob:https://api.myshop.example.com/1234", body="{}"),
        ]
        return _write_har(tmp_path, entries)

    def test_non_http_entries_are_dropped_as_other_scheme(self, tmp_path: Path) -> None:
        result = digest(DigestSource.from_har(self._mixed_har(tmp_path)))
        assert result.dropped["other_scheme"] == 4

    def test_a_pseudo_host_never_reaches_the_host_counts(self, tmp_path: Path) -> None:
        result = digest(DigestSource.from_har(self._mixed_har(tmp_path)))
        assert set(result.hosts) == {"api.myshop.example.com"}
        assert result.primary_host == "api.myshop.example.com"

    def test_a_non_http_entry_is_never_an_endpoint(self, tmp_path: Path) -> None:
        result = digest(DigestSource.from_har(self._mixed_har(tmp_path)))
        assert [e.template for e in result.endpoints] == ["/orders"]

    def test_the_scheme_rule_runs_before_the_static_rule(self, tmp_path: Path) -> None:
        """A non-HTTP entry counts once, under its own reason, even when its
        content type would also have made it static."""
        result = digest(DigestSource.from_har(self._mixed_har(tmp_path)))
        assert result.dropped["static"] == 0
        assert result.dropped["third_party"] == 0


class TestTypeObservation:
    def test_query_param_int_type(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/orders?page=1")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        endpoint = result.endpoints[0]
        assert endpoint.query_params["page"] == "int"

    def test_query_param_bool_type(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/orders?archived=true")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].query_params["archived"] == "bool"

    def test_body_param_types_on_post(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/orders",
                post_data=json.dumps({"quantity": 3, "gift": True, "note": "hi"}),
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        endpoint = result.endpoints[0]
        assert endpoint.body_params["quantity"] == "int"
        assert endpoint.body_params["gift"] == "bool"
        assert endpoint.body_params["note"] == "str"

    def test_a_json_float_is_float_not_int(self, tmp_path: Path) -> None:
        """An int-typed option refuses --amount 12.5, so a float is its own type."""
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/orders",
                post_data=json.dumps({"amount": 12.5, "quantity": 3, "gift": True}),
            )
        ]
        endpoint = digest(DigestSource.from_har(_write_har(tmp_path, entries))).endpoints[0]
        assert endpoint.body_params == {"amount": "float", "quantity": "int", "gift": "bool"}

    def test_a_query_float_is_float(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/orders?min_total=12.5")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].query_params["min_total"] == "float"

    @pytest.mark.parametrize(
        "value",
        ["07030", "007", "+5", " 5", "1_000", "12.50", "1e5", "inf", "nan", "True", "FALSE"],
    )
    def test_a_value_is_typed_only_when_the_typed_value_spells_it_the_same(
        self, tmp_path: Path, value: str
    ) -> None:
        """A postal code 07030 typed int would be sent as 7030; a bool is sent as
        lowercase true/false, so a recorded True stays text."""
        entries = [_entry("GET", f"https://api.myshop.example.com/orders?code={value}")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].query_params["code"] == "str"

    @pytest.mark.parametrize(
        ("value", "expected"), [("-5", "int"), ("0", "int"), ("-0.5", "float")]
    )
    def test_a_value_that_round_trips_keeps_its_type(
        self, tmp_path: Path, value: str, expected: str
    ) -> None:
        entries = [_entry("GET", f"https://api.myshop.example.com/orders?code={value}")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].query_params["code"] == expected

    def test_query_types_that_disagree_across_requests_fall_back_to_str(
        self, tmp_path: Path
    ) -> None:
        """Seen as abc then 1: the last request used to win, and the command then
        refused abc."""
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders?q=abc&page=1"),
            _entry("GET", "https://api.myshop.example.com/orders?q=1&page=2"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].query_params == {"q": "str", "page": "int"}

    def test_a_query_key_that_does_not_read_as_a_field_name_is_dropped(
        self, tmp_path: Path
    ) -> None:
        """The body-key rule applies to query keys too: an email-shaped or digit
        key is data, and it reached the projection and generated source."""
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/orders?alice@example.com&4111111111111111=1&page=2",
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].query_params == {"page": "int"}
        assert "alice@example.com" not in render_endpoints_json(result)

    @staticmethod
    def _json_posts(tmp_path: Path, *bodies: dict) -> Endpoint:
        entries = [
            _entry("POST", "https://api.myshop.example.com/orders", post_data=json.dumps(body))
            for body in bodies
        ]
        (endpoint,) = digest(DigestSource.from_har(_write_har(tmp_path, entries))).endpoints
        return endpoint

    @staticmethod
    def _form_posts(tmp_path: Path, *bodies: str) -> Endpoint:
        entries = []
        for body in bodies:
            entry = _entry("POST", "https://api.myshop.example.com/orders", post_data=body)
            entry["request"]["postData"]["mimeType"] = _FORM_CONTENT_TYPE
            entries.append(entry)
        (endpoint,) = digest(DigestSource.from_har(_write_har(tmp_path, entries))).endpoints
        return endpoint

    def test_a_query_key_holding_an_id_is_dropped(self, tmp_path: Path) -> None:
        """A key is a field name, not a place for an account's id to ride along."""
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/orders"
                "?u_40912873=1&k_ab12cd34ef56=2&page=1&sha256=x&v2=y",
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].query_params == {"page": "int", "sha256": "str", "v2": "str"}

    def test_a_body_key_holding_an_id_is_dropped(self, tmp_path: Path) -> None:
        endpoint = self._json_posts(tmp_path, {"acct_40912873": 1, "ab12cd34ef56": 2, "note": "x"})
        assert endpoint.body_params == {"note": "str"}

    def test_a_form_key_holding_an_id_is_dropped_and_the_body_is_still_a_form(
        self, tmp_path: Path
    ) -> None:
        endpoint = self._form_posts(tmp_path, "u_40912873=1&name=alice")
        assert endpoint.body_kind == "form"
        assert endpoint.body_params == {"name": "str"}

    def test_json_int_then_float_merges_to_float(self, tmp_path: Path) -> None:
        endpoint = self._json_posts(tmp_path, {"amount": 3}, {"amount": 3.5})
        assert endpoint.body_params == {"amount": "float"}

    def test_a_json_null_is_not_a_type_observation(self, tmp_path: Path) -> None:
        endpoint = self._json_posts(tmp_path, {"amount": None}, {"amount": 3})
        assert endpoint.body_params == {"amount": "int"}

    def test_a_field_only_ever_null_is_not_declared(self, tmp_path: Path) -> None:
        endpoint = self._json_posts(tmp_path, {"amount": None, "note": "x"})
        assert endpoint.body_params == {"note": "str"}

    def test_json_text_then_number_is_mixed(self, tmp_path: Path) -> None:
        """No one type sends both, so the generator must not declare it."""
        endpoint = self._json_posts(tmp_path, {"ref": "a"}, {"ref": 3})
        assert endpoint.body_params == {"ref": "mixed"}

    def test_json_mixed_is_final(self, tmp_path: Path) -> None:
        endpoint = self._json_posts(tmp_path, {"ref": "a"}, {"ref": 3}, {"ref": "b"})
        assert endpoint.body_params == {"ref": "mixed"}

    def test_form_int_then_text_is_str(self, tmp_path: Path) -> None:
        endpoint = self._form_posts(tmp_path, "qty=3", "qty=abc")
        assert endpoint.body_params == {"qty": "str"}

    def test_a_json_object_is_object(self, tmp_path: Path) -> None:
        endpoint = self._json_posts(tmp_path, {"address": {"city": "x"}})
        assert endpoint.body_params == {"address": "object"}

    def test_a_json_array_records_its_element_type(self, tmp_path: Path) -> None:
        endpoint = self._json_posts(
            tmp_path,
            {"ids": [1, 2], "tags": ["a"], "prices": [1, 2.5], "mix": [1, "a"], "none": []},
        )
        assert endpoint.body_params == {
            "ids": "list[int]",
            "tags": "list[str]",
            "prices": "list[float]",
            "mix": "list[mixed]",
            "none": "list[unknown]",
        }

    def test_json_array_element_types_merge_across_requests(self, tmp_path: Path) -> None:
        endpoint = self._json_posts(
            tmp_path, {"ids": [], "codes": [1]}, {"ids": [4], "codes": ["a"]}
        )
        assert endpoint.body_params == {"ids": "list[int]", "codes": "list[mixed]"}

    def test_a_json_array_and_a_scalar_are_mixed(self, tmp_path: Path) -> None:
        endpoint = self._json_posts(tmp_path, {"ids": [1]}, {"ids": 1})
        assert endpoint.body_params == {"ids": "mixed"}

    def test_a_repeated_query_key_records_its_element_type(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders?id=1&id=2&tag=a&tag=1"),
            _entry("GET", "https://api.myshop.example.com/orders?id=3"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].query_params == {"id": "list[int]", "tag": "list[str]"}

    def test_a_repeated_form_key_records_its_element_type(self, tmp_path: Path) -> None:
        endpoint = self._form_posts(tmp_path, "id=1&id=2")
        assert endpoint.body_params == {"id": "list[int]"}


class TestBodyParams:
    """The one owner of body-param parsing: digest()'s endpoint accumulation
    and the fixtures sidecar (Task 7) both call this directly
    (validation net-negative, addressed 2026-09-12)."""

    def test_json_body_field_types(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/orders",
                post_data=json.dumps({"quantity": 3, "gift": True}),
            )
        ]
        (entry,) = parse_har_file(_write_har(tmp_path, entries)).entries
        assert body_params(entry) == {"quantity": "int", "gift": "bool"}

    def test_form_encoded_body_field_types(self, tmp_path: Path) -> None:
        entry_dict = _entry("POST", "https://api.myshop.example.com/login")
        entry_dict["request"]["postData"] = {
            "mimeType": "application/x-www-form-urlencoded",
            "text": "username=alice&remember=true",
        }
        (entry,) = parse_har_file(_write_har(tmp_path, [entry_dict])).entries
        assert body_params(entry) == {"username": "str", "remember": "bool"}

    def test_no_body_is_empty(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/orders")]
        (entry,) = parse_har_file(_write_har(tmp_path, entries)).entries
        assert body_params(entry) == {}

    def test_a_json_key_that_does_not_read_as_a_field_name_is_dropped(self, tmp_path: Path) -> None:
        """A JSON object keyed by data (an email address used as a map key, a
        session id) is not a field name, held to the same rule
        (_plausible_field_name) a form body's keys already were: reporting the
        key itself put the data into the digest report and the fixtures sidecar."""
        long_hex_key = "a" * 128
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/orders",
                post_data=json.dumps(
                    {
                        "quantity": 3,
                        "alice@example.com": {"role": "owner"},
                        long_hex_key: "planted",
                    }
                ),
            )
        ]
        (entry,) = parse_har_file(_write_har(tmp_path, entries)).entries
        assert body_params(entry) == {"quantity": "int"}


class TestNonFormBodiesHaveNoFieldNames:
    """parse_qs returns the whole text as one key for anything that is not a
    form, which put an XML credential post's entire body (values included) into
    Endpoint.body_params and everything downstream of it."""

    @staticmethod
    def _parsed(tmp_path: Path, *, mime_type: str, text: str) -> tuple[dict[str, str], str]:
        entry_dict = _entry("POST", "https://api.myshop.example.com/submit")
        entry_dict["request"]["postData"] = {"mimeType": mime_type, "text": text}
        (entry,) = parse_har_file(_write_har(tmp_path, [entry_dict])).entries
        return _parse_body(entry)

    def test_json_object_is_a_json_body_with_field_names(self, tmp_path: Path) -> None:
        types, kind = self._parsed(
            tmp_path, mime_type="application/json", text=json.dumps({"quantity": 3})
        )
        assert (types, kind) == ({"quantity": "int"}, "json")

    def test_json_array_is_a_json_body_with_no_field_names(self, tmp_path: Path) -> None:
        types, kind = self._parsed(
            tmp_path, mime_type="application/json", text=json.dumps([{"sku": "a"}, {"sku": "b"}])
        )
        assert (types, kind) == ({}, "json")

    def test_json_scalar_is_a_json_body_with_no_field_names(self, tmp_path: Path) -> None:
        types, kind = self._parsed(tmp_path, mime_type="application/json", text='"just-a-string"')
        assert (types, kind) == ({}, "json")

    def test_an_xml_credential_post_yields_nothing(self, tmp_path: Path) -> None:
        xml = "<login><account>SECRET-ACCT-99</account><password>hunter2</password></login>"
        types, kind = self._parsed(tmp_path, mime_type="application/xml", text=xml)
        assert (types, kind) == ({}, "none")

    def test_plain_text_yields_nothing(self, tmp_path: Path) -> None:
        types, kind = self._parsed(
            tmp_path, mime_type="text/plain", text="a sentence with no equals sign"
        )
        assert (types, kind) == ({}, "none")

    def test_a_form_body_declared_as_something_else_is_not_read_as_a_form(
        self, tmp_path: Path
    ) -> None:
        types, kind = self._parsed(tmp_path, mime_type="text/plain", text="username=alice")
        assert (types, kind) == ({}, "none")

    def test_a_field_name_past_the_cap_disqualifies_the_body(self, tmp_path: Path) -> None:
        long_name = "f" * (_MAX_FIELD_NAME_LEN + 1)
        types, kind = self._parsed(
            tmp_path, mime_type=_FORM_CONTENT_TYPE, text=f"{long_name}=value"
        )
        assert (types, kind) == ({}, "none")

    def test_a_field_name_at_the_cap_is_accepted(self, tmp_path: Path) -> None:
        # Not hex: 64 f's is a hex token, which the id rule drops.
        name = "z" * _MAX_FIELD_NAME_LEN
        types, kind = self._parsed(tmp_path, mime_type=_FORM_CONTENT_TYPE, text=f"{name}=value")
        assert (types, kind) == ({name: "str"}, "form")

    def test_an_xml_credential_post_is_not_a_login_observation(self, tmp_path: Path) -> None:
        """No field names means no password hint: the digest reports no
        credential post rather than one whose 'field name' is the body."""
        entry_dict = _entry("POST", "https://api.myshop.example.com/submit")
        entry_dict["request"]["postData"] = {
            "mimeType": "application/xml",
            "text": "<login><account>SECRET-ACCT-99</account><password>hunter2</password></login>",
        }
        result = digest(DigestSource.from_har(_write_har(tmp_path, [entry_dict])))
        assert not any(o.kind == "credential_post" for o in result.login)
        assert "SECRET-ACCT-99" not in repr(result)


class TestShapeNode:
    def test_object_shape_has_children(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/orders/1",
                body=json.dumps({"id": 1, "total": 9.5, "paid": True}),
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        shape = result.endpoints[0].shape
        assert shape is not None
        assert shape.kind == "object"
        assert shape.children is not None
        assert shape.children["id"].kind == "number"
        assert shape.children["paid"].kind == "boolean"

    def test_depth_beyond_the_max_is_truncated(self, tmp_path: Path) -> None:
        nested = _nested_dict(_SHAPE_MAX_DEPTH + 1)
        entries = [_entry("GET", "https://api.myshop.example.com/nested", body=json.dumps(nested))]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        shape = result.endpoints[0].shape
        assert shape is not None
        node = shape
        for i in range(_SHAPE_MAX_DEPTH):
            assert node is not None
            assert node.children is not None
            node = node.children[f"level{i}"]
        assert node.truncated is True

    def test_more_than_12_keys_is_truncated(self, tmp_path: Path) -> None:
        wide = {f"key{i}": i for i in range(_SHAPE_MAX_KEYS + 3)}
        entries = [_entry("GET", "https://api.myshop.example.com/wide", body=json.dumps(wide))]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        shape = result.endpoints[0].shape
        assert shape is not None
        assert shape.truncated is True
        assert len(shape.children) == _SHAPE_MAX_KEYS

    def test_a_json_body_over_the_threshold_still_reports_its_shape(self, tmp_path: Path) -> None:
        """The body is parsed whole. Parsing a fixed-size prefix of it could
        never succeed, so every large body reported non-JSON."""
        padding = "x" * _BODY_SAMPLE_THRESHOLD
        body = json.dumps({"id": 1, "padding": padding})
        assert len(body.encode("utf-8")) > _BODY_SAMPLE_THRESHOLD
        entries = [_entry("GET", "https://api.myshop.example.com/big", body=body)]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        shape = result.endpoints[0].shape
        assert shape is not None
        assert shape.kind == "object"
        assert shape.children is not None
        assert shape.children["id"].kind == "number"

    def test_an_unparseable_body_over_the_threshold_is_unavailable_not_non_json(
        self, tmp_path: Path
    ) -> None:
        truncated = '{"orders": [' + '{"id": 1},' * 40000
        assert len(truncated.encode("utf-8")) > _BODY_SAMPLE_THRESHOLD
        entries = [_entry("GET", "https://api.myshop.example.com/big", body=truncated)]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].shape == SHAPE_UNAVAILABLE

    def test_a_small_unparseable_json_body_is_still_shapeless(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/broken", body="not json at all")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].shape is None

    def test_non_json_response_has_no_shape(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/page",
                content_type="text/html",
                body="<html></html>",
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].shape is None


class TestCustomHeaders:
    def test_non_standard_header_name_reported(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/orders",
                request_headers={"X-Shop-Client": "web", "Accept": "application/json"},
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert "X-Shop-Client" in result.endpoints[0].custom_headers
        assert "Accept" not in result.endpoints[0].custom_headers

    def test_header_values_never_appear_anywhere_in_the_model(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/orders",
                request_headers={"X-Shop-Client": "super-secret-build-id-42"},
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert "super-secret-build-id-42" not in repr(result)


class TestLoginObservations:
    def test_form_page_observation(self, tmp_path: Path) -> None:
        """A page carrying a login form is the form page when a credential post
        follows it."""
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/login",
                content_type="text/html",
                body='<form><input type="password" name="pw"></form>',
            ),
            _entry("POST", "https://api.myshop.example.com/login", post_data='{"pw": "x"}'),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.login[0].kind == "form_page"

    def test_credential_post_lists_field_names_never_values(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/login",
                post_data=json.dumps({"email": "alice@example.com", "password": "hunter2"}),
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (observation,) = [o for o in result.login if o.kind == "credential_post"]
        assert set(observation.fields) == {"email", "password"}
        assert "hunter2" not in repr(result)

    def test_redirect_within_window_after_credential_post_is_an_observation(
        self, tmp_path: Path
    ) -> None:
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/login",
                post_data=json.dumps({"password": "x"}),
            ),
            _entry("GET", "https://api.myshop.example.com/dashboard", status=302, body=""),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert any(o.kind == "redirect" for o in result.login)

    def test_a_redirecting_credential_post_carries_its_target(self, tmp_path: Path) -> None:
        """The common shape: the POST itself answers 302, so it is the only record."""
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/login",
                status=302,
                body="",
                post_data=json.dumps({"password": "x"}),
                response_headers={"Location": "https://api.myshop.example.com/dashboard?welcome=1"},
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (observation,) = [o for o in result.login if o.kind == "credential_post"]
        assert observation.redirect_to == "/dashboard"

    def test_a_following_redirect_carries_its_own_target(self, tmp_path: Path) -> None:
        """Each hop of the chain records where it sent the client next."""
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/login",
                status=302,
                body="",
                post_data=json.dumps({"password": "x"}),
                response_headers={"Location": "/auth/callback"},
            ),
            _entry(
                "GET",
                "https://api.myshop.example.com/auth/callback",
                status=302,
                body="",
                response_headers={"Location": "/dashboard"},
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        targets = [o.redirect_to for o in result.login]
        assert targets == ["/auth/callback", "/dashboard"]

    def test_a_non_redirect_observation_carries_no_target(self, tmp_path: Path) -> None:
        """A 200 has nowhere it sent the client, whatever headers it carries."""
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/login",
                post_data=json.dumps({"password": "x"}),
                response_headers={"Location": "/dashboard"},
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (observation,) = [o for o in result.login if o.kind == "credential_post"]
        assert observation.redirect_to == ""

    def test_redirect_outside_window_is_not_an_observation(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/login",
                post_data=json.dumps({"password": "x"}),
            ),
            *[
                _entry("GET", f"https://api.myshop.example.com/item/{i}")
                for i in range(_LOGIN_WINDOW + 1)
            ],
            _entry("GET", "https://api.myshop.example.com/elsewhere", status=302, body=""),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert not any(o.kind == "redirect" for o in result.login)

    def test_set_cookie_within_window_is_an_observation(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/login",
                post_data=json.dumps({"password": "x"}),
            ),
            _entry(
                "GET",
                "https://api.myshop.example.com/dashboard",
                set_cookies=["session_id=abc123; Path=/; HttpOnly"],
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (observation,) = [o for o in result.login if o.kind == "set_cookie"]
        assert observation.fields == ("session_id",)

    def test_auth_api_path_is_an_observation(self, tmp_path: Path) -> None:
        entries = [_entry("POST", "https://api.myshop.example.com/api/auth", post_data="{}")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert any(o.kind == "auth_api" for o in result.login)

    def test_an_auth_word_inside_a_longer_segment_is_not_an_observation(
        self, tmp_path: Path
    ) -> None:
        """/token matched as a bare substring, so /api/tokens/list was labelled
        auth_api."""
        entries = [_entry("GET", "https://api.myshop.example.com/api/tokens/list")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.login == ()

    def test_an_auth_word_ending_a_segment_is_an_observation(self, tmp_path: Path) -> None:
        entries = [_entry("POST", "https://api.myshop.example.com/oauth/token", post_data="{}")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert any(o.kind == "auth_api" for o in result.login)

    def test_unrelated_asset_load_is_not_a_login_observation(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/products")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.login == ()


class TestTokenCandidatePairing:
    def test_header_and_meta_both_counted(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/dashboard",
                content_type="text/html",
                body='<meta name="csrf-token" content="abc">',
                request_headers={"X-CSRF-Token": "abc"},
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        kinds = {c.kind for c in result.tokens}
        assert "header" in kinds
        assert "meta" in kinds


class TestRunLevelCookies:
    def test_multiple_set_cookie_headers_both_names_appear(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/dashboard",
                set_cookies=[
                    "session_id=abc123; Path=/; HttpOnly",
                    "csrf_token=xyz; Path=/",
                ],
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert "session_id" in result.cookies
        assert "csrf_token" in result.cookies


class TestDigestSourceFromHar:
    def test_bare_har_has_no_run_metadata(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/x")]
        source = DigestSource.from_har(_write_har(tmp_path, entries))
        assert source.session is None
        assert source.bodies_dir is None
        assert source.page_source is None

    def test_run_dir_picks_up_bodies_and_page_source(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "myshop" / "20260910-100000-1"
        (run_dir / "bodies").mkdir(parents=True)
        (run_dir / "page-source.html").write_text("<html></html>")
        _write_har(run_dir, [_entry("GET", "https://api.myshop.example.com/x")])
        source = DigestSource.from_run_dir(run_dir, session="myshop", run_id=run_dir.name)
        assert source.bodies_dir == run_dir / "bodies"
        assert source.page_source == run_dir / "page-source.html"
        assert source.session == "myshop"
        assert source.run_id == run_dir.name


class TestEndpointExamples:
    def test_an_endpoint_that_answered_with_no_body_says_so(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/go",
                status=302,
                content_type="text/html",
                body="",
                response_headers={"Location": "/app"},
            ),
            _entry("GET", "https://api.myshop.example.com/orders", body='{"orders": []}'),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        empty = {e.template: e.response_body_empty for e in result.endpoints}
        assert empty == {"/go": True, "/orders": False}

    @pytest.mark.parametrize(
        ("body", "falsy"),
        [
            ("{}", "{}"),
            ("{ }", "{}"),
            ("[]", "[]"),
            ('""', '""'),
            ("0", "0"),
            ("false", "false"),
            ("null", "null"),
            ("", None),
            ('{"ok": true}', None),
            ("[1]", None),
            ("not json", None),
        ],
        ids=[
            "object",
            "spaced-object",
            "array",
            "string",
            "zero",
            "false",
            "null",
            "empty",
            "truthy",
            "list",
            "text",
        ],
    )
    def test_the_first_response_s_falsy_value_is_recorded(
        self, tmp_path: Path, body: str, falsy: str | None
    ) -> None:
        """A falsy JSON value is recorded as canonical JSON; an empty body (which
        response_body_empty records) and anything else is None."""
        entries = [_entry("POST", "https://api.myshop.example.com/ack", body=body)]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (endpoint,) = result.endpoints
        assert endpoint.falsy_first_response == falsy

    @pytest.mark.parametrize(
        ("bodies", "falsy"),
        [
            (["{}", '{"ok": true}'], "{}"),
            (['{"ok": true}', "{}"], None),
            (["", "{}"], "{}"),
            (["", '{"ok": true}'], None),
        ],
        ids=["falsy-first", "truthy-first", "empty-then-falsy", "empty-then-truthy"],
    )
    def test_the_fixture_recording_decides(
        self, tmp_path: Path, bodies: list[str], falsy: str | None
    ) -> None:
        """gp observe fixtures names the first recording with a body without a
        suffix, and that is the one a generated test reads."""
        entries = [_entry("GET", "https://api.myshop.example.com/ack", body=b) for b in bodies]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (endpoint,) = result.endpoints
        assert endpoint.falsy_first_response == falsy

    @staticmethod
    def _no_text(entry: dict) -> dict:
        entry["response"]["content"] = {"mimeType": "text/html", "size": 0}
        return entry

    @pytest.mark.parametrize(
        ("statuses_and_texts", "written"),
        [
            ([(200, "")], True),
            ([(200, None)], False),
            ([(500, None)], False),
            ([(200, None), (302, None)], True),
        ],
        ids=["empty-text", "no-text", "no-text-500", "no-text-then-302"],
    )
    def test_whether_a_fixture_is_written_follows_gp_observe_fixtures(
        self, tmp_path: Path, statuses_and_texts: list, written: bool
    ) -> None:
        entries = []
        for status, text in statuses_and_texts:
            entry = _entry(
                "GET", "https://api.myshop.example.com/go", status=status, content_type="text/html"
            )
            entry["response"]["content"] = {"mimeType": "text/html", "size": 0}
            if text is not None:
                entry["response"]["content"]["text"] = text
            entries.append(entry)
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (endpoint,) = result.endpoints
        assert endpoint.fixture_written is written

    def test_a_recording_fixtures_skips_is_never_the_fixture_recording(
        self, tmp_path: Path
    ) -> None:
        """A 200 with no text (fixtures skips it), then a 302 with none: the fixture
        is the 302's, so its content type is the fixture's."""
        first = _entry("GET", "https://api.myshop.example.com/go", content_type="application/json")
        first["response"]["content"] = {"mimeType": "application/json", "size": 0}
        second = _entry(
            "GET", "https://api.myshop.example.com/go", status=302, content_type="text/html"
        )
        second["response"]["content"] = {"mimeType": "text/html", "size": 0}
        result = digest(DigestSource.from_har(_write_har(tmp_path, [first, second])))
        (endpoint,) = result.endpoints
        assert endpoint.fixture_content_type == "text/html"

    def test_the_fixture_recording_is_of_the_endpoint_s_main_content_type(
        self, tmp_path: Path
    ) -> None:
        """/v1/cart answered an HTML page first, then JSON twice: the endpoint is JSON,
        so its fixture is its first JSON recording, and its falsy value is that one's."""
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/v1/cart",
                content_type="text/html",
                body="<p>sign in</p>",
            ),
            _entry("GET", "https://api.myshop.example.com/v1/cart", body="{}"),
            _entry("GET", "https://api.myshop.example.com/v1/cart", body='{"items": [1]}'),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (endpoint,) = result.endpoints
        assert endpoint.content_type == "application/json"
        assert endpoint.fixture_content_type == "application/json"
        assert endpoint.falsy_first_response == "{}"

    def test_with_no_written_recording_of_the_main_type_the_fixture_is_another(
        self, tmp_path: Path
    ) -> None:
        """The JSON recordings kept no text, so fixtures writes only the HTML one."""
        no_text = []
        for _ in range(2):
            entry = _entry("GET", "https://api.myshop.example.com/v1/cart")
            entry["response"]["content"] = {"mimeType": "application/json", "size": 0}
            no_text.append(entry)
        html = _entry(
            "GET", "https://api.myshop.example.com/v1/cart", content_type="text/html", body="<p/>"
        )
        result = digest(DigestSource.from_har(_write_har(tmp_path, [*no_text, html])))
        (endpoint,) = result.endpoints
        assert endpoint.content_type == "application/json"
        assert endpoint.fixture_content_type == "text/html"
        assert endpoint.fixture_written is True

    @pytest.mark.parametrize("body", ["0", "false", "{}"])
    def test_a_text_response_has_no_falsy_json_value(self, tmp_path: Path, body: str) -> None:
        """Only a JSON response is read as JSON: a text/plain 0 is the text "0"."""
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/count", content_type="text/plain", body=body
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (endpoint,) = result.endpoints
        assert endpoint.falsy_first_response is None

    def test_examples_stop_at_the_cap(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", f"https://api.myshop.example.com/orders/{1000 + i}")
            for i in range(_MAX_ENDPOINT_EXAMPLES + 4)
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (endpoint,) = [e for e in result.endpoints if e.template == "/orders/{order_id}"]
        assert len(endpoint.examples) == _MAX_ENDPOINT_EXAMPLES


class TestTheFixtureRecordingIsOneOwner:
    """X1 (polish #212 round 21): the digest decides which recording is the
    fixture by one rule, over the recordings it kept (in scope, not static,
    capture_text not None), media types compared normalised."""

    @staticmethod
    def _no_body(method: str, url: str, *, status: int, content_type: str = "") -> dict:
        entry = _entry(method, url, status=status, content_type=content_type, body="")
        entry["response"]["content"] = {"mimeType": content_type, "size": 0}
        return entry

    def test_two_204s_then_a_200_json_writes_the_json_fixture(self, tmp_path: Path) -> None:
        """A round-20 regression: the fixture used to prefer the 204s' type just
        because two of them outnumbered the one JSON response."""
        entries = [
            self._no_body("POST", "https://api.myshop.example.com/ack", status=204),
            self._no_body("POST", "https://api.myshop.example.com/ack", status=204),
            _entry("POST", "https://api.myshop.example.com/ack", body='{"ok": true}'),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (endpoint,) = result.endpoints
        assert endpoint.fixture_content_type == "application/json"
        assert endpoint.falsy_first_response is None

    def test_204_json_then_204_then_200_html_writes_the_html_fixture(self, tmp_path: Path) -> None:
        """Neither 204 has a body, whatever type either declares: the one
        recording with a body wins, however it is typed."""
        entries = [
            self._no_body(
                "PUT",
                "https://api.myshop.example.com/cart",
                status=204,
                content_type="application/json",
            ),
            self._no_body("PUT", "https://api.myshop.example.com/cart", status=204),
            _entry(
                "PUT",
                "https://api.myshop.example.com/cart",
                content_type="text/html",
                body="<p>ok</p>",
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (endpoint,) = result.endpoints
        assert endpoint.fixture_content_type == "text/html"

    def test_a_204_then_an_empty_array_writes_the_array_fixture(self, tmp_path: Path) -> None:
        entries = [
            self._no_body("DELETE", "https://api.myshop.example.com/items/1", status=204),
            _entry("DELETE", "https://api.myshop.example.com/items/1", body="[]"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (endpoint,) = result.endpoints
        assert endpoint.fixture_content_type == "application/json"
        assert endpoint.falsy_first_response == "[]"

    def test_an_out_of_scope_host_recorded_first_is_never_the_fixture(self, tmp_path: Path) -> None:
        """A third-party host answering the same path first, with a body: the
        digest never saw it (dropped as third_party), so it is never a
        candidate, whatever order the HAR recorded it in."""
        entries = [
            _entry("GET", "https://vendor.example.net/cart", body='{"vendor": true}'),
            _entry("GET", "https://api.myshop.example.com/", content_type="text/html", body="<p/>"),
            _entry("GET", "https://api.myshop.example.com/other"),
            _entry("GET", "https://api.myshop.example.com/cart", body='{"n": 1}'),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.primary_host == "api.myshop.example.com"
        (endpoint,) = [e for e in result.endpoints if e.template == "/cart"]
        assert endpoint.count == 1
        assert endpoint.host == "api.myshop.example.com"
        assert endpoint.falsy_first_response is None

    def test_a_charset_suffixed_json_majority_still_wins_the_vote(self, tmp_path: Path) -> None:
        """Two JSON recordings, spelled with two different charset parameters, and
        one HTML: normalised, JSON is 2 and HTML is 1."""
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/profile",
                content_type="application/json;charset=UTF-8",
                body='{"a": 1}',
            ),
            _entry(
                "GET",
                "https://api.myshop.example.com/profile",
                content_type="application/json; charset=utf-8",
                body='{"a": 2}',
            ),
            _entry(
                "GET",
                "https://api.myshop.example.com/profile",
                content_type="text/html",
                body="<p/>",
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (endpoint,) = result.endpoints
        assert endpoint.fixture_content_type == "application/json"
        assert endpoint.falsy_first_response is None

    def test_a_one_to_one_html_and_json_tie_goes_to_json(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/ping", content_type="text/html", body="<p/>"
            ),
            _entry("GET", "https://api.myshop.example.com/ping", body='{"ok": true}'),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (endpoint,) = result.endpoints
        assert endpoint.fixture_content_type == "application/json"

    def test_a_one_to_one_tie_with_neither_type_json_goes_to_the_earliest(
        self, tmp_path: Path
    ) -> None:
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/feed", content_type="text/xml", body="<a/>"
            ),
            _entry(
                "GET", "https://api.myshop.example.com/feed", content_type="text/html", body="<p/>"
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (endpoint,) = result.endpoints
        assert endpoint.fixture_content_type == "text/xml"

    def test_shape_reflects_the_fixture_recording_not_a_losing_type(self, tmp_path: Path) -> None:
        """One JSON response, then two HTML: HTML wins the vote (2 to 1), so the
        docstring must not print the JSON response's shape, which the generated
        test's fixture never holds."""
        entries = [
            _entry("GET", "https://api.myshop.example.com/mixed", body='{"a": 1}'),
            _entry(
                "GET",
                "https://api.myshop.example.com/mixed",
                content_type="text/html",
                body="<p>1</p>",
            ),
            _entry(
                "GET",
                "https://api.myshop.example.com/mixed",
                content_type="text/html",
                body="<p>2</p>",
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (endpoint,) = result.endpoints
        assert endpoint.fixture_content_type == "text/html"
        assert endpoint.shape is None

    def test_fixture_entry_index_names_the_single_recording(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/solo", body='{"n": 1}')]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (endpoint,) = result.endpoints
        assert endpoint.fixture_entry_index == 0

    def test_fixture_entry_index_names_the_winning_recording_not_the_first_seen(
        self, tmp_path: Path
    ) -> None:
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/mix", content_type="text/html", body="<p/>"
            ),
            _entry("GET", "https://api.myshop.example.com/mix", body='{"n": 1}'),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (endpoint,) = result.endpoints
        assert endpoint.fixture_entry_index == 1

    def test_no_candidates_leaves_the_entry_index_unset(self, tmp_path: Path) -> None:
        bodyless = _entry(
            "GET", "https://api.myshop.example.com/report", status=500, content_type="text/html"
        )
        bodyless["response"]["content"] = {"mimeType": "text/html", "size": 0}
        result = digest(DigestSource.from_har(_write_har(tmp_path, [bodyless])))
        (endpoint,) = result.endpoints
        assert endpoint.fixture_written is False
        assert endpoint.fixture_entry_index is None


class TestEveryEndpointPresent:
    def test_low_and_high_count_endpoints_both_appear(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/rare")]
        entries += [_entry("GET", "https://api.myshop.example.com/common") for _ in range(50)]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert "/rare" in templates
        assert "/common" in templates


class TestHighCardinalityCollapse:
    def test_segment_with_many_distinct_literal_values_collapses(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", f"https://api.myshop.example.com/products/item-{i}")
            for i in range(_HIGH_CARDINALITY_THRESHOLD + 2)
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert "/products/{product_id}" in templates

    def test_endpoint_template_answers_with_the_collapsed_template(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", f"https://api.myshop.example.com/products/item-{i}")
            for i in range(_HIGH_CARDINALITY_THRESHOLD + 2)
        ]
        entries.append(_entry("GET", "https://api.myshop.example.com/orders/7001"))
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert endpoint_template(result, "/products/item-3") == "/products/{product_id}"
        assert endpoint_template(result, "/orders/7001") == "/orders/{order_id}"
        assert endpoint_template(result, "/never/seen") == "/never/seen"

    def test_segment_at_or_below_threshold_stays_literal(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", f"https://api.myshop.example.com/products/item-{i}")
            for i in range(_HIGH_CARDINALITY_THRESHOLD)
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert all(t.startswith("/products/item-") for t in templates)

    def test_single_segment_routes_never_collapse(self, tmp_path: Path) -> None:
        """A single segment has no other position to compare, so the family
        check would otherwise treat any run of distinct root routes as one
        high-cardinality family and collapse them all into `/{id}`."""
        entries = [
            _entry("GET", f"https://api.myshop.example.com/route-{i}")
            for i in range(_HIGH_CARDINALITY_THRESHOLD + 2)
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert templates == {f"/route-{i}" for i in range(_HIGH_CARDINALITY_THRESHOLD + 2)}


class TestHighCardinalityEligibility:
    """A position collapses on count *and* on its values looking like identifiers."""

    def test_word_like_sibling_routes_stay_separate_endpoints(self, tmp_path: Path) -> None:
        names = [
            "orders",
            "products",
            "users",
            "carts",
            "invoices",
            "shipments",
            "returns",
            "coupons",
            "reviews",
        ]
        assert len(names) > _HIGH_CARDINALITY_THRESHOLD
        entries = [_entry("GET", f"https://api.myshop.example.com/api/{name}") for name in names]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert templates == {f"/api/{name}" for name in names}

    def test_digit_bearing_slug_family_still_collapses(self, tmp_path: Path) -> None:
        count = _HIGH_CARDINALITY_THRESHOLD + 1
        entries = [
            _entry("GET", f"https://api.myshop.example.com/products/red-widget-{2000 + i}")
            for i in range(count)
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert templates == {"/products/{product_id}"}

    def test_minority_of_eligible_values_does_not_collapse(self, tmp_path: Path) -> None:
        total = _HIGH_CARDINALITY_THRESHOLD + 3
        eligible = int(total * _DYNAMIC_MAJORITY)  # a minority of *total*, by definition
        # Each carries a digit but is literal on its own (a digit run of at most 2).
        slugs = [f"red-widget-{i}" for i in range(eligible)]
        slugs += [f"red-widget-{chr(ord('a') + i)}" for i in range(total - eligible)]
        entries = [
            _entry("GET", f"https://api.myshop.example.com/products/{slug}") for slug in slugs
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert templates == {f"/products/{slug}" for slug in slugs}


class TestCollapseIsScopedToTheFamilyThatQualified:
    def test_siblings_of_the_same_depth_outside_the_family_stay_literal(
        self, tmp_path: Path
    ) -> None:
        """One slug family used to collapse every template of its segment count:
        /account/profile and /account/settings became /account/{account_id} and
        merged, and /api/health became /api/{api_id}."""
        slug_count = 80
        assert slug_count > _HIGH_CARDINALITY_THRESHOLD
        entries = [
            _entry("GET", f"https://api.myshop.example.com/products/red-widget-{2000 + i}")
            for i in range(slug_count)
        ]
        entries += [
            _entry("GET", "https://api.myshop.example.com/api/health"),
            _entry("GET", "https://api.myshop.example.com/account/profile"),
            _entry("GET", "https://api.myshop.example.com/account/settings"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert templates == {
            "/products/{product_id}",
            "/api/health",
            "/account/profile",
            "/account/settings",
        }

    def test_an_ineligible_value_inside_the_family_stays_literal(self, tmp_path: Path) -> None:
        """The family qualifies on its values as a whole; a word-like member of
        it is still not an identifier and keeps its own template."""
        slug_count = _HIGH_CARDINALITY_THRESHOLD + 2
        entries = [
            _entry("GET", f"https://api.myshop.example.com/products/red-widget-{2000 + i}")
            for i in range(slug_count)
        ]
        entries.append(_entry("GET", "https://api.myshop.example.com/products/featured"))
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert templates == {"/products/{product_id}", "/products/featured"}


class TestCollapsePreservesATrailingSlash:
    def test_a_family_recorded_with_a_trailing_slash_keeps_it(self, tmp_path: Path) -> None:
        """paths.template_path preserves a trailing slash exactly as given, so
        the collapse must too: dropping it renamed the route."""
        count = _HIGH_CARDINALITY_THRESHOLD + 2
        entries = [
            _entry("GET", f"https://api.myshop.example.com/products/red-widget-{2000 + i}/")
            for i in range(count)
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert templates == {"/products/{product_id}/"}


class TestCollapseMergeCarriesShapeAndBodyKind:
    def test_a_later_members_shape_and_body_kind_survive_the_merge(self, tmp_path: Path) -> None:
        """The first member of a collapsed family answers for the family, and it may
        be the one that returned HTML and posted nothing."""
        count = _HIGH_CARDINALITY_THRESHOLD + 1
        # A letter and one digit: each path stays its own raw template, so only the
        # high-cardinality collapse makes them one endpoint.
        paths = [f"/products/widget-{chr(ord('a') + i)}1" for i in range(count)]
        entries = [
            _entry(
                "POST",
                f"https://api.myshop.example.com{paths[0]}",
                content_type="text/html",
                body="<html></html>",
            )
        ]
        entries += [
            _entry(
                "POST",
                f"https://api.myshop.example.com{path}",
                body='{"id": 1}',
                post_data='{"sku": "widget"}',
            )
            for path in paths[1:]
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        # The merge ran: every member's raw template was re-templated into one family.
        assert set(result.collapsed_templates) == set(paths)
        merged = [e for e in result.endpoints if e.template == "/products/{product_id}"]
        assert len(merged) == 1, [e.template for e in result.endpoints]
        assert len(result.endpoints) == 1
        assert merged[0].count == count
        assert merged[0].shape is not None
        assert merged[0].shape != SHAPE_UNAVAILABLE
        assert merged[0].body_kind == "json"
        assert "sku" in merged[0].body_params

    @pytest.mark.parametrize(
        ("falsy_member", "falsy"), [(0, "{}"), (-1, None)], ids=["first", "later"]
    )
    def test_the_family_s_first_member_decides_its_falsy_value(
        self, tmp_path: Path, falsy_member: int, falsy: str | None
    ) -> None:
        """The family's fixture is its first member's recording, so only that
        member's {} counts."""
        count = _HIGH_CARDINALITY_THRESHOLD + 1
        falsy_index = falsy_member % count
        entries = [
            _entry(
                "GET",
                # A letter and one digit: each path is its own raw template, so the
                # family is merged by the high-cardinality collapse.
                f"https://api.myshop.example.com/products/widget-{chr(ord('a') + i)}1",
                body="{}" if i == falsy_index else '{"id": 1}',
            )
            for i in range(count)
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (merged,) = [e for e in result.endpoints if e.template == "/products/{product_id}"]
        assert merged.falsy_first_response == falsy

    def test_the_family_s_fixture_is_its_first_recording_with_a_body(self, tmp_path: Path) -> None:
        """The first member's first recording is empty, so the family's fixture is the
        second member's {}, recorded before the first member's own body."""
        count = _HIGH_CARDINALITY_THRESHOLD + 1
        paths = [f"/products/widget-{chr(ord('a') + i)}1" for i in range(count)]
        first_empty = _entry("GET", f"https://api.myshop.example.com{paths[0]}", body="")
        first_empty["response"]["status"] = 204
        entries = [
            first_empty,
            _entry("GET", f"https://api.myshop.example.com{paths[1]}", body="{}"),
            *(
                _entry("GET", f"https://api.myshop.example.com{path}", body='{"id": 1}')
                for path in [*paths[2:], paths[0]]
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (merged,) = [e for e in result.endpoints if e.template == "/products/{product_id}"]
        assert merged.falsy_first_response == "{}"

    def test_the_family_s_fixture_content_type_is_its_fixture_recording_s(
        self, tmp_path: Path
    ) -> None:
        """The first member's first recording is empty, so the family's fixture is the
        second member's HTML page, and its type is the family's fixture type."""
        count = _HIGH_CARDINALITY_THRESHOLD + 1
        paths = [f"/products/widget-{chr(ord('a') + i)}1" for i in range(count)]
        first_empty = _entry("GET", f"https://api.myshop.example.com{paths[0]}", body="")
        first_empty["response"]["status"] = 204
        entries = [
            first_empty,
            _entry(
                "GET",
                f"https://api.myshop.example.com{paths[1]}",
                content_type="text/html",
                body="<p>widget</p>",
            ),
            *(
                _entry("GET", f"https://api.myshop.example.com{path}", body='{"id": 1}')
                for path in [*paths[2:], paths[0]]
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (merged,) = [e for e in result.endpoints if e.template == "/products/{product_id}"]
        # The family is mostly JSON: its fixture is its first JSON recording with a
        # body, the third member's, never the earlier HTML page.
        assert merged.content_type == "application/json"
        assert merged.fixture_content_type == "application/json"

    def test_members_that_type_a_parameter_differently_merge_to_str(self, tmp_path: Path) -> None:
        """The family merge applies the same rule a single endpoint's requests do."""
        count = _HIGH_CARDINALITY_THRESHOLD + 1
        entries = [
            _entry(
                "GET",
                f"https://api.myshop.example.com/products/red-widget-{2000 + i}"
                f"?ref={'abc' if i == 0 else i}&page={i}",
            )
            for i in range(count)
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (merged,) = [e for e in result.endpoints if e.template == "/products/{product_id}"]
        assert merged.query_params == {"ref": "str", "page": "int"}


class TestParseErrorsAndMissingBodies:
    def test_malformed_entry_counts_as_dropped_error(self, tmp_path: Path) -> None:
        good = _entry("GET", "https://api.myshop.example.com/orders")
        bad = {"request": None, "response": {}}
        har_path = _write_har(tmp_path, [good, bad])
        result = digest(DigestSource.from_har(har_path))
        assert result.dropped["error"] == 1

    def test_missing_body_file_is_dropped_not_raised(self, tmp_path: Path) -> None:
        entry = _entry("GET", "https://api.myshop.example.com/big")
        entry["response"]["content"] = {
            "mimeType": "application/json",
            "size": 0,
            "_bodyFile": "bodies/missing.json",
        }
        har_path = _write_har(tmp_path, [entry])
        result = digest(DigestSource.from_har(har_path))  # must not raise
        assert result.dropped["error"] == 1

    def test_the_missing_body_warning_logs_a_bare_url(self, tmp_path: Path) -> None:
        entry = _entry("GET", "https://api.myshop.example.com/big;s=PARAMVALUE?q=QUERYVALUE#FRAG")
        entry["response"]["content"] = {
            "mimeType": "application/json",
            "size": 0,
            "_bodyFile": "bodies/missing.json",
        }
        har_path = _write_har(tmp_path, [entry])
        with capture_logs() as events:
            digest(DigestSource.from_har(har_path))
        (event,) = [e for e in events if e["event"] == "digest_body_file_missing"]
        assert event["url"] == "https://api.myshop.example.com/big"

    def test_a_malformed_form_action_is_recorded_as_empty(self, tmp_path: Path) -> None:
        """urlsplit raises on an unclosed IPv6 bracket; the digest must still complete,
        and the warning names the page, never the action text."""
        page = _entry(
            "GET",
            "https://myshop.example.com/signin?next=QUERYVALUE",
            content_type="text/html",
            body=(
                '<form action="http://[bad/login"><input name="username">'
                '<input type="password" name="password"></form>'
            ),
        )
        with capture_logs() as events:
            result = digest(DigestSource.from_har(_write_har(tmp_path, [page])))
        (form,) = result.login_forms
        assert form.action == ""
        # Unscoped: an empty-action scope would never match the live form.
        assert form.fields["username"] == 'input[name="username"]'
        (event,) = [e for e in events if e["event"] == "login_form_action_unparseable"]
        assert event["source"] == "https://myshop.example.com/signin"
        assert "[bad" not in str(event)

    def test_a_malformed_request_url_is_dropped_as_an_error(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "http://[bad/orders"),
        ]
        with capture_logs() as events:
            result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["error"] == 1
        assert [e.template for e in result.endpoints] == ["/orders"]
        assert any(e["event"] == "digest_url_unparseable" for e in events)

    def test_a_malformed_redirect_target_records_no_landing_path(self, tmp_path: Path) -> None:
        post = _entry(
            "POST",
            "https://myshop.example.com/session",
            status=302,
            content_type="text/html",
            body="",
            response_headers={"Location": "http://[bad/dashboard"},
            post_data="username=alice&password=x",
        )
        post["request"]["postData"]["mimeType"] = _FORM_CONTENT_TYPE
        result = digest(DigestSource.from_har(_write_har(tmp_path, [post])))
        (observation,) = [o for o in result.login if o.kind == "credential_post"]
        assert observation.redirect_to == ""


def _flow_endpoint(template: str, method: str = "GET", examples: tuple[str, ...] = ()) -> Endpoint:
    return Endpoint(
        host="api.myshop.example.com",
        template=template,
        methods=(method,),
        count=1,
        statuses=(200,),
        content_type="text/html",
        query_params={},
        body_params={},
        body_kind="none",
        shape=None,
        custom_headers=(),
        examples=examples,
    )


def _flow_observation(kind: ObservationKind, method: str, path: str) -> LoginObservation:
    return LoginObservation(
        order=1,
        method=method,
        url=f"https://api.myshop.example.com{path}",
        status=200,
        kind=kind,
        fields=(),
    )


class TestLoginFlowFlag:
    """login_flow marks the endpoints login_config drives: the login form's GET and
    the credential POST. It moved here from the generator so the proposal table and
    the scaffold read one flag (graft skill spec, 2026-09-21)."""

    def test_the_form_page_and_the_credential_post_are_flagged(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/login",
                content_type="text/html",
                body=(
                    '<form action="/login" method="post">'
                    '<input type="email" name="email"><input type="password" name="password">'
                    "</form>"
                ),
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/login",
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
            _entry("GET", "https://api.myshop.example.com/api/orders", body='{"orders": []}'),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags == {
            ("GET", "/login"): True,
            ("POST", "/login"): True,
            ("GET", "/api/orders"): False,
        }

    def test_a_header_login_form_on_every_page_does_not_claim_other_posts(
        self, tmp_path: Path
    ) -> None:
        """A login form in a site-wide header marks only a POST to its own action;
        a POST whose HTML response carries that form is still an ordinary command."""
        header = (
            '<form action="/login" method="post"><input type="email" name="email">'
            '<input type="password" name="passcode"></form>'
        )
        entries = [
            _entry("GET", "https://api.myshop.example.com/", content_type="text/html", body=header),
            _entry(
                "POST",
                "https://api.myshop.example.com/cart/add",
                content_type="text/html",
                body=header + "<p>added</p>",
                post_data=json.dumps({"sku": "x", "quantity": 1}),
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/newsletter",
                content_type="text/html",
                body=header + "<p>subscribed</p>",
                post_data=json.dumps({"email": "alice@example.com"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert not any(o.kind == "credential_post" for o in result.login)
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags[("POST", "/cart/add")] is False
        assert flags[("POST", "/newsletter")] is False

    @pytest.mark.parametrize(
        ("page", "action", "post"),
        [
            (
                "https://www.myshop.example.com/",
                "/login",
                "https://api.myshop.example.com/login",
            ),
            (
                "https://www.myshop.example.com/u/alice@example.com/",
                "/u/alice@example.com/login",
                "https://www.myshop.example.com/u/bob@example.com/login",
            ),
        ],
        ids=["another-host", "another-email"],
    )
    def test_a_post_matches_a_form_action_by_host_and_unmasked_path(
        self, tmp_path: Path, page: str, action: str, post: str
    ) -> None:
        form = (
            f'<form action="{action}" method="post"><input type="email" name="email">'
            '<input type="password" name="passcode"></form>'
        )
        entries = [
            _entry("GET", page, content_type="text/html", body=form),
            _entry("POST", post, post_data=json.dumps({"sku": "x"})),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert not any(o.kind == "credential_post" for o in result.login)

    def test_the_same_login_form_on_several_pages_is_recorded_once(self, tmp_path: Path) -> None:
        header = (
            '<form action="/login" method="post"><input type="email" name="email">'
            '<input type="password" name="password"></form>'
        )
        entries = [
            _entry(
                "GET",
                f"https://api.myshop.example.com/{page}",
                content_type="text/html",
                body=header,
            )
            for page in ("", "products", "cart")
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert len(result.login_forms) == 1

    @pytest.mark.parametrize(
        "form_open",
        ['<form method="post">', '<form action="./add" method="post">'],
        ids=["no-action", "relative-action"],
    )
    def test_a_self_posting_header_form_does_not_claim_the_post_that_served_it(
        self, tmp_path: Path, form_open: str
    ) -> None:
        """Targets come only from forms a GET served, and an entry is classified
        before its own forms are recorded."""
        header = (
            f'{form_open}<input type="email" name="email">'
            '<input type="password" name="passcode"></form>'
        )
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/shop/", content_type="text/html", body=header
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/cart/add",
                content_type="text/html",
                body=header + "<p>added</p>",
                post_data=json.dumps({"sku": "x", "quantity": 1}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert not any(o.kind == "credential_post" for o in result.login)
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags[("POST", "/cart/add")] is False
        assert flags[("GET", "/shop/")] is False

    def test_a_page_with_a_login_form_is_the_form_page_only_before_a_credential_post(
        self, tmp_path: Path
    ) -> None:
        """An ordinary page carrying a site-wide login form keeps its stub."""
        form = (
            '<form action="/session" method="post"><input type="email" name="email">'
            '<input type="password" name="password"></form>'
        )
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/products",
                content_type="text/html",
                body=form,
            ),
            _entry("GET", "https://api.myshop.example.com/api/orders", body='{"orders": []}'),
            _entry(
                "GET", "https://api.myshop.example.com/signin", content_type="text/html", body=form
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/session",
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
        ]
        entries = [
            entries[0],
            entries[1],
            *[
                _entry("GET", f"https://api.myshop.example.com/api/filler/{n}", body="{}")
                for n in range(1001, 1030)
            ],
            entries[2],
            entries[3],
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        pages = [o.url for o in result.login if o.kind == "form_page"]
        assert pages == ["https://api.myshop.example.com/signin"]
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags[("GET", "/products")] is False
        assert flags[("GET", "/signin")] is True
        assert [o.order for o in result.login] == list(range(1, len(result.login) + 1))

    @pytest.mark.parametrize(
        ("action", "post_path"),
        [("", "/u/alice@example.com/signin"), ('action="verify"', "/u/alice@example.com/verify")],
        ids=["no-action", "relative-action"],
    )
    def test_a_form_target_resolves_against_the_unmasked_page(
        self, tmp_path: Path, action: str, post_path: str
    ) -> None:
        """The page path holds an email; the target is resolved against the page
        as requested, not as the digest masks it for printing."""
        form = (
            f'<form {action} method="post"><input type="email" name="email">'
            '<input type="password" name="passcode"></form>'
        )
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/u/alice@example.com/signin",
                content_type="text/html",
                body=form,
            ),
            _entry(
                "POST",
                f"https://api.myshop.example.com{post_path}",
                post_data=json.dumps({"email": "alice@example.com", "passcode": "x"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert [o.kind for o in result.login if o.method == "POST"] == ["credential_post"]
        assert "alice@example.com" not in render_json(result)

    def test_the_same_empty_action_form_on_several_pages_is_listed_once(
        self, tmp_path: Path
    ) -> None:
        """The target, which differs per page, is not part of the identity."""
        header = (
            '<form method="post"><input type="email" name="email">'
            '<input type="password" name="password"></form>'
        )
        entries = [
            _entry(
                "GET",
                f"https://api.myshop.example.com/{page}",
                content_type="text/html",
                body=header,
            )
            for page in ("", "products", "cart")
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert len(result.login_forms) == 1

    def test_the_form_a_credential_post_went_to_is_listed_first(self, tmp_path: Path) -> None:
        """The generator takes the first form, which is the one that was used."""
        page = (
            '<form action="/a/login" method="post"><input type="email" name="email">'
            '<input type="password" name="password"></form>'
            '<form action="/b/login" method="post"><input type="text" name="username">'
            '<input type="password" name="password"></form>'
        )
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/signin", content_type="text/html", body=page
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/b/login",
                post_data=json.dumps({"username": "alice", "password": "x"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert [form.action for form in result.login_forms] == ["/b/login", "/a/login"]

    def test_assets_between_the_login_page_and_the_post_do_not_demote_the_page(
        self, tmp_path: Path
    ) -> None:
        """The form page is found by the post's target, not by distance."""
        form = (
            '<form action="/session" method="post"><input type="email" name="email">'
            '<input type="password" name="password"></form>'
        )
        assets = [
            _entry(
                "GET",
                f"https://api.myshop.example.com/static/app{n}.js",
                content_type="application/javascript",
                body="var a;",
            )
            for n in range(25)
        ]
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/signin", content_type="text/html", body=form
            ),
            *assets,
            _entry(
                "POST",
                "https://api.myshop.example.com/session",
                status=302,
                response_headers={"Location": "/dashboard"},
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
            *assets,
            _entry("GET", "https://api.myshop.example.com/dashboard", set_cookies=["s=v; Path=/"]),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        kinds = [o.kind for o in result.login]
        assert kinds[:2] == ["form_page", "credential_post"]
        assert "set_cookie" in kinds
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags[("GET", "/signin")] is True

    @pytest.mark.parametrize("action", ["session", "./session"])
    def test_a_relative_action_in_a_saved_page_source_marks_the_post(
        self, tmp_path: Path, action: str
    ) -> None:
        """A page source file has no URL to resolve against."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "page-source.html").write_text(
            f'<form action="{action}" method="post"><input type="email" name="email">'
            '<input type="password" name="passcode"></form>'
        )
        _write_har(
            run_dir,
            [
                _entry(
                    "POST",
                    "https://api.myshop.example.com/account/session",
                    post_data=json.dumps({"email": "alice@example.com", "passcode": "x"}),
                )
            ],
        )
        result = digest(DigestSource.from_run_dir(run_dir, session="myshop", run_id="run"))
        assert [o.kind for o in result.login if o.method == "POST"] == ["credential_post"]

    def test_the_form_on_the_promoted_page_is_first_among_forms_posting_there(
        self, tmp_path: Path
    ) -> None:
        """Two pages carry forms posting to /session; the one the post followed
        wins, then the form whose names cover the post body."""
        header = (
            '<form action="/session" method="post"><input type="email" name="email">'
            '<input type="password" name="password"></form>'
        )
        signin = (
            '<form action="/session" method="post"><input type="text" name="username">'
            '<input type="password" name="password"><input type="hidden" name="otp"></form>'
        )
        entries = [
            _entry("GET", "https://api.myshop.example.com/", content_type="text/html", body=header),
            _entry(
                "GET",
                "https://api.myshop.example.com/signin",
                content_type="text/html",
                body=signin,
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/session",
                post_data=json.dumps({"username": "alice", "password": "x", "otp": "1"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.login_forms[0].source == "https://api.myshop.example.com/signin"

    def test_a_site_wide_header_form_ranks_below_the_main_form(self, tmp_path: Path) -> None:
        """Both post to /session and both sit on the promoted page; the header
        form also sits on a page no post promoted, so the main form lists first."""
        header = (
            '<form action="/session" method="post" class="mini">'
            '<input type="email" name="login[username]">'
            '<input type="password" name="login[password]">'
            "<button>Go</button>"
            "</form>"
        )
        main = (
            '<form action="/session" method="post" class="main">'
            '<input type="email" name="login[username]" id="email">'
            '<input type="password" name="login[password]" id="pass">'
            '<button id="send2">Sign in</button>'
            "</form>"
        )
        entries = [
            _entry("GET", "https://api.myshop.example.com/", content_type="text/html", body=header),
            _entry(
                "GET",
                "https://api.myshop.example.com/login",
                content_type="text/html",
                body=header + main,
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/session",
                post_data=json.dumps({"login[username]": "alice", "login[password]": "x"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.login_forms[0].fields == {"username": "#email", "password": "#pass"}

    def test_a_form_on_the_promoted_page_beats_one_covering_more_of_the_body(
        self, tmp_path: Path
    ) -> None:
        """Promoted-page preference: the form on the page the post followed wins over
        a form on two other pages whose names cover more of the body."""
        elsewhere = (
            '<form action="/session" method="post"><input type="text" name="username" id="u1">'
            '<input type="password" name="password" id="p1"><input type="hidden" name="otp">'
            "</form>"
        )
        promoted = (
            '<form action="/session" method="post"><input type="text" name="username" id="u2">'
            '<input type="password" name="password" id="p2"></form>'
        )
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/a", content_type="text/html", body=elsewhere
            ),
            # The covering form sits on two pages, so the page-specific form's page
            # is the one the post promotes.
            _entry(
                "GET", "https://api.myshop.example.com/c", content_type="text/html", body=elsewhere
            ),
            _entry(
                "GET", "https://api.myshop.example.com/b", content_type="text/html", body=promoted
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/session",
                post_data=json.dumps({"username": "alice", "password": "x", "otp": "1"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.login_forms[0].fields["username"] == "#u2"

    def test_on_one_page_the_form_covering_more_of_the_body_wins(self, tmp_path: Path) -> None:
        """Body-name coverage: two forms on the promoted page, the second one
        carrying the hidden field the post sent."""
        page = (
            '<form action="/session" method="post"><input type="text" name="username" id="u1">'
            '<input type="password" name="password" id="p1"></form>'
            '<form action="/session" method="post"><input type="text" name="username" id="u2">'
            '<input type="password" name="password" id="p2"><input type="hidden" name="otp">'
            "</form>"
        )
        entries = [
            _entry("GET", "https://api.myshop.example.com/b", content_type="text/html", body=page),
            _entry(
                "POST",
                "https://api.myshop.example.com/session",
                post_data=json.dumps({"username": "alice", "password": "x", "otp": "1"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.login_forms[0].fields["username"] == "#u2"

    def test_a_post_found_by_its_field_names_promotes_no_page_with_a_real_target(
        self, tmp_path: Path
    ) -> None:
        """A change-password XHR long after a page with a header login form."""
        header = (
            '<form action="/login" method="post"><input type="email" name="email">'
            '<input type="password" name="passcode"></form>'
        )
        entries = [
            _entry("GET", "https://api.myshop.example.com/", content_type="text/html", body=header),
            *[
                _entry("GET", f"https://api.myshop.example.com/api/items/{n}", body="{}")
                for n in range(1001, 1061)
            ],
            _entry(
                "POST",
                "https://api.myshop.example.com/api/account/password",
                post_data=json.dumps({"current_password": "x", "new_password": "y"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags[("GET", "/")] is False

    def test_a_post_found_by_its_field_names_promotes_a_js_driven_form_page(
        self, tmp_path: Path
    ) -> None:
        """A form with no action of its own posts by script."""
        form = (
            '<form><input type="email" name="email"><input type="password" name="password"></form>'
        )
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/signin", content_type="text/html", body=form
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/api/auth",
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags[("GET", "/signin")] is True

    _MAGENTO_HEADER = (
        '<form action="/session" method="post" class="mini">'
        '<input type="email" name="login[username]" id="customer-email">'
        '<input type="password" name="login[password]" id="customer-pass">'
        '<button id="mini-go">Go</button></form>'
    )
    _MAGENTO_MAIN = (
        '<form action="/session" method="post" class="main">'
        '<input type="email" name="login[username]" id="email">'
        '<input type="password" name="login[password]" id="pass">'
        '<button id="send2">Sign in</button></form>'
    )

    def _magento_run(self, tmp_path: Path, home: str) -> list[dict]:
        return [
            _entry("GET", "https://api.myshop.example.com/", content_type="text/html", body=home),
            _entry(
                "GET",
                "https://api.myshop.example.com/login",
                content_type="text/html",
                body=self._MAGENTO_HEADER + self._MAGENTO_MAIN,
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/session",
                post_data=json.dumps({"login[username]": "alice", "login[password]": "x"}),
            ),
        ]

    def test_a_site_wide_form_with_ids_on_every_page_ranks_below_the_main_form(
        self, tmp_path: Path
    ) -> None:
        """Site-wide term: the header form is one form on both pages."""
        entries = self._magento_run(tmp_path, self._MAGENTO_HEADER)
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.login_forms[0].fields == {"username": "#email", "password": "#pass"}
        assert len(result.login_forms) == 2

    def test_the_same_form_alone_and_beside_another_is_listed_once_at_its_best(
        self, tmp_path: Path
    ) -> None:
        """Without ids the header resolves by name alone on /, and not beside the
        main form on /login; it is one form, kept as its best copy."""
        header = (
            '<form action="/session" method="post" class="mini">'
            '<input type="email" name="login[username]">'
            '<input type="password" name="login[password]"><button>Go</button></form>'
        )
        entries = [
            _entry("GET", "https://api.myshop.example.com/", content_type="text/html", body=header),
            _entry(
                "GET",
                "https://api.myshop.example.com/login",
                content_type="text/html",
                body=header + self._MAGENTO_MAIN,
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/session",
                post_data=json.dumps({"login[username]": "alice", "login[password]": "x"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert len(result.login_forms) == 2
        (header_form,) = [f for f in result.login_forms if f.fields.get("username") != "#email"]
        assert header_form.unresolved_roles == ()

    def test_the_form_on_the_page_its_own_post_promoted_beats_one_elsewhere(
        self, tmp_path: Path
    ) -> None:
        """On-its-page term: /a is promoted, but by another post; /b by this one."""
        page_a = (
            '<form action="/session" method="post"><input type="text" name="username" id="u1">'
            '<input type="password" name="password" id="p1"><input type="hidden" name="otp">'
            "</form>"
            '<form action="/other" method="post"><input type="text" name="user" id="u3">'
            '<input type="password" name="pw" id="p3"></form>'
        )
        page_b = (
            '<form action="/session" method="post"><input type="text" name="username" id="u2">'
            '<input type="password" name="password" id="p2"></form>'
        )
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/a", content_type="text/html", body=page_a
            ),
            # The /session form of /a sits on /c too, so /b's page-specific form's
            # page is the one the /session post promotes.
            _entry(
                "GET",
                "https://api.myshop.example.com/c",
                content_type="text/html",
                body=page_a.split("</form>")[0] + "</form>",
            ),
            _entry(
                "GET", "https://api.myshop.example.com/b", content_type="text/html", body=page_b
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/other",
                post_data=json.dumps({"user": "alice", "pw": "x"}),
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/session",
                post_data=json.dumps({"username": "alice", "password": "x", "otp": "1"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        order = [form.fields.get("username") for form in result.login_forms]
        assert order.index("#u2") < order.index("#u1")

    def test_among_equal_forms_the_one_with_fewer_unresolved_roles_wins(
        self, tmp_path: Path
    ) -> None:
        """Unresolved term: same page, same coverage, neither site-wide."""
        page = (
            '<form action="/session" method="post"><input type="text">'
            '<input type="password" name="password" id="p1"></form>'
            '<form action="/session" method="post"><input type="text" name="username" id="u2">'
            '<input type="password" name="password" id="p2"></form>'
        )
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/login", content_type="text/html", body=page
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/session",
                post_data=json.dumps({"password": "x"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.login_forms[0].fields["username"] == "#u2"

    def test_the_login_form_outranks_a_later_change_password_form(self, tmp_path: Path) -> None:
        """A login precedes a password change, so the earliest post's form wins
        even though the change-password form covers more of its body."""
        login = (
            '<form action="/session" method="post"><input type="email" name="email" id="e1">'
            '<input type="password" name="password" id="p1"></form>'
        )
        change = (
            '<form action="/account/password" method="post">'
            '<input type="text" name="username" id="u9" autocomplete="username">'
            '<input type="password" name="current_password" id="c9" '
            'autocomplete="current-password">'
            '<input type="password" name="new_password" autocomplete="new-password">'
            '<input type="password" name="confirm_password" autocomplete="new-password"></form>'
        )
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/login", content_type="text/html", body=login
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/session",
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
            _entry(
                "GET",
                "https://api.myshop.example.com/account",
                content_type="text/html",
                body=change,
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/account/password",
                post_data=json.dumps(
                    {
                        "username": "alice",
                        "current_password": "x",
                        "new_password": "y",
                        "confirm_password": "y",
                    }
                ),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.login_forms[0].fields["password"] == "#p1"  # noqa: S105

    def test_a_post_matched_by_a_page_source_target_takes_no_scripted_fallback(
        self, tmp_path: Path
    ) -> None:
        """Only a post found by its field names alone falls back to a page whose
        form posts by script."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "page-source.html").write_text(
            '<form action="session" method="post"><input type="email" name="email">'
            '<input type="password" name="passcode"></form>'
        )
        scripted = (
            '<form><input type="email" name="email"><input type="password" name="password"></form>'
        )
        _write_har(
            run_dir,
            [
                _entry(
                    "GET",
                    "https://api.myshop.example.com/products",
                    content_type="text/html",
                    body=scripted,
                ),
                _entry(
                    "POST",
                    "https://api.myshop.example.com/account/session",
                    post_data=json.dumps({"email": "alice@example.com", "passcode": "x"}),
                ),
            ],
        )
        result = digest(DigestSource.from_run_dir(run_dir, session="myshop", run_id="run"))
        assert [o.kind for o in result.login if o.method == "POST"] == ["credential_post"]
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags[("GET", "/products")] is False

    def test_a_change_password_post_is_not_a_credential_post_by_field_names(
        self, tmp_path: Path
    ) -> None:
        """A new-password-shaped body name rules out the field-name match."""
        scripted = (
            '<form><input type="email" name="email"><input type="password" name="password"></form>'
        )
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/account",
                content_type="text/html",
                body=scripted,
            ),
            _entry(
                "GET",
                "https://api.myshop.example.com/products",
                content_type="text/html",
                body=scripted,
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/api/account/password",
                post_data=json.dumps({"current_password": "x", "new_password": "y"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert not any(o.kind == "credential_post" for o in result.login)
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags[("GET", "/account")] is False
        assert flags[("GET", "/products")] is False

    def test_a_javascript_action_is_a_script_driven_form(self, tmp_path: Path) -> None:
        """The javascript: branch of the scripted-form test."""
        form = (
            '<form action="javascript:void(0)"><input type="email" name="email">'
            '<input type="password" name="password"></form>'
        )
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/signin", content_type="text/html", body=form
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/api/auth",
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags[("GET", "/signin")] is True

    def test_a_magento_account_edit_flow_is_not_the_login_flow(self, tmp_path: Path) -> None:
        """The edit form holds an email input, so it stays a login-shaped form, but
        login_flow narrows to the login the generator uses."""
        login = (
            '<form action="/customer/account/loginPost/" method="post">'
            '<input type="email" name="login[username]" id="email">'
            '<input type="password" name="login[password]" id="pass"></form>'
        )
        edit = (
            '<form action="/customer/account/editPost/" method="post">'
            '<input type="text" name="firstname" id="firstname">'
            '<input type="email" name="email" id="email-edit">'
            '<input type="password" name="current_password" id="current-password" '
            'autocomplete="current-password">'
            '<input type="password" name="password" id="password" autocomplete="new-password">'
            '<input type="password" name="password_confirmation" autocomplete="new-password">'
            "</form>"
        )
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/customer/account/login/",
                content_type="text/html",
                body=login,
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/customer/account/loginPost/",
                status=302,
                response_headers={"Location": "/customer/account/"},
                post_data=json.dumps({"login[username]": "alice", "login[password]": "x"}),
            ),
            _entry(
                "GET",
                "https://api.myshop.example.com/customer/account/edit/",
                content_type="text/html",
                body=edit,
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/customer/account/editPost/",
                status=302,
                response_headers={"Location": "/customer/account/edit/saved"},
                post_data=json.dumps(
                    {
                        "firstname": "Alice",
                        "email": "alice@example.com",
                        "current_password": "x",
                        "password": "y",
                        "password_confirmation": "y",
                    }
                ),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags[("GET", "/customer/account/login/")] is True
        assert flags[("POST", "/customer/account/loginPost/")] is True
        assert flags[("GET", "/customer/account/edit/")] is False
        assert flags[("POST", "/customer/account/editPost/")] is False
        # The landing is the login's (/customer/account/), never the edit post's; its
        # glob */customer/account* would match the login page, so none is written.
        plugin_code = render(
            ScaffoldSpec(
                name="myshop",
                mode="new_project",
                backend="nodriver",
                base_url="https://api.myshop.example.com",
                digest=result,
            )
        )["src/graftpunk_myshop/plugin.py"]
        assert "success_url=" not in plugin_code
        assert "edit/saved" not in plugin_code
        assert "glob would match the login page" in self._comments(plugin_code)

    def test_a_change_password_form_is_not_a_login_form(self, tmp_path: Path) -> None:
        """Current and new password, no username: a change-password form."""
        login = (
            '<form action="/session" method="post"><input type="email" name="email">'
            '<input type="password" name="password"></form>'
        )
        change = (
            '<form action="/account/password" method="post">'
            '<input type="password" name="current_password" autocomplete="current-password">'
            '<input type="password" name="new_password" autocomplete="new-password">'
            '<input type="password" name="new_password_confirm" autocomplete="new-password">'
            "</form>"
        )
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/login", content_type="text/html", body=login
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/session",
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
            _entry(
                "GET",
                "https://api.myshop.example.com/account/password",
                content_type="text/html",
                body=change,
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/account/password",
                post_data=json.dumps(
                    {"current_password": "x", "new_password": "y", "new_password_confirm": "y"}
                ),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert [form.action for form in result.login_forms] == ["/session"]
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags[("GET", "/account/password")] is False
        assert flags[("POST", "/account/password")] is False
        assert flags[("GET", "/login")] is True
        assert flags[("POST", "/session")] is True

    @staticmethod
    def _plugin(result) -> str:  # noqa: ANN001
        return render(
            ScaffoldSpec(
                name="myshop",
                mode="new_project",
                backend="nodriver",
                base_url="https://api.myshop.example.com",
                digest=result,
            )
        )["src/graftpunk_myshop/plugin.py"]

    _LOGIN_PAGE = (
        '<form action="/session" method="post"><input type="email" name="email" id="email">'
        '<input type="password" name="password" id="pass"></form>'
    )

    @staticmethod
    def _comments(code: str) -> str:
        """*code*'s comment text with each wrapped comment joined into one line."""
        return re.sub(r"\s*\n\s*#\s*", " ", code)

    def _refused_then_retried(self, retry: dict) -> list[dict]:
        """A login whose chain rests on a page holding a hidden-only form it did not
        follow (a refused landing), then *retry*, a second post of the same form."""
        dashboard = '<form method="post" action="/logout"><input type="hidden" name="csrf"></form>'
        return [
            *self._login_entries(),
            _entry(
                "GET",
                "https://api.myshop.example.com/dashboard",
                content_type="text/html",
                body=dashboard,
            ),
            retry,
        ]

    def _login_entries(self, landing: str = "/dashboard") -> list[dict]:
        return [
            _entry(
                "GET",
                "https://api.myshop.example.com/login",
                content_type="text/html",
                body=self._LOGIN_PAGE,
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/session",
                status=302,
                response_headers={"Location": landing},
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
        ]

    def test_a_later_post_answering_a_redirect_is_not_the_login_s_landing(
        self, tmp_path: Path
    ) -> None:
        """A change-password POST after the login answers 302; the login still
        lands on /dashboard."""
        change = (
            '<form action="/account/password" method="post">'
            '<input type="password" name="current_password" autocomplete="current-password">'
            '<input type="password" name="new_password" autocomplete="new-password"></form>'
        )
        entries = [
            *self._login_entries(),
            _entry("GET", "https://api.myshop.example.com/dashboard", set_cookies=["s=v; Path=/"]),
            _entry(
                "GET",
                "https://api.myshop.example.com/account/password",
                content_type="text/html",
                body=change,
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/account/password",
                status=302,
                response_headers={"Location": "/account/password/done"},
                post_data=json.dumps({"current_password": "x", "new_password": "y"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert 'success_url="*/dashboard*",' in self._plugin(result)

    def test_a_later_post_off_the_chain_is_not_a_hop(self, tmp_path: Path) -> None:
        """Chain: a POST with no password field answering 302 soon after the
        login does not continue its chain, so the landing stays /dashboard."""
        entries = [
            *self._login_entries(),
            _entry("GET", "https://api.myshop.example.com/dashboard", set_cookies=["s=v; Path=/"]),
            _entry(
                "POST",
                "https://api.myshop.example.com/cart/add",
                status=302,
                response_headers={"Location": "/cart"},
                post_data=json.dumps({"sku": "x"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert 'success_url="*/dashboard*",' in self._plugin(result)

    def test_the_markdown_marks_an_observation_outside_the_login(self, tmp_path: Path) -> None:
        """A later POST's redirect is listed, and says it is not part of the login."""
        entries = [
            *self._login_entries(),
            _entry(
                "GET",
                "https://api.myshop.example.com/cart",
                status=302,
                response_headers={"Location": "/cart/view"},
                content_type="text/html",
                body="",
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        observations = [
            line
            for line in render_markdown(result).splitlines()
            if "] redirect" in line or "] credential_post" in line
        ]
        cart = [line for line in observations if "/cart" in line]
        session = [line for line in observations if "/session" in line]
        assert cart and all(line.endswith("(not part of the login)") for line in cart)
        assert session and not any("not part of the login" in line for line in session)

    def test_a_password_post_on_the_login_s_next_hop_is_not_a_hop(self, tmp_path: Path) -> None:
        """A POST with a password field is never a redirect hop of another post,
        even when its path is where the login redirected."""
        entries = [
            *self._login_entries(landing="/account/security"),
            _entry(
                "POST",
                "https://api.myshop.example.com/account/security",
                status=302,
                response_headers={"Location": "/account/security/done"},
                post_data=json.dumps({"current_password": "x", "new_password": "y"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert 'success_url="*/account/security*",' in self._plugin(result)
        assert "security/done" not in self._plugin(result)

    def test_each_hop_of_the_login_s_redirect_chain_belongs_to_it(self, tmp_path: Path) -> None:
        """The chain /step1, /step2, then /app ends at /app: each hop continues it."""
        entries = [
            *self._login_entries(landing="/step1"),
            _entry(
                "GET",
                "https://api.myshop.example.com/step1",
                status=302,
                response_headers={"Location": "/step2"},
                content_type="text/html",
                body="",
            ),
            _entry(
                "GET",
                "https://api.myshop.example.com/step2",
                status=302,
                response_headers={"Location": "/app"},
                content_type="text/html",
                body="",
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert 'success_url="*/app*",' in self._plugin(result)

    def test_an_oauth_form_post_chain_ends_at_the_app(self, tmp_path: Path) -> None:
        """An IdP answers 200 with a form that posts the code to the app's callback:
        the chain continues through that POST."""
        login = (
            '<form action="/u/login" method="post"><input type="email" name="username" id="u">'
            '<input type="password" name="password" id="p"></form>'
        )
        resume = (
            '<form method="post" action="https://api.myshop.example.com/callback">'
            '<input type="hidden" name="code" value="c"><input type="hidden" name="state" '
            'value="s"><noscript><input type="submit" value="Continue"></noscript></form>'
            "<script>document.forms[0].submit()</script>"
        )
        entries = [
            _entry(
                "GET",
                "https://auth.myshop.example.com/u/login",
                content_type="text/html",
                body=login,
            ),
            _entry(
                "POST",
                "https://auth.myshop.example.com/u/login",
                status=302,
                response_headers={"Location": "/authorize/resume"},
                post_data=json.dumps({"username": "alice", "password": "x"}),
            ),
            _entry(
                "GET",
                "https://auth.myshop.example.com/authorize/resume",
                content_type="text/html",
                body=resume,
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/callback",
                status=302,
                response_headers={"Location": "/app"},
                post_data=json.dumps({"code": "c", "state": "s"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert 'success_url="*/app*",' in self._plugin(result)

    def _provider_login(self, first_hop: str) -> list[dict]:
        """An app GET to *first_hop* on api. redirects to the identity provider,
        which redirects to its login form page on auth.; the login posts there."""
        login = (
            '<form action="/u/login" method="post"><input type="email" name="username" id="u">'
            '<input type="password" name="password" id="p"></form>'
        )
        return [
            _entry(
                "GET",
                f"https://api.myshop.example.com{first_hop}",
                status=302,
                response_headers={"Location": "https://auth.myshop.example.com/authorize?state=s"},
                content_type="text/html",
                body="",
            ),
            _entry(
                "GET",
                "https://auth.myshop.example.com/authorize?state=s",
                status=302,
                response_headers={"Location": "/u/login?state=s"},
                content_type="text/html",
                body="",
            ),
            _entry(
                "GET",
                "https://auth.myshop.example.com/u/login?state=s",
                content_type="text/html",
                body=login,
            ),
            _entry(
                "POST",
                "https://auth.myshop.example.com/u/login",
                status=302,
                response_headers={"Location": "https://api.myshop.example.com/app"},
                post_data=json.dumps({"username": "alice", "password": "x"}),
            ),
        ]

    def test_a_provider_s_form_page_is_opened_through_the_app_get_that_led_to_it(
        self, tmp_path: Path
    ) -> None:
        """The provider's form page needs the state the app's redirect gave it, so
        LoginConfig.url is the app GET that redirected there."""
        result = digest(DigestSource.from_har(_write_har(tmp_path, self._provider_login("/login"))))
        code = self._plugin(result)
        assert 'url="/login",' in code
        assert "auth.myshop.example.com/u/login" not in code

    def test_the_last_app_get_of_the_chain_is_the_login_url(self, tmp_path: Path) -> None:
        """/account redirects to /login, which redirects to the provider: the login
        starts at /login, the app GET that left for the provider."""
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/account",
                status=302,
                response_headers={"Location": "/login"},
                content_type="text/html",
                body="",
            ),
            *self._provider_login("/login"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert 'url="/login",' in self._plugin(result)

    @pytest.mark.parametrize(
        "action",
        [
            "javascript:go('alice.secret@realuser-account.example', 'ref')",
            "javascript:go('alice.secret@realuser-account.example', 'f3a9c2e1b7d4a6f0e2c8b1d9')",
        ],
        ids=["email", "email-and-token"],
    )
    def test_an_email_in_a_multi_argument_action_reaches_no_output(
        self, tmp_path: Path, action: str
    ) -> None:
        """Neither the email nor the token is in the markdown, the JSON, the
        projection, or any file of the generated project."""
        page = (
            f'<form action="{action}"><input name="email">'
            '<input type="password" name="password"><button>Go</button></form>'
        )
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/login", content_type="text/html", body=page
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        files = render(
            ScaffoldSpec(
                name="myshop",
                mode="new_project",
                backend="nodriver",
                base_url="https://api.myshop.example.com",
                digest=result,
            )
        )
        assert "login_config = LoginConfig(" in files["src/graftpunk_myshop/plugin.py"]
        outputs = [render_markdown(result), render_json(result), render_endpoints_json(result)]
        outputs += list(files.values())
        for text in outputs:
            for planted in ("alice.secret", "realuser-account", "f3a9c2e1b7d4a6f0e2c8b1d9"):
                assert planted not in text, planted

    def test_a_token_in_a_javascript_action_never_reaches_the_generated_plugin(
        self, tmp_path: Path
    ) -> None:
        token = "f3a9c2e1b7d4a6f0e2c8b1d9"  # noqa: S105 - a planted token, not a secret
        page = (
            f'<form action="javascript:login(\'{token}\')"><input name="email">'
            '<input type="password" name="password"><button>Go</button></form>'
        )
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/login", content_type="text/html", body=page
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert token in result.login_forms[0].action
        code = self._plugin(result)
        assert "login_config = LoginConfig(" in code
        assert token not in code

    def test_a_logout_that_redirected_to_the_provider_is_not_the_login_url(
        self, tmp_path: Path
    ) -> None:
        """Logging out redirected to the provider's login form page: opening /logout
        would log out, so the login keeps the provider's page."""
        entries = self._provider_login("/login")
        entries[0]["request"]["url"] = "https://api.myshop.example.com/logout"
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        code = self._plugin(result)
        assert 'url="/logout",' not in code
        assert (
            "/logout"
            not in next(f for f in result.login_forms if "password" in f.fields).opened_from
        )

    def test_the_last_app_get_before_a_logout_hop_is_skipped_past(self, tmp_path: Path) -> None:
        """/login redirects to /sign-out on the app's host, which redirects to the
        provider: the logout hop is skipped, and /login is the login URL."""
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/login",
                status=302,
                response_headers={"Location": "https://api.myshop.example.com/sign-out"},
                content_type="text/html",
                body="",
            ),
            _entry(
                "GET",
                "https://api.myshop.example.com/sign-out",
                status=302,
                response_headers={"Location": "https://auth.myshop.example.com/authorize?state=s"},
                content_type="text/html",
                body="",
            ),
            *self._provider_login("/unused")[1:],
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert 'url="/login",' in self._plugin(result)

    def test_a_same_host_redirect_to_the_login_page_keeps_the_page_as_the_url(
        self, tmp_path: Path
    ) -> None:
        """/account redirects to /login on the same host: the form page opens on its
        own, so LoginConfig.url stays /login."""
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/account",
                status=302,
                response_headers={"Location": "/login"},
                content_type="text/html",
                body="",
            ),
            *self._login_entries(),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert 'url="/login",' in self._plugin(result)

    def test_forms_on_the_landing_page_do_not_continue_the_chain(self, tmp_path: Path) -> None:
        """Only an OAuth form_post-shaped page continues the chain: a cart form with a
        visible field and a logout form with a visible button do not."""
        dashboard = (
            '<form method="post" action="/cart/add"><input type="hidden" name="sku" value="1">'
            '<input type="number" name="qty"></form>'
            '<form method="post" action="/logout"><input type="hidden" name="csrf" value="t">'
            "<button>Log out</button></form>"
            '<form method="get" action="/search"><input type="hidden" name="q"></form>'
        )
        entries = [
            *self._login_entries(),
            _entry(
                "GET",
                "https://api.myshop.example.com/dashboard",
                content_type="text/html",
                body=dashboard,
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/logout",
                status=302,
                response_headers={"Location": "/login"},
                post_data=json.dumps({"csrf": "t"}),
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/cart/add",
                status=302,
                response_headers={"Location": "/cart"},
                post_data=json.dumps({"sku": "1", "qty": 2}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert 'success_url="*/dashboard*",' in self._plugin(result)

    def test_a_post_carrying_more_than_the_form_s_hidden_names_is_not_its_submission(
        self, tmp_path: Path
    ) -> None:
        """A form_post-shaped form on the identity provider's page, then a POST to the
        app carrying a name the form does not hold: not that form's submission, so
        the chain rests on a page holding a form it did not follow, and the login
        takes no landing."""
        consent = (
            '<form method="post" action="https://api.myshop.example.com/callback">'
            '<input type="hidden" name="code"></form>'
        )
        entries = [
            *self._login_entries(landing="https://auth.myshop.example.com/consent"),
            _entry(
                "GET",
                "https://auth.myshop.example.com/consent",
                content_type="text/html",
                body=consent,
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/callback",
                status=302,
                response_headers={"Location": "/app"},
                post_data=json.dumps({"code": "c", "note": "n"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        code = self._plugin(result)
        assert "*/app*" not in code
        assert "success_url=" not in code
        assert "GP-FILL: success_url" in code

    def test_a_same_host_hidden_only_form_does_not_continue_the_chain(self, tmp_path: Path) -> None:
        """A logout form of hidden inputs only, submitted by script right after the
        login's last hop, posts to the host that served it: not an OAuth form_post,
        which crosses from the identity provider to the app. The chain rests on a page
        holding that unfollowed form, so the login takes no landing, and never
        /login."""
        dashboard = '<form method="post" action="/logout"><input type="hidden" name="csrf"></form>'
        entries = [
            *self._login_entries(),
            _entry(
                "GET",
                "https://api.myshop.example.com/dashboard",
                content_type="text/html",
                body=dashboard,
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/logout",
                status=302,
                response_headers={"Location": "/login"},
                post_data=json.dumps({"csrf": "t"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        code = self._plugin(result)
        assert "*/login*" not in code
        assert "success_url=" not in code
        assert "GP-FILL: success_url" in code

    def test_observation_urls_spell_the_host_as_endpoints_do(self, tmp_path: Path) -> None:
        """A login recorded at API.MYSHOP...:443: each observation, the form's source,
        and the endpoint all name api.myshop.example.com."""
        entries = self._login_entries()
        for entry in entries:
            entry["request"]["url"] = entry["request"]["url"].replace(
                "https://api.myshop.example.com", "https://API.MYSHOP.example.com:443"
            )
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert {o.url for o in result.login} == {
            "https://api.myshop.example.com/login",
            "https://api.myshop.example.com/session",
        }
        assert {e.host for e in result.endpoints} == {"api.myshop.example.com"}
        assert result.login_forms[0].source == "https://api.myshop.example.com/login"
        assert "API.MYSHOP" not in render_markdown(result)

    @pytest.mark.parametrize(
        "base_url",
        ["https://API.myshop.example.com", "https://api.myshop.example.com:443"],
        ids=["upper-case", "default-port"],
    )
    def test_the_login_page_is_a_path_on_the_base_url_s_host_however_spelled(
        self, tmp_path: Path, base_url: str
    ) -> None:
        """The login page is on the base URL's host, spelled differently: url is the
        path, not the absolute URL."""
        result = digest(DigestSource.from_har(_write_har(tmp_path, self._login_entries())))
        code = render(
            ScaffoldSpec(
                name="myshop",
                mode="new_project",
                backend="nodriver",
                base_url=base_url,
                digest=result,
            )
        )["src/graftpunk_myshop/plugin.py"]
        assert 'url="/login",' in code

    def test_a_later_post_answering_200_does_not_bring_back_a_refused_landing(
        self, tmp_path: Path
    ) -> None:
        """The first post's landing was refused; the second post answers 200 and
        decides alone: no landing, and the comment says no redirect followed it."""
        retry = _entry(
            "POST",
            "https://api.myshop.example.com/session",
            post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
        )
        entries = self._refused_then_retried(retry)
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        code = self._plugin(result)
        assert "success_url=" not in code
        assert "observed no redirect after the credential post" in self._comments(code)

    def _saml_entries(self, *, acs: bool) -> list[dict]:
        """A SAML POST-binding login: the credential post answers 200 with a form the
        browser posts to the app's /acs, which redirects to /app (when *acs*)."""
        binding = (
            '<form method="post" action="https://api.myshop.example.com/acs">'
            '<input type="hidden" name="SAMLResponse" value="r"></form>'
        )
        entries = [
            _entry(
                "GET",
                "https://idp.myshop.example.com/login",
                content_type="text/html",
                body=self._LOGIN_PAGE,
            ),
            _entry(
                "POST",
                "https://idp.myshop.example.com/session",
                content_type="text/html",
                body=binding,
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
        ]
        if acs:
            entries.append(
                _entry(
                    "POST",
                    "https://api.myshop.example.com/acs",
                    status=302,
                    response_headers={"Location": "/app"},
                    post_data=json.dumps({"SAMLResponse": "r"}),
                )
            )
        return entries

    def test_a_200_credential_post_followed_by_a_redirect_says_so(self, tmp_path: Path) -> None:
        """The redirect after a SAML POST binding is observed, but no chain follows a
        200: the GP-FILL says the post answered 200, never that no redirect was
        observed."""
        entries = self._saml_entries(acs=True)
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        comments = self._comments(self._plugin(result))
        assert "The credential post answered 200, so no redirect chain follows it." in comments
        assert "observed no redirect" not in comments

    def test_a_200_credential_post_followed_by_nothing_says_no_redirect(
        self, tmp_path: Path
    ) -> None:
        entries = self._saml_entries(acs=False)
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        comments = self._comments(self._plugin(result))
        assert "This run observed no redirect after the credential post." in comments
        assert "answered 200" not in comments

    def test_a_landing_on_the_login_page_itself_is_no_success_url(self, tmp_path: Path) -> None:
        """A later failed attempt redirects back to /login: a failed login matches
        */login*, so no pattern is written, and the GP-FILL says why."""
        failed = _entry(
            "POST",
            "https://api.myshop.example.com/session",
            status=302,
            response_headers={"Location": "/login/"},
            post_data=json.dumps({"email": "alice@example.com", "password": "wrong"}),
        )
        entries = [*self._login_entries(), failed]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        code = self._plugin(result)
        assert "success_url=" not in code
        assert "glob would match the login page" in self._comments(code)

    def test_a_landing_on_the_page_the_login_opens_at_is_no_success_url(
        self, tmp_path: Path
    ) -> None:
        """The provider's login redirects back to the app's /signin, the page the login
        opens at (its own form page is /u/login): no pattern."""
        entries = self._provider_login("/signin")
        entries[-1]["response"]["headers"] = [
            {"name": "Content-Type", "value": "application/json"},
            {"name": "Location", "value": "https://api.myshop.example.com/signin"},
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        code = self._plugin(result)
        assert "success_url=" not in code
        assert "glob would match the login page" in self._comments(code)

    def test_a_glob_that_would_match_the_login_page_is_no_success_url(self, tmp_path: Path) -> None:
        """A login at /account/login lands on /account: */account* matches
        /account/login?error=1, so a failed login would pass it."""
        page = self._LOGIN_PAGE.replace('action="/session"', 'action="/account/session"')
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/account/login",
                content_type="text/html",
                body=page,
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/account/session",
                status=302,
                response_headers={"Location": "/account"},
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        code = self._plugin(result)
        assert "success_url=" not in code
        assert "would match the login page" in self._comments(code)

    def test_a_saved_page_source_s_file_path_is_not_a_login_page(self, tmp_path: Path) -> None:
        """The form came from a saved page source under an account/ directory: its
        file path is no URL the login returns to, so */account* is kept."""
        saved = tmp_path / "account" / "page-source.html"
        saved.parent.mkdir()
        saved.write_text(self._LOGIN_PAGE)
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/session",
                status=302,
                response_headers={"Location": "/account"},
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            )
        ]
        source = DigestSource(har_path=_write_har(tmp_path, entries), page_source=saved)
        result = digest(source)
        assert result.login_forms[0].source == str(saved)
        assert 'success_url="*/account*",' in self._plugin(result)

    def test_a_root_landing_is_not_mistaken_for_the_login_page(self, tmp_path: Path) -> None:
        """No page the login opens at was recorded, so nothing names /: a landing on
        the root keeps its own reason."""
        result = digest(
            DigestSource.from_har(_write_har(tmp_path, self._login_entries(landing="/")))
        )
        comments = self._comments(self._plugin(result))
        assert "has no literal path segment to match on" in comments
        assert "would match the login page" not in comments

    def test_a_later_post_s_own_redirect_is_the_landing(self, tmp_path: Path) -> None:
        """The first post's landing was refused; the second post redirects to /app,
        and its own chain decides."""
        retry = _entry(
            "POST",
            "https://api.myshop.example.com/session",
            status=302,
            response_headers={"Location": "/app"},
            post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
        )
        entries = self._refused_then_retried(retry)
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        code = self._plugin(result)
        assert 'success_url="*/app*",' in code
        assert "*/dashboard*" not in code

    def test_a_form_post_callback_answering_200_takes_no_landing(self, tmp_path: Path) -> None:
        """The identity provider's form posts to the app's callback, which answers 200:
        the client rests on the callback, not on the provider's page, so the login
        takes no landing."""
        entries = self._form_post_chain(
            "https://api.myshop.example.com/callback", "https://api.myshop.example.com/callback"
        )
        callback = entries[-1]
        callback["response"]["status"] = 200
        callback["response"]["headers"] = [
            h for h in callback["response"]["headers"] if h["name"].lower() != "location"
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        code = self._plugin(result)
        assert "*/authorize/resume*" not in code
        assert "success_url=" not in code
        comments = self._comments(code)
        assert "the last hop answered 200" in comments
        assert "observed no redirect" not in comments

    def test_a_submission_answering_a_page_that_the_chain_follows_on_keeps_the_landing(
        self, tmp_path: Path
    ) -> None:
        """The callback answers 200 with another form_post form, whose submission
        redirects to /app: the chain ends on that redirect, and /app is the landing."""
        entries = self._form_post_chain(
            "https://api.myshop.example.com/callback", "https://api.myshop.example.com/callback"
        )
        callback = entries[-1]
        callback["response"]["status"] = 200
        callback["response"]["headers"] = [{"name": "Content-Type", "value": "text/html"}]
        page = (
            '<form method="post" action="https://auth.myshop.example.com/continue">'
            '<input type="hidden" name="ticket" value="t"></form>'
        )
        callback["response"]["content"] = {"mimeType": "text/html", "text": page, "size": 1}
        entries.append(
            _entry(
                "POST",
                "https://auth.myshop.example.com/continue",
                status=302,
                response_headers={"Location": "/app"},
                post_data=json.dumps({"ticket": "t"}),
            )
        )
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert 'success_url="*/app*",' in self._plugin(result)

    def test_a_same_host_identity_provider_s_form_post_takes_no_landing(
        self, tmp_path: Path
    ) -> None:
        """An identity provider under /auth on the app's own host answers 200 with a
        form_post form to /callback. Its submission stays on one host, so the chain
        does not follow it, and the login takes no landing rather than the
        provider's intermediate page."""
        login = (
            '<form action="/auth/login" method="post"><input type="email" name="username" '
            'id="u"><input type="password" name="password" id="p"></form>'
        )
        resume = (
            '<form method="post" action="/callback">'
            '<input type="hidden" name="code" value="c"><input type="hidden" name="state" '
            'value="s"><noscript><input type="submit" value="Continue"></noscript></form>'
        )
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/auth/login",
                content_type="text/html",
                body=login,
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/auth/login",
                status=302,
                response_headers={"Location": "/auth/resume"},
                post_data=json.dumps({"username": "alice", "password": "x"}),
            ),
            _entry(
                "GET",
                "https://api.myshop.example.com/auth/resume",
                content_type="text/html",
                body=resume,
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/callback",
                status=302,
                response_headers={"Location": "/app"},
                post_data=json.dumps({"code": "c", "state": "s"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        code = self._plugin(result)
        assert "*/auth/resume*" not in code
        assert "success_url=" not in code
        assert "GP-FILL: success_url" in code
        comments = self._comments(code)
        assert "came to rest on a page holding a form it did not follow" in comments
        assert "observed no redirect" not in comments

    def _form_post_chain(self, form_action: str, callback_url: str) -> list[dict]:
        """An identity provider's login on auth., whose resume page's form_post form
        posts to *form_action*; the browser's POST went to *callback_url*."""
        login = (
            '<form action="/u/login" method="post"><input type="email" name="username" id="u">'
            '<input type="password" name="password" id="p"></form>'
        )
        resume = (
            f'<form method="post" action="{form_action}">'
            '<input type="hidden" name="code" value="c"></form>'
        )
        return [
            _entry(
                "GET",
                "https://auth.myshop.example.com/u/login",
                content_type="text/html",
                body=login,
            ),
            _entry(
                "POST",
                "https://auth.myshop.example.com/u/login",
                status=302,
                response_headers={"Location": "/authorize/resume"},
                post_data=json.dumps({"username": "alice", "password": "x"}),
            ),
            _entry(
                "GET",
                "https://auth.myshop.example.com/authorize/resume",
                content_type="text/html",
                body=resume,
            ),
            _entry(
                "POST",
                callback_url,
                status=302,
                response_headers={"Location": "/app"},
                post_data=json.dumps({"code": "c"}),
            ),
        ]

    def test_a_form_post_action_s_host_is_compared_without_case_or_a_default_port(
        self, tmp_path: Path
    ) -> None:
        """The form posts to API.myshop...:443/callback; the POST to
        api.myshop.../callback is its submission."""
        entries = self._form_post_chain(
            "https://API.myshop.example.com:443/callback", "https://api.myshop.example.com/callback"
        )
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert 'success_url="*/app*",' in self._plugin(result)

    @pytest.mark.parametrize(
        "callback_url",
        [
            "https://API.MYSHOP.example.com/callback",
            "https://api.myshop.example.com:443/callback",
            "https://API.MYSHOP.example.com:443/callback",
        ],
        ids=["upper-case", "default-port", "both"],
    )
    def test_a_form_post_submission_s_host_is_compared_without_case_or_a_default_port(
        self, tmp_path: Path, callback_url: str
    ) -> None:
        """The form posts to api.myshop.../callback; the recorded POST went to the same
        host spelled in upper case, with :443, or both, and is its submission."""
        entries = self._form_post_chain("https://api.myshop.example.com/callback", callback_url)
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["third_party"] == 0
        assert 'success_url="*/app*",' in self._plugin(result)

    def test_a_200_login_takes_no_landing_even_through_an_auto_submit_form(
        self, tmp_path: Path
    ) -> None:
        """A credential post answering 200 starts no chain, whatever its body holds."""
        auto_submit = (
            '<form method="post" action="https://api.myshop.example.com/callback">'
            '<input type="hidden" name="code" value="c"></form>'
        )
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/login",
                content_type="text/html",
                body=self._LOGIN_PAGE,
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/session",
                content_type="text/html",
                body=auto_submit,
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/callback",
                status=302,
                response_headers={"Location": "/app"},
                post_data=json.dumps({"code": "c"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        code = self._plugin(result)
        assert "success_url=" not in code
        assert "GP-FILL: success_url" in code

    def test_a_hop_s_host_is_compared_without_case_or_a_default_port(self, tmp_path: Path) -> None:
        """The login redirects to API.MYSHOP...:443/step1; the hop to api.myshop.../step1
        continues the chain."""
        entries = [
            *self._login_entries(landing="https://API.myshop.example.com:443/step1"),
            _entry(
                "GET",
                "https://api.myshop.example.com/step1",
                status=302,
                response_headers={"Location": "/app"},
                content_type="text/html",
                body="",
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert 'success_url="*/app*",' in self._plugin(result)

    def test_a_hop_must_be_on_the_host_the_chain_went_to(self, tmp_path: Path) -> None:
        """The login redirects to auth's /step1; a request to /step1 on another host
        is not a hop."""
        entries = [
            *self._login_entries(landing="https://auth.myshop.example.com/step1"),
            _entry(
                "GET",
                "https://api.myshop.example.com/step1",
                status=302,
                response_headers={"Location": "/elsewhere"},
                content_type="text/html",
                body="",
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert 'success_url="*/step1*",' in self._plugin(result)

    def test_a_tie_on_specificity_goes_to_the_form_covering_the_post(self, tmp_path: Path) -> None:
        """The header form is hidden on the login page, so each page's matching form
        is on one page; the one covering the post's body names wins over nearness."""
        header = (
            '<form action="/session" method="post" class="mini">'
            '<input type="email" name="login[username]" id="mini-user">'
            '<input type="password" name="login[password]" id="mini-pass"></form>'
        )
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/login",
                content_type="text/html",
                body=self._LOGIN_PAGE,
            ),
            _entry(
                "GET",
                "https://api.myshop.example.com/catalog",
                content_type="text/html",
                body=header,
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/session",
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags[("GET", "/login")] is True
        assert flags[("GET", "/catalog")] is False

    def test_a_page_ranks_by_its_best_single_form(self, tmp_path: Path) -> None:
        """A page's rank is one form's (pages, coverage), never the fewest pages of
        one form with the best coverage of another: /a's page-specific form covers
        one body name and its site-wide form two; /b's page-specific form covers two."""
        site_wide = (
            '<form action="/session" method="post" class="mini">'
            '<input type="email" name="email" id="mini-e">'
            '<input type="password" name="password" id="mini-p"></form>'
        )
        specific_a = (
            '<form action="/session" method="post"><input type="text" name="user_x" id="ax">'
            '<input type="password" name="password" id="ap"></form>'
        )
        specific_b = (
            '<form action="/session" method="post"><input type="email" name="email" id="be">'
            '<input type="password" name="password" id="bp"><input type="hidden" name="t">'
            "</form>"
        )
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/c", content_type="text/html", body=site_wide
            ),
            _entry(
                "GET", "https://api.myshop.example.com/d", content_type="text/html", body=site_wide
            ),
            _entry(
                "GET", "https://api.myshop.example.com/b", content_type="text/html", body=specific_b
            ),
            _entry(
                "GET",
                "https://api.myshop.example.com/a",
                content_type="text/html",
                body=site_wide + specific_a,
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/session",
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags[("GET", "/b")] is True
        assert flags[("GET", "/a")] is False

    def test_a_script_posted_login_to_another_action_is_owned(self, tmp_path: Path) -> None:
        """The form says /auth/login, script posts /api/v1/sessions."""
        form = (
            '<form action="/auth/login" method="post"><input type="email" name="email" id="email">'
            '<input type="password" name="password" id="pass"></form>'
        )
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/login", content_type="text/html", body=form
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/api/v1/sessions",
                status=302,
                response_headers={"Location": "/app"},
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags[("POST", "/api/v1/sessions")] is True
        code = self._plugin(result)
        assert 'success_url="*/app*",' in code
        assert "def api_v1_sessions(" not in code

    @pytest.mark.parametrize(
        ("path", "body", "status", "location"),
        [
            (
                "/api/account/email",
                {"email": "alice@example.com", "password": "x"},
                302,
                "/account",
            ),
            ("/api/account/delete", {"password": "x"}, 303, "/goodbye"),
        ],
        ids=["change-email", "delete-account"],
    )
    def test_a_later_password_confirmed_action_is_not_absorbed_into_the_login(
        self, tmp_path: Path, path: str, body: dict, status: int, location: str
    ) -> None:
        """Once the login posted to its form, a later script post found by its
        field names alone is not the login's."""
        entries = [
            *self._login_entries(),
            _entry(
                "POST",
                f"https://api.myshop.example.com{path}",
                status=status,
                response_headers={"Location": location},
                post_data=json.dumps(body),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags[("POST", path)] is False
        assert 'success_url="*/dashboard*",' in self._plugin(result)

    @pytest.mark.parametrize("form_open", ['<form method="post">', "<form>"], ids=["self", "none"])
    def test_password_confirmed_actions_keep_their_commands_whatever_the_form_action(
        self, tmp_path: Path, form_open: str
    ) -> None:
        """The login form posts to its own page, or has no action at all: later
        password-confirmed actions found by their field names are still their own."""
        form = (
            f'{form_open}<input type="email" name="email" id="email">'
            '<input type="password" name="password" id="pass"></form>'
        )
        login_post = (
            _entry(
                "POST",
                "https://api.myshop.example.com/login",
                status=302,
                response_headers={"Location": "/app"},
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            )
            if form_open == '<form method="post">'
            else _entry(
                "POST",
                "https://api.myshop.example.com/api/v1/sessions",
                status=302,
                response_headers={"Location": "/app"},
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            )
        )
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/login", content_type="text/html", body=form
            ),
            login_post,
            _entry(
                "POST",
                "https://api.myshop.example.com/api/account/email",
                status=302,
                response_headers={"Location": "/account"},
                post_data=json.dumps({"email": "bob@example.com", "password": "x"}),
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/api/account/delete",
                status=303,
                response_headers={"Location": "/goodbye"},
                post_data=json.dumps({"password": "x"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags[("POST", "/api/account/email")] is False
        assert flags[("POST", "/api/account/delete")] is False
        assert 'success_url="*/app*",' in self._plugin(result)

    def test_only_the_first_script_login_target_is_owned(self, tmp_path: Path) -> None:
        """No post went to the form's action; the first field-name-only post's
        target is the login's, and a later one to another target is not."""
        form = (
            '<form action="/auth/login" method="post"><input type="email" name="email" id="email">'
            '<input type="password" name="password" id="pass"></form>'
        )
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/login", content_type="text/html", body=form
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/api/v1/sessions",
                status=302,
                response_headers={"Location": "/app"},
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/api/account/delete",
                status=303,
                response_headers={"Location": "/goodbye"},
                post_data=json.dumps({"password": "x"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags[("POST", "/api/v1/sessions")] is True
        assert flags[("POST", "/api/account/delete")] is False
        assert 'success_url="*/app*",' in self._plugin(result)

    def test_a_script_login_from_an_actionless_form_beats_a_later_self_posting_form(
        self, tmp_path: Path
    ) -> None:
        """The login page's form has no action and script posts /api/login; a later
        change-email form posts to its own page. The earliest post is the login's,
        so login_config is the login page's form, and the change-email post keeps
        its command."""
        login = (
            '<form><input type="email" name="email" id="email">'
            '<input type="password" name="password" id="pass"><button>Sign in</button></form>'
        )
        change_email = (
            '<form method="post"><input type="email" name="new_email" id="new-email">'
            '<input type="email" name="confirm_email" id="confirm-email">'
            '<input type="password" name="password" id="confirm-pass"><button>Save</button></form>'
        )
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/login", content_type="text/html", body=login
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/api/login",
                content_type="application/json",
                body='{"ok": true}',
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
            _entry(
                "GET",
                "https://api.myshop.example.com/account/email",
                content_type="text/html",
                body=change_email,
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/account/email",
                status=302,
                response_headers={"Location": "/account"},
                post_data=json.dumps(
                    {
                        "new_email": "bob@example.com",
                        "confirm_email": "bob@example.com",
                        "password": "x",
                    }
                ),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        selected = next(f for f in result.login_forms if "password" in f.fields)
        assert selected.source == "https://api.myshop.example.com/login"
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags[("POST", "/api/login")] is True
        assert flags[("POST", "/account/email")] is False
        code = self._plugin(result)
        assert 'url="/login",' in code
        assert "def account_email(" in code

    def test_a_form_with_its_own_target_is_not_the_script_login_s_form(
        self, tmp_path: Path
    ) -> None:
        """The login page holds a form posting to /auth/legacy and an actionless form;
        script posts /api/login from the page. The actionless form is the one script
        sent, so login_config is built from it."""
        page = (
            '<form action="/auth/legacy" method="post"><input type="email" name="email" '
            'id="legacy-email"><input type="password" name="password" id="legacy-pass"></form>'
            '<form><input type="email" name="email" id="email">'
            '<input type="password" name="password" id="pass"></form>'
        )
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/login", content_type="text/html", body=page
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/api/login",
                status=302,
                response_headers={"Location": "/app"},
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        selected = next(f for f in result.login_forms if "password" in f.fields)
        assert selected.fields["username"] == "#email"

    def test_a_post_to_a_form_s_target_is_not_an_actionless_form_s_post(
        self, tmp_path: Path
    ) -> None:
        """The login page holds an actionless form before the form posting to
        /session; a POST to /session is that form's, so login_config is built from
        it."""
        page = (
            '<form class="mini"><input type="email" name="email" id="mini-email">'
            '<input type="password" name="password" id="mini-pass"></form>'
            '<form action="/session" method="post"><input type="email" name="email" id="email">'
            '<input type="password" name="password" id="pass"></form>'
        )
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/login", content_type="text/html", body=page
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/session",
                status=302,
                response_headers={"Location": "/app"},
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        selected = next(f for f in result.login_forms if "password" in f.fields)
        assert selected.fields["username"] == "#email"

    def test_a_script_post_to_a_recorded_unselected_form_is_not_owned(self, tmp_path: Path) -> None:
        """A field-name-only post to a recorded form's action is not a script login,
        even when that form is not the one login_config uses."""
        main = (
            '<form action="/session" method="post"><input type="email" name="email" id="email">'
            '<input type="password" name="password" id="pass"></form>'
        )
        other = (
            '<form action="/api/other" method="post"><input type="email" name="email">'
            '<input type="password"><input type="password"></form>'
        )
        entries = [
            _entry(
                "GET", "https://api.myshop.example.com/login", content_type="text/html", body=main
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/search",
                content_type="text/html",
                body=other,
                post_data=json.dumps({"q": "x"}),
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/api/other",
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        # login_config is built from the first form with a password field.
        assert next(f for f in result.login_forms if "password" in f.fields).action == "/session"
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags[("POST", "/api/other")] is False

    def test_a_login_with_no_form_owns_every_credential_post(self, tmp_path: Path) -> None:
        """With no login form captured, every credential post is the login's."""
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/api/v1/sessions",
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags[("POST", "/api/v1/sessions")] is True

    def test_the_login_page_beats_a_nearer_page_with_only_the_header_form(
        self, tmp_path: Path
    ) -> None:
        """Prefer the page whose matching form is page-specific, then the nearest."""
        header = (
            '<form action="/session" method="post" class="mini">'
            '<input type="email" name="login[username]" id="mini-user">'
            '<input type="password" name="login[password]" id="mini-pass"></form>'
        )
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/login",
                content_type="text/html",
                body=header + self._LOGIN_PAGE,
            ),
            _entry(
                "GET",
                "https://api.myshop.example.com/catalog",
                content_type="text/html",
                body=header,
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/session",
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags[("GET", "/login")] is True
        assert flags[("GET", "/catalog")] is False
        assert result.login_forms[0].fields == {"username": "#email", "password": "#pass"}
        assert 'url="/login",' in self._plugin(result)

    def test_a_post_to_a_login_form_action_is_the_credential_post(self, tmp_path: Path) -> None:
        """The form's type="password" input names the field, so a name outside the
        password hints (passcode) still marks the POST to its action."""
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/signin",
                content_type="text/html",
                body=(
                    '<form action="/session" method="post">'
                    '<input type="email" name="email"><input type="password" name="passcode">'
                    "</form>"
                ),
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/session",
                post_data=json.dumps({"email": "alice@example.com", "passcode": "x"}),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (post,) = [o for o in result.login if o.method == "POST"]
        assert post.kind == "credential_post"
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags[("POST", "/session")] is True

    def test_the_same_path_under_another_method_is_not_flagged(self) -> None:
        (endpoint,) = _with_login_flow(
            (_flow_endpoint("/login", "DELETE"),),
            (_flow_observation("form_page", "GET", "/login"),),
        )
        assert endpoint.login_flow is False

    def test_a_login_path_inside_a_collapsed_family_is_flagged(self) -> None:
        """The high-cardinality collapse can re-template the endpoint the observation
        belongs to, so templating the observation's path alone would miss it."""
        family = _flow_endpoint("/account/{account_id}", examples=("/account/login",))
        (endpoint,) = _with_login_flow(
            (family,), (_flow_observation("form_page", "GET", "/account/login"),)
        )
        assert endpoint.login_flow is True

    def test_an_endpoint_the_login_flow_owns_under_only_one_of_its_methods_is_not_flagged(
        self,
    ) -> None:
        """digest() gives each endpoint one method; a hand-built endpoint with two is
        flagged only when the flow owns both."""
        two_methods = dataclasses.replace(_flow_endpoint("/login"), methods=("GET", "DELETE"))
        (endpoint,) = _with_login_flow(
            (two_methods,), (_flow_observation("form_page", "GET", "/login"),)
        )
        assert endpoint.login_flow is False

    def test_an_endpoint_with_no_methods_is_not_flagged(self) -> None:
        no_methods = dataclasses.replace(_flow_endpoint("/login"), methods=())
        (endpoint,) = _with_login_flow(
            (no_methods,), (_flow_observation("form_page", "GET", "/login"),)
        )
        assert endpoint.login_flow is False

    def test_a_redirect_or_auth_api_observation_flags_nothing(self) -> None:
        endpoints = (_flow_endpoint("/auth/callback"), _flow_endpoint("/api/session"))
        observations = (
            _flow_observation("redirect", "GET", "/auth/callback"),
            _flow_observation("auth_api", "GET", "/api/session"),
        )
        assert [e.login_flow for e in _with_login_flow(endpoints, observations)] == [False, False]

    def test_the_flag_defaults_to_false(self) -> None:
        assert _flow_endpoint("/anything").login_flow is False


# The rate script's shapes, loaded by path: scripts/ is not a package.
_RATES_SCRIPT = REPO_ROOT / "scripts" / "cookie_value_rates.py"


def _rate_shapes(seed: str) -> dict[str, Callable[[], str]]:
    spec = importlib.util.spec_from_file_location("cookie_value_rates", _RATES_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.shapes(random.Random(seed))  # noqa: S311 - a fixed sample


# shape (scripts/cookie_value_rates.py): the share of random values the
# form-state rule accepts and reads_as_words rejects. Each ceiling is the share
# over 100,000 other values of the shape (2026-10-24) plus three standard errors
# of the test's 10,000-value sample, sqrt(p * (1 - p) / 10,000), rounded up to 4
# places; a shape with no loss in 100,000 has a ceiling of 0. Words in capitals,
# alone or as a name, are told from random capitals only by spelling, which is
# why the capital shapes lose most.
_FORM_STATE_LOSS_CEILINGS = {
    "mixed-case 6": 0.0231,
    "mixed-case 10": 0.0043,
    "capitals and spaces 8": 0.1605,
    "capitals and name punctuation 8": 0.0632,
    "letters and name punctuation 8": 0.0052,
    "three words of 3 capitals": 0.0,
    "two capitalised words of 4": 0.0,
    "two words of 4 capitals": 0.1869,
    "3 capitals, capitalised 8": 0.0,
    '"3 capitals, capitalised 8"': 0.0758,
    "4 lower, 3 capitals, 5 lower": 0.0739,
    "5 lower, 5 capitals": 0.1041,
    "4 lower, 2 to 4 capitals, 4 lower": 0.0864,
    "three capitalised words of 3 to 5": 0.0272,
    "4 capitals, 4 lower, capitalised 6": 0.0492,
}


class TestFlaggedNamesOf:
    def test_flagged_names_are_exactly_the_digest_cookie_and_token_names(
        self, tmp_path: Path
    ) -> None:
        entry = {
            "startedDateTime": "2026-09-10T10:00:00.000Z",
            "time": 1,
            "request": {
                "method": "GET",
                "url": "https://myshop.example.com/account",
                "headers": [],
                "cookies": [],
                "queryString": [],
            },
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": [{"name": "Content-Type", "value": "text/html"}],
                "cookies": [{"name": "sid", "value": "planted"}],
                "content": {
                    "mimeType": "text/html",
                    "text": '<meta name="csrf-token" content="planted">',
                    "size": 42,
                },
            },
        }
        har = tmp_path / "network.har"
        har.write_text(json.dumps({"log": {"version": "1.2", "entries": [entry]}}))
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result) == tuple(
            sorted(set(result.cookies) | {t.name for t in result.tokens})
        )
        assert flagged_names_of(result) == ("csrf-token", "sid")

    def test_entries_add_cookies_the_digests_own_list_leaves_out(self, tmp_path: Path) -> None:
        """A static response and an out-of-scope host are outside d.cookies, which
        stays scoped; passing the entries widens only the flagged list."""
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://api.myshop.example.com/orders/1"),
            _entry(
                "GET",
                "https://api.myshop.example.com/assets/app.js",
                content_type="application/javascript",
                body="var a = 1;",
                set_cookies=["asset_cookie=xyz; Path=/"],
            ),
            _entry(
                "GET",
                "https://tracker.example.net/pixel",
                set_cookies=["other_host_cookie=xyz; Path=/"],
            ),
        ]
        har = _write_har(tmp_path, entries)
        result = digest(DigestSource.from_har(har))
        assert result.cookies == ()
        assert flagged_names_of(result) == ()
        assert flagged_names_of(result, parse_har_file(har).entries) == (
            "asset_cookie",
            "other_host_cookie",
        )

    def test_a_cookie_set_by_a_second_in_scope_host_is_flagged_too(self, tmp_path: Path) -> None:
        """flagged_names must not miss a cookie a first-party subdomain sets: an
        auth or widget host distinct from the primary one."""
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://api.myshop.example.com/orders/1"),
            _entry(
                "GET",
                "https://www.myshop.example.com/widget",
                content_type="text/plain",
                body="ok",
                set_cookies=["shop_session=xyz; Path=/; HttpOnly"],
            ),
        ]
        har = _write_har(tmp_path, entries)
        result = digest(DigestSource.from_har(har))
        assert result.primary_host == "api.myshop.example.com"
        assert "www.myshop.example.com" in result.hosts
        assert "shop_session" in result.cookies
        assert "shop_session" in flagged_names_of(result)

    @staticmethod
    def _with_cookies(
        response: list[tuple[str, str]], request: tuple[tuple[str, str], ...] = ()
    ) -> dict:
        entry = _entry("GET", "https://myshop.example.com/api/search")
        entry["response"]["cookies"] = [{"name": n, "value": v} for n, v in response]
        entry["request"]["cookies"] = [{"name": n, "value": v} for n, v in request]
        return entry

    def test_a_cookie_that_only_holds_words_is_left_out(self, tmp_path: Path) -> None:
        """A search form's state (#270): a term, a lot number, and a date are no
        credential, and their names are words an invented fixture keeps."""
        entry = self._with_cookies(
            [("text", "running%20shoes"), ("lot", "12"), ("since", "2026-10-01")],
            (("text", "running+shoes"), ("lot", "7")),
        )
        har = _write_har(tmp_path, [entry])
        result = digest(DigestSource.from_har(har))
        assert set(result.cookies) == {"text", "lot", "since"}
        assert flagged_names_of(result, parse_har_file(har).entries) == ()

    def test_session_and_csrf_cookies_stay_flagged_by_name(self, tmp_path: Path) -> None:
        """A session id's value does not read as words; a token-named cookie is
        flagged whatever its value."""
        entry = self._with_cookies(
            [("session", "Zq9XkLmPwR4t7Bv2Hn8Ks3Jd"), ("csrftoken", "planted"), ("lot", "12")]
        )
        har = _write_har(tmp_path, [entry])
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == ("csrftoken", "session")

    def test_one_value_that_does_not_read_as_words_flags_the_cookie(self, tmp_path: Path) -> None:
        entries = [
            self._with_cookies([("sid", "guest")]),
            self._with_cookies([], (("sid", "x7Kq29LpVb"),)),
        ]
        har = _write_har(tmp_path, entries)
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == ("sid",)

    @pytest.mark.parametrize(
        "entry",
        [
            _entry("GET", "https://myshop.example.com/api/search", set_cookies=["lot=12; Path=/"]),
            {
                **_entry("GET", "https://myshop.example.com/api/search"),
                "response": {
                    **_entry("GET", "https://myshop.example.com/api/search")["response"],
                    "cookies": [{"name": "lot", "value": ""}],
                },
            },
        ],
        ids=["set-cookie-header-only", "empty-value"],
    )
    def test_a_cookie_with_no_recorded_value_stays_flagged(
        self, tmp_path: Path, entry: dict
    ) -> None:
        """No value to judge is no evidence the cookie holds only words."""
        har = _write_har(tmp_path, [entry])
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == ("lot",)
        assert flagged_names_of(result) == ("lot",)

    def test_an_empty_value_beside_a_word_value_is_a_cleared_field(self, tmp_path: Path) -> None:
        """A search reset sends text= with an expiry; it holds nothing, so the
        cookie is judged on the values it did hold."""
        entries = [
            self._with_cookies([("text", "running%20shoes"), ("lot", "12")]),
            self._with_cookies([("text", "")], (("lot", ""),)),
        ]
        har = _write_har(tmp_path, entries)
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == ()

    @pytest.mark.parametrize(
        "name",
        [
            "myshop_session",
            "sessionid",
            "PHPSESSID",
            "JSESSIONID",
            "connect.sid",
            "laravel_session",
            "ASP.NET_SessionId",
            ".ASPXAUTH",
            "_session_id",
            "auth",
            "jwt",
            "login",
            "rememberMe",
            "XSRF-TOKEN",
            "sess",
        ],
    )
    def test_a_session_named_cookie_stays_flagged_whatever_its_value(
        self, tmp_path: Path, name: str
    ) -> None:
        har = _write_har(tmp_path, [self._with_cookies([(name, "lanterns")])])
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == (name,)

    @pytest.mark.parametrize("name", ["sidebar", "author", "inside", "outside", "assessment"])
    def test_a_name_holding_a_session_word_inside_another_word_is_judged_by_value(
        self, tmp_path: Path, name: str
    ) -> None:
        har = _write_har(tmp_path, [self._with_cookies([(name, "open")])])
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == ()

    @pytest.mark.parametrize(
        "set_cookie",
        [
            "visit=lanterns; Path=/; Secure; HttpOnly",
            "visit=lanterns; path=/; httponly; samesite=Lax",
            "lot=12; Path=/\nvisit=lanterns; Path=/; HTTPONLY",
        ],
        ids=["header-attribute", "lowercase-attribute", "second-line-of-a-joined-header"],
    )
    def test_an_httponly_set_cookie_header_flags_a_cookie_the_cookies_array_does_not_mark(
        self, tmp_path: Path, set_cookie: str
    ) -> None:
        """A HAR's response.cookies often leaves httpOnly out when the header
        carries it; absent there is unknown, so the header decides."""
        entry = _entry(
            "GET",
            "https://myshop.example.com/api/search",
            response_headers={"Set-Cookie": set_cookie},
        )
        entry["response"]["cookies"] = [
            {"name": "visit", "value": "lanterns"},
            {"name": "lot", "value": "12"},
        ]
        har = _write_har(tmp_path, [entry])
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == ("visit",)

    def test_a_cookie_only_on_a_later_line_of_a_joined_set_cookie_header_is_flagged(
        self, tmp_path: Path
    ) -> None:
        """Chromium joins several Set-Cookie headers with newlines. A cookie on the
        second line that response.cookies leaves out has no recorded value, so it
        is flagged; the first line's word-valued cookie is judged by value."""
        entry = _entry(
            "GET",
            "https://myshop.example.com/api/search",
            response_headers={
                "Set-Cookie": "lot=12; Path=/\nshopper=Zq9XkLmPwR4t7Bv2Hn8Ks3Jd; Path=/"
            },
        )
        entry["response"]["cookies"] = [{"name": "lot", "value": "12"}]
        har = _write_har(tmp_path, [entry])
        result = digest(DigestSource.from_har(har))
        assert "shopper" in result.cookies
        assert flagged_names_of(result, parse_har_file(har).entries) == ("shopper",)

    def test_a_cookie_neither_the_array_nor_the_header_marks_httponly_is_judged_by_value(
        self, tmp_path: Path
    ) -> None:
        entry = _entry(
            "GET",
            "https://myshop.example.com/api/search",
            set_cookies=[
                "lot=12; Path=/; Secure; SameSite=Lax",
                "text=running%20shoes; Path=/",
            ],
        )
        entry["response"]["cookies"] = [
            {"name": "lot", "value": "12", "secure": True},
            {"name": "text", "value": "running%20shoes"},
        ]
        har = _write_har(tmp_path, [entry])
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == ()

    def test_a_cookie_set_with_httponly_stays_flagged_whatever_its_value(
        self, tmp_path: Path
    ) -> None:
        """A server hides a cookie from scripts to protect it; Secure alone says
        nothing, since an HTTPS-only site can mark every cookie Secure."""
        entry = _entry("GET", "https://myshop.example.com/api/search")
        entry["response"]["cookies"] = [
            {"name": "visit", "value": "lanterns", "httpOnly": True, "secure": True},
            {"name": "lot", "value": "12", "secure": True},
        ]
        later = _entry("GET", "https://myshop.example.com/api/search")
        later["request"]["cookies"] = [{"name": "visit", "value": "lanterns"}]
        har = _write_har(tmp_path, [entry, later])
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == ("visit",)

    def test_a_search_forms_cleared_and_cased_word_cookies_are_left_out(
        self, tmp_path: Path
    ) -> None:
        """A property search's form state (#296): fields cleared to a quoted empty
        string, a name in capitals, a 4-letter code and a camelCase tab name
        hold no credential. The session cookie (HttpOnly), the CSRF cookie, header
        and hidden input (token names) stay flagged."""
        page = _entry(
            "GET",
            "https://records.example.com/search",
            content_type="text/html",
            body=(
                '<form method="post" action="/search">'
                '<input type="hidden" name="csrfmiddlewaretoken" value="planted">'
                "</form>"
            ),
            set_cookies=["sessionid=k3v9q2m7x1c8z4w6r5t0y2u8i3o7p1a9; Path=/; HttpOnly"],
        )
        page["response"]["cookies"] = [
            {"name": "sessionid", "value": "k3v9q2m7x1c8z4w6r5t0y2u8i3o7p1a9"},
            {"name": "csrftoken", "value": "Qm7Zr2Xk9Lp4Wv8Bn3Hd6Ts1Fy5Gc0Je"},
            {"name": "block", "value": '""'},
            {"name": "lot", "value": '""'},
            {"name": "subdivision", "value": '""'},
            {"name": "text", "value": '""'},
            {"name": "startDate", "value": '""'},
        ]
        search = _entry(
            "POST",
            "https://records.example.com/search",
            request_headers={"X-CSRFToken": "planted"},
        )
        search["response"]["cookies"] = [
            {"name": "text", "value": "corner%20lot"},
            {"name": "startDate", "value": "10/01/2026"},
            {"name": "name1", "value": "LANDOWNER"},
            {"name": "name1", "value": "GRANTEE"},
            {"name": "name1", "value": '"BRL Grantees"'},
            {"name": "name_type1", "value": "BRAL"},
            {"name": "currentTab", "value": "panelGRIDview"},
            {"name": "currentTab", "value": "GRIDviewPanels"},
        ]
        har = _write_har(tmp_path, [page, search])
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == (
            "X-CSRFToken",
            "csrfmiddlewaretoken",
            "csrftoken",
            "sessionid",
        )

    @pytest.mark.parametrize("cleared", ['""', "''", "%22%22", "%20", "-"])
    def test_a_value_of_punctuation_alone_holds_nothing(self, tmp_path: Path, cleared: str) -> None:
        """A field cleared to a quoted empty string is a value the server sent,
        and it holds nothing; a cookie only ever set so is not flagged."""
        har = _write_har(tmp_path, [self._with_cookies([("block", cleared)])])
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == ()

    @pytest.mark.parametrize(
        "value", ["kqzpwmxtrvbnhgfd", "xQzKwPmRtVbNhGfL", "Zq9XkLmP", "LISTview", "HTMLParser"]
    )
    def test_a_random_value_with_no_id_shape_still_flags_a_plain_named_cookie(
        self, tmp_path: Path, value: str
    ) -> None:
        """holds_an_id reads none of these as an id; they are flagged because
        they read neither as words nor as cased words. camelCase that starts with
        a capital reads as cased words only at 12 letters or more, so the
        8-letter LISTview and the 10-letter HTMLParser stay flagged."""
        har = _write_har(tmp_path, [self._with_cookies([("visit", value)])])
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == ("visit",)

    def test_percent_encoded_bytes_are_not_punctuation(self, tmp_path: Path) -> None:
        """Invalid UTF-8 decodes to U+FFFD, which is neither a letter nor a digit;
        only an ASCII value with none counts as holding nothing."""
        value = quote(bytes(range(0x80, 0x90)), safe="")
        har = _write_har(tmp_path, [self._with_cookies([("visit", value)])])
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == ("visit",)

    def test_a_cleared_value_beside_a_random_one_does_not_hide_it(self, tmp_path: Path) -> None:
        entries = [
            self._with_cookies([("visit", '""')]),
            self._with_cookies([("visit", "xQzKwPmRtVbNhGfL")]),
        ]
        har = _write_har(tmp_path, entries)
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == ("visit",)

    @pytest.mark.parametrize(
        "value",
        ['"LANDOWNER"', "%22LANDOWNER%22", '"currentTab"', '"corner lot"', '"BRL Grantees"'],
    )
    def test_a_quoted_form_value_is_read_without_its_quotes(
        self, tmp_path: Path, value: str
    ) -> None:
        """RFC 6265 lets a server send a value in double quotes, and a browser
        keeps them."""
        har = _write_har(tmp_path, [self._with_cookies([("name1", value)])])
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == ()

    @pytest.mark.parametrize(
        "value", ['"xQzKwPmRtVbNhGfL"', "%22xQzKwPmRtVbNhGfL%22", '"LANDOWNER']
    )
    def test_quotes_hide_no_random_value(self, tmp_path: Path, value: str) -> None:
        """Only one balanced pair comes off, and what is inside is judged as any
        other value."""
        har = _write_har(tmp_path, [self._with_cookies([("name1", value)])])
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == ("name1",)

    @pytest.mark.parametrize("high", [False, True], ids=["any-bytes", "high-bytes"])
    @pytest.mark.parametrize("length", [8, 16, 32])
    def test_no_percent_encoded_random_bytes_read_as_cleared(self, length: int, high: bool) -> None:
        """The share of percent-encoded random bytes the form-state rule accepts
        and reads_as_words rejects: 0 of 10,000 per shape (2026-10-24). Bytes that
        are not UTF-8 decode to U+FFFD, which must not read as punctuation."""
        shape = f"{'high ' if high else ''}bytes {length}"
        rng = random.Random(f"296-{shape}")  # noqa: S311 - a fixed sample, not a secret
        low = 0x80 if high else 0
        values = [
            quote(bytes(rng.randrange(low, 0x100) for _ in range(length)), safe="")
            for _ in range(10_000)
        ]
        assert [v for v in values if _form_state(v) and not reads_as_words(v)] == []

    @pytest.mark.parametrize(
        "value",
        [
            '""',
            "%22%22",
            '"BRL Grantees"',
            '"XQZ Grantees"',
            "%22XQZ%20Grantees%22",
            "GRANTEE",
            "LANDOWNER",
            "GRIDviewPanels",
        ],
    )
    def test_the_reported_shapes_are_form_state(self, value: str) -> None:
        """#296's reporter sent case masks, never values: a cleared "", a quoted
        "AAA Aaaaaaaa" (it failed only on its quotes), capitals of 7 and 9 and
        an AAAAaaaaAaaaaa tab name. These are invented values with those masks."""
        assert _form_state(value)

    @pytest.mark.parametrize("value", ["%00", "%00%00", "%01%02", "\x01", '"%00"', "-%00-"])
    def test_a_control_character_is_not_punctuation(self, tmp_path: Path, value: str) -> None:
        """Only ASCII punctuation and spaces hold nothing; a control character is
        a byte of a value, so the cookie stays flagged."""
        assert not _form_state(value)
        har = _write_har(tmp_path, [self._with_cookies([("visit", value)])])
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == ("visit",)

    def test_a_cookie_the_recording_only_sends_is_judged_like_one_it_sets(
        self, tmp_path: Path
    ) -> None:
        """A recording made with a cached session (#274) never sees the session
        set: its requests send it. A sent session name or an opaque sent value is
        flagged, and sent form state is left out, as for a cookie a response sets."""
        entry = self._with_cookies(
            [],
            (
                ("sessionid", "lanterns"),
                ("visit", "Zq9XkLmPwR4t7Bv2Hn8Ks3Jd"),
                ("lot", "12"),
                ("text", "running+shoes"),
            ),
        )
        har = _write_har(tmp_path, [entry])
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == ("sessionid", "visit")

    @pytest.mark.parametrize("array", ["absent", "empty"])
    def test_a_sent_cookie_is_read_from_the_cookie_header_when_the_array_has_none(
        self, tmp_path: Path, array: str
    ) -> None:
        """Every Cookie header counts, in any case, as HTTP/2 can split one into
        several."""
        entry = _entry("GET", "https://myshop.example.com/api/search")
        entry["request"]["headers"] = [
            {"name": "cookie", "value": "sessionid=Zq9XkLmPwR4t7Bv2Hn8Ks3Jd; lot=12"},
            {"name": "Cookie", "value": "visit=Hn8Ks3JdZq9XkLmPwR4t7Bv2"},
        ]
        if array == "absent":
            del entry["request"]["cookies"]
        har = _write_har(tmp_path, [entry])
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == ("sessionid", "visit")

    def test_a_cookie_only_ever_sent_empty_is_left_out(self, tmp_path: Path) -> None:
        """An empty value a request sends is what the browser sent, so the cookie
        held nothing; a response that only ever sets a cookie empty may be a
        recorder that dropped the value, so that cookie stays flagged."""
        entry = self._with_cookies([("visit", "")], (("cart", ""),))
        har = _write_har(tmp_path, [entry])
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == ("visit",)

    def test_sending_empty_does_not_clear_a_cookie_the_cookies_array_sets(
        self, tmp_path: Path
    ) -> None:
        """A response.cookies entry alone (no Set-Cookie header) marks the cookie
        as one a response sets."""
        entry = self._with_cookies([("visit", "")], (("visit", ""),))
        har = _write_har(tmp_path, [entry])
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == ("visit",)

    @pytest.mark.parametrize("side", ["request", "response"])
    def test_a_cookie_with_a_value_field_missing_anywhere_stays_flagged(
        self, tmp_path: Path, side: str
    ) -> None:
        """A missing value is one the recorder did not keep, so a form-state value
        elsewhere does not clear it."""
        entry = self._with_cookies([("lot", "12")], (("lot", "7"),))
        entry[side]["cookies"].append({"name": "lot"})
        har = _write_har(tmp_path, [entry])
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == ("lot",)

    def test_a_sent_cookie_with_no_value_field_stays_flagged(self, tmp_path: Path) -> None:
        """A missing value is a value the recorder did not keep, not an empty one."""
        entry = _entry("GET", "https://myshop.example.com/api/search")
        entry["request"]["cookies"] = [{"name": "cart"}]
        har = _write_har(tmp_path, [entry])
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == ("cart",)

    def test_sending_empty_does_not_clear_a_cookie_a_response_sets(self, tmp_path: Path) -> None:
        """The Set-Cookie header holds a value the response.cookies array lost; a
        later request sending the cookie empty is no evidence about that value."""
        entry = _entry(
            "GET",
            "https://myshop.example.com/api/search",
            set_cookies=["visit=Zq9XkLmPwR4t7Bv2Hn8Ks3Jd; Path=/"],
        )
        entry["response"]["cookies"] = [{"name": "visit", "value": ""}]
        later = self._with_cookies([], (("visit", ""),))
        har = _write_har(tmp_path, [entry, later])
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result, parse_har_file(har).entries) == ("visit",)

    def test_a_token_named_cookie_read_from_a_cookie_header_is_a_token_candidate(
        self, tmp_path: Path
    ) -> None:
        """For a HAR with no request.cookies array, the Cookie header's cookies
        reach the digest's token candidates like the array's would."""
        entry = _entry(
            "GET",
            "https://myshop.example.com/api/search",
            request_headers={"Cookie": "csrftoken=Zq9XkLmPwR4t7Bv2Hn8Ks3Jd"},
        )
        del entry["request"]["cookies"]
        result = digest(DigestSource.from_har(_write_har(tmp_path, [entry])))
        assert [token.name for token in result.tokens] == ["csrftoken"]

    def test_each_flagged_name_carries_the_reasons_it_is_flagged(self, tmp_path: Path) -> None:
        """One recording exercising every reason (#276)."""
        page = _entry(
            "GET",
            "https://myshop.example.com/api/search",
            set_cookies=["guard=lanterns; Path=/; HttpOnly", "kept_name_only=x"],
            request_headers={"X-Csrf-Token": "Zq9XkLmPwR4t7Bv2Hn8Ks3Jd"},
        )
        page["response"]["cookies"] = [
            {"name": "guard", "value": "lanterns"},
            {"name": "visit", "value": "Zq9XkLmPwR4t7Bv2Hn8Ks3Jd"},
            {"name": "lot", "value": "12"},
            {"name": "blank", "value": ""},
        ]
        page["request"]["cookies"] = [
            {"name": "sessionid", "value": "lanterns"},
            {"name": "cached", "value": "Hn8Ks3JdZq9XkLmPwR4t7Bv2"},
            {"name": "text", "value": "running+shoes"},
        ]
        har = _write_har(tmp_path, [page])
        result = digest(DigestSource.from_har(har))
        entries = parse_har_file(har).entries
        reasons = flagged_reasons_of(result, entries)
        assert reasons == {
            "X-Csrf-Token": ("token_candidate",),
            "blank": ("no_value_seen",),
            "cached": ("sent_only", "value_not_form_state"),
            "guard": ("http_only",),
            "kept_name_only": ("no_value_seen", "value_not_kept"),
            "sessionid": ("sent_only", "session_name"),
            "visit": ("value_not_form_state",),
        }
        assert tuple(reasons) == flagged_names_of(result, entries)

    def test_a_cookie_the_digest_saw_set_is_not_sent_only_with_partial_entries(
        self, tmp_path: Path
    ) -> None:
        """gp observe fixtures passes every entry, but a caller passing only some
        must not have a cookie a left-out response set called sent_only."""
        setting = _entry(
            "GET",
            "https://myshop.example.com/api/a",
            set_cookies=["visit=Zq9XkLmPwR4t7Bv2Hn8Ks3Jd; Path=/"],
        )
        sending = self._with_cookies([], (("visit", "Zq9XkLmPwR4t7Bv2Hn8Ks3Jd"),))
        har = _write_har(tmp_path, [setting, sending])
        result = digest(DigestSource.from_har(har))
        later = parse_har_file(har).entries[1:]
        assert flagged_reasons_of(result, later) == {"visit": ("value_not_form_state",)}

    def test_without_entries_every_digest_cookie_is_flagged_for_no_value_seen(
        self, tmp_path: Path
    ) -> None:
        """Called without the run's entries nothing is known of the values, and no
        cookie is called sent_only."""
        har = _write_har(tmp_path, [self._with_cookies([("lot", "12")])])
        result = digest(DigestSource.from_har(har))
        assert flagged_reasons_of(result) == {"lot": ("no_value_seen",)}

    def test_a_sent_id_cookie_name_is_left_out_and_counted(self, tmp_path: Path) -> None:
        entry = self._with_cookies(
            [], (("sess_40912873", "Zq9XkLmPwR4t7Bv2Hn8Ks3Jd"), ("sessionid", "x"))
        )
        har = _write_har(tmp_path, [entry])
        result = digest(DigestSource.from_har(har))
        parsed = parse_har_file(har).entries
        assert flagged_names_of(result, parsed) == ("sessionid",)
        assert redacted_names_of(result, parsed) == 1

    @pytest.mark.parametrize("shape", sorted(_FORM_STATE_LOSS_CEILINGS))
    def test_few_random_values_of_a_biased_shape_read_as_form_state(self, shape: str) -> None:
        """Shapes the #296 reviews measured beyond uniform alphabets: short mixed
        case, the name rule's alphabets, and words with a run of capitals inside.
        Each ceiling is the share of 10,000 values the form-state rule accepts and
        reads_as_words rejects (2026-10-24), plus 0.0005; the full table is
        printed by scripts/cookie_value_rates.py."""
        generate = _rate_shapes(f"296-{shape}")[shape]
        values = [generate() for _ in range(10_000)]
        lost = [v for v in values if _form_state(v) and not reads_as_words(v)]
        assert len(lost) / len(values) <= _FORM_STATE_LOSS_CEILINGS[shape]


class TestEveryNamePositionGoesThroughTheIdRule:
    """Response keys, header names, and cookie names go through
    holds_an_id like path segments and parameter keys, and a dropped key is
    counted, never lost silently."""

    def test_an_id_response_key_becomes_one_placeholder_key(self, tmp_path: Path) -> None:
        body = json.dumps({"40912873": {"total": 1}, "ab12cd34ef56": {"total": 2}, "name": "x"})
        entries = [_entry("GET", "https://api.myshop.example.com/balances", body=body)]
        (endpoint,) = digest(DigestSource.from_har(_write_har(tmp_path, entries))).endpoints
        assert endpoint.shape is not None
        assert set(endpoint.shape.children) == {"{key}", "name"}

    @pytest.mark.parametrize(
        "keys",
        [
            ["-NqF7xYz3abcDEFghiJK", "-NqF7xZ01bcdEFGhijKL", "-NqF7y0Q2cdeFGHijkLM"],
            ["recA1b2C3d4E5f6G7", "recH8i9J0k1L2m3N4", "recO5p6Q7r8S9t0U1"],
        ],
        ids=["push-ids", "record-ids"],
    )
    def test_a_map_keyed_by_ids_the_name_rule_keeps_becomes_one_placeholder_key(
        self, tmp_path: Path, keys: list[str]
    ) -> None:
        body = json.dumps({"items": {key: {"total": 1} for key in keys}})
        entries = [_entry("GET", "https://api.myshop.example.com/balances", body=body)]
        (endpoint,) = digest(DigestSource.from_har(_write_har(tmp_path, entries))).endpoints
        assert endpoint.shape is not None
        items = endpoint.shape.children["items"]
        assert set(items.children) == {"{key}"}
        text = render_json(digest(DigestSource.from_har(_write_har(tmp_path, entries))))
        for key in keys:
            assert key not in text

    def test_a_map_of_two_keys_or_of_mixed_lengths_keeps_its_names(self, tmp_path: Path) -> None:
        body = json.dumps({"sha256Checksum": 1, "md5Checksum2": 2, "x509Certificate": 3, "ab": 4})
        entries = [_entry("GET", "https://api.myshop.example.com/files", body=body)]
        (endpoint,) = digest(DigestSource.from_har(_write_har(tmp_path, entries))).endpoints
        assert endpoint.shape is not None
        assert "{key}" not in endpoint.shape.children

    def test_an_id_header_name_is_dropped_and_counted(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/orders",
                request_headers={"X-Acct-40912873": "1", "X-Shop-Client": "web"},
            )
        ]
        (endpoint,) = digest(DigestSource.from_har(_write_har(tmp_path, entries))).endpoints
        assert endpoint.custom_headers == ("X-Shop-Client",)
        assert endpoint.header_names_dropped_as_ids == 1

    def test_dropped_query_and_body_keys_are_counted(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/orders?u_40912873=1&cus_NffrFeUfNV2Hib=2&page=1",
                post_data=json.dumps({"acct_40912873": 1, "note": "x"}),
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/orders?u_40912873=3",
                post_data=json.dumps({"usr-Zq9XkLmPwR4t7B": 1}),
            ),
        ]
        (endpoint,) = digest(DigestSource.from_har(_write_har(tmp_path, entries))).endpoints
        assert endpoint.query_params == {"page": "int"}
        assert endpoint.body_params == {"note": "str"}
        assert (endpoint.query_keys_dropped_as_ids, endpoint.body_keys_dropped_as_ids) == (2, 2)

    def test_an_id_cookie_name_is_left_out_and_counted(self, tmp_path: Path) -> None:
        """An id-bearing name is written in no form, not even hashed."""
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/orders",
                set_cookies=["sess_40912873=v; Path=/", "shop_session=v; Path=/"],
            )
        ]
        har = _write_har(tmp_path, entries)
        result = digest(DigestSource.from_har(har))
        assert result.cookies == ("shop_session",)
        assert result.cookie_names_dropped_as_ids == 1
        parsed = parse_har_file(har).entries
        assert flagged_names_of(result, parsed) == ("shop_session",)
        assert redacted_names_of(result, parsed) == 1
        text = render_json(result)
        assert "sess_40912873" not in text and "sha256:" not in text

    def test_the_redacted_count_is_never_below_the_digests_own_count(self, tmp_path: Path) -> None:
        """Called without the run's entries, the count still holds the id cookie
        names the digest dropped."""
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/orders",
                set_cookies=["sess_40912873=v; Path=/", "shop_session=v; Path=/"],
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert redacted_names_of(result) == 1

    def test_an_id_token_candidate_name_is_left_out_and_counted(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/orders",
                request_headers={"X-Csrf-Token-40912873": "t", "X-Csrf-Token": "u"},
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert {token.name for token in result.tokens} == {"X-Csrf-Token"}
        assert result.token_names_dropped_as_ids == 1

    def test_keys_that_are_not_field_names_are_counted_apart_from_ids(self, tmp_path: Path) -> None:
        """The two drop causes are counted and worded separately. A name that
        holds an id counts as an id whether or not it is also outside the field-name
        alphabet (an email address is both)."""
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/orders?9lives=1&u_40912873=1&page=1",
                post_data=json.dumps({"3d": 1, "acct_40912873": 1, "note": "x"}),
            )
        ]
        (endpoint,) = digest(DigestSource.from_har(_write_har(tmp_path, entries))).endpoints
        assert (endpoint.query_keys_dropped_as_ids, endpoint.query_keys_dropped_as_non_names) == (
            1,
            1,
        )
        assert (endpoint.body_keys_dropped_as_ids, endpoint.body_keys_dropped_as_non_names) == (
            1,
            1,
        )


class TestDollarNames:
    """OData and ASP.NET WebForms spell field names with a "$"."""

    def test_odata_query_keys_are_field_names(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/orders?$filter=x&$top=5")]
        (endpoint,) = digest(DigestSource.from_har(_write_har(tmp_path, entries))).endpoints
        assert endpoint.query_params == {"$filter": "str", "$top": "int"}

    def test_a_webforms_postback_is_a_form(self, tmp_path: Path) -> None:
        entry = _entry(
            "POST",
            "https://myshop.example.com/search.aspx",
            post_data="__VIEWSTATE=dDwtMTA4&ctl00$Main$txtSearch=widget",
        )
        entry["request"]["postData"]["mimeType"] = _FORM_CONTENT_TYPE
        (endpoint,) = digest(DigestSource.from_har(_write_har(tmp_path, [entry]))).endpoints
        assert endpoint.body_kind == "form"
        assert endpoint.body_params == {"__VIEWSTATE": "str", "ctl00$Main$txtSearch": "str"}

    def test_a_form_with_some_unreadable_keys_is_still_a_form_and_counts_them(
        self, tmp_path: Path
    ) -> None:
        entry = _entry(
            "POST",
            "https://myshop.example.com/search",
            post_data="9lives=1&name=alice",
        )
        entry["request"]["postData"]["mimeType"] = _FORM_CONTENT_TYPE
        (endpoint,) = digest(DigestSource.from_har(_write_har(tmp_path, [entry]))).endpoints
        assert endpoint.body_kind == "form"
        assert endpoint.body_params == {"name": "str"}
        assert endpoint.body_keys_dropped_as_non_names == 1


class TestLoginFieldsInADialogOutsideAnyForm:
    """A script-driven login modal marked as a dialog whose inputs sit in no
    ``<form>`` (#267): in the page's HTML or in the saved page source."""

    _HOST = "https://myshop.example.com"
    _HEADER = '<header><a href="#" class="js-signin" id="signin-link">Sign in</a></header>'
    _MODAL = (
        '<div class="modal" id="login-modal" role="dialog" hidden><div class="login-form">'
        '<input type="text" id="login-username" name="username" autocomplete="username">'
        '<input type="password" id="login-password" name="password" '
        'autocomplete="current-password">'
        '<button type="submit" class="btn" id="login-submit">Sign in</button></div></div>'
    )
    _DIV_MODAL = _MODAL.replace(' role="dialog"', "")
    _SPA_SHELL = '<div id="app"></div><script src="/static/app.js"></script>'
    _POST_BODY = {"username": "alice", "password": "hunter2hunter2"}

    def _entries(self, pages: list[tuple[str, str]], body: dict | None = None) -> list[dict]:
        return [
            *(
                _entry("GET", f"{self._HOST}{path}", content_type="text/html", body=html)
                for path, html in pages
            ),
            _entry(
                "GET",
                f"{self._HOST}/static/app.js",
                content_type="application/javascript",
                body="/*x*/",
            ),
            _entry(
                "POST",
                f"{self._HOST}/api/auth/login/",
                body='{"ok": true}',
                post_data=json.dumps(body or self._POST_BODY),
            ),
            _entry("GET", f"{self._HOST}/api/account/", body='{"name": "x"}'),
        ]

    def _digest(self, tmp_path: Path, pages: list[tuple[str, str]], body: dict | None = None):  # noqa: ANN202
        return digest(DigestSource.from_har(_write_har(tmp_path, self._entries(pages, body))))

    @staticmethod
    def _flags(result) -> dict:  # noqa: ANN001
        return {(e.methods[0], e.template): e.login_flow for e in result.endpoints}

    @staticmethod
    def _plugin(result) -> str:  # noqa: ANN001
        return render(
            ScaffoldSpec(
                name="myshop",
                mode="new_project",
                backend="nodriver",
                base_url="https://myshop.example.com",
                digest=result,
            )
        )["src/graftpunk_myshop/plugin.py"]

    def test_a_dialog_in_the_page_html_gives_one_login_form(self, tmp_path: Path) -> None:
        result = self._digest(tmp_path, [("/", self._HEADER + self._MODAL)])
        (form,) = result.login_forms
        assert form.formless
        assert form.source == f"{self._HOST}/"
        assert form.fields == {"username": "#login-username", "password": "#login-password"}
        assert form.submit == "#login-submit"
        kinds = [(o.method, o.kind) for o in result.login if o.login_flow]
        assert kinds == [("GET", "form_page"), ("POST", "credential_post")]
        # Found by its field names alone, the post is credited to the page whose
        # dialog's input names it carries.
        flags = self._flags(result)
        assert flags[("GET", "/")] is True
        assert flags[("POST", "/api/auth/login/")] is True
        projected = json.loads(render_endpoints_json(result))["login"]["forms"]
        assert projected == [
            {
                "action": "",
                "fields": {"password": "#login-password", "username": "#login-username"},
                "neutral_roles": [],
                "submit": "#login-submit",
                "unresolved_roles": [],
                "opener_candidate": None,
            }
        ]
        code = self._plugin(result)
        assert "login_config = LoginConfig(" in code
        assert 'url="/",' in code
        assert '"username": "#login-username"' in code
        assert 'submit="#login-submit"' in code

    def test_a_dialog_in_the_page_source_gives_one_login_form(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "page-source.html").write_text(self._HEADER + self._MODAL)
        _write_har(run_dir, self._entries([("/", self._HEADER + self._SPA_SHELL)]))
        result = digest(DigestSource.from_run_dir(run_dir, session="myshop", run_id="run"))
        (form,) = result.login_forms
        assert form.formless
        assert [o.kind for o in result.login if o.method == "POST"] == ["credential_post"]
        assert '"password": "#login-password"' in self._plugin(result)

    def test_repro_cases_b_and_e_a_plain_div_modal_gives_no_login_form(
        self, tmp_path: Path
    ) -> None:
        """Inputs in a ``<div>`` that is not a dialog stay unfound until the DOM
        snapshot records the live modal."""
        result = self._digest(tmp_path, [("/", self._HEADER + self._DIV_MODAL)])
        assert result.login_forms == ()
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "page-source.html").write_text(self._HEADER + self._DIV_MODAL)
        _write_har(run_dir, self._entries([("/", self._HEADER + self._SPA_SHELL)]))
        from_page_source = digest(
            DigestSource.from_run_dir(run_dir, session="myshop", run_id="run")
        )
        assert from_page_source.login_forms == ()
        assert "# No login form detected." in self._plugin(from_page_source)

    def test_a_post_whose_names_the_dialog_lacks_is_not_credited_to_it(
        self, tmp_path: Path
    ) -> None:
        result = self._digest(
            tmp_path, [("/", self._MODAL)], {"member": "alice", "pin_password": "1234"}
        )
        assert len(result.login_forms) == 1
        assert self._flags(result)[("GET", "/")] is False
        assert [o.kind for o in result.login if o.login_flow] == ["credential_post"]

    def test_a_dialog_with_nameless_inputs_is_never_credited(self, tmp_path: Path) -> None:
        nameless = self._MODAL.replace(' name="username"', "").replace(' name="password"', "")
        result = self._digest(tmp_path, [("/", nameless)])
        (form,) = result.login_forms
        assert form.input_names == ()
        assert self._flags(result)[("GET", "/")] is False

    def test_the_post_goes_to_the_dialog_sharing_most_of_its_names(self, tmp_path: Path) -> None:
        """Review round 2 (sitewide V): the checkout's own login dialog, not the
        site-wide modal, is the one whose names the post carries."""
        inline = (
            '<section role="dialog" class="checkout-login"><input type="text" name="member" '
            'id="member"><input type="password" name="pin" id="pin">'
            '<button id="pin-go">Continue</button></section>'
        )
        result = self._digest(
            tmp_path,
            [("/", self._MODAL), ("/checkout", self._MODAL + inline)],
            {"member": "alice", "pin": "1234", "password": "x"},
        )
        selected = next(f for f in result.login_forms if "password" in f.fields)
        assert selected.fields == {"username": "#member", "password": "#pin"}
        assert self._flags(result)[("GET", "/checkout")] is True
        assert self._flags(result)[("GET", "/")] is False
        assert 'url="/checkout",' in self._plugin(result)

    def test_the_same_dialog_on_pages_that_differ_elsewhere_is_listed_once(
        self, tmp_path: Path
    ) -> None:
        search = '<header><input type="search" name="q" id="q"></header>'
        color_filter = '<aside><input type="checkbox" name="color" id="c-red"></aside>'
        result = self._digest(
            tmp_path,
            [
                ("/", self._MODAL),
                ("/products", search + self._MODAL),
                ("/cart", color_filter + self._MODAL),
            ],
        )
        (form,) = result.login_forms
        assert form.formless

    def test_a_site_wide_dialog_marks_no_page_as_the_login_page(self, tmp_path: Path) -> None:
        """The modal is on / and /products/77 and the user signed in from
        /products/77: the post is credited to /, the page with the fewest path
        segments, and neither page's GET is part of the login flow."""
        page = self._HEADER + "<main>product</main>" + self._MODAL
        result = self._digest(tmp_path, [("/", page), ("/products/77", page)])
        flags = self._flags(result)
        assert flags[("GET", "/")] is False
        assert flags[("GET", "/products/77")] is False
        assert flags[("POST", "/api/auth/login/")] is True
        assert [(o.method, o.kind) for o in result.login if o.login_flow] == [
            ("POST", "credential_post")
        ]
        (form,) = result.login_forms
        assert form.source == f"{self._HOST}/"
        code = self._plugin(result)
        assert 'url="/",' in code
        assert "def products_77(" in code

    def test_a_site_wide_dialog_is_credited_to_the_earliest_shallowest_page(
        self, tmp_path: Path
    ) -> None:
        page = "<main>product</main>" + self._MODAL
        result = self._digest(
            tmp_path,
            [("/shop/products/77", page), ("/products/42", page), ("/account/orders", page)],
        )
        (form,) = result.login_forms
        assert form.source == f"{self._HOST}/products/42"
        assert not any(
            flag for (method, _t), flag in self._flags(result).items() if method == "GET"
        )

    def test_login_config_url_is_the_page_of_the_copy_that_resolves_best(
        self, tmp_path: Path
    ) -> None:
        """Review round 2 (sitewide U, d1 S1): the post is credited to /, but on /
        the dialog's email name is shared with a newsletter input, so the copy kept
        is /about's and the login opens there."""
        named = (
            '<div class="modal" role="dialog"><input type="email" name="email">'
            '<input type="password" name="password">'
            '<button type="submit" name="signin">Sign in</button></div>'
        )
        news = '<footer><input type="email" name="email"><button name="sub">Join</button></footer>'
        result = self._digest(
            tmp_path,
            [("/", named + news), ("/about", named)],
            {"email": "alice@example.com", "password": "hunter2hunter2"},
        )
        (form,) = result.login_forms
        assert form.source == f"{self._HOST}/about"
        assert 'url="/about",' in self._plugin(result)

    def test_a_real_script_driven_form_on_the_page_keeps_the_credit(self, tmp_path: Path) -> None:
        """Review round 2 (M10, M16): the page the user signed in from holds an
        actionless real login form beside the site-wide dialog; it stays the login
        page, as it was before formless groups existed."""
        real = (
            '<form><input type="email" name="username" id="email">'
            '<input type="password" name="password" id="pass"><button>Sign in</button></form>'
        )
        result = self._digest(tmp_path, [("/about", self._MODAL), ("/login", real + self._MODAL)])
        selected = next(f for f in result.login_forms if "password" in f.fields)
        assert not selected.formless
        assert self._flags(result)[("GET", "/login")] is True
        assert self._flags(result)[("GET", "/about")] is False

    def test_a_page_credited_by_target_stays_in_the_flow_beside_a_site_wide_credit(
        self, tmp_path: Path
    ) -> None:
        """Review round 2 (M17): / holds the site-wide dialog and a form posting to
        /session; one post goes there, a later script post is credited to /
        through the dialog. / is still the login page."""
        real = (
            '<form action="/session" method="post"><input name="username" id="email">'
            '<input type="password" name="password" id="pass"><button>Go</button></form>'
        )
        entries = [
            _entry("GET", f"{self._HOST}/", content_type="text/html", body=real + self._MODAL),
            _entry("GET", f"{self._HOST}/about", content_type="text/html", body=self._MODAL),
            _entry(
                "POST",
                f"{self._HOST}/session",
                status=302,
                response_headers={"Location": "/"},
                post_data=json.dumps(self._POST_BODY),
            ),
            _entry(
                "POST",
                f"{self._HOST}/api/auth/login/",
                post_data=json.dumps(self._POST_BODY),
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert self._flags(result)[("GET", "/")] is True
        assert self._flags(result)[("GET", "/about")] is False

    def test_a_post_to_the_page_itself_is_not_matched_by_target(self, tmp_path: Path) -> None:
        """A formless group has no target: a POST to its page without a password
        field is not a credential post, unlike an actionless form's."""
        entries = [
            _entry("GET", f"{self._HOST}/products", content_type="text/html", body=self._MODAL),
            _entry("POST", f"{self._HOST}/products", post_data=json.dumps({"q": "tea"})),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert not any(o.kind == "credential_post" for o in result.login)

    def test_markdown_names_the_group_as_inputs_outside_a_form(self, tmp_path: Path) -> None:
        result = self._digest(tmp_path, [("/", self._MODAL)])
        markdown = render_markdown(result)
        assert f"- inputs outside a form (sent by script), source: {self._HOST}/" in markdown
        assert "form at ``" not in markdown


class _DialogDigestCase:
    """Shared recordings and readers for the dialog-login digest tests."""

    _HOST = "https://myshop.example.com"
    _MODAL = (
        '<div role="dialog" id="login-modal"><input type="email" id="m-email" name="email">'
        '<input type="password" id="m-pw" name="password">'
        '<button type="submit" id="m-go">Sign in</button></div>'
    )
    _REAL = (
        '<form action="/login" method="post"><input type="text" name="username">'
        '<input type="password" name="password"><button>Sign in</button></form>'
    )
    _BODY = {"email": "alice@example.com", "password": "hunter2hunter2"}

    def _get(self, path: str, html: str, **kwargs: Any) -> dict:
        return _entry("GET", f"{self._HOST}{path}", content_type="text/html", body=html, **kwargs)

    def _post(self, path: str, payload: dict, **kwargs: Any) -> dict:
        kwargs.setdefault("body", '{"ok": true}')
        return _entry("POST", f"{self._HOST}{path}", post_data=json.dumps(payload), **kwargs)

    @staticmethod
    def _run(tmp_path: Path, entries: list[dict]):  # noqa: ANN205
        return digest(DigestSource.from_har(_write_har(tmp_path, entries)))

    @staticmethod
    def _flags(result) -> dict:  # noqa: ANN001
        return {(e.methods[0], e.template): e.login_flow for e in result.endpoints}

    @staticmethod
    def _plugin(result) -> str:  # noqa: ANN001
        return render(
            ScaffoldSpec(
                name="myshop",
                mode="new_project",
                backend="nodriver",
                base_url="https://myshop.example.com",
                digest=result,
            )
        )["src/graftpunk_myshop/plugin.py"]


class TestDialogLoginsLeaveRealFormLoginsAsTheyWere(_DialogDigestCase):
    """Review round 3 of #267: a real form a post went to, or one sharing a post's
    names, keeps the login beside a dialog, and only
    GET pages count for it. Each "as on main" result was read from a digest at
    ae142f3 of the same recording."""

    def test_d1_a_real_form_a_later_post_went_to_keeps_the_login(self, tmp_path: Path) -> None:
        result = self._run(
            tmp_path,
            [
                self._get("/", "<main>home</main>" + self._MODAL),
                self._post("/api/auth", self._BODY),
                self._get("/login", self._REAL),
                self._post("/login", {"username": "alice", "password": "hunter2hunter2"}),
                self._get("/account", "<main>acct</main>"),
            ],
        )
        flags = self._flags(result)
        # As on main.
        assert flags[("GET", "/login")] is True
        assert flags[("POST", "/login")] is True
        assert flags[("POST", "/api/auth")] is False
        code = self._plugin(result)
        assert 'url="/login",' in code
        assert "def login_2(" not in code

    def test_d2_with_nothing_credited_a_real_form_is_the_login(self, tmp_path: Path) -> None:
        result = self._run(
            tmp_path,
            [
                self._get("/", "<main>home</main>" + self._MODAL),
                self._get("/signin", self._REAL),
                self._post("/api/session", {"member": "alice", "secret_password": "x"}),
            ],
        )
        selected = next(f for f in result.login_forms if "password" in f.fields)
        assert not selected.formless
        # As on main.
        assert 'url="/signin",' in self._plugin(result)
        assert self._flags(result)[("POST", "/api/session")] is True

    def test_d8_a_dialog_login_owns_every_credential_post(self, tmp_path: Path) -> None:
        """With no real form, every credential post stays part of the login, as on
        main: a dialog's post is found by its names alone, so a later password post
        cannot be told apart from it."""
        result = self._run(
            tmp_path,
            [
                self._get("/", "<main>home</main>" + self._MODAL),
                self._post("/api/auth", self._BODY),
                self._get("/settings", "<main>s</main>"),
                self._post("/api/reauth", {"password": "hunter2hunter2"}),
            ],
        )
        flags = self._flags(result)
        assert flags[("POST", "/api/auth")] is True
        assert flags[("POST", "/api/reauth")] is True
        code = self._plugin(result)
        assert "def api_reauth(" not in code
        assert "login_config = LoginConfig(" in code

    def test_a_landing_page_showing_the_dialog_keeps_its_redirect_hop(self, tmp_path: Path) -> None:
        """Review round 3 (M1): the login redirects to /account, whose layout still
        holds the modal; its set_cookie hop is listed, as on main."""
        result = self._run(
            tmp_path,
            [
                self._get("/", self._MODAL),
                _entry(
                    "POST",
                    f"{self._HOST}/login",
                    status=302,
                    response_headers={"Location": "/account"},
                    post_data=json.dumps(self._BODY),
                    set_cookies=["sid=abc; Path=/"],
                ),
                self._get(
                    "/account", "<main>hi</main>" + self._MODAL, set_cookies=["seen=1; Path=/"]
                ),
            ],
        )
        login = [(o.method, urlparse(o.url).path, o.kind, o.login_flow) for o in result.login]
        assert ("GET", "/account", "set_cookie", True) in login

    def test_a_dialog_in_a_post_response_is_no_page_of_the_login(self, tmp_path: Path) -> None:
        """Review round 3 (I2, E): a failed attempt re-renders the modal in a POST's
        HTML response; the dialog is still on one GET page, so that page is the
        login page."""
        result = self._run(
            tmp_path,
            [
                self._get("/", self._MODAL),
                self._post(
                    "/login",
                    self._BODY,
                    content_type="text/html",
                    body="<main>Wrong password</main>" + self._MODAL,
                ),
                _entry(
                    "POST",
                    f"{self._HOST}/login",
                    status=302,
                    response_headers={"Location": "/account"},
                    post_data=json.dumps(self._BODY),
                ),
            ],
        )
        assert len(result.login_forms) == 1
        assert self._flags(result)[("GET", "/")] is True
        assert 'url="/",' in self._plugin(result)

    def test_a_dialog_only_in_a_post_response_gives_no_login_form(self, tmp_path: Path) -> None:
        """Review round 3 (I2, E3): never a url the run reached only by POST."""
        result = self._run(
            tmp_path,
            [
                self._post("/s", {"q": "tea"}, content_type="text/html", body=self._MODAL),
                self._post("/api/auth", self._BODY),
            ],
        )
        assert result.login_forms == ()
        assert 'url="/s"' not in self._plugin(result)

    def test_a_dialog_recorded_after_the_post_is_not_credited(self, tmp_path: Path) -> None:
        """Review round 3 (D5)."""
        result = self._run(
            tmp_path,
            [
                self._post("/api/auth", self._BODY),
                self._get("/", "<main>home</main>" + self._MODAL),
            ],
        )
        assert self._flags(result)[("GET", "/")] is False


class TestDialogCreditAndPagesRound4(_DialogDigestCase):
    """Review round 4 of #267."""

    def _kinds(self, result) -> list[tuple[str, str, str, bool]]:  # noqa: ANN001
        return [(o.method, urlparse(o.url).path, o.kind, o.login_flow) for o in result.login]

    def test_d9_a_real_form_sharing_the_post_s_names_keeps_the_login(self, tmp_path: Path) -> None:
        """A dialog is credited only when no real login form shares a name with the
        post; here the /signin form does, so the result is main's."""
        real = (
            '<form action="/login" method="post"><input type="email" name="email">'
            '<input type="password" name="password"><button>Sign in</button></form>'
        )
        result = self._run(
            tmp_path,
            [
                self._get("/", "<main>home</main>" + self._MODAL),
                self._get("/signin", real),
                self._post("/api/session", self._BODY),
            ],
        )
        selected = next(f for f in result.login_forms if "password" in f.fields)
        assert not selected.formless
        assert self._flags(result)[("GET", "/")] is False
        code = self._plugin(result)
        assert 'url="/signin",' in code
        assert "def root(" in code

    def test_a_real_form_in_the_saved_page_source_keeps_the_post_too(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "page-source.html").write_text(
            '<form action="#"><input type="email" name="email">'
            '<input type="password" name="password"><button>Sign in</button></form>'
        )
        _write_har(
            run_dir,
            [
                self._get("/", "<main>home</main>" + self._MODAL),
                self._post("/api/auth", self._BODY),
            ],
        )
        result = digest(DigestSource.from_run_dir(run_dir, session="myshop", run_id="run"))
        assert self._flags(result)[("GET", "/")] is False

    def test_reloading_the_dialog_s_page_keeps_it_one_page(self, tmp_path: Path) -> None:
        result = self._run(
            tmp_path,
            [
                self._get("/", self._MODAL),
                self._get("/", self._MODAL),
                self._post("/api/auth", self._BODY),
            ],
        )
        assert self._flags(result)[("GET", "/")] is True

    def test_landing_back_on_the_dialog_s_page_keeps_it_one_page(self, tmp_path: Path) -> None:
        result = self._run(
            tmp_path,
            [
                self._get("/", self._MODAL),
                _entry(
                    "POST",
                    f"{self._HOST}/api/login",
                    status=302,
                    response_headers={"Location": "/"},
                    post_data=json.dumps(self._BODY),
                ),
                self._get("/", self._MODAL),
            ],
        )
        assert self._flags(result)[("GET", "/")] is True

    def _login_then(self, after: list[dict]) -> list[dict]:
        """A page with no form, a field-name login redirecting to /account, then
        *after*."""
        return [
            self._get("/start", "<main>start</main>"),
            _entry(
                "POST",
                f"{self._HOST}/login",
                status=302,
                response_headers={"Location": "/account"},
                post_data=json.dumps(self._BODY),
            ),
            *after,
        ]

    def test_a_landing_page_holding_a_real_login_form_stays_a_form_page(
        self, tmp_path: Path
    ) -> None:
        """Only a landing page holding nothing but dialogs is the chain's hop: one with
        a real login form stays a form page, as on main, so its cookie is no hop."""
        result = self._run(
            tmp_path,
            self._login_then(
                [self._get("/account", self._REAL + self._MODAL, set_cookies=["seen=1; Path=/"])]
            ),
        )
        assert ("GET", "/account", "set_cookie") not in [k[:3] for k in self._kinds(result)]

    def test_a_dialog_page_past_the_login_window_is_a_form_page(self, tmp_path: Path) -> None:
        """Past the window, /account is no hop of the first login, so it is a form
        page a later post is credited to."""
        filler = [
            _entry("GET", f"{self._HOST}/api/item/{n}", body='{"n": 1}')
            for n in range(_LOGIN_WINDOW + 1)
        ]
        result = self._run(
            tmp_path,
            self._login_then(
                [
                    *filler,
                    self._get("/account", "<main>a</main>" + self._MODAL),
                    self._post("/api/auth", self._BODY),
                ]
            ),
        )
        assert self._flags(result)[("GET", "/account")] is True

    def test_a_dialog_page_the_chain_did_not_lead_to_is_a_form_page(self, tmp_path: Path) -> None:
        result = self._run(
            tmp_path,
            self._login_then(
                [
                    self._get("/p/1", "<main>p</main>" + self._MODAL),
                    self._post("/api/auth", self._BODY),
                ]
            ),
        )
        assert self._flags(result)[("GET", "/p/1")] is True


class TestDialogCreditRound5(_DialogDigestCase):
    """Review round 5 of #267: the credit gate looks at every credential post and
    at visible names only, and pages are counted by host and path."""

    @pytest.mark.parametrize("sharing_first", [True, False], ids=["sharing-first", "sharing-last"])
    def test_p1_a_real_form_sharing_names_with_any_post_blocks_every_credit(
        self, tmp_path: Path, sharing_first: bool
    ) -> None:
        """One post shares names with the /signin form, so the other, which shares
        names only with the dialog, is credited to no dialog, in either order:
        main's login_config."""
        modal = self._MODAL.replace('name="email"', 'name="login_email"').replace(
            'name="password"', 'name="passwd"'
        )
        posts = [
            self._post("/api/v1/session", {"username": "alice", "password": "x"}),
            self._post("/api/auth", {"login_email": "alice@example.com", "passwd": "x"}),
        ]
        result = self._run(
            tmp_path,
            [
                self._get("/", "<main>home</main>" + modal),
                self._get("/signin", self._REAL),
                *(posts if sharing_first else posts[::-1]),
            ],
        )
        selected = next(f for f in result.login_forms if "password" in f.fields)
        assert not selected.formless
        code = self._plugin(result)
        assert 'url="/signin",' in code
        assert "def root(" in code
        assert self._flags(result)[("GET", "/")] is False

    def test_a_post_matched_by_target_counts_in_the_credit_gate(self, tmp_path: Path) -> None:
        """Review round 6 (toolkit M2): the post to the real form's own target shares
        its names, so the later dialog post is credited to no dialog and / is no
        form page of the login, as on main."""
        modal = self._MODAL.replace('name="email"', 'name="login_email"').replace(
            'name="password"', 'name="passwd"'
        )
        result = self._run(
            tmp_path,
            [
                self._get("/", "<main>home</main>" + modal),
                self._get("/signin", self._REAL),
                self._post("/login", {"username": "alice", "password": "x"}),
                self._post("/api/auth", {"login_email": "alice@example.com", "passwd": "x"}),
            ],
        )
        assert not any(o.kind == "form_page" and urlparse(o.url).path == "/" for o in result.login)
        assert 'url="/signin",' in self._plugin(result)

    def test_p2_a_shared_hidden_name_does_not_block_the_credit(self, tmp_path: Path) -> None:
        """A site-wide CSRF field is no shared name."""
        modal = self._MODAL.replace("</div>", '<input type="hidden" name="csrf" value="t"></div>')
        delete = (
            '<form action="/account/delete" method="post"><input type="hidden" name="csrf" '
            'value="t"><input type="password" name="confirm_pw"><button>Delete</button></form>'
        )
        result = self._run(
            tmp_path,
            [
                self._get("/", "<main>home</main>" + modal),
                self._post("/api/auth", {**self._BODY, "csrf": "t"}),
                self._get("/account", delete),
            ],
        )
        selected = next(f for f in result.login_forms if "password" in f.fields)
        assert selected.formless
        assert 'url="/",' in self._plugin(result)
        assert self._flags(result)[("GET", "/")] is True

    def test_a_dialog_takes_over_from_a_real_form_sharing_no_name(self, tmp_path: Path) -> None:
        """Review round 5 (superpowers M7): the one documented case where a dialog is
        the login beside a real login form, which no post went to and which shares
        no name with any post."""
        real = (
            '<form action="/login" method="post"><input name="user">'
            '<input type="password" name="pass"><button>Sign in</button></form>'
        )
        result = self._run(
            tmp_path,
            [
                self._get("/", "<main>home</main>" + self._MODAL),
                self._get("/signin", real),
                self._post("/api/auth", self._BODY),
            ],
        )
        selected = next(f for f in result.login_forms if "password" in f.fields)
        assert selected.formless
        assert 'url="/",' in self._plugin(result)
        assert self._flags(result)[("GET", "/")] is True

    def test_pages_are_counted_without_the_query(self, tmp_path: Path) -> None:
        """Review round 5 (superpowers M9, P3)."""
        result = self._run(
            tmp_path,
            [
                self._get("/?a=1", self._MODAL),
                self._get("/?a=2", self._MODAL),
                self._post("/api/auth", self._BODY),
            ],
        )
        assert self._flags(result)[("GET", "/")] is True
        assert "def root(" not in self._plugin(result)

    def test_pages_on_two_hosts_are_two_pages(self, tmp_path: Path) -> None:
        """Counted by host and path (review round 5, P3b)."""
        result = self._run(
            tmp_path,
            [
                self._get("/", self._MODAL),
                _entry(
                    "GET",
                    "https://www.myshop.example.com/",
                    content_type="text/html",
                    body=self._MODAL,
                ),
                self._post("/api/auth", self._BODY),
            ],
        )
        selected = next(f for f in result.login_forms if "password" in f.fields)
        assert selected.formless
        # The page credited is listed as a form page outside the login flow, and the
        # GET / endpoint (both hosts' loads) is no part of the login.
        pages = [o for o in result.login if o.kind == "form_page"]
        assert [urlparse(o.url).path for o in pages] == ["/"]
        assert not any(o.login_flow for o in pages)
        assert self._flags(result)[("GET", "/")] is False
        assert "def root(" in self._plugin(result)


_SNAPSHOT_GP_FILL = (
    "GP-FILL: read from the page at the moment of the credential post; if a click opens "
    "this form, add a click-only LoginStep before this one."
)


def _comment_text(code: str) -> str:
    return " ".join(
        line.strip().lstrip("#").strip()
        for line in code.splitlines()
        if line.strip().startswith("#")
    )


class TestALoginReadFromTheDomSnapshot(_DialogDigestCase):
    """A credential post's ``request._domSnapshot`` (#267, part 2): the live page at
    the moment the post was sent, read as a GET page just before the post."""

    _SHELL = '<header><a href="#" id="signin">Sign in</a></header><div id="app"></div>'
    # The modal the page's script rendered after the click, as the recorder stores it.
    _DIV_MODAL = (
        '<html><head><script></script></head><body><header><a href="#" id="signin">Sign in'
        '</a></header><div id="app"><div class="modal" id="login-modal">'
        '<input type="email" id="m-email" name="email">'
        '<input type="password" id="m-pw" name="password">'
        '<button type="submit" id="m-go">Sign in</button></div></div></body></html>'
    )

    def _snapshot_post(
        self,
        run: Path,
        path: str,
        html: str,
        name: str = "9.1",
        *,
        page: str | None = None,
        body: dict | None = None,
    ) -> dict:
        (run / "snapshots").mkdir(exist_ok=True)
        (run / "snapshots" / f"{name}.html").write_text(html)
        post = self._post(path, body or self._BODY)
        post["request"]["_domSnapshot"] = f"snapshots/{name}.html"
        post["request"]["_domSnapshotUrl"] = page or f"{self._HOST}/"
        return post

    def test_a_script_rendered_modal_gives_a_login_form_with_its_page(self, tmp_path: Path) -> None:
        entries = [
            self._get("/", self._SHELL),
            self._snapshot_post(tmp_path, "/api/auth/login/", self._DIV_MODAL),
            _entry("GET", f"{self._HOST}/api/account/", body='{"name": "x"}'),
        ]
        result = self._run(tmp_path, entries)

        (form,) = result.login_forms
        assert form.from_snapshot
        assert form.source == f"{self._HOST}/"
        assert form.fields == {"username": "#m-email", "password": "#m-pw"}
        assert form.submit == "#m-go"
        flow = [(o.method, urlparse(o.url).path, o.kind) for o in result.login if o.login_flow]
        assert flow == [("GET", "/", "form_page"), ("POST", "/api/auth/login/", "credential_post")]
        code = self._plugin(result)
        assert 'url="/",' in code
        assert '"password": "#m-pw"' in code
        assert _SNAPSHOT_GP_FILL in _comment_text(code)

    def test_without_the_snapshot_the_same_run_has_no_login_form(self, tmp_path: Path) -> None:
        post = self._snapshot_post(tmp_path, "/api/auth/login/", self._DIV_MODAL)
        del post["request"]["_domSnapshot"]
        result = self._run(tmp_path, [self._get("/", self._SHELL), post])
        assert result.login_forms == ()

    def test_an_actionless_form_in_the_snapshot_is_credited_too(self, tmp_path: Path) -> None:
        html = self._DIV_MODAL.replace('<div class="modal"', '<form class="modal"').replace(
            "</button></div>", "</button></form>"
        )
        entries = [
            self._get("/", self._SHELL),
            self._snapshot_post(tmp_path, "/api/auth/login/", html),
        ]
        result = self._run(tmp_path, entries)

        (form,) = result.login_forms
        assert not form.formless
        assert form.from_snapshot
        assert [o.login_flow for o in result.login if o.kind == "credential_post"] == [True]
        # The post promoted the snapshot's page through its script-driven form.
        pages = [urlparse(o.url).path for o in result.login if o.kind == "form_page"]
        assert pages == ["/"]
        assert 'url="/",' in self._plugin(result)

    def test_a_snapshot_does_not_displace_a_real_form_recorded_elsewhere(
        self, tmp_path: Path
    ) -> None:
        """The real /login form the post went to stays the login; the snapshot's
        site-wide modal is listed after it."""
        body = {"username": "alice", "password": "hunter2hunter2"}
        entries = [
            self._get("/login", self._REAL),
            self._snapshot_post(tmp_path, "/login", self._DIV_MODAL),
        ]
        entries[1]["request"]["postData"]["text"] = json.dumps(body)
        result = self._run(tmp_path, entries)

        first = result.login_forms[0]
        assert not first.formless
        assert not first.from_snapshot
        assert first.source == f"{self._HOST}/login"
        assert [f.from_snapshot for f in result.login_forms] == [False, True]
        code = self._plugin(result)
        assert 'url="/login",' in code
        assert _SNAPSHOT_GP_FILL not in _comment_text(code)

    def test_the_recorded_copy_of_a_form_wins_over_its_snapshot_copy(self, tmp_path: Path) -> None:
        body = {"username": "alice", "password": "hunter2hunter2"}
        entries = [
            self._get("/login", self._REAL),
            self._snapshot_post(tmp_path, "/login", f"<html><body>{self._REAL}</body></html>"),
        ]
        entries[1]["request"]["postData"]["text"] = json.dumps(body)
        entries[1]["request"]["_domSnapshotUrl"] = f"{self._HOST}/login"
        result = self._run(tmp_path, entries)

        (form,) = result.login_forms
        assert not form.from_snapshot
        assert _SNAPSHOT_GP_FILL not in _comment_text(self._plugin(result))
        # The snapshot added no page: the recorded GET is the one form page.
        assert [o.kind for o in result.login].count("form_page") == 1

    _CREDS = {"username": "alice", "password": "hunter2hunter2"}

    def test_a_form_the_page_s_script_added_a_hidden_input_to_is_listed_once(
        self, tmp_path: Path
    ) -> None:
        """Review M6: the same inputs and roles on the same page is the same form,
        whatever hidden inputs or attributes the page's script added."""
        live = self._REAL.replace(
            '<input type="password" name="password">',
            '<input type="password" name="password" autocomplete="current-password">'
            '<input type="hidden" name="device_fp">',
        )
        entries = [
            self._get("/login", self._REAL),
            self._snapshot_post(
                tmp_path, "/login", live, page=f"{self._HOST}/login", body=self._CREDS
            ),
        ]
        result = self._run(tmp_path, entries)
        (form,) = result.login_forms
        assert not form.from_snapshot
        assert [o.kind for o in result.login].count("form_page") == 1
        assert _SNAPSHOT_GP_FILL not in _comment_text(self._plugin(result))

    def test_a_hidden_input_named_with_an_id_is_still_hidden(self, tmp_path: Path) -> None:
        """Review round 2 (M5): a hidden input whose name holds an id is left out of
        LoginForm.hidden, and is hidden all the same for telling copies apart."""
        live = self._REAL.replace(
            '<input type="password" name="password">',
            '<input type="password" name="password">'
            '<input type="hidden" name="fp_3f9a8c2e1b7d4a60">',
        )
        entries = [
            self._get("/login", self._REAL),
            self._snapshot_post(
                tmp_path, "/login", live, page=f"{self._HOST}/login", body=self._CREDS
            ),
        ]
        (form,) = self._run(tmp_path, entries).login_forms
        assert not form.from_snapshot

    def test_the_same_names_with_other_roles_are_another_form(self, tmp_path: Path) -> None:
        """A typeless input the form's script turned into a checkbox is no longer a
        field: the roles differ, so both copies are listed."""
        recorded = self._REAL.replace(
            "<button>Sign in</button>", '<input name="remember"><button>Sign in</button>'
        )
        live = recorded.replace(
            '<input name="remember">', '<input type="checkbox" name="remember">'
        )
        entries = [
            self._get("/login", recorded),
            self._snapshot_post(
                tmp_path, "/login", live, page=f"{self._HOST}/login", body=self._CREDS
            ),
        ]
        result = self._run(tmp_path, entries)
        assert sorted(f.from_snapshot for f in result.login_forms) == [False, True]

    def test_the_same_names_posting_elsewhere_are_another_form(self, tmp_path: Path) -> None:
        live = self._REAL.replace('action="/login"', 'action="/club/join"')
        entries = [
            self._get("/login", self._REAL),
            self._snapshot_post(
                tmp_path, "/login", live, page=f"{self._HOST}/login", body=self._CREDS
            ),
        ]
        result = self._run(tmp_path, entries)
        assert sorted(f.from_snapshot for f in result.login_forms) == [False, True]

    def test_a_form_with_other_inputs_on_the_same_page_is_read(self, tmp_path: Path) -> None:
        live = self._REAL.replace('name="username"', 'name="member_email"')
        entries = [
            self._get("/login", self._REAL),
            self._snapshot_post(
                tmp_path,
                "/login",
                live,
                page=f"{self._HOST}/login",
                body={"member_email": "alice", "password": "hunter2hunter2"},
            ),
        ]
        result = self._run(tmp_path, entries)
        assert [f.from_snapshot for f in result.login_forms].count(True) == 1

    def test_the_same_form_on_another_page_keeps_the_recorded_copy(self, tmp_path: Path) -> None:
        """The tie-break of _unique_forms: a snapshot of / holding /login's form."""
        entries = [
            self._get("/login", self._REAL),
            self._snapshot_post(tmp_path, "/login", self._REAL, body=self._CREDS),
        ]
        result = self._run(tmp_path, entries)
        (form,) = result.login_forms
        assert not form.from_snapshot
        assert form.source == f"{self._HOST}/login"

    def test_a_snapshot_page_reached_by_a_cross_host_redirect_opens_from_the_app(
        self, tmp_path: Path
    ) -> None:
        idp = "https://id.myshop.example.com"
        app_login = f"{self._HOST}/login"
        redirect = _entry(
            "GET",
            app_login,
            status=302,
            content_type="text/html",
            body="",
            response_headers={"Location": f"{idp}/authorize"},
        )
        redirect["response"]["redirectURL"] = f"{idp}/authorize"
        shell = _entry("GET", f"{idp}/authorize", content_type="text/html", body=self._SHELL)
        post = self._snapshot_post(tmp_path, "/x", self._DIV_MODAL, page=f"{idp}/authorize")
        post["request"]["url"] = f"{idp}/authorize/login"
        result = self._run(tmp_path, [redirect, shell, post])
        (form,) = result.login_forms
        assert form.from_snapshot
        assert form.opened_from == app_login

    def test_the_snapshot_page_is_its_own_step(self, tmp_path: Path) -> None:
        """The snapshot's page is not the page recorded just before it."""
        other = (
            '<form action="/club/join" method="post"><input type="text" name="member">'
            '<input type="password" name="secret"><button>Join</button></form>'
        )
        entries = [
            self._get("/club", other),
            self._snapshot_post(tmp_path, "/api/auth/login/", self._DIV_MODAL),
        ]
        result = self._run(tmp_path, entries)
        flagged = {(o.method, urlparse(o.url).path): o.login_flow for o in result.login}
        assert flagged.get(("GET", "/club")) in (None, False)
        assert flagged[("GET", "/")] is True

    def test_a_snapshot_with_no_http_url_is_not_read(self, tmp_path: Path) -> None:
        post = self._snapshot_post(
            tmp_path, "/api/auth/login/", self._DIV_MODAL, page="about:blank"
        )
        result = self._run(tmp_path, [self._get("/", self._SHELL), post])
        assert result.login_forms == ()
        assert [o.kind for o in result.login] == ["credential_post"]

    def test_only_a_recorded_get_makes_a_snapshot_form_a_copy(self, tmp_path: Path) -> None:
        """A POST's HTML response at the same path is not the page the user saw."""
        search = _entry(
            "POST",
            f"{self._HOST}/login",
            content_type="text/html",
            body=self._REAL,
            post_data=json.dumps({"q": "shoes"}),
        )
        entries = [
            search,
            self._snapshot_post(
                tmp_path,
                "/login",
                f"<html><body>{self._REAL}</body></html>",
                page=f"{self._HOST}/login",
                body=self._CREDS,
            ),
        ]
        result = self._run(tmp_path, entries)
        assert [o.kind for o in result.login if o.kind == "form_page"] == ["form_page"]

    def test_a_snapshot_form_s_action_marks_a_later_post_to_it(self, tmp_path: Path) -> None:
        """Read as a GET page, the snapshot's form makes a later POST to its action a
        credential post whatever its fields are named, as a recorded page's does."""
        form = (
            '<form action="/session" method="post"><input type="text" name="username">'
            '<input type="password" name="password"><button>Go</button></form>'
        )
        entries = [
            self._get("/", self._SHELL),
            self._snapshot_post(tmp_path, "/session", form, body=self._CREDS),
            self._post("/session", {"otp": "123456"}),
        ]
        result = self._run(tmp_path, entries)
        assert [o.kind for o in result.login if o.method == "POST"] == [
            "credential_post",
            "credential_post",
        ]

    def test_a_snapshot_on_a_post_that_is_no_credential_post_is_not_read(
        self, tmp_path: Path
    ) -> None:
        post = self._snapshot_post(tmp_path, "/api/search", self._DIV_MODAL)
        post["request"]["postData"]["text"] = json.dumps({"query": "alice"})
        result = self._run(tmp_path, [self._get("/", self._SHELL), post])
        assert result.login_forms == ()
        assert result.login == ()

    def test_a_snapshot_in_a_post_s_html_response_rule_does_not_apply(self, tmp_path: Path) -> None:
        """The snapshot is the page the post was sent from, so its dialog counts
        even though the entry is a POST."""
        modal = self._DIV_MODAL.replace('class="modal"', 'class="modal" role="dialog"')
        result = self._run(
            tmp_path,
            [self._get("/", self._SHELL), self._snapshot_post(tmp_path, "/api/auth", modal)],
        )
        (form,) = result.login_forms
        assert form.formless
        assert not form.plain_block


_OPENER_SELENIUM_COMMENT = (
    "Selenium runs no step wait_for, so this step pauses after the click instead; raise "
    "the delay if the form takes longer to appear, as one whose markup the page fetches "
    "on the click can."
)


def _suggestion(selector: str, *, waits: bool = True) -> str:
    """The comment a generated login carries for a suggested opener; *waits* when the
    form's step holds a commented wait_for (nodriver, with a printable first field)."""
    tail = (
        "uncomment the LoginStep below and the commented wait_for in the step after it."
        if waits
        else "uncomment the LoginStep below."
    )
    return (
        f'GP-FILL: a click recorded before this form\'s first field was on "{selector}". '
        f"If that click opens this form, {tail} If it does not, find the control that "
        "opens it and add a click-only LoginStep with its selector."
    )


def _uncommented(code: str) -> str:
    """*code* with the suggested opener step and its wait uncommented, as an author
    who confirmed it would leave it: each commented block from its opening line to the
    ``# ),`` at the same indent."""
    out: list[str] = []
    closing_pad: str | None = None
    for line in code.split("\n"):
        stripped = line.lstrip()
        pad = line[: len(line) - len(stripped)]
        opens = stripped.startswith("# LoginStep(") or stripped.startswith("# wait_for=")
        if closing_pad is None and opens:
            out.append(pad + stripped[2:])
            if stripped.endswith("("):
                closing_pad = pad
            continue
        if closing_pad is not None and pad.startswith(closing_pad) and stripped.startswith("#"):
            out.append(pad + stripped[2:])
            if pad == closing_pad and stripped.startswith("# )"):
                closing_pad = None
            continue
        out.append(line)
    return "\n".join(out)


# The state the listener sends with a click: a control, not in a dialog, shown at
# the click before it, with no field shown; a test step's fourth item overrides it,
# and gives a focus its path.
_CLICK = {"control": True, "inDialog": False, "revealed": False, "shownFields": []}


class _OpenerCase(_DialogDigestCase):
    """A run whose page's script renders a formless login after a header link."""

    _SHELL = '<header><a href="#" id="signin">Sign in</a></header><div id="app"></div>'
    _DIV_MODAL = TestALoginReadFromTheDomSnapshot._DIV_MODAL
    _HEADER = '<header><a href="#" id="signin">Sign in</a></header>'
    _FORM = (
        '<form><input type="email" id="m-email" name="email">'
        '<input type="password" id="m-pw" name="password">'
        '<button type="submit" id="m-go">Sign in</button></form>'
    )
    # The post is sent ten seconds after the page loaded.
    _PAGE_AT = "2026-09-10T10:00:00.000Z"
    _POST_AT = "2026-09-10T10:00:10.000Z"
    _OPENED = (
        ("click", "#signin", 5000),
        ("focus", "#m-email", 3000),
        ("focus", "#m-pw", 2000),
        ("click", "#m-go", 40),
    )

    def _entries(
        self,
        run: Path,
        steps: tuple[tuple, ...] = _OPENED,
        *,
        snapshot: bool = True,
        between: list[dict] | None = None,
    ) -> list[dict]:
        if snapshot:
            (run / "snapshots").mkdir(exist_ok=True)
            (run / "snapshots" / "9.1.html").write_text(self._DIV_MODAL)
        post = self._post("/api/auth/login/", self._BODY, started=self._POST_AT)
        if snapshot:
            post["request"]["_domSnapshot"] = "snapshots/9.1.html"
            post["request"]["_domSnapshotUrl"] = f"{self._HOST}/"
        post["request"]["_loginInteractions"] = [
            {
                "kind": step[0],
                "selector": step[1],
                "msBefore": step[2],
                **(_CLICK if step[0] == "click" else {}),
                **(step[3] if len(step) > 3 else {}),
            }
            for step in steps
        ]
        return [self._get("/", self._SHELL, started=self._PAGE_AT), *(between or []), post]

    def _candidate(self, tmp_path: Path, steps: tuple = _OPENED, **kwargs: Any) -> str:
        (form,) = self._run(tmp_path, self._entries(tmp_path, steps, **kwargs)).login_forms
        return form.opener_candidate

    def _with_snapshot(self, tmp_path: Path, html: str, steps: tuple = _OPENED, served: str = ""):  # noqa: ANN202
        entries = self._entries(tmp_path, steps)
        (tmp_path / "snapshots" / "9.1.html").write_text(html)
        if served:
            entries[0] = self._get("/", served, started=self._PAGE_AT)
        return self._run(tmp_path, entries)

    def _served(self, tmp_path: Path, page: str, steps: tuple = _OPENED):  # noqa: ANN202
        entries = self._entries(tmp_path, steps, snapshot=False)
        entries[0] = self._get("/", page, started=self._PAGE_AT)
        return self._run(tmp_path, entries)

    def _load(self, at: str, **kwargs: Any) -> dict:
        """A GET answered with HTML at *at* (seconds past 10:00:00)."""
        status = kwargs.pop("status", 200)
        entry = self._get(
            "/account", "<main>account</main>", started=f"2026-09-10T10:00:{at}Z", status=status
        )
        if "kind" in kwargs:
            entry["request"]["_resourceType"] = kwargs["kind"]
        for name, value in kwargs.get("headers", {}).items():
            entry["request"]["headers"].append({"name": name, "value": value})
        return entry

    @staticmethod
    def _projected(result) -> list:  # noqa: ANN001
        return [
            form["opener_candidate"]
            for form in json.loads(render_endpoints_json(result))["login"]["forms"]
        ]

    @staticmethod
    def _plugin_on(result, backend: str) -> str:  # noqa: ANN001
        return render(
            ScaffoldSpec(
                name="myshop",
                mode="new_project",
                backend=backend,  # type: ignore[arg-type]
                base_url="https://myshop.example.com",
                digest=result,
            )
        )["src/graftpunk_myshop/plugin.py"]

    @staticmethod
    def _steps(code: str) -> list:
        from graftpunk.plugins.cli_plugin import LoginStep

        steps = code[code.index("steps=[") + len("steps=[") : code.index("        ],")]
        return eval(f"[{steps}]", {"LoginStep": LoginStep})  # noqa: S307 - our own render


class TestTheRecordingSuggestsTheOpener(_OpenerCase):
    """``request._loginInteractions`` (#267, part 3): the recording suggests the click
    that opened a login; a generated login never clicks it until the author confirms
    it by uncommenting the step."""

    def test_the_last_control_click_before_the_first_field_is_suggested(
        self, tmp_path: Path
    ) -> None:
        result = self._run(tmp_path, self._entries(tmp_path))
        (form,) = result.login_forms
        assert form.opener_candidate == "#signin"
        assert self._projected(result) == ["#signin"]

    def test_nodriver_gets_a_commented_step_and_a_commented_wait(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, self._entries(tmp_path))
        code = self._plugin_on(result, "nodriver")
        assert _suggestion("#signin") in _comment_text(code)
        # The snapshot's comment stays: the suggestion may be wrong.
        assert _SNAPSHOT_GP_FILL in _comment_text(code)
        # As generated, the one-step login it was before.
        (step,) = self._steps(code)
        assert (step.submit, step.wait_for) == ("#m-go", "")
        # Confirmed, two steps, the form's waiting for its first field.
        confirmed = _uncommented(code)
        compile(confirmed, "plugin.py", "exec")
        opener, form_step = self._steps(confirmed)
        assert (opener.submit, opener.fields, opener.delay) == ("#signin", {}, 0.0)
        assert (form_step.submit, form_step.wait_for) == ("#m-go", "#m-email")

    def test_selenium_gets_a_commented_step_with_a_delay(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, self._entries(tmp_path))
        code = self._plugin_on(result, "selenium")
        comments = _comment_text(code)
        assert _suggestion("#signin", waits=False) in comments
        assert _OPENER_SELENIUM_COMMENT in comments
        assert "wait_for=" not in code
        (step,) = self._steps(code)
        assert step.delay == 0.0
        opener, form_step = self._steps(_uncommented(code))
        assert (opener.submit, opener.delay) == ("#signin", 2.0)
        assert form_step.wait_for == ""

    def test_without_interactions_nothing_is_suggested_from_markup(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, self._entries(tmp_path, ()))
        assert result.login_forms[0].opener_candidate == ""
        assert self._projected(result) == [None]
        assert _SNAPSHOT_GP_FILL in _comment_text(self._plugin(result))

    def test_a_selector_of_any_character_still_compiles(self, tmp_path: Path) -> None:
        """A NUL, a line separator, and a next-line in the suggestion: escaped in the
        comment, and a string literal in the commented step, so the file compiles
        commented and uncommented."""
        result = self._run(tmp_path, self._entries(tmp_path))
        odd = "#a\x00b c\x85d"
        spec = dataclasses.replace(
            result,
            login_forms=(dataclasses.replace(result.login_forms[0], opener_candidate=odd),),
        )
        for backend in ("nodriver", "selenium"):
            code = self._plugin_on(spec, backend)
            compile(code, "plugin.py", "exec")
            assert "\x00" not in code
            assert "\\u2028" in _comment_text(code)
            # Escaped in the commented step too, not only in the GP-FILL.
            assert "\u2028" not in code
            assert "\x85" not in code
            confirmed = _uncommented(code)
            compile(confirmed, "plugin.py", "exec")
            assert self._steps(confirmed)[0].submit == odd


class TestWhichClickIsSuggested(_OpenerCase):
    """The candidate: the last click on a control, not flagged inside the login, before
    the first focus on one of the form's fields, with no navigation between."""

    def test_a_focus_elsewhere_first_does_not_count(self, tmp_path: Path) -> None:
        assert self._candidate(tmp_path, (("focus", "#search", 9000), *self._OPENED)) == "#signin"

    def test_the_last_click_wins(self, tmp_path: Path) -> None:
        assert self._candidate(tmp_path, (("click", "#menu", 7000), *self._OPENED)) == "#signin"

    def test_a_field_spelled_another_way_is_still_the_field(self, tmp_path: Path) -> None:
        steps = (("click", "#signin", 5000), ("focus", '[id="m-pw"]', 2000))
        assert self._candidate(tmp_path, steps) == "#signin"

    def test_a_click_on_no_control_after_it_means_no_suggestion(self, tmp_path: Path) -> None:
        """A consent button, then a div opener: the div may be what opened the login."""
        steps = (
            ("click", "#accept", 6000),
            ("click", None, 5000, {"control": False}),
            *self._OPENED[1:],
        )
        assert self._candidate(tmp_path, steps) == ""

    def test_a_click_on_no_control_before_it_does_not_count(self, tmp_path: Path) -> None:
        steps = (("click", None, 7000, {"control": False}), *self._OPENED)
        assert self._candidate(tmp_path, steps) == "#signin"

    def test_a_click_flagged_inside_the_login_is_passed_over(self, tmp_path: Path) -> None:
        """Revealed or not: an inline sign-up beside the opener gives a wrong
        suggestion, which the author does not confirm."""
        for revealed in (False, True):
            steps = (
                ("click", "#accept", 8000),
                ("click", "#signin", 6000, {"inLogin": True, "revealed": revealed}),
                *self._OPENED[1:],
            )
            assert self._candidate(tmp_path, steps) == "#accept"

    def test_a_click_on_the_form_s_own_controls_is_no_candidate(self, tmp_path: Path) -> None:
        for selector in ("#m-go", '[id="m-email"]'):
            steps = (("click", "#signin", 5000), ("click", selector, 4000), *self._OPENED[1:])
            assert self._candidate(tmp_path, steps) == ""

    def test_a_click_after_the_first_focus_is_no_candidate(self, tmp_path: Path) -> None:
        steps = (("focus", "#m-email", 3000), ("click", "#signin", 2500))
        assert self._candidate(tmp_path, steps) == ""

    def test_no_focus_on_a_field_gives_none(self, tmp_path: Path) -> None:
        steps = (("click", "#signin", 5000), ("focus", "#search", 3000))
        assert self._candidate(tmp_path, steps) == ""

    def test_the_first_credential_post_gives_the_suggestion(self, tmp_path: Path) -> None:
        entries = self._entries(tmp_path)
        again = json.loads(json.dumps(entries[-1]))
        again["startedDateTime"] = "2026-09-10T10:00:30.000Z"
        again["request"]["_loginInteractions"][0]["selector"] = "#retry"
        result = self._run(tmp_path, [*entries, again])
        assert [form.opener_candidate for form in result.login_forms] == ["#signin"]


class TestANavigationBetweenTheClickAndTheField(_OpenerCase):
    def test_a_page_loaded_between_gives_none(self, tmp_path: Path) -> None:
        loaded = self._get("/account", "<main>a</main>", started="2026-09-10T10:00:06.000Z")
        assert self._candidate(tmp_path, between=[loaded]) == ""

    def test_a_page_loaded_before_the_click_does_not_count(self, tmp_path: Path) -> None:
        loaded = self._get("/help", "<main>h</main>", started="2026-09-10T10:00:04.000Z")
        assert self._candidate(tmp_path, between=[loaded]) == "#signin"

    def test_a_json_fetch_or_an_html_fragment_is_no_navigation(self, tmp_path: Path) -> None:
        fetched = _entry("GET", f"{self._HOST}/api/config", started="2026-09-10T10:00:06.000Z")
        assert self._candidate(tmp_path, between=[fetched]) == "#signin"
        assert self._candidate(tmp_path, between=[self._load("06.000", kind="XHR")]) == "#signin"

    def test_a_document_request_is_one_whatever_its_status(self, tmp_path: Path) -> None:
        for status in (200, 404):
            loaded = self._load("06.000", kind="Document", status=status)
            assert self._candidate(tmp_path, between=[loaded]) == ""

    @pytest.mark.parametrize(
        ("headers", "candidate"),
        [
            ({"Sec-Fetch-Mode": "navigate"}, ""),
            ({"Sec-Fetch-Mode": " Navigate "}, ""),
            ({"Sec-Fetch-Dest": "document"}, ""),
            ({"Sec-Fetch-Dest": "iframe"}, ""),
            ({"Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}, "#signin"),
        ],
    )
    def test_without_a_type_the_sec_fetch_headers_decide(
        self, tmp_path: Path, headers: dict, candidate: str
    ) -> None:
        loaded = self._load("06.000", headers=headers)
        assert self._candidate(tmp_path, between=[loaded]) == candidate

    def test_with_neither_a_non_2xx_html_get_is_no_navigation(self, tmp_path: Path) -> None:
        assert self._candidate(tmp_path, between=[self._load("06.000", status=404)]) == "#signin"

    @pytest.mark.parametrize(
        ("at", "candidate"),
        [("04.999", "#signin"), ("05.000", ""), ("07.000", ""), ("07.001", "#signin")],
    )
    def test_the_window_includes_both_ends(self, tmp_path: Path, at: str, candidate: str) -> None:
        loaded = self._load(at, kind="Document")
        assert self._candidate(tmp_path, between=[loaded]) == candidate


class TestWhatDisqualifiesTheCandidate(_OpenerCase):
    """No suggestion when the page's state at the click says it did not open the
    login."""

    def test_the_form_s_field_already_shown(self, tmp_path: Path) -> None:
        steps = (("click", "#accept", 7000, {"shownFields": ["#m-email"]}), *self._OPENED[1:])
        result = self._run(tmp_path, self._entries(tmp_path, steps))
        assert result.login_forms[0].opener_candidate == ""
        assert "accept" not in self._plugin(result)
        assert _SNAPSHOT_GP_FILL in _comment_text(self._plugin(result))

    def test_a_route_change(self, tmp_path: Path) -> None:
        steps = (
            ("click", "#login-link", 6000, {"path": "/"}),
            ("focus", "#m-email", 3000, {"path": "/login"}),
        )
        assert self._candidate(tmp_path, steps) == ""
        same = (
            ("click", "#signin", 6000, {"path": "/"}),
            ("focus", "#m-email", 3000, {"path": "/"}),
        )
        assert self._candidate(tmp_path, same) == "#signin"

    def test_inside_a_dialog_or_revealed(self, tmp_path: Path) -> None:
        for state in ({"inDialog": True}, {"revealed": True}):
            steps = (
                ("click", "#signin", 7000),
                ("click", "#use-email", 6000, state),
                *self._OPENED[1:],
            )
            assert self._candidate(tmp_path, steps) == ""

    def test_no_usable_selector(self, tmp_path: Path) -> None:
        for selector in (None, "", "#user_40912873", "#" + "a" * 300):
            steps = (("click", selector, 5000), *self._OPENED[1:])
            assert self._candidate(tmp_path, steps) == ""

    def test_a_megabyte_selector_is_read_at_once(self, tmp_path: Path) -> None:
        import time

        entries = self._entries(
            tmp_path, (("click", "#" + "a" * 1_000_000, 5000), *self._OPENED[1:])
        )
        started = time.perf_counter()
        (form,) = self._run(tmp_path, entries).login_forms
        assert time.perf_counter() - started < 5
        assert form.opener_candidate == ""


class TestAPasswordOnlyForm(_OpenerCase):
    """Identifier-first: the snapshot holds the password step alone."""

    _SECOND_SCREEN = (
        '<html><body><div class="login"><input type="password" id="m-pw" name="password">'
        '<button type="submit" id="m-go">Sign in</button></div></body></html>'
    )

    def _password_only(self, tmp_path: Path, steps: tuple) -> str:
        result = self._with_snapshot(tmp_path, self._SECOND_SCREEN, steps)
        (form,) = result.login_forms
        assert "username" not in form.fields
        return form.opener_candidate

    def test_a_field_focused_before_the_click_is_an_earlier_step(self, tmp_path: Path) -> None:
        steps = (("focus", "#username", 9000), ("click", "#next", 6000), ("focus", "#m-pw", 3000))
        assert self._password_only(tmp_path, steps) == ""

    def test_a_field_shown_at_the_click_is_one_too(self, tmp_path: Path) -> None:
        """The username was prefilled or autofilled with no focus."""
        steps = (("click", "#next", 6000, {"shownFields": ["#username"]}), ("focus", "#m-pw", 3000))
        assert self._password_only(tmp_path, steps) == ""

    def test_only_a_focus_before_the_click_counts(self, tmp_path: Path) -> None:
        steps = (
            ("click", "#menu", 8000),
            ("click", "#next", 6000),
            ("focus", "#search", 5000),
            ("focus", "#m-pw", 3000),
        )
        assert self._password_only(tmp_path, steps) == "#next"


class TestWhichFormsCanBeOpenedByAClick(_OpenerCase):
    """A formless group, or a real form whose password sits in a dialog-marked element:
    a real form a page shows as it loads is never behind a click."""

    @pytest.mark.parametrize(
        "wrapper",
        [
            '<div role="dialog" style="display:none">{}</div>',
            "<dialog>{}</dialog>",
            '<div class="modal" aria-modal="true" hidden>{}</div>',
        ],
    )
    def test_a_served_form_inside_a_dialog(self, tmp_path: Path, wrapper: str) -> None:
        (form,) = self._served(tmp_path, self._HEADER + wrapper.format(self._FORM)).login_forms
        assert (form.formless, form.in_dialog, form.opener_candidate) == (False, True, "#signin")

    def test_a_served_form_hidden_but_not_marked(self, tmp_path: Path) -> None:
        page = self._HEADER + f'<div class="modal" hidden>{self._FORM}</div>'
        (form,) = self._served(tmp_path, page).login_forms
        assert (form.in_dialog, form.opener_candidate) == (False, "")

    def test_a_formless_dialog_in_the_served_html(self, tmp_path: Path) -> None:
        (form,) = self._served(tmp_path, self._HEADER + self._MODAL).login_forms
        assert form.formless
        assert form.opener_candidate == "#signin"

    def test_a_real_form_on_a_plain_login_page(self, tmp_path: Path) -> None:
        post = self._post(
            "/login", {"username": "alice", "password": "hunter2hunter2"}, started=self._POST_AT
        )
        post["request"]["_loginInteractions"] = [
            {"kind": "click", "selector": "#accept-cookies", "msBefore": 5000, **_CLICK},
            {"kind": "focus", "selector": 'input[name="username"]', "msBefore": 3000},
        ]
        result = self._run(tmp_path, [self._get("/login", self._REAL, started=self._PAGE_AT), post])
        assert result.login_forms[0].opener_candidate == ""

    def test_a_snapshot_form_inside_or_outside_a_dialog(self, tmp_path: Path) -> None:
        inside = self._with_snapshot(
            tmp_path,
            f'<html><body>{self._HEADER}<div role="dialog">{self._FORM}</div></body></html>',
        )
        assert inside.login_forms[0].opener_candidate == "#signin"
        outside = self._with_snapshot(
            tmp_path, f"<html><body>{self._HEADER}<main>{self._FORM}</main></body></html>"
        )
        assert outside.login_forms[0].opener_candidate == ""
        assert _SNAPSHOT_GP_FILL in _comment_text(self._plugin(outside))

    def test_a_served_modal_marked_only_once_open(self, tmp_path: Path) -> None:
        """Bootstrap 5 adds role and aria-modal as it shows the modal: the served copy
        is kept and takes the snapshot copy's dialog mark."""
        served = f'<div class="modal fade" tabindex="-1" aria-hidden="true">{self._FORM}</div>'
        shown = f'<div class="modal fade show" role="dialog" aria-modal="true">{self._FORM}</div>'
        result = self._with_snapshot(
            tmp_path,
            f"<html><body>{self._HEADER}{shown}</body></html>",
            served=self._HEADER + served,
        )
        (form,) = result.login_forms
        assert (form.from_snapshot, form.in_dialog, form.opener_candidate) == (
            False,
            True,
            "#signin",
        )

    def test_an_unmarked_snapshot_copy_marks_nothing(self, tmp_path: Path) -> None:
        served = f'<div class="modal">{self._FORM}</div>'
        result = self._with_snapshot(
            tmp_path,
            f"<html><body>{self._HEADER}{served}</body></html>",
            served=self._HEADER + served,
        )
        assert (result.login_forms[0].in_dialog, result.login_forms[0].opener_candidate) == (
            False,
            "",
        )

    def test_a_marked_copy_on_another_page_marks_nothing(self, tmp_path: Path) -> None:
        served = f'<div class="modal">{self._FORM}</div>'
        shown = f'<div class="modal" role="dialog">{self._FORM}</div>'
        entries = self._entries(tmp_path)
        (tmp_path / "snapshots" / "9.1.html").write_text(f"<html><body>{shown}</body></html>")
        entries[-1]["request"]["_domSnapshotUrl"] = f"{self._HOST}/other"
        entries[0] = self._get("/", self._HEADER + served, started=self._PAGE_AT)
        forms = self._run(tmp_path, entries).login_forms
        served_form = next(f for f in forms if not f.from_snapshot)
        assert served_form.in_dialog is False


class TestLoginOpenerRule:
    """``_login_opener`` read directly."""

    _AT = datetime(2026, 9, 10, 10, 0, 10, tzinfo=UTC)

    @staticmethod
    def _form(**kwargs: Any) -> LoginForm:
        return LoginForm(
            action="",
            method="",
            fields=kwargs.pop("fields", {"username": 'input[type="email"]'}),
            submit=None,
            hidden=(),
            source="https://myshop.example.com/",
            **kwargs,
        )

    def test_a_field_reached_only_through_its_unscoped_selector(self) -> None:
        form = self._form(unscoped_fields={"username": "#m-email"})
        steps = (
            LoginInteraction("click", "#signin", 5000.0, control=True),
            LoginInteraction("focus", "#m-email", 3000.0),
        )
        assert _login_opener(form, steps, self._AT, []) == "#signin"
        assert _login_opener(self._form(), steps, self._AT, []) is None

    def test_an_escaped_id_holding_an_account_value_is_no_suggestion(self) -> None:
        form = self._form(fields={"username": "#m-email"})
        escaped = "#\\34 \\30 \\39 \\31 \\32 \\38 \\37 \\33"
        steps = (
            LoginInteraction("click", escaped, 5000.0, control=True),
            LoginInteraction("focus", "#m-email", 3000.0),
        )
        assert _login_opener(form, steps, self._AT, []) is None

    def test_a_megabyte_focus_is_not_read(self) -> None:
        form = self._form(fields={"username": "#m-email"})
        steps = (
            LoginInteraction("click", "#signin", 5000.0, control=True),
            LoginInteraction("focus", "#m-email" + " " * 1_000_000, 3000.0),
        )
        assert _login_opener(form, steps, self._AT, []) is None


class TestTheCommentedStepFitsTheGeneratedProject(_OpenerCase):
    """Review round 5: the commented opener step and wait_for wrap as real step
    arguments do, so the generated project passes its own ruff check, and compile
    commented and uncommented."""

    @staticmethod
    def _spec(result, opener: str, first: str = ""):  # noqa: ANN001, ANN205
        form = dataclasses.replace(result.login_forms[0], opener_candidate=opener)
        if first:
            form = dataclasses.replace(
                form, fields={"username": first, "password": "#m-pw"}, unscoped_fields={}
            )
        return dataclasses.replace(result, login_forms=(form,))

    @pytest.mark.parametrize(
        "selector",
        [
            "#" + "a" * 69,
            "#" + "b" * 99,
            "#" + "c" * 299,
            "div:nth-of-type(1) > " * 10 + "a:nth-of-type(3)",
            "#\u00e9" * 40,
        ],
    )
    @pytest.mark.parametrize("backend", ["nodriver", "selenium"])
    def test_any_length_is_ruff_clean_and_reads_back(
        self, tmp_path: Path, selector: str, backend: str
    ) -> None:
        from tests.unit import test_scaffold_render as rendered

        result = self._run(tmp_path, self._entries(tmp_path))
        first = "#" + "u" * 90
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend=backend,  # type: ignore[arg-type]
            base_url="https://myshop.example.com",
            digest=self._spec(result, selector, first),
        )
        files = render(spec)
        checker = rendered.TestRenderedTreeIsRuffClean()
        tree = checker._write_tree(tmp_path / "tree", files)
        checker._assert_ruff_clean(tree, "check")
        code = files["src/graftpunk_myshop/plugin.py"]
        compile(code, "plugin.py", "exec")
        confirmed = _uncommented(code)
        compile(confirmed, "plugin.py", "exec")
        opener, form_step = self._steps(confirmed)
        assert opener.submit == selector
        assert form_step.wait_for == (first if backend == "nodriver" else "")

    def test_no_printable_first_field_means_no_wait(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, self._entries(tmp_path))
        # An action holding an id: a scoped selector cannot be printed unscoped.
        form = dataclasses.replace(
            result.login_forms[0],
            action="/accounts/40912873/login",
            fields={"password": 'form[action="/x"] input[name="password"]'},
            unscoped_fields={},
            opener_candidate="#signin",
        )
        spec = dataclasses.replace(result, login_forms=(form,))
        code = self._plugin_on(spec, "nodriver")
        assert _suggestion("#signin", waits=False) in _comment_text(code)
        assert "# wait_for" not in code


_SUCCESS_GP_FILL = (
    "GP-FILL: the recording suggests a success selector: an element on the logged-in page "
    "and on no recorded login page. Check that it appears only after login on a fresh "
    "visit to the login URL, then uncomment one suggestion (all of its lines):"
)

# The login_config main (008f2aa0) generates for a run with no logged-in page.
_LOGIN_CONFIG_ON_MAIN = """\
    login_config = LoginConfig(
        steps=[
            LoginStep(
                fields={
                    "password": "#password",
                    "username": "#email",
                },
                submit="#go",
            ),
        ],
        url="/login",
        failure="GP-FILL: text on the page indicating login failure",
        # GP-FILL: success, a CSS selector for an element that is on the page this login lands on
        #   and not on the login form itself.
        # GP-FILL: success_url, a glob matched against the whole URL this login lands on, e.g.
        #   */dashboard*. This run observed no redirect after the credential post.
    )
"""


def _uncommented_success(code: str, which: int) -> str:
    """*code* with the *which*-th suggested ``success`` argument uncommented, as an
    author who confirmed it would leave it: its ``# success=`` line, and, when it is
    wrapped, every commented line to its ``# ),``."""
    out: list[str] = []
    seen = -1
    open_block = False
    for line in code.split("\n"):
        stripped = line.lstrip()
        pad = line[: len(line) - len(stripped)]
        if stripped.startswith("# success="):
            seen += 1
            if seen == which:
                out.append(pad + stripped[2:])
                open_block = stripped.endswith("(")
                continue
        elif open_block and stripped.startswith("#"):
            out.append(pad + stripped[2:])
            open_block = not stripped.startswith("# )")
            continue
        out.append(line)
    return "\n".join(out)


class TestTheRecordingSuggestsASuccessSelector:
    """#268, part 1: elements on the run's logged-in page and on no recorded copy of
    the login page, suggested for ``success`` and never written live."""

    _HOST = "https://myshop.example.com"
    _LOGIN = (
        "<html><body><header><nav class='site-nav'></nav></header><main>"
        "<form action='/session' method='post'><input type='email' id='email' name='email'>"
        "<input type='password' id='password' name='password'><button id='go'>Sign in</button>"
        "</form></main></body></html>"
    )
    _LANDED = (
        "<html><body><header><nav class='site-nav'><div id='account-menu'>"
        "<a class='signout-link' href='/bye'>Sign out</a></div></nav></header>"
        "<main><p>Orders</p></main></body></html>"
    )
    _CREDS = {"email": "alice@example.com", "password": "hunter2hunter2"}

    def _entries(
        self, run: Path, *, snapshot: str = "", after: str = "", home: str = ""
    ) -> list[dict]:
        post = _entry(
            "POST",
            f"{self._HOST}/session",
            post_data=json.dumps(self._CREDS),
            started="2026-09-10T10:00:05.000Z",
        )
        if snapshot:
            (run / "snapshots").mkdir(exist_ok=True)
            (run / "snapshots" / "9.1.html").write_text(snapshot)
            post["request"]["_domSnapshot"] = "snapshots/9.1.html"
            post["request"]["_domSnapshotUrl"] = f"{self._HOST}/login"
        entries = [
            _entry("GET", f"{self._HOST}/login", content_type="text/html", body=self._LOGIN),
            post,
        ]
        if home:
            visited = _entry("GET", f"{self._HOST}/", content_type="text/html", body=home)
            visited["request"]["_resourceType"] = "Document"
            entries.insert(0, visited)
        if after:
            landed = _entry(
                "GET",
                f"{self._HOST}/account",
                content_type="text/html",
                body=after,
                started="2026-09-10T10:00:06.000Z",
            )
            landed["request"]["_resourceType"] = "Document"
            entries.append(landed)
        return entries

    def _run(  # noqa: ANN202
        self,
        run: Path,
        *,
        page_source: str = "",
        snapshot: str = "",
        after: str = "",
        home: str = "",
    ):
        har = _write_har(run, self._entries(run, snapshot=snapshot, after=after, home=home))
        source = None
        if page_source:
            source = run / "page-source.html"
            source.write_text(page_source)
        return digest(DigestSource(har_path=har, page_source=source))

    @staticmethod
    def _files(result, backend: str) -> dict[str, str]:  # noqa: ANN001
        return render(
            ScaffoldSpec(
                name="myshop",
                mode="new_project",
                backend=backend,  # type: ignore[arg-type]
                base_url="https://myshop.example.com",
                digest=result,
            )
        )

    @staticmethod
    def _login_config(code: str):  # noqa: ANN205
        from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep

        start = code.index("LoginConfig(")
        end = code.index("\n    )\n", start) + 6
        namespace = {"LoginConfig": LoginConfig, "LoginStep": LoginStep}
        return eval(code[start:end], namespace)  # noqa: S307 - our own render

    def test_the_page_source_at_stop_gives_the_suggestions(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, page_source=self._LANDED)
        assert result.success_candidates == ("#account-menu", "a.signout-link")
        payload = json.loads(render_endpoints_json(result))
        assert payload["login"]["success_candidates"] == ["#account-menu", "a.signout-link"]

    def test_a_page_loaded_after_the_post_gives_the_suggestions(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, after=self._LANDED)
        assert result.success_candidates == ("#account-menu", "a.signout-link")

    def test_the_snapshot_at_the_post_is_a_login_copy(self, tmp_path: Path) -> None:
        snapshot = self._LOGIN.replace("<main>", "<main><div id='account-menu'></div>")
        result = self._run(tmp_path, page_source=self._LANDED, snapshot=snapshot)
        assert result.success_candidates == ("a.signout-link",)

    def test_a_page_loaded_before_the_login_is_a_logged_out_page(self, tmp_path: Path) -> None:
        """An element the home page showed before the login marks no logged-in state,
        though the login page itself lacks it."""
        home = "<html><body><div id='account-menu'></div></body></html>"
        result = self._run(tmp_path, page_source=self._LANDED, home=home)
        assert result.success_candidates == ("a.signout-link",)

    def test_a_page_source_holding_a_login_form_gives_nothing(self, tmp_path: Path) -> None:
        stopped = self._LANDED.replace("<main>", "<main>" + self._LOGIN)
        assert self._run(tmp_path, page_source=stopped).success_candidates == ()

    @pytest.mark.parametrize("backend", ["nodriver", "selenium"])
    def test_no_logged_in_page_gives_nothing_and_main_s_output(
        self, tmp_path: Path, backend: str
    ) -> None:
        result = self._run(tmp_path)
        assert result.success_candidates == ()
        assert json.loads(render_endpoints_json(result))["login"]["success_candidates"] == []
        code = self._files(result, backend)["src/graftpunk_myshop/plugin.py"]
        assert _LOGIN_CONFIG_ON_MAIN in code
        assert "# success=" not in code

    @pytest.mark.parametrize("backend", ["nodriver", "selenium"])
    def test_the_generator_writes_them_commented_and_success_stays_unset(
        self, tmp_path: Path, backend: str
    ) -> None:
        from tests.unit import test_scaffold_render as rendered

        files = self._files(self._run(tmp_path, page_source=self._LANDED), backend)
        code = files["src/graftpunk_myshop/plugin.py"]
        assert _SUCCESS_GP_FILL in _comment_text(code)
        assert '        # success="a.signout-link",' in code
        assert '        # success="#account-menu",' in code
        compile(code, "plugin.py", "exec")
        assert self._login_config(code).success == ""
        checker = rendered.TestRenderedTreeIsRuffClean()
        checker._assert_tree_is_clean(checker._write_tree(tmp_path / "tree", files))
        for which, selector in enumerate(("#account-menu", "a.signout-link")):
            confirmed = _uncommented_success(code, which)
            compile(confirmed, "plugin.py", "exec")
            assert self._login_config(confirmed).success == selector

    @pytest.mark.parametrize(
        "selector", ["#" + "a" * 69, "#" + "b" * 99, "#" + "c" * 250, '#a\x00b c\x85d"e']
    )
    @pytest.mark.parametrize("backend", ["nodriver", "selenium"])
    def test_any_selector_is_ruff_clean_and_reads_back(
        self, tmp_path: Path, selector: str, backend: str
    ) -> None:
        """Escaped and wrapped as the commented opener step is, so the generated
        project passes its own ruff check and compiles commented and uncommented."""
        from tests.unit import test_scaffold_render as rendered

        result = dataclasses.replace(
            self._run(tmp_path, page_source=self._LANDED), success_candidates=(selector,)
        )
        files = self._files(result, backend)
        checker = rendered.TestRenderedTreeIsRuffClean()
        checker._assert_ruff_clean(checker._write_tree(tmp_path / "tree", files), "check")
        code = files["src/graftpunk_myshop/plugin.py"]
        compile(code, "plugin.py", "exec")
        assert self._login_config(code).success == ""
        confirmed = _uncommented_success(code, 0)
        compile(confirmed, "plugin.py", "exec")
        assert self._login_config(confirmed).success == selector

    # Review round 1 (#325): the final logged-in page, the pages that do not count,
    # and the credential values.

    def _get(self, path: str, body: str, *, status: int = 200, **headers: str) -> dict:
        entry = _entry(
            "GET",
            f"{self._HOST}{path}",
            content_type="text/html",
            body=body,
            status=status,
            request_headers=headers or None,
        )
        if not headers:
            entry["request"]["_resourceType"] = "Document"
        return entry

    def _credential_post(self, payload: dict | None = None, html: str = "") -> dict:
        return _entry(
            "POST",
            f"{self._HOST}/session",
            post_data=json.dumps(payload or self._CREDS),
            content_type="text/html" if html else "application/json",
            body=html or "{}",
        )

    @staticmethod
    def _digest(run: Path, entries: list[dict], page_source: str = ""):  # noqa: ANN205
        run.mkdir(parents=True, exist_ok=True)
        har = _write_har(run, entries)
        source = None
        if page_source:
            source = run / "page-source.html"
            source.write_text(page_source)
        return digest(DigestSource(har_path=har, page_source=source))

    def _document_post(self, path: str, payload: dict) -> dict:
        """A form submitted from the page the browser is on: a top-level document
        POST."""
        entry = _entry("POST", f"{self._HOST}{path}", status=302, post_data=json.dumps(payload))
        entry["request"]["_resourceType"] = "Document"
        return entry

    @pytest.mark.parametrize("with_page_source", [True, False])
    def test_the_landing_page_in_the_har_and_at_stop_gives_its_markers(
        self, tmp_path: Path, with_page_source: bool
    ) -> None:
        """A server-rendered login recorded with Ctrl+C on the landing page: the HAR's
        copy of the landing page is a logged-in page, not a logged-out one."""
        landing = self._LANDED.replace("<main>", "<main><div id='welcome'></div>")
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/account", landing),
        ]
        result = self._digest(tmp_path, entries, landing if with_page_source else "")
        assert result.success_candidates == ("#account-menu", "a.signout-link", "#welcome")

    @pytest.mark.parametrize("with_page_source", [True, False])
    def test_after_browsing_on_the_landing_page_s_markers_rank_first(
        self, tmp_path: Path, with_page_source: bool
    ) -> None:
        """The run browsed to /orders after landing and stopped there: what every
        logged-in page carries ranks above what only the final page has."""
        landing = self._LANDED.replace("<main>", "<main><div id='welcome'></div>")
        orders = self._LANDED.replace(
            "<main><p>Orders</p></main>", "<main><div id='orders-table'></div></main>"
        )
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/account", landing),
            self._get("/orders", orders),
        ]
        result = self._digest(tmp_path, entries, orders if with_page_source else "")
        assert result.success_candidates == ("#account-menu", "a.signout-link", "#orders-table")

    def test_a_page_after_the_post_that_submits_no_form_is_logged_in(self, tmp_path: Path) -> None:
        between = (
            "<html><body><div id='user-consent'></div><div id='account-menu'></div></body></html>"
        )
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/consent", between),
            self._get("/account", self._LANDED),
        ]
        result = self._digest(tmp_path, entries)
        assert result.success_candidates == ("#account-menu", "a.signout-link")

    def test_a_page_a_form_was_submitted_from_after_the_post_is_logged_out(
        self, tmp_path: Path
    ) -> None:
        """A "stay signed in" page: the browser posted a form from it, so it is a step
        of the login, and an element it shares with the landing is no marker."""
        consent = (
            "<html><body><div id='user-consent'></div><div id='account-menu'></div></body></html>"
        )
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/consent", consent),
            self._document_post("/consent", {"remember": "yes"}),
            self._get("/account", self._LANDED),
        ]
        assert self._digest(tmp_path, entries).success_candidates == ("a.signout-link",)

    def test_a_script_post_from_a_page_after_the_post_leaves_it_logged_in(
        self, tmp_path: Path
    ) -> None:
        between = "<html><body><div id='account-menu'></div></body></html>"
        fetched = _entry("POST", f"{self._HOST}/api/track", post_data=json.dumps({"e": "x"}))
        fetched["request"]["_resourceType"] = "Fetch"
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/home", between),
            fetched,
            self._get("/account", self._LANDED),
        ]
        assert self._digest(tmp_path, entries).success_candidates == (
            "#account-menu",
            "a.signout-link",
        )

    def test_a_page_asking_for_a_one_time_code_after_the_post_is_logged_out(
        self, tmp_path: Path
    ) -> None:
        mfa = (
            "<html><body><div id='account-verify'></div>"
            "<input id='code' name='code' autocomplete='one-time-code'></body></html>"
        )
        final = self._LANDED.replace("<main>", "<main><div id='account-verify'></div>")
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/mfa", mfa),
            self._get("/account", final),
        ]
        result = self._digest(tmp_path, entries)
        assert result.success_candidates == ("#account-menu", "a.signout-link")

    def test_an_mfa_page_before_the_page_source_is_a_logged_out_page(self, tmp_path: Path) -> None:
        mfa = (
            "<html><body><div id='account-verify'></div><form action='/mfa' method='post'>"
            "<input id='otp-code' name='otp'></form></body></html>"
        )
        final = self._LANDED.replace("<main>", "<main><div id='account-verify'></div>")
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/mfa", mfa),
            self._document_post("/mfa", {"otp": "123456"}),
        ]
        result = self._digest(tmp_path, entries, page_source=final)
        assert result.success_candidates == ("#account-menu", "a.signout-link")
        # Without the submission the same page is a logged-in one.
        unsubmitted = self._digest(tmp_path / "other", entries[:-1], page_source=final)
        assert "#account-verify" in unsubmitted.success_candidates

    def test_a_page_source_holding_a_login_form_leaves_the_last_page_final(
        self, tmp_path: Path
    ) -> None:
        """The user signed out and stopped on the login page: the last page loaded
        after the post is the final logged-in page."""
        stopped = self._LOGIN.replace("<main>", "<main><div id='signed-off-note'></div>")
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/account", self._LANDED),
        ]
        result = self._digest(tmp_path, entries, page_source=stopped)
        assert result.success_candidates == ("#account-menu", "a.signout-link")

    @pytest.mark.parametrize("dest", ["iframe", "frame", "embed", "object", "IFrame"])
    def test_an_iframe_document_is_no_page(self, tmp_path: Path, dest: str) -> None:
        frame = self._get("/widget", self._LANDED, **{"Sec-Fetch-Dest": dest})
        frame["request"]["_resourceType"] = "Document"
        entries = [self._get("/login", self._LOGIN), self._credential_post(), frame]
        assert self._digest(tmp_path, entries).success_candidates == ()

    def test_a_document_of_another_resource_type_is_no_page(self, tmp_path: Path) -> None:
        frame = self._get("/widget", self._LANDED)
        frame["request"]["_resourceType"] = "Other"
        entries = [self._get("/login", self._LOGIN), self._credential_post(), frame]
        assert self._digest(tmp_path, entries).success_candidates == ()

    def test_an_iframe_does_not_take_the_final_page(self, tmp_path: Path) -> None:
        frame = self._get(
            "/widget",
            "<html><body><div id='user-widget'></div></body></html>",
            **{"Sec-Fetch-Dest": "iframe"},
        )
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/account", self._LANDED),
            frame,
        ]
        result = self._digest(tmp_path, entries)
        assert result.success_candidates == ("#account-menu", "a.signout-link")

    def test_a_post_response_holding_a_login_form_is_a_logged_out_page(
        self, tmp_path: Path
    ) -> None:
        """A failed attempt the server re-rendered with the form."""
        failed = self._LOGIN.replace("<main>", "<main><div id='account-menu'></div>")
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(html=failed),
            self._credential_post(),
        ]
        result = self._digest(tmp_path, entries, page_source=self._LANDED)
        assert result.success_candidates == ("a.signout-link",)

    def test_a_login_page_loaded_again_after_the_post_is_a_logged_out_page(
        self, tmp_path: Path
    ) -> None:
        again = self._LOGIN.replace("<main>", "<main><div id='account-menu'></div>")
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/login", again),
        ]
        result = self._digest(tmp_path, entries, page_source=self._LANDED)
        assert result.success_candidates == ("a.signout-link",)

    def test_a_page_answering_404_after_the_post_is_no_page(self, tmp_path: Path) -> None:
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/account", self._LANDED, status=404),
        ]
        assert self._digest(tmp_path, entries).success_candidates == ()

    def test_an_html_fragment_a_script_fetched_is_no_page(self, tmp_path: Path) -> None:
        fragment = self._get("/account", self._LANDED, **{"Sec-Fetch-Mode": "cors"})
        entries = [self._get("/login", self._LOGIN), self._credential_post(), fragment]
        assert self._digest(tmp_path, entries).success_candidates == ()

    def test_a_run_with_no_credential_post_suggests_nothing(self, tmp_path: Path) -> None:
        result = self._digest(tmp_path, [self._get("/login", self._LOGIN)], self._LANDED)
        assert result.success_candidates == ()
        assert json.loads(render_endpoints_json(result))["login"]["success_candidates"] == []

    def test_a_name_holding_a_credential_value_is_not_suggested(self, tmp_path: Path) -> None:
        payload = {"email": "jsmith@example.com", "username": "jdoe", "password": "pw-12345"}
        final = self._LANDED.replace(
            "<main>", "<main><div id='user-jsmith'></div><img class='avatar-jdoe'>"
        )
        entries = [self._get("/login", self._LOGIN), self._credential_post(payload)]
        result = self._digest(tmp_path, entries, page_source=final)
        assert result.success_candidates == ("#account-menu", "a.signout-link")
        unrelated = self._digest(
            tmp_path / "other",
            [self._get("/login", self._LOGIN), self._credential_post()],
            page_source=final,
        )
        assert "#user-jsmith" in unrelated.success_candidates

    _NAMED = (
        "<main><div id='user-jsmith'></div><div id='user-hunter-two'></div>"
        "<div class='plan-user'></div>"
    )

    def _named_run(self, run: Path, post: dict):  # noqa: ANN202
        final = self._LANDED.replace("<main>", self._NAMED)
        return self._digest(run, [self._get("/login", self._LOGIN), post], page_source=final)

    def test_a_form_encoded_post_s_values_are_not_suggested(self, tmp_path: Path) -> None:
        post = _entry(
            "POST",
            f"{self._HOST}/session",
            post_data="email=jsmith%40example.com&password=hunter.two&type=user",
            request_headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        result = self._named_run(tmp_path, post)
        # The username and password values are rejected; "type=user" is no
        # identity field, so "user" rejects nothing.
        assert result.success_candidates == ("#account-menu", "a.signout-link", "div.plan-user")

    def test_a_multipart_post_s_values_are_not_suggested(self, tmp_path: Path) -> None:
        body = (
            "------b7\r\n"
            'Content-Disposition: form-data; name="login"\r\n\r\n'
            "jsmith\r\n"
            "------b7\r\n"
            'Content-Disposition: form-data; name="password"\r\n\r\n'
            "hunter-two\r\n"
            "------b7\r\n"
            'Content-Disposition: form-data; name="avatar"; filename="user.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
            "plan\r\n"
            "------b7--\r\n"
        )
        post = _entry(
            "POST",
            f"{self._HOST}/session",
            post_data=body,
            request_headers={"Content-Type": "multipart/form-data; boundary=----b7"},
        )
        result = self._named_run(tmp_path, post)
        assert result.success_candidates == ("#account-menu", "a.signout-link", "div.plan-user")

    def test_a_nested_json_post_s_values_are_not_suggested(self, tmp_path: Path) -> None:
        payload = {
            "auth": {"users": [{"username": "jsmith"}], "password": "hunter_two"},
            "account_no": 40912873,
            "remember": True,
            "plan": "user",
        }
        result = self._named_run(tmp_path, self._credential_post(payload))
        assert result.success_candidates == ("#account-menu", "a.signout-link", "div.plan-user")
        har = _write_har(tmp_path, [self._credential_post(payload)], "one.har")
        (entry,) = parse_har_file(har).entries
        assert sorted(_account_values(entry, frozenset()) or ()) == ["hunter_two", "jsmith"]

    def test_any_value_written_as_an_email_is_not_suggested(self, tmp_path: Path) -> None:
        payload = {"id": "jsmith@example.com", "pass": "x"}
        result = self._named_run(tmp_path, self._credential_post(payload))
        assert "#user-jsmith" not in result.success_candidates

    # Review round 3 (#325): the login's steps are only the leading run of the pages
    # after the post (login_signals.success_candidates), and a POST makes a page a
    # step only when it is step-shaped (login_signals.is_login_step_post).

    def _form_post(
        self, path: str, body: str, *, status: int = 302, html: str = "", **headers: str
    ) -> dict:
        """A form submitted from the page the browser is on: a top-level document
        POST, form-encoded."""
        entry = _entry(
            "POST",
            f"{self._HOST}{path}",
            status=status,
            content_type="text/html" if html else "application/json",
            body=html or "{}",
            post_data=body,
            request_headers={"Content-Type": "application/x-www-form-urlencoded", **headers},
        )
        entry["request"]["_resourceType"] = "Document"
        return entry

    def _with(self, main: str) -> str:
        """The landed page with *main* as its main content."""
        return self._LANDED.replace("<main><p>Orders</p></main>", f"<main>{main}</main>")

    _MFA = (
        "<html><body><div id='brand-bar'></div><form action='/mfa' method='post'>"
        "<input name='code' autocomplete='one-time-code'><button>Verify</button>"
        "</form></body></html>"
    )

    @pytest.mark.parametrize("with_page_source", [True, False])
    def test_a_search_from_the_landing_page_leaves_it_logged_in(
        self, tmp_path: Path, with_page_source: bool
    ) -> None:
        results = self._with("<div id='results'></div>")
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/dashboard", self._with("<div id='dash'></div>")),
            self._form_post("/search", "q=milk"),
            self._get("/results", results),
        ]
        result = self._digest(tmp_path, entries, results if with_page_source else "")
        assert result.success_candidates == ("#account-menu", "a.signout-link", "#results")

    def test_an_add_to_cart_from_a_browsed_page_leaves_it_logged_in(self, tmp_path: Path) -> None:
        orders = self._with("<div id='orders-table'></div>")
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/dashboard", self._with("<div id='dash'></div>")),
            self._get("/orders", orders),
            self._form_post("/cart/add", "sku=1&qty=2"),
            self._get("/orders", orders),
        ]
        result = self._digest(tmp_path, entries, orders)
        assert result.success_candidates == ("#account-menu", "a.signout-link", "#orders-table")

    def test_a_post_answered_with_its_own_page_leaves_the_landing_logged_in(
        self, tmp_path: Path
    ) -> None:
        cart = self._with("<div id='cart'></div>")
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/dashboard", self._with("<div id='dash'></div>")),
            self._form_post("/cart/add", "sku=1", status=200, html=cart),
        ]
        result = self._digest(tmp_path, entries, cart)
        assert result.success_candidates == ("#account-menu", "a.signout-link", "#cart")

    def test_a_webforms_postback_leaves_the_landing_logged_in(self, tmp_path: Path) -> None:
        orders = self._with("<div id='orders'></div>")
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/Default.aspx", self._with("<div id='dash'></div>")),
            self._form_post(
                "/Default.aspx",
                "__EVENTTARGET=menu&__VIEWSTATE=dDwtMTA&__EVENTVALIDATION=x",
                status=200,
                html=orders,
            ),
        ]
        result = self._digest(tmp_path, entries, orders)
        assert result.success_candidates == ("#account-menu", "a.signout-link", "#orders")

    def test_a_logout_form_at_the_end_leaves_the_last_page_logged_in(self, tmp_path: Path) -> None:
        """The run signed out through a form from the final page and stopped on the
        login page, with no page-source: the POST from the final page changes
        nothing, and the login page is a logged-out page."""
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/dashboard", self._with("<div id='dash'></div>")),
            self._form_post("/logout", "_token=abc123"),
            self._get("/login", self._LOGIN),
        ]
        result = self._digest(tmp_path, entries)
        assert result.success_candidates == ("#account-menu", "a.signout-link", "#dash")

    # Review round 4 (#325): a token field, a logout path, and an ordinary name
    # ending in "code" or starting with "remember" make no step.

    @pytest.mark.parametrize(
        "logout",
        [
            "/auth/logout",
            "/sso/logout",
            "/login/logout",
            "/Identity/Account/Logout",
            # Other spellings, a file extension, case, and a segment before the last.
            "/auth/sign_out",
            "/sso/log_out",
            "/auth/logout.php",
            "/auth/signout.aspx",
            "/login/logoff",
            "/sso/signoff",
            "/Auth/Logout/confirm",
        ],
    )
    def test_a_logout_post_to_a_step_word_path_leaves_the_last_page_logged_in(
        self, tmp_path: Path, logout: str
    ) -> None:
        """ASP.NET Identity's sign-out form posts its antiforgery token, whose name
        holds "verification"; the others post to a path holding a step word."""
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/dashboard", self._with("<div id='dash'></div>")),
            self._form_post(logout, "__RequestVerificationToken=CfDJ8abc"),
            self._get("/login", self._LOGIN),
        ]
        result = self._digest(tmp_path, entries)
        assert result.success_candidates == ("#account-menu", "a.signout-link", "#dash")

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            # ASP.NET MVC and Core put the antiforgery token in every form.
            ("/Search", "q=milk&__RequestVerificationToken=CfDJ8abc"),
            ("/Cart/Add", "ProductId=7&__RequestVerificationToken=CfDJ8abc"),
            ("/search", "q=milk&auth_token=abc123"),
            ("/search", "q=milk&csrf_token=abc123"),
            ("/cart/coupon", "coupon_code=SAVE10"),
            ("/delivery", "postal_code=99999"),
            ("/delivery", "zipCode=99999"),
            ("/currency", "currencyCode=EUR"),
            ("/orders/filter", "remember_filters=1&status=open"),
            ("/cart/coupon", "promo_code=SAVE10"),
            # A path segment "code" is no step word; only a field named so is.
            ("/promo/code", "coupon=SAVE10"),
        ],
    )
    def test_an_ordinary_form_from_the_landing_page_leaves_it_logged_in(
        self, tmp_path: Path, path: str, body: str
    ) -> None:
        results = self._with("<div id='results'></div>")
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/dashboard", self._with("<div id='dash'></div>")),
            self._form_post(path, body),
            self._get("/results", results),
        ]
        result = self._digest(tmp_path, entries, results)
        assert result.success_candidates == ("#account-menu", "a.signout-link", "#results")

    def test_a_multipart_step_form_is_a_step(self, tmp_path: Path) -> None:
        """The step word is only in a multipart part's name, which body_params does
        not read and body_values does, and the path holds none."""
        body = '--XyZ\r\nContent-Disposition: form-data; name="otp"\r\n\r\n123456\r\n--XyZ--\r\n'
        dash = self._with("<div id='brand-bar'></div><div id='dash'></div>")
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/session/second", "<html><body><div id='brand-bar'></div></body></html>"),
            self._form_post(
                "/session/second", body, **{"Content-Type": "multipart/form-data; boundary=XyZ"}
            ),
            self._get("/dashboard", dash),
        ]
        result = self._digest(tmp_path, entries)
        assert result.success_candidates == ("#account-menu", "a.signout-link", "#dash")

    def test_a_2fa_settings_page_browsed_after_landing_is_logged_in(self, tmp_path: Path) -> None:
        settings = self._with(
            "<div id='security'></div><input name='otp' autocomplete='one-time-code'>"
        )
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/dashboard", self._with("<div id='dash'></div>")),
            self._get("/security", settings),
            self._get("/orders", self._with("<div id='orders-table'></div>")),
        ]
        result = self._digest(tmp_path, entries)
        assert result.success_candidates == ("#account-menu", "a.signout-link", "#orders-table")

    @pytest.mark.parametrize(
        ("step_path", "step_body", "step_page"),
        [
            ("/mfa", "code=123456", _MFA.replace(" autocomplete='one-time-code'", "")),
            ("/kmsi", "stay=yes", "<html><body><div id='brand-bar'></div></body></html>"),
            ("/consent", "rememberMe=true", "<html><body><div id='brand-bar'></div></body></html>"),
            # "code" as the only step word: a whole field name, on a path with none.
            ("/session/second", "code=123456", _MFA.replace(" autocomplete='one-time-code'", "")),
            # "code" beside a word saying what code it is, and a token-named field
            # naming a one-time code, each the only step word.
            *(
                (
                    "/session/second",
                    f"{name}=123456&authenticity_token=abc",
                    "<html><body><div id='brand-bar'></div></body></html>",
                )
                for name in (
                    "sms_code",
                    "security_code",
                    "one_time_code",
                    "passCode",
                    "tfa_code",
                    "otp_token",
                    "mfa_token",
                )
            ),
            (
                "/session/second",
                "remember_device_30_days=1",
                "<html><body><div id='brand-bar'></div></body></html>",
            ),
        ],
    )
    def test_a_step_then_the_landing_gives_the_landing_s_markers(
        self, tmp_path: Path, step_path: str, step_body: str, step_page: str
    ) -> None:
        """An MFA or "stay signed in" page submitted by a step-shaped form, then the
        landing: the step's #brand-bar, which the landing also shows, is no marker."""
        dash = self._with("<div id='brand-bar'></div><div id='dash'></div>")
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get(step_path, step_page),
            self._form_post(step_path, step_body),
            self._get("/dashboard", dash),
        ]
        result = self._digest(tmp_path, entries)
        assert result.success_candidates == ("#account-menu", "a.signout-link", "#dash")

    def test_a_run_ending_on_an_mfa_page_without_page_source_suggests_nothing(
        self, tmp_path: Path
    ) -> None:
        """The code was posted and answered with a page, not a redirect, so the last
        page loaded is the MFA page, which the browser submitted a step from."""
        dash = self._with("<div id='brand-bar'></div><div id='dash'></div>")
        no_autocomplete = self._MFA.replace(" autocomplete='one-time-code'", "")
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/mfa", no_autocomplete),
            self._form_post("/mfa", "code=123456", status=200, html=dash),
        ]
        assert self._digest(tmp_path, entries).success_candidates == ()
        # With the one-time-code input and no submission at all, too.
        entries = [*entries[:2], self._get("/mfa", self._MFA)]
        assert self._digest(tmp_path / "otc", entries).success_candidates == ()

    def test_a_run_stopped_on_an_mfa_page_suggests_nothing(self, tmp_path: Path) -> None:
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/mfa", self._MFA),
        ]
        assert self._digest(tmp_path, entries, self._MFA).success_candidates == ()

    def test_a_step_form_from_the_only_page_makes_it_a_step(self, tmp_path: Path) -> None:
        """A POST from the final page makes it a step only while the run has not
        ended: here it is the only page after the post."""
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/verify", self._with("<div id='dash'></div>")),
            self._form_post("/verify", "otp=123456", status=200, html="<html></html>"),
        ]
        assert self._digest(tmp_path, entries).success_candidates == ()

    def test_a_page_load_between_a_page_and_a_post_takes_the_post(self, tmp_path: Path) -> None:
        """The run loaded /home, then a page answering 404, then posted a step-shaped
        form from that page: the POST is the 404 page's, so /home stays the landing."""
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/home", self._with("<div id='home'></div>")),
            self._get("/missing", "<html><body>Not found</body></html>", status=404),
            self._form_post("/verify", "code=1"),
            self._get("/account", self._with("<div id='acct'></div>")),
        ]
        result = self._digest(tmp_path, entries)
        assert result.success_candidates == ("#account-menu", "a.signout-link", "#acct")

    def test_a_post_from_an_iframe_makes_no_step(self, tmp_path: Path) -> None:
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/home", self._with("<div id='home'></div>")),
            self._form_post("/verify", "code=1", **{"Sec-Fetch-Dest": "iframe"}),
            self._get("/account", self._with("<div id='acct'></div>")),
        ]
        result = self._digest(tmp_path, entries)
        assert result.success_candidates == ("#account-menu", "a.signout-link", "#acct")

    def test_a_step_is_left_out_of_the_every_page_rank(self, tmp_path: Path) -> None:
        """An MFA step, the landing, then /orders, with names holding no keyword so
        the keyword rank cannot hide the order: what the landing and /orders share
        ranks first, the step not counted among the logged-in pages."""
        landing = (
            "<html><body><div class='site-header'></div><div id='welcome'></div></body></html>"
        )
        orders = (
            "<html><body><div class='site-header'></div><div id='orders-table'></div></body></html>"
        )
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/mfa", "<html><body><div id='step-box'></div></body></html>"),
            self._form_post("/mfa", "code=123456"),
            self._get("/home", landing),
            self._get("/orders", orders),
        ]
        result = self._digest(tmp_path, entries)
        assert result.success_candidates == ("div.site-header", "#orders-table")

    def test_two_browsed_pages_rank_what_every_page_shows_first(self, tmp_path: Path) -> None:
        """#promo is on the second and the final page only; div.site-header is on all
        three, and ranks first though an id would otherwise come before a class."""
        header = "<html><body><div class='site-header'></div>"
        entries = [
            self._get("/login", self._LOGIN),
            self._credential_post(),
            self._get("/home", f"{header}</body></html>"),
            self._get("/deals", f"{header}<div id='promo'></div></body></html>"),
            self._get("/orders", f"{header}<div id='promo'></div></body></html>"),
        ]
        result = self._digest(tmp_path, entries)
        assert result.success_candidates == ("div.site-header", "#promo")

    # Round 3: the account values (_account_values).

    _GREETED = (
        "<html><body><div id='account-menu'></div><div id='greeting-jsmith'></div>"
        "<div class='plan-box'></div></body></html>"
    )
    _KEPT = ("#account-menu", "#greeting-jsmith", "div.plan-box")
    _REJECTED = ("#account-menu", "div.plan-box")

    def _valued_run(self, run: Path, body: str, content_type: str, login: str = ""):  # noqa: ANN202
        post = _entry(
            "POST",
            f"{self._HOST}/session",
            post_data=body,
            request_headers={"Content-Type": content_type},
        )
        entries = [self._get("/login", login or self._LOGIN), post]
        return self._digest(run, entries, page_source=self._GREETED)

    _WORDPRESS_LOGIN = (
        "<html><body><form action='/session' method='post'>"
        "<input type='text' name='log' id='user_login' autocomplete='username'>"
        "<input type='password' name='pwd' id='pw'><button id='go'>Log in</button>"
        "</form></body></html>"
    )

    def test_the_login_form_s_username_field_is_an_account_value(self, tmp_path: Path) -> None:
        """WordPress posts the username as "log". Another field ("account") is an
        identifier, so only the login form's own username field finds it."""
        body = "log=jsmith&pwd=hunter-two&account=personal"
        form = "application/x-www-form-urlencoded"
        result = self._valued_run(tmp_path, body, form, self._WORDPRESS_LOGIN)
        assert result.success_candidates == self._REJECTED
        # With a login form whose username is another field, "log" is no username.
        other = self._valued_run(tmp_path / "other", body, form)
        assert other.success_candidates == self._KEPT

    @pytest.mark.parametrize(
        ("body", "content_type"),
        [
            # An identifier field (session_identity.is_identifier_field). Each case
            # below that names one also posts "account", an identifier, so the
            # every-value fallback is off and the rule itself must find jsmith.
            (json.dumps({"identifier": "jsmith", "password": "x"}), "application/json"),
            # A Rails-style name, read by its last bracketed part.
            (
                "session%5Blogin%5D=jsmith&session%5Bpassword%5D=x&account=personal",
                "application/x-www-form-urlencoded",
            ),
            # Under an identifier name at any depth, or in a list.
            (
                json.dumps({"user": {"id": "jsmith"}, "password": "x", "account": "personal"}),
                "application/json",
            ),
            (
                json.dumps({"username": ["jsmith"], "password": "x", "account": "personal"}),
                "application/json",
            ),
            # Every value of a repeated form field.
            ("username=a&username=jsmith&password=x", "application/x-www-form-urlencoded"),
            # An email inside a longer value of a field that is no identifier.
            (
                json.dumps({"username": "x9q", "note": "Alice <jsmith@example.com>", "pw": "x"}),
                "application/json",
            ),
            # No identifier field: every value but a token's.
            ("uid=jsmith&pwd=x", "application/x-www-form-urlencoded"),
            ("name=jsmith&pass=xyzzy", "application/x-www-form-urlencoded"),
            # An identifier field whose value could not be the username (empty, a
            # submit button's own name, too short, a boolean word) leaves every
            # value an account value.
            ("email=&uid=jsmith&pwd=x", "application/x-www-form-urlencoded"),
            ("uid=jsmith&pwd=x&login=Login", "application/x-www-form-urlencoded"),
            ("uid=jsmith&pwd=x&account=1", "application/x-www-form-urlencoded"),
            ("uid=jsmith&pwd=x&account=true", "application/x-www-form-urlencoded"),
            (json.dumps({"uid": "jsmith", "password": "x", "user": ""}), "application/json"),
        ],
    )
    def test_an_account_value_is_not_suggested(
        self, tmp_path: Path, body: str, content_type: str
    ) -> None:
        assert self._valued_run(tmp_path, body, content_type).success_candidates == self._REJECTED

    @pytest.mark.parametrize(
        ("body", "content_type"),
        [
            # An identifier qualified, so another field's value is no account value.
            (
                json.dumps({"username": "x9q", "plan": "jsmith", "password": "x"}),
                "application/json",
            ),
            # No identifier field, and the only other value is a token's.
            ("pwd=hunter-two&_token=jsmith", "application/x-www-form-urlencoded"),
        ],
    )
    def test_other_values_are_not_account_values(
        self, tmp_path: Path, body: str, content_type: str
    ) -> None:
        assert self._valued_run(tmp_path, body, content_type).success_candidates == self._KEPT

    _PARTS = (
        "--XyZ\r\n"
        'Content-Disposition: form-data; name="username"\r\n\r\n'
        "jsmith\r\n"
        "--XyZ\r\n"
        'Content-Disposition: form-data; name="password"\r\n\r\n'
        "hunter-two\r\n"
        "--XyZ--\r\n"
    )

    @pytest.mark.parametrize(
        ("body", "content_type"),
        [
            (_PARTS, "multipart/form-data; boundary=XyZ"),
            (_PARTS, 'multipart/form-data; boundary="XyZ"'),
            (_PARTS.replace("\r\n", "\n"), "multipart/form-data; boundary=XyZ"),
            # A leading blank line, with the boundary declared and without it.
            ("\r\n" + _PARTS, "multipart/form-data; boundary=XyZ"),
            ("\r\n" + _PARTS, "multipart/form-data"),
            # A preamble line the declared boundary skips.
            ("This is a preamble.\r\n" + _PARTS, "multipart/form-data; boundary=XyZ"),
        ],
    )
    def test_a_multipart_post_s_account_values(
        self, tmp_path: Path, body: str, content_type: str
    ) -> None:
        assert self._valued_run(tmp_path, body, content_type).success_candidates == self._REJECTED

    def test_a_multipart_file_part_is_no_value(self, tmp_path: Path) -> None:
        """No identifier field, so every text value is an account value; the file
        part's content is not one."""
        body = (
            "--XyZ\r\n"
            'Content-Disposition: form-data; name="pwd"\r\n\r\n'
            "hunter-two\r\n"
            "--XyZ\r\n"
            'Content-Disposition: form-data; name="upload"; filename="plan.txt"\r\n'
            "Content-Type: text/plain\r\n\r\n"
            "plan\r\n"
            "--XyZ--\r\n"
        )
        result = self._valued_run(tmp_path, body, "multipart/form-data; boundary=XyZ")
        assert result.success_candidates == self._KEPT

    def test_a_deeply_nested_json_value_is_walked_without_recursion(self) -> None:
        value: object = "jsmith"
        for _ in range(5_000):
            value = {"a": value}
        assert _scalar_values({"username": value}) == [(("username", *["a"] * 5_000), "jsmith")]

    def test_a_body_too_deep_for_the_json_parser_suggests_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """How deep json.loads goes before RecursionError depends on the Python
        version and the platform's stack (3.11 stops near 1,000; 3.14 on Linux read
        100,000), so the parser's refusal is stood in for. The body reads as no
        values, so the guard cannot see the username, and nothing is suggested."""
        body = json.dumps({"username": {"a": "jsmith"}, "password": "x"})
        loads = json.loads

        def too_deep(text: str | bytes, *args: Any, **kwargs: Any) -> object:
            if text == body:
                raise RecursionError("maximum recursion depth exceeded")
            return loads(text, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(json, "loads", too_deep)
            assert body_values(body, "application/json") == []
            result = self._valued_run(tmp_path, body, "application/json")
        assert result.login
        assert result.success_candidates == ()
        # A body the parser may or may not refuse here: either way the digest
        # finishes, and the username is never suggested.
        depth = 100_000
        deep = '{"username": ' + '{"a": ' * depth + '"jsmith"' + "}" * depth + "}"
        real = self._valued_run(tmp_path / "real", deep, "application/json")
        assert "#greeting-jsmith" not in real.success_candidates

    @pytest.mark.parametrize(
        "body",
        [
            "<login><user>jsmith</user><pass>hunter-two</pass></login>",
            '<?xml version="1.0"?><login><user>jsmith</user><pass>hunter-two</pass></login>',
        ],
    )
    def test_a_credential_body_read_by_no_field_name_suggests_nothing(
        self, tmp_path: Path, body: str
    ) -> None:
        """XML: the second body holds an "=", so parse_qs reads a value from it, but
        by no field name, and the username inside it is no account value."""
        result = self._valued_run(tmp_path, body, "application/xml")
        assert result.login
        assert result.success_candidates == ()

