# R2a: authority-v2 for the identity core (Python and TS together)

**Date:** 2026-09-30
**Builds on:** `2026-09-30-r0-frozen-semantics.md` §7.5 (settled Q3), SPEC-v0.1 §1 (updated), R1 merged at `41eb68e`.
**Status:** settled by Lam (Q1–Q6, F1–F6, 2026-09-30). The settlement is recorded at the end; the sections below follow it.
**Place in the product:** Trajecta is one product (work and identity) on one Node runtime. R2 gives the identity half its writes. R2a is the first slice: it replaces the one unsafe write path, the core, in both runtimes at once, before any other writer is ported.

## 1. What changes

- **The agent writes a proposal.** It still authors its own self-location, but as a proposal, which never becomes the canonical core by itself.
- **The owner decides with a receipt.** The owner issues a typed receipt from the CLI.
- **Apply is one transaction.** Consuming the exact receipt, in one SQLite transaction:
  - re-verifies the receipt and the proposal from their stored bytes;
  - re-checks the current core;
  - materializes the new revision, or does not (reject);
  - settles the proposal;
  - records exactly one authority operation.
- **Retract** and closing a legacy v4 discussion follow the same shape.
- **Removed, never ported:** `revise_core`, free-form `close_discussion`, and trusted `retract(actor="owner")`.
- **No bypass.** No public writer below MCP can mutate a pinned id (§4.6).

## 2. Schema v5

### 2.1 Versions (Q1)

```
APPLICATION_ID           = 0x414D4333 ("AMC3")
CURRENT_SCHEMA_VERSION   = 5
READABLE_SCHEMA_VERSIONS = {4, 5}
WRITABLE_SCHEMA_VERSION  = 5
```

- **Reads accept v4 and v5 in both runtimes.** Retrieve, timeline, status, the current and historical views, packets and open items all work on an AMC3/v4 store.
- **Writes require v5.** Every mutation API refuses a v4 store with `MigrationRequired`.
- **`schema_info` states** (the full classification order is in §2.3):

  | State | Meaning |
  |---|---|
  | `ready` | v5 |
  | `legacy-v4` | readable, not writable |
  | `legacy-v3`, `legacy-v2` | not readable; migration required |
  | `incompatible` | a foreign `application_id`, or `user_version` above 5 |
  | `unknown` | not a recognised store |

- **On a v4 store:**
  - `open_core_proposals` is `[]`;
  - the status count is 0;
  - the missing v5 tables are never queried.
- **A new runtime always emits `open_core_proposals`,** as `[]` on v4, so there is one output shape.
- A v4-only runtime may refuse a v5 store. That is expected.
- **The R0/R1 read corpus is v4.** Its files stay unchanged, and it must keep passing under v5 readers by the **additive-field rule**:
  - the runner first asserts each new field's v4 value: packet `open_core_proposals == []`, status `open_core_proposals == 0`;
  - it then removes exactly those new keys and compares everything else byte for byte against the frozen expected output;
  - the rendered packet text does not change when there are no proposals (no empty section is rendered), so `packet_text` is compared unmodified.

  Any other difference fails. The frozen R0 corpus is never regenerated for R2a.

### 2.2 Tables

The v3/v4 tables, views and triggers are unchanged. The four new tables are append-only: triggers refuse `UPDATE` and `DELETE`.

