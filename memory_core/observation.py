from __future__ import annotations

from typing import Any

from .profile import MemoryProfile
from .retrieval import CueDrivenRetriever
from .store import SCHEMA_VERSION, MemoryStore


def validate_store(store: MemoryStore) -> dict[str, Any]:
    store.initialize()
    return _ready_checks(store)


def _ready_checks(store: MemoryStore) -> dict[str, Any]:
    """The legacy seven checks, without initialization or migration."""
    with store.connect(readonly=True) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = [
            dict(row) for row in conn.execute("PRAGMA foreign_key_check")
        ]
        duplicate_current = conn.execute(
            "SELECT record_id,COUNT(*) AS count FROM memory_current_v3 "
            "GROUP BY record_id HAVING count>1"
        ).fetchall()
        unmigrated_relations = conn.execute(
            "SELECT r.relation_id FROM memory_relations_v3 r "
            "WHERE NOT EXISTS (SELECT 1 FROM memory_relation_events_v4 e "
            "WHERE e.from_record_id=r.from_record_id "
            "AND e.to_record_id=r.to_record_id "
            "AND e.relation_type=r.relation_type)"
        ).fetchall()
        orphan_current = conn.execute(
            "SELECT v.revision_id FROM memory_current_v3 v "
            "LEFT JOIN memory_records_v3 r ON r.record_id=v.record_id "
            "WHERE r.record_id IS NULL"
        ).fetchall()
    checks = {
        "integrity_ok": integrity == "ok",
        "foreign_keys_ok": not foreign_keys,
        "one_current_revision_per_record": not duplicate_current,
        "no_orphan_current_revision": not orphan_current,
        "relations_carried_to_event_stream": not unmigrated_relations,
        "schema_application_id": (
            store.schema_info()["application_id"] != 0
        ),
        "schema_version_current": (
            store.schema_info()["user_version"] == SCHEMA_VERSION
        ),
    }
    return {
        "schema": "memory-core-doctor/v1",
        "passed": all(checks.values()),
        "checks": checks,
        "foreign_key_errors": foreign_keys,
    }


def evaluate_cue_contract(
    store: MemoryStore,
    profile: MemoryProfile,
    cases: list[dict[str, Any]],
    *,
    surface: str = "evaluation",
    limit: int = 10,
) -> dict[str, Any]:
    retriever = CueDrivenRetriever(store, profile)
    results: list[dict[str, Any]] = []
    for case in cases:
        hits = retriever.retrieve(
            case["cue"],
            scope=case.get("scope", "global"),
            surface=surface,
            limit=limit,
        )
        selected = {hit.revision["record_id"] for hit in hits}
        required = set(case.get("required_ids", []))
        results.append(
            {
                "name": case.get("name", case["cue"]),
                "cue": case["cue"],
                "required_ids": sorted(required),
                "missing": sorted(required - selected),
                "passed": not (required - selected),
            }
        )
    return {
        "schema": "memory-core-cue-evaluation/v1",
        "passed": all(item["passed"] for item in results),
        "cases": results,
    }


def doctor(store: MemoryStore) -> dict[str, Any]:
    """Non-initializing CLI diagnostic; profile resolution is a separate surface."""
    state = store.schema_info()["state"]
    result = {"schema": "memory-core-doctor/v2", "state": state, "passed": False}
    if state == "uninitialized":
        result.update(state="missing", action="init")
    elif state.startswith("legacy-v"):
        result["action"] = "migrate-to"
    elif state == "incompatible":
        with store.connect(readonly=True):
            pass  # schema assertion raises the existing typed error
    elif state == "ready":
        checks = _ready_checks(store)
        result.update(passed=checks["passed"], checks=checks["checks"],
                      foreign_key_errors=checks["foreign_key_errors"])
    return result
