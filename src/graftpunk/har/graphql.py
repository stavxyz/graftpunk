"""Which GraphQL operation a request body carries (#367).

The one reader of the rule: the digest keys an endpoint row by it, ``gp observe
fixtures`` names a fixture by it, ``graftpunk.testing.FixtureSession`` looks a
fixture up by it, and the graft skill's ``har-facts.py`` carries a copy held equal
by a test. Standard library only, so the testing layer can import it.

A body is a GraphQL request when its JSON is an object holding a ``query`` string
that reads as a GraphQL document (:func:`document_operation`), or an
``operationName`` string beside ``extensions.persistedQuery`` (a persisted query
sent by hash alone). A JSON array whose every element is one is a batch. A
``query`` string that is not a document (a search API's ``{"query": "red shoes"}``)
is not GraphQL.

The operation is ``operationName`` when it is a GraphQL Name, else the name of the
document's first operation definition, else :data:`ANONYMOUS_OPERATION`. A name
past :data:`MAX_OPERATION_NAME` characters, or one ``holds_an_id`` reads as an id,
is filed as anonymous: names are identifiers fixed in a client's code, the same
for every account, and this is their gate in place of ``printable_name``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from graftpunk.har.paths import holds_an_id

__all__ = [
    "ANONYMOUS_OPERATION",
    "MAX_OPERATION_NAME",
    "GraphQLOperation",
    "document_literals_to_fill",
    "document_operation",
    "filled_document",
    "operation_kind",
    "operations_in",
    "request_operation",
]

ANONYMOUS_OPERATION = "(anonymous)"
"""The key of every operation without a usable name; never a GraphQL Name."""

MAX_OPERATION_NAME = 128

_NAME_RE = re.compile(r"[_A-Za-z][_0-9A-Za-z]*")
_OPERATION_KEYWORDS = frozenset({"query", "mutation", "subscription"})
# The GraphQL lexical grammar (October 2021 spec, section 2.1), less the ignored
# tokens, which the scanner skips: a block string before a string, a float before
# an int, and ``...`` before any other punctuator.
_TOKEN_RE = re.compile(
    r'(?P<block>"""(?:\\"""|[^"]|"(?!""))*""")'
    r'|(?P<string>"(?:[^"\\\n\r]|\\.)*")'
    r"|(?P<number>-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)"
    r"|(?P<name>[_A-Za-z][_0-9A-Za-z]*)"
    r"|(?P<punct>\.\.\.|[!$&()\[\]{}:=@|])"
    r"|(?P<comment>#[^\n\r]*)"
    r"|(?P<ignored>[\s,﻿]+)"
)


@dataclass(frozen=True)
class GraphQLOperation:
    """One operation of a request body: its key, its variables object (None when
    the request sent none, or sent something else), its document (None for a
    persisted query sent by hash alone), the persisted query's hash and version
    when it sent one, and whether it came in a batch."""

    name: str
    variables: Mapping[str, Any] | None
    document: str | None
    persisted_hash: str | None = None
    persisted_version: int | None = None
    batched: bool = False
    # The request's own extensions object (empty when it sent none).
    extensions: Mapping[str, Any] = field(default_factory=dict)


def _tokens(document: str) -> list[tuple[str, str]] | None:
    """*document*'s significant tokens as ``(kind, text)``, comments included, or
    None when it holds a character the GraphQL grammar does not."""
    tokens: list[tuple[str, str]] = []
    at = 0
    while at < len(document):
        match = _TOKEN_RE.match(document, at)
        if match is None:
            return None
        kind = match.lastgroup or ""
        if kind != "ignored":
            tokens.append((kind, match.group()))
        at = match.end()
    return tokens


def _usable_name(name: str) -> bool:
    return (
        len(name) <= MAX_OPERATION_NAME
        and _NAME_RE.fullmatch(name) is not None
        and not holds_an_id(name)
    )


def document_operation(document: str) -> tuple[str, str] | None:
    """``(name, kind)`` of *document*'s first operation definition, or None when
    *document* is not a GraphQL document (one with no selection set among them).
    *kind* is ``query``, ``mutation``, or
    ``subscription`` (``query`` for the ``{ ... }`` shorthand); *name* is the
    operation's Name, or :data:`ANONYMOUS_OPERATION` when it has none or one this
    module's gate files as anonymous (see the module docstring). Fragment
    definitions before it are skipped."""
    tokens = _tokens(document)
    if tokens is None:
        return None
    significant = [token for token in tokens if token[0] != "comment"]
    if not significant:
        return None
    first = significant[0]
    if first != ("punct", "{") and not (
        first[0] == "name" and (first[1] in _OPERATION_KEYWORDS or first[1] == "fragment")
    ):
        return None
    depth = 0
    found: tuple[str, str] | None = None
    for index, (kind, text) in enumerate(significant):
        if kind == "punct" and text == "{":
            if depth == 0 and found is None and _definition_start(significant, index):
                found = (ANONYMOUS_OPERATION, "query")
            depth += 1
        elif kind == "punct" and text == "}":
            depth -= 1
            if depth < 0:
                return None
        elif depth == 0 and found is None and kind == "name" and text in _OPERATION_KEYWORDS:
            following = significant[index + 1] if index + 1 < len(significant) else ("", "")
            name = following[1] if following[0] == "name" else ANONYMOUS_OPERATION
            found = (name if _usable_name(name) else ANONYMOUS_OPERATION, text)
    if depth != 0 or found is None or ("punct", "{") not in significant:
        return None
    return found