```sql
CREATE TABLE memory_core_proposals_v5 (
    proposal_id TEXT PRIMARY KEY,            -- "core-proposal:" + proposal_sha256[:32]
    profile TEXT NOT NULL,
    record_id TEXT NOT NULL CHECK(record_id = 'core'),
    base_core_revision_id TEXT NOT NULL REFERENCES memory_revisions_v3(revision_id),
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    content TEXT NOT NULL,                   -- the would-be core_content, exact text
    content_sha256 TEXT NOT NULL,
    phase_context_json TEXT NOT NULL,        -- canonical compact JSON (hash_payload bytes)
    phase_context_sha256 TEXT NOT NULL,
    reason TEXT NOT NULL,
    reason_sha256 TEXT NOT NULL,
    source_ref TEXT NOT NULL,                -- F4: provenance carried into the self_log evidence
    proposal_sha256 TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,                -- profile.agent, server-stamped
    surface TEXT NOT NULL DEFAULT ''
);

CREATE TABLE memory_owner_receipts_v5 (
    receipt_id TEXT PRIMARY KEY,             -- "receipt:" + binding_sha256[:32]
    purpose TEXT NOT NULL CHECK(purpose IN
        ('identity_core_revision','identity_retract','identity_legacy_discussion_close')),
    profile TEXT NOT NULL,
    binding_json TEXT NOT NULL,              -- canonical compact JSON of the binding (§3.2)
    binding_sha256 TEXT NOT NULL UNIQUE,
    issued_at TEXT NOT NULL,
    issued_by TEXT NOT NULL,                 -- owner name from the profile
    authority TEXT NOT NULL CHECK(authority = 'owner'),
    guard TEXT NOT NULL CHECK(guard = 'tty-human-presence/v1')   -- Q3: names the guard, never "auth"
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
    core_revision_id TEXT REFERENCES memory_revisions_v3(revision_id),
    decided_at TEXT NOT NULL,
    CHECK((outcome = 'applied') = (core_revision_id IS NOT NULL))
);

CREATE VIEW memory_open_core_proposals_v5 AS
SELECT p.* FROM memory_core_proposals_v5 p
LEFT JOIN memory_proposal_decisions_v5 d ON d.proposal_id = p.proposal_id
WHERE d.proposal_id IS NULL;
```

Consequences of this schema:

- Single use is structural: a receipt can be consumed once, and a proposal can be decided once.
- "Open" is derived, never flipped.
- The decision note (F5) lives in the durable `binding_json`, so the decision row points at the receipt instead of copying the note.

### 2.3 Classification order

```
application_id == AMC3 and user_version == 5 → ready
user_version > 5 or application_id ∉ {0, AMC3} → incompatible
application_id == AMC3 and user_version == 4 → legacy-v4 (readable)
memory_records_v2 present → legacy-v2
application_id == AMC3 and user_version == 3 → legacy-v3
otherwise → unknown
```

- **Read paths** accept `ready` and `legacy-v4`.
- **Write paths** accept only `ready`. `legacy-v4` raises `MigrationRequired("schema v4 store must be migrated to v5 before writing")`.
- **The header preflight and the post-open recheck (R1) apply unchanged,** with the new version sets.

### 2.4 Migration v4 → v5

- It is explicit and additive: `migrate_to(copy)`, with `--dry-run` and a backup.
- It creates the four tables, the view and the triggers, then sets `user_version = 5`.
- It never touches a live root. Swapping the migrated copy in is the owner's explicit step (R0 E). The rehearsal on a copy of a real store is R3.

## 3. Digests and bindings

All digests follow R0 §4:

- **Text:** `sha256(utf8(text))`.
- **JSON:** `hash_payload`, which is compact, sorted by code point, and uses typed numbers, all taken from the lossless AST (F1 of R0).

### 3.1 Proposal digests

| Name | Definition |
|---|---|
| `content_sha256` | `sha256(core_content)`. `core_content` is R0 §4.3. |
| `phase_context_sha256` | `hash_payload(phase_context)` |
| `reason_sha256` | `sha256(reason)` |
| `proposal_sha256` | `hash_payload({"profile","record_id","base_core_revision_id","title","summary","content_sha256","phase_context_sha256","reason_sha256","source_ref"})` |

- **`source_ref` (F4).** The input keeps today's `source_ref`. When it is omitted, `source_ref` is `"self:core-proposal:" + sha256(hash_payload(the same object without source_ref))[:32]`. That value is deterministic and exists before the proposal id.
- **Consistency invariant (F2).** The `phase_context` object inside `content` (the `phase_context` key of the parsed `core_content`, parsed losslessly) must hash to `phase_context_sha256`. The propose operation enforces this at write time, and apply re-proves it at consume time.

