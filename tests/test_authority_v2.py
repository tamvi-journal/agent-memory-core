from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from memory_core import MemoryStore, MigrationRequiredError, PinnedRecordError
from memory_core.store import hash_payload
from trajecta_identity import (
    ConfirmationMismatch,
    HumanPresenceRequired,
    IdentityMemory,
    ProposalDecided,
    ProposalIntegrityError,
    ReceiptIntegrityError,
    ReceiptNotFound,
    StaleAuthority,
    load_profile,
)
from trajecta_identity.identity import PINNED
from trajecta_identity.cli import parser
from trajecta_identity.mcp_server import TOOLS

ROOT = Path(__file__).resolve().parents[1]


class TTY:
    def __init__(self, value: str, *, tty: bool = True):
        self.value = value
        self.tty = tty
        self.output = ""

    def isatty(self):
        return self.tty

    def readline(self):
        return self.value + "\n"

    def write(self, value):
        self.output += value
        return len(value)

    def flush(self):
        pass


def confirmation(action: str, target: str) -> TTY:
    short = target.split(":", 1)[-1][:12] if action in {"APPLY", "REJECT", "CLOSE"} else target
    return TTY(f"{action} {short}")


@pytest.fixture
def memory(tmp_path: Path) -> IdentityMemory:
    result = IdentityMemory(load_profile("example"), tmp_path / "authority.sqlite3", surface="test")
    result.bootstrap()
    return result


def propose(memory: IdentityMemory, *, reason="new reading", context=None, source_ref=""):
    return memory.identity_core_propose(
        reason=reason,
        phase_context=context or {"model": "oracle", "1": 1, "2": 1.0},
        source_ref=source_ref,
    )


def issue_core(memory: IdentityMemory, proposal: dict, outcome="apply", note=""):
    terminal = confirmation(outcome.upper(), proposal["proposal_id"])
    return memory.issue_core_receipt(
        proposal["proposal_id"], outcome=outcome, decision_note=note,
        stdin=terminal, stdout=terminal,
    )


def operation_count(memory: IdentityMemory) -> int:
    with memory.store.connect(readonly=True) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM memory_operations_v3").fetchone()[0])


def assert_unchanged_failure(memory: IdentityMemory, error, call):
    before = memory.db_path.read_bytes()
    operations = operation_count(memory)
    with pytest.raises(error):
        call()
    assert memory.db_path.read_bytes() == before
    assert operation_count(memory) == operations


def drop_guard_and_update(memory: IdentityMemory, table: str, sql: str, params=()):
    with memory.store._raw_connect() as conn:
        for suffix in ("no_update", "no_delete"):
            conn.execute(f"DROP TRIGGER IF EXISTS {table}_{suffix}")
        conn.execute(sql, params)


def test_schema_v5_and_legacy_v4_read_write_split(tmp_path: Path):
    fresh = MemoryStore(tmp_path / "fresh.sqlite3")
    assert fresh.initialize()["user_version"] == 5
    source = ROOT / "spec/golden/identity-open/store.sqlite3"
    legacy = tmp_path / "legacy.sqlite3"
    shutil.copyfile(source, legacy)
    store = MemoryStore(legacy)
    assert store.schema_info()["state"] == "legacy-v4"
    assert store.current_view("core")
    before = legacy.read_bytes()
    with pytest.raises(MigrationRequiredError, match="v4 store must be migrated"):
        store.create_current(
            record_id="x", record_class="belief", domain="fact", title="x",
            actor="test", reason="test", evidence={"source_ref": "test", "content_summary": "test"},
            idempotency_key="test:x",
        )
    assert legacy.read_bytes() == before


def test_explicit_v4_migration_dry_run_and_backup(tmp_path: Path):
    source = tmp_path / "legacy.sqlite3"
    shutil.copyfile(ROOT / "spec/golden/identity-open/store.sqlite3", source)
    target = tmp_path / "migrated.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    store = MemoryStore(source)
    source_bytes = source.read_bytes()
    assert store.migrate_to(target, dry_run=True) == {
        "state": "ready", "from": "legacy-v4", "dry_run": True,
    }
    assert not target.exists()
    migrated = store.migrate_to(target, backup_path=backup)
    assert isinstance(migrated, MemoryStore)
    assert migrated.schema_info()["state"] == "ready"
    assert source.read_bytes() == source_bytes == backup.read_bytes()


