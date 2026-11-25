"""Saved sessions written by released graftpunk versions, and the helpers the
stored-format tests share.

The fixtures under ``tests/fixtures/session_compat/<version>/`` were written by
``scripts/session_compat.py generate``; see the README there. They describe
frozen releases and are never rewritten, so what a case should do today
(its kind, whether it can be stored as a record) lives here, keyed by case
name, and not in the fixture files.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import pickletools
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from requests.cookies import RequestsCookieJar, create_cookie

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "session_compat"
TEST_KEY: bytes = (FIXTURES / "key").read_bytes().strip()
VERSIONS = ("1.21.0", "1.40.1", "1.43.1", "1.44.0")

# The kind each case's saved class maps to; None for a class outside the three.
EXPECTED_KIND: dict[str, str | None] = {
    "nodriver": "browser",
    "requests": "requests",
    "api": "api",
    "tokens": "browser",
    "account": "browser",
    "adapter": "requests",
    "selenium": "browser",
    "subclass": None,
    "flatfile": "browser",
}
# Whether the record codec can store the case: a custom adapter, a selenium
# session (Ruling 1) and a subclass stay pickles.
RECORDABLE: dict[str, bool] = {
    "nodriver": True,
    "requests": True,
    "api": True,
    "tokens": True,
    "account": True,
    "adapter": False,
    "selenium": False,
    "subclass": False,
    "flatfile": True,
}

BROWSER_MODULES = ("selenium", "requestium", "nodriver", "httpie")
COOKIE_ARGS = (
    "version",
    "name",
    "value",
    "port",
    "port_specified",
    "domain",
    "domain_specified",
    "domain_initial_dot",
    "path",
    "path_specified",
    "secure",
    "expires",
    "discard",
    "comment",
    "comment_url",
    "rest",
    "rfc2109",
)
HEADERS: tuple[tuple[str, str], ...] = (
    ("User-Agent", "compat/1"),
    ("Accept", "*/*"),
    ("X-Compat", "1"),
)
TOKENS: dict[str, dict[str, Any]] = {
    "csrf": {"name": "csrf", "value": "compat-csrf", "extracted_at": 1790000000.0, "ttl": 3600.0},
    "bearer": {
        "name": "bearer",
        "value": "compat-bearer",
        "extracted_at": 1790000000.0,
        "ttl": 600.0,
    },
}
HEADER_ROLES: dict[str, dict[str, str]] = {
    "xhr": {
        "User-Agent": "compat/1",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    }
}
HEADER_ROLE_ORIGINS: dict[str, str] = {"xhr": "https://myshop.example"}


@dataclass(frozen=True)
class CompatCase:
    version: str
    case: str
    encrypted: bytes
    key: bytes
    metadata: dict[str, Any] | None
    expected: dict[str, Any]


def _load_case(version: str, case: str) -> CompatCase:
    base = FIXTURES / version
    metadata_file = base / f"{case}.metadata.json"
    return CompatCase(
        version=version,
        case=case,
        encrypted=(base / f"{case}.enc").read_bytes(),
        key=TEST_KEY,
        metadata=json.loads(metadata_file.read_text()) if metadata_file.exists() else None,
        expected=json.loads((base / f"{case}.expected.json").read_text()),
    )


def iter_cases() -> list[CompatCase]:
    """Every case of every version. A missing fixture file raises."""
    return [_load_case(version, case) for version in VERSIONS for case in EXPECTED_KIND]


def cases(
    *, kinds: tuple[str, ...] | None = None, recordable: bool | None = None
) -> list[CompatCase]:
    return [
        c
        for c in iter_cases()
        if (kinds is None or EXPECTED_KIND[c.case] in kinds)
        and (recordable is None or RECORDABLE[c.case] is recordable)
    ]


def case_named(version: str, case: str) -> CompatCase:
    if version not in VERSIONS or case not in EXPECTED_KIND:
        raise KeyError(f"no compat case {version}/{case}")
    return _load_case(version, case)


def case_id(case: CompatCase) -> str:
    return f"{case.version}-{case.case}"


def decrypt(case: CompatCase) -> bytes:
    return Fernet(case.key).decrypt(case.encrypted)


def encrypt(plain: bytes, key: bytes = TEST_KEY) -> bytes:
    return Fernet(key).encrypt(plain)


def _cookie_dict(cookie: Any) -> dict[str, Any]:
    # http.cookiejar.Cookie keeps its rest argument as _rest.
    return {a: dict(cookie._rest) if a == "rest" else getattr(cookie, a) for a in COOKIE_ARGS}


def cookie_dicts(jar: Any) -> list[dict[str, Any]]:
    """Every ``http.cookiejar.Cookie`` constructor argument of each cookie in
    *jar*, sorted by name."""
    return sorted((_cookie_dict(c) for c in jar), key=lambda c: c["name"])


def token_dicts(session: Any) -> dict[str, dict[str, Any]]:
    tokens = getattr(session, "_gp_cached_tokens", None) or {}
    return {
        name: {"name": t.name, "value": t.value, "extracted_at": t.extracted_at, "ttl": t.ttl}
        for name, t in tokens.items()
    }


def _jar() -> RequestsCookieJar:
    jar = RequestsCookieJar()
    jar.set_cookie(
        create_cookie(
            "sid", "compat-sid", domain=".myshop.example", secure=True, expires=1790000000, rest={}
        )
    )
    jar.set_cookie(
        create_cookie(
            "pref", "café", domain=".myshop.example", expires=None, rest={"HttpOnly": None}
        )
    )
    return jar


def nodriver_state(*, tokens: bool = False) -> dict[str, Any]:
    """A dict shaped as a nodriver ``BrowserSession.__getstate__`` returns it,
    with the fixture cookies and headers."""
    from graftpunk.tokens import CachedToken

    return {
        "_backend_type": "nodriver",
        "_use_stealth": False,
        "cookies": _jar(),
        "headers": dict(HEADERS),
        "session_name": "myshop",
        "current_url": "https://myshop.example/account",
        "_gp_cached_tokens": {n: CachedToken(**t) for n, t in TOKENS.items()} if tokens else {},
        "_gp_header_roles": json.loads(json.dumps(HEADER_ROLES)) if tokens else {},
        "_gp_header_role_origins": dict(HEADER_ROLE_ORIGINS) if tokens else {},
        "_gp_account_identifier": None,
    }


def nodriver_session(*, tokens: bool = False) -> Any:
    """A nodriver ``BrowserSession`` restored from :func:`nodriver_state`; no browser."""
    from graftpunk.session import BrowserSession

    session = BrowserSession.__new__(BrowserSession)
    session.__setstate__(nodriver_state(tokens=tokens))
    return session


def _record(kind: str, version: int, account: str | None, cookie_value: str | None) -> dict:
    cookies = cookie_dicts(_jar())
    if cookie_value is not None:
        next(c for c in cookies if c["name"] == "sid")["value"] = cookie_value
    record: dict[str, Any] = {"graftpunk_session": version, "kind": kind}
    if kind == "browser":
        record |= {
            "backend_type": "nodriver",
            "use_stealth": False,
            "session_name": "myshop",
            "current_url": "https://myshop.example/account",
        }
    record |= {"headers": [list(h) for h in HEADERS], "cookies": cookies}
    if kind == "browser":
        record["riders"] = {
            "cached_tokens": json.loads(json.dumps(TOKENS)),
            "header_roles": json.loads(json.dumps(HEADER_ROLES)),
            "header_role_origins": dict(HEADER_ROLE_ORIGINS),
            "account": account,
        }
    else:
        record["auth"] = None
        record["requests"] = {
            "params": {},
            "verify": True,
            "proxies": {},
            "cert": None,
            "max_redirects": 30,
            "trust_env": True,
            "stream": False,
        }
    return record


def record_bytes(
    kind: str = "browser",
    *,
    version: int = 1,
    extra: dict | None = None,
    drop: tuple[str, ...] = (),
    cookie_value: str | None = None,
    account: str | None = None,
) -> bytes:
    """A stored session record built by hand, independent of the codec.

    A ``browser`` record holds a nodriver session with the fixture cookies,
    headers, two cached tokens and header roles. *extra* adds top-level keys,
    *drop* removes them, and *cookie_value* replaces the ``sid`` cookie's value.
    """
    record = _record(kind, version, account, cookie_value)
    record |= extra or {}
    for key in drop:
        record.pop(key, None)
    return json.dumps(record).encode()


def cookies_requests_builds() -> list[Any]:
    """Cookies as requests' own builders make them: ``morsel_to_cookie`` (what
    ``RequestsCookieJar.set`` does with a ``Morsel``) gives a bool
    ``comment_url`` and a bool or empty-string ``HttpOnly`` in ``rest``;
    ``create_cookie`` and ``RequestsCookieJar.set`` give ``{"HttpOnly": None}``."""
    from http.cookies import SimpleCookie

    from requests.cookies import create_cookie, morsel_to_cookie

    full = SimpleCookie()
    full.load(
        "m-full=9; Version=1; Comment=hello; HttpOnly; Secure; Path=/; "
        "Domain=.myshop.example; Max-Age=60"
    )
    bare = SimpleCookie()
    bare.load("m-bare=1")
    jar = RequestsCookieJar()
    jar.set("jar-set", "2", domain=".myshop.example")
    return [
        morsel_to_cookie(full["m-full"]),
        morsel_to_cookie(bare["m-bare"]),
        create_cookie("created", "3", domain=".myshop.example"),
        *jar,
    ]


def nested(depth: int) -> dict:
    """A JSON object *depth* objects deep, built without recursion."""
    node: dict = {}
    for _ in range(depth - 1):
        node = {"a": node}
    return node


def records_json_could_not_have_written() -> dict[str, bytes]:
    """Hand-built records that ``json.loads`` parses and graftpunk's writer
    never produces: a non-finite number, or nesting deeper than a reader walks."""

    def api(params: dict) -> bytes:
        record = json.loads(record_bytes("api"))
        record["requests"]["params"] = params
        return json.dumps(record).encode()

    return {
        "nan-top-level": record_bytes(extra={"x": float("nan")}),
        "infinity-in-params": api({"a": float("inf")}),
        "minus-infinity-in-params": api({"a": float("-inf")}),
        "deep-params": api(nested(600)),
        "deep-top-level": record_bytes(extra={"x": nested(600)}),
    }


def _graftpunk_imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names if a.name.split(".")[0] == "graftpunk"}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise AssertionError(f"relative import at line {node.lineno}")
            if node.module == "graftpunk":
                names |= {f"graftpunk.{a.name}" for a in node.names}
            elif node.module and node.module.startswith("graftpunk."):
                names.add(node.module)
    return names


def assert_leaf_imports(module_name: str, allowed: set[str]) -> None:
    """Assert *module_name* imports from graftpunk only modules in *allowed*,
    and that importing it in a fresh interpreter loads no browser module."""
    spec = importlib.util.find_spec(module_name)
    assert spec is not None and spec.origin, f"{module_name} not found"
    found = _graftpunk_imports(ast.parse(Path(spec.origin).read_text(), spec.origin))
    assert found <= allowed, f"{module_name} imports {sorted(found - allowed)}"
    probe = (
        f"import sys, {module_name}\n"
        f"print(' '.join(m for m in {BROWSER_MODULES!r} if m in sys.modules))"
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert done.stdout.strip() == "", f"importing {module_name} loads {done.stdout.strip()}"


_STRING_OPS = frozenset(
    {"SHORT_BINUNICODE", "BINUNICODE", "BINUNICODE8", "UNICODE"}
    | {"SHORT_BINSTRING", "BINSTRING", "STRING"}
)


def pickled_global_names(payload: bytes) -> set[str]:
    """Each global *payload* names, as ``"module name"``.

    Runs the pickle's opcodes over a value stack and memo table without
    executing anything: strings are kept, every other value is a placeholder.
    """
    mark = object()
    stack: list[Any] = []
    memo: dict[int, Any] = {}
    names: set[str] = set()
    for op, arg, _pos in pickletools.genops(payload):
        if op.name in _STRING_OPS:
            stack.append(arg)
        elif op.name == "MEMOIZE":
            memo[len(memo)] = stack[-1]
        elif op.name in ("PUT", "BINPUT", "LONG_BINPUT"):
            memo[arg] = stack[-1]
        elif op.name in ("GET", "BINGET", "LONG_BINGET"):
            stack.append(memo[arg])
        elif op.name == "STACK_GLOBAL":
            name, module = stack.pop(), stack.pop()
            names.add(f"{module} {name}")
            stack.append(None)
        elif op.name in ("GLOBAL", "INST"):
            names.add(arg)  # genops gives "module name"
            if op.name == "INST":
                while stack.pop() is not mark:
                    pass
            stack.append(None)
        elif op.name == "MARK":
            stack.append(mark)
        else:
            before = op.stack_before
            if pickletools.markobject in before:
                while stack.pop() is not mark:
                    pass
                below = before.index(pickletools.markobject)
            else:
                below = len(before)
            del stack[len(stack) - below :]
            stack.extend(None for _ in op.stack_after)
    return names


def events(captured_logs: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """The entries of the ``captured_logs`` fixture whose event is *name*."""
    return [entry for entry in captured_logs if entry.get("event") == name]