def _definition_start(tokens: list[tuple[str, str]], index: int) -> bool:
    """True when the ``{`` at *index* (brace depth 0) starts a definition of its
    own, the query shorthand, rather than the selection set of the definition
    before it: the token before it closed a definition or there is none."""
    return index == 0 or tokens[index - 1] == ("punct", "}")


def operation_kind(document: str | None) -> str:
    """``query``, ``mutation``, or ``subscription`` for *document*, or ``persisted``
    for a request sent by hash alone (None)."""
    if document is None:
        return "persisted"
    read = document_operation(document)
    return "query" if read is None else read[1]


def _operation(body: Mapping[str, Any], *, batched: bool) -> GraphQLOperation | None:
    sent_name = body.get("operationName")
    variables = body.get("variables")
    variables = variables if isinstance(variables, Mapping) else None
    extensions = body.get("extensions")
    extensions = extensions if isinstance(extensions, Mapping) else {}
    query = body.get("query")
    if isinstance(query, str):
        read = document_operation(query)
        if read is None:
            return None
        if isinstance(sent_name, str) and _NAME_RE.fullmatch(sent_name):
            name = sent_name if _usable_name(sent_name) else ANONYMOUS_OPERATION
        else:
            name = read[0]
        return GraphQLOperation(name, variables, query, extensions=extensions, batched=batched)
    persisted = extensions.get("persistedQuery")
    if not isinstance(persisted, Mapping) or not isinstance(sent_name, str):
        return None
    if not _NAME_RE.fullmatch(sent_name):
        return None
    digest = persisted.get("sha256Hash")
    version = persisted.get("version")
    return GraphQLOperation(
        sent_name if _usable_name(sent_name) else ANONYMOUS_OPERATION,
        variables,
        None,
        extensions=extensions,
        persisted_hash=digest if isinstance(digest, str) else None,
        persisted_version=version
        if isinstance(version, int) and not isinstance(version, bool)
        else None,
        batched=batched,
    )


def operations_in(parsed: Any) -> tuple[GraphQLOperation, ...]:
    """Every operation a decoded JSON request body carries, in order: one for a
    GraphQL object, one per element of a batch whose every element is one, and
    none for anything else (an empty array included)."""
    if isinstance(parsed, Mapping):
        found = _operation(parsed, batched=False)
        return () if found is None else (found,)
    if isinstance(parsed, list) and parsed:
        operations = []
        for element in parsed:
            found = _operation(element, batched=True) if isinstance(element, Mapping) else None
            if found is None:
                return ()
            operations.append(found)
        return tuple(operations)
    return ()


def request_operation(parsed: Any) -> GraphQLOperation | None:
    """The one operation a decoded request body carries, or None for a batch or a
    body that is not GraphQL."""
    operations = operations_in(parsed)
    return operations[0] if len(operations) == 1 and not operations[0].batched else None


_FILL = '"GP-FILL"'


def _literal_to_fill(kind: str, text: str) -> bool:
    """True for an inline literal a generated plugin must not carry: any string or
    block string, and a number ``holds_an_id`` reads as an id."""
    return kind in ("string", "block") or (kind == "number" and holds_an_id(text.lstrip("-")))


def document_literals_to_fill(document: str) -> int:
    """How many inline literals :func:`filled_document` replaces in *document*."""
    tokens = _tokens(document) or []
    return sum(1 for kind, text in tokens if _literal_to_fill(kind, text))


def filled_document(document: str) -> str:
    """*document* as a generated plugin writes it: byte for byte when no inline
    literal needs filling (:func:`document_literals_to_fill` is 0), else with each
    such literal replaced by ``"GP-FILL"`` and everything else, comments and
    whitespace included, as recorded."""
    if not document_literals_to_fill(document):
        return document
    pieces: list[str] = []
    at = 0
    while at < len(document):
        match = _TOKEN_RE.match(document, at)
        if match is None:
            pieces.append(document[at:])
            break
        text = match.group()
        pieces.append(_FILL if _literal_to_fill(match.lastgroup or "", text) else text)
        at = match.end()
    return "".join(pieces)
