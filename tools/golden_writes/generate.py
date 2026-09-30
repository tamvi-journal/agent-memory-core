"""Generate the deterministic R2b public-writer oracle corpus."""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from memory_core import GovernancePolicy, MemoryStore, ValidatedIntake  # noqa: E402
from tools.golden.generate import clock_context, dump_database, legacy_v2, legacy_v3  # noqa: E402
from trajecta_identity import IdentityMemory, load_profile  # noqa: E402
from trajecta_identity.identity import PINNED  # noqa: E402

ORACLE_BASE_COMMIT = "bb86dbf0e6ffc968d6320a91705dc9b4f629dd15"
ORACLE_SEMANTICS = ["R0-frozen", "R2a-authority-v2", "P4-decay-v4-refusal", "P5-writer-v4-precheck"]
TABLES = ROOT / "memory_core" / "tables"
LEGACY_V4 = ROOT / "spec" / "golden" / "identity-open" / "store.sqlite3"
SOURCES = (
    "memory_core/*.py", "memory_core/schema.sql", "trajecta_identity/*.py",
    "trajecta_identity/profiles/example/profile.json", "tools/golden_writes/generate.py",
)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_json(path: Path, value: Any, *, ordered: bool = False) -> None:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=not ordered)
    path.write_text(text + "\n", encoding="utf-8")


def resolve_refs(value: Any, saved: dict[str, Any]) -> Any:
    if isinstance(value, dict) and set(value) == {"$ref"}:
        current: Any = saved
        for part in value["$ref"].split("."):
            current = current[part]
        return current
    if isinstance(value, dict):
        return {key: resolve_refs(item, saved) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_refs(item, saved) for item in value]
    return value


def ref(name: str, field: str) -> dict[str, str]:
    return {"$ref": f"{name}.{field}"}


def evidence(label: str, confidence: float = 0.9, **extra: Any) -> dict[str, Any]:
    return {
        "evidence_type": extra.pop("evidence_type", "synthetic"),
        "source_ref": f"golden:{label}", "content_summary": label,
        "confidence": confidence, "privacy_class": "synthetic", **extra,
    }


def proposal(record_id: str, key: str, *, operation="create", record_class="belief", domain="fact", confidence=0.9, **extra):
    result = {
        "operation_type": operation, "record_id": record_id,
        "record_class": record_class if operation == "create" else None,
        "domain": domain if operation == "create" else None,
        "actor": "golden", "reason": extra.pop("reason", "golden proposal"),
        "logic": "bounded fixture", "truth_basis": "golden source",
        "evidence": extra.pop("evidence", [evidence(key, confidence)]),
        "idempotency_key": key, "changes": extra.pop("changes", {"title": record_id, "summary": key}),
    }
    result.update(extra)
    return result


