"""Shared R3 isolation and the settled comparison laws. Synthetic diagnostics only."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import shutil
import sqlite3
import stat
from datetime import datetime
from pathlib import Path

from tools.golden.generate import dump_database
from tools.golden_cli.generate import schema_record
from tools.golden_cli.tokens import render, template

ROOT = Path(__file__).resolve().parents[2]
TRANSIENT = ("-wal", "-shm", "-journal")


def contained(root: Path, path: Path) -> Path:
    root = root.resolve(strict=True)
    target = path.resolve()
    if not target.is_relative_to(root):
        raise ValueError("path outside selected root")
    return target


def isolated_env(root: Path) -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve(strict=True)
    env = {"PATH": os.environ.get("PATH", os.defpath), "PYTHONDONTWRITEBYTECODE": "1"}
    if os.name == "nt":
        for key in ("SYSTEMROOT", "COMSPEC"):
            if key in os.environ:
                env[key] = os.environ[key]
    for key, leaf in {
        "HOME": "home", "USERPROFILE": "home", "XDG_DATA_HOME": "xdg",
        "LOCALAPPDATA": "local", "APPDATA": "app", "TRAJECTA_IDENTITY_DATA_DIR": "data",
        "TRAJECTA_IDENTITY_PROFILES": "profiles", "TMPDIR": "tmp", "TMP": "tmp", "TEMP": "tmp",
    }.items():
        path = contained(root, root / leaf)
        path.mkdir(exist_ok=True)
        env[key] = str(path)
    if not (root / "profiles/example/profile.json").exists():
        shutil.copytree(ROOT / "trajecta_identity/profiles", root / "profiles", dirs_exist_ok=True)
    return env


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def snapshot(root: Path) -> dict:
    """No content escapes this local synthetic comparator."""
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("symlink in harness tree")
        contained(root, path)
        key = path.relative_to(root).as_posix()
        if path.is_dir():
            result[key] = {"kind": "directory"}
        else:
            info = path.stat()
            result[key] = {"kind": "file", "bytes": path.read_bytes(),
                           "mtime": info.st_mtime_ns, "mode": stat.S_IMODE(info.st_mode)}
    return result


def schema_and_dump(path: Path) -> tuple[dict, dict]:
    return schema_record(path.resolve()), dump_database(path)


def expected_for_run(raw: bytes, file: str, oracle_root: Path, actual_root: Path,
                     context: str = "json-string") -> bytes:
    encoded, registry = template(raw, file=file, root=str(oracle_root.resolve()), origin="", context=context)
    return render(encoded, registry, file=file, root=str(actual_root.resolve()), origin="")


def adjustment_number_spans(raw: str) -> dict[tuple, tuple[int, int]]:
    """Locate only adjustment new_value atoms, retaining all other stored bytes."""
    decoder = json.JSONDecoder()
    spans = {}
    def whitespace(index):
        while index < len(raw) and raw[index] in " \t\r\n": index += 1
        return index
    def visit(index, path):
        index = whitespace(index)
        if raw[index] == "{":
            index = whitespace(index + 1)
            while raw[index] != "}":
                key, index = decoder.raw_decode(raw, index)
                index = whitespace(index)
                assert raw[index] == ":"
                index = whitespace(visit(index + 1, (*path, key)))
                if raw[index] != ",": break
                index = whitespace(index + 1)
            assert raw[index] == "}"
            return index + 1
        if raw[index] == "[":
            index = whitespace(index + 1)
            item = 0
            while raw[index] != "]":
                index = whitespace(visit(index, (*path, item)))
                item += 1
                if raw[index] != ",": break
                index = whitespace(index + 1)
            assert raw[index] == "]"
            return index + 1
        value, end = decoder.raw_decode(raw, index)
        if len(path) == 3 and path[0] == "adjustments" and isinstance(path[1], int) and path[2] == "new_value":
            assert type(value) in (int, float), "decay new_value must remain numeric"
            spans[path] = (index, end)
        return end
    assert whitespace(visit(0, ())) == len(raw)
    return spans


def compare_dumps(expected: dict, actual: dict, *, decay: bool = False) -> None:
    if not decay:
        assert actual == expected, "R0 dump mismatch"
        return
    actual = copy.deepcopy(actual)
    allowed = set()
    wanted_ops = expected["tables"].get("memory_operations_v3", [])
    got_ops = actual["tables"].get("memory_operations_v3", [])
    assert len(wanted_ops) == len(got_ops)
    for want, got in zip(wanted_ops, got_ops):
        if not want["idempotency_key"].startswith("maintenance:decay:"):
            continue
        wd, gd = json.loads(want["details_json"]), json.loads(got["details_json"])
        assert len(wd["adjustments"]) == len(gd["adjustments"])
        for wa, ga in zip(wd["adjustments"], gd["adjustments"]):
            assert wa["record_id"] == ga["record_id"] and wa["field"] == ga["field"] == "accessibility"
            assert type(wa["new_value"]) is type(ga["new_value"]), "decay numeric kind mismatch"
            assert abs(wa["new_value"] - ga["new_value"]) <= 1e-6
            allowed.add(wa["record_id"])
        wanted_spans = adjustment_number_spans(want["details_json"])
        actual_spans = adjustment_number_spans(got["details_json"])
        assert wanted_spans.keys() == actual_spans.keys()
        assert len(wanted_spans) == len(wd["adjustments"])
        masked = got["details_json"]
        for path, (start, end) in sorted(actual_spans.items(), key=lambda item: item[1][0], reverse=True):
            a, b = wanted_spans[path]
            masked = masked[:start] + want["details_json"][a:b] + masked[end:]
        assert masked == want["details_json"], "decay difference outside new_value numeric tokens"
        got["details_json"] = masked
    revisions = {r["revision_id"]: r["record_id"] for r in expected["tables"].get("memory_revisions_v3", [])}
    for want, got in zip(expected["tables"].get("memory_telemetry_v3", []), actual["tables"].get("memory_telemetry_v3", [])):
        if revisions.get(want["revision_id"]) in allowed:
            assert abs(float.fromhex(want["accessibility"]["hex"]) - float.fromhex(got["accessibility"]["hex"])) <= 1e-6
            got["accessibility"] = want["accessibility"]
    assert actual == expected, "dump difference outside record-by-record decay whitelist"


def compare_trees(oracle_root: Path, actual_root: Path, before_oracle: dict, before_actual: dict,
                  *, decay: bool = False) -> None:
    want, got = snapshot(oracle_root), snapshot(actual_root)
    assert want.keys() == got.keys(), "file inventory mismatch"
    for key in want:
        assert want[key]["kind"] == got[key]["kind"]
        if want[key]["kind"] == "directory":
            continue
        if key.endswith(".sqlite3"):
            unchanged = key in before_oracle and want[key]["bytes"] == before_oracle[key]["bytes"]
            if unchanged:
                assert want[key] == before_oracle[key], "oracle changed stat of an unchanged DB"
                assert got[key] == before_actual[key], "unchanged DB bytes/stat modified"
                for suffix in TRANSIENT:
                    side = key + suffix
                    assert (side in want) == (side in got)
                    if side in want:
                        assert got[side] == before_actual[side] and want[side] == before_oracle[side]
            else:
                for suffix in TRANSIENT:
                    assert key + suffix not in want and key + suffix not in got
                ws, wd = schema_and_dump(oracle_root / key)
                gs, gd = schema_and_dump(actual_root / key)
                assert ws == gs, "schema record mismatch"
                compare_dumps(wd, gd, decay=decay)
        elif key.endswith(".bak"):
            assert want[key]["bytes"] == got[key]["bytes"] and want[key]["mtime"] // 1000 == got[key]["mtime"] // 1000
        else:
            file = "data/.last-profile" if key == "data/.last-profile" else key
            raw = expected_for_run(want[key]["bytes"], file, oracle_root, actual_root, "raw-text")
            assert raw == got[key]["bytes"], f"non-database bytes mismatch: {key}"


def assert_decay_margin(path: Path, moment: str) -> None:
    """Reject generated fixtures before either run; old half-way fixtures stay frozen."""
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute("SELECT record_id,domain,created_at,stability,accessibility,last_accessed_at FROM memory_current_v3").fetchall()
    now = datetime.fromisoformat(moment)
    from trajecta_identity.identity import PINNED
    state = path.with_suffix(".activation.json")
    last_run = json.loads(state.read_text()).get("last_decay_at") if state.exists() else None
    for record_id, domain, created, stability, old, accessed in rows:
        if record_id in PINNED or domain == "anchor":
            continue
        created = max(x for x in (created, accessed, last_run) if x)
        age = max(0.0, (now - datetime.fromisoformat(created)).total_seconds() / 86400.0)
        raw = min(0.9, old * 0.5 ** (age / (21.0 * (0.5 + stability))))
        if abs(abs(raw - old) - 1e-6) < 1e-9 or abs(raw * 1e6 % 1 - 0.5) / 1e6 < 1e-9:
            raise ValueError("generated decay case too close to boundary")
