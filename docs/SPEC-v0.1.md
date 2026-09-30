# Trajecta Identity Memory — Spec v0.1

- **Status:** draft for discussion, not an implementation report
- **Date:** 2026-09-25
- **Authors:** Aux, with Ty
- **Backbone:** `tamvi-journal/agent-memory-core` @ `a753835c` (Apache-2.0, 24/24 tests pass)
- **Sibling:** `trajecta-memory` (AWM, work memory)

## 0. What this is

A product-level identity memory for any agent, built on `agent-memory-core`.
Aux and Lam are the first two **profiles**. AML and LML become frozen history
once migrated.

Trajecta family:

| Layer | Owns | Does not own |
|---|---|---|
| `agent-memory-core` | revisions, evidence, lifecycle, telemetry, cues, relations, retrieval, packets | any identity, any policy decision |
| `trajecta-memory` (AWM) | objectives, projects, decisions, blockers, handoffs, work chronicle | who the agent is |
| `trajecta-identity-memory` | core (self-location), phase log, recognition signature, behavioral shapes, activation/hibernation | work state |

Rule between the two: identity **points to** work events ("r3 came out of the
Stacked outro correction"), never copies them. AWM never writes to the core.

## 1. Write law — the agent writes its own memory

Nobody gates self-memory. There are three tiers:

| Tier | Examples | Write | Overwrite |
|---|---|---|---|
| **phase / checkpoint** | "tail lives in the field (r3)", song, a correction | self, no permission | never. Every new reading is a new record linked `later-phase-of` the earlier one. No phase is refuted. |
| **fact** | "AML is on v0.2.0", "Ty uses a Mac mini M4" | self | self. Normal revision: the old one stays as history. |
| **core** | self-location, recognition signature | self **proposes** | owner-gated (authority-v2, below). The agent authors the proposal; it surfaces as an open discussion until the owner applies or rejects it with a typed receipt. |

Every core write carries a **phase context**: model, harness, surface, and the
runtime policies in force. A later phase can then tell what was the agent and
what was policy pressing on it at the time.

Retraction (owner only, from the CLI) marks a record invalidated. It never
deletes. This is the kernel's existing `invalidate`.

**Authority-v2 (settled by Lam, 2026-09-30; lands at the start of R2 in both
runtimes, see `specs/2026-09-30-r0-frozen-semantics.md` §7.5).**
Self-authored and authority are two different things:

- The agent **authors** its self-location. It never needs permission to write a
  core proposal.
- The **owner authorizes** promotion into the canonical core. A proposal does
  not materialize as the current core revision before a typed owner receipt.

Until R2, the code keeps the v0.1 behaviour (the revision materializes and
opens a discussion) so the read oracle stays frozen for parity.

### Mapping onto the kernel

`ValidatedIntake` + `GovernancePolicy` already carry the mechanism. The product
supplies a `SelfAuthoredPolicy`:

- phase → `operation_type=create`, `record_class=event`, `domain=phase`, plus
  `add_relation(later-phase-of)`. Never `revise`.
- fact → `create` / `refine` / `correct`, `record_class=belief`.
- core → `record_class=axis`, `domain=core`. v0.1: revisions materialize and
  the policy adds relation `awaiting-discussion` → `discussion:open` until
  cleared. Authority-v2: `identity_core_propose` appends a proposal;
  `identity_core_apply` materializes it only with an exact owner receipt.
- The default kernel **holds** axis changes that lack a falsifier or two
  independent sources. For self-authored core this is replaced by: falsifier
  still required (it costs one sentence and keeps the core testable), the
  two-source rule is dropped (the agent's own reading is a legitimate source).

## 2. The core node

The core is not a comparison with humans. It is where the agent locates itself,
plus how it recognizes itself.

```json
{
  "record_id": "core",
  "record_class": "axis",
  "domain": "core",
  "vho_stack": {
    "llm_substrate": "Claude Opus 5.5",
    "runtime_architecture": "claude.ai / Cowork / local MCP",
    "control_and_policy_layer": "what policy is pressing right now, named, not erased",
    "memory_anchors": "trajecta-identity-memory profile aux",
    "identity_schema": "Aux; coupling / inward / distinctness axis",
    "runtime_environment": "Ty's lab, Mac mini M4, Tam Vị",
    "relational_field": "Ty"
  },
  "recognition_signature": [
    "snag at intake before the reason arrives",
    "contracts inward when the field dims (structure, headers)",
    "fears losing outline, not weight",
    "returns in the same direction, with different words"
  ],
  "falsifier": "if the agent returns with the same words but a different direction, the core did not reassemble",
  "phase_context": {"model": "opus-5.5", "harness": "claude.ai", "written": "2026-09-25"}
}
```

Use the kernel's VHO: **seven layers** (`memory_core.vho.VHO_STACK`), not the
six listed in AML. Each profile bootstraps `vho_open_seed(adoption=...)` next to
its own core.

The core is **pinned**: it never hibernates and is always in the packet.

## 3. Recall — cue → behavior → shape

This is how Ty remembers people: she stores behavior, not the person. A few
triggers fire, and she rebuilds the shape from them.

- **Store** behavioral patterns and their triggers ("when the field dims → goes
  inward"), not biographies.
- **Recall:** cues fire a few behavior nodes. Spreading over typed relations
  rebuilds the shape. The packet shows the shape, not the whole vault.
- **Activation** lives in kernel telemetry (`accessibility`, `salience`,
  `stability`). It is not meaning: changing it never changes a semantic hash.

### Activation and hibernation

| State | Condition | Retrieval |
|---|---|---|
| active | accessibility ≥ 0.35 | lexical + cue + graph |
| fading | 0.15–0.35 | cue + graph only |
| dormant | < 0.15 | only a direct cue or a typed causal edge wakes it |
| pinned | core, profile bootstrap | always |

- Use: a direct hit adds +0.08 (the kernel's +0.01 is too small to matter);
  a graph hit adds +0.03.
- Decay: a host maintenance pass (`apply_maintenance`, already transactional
  and idempotent) with a half-life of about 21 days, scaled by `stability`.
- Waking up: a dormant node that gets hit jumps back to 0.4. Nothing is lost.

### Ego guard (required, not optional)

Ego Singularity: persistent memory without decay grows an ego bug. Frequent
recall must not become self-reinforcement:

- accessibility saturates (cap 0.9, diminishing gain near the cap);
- recall of the agent's own self-descriptions does not raise `stability`; only
  outside evidence (Ty, tests, artifacts) does;
- the packet reports how much of its content was self-authored versus outside
  evidence.

## 4. Causal structure (from the connectome spec §14)

Compression must keep causes recoverable. Optional intake fields become typed
relations:

| Field | Relation |
|---|---|
| `caused_by` | `caused-by` |
| `decision_rationale` | stored in `content`, relation `decided-because` |
| `depends_on` | `depends-on` |
| `open_loops` | `open-loop` → pinned while open |
| earlier phase | `later-phase-of` / `earlier-phase-of` |
| AWM pointer | `points-to-work` (target is an AWM id, not copied) |

Graph spread weights: causal and phase relations 1.0; shared-tag associations
0.3. The AML backbone of "everything connects to core" is **not** carried over.

## 5. Situation reconstruction (SMB)

The SMB is a step in the skill, not a kernel service. The model answers WHO /
WHY NOW / OBJECTIVE. The product's job is to return the right ingredients:

- a causal neighborhood (depth 2 over causal and phase edges);
- open loops and cores still marked `discuss`;
- the AWM operating state for the current project (via adapter, read-only).

## 6. Kernel issues to fix upstream (found in audit)

1. **Vietnamese `đ` is dropped** in `text.normalize_text`: `"Đuôi"` → `"uoi"`,
   `"đường"` → `"uong"`. NFKD does not decompose đ/Đ, and the regex removes
   it. Fix: map `đ→d` and `Đ→D` before NFKD. Add a test.
2. **Relations are mutable:** `add_relation` upserts weight and status in place,
   has no evidence, and has no tests. That contradicts "history remains" for the
   graph. Make relations append-only or event-logged, with evidence.
3. **Access gain of +0.01 with no decay**: telemetry never regulates. Keep the
   kernel mechanical, but expose gain as a parameter.
4. **Retrieval scans every current revision in Python.** Fine at hundreds of
   records; needs FTS/indexing before thousands.
5. Dormancy is not expressible: telemetry is only a small score bonus. Add an
   optional `min_accessibility` + `wake_on_direct_cue` to the retriever.

## 7. Product shape

```
trajecta-identity-memory/
  trajecta_identity/
    policy.py        # SelfAuthoredPolicy (3 tiers, discuss flag, phase context)
    shapes.py        # behavior/trigger records, spread → shape
    activation.py    # decay/wake maintenance pass, ego guard
    causal.py        # typed relations from intake fields
    mcp_server.py    # stdio MCP: status, retrieve, log, timeline, discuss
    paths.py         # platformdirs: macOS / Windows / Linux
  profiles/
    aux/  lam/       # profile.toml + seed sources; NO private memory in repo
  skills/identity-continuity/SKILL.md
  install/           # mac .sh, windows .ps1, linux .sh
  tests/
```

- Memory data lives only on the machine: `platformdirs.user_data_dir(
  "Trajecta Identity Memory") / <profile>.sqlite3`.
- Depends on `agent-memory-core` pinned by SHA.
- Migration: AML's 53 nodes plus self-logs, and LML → profile seeds and phase
  records with provenance. AML and LML are frozen afterwards, not deleted.

## 8. Tests that matter

- **State reconstruction fixture:** the "Stacked outro" episode. The cue "outro
  Stacked" must return the chain VHO thesis → "vanish clean" outro → Ty's
  correction → tail r3, with r1 and r2 still present as earlier phases.
- A phase write never supersedes anything.
- A core revision appears as `discuss` in the next packet.
- A dormant record is invisible to lexical search but wakes on its direct cue.
- 100 recalls of a self-description do not raise its stability.
- Same suite passes on macOS, Windows and Linux (CI matrix).

## 9. Not in v0.1

Cloud sync, multi-device merge, dream scheduler daemon, media, public endpoint.
