"""Answer a question about a recording's HAR with names, attributes, shapes, and counts.

Usage:
  har-facts.py cookies HAR
  har-facts.py request HAR URL_SUBSTRING [--method METHOD]
  har-facts.py response HAR URL_SUBSTRING [--method METHOD]

A HAR holds the account's data and, from a recorded login, the password the user
typed. The graft skill reads one through this script so that no value reaches its
context: the script prints cookie, header, query, and field names, cookie
attributes, value types, and counts, and never a cookie, header, query, or body
value.

``cookies`` prints one block per cookie a response sets: each response that sets
it (method, host, and path), how many times, and the attributes of that setting,
``HttpOnly``, ``Secure``, ``SameSite``, ``Path``, ``Domain``, and whether it has an
expiry (``Expires`` or ``Max-Age``). The attributes come from the raw
``Set-Cookie`` header, every line of a newline-joined value, parsed as graftpunk's
own HAR parser does, and from the entry's ``response.cookies`` array; a line says
when the two disagree, since the array can lack a flag the header carries. Then it
names the cookies the recording only sends and never sets.

``request`` and ``response`` match URL_SUBSTRING against each entry's own request
URL, its query string and fragment left off (never a ``Referer`` or ``Location``
header), and ``--method`` narrows by method. ``request`` prints the method, the
URL with its query keys only, the request header and cookie names, and the body:
its content type and its field names with each value's type (string, number,
bool, null, object, or array) and, for a string, a length class (empty, short up
to 16 characters, medium up to 64, long). ``response`` prints the status, the
content type, the names of the cookies it sets, and the body's shape: JSON keys
and types, or an HTML page's title and its forms' field names and input types. A
field whose name reads as a password, a secret, a token, a card, a key, or the
like is listed by name alone. A URL keeps its scheme, host, and path; a path
segment can still hold an account or order id, which the Secrets rule covers.

At most 20 matching entries are described; past that, a count prints. Exit 0 when
something was described, 1 when nothing matched, 2 on a usage error or a HAR that
is missing or is not a HAR. Standard library only, so it runs under any Python 3.9
or later.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

_MAX_ENTRIES = 20
_MAX_KEYS = 40
_MAX_DEPTH = 4
_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")
_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
# A field named with one of these words is listed by name alone.
_SECRET_WORDS = frozenset(
    {
        "pass",
        "passwd",
        "password",
        "passcode",
        "passphrase",
        "pwd",
        "pin",
        "otp",
        "mfa",
        "totp",
        "secret",
        "token",
        "auth",
        "authorization",
        "credential",
        "credentials",
        "key",
        "apikey",
        "sig",
        "signature",
        "session",
        "sid",
        "card",
        "cc",
        "cvv",
        "cvc",
        "csc",
        "pan",
        "iban",
        "ssn",
        "code",
    }
)
# A key or field name that does not look like a name (an email address, an id, a
# long digit run) may itself be account data, so it is counted, not printed.
_NAME_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_.$:\[\]-]{0,63}")
_DIGIT_RUN_RE = re.compile(r"[0-9]{4,}")
_SECRET_SUBSTRINGS = ("password", "passwd", "secret", "token", "apikey", "cardnumber", "credential")


class HarError(Exception):
    """The HAR cannot be read; the message says why."""


def set_cookie_lines(headers: list[Any]) -> list[tuple[str, dict[str, str]]]:
    """``(name, attributes)`` for every cookie a ``Set-Cookie`` header in *headers*
    sets, in order: each line of a header value is one cookie (Chromium joins
    several ``Set-Cookie`` headers into one value with newlines), its name is
    before the first ``=`` of the part before the first ``;``, and its attributes
    are the ``;``-separated parts after it, keyed by lower-cased attribute name.
    The same parse as graftpunk.har.parser._set_cookie_lines, which a test holds
    equal. The cookie's value is dropped here and never returned."""
    cookies: list[tuple[str, dict[str, str]]] = []
    for header in headers:
        if not isinstance(header, dict) or str(header.get("name", "")).lower() != "set-cookie":
            continue
        for line in _LINE_BREAK_RE.split(str(header.get("value", ""))):
            pair, *parts = line.split(";")
            if "=" not in pair:
                continue
            name = pair.split("=", 1)[0].strip()
            if not name:
                continue
            attributes: dict[str, str] = {}
            for part in parts:
                key, _sep, value = part.partition("=")
                key = key.strip().lower()
                if key:
                    attributes[key] = value.strip()
            cookies.append((name, attributes))
    return cookies


