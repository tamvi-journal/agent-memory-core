from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .store import (
    MemoryStore,
    canonical_evidence_identity,
    hash_payload,
    semantic_hash,
    utc_now,
)


@dataclass(frozen=True)
class GovernancePolicy:
    event_min_confidence: float = 0.6
    belief_min_confidence: float = 0.7
    axis_min_confidence: float = 0.75
    axis_min_independent_sources: int = 2
    protected_domains: tuple[str, ...] = ("boundary",)
    protected_safe_effects: tuple[str, ...] = ("strengthen", "clarify")
    protected_effects_requiring_authority: tuple[str, ...] = (
        "weaken",
        "delete",
        "reduce_autonomy",
    )


class ValidatedIntake:
    def __init__(
        self,
        store: MemoryStore,
        *,
        surface: str,
        policy: GovernancePolicy | None = None,
    ):
        self.store = store
        self.surface = surface
        self.policy = policy or GovernancePolicy()

    def preview(self, **proposal: Any) -> dict[str, Any]:
        payload = self._normalized_proposal(proposal, require_key=False)
        self._validate(payload, check_existence=True)
        status, reason = self._evaluate(payload)
        return {
            "schema": "memory-core-intake-preview/v1",
            "status": status,
            "decision_reason": reason,
            "record_id": payload["record_id"],
            "proposal_sha256": hash_payload(payload),
            "wrote_state": False,
        }

    def submit(self, **proposal: Any) -> dict[str, Any]:
        payload = self._normalized_proposal(proposal, require_key=True)
        self.store._guard_pinned(payload["record_id"])
        self.store.init()
        proposal_sha256 = hash_payload(payload)
        prior = self._intake_by_key(payload["idempotency_key"])
        if prior:
            if prior["proposal_sha256"] != proposal_sha256:
                raise ValueError(
                    "idempotency_key already exists with a different proposal"
                )
            return prior
        self._validate(payload, check_existence=True)
        status, decision_reason = self._evaluate(payload)
        evidence_ids = self._capture_evidence(payload)
        intake_id = "intake:" + hashlib.sha256(
            payload["idempotency_key"].encode("utf-8")
        ).hexdigest()[:32]
        target = None if payload["operation_type"] == "create" else payload["record_id"]
        self._insert_intake(
            intake_id=intake_id,
            payload=payload,
            proposal_sha256=proposal_sha256,
            evidence_ids=evidence_ids,
            target_record_id=target,
        )
        if status in {"held", "rejected", "no_op"}:
            self._decide_intake(
                intake_id,
                status=status,
                reason=decision_reason,
                target_record_id=target,
            )
            return self._intake_by_key(payload["idempotency_key"])

        primary = dict(payload["evidence"][0])
        primary.setdefault("actor", payload["actor"])
        primary.setdefault("surface", self.surface)
        primary.setdefault("model_family", payload["model_family"])
        primary.setdefault("privacy_class", "private")
        operation_key = f"intake:{payload['idempotency_key']}"
        operation_type = payload["operation_type"]
        if operation_type == "create":
            values = payload["changes"]
            result = self.store.create_current(
                record_id=payload["record_id"],
                record_class=payload["record_class"],
                domain=payload["domain"],
                title=str(values.get("title", "")),
                summary=str(values.get("summary", "")),
                content=str(values.get("content", "")),
                impact=str(values.get("impact", "")),
                confidence=float(values.get("confidence", 0.7)),
                salience=float(values.get("salience", 0.5)),
                stability=float(values.get("stability", 0.5)),
                accessibility=float(values.get("accessibility", 0.5)),
                authority_status=str(
                    values.get(
                        "authority_status",
                        "protected"
                        if payload["domain"] in self.policy.protected_domains
                        else "canonical_reference",
                    )
                ),
                scope=payload["scope"],
                actor=payload["actor"],
                surface=self.surface,
                model_family=payload["model_family"],
                reason=payload["reason"],
                evidence=primary,
                idempotency_key=operation_key,
            )
        elif operation_type == "invalidate":
            result = self.store.invalidate(
                payload["record_id"],
                actor=payload["actor"],
                surface=self.surface,
                reason=payload["reason"],
                evidence=primary,
                idempotency_key=operation_key,
            )
        else:
            result = self.store.revise(
                payload["record_id"],
                operation_type=operation_type,
                actor=payload["actor"],
                surface=self.surface,
                model_family=payload["model_family"],
                reason=payload["reason"],
                evidence=primary,
                idempotency_key=operation_key,
                changes=payload["changes"],
            )
        self._link_evidence(
            result.get("target_revision_id"), evidence_ids, payload["reason"]
        )
        self._decide_intake(
            intake_id,
            status="materialized",
            reason=decision_reason,
            target_record_id=payload["record_id"],
            operation_id=result["operation_id"],
        )
        return self._intake_by_key(payload["idempotency_key"])

    def _normalized_proposal(
        self, value: dict[str, Any], *, require_key: bool
    ) -> dict[str, Any]:
        payload = {
            "operation_type": value.get("operation_type"),
            "record_id": value.get("record_id"),
            "record_class": value.get("record_class"),
            "domain": value.get("domain"),
            "scope": value.get("scope", "global"),
            "actor": value.get("actor"),
            "surface": self.surface,
            "model_family": value.get("model_family", ""),
            "reason": value.get("reason"),
            "logic": value.get("logic"),
            "truth_basis": value.get("truth_basis"),
            "falsifier": value.get("falsifier", ""),
            "unresolved_conflict": bool(value.get("unresolved_conflict", False)),
            "protected_effect": value.get("protected_effect", "neutral"),
            "protected_authorized": bool(
                value.get("protected_authorized", False)
            ),
            "changes": value.get("changes") or {},
            "evidence": value.get("evidence") or [],
            "idempotency_key": value.get("idempotency_key")
            or ("" if require_key else "preview-only"),
        }
        return payload

    def _validate(
        self, payload: dict[str, Any], *, check_existence: bool
    ) -> None:
        if payload["operation_type"] not in {
            "create",
            "correct",
            "refine",
            "supersede",
            "invalidate",
        }:
            raise ValueError("unsupported operation_type")
        for name in (
            "record_id",
            "actor",
            "reason",
            "logic",
            "truth_basis",
            "idempotency_key",
        ):
            value = payload[name]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty text")
        if not payload["evidence"]:
            raise ValueError("at least one provenance-bearing evidence item is required")
        for item in payload["evidence"]:
            if not item.get("source_ref") or not item.get("content_summary"):
                raise ValueError(
                    "each evidence item requires source_ref and content_summary"
                )
            confidence = float(item.get("confidence", 0.0))
            if not 0.0 <= confidence <= 1.0:
                raise ValueError("evidence confidence must be between 0 and 1")
        if payload["operation_type"] == "create":
            if payload["record_class"] not in {"event", "belief", "axis"}:
                raise ValueError("create requires event, belief, or axis")
            if not payload["domain"] or not payload["changes"].get("title"):
                raise ValueError("create requires domain and changes.title")
            if check_existence and (
                self.store.current_view(payload["record_id"])
                or self.store.historical_view(payload["record_id"])
            ):
                raise ValueError("record already exists")
        elif check_existence and not self.store.historical_view(
            payload["record_id"]
        ):
            raise ValueError("target record does not exist")

    def _evaluate(self, payload: dict[str, Any]) -> tuple[str, str]:
        current_rows = self.store.current_view(payload["record_id"])
        current = current_rows[0] if current_rows else None
        record_class = payload["record_class"] or (
            current["record_class"] if current else ""
        )
        domain = payload["domain"] or (current["domain"] if current else "")
        protected = bool(
            domain in self.policy.protected_domains
            or (current and current["authority_status"] == "protected")
        )
        if protected and not payload["protected_authorized"]:
            weakening = (
                payload["operation_type"] == "invalidate"
                or payload["protected_effect"]
                in self.policy.protected_effects_requiring_authority
                or (
                    current
                    and current["authority_status"] == "protected"
                    and payload["changes"].get(
                        "authority_status", "protected"
                    )
                    != "protected"
                )
            )
            if weakening:
                return "held", "protected weakening requires host authority"
            if (
                payload["operation_type"] != "create"
                and payload["protected_effect"]
                not in self.policy.protected_safe_effects
            ):
                return (
                    "held",
                    "protected revisions must declare a configured safe effect",
                )
        if payload["unresolved_conflict"]:
            return "held", "unresolved contradictory evidence"
        minimum = (
            self.policy.event_min_confidence
            if record_class == "event"
            else self.policy.axis_min_confidence
            if record_class == "axis"
            else self.policy.belief_min_confidence
        )
        confidences = [
            float(item.get("confidence", 0.0)) for item in payload["evidence"]
        ]
        if min(confidences) < minimum:
            return "held", f"evidence confidence is below {minimum:.2f}"
        if record_class == "axis":
            sources = {
                canonical_evidence_identity(item)["independence_group"]
                for item in payload["evidence"]
            }
            if not str(payload["falsifier"]).strip():
                return "held", "axis evolution requires an explicit falsifier"
            if len(sources) < self.policy.axis_min_independent_sources:
                return "held", "axis evolution lacks independent evidence"
        if (
            payload["operation_type"]
            in {"correct", "refine", "supersede"}
            and current
        ):
            desired = {
                key: current[key]
                for key in (
                    "title",
                    "summary",
                    "content",
                    "impact",
                    "confidence",
                    "authority_status",
                )
            }
            desired.update(payload["changes"])
            if semantic_hash(desired) == current["content_sha256"]:
                return "no_op", "proposal does not change semantic content"
        return "materialized", "logic, truth basis, provenance, and evidence passed"

    def _capture_evidence(self, payload: dict[str, Any]) -> list[str]:
        evidence_ids: list[str] = []
        with self.store.connect() as conn:
            for item in payload["evidence"]:
                evidence = dict(item)
                evidence.setdefault("actor", payload["actor"])
                evidence.setdefault("surface", self.surface)
                evidence.setdefault("model_family", payload["model_family"])
                evidence.setdefault("privacy_class", "private")
                evidence_ids.append(self.store._insert_evidence(conn, evidence))
        return evidence_ids

    def _link_evidence(
        self, revision_id: str | None, evidence_ids: list[str], reason: str
    ) -> None:
        if not revision_id:
            return
        with self.store.connect() as conn:
            for evidence_id in evidence_ids:
                self.store._link_evidence(
                    conn, revision_id, evidence_id, "supports", reason
                )

    def _insert_intake(
        self,
        *,
        intake_id: str,
        payload: dict[str, Any],
        proposal_sha256: str,
        evidence_ids: list[str],
        target_record_id: str | None,
    ) -> None:
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO memory_intake_v3("
                "intake_id,operation_type,target_record_id,proposal_sha256,"
                "evidence_ids_json,status,decision_reason,operation_id,actor,"
                "surface,idempotency_key,created_at,decided_at"
                ") VALUES(?,?,?,?,?,'received','',NULL,?,?,?,?,NULL)",
                (
                    intake_id,
                    payload["operation_type"],
                    target_record_id,
                    proposal_sha256,
                    json.dumps(evidence_ids, ensure_ascii=False),
                    payload["actor"],
                    self.surface,
                    payload["idempotency_key"],
                    utc_now(),
                ),
            )

    def _decide_intake(
        self,
        intake_id: str,
        *,
        status: str,
        reason: str,
        target_record_id: str | None,
        operation_id: str | None = None,
    ) -> None:
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE memory_intake_v3 SET status=?,decision_reason=?,"
                "target_record_id=?,operation_id=?,decided_at=? WHERE intake_id=?",
                (
                    status,
                    reason,
                    target_record_id,
                    operation_id,
                    utc_now(),
                    intake_id,
                ),
            )

    def _intake_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM memory_intake_v3 WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["evidence_ids"] = json.loads(
            result.pop("evidence_ids_json") or "[]"
        )
        return result
