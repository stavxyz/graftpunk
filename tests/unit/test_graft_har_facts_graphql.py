"""The graft skill's ``har-facts.py`` on GraphQL (#367): its copy of the operation
reader matches ``graftpunk.har.graphql``, and ``operations`` prints each operation's
calls, kind, and nested shapes, never a value."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

from graftpunk.har.graphql import document_operation, operations_in
from tests.unit import graphql_har
from tests.unit.guide_harness import REPO_ROOT

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
]


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
        ours = [(o.name, o.batched) for o in operations_in(body)]
        theirs = [(o["name"], o["batched"]) for o in script.operations_in(body)]
        assert theirs == ours, body
        if isinstance(body, dict) and isinstance(body.get("query"), str):
            query = body["query"]
            assert script.document_operation(query) == document_operation(query), body


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
