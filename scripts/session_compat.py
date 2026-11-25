#!/usr/bin/env python3
"""Write and read saved sessions with a released graftpunk, to check that
stored sessions stay readable across releases.

``generate`` installs a released graftpunk from PyPI into a scratch venv and has
it cache one synthetic session per case (placeholder hosts, a test key), then
copies out what that release stored: ``<case>.enc`` (the encrypted payload),
``<case>.metadata.json`` (the ``metadata.json`` it wrote beside it) and
``<case>.expected.json`` (what the session held when it was saved). The
committed fixtures under ``tests/fixtures/session_compat/<version>/`` are made
this way; ``tests/unit/test_session_compat.py`` loads them with the working
tree's code.

``read`` runs a released graftpunk against a store written by the working tree
(a graftpunk config directory) and reports whether that release can load the
named session for API use. It exits 0 and prints ``cookies: <sorted names>``
and ``tokens: <sorted cached token names>`` when it can, and exits 3 and prints
the exception's class name when the load raises.

Both run the release in a venv made with ``uv venv`` under a temporary
directory, never the repository's ``.venv``, with ``GRAFTPUNK_CONFIG_DIR``
pointed at a directory under ``--out`` (removed afterwards) or at ``--store``,
and the key written to ``<config_dir>/.session_key``, because 1.21.0's
``cache_session`` and ``load_session_for_api`` take no ``key=``. No browser is
started: the selenium case is built with ``requestium.Session.__init__``, which
starts no driver.

Run it before any change to the stored session format or to
``BrowserSession.__getstate__``, and when a release is added to the fixtures.

Usage:
    python scripts/session_compat.py generate --graftpunk VERSION --out DIR [--key FILE]
    python scripts/session_compat.py read --graftpunk VERSION --store DIR --name NAME [--key FILE]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_KEY = REPO_ROOT / "tests" / "fixtures" / "session_compat" / "key"
CHILD_PYTHON = "3.12"
# The child's own config directory under --out, removed when it is done.
STORE_DIRNAME = ".store"
# A century, so a committed fixture's metadata.json never expires.
TTL_HOURS = 24 * 365 * 100
READ_FAILED = 3
PLACEHOLDER_CONFIG_DIR = "~/.config/graftpunk"

CASES = (
    "nodriver",
    "requests",
    "api",
    "tokens",
    "account",
    "adapter",
    "selenium",
    "subclass",
    "flatfile",
)
SUBCLASS_NAME = "CompatSession"
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
HEADERS = (("User-Agent", "compat/1"), ("Accept", "*/*"), ("X-Compat", "1"))


class CompatError(Exception):
    """A refusal or failure, reported as one line and exit status 2."""


# --- host side: runs in the caller's Python, imports no graftpunk -----------


def _inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def check_out_dir(out: Path) -> None:
    """Refuse an --out that already holds files: fixtures describe a frozen
    release and are never rewritten, so a second run goes to a new directory."""
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise CompatError(f"--out {out} exists and is not an empty directory; refusing to write")


def _venv(tmp: Path, version: str) -> Path:
    venv = tmp / "venv"
    steps = (
        ["uv", "venv", "-q", str(venv), "-p", CHILD_PYTHON],
        ["uv", "pip", "install", "-q", "--python", str(venv / "bin" / "python"),
         f"graftpunk=={version}"],
    )  # fmt: skip
    for cmd in steps:
        done = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
        if done.returncode != 0:
            raise CompatError(f"{' '.join(cmd)} failed:\n{done.stderr.strip()}")
    return venv / "bin" / "python"


def _child_env(config_dir: Path, home: Path) -> dict[str, str]:
    # A minimal environment: no developer settings, no real home directory.
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GRAFTPUNK_CONFIG_DIR": str(config_dir),
        "GRAFTPUNK_STORAGE_BACKEND": "local",
        "GRAFTPUNK_SESSION_TTL_HOURS": str(TTL_HOURS),
        "NO_COLOR": "1",
    }


def _install_key(config_dir: Path, key: bytes) -> None:
    key_file = config_dir / ".session_key"
    if key_file.exists():
        if key_file.read_bytes().strip() != key.strip():
            raise CompatError(f"{key_file} holds a different key; refusing to replace it")
        return
    config_dir.mkdir(parents=True, exist_ok=True)
    key_file.write_bytes(key)
    key_file.chmod(0o600)


def run_child(python: str, args: list[str], config_dir: Path, cwd: Path) -> int:
    """Run this file's child half under *python*; return its exit status."""
    home = cwd / "home"
    home.mkdir(exist_ok=True)
    # -I: the child imports graftpunk from its own venv, never from this
    # checkout, and reads no PYTHON* variables. cwd is a temp directory so no
    # .env file is read.
    cmd = [python, "-I", str(Path(__file__).resolve()), *args]
    return subprocess.run(  # noqa: S603
        cmd, env=_child_env(config_dir, home), cwd=cwd, check=False
    ).returncode


