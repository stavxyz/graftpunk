"""Does GraftpunkSession send what a release sent? Runs a fixed set of request
cases against a ``git archive`` of a ref (the last release tag by default) and
against this checkout, each through a fake requests adapter that records the
request and answers 200 (nothing leaves the process), and prints every
difference in the headers sent: a value, a header added or gone, or the order
of the header names. A difference listed in the allowlist file (a case, the
headers allowed to differ there, and a one-line reason) is reported as allowed.

Run it after any change to src/graftpunk/graftpunk_session.py or
src/graftpunk/observe/headers.py.

Usage: python scripts/header_parity.py [--ref v1.46.0] [--allowed FILE] [--checkout DIR]

Exit 0 when only allowed differences remain, 1 when any other is found, 2 on a
usage error (an unknown ref, an unreadable allowlist).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_REF = "v1.46.0"
DEFAULT_ALLOWED = HERE / "header_parity_allowed.json"
ORDER = "<order>"

# ---------------------------------------------------------------------------
# The cases. Each is (name, call): call(make_session) makes its requests on the
# sessions make_session returns. Run only in --emit mode, with graftpunk
# imported from the tree under test.
# ---------------------------------------------------------------------------

SITE = "https://myshop.example"
OWN = f"{SITE}/api/x"
SUB = "https://cdn.myshop.example/v1/x"
OTHER = "https://api.other.example/v1/x"
CAPTURED = {
    "navigation": {
        "User-Agent": "UA/1",
        "Accept": "text/html",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Upgrade-Insecure-Requests": "1",
    },
    "xhr": {
        "User-Agent": "UA/1",
        "Accept": "application/json",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    },
}
# A navigation role as 1.46.1 captures one over HTTP/2: the names Chrome's
# network stack adds are in lower case.
CAPTURED_HTTP2 = {
    "navigation": {
        "accept": "text/html,application/xhtml+xml",
        "accept-language": "en-US",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "User-Agent": "UA/1",
        "Upgrade-Insecure-Requests": "1",
    }
}
DECLARED = {
    "api": {"Accept": "application/vnd.api+json", "X-Api": "1", "Sec-Fetch-Mode": "cors"},
    "xhr": {"Accept": "application/json", "X-Decl": "1"},
}
DECLARED_SFS = {name: {**role, "Sec-Fetch-Site": "same-site"} for name, role in DECLARED.items()}


def _cases() -> list[tuple[str, Callable[[Callable[..., Any]], None]]]:
    from graftpunk.graftpunk_session import apply_plugin_context

    def plugin(roles: dict[str, dict[str, str]]) -> Any:
        return types.SimpleNamespace(site_name="myshop", base_url=SITE, header_roles=roles)

    cases: list[tuple[str, Callable[[Callable[..., Any]], None]]] = []

    def add(name: str, call: Callable[[Callable[..., Any]], None]) -> None:
        cases.append((name, call))

    for roles_name, roles in (("none", None), ("captured", CAPTURED), ("http2", CAPTURED_HTTP2)):
        for url_name, url in (("own", OWN), ("sub", SUB), ("other", OTHER)):
            tag = f"{roles_name} {url_name}"

            def session(make: Callable[..., Any], roles: Any = roles) -> Any:
                s = make(header_roles={k: dict(v) for k, v in (roles or {}).items()} or None)
                if roles:
                    s._gp_header_role_origins = dict.fromkeys(roles, SITE)
                return s

            add(f"{tag} get", lambda m, u=url, s=session: s(m).get(u))
            add(
                f"{tag} get caller-accept",
                lambda m, u=url, s=session: s(m).get(
                    u, headers={"accept": "text/plain", "X-A": "1"}
                ),
            )
            add(
                f"{tag} get json",
                lambda m, u=url, s=session: s(m).get(u, headers={"Accept": "application/json"}),
            )
            add(
                f"{tag} get json lower",
                lambda m, u=url, s=session: s(m).get(u, headers={"accept": "application/json"}),
            )
            add(f"{tag} post data", lambda m, u=url, s=session: s(m).post(u, data={"a": "1"}))
            add(f"{tag} post json", lambda m, u=url, s=session: s(m).post(u, json={"a": 1}))
            add(f"{tag} delete", lambda m, u=url, s=session: s(m).delete(u))
            add(f"{tag} xhr", lambda m, u=url, s=session: s(m).xhr("GET", u))
            add(
                f"{tag} navigate referer",
                lambda m, u=url, s=session: s(m).navigate("GET", u, referer="/p"),
            )
            add(
                f"{tag} form",
                lambda m, u=url, s=session: s(m).form_submit("POST", u, data={"a": "1"}),
            )
            for role in ("navigation", "xhr", "form", "api", "nope"):
                add(
                    f"{tag} rwr {role}",
                    lambda m, u=url, s=session, r=role: s(m).request_with_role(r, "GET", u),
                )
                add(
                    f"{tag} rwr {role} caller+referer",
                    lambda m, u=url, s=session, r=role: s(m).request_with_role(
                        r, "POST", u, headers={"accept": "text/csv", "X-A": "1"}, referer="/p"
                    ),
                )
                add(
                    f"{tag} rwr {role} caller canonical",
                    lambda m, u=url, s=session, r=role: s(m).request_with_role(
                        r, "GET", u, headers={"Sec-Fetch-Mode": "no-cors", "X-A": "1"}
                    ),
                )

            def user_set(make: Callable[..., Any], u: str = url, s: Any = session) -> None:
                one = s(make)
                one.headers["accept-language"] = "fr-FR"
                one.headers["X-Session"] = "1"
                one.get(u)

            add(f"{tag} user-set get", user_set)

            for default_role in ("api", "xhr", "navigation"):

                def defaulted(
                    make: Callable[..., Any], s: Any = session, d: str = default_role
                ) -> Any:
                    one = s(make)
                    one.gp_default_role = d
                    return one

                add(f"{tag} default {default_role} get", lambda m, u=url, s=defaulted: s(m).get(u))
                add(
                    f"{tag} default {default_role} post data",
                    lambda m, u=url, s=defaulted: s(m).post(u, data={"a": "1"}),
                )
                add(
                    f"{tag} default {default_role} rwr xhr caller+referer",
                    lambda m, u=url, s=defaulted: s(m).request_with_role(
                        "xhr", "POST", u, headers={"accept": "text/csv"}, referer="/p"
                    ),
                )

    for label, declared in (("noSFS", DECLARED), ("SFS", DECLARED_SFS)):
        for cap_name, cap in (("captured", CAPTURED), ("none", None)):
            for override in (True, False):
                mode = "gp-http" if override else "command"

                def declared_session(
                    make: Callable[..., Any],
                    cap: Any = cap,
                    declared: Any = declared,
                    override: bool = override,
                ) -> Any:
                    s = make(header_roles={k: dict(v) for k, v in (cap or {}).items()} or None)
                    if cap:
                        s._gp_header_role_origins = dict.fromkeys(cap, SITE)
                    apply_plugin_context(
                        s,
                        plugin({k: dict(v) for k, v in declared.items()}),
                        declared_roles_override_captured=override,
                    )
                    return s

                tag = f"declared {label} {cap_name} {mode}"
                for url_name, url in (("own", OWN), ("sub", SUB), ("other", OTHER)):
                    for role in ("api", "xhr"):
                        add(
                            f"{tag} rwr {role} {url_name}",
                            lambda m, u=url, s=declared_session, r=role: s(m).request_with_role(
                                r, "GET", u
                            ),
                        )
                        add(
                            f"{tag} rwr {role} {url_name} caller+referer",
                            lambda m, u=url, s=declared_session, r=role: s(m).request_with_role(
                                r, "GET", u, headers={"Accept": "text/csv"}, referer="/p"
                            ),
                        )
                    add(f"{tag} get {url_name}", lambda m, u=url, s=declared_session: s(m).get(u))
    return cases


def _emit() -> dict[str, list[list[Any]]]:
    """Run every case with the graftpunk on sys.path; its requests, by case."""
    import requests

    from graftpunk.graftpunk_session import GraftpunkSession

    sent: list[Any] = []

    class Wire(requests.adapters.BaseAdapter):
        def send(self, request: Any, **_kwargs: Any) -> Any:
            sent.append(request)
            response = requests.Response()
            response.status_code = 200
            response.request = request
            response.url = request.url
            response._content = b"{}"
            return response

        def close(self) -> None:
            pass

    def make(**kwargs: Any) -> Any:
        session = GraftpunkSession(base_url=SITE, **kwargs)
        wire = Wire()
        session.mount("https://", wire)
        session.mount("http://", wire)
        return session

    out: dict[str, list[list[Any]]] = {}
    for name, call in _cases():
        sent.clear()
        call(make)
        out[name] = [[r.method, r.url, list(r.headers.items())] for r in sent]
    return out


# ---------------------------------------------------------------------------
# The comparison: pure, so it is tested without git or a second tree.
# ---------------------------------------------------------------------------


def differences(
    old: dict[str, list[list[Any]]], new: dict[str, list[list[Any]]]
) -> list[tuple[str, str, str]]:
    """Every difference between two runs, as (case, header, what): the header's
    name in lower case (``<order>`` for the order of the names), and a
    description. A request's method or URL changing is reported as ``<request>``."""
    found: list[tuple[str, str, str]] = []
    for case in sorted(set(old) | set(new)):
        before, after = old.get(case), new.get(case)
        if before is None or after is None:
            found.append((case, "<case>", "only in the release" if after is None else "new"))
            continue
        if len(before) != len(after):
            found.append((case, "<request>", f"{len(before)} requests, now {len(after)}"))
            continue
        for (m0, u0, h0), (m1, u1, h1) in zip(before, after, strict=True):
            if (m0, u0) != (m1, u1):
                found.append((case, "<request>", f"{m0} {u0} is now {m1} {u1}"))
                continue
            a = {k.lower(): (k, v) for k, v in h0}
            b = {k.lower(): (k, v) for k, v in h1}
            for name in sorted(set(a) | set(b)):
                if name not in b:
                    found.append((case, name, f"gone (was {a[name][0]}: {a[name][1]})"))
                elif name not in a:
                    found.append((case, name, f"added {b[name][0]}: {b[name][1]}"))
                elif a[name] != b[name]:
                    found.append(
                        (
                            case,
                            name,
                            f"{a[name][0]}: {a[name][1]} is now {b[name][0]}: {b[name][1]}",
                        )
                    )
            if [k.lower() for k, _ in h0 if k.lower() in b] != [
                k.lower() for k, _ in h1 if k.lower() in a
            ]:
                found.append((case, ORDER, f"{[k for k, _ in h0]} is now {[k for k, _ in h1]}"))
    return found


