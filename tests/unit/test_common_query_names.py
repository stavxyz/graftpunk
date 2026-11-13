"""The name gate (graftpunk.har.paths.printable_name, and its copy in the graft
skill's har-facts.py) over a list of common query parameter names, so the claim
that a generated stub still sends them is checkable and pinned (#359).

Since 1.42.2 a query parameter name prints in the digest, --json and
--endpoints-json, and a generated stub sends it, only when the gate passes it. The
list covers paging, search, sort and filter, locale, cache-busters, OAuth and
OpenID Connect, UTM and ad click ids, and common API query names.
"""

from __future__ import annotations

from graftpunk.har.paths import printable_name
from tests.unit.test_har_paths import _load_har_facts

_PAGING = (  # noqa: SIM905 - one compact string on purpose: the list is data
    "page per_page perPage pageSize page_size pageNumber page_number limit offset start "
    "count size cursor after before first last next prev skip take top $top $skip pg "
    "$filter $orderby $select $expand $count"
).split()
_SEARCH_SORT_FILTER = (  # noqa: SIM905
    "q query search term keyword keywords s filter filters sort sortBy sort_by order "
    "orderBy order_by direction dir asc desc fields include exclude expand embed view "
    "type category categoryId status tag tags from to since until date startDate endDate"
).split()
_LOCALE = (  # noqa: SIM905
    "locale lang language hl gl country region currency tz timezone market"
).split()
_CACHE_BUSTERS = (  # noqa: SIM905
    "_ t ts timestamp v ver version cb cacheBuster cache_buster nocache rand r rnd bust _dc"
).split()
_OAUTH_AND_OIDC = (  # noqa: SIM905
    "client_id redirect_uri response_type response_mode scope state nonce code "
    "code_challenge code_challenge_method prompt login_hint max_age ui_locales "
    "id_token_hint post_logout_redirect_uri error error_description access_token "
    "token_type expires_in"
).split()
_UTM_AND_CLICK_IDS = (  # noqa: SIM905
    "utm_source utm_medium utm_campaign utm_term utm_content utm_id gclid fbclid msclkid "
    "dclid ref referrer source campaign affiliate"
).split()
_API = (  # noqa: SIM905
    "id ids format callback jsonp api_key apiKey key fields expand include_deleted "
    "return_url returnUrl ReturnUrl next redirect redirect_to continue lat lng lon "
    "latitude longitude radius zip postalCode storeId store_id locationId sku productId "
    "variant cartId userId accountId output pretty debug objectId AWSAccessKeyId fmt fq "
    "qt df fm ixlib mc_cid mc_eid"
).split()

COMMON_QUERY_NAMES = tuple(
    dict.fromkeys(
        _PAGING
        + _SEARCH_SORT_FILTER
        + _LOCALE
        + _CACHE_BUSTERS
        + _OAUTH_AND_OIDC
        + _UTM_AND_CLICK_IDS
        + _API
    )
)


def test_the_list_is_the_one_measured() -> None:
    assert len(COMMON_QUERY_NAMES) == 176


def test_the_gate_holds_back_none_of_the_common_query_names() -> None:
    assert [name for name in COMMON_QUERY_NAMES if not printable_name(name)] == []


# Held back by the rules until #359's second round, now fixed names.
_FIXED_SINCE_359 = (
    "_",
    "tz",
    "gclid",
    "fbclid",
    "dclid",
    "jsonp",
    "AWSAccessKeyId",
    "_dc",
    "pg",
    "fmt",
    "fq",
    "qt",
    "df",
    "fm",
    "ixlib",
    "mc_cid",
    "mc_eid",
)


def test_the_fixed_names_pass_only_whole() -> None:
    for name in _FIXED_SINCE_359:
        assert printable_name(name)
        assert not printable_name(f"{name}Zq9vKx")


def test_har_facts_reads_each_name_as_the_digest_does() -> None:
    script = _load_har_facts()
    assert [n for n in COMMON_QUERY_NAMES if script.printable_name(n) != printable_name(n)] == []
