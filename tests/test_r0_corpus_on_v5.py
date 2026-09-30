"""The frozen R0 read corpus (v4) must still pass on the v5 Python readers.

R2a spec §2.1, additive-field rule: the runner asserts each field the settlement
changed, removes or pins exactly those, and compares everything else against the
frozen expected output. The list below is closed; any other difference fails.
"""
from __future__ import annotations

import json
import shutil
import sys
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "spec" / "golden"
sys.path.insert(0, str(ROOT / "tools" / "golden"))

from generate import sample  # noqa: E402

from memory_core.store import MemoryStore  # noqa: E402
from trajecta_identity.identity import IdentityMemory  # noqa: E402
from trajecta_identity.profile import load_profile  # noqa: E402

# Settlement-changed fields (R2a spec §2.1). Closed list.
V5_WRITE_POLICY = "self-authored proposals; owner receipt controls canonical core"
V5_LEGACY_V3_MESSAGE = "schema v3 store must be initialized or migrated to v5 before use"


def _canon(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _cases():
    for scenario in sorted(p for p in GOLDEN.iterdir() if p.is_dir()):
        for line in (scenario / "cases.jsonl").read_text(encoding="utf-8").splitlines():
            case = json.loads(line)
            yield pytest.param(scenario.name, case, id=f"{scenario.name}/{case['label']}")


def _frozen_clock(packet: str | None):
    stack = ExitStack()
    if not packet:
        return stack
    stamp = packet.split("Generated: ", 1)[1].split("\n", 1)[0]
    moment = datetime.fromisoformat(stamp)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz is None else moment.astimezone(tz)

    for module in ("memory_core.packet", "trajecta_identity.identity"):
        stack.enter_context(patch(f"{module}.datetime", Frozen))
    return stack


def _ordinary(path: Path, expected: dict) -> dict:
    inp = expected["input"]
    history = expected["historical_view"]
    kwargs = dict(limit=inp["limit"], budget=inp["token_budget"],
                  history=inp["include_history"], scope=inp["scope"],
                  history_id=history[0]["record_id"] if history else None)
    packet = expected["packet_text"] or (expected["identity_packet_json"] or {}).get("packet")
    mem = IdentityMemory(load_profile("example"), path, surface="golden")
    with _frozen_clock(packet):
        actual = sample(mem, expected["label"], inp["cue"], **kwargs)
    # packet_text and identity packet may carry different Generated lines.
    if expected["identity_packet_json"] and expected["identity_packet_json"].get("packet"):
        with _frozen_clock(expected["identity_packet_json"]["packet"]):
            again = sample(mem, expected["label"], inp["cue"], **kwargs)
        actual["identity_packet_json"] = again["identity_packet_json"]
    return actual


@pytest.mark.parametrize(("scenario", "expected"), list(_cases()))
def test_r0_case_on_v5_readers(tmp_path, scenario, expected):
    source = GOLDEN / scenario / "store.sqlite3"
    source_before = source.read_bytes()
    path = tmp_path / "store.sqlite3"
    shutil.copyfile(source, path)
    copy_before = path.read_bytes()
    oracle = json.loads(json.dumps(expected))

    if "result" in expected:
        store = MemoryStore(path)
        label = expected["label"]
        if label in {"migrated-v2", "migrated-v3"}:
            result = {"status": "migrated", "current_view": store.current_view()}
            if label == "migrated-v3":
                result["active_relations"] = store.active_relation_rows()
            result["source_byte_identical"] = True
        else:
            with pytest.raises(Exception) as caught:
                store.current_view()
            result = {"error": type(caught.value).__name__, "message": str(caught.value)}
            if label == "legacy-v3":
                result["byte_identical_after_read"] = True
                assert oracle["result"]["message"] != V5_LEGACY_V3_MESSAGE
                oracle["result"]["message"] = V5_LEGACY_V3_MESSAGE
        assert result == oracle["result"]
    else:
        actual = _ordinary(path, expected)
        oracle.pop("write_outcomes", None)
        # Additive fields: assert their v4 value, then remove exactly them.
        identity = actual["identity_packet_json"]
        if "error" not in identity:
            assert identity.pop("open_core_proposals") == []
        assert actual["status"].pop("open_core_proposals") == 0
        # Settlement-changed fields: v4 store is now read-only legacy.
        assert oracle["status"]["store"] == "ready"
        assert actual["status"]["store"] == "legacy-v4"
        assert actual["status"]["write_policy"] == V5_WRITE_POLICY
        assert oracle["status"]["write_policy"] != V5_WRITE_POLICY
        for key in ("store", "write_policy"):
            actual["status"][key] = oracle["status"][key]
        assert _canon(actual) == _canon(oracle)

    assert path.read_bytes() == copy_before, "read changed the copied store"
    assert source.read_bytes() == source_before, "read changed the checked-in store"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["store.sqlite3"]