def scripts() -> dict[str, dict[str, Any]]:
    base = [{"call": "bootstrap", "save": "boot"}]
    return {
        "bootstrap": {"actions": [*base, {"call": "bootstrap"}, {"call": "bootstrap_invalid", "expect_error": "ValueError"}]},
        "phase": {"actions": [
            *base,
            {"call": "log_phase", "save": "p1", "arguments": {
                "event_id": "first", "title": "First phase", "summary": "first summary", "content": "body",
                "decided_because": "evidence", "cues": ["first light", ""], "phase_context": {"2": 2, "1": 1.0, "model": "orácle"},
                "occurred_at": "2026-09-29T23:59:00+00:00", "evidence": [{"source_ref": "outside:one", "content_summary": "outside", "confidence": 0.91}],
            }},
            {"call": "log_phase", "arguments": {"event_id": "first", "title": "changed", "summary": "changed"}},
            {"call": "log_phase", "arguments": {"event_id": "second", "title": "Second phase", "summary": "linked", "follows": ["phase:first"], "caused_by": ["phase:first"], "depends_on": ["phase:first"], "open_loop": True}},
            {"call": "log_phase", "arguments": {"event_id": "orphan", "title": "Orphan", "summary": "bad", "follows": ["phase:missing"]}, "expect_error": "ValueError"},
            {"call": "log_phase", "arguments": {"event_id": "bad id", "title": "Bad", "summary": "bad"}, "expect_error": "ValueError"},
        ]},
        "work-refs": {"work_fixture": "valid-torn", "actions": [
            *base,
            {"call": "log_phase", "arguments": {"event_id": "work", "title": "Work", "summary": "linked work", "work_refs": ["work:12345678-abcd", "delta:abcdef12-3456"]}},
            {"call": "log_phase", "arguments": {"event_id": "unknown-work", "title": "Unknown", "summary": "bad", "work_refs": ["work:ffffffff-ffff"]}, "expect_error": "ValueError"},
        ]},
        "work-wrong-schema": {"work_fixture": "wrong-schema", "actions": [
            *base, {"call": "log_phase", "arguments": {"event_id": "bad-work", "title": "Bad", "summary": "bad", "work_refs": ["work:12345678-abcd"]}, "expect_error": "WorkStoreError"},
        ]},
        "work-no-root": {"actions": [*base, {"call": "log_phase", "arguments": {"event_id": "plain-work", "title": "Plain", "summary": "text only", "work_refs": ["work:12345678-abcd"]}}]},
        "fact-loop": {"actions": [
            *base,
            {"call": "log_phase", "arguments": {"event_id": "loop", "title": "Loop", "summary": "open", "open_loop": True}},
            {"call": "log_fact", "arguments": {"fact_id": "belief", "title": "Belief", "summary": "v1", "caused_by": ["phase:loop"], "cues": ["belief cue"]}},
            {"call": "log_fact", "arguments": {"fact_id": "belief", "title": "Belief", "summary": "v1", "caused_by": ["phase:loop"]}},
            {"call": "log_fact", "arguments": {"fact_id": "belief", "title": "Belief", "summary": "v2"}},
            {"call": "log_fact", "arguments": {"fact_id": "int-confidence", "title": "Typed", "summary": "integer", "confidence": 1}},
            {"call": "log_fact", "arguments": {"fact_id": "float-confidence", "title": "Typed", "summary": "float", "confidence": 1.0}},
            {"call": "runtime_submit", "arguments": {"proposal": {"$pinned": True}}, "expect_error": "PinnedRecordError"},
            {"call": "close_loop", "arguments": {"record_id": "phase:loop", "note": "done"}},
            {"call": "close_loop", "arguments": {"record_id": "phase:loop", "note": "again"}},
        ]},
        "intake": {"actions": [
            {"call": "initialize"},
            {"call": "intake_submit", "arguments": {"proposal": proposal("low", "low", confidence=0.5)}},
            {"call": "intake_submit", "arguments": {"proposal": proposal("axis-no-falsifier", "axis-no-f", record_class="axis", domain="logic", evidence=[evidence("a"), evidence("b")])}},
            {"call": "intake_submit", "arguments": {"proposal": proposal("axis-one-source", "axis-one", record_class="axis", domain="logic", falsifier="counterexample")}},
            {"call": "intake_submit", "arguments": {"proposal": proposal("conflict", "conflict", unresolved_conflict=True)}},
            {"call": "intake_submit", "save": "boundary", "arguments": {"proposal": proposal("boundary", "boundary", domain="boundary", confidence=0.9)}},
            {"call": "intake_submit", "arguments": {"proposal": proposal("boundary", "boundary-revise", operation="refine", protected_effect="weaken", changes={"summary": "weaker"})}},
            {"call": "intake_submit", "arguments": {"proposal": proposal("boundary", "boundary-neutral", operation="refine", protected_effect="neutral", changes={"summary": "neutral"})}},
            {"call": "intake_submit", "save": "normal", "arguments": {"proposal": proposal("normal", "normal")}},
            {"call": "intake_submit", "arguments": {"proposal": proposal("normal", "normal-noop", operation="refine", changes={"title": "normal", "summary": "normal"})}},
            {"call": "intake_submit", "arguments": {"proposal": proposal("normal", "normal", operation="refine", changes={"summary": "different"})}, "expect_error": "ValueError"},
            # Evidence identity law (Lam, R2b post-merge): independence is decided on
            # canonical_evidence_identity (frozen identity-v1 table, str.strip), the same
            # identity the evidence rows store. Unicode- or whitespace-equivalent sources
            # are one source; a genuinely distinct pair still materializes.
            {"call": "intake_submit", "arguments": {"proposal": proposal("axis-accent", "axis-accent", record_class="axis", domain="logic", falsifier="counterexample", evidence=[evidence("accent-a", source_ref="caf\u00e9"), evidence("accent-b", source_ref="cafe")])}},
            {"call": "intake_submit", "arguments": {"proposal": proposal("axis-group", "axis-group", record_class="axis", domain="logic", falsifier="counterexample", evidence=[evidence("group-a", independence_group="Group \u00c4"), evidence("group-b", independence_group="group-a")])}},
            {"call": "intake_submit", "arguments": {"proposal": proposal("axis-space", "axis-space", record_class="axis", domain="logic", falsifier="counterexample", evidence=[evidence("space-a", source_ref=" golden:tea "), evidence("space-b", source_ref="golden:tea")])}},
            {"call": "intake_submit", "arguments": {"proposal": proposal("axis-distinct", "axis-distinct", record_class="axis", domain="logic", falsifier="counterexample", evidence=[evidence("distinct-a", source_ref="cafe"), evidence("distinct-b", source_ref="golden:tea")])}},
        ]},
        "kernel": {"actions": [
            *base,
            {"call": "log_phase", "arguments": {"event_id": "aa", "title": "A", "summary": "A"}},
            {"call": "log_phase", "arguments": {"event_id": "bb", "title": "B", "summary": "B"}},
            {"call": "add_relation", "arguments": {"relation_id": "a-b", "from_record_id": "phase:aa", "to_record_id": "phase:bb", "relation_type": "depends-on", "weight": 1.0}},
            {"call": "add_relation", "arguments": {"relation_id": "a-b", "from_record_id": "phase:aa", "to_record_id": "phase:bb", "relation_type": "depends-on", "weight": 1.0}},
            {"call": "add_relation", "arguments": {"relation_id": "bad", "from_record_id": "phase:aa", "to_record_id": "phase:aa", "relation_type": "depends-on"}, "expect_error": "ValueError"},
            {"call": "add_relation", "arguments": {"relation_id": "bad-weight", "from_record_id": "phase:aa", "to_record_id": "phase:bb", "relation_type": "depends-on", "weight": 11}, "expect_error": "ValueError"},
            {"call": "add_cue", "arguments": {"profile": "example", "cue": "mutable", "target_record_id": "phase:aa", "weight": 1.0}},
            {"call": "add_cue", "arguments": {"profile": "example", "cue": "mutable", "target_record_id": "phase:aa", "weight": 2.0}},
            {"call": "record_access", "arguments": {"cue": "a", "record_id": "phase:aa", "revision_id": {"$current": "phase:aa"}, "retrieval_reason": "cue:a", "rank": 1, "surface": "golden", "gain": -0.1}, "expect_error": "ValueError"},
            {"call": "maintenance", "arguments": {"run_id": "clamp", "adjustments": [{"record_id": "phase:aa", "field": "accessibility", "old_value": 0.6, "new_value": 2}], "actor": "golden", "reason": "clamp"}},
            {"call": "maintenance", "arguments": {"run_id": "skip", "adjustments": [{"record_id": "phase:aa", "field": "accessibility", "old_value": 1.0, "new_value": 1.0000000001}], "actor": "golden", "reason": "skip"}},
            {"call": "maintenance", "arguments": {"run_id": "old-mismatch", "adjustments": [{"record_id": "phase:aa", "field": "accessibility", "old_value": 0.2, "new_value": 0.3}], "actor": "golden", "reason": "bad"}, "expect_error": "ValueError"},
            {"call": "maintenance", "arguments": {"$semantic_drift": True, "run_id": "semantic-drift", "adjustments": [{"record_id": "phase:aa", "field": "accessibility", "old_value": 1.0, "new_value": 0.5}], "actor": "golden", "reason": "drift"}, "expect_error": "RuntimeError"},
        ]},
        "recall": {"actions": [
            *base,
            {"call": "log_phase", "arguments": {"event_id": "direct", "title": "Direct", "summary": "direct", "cues": ["wake direct"]}},
            {"call": "log_phase", "arguments": {"event_id": "graph", "title": "Graph", "summary": "graph", "follows": ["phase:direct"]}},
            {"call": "maintenance", "arguments": {"run_id": "recall-fixture", "adjustments": [{"record_id": "phase:direct", "field": "accessibility", "old_value": 0.6, "new_value": 0.1}, {"record_id": "phase:graph", "field": "accessibility", "old_value": 0.6, "new_value": 0.89}], "actor": "golden", "reason": "fixture"}},
            {"call": "retrieve", "arguments": {"cue": "wake direct", "track": True, "limit": 10}},
            {"call": "retrieve", "arguments": {"cue": "wake direct", "track": True, "limit": 10}},
        ]},
        "decay": {"actions": [
            *base,
            {"call": "log_phase", "arguments": {"event_id": "decay", "title": "Decay", "summary": "decay"}},
            {"call": "log_phase", "arguments": {"event_id": "cap", "title": "Cap", "summary": "cap"}},
            {"call": "maintenance", "arguments": {"run_id": "decay-fixture", "adjustments": [{"record_id": "phase:cap", "field": "accessibility", "old_value": 0.6, "new_value": 1.0}], "actor": "golden", "reason": "fixture"}},
            {"call": "decay", "arguments": {"now": "2026-10-21T00:00:00+00:00"}},
            {"call": "decay", "arguments": {"now": "2026-10-22T00:00:00.000500+00:00"}},
        ]},
        "decay-boundary": {"actions": [
            *base,
            {"call": "log_phase", "arguments": {"event_id": "boundary", "title": "Boundary", "summary": "threshold margin"}},
            {"call": "decay", "arguments": {"now": "2026-09-30T00:00:35+00:00"}},
        ]},
        "decay-half": {"actions": [
            *base,
            {"call": "log_phase", "arguments": {"event_id": "half", "title": "Half", "summary": "round6 halfway"}},
            {"call": "decay", "arguments": {"now": "2026-11-28T22:43:14+00:00"}},
        ]},
    }


