import json
from pathlib import Path
import shutil

import pytest

from tools.r3.common import isolated_env, schema_and_dump
from tools.r3.crashes import intake_case
from tools.r3.run import run_case
from tools.r3.replay import replay_units

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("outcome", ["materialized", "held", "no_op"])
def test_g2_units_are_actual_oracle_commits_and_replay_exactly(tmp_path, outcome):
    fixture = ROOT / "spec/golden-writes-v5/bootstrap/store.sqlite3"
    if outcome == "no_op":
        seed = tmp_path / "seed"
        isolated_env(seed)
        shutil.copy2(fixture, seed / "store.sqlite3")
        run_case("py", seed, {"surface": "kernel", "call": "log_phase", "arguments": {
            "event_id": "base", "title": "Synthetic", "summary": "synthetic"}})
        fixture = seed / "store.sqlite3"
    updates = {"unresolved_conflict": True} if outcome == "held" else {}
    if outcome == "no_op": updates = {"operation_type": "refine", "record_id": "phase:base", "changes": {}}
    outputs = []
    for runtime in ("py", "ts"):
        root = tmp_path / runtime
        isolated_env(root)
        shutil.copy2(fixture, root / "store.sqlite3")
        result = run_case(runtime, root, {**intake_case(**updates), "capture": True})
        assert json.loads(result["stdout"])["status"] == outcome
        calls = [u["call"] for u in result["units"]]
        assert calls == (["_capture_evidence", "_insert_intake", "create_current", "_link_evidence", "_decide_intake"]
                         if outcome == "materialized" else ["_capture_evidence", "_insert_intake", "_decide_intake"])
        replay = replay_units(fixture, tmp_path / (runtime + "-replay"), result["units"])
        assert schema_and_dump(replay) == schema_and_dump(root / "store.sqlite3")
        outputs.append(result)
    assert outputs[0] == outputs[1]
