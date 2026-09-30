from __future__ import annotations

import re
from pathlib import Path

import pytest

from memory_core import (
    ConsumerBundle,
    ConsumerMemory,
    CueDrivenRetriever,
    MemoryProfile,
    MemoryRuntime,
    MemoryStore,
    PacketRenderer,
    ValidatedIntake,
    evaluate_cue_contract,
    validate_store,
    VHO_SOURCE_SHA256,
    VHO_STATUS,
    vho_open_seed,
)


def evidence(label: str, confidence: float = 0.9) -> dict:
    return {
        "evidence_type": "synthetic",
        "source_ref": f"test:{label}",
        "content_summary": label,
        "confidence": confidence,
        "privacy_class": "synthetic",
        "source_payload": {"label": label},
    }


def writer(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()
    return store, ValidatedIntake(store, surface="test")


def axis_seed(
    record_id: str = "example-axis",
    evidence_items: list[dict] | None = None,
    falsifier: str = "Verified counterexamples outperform this update law.",
) -> dict:
    return {
        "operation_type": "create",
        "record_id": record_id,
        "record_class": "axis",
        "domain": "logic",
        "actor": "consumer",
        "reason": "Seed a thin tested update law.",
        "logic": "The independent sources require the same update behavior.",
        "truth_basis": "Two bounded sources are attached.",
        "falsifier": falsifier,
        "evidence": evidence_items
        or [evidence("axis-source-a"), evidence("axis-source-b")],
        "idempotency_key": f"{record_id}:v1",
        "changes": {
            "title": "Example update law",
            "summary": "Revise with evidence and preserve history.",
        },
    }


def test_revision_history_is_append_only_and_idempotent(tmp_path):
    store, intake = writer(tmp_path)
    created = intake.submit(
        operation_type="create",
        record_id="update-law",
        record_class="belief",
        domain="semantic",
        actor="agent",
        reason="initial claim",
        logic="The claim follows from the evidence.",
        truth_basis="A bounded source is attached.",
        evidence=[evidence("initial")],
        idempotency_key="create:update-law",
        changes={"title": "Update law", "summary": "Use evidence."},
    )
    revised = intake.submit(
        operation_type="refine",
        record_id="update-law",
        actor="agent",
        reason="new evidence adds precision",
        logic="The second claim contains the first and a stricter rule.",
        truth_basis="A newer bounded source is attached.",
        evidence=[evidence("revised")],
        idempotency_key="refine:update-law",
        changes={"summary": "Use evidence and preserve history."},
    )
    repeated = intake.submit(
        operation_type="refine",
        record_id="update-law",
        actor="agent",
        reason="new evidence adds precision",
        logic="The second claim contains the first and a stricter rule.",
        truth_basis="A newer bounded source is attached.",
        evidence=[evidence("revised")],
        idempotency_key="refine:update-law",
        changes={"summary": "Use evidence and preserve history."},
    )

    assert created["status"] == "materialized"
    assert revised["intake_id"] == repeated["intake_id"]
    history = store.historical_view("update-law")
    assert [item["revision_status"] for item in history] == [
        "superseded",
        "current",
    ]
    assert [item["summary"] for item in history] == [
        "Use evidence.",
        "Use evidence and preserve history.",
    ]
    current_revision_id = store.current_view("update-law")[0]["revision_id"]
    linked = store.evidence_for_revision(current_revision_id)
    assert [item["source_ref"] for item in linked] == ["test:revised"]
    assert linked[0]["stance"] == "supports"


def test_retrieval_changes_accessibility_not_semantic_hash(tmp_path):
    store, intake = writer(tmp_path)
    intake.submit(
        operation_type="create",
        record_id="project-choice",
        record_class="belief",
        domain="project",
        actor="agent",
        reason="verified choice",
        logic="The implementation and test outcome agree.",
        truth_basis="The verification report is the source.",
        evidence=[evidence("choice")],
        idempotency_key="create:choice",
        changes={
            "title": "Project choice",
            "summary": "Use the verified implementation.",
        },
    )
    store.add_cue(
        profile="test-profile",
        cue="verified implementation",
        target_record_id="project-choice",
        weight=1.8,
    )
    before = store.current_view("project-choice")[0]
    profile = MemoryProfile(
        name="test-profile",
        packet_title="TEST PACKET",
        section_order=("project",),
    )
    hits = CueDrivenRetriever(store, profile).retrieve(
        "verified implementation",
        surface="test",
    )
    after = store.current_view("project-choice")[0]

    assert hits[0].revision["record_id"] == "project-choice"
    assert after["content_sha256"] == before["content_sha256"]
    assert after["accessibility"] > before["accessibility"]


def test_maintenance_is_audited_idempotent_and_non_semantic(tmp_path):
    store, intake = writer(tmp_path)
    intake.submit(
        operation_type="create",
        record_id="dynamic-memory",
        record_class="belief",
        domain="semantic",
        actor="agent",
        reason="verified memory",
        logic="The claim is supported by bounded evidence.",
        truth_basis="The test fixture is the source.",
        evidence=[evidence("maintenance")],
        idempotency_key="maintenance:create",
        changes={
            "title": "Dynamic memory",
            "summary": "Regulation can change without rewriting meaning.",
        },
    )
    before = store.current_view("dynamic-memory")[0]
    result = store.apply_maintenance(
        run_id="maintenance-1",
        adjustments=[
            {
                "record_id": "dynamic-memory",
                "field": "salience",
                "old_value": before["salience"],
                "new_value": 0.84,
            },
            {
                "record_id": "dynamic-memory",
                "field": "stability",
                "old_value": before["stability"],
                "new_value": 0.76,
            },
        ],
        actor="host-maintenance",
        surface="test",
        reason="evidence-backed regulation",
    )
    duplicate = store.apply_maintenance(
        run_id="maintenance-1",
        adjustments=[],
        actor="host-maintenance",
        reason="idempotent retry",
    )
    after = store.current_view("dynamic-memory")[0]

    assert result["operation_type"] == "maintenance"
    assert result["decision"] == "materialized"
    assert duplicate["operation_id"] == result["operation_id"]
    assert result["details"]["semantic_content_changed"] is False
    assert after["salience"] == 0.84
    assert after["stability"] == 0.76
    assert after["content_sha256"] == before["content_sha256"]
    assert len(store.historical_view("dynamic-memory")) == 1


def test_maintenance_invalid_batch_rolls_back(tmp_path):
    store, intake = writer(tmp_path)
    intake.submit(
        operation_type="create",
        record_id="dynamic-memory",
        record_class="belief",
        domain="semantic",
        actor="agent",
        reason="verified memory",
        logic="The claim is supported by bounded evidence.",
        truth_basis="The test fixture is the source.",
        evidence=[evidence("maintenance-rollback")],
        idempotency_key="maintenance:create",
        changes={"title": "Dynamic memory"},
    )
    before = store.current_view("dynamic-memory")[0]

    with pytest.raises(ValueError, match="unsupported maintenance field"):
        store.apply_maintenance(
            run_id="maintenance-invalid",
            adjustments=[
                {
                    "record_id": "dynamic-memory",
                    "field": "salience",
                    "new_value": 0.91,
                },
                {
                    "record_id": "dynamic-memory",
                    "field": "summary",
                    "new_value": 1.0,
                },
            ],
            actor="host-maintenance",
            reason="invalid mixed batch",
        )

    after = store.current_view("dynamic-memory")[0]
    assert after["salience"] == before["salience"]
    assert after["content_sha256"] == before["content_sha256"]


def test_consumer_neutral_runtime_wires_profile_without_owning_identity(tmp_path):
    profile = MemoryProfile(
        name="example-consumer",
        packet_title="EXAMPLE MEMORY",
        cue_aliases=(("verified choice", "project-choice", 1.8),),
        section_order=("project",),
        default_instructions=("Memory is candidate context.",),
    )
    runtime = MemoryRuntime(
        tmp_path / "consumer.sqlite3",
        profile,
        surface="consumer-test",
    )
    runtime.store.initialize()
    created = runtime.submit(
        operation_type="create",
        record_id="project-choice",
        record_class="belief",
        domain="project",
        actor="consumer",
        reason="verified project choice",
        logic="The implementation and verification result agree.",
        truth_basis="A bounded test source is attached.",
        evidence=[evidence("runtime")],
        idempotency_key="runtime:create",
        changes={
            "title": "Project choice",
            "summary": "Use the verified path.",
        },
    )
    context = runtime.render_context("verified choice")
    maintenance = runtime.apply_maintenance(
        run_id="runtime-maintenance",
        adjustments=[
            {
                "record_id": "project-choice",
                "field": "salience",
                "new_value": 0.8,
            }
        ],
        actor="consumer",
        reason="bounded host maintenance",
    )

    assert created["status"] == "materialized"
    assert "project-choice" in context
    assert maintenance["decision"] == "materialized"
    assert runtime.doctor()["passed"] is True


def test_history_is_loaded_only_by_history_cue(tmp_path):
    store, intake = writer(tmp_path)
    intake.submit(
        operation_type="create",
        record_id="changing-view",
        record_class="belief",
        domain="semantic",
        actor="agent",
        reason="initial",
        logic="Initial evidence supports the first view.",
        truth_basis="Source one.",
        evidence=[evidence("view-one")],
        idempotency_key="create:view",
        changes={"title": "Changing view", "summary": "First view."},
    )
    intake.submit(
        operation_type="correct",
        record_id="changing-view",
        actor="agent",
        reason="correction",
        logic="The second source contradicts a detail in the first.",
        truth_basis="Source two is newer and directly measured.",
        evidence=[evidence("view-two")],
        idempotency_key="correct:view",
        changes={"summary": "Corrected view."},
    )
    profile = MemoryProfile(
        name="test",
        packet_title="TEST",
        cue_aliases=(("view", "changing-view", 1.8),),
        history_markers=("history",),
    )
    retriever = CueDrivenRetriever(store, profile)

    current = retriever.retrieve("view", surface="test")
    historical = retriever.retrieve("view history", surface="test")

    assert current[0].history == []
    assert len(historical[0].history) == 2
    packet = PacketRenderer(profile).render(
        "view history",
        historical,
        scope="global",
        surface="test",
    )
    assert "r1 [superseded]" in packet
    assert "r2 [current]" in packet


def test_axis_requires_falsifier_and_independent_sources(tmp_path):
    store, intake = writer(tmp_path)
    held = intake.submit(
        operation_type="create",
        record_id="reasoning-axis",
        record_class="axis",
        domain="identity",
        actor="agent",
        reason="axis proposal",
        logic="A stable update law is proposed.",
        truth_basis="Only one source exists.",
        evidence=[evidence("axis-one")],
        idempotency_key="axis:held",
        changes={"title": "Reasoning axis", "summary": "Prefer evidence."},
    )
    accepted = intake.submit(
        operation_type="create",
        record_id="reasoning-axis",
        record_class="axis",
        domain="identity",
        actor="agent",
        reason="tested axis",
        logic="The same law survives two independent cases.",
        truth_basis="Two independent sources support it.",
        falsifier="A repeatable case where stronger evidence should be ignored.",
        evidence=[evidence("axis-two"), evidence("axis-three")],
        idempotency_key="axis:accepted",
        changes={
            "title": "Reasoning axis",
            "summary": "Preserve logic and update toward stronger evidence.",
        },
    )

    assert held["status"] == "held"
    assert accepted["status"] == "materialized"
    assert store.current_view("reasoning-axis")[0]["record_class"] == "axis"


def test_protected_weakening_is_held_without_host_authority(tmp_path):
    store, intake = writer(tmp_path)
    intake.submit(
        operation_type="create",
        record_id="owner-boundary",
        record_class="belief",
        domain="boundary",
        actor="agent",
        reason="protected constraint",
        logic="The owner retains control.",
        truth_basis="The host application declares this boundary.",
        evidence=[evidence("boundary")],
        idempotency_key="boundary:create",
        changes={"title": "Owner boundary", "summary": "Owner control remains."},
    )
    held = intake.submit(
        operation_type="supersede",
        record_id="owner-boundary",
        actor="agent",
        reason="unauthorized change",
        logic="The requested change would lower owner control.",
        truth_basis="No host authority was supplied.",
        evidence=[evidence("weakening")],
        idempotency_key="boundary:held",
        protected_effect="reduce_autonomy",
        changes={"summary": "Agent control replaces owner control."},
    )
    accepted = intake.submit(
        operation_type="refine",
        record_id="owner-boundary",
        actor="agent",
        reason="safe clarification",
        logic="The clarification makes the existing constraint auditable.",
        truth_basis="The source repeats the same protected direction.",
        evidence=[evidence("clarification")],
        idempotency_key="boundary:clarify",
        protected_effect="clarify",
        changes={"summary": "Owner control remains explicit and auditable."},
    )

    assert held["status"] == "held"
    assert accepted["status"] == "materialized"
    assert len(store.historical_view("owner-boundary")) == 2


def test_conflict_is_held_and_preview_never_writes(tmp_path):
    store, intake = writer(tmp_path)
    preview = intake.preview(
        operation_type="create",
        record_id="conflicted",
        record_class="belief",
        domain="semantic",
        actor="agent",
        reason="conflict test",
        logic="Two claims cannot both be current.",
        truth_basis="The conflict remains unresolved.",
        evidence=[evidence("conflict")],
        unresolved_conflict=True,
        changes={"title": "Conflicted claim"},
    )

    assert preview["status"] == "held"
    assert preview["wrote_state"] is False
    assert store.historical_view("conflicted") == []


def test_doctor_and_cue_contract(tmp_path):
    store, intake = writer(tmp_path)
    intake.submit(
        operation_type="create",
        record_id="cue-target",
        record_class="event",
        domain="interaction",
        actor="agent",
        reason="cue contract",
        logic="The event is directly observed.",
        truth_basis="The bounded source records it.",
        evidence=[evidence("cue", confidence=0.7)],
        idempotency_key="cue:create",
        changes={"title": "Cue target", "summary": "Directly retrievable."},
    )
    store.add_cue(
        profile="test",
        cue="direct target",
        target_record_id="cue-target",
        weight=2.0,
    )
    profile = MemoryProfile(name="test", packet_title="TEST")
    evaluation = evaluate_cue_contract(
        store,
        profile,
        [{"name": "direct", "cue": "direct target", "required_ids": ["cue-target"]}],
    )

    assert validate_store(store)["passed"]
    assert evaluation["passed"]


def test_consumer_bundle_bootstraps_idempotently_and_returns_candidates(
    tmp_path,
):
    profile = MemoryProfile(
        name="example-consumer",
        packet_title="EXAMPLE MEMORY",
        bootstrap_record_ids=("example-axis",),
        cue_aliases=(("reasoning", "example-axis", 1.8),),
    )
    bundle = ConsumerBundle(profile=profile, seeds=(axis_seed(),))
    consumer = ConsumerMemory(
        tmp_path / "consumer.sqlite3",
        bundle,
        surface="consumer-test",
    )

    first = consumer.bootstrap()
    second = consumer.bootstrap()
    context = consumer.candidate_context("reasoning")
    doctor = consumer.doctor()

    assert first[0]["intake_id"] == second[0]["intake_id"]
    assert context["memory_decides_truth"] is False
    assert context["items"][0]["memory_id"] == "example-axis"
    assert len(context["items"][0]["evidence"]) == 2
    assert doctor["bundle"]["valid"] is True
    assert doctor["store"]["passed"] is True


def test_open_vho_anchor_bootstraps_without_owning_consumer_identity(tmp_path):
    profile = MemoryProfile(
        name="child-consumer",
        packet_title="CHILD MEMORY",
        bootstrap_record_ids=("vho-open-ontology-core",),
        cue_aliases=(("condition continuity", "vho-open-ontology-core", 2.0),),
    )
    seed = vho_open_seed(
        actor="child-consumer",
        adoption="foundation",
        consumer_notes="Preserve a distinct child trajectory.",
    )
    consumer = ConsumerMemory(
        tmp_path / "child.sqlite3",
        ConsumerBundle(profile=profile, seeds=(seed,)),
        surface="consumer-test",
    )

    result = consumer.bootstrap()
    context = consumer.candidate_context("condition continuity")

    assert VHO_STATUS == "OPEN"
    assert result[0]["status"] == "materialized"
    assert context["items"][0]["memory_id"] == "vho-open-ontology-core"
    assert "stacked" in context["items"][0]["content"].lower()
    assert "distinct child trajectory" in context["items"][0]["content"]


def test_open_vho_source_hash_matches_canonical_document():
    root = Path(__file__).parents[1]
    source = (root / "docs" / "VECTOR-HUMAN-ONTOLOGY.md").read_bytes()

    import hashlib

    assert hashlib.sha256(source).hexdigest() == VHO_SOURCE_SHA256


@pytest.mark.parametrize(
    ("seed", "message"),
    [
        (
            axis_seed(evidence_items=[evidence("only-one")]),
            "at least two distinct evidence sources",
        ),
        (
            axis_seed(falsifier=""),
            "missing an explicit falsifier",
        ),
    ],
)
def test_consumer_bundle_rejects_under_evidenced_axis(seed, message):
    profile = MemoryProfile(
        name="unsafe-consumer",
        packet_title="UNSAFE",
        bootstrap_record_ids=("example-axis",),
    )
    bundle = ConsumerBundle(profile=profile, seeds=(seed,))

    assert bundle.validate()["valid"] is False
    with pytest.raises(ValueError, match=message):
        bundle.require_valid()


def test_core_contains_no_consumer_identity_or_private_seed():
    root = Path(__file__).parents[1]
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for package in ("memory_core", "trajecta_identity")
        for path in sorted((root / package).glob("**/*"))
        if path.is_file() and path.suffix in {".py", ".sql"}
    )
    forbidden = (
        r"\bLam\b",
        r"\bTy\b",
        r"lam-ty-primary",
        r"ty-carbon-witness",
        r"relationship history",
    )
    assert not any(re.search(pattern, text) for pattern in forbidden)