def generate(version: str, out: Path, key: bytes, *, python: str | None = None) -> None:
    out = out.resolve()  # the child runs in a temp directory
    check_out_dir(out)
    out.mkdir(parents=True, exist_ok=True)
    store = out / STORE_DIRNAME
    try:
        with tempfile.TemporaryDirectory(prefix="session-compat-") as tmp:
            child = python or str(_venv(Path(tmp), version))
            _install_key(store, key)
            status = run_child(child, ["_generate", "--out", str(out)], store, Path(tmp))
            if status != 0:
                raise CompatError(f"generating with graftpunk {version} failed (exit {status})")
    finally:
        # Only the store this run created; check_out_dir guaranteed it was new.
        if store.exists() and _inside(store, out):
            shutil.rmtree(store)


def read(version: str, store: Path, name: str, key: bytes, *, python: str | None = None) -> int:
    store = store.resolve()  # the child runs in a temp directory
    if not store.is_dir():
        raise CompatError(f"--store {store} is not a directory")
    with tempfile.TemporaryDirectory(prefix="session-compat-") as tmp:
        child = python or str(_venv(Path(tmp), version))
        _install_key(store, key)
        return run_child(child, ["_read", "--name", name], store, Path(tmp))


# --- child side: runs in the release's venv as __main__ ---------------------


def _cookie_dict(cookie: Any) -> dict[str, Any]:
    # http.cookiejar.Cookie keeps its rest argument as _rest.
    return {
        arg: dict(cookie._rest) if arg == "rest" else getattr(cookie, arg) for arg in COOKIE_ARGS
    }


def _expected(session: Any) -> dict[str, Any]:
    """What *session* holds as the pickle will save it: its own __getstate__."""
    state = session.__getstate__()
    tokens = state.get("_gp_cached_tokens") or {}
    cls = type(session)
    return {
        "cookies": sorted((_cookie_dict(c) for c in state["cookies"]), key=lambda c: c["name"]),
        "headers": [[k, v] for k, v in state["headers"].items()],
        "header_roles": state.get("_gp_header_roles") or {},
        "cached_tokens": {
            n: {"name": t.name, "value": t.value, "extracted_at": t.extracted_at, "ttl": t.ttl}
            for n, t in tokens.items()
        },
        "account": state.get("_gp_account_identifier"),
        "class": f"{cls.__module__}.{cls.__qualname__}",
    }


def _jar() -> Any:
    from requests.cookies import RequestsCookieJar, create_cookie

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


def _fill(session: Any) -> Any:
    session.headers.clear()
    session.headers.update(dict(HEADERS))
    for cookie in _jar():
        session.cookies.set_cookie(cookie)
    return session


def _nodriver(*, tokens: bool = False, account: str | None = None) -> Any:
    from graftpunk.session import BrowserSession
    from graftpunk.tokens import CachedToken

    state: dict[str, Any] = {
        "_backend_type": "nodriver",
        "_use_stealth": False,
        "cookies": _jar(),
        "headers": dict(HEADERS),
        "session_name": "myshop",
        "current_url": "https://myshop.example/account",
        "_gp_cached_tokens": {},
        "_gp_header_roles": {},
        "_gp_header_role_origins": {},
        "_gp_account_identifier": account,
    }
    if tokens:
        state["_gp_cached_tokens"] = {
            "csrf": CachedToken("csrf", "compat-csrf", 1790000000.0, 3600.0),
            "bearer": CachedToken("bearer", "compat-bearer", 1790000000.0, 600.0),
        }
        state["_gp_header_roles"] = {
            "xhr": {
                "User-Agent": "compat/1",
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            }
        }
        state["_gp_header_role_origins"] = {"xhr": "https://myshop.example"}
    session = BrowserSession.__new__(BrowserSession)
    session.__setstate__(state)
    # A release whose __setstate__ predates a rider ignores it; set it anyway,
    # and that release's __getstate__ decides whether it is saved.
    for attr in ("_gp_header_role_origins", "_gp_account_identifier"):
        setattr(session, attr, state[attr])
    return session


def _selenium() -> Any:
    import requestium

    from graftpunk.session import BrowserSession

    session = BrowserSession.__new__(BrowserSession)
    requestium.Session.__init__(session)  # starts no driver
    session._backend_type = "selenium"
    session._use_stealth = True
    session._session_name = "myshop"
    return _fill(session)


