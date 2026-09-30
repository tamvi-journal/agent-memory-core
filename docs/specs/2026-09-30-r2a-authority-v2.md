# R2a: authority-v2 for the identity core (Python and TS together)

**Date:** 2026-09-30
**Builds on:** `2026-09-30-r0-frozen-semantics.md` §7.5 (settled Q3), SPEC-v0.1 §1 (updated), R1 merged at `41eb68e`.
**Status:** draft for Lam's review. Questions Q1–Q6 are at the end.
**Place in the product:** Trajecta is one product (work + identity) on one Node runtime. R2 gives the identity half its writes. R2a is the first slice: it replaces the one write path that was unsafe (the core), in both runtimes at once, before any other writer is ported.

## 1. What changes, in one paragraph

The agent still writes its own self-location, but as a **proposal**. A proposal never becomes the canonical core by itself. The owner decides with a **typed receipt** issued from the CLI. `identity_core_apply` consumes that exact receipt and, in **one SQLite transaction**, re-checks the current core, materializes the revision (or not, for a reject), and settles the proposal. Retract works the same way. These legacy paths are removed and never ported:
- `revise_core` (materializes the core directly);
- free-form `close_discussion` (fabricates `"{agent}+{owner}"`);
- trusted `retract(actor="owner")`.

## 2. Schema v5 (additive)

`SCHEMA_VERSION = 5`. Every v3/v4 table, view and trigger is unchanged. There are four new tables, all append-only: triggers refuse any `UPDATE` or `DELETE`, as for revisions.

```sql
CREATE TABLE memory_core_proposals_v5 (
    proposal_id TEXT PRIMARY KEY,            -- "core-proposal:" + proposal_sha256[:32]
    profile TEXT NOT NULL,
    record_id TEXT NOT NULL,                 -- always 'core' in R2a
    base_core_revision_id TEXT NOT NULL,     -- current core revision at proposal time
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    content TEXT NOT NULL,                   -- the would-be core_content, exact bytes
    content_sha256 TEXT NOT NULL,
    phase_context_json TEXT NOT NULL,        -- canonical compact JSON (hash_payload bytes)
    phase_context_sha256 TEXT NOT NULL,
    reason TEXT NOT NULL,
    reason_sha256 TEXT NOT NULL,
    proposal_sha256 TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,                -- the agent (profile.agent), server-stamped
    surface TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(base_core_revision_id) REFERENCES memory_revisions_v3(revision_id)
);

CREATE TABLE memory_owner_receipts_v5 (
    receipt_id TEXT PRIMARY KEY,             -- "receipt:" + binding_sha256[:32]
    purpose TEXT NOT NULL CHECK(purpose IN
        ('identity_core_revision','identity_retract','identity_legacy_discussion_close')),
    profile TEXT NOT NULL,
    binding_json TEXT NOT NULL,              -- canonical compact JSON of the binding (§3)
    binding_sha256 TEXT NOT NULL UNIQUE,
    issued_at TEXT NOT NULL,
    issued_by TEXT NOT NULL,                 -- owner name from the profile
    authority TEXT NOT NULL CHECK(authority = 'owner')
);

CREATE TABLE memory_receipt_consumptions_v5 (
    receipt_id TEXT PRIMARY KEY REFERENCES memory_owner_receipts_v5(receipt_id),
    operation_id TEXT NOT NULL UNIQUE REFERENCES memory_operations_v3(operation_id),
    consumed_at TEXT NOT NULL
);

CREATE TABLE memory_proposal_decisions_v5 (
    proposal_id TEXT PRIMARY KEY REFERENCES memory_core_proposals_v5(proposal_id),
    receipt_id TEXT NOT NULL UNIQUE REFERENCES memory_owner_receipts_v5(receipt_id),
    outcome TEXT NOT NULL CHECK(outcome IN ('applied','rejected')),
    core_revision_id TEXT REFERENCES memory_revisions_v3(revision_id),  -- set iff applied
    decided_at TEXT NOT NULL,
    CHECK((outcome = 'applied') = (core_revision_id IS NOT NULL))
);

CREATE VIEW memory_open_core_proposals_v5 AS
SELECT p.* FROM memory_core_proposals_v5 p
LEFT JOIN memory_proposal_decisions_v5 d ON d.proposal_id = p.proposal_id
WHERE d.proposal_id IS NULL;
```

Consequences:

- **Single use is structural.** `PRIMARY KEY(receipt_id)` in consumptions and `UNIQUE(receipt_id)` in decisions mean a receipt can be consumed once, and a proposal can be decided once.
- **"Open" is derived, never flipped.** Nothing is updated.
- **Migration v4 → v5** is an explicit, additive command: `migrate_to(copy)`, with dry-run and backup, as in R0 §1. A v4 store is `legacy-v4` for writers and still fully **readable** by v5 readers. Both runtimes' read paths accept v4 and v5 (Q1).
- **The R0/R1 read corpus stays valid.** It is all v4. R2a adds its own corpus (§7).

