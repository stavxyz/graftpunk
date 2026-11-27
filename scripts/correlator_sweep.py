"""Does the ExtraInfo correlator give each request its own sent headers in every
event order? Replays every order of the CDP events of several two-hop request
chains (a form POST and its redirect GET on HTTP/1.1 and HTTP/2, a POST with an
empty body, 307 and 308 POST pairs, XHR GETs naming a Content-Type, with each
chain's request ExtraInfo dropped in turn) through SeleniumCaptureBackend, in a
``git archive`` of each named ref and in this checkout, and compares: for each
order, which entry holds whose sent headers, and the header roles, read
mid-capture and after the drain. Nothing leaves the process.

Usage: python scripts/correlator_sweep.py --ref REF [--ref REF ...] [--checkout DIR]

It prints, per chain and ref, how many reads the checkout does worse in (more
entries holding another request's sent headers, or, where the ref attributed
every entry rightly, a role lost or a role missing a header the ref's had), how
many it has fewer entries holding another request's headers in (a read can go
from two such entries to one, so this is not the difference of the two ``wrong
in`` counts, which count reads with any), and how many it builds a role in that
the ref did not build (every read, against a ref that builds no role from sent
headers), with examples of the worse ones. A role the ref built from another
request's headers and the checkout does not have is counted apart, as
``dropped a misattributed role``, not as worse.
Exit 0 when no order is worse than in any ref, 1 when one is, 2 on a usage
error (a ref git cannot archive).
"""

from __future__ import annotations

import argparse
import collections
import copy
import itertools
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
H = "http://127.0.0.1:9"

# ---------------------------------------------------------------------------
# The chains and their orders, run only in --emit mode with graftpunk imported
# from the tree under test.
# ---------------------------------------------------------------------------


def _hdr(
    method: str,
    path: str,
    *,
    cl: int | None = None,
    ct: str | None = None,
    h2: bool = False,
    mode: str = "navigate",
    dest: str = "document",
) -> dict[str, str]:
    h = {
        "Accept": "text/html",
        "User-Agent": "UA",
        "Sec-Fetch-Mode": mode,
        "Sec-Fetch-Dest": dest,
        "Sec-Fetch-Site": "same-origin",
        "X-Which": f"{method} {path}",
    }
    if ct:
        h["Content-Type"] = ct
    if cl is not None:
        h["Content-Length"] = str(cl)
    if method == "POST":
        h["Origin"] = H
    if h2:
        h = {k.lower(): v for k, v in h.items()}
        h.update({":method": method, ":path": path, ":authority": "127.0.0.1:9", ":scheme": "http"})
    else:
        h["Host"] = "127.0.0.1:9"
    return h


def _rwbs(
    rid: str, method: str, path: str, *, post: bool | None, typ: str, redirect: int | None = None
) -> tuple[str, dict[str, Any]]:
    request: dict[str, Any] = {"url": H + path, "method": method, "headers": {}}
    if post is not None:
        request["hasPostData"] = post
    params: dict[str, Any] = {"requestId": rid, "request": request, "type": typ}
    if redirect:
        params["redirectResponse"] = {
            "status": redirect,
            "headers": {"Location": path},
            "mimeType": "",
        }
        params["redirectHasExtraInfo"] = True
    return ("Network.requestWillBeSent", params)


def _chain(
    first: tuple[str, str, dict[str, str], bool | None],
    status: int,
    second: tuple[str, str, dict[str, str], bool | None],
    typ: str = "Document",
    mime: str = "text/html",
) -> dict[str, tuple[str, dict[str, Any]]]:
    """A=announce 1, B=sent 1, C=response 1, S=announce 2, T=sent 2, U=response 2,
    V=response received."""
    m1, p1, h1, post1 = first
    m2, p2, h2, post2 = second
    return {
        "A": _rwbs("1", m1, p1, post=post1, typ=typ),
        "B": ("Network.requestWillBeSentExtraInfo", {"requestId": "1", "headers": h1}),
        "C": (
            "Network.responseReceivedExtraInfo",
            {"requestId": "1", "headers": {"Location": p2}, "statusCode": status},
        ),
        "S": _rwbs("1", m2, p2, post=post2, typ=typ, redirect=status),
        "T": ("Network.requestWillBeSentExtraInfo", {"requestId": "1", "headers": h2}),
        "U": (
            "Network.responseReceivedExtraInfo",
            {"requestId": "1", "headers": {}, "statusCode": 200},
        ),
        "V": (
            "Network.responseReceived",
            {
                "requestId": "1",
                "response": {"url": H + p2, "status": 200, "headers": {}, "mimeType": mime},
            },
        ),
    }