def test_propose_is_idempotent_and_keeps_core_unchanged(memory: IdentityMemory):
    before = memory.store.current_view("core")[0]["revision_id"]
    first = propose(memory)
    second = propose(memory)
    assert first["proposal_id"] == second["proposal_id"]
    assert second["status"] == "existing"
    assert memory.store.current_view("core")[0]["revision_id"] == before
    assert memory.open_core_proposals()[0]["stale"] is False
    assert first["phase_context_sha256"] == hash_payload({"model": "oracle", "1": 1, "2": 1.0})
    assert first["source_ref"].startswith("self:core-proposal:")


def test_apply_and_replay_have_one_authority_operation(memory: IdentityMemory):
    proposal = propose(memory, source_ref="test:source")
    receipt = issue_core(memory, proposal, note="owner accepted")
    before = operation_count(memory)
    result = memory.identity_core_apply(receipt["receipt_id"])
    replay = memory.identity_core_apply(receipt["receipt_id"])
    assert replay["operation_id"] == result["operation_id"]
    assert operation_count(memory) == before + 1
    assert result["idempotency_key"] == "authority-v2:" + receipt["receipt_id"]
    assert result["details"]["decision_note"] == "owner accepted"
    revision = memory.store.current_view("core")[0]
    assert revision["idempotency_key"] == result["idempotency_key"]
    with memory.store.connect(readonly=True) as conn:
        lifecycle = conn.execute(
            "SELECT operation_id FROM memory_lifecycle_events_v3 WHERE revision_id=?",
            (revision["revision_id"],),
        ).fetchone()
        evidence = conn.execute(
            "SELECT evidence_type,source_ref FROM memory_revision_evidence_v3 l "
            "JOIN memory_evidence_v3 e ON e.evidence_id=l.evidence_id WHERE l.revision_id=? "
            "ORDER BY evidence_type",
            (revision["revision_id"],),
        ).fetchall()
    assert lifecycle["operation_id"] == result["operation_id"]
    assert [(row[0], row[1]) for row in evidence] == [
        ("owner_receipt", receipt["receipt_id"]), ("self_log", "test:source")
    ]


def test_replay_checks_receipt_integrity_first(memory: IdentityMemory):
    proposal = propose(memory)
    receipt = issue_core(memory, proposal, outcome="reject")
    memory.identity_core_apply(receipt["receipt_id"])
    drop_guard_and_update(
        memory, "memory_owner_receipts_v5",
        "UPDATE memory_owner_receipts_v5 SET binding_sha256=? WHERE receipt_id=?",
        ("f" * 64, receipt["receipt_id"]),
    )
    assert_unchanged_failure(
        memory, ReceiptIntegrityError,
        lambda: memory.identity_core_apply(receipt["receipt_id"]),
    )


def test_reject_settles_without_core_mutation(memory: IdentityMemory):
    proposal = propose(memory)
    receipt = issue_core(memory, proposal, outcome="reject", note="not this phase")
    current = memory.store.current_view("core")[0]["revision_id"]
    before = operation_count(memory)
    result = memory.identity_core_apply(receipt["receipt_id"])
    assert memory.store.current_view("core")[0]["revision_id"] == current
    assert operation_count(memory) == before + 1
    assert result["details"]["outcome"] == "reject"
    assert memory.open_core_proposals() == []


def test_two_proposals_one_base_stale_apply_refused_reject_works(memory: IdentityMemory):
    first = propose(memory, reason="first")
    second = propose(memory, reason="second")
    first_apply = issue_core(memory, first)
    second_apply = issue_core(memory, second)
    memory.identity_core_apply(first_apply["receipt_id"])
    assert_unchanged_failure(
        memory, StaleAuthority,
        lambda: memory.identity_core_apply(second_apply["receipt_id"]),
    )
    second_reject = issue_core(memory, second, outcome="reject")
    result = memory.identity_core_apply(second_reject["receipt_id"])
    assert result["details"]["outcome"] == "reject"


