"""Which GraphQL operation a request body carries (#367).

The one reader of the rule: the digest keys an endpoint row by it, ``gp observe
fixtures`` names a fixture by it, ``graftpunk.testing.FixtureSession`` looks a
fixture up by it, and the graft skill's ``har-facts.py`` carries a copy held equal
by a test. Standard library only, so the testing layer can import it.

A body is a GraphQL request when its JSON is an object holding a ``query`` string
that parses as an executable GraphQL document (:func:`document_operation`: every
definition an operation or a fragment, every selection set holding at least one
selection), or an ``operationName`` string beside ``extensions.persistedQuery`` (a
persisted query sent by hash alone). A document that is only the ``{ ... }``
shorthand counts without a non-empty ``operationName`` only when the request path
holds ``graphql`` (in any case) or the body also carries a ``variables`` or an
``extensions`` object: ``{name:jane}`` parses as a selection set, and a search API
may send just that. A JSON array whose every element is one is a batch.
``{"query": "red shoes"}``, ``"{brand} shoes"``, ``"{}"`` and a JSON object
serialised into the string are not GraphQL.

The operation is ``operationName`` when it is a GraphQL Name, else the name of the
document's first operation definition, else :data:`ANONYMOUS_OPERATION`. A name
past :data:`MAX_OPERATION_NAME` characters, or one ``holds_an_id`` reads as an id,
is filed as anonymous: names are identifiers fixed in a client's code, the same
for every account, and this is their gate in place of ``printable_name``.

:func:`form_state_value` is the one rule for which recorded value a generated
plugin may carry as source: a variable's default, a constant extension, or a
literal written inline in a document (:func:`filled_document`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from graftpunk.har.paths import holds_an_id

__all__ = [
    "ANONYMOUS_OPERATION",
    "MAX_OPERATION_NAME",
    "DocumentFill",
    "FilledDocument",
    "GraphQLOperation",
    "document_operation",
    "filled_document",
    "form_state_value",
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
# Nesting past this is no document a client sends; the parser stops there rather
# than recurse without bound.
_MAX_NESTING = 128
# Distinct documents remembered: a recording sends a few dozen, each many times.
_PARSE_CACHE = 256


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


# --- form state -------------------------------------------------------------

# The variable, argument and key names whose recorded value a plugin may carry,
# compared in lower case with every character but letters and digits removed
# (page_size, pageSize and PAGE-SIZE are one name).
_PAGING_WORDS = frozenset(
    {"first", "last", "limit", "offset", "page", "pagesize", "perpage", "size", "count"}
    | {"take", "skip"}
)
_SETTING_WORDS = frozenset(
    {"status", "state", "sort", "sortby", "order", "orderby", "direction", "type", "kind"}
    | {"mode", "filter", "format", "locale", "currency", "category"}
)
_MAX_FORM_NUMBER = 1000
# An enum value as GraphQL spells one by convention: capitals and digits in words
# joined by single underscores (OPEN, DESC, CREATED_AT).
_ENUM_TOKEN_RE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*")
_MAX_ENUM_TOKEN = 64
_NOT_NAME_CHARS_RE = re.compile(r"[^0-9a-z]+")


def _name_word(name: str) -> str:
    return _NOT_NAME_CHARS_RE.sub("", name.lower())


def form_state_value(name: str, value: Any) -> bool:
    """True when a recorded *value* sent under *name* is form state a generated
    plugin may carry as source, the same for every account: a bool or null under
    any name; an integer under 1000 (absolute value) under a paging or size word
    (``first``, ``last``, ``limit``, ``offset``, ``page``, ``pageSize``,
    ``perPage``, ``size``, ``count``, ``take``, ``skip``); or a capitalised enum
    token (``OPEN``, ``CREATED_AT``) of at most 64 characters that holds no id under
    a setting word (``status``, ``state``, ``sort``, ``sortBy``, ``order``,
    ``orderBy``, ``direction``, ``type``, ``kind``, ``mode``, ``filter``,
    ``format``, ``locale``, ``currency``, ``category``). Names compare in any case
    and with any separator. A float, an array, an object and every other value
    never pass."""
    if value is None or isinstance(value, bool):
        return True
    word = _name_word(name)
    if isinstance(value, int):
        return word in _PAGING_WORDS and abs(value) < _MAX_FORM_NUMBER
    if isinstance(value, str):
        return (
            word in _SETTING_WORDS
            and len(value) <= _MAX_ENUM_TOKEN
            and _ENUM_TOKEN_RE.fullmatch(value) is not None
            and not holds_an_id(value)
        )
    return False


_ID_NAME_PARTS = frozenset({"id", "ids", "uuid", "guid", "key", "token"})
_NAME_PART_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+")


def _names_an_id(name: str) -> bool:
    """True when *name* says it holds an id: a word of it, split at case changes
    and punctuation, is ``id``, ``uuid``, ``key``, ``token``, and the like."""
    return any(word.lower() in _ID_NAME_PARTS for word in _NAME_PART_RE.findall(name))


# --- the parser ---------------------------------------------------------------


_KEYWORD_VALUES = frozenset({"true", "false", "null"})


class _NotADocumentError(Exception):
    """The text is not an executable GraphQL document."""


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class _Literal:
    """An inline number, string, or enum value: where it is, the name it is read under (the
    argument's, the input object field's, or for a variable's default the
    variable's; a list element takes its list's), the definition holding it, and
    whether it is in a variable's default (*default_of*, the variable) or in
    another place a variable cannot stand (*const*)."""

    start: int
    end: int
    kind: str
    text: str
    name: str
    definition: int
    default_of: str | None
    const: bool


@dataclass(frozen=True)
class _VariableDefinition:
    name: str
    type: str
    # From the end of the type to the end of the default value: the span that
    # removing the default takes out. None without a default.
    default_span: tuple[int, int] | None


@dataclass(frozen=True)
class _Definition:
    kind: str  # query, mutation, subscription, or fragment
    name: str
    shorthand: bool
    start: int
    # Where new variable definitions go, and whether a "(...)" is already there
    # (they go before its ")") or not (a new "(...)" goes there).
    insert_at: int
    has_variables: bool
    variables: tuple[_VariableDefinition, ...]
    spreads: tuple[str, ...]


@dataclass(frozen=True)
class _Document:
    definitions: tuple[_Definition, ...]
    literals: tuple[_Literal, ...]


def _tokens(document: str) -> list[_Token] | None:
    """*document*'s significant tokens, comments left out, or None when it holds a
    character the GraphQL grammar does not."""
    tokens: list[_Token] = []
    at = 0
    while at < len(document):
        match = _TOKEN_RE.match(document, at)
        if match is None:
            return None
        kind = match.lastgroup or ""
        if kind not in ("ignored", "comment"):
            tokens.append(_Token(kind, match.group(), match.start(), match.end()))
        at = match.end()
    return tokens


class _Parser:
    """A recursive-descent reader of the executable document grammar (spec
    section 2): it checks the structure and records where each definition, variable
    definition and inline literal is. It does not validate against a schema."""

    def __init__(self, tokens: list[_Token]) -> None:
        self.tokens = tokens
        self.at = 0
        self.literals: list[_Literal] = []
        self.definition = 0
        self.spreads: list[str] = []
        self.depth = 0

    def peek(self, offset: int = 0) -> _Token | None:
        index = self.at + offset
        return self.tokens[index] if index < len(self.tokens) else None

    def is_punct(self, text: str, offset: int = 0) -> bool:
        token = self.peek(offset)
        return token is not None and token.kind == "punct" and token.text == text

    def is_name(self, offset: int = 0) -> bool:
        token = self.peek(offset)
        return token is not None and token.kind == "name"

    def take(self) -> _Token:
        token = self.peek()
        if token is None:
            raise _NotADocumentError
        self.at += 1
        return token

    def punct(self, text: str) -> _Token:
        if not self.is_punct(text):
            raise _NotADocumentError
        return self.take()

    def name(self) -> _Token:
        if not self.is_name():
            raise _NotADocumentError
        return self.take()

    def nest(self) -> None:
        self.depth += 1
        if self.depth > _MAX_NESTING:
            raise _NotADocumentError

    def document(self) -> _Document:
        definitions: list[_Definition] = []
        while self.peek() is not None:
            self.definition = len(definitions)
            self.spreads = []
            definitions.append(self.definition_())
        operations = [d for d in definitions if d.kind != "fragment"]
        # The spec's Lone Anonymous Operation rule: "{brand} {color}" is no request.
        if not definitions or (len(operations) > 1 and any(not d.name for d in operations)):
            raise _NotADocumentError
        return _Document(tuple(definitions), tuple(self.literals))

    def definition_(self) -> _Definition:
        first = self.peek()
        assert first is not None
        if first.kind == "punct" and first.text == "{":
            self.selection_set()
            return _Definition(
                "query", "", True, first.start, first.start, False, (), tuple(self.spreads)
            )
        if first.kind != "name":
            raise _NotADocumentError
        if first.text == "fragment":
            self.take()
            name = self.name()
            if name.text == "on":
                raise _NotADocumentError
            if self.name().text != "on":
                raise _NotADocumentError
            self.name()
            self.directives(const=False)
            self.selection_set()
            return _Definition(
                "fragment", name.text, False, first.start, name.end, False, (), tuple(self.spreads)
            )
        if first.text not in _OPERATION_KEYWORDS:
            raise _NotADocumentError
        self.take()
        name = self.take() if self.is_name() else None
        insert_at = (name or first).end
        variables: tuple[_VariableDefinition, ...] = ()
        has_variables = self.is_punct("(")
        if has_variables:
            variables, insert_at = self.variable_definitions()
        self.directives(const=False)
        self.selection_set()
        return _Definition(
            first.text,
            "" if name is None else name.text,
            False,
            first.start,
            insert_at,
            has_variables,
            variables,
            tuple(self.spreads),
        )

    def variable_definitions(self) -> tuple[tuple[_VariableDefinition, ...], int]:
        self.punct("(")
        found: list[_VariableDefinition] = []
        while not self.is_punct(")"):
            self.punct("$")
            variable = self.name().text
            self.punct(":")
            type_start = self.at
            self.type_()
            type_end = self.tokens[self.at - 1]
            type_text = "".join(t.text for t in self.tokens[type_start : self.at])
            span = None
            if self.is_punct("="):
                self.take()
                self.value(variable, const=True, default_of=variable)
                span = (type_end.end, self.tokens[self.at - 1].end)
            self.directives(const=True)
            found.append(_VariableDefinition(variable, type_text, span))
        if not found:
            raise _NotADocumentError
        close = self.punct(")")
        return tuple(found), close.start

    def type_(self) -> None:
        if self.is_punct("["):
            self.take()
            self.nest()
            self.type_()
            self.depth -= 1
            self.punct("]")
        else:
            self.name()
        if self.is_punct("!"):
            self.take()

    def directives(self, *, const: bool) -> None:
        while self.is_punct("@"):
            self.take()
            self.name()
            if self.is_punct("("):
                self.arguments(const=const)

    def arguments(self, *, const: bool) -> None:
        self.punct("(")
        count = 0
        while not self.is_punct(")"):
            name = self.name().text
            self.punct(":")
            self.value(name, const=const, default_of=None)
            count += 1
        if not count:
            raise _NotADocumentError
        self.take()

    def selection_set(self) -> None:
        self.punct("{")
        self.nest()
        count = 0
        while not self.is_punct("}"):
            self.selection()
            count += 1
        if not count:
            raise _NotADocumentError
        self.take()
        self.depth -= 1

    def selection(self) -> None:
        if self.is_punct("..."):
            self.take()
            following = self.peek()
            if following is not None and following.kind == "name" and following.text != "on":
                self.spreads.append(self.take().text)
                self.directives(const=False)
                return
            if following is not None and following.kind == "name":  # "on"
                self.take()
                self.name()
            self.directives(const=False)
            self.selection_set()
            return
        self.name()
        if self.is_punct(":"):
            self.take()
            self.name()
        if self.is_punct("("):
            self.arguments(const=False)
        self.directives(const=False)
        if self.is_punct("{"):
            self.selection_set()

    def value(self, name: str, *, const: bool, default_of: str | None) -> None:
        token = self.take()
        if token.kind == "name" and token.text in _KEYWORD_VALUES:
            return
        if token.kind in ("number", "string", "block", "name"):
            # A bare name here is an enum value: recorded text like a string.
            self.literals.append(
                _Literal(
                    token.start,
                    token.end,
                    "enum" if token.kind == "name" else token.kind,
                    token.text,
                    name,
                    self.definition,
                    default_of,
                    const,
                )
            )
            return
        if token.kind != "punct":
            raise _NotADocumentError
        if token.text == "$" and not const:
            self.name()
            return
        if token.text == "[":
            self.nest()
            while not self.is_punct("]"):
                self.value(name, const=const, default_of=default_of)
            self.take()
            self.depth -= 1
            return
        if token.text == "{":
            self.nest()
            while not self.is_punct("}"):
                key = self.name().text
                self.punct(":")
                self.value(key, const=const, default_of=default_of)
            self.take()
            self.depth -= 1
            return
        raise _NotADocumentError


@lru_cache(maxsize=_PARSE_CACHE)
def _parsed(document: str) -> _Document | None:
    """*document* read once per distinct text (a recording sends one document many
    times), or None when it is not an executable GraphQL document."""
    tokens = _tokens(document)
    if not tokens:
        return None
    try:
        return _Parser(tokens).document()
    except (_NotADocumentError, RecursionError):
        return None


def _usable_name(name: str) -> bool:
    return (
        len(name) <= MAX_OPERATION_NAME
        and _NAME_RE.fullmatch(name) is not None
        and not holds_an_id(name)
    )


def document_operation(document: str) -> tuple[str, str] | None:
    """``(name, kind)`` of *document*'s first operation definition, or None when
    *document* is not an executable GraphQL document or holds no operation.
    *kind* is ``query``, ``mutation``, or ``subscription`` (``query`` for the
    ``{ ... }`` shorthand); *name* is the operation's Name, or
    :data:`ANONYMOUS_OPERATION` when it has none or one this module's gate files as
    anonymous (see the module docstring). Fragment definitions before it are
    skipped."""
    parsed = _parsed(document)
    if parsed is None:
        return None
    for definition in parsed.definitions:
        if definition.kind != "fragment":
            name = definition.name if _usable_name(definition.name) else ANONYMOUS_OPERATION
            return name, definition.kind
    return None


def operation_kind(document: str | None) -> str:
    """``query``, ``mutation``, or ``subscription`` for *document*, or ``persisted``
    for a request sent by hash alone (None)."""
    if document is None:
        return "persisted"
    read = document_operation(document)
    return "query" if read is None else read[1]


def _shorthand(document: str) -> bool:
    parsed = _parsed(document)
    return parsed is not None and parsed.definitions[0].shorthand


def _operation(body: Mapping[str, Any], path: str, *, batched: bool) -> GraphQLOperation | None:
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
        named = isinstance(sent_name, str) and bool(sent_name)
        if (
            not named
            and _shorthand(query)
            and "graphql" not in path.casefold()
            and variables is None
            and not isinstance(body.get("extensions"), Mapping)
        ):
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


def operations_in(parsed: Any, *, path: str) -> tuple[GraphQLOperation, ...]:
    """Every operation a decoded JSON request body sent to *path* (the URL's path)
    carries, in order: one for a GraphQL object, one per element of a batch whose
    every element is one, and none for anything else (an empty array included).
    *path* decides a shorthand document sent with no operation name (see the module
    docstring)."""
    if isinstance(parsed, Mapping):
        found = _operation(parsed, path, batched=False)
        return () if found is None else (found,)
    if isinstance(parsed, list) and parsed:
        operations = []
        for element in parsed:
            found = (
                _operation(element, path, batched=True) if isinstance(element, Mapping) else None
            )
            if found is None:
                return ()
            operations.append(found)
        return tuple(operations)
    return ()


def request_operation(parsed: Any, *, path: str) -> GraphQLOperation | None:
    """The one operation a decoded request body sent to *path* carries, or None for
    a batch or a body that is not GraphQL."""
    operations = operations_in(parsed, path=path)
    return operations[0] if len(operations) == 1 and not operations[0].batched else None


# --- the document a plugin carries --------------------------------------------


@dataclass(frozen=True)
class DocumentFill:
    """One inline value of a recorded document that a generated plugin does not
    carry. *variable* is the variable that now stands in its place (added to the
    operation's variable definitions as *type*, a type guessed from the recorded
    literal and the name it was read under), or the variable whose default was
    removed; *name* is the argument, input field or variable name the value was
    read under; *placeholder* is True for a value in a place no variable may stand
    (a directive of a variable definition), replaced with ``"GP-FILL"``."""

    variable: str
    type: str
    name: str
    removed_default: bool = False
    placeholder: bool = False


@dataclass(frozen=True)
class FilledDocument:
    """A recorded document as a plugin writes it, and what was taken out."""

    text: str
    fills: tuple[DocumentFill, ...]

    @property
    def added_variables(self) -> tuple[DocumentFill, ...]:
        """The variables this document gained in place of an inline value."""
        return tuple(f for f in self.fills if not f.removed_default and not f.placeholder)


_PLACEHOLDER = '"GP-FILL"'


def _literal_value(literal: _Literal) -> Any:
    """The recorded value of *literal* as JSON reads it, or a value no rule passes
    (a float, a block string, a string JSON cannot decode)."""
    if literal.kind == "number":
        if re.fullmatch(r"-?\d+", literal.text):
            return int(literal.text)
        return float(literal.text)
    if literal.kind == "enum":
        return literal.text
    if literal.kind == "string":
        try:
            return json.loads(literal.text)
        except ValueError:
            return object()
    return object()


def _kept(literal: _Literal) -> bool:
    return form_state_value(literal.name, _literal_value(literal))


def _guessed_type(literal: _Literal) -> str:
    """The type a variable standing in for *literal* is declared with: ``ID!`` under
    a name that says it holds an id, else the literal's own scalar, non-null so the
    variable fits a nullable and a non-null position alike; ``String!`` for an enum
    value, whose enum type the recording does not name."""
    if _names_an_id(literal.name):
        return "ID!"
    if literal.kind == "number":
        return "Int!" if re.fullmatch(r"-?\d+", literal.text) else "Float!"
    return "String!"


def _fragment_users(document: _Document) -> dict[str, set[int]]:
    """For each fragment name, the operation definitions (by index) that spread it,
    directly or through other fragments."""
    fragments = {d.name: d for d in document.definitions if d.kind == "fragment"}
    users: dict[str, set[int]] = {}
    for index, definition in enumerate(document.definitions):
        if definition.kind == "fragment":
            continue
        pending = list(definition.spreads)
        seen: set[str] = set()
        while pending:
            name = pending.pop()
            if name in seen or name not in fragments:
                continue
            seen.add(name)
            users.setdefault(name, set()).add(index)
            pending.extend(fragments[name].spreads)
    return users


def _variable_name(base: str, taken: set[str]) -> str:
    name = base if _NAME_RE.fullmatch(base) else "value"
    candidate, suffix = name, 2
    while candidate in taken:
        candidate, suffix = f"{name}{suffix}", suffix + 1
    taken.add(candidate)
    return candidate


def filled_document(document: str) -> FilledDocument:
    """*document* as a generated plugin carries it.

    Every inline number, string, and enum literal (``owner: JSMITH``, a recorded
    value like a string) is kept only when it is form state under the name it is
    read under (:func:`form_state_value`): the argument's name
    (``first`` in ``orders(first: 10)``), an input object field's name (``status``
    in ``where: {status: "OPEN"}``), the enclosing name for an element of a list,
    and the variable's name for a variable definition's default. Any other literal
    is taken out, and the document stays valid GraphQL:

    - in a selection (an argument, an operation's or a field's directive), the
      literal is replaced with a new variable, ``$<name>`` (a number added when the
      name is taken), declared on every operation that holds or spreads it, with a
      type guessed from the value (:func:`_guessed_type`) for the author to check;
      the shorthand ``{ ... }`` becomes ``query (...) { ... }`` for it;
    - in a variable's default, the default is removed;
    - in a variable definition's directive, where no variable may stand, it is
      replaced with ``"GP-FILL"``.

    With nothing taken out, the text is *document* byte for byte; else every byte
    but those changes is as recorded, comments included. A document that does not
    parse is returned as it is, with no fills (the digest never keys one)."""
    parsed = _parsed(document)
    if parsed is None:
        return FilledDocument(document, ())
    failing = [literal for literal in parsed.literals if not _kept(literal)]
    if not failing:
        return FilledDocument(document, ())
    taken = {v.name for d in parsed.definitions for v in d.variables}
    users = _fragment_users(parsed)
    edits: list[tuple[int, int, str]] = []
    fills: list[DocumentFill] = []
    added: dict[int, list[str]] = {}
    removed: set[str] = set()
    for literal in failing:
        definition = parsed.definitions[literal.definition]
        if literal.default_of is not None:
            key = f"{literal.definition}:{literal.default_of}"
            if key in removed:
                continue
            removed.add(key)
            variable = next(v for v in definition.variables if v.name == literal.default_of)
            assert variable.default_span is not None
            edits.append((*variable.default_span, ""))
            fills.append(
                DocumentFill(variable.name, variable.type, literal.name, removed_default=True)
            )
            continue
        if literal.const:
            edits.append((literal.start, literal.end, _PLACEHOLDER))
            fills.append(DocumentFill("", "", literal.name, placeholder=True))
            continue
        variable_name = _variable_name(literal.name, taken)
        variable_type = _guessed_type(literal)
        edits.append((literal.start, literal.end, f"${variable_name}"))
        fills.append(DocumentFill(variable_name, variable_type, literal.name))
        owners = (
            users.get(definition.name, set())
            if definition.kind == "fragment"
            else {literal.definition}
        )
        for owner in owners:
            added.setdefault(owner, []).append(f"${variable_name}: {variable_type}")
    for index, declared in added.items():
        definition = parsed.definitions[index]
        joined = ", ".join(declared)
        if definition.shorthand:
            edits.append((definition.start, definition.start, f"query ({joined}) "))
        elif definition.has_variables:
            edits.append((definition.insert_at, definition.insert_at, f", {joined}"))
        else:
            edits.append((definition.insert_at, definition.insert_at, f"({joined})"))
    text = document
    for start, end, replacement in sorted(edits, key=lambda edit: (edit[0], edit[1]), reverse=True):
        text = text[:start] + replacement + text[end:]
    return FilledDocument(text, tuple(fills))
