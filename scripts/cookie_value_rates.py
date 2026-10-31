"""The share of random cookie values per shape that would flag a plain-named,
non-HttpOnly cookie in ``flagged_names_of``, under the 1.24.0 rule (a value that
does not read as words) and the current one (a value that is not form state), with
``holds_an_id`` alone for comparison.

Not a test: the rate tests in ``tests/unit/test_har_paths.py`` and
``tests/unit/test_har_digest.py`` pin smaller samples of the same shapes (the
latter loads ``shapes`` from this file). Run it from the repository root to print the
table the #296 changelog entry and docstrings quote::

    uv run -p 3.12 python scripts/cookie_value_rates.py [N]

N defaults to 100,000. Each shape draws from one ``random.Random(273)`` stream in
table order, so a row's sample depends on the rows above it; new shapes go last.
"""

from __future__ import annotations

import base64
import json
import random
import string
import sys
import uuid
from collections.abc import Callable
from urllib.parse import quote

from graftpunk.har.digest import form_state
from graftpunk.har.paths import holds_an_id, reads_as_words

_B64 = string.ascii_letters + string.digits + "+/"
_HEX = "0123456789abcdef"
_NAME_PUNCTUATION = " ,.'-"


def shapes(rng: random.Random) -> dict[str, Callable[[], str]]:
    def draw(alphabet: str, k: int) -> str:
        return "".join(rng.choices(alphabet, k=k))

    def b64url(nbytes: int) -> str:
        return base64.urlsafe_b64encode(rng.randbytes(nbytes)).rstrip(b"=").decode()

    def jwt() -> str:
        header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
        claims = json.dumps({"sub": str(rng.randrange(10**9)), "iat": rng.randrange(10**10)})
        payload = base64.urlsafe_b64encode(claims.encode()).rstrip(b"=").decode()
        return f"{header}.{payload}.{b64url(32)}"

    def capitalised(k: int) -> str:
        return draw(string.ascii_uppercase, 1) + draw(string.ascii_lowercase, k - 1)

    upper, lower = string.ascii_uppercase, string.ascii_lowercase
    out: dict[str, Callable[[], str]] = {}
    for k in (8, 16, 22, 32):
        out[f"base64 {k}"] = lambda k=k: draw(_B64, k)
    for k in (8, 16, 32):
        out[f"hex {k}"] = lambda k=k: draw(_HEX, k)
    for k in (8, 12, 16, 32):
        out[f"lowercase {k}"] = lambda k=k: draw(lower, k)
    for k in (8, 12, 16, 32):
        out[f"mixed-case {k}"] = lambda k=k: draw(string.ascii_letters, k)
    for k in (4, 7, 9, 12):
        out[f"capitals {k}"] = lambda k=k: draw(upper, k)
    for k in (8, 12, 16):
        out[f"capitals and digits {k}"] = lambda k=k: draw(upper + string.digits, k)
    out["uuid"] = lambda: str(uuid.UUID(int=rng.getrandbits(128), version=4))
    out["jwt"] = jwt
    out["jwt signature (43)"] = lambda: b64url(32)
    for k in (5, 6, 8, 10):
        out[f"numeric {k}"] = lambda k=k: str(rng.randrange(10 ** (k - 1), 10**k))
    for k in (8, 16, 32):
        out[f"percent-encoded bytes {k}"] = lambda k=k: quote(rng.randbytes(k), safe="")
    for k in (8, 16, 32):
        out[f"percent-encoded bytes 0x80-0xFF {k}"] = lambda k=k: quote(
            bytes(rng.randrange(0x80, 0x100) for _ in range(k)), safe=""
        )
    out["base64 12"] = lambda: draw(_B64, 12)
    # Shapes the #296 round-2 reviews measured: short mixed case, the name rule's
    # alphabets, and words with a run of capitals inside.
    for k in (6, 10):
        out[f"mixed-case {k}"] = lambda k=k: draw(string.ascii_letters, k)
    out["capitals and spaces 8"] = lambda: draw(upper + " ", 8)
    out["capitals and name punctuation 8"] = lambda: draw(upper + _NAME_PUNCTUATION, 8)
    out["letters and name punctuation 8"] = lambda: draw(
        string.ascii_letters + _NAME_PUNCTUATION, 8
    )
    out["three words of 3 capitals"] = lambda: " ".join(draw(upper, 3) for _ in range(3))
    out["two capitalised words of 4"] = lambda: f"{capitalised(4)} {capitalised(4)}"
    out["two words of 4 capitals"] = lambda: f"{draw(upper, 4)} {draw(upper, 4)}"
    out["3 capitals, capitalised 8"] = lambda: f"{draw(upper, 3)} {capitalised(8)}"
    out['"3 capitals, capitalised 8"'] = lambda: f'"{draw(upper, 3)} {capitalised(8)}"'
    out["4 lower, 3 capitals, 5 lower"] = lambda: draw(lower, 4) + draw(upper, 3) + draw(lower, 5)
    out["5 lower, 5 capitals"] = lambda: draw(lower, 5) + draw(upper, 5)
    out["4 lower, 2 to 4 capitals, 4 lower"] = lambda: (
        draw(lower, 4) + draw(upper, rng.randint(2, 4)) + draw(lower, 4)
    )
    out["three capitalised words of 3 to 5"] = lambda: "".join(
        capitalised(rng.randint(3, 5)) for _ in range(3)
    )
    out["4 capitals, 4 lower, capitalised 6"] = lambda: (
        draw(upper, 4) + draw(lower, 4) + capitalised(6)
    )
    return out


RULES: dict[str, Callable[[str], bool]] = {
    "1.24.0": lambda v: not reads_as_words(v),
    "this branch": lambda v: not form_state(v),
    "holds_an_id only": holds_an_id,
}


def main(n: int) -> None:
    table = shapes(random.Random(273))  # noqa: S311 - a fixed sample, not a secret
    print("| shape | " + " | ".join(RULES) + " |")
    print("|---|" + "---|" * len(RULES))
    for name, generate in table.items():
        values = [generate() for _ in range(n)]
        cells = [f"{100 * sum(map(rule, values)) / n:.3f}%" for rule in RULES.values()]
        print(f"| {name} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 100_000)
