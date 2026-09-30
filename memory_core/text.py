"""Frozen Unicode 14 text normalization for retrieval and evidence identity."""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path


TEXT_NORMALIZER_VERSION = "text-norm/v2"
# Package data next to schema.sql, so every install (wheel, --user, the
# vendored Claude .plugin) carries the exact frozen tables.
_TABLES = Path(__file__).resolve().with_name("tables")
_DIGESTS = {
    "text-norm/v2": ("text-norm-v2.json", "d1b7f523d9bbd35968543ef9582837d8cd28d8bb8a1fb1771f001682618c9b69"),
    "identity-v1": ("identity-v1.json", "51dab4bc79928eef80a063130f4638864a0c97391964c81a14ad32dd999749a9"),
}


@lru_cache(maxsize=2)
def _table(name: str) -> dict[str, str]:
    file, expected = _DIGESTS[name]
    path = _TABLES / file
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise RuntimeError(f"normalizer table {file} sha256 mismatch: {actual}")
    payload = json.loads(raw)
    if (payload.get("schema"), payload.get("name"), payload.get("unicode")) != (
        "trajecta.norm-table/v1", name, "14.0.0"
    ):
        raise RuntimeError(f"normalizer table {file} has invalid metadata")
    return payload["map"]


def _normalize(value: str, name: str) -> str:
    table = _table(name)
    folded = "".join(table.get(f"{ord(char):x}", " ") for char in value)
    return " ".join(re.findall(r"[a-z0-9_]+", folded))


def normalize_text(value: str) -> str:
    """Retrieval normalizer (`text-norm/v2`), pinned to Unicode 14."""

    return _normalize(value, "text-norm/v2")


def normalize_identity_v1(value: str) -> str:
    """Frozen evidence-v2 identity normalizer, pinned to Unicode 14."""

    return _normalize(value, "identity-v1")


def tokens(value: str) -> list[str]:
    return [part for part in normalize_text(value).split() if len(part) > 1]


# Loading the runtime also verifies both pinned artifacts, including the
# evidence identity table even if the first operation is only a retrieval.
_table("text-norm/v2")
_table("identity-v1")