def looks_secret(name: str) -> bool:
    """Whether a field *name* reads as a password, secret, token, card, or key."""
    lowered = name.lower()
    if any(part in lowered for part in _SECRET_SUBSTRINGS):
        return True
    return any(word.lower() in _SECRET_WORDS for word in _WORD_RE.findall(name))


def reads_as_name(key: str) -> bool:
    """Whether *key* looks like a field name rather than a value used as a key."""
    return bool(_NAME_RE.fullmatch(key)) and not _DIGIT_RUN_RE.search(key)


def _unnamed(count: int, indent: str) -> list[str]:
    if not count:
        return []
    return [f"{indent}({count} more keys that read as values, such as ids, not listed)"]


def _load(path: str) -> list[dict[str, Any]]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise HarError(f"cannot read {path}: {exc.strerror or exc}") from exc
    except UnicodeDecodeError as exc:
        raise HarError(f"{path} is not UTF-8 text") from exc
    try:
        har = json.loads(text)
    except ValueError as exc:
        raise HarError(f"{path} is not JSON") from exc
    entries = har.get("log", {}).get("entries") if isinstance(har, dict) else None
    if not isinstance(entries, list):
        raise HarError(f"{path} has no log.entries list, so it is not a HAR")
    return [entry for entry in entries if isinstance(entry, dict)]


def _part(entry: dict[str, Any], key: str) -> dict[str, Any]:
    value = entry.get(key)
    return value if isinstance(value, dict) else {}


def _list(container: dict[str, Any], key: str) -> list[Any]:
    value = container.get(key)
    return value if isinstance(value, list) else []