FORM = "application/x-www-form-urlencoded"
JSON = "application/json"
CHAINS = {
    "form POST 302 GET h1": _chain(
        ("POST", "/login", _hdr("POST", "/login", cl=27, ct=FORM), True),
        302,
        ("GET", "/home", _hdr("GET", "/home"), None),
    ),
    "form POST 302 GET h2": _chain(
        ("POST", "/login", _hdr("POST", "/login", cl=27, ct=FORM, h2=True), True),
        302,
        ("GET", "/home", _hdr("GET", "/home", h2=True), None),
    ),
    "POST CL0 with post data 302 GET h1": _chain(
        ("POST", "/login", _hdr("POST", "/login", cl=0, ct=FORM), True),
        302,
        ("GET", "/home", _hdr("GET", "/home"), None),
    ),
    "POST CL0 no post data 302 GET h1": _chain(
        ("POST", "/login", _hdr("POST", "/login", cl=0), None),
        302,
        ("GET", "/home", _hdr("GET", "/home"), None),
    ),
    "307 POST POST h1": _chain(
        ("POST", "/a", _hdr("POST", "/a", cl=10, ct=FORM), True),
        307,
        ("POST", "/b", _hdr("POST", "/b", cl=10, ct=FORM), True),
    ),
    "308 POST POST h1": _chain(
        ("POST", "/a", _hdr("POST", "/a", cl=10, ct=FORM), True),
        308,
        ("POST", "/b", _hdr("POST", "/b", cl=10, ct=FORM), True),
    ),
    "GET xhr CT 302 GET xhr CT h1": _chain(
        ("GET", "/api", _hdr("GET", "/api", ct=JSON, mode="cors", dest="empty"), None),
        302,
        ("GET", "/api2", _hdr("GET", "/api2", ct=JSON, mode="cors", dest="empty"), None),
        typ="XHR",
        mime=JSON,
    ),
    "POST xhr json 302 GET xhr CT h1": _chain(
        ("POST", "/api", _hdr("POST", "/api", cl=9, ct=JSON, mode="cors", dest="empty"), True),
        302,
        ("GET", "/api2", _hdr("GET", "/api2", ct=JSON, mode="cors", dest="empty"), None),
        typ="XHR",
        mime=JSON,
    ),
}


def _orders() -> list[tuple[str, list[str]]]:
    out = []
    for name in CHAINS:
        for drop in (None, "B", "T"):
            keys = [k for k in "ABCSTUV" if k != drop]
            for perm in itertools.permutations(keys):
                s = "".join(perm)
                # The announcements and the final response keep their order; a
                # request's sent headers may come at most one event before it.
                if not (s.index("A") < s.index("S") < s.index("V")):
                    continue
                if "B" in s and s.index("B") < s.index("A") - 1:
                    continue
                out.append((f"{name} | drop={drop} {s}", list(perm)))
    return out


def _emit() -> dict[str, Any]:
    from graftpunk.observe import capture as cap
    from graftpunk.observe.headers import extract_header_roles

    class Driver:
        def __init__(self, events: list[tuple[str, dict[str, Any]]]) -> None:
            self.events = events

        def get_log(self, kind: str) -> list[dict[str, str]]:
            if kind != "performance":
                return []
            return [
                {"message": json.dumps({"message": {"method": m, "params": p}})}
                for m, p in self.events
            ]

        def execute_cdp_cmd(self, *_args: Any) -> Any:
            raise RuntimeError("no browser")

    def snapshot(request_map: dict[str, Any]) -> dict[str, Any]:
        entries = []
        for data in request_map.values():
            got = next(
                (
                    v
                    for k, v in (data.get("_request_extra_headers") or {}).items()
                    if k.lower() == "x-which"
                ),
                None,
            )
            entries.append(f"{data['method']} {data['url'][len(H) :]}<-{got}")
        roles = {r: sorted(v) for r, v in extract_header_roles(request_map, H).items()}
        return {"entries": sorted(entries), "roles": roles}

    out: dict[str, Any] = {}
    for key, order in _orders():
        events = [CHAINS[key.split(" | ")[0]][k] for k in order]
        backend = cap.SeleniumCaptureBackend(
            Driver(events), record_request_bodies=False, record_console=False
        )
        backend.start_capture()
        drain = backend._extra_info.log_unmatched
        backend._extra_info.log_unmatched = lambda _map: None
        backend.stop_capture()
        correlator = backend._extra_info
        if hasattr(correlator, "tentative_view"):
            view = correlator.tentative_view(backend._request_map)
        else:
            view = copy.deepcopy(backend._request_map)
            if hasattr(correlator, "attach_buffered"):
                copy.deepcopy(correlator).attach_buffered(view)
        mid = snapshot(view)
        drain(backend._request_map)
        out[key] = {"mid": mid, "final": snapshot(backend._request_map)}
    return out


