"""PR29 corpus impact: full-file comparison with precisely eight header bytes masked."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

MASK = (range(24, 28), range(92, 96))
PROVENANCE = {"files", "oracle_sources", "oracle_source_sha256", "oracle_semantics"}


def masked(raw: bytes) -> bytes:
    result = bytearray(raw)
    for span in MASK:
        for index in span:
            if index < len(result):
                result[index] = 0
    return bytes(result)


def sqlite_proof(name: str, a: bytes, b: bytes) -> dict:
    if not a.startswith(b"SQLite format 3\0") or not b.startswith(b"SQLite format 3\0"):
        raise ValueError(f"hash reference is not a SQLite file: {name}")
    if masked(a) != masked(b):
        raise ValueError(f"initialization patch changed bytes outside the eight header bytes: {name}")
    return {"file": name, "size": len(a), "old_sha256": hashlib.sha256(a).hexdigest(),
            "new_sha256": hashlib.sha256(b).hexdigest(), "masked_sha256": hashlib.sha256(masked(a)).hexdigest()}


def json_spans(raw: bytes) -> tuple[object, dict]:
    """Keep raw token spans; reserialization cannot prove unchanged JSON bytes/order."""
    text = raw.decode("utf-8")
    decoder = json.JSONDecoder()
    spans = {}

    def whitespace(pos):
        while pos < len(text) and text[pos] in " \r\n\t":
            pos += 1
        return pos

    def value(pos, path):
        pos = whitespace(pos)
        start = pos
        if text[pos] == "{":
            result = {}
            pos = whitespace(pos + 1)
            if text[pos] != "}":
                while True:
                    key, pos = decoder.raw_decode(text, pos)
                    if not isinstance(key, str) or key in result:
                        raise ValueError("invalid/duplicate JSON key in fixture")
                    pos = whitespace(pos)
                    if text[pos] != ":": raise ValueError("invalid fixture JSON object")
                    result[key], pos = value(pos + 1, path + (key,))
                    pos = whitespace(pos)
                    if text[pos] != ",": break
                    pos = whitespace(pos + 1)
            if text[pos] != "}": raise ValueError("invalid fixture JSON object")
            pos += 1
        elif text[pos] == "[":
            result = []
            pos = whitespace(pos + 1)
            if text[pos] != "]":
                while True:
                    item, pos = value(pos, path + (len(result),))
                    result.append(item)
                    pos = whitespace(pos)
                    if text[pos] != ",": break
                    pos = whitespace(pos + 1)
            if text[pos] != "]": raise ValueError("invalid fixture JSON array")
            pos += 1
        else:
            result, pos = decoder.raw_decode(text, pos)
        spans[path] = (len(text[:start].encode("utf-8")), len(text[:pos].encode("utf-8")))
        return result, pos

    result, end = value(0, ())
    if whitespace(end) != len(text): raise ValueError("trailing fixture JSON content")
    return result, spans


def changed_values(a, b, path=()):
    if type(a) != type(b): raise ValueError(f"fixture JSON kind changed: {path}")
    if isinstance(a, dict):
        if a.keys() != b.keys(): raise ValueError(f"fixture JSON keys changed: {path}")
        return [row for key in a for row in changed_values(a[key], b[key], path + (key,))]
    if isinstance(a, list):
        if len(a) != len(b): raise ValueError(f"fixture JSON array changed: {path}")
        return [row for i in range(len(a)) for row in changed_values(a[i], b[i], path + (i,))]
    return [] if a == b else [(path, a, b)]


def hash_references(name, a, b, old, new, old_references, new_references):
    left, left_spans = json_spans(a)
    right, right_spans = json_spans(b)
    proof, masks = [], []
    for path, before, after in changed_values(left, right):
        if (len(path) != 3 or not isinstance(path[1], str) or
                (path[0], path[2]) not in {("databases", "oracle_sha256"),
                                         ("tree", "unchanged_sha256"), ("tree", "sha256")}):
            raise ValueError(f"non-hash fixture JSON change: {name}:{path}")
        if not all(isinstance(x, str) and re.fullmatch(r"[0-9a-f]{64}", x) for x in (before, after)):
            raise ValueError(f"invalid derived SHA: {name}:{path}")
        relative = (Path(name).parent / path[1]).as_posix()
        # Final databases (including backup files) may be temporary generator outputs.
        # Retain those exact old/new bytes at the existing database_classes read seam.
        if old_references is not None and new_references is not None:
            old_file, new_file = old_references / relative, new_references / relative
        else:
            old_file, new_file = old.get(relative), new.get(relative)
            if old_file is None or new_file is None:
                raise ValueError(f"missing retained SQLite hash reference: {name}:{path}")
        if not old_file.is_file() or not new_file.is_file():
            raise ValueError(f"missing retained SQLite hash reference: {name}:{path}")
        reference = sqlite_proof(relative, old_file.read_bytes(), new_file.read_bytes())
        if before != reference["old_sha256"] or after != reference["new_sha256"]:
            raise ValueError(f"derived SHA does not match exact referenced file: {name}:{path}")
        proof.append({**reference, "file": name, "json_path": list(path), "reference_file": relative})
        masks.append(path)

    def mask_set(raw, spans):
        # All validated fields together, retaining every other byte (including order).
        for path in sorted(masks, key=lambda path: spans[path][0], reverse=True):
            start, end = spans[path]
            raw = raw[:start] + b'"' + b"0" * 64 + b'"' + raw[end:]
        return raw

    if mask_set(a, left_spans) != mask_set(b, right_spans):
        raise ValueError(f"fixture JSON bytes/order changed outside validated hash set: {name}")
    return proof


def compare(before: Path, after: Path, *, old_references: Path | None = None,
            new_references: Path | None = None) -> list[dict]:
    old = {p.relative_to(before).as_posix(): p for p in before.rglob("*") if p.is_file()}
    new = {p.relative_to(after).as_posix(): p for p in after.rglob("*") if p.is_file()}
    if old.keys() != new.keys():
        raise ValueError("initialization patch changed the corpus file inventory")
    proof = []
    for name, path in sorted(old.items()):
        a, b = path.read_bytes(), new[name].read_bytes()
        if name == "MANIFEST.json":
            left, right = json.loads(a), json.loads(b)
            if {k: v for k, v in left.items() if k not in PROVENANCE} != {k: v for k, v in right.items() if k not in PROVENANCE}:
                raise ValueError("initialization patch changed non-provenance manifest fields")
            continue
        if a == b:
            continue
        if name.endswith("/expected.json"):
            proof.extend(hash_references(name, a, b, old, new, old_references, new_references))
            continue
        if not name.endswith(".sqlite3") or not a.startswith(b"SQLite format 3\0") or not b.startswith(b"SQLite format 3\0"):
            raise ValueError(f"initialization patch changed a frozen payload: {name}")
        proof.append(sqlite_proof(name, a, b))
    return proof