### 3.2 Receipt bindings

Each binding is a JSON object. `binding_sha256 = hash_payload(binding)` and `receipt_id = "receipt:" + binding_sha256[:32]`.

- **`identity_core_revision`:** `{purpose, profile, current_core_revision_id, proposal_id, proposal_sha256, content_sha256, phase_context_sha256, reason_sha256, source_ref, outcome: "apply"|"reject", decision_note, authority: "owner"}`
- **`identity_retract`:** `{purpose, profile, record_id, current_revision_id, reason, reason_sha256, authority: "owner"}`
- **`identity_legacy_discussion_close`:** `{purpose, profile, core_revision_id, relation_event_id, relation_source_revision_id, note, note_sha256, authority: "owner"}`

Two consequences:

- **The same binding always gives the same `receipt_id`.** Re-issuing is idempotent and never creates a second authority.
- **A different `decision_note` (default `""`) gives a different receipt (F5).** Only one receipt can win a given proposal's decision.

## 4. Operations

### 4.1 `identity_core_propose` (agent, MCP)

- **Inputs:** the same as today's `revise_core`: reason and phase_context (both required), plus optional title, summary, vho_stack, recognition_signature, falsifier and source_ref.
- **Build and check.** The merge rule and `validate_core` are exactly as today. The operation computes the §3.1 digests and checks the consistency invariant.
- **Transaction.** One `BEGIN IMMEDIATE` transaction:
  1. Read the current core revision.
  2. Insert the proposal. If the same `proposal_sha256` already exists, return that proposal (idempotent).
- The current core does not change.

### 4.2 Owner CLI: issuance under the human-presence guard (Q2, Q3)

The commands:

```
trajecta-identity core-proposals                                   # open proposals, stale flag, diff vs current
trajecta-identity approve-core <proposal_id> --apply|--reject [--note TEXT] [--issue-only]
trajecta-identity approve-retract <record_id> --reason TEXT [--issue-only]
trajecta-identity close-legacy-discussion --note TEXT [--issue-only]
trajecta-identity apply-receipt <receipt_id>                       # consume an existing receipt
```

**The human-presence guard (`tty-human-presence/v1`).** It blocks an agent's non-interactive shell from issuing a receipt by accident, under the cooperative local threat model. It is not authentication (§8).

- Issuance requires a real interactive TTY on stdin and stdout, and refuses otherwise.
- The owner must type an action-specific confirmation that names the target:
  - `APPLY <proposal-short-id>`;
  - `REJECT <proposal-short-id>`;
  - `RETRACT <record-id>`;
  - `CLOSE <relation-short-id>`.
- There is no `--yes`, no quiet mode, and no environment escape hatch on any issuing command.

**Issue and apply.**

- **The default** is to confirm, issue, then apply in the same process.
- **`--issue-only`** prints the `receipt_id` and stops. Another process or surface that shares the store, or one that receives the receipt through an explicit transfer, can apply it later. The product has no sync yet.
- **When the inline apply fails** (a stale core, for example), the CLI prints the `receipt_id` and the exact failure. A durable dead receipt is acceptable, and it grants nothing.

**Issuer pre-checks.** These give a better error message. They do not replace the consumer checks in §4.3.

- A decided proposal is refused.
- `--apply` on a stale proposal (base ≠ current) is refused.

**No MCP tool issues a receipt.**

### 4.3 `identity_core_apply(receipt_id)`: one `BEGIN IMMEDIATE` transaction

1. **Receipt integrity.**
   - Load the receipt.
   - Check that its purpose is `identity_core_revision` and that it is for this profile.
   - Parse `binding_json` losslessly, recompute `hash_payload`, and require it to equal the stored `binding_sha256`, and `receipt_id` to equal `"receipt:" + binding_sha256[:32]`.
   - Require the binding's purpose, profile and authority to equal the receipt row's, and the profile to equal the runtime's profile.