## 3. Digests and bindings (canonical, both runtimes)

All digests use R0 §4:
- `sha256(utf8)` for text;
- `hash_payload` (compact, code-point-sorted, typed numbers) for JSON.

| Name | Definition |
|---|---|
| `content_sha256` | `sha256(core_content)`. `core_content` is R0 §4.3: indent=1, fixed key order, `phase_context` in input order. |
| `phase_context_sha256` | `hash_payload(phase_context)`, from the lossless AST (F1) |
| `reason_sha256` | `sha256(reason)` |
| `proposal_sha256` | `hash_payload({"profile","record_id","base_core_revision_id","content_sha256","phase_context_sha256","reason_sha256","title","summary"})` |

**Receipt bindings.** Each binding is a JSON object hashed with `hash_payload`, and `receipt_id = "receipt:" + binding_sha256[:32]`.

- `identity_core_revision`: `{purpose, profile, current_core_revision_id, proposal_id, proposal_sha256, content_sha256, phase_context_sha256, reason_sha256, outcome: "apply"|"reject", authority: "owner"}`
- `identity_retract`: `{purpose, profile, record_id, current_revision_id, reason_sha256, authority: "owner"}`
- `identity_legacy_discussion_close`: `{purpose, profile, core_revision_id, relation_event_id, note_sha256, authority: "owner"}`. This closes an `awaiting-discussion` relation left by a v4 store (§5).

The same binding always gives the same `receipt_id`. So issuing a receipt twice is idempotent, and it never creates a second authority.

## 4. Operations

### 4.1 `identity_core_propose` (agent, MCP)

- **Inputs:** the fields of today's `revise_core`: reason, phase_context, and optional title, summary, vho_stack, recognition_signature, falsifier.
- **Merge and validate:** the merge rule and `validate_core` are exactly as today.
- **Transaction:** one transaction that:
  1. reads the current core revision;
  2. computes the digests;
  3. inserts the proposal. If the same `proposal_sha256` already exists, it returns the existing proposal (idempotent).
- **Effects:** no change to `memory_current_v3`. Packets show it through `open_core_proposals` (§6).

### 4.2 Owner CLI (the only receipt issuer)

- `trajecta-identity core-proposals` lists the open proposals, with a diff against the current core.
- `trajecta-identity approve-core <proposal_id> (--apply | --reject) [--note …]` works in four steps:
  1. It reads the proposal and the current core.
  2. It refuses if the proposal is decided, or if `--apply` and `base_core_revision_id ≠ current` (the proposal is stale; only `--reject` is possible).
  3. It builds the binding with `current_core_revision_id` = current, and inserts the receipt.
  4. It then calls `identity_core_apply(receipt_id)` in the same process. The owner does not need a second step.
- `trajecta-identity approve-retract <record_id> --reason …` and `trajecta-identity close-legacy-discussion --note …` work the same way.
- **No MCP tool issues a receipt.** An MCP tool can only *consume* an existing receipt (§4.3). That lets a receipt issued on one machine be applied by a server on another, and it is also the replay path.

### 4.3 `identity_core_apply(receipt_id)` (MCP and CLI)

It runs in **one** `BEGIN IMMEDIATE` transaction:

1. **Receipt.** Load the receipt. Its purpose must be `identity_core_revision`, and it must be for this profile. Re-compute `binding_sha256` from `binding_json`.
2. **Replay.** If the receipt is already consumed, return the prior operation's result. Replay is idempotent and grants nothing new (A7).
3. **Proposal.** Load the proposal named by the binding, and check:
   - it is open;
   - `proposal_sha256`, `content_sha256`, `phase_context_sha256` and `reason_sha256` all equal the binding.
