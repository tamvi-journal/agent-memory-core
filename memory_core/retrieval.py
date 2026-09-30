from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .profile import MemoryProfile
from .store import MemoryStore
from .text import normalize_text, tokens


@dataclass
class MemoryHit:
    revision: dict[str, Any]
    score: float
    reasons: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)


class CueDrivenRetriever:
    def __init__(self, store: MemoryStore, profile: MemoryProfile):
        self.store = store
        self.profile = profile

    def retrieve(
        self,
        query: str,
        *,
        scope: str = "global",
        surface: str = "local",
        limit: int = 10,
        token_budget: int = 1800,
        include_history: bool | None = None,
        track_access: bool = True,
        min_accessibility: float | None = None,
        wake_on_direct_cue: bool = True,
        wake_relation_types: tuple[str, ...] = (),
        access_gain: float = 0.01,
    ) -> list[MemoryHit]:
        """Rank current revisions for a cue.

        Dormancy is opt-in. With ``min_accessibility`` set, a revision whose
        accessibility is below it is dormant: lexical overlap and ordinary
        graph spread skip it. Bootstrap records never go dormant. A dormant
        revision wakes on a direct cue (``wake_on_direct_cue``) or when it is
        reached through one of ``wake_relation_types``. Dormancy only changes
        selection; it never changes meaning or telemetry.
        """

        if min_accessibility is not None and not 0.0 <= min_accessibility <= 1.0:
            raise ValueError("min_accessibility must be between 0 and 1")
        wake_types = {str(item) for item in wake_relation_types}
        normalized = normalize_text(query)
        query_tokens = set(tokens(query))
        revisions = {
            item["record_id"]: item
            for item in self.store.current_view()
            if item["authority_status"] != "non_authoritative"
            and item["record_class"] != "unclassified"
            and item["scope"] in {"global", scope}
        }
        scores = {record_id: 0.0 for record_id in revisions}
        reasons = {record_id: [] for record_id in revisions}
        direct: set[str] = set()
        bootstrap = set(self.profile.bootstrap_record_ids)
        dormant: set[str] = (
            {
                record_id
                for record_id, revision in revisions.items()
                if record_id not in bootstrap
                and float(revision["accessibility"]) < min_accessibility
            }
            if min_accessibility is not None
            else set()
        )

        def wake(record_id: str, why: str) -> None:
            dormant.discard(record_id)
            reasons[record_id].append(f"woke:{why}")

        stored_cues = self.store.cue_rows(self.profile.name, scope)
        relations = self.store.active_relation_rows()
        cues: list[dict] = [
            {
                **row,
                # Recompute from the raw cue so rows written under an older
                # normalizer still match text-norm/v2 queries.
                "cue_norm": normalize_text(row["cue"]),
            }
            for row in stored_cues
        ]
        cues.extend(
            {
                "cue": cue,
                "cue_norm": normalize_text(cue),
                "target_record_id": target,
                "weight": weight,
            }
            for cue, target, weight in self.profile.cue_aliases
        )
        strongest: dict[tuple[str, str], dict] = {}
        for cue in cues:
            key = (cue["cue_norm"], cue["target_record_id"])
            if key not in strongest or float(cue["weight"]) > float(
                strongest[key]["weight"]
            ):
                strongest[key] = cue
        for cue in strongest.values():
            target = cue["target_record_id"]
            if target not in revisions:
                continue
            if target in dormant and not wake_on_direct_cue:
                continue
            cue_tokens = set(tokens(cue["cue_norm"]))
            exact = bool(cue["cue_norm"] and cue["cue_norm"] in normalized)
            overlap = len(query_tokens & cue_tokens) / max(1, len(cue_tokens))
            if exact or overlap >= 0.8:
                gain = float(cue["weight"]) * (
                    1.45 if exact else 0.9 * overlap
                )
                if target in dormant:
                    wake(target, "direct-cue")
                scores[target] += gain
                reasons[target].append(f"cue:{cue['cue']}")
                direct.add(target)

        for record_id, revision in revisions.items():
            if record_id in dormant:
                continue
            memory_tokens = set(
                tokens(
                    "\n".join(
                        [
                            revision["title"],
                            revision["summary"],
                            revision["content"],
                        ]
                    )
                )
            )
            if query_tokens and memory_tokens:
                overlap = len(query_tokens & memory_tokens) / max(
                    1, len(query_tokens)
                )
                if overlap:
                    scores[record_id] += min(1.0, overlap) * 0.9
                    reasons[record_id].append(f"lexical:{overlap:.2f}")
                # A match in the title says more than the same words buried in
                # a long body, where common words match almost anything.
                title_tokens = set(tokens(revision["title"]))
                title_overlap = len(query_tokens & title_tokens) / max(
                    1, len(query_tokens)
                )
                if title_overlap:
                    scores[record_id] += min(1.0, title_overlap) * 0.6
                    reasons[record_id].append(f"title:{title_overlap:.2f}")

        for record_id in self.profile.bootstrap_record_ids:
            if record_id in revisions:
                scores[record_id] += 0.34
                reasons[record_id].append("bootstrap")

        frontier = {record_id: scores[record_id] for record_id in sorted(direct)}
        for depth in (1, 2):
            next_frontier: dict[str, float] = {}
            for source_id, activation in frontier.items():
                for relation in relations:
                    if relation["from_record_id"] == source_id:
                        target = relation["to_record_id"]
                    elif relation["to_record_id"] == source_id:
                        target = relation["from_record_id"]
                    else:
                        continue
                    if target not in revisions:
                        continue
                    if target in dormant:
                        if relation["relation_type"] not in wake_types:
                            continue
                        wake(target, f"relation:{relation['relation_type']}")
                    gain = activation * float(relation["weight"]) * (0.46**depth)
                    if gain < 0.05:
                        continue
                    scores[target] += gain
                    reasons[target].append(
                        f"graph:{source_id}-[{relation['relation_type']}]->"
                        f"{target}:d{depth}"
                    )
                    next_frontier[target] = max(
                        next_frontier.get(target, 0.0), gain
                    )
            frontier = next_frontier

        for record_id, revision in revisions.items():
            if scores[record_id] <= 0 or record_id in dormant:
                continue
            scores[record_id] += float(revision["confidence"]) * 0.28
            scores[record_id] += float(revision["salience"]) * 0.14
            scores[record_id] += float(revision["stability"]) * 0.12
            scores[record_id] += float(revision["accessibility"]) * 0.08

        ranked = [
            MemoryHit(revisions[record_id], score, reasons[record_id])
            for record_id, score in scores.items()
            if score >= 0.24 and record_id not in dormant
        ]
        ranked.sort(key=lambda item: (-item.score, item.revision["record_id"]))
        wants_history = (
            include_history
            if include_history is not None
            else any(
                normalize_text(marker) in normalized
                for marker in self.profile.history_markers
            )
        )
        selected: list[MemoryHit] = []
        spent = 0
        for hit in ranked:
            estimate = max(
                1,
                len(
                    (
                        hit.revision["title"]
                        + hit.revision["summary"]
                        + hit.revision["content"]
                    ).split()
                )
                * 2,
            )
            if selected and spent + estimate > token_budget:
                continue
            if len(selected) >= limit:
                break
            if wants_history:
                hit.history = self.store.historical_view(
                    hit.revision["record_id"]
                )
            selected.append(hit)
            spent += estimate
            if track_access:
                self.store.record_access(
                    cue=query,
                    record_id=hit.revision["record_id"],
                    revision_id=hit.revision["revision_id"],
                    retrieval_reason=",".join(hit.reasons[:5]),
                    rank=len(selected),
                    surface=surface,
                    gain=access_gain,
                )
        return selected