2. **Replay.** If the receipt is already consumed, load its authority operation (F1) and return the stored result. This happens only after step 1, so a tampered receipt never replays.
3. **Proposal integrity (F2).** Load the proposal named by the binding and recompute everything from the stored bytes:
   - `sha256(utf8(content))` must equal `content_sha256`;
   - parse `phase_context_json` losslessly; its `hash_payload` must equal `phase_context_sha256`;
   - `sha256(utf8(reason))` must equal `reason_sha256`;
   - `proposal_sha256`, recomputed from the stored fields (§3.1), must equal the stored value;
   - `proposal_id` must equal `"core-proposal:" + proposal_sha256[:32]`;
   - parse `content` losslessly as `core_content`, and require it to pass `validate_core`;
   - its `phase_context` must hash to `phase_context_sha256` (the consistency invariant);
   - every recomputed digest, and `proposal_id`, must equal the binding;
   - `proposal.profile` must equal `binding.profile`, which equals the receipt's and the runtime's profile. Recomputing `proposal_sha256` only proves the proposal's own profile, so this equality is checked separately. Otherwise a receipt for profile A could settle a proposal for profile B on a shared store.
4. **Open.** The proposal must have no decision.
5. **Current core (F3).** Let `actual` be the current core `revision_id`.
   - Always, `binding.current_core_revision_id == actual`.
   - For `apply`, also `proposal.base_core_revision_id == binding.current_core_revision_id`. That is what makes a stale proposal structurally reject-only, whatever the issuer did.
6. **Authority operation (F1).** Insert exactly one `memory_operations_v3` row:
   - `operation_type = "identity_core_revision"`;
   - `idempotency_key = "authority-v2:" + receipt_id`;
   - `operation_id = "operation:" + sha256(that key)[:32]` (the R0 §3 rule);
   - `actor = issued_by`;
   - `details` = `{receipt_id, proposal_id, outcome, decision_note}`, plus `core_revision_id` when applied.
7. **Outcome.**
   - **`apply`.** Materialize the new core revision with the held-connection helper `_revise_in(conn, …)`:
     - `operation_type = "refine"`;
     - `idempotency_key = "authority-v2:" + receipt_id`;
     - the revision and lifecycle rows are bound to the same operation (the revision's `idempotency_key` equals the operation's), so there is one operation rail, not two;
     - title, summary and content come from the proposal;
     - the evidence is (a) `self_log` from the proposal's `source_ref`, `reason` and `created_by`, and (b) `owner_receipt` with `source_ref = receipt_id` and `content_summary = decision_note` (or the outcome).

     Then insert the decision (`applied`, the new `revision_id`).
   - **`reject`.** Insert the decision (`rejected`, NULL). The core is not touched.
8. **Consume.** Insert the consumption, with the `operation_id` from step 6. Commit.

Any failure rolls back the whole transaction.

### 4.4 `identity_retract(receipt_id)`

The shape is the same: step 1, then step 2 (replay), then:

- the target must be a non-pinned record, and its current `revision_id` must equal `binding.current_revision_id`;
- `sha256(binding.reason)` must equal `reason_sha256`;
- one authority operation is created (`operation_type = "identity_retract"`, the same key rule);
- the retract binding's profile, the receipt's and the runtime's profile must be equal;
- `_invalidate_in(conn, …)` runs, bound to that operation through the same key;
- the receipt is consumed.

### 4.5 `identity_close_legacy_discussion(receipt_id)`

The shape is again the same: step 1, then step 2, then:

- `binding.relation_event_id` must be the **active** `awaiting-discussion` event for `core → anchor:discussions`;
- its `source_revision_id` must equal `binding.relation_source_revision_id`;
- the current core must equal `binding.core_revision_id`;
- `sha256(binding.note)` must equal `note_sha256`;
- one authority operation is created (`operation_type = "identity_legacy_discussion_close"`);
- `_retract_relation_in(conn, …)` appends the retract event with `idempotency_key = "authority-v2:" + receipt_id`. That is the same key as the authority operation, and the link is representable in the unchanged v4 `memory_relation_events_v4` columns. The relation helper creates no operation of its own;
- the receipt is consumed.