def _subclass() -> Any:
    from graftpunk.graftpunk_session import GraftpunkSession

    main = sys.modules["__main__"]
    cls = getattr(main, SUBCLASS_NAME, None)
    if cls is None:
        # Module __main__, so dill embeds the class by value: the reader has no
        # module to import it from, as with a program's own subclass. No
        # methods: a function would embed this file's absolute path.
        cls = type(
            SUBCLASS_NAME,
            (GraftpunkSession,),
            {"__module__": "__main__", "__qualname__": SUBCLASS_NAME, "compat_marker": 1},
        )
        setattr(main, SUBCLASS_NAME, cls)
    return _fill(cls())


def _build(case: str) -> tuple[Any, str]:
    import requests
    from requests.adapters import HTTPAdapter

    from graftpunk.graftpunk_session import GraftpunkSession

    if case == "nodriver":
        return _nodriver(), "myshop"
    if case == "tokens":
        return _nodriver(tokens=True), "myshop"
    if case == "account":
        return _nodriver(account="alice"), "myshop@alice"
    if case == "requests":
        return _fill(requests.Session()), "myshop"
    if case == "api":
        return _fill(GraftpunkSession()), "myshop"
    if case == "adapter":
        session = _fill(requests.Session())
        session.mount("https://", HTTPAdapter(max_retries=3))
        return session, "myshop"
    if case == "selenium":
        return _selenium(), "myshop"
    if case == "subclass":
        return _subclass(), "myshop"
    raise CompatError(f"unknown case {case!r}")


def _write(out: Path, name: str, data: bytes) -> None:
    target = out / name
    if not _inside(target, out):
        raise CompatError(f"refusing to write {target} outside {out}")
    target.write_bytes(data)


def child_generate(out: Path) -> None:
    from graftpunk.cache import cache_session

    config_dir = Path(os.environ["GRAFTPUNK_CONFIG_DIR"])
    sessions = config_dir / "sessions"
    for case in CASES:
        if case == "flatfile":
            # No release writes the flat-file layout; it is the nodriver
            # payload as LocalSessionStorage._load_legacy_session reads it.
            for suffix in (".enc", ".expected.json"):
                _write(out, f"flatfile{suffix}", (out / f"nodriver{suffix}").read_bytes())
            continue
        # The release's own logging goes to stdout by default; keep it apart.
        with contextlib.redirect_stdout(sys.stderr):
            session, name = _build(case)
            expected = _expected(session)
            cache_session(session, name)
        slot = sessions / name
        _write(out, f"{case}.enc", (slot / "session.pickle").read_bytes())
        # storage_location is this machine's absolute path; a committed
        # fixture carries the default location in its place.
        metadata = (slot / "metadata.json").read_bytes()
        metadata = metadata.replace(str(config_dir).encode(), PLACEHOLDER_CONFIG_DIR.encode())
        _write(out, f"{case}.metadata.json", metadata)
        _write(out, f"{case}.expected.json", (json.dumps(expected, indent=2) + "\n").encode())
        print(f"{case}: {expected['class']}")


def child_read(name: str) -> int:
    with contextlib.redirect_stdout(sys.stderr):
        try:
            from graftpunk.cache import load_session_for_api

            api = load_session_for_api(name)
        except Exception as exc:  # noqa: BLE001 (the class name is the report)
            failure = type(exc).__name__
        else:
            failure = None
    if failure is not None:
        print(failure)
        return READ_FAILED
    print("cookies: " + " ".join(sorted(c.name for c in api.cookies)))
    print("tokens: " + " ".join(sorted(getattr(api, "_gp_cached_tokens", None) or {})))
    return 0


# --- command line -----------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0], formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate", help="cache every case with a release, into --out")
    gen.add_argument("--graftpunk", required=True, metavar="VERSION")
    gen.add_argument("--out", required=True, type=Path, metavar="DIR")
    gen.add_argument("--key", type=Path, default=DEFAULT_KEY, metavar="FILE")
    rd = sub.add_parser("read", help="load --name from --store with a release")
    rd.add_argument("--graftpunk", required=True, metavar="VERSION")
    rd.add_argument("--store", required=True, type=Path, metavar="DIR")
    rd.add_argument("--name", required=True)
    rd.add_argument("--key", type=Path, default=DEFAULT_KEY, metavar="FILE")
    # The child half, run by the two above inside the release's venv.
    child_gen = sub.add_parser("_generate")
    child_gen.add_argument("--out", required=True, type=Path)
    child_rd = sub.add_parser("_read")
    child_rd.add_argument("--name", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "generate":
            generate(args.graftpunk, args.out, args.key.read_bytes())
            return 0
        if args.command == "read":
            return read(args.graftpunk, args.store, args.name, args.key.read_bytes())
        if args.command == "_generate":
            child_generate(args.out)
            return 0
        return child_read(args.name)
    except CompatError as exc:
        print(f"session_compat: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