# ---------------------------------------------------------------------------
# The comparison.
# ---------------------------------------------------------------------------


def wrong(snap: dict[str, Any]) -> int:
    """How many entries of *snap* hold another request's sent headers."""
    count = 0
    for entry in snap["entries"]:
        own, got = entry.split("<-")
        if got != "None" and got != own:
            count += 1
    return count


def compare(base: dict[str, Any], head: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per chain: orders, how many reads were worse, had fewer wrong attributions, or
    gained a role, and up to three worse examples."""
    summary: dict[str, dict[str, Any]] = collections.defaultdict(
        lambda: {
            "orders": 0,
            "base_wrong": 0,
            "head_wrong": 0,
            "worse": 0,
            "fewer_wrong": 0,
            "gained_role": 0,
            "dropped_misattributed": 0,
            "examples": [],
        }
    )
    for key in base:
        chain = key.split(" | ")[0]
        for phase in ("mid", "final"):
            a, b = base[key][phase], head[key][phase]
            wa, wb = wrong(a), wrong(b)
            lost = set(a["roles"]) - set(b["roles"])
            missing = {
                r
                for r in set(a["roles"]) & set(b["roles"])
                if set(a["roles"][r]) - set(b["roles"][r])
            }
            c = summary[chain]
            c["orders"] += 1
            c["base_wrong"] += wa > 0
            c["head_wrong"] += wb > 0
            if wb <= wa and wa > 0 and (lost or missing):
                c["dropped_misattributed"] += 1
            elif wb > wa or lost or missing:
                c["worse"] += 1
                if len(c["examples"]) < 3:
                    c["examples"].append(f"{phase} {key}: {a['entries']} -> {b['entries']}")
            # Counted apart: a checkout that builds a role the ref never built gains one in
            # every read, which says nothing about whether it attributes headers better.
            if wb < wa:
                c["fewer_wrong"] += 1
            if set(b["roles"]) - set(a["roles"]):
                c["gained_role"] += 1
    return dict(summary)


def _run_tree(src: Path) -> dict[str, Any]:
    env = {**os.environ, "PYTHONPATH": str(src)}
    done = subprocess.run(  # noqa: S603 - this interpreter, this script
        [sys.executable, str(Path(__file__).resolve()), "--emit"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(f"the sweep failed under {src}:\n{done.stderr}")
    return json.loads(done.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ref", action="append", default=[], help="a git ref to compare with")
    parser.add_argument("--checkout", type=Path, default=HERE.parent)
    parser.add_argument("--emit", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.emit:
        json.dump(_emit(), sys.stdout)
        return 0
    if not args.ref:
        parser.print_usage(sys.stderr)
        return 2
    head = _run_tree(args.checkout / "src")
    status = 0
    for ref in args.ref:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "ref.tar"
            command = ["git", "-C", str(args.checkout), "archive", "-o", str(archive), ref, "src"]
            made = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
            if made.returncode != 0:
                print(f"cannot archive {ref}: {made.stderr.strip()}", file=sys.stderr)
                return 2
            with tarfile.open(archive) as tar:
                tar.extractall(Path(tmp) / "ref", filter="data")
            base = _run_tree(Path(tmp) / "ref" / "src")
        print(f"== {ref} against {args.checkout}: {len(head)} orders, each read twice")
        for chain, c in compare(base, head).items():
            print(
                f"{chain}: {c['orders']} reads, {ref} wrong in {c['base_wrong']}, checkout wrong "
                f"in {c['head_wrong']}, worse {c['worse']}, fewer wrong entries in "
                f"{c['fewer_wrong']}, gained a role {c['gained_role']}, "
                f"dropped a misattributed role {c['dropped_misattributed']}"
            )
            for example in c["examples"]:
                print(f"   worse: {example}")
            if c["worse"]:
                status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
