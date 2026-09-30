"""Freeze Python 3.11 / Unicode 14 ``str.title()`` behaviour for the TS port.

``str.title()`` title-cases a character that follows an uncased one and
lower-cases every other character, where "cased" is Unicode's derived Cased
property (it includes Other_Lowercase, e.g. U+00AA and the circled letters).
Title and lower mappings are full mappings (``ß`` -> ``Ss``). JavaScript has
no titlecase mapping and its Unicode version moves with Node, so the section
label fallback (spec 6.6) reads this table instead.

Output: node/tables/py-title-u14.json with
  cased: [[first, last], ...] code point ranges,
  title: {hex: str} where the title form differs from the character,
  lower: {hex: str} where the lower form differs from the character.
"""

from __future__ import annotations

import hashlib
import json
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "node" / "tables" / "py-title-u14.json"


def build() -> dict:
    cased: list[list[int]] = []
    title: dict[str, str] = {}
    lower: dict[str, str] = {}
    for cp in range(0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        char = chr(cp)
        # A following "a" is lower-cased exactly when char is Cased.
        if (char + "a").title()[-1] == "a":
            if cased and cased[-1][1] == cp - 1:
                cased[-1][1] = cp
            else:
                cased.append([cp, cp])
        if char.title() != char:
            title[f"{cp:x}"] = char.title()
        if char.lower() != char:
            lower[f"{cp:x}"] = char.lower()
    return {
        "schema": "trajecta.py-title-table/v1",
        "unicode": unicodedata.unidata_version,
        "python": sys.version.split()[0],
        "cased": cased,
        "title": title,
        "lower": lower,
    }


def encode(table: dict) -> bytes:
    return (json.dumps(table, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


if __name__ == "__main__":
    if sys.version_info[:2] != (3, 11) or unicodedata.unidata_version != "14.0.0":
        raise SystemExit("title table generation requires Python 3.11 / Unicode 14.0.0")
    data = encode(build())
    OUTPUT.write_bytes(data)
    print(OUTPUT.relative_to(ROOT), hashlib.sha256(data).hexdigest())