4. **Current core.** Load the current core, and check that its `revision_id` equals `current_core_revision_id`. A mismatch refuses the receipt: it is dead, and the owner must issue a new one (A7).
5. **Apply or reject.**
   - `apply`: materialize the new core revision with the kernel `revise` logic *inside this transaction* (operation `refine`, `idempotency_key = "core-apply:" + receipt_id`, evidence `owner_receipt` plus the proposal's `self_log`). Then insert the decision (`applied`, `core_revision_id`).
   - `reject`: insert the decision (`rejected`, NULL). The core is not touched.
6. **Consume.** Insert the consumption. Commit.

Any failure rolls back everything; no step is partial. Python's `revise` today opens its own connection. R2a extracts a `_revise_in(conn, …)` helper used by both `revise` and apply, and TS mirrors it.

### 4.4 `identity_retract(receipt_id)` and legacy close

- **Retract** has the same transaction shape as apply: verify the binding, check that `current_revision_id` still matches, then `invalidate` inside the transaction and consume.
- **Legacy close** checks that the relation event is still the active `awaiting-discussion` event and that the core revision matches. It then retracts that relation inside the transaction and consumes the receipt.

## 5. What happens to existing v4 stores

- Aux's and Lam's live stores may carry an **open `awaiting-discussion`** from a legacy `revise_core`. That revision is already canonical. Migration does not undo or re-judge it. The open discussion keeps showing (as `legacy_discussion`) until the owner closes it with an `identity_legacy_discussion_close` receipt.
- No new `awaiting-discussion` relations are ever created in v5.
- Migration never touches a live root. It works on a copy, and swapping the files in is the owner's explicit step (R0 E). The rehearsal on a copy of a real store is R3.

## 6. Reads and MCP surface

- **Identity packet.** It gains `open_core_proposals`: `[{proposal_id, base_core_revision_id, stale: base ≠ current, since, reason, title}]`, in `created_at, proposal_id` order.
  - `open_discussions` keeps its shape and lists only legacy discussions.
  - The packet schema string stays `trajecta-identity-packet/v1`, because the field is additive (Q4).
- **Status** gains `open_core_proposals: n`.
- **MCP tools.**
  - Removed: `identity_revise_core`, `identity_close_discussion`.
  - Added: `identity_core_propose`, `identity_core_apply(receipt_id)`, `identity_retract(receipt_id)`.
  - `identity_core_proposals` is read-only.
  - Tool descriptions say plainly that the owner decides, and how.

## 7. Conformance: the authority-v2 corpus

`spec/golden-authority-v2/<scenario>/` holds:
- `script.json`: the ordered write calls with raw JSON arguments. This is the §9.3 deferral from R0, now delivered.
- `store.sqlite3`, `dump.json`, `cases.jsonl`.
- `MANIFEST.json`, with oracle provenance as in R0b.

The Python writer generates the corpus, and the TS writer replays each `script.json` from an empty v5 store with the same injected clock. The result must equal `dump.json` byte for byte (floats as repr plus hex).

**Scenarios (minimum):**
- propose → apply;
- propose → reject;
- two proposals on one base: apply one, then the other is stale and can only be rejected;
- the same proposal twice (idempotent);
- the same binding issued twice (one receipt);
- apply replayed with the same receipt (same result, no new revision);
- retract via receipt;
- a legacy v4 store with an open discussion, migrated, then closed via receipt;
- `phase_context` with integer-like keys and `1` vs `1.0` (F1 through a hash).

**Negative tests** (both runtimes; each must fail with a typed error and leave the store bytes unchanged):
- an unknown receipt;
- a receipt of the wrong purpose;
- a receipt for another profile;
- a binding tampered after issue, so its sha no longer matches;
- a stale current core (a revision happened after issue);
- a proposal already decided;
- a proposal whose content was altered, so its digest no longer matches;
- an MCP call trying to insert a receipt (no such tool exists);
- a raw submit to `core`, and to each pinned id (A6);
- a phase id or fact id equal to a pinned id;
- `UPDATE` or `DELETE` on each v5 table (the triggers fire);
- a crash between the steps of apply (kill after the revision insert, before commit), after which reopen shows no partial state.

## 8. R2 after R2a

- **R2b:** the TS writers for everything else in R0 §7 (phase, fact, cues, relations, record_access and recall adjustment, maintenance, decay), replaying from `script.json` against the Python corpus.
- **R2c:** the TS MCP server, with the F1 raw-boundary parser. It also settles the connection lifecycle Lam flagged: a cached read-only connection versus per-call opens, revalidation, and file replacement. Proposal: open per call, as Python does, unless a measured need says otherwise.
- **G1 and G2** (store replay without an input check, and non-atomic intake) are still fixed after parity, in both runtimes.

## Questions for Lam

- **Q1 — v5 as a schema bump.** v4 stays readable by v5 readers, v5 writers require v5, and migration is explicit and additive. Alternatively the tables could live in v4 as "optional" tables, but that muddies "ready". I prefer the bump.
- **Q2 — The CLI applies in the same command by default,** while MCP can consume an existing receipt. Is that acceptable, or should applying always be a separate step?
- **Q3 — Owner proof.** As in TWM, a receipt is attribution between cooperating agents on one host, not authentication. Any process with write access to the DB (or a shell that can run the CLI) could insert one. Options:
  - (a) accept this for now, as TWM does;
  - (b) the CLI requires an interactive TTY confirmation, so an agent's non-interactive shell cannot issue receipts;
  - (c) an HMAC over `binding_sha256` with an owner key in the OS keychain, verified on apply.

  I propose (b) now and (c) in R4 packaging.
- **Q4 — Keep the packet schema at v1** with the additive `open_core_proposals`, or bump it to v2?
- **Q5 — Stale proposals stay open** (reject-only) rather than being auto-settled when another proposal is applied. Is that right? It keeps "no silent state change" and puts every decision in the owner's hands.
- **Q6 — Scope** is R2a in one PR (Python and TS together, with the new corpus), then R2b and R2c. Codex builds; I review, then you.
