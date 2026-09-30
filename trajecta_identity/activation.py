"""Activation, hibernation and the ego guard (SPEC §3).

Activation is kernel telemetry (``accessibility``). Changing it never changes
meaning. Pinned records (core, ontology, anchors) never decay.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from memory_core import MemoryStore


@dataclass(frozen=True)
class ActivationPolicy:
    new_record: float = 0.6
    dormant_below: float = 0.15
    fading_below: float = 0.35
    cap: float = 0.9
    direct_gain: float = 0.08
    graph_gain: float = 0.03
    wake_to: float = 0.4
    half_life_days: float = 21.0


def state_of(row: dict[str, Any], policy: ActivationPolicy, pinned: Iterable[str]) -> str:
    if row["record_id"] in set(pinned):
        return "pinned"
    value = float(row["accessibility"])
    if value < policy.dormant_below:
        return "dormant"
    if value < policy.fading_below:
        return "fading"
    return "active"


def _gain(value: float, gain: float, cap: float) -> float:
    """Diminishing gain: the closer to the cap, the less each recall adds."""

    if value >= cap:
        return cap
    return min(cap, value + gain * (1.0 - value / cap))


def apply_recall(store: MemoryStore, hits, policy: ActivationPolicy, *, pinned: Iterable[str]) -> list[dict]:
    """Raise accessibility for recalled records; wake dormant ones.

    Stability is never touched here: recalling a self-description again is not
    evidence that it is truer (ego guard).
    """

    store.require_writable()
    pinned = set(pinned)
    adjustments = []
    for hit in hits:
        revision = hit.revision
        if revision["record_id"] in pinned:
            continue
        current = store.current_view(revision["record_id"])
        if not current:
            continue
        value = float(current[0]["accessibility"])
        direct = any(reason.startswith(("cue:", "lexical:")) for reason in hit.reasons)
        new = _gain(value, policy.direct_gain if direct else policy.graph_gain, policy.cap)
        if any(reason.startswith("woke:") for reason in hit.reasons):
            new = max(new, policy.wake_to)
        if abs(new - value) > 1e-9:
            adjustments.append({
                "record_id": revision["record_id"],
                "field": "accessibility",
                "old_value": value,
                "new_value": round(new, 6),
            })
    if adjustments:
        stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        store.apply_maintenance(
            run_id=f"recall:{stamp}",
            adjustments=adjustments,
            actor="trajecta-identity",
            reason="recall activation (diminishing gain, capped)",
            surface="activation",
        )
    return adjustments


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _state_path(store: MemoryStore) -> Path:
    return store.db_path.with_suffix(".activation.json")


def run_decay(
    store: MemoryStore,
    policy: ActivationPolicy,
    *,
    pinned: Iterable[str],
    now: str | None = None,
) -> dict[str, Any]:
    """Fade records by elapsed time since the later of last recall / last decay.

    Half-life scales with stability. Also enforces the cap. Safe to run often:
    each run only applies the time elapsed since the previous one.
    """

    store.require_writable()
    pinned = set(pinned)
    moment = _parse(now) or datetime.now(timezone.utc)
    state_file = _state_path(store)
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    last_run = _parse(state.get("last_decay_at"))
    adjustments = []
    counts: dict[str, int] = {}
    for row in store.current_view():
        if row["record_id"] in pinned or row["domain"] == "anchor":
            continue
        value = float(row["accessibility"])
        since = max(
            filter(None, [last_run, _parse(row["last_accessed_at"]), _parse(row["created_at"])])
        )
        days = max(0.0, (moment - since).total_seconds() / 86400.0)
        half_life = policy.half_life_days * (0.5 + float(row["stability"]))
        new = min(policy.cap, value * 0.5 ** (days / half_life))
        if abs(new - value) > 1e-6:
            adjustments.append({
                "record_id": row["record_id"],
                "field": "accessibility",
                "old_value": value,
                "new_value": round(new, 6),
            })
        shown = dict(row, accessibility=new)
        name = state_of(shown, policy, pinned)
        counts[name] = counts.get(name, 0) + 1
    if adjustments:
        store.apply_maintenance(
            run_id=f"decay:{moment.isoformat()}",
            adjustments=adjustments,
            actor="trajecta-identity",
            reason="time decay (half-life scaled by stability) and cap",
            surface="activation",
        )
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"last_decay_at": moment.isoformat()}))
    return {"at": moment.isoformat(), "adjusted": len(adjustments), "states": counts}
