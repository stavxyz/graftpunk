"""The graft skill's ``har-facts.py`` on GraphQL (#367): its copy of the operation
reader matches ``graftpunk.har.graphql``, and ``operations`` prints each operation's
calls, kind, and nested shapes, never a value."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from graftpunk.har.digest import DigestSource, digest
from graftpunk.har.graphql import document_operation, operations_in
from graftpunk.har.paths import template_path
from graftpunk.har.report import endpoints_projection
from tests.unit import graphql_har
from tests.unit.guide_harness import REPO_ROOT
from tests.unit.test_har_digest import _entry, _write_har

_SCRIPT = REPO_ROOT / "skills" / "graft" / "scripts" / "har-facts.py"

_CORPUS: list[object] = [
    {"query": graphql_har.ORDERS_DOCUMENT},
    {"operationName": "AddToCart", "query": graphql_har.ADD_TO_CART_DOCUMENT},
    {"operationName": "Q40912873", "query": "query Q { a }"},
    {"operationName": "not a name", "query": "query Real { a }"},
    {"query": "red shoes"},
    {"query": "query shoes"},
    {"query": "{ a } }"},
    {"query": '{ a(s: "}") { b } }'},
    {"query": '{ a(s: """ block } """) { b } }'},
    {"query": "# comment {\n{ viewer { id } }"},
    {"query": "fragment F on T { a }\nmutation Save { save { ...F } }"},
    {"query": "subscription OnEvent { event { id } }"},
    {"query": "query A { a } query B { b }", "operationName": "B"},
    {"operationName": "P", "extensions": {"persistedQuery": {"sha256Hash": "a" * 64}}},
    {"operationName": "P", "extensions": {"persistedQuery": "nope"}},
    {"operationName": "P"},
    [{"query": "{ a }"}, {"operationName": "B", "query": "query B { b }"}],
    [{"query": "{ a }"}, {"page": 1}],
    [],
    "text",
    None,
    {"query": json.dumps({"status": "open", "customer": "jane"})},
    {"query": "{}"},
    {"query": "{name:jane}"},
    {"query": "{name:jane}", "variables": {}},
    {"query": "{name:jane}", "extensions": {}},
    {"query": "{name:jane}", "operationName": "Q"},
    {"query": "{brand} shoes"},
    {"query": "{name} {city}"},
    {"query": "query: shoes {size}"},
    {"query": "query Q { a(x: ) }"},
    {"query": "query Q($id: ID = $other) { a }"},
    {"query": "query Q($id: ID = 5 @d(x: 1)) { a(where: {b: [1, {c: $id}]}) @e { ...F } }"},
    {"query": "query Q { ... on T { a } ... @skip(if: true) { b } }"},
    {"query": "fragment on on T { a } query Q { a }"},
    {"query": "query A { a } { b }"},
    {"query": "{" * 200 + "a" + "}" * 200},
]
# Each body is read once as sent to a GraphQL path and once to another.
_PATHS = ("/graphql", "/api/search", "/GraphQLController")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [sys.executable, "-I", str(_SCRIPT), *args], capture_output=True, text=True
    )


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("har_facts_graphql", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_copied_reader_matches_graftpunk_on_a_corpus() -> None:
    script = _load_script()
    for body in _CORPUS:
        for path in _PATHS:
            ours = [(o.name, o.batched) for o in operations_in(body, path=path)]
            theirs = [(o["name"], o["batched"]) for o in script.operations_in(body, path)]
            assert theirs == ours, (body, path)
        if isinstance(body, dict) and isinstance(body.get("query"), str):
            query = body["query"]
            assert script.document_operation(query) == document_operation(query), body


@pytest.mark.parametrize(
    "path",
    [
        "/graphql",
        "/GraphQLController",
        "/api/v2/graphql",
        "/orders/40912873/graphql",
        "/u/jane@myshop.example/gql",
        "/a;jsessionid=abc/graphql",
        "/ab12-cd34-ef56/x",
        "/page/2",
        "/MAPLETON7/x",
        "/",
        "",
        "/trailing/",
    ],
)
def test_the_copied_path_template_matches_the_digest_s(path: str) -> None:
    assert _load_script().template_path(path) == template_path(path)[0]


def test_operations_name_the_path_as_the_digest_keys_it(tmp_path: Path) -> None:
    """A path the print gate would hide (/GraphQLController) is the digest's key,
    so the key har-facts prints is one --command and --match take."""
    body = {"operationName": "AddToCart", "query": graphql_har.ADD_TO_CART_DOCUMENT}
    entries = [
        _entry(
            "POST",
            f"{graphql_har.HOST}/GraphQLController",
            post_data=json.dumps(body),
            body='{"data": {"addToCart": {"ok": true}}}',
        )
    ]
    run = tmp_path / "run"
    run.mkdir()
    har = _write_har(run, entries)
    result = _run("operations", str(har), "--operation", "AddToCart")
    assert result.returncode == 0, result.stderr
    first = result.stdout.splitlines()[0]
    assert first == (
        "POST https://myshop.example.com/GraphQLController (AddToCart): 1 call(s), mutation"
    )
    request = _run("request", str(har), "/GraphQLController", "--operation", "AddToCart")
    assert request.stdout.splitlines()[0] == (
        "POST https://myshop.example.com/GraphQLController (AddToCart)"
    )
    projected = endpoints_projection(digest(DigestSource.from_har(har)))
    (row,) = projected["endpoints"]
    assert f"{row['method']} {row['template']} ({row['operation']})" in first.replace(
        "https://myshop.example.com", ""
    )


def test_operations_lists_calls_kind_and_nested_shapes(tmp_path: Path) -> None:
    har = graphql_har.write(tmp_path / "run")
    result = _run("operations", str(har))
    assert result.returncode == 0, result.stderr
    blocks = result.stdout.split("\n\n")
    orders = next(b for b in blocks if "(OrdersByCustomer)" in b)
    assert orders.splitlines()[0] == (
        "POST https://myshop.example.com/graphql (OrdersByCustomer): 3 call(s), query, 1 in a batch"
    )
    assert "    customerId: string (short)" in orders
    assert "          total: number" in orders
    cart = next(b for b in blocks if "(Cart)" in b)
    assert "            sku: string (short)" in cart
    assert any(b.startswith("POST https://myshop.example.com/graphql (anonymous)") for b in blocks)
    add = next(b for b in blocks if "(AddToCart)" in b)
    assert ": 1 call(s), mutation" in add.splitlines()[0]
    product = next(b for b in blocks if "(ProductPage)" in b)
    assert ": 1 call(s), persisted" in product.splitlines()[0]
    for value in ("4021", graphql_har.EMAIL, graphql_har.PASSWORD, "red-mug", "OPEN"):
        assert value not in result.stdout


def test_shared_values_match_the_digest_and_print_no_value(tmp_path: Path) -> None:
    har = graphql_har.write(tmp_path / "run")
    result = _run("operations", str(har))
    shared = result.stdout.split("shared values (names only):\n", 1)[1].splitlines()
    d = digest(DigestSource.from_har(har))
    assert shared == [
        f"  {s.source_operation} {s.source_path} -> {s.target_operation} "
        f"variables.{s.target_variable}: {s.count} request(s)"
        for s in d.shared_values
    ]
    assert (
        "  LoginMutation data.login.customer.customerId -> OrdersByCustomer "
        "variables.customerId: 1 request(s)"
    ) in shared
    for value in ("4021", "red-mug"):
        assert value not in result.stdout
    narrowed = _run("operations", str(har), "--operation", "ProductPage").stdout
    assert narrowed.endswith(
        "  Cart data.cart.items[].sku -> ProductPage variables.slug: 1 request(s)\n"
    )


def test_operation_narrows_operations_request_and_response(tmp_path: Path) -> None:
    har = graphql_har.write(tmp_path / "run")
    only = _run("operations", str(har), "--operation", "AddToCart")
    assert only.stdout.count("call(s)") == 1
    request = _run("request", str(har), "/graphql", "--operation", "anonymous")
    assert request.returncode == 0, request.stderr
    assert request.stdout.splitlines()[0].endswith("/graphql (anonymous)")
    response = _run("response", str(har), "/graphql", "--operation", "Nope")
    assert response.returncode == 1
    missing = _run("operations", str(har), "--operation", "Nope")
    assert missing.returncode == 1
    assert "no GraphQL operation named 'Nope'" in missing.stderr


def _two_host_entries() -> list[dict]:
    """The synthetic recording, plus a third-party GraphQL host whose response
    holds the cart's sku and whose request sends the customer id."""
    cms = "https://cms.thirdparty.example/graphql"
    return [
        *graphql_har.entries(),
        _entry(
            "POST",
            cms,
            post_data=json.dumps({"query": "query Blocks { blocks { sku } }"}),
            body=json.dumps({"data": {"blocks": [{"sku": "red-mug", "ref": "4021"}]}}),
        ),
        _entry(
            "POST",
            cms,
            post_data=json.dumps(
                {"query": "query Ref($r: ID) { ref(r: $r) { a } }", "variables": {"r": "4021"}}
            ),
            body='{"data": {"ref": {"a": 1}}}',
        ),
    ]