def _where(url: str) -> str:
    """*url* as scheme, host, and path, with no query, fragment, or userinfo."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return f"{parts.scheme}://{host}{parts.path}" if parts.scheme else parts.path


def _header_names(headers: list[Any]) -> list[str]:
    names = [str(h.get("name", "")) for h in headers if isinstance(h, dict)]
    return list(dict.fromkeys(name for name in names if name))


def _header(headers: list[Any], name: str) -> str:
    for header in headers:
        if isinstance(header, dict) and str(header.get("name", "")).lower() == name:
            return str(header.get("value", ""))
    return ""


def _request_cookie_names(request: dict[str, Any]) -> list[str]:
    names = [str(c.get("name", "")) for c in _list(request, "cookies") if isinstance(c, dict)]
    for header in _list(request, "headers"):
        if isinstance(header, dict) and str(header.get("name", "")).lower() == "cookie":
            for fragment in str(header.get("value", "")).split(";"):
                name, sep, _value = fragment.partition("=")
                if sep:
                    names.append(name.strip())
    return list(dict.fromkeys(name for name in names if name))


# cookies


def _header_view(attributes: dict[str, str]) -> dict[str, str]:
    return {
        "HttpOnly": "yes" if "httponly" in attributes else "no",
        "Secure": "yes" if "secure" in attributes else "no",
        "SameSite": attributes.get("samesite") or "-",
        "Path": attributes.get("path") or "-",
        "Domain": attributes.get("domain") or "-",
        "expiry": "yes" if ("expires" in attributes or "max-age" in attributes) else "no",
    }


def _array_view(cookie: dict[str, Any]) -> dict[str, str]:
    return {
        "HttpOnly": "yes" if cookie.get("httpOnly") is True else "no",
        "Secure": "yes" if cookie.get("secure") is True else "no",
        "SameSite": str(cookie.get("sameSite") or "-"),
        "Path": str(cookie.get("path") or "-"),
        "Domain": str(cookie.get("domain") or "-"),
        "expiry": "yes" if cookie.get("expires") else "no",
    }


def _describe(view: dict[str, str]) -> str:
    return " ".join(f"{key}={value}" for key, value in view.items())


def cookies(entries: list[dict[str, Any]]) -> list[str]:
    settings: dict[str, dict[tuple[str, str], int]] = {}
    sent: dict[str, None] = {}
    for entry in entries:
        request, response = _part(entry, "request"), _part(entry, "response")
        for name in _request_cookie_names(request):
            sent[name] = None
        where = f"{str(request.get('method', '?')).upper()} {_where(str(request.get('url', '')))}"
        from_header = set_cookie_lines(_list(response, "headers"))
        array = {
            str(c.get("name", "")): c
            for c in _list(response, "cookies")
            if isinstance(c, dict) and c.get("name")
        }
        described: set[str] = set()
        for name, attributes in from_header:
            view = _header_view(attributes)
            text = f"header: {_describe(view)}"
            if name in array:
                other = _array_view(array[name])
                differ = [
                    f"{key} is {other[key]} there"
                    for key in ("HttpOnly", "Secure", "Path", "Domain")
                    if other[key] != view[key] and not (other[key] == "-" and key != "HttpOnly")
                ]
                if differ:
                    text += f"; the response.cookies array differs: {', '.join(differ)}"
            else:
                text += "; not in the response.cookies array"
            settings.setdefault(name, {})
            settings[name][(where, text)] = settings[name].get((where, text), 0) + 1
            described.add(name)
        for name, cookie in array.items():
            if name in described:
                continue
            view = _describe(_array_view(cookie))
            text = f"response.cookies array only (no Set-Cookie header line): {view}"
            settings.setdefault(name, {})
            settings[name][(where, text)] = settings[name].get((where, text), 0) + 1
    lines: list[str] = []
    for name in sorted(settings):
        lines.append(f"cookie {name}")
        for (where, text), count in settings[name].items():
            times = "1 time" if count == 1 else f"{count} times"
            lines.append(f"  set by {where} ({times}); {text}")
    only_sent = sorted(name for name in sent if name not in settings)
    if only_sent:
        lines.append(f"sent in requests and never set in this recording: {', '.join(only_sent)}")
    return lines


# shapes


def _length_class(text: str) -> str:
    if not text:
        return "empty"
    if len(text) <= 16:
        return "short"
    return "medium" if len(text) <= 64 else "long"


def _type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return f"string ({_length_class(value)})"
    if isinstance(value, dict):
        return "object"
    return "array"


def _shape(value: Any, indent: str, depth: int) -> list[str]:
    """Lines describing *value*'s keys and types, never a value."""
    lines: list[str] = []
    if isinstance(value, dict):
        keys = [key for key in value if reads_as_name(str(key))]
        hidden = len(value) - len(keys)
        for key in keys[:_MAX_KEYS]:
            child = value[key]
            if looks_secret(str(key)):
                lines.append(f"{indent}{key}: (name reads as a secret; not described)")
                continue
            lines.append(f"{indent}{key}: {_type(child)}{_count(child)}")
            if isinstance(child, (dict, list)) and depth < _MAX_DEPTH:
                lines.extend(_shape(child, indent + "  ", depth + 1))
        if len(keys) > _MAX_KEYS:
            lines.append(f"{indent}({len(keys) - _MAX_KEYS} more keys)")
        lines.extend(_unnamed(hidden, indent))
    elif isinstance(value, list) and value:
        first = value[0]
        lines.append(f"{indent}[first item]: {_type(first)}")
        if isinstance(first, (dict, list)) and depth < _MAX_DEPTH:
            lines.extend(_shape(first, indent + "  ", depth + 1))
    return lines


def _count(value: Any) -> str:
    if isinstance(value, (list, dict)):
        noun = "item" if isinstance(value, list) else "key"
        return f" of {len(value)} {noun}{'' if len(value) == 1 else 's'}"
    return ""


