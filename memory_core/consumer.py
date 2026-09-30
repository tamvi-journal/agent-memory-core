from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .profile import MemoryProfile
from .runtime import MemoryRuntime


@dataclass(frozen=True)
class ConsumerBundle:
    """A consumer-owned profile and its provenance-bearing bootstrap seeds."""

    profile: MemoryProfile
    seeds: tuple[dict[str, Any], ...] = ()

    def validate(self) -> dict[str, Any]:
        errors: list[str] = []
        record_ids: list[str] = []
        idempotency_keys: list[str] = []

        if not self.profile.name.strip():
            errors.append("profile name must be non-empty")
        if not self.profile.packet_title.strip():
            errors.append("packet title must be non-empty")

        for index, seed in enumerate(self.seeds):
            prefix = f"seed[{index}]"
            record_id = str(seed.get("record_id", "")).strip()
            idempotency_key = str(seed.get("idempotency_key", "")).strip()
            if not record_id:
                errors.append(f"{prefix} is missing record_id")
            else:
                record_ids.append(record_id)
            if not idempotency_key:
                errors.append(f"{prefix} is missing idempotency_key")
            else:
                idempotency_keys.append(idempotency_key)

            if seed.get("record_class") == "axis":
                falsifier = str(seed.get("falsifier", "")).strip()
                if not falsifier:
                    errors.append(f"{prefix} axis is missing an explicit falsifier")
                evidence = seed.get("evidence", [])
                source_refs = {
                    str(item.get("source_ref", "")).strip()
                    for item in evidence
                    if isinstance(item, dict)
                    and str(item.get("source_ref", "")).strip()
                }
                if len(source_refs) < 2:
                    errors.append(
                        f"{prefix} axis needs at least two distinct evidence sources"
                    )

        if len(record_ids) != len(set(record_ids)):
            errors.append("seed record_ids must be unique")
        if len(idempotency_keys) != len(set(idempotency_keys)):
            errors.append("seed idempotency_keys must be unique")

        missing_bootstrap = sorted(
            set(self.profile.bootstrap_record_ids) - set(record_ids)
        )
        if missing_bootstrap:
            errors.append(
                "bootstrap records have no seed proposal: "
                + ", ".join(missing_bootstrap)
            )

        return {
            "schema": "agent-memory-consumer-bundle-validation/v1",
            "profile": self.profile.name,
            "valid": not errors,
            "errors": errors,
            "seed_count": len(self.seeds),
            "bootstrap_count": len(self.profile.bootstrap_record_ids),
        }

    def require_valid(self) -> None:
        report = self.validate()
        if not report["valid"]:
            raise ValueError("; ".join(report["errors"]))


class ConsumerMemory:
    """Reusable host shell that never becomes the consumer's truth authority."""

    def __init__(
        self,
        db_path: str | Path,
        bundle: ConsumerBundle,
        *,
        surface: str = "local",
    ):
        bundle.require_valid()
        self.bundle = bundle
        self.runtime = MemoryRuntime(
            db_path,
            bundle.profile,
            surface=surface,
        )

    def bootstrap(self) -> list[dict[str, Any]]:
        self.runtime.store.require_writable(allow_uninitialized=True)
        self.runtime.store.initialize()
        return [self.runtime.submit(**seed) for seed in self.bundle.seeds]

    def candidate_context(
        self,
        query: str,
        *,
        scope: str = "global",
        limit: int = 8,
        token_budget: int = 1400,
        include_history: bool | None = None,
    ) -> dict[str, Any]:
        hits = self.runtime.retrieve(
            query,
            scope=scope,
            limit=limit,
            token_budget=token_budget,
            include_history=include_history,
        )
        return {
            "schema": "agent-memory-candidate-context/v1",
            "profile": self.bundle.profile.name,
            "query": query,
            "scope": scope,
            "memory_decides_truth": False,
            "items": [
                {
                    "memory_id": hit.revision["record_id"],
                    "revision_id": hit.revision["revision_id"],
                    "record_class": hit.revision["record_class"],
                    "domain": hit.revision["domain"],
                    "title": hit.revision["title"],
                    "summary": hit.revision["summary"],
                    "content": hit.revision["content"],
                    "confidence": hit.revision["confidence"],
                    "authority_status": hit.revision["authority_status"],
                    "score": hit.score,
                    "retrieval_reasons": list(hit.reasons),
                    "evidence": [
                        {
                            "source_ref": item["source_ref"],
                            "evidence_type": item["evidence_type"],
                            "stance": item["stance"],
                            "confidence": item["confidence"],
                        }
                        for item in self.runtime.store.evidence_for_revision(
                            hit.revision["revision_id"]
                        )
                    ],
                    "history_count": len(hit.history),
                }
                for hit in hits
            ],
        }

    def doctor(self) -> dict[str, Any]:
        bundle = self.bundle.validate()
        store = self.runtime.doctor()
        return {
            **store,
            "passed": bool(bundle["valid"] and store["passed"]),
            "bundle": bundle,
            "store": store,
        }


def bundle_from(
    profile: MemoryProfile,
    seeds: Iterable[dict[str, Any]],
) -> ConsumerBundle:
    return ConsumerBundle(profile=profile, seeds=tuple(seeds))
