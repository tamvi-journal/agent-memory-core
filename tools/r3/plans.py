"""Pure seeded plans and serial interleavings; no expected outcomes here."""
from __future__ import annotations

import hashlib
import itertools

SURFACES = tuple(itertools.product(("py", "ts"), ("cli", "mcp")))


def step(runtime, surface, operation, **arguments):
    return {"runtime": runtime, "surface": surface, "operation": operation, "arguments": arguments}


def fixed_plans():
    plans = []
    for index, (a, b) in enumerate(itertools.product(SURFACES, repeat=2)):
        plans.append({"name": f"surface-{index}", "pair": [a, b], "steps": [
            step(*a, "log-phase", event_id="cross", title="Cross cue", summary="synthetic", open_loop=True),
            step(*b, "retrieve", cue="Cross cue", track=True),
            step(*a, "status"), step(*b, "timeline"), step(*a, "core-proposals"),
            step(*b, "close-loop", record_id="phase:cross", note="synthetic close"),
            step(*a, "retrieve", cue="Cross cue", track=False),
            step(a[0], "cli", "decay"), step(b[0], "cli", "doctor"),
        ]})
    for kind, (issuer, consumer) in itertools.product(("apply", "reject", "retract", "legacy-close"), (("py", "ts"), ("ts", "py"))):
        plans.append({"name": f"receipt-{kind}-{issuer}", "receipt": kind, "issuer": issuer,
                      "consumer": consumer, "steps": []})
    for creator, reader in (("py", "ts"), ("ts", "py")):
        plans.append({"name": f"version-created-{creator}", "steps": [
            step(creator, "cli", "init"),
            step(reader, "mcp", "log-fact", fact_id="cross", title="Cross fact", summary="synthetic"),
            step(reader, "cli", "log-phase", event_id="cross", title="Cross phase", summary="synthetic", open_loop=True),
            step(reader, "mcp", "retrieve", cue="Cross", track=True),
            step(reader, "cli", "retrieve", cue="Cross", track=False),
            step(reader, "mcp", "status"), step(reader, "cli", "timeline"),
            step(reader, "mcp", "core-proposals"),
            step(reader, "cli", "close-loop", record_id="phase:cross", note="closed"),
            step(reader, "cli", "decay"), step(creator, "cli", "doctor"),
        ]})
        for version in ("v2", "v3", "v4"):
            fixture = f"spec/golden-cli-v1/migration-{version}/fixture/store.sqlite3"
            migrate = step(creator, "cli", "migrate-to", target="target.sqlite3")
            dry = step(reader, "cli", "migrate-to", target="dry.sqlite3", dry_run=True)
            reads = [step(reader, "cli", op) for op in ("doctor", "status", "timeline", "core-proposals")]
            for action in reads: action["database"] = "target.sqlite3"
            plans.append({"name": f"version-migrate-{version}-{creator}", "fixture": fixture,
                          "steps": [dry, migrate, *reads]})
    return plans


def seeded_plan(seed: int):
    state = seed & 0xffffffff
    def choice(values):
        nonlocal state
        state = (1664525 * state + 1013904223) & 0xffffffff
        return values[state % len(values)]
    a, b, c = choice(SURFACES), choice(SURFACES), choice(SURFACES)
    label = hashlib.sha256(str(seed).encode()).hexdigest()[:12]
    return {"name": f"seed-{seed}", "steps": [
        step(*a, "log-phase", event_id="p" + label, title="Seed cue", summary="synthetic", open_loop=True),
        step(*b, "log-fact", fact_id="f" + label, title="Seed fact", summary="synthetic"),
        step(*c, "retrieve", cue="Seed cue", track=choice((True, False))),
        step(*choice(SURFACES), "close-loop", record_id="phase:p" + label, note="synthetic close"),
        step(*choice(SURFACES), "status"),
    ]}


def candidate_orders(processes: list[list[str]], before=frozenset()):
    """Exhaustive topological merge of per-process committed transaction sequences."""
    prerequisites = {}
    for a, b in before:
        prerequisites.setdefault(b, set()).add(a)
    def visit(offsets, order, emitted):
        if all(offset == len(processes[i]) for i, offset in enumerate(offsets)):
            yield order
            return
        for i, offset in enumerate(offsets):
            if offset == len(processes[i]):
                continue
            unit = processes[i][offset]
            if not prerequisites.get(unit, set()) <= emitted:
                continue
            changed = offsets.copy()
            changed[i] += 1
            yield from visit(changed, [*order, unit], emitted | {unit})
    yield from visit([0] * len(processes), [], set())