def allowed_reason(case: str, header: str, allowed: list[dict[str, Any]]) -> str | None:
    """The reason the allowlist gives for *header* differing in *case*, or None.
    An entry's ``case`` is an fnmatch pattern; ``headers`` lists lower-case names
    (``<order>`` for the order)."""
    for entry in allowed:
        if fnmatch.fnmatchcase(case, entry["case"]) and header in entry["headers"]:
            return str(entry["reason"])
    return None


def report(
    found: list[tuple[str, str, str]], allowed: list[dict[str, Any]]
) -> tuple[list[str], int]:
    """The lines to print and the exit status for *found* under *allowed*."""
    lines: list[str] = []
    unexpected = 0
    for case, header, what in found:
        reason = allowed_reason(case, header, allowed)
        if reason is None:
            unexpected += 1
            lines.append(f"DIFF     {case} | {header} | {what}")
        else:
            lines.append(f"allowed  {case} | {header} | {what} ({reason})")
    lines.append(f"{len(found)} differences, {len(found) - unexpected} allowed, {unexpected} not")
    return lines, (1 if unexpected else 0)


# ---------------------------------------------------------------------------


def _run_tree(src: Path) -> dict[str, list[list[Any]]]:
    env = {**os.environ, "PYTHONPATH": str(src), "PYTHONHASHSEED": "0"}
    done = subprocess.run(  # noqa: S603 - this interpreter, this script
        [sys.executable, str(Path(__file__).resolve()), "--emit"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(f"the cases failed under {src}:\n{done.stderr}")
    return json.loads(done.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ref", default=DEFAULT_REF, help="the git ref to compare with")
    parser.add_argument("--allowed", type=Path, default=DEFAULT_ALLOWED)
    parser.add_argument("--checkout", type=Path, default=HERE.parent)
    parser.add_argument("--emit", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.emit:
        json.dump(_emit(), sys.stdout)
        return 0
    try:
        allowed = json.loads(args.allowed.read_text())["allowed"]
    except (OSError, ValueError, KeyError) as exc:
        print(f"cannot read the allowlist {args.allowed}: {exc}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "ref.tar"
        command = ["git", "-C", str(args.checkout), "archive", "-o", str(archive), args.ref, "src"]
        made = subprocess.run(  # noqa: S603 - git, with a ref the caller named
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if made.returncode != 0:
            print(f"cannot archive {args.ref}: {made.stderr.strip()}", file=sys.stderr)
            return 2
        with tarfile.open(archive) as tar:
            tar.extractall(Path(tmp) / "ref", filter="data")
        old = _run_tree(Path(tmp) / "ref" / "src")
    new = _run_tree(args.checkout / "src")
    lines, status = report(differences(old, new), allowed)
    print(f"{len(new)} cases, {args.ref} against {args.checkout}")
    print("\n".join(lines))
    return status


if __name__ == "__main__":
    sys.exit(main())