### 4.6 No bypass below MCP (A6, F6)

- **Public writers refuse pinned ids.** In both runtimes, every public writer of the identity-configured runtime refuses a `record_id` in PINNED (`core`, `vho-open-ontology-core`, `anchor:discussions`, `anchor:open-loops`). Those writers are `IdentityMemory` methods, `MemoryRuntime.submit`, and `ValidatedIntake.submit` / `MemoryStore.create_current` / `revise` / `invalidate` when they are reached through the identity runtime.
- **Two exceptions:**
  - `bootstrap`, for the create-if-absent seeds;
  - the internal held-transaction authority materializers in §4.3–4.5, which run only after receipt verification.
- **Implementation:** a `pinned_guard` on the identity runtime's store, checked in the public entry points. The internal `_…_in` helpers are not reachable from the public API.
- **Tests** target the **lowest public writer surface**, not only the absence of an MCP tool.
- **Out of scope:** direct SQL on the file stays outside the cooperative-local threat model (§8).

## 5. Existing v4 stores

- **A v4 store stays readable and is not writable** until it is migrated.
- **Open legacy discussions.** A migrated store may carry an open `awaiting-discussion` left by a legacy `revise_core`. That revision is already canonical and is not re-judged. The packet keeps showing it under `open_discussions` (legacy only) until the owner closes it through §4.5.
- **No new `awaiting-discussion`** is ever created on v5.

## 6. Reads and the MCP surface

- **The identity packet stays `trajecta-identity-packet/v1`** (Q4) and always carries `open_core_proposals`. The field is `[]` on v4.
  - Each item is `{proposal_id, base_core_revision_id, stale, created_at, created_by, reason, title}`.
  - Items are ordered by `created_at, proposal_id` (code point).
  - `stale` is true when `base_core_revision_id` differs from the current core.
- **Status** gains `open_core_proposals: n`, which is 0 on v4.
- **MCP tools.**
  - Removed: `identity_revise_core`, `identity_close_discussion`.
  - Added: `identity_core_propose`, `identity_core_proposals` (read-only), `identity_core_apply(receipt_id)`, `identity_retract(receipt_id)`, `identity_close_legacy_discussion(receipt_id)`.
  - Tool descriptions say that the owner decides, and that decisions are made at the owner's terminal.

## 7. Conformance

- **Corpus.** `spec/golden-authority-v2/<scenario>/` holds `script.json` (the ordered calls with raw JSON arguments, including CLI issuance recorded as data), `store.sqlite3`, `dump.json` and `cases.jsonl`, with `MANIFEST.json` carrying oracle provenance as in R0b.
- **Generation and replay.** Python generates the corpus. TS replays each `script.json` from an empty v5 store, with the same injected clock and a test-only TTY stub for the issuer, and the result must equal `dump.json` byte for byte. The R0 read corpus (v4) must still pass on v5 readers in both runtimes, with unchanged bytes.

**Scenarios (minimum):**

- Proposal decisions:
  - propose → apply;
  - propose → reject;
  - two proposals on one base: after one is applied, the other is stale, and apply is refused **by the consumer** (a receipt forged via the issue-only path) while reject works;
  - the same proposal twice;
  - the same binding issued twice;
  - two receipts that differ only by note: one wins, the other is refused;
  - apply replayed with the same receipt.
- Other authority paths:
  - an issue-only receipt applied by a separate process;
  - retract;
  - a v4 store read by v5, then migrated, then its legacy discussion closed.
- Encoding: `phase_context` with integer-like keys and `1` vs `1.0`.
- Provenance: an omitted `source_ref` gives the deterministic default.