def test_shared_values_read_the_hosts_the_digest_reads(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    har = _write_har(run, _two_host_entries())
    result = _run("operations", str(har))
    assert result.returncode == 0, result.stderr
    assert "cms.thirdparty.example" not in result.stdout
    assert "(Blocks)" not in result.stdout
    shared = result.stdout.split("shared values (names only):\n", 1)[1].splitlines()
    d = digest(DigestSource.from_har(har))
    assert shared == [
        f"  {s.source_operation} {s.source_path} -> {s.target_operation} "
        f"variables.{s.target_variable}: {s.count} request(s)"
        for s in d.shared_values
    ]
    assert not any("Blocks" in line or "Ref " in line for line in shared)


def test_form_state_and_short_values_pair_nothing(tmp_path: Path) -> None:
    response = {"data": {"o": {"status": "OPEN", "currency": "USD", "total": 100}}}
    variables = {"status": "OPEN", "currency": "USD", "limit": 100}
    entries = [
        _entry(
            "POST",
            graphql_har.GRAPHQL_URL,
            post_data=json.dumps({"query": "query A { o { status } }"}),
            body=json.dumps(response),
        ),
        _entry(
            "POST",
            graphql_har.GRAPHQL_URL,
            post_data=json.dumps({"query": "query B { p }", "variables": variables}),
            body='{"data": {"p": 1}}',
        ),
    ]
    run = tmp_path / "run"
    run.mkdir()
    har = _write_har(run, entries)
    result = _run("operations", str(har))
    assert result.returncode == 0, result.stderr
    assert "shared values" not in result.stdout
    assert digest(DigestSource.from_har(har)).shared_values == ()
