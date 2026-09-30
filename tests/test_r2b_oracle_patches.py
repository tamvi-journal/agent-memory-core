from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from memory_core import MemoryStore, MigrationRequiredError, ValidatedIntake
from trajecta_identity import IdentityMemory, load_profile


ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "spec/golden/identity-open/store.sqlite3"
ERROR = "schema v4 store must be migrated to v5 before writing"


def legacy_memory(tmp_path: Path, name: str) -> IdentityMemory:
    folder = tmp_path / name
    folder.mkdir()
    target = folder / "store.sqlite3"
    shutil.copyfile(LEGACY, target)
    return IdentityMemory(load_profile("example"), target, surface="r2b-test")


def assert_refused_without_file_changes(memory: IdentityMemory, call) -> None:
    before_names = sorted(path.name for path in memory.db_path.parent.iterdir())
    before_bytes = memory.db_path.read_bytes()
    with pytest.raises(MigrationRequiredError, match=ERROR):
        call()
    assert memory.db_path.read_bytes() == before_bytes
    assert sorted(path.name for path in memory.db_path.parent.iterdir()) == before_names


@pytest.mark.parametrize("moment", ["2026-09-30T00:00:00+00:00", "2100-01-01T00:00:00+00:00"])
def test_p4_decay_refuses_v4_before_sidecar_for_zero_and_nonzero_adjustments(
    tmp_path: Path, moment: str
) -> None:
    memory = legacy_memory(tmp_path, moment[:4] + moment[5:7])
    assert_refused_without_file_changes(memory, lambda: memory.decay(moment))


def test_p5_identity_writer_entry_gates_cover_mutating_and_noop_paths(tmp_path: Path) -> None:
    calls = {
        "bootstrap-exists": lambda memory: memory.bootstrap(),
        "phase": lambda memory: memory.log_phase("new-phase", title="new", summary="new"),
        "fact-noop": lambda memory: memory.log_fact(
            "belief", title="Held fact", summary="after", content=""
        ),
        "close-loop-noop": lambda memory: memory.close_loop("phase:not-open", note="done"),
        "core-propose": lambda memory: memory.identity_core_propose(
            reason="new", phase_context={"model": "test"}
        ),
    }
    for name, call in calls.items():
        memory = legacy_memory(tmp_path, name)
        assert_refused_without_file_changes(memory, lambda call=call, memory=memory: call(memory))


def test_p5_kernel_runtime_and_intake_gate_before_validation_or_replay(tmp_path: Path) -> None:
    def create(store: MemoryStore):
        return store.create_current(
            record_id="new", record_class="belief", domain="fact", title="new",
            actor="test", reason="test", evidence={}, idempotency_key="new",
        )

    calls = {
        "create": create,
        "revise": lambda store: store.revise(
            "fact:belief", operation_type="refine", actor="test", reason="test",
            evidence={}, idempotency_key="revise", changes={"summary": "new"},
        ),
        "invalidate": lambda store: store.invalidate(
            "fact:belief", actor="test", reason="test", evidence={},
            idempotency_key="invalidate",
        ),
        "cue-upsert": lambda store: store.add_cue(
            profile="example", cue="first light", target_record_id="phase:first",
            weight=1.0, cue_type="phrase", scope="global",
        ),
        "relation-noop": lambda store: store.add_relation(
            relation_id="phase:first->open-loop->anchor:open-loops",
            from_record_id="phase:first", to_record_id="anchor:open-loops",
            relation_type="open-loop", weight=1.0, source_revision_id=None,
        ),
        "relation-retract": lambda store: store.retract_relation(
            from_record_id="phase:first", to_record_id="anchor:open-loops",
            relation_type="open-loop", actor="test", reason="test",
        ),
        "access": lambda store: store.record_access(
            cue="first", record_id="phase:first",
            revision_id="phase:first@r1-6f2c308629dd", retrieval_reason="cue:first",
            rank=1, surface="test", gain=0.0,
        ),
        "maintenance-replay": lambda store: store.apply_maintenance(
            run_id="fixture:fact:belief:accessibility:0x1.3333333333333p-1",
            adjustments=[], actor="test", reason="replay",
        ),
        "runtime-submit": lambda store: legacy_runtime_submit(store),
        "intake-submit": lambda store: legacy_intake_submit(store),
    }
    for name, call in calls.items():
        memory = legacy_memory(tmp_path, name)
        assert_refused_without_file_changes(memory, lambda call=call, store=memory.store: call(store))


def legacy_runtime_submit(store: MemoryStore):
    memory = IdentityMemory(load_profile("example"), store.db_path, surface="r2b-test")
    return memory.runtime.submit(record_id="fact:belief")


def legacy_intake_submit(store: MemoryStore):
    return ValidatedIntake(store, surface="r2b-test").submit(record_id="fact:belief")


def test_default_retrieve_on_uninitialized_store_stays_a_pure_read(tmp_path: Path) -> None:
    """Tracking only runs on a ready store; a fresh path is read, never created."""
    memory = IdentityMemory(load_profile("example"), tmp_path / "fresh.sqlite3", surface="test")
    packet = memory.retrieve("hello")  # default track=True
    assert packet["items"] == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("version", [3, 4])
def test_migration_default_backup_is_named_after_the_source_version(tmp_path: Path, version: int) -> None:
    import sys

    source = tmp_path / f"source-v{version}.sqlite3"
    if version == 4:
        shutil.copyfile(ROOT / "spec" / "golden" / "identity-open" / "store.sqlite3", source)
    else:
        sys.path.insert(0, str(ROOT / "tools" / "golden"))
        from generate import clock_context, legacy_v3

        with clock_context():
            legacy_v3(source)
    original = source.read_bytes()
    target = tmp_path / "out" / "store.sqlite3"
    dry = MemoryStore(source).migrate_to(target, dry_run=True)
    assert dry["dry_run"] is True
    assert not (tmp_path / "out").exists() or list((tmp_path / "out").iterdir()) == []
    MemoryStore(source).migrate_to(target)
    assert sorted(p.name for p in target.parent.iterdir()) == [
        "store.sqlite3", f"store.sqlite3.v{version}.bak"
    ]
    assert (target.parent / f"store.sqlite3.v{version}.bak").read_bytes() == original
    assert source.read_bytes() == original


def test_decay_sidecar_bytes_are_pinned(tmp_path: Path) -> None:
    """Same literal bytes as node/test/writers.test.ts (spec §6: sidecar bytes exact)."""
    memory = IdentityMemory(load_profile("example"), tmp_path / "store.sqlite3", surface="test")
    memory.bootstrap()
    sidecar = tmp_path / "store.activation.json"
    memory.decay(now="2026-10-21T00:00:00Z")
    assert sidecar.read_text() == '{"last_decay_at": "2026-10-21T00:00:00+00:00"}'
    memory.decay(now="2026-10-22T00:00:00.000500+00:00")
    assert sidecar.read_text() == '{"last_decay_at": "2026-10-22T00:00:00.000500+00:00"}'