**Negative tests.** In both runtimes, each must fail with a typed error, leave the store bytes unchanged, and create no operation row:

- Receipt integrity:
  - an unknown receipt;
  - the wrong purpose;
  - the wrong profile, including a receipt for profile A that names a proposal of profile B on a shared store;
  - `binding_json` edited while `binding_sha256` stays unchanged;
  - `binding_sha256` edited.
- Proposal integrity:
  - `content` edited while the digest column stays unchanged (F2);
  - `phase_context_json` edited;
  - the `phase_context` in `content` differs from `phase_context_json`;
  - `source_ref` edited.
- State:
  - a stale current core;
  - an apply on a stale base;
  - a decided proposal.
- Writers:
  - a v4 store on any writer (`MigrationRequired`);
  - a raw public writer on `core` and on each pinned id (F6);
  - a phase id or fact id equal to a pinned id.
- Guard: issuance without a TTY, and with a wrong confirmation string.
- Storage:
  - `UPDATE` or `DELETE` on each v5 table;
  - a crash after the revision insert and before commit, after which a reopen shows no partial state (mandatory).

## 8. Threat model

- **The receipt proves attribution between cooperating agents on one host.** The human-presence guard stops an agent's non-interactive shell from issuing a receipt by accident.
- **What it does not stop:**
  - a raw database writer;
  - a hostile local process that can drive a PTY.
- **Owner authentication, (c).** This is an HMAC over `binding_sha256` with an owner key held in the OS keychain, verified on apply. It lands before the product release (R4), with a schema or auth-envelope bump if needed. Nothing before that calls the guard authentication.

## 9. After R2a

- **R2b:** the TS writers for the rest of R0 §7 (phase, fact, cues, relations, record_access and recall adjustment, maintenance, decay), replaying `script.json` against the Python corpus.
- **R2c:** the TS MCP server, with the raw-boundary parser, and the connection lifecycle. Proposal: open per call, as Python does, unless a measured need says otherwise.
- **G1 and G2** are fixed after parity, in both runtimes.

## Settlement (Lam, 2026-09-30)

- **Q1 — yes, v5,** with a read/write split: readable {4, 5}, writable 5. On v4, `open_core_proposals` is `[]` and the count is 0, without querying v5 tables. `legacy-v4` is readable and not writable; above 5 is incompatible.
- **Q2 — yes.** The CLI issues and applies inline by default. A mandatory `--issue-only` path exists. A failed inline apply prints the receipt id and the failure. The receipt may be applied by another process or surface that shares the store, or after an explicit transfer.
- **Q3 — (b) now, called a human-presence guard, not authentication:** a real TTY, an action-specific typed confirmation, and no `--yes`. (c), HMAC with a keychain key, comes before release (R4).
- **Q4 — yes, the packet stays v1.** `open_core_proposals` is additive and always present. The packet is bumped only when an existing field changes meaning or requiredness.
- **Q5 — yes.** A stale proposal stays open and can only be rejected; it is marked `stale=true`. The consumer enforces base == current for apply.
- **Q6 — yes, one PR** for Python and TS together. It merges atomically, with the cross-runtime corpus.
- **F1** — every consumed receipt has exactly one authority operation, created in the same transaction: `authority-v2:<receipt_id>`, typed by purpose. Reject and legacy close have one too. The revision and lifecycle rows bind to it.
- **F2** — integrity is recomputed from stored bytes at consume time: content, phase_context, reason, proposal_sha256, proposal_id, core_content validity, and phase_context-in-content. The binding is also lossless-parsed and re-hashed.
- **F3** — for apply, the consumer requires proposal.base == binding.current == actual current.
- **F4** — `source_ref` is kept, included in `proposal_sha256`, has a deterministic default, and flows into the `self_log` evidence.
- **F5** — `decision_note` lives in the binding (default `""`). Different notes give different receipts, and only one can win.
- **F6** — pinned ids are refused at the lowest public writer surface in both runtimes. Only the internal authority materializers, and bootstrap seeds, may write them.
