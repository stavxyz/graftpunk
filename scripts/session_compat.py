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

``write`` is the other half of that check: it has the code under test (the
Python that runs this script, with graftpunk installed) cache the ``tokens``
session, a nodriver session with two cached tokens and header roles, into
``--store``. Without ``--format`` the child runs with
``GRAFTPUNK_SESSION_FORMAT`` unset, so it writes what the code's own default
setting writes (a caller's value is not passed on); ``--format pickle`` or
``--format record`` sets it. It refuses a store that already holds a slot of
that name, and leaves the key file it installed in the store so that ``read``
can use it.

``compare`` asks whether the code under test, with the format setting
unset, reads and writes back every committed fixture exactly as a release does.
It runs the same steps in two children, the release and the code under test,
each with its own scratch store: for each fixture under ``--fixtures``,
``load_session_for_api_from_bytes``, ``load_session_for_api``, and a
write-back (``update_session_cookies`` after adding one cookie), recording the
API sessions' cookies, cookie policy, headers and riders, the sha256 of the
decrypted payload the write-back stored, and the class ``load_session``
returns after it, and the versions of graftpunk, requests, urllib3 and dill
each side ran. The release is installed with the working tree's ``uv.lock``
versions of the dependencies the two share (``--no-pin`` installs the newest),
so only graftpunk differs. It prints each side's versions, then each value
that differs and exits 1, or one ``no differences`` line and exits 0.

``bad-payloads`` asks what each public reader (``load_session``, the three
API loaders, ``copy_session`` and ``update_session_cookies``) does with each
kind of stored value no session can use (an empty or unreadable pickle, a
class that refuses its state, cookies that are not a jar, an invalid record,
and others in ``BAD_PAYLOADS``), on a release and on the code under test. Each
side builds the payloads with its own graftpunk and stores them in its own
scratch store. It prints, per payload and reader, what each side returned or
raised and the warning and error log lines it wrote, marks with ``!`` each
line whose sides differ, and exits 0. Run it after any change to a loader's
error handling, and compare its ``!`` lines with the CHANGELOG's notes.

``generate``, ``read``, ``compare`` and ``bad-payloads`` run the release in a venv made with
``uv venv`` under a temporary directory, never the repository's ``.venv``,
with ``GRAFTPUNK_CONFIG_DIR`` pointed at a store in the run's staging
directory, at ``--store`` or at a scratch store, and the key written to
``<config_dir>/.session_key``, because 1.21.0's ``cache_session`` and
``load_session_for_api`` take no ``key=``. No browser is started: the
selenium case is built with ``requestium.Session.__init__``, which starts no
driver.

What each writes: ``generate`` refuses an ``--out`` that holds files, writes
into a staging directory beside it and renames that to ``--out`` only on
success, so a failed run leaves nothing. ``read`` writes ``--key`` to
``<store>/.session_key`` when the store has no key file, and removes it on
exit; it refuses a store whose key file holds a different key. The release
itself may change the store as any load does (it tightens file modes).

Run it before any change to the stored session format or to
``BrowserSession.__getstate__``, and when a release is added to the fixtures.

Usage:
    python scripts/session_compat.py generate --graftpunk VERSION --out DIR [--key FILE]
    python scripts/session_compat.py write --store DIR --name NAME [--format pickle|record]
    python scripts/session_compat.py read --graftpunk VERSION --store DIR --name NAME [--key FILE]
    python scripts/session_compat.py compare --graftpunk VERSION [--fixtures DIR] [--key FILE]
        [--no-pin]
    python scripts/session_compat.py bad-payloads --graftpunk VERSION [--key FILE] [--no-pin]
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
DEFAULT_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "session_compat"
DEFAULT_KEY = DEFAULT_FIXTURES / "key"
CHILD_PYTHON = "3.12"
# The child's own config directory under --out, removed when it is done.
STORE_DIRNAME = ".store"
# A century, so a committed fixture's metadata.json never expires.
TTL_HOURS = 24 * 365 * 100
READ_FAILED = 3
COMPARE_DIFFERS = 1
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


def _venv(tmp: Path, version: str, constraints: Path | None = None) -> Path:
    venv = tmp / "venv"
    pinned = ["--constraint", str(constraints)] if constraints else []
    steps = (
        ["uv", "venv", "-q", str(venv), "-p", CHILD_PYTHON],
        ["uv", "pip", "install", "-q", "--python", str(venv / "bin" / "python"),
         *pinned, f"graftpunk=={version}"],
    )  # fmt: skip
    for cmd in steps:
        done = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
        if done.returncode != 0:
            raise CompatError(f"{' '.join(cmd)} failed:\n{done.stderr.strip()}")
    return venv / "bin" / "python"


def _child_env(config_dir: Path, home: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    # A minimal environment: no developer settings, no real home directory.
    return {
        **(extra or {}),
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


def run_child(
    python: str,
    args: list[str],
    config_dir: Path,
    cwd: Path,
    extra_env: dict[str, str] | None = None,
    *,
    fixed_hash_seed: bool = False,
) -> int:
    """Run this file's child half under *python*; return its exit status.

    *fixed_hash_seed* runs it with ``PYTHONHASHSEED=0``, so two children
    pickle a set in the same order and their payloads can be compared byte
    for byte.
    """
    home = cwd / "home"
    home.mkdir(exist_ok=True)
    # -I: the child imports graftpunk from its own venv, never from this
    # checkout, and reads no PYTHON* variables. cwd is a temp directory so no
    # .env file is read. -I also ignores PYTHONHASHSEED, so a fixed seed uses
    # -s -P instead (no user site, no script directory on sys.path), and the
    # environment _child_env builds passes no other PYTHON* variable.
    flags = ["-s", "-P"] if fixed_hash_seed else ["-I"]
    env = _child_env(config_dir, home, extra_env)
    if fixed_hash_seed:
        env["PYTHONHASHSEED"] = "0"
    cmd = [python, *flags, str(Path(__file__).resolve()), *args]
    return subprocess.run(cmd, env=env, cwd=cwd, check=False).returncode  # noqa: S603


def generate(version: str, out: Path, key: bytes, *, python: str | None = None) -> None:
    out = out.resolve()  # the child runs in a temp directory
    check_out_dir(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # The child writes into a staging directory beside --out, renamed into
    # place only on success: a failed run leaves nothing, and a rerun works.
    staging = Path(tempfile.mkdtemp(prefix=f".{out.name}.", dir=out.parent))
    try:
        store = staging / STORE_DIRNAME
        with tempfile.TemporaryDirectory(prefix="session-compat-") as tmp:
            child = python or str(_venv(Path(tmp), version))
            _install_key(store, key)
            status = run_child(child, ["_generate", "--out", str(staging)], store, Path(tmp))
            if status != 0:
                raise CompatError(f"generating with graftpunk {version} failed (exit {status})")
        shutil.rmtree(store)
        if out.exists():
            out.rmdir()  # empty, by check_out_dir
        staging.rename(out)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def write(
    store: Path, name: str, fmt: str | None, key: bytes, *, python: str | None = None
) -> None:
    """Cache the ``tokens`` session as *name* in *store*. *fmt* None leaves the
    format setting unset, so the code's own default decides."""
    store = store.resolve()  # the child runs in a temp directory
    if (store / "sessions" / name).exists():
        raise CompatError(f"{store / 'sessions' / name} exists; refusing to replace it")
    _install_key(store, key)
    with tempfile.TemporaryDirectory(prefix="session-compat-") as tmp:
        status = run_child(
            python or sys.executable,
            ["_write", "--name", name],
            store,
            Path(tmp),
            {"GRAFTPUNK_SESSION_FORMAT": fmt} if fmt else None,
        )
    if status != 0:
        raise CompatError(f"writing {name} failed (exit {status})")


def read(version: str, store: Path, name: str, key: bytes, *, python: str | None = None) -> int:
    store = store.resolve()  # the child runs in a temp directory
    if not store.is_dir():
        raise CompatError(f"--store {store} is not a directory")
    key_file = store / ".session_key"
    created_key = not key_file.exists()
    try:
        with tempfile.TemporaryDirectory(prefix="session-compat-") as tmp:
            child = python or str(_venv(Path(tmp), version))
            _install_key(store, key)
            return run_child(child, ["_read", "--name", name], store, Path(tmp))
    finally:
        if created_key:
            key_file.unlink(missing_ok=True)


def _locked_constraints(tmp: Path) -> Path:
    """The working tree's locked dependency versions, as a constraints file."""
    cmd = ["uv", "export", "--frozen", "--no-hashes", "--no-emit-project", "--all-extras",
           "--project", str(REPO_ROOT)]  # fmt: skip
    done = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
    if done.returncode != 0:
        raise CompatError(f"{' '.join(cmd)} failed:\n{done.stderr.strip()}")
    constraints = tmp / "constraints.txt"
    constraints.write_text(done.stdout)
    return constraints


def _run_both(
    version: str,
    child_args: list[str],
    key: bytes,
    *,
    pin: bool,
    release_python: str | None,
    python: str | None,
) -> dict[str, dict[str, Any]]:
    """Run one child command under release *version* and under the code under
    test, each with its own scratch store; return each side's JSON result.

    The command gets ``--out FILE`` appended and writes its result there.
    """
    results: dict[str, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory(prefix="session-compat-") as tmp:
        if release_python is None:
            constraints = _locked_constraints(Path(tmp)) if pin else None
            release_python = str(_venv(Path(tmp), version, constraints))
        children = (("release", release_python), ("tree", python or sys.executable))
        for label, child in children:
            run_dir = Path(tmp) / label
            run_dir.mkdir()
            store, out = run_dir / "store", run_dir / "results.json"
            _install_key(store, key)
            args = [*child_args, "--out", str(out)]
            status = run_child(child, args, store, run_dir, fixed_hash_seed=True)
            if status != 0:
                step = child_args[0].lstrip("_")
                raise CompatError(f"the {step} steps failed under the {label} (exit {status})")
            results[label] = json.loads(out.read_text())
    return results


def compare(
    version: str,
    fixtures: Path,
    key: bytes,
    *,
    pin: bool = True,
    release_python: str | None = None,
    python: str | None = None,
) -> int:
    """Run the compare steps with release *version* and with the code under
    test (*python*, default this interpreter); print what differs.

    With *pin* the release is installed under the working tree's ``uv.lock``
    versions of every dependency the two share, so only graftpunk differs: a
    newer ``requests`` or ``dill`` changes headers and pickled bytes.
    """
    fixtures = fixtures.resolve()  # the children run in temp directories
    if not any(_fixture_dirs(fixtures)):
        raise CompatError(f"--fixtures {fixtures} holds no <version>/ directories")
    results = _run_both(
        version,
        ["_compare", "--fixtures", str(fixtures)],
        key,
        pin=pin,
        release_python=release_python,
        python=python,
    )
    for label, result in results.items():
        versions = ", ".join(f"{n} {v}" for n, v in sorted(result["environment"].items()))
        print(f"{label}: {versions}")
    # graftpunk's own version differs by design; every other library is compared.
    for result in results.values():
        result["environment"].pop("graftpunk")
    lines = differences(results["release"], results["tree"], old_label=version)
    for line in lines:
        print(line)
    if lines:
        print(f"{len(lines)} values differ from graftpunk {version}")
        return COMPARE_DIFFERS
    count = len(results["tree"]["fixtures"])
    print(f"no differences from graftpunk {version}: {count} fixtures")
    return 0


def _flatten(value: Any, path: str = "") -> dict[str, Any]:
    if isinstance(value, dict) and value:
        flat: dict[str, Any] = {}
        for key in value:
            flat |= _flatten(value[key], f"{path}/{key}" if path else str(key))
        return flat
    return {path: value}


def differences(old: dict[str, Any], new: dict[str, Any], *, old_label: str) -> list[str]:
    """One line per value that differs between two compare results."""
    flat_old, flat_new = _flatten(old), _flatten(new)
    missing = object()
    lines = []
    for path in sorted(flat_old.keys() | flat_new.keys()):
        a, b = flat_old.get(path, missing), flat_new.get(path, missing)
        if a != b:
            shown = ["<absent>" if v is missing else json.dumps(v)[:200] for v in (a, b)]
            lines.append(f"{path}: graftpunk {old_label} {shown[0]} | working tree {shown[1]}")
    return lines


def _fixture_dirs(fixtures: Path) -> list[Path]:
    # Release directories only: legacy/ is not a compat case.
    return sorted(p for p in fixtures.iterdir() if p.is_dir() and p.name[:1].isdigit())


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


def child_write(name: str) -> None:
    from graftpunk.cache import cache_session

    with contextlib.redirect_stdout(sys.stderr):
        cache_session(_nodriver(tokens=True), name)


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


_RIDERS = (
    "_gp_cached_tokens",
    "_gp_csrf_tokens",
    "_gp_csrf_origin",
    "_gp_header_roles",
    "_gp_header_role_origins",
    "_gp_session_name",
    "_gp_account_identifier",
)


def _api_dump(api: Any) -> dict[str, Any]:
    return {
        "type": type(api).__qualname__,
        "policy": type(api.cookies._policy).__qualname__,
        "cookies": sorted(
            (_cookie_dict(c) for c in api.cookies),
            key=lambda c: (c["domain"], c["path"], c["name"]),
        ),
        "headers": list(api.headers.items()),
        "riders": {attr: repr(getattr(api, attr, "<absent>")) for attr in _RIDERS},
        "default_role": repr(getattr(api, "gp_default_role", "<absent>")),
    }


def _attempt(step: Any) -> Any:
    try:
        return step()
    except Exception as exc:  # noqa: BLE001 (the exception is the result)
        return {"raised": type(exc).__name__, "message": str(exc)[:200]}


def _put(sessions: Path, version_dir: Path, case: str, name: str) -> bytes:
    """Lay a fixture out as its release stored it, under slot *name*."""
    encrypted = (version_dir / f"{case}.enc").read_bytes()
    metadata_file = version_dir / f"{case}.metadata.json"
    sessions.mkdir(exist_ok=True)
    if not metadata_file.exists():  # the flat-file layout
        _write(sessions, f"{name}.session.pickle", encrypted)
        return encrypted
    slot = sessions / name
    slot.mkdir()
    _write(slot, "session.pickle", encrypted)
    metadata = json.loads(metadata_file.read_text())
    _write(slot, "metadata.json", json.dumps({**metadata, "name": name}).encode())
    return encrypted


def _compare_steps(sessions: Path, key: bytes, name: str, encrypted: bytes) -> dict[str, Any]:
    import hashlib

    from cryptography.fernet import Fernet

    from graftpunk import cache

    def write_back() -> dict[str, Any]:
        api = cache.load_session_for_api(name, resolve=False)
        api.cookies.set("probe", "1", domain="probe.myshop.example", path="/")
        cache.update_session_cookies(api, name)
        slot = sessions / name / "session.pickle"
        stored = slot if slot.exists() else sessions / f"{name}.session.pickle"
        plain = Fernet(key).decrypt(stored.read_bytes())
        return {
            "sha256": hashlib.sha256(plain).hexdigest(),
            "api_after": _api_dump(cache.load_session_for_api(name, resolve=False)),
            "load_session": _attempt(lambda: type(cache.load_session(name)).__qualname__),
        }

    return {
        "from_bytes": _attempt(
            lambda: _api_dump(cache.load_session_for_api_from_bytes(encrypted, key=key))
        ),
        "named": _attempt(lambda: _api_dump(cache.load_session_for_api(name, resolve=False))),
        "write_back": _attempt(write_back),
    }


# The libraries whose version changes what the compare steps record.
_COMPARED_LIBRARIES = ("graftpunk", "requests", "urllib3", "dill")


def child_compare(fixtures: Path, out: Path) -> None:
    import importlib.metadata

    config_dir = Path(os.environ["GRAFTPUNK_CONFIG_DIR"])
    sessions = config_dir / "sessions"
    key = (config_dir / ".session_key").read_bytes().strip()
    results: dict[str, Any] = {}
    for version_dir in _fixture_dirs(fixtures):
        for case in sorted(p.stem for p in version_dir.glob("*.enc")):
            name = f"{version_dir.name.replace('.', '-')}-{case}"
            # The release's own logging goes to stdout by default; keep it apart.
            with contextlib.redirect_stdout(sys.stderr):
                encrypted = _put(sessions, version_dir, case, name)
                results[f"{version_dir.name}/{case}"] = _compare_steps(
                    sessions, key, name, encrypted
                )
    environment = {n: importlib.metadata.version(n) for n in _COMPARED_LIBRARIES}
    report = {"environment": environment, "fixtures": results}
    out.write_text(json.dumps(report, indent=1, sort_keys=True, default=repr))


# --- bad payloads: what each public reader does with a stored value no
# session can use ------------------------------------------------------------

BAD_PAYLOADS = (
    "empty",
    "not_a_pickle",
    "no_cookies",
    "cookies_not_a_jar",
    "class_refuses_state",
    "headers_not_copyable",
    "roles_not_a_mapping",
    "missing_class",
    "missing_module",
    "raises_import_error",
    "raises_graftpunk_error",
    "invalid_record",
    "newer_record",
)
BAD_PAYLOAD_READERS = (
    "load_session",
    "load_session_for_api",
    "load_session_for_api_resolved",
    "load_session_for_api_from_bytes",
    "copy_session",
    "update_session_cookies",
)


class _Restored:
    """Pickles as *cls* holding *state*, so a payload can name a real session
    class with a state no session of it holds."""

    def __init__(self, cls: Any, state: Any) -> None:
        self.cls, self.state = cls, state

    def __reduce_ex__(self, protocol: Any) -> Any:
        import copyreg

        return (copyreg._reconstructor, (self.cls, object, None), self.state)


class _Runs:
    """Pickles as a call of ``exec`` on *source*, which runs while it is read."""

    def __init__(self, source: str) -> None:
        self.source = source

    def __reduce_ex__(self, protocol: Any) -> Any:
        import builtins

        return (builtins.exec, (self.source,))


def _bad_payload(case: str) -> bytes:
    import dill
    import requests

    from graftpunk.session import BrowserSession

    def browser(**over: Any) -> bytes:
        return dill.dumps(_Restored(BrowserSession, {**_nodriver().__getstate__(), **over}))

    builders = {
        "empty": lambda: b"",
        "not_a_pickle": lambda: b"not a pickle",
        "no_cookies": lambda: dill.dumps(__import__("types").SimpleNamespace(headers={})),
        "cookies_not_a_jar": lambda: browser(cookies="not a jar"),
        "class_refuses_state": lambda: browser(headers=5),
        "headers_not_copyable": lambda: dill.dumps(
            _Restored(requests.Session, {**requests.Session().__getstate__(), "headers": 5})
        ),
        "roles_not_a_mapping": lambda: browser(_gp_header_roles=["xhr"]),
        # Protocol 2: GLOBAL, EMPTY_TUPLE, NEWOBJ, STOP.
        "missing_class": lambda: b"\x80\x02cbuiltins\nno_such_class\n)\x81.",
        "missing_module": lambda: b"\x80\x02cno_such_module_for_compat\nThing\n)\x81.",
        "raises_import_error": lambda: dill.dumps(_Runs("raise ImportError('compat probe')")),
        "raises_graftpunk_error": lambda: dill.dumps(
            _Runs("from graftpunk.exceptions import BrowserError\nraise BrowserError('compat')")
        ),
        "invalid_record": lambda: b'{"graftpunk_session": 1, "kind": "browser"}',
        "newer_record": lambda: b'{"graftpunk_session": 2, "kind": "browser"}',
    }
    return builders[case]()


def _put_plain(sessions: Path, key: bytes, name: str, plain: bytes) -> bytes:
    """Store *plain* under *name* with metadata whose checksum matches it."""
    import hashlib

    import requests
    from cryptography.fernet import Fernet

    from graftpunk import cache

    cache.cache_session(requests.Session(), name)
    encrypted = Fernet(key).encrypt(plain)
    slot = sessions / name
    _write(slot, "session.pickle", encrypted)
    metadata = json.loads((slot / "metadata.json").read_text())
    metadata["checksum"] = hashlib.sha256(plain).hexdigest()
    _write(slot, "metadata.json", json.dumps(metadata).encode())
    return encrypted


def _outcome(step: Any) -> dict[str, Any]:
    """What *step* returned or raised, and the log lines a user can see: every
    warning or error, and any event named ``failed...`` at any level."""
    import structlog
    from structlog.testing import capture_logs

    structlog.reset_defaults()
    with capture_logs() as logs:
        try:
            result = step()
            outcome = "returns" if result is None else f"returns {type(result).__name__}"
        except Exception as exc:  # noqa: BLE001 (the exception is the result)
            outcome = f"raises {type(exc).__name__}"
    shown = [
        f"{entry['log_level']} {entry['event']}"
        for entry in logs
        if entry["log_level"] in ("warning", "error", "critical")
        or str(entry["event"]).startswith("failed")
    ]
    return {"outcome": outcome, "logs": shown}


def _bad_payload_steps(name: str, encrypted: bytes, key: bytes) -> dict[str, Any]:
    import requests

    from graftpunk import cache

    return {
        "load_session": lambda: cache.load_session(name),
        "load_session_for_api": lambda: cache.load_session_for_api(name, resolve=False),
        "load_session_for_api_resolved": lambda: cache.load_session_for_api_resolved(
            name, resolve=False
        ),
        "load_session_for_api_from_bytes": lambda: cache.load_session_for_api_from_bytes(
            encrypted, key=key
        ),
        "copy_session": lambda: cache.copy_session(name, f"{name}-copy", account_identifier=None),
        "update_session_cookies": lambda: cache.update_session_cookies(requests.Session(), name),
    }


def child_bad_payloads(out: Path) -> None:
    config_dir = Path(os.environ["GRAFTPUNK_CONFIG_DIR"])
    sessions = config_dir / "sessions"
    key = (config_dir / ".session_key").read_bytes().strip()
    results: dict[str, Any] = {}
    for case in BAD_PAYLOADS:
        name = case.replace("_", "-")
        # The release's own logging goes to stdout by default; keep it apart.
        with contextlib.redirect_stdout(sys.stderr):
            encrypted = _put_plain(sessions, key, name, _bad_payload(case))
            steps = _bad_payload_steps(name, encrypted, key)
            results[case] = {reader: _outcome(steps[reader]) for reader in BAD_PAYLOAD_READERS}
            stored = (sessions / name / "session.pickle").read_bytes()
            results[case]["update_session_cookies"]["store_unchanged"] = stored == encrypted
    out.write_text(json.dumps({"payloads": results}, indent=1, sort_keys=True))


def _shown(result: dict[str, Any]) -> str:
    logs = f" [{', '.join(result['logs'])}]" if result["logs"] else ""
    kept = "" if result.get("store_unchanged", True) else " (store changed)"
    return f"{result['outcome']}{logs}{kept}"


def bad_payloads(
    version: str,
    key: bytes,
    *,
    pin: bool = True,
    release_python: str | None = None,
    python: str | None = None,
) -> int:
    """Read each of :data:`BAD_PAYLOADS` with each public reader, under release
    *version* and under the code under test; print one line per payload and
    reader, marking each one whose outcome or log lines differ. Exits 0: the
    differences are the report, and some are intended."""
    results = _run_both(
        version, ["_bad_payloads"], key, pin=pin, release_python=release_python, python=python
    )
    release, tree = (results[side]["payloads"] for side in ("release", "tree"))
    differ = 0
    for case in BAD_PAYLOADS:
        for reader in BAD_PAYLOAD_READERS:
            old, new = _shown(release[case][reader]), _shown(tree[case][reader])
            mark = "=" if old == new else "!"
            differ += old != new
            print(f"{mark} {case} {reader}: {version}: {old} | working tree: {new}")
    total = len(BAD_PAYLOADS) * len(BAD_PAYLOAD_READERS)
    print(f"{differ} of {total} outcomes differ from graftpunk {version}")
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
    wr = sub.add_parser(
        "write",
        help="cache the tokens session into --store with the code under test "
        "(installs the key at <store>/.session_key and leaves it there)",
    )
    wr.add_argument("--store", required=True, type=Path, metavar="DIR")
    wr.add_argument("--name", required=True)
    wr.add_argument("--format", choices=("pickle", "record"), default=None)
    wr.add_argument("--key", type=Path, default=DEFAULT_KEY, metavar="FILE")
    rd = sub.add_parser(
        "read",
        help="load --name from --store with a release (writes the key to "
        "<store>/.session_key if absent, removed on exit)",
    )
    rd.add_argument("--graftpunk", required=True, metavar="VERSION")
    rd.add_argument("--store", required=True, type=Path, metavar="DIR")
    rd.add_argument("--name", required=True)
    rd.add_argument("--key", type=Path, default=DEFAULT_KEY, metavar="FILE")
    cmp = sub.add_parser(
        "compare",
        help="read and write back every fixture with a release and with the code "
        "under test, setting unset; print what differs (exit 1) or nothing (exit 0)",
    )
    cmp.add_argument("--graftpunk", required=True, metavar="VERSION")
    cmp.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES, metavar="DIR")
    cmp.add_argument("--key", type=Path, default=DEFAULT_KEY, metavar="FILE")
    cmp.add_argument(
        "--no-pin",
        dest="pin",
        action="store_false",
        help="install the release with the newest dependencies instead of uv.lock's",
    )
    bad = sub.add_parser(
        "bad-payloads",
        help="read each kind of unusable stored value with each public reader, with a "
        "release and with the code under test; print every outcome, marking differences",
    )
    bad.add_argument("--graftpunk", required=True, metavar="VERSION")
    bad.add_argument("--key", type=Path, default=DEFAULT_KEY, metavar="FILE")
    bad.add_argument(
        "--no-pin",
        dest="pin",
        action="store_false",
        help="install the release with the newest dependencies instead of uv.lock's",
    )
    # The child half, run by the commands above inside the release's venv.
    child_gen = sub.add_parser("_generate")
    child_gen.add_argument("--out", required=True, type=Path)
    child_wr = sub.add_parser("_write")
    child_wr.add_argument("--name", required=True)
    child_rd = sub.add_parser("_read")
    child_rd.add_argument("--name", required=True)
    child_cmp = sub.add_parser("_compare")
    child_cmp.add_argument("--fixtures", required=True, type=Path)
    child_cmp.add_argument("--out", required=True, type=Path)
    child_bad = sub.add_parser("_bad_payloads")
    child_bad.add_argument("--out", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "generate":
            generate(args.graftpunk, args.out, args.key.read_bytes())
            return 0
        if args.command == "write":
            write(args.store, args.name, args.format, args.key.read_bytes())
            return 0
        if args.command == "read":
            return read(args.graftpunk, args.store, args.name, args.key.read_bytes())
        if args.command == "compare":
            return compare(args.graftpunk, args.fixtures, args.key.read_bytes(), pin=args.pin)
        if args.command == "bad-payloads":
            return bad_payloads(args.graftpunk, args.key.read_bytes(), pin=args.pin)
        if args.command == "_bad_payloads":
            child_bad_payloads(args.out)
            return 0
        if args.command == "_generate":
            child_generate(args.out)
            return 0
        if args.command == "_write":
            child_write(args.name)
            return 0
        if args.command == "_compare":
            child_compare(args.fixtures, args.out)
            return 0
        return child_read(args.name)
    except CompatError as exc:
        print(f"session_compat: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
