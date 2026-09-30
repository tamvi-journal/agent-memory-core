"""Generate the deterministic authority-v2 Python oracle corpus."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from memory_core import MemoryStore, PinnedRecordError  # noqa: E402
from memory_core.store import hash_payload  # noqa: E402
from tools.golden.generate import clock_context, dump_database  # noqa: E402
from trajecta_identity import IdentityMemory, load_profile  # noqa: E402
from trajecta_identity.identity import PINNED  # noqa: E402

ORACLE_COMMIT = "9b22b4acae7e016cf28548dbdeae72420d493d98"
TABLES = ROOT / "memory_core" / "tables"
LEGACY_V4 = ROOT / "spec" / "golden" / "identity-open" / "store.sqlite3"
SOURCES = (
    "memory_core/*.py", "memory_core/schema.sql", "trajecta_identity/*.py",
    "trajecta_identity/profiles/example/profile.json", "tools/golden_authority/generate.py",
)


class TTY:
    def __init__(self, value: str, present: bool = True):
        self.value = value
        self.present = present
        self.output = ""

    def isatty(self): return self.present
    def readline(self): return self.value + "\n"
    def write(self, value): self.output += value; return len(value)
    def flush(self): pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(canonical(value) + "\n", encoding="utf-8")


def write_script(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")


def short(value: str) -> str:
    return value.split(":", 1)[-1][:12]


def resolve_refs(value: Any, saved: dict[str, Any]) -> Any:
    if isinstance(value, dict) and set(value) == {"$ref"}:
        parts = value["$ref"].split(".")
        current = saved[parts[0]]
        for part in parts[1:]: current = current[part]
        return current
    if isinstance(value, dict):
        return {key: resolve_refs(item, saved) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_refs(item, saved) for item in value]
    return value


def operation_count(mem: IdentityMemory) -> int:
    with mem.store.connect(readonly=True) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM memory_operations_v3").fetchone()[0])


def drop_guards(conn: sqlite3.Connection, table: str) -> None:
    conn.execute(f"DROP TRIGGER IF EXISTS {table}_no_update")
    conn.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete")


def invoke(mem: IdentityMemory, call: str, args: dict[str, Any], saved: dict[str, Any]):
    args = resolve_refs(args, saved)
    if call == "bootstrap":
        return mem.bootstrap()
    if call == "identity_core_propose":
        return mem.identity_core_propose(**args)
    if call == "owner_approve_core":
        proposal_id = args["proposal_id"]
        outcome = args["outcome"]
        mode = args.get("confirmation", "correct")
        expected = f"{outcome.upper()} {short(proposal_id)}"
        terminal = TTY(expected if mode == "correct" else str(mode), args.get("tty", True))
        return mem.issue_core_receipt(
            proposal_id, outcome=outcome, decision_note=args.get("decision_note", ""),
            stdin=terminal, stdout=terminal,
        )
    if call == "identity_core_apply":
        return mem.identity_core_apply(args["receipt_id"])
    if call == "fixture_log_phase":
        event_id = args.pop("event_id")
        return mem.log_phase(event_id, **args)
    if call == "owner_approve_retract":
        record_id = args["record_id"]
        terminal = TTY(f"RETRACT {record_id}" if args.get("confirmation", "correct") == "correct" else str(args["confirmation"]), args.get("tty", True))
        return mem.issue_retract_receipt(
            record_id, reason=args["reason"], stdin=terminal, stdout=terminal
        )
    if call == "identity_retract":
        return mem.identity_retract(args["receipt_id"])
    if call == "identity_status":
        result = mem.status(); result["db"] = "store.sqlite3"; return result
    if call == "identity_packet":
        return mem.retrieve(args.get("cue", "who are you"), track=False)
    if call == "apply_core_as_profile":
        other = IdentityMemory(
            load_profile("example").__class__(
                **{**load_profile("example").__dict__, "name": args["profile"]}
            ), mem.db_path, surface="golden"
        )
        return other.identity_core_apply(args["receipt_id"])
    if call == "apply_retract_wrong_purpose":
        return mem.identity_retract(args["receipt_id"])
    if call == "public_writer_pinned":
        record_id, writer = args["record_id"], args["writer"]
        evidence = {"source_ref": "negative:pinned", "content_summary": "negative"}
        if writer == "create":
            return mem.store.create_current(
                record_id=record_id, record_class="axis", domain="core", title="bad",
                actor="negative", reason="negative", evidence=evidence,
                idempotency_key=f"negative:create:{record_id}",
            )
        if writer == "revise":
            return mem.store.revise(
                record_id, operation_type="refine", actor="negative", reason="negative",
                evidence=evidence, idempotency_key=f"negative:revise:{record_id}",
                changes={"summary": "bad"},
            )
        return mem.store.invalidate(
            record_id, actor="negative", reason="negative", evidence=evidence,
            idempotency_key=f"negative:invalidate:{record_id}",
        )
    if call == "runtime_submit_pinned":
        return mem.runtime.submit(
            operation_type="create", record_id=args["record_id"],
            record_class=args["record_class"], domain=args["domain"], actor="agent",
            reason="negative", logic="negative", truth_basis="negative",
            evidence=[{"source_ref": "negative", "content_summary": "negative"}],
            idempotency_key=f"negative:{args['domain']}", changes={"title": "bad"},
        )
    if call == "raw_append_only":
        table, verb = args["table"], args["verb"]
        with mem.store.connect() as conn:
            sql = f"DELETE FROM {table}" if verb == "DELETE" else f"UPDATE {table} SET rowid=rowid"
            conn.execute(sql)
        return None
    if call == "crash_after_revision":
        original = mem.store._insert_telemetry
        def crash(*_args, **_kwargs): raise RuntimeError("crash after revision insert")
        mem.store._insert_telemetry = crash
        try:
            return mem.identity_core_apply(args["receipt_id"])
        finally:
            mem.store._insert_telemetry = original
    raise AssertionError(call)


def tamper(mem: IdentityMemory, kind: str, args: dict[str, Any], saved: dict[str, Any]):
    args = resolve_refs(args, saved)
    with mem.store._raw_connect() as conn:
        if kind.startswith("receipt-"):
            drop_guards(conn, "memory_owner_receipts_v5")
            receipt_id = args["receipt_id"]
            if kind == "receipt-binding-json":
                row = conn.execute("SELECT binding_json FROM memory_owner_receipts_v5 WHERE receipt_id=?", (receipt_id,)).fetchone()
                changed = row[0].replace('"decision_note":""', '"decision_note":"tampered"')
                conn.execute("UPDATE memory_owner_receipts_v5 SET binding_json=? WHERE receipt_id=?", (changed, receipt_id))
            elif kind == "receipt-binding-sha":
                conn.execute("UPDATE memory_owner_receipts_v5 SET binding_sha256=? WHERE receipt_id=?", ("0" * 64, receipt_id))
        elif kind.startswith("proposal-"):
            drop_guards(conn, "memory_core_proposals_v5")
            proposal_id = args["proposal_id"]
            if kind == "proposal-content":
                conn.execute("UPDATE memory_core_proposals_v5 SET content=content||' ' WHERE proposal_id=?", (proposal_id,))
            elif kind == "proposal-phase-json":
                conn.execute("UPDATE memory_core_proposals_v5 SET phase_context_json=? WHERE proposal_id=?", ('{"model":"tampered"}', proposal_id))
            elif kind == "proposal-source-ref":
                conn.execute("UPDATE memory_core_proposals_v5 SET source_ref=source_ref||':tampered' WHERE proposal_id=?", (proposal_id,))
        else:
            raise AssertionError(kind)


def forge_phase_mismatch(mem: IdentityMemory, proposal: dict[str, Any]):
    content = json.loads(proposal["content"])
    content["phase_context"] = {"model": "different"}
    changed = json.dumps(content, ensure_ascii=False, indent=1)
    content_sha = hashlib.sha256(changed.encode()).hexdigest()
    fields = {
        "profile": proposal["profile"], "record_id": proposal["record_id"],
        "base_core_revision_id": proposal["base_core_revision_id"],
        "title": proposal["title"], "summary": proposal["summary"],
        "content_sha256": content_sha,
        "phase_context_sha256": proposal["phase_context_sha256"],
        "reason_sha256": proposal["reason_sha256"], "source_ref": proposal["source_ref"],
    }
    proposal_sha = hash_payload(fields)
    proposal_id = "core-proposal:" + proposal_sha[:32]
    with mem.store._raw_connect() as conn:
        drop_guards(conn, "memory_core_proposals_v5")
        conn.execute(
            "UPDATE memory_core_proposals_v5 SET proposal_id=?,content=?,content_sha256=?,proposal_sha256=? WHERE proposal_id=?",
            (proposal_id, changed, content_sha, proposal_sha, proposal["proposal_id"]),
        )
    binding = {
        "purpose": "identity_core_revision", "profile": mem.profile.name,
        "current_core_revision_id": proposal["base_core_revision_id"],
        "proposal_id": proposal_id, "proposal_sha256": proposal_sha,
        "content_sha256": content_sha,
        "phase_context_sha256": proposal["phase_context_sha256"],
        "reason_sha256": proposal["reason_sha256"], "source_ref": proposal["source_ref"],
        "outcome": "apply", "decision_note": "", "authority": "owner",
    }
    return mem._issue_receipt("identity_core_revision", binding)


def forge_stale_base(mem: IdentityMemory, proposal: dict[str, Any]):
    current = mem.store.current_view("core")[0]["revision_id"]
    binding = {
        "purpose": "identity_core_revision", "profile": mem.profile.name,
        "current_core_revision_id": current,
        "proposal_id": proposal["proposal_id"], "proposal_sha256": proposal["proposal_sha256"],
        "content_sha256": proposal["content_sha256"],
        "phase_context_sha256": proposal["phase_context_sha256"],
        "reason_sha256": proposal["reason_sha256"], "source_ref": proposal["source_ref"],
        "outcome": "apply", "decision_note": "stale-base fixture", "authority": "owner",
    }
    return mem._issue_receipt("identity_core_revision", binding)


def forge_cross_profile(mem: IdentityMemory, proposal: dict[str, Any]):
    fields = {
        "profile": "foreign-profile", "record_id": proposal["record_id"],
        "base_core_revision_id": proposal["base_core_revision_id"],
        "title": proposal["title"], "summary": proposal["summary"],
        "content_sha256": proposal["content_sha256"],
        "phase_context_sha256": proposal["phase_context_sha256"],
        "reason_sha256": proposal["reason_sha256"], "source_ref": proposal["source_ref"],
    }
    proposal_sha = hash_payload(fields)
    proposal_id = "core-proposal:" + proposal_sha[:32]
    with mem.store._raw_connect() as conn:
        drop_guards(conn, "memory_core_proposals_v5")
        conn.execute(
            "UPDATE memory_core_proposals_v5 SET proposal_id=?,profile=?,proposal_sha256=? WHERE proposal_id=?",
            (proposal_id, "foreign-profile", proposal_sha, proposal["proposal_id"]),
        )
    binding = {
        "purpose": "identity_core_revision", "profile": mem.profile.name,
        "current_core_revision_id": proposal["base_core_revision_id"],
        "proposal_id": proposal_id, "proposal_sha256": proposal_sha,
        "content_sha256": proposal["content_sha256"],
        "phase_context_sha256": proposal["phase_context_sha256"],
        "reason_sha256": proposal["reason_sha256"], "source_ref": proposal["source_ref"],
        "outcome": "apply", "decision_note": "", "authority": "owner",
    }
    return mem._issue_receipt("identity_core_revision", binding)


def execute(mem: IdentityMemory, script: dict[str, Any]) -> list[dict[str, Any]]:
    saved: dict[str, Any] = {}
    results = []
    for index, raw in enumerate(script["actions"]):
        action = resolve_refs(raw, saved)
        if action["call"] == "tamper":
            tamper(mem, action["kind"], action.get("arguments", {}), saved)
            continue
        if action["call"] == "forge_phase_mismatch":
            result = forge_phase_mismatch(mem, saved[action["proposal"]])
            saved[action["save"]] = result
            continue
        if action["call"] == "forge_stale_base":
            result = forge_stale_base(mem, saved[action["proposal"]])
            saved[action["save"]] = result
            continue
        if action["call"] == "forge_cross_profile":
            result = forge_cross_profile(mem, saved[action["proposal"]])
            saved[action["save"]] = result
            continue
        expected = action.get("expect_error")
        before = mem.db_path.read_bytes() if expected else None
        before_ops = operation_count(mem) if expected else None
        try:
            result = invoke(mem, action["call"], action.get("arguments", {}), saved)
        except Exception as exc:
            if not expected or type(exc).__name__ != expected:
                raise
            unchanged = mem.db_path.read_bytes() == before
            no_operation = operation_count(mem) == before_ops
            if not unchanged or not no_operation:
                raise AssertionError(f"negative case mutated store: {action['call']}")
            result = {"error": type(exc).__name__, "message": str(exc), "store_bytes_unchanged": True, "no_operation": True}
        else:
            if expected:
                raise AssertionError(f"expected {expected}: {action['call']}")
        if action.get("save"):
            saved[action["save"]] = result
        if action.get("record", True):
            results.append({"index": index, "call": action["call"], "result": result})
    return results


def ref(name: str, field: str) -> dict[str, str]: return {"$ref": f"{name}.{field}"}


def base_actions(): return [{"call": "bootstrap", "save": "boot", "record": False}]


def proposal_action(reason="new reading", context=None, source_ref=None, save="p"):
    arguments: dict[str, Any] = {"reason": reason, "phase_context": context or {"model": "oracle", "harness": "golden"}}
    if source_ref is not None: arguments["source_ref"] = source_ref
    return {"call": "identity_core_propose", "arguments": arguments, "save": save}


def issue_action(proposal="p", outcome="apply", note="", save="r", **extra):
    args = {"proposal_id": ref(proposal, "proposal_id"), "outcome": outcome, "decision_note": note, **extra}
    return {"call": "owner_approve_core", "arguments": args, "save": save}


def apply_action(receipt="r", *, expect=None):
    item = {"call": "identity_core_apply", "arguments": {"receipt_id": ref(receipt, "receipt_id")}}
    if expect: item["expect_error"] = expect
    return item


def positive_scripts() -> dict[str, dict[str, Any]]:
    scripts: dict[str, dict[str, Any]] = {}
    scripts["proposal-apply"] = {"actions": [*base_actions(), proposal_action(), issue_action(note="accepted"), apply_action(), {"call": "identity_packet", "arguments": {"cue": "who are you"}}]}
    scripts["proposal-reject"] = {"actions": [*base_actions(), proposal_action(), issue_action(outcome="reject", note="not now"), apply_action(), {"call": "identity_status"}]}
    scripts["stale-proposals"] = {"actions": [*base_actions(), proposal_action("first", save="p1"), proposal_action("second", save="p2"), issue_action("p1", save="r1"), issue_action("p2", save="r2"), apply_action("r1"), apply_action("r2", expect="StaleAuthority"), issue_action("p2", outcome="reject", save="rr"), apply_action("rr")]}
    scripts["proposal-idempotent"] = {"actions": [*base_actions(), proposal_action(save="p1"), proposal_action(save="p2")]}
    scripts["binding-idempotent"] = {"actions": [*base_actions(), proposal_action(), issue_action(save="r1"), issue_action(save="r2")]}
    scripts["decision-note-race"] = {"actions": [*base_actions(), proposal_action(), issue_action(outcome="reject", note="one", save="r1"), issue_action(outcome="reject", note="two", save="r2"), apply_action("r1"), apply_action("r2", expect="ProposalDecided")]}
    scripts["apply-replay"] = {"actions": [*base_actions(), proposal_action(), issue_action(), apply_action(), apply_action()]}
    scripts["issue-only"] = {"actions": [*base_actions(), proposal_action(), issue_action(save="issued"), apply_action("issued")]}
    scripts["retract"] = {"actions": [*base_actions(), {"call": "fixture_log_phase", "arguments": {"event_id": "retract-me", "title": "Retract me", "summary": "fixture"}}, {"call": "owner_approve_retract", "arguments": {"record_id": "phase:retract-me", "reason": "owner correction"}, "save": "r"}, {"call": "identity_retract", "arguments": {"receipt_id": ref("r", "receipt_id")}}]}
    scripts["encoding"] = {"actions": [*base_actions(), proposal_action("encoding", {"b": 1, "2": 2, "1": 1.0, "float-small": 1e-5, "float-large": 1e16}), issue_action(), apply_action()]}
    scripts["default-source-ref"] = {"actions": [*base_actions(), proposal_action("default source", {"model": "oracle"}), issue_action(outcome="reject"), apply_action()]}
    return scripts


def negative_scripts() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    result["negative-receipt-unknown"] = {"actions": [*base_actions(), {"call": "identity_core_apply", "arguments": {"receipt_id": "receipt:" + "0" * 32}, "expect_error": "ReceiptNotFound"}]}
    for name, call, expected in (("purpose", "apply_retract_wrong_purpose", "ReceiptIntegrityError"),):
        result[f"negative-receipt-{name}"] = {"actions": [*base_actions(), proposal_action(), issue_action(), {"call": call, "arguments": {"receipt_id": ref("r", "receipt_id")}, "expect_error": expected}]}
    result["negative-receipt-profile"] = {"actions": [*base_actions(), proposal_action(), issue_action(), {"call": "apply_core_as_profile", "arguments": {"receipt_id": ref("r", "receipt_id"), "profile": "foreign-profile"}, "expect_error": "ReceiptIntegrityError"}]}
    result["negative-receipt-cross-profile-proposal"] = {"actions": [*base_actions(), proposal_action(), {"call": "forge_cross_profile", "proposal": "p", "save": "r", "record": False}, apply_action(expect="ProposalIntegrityError")]}
    for kind in ("binding-json", "binding-sha"):
        result[f"negative-receipt-{kind}"] = {"actions": [*base_actions(), proposal_action(), issue_action(), {"call": "tamper", "kind": f"receipt-{kind}", "arguments": {"receipt_id": ref("r", "receipt_id")}}, apply_action(expect="ReceiptIntegrityError")]}
    for kind in ("content", "phase-json", "source-ref"):
        result[f"negative-proposal-{kind}"] = {"actions": [*base_actions(), proposal_action(), issue_action(), {"call": "tamper", "kind": f"proposal-{kind}", "arguments": {"proposal_id": ref("p", "proposal_id")}}, apply_action(expect="ProposalIntegrityError")]}
    result["negative-proposal-phase-content"] = {"actions": [*base_actions(), proposal_action(), {"call": "forge_phase_mismatch", "proposal": "p", "save": "r", "record": False}, apply_action(expect="ProposalIntegrityError")]}
    result["negative-state-stale-current"] = {"actions": [*base_actions(), proposal_action("first", save="p1"), proposal_action("second", save="p2"), issue_action("p2", outcome="reject", save="stale"), issue_action("p1", save="winner"), apply_action("winner"), apply_action("stale", expect="StaleAuthority")]}
    result["negative-state-stale-base"] = {"actions": [*base_actions(), proposal_action("first", save="p1"), proposal_action("second", save="p2"), issue_action("p1", save="winner"), apply_action("winner"), {"call": "forge_stale_base", "proposal": "p2", "save": "forged", "record": False}, apply_action("forged", expect="StaleAuthority")]}
    result["negative-state-decided"] = {"actions": [*base_actions(), proposal_action(), issue_action(outcome="reject", note="one", save="r1"), issue_action(outcome="reject", note="two", save="r2"), apply_action("r1"), apply_action("r2", expect="ProposalDecided")]}
    pinned = []
    for record_id in PINNED:
        for writer in ("create", "revise", "invalidate"):
            pinned.append({"call": "public_writer_pinned", "arguments": {"record_id": record_id, "writer": writer}, "expect_error": "PinnedRecordError"})
    result["negative-writer-pinned"] = {"actions": [*base_actions(), *pinned]}
    result["negative-writer-phase-fact-pinned"] = {"actions": [*base_actions(), {"call": "runtime_submit_pinned", "arguments": {"record_id": "core", "record_class": "event", "domain": "phase"}, "expect_error": "PinnedRecordError"}, {"call": "runtime_submit_pinned", "arguments": {"record_id": "core", "record_class": "belief", "domain": "fact"}, "expect_error": "PinnedRecordError"}]}
    result["negative-guard-no-tty"] = {"actions": [*base_actions(), proposal_action(), issue_action(confirmation="correct", tty=False),]}; result["negative-guard-no-tty"]["actions"][-1]["expect_error"] = "HumanPresenceRequired"
    result["negative-guard-confirmation"] = {"actions": [*base_actions(), proposal_action(), issue_action(confirmation="WRONG")]} ; result["negative-guard-confirmation"]["actions"][-1]["expect_error"] = "ConfirmationMismatch"
    for table in ("memory_core_proposals_v5", "memory_owner_receipts_v5", "memory_receipt_consumptions_v5", "memory_proposal_decisions_v5"):
        slug = table.removeprefix("memory_").removesuffix("_v5").replace("_", "-")
        result[f"negative-storage-{slug}"] = {"actions": [*base_actions(), proposal_action(), issue_action(outcome="reject"), apply_action(), {"call": "raw_append_only", "arguments": {"table": table, "verb": "UPDATE"}, "expect_error": "IntegrityError"}, {"call": "raw_append_only", "arguments": {"table": table, "verb": "DELETE"}, "expect_error": "IntegrityError"}]}
    result["negative-crash-rollback"] = {"actions": [*base_actions(), proposal_action(), issue_action(), {"call": "crash_after_revision", "arguments": {"receipt_id": ref("r", "receipt_id")}, "expect_error": "RuntimeError"}]}
    return result


def legacy_close(output_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    script = {"actions": [
        {"call": "install_legacy_v4", "source": "spec/golden/identity-open/store.sqlite3", "record": False},
        {"call": "migrate_v4_to_v5", "record": False},
        {"call": "owner_close_legacy", "arguments": {"note": "settled"}, "save": "r"},
        {"call": "identity_close_legacy_discussion", "arguments": {"receipt_id": ref("r", "receipt_id")}},
    ]}
    shutil.copyfile(LEGACY_V4, output_path)
    backup = output_path.with_name("migration-backup.sqlite3")
    target = output_path.with_name("migrated.sqlite3")
    migrated = MemoryStore(output_path).migrate_to(target, backup_path=backup)
    output_path.unlink(); target.rename(output_path); backup.unlink()
    mem = IdentityMemory(load_profile("example"), output_path, surface="golden")
    with mem.store.connect(readonly=True) as conn:
        relation = conn.execute("SELECT * FROM memory_relation_events_v4 WHERE relation_type='awaiting-discussion' ORDER BY sequence_number DESC LIMIT 1").fetchone()
    terminal = TTY(f"CLOSE {short(relation['relation_event_id'])}")
    receipt = mem.issue_legacy_close_receipt(note="settled", stdin=terminal, stdout=terminal)
    result = mem.identity_close_legacy_discussion(receipt["receipt_id"])
    return script, [{"index": 3, "call": "identity_close_legacy_discussion", "result": result}]


def writer_v4(output_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    script = {"actions": [
        {"call": "install_legacy_v4", "source": "spec/golden/identity-open/store.sqlite3", "record": False},
        {"call": "fixture_log_phase", "arguments": {"event_id": "blocked", "title": "blocked", "summary": "blocked"}, "expect_error": "MigrationRequiredError"},
    ]}
    shutil.copyfile(LEGACY_V4, output_path)
    mem = IdentityMemory(load_profile("example"), output_path, surface="golden")
    before = output_path.read_bytes(); ops = operation_count(mem)
    try: mem.log_phase("blocked", title="blocked", summary="blocked")
    except Exception as exc:
        if type(exc).__name__ != "MigrationRequiredError": raise
        assert output_path.read_bytes() == before and operation_count(mem) == ops
        results = [{"index": 1, "call": "fixture_log_phase", "result": {"error": type(exc).__name__, "message": str(exc), "store_bytes_unchanged": True, "no_operation": True}}]
    else: raise AssertionError("v4 writer succeeded")
    return script, results


def generate(output: Path) -> None:
    if sys.version_info[:2] != (3, 11) or unicodedata.unidata_version != "14.0.0":
        raise SystemExit("authority corpus generation requires Python 3.11 / Unicode 14.0.0")
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    scripts = {**positive_scripts(), **negative_scripts()}
    scripts["negative-writer-v4"] = {"custom": "writer-v4"}
    scripts["legacy-migrate-close"] = {"custom": "legacy-close"}
    with tempfile.TemporaryDirectory(prefix="trajecta-authority-v2-") as tmp:
        workspace = Path(tmp)
        for name in sorted(scripts):
            scenario = output / name; scenario.mkdir()
            path = workspace / f"{name}.sqlite3"
            with clock_context():
                if name == "legacy-migrate-close":
                    script, results = legacy_close(path)
                elif name == "negative-writer-v4":
                    script, results = writer_v4(path)
                else:
                    script = scripts[name]
                    mem = IdentityMemory(load_profile("example"), path, surface="golden")
                    results = execute(mem, script)
            shutil.copyfile(path, scenario / "store.sqlite3")
            write_script(scenario / "script.json", script)
            write_json(scenario / "dump.json", dump_database(path))
            (scenario / "cases.jsonl").write_text(
                canonical({"label": name, "results": results}) + "\n", encoding="utf-8"
            )
    source_paths = sorted({path for pattern in SOURCES for path in ROOT.glob(pattern)})
    source_hashes = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths}
    files = sorted(path for path in output.rglob("*") if path.is_file())
    manifest_files = {
        path.relative_to(output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
    }
    for table in sorted(TABLES.glob("*.json")):
        manifest_files[f"tables/{table.name}"] = hashlib.sha256(table.read_bytes()).hexdigest()
    manifest = {
        "schema": "trajecta.golden-authority-manifest/v1",
        "oracle_commit": ORACLE_COMMIT,
        "oracle_sources": source_hashes,
        "python": sys.version.split()[0],
        "unicode": unicodedata.unidata_version,
        "sqlite": sqlite3.sqlite_version,
        "legacy_v4_sha256": hashlib.sha256(LEGACY_V4.read_bytes()).hexdigest(),
        "files": dict(sorted(manifest_files.items())),
    }
    write_json(output / "MANIFEST.json", manifest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "spec" / "golden-authority-v2")
    args = parser.parse_args()
    generate(args.output)
