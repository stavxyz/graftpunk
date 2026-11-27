"""Trim a saved Selenium performance log to the Network events of the requests
sent to 127.0.0.1 (a local test site), with every header value a placeholder
except those the ExtraInfo correlator and the role classifier read, the site's
port rewritten to 8080, and request bodies left out, and write it as a test
fixture. It answers "does this captured event order keep each request's sent
headers apart" without committing a real log.

Input: JSON with an "events" list of {"method", "params"} CDP events, as saved
by a probe of SeleniumCaptureBackend's performance log.

Usage: python3 scripts/trim_selenium_perflog.py <perflog.json> <out.json>
"""

import json
import sys
from pathlib import Path

KEEP = {
    "content-length",
    "content-type",
    "host",
    "location",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "accept",
    "upgrade-insecure-requests",
}
EVENTS = {
    "Network.requestWillBeSent",
    "Network.requestWillBeSentExtraInfo",
    "Network.responseReceived",
    "Network.responseReceivedExtraInfo",
}
PORT = None


def site(text):
    return text.replace(f"127.0.0.1:{PORT}", "127.0.0.1:8080")


def headers(h):
    out = {}
    for k, v in (h or {}).items():
        if k.lower() == "cookie" or k.lower() == "set-cookie":
            out[k] = "placeholder=1"
        elif k.lower() in KEEP:
            out[k] = site(str(v))
        elif k.lower() == "user-agent":
            out[k] = "Mozilla/5.0 Chrome/158.0.0.0"
        else:
            out[k] = "placeholder"
    return out


src, dst = sys.argv[1:]
events = json.loads(Path(src).read_text())["events"]
for e in events:
    url = json.dumps(e["params"])
    if "127.0.0.1:" in url and PORT is None:
        PORT = url.split("127.0.0.1:")[1].split("/")[0].split('"')[0]
ids = {
    e["params"]["requestId"]
    for e in events
    if e["method"] == "Network.requestWillBeSent"
    and f"127.0.0.1:{PORT}" in e["params"]["request"]["url"]
}
out = []
for e in events:
    m, p = e["method"], e["params"]
    if m not in EVENTS or p.get("requestId") not in ids:
        continue
    q = {"requestId": p["requestId"][-6:]}
    if "wallTime" in p:
        q["wallTime"] = p["wallTime"]
    for k in ("type", "redirectHasExtraInfo", "statusCode"):
        if k in p:
            q[k] = p[k]
    if "documentURL" in p:
        q["documentURL"] = site(p["documentURL"])
    if "request" in p:
        r = p["request"]
        q["request"] = {
            "url": site(r["url"]),
            "method": r["method"],
            "headers": headers(r.get("headers")),
            "hasPostData": r.get("hasPostData", False),
        }
    if "redirectResponse" in p:
        r = p["redirectResponse"]
        q["redirectResponse"] = {
            "status": r["status"],
            "statusText": r.get("statusText", ""),
            "headers": headers(r.get("headers")),
            "mimeType": r.get("mimeType", ""),
        }
    if "response" in p:
        r = p["response"]
        q["response"] = {
            "url": site(r["url"]),
            "status": r["status"],
            "statusText": r.get("statusText", ""),
            "headers": headers(r.get("headers")),
            "mimeType": r.get("mimeType", ""),
        }
    if "headers" in p and m.endswith("ExtraInfo"):
        q["headers"] = headers(p["headers"])
    out.append({"method": m, "params": q})
Path(dst).write_text(
    json.dumps(
        {
            "source": "A Selenium login's performance log, Chrome 158, local http site, "
            "2026-11-27; trimmed to the site's Network events, values replaced",
            "events": out,
        },
        indent=1,
    )
)
print(len(out), "events")