def test_same_binding_idempotent_and_different_note_only_one_wins(memory: IdentityMemory):
    proposal = propose(memory)
    same_a = issue_core(memory, proposal, outcome="reject", note="same")
    same_b = issue_core(memory, proposal, outcome="reject", note="same")
    other = issue_core(memory, proposal, outcome="reject", note="other")
    assert same_a["receipt_id"] == same_b["receipt_id"]
    assert other["receipt_id"] != same_a["receipt_id"]
    memory.identity_core_apply(same_a["receipt_id"])
    assert_unchanged_failure(
        memory, ProposalDecided,
        lambda: memory.identity_core_apply(other["receipt_id"]),
    )


def test_issue_only_receipt_can_be_consumed_by_another_instance(memory: IdentityMemory):
    proposal = propose(memory)
    receipt = issue_core(memory, proposal)
    other = IdentityMemory(memory.profile, memory.db_path, surface="other-process")
    assert other.apply_receipt(receipt["receipt_id"])["operation_type"] == "identity_core_revision"


def test_retract_receipt_and_replay(memory: IdentityMemory):
    memory.log_phase("retract-me", title="Retract", summary="fixture")
    terminal = confirmation("RETRACT", "phase:retract-me")
    receipt = memory.issue_retract_receipt(
        "phase:retract-me", reason="owner correction", stdin=terminal, stdout=terminal
    )
    before = operation_count(memory)
    result = memory.identity_retract(receipt["receipt_id"])
    assert result["operation_type"] == "identity_retract"
    assert not memory.store.current_view("phase:retract-me")
    assert memory.store.historical_view("phase:retract-me")
    assert memory.identity_retract(receipt["receipt_id"])["operation_id"] == result["operation_id"]
    assert operation_count(memory) == before + 1


def test_migrated_legacy_discussion_close(memory: IdentityMemory, tmp_path: Path):
    source = ROOT / "spec/golden/identity-open/store.sqlite3"
    migrated_path = tmp_path / "legacy-migrated.sqlite3"
    backup = tmp_path / "legacy-v4.bak"
    migrated = MemoryStore(source).migrate_to(migrated_path, backup_path=backup)
    assert isinstance(migrated, MemoryStore)
    legacy = IdentityMemory(load_profile("example"), migrated_path, surface="test")
    with legacy.store.connect(readonly=True) as conn:
        relation = dict(conn.execute(
            "SELECT * FROM memory_relation_events_v4 WHERE relation_type='awaiting-discussion' "
            "ORDER BY sequence_number DESC LIMIT 1"
        ).fetchone())
    terminal = confirmation("CLOSE", relation["relation_event_id"])
    receipt = legacy.issue_legacy_close_receipt(note="settled", stdin=terminal, stdout=terminal)
    before = operation_count(legacy)
    result = legacy.identity_close_legacy_discussion(receipt["receipt_id"])
    assert result["operation_type"] == "identity_legacy_discussion_close"
    assert legacy.open_discussions() == []
    assert operation_count(legacy) == before + 1


@pytest.mark.parametrize("tty,value,error", [
    (False, "APPLY anything", HumanPresenceRequired),
    (True, "APPLY wrong", ConfirmationMismatch),
])
def test_human_presence_guard(memory: IdentityMemory, tty, value, error):
    proposal = propose(memory)
    terminal = TTY(value, tty=tty)
    assert_unchanged_failure(
        memory, error,
        lambda: memory.issue_core_receipt(
            proposal["proposal_id"], outcome="apply", stdin=terminal, stdout=terminal
        ),
    )


def test_owner_cli_has_no_noninteractive_bypass_and_mcp_issues_no_receipts():
    help_text = parser().format_help()
    assert "--yes" not in help_text
    names = {tool["name"] for tool in TOOLS}
    assert "identity_revise_core" not in names
    assert "identity_close_discussion" not in names
    assert {"identity_core_propose", "identity_core_proposals", "identity_core_apply",
            "identity_retract", "identity_close_legacy_discussion"} <= names
    assert not any("issue" in name and "receipt" in name for name in names)