def _json_shape(text: str, indent: str) -> list[str] | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return [f"{indent}JSON {_type(value)}{_count(value)}", *_shape(value, indent + "  ", 1)]


def _form_shape(text: str, indent: str) -> list[str]:
    lines: list[str] = []
    pairs = parse_qsl(text, keep_blank_values=True)
    hidden = sum(1 for name, _value in pairs if not reads_as_name(name))
    for name, value in pairs:
        if not reads_as_name(name):
            continue
        if looks_secret(name):
            lines.append(f"{indent}{name}: (name reads as a secret; not described)")
        else:
            lines.append(f"{indent}{name}: string ({_length_class(value)})")
    lines.extend(_unnamed(hidden, indent))
    return lines or [f"{indent}(no fields)"]


def _multipart_names(text: str) -> list[str]:
    return list(
        dict.fromkeys(re.findall(r'Content-Disposition:[^\r\n]*?\bname="([^"]*)"', text, re.I))
    )


class _PageReader(HTMLParser):
    """An HTML page's title and its forms' field names and input types."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: list[str] = []
        self.in_title = False
        self.forms: list[tuple[str, str, list[tuple[str, str]]]] = []
        self.loose: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = {key: value or "" for key, value in attrs}
        if tag == "title":
            self.in_title = True
        elif tag == "form":
            action = _where(found.get("action", "")) if found.get("action") else "(this page)"
            self.forms.append((found.get("method", "get").upper(), action, []))
        elif tag in ("input", "select", "textarea", "button") and found.get("name"):
            kind = found.get("type", "text").lower() if tag == "input" else tag
            target = self.forms[-1][2] if self.forms else self.loose
            target.append((found["name"], kind))

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title.append(data)


def _html_shape(text: str, indent: str) -> list[str]:
    reader = _PageReader()
    reader.feed(text)
    title = " ".join("".join(reader.title).split())[:80]
    lines = [f"{indent}HTML page, title: {title or '(none)'}"]
    groups = [(f"form {m} {a}", fields) for m, a, fields in reader.forms]
    if reader.loose:
        groups.append(("fields outside any form", reader.loose))
    for label, fields in groups:
        secret = any(kind == "password" or looks_secret(name) for name, kind in fields)
        note = " (holds a password-like field)" if secret else ""
        lines.append(f"{indent}{label}{note}")
        lines.extend(f"{indent}  {name}: {kind}" for name, kind in fields)
    return lines


def _body_lines(mime: str, text: str | None, indent: str) -> list[str]:
    if text is None:
        return [f"{indent}no body text recorded"]
    if not text:
        return [f"{indent}empty"]
    lowered = mime.lower()
    if "json" in lowered or text.lstrip()[:1] in ("{", "["):
        shape = _json_shape(text, indent)
        if shape is not None:
            return shape
    if "x-www-form-urlencoded" in lowered:
        return _form_shape(text, indent)
    if "multipart/form-data" in lowered:
        names = _multipart_names(text)
        listed = [
            f"{indent}{n}: (name reads as a secret; not described)"
            if looks_secret(n)
            else f"{indent}{n}"
            for n in names
        ]
        return (
            [f"{indent}multipart fields:", *listed] if names else [f"{indent}multipart, no fields"]
        )
    if "html" in lowered:
        return _html_shape(text, indent)
    return [f"{indent}{len(text)} characters of {mime or 'unnamed type'}, not parsed"]


# request and response


def _matches(
    entries: list[dict[str, Any]], needle: str, method: str | None
) -> list[dict[str, Any]]:
    found = []
    for entry in entries:
        request = _part(entry, "request")
        url = str(request.get("url", ""))
        if needle not in url.split("#", 1)[0].split("?", 1)[0]:
            continue
        if method and str(request.get("method", "")).upper() != method.upper():
            continue
        found.append(entry)
    return found


def _query_keys(url: str) -> str:
    query = urlsplit(url).query
    names = list(dict.fromkeys(name for name, _v in parse_qsl(query, keep_blank_values=True)))
    keys = [name if reads_as_name(name) else "(a key that reads as a value)" for name in names]
    return f"?{'&'.join(keys)}" if keys else ""


def describe_request(entry: dict[str, Any]) -> list[str]:
    request = _part(entry, "request")
    url = str(request.get("url", ""))
    lines = [f"{str(request.get('method', '?')).upper()} {_where(url)}{_query_keys(url)}"]
    headers = _list(request, "headers")
    lines.append(f"  request headers: {', '.join(_header_names(headers)) or '(none)'}")
    cookie_names = _request_cookie_names(request)
    lines.append(f"  request cookies: {', '.join(cookie_names) or '(none)'}")
    post = request.get("postData")
    if not isinstance(post, dict):
        lines.append("  body: none recorded")
        return lines
    mime = str(post.get("mimeType") or _header(headers, "content-type"))
    lines.append(f"  body: {mime or 'no content type'}")
    params = [p for p in _list(post, "params") if isinstance(p, dict)]
    text = post.get("text")
    if params and not isinstance(text, str):
        for param in params:
            name = str(param.get("name", ""))
            detail = "(name reads as a secret; not described)" if looks_secret(name) else "string"
            lines.append(f"    {name}: {detail}")
        return lines
    lines.extend(_body_lines(mime, text if isinstance(text, str) else None, "    "))
    return lines


def _response_text(entry: dict[str, Any], har_path: Path) -> str | None:
    content = _part(_part(entry, "response"), "content")
    body_file = content.get("_bodyFile")
    if isinstance(body_file, str) and body_file:
        try:
            return (har_path.parent / body_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
    text = content.get("text")
    if not isinstance(text, str):
        return None
    if content.get("encoding") == "base64":
        try:
            return base64.b64decode(text).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    return text


def describe_response(entry: dict[str, Any], har_path: Path) -> list[str]:
    request, response = _part(entry, "request"), _part(entry, "response")
    url = str(request.get("url", ""))
    lines = [f"{str(request.get('method', '?')).upper()} {_where(url)}"]
    content = _part(response, "content")
    headers = _list(response, "headers")
    mime = str(content.get("mimeType") or _header(headers, "content-type"))
    lines.append(f"  status {response.get('status', '?')}, content type {mime or '(none)'}")
    names = list(dict.fromkeys(name for name, _a in set_cookie_lines(headers)))
    names += [
        str(c.get("name"))
        for c in _list(response, "cookies")
        if isinstance(c, dict) and c.get("name") and str(c.get("name")) not in names
    ]
    lines.append(f"  sets cookies: {', '.join(names) or '(none)'}")
    lines.append("  body:")
    lines.extend(_body_lines(mime, _response_text(entry, har_path), "    "))
    return lines


def main(argv: list[str]) -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(prog="har-facts.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("cookies").add_argument("har")
    for name in ("request", "response"):
        one = sub.add_parser(name)
        one.add_argument("har")
        one.add_argument("url_substring")
        one.add_argument("--method")
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        entries = _load(args.har)
    except HarError as exc:
        print(f"har-facts: {exc}.", file=sys.stderr)
        return 2
    if args.command == "cookies":
        lines = cookies(entries)
        if not lines:
            print(
                "har-facts: no response in this HAR sets a cookie, and no request sends one.",
                file=sys.stderr,
            )
            return 1
        print("\n".join(lines))
        return 0
    found = _matches(entries, args.url_substring, args.method)
    if not found:
        method = f" with method {args.method.upper()}" if args.method else ""
        print(f"har-facts: no request URL holds {args.url_substring!r}{method}.", file=sys.stderr)
        return 1
    blocks = []
    for entry in found[:_MAX_ENTRIES]:
        if args.command == "request":
            blocks.append("\n".join(describe_request(entry)))
        else:
            blocks.append("\n".join(describe_response(entry, Path(args.har))))
    print("\n\n".join(blocks))
    if len(found) > _MAX_ENTRIES:
        more = len(found) - _MAX_ENTRIES
        print(f"\n({more} more entries match; narrow the URL substring or add --method)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