def prepare_work(root: Path, fixture: str) -> None:
    root.mkdir()
    if fixture == "wrong-schema":
        write_json(root / "state.json", {"schema": "wrong", "work": []}, ordered=True)
        return
    state = {"schema": "trajecta.state/v1", "work": [{
        "id": "work:12345678-abcd", "topic": "R2b", "goal": "parity", "status": "active", "revision": 3,
        "nextAction": "replay", "openLoops": ["crash"], "activeBranchId": "branch:one", "branches": [{"id": "branch:one", "label": "writers"}],
        "updatedAt": "2026-09-30T00:00:00Z",
    }]}
    write_json(root / "state.json", state, ordered=True)
    delta = {"id": "delta:abcdef12-3456", "workId": "work:12345678-abcd", "kind": "progress", "summary": "ported", "revision": 3, "createdAt": "2026-09-30T00:00:01Z"}
    (root / "deltas.jsonl").write_text(canonical(delta) + "\n{\"torn\":\n", encoding="utf-8")


def operation_count(mem: IdentityMemory) -> int:
    with mem.store.connect(readonly=True) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM memory_operations_v3").fetchone()[0])


def portable_result(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("store.sqlite3" if key == "db" else portable_result(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [portable_result(item) for item in value]
    return value


def resolve_special(value: Any, mem: IdentityMemory) -> Any:
    if isinstance(value, dict) and set(value) == {"$current"}:
        return mem.store.current_view(value["$current"])[0]["revision_id"]
    if isinstance(value, dict): return {k: resolve_special(v, mem) for k, v in value.items()}
    if isinstance(value, list): return [resolve_special(v, mem) for v in value]
    return value


def invoke(mem: IdentityMemory, call: str, args: dict[str, Any]):
    args = resolve_special(args, mem)
    if call == "initialize": return mem.store.initialize()
    if call == "bootstrap": return mem.bootstrap()
    if call == "bootstrap_invalid":
        bad = dataclasses.replace(mem.profile, core={**mem.profile.core, "falsifier": ""})
        return IdentityMemory(bad, mem.db_path, surface=mem.surface).bootstrap()
    if call == "log_phase":
        event = args.pop("event_id"); return mem.log_phase(event, **args)
    if call == "log_fact":
        fact = args.pop("fact_id"); return mem.log_fact(fact, **args)
    if call == "close_loop": return mem.close_loop(**args)
    if call == "runtime_submit":
        if args["proposal"].pop("$pinned", False):
            args["proposal"] = proposal("core", "pinned", record_class="belief", domain="fact")
        return mem.runtime.submit(**args["proposal"])
    if call == "intake_submit": return ValidatedIntake(mem.store, surface="golden", policy=GovernancePolicy()).submit(**args["proposal"])
    if call == "add_relation": return mem.store.add_relation(**args)
    if call == "add_cue": return mem.store.add_cue(**args)
    if call == "record_access": return mem.store.record_access(**args)
    if call == "maintenance":
        drift = args.pop("$semantic_drift", False)
        if not drift:
            return mem.store.apply_maintenance(**args)
        def mutate_semantic_hash(conn):
            conn.execute("DROP TRIGGER memory_revisions_v3_no_update")
            conn.execute("UPDATE memory_revisions_v3 SET content_sha256='drifted' WHERE record_id='phase:aa'")
        mem.store._test_before_maintenance_hash_check = mutate_semantic_hash
        try:
            return mem.store.apply_maintenance(**args)
        finally:
            del mem.store._test_before_maintenance_hash_check
    if call == "retrieve": return mem.retrieve(**args)
    if call == "decay": return mem.decay(**args)
    raise AssertionError(call)


def execute(mem: IdentityMemory, script: dict[str, Any]) -> list[dict[str, Any]]:
    saved: dict[str, Any] = {}
    results = []
    for index, raw in enumerate(script["actions"]):
        action = resolve_refs(raw, saved)
        expected = action.get("expect_error")
        before = mem.db_path.read_bytes() if expected and mem.db_path.exists() else None
        before_ops = operation_count(mem) if expected and mem.db_path.exists() else 0
        try:
            result = invoke(mem, action["call"], dict(action.get("arguments", {})))
        except Exception as exc:
            if type(exc).__name__ != expected: raise
            unchanged = before is None or mem.db_path.read_bytes() == before
            no_operation = not mem.db_path.exists() or operation_count(mem) == before_ops
            if not unchanged or not no_operation:
                raise AssertionError(f"negative action mutated store: {action['call']}")
            message = str(exc).replace(str(mem.db_path), "store.sqlite3")
            if mem.work is not None:
                message = message.replace(str(mem.work.root), "<work-root>")
            result = {"error": type(exc).__name__, "message": message, "store_bytes_unchanged": True, "no_operation": True}
        else:
            if expected: raise AssertionError(f"expected {expected}: {action['call']}")
        result = portable_result(result)
        if action.get("save"): saved[action["save"]] = result
        results.append({"index": index, "call": action["call"], "result": result})
    return results


def legacy_scenario(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    shutil.copyfile(LEGACY_V4, path)
    mem = IdentityMemory(load_profile("example"), path, surface="golden")
    script = {"source": "spec/golden/identity-open/store.sqlite3", "actions": []}
    calls = [
        ("decay-zero", lambda: mem.decay("2026-09-30T00:00:00+00:00")),
        ("decay-adjust", lambda: mem.decay("2100-01-01T00:00:00+00:00")),
        ("bootstrap-exists", mem.bootstrap),
        ("fact-noop", lambda: mem.log_fact("belief", title="Held fact", summary="after", content="")),
        ("close-noop", lambda: mem.close_loop("phase:not-open", note="done")),
        ("relation-noop", lambda: mem.store.add_relation(relation_id="phase:first->open-loop->anchor:open-loops", from_record_id="phase:first", to_record_id="anchor:open-loops", relation_type="open-loop", weight=1.0)),
        ("maintenance-replay", lambda: mem.store.apply_maintenance(run_id="fixture:fact:belief:accessibility:0x1.3333333333333p-1", adjustments=[], actor="golden", reason="replay")),
    ]
    results = []
    for index, (name, call) in enumerate(calls):
        before = path.read_bytes(); listing = sorted(p.name for p in path.parent.iterdir())
        try: call()
        except Exception as exc:
            if type(exc).__name__ != "MigrationRequiredError": raise
            if path.read_bytes() != before or sorted(p.name for p in path.parent.iterdir()) != listing: raise AssertionError(name)
            result = {"error": type(exc).__name__, "message": str(exc), "store_bytes_unchanged": True, "directory_unchanged": True, "no_operation": True}
        else: raise AssertionError(name)
        script["actions"].append({"call": name, "expect_error": "MigrationRequiredError"})
        results.append({"index": index, "call": name, "result": result})
    return script, results


def migration_scenario(path: Path, version: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source = path.with_name(f"source-v{version}.sqlite3")
    if version == 2: legacy_v2(source)
    elif version == 3: legacy_v3(source)
    else: shutil.copyfile(LEGACY_V4, source)
    original = source.read_bytes()
    refusals = []
    for kind in ("wal", "foreign", "future"):
        guarded = path.with_name(f"guard-{kind}-v{version}.sqlite3")
        shutil.copyfile(source, guarded)
        if kind == "wal":
            Path(str(guarded) + "-wal").write_bytes(b"")
        else:
            payload = bytearray(guarded.read_bytes())
            offset, value = (68, 123) if kind == "foreign" else (60, 99)
            payload[offset:offset + 4] = value.to_bytes(4, "big")
            guarded.write_bytes(payload)
        guarded_before = guarded.read_bytes()
        guarded_target = path.with_name(f"guard-target-{kind}-v{version}.sqlite3")
        try:
            MemoryStore(guarded).migrate_to(guarded_target)
        except Exception as exc:
            refusals.append(f"{kind}:{type(exc).__name__}")
        else:
            raise AssertionError(f"{kind} migration source was accepted")
        assert guarded.read_bytes() == guarded_before and not guarded_target.exists()
        guarded.unlink()
        Path(str(guarded) + "-wal").unlink(missing_ok=True)
    store = MemoryStore(source)
    dry_target = path.with_name("dry.sqlite3")
    dry = store.migrate_to(dry_target, dry_run=True)
    assert not dry_target.exists()
    backup = path.with_name(f"backup-v{version}.sqlite3")
    migrated = store.migrate_to(path, backup_path=backup)
    assert source.read_bytes() == original and backup.read_bytes() == original
    result = {"dry_run": dry, "preflight_refusals": refusals, "source_unchanged": True, "backup_name": backup.name, "backup_byte_identical": True, "state": migrated.schema_info()["state"]}
    source.unlink(); backup.unlink()
    return {"source_version": version, "actions": [{"call": "migrate", "version": version}]}, [{"index": 0, "call": "migrate", "result": result}]


def assert_decay_margins(path: Path, *, boundary: bool = False, require_half: bool = False) -> None:
    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT details_json FROM memory_operations_v3 WHERE idempotency_key LIKE 'maintenance:decay:%'").fetchall()
    saw = False
    for (text,) in rows:
        for item in json.loads(text)["adjustments"]:
            saw = True
    if not saw: raise AssertionError("decay fixture produced no adjustments")
    if boundary or require_half:
        with sqlite3.connect(path) as conn:
            record_id = "phase:boundary" if boundary else "phase:half"
            row = conn.execute(
                "SELECT r.created_at,t.stability,o.idempotency_key,d.value "
                "FROM memory_revisions_v3 r JOIN memory_telemetry_v3 t USING(revision_id) "
                "JOIN memory_operations_v3 o ON o.idempotency_key LIKE 'maintenance:decay:%' "
                "JOIN json_each(o.details_json,'$.adjustments') d "
                "WHERE r.record_id=? LIMIT 1", (record_id,)
            ).fetchone()
        from datetime import datetime
        created, stability, key, adjustment = row
        moment = datetime.fromisoformat(key.removeprefix("maintenance:decay:"))
        start = datetime.fromisoformat(created)
        old = float(json.loads(adjustment)["old_value"])
        raw = old * 0.5 ** (((moment - start).total_seconds() / 86400.0) / (21.0 * (0.5 + stability)))
        if boundary and abs(abs(raw - old) - 1e-6) < 1e-9:
            raise AssertionError("decay fixture is too close to decision boundary")
        if abs((raw * 1_000_000) % 1.0 - 0.5) > 1e-7:
            if require_half:
                raise AssertionError("decay round6 fixture is not close enough to halfway")


def generate(output: Path) -> None:
    if sys.version_info[:2] != (3, 11) or unicodedata.unidata_version != "14.0.0":
        raise SystemExit("writes corpus generation requires Python 3.11 / Unicode 14.0.0")
    if output.exists() and any(output.iterdir()): raise SystemExit(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    all_scripts = scripts()
    with tempfile.TemporaryDirectory(prefix="trajecta-writes-v5-") as temp:
        workspace = Path(temp)
        for name in sorted([*all_scripts, "legacy-v4", "migration-v2", "migration-v3", "migration-v4"]):
            scenario = output / name; scenario.mkdir()
            path = workspace / f"{name}.sqlite3"
            with clock_context():
                if name == "legacy-v4": script, results = legacy_scenario(path)
                elif name.startswith("migration-"): script, results = migration_scenario(path, int(name[-1]))
                else:
                    script = all_scripts[name]
                    work = workspace / f"{name}-work"
                    fixture = script.get("work_fixture")
                    if fixture: prepare_work(work, fixture)
                    profile = load_profile("example")
                    if fixture: profile = dataclasses.replace(profile, extra={**profile.extra, "work_root": str(work)})
                    mem = IdentityMemory(profile, path, surface="golden")
                    results = execute(mem, script)
            if name.startswith("decay"):
                assert_decay_margins(path, boundary=name == "decay-boundary", require_half=name == "decay-half")
            shutil.copyfile(path, scenario / "store.sqlite3")
            write_json(scenario / "script.json", script, ordered=True)
            write_json(scenario / "dump.json", dump_database(path))
            (scenario / "cases.jsonl").write_text(canonical({"label": name, "results": results}) + "\n", encoding="utf-8")
    source_paths = sorted({p for pattern in SOURCES for p in ROOT.glob(pattern)})
    sources = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    corpus = sorted(p for p in output.rglob("*") if p.is_file())
    files = {p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in corpus}
    for table in sorted(TABLES.glob("*.json")): files[f"tables/{table.name}"] = hashlib.sha256(table.read_bytes()).hexdigest()
    manifest = {
        "schema": "trajecta.golden-writes-manifest/v1", "oracle_base_commit": ORACLE_BASE_COMMIT,
        "oracle_semantics": ORACLE_SEMANTICS, "oracle_sources": sources,
        "python": sys.version.split()[0], "unicode": unicodedata.unidata_version, "sqlite": sqlite3.sqlite_version,
        "legacy_v4_sha256": hashlib.sha256(LEGACY_V4.read_bytes()).hexdigest(), "files": dict(sorted(files.items())),
    }
    write_json(output / "MANIFEST.json", manifest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--output", type=Path, default=ROOT / "spec/golden-writes-v5")
    generate(parser.parse_args().output)