def test_unknown_wrong_purpose_and_wrong_profile_receipts(memory: IdentityMemory):
    assert_unchanged_failure(
        memory, ReceiptNotFound,
        lambda: memory.identity_core_apply("receipt:" + "0" * 32),
    )
    proposal = propose(memory)
    receipt = issue_core(memory, proposal)
    assert_unchanged_failure(
        memory, ReceiptIntegrityError,
        lambda: memory.identity_retract(receipt["receipt_id"]),
    )
    other_profile = dataclasses.replace(memory.profile, name="other-profile")
    other = IdentityMemory(other_profile, memory.db_path, surface="test")
    assert_unchanged_failure(
        other, ReceiptIntegrityError,
        lambda: other.identity_core_apply(receipt["receipt_id"]),
    )


@pytest.mark.parametrize("kind", ["binding_json", "binding_sha256"])
def test_receipt_integrity_tampering(memory: IdentityMemory, kind: str):
    proposal = propose(memory)
    receipt = issue_core(memory, proposal)
    if kind == "binding_json":
        drop_guard_and_update(
            memory, "memory_owner_receipts_v5",
            "UPDATE memory_owner_receipts_v5 SET binding_json=? WHERE receipt_id=?",
            (receipt["binding_json"].replace('"decision_note":""', '"decision_note":"tampered"'), receipt["receipt_id"]),
        )
    else:
        drop_guard_and_update(
            memory, "memory_owner_receipts_v5",
            "UPDATE memory_owner_receipts_v5 SET binding_sha256=? WHERE receipt_id=?",
            ("0" * 64, receipt["receipt_id"]),
        )
    assert_unchanged_failure(
        memory, ReceiptIntegrityError,
        lambda: memory.identity_core_apply(receipt["receipt_id"]),
    )


@pytest.mark.parametrize("kind", ["content", "phase_context_json", "source_ref"])
def test_proposal_integrity_tampering(memory: IdentityMemory, kind: str):
    proposal = propose(memory)
    receipt = issue_core(memory, proposal)
    values = {
        "content": proposal["content"] + " ",
        "phase_context_json": '{"model":"tampered"}',
        "source_ref": proposal["source_ref"] + ":tampered",
    }
    drop_guard_and_update(
        memory, "memory_core_proposals_v5",
        f"UPDATE memory_core_proposals_v5 SET {kind}=? WHERE proposal_id=?",
        (values[kind], proposal["proposal_id"]),
    )
    assert_unchanged_failure(
        memory, ProposalIntegrityError,
        lambda: memory.identity_core_apply(receipt["receipt_id"]),
    )


def test_phase_context_in_content_mismatch_is_recomputed(memory: IdentityMemory):
    proposal = propose(memory)
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
    with memory.store._raw_connect() as conn:
        conn.execute("DROP TRIGGER memory_core_proposals_v5_no_update")
        conn.execute(
            "UPDATE memory_core_proposals_v5 SET proposal_id=?,content=?,content_sha256=?,proposal_sha256=? WHERE proposal_id=?",
            (proposal_id, changed, content_sha, proposal_sha, proposal["proposal_id"]),
        )
    binding = {
        "purpose": "identity_core_revision", "profile": memory.profile.name,
        "current_core_revision_id": proposal["base_core_revision_id"],
        "proposal_id": proposal_id, "proposal_sha256": proposal_sha,
        "content_sha256": content_sha,
        "phase_context_sha256": proposal["phase_context_sha256"],
        "reason_sha256": proposal["reason_sha256"], "source_ref": proposal["source_ref"],
        "outcome": "apply", "decision_note": "", "authority": "owner",
    }
    receipt = memory._issue_receipt("identity_core_revision", binding)
    assert_unchanged_failure(
        memory, ProposalIntegrityError,
        lambda: memory.identity_core_apply(receipt["receipt_id"]),
    )


