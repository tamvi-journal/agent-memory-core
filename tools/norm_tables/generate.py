"""Generate the frozen Unicode 14 normalizer tables with Python 3.11.

Run with Python 3.11 and supply Python interpreters for Unicode 13, 15.1 and
17 via ``--compare VERSION=PYTHON``. The comparison runtimes are explicit
inputs; Python 3.11 alone cannot infer their mappings.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TABLES = ROOT / "spec" / "tables"
TRANSLITERATE = str.maketrans({
    "đ": "d", "ð": "d", "ł": "l", "ø": "o", "ħ": "h", "ı": "i",
    "ŧ": "t", "æ": "ae", "œ": "oe", "þ": "th",
})


def mapping(char: str, *, identity: bool) -> str:
    folded = char.casefold()
    if not identity:
        folded = folded.translate(TRANSLITERATE)
    folded = unicodedata.normalize("NFKD", folded)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9_]+", " ", folded)


def generate_maps() -> tuple[dict[str, str], dict[str, str]]:
    if sys.version_info[:2] != (3, 11) or unicodedata.unidata_version != "14.0.0":
        raise SystemExit("table generation requires Python 3.11 / Unicode 14.0.0")
    text: dict[str, str] = {}
    identity: dict[str, str] = {}
    for cp in range(0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        char = chr(cp)
        if unicodedata.category(char) == "Cn":
            continue
        for table, old in ((text, False), (identity, True)):
            value = mapping(char, identity=old)
            if value != " ":
                table[f"{cp:x}"] = value
    return text, identity


def write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--compare", action="append", default=[], metavar="UNICODE=PYTHON",
        help="Python executable with the named Unicode version",
    )
    args = parser.parse_args()
    compared = [item.split("=", 1)[0] for item in args.compare]
    if sorted(compared) != ["13.0.0", "15.1.0", "17.0.0"]:
        raise SystemExit("supply --compare for Unicode 13.0.0, 15.1.0 and 17.0.0")
    text, identity = generate_maps()
    TABLES.mkdir(parents=True, exist_ok=True)
    for name, values, file in (
        ("text-norm/v2", text, "text-norm-v2.json"),
        ("identity-v1", identity, "identity-v1.json"),
    ):
        write_json(TABLES / file, {
            "schema": "trajecta.norm-table/v1",
            "name": name,
            "unicode": "14.0.0",
            "generator": {"python": "3.11", "source": "tools/norm_tables/generate.py"},
            "map": values,
        })

    # A comparison is recorded only when every requested interpreter reports
    # the advertised Unicode version. This cannot be inferred from Python 3.11.
    differences: dict[str, dict[str, object]] = {}
    for pair in args.compare:
        version, executable = pair.split("=", 1)
        probe = subprocess.run(
            [executable, __file__, "--emit-comparison"],
            capture_output=True, check=False, text=True,
        )
        if probe.returncode:
            raise SystemExit(f"comparison interpreter failed: {executable}: {probe.stderr}")
        result = json.loads(probe.stdout)
        if result["unicode"] != version:
            raise SystemExit(f"{executable} has Unicode {result['unicode']}, expected {version}")
        for name, baseline in (("text-norm/v2", text), ("identity-v1", identity)):
            other = result["maps"][name]
            for cp in set(baseline) | set(other):
                old, new = baseline.get(cp, " "), other.get(cp, " ")
                if old != new:
                    entry = differences.setdefault(cp, {"unicode14": {}, "variants": {}})
                    entry["unicode14"][name] = old
                    entry["variants"].setdefault(version, {})[name] = new
    write_json(TABLES / "version-sensitive.json", {
        "schema": "trajecta.norm-version-sensitive/v1",
        "baseline": "14.0.0",
        "compared": compared,
        "map": {
            cp: {
                "unicode14": dict(sorted(differences[cp]["unicode14"].items())),
                "variants": {
                    version: dict(sorted(fields.items()))
                    for version, fields in sorted(differences[cp]["variants"].items())
                },
            }
            for cp in sorted(differences, key=lambda k: int(k, 16))
        },
    })


if __name__ == "__main__":
    if sys.argv[1:] == ["--emit-comparison"]:
        # Comparison interpreters may use different Unicode tables. Reuse the
        # same per-code-point mapping law without enforcing the pinned version.
        maps = {"text-norm/v2": {}, "identity-v1": {}}
        for cp in range(0x110000):
            if 0xD800 <= cp <= 0xDFFF:
                continue
            char = chr(cp)
            if unicodedata.category(char) == "Cn":
                continue
            for name, old in (("text-norm/v2", False), ("identity-v1", True)):
                value = mapping(char, identity=old)
                if value != " ":
                    maps[name][f"{cp:x}"] = value
        print(json.dumps({"unicode": unicodedata.unidata_version, "maps": maps}, separators=(",", ":")))
    else:
        main()
