"""Trim a saved Selenium performance log to the Network events of the requests
sent to 127.0.0.1 (a local test site), with every header value a placeholder
except those the ExtraInfo correlator and the role classifier read, the site's
port rewritten to 8080, and request bodies left out, and write it as a test
fixture. It answers "does this captured event order keep each request's sent
headers apart" without committing a real log.

Input: JSON with an "events" list of {"method", "params"} CDP events, as saved
by a probe of SeleniumCaptureBackend's performance log. The events keep their
order. An existing output file is left alone unless --force is given.

Usage: python3 scripts/trim_selenium_perflog.py [--force] [--source TEXT] <perflog.json> <out.json>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# The header values the correlator and the classifier read; every other value
# becomes a placeholder, since a log can hold an account's data.
KEEP = frozenset(
    {
        "accept",
        "content-length",
        "content-type",
        "host",
        "location",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
        "upgrade-insecure-requests",
    }
)
EVENTS = frozenset(
    {
        "Network.requestWillBeSent",
        "Network.requestWillBeSentExtraInfo",
        "Network.responseReceived",
        "Network.responseReceivedExtraInfo",
    }
)
DEFAULT_SOURCE = (
    "A Selenium performance log, trimmed to a local site's Network events, values replaced"
)


def _site_port(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        if event.get("method") != "Network.requestWillBeSent":
            continue
        url = event["params"]["request"]["url"]
        if "://127.0.0.1:" in url:
            return url.split("://127.0.0.1:")[1].split("/")[0]
    return None


def trim(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """*events* trimmed as the module docstring says, in their order."""
    port = _site_port(events)
    if port is None:
        return []
    here = f"127.0.0.1:{port}"

    def site(text: str) -> str:
        return text.replace(here, "127.0.0.1:8080")

    def headers(given: dict[str, Any] | None) -> dict[str, str]:
        out: dict[str, str] = {}
        for name, value in (given or {}).items():
            lower = name.lower()
            if lower in ("cookie", "set-cookie"):
                out[name] = "placeholder=1"
            elif lower in KEEP:
                out[name] = site(str(value))
            elif lower == "user-agent":
                out[name] = "Mozilla/5.0 Chrome/158.0.0.0"
            else:
                out[name] = "placeholder"
        return out

    ids = {
        e["params"]["requestId"]
        for e in events
        if e.get("method") == "Network.requestWillBeSent" and here in e["params"]["request"]["url"]
    }
    out: list[dict[str, Any]] = []
    for event in events:
        method, params = event.get("method"), event.get("params", {})
        if method not in EVENTS or params.get("requestId") not in ids:
            continue
        kept: dict[str, Any] = {"requestId": params["requestId"][-6:]}
        for key in ("wallTime", "type", "redirectHasExtraInfo", "statusCode"):
            if key in params:
                kept[key] = params[key]
        if "documentURL" in params:
            kept["documentURL"] = site(params["documentURL"])
        if "request" in params:
            request = params["request"]
            # postData and postDataEntries are left out: a login's body holds
            # its credentials.
            kept["request"] = {
                "url": site(request["url"]),
                "method": request["method"],
                "headers": headers(request.get("headers")),
                "hasPostData": request.get("hasPostData", False),
            }
        for key in ("redirectResponse", "response"):
            if key in params:
                response = params[key]
                kept[key] = {
                    **({"url": site(response["url"])} if "url" in response else {}),
                    "status": response["status"],
                    "statusText": response.get("statusText", ""),
                    "headers": headers(response.get("headers")),
                    "mimeType": response.get("mimeType", ""),
                }
        if "headers" in params and str(method).endswith("ExtraInfo"):
            kept["headers"] = headers(params["headers"])
        out.append({"method": method, "params": kept})
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("perflog", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--force", action="store_true", help="replace an existing output file")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="the fixture's source note")
    args = parser.parse_args(argv)
    if args.out.exists() and not args.force:
        print(f"{args.out} exists; pass --force to replace it", file=sys.stderr)
        return 1
    events = trim(json.loads(args.perflog.read_text())["events"])
    args.out.write_text(json.dumps({"source": args.source, "events": events}, indent=1))
    print(len(events), "events")
    return 0


if __name__ == "__main__":
    sys.exit(main())