def test_stale_current_and_apply_stale_base(memory: IdentityMemory):
    first = propose(memory, reason="first")
    second = propose(memory, reason="second")
    stale_current = issue_core(memory, second, outcome="reject")
    memory.identity_core_apply(issue_core(memory, first)["receipt_id"])
    assert_unchanged_failure(
        memory, StaleAuthority,
        lambda: memory.identity_core_apply(stale_current["receipt_id"]),
    )
    current = memory.store.current_view("core")[0]["revision_id"]
    binding = {
        "purpose": "identity_core_revision", "profile": memory.profile.name,
        "current_core_revision_id": current,
        "proposal_id": second["proposal_id"], "proposal_sha256": second["proposal_sha256"],
        "content_sha256": second["content_sha256"],
        "phase_context_sha256": second["phase_context_sha256"],
        "reason_sha256": second["reason_sha256"], "source_ref": second["source_ref"],
        "outcome": "apply", "decision_note": "forged stale-base fixture", "authority": "owner",
    }
    forged = memory._issue_receipt("identity_core_revision", binding)
    assert_unchanged_failure(
        memory, StaleAuthority,
        lambda: memory.identity_core_apply(forged["receipt_id"]),
    )


@pytest.mark.parametrize("record_id", PINNED)
def test_lowest_public_writers_refuse_every_pinned_id(memory: IdentityMemory, record_id: str):
    calls = [
        lambda: memory.store.create_current(
            record_id=record_id, record_class="axis", domain="core", title="x",
            actor="test", reason="test", evidence={"source_ref": "test", "content_summary": "test"},
            idempotency_key="pinned:create:" + record_id,
        ),
        lambda: memory.store.revise(
            record_id, operation_type="refine", actor="test", reason="test",
            evidence={"source_ref": "test", "content_summary": "test"},
            idempotency_key="pinned:revise:" + record_id, changes={"summary": "x"},
        ),
        lambda: memory.store.invalidate(
            record_id, actor="test", reason="test",
            evidence={"source_ref": "test", "content_summary": "test"},
            idempotency_key="pinned:invalidate:" + record_id,
        ),
    ]
    for call in calls:
        assert_unchanged_failure(memory, PinnedRecordError, call)


@pytest.mark.parametrize("domain,record_class", [("phase", "event"), ("fact", "belief")])
def test_identity_runtime_submit_refuses_pinned_phase_or_fact_id(memory, domain, record_class):
    assert_unchanged_failure(
        memory, PinnedRecordError,
        lambda: memory.runtime.submit(
            operation_type="create", record_id="core", record_class=record_class,
            domain=domain, actor="agent", reason="bad", logic="bad",
            truth_basis="bad", evidence=[{"source_ref": "bad", "content_summary": "bad"}],
            idempotency_key=f"bad:{domain}", changes={"title": "bad"},
        ),
    )


@pytest.mark.parametrize("table", [
    "memory_core_proposals_v5", "memory_owner_receipts_v5",
    "memory_receipt_consumptions_v5", "memory_proposal_decisions_v5",
])
@pytest.mark.parametrize("verb", ["UPDATE", "DELETE"])
def test_v5_tables_are_append_only(memory: IdentityMemory, table: str, verb: str):
    proposal = propose(memory)
    receipt = issue_core(memory, proposal, outcome="reject")
    memory.identity_core_apply(receipt["receipt_id"])
    before = memory.db_path.read_bytes()
    operations = operation_count(memory)
    with memory.store.connect() as conn:
        sql = f"{verb} FROM {table}" if verb == "DELETE" else f"UPDATE {table} SET rowid=rowid"
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(sql)
    assert memory.db_path.read_bytes() == before
    assert operation_count(memory) == operations


def test_crash_after_revision_insert_rolls_back_everything(memory: IdentityMemory, monkeypatch):
    proposal = propose(memory)
    receipt = issue_core(memory, proposal)
    before = memory.db_path.read_bytes()
    operations = operation_count(memory)
    original = memory.store._insert_telemetry

    def crash(*args, **kwargs):
        raise RuntimeError("test crash after revision insert")

    monkeypatch.setattr(memory.store, "_insert_telemetry", crash)
    with pytest.raises(RuntimeError, match="after revision insert"):
        memory.identity_core_apply(receipt["receipt_id"])
    monkeypatch.setattr(memory.store, "_insert_telemetry", original)
    memory.store = MemoryStore(memory.db_path, pinned_guard=PINNED)
    memory.runtime.store = memory.store
    assert memory.db_path.read_bytes() == before
    assert operation_count(memory) == operations
    assert len(memory.store.historical_view("core")) == 1
    with memory.store.connect(readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_proposal_decisions_v5").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_receipt_consumptions_v5").fetchone()[0] == 0
