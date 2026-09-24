"""RunDigest over synthetic HARs built in tmp_path (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

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
    _parse_body,
    _with_login_flow,
    body_params,
    digest,
    endpoint_template,
    flagged_names_of,
    redacted_names_of,
)
from graftpunk.har.parser import parse_har_file
from graftpunk.har.report import render_endpoints_json, render_json, render_markdown


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
    def test_examples_stop_at_the_cap(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", f"https://api.myshop.example.com/orders/{1000 + i}")
            for i in range(_MAX_ENDPOINT_EXAMPLES + 4)
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (endpoint,) = [e for e in result.endpoints if e.template == "/orders/{order_id}"]
        assert len(endpoint.examples) == _MAX_ENDPOINT_EXAMPLES


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
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/products/red-widget-2000",
                content_type="text/html",
                body="<html></html>",
            )
        ]
        entries += [
            _entry(
                "POST",
                f"https://api.myshop.example.com/products/red-widget-{2000 + i}",
                body='{"id": 1}',
                post_data='{"sku": "widget"}',
            )
            for i in range(1, count)
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        merged = [e for e in result.endpoints if e.template == "/products/{product_id}"]
        assert len(merged) == 1, [e.template for e in result.endpoints]
        assert merged[0].shape is not None
        assert merged[0].body_kind == "json"
        assert "sku" in merged[0].body_params

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
        # The success_url comes from the login's landing, not the edit post's.
        plugin_code = render(
            ScaffoldSpec(
                name="myshop",
                mode="new_project",
                backend="nodriver",
                base_url="https://api.myshop.example.com",
                digest=result,
            )
        )["src/graftpunk_myshop/plugin.py"]
        assert 'success_url="*/customer/account*",' in plugin_code

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
            'value="s"></form><script>document.forms[0].submit()</script>'
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
