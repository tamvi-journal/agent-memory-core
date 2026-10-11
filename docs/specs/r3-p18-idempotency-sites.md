# R3 P18: inventory before implementation

This inventories product source at `be9f9fcf09fe9245d830fc32ca70e39f9fe80189`
before P18 implementation. Lookup/insert line references below identify that
snapshot; function and table names are the durable references.

PR #28 was settled with Lam at `91995f2c0e01c05e5c8b6df539993f8842f11ab0`
and merged into main `c5a5df8bb78467254d3bc8ea617c455176094c0e`.
That main is merged into the implementation branch at `0d3e00d`.
The ruling authorizes recovery only for intake submit. The other 12 sites retain
their exact replay rules; their differing rules below are settled exclusions,
not further P18 scope questions.

## Paired check-then-insert sites

Each row identifies both implementations, the lookup and insert it controls,
and the current replay result. Shared helpers are listed with every caller.
Except intake, the public rows already hold `BEGIN IMMEDIATE` before the
pre-check. Their exclusion is explicit in the merged scope ruling.

| # | Python file/function and lookup | TypeScript file/function and lookup | Insert controlled | Existing replay rule and P18 finding |
| --- | --- | --- | --- | --- |
| 1 | `memory_core/governance.py`, `ValidatedIntake.submit`:63, `_intake_by_key`:393 | `node/src/governance.ts`, `ValidatedIntake.submit`:193, `intake`:304 | Python `_insert_intake`:351; TS second submit transaction:218; `memory_intake_v3` | Same proposal SHA returns existing intake; different SHA raises `ValueError("idempotency_key already exists with a different proposal")`. Matches P18. Evidence capture precedes the separate received INSERT. |
| 2 | `memory_core/store.py`, `create_current`:630, `_operation_by_key`:1607 | `node/src/kernel.ts`, `createCurrent`:241 | Python `_insert_operation`:676/1634; TS `insertOperation`:288/158; `memory_operations_v3` (plus preceding record/revision inserts) | Any existing operation key returns `_operation_result`/`operationResult`, without comparing caller payload or digest. No existing typed mismatch refusal. Excluded from recovery by the settled scope ruling. |
| 3 | `memory_core/store.py`, `revise`:738, `_operation_by_key`:1607 | `node/src/kernel.ts`, `revise`:341 | Python `_insert_operation`:793/1634; TS `insertOperation`:388/158; `memory_operations_v3` (plus preceding revision insert) | Same unconditional operation-key replay as row 2, including a changed proposal. Excluded from recovery by the settled scope ruling. |
| 4 | `memory_core/store.py`, `invalidate`:847, `_operation_by_key`:1607 | `node/src/kernel.ts`, `invalidate`:440 | Python `_insert_operation`:857/1634; TS `insertOperation`:446/158; `memory_operations_v3` | Same unconditional operation-key replay; no payload mismatch check/refusal. Excluded from recovery by the settled scope ruling. |
| 5 | `memory_core/store.py`, `apply_maintenance`:1396, `_operation_by_key`:1607 | `node/src/kernel.ts`, `applyMaintenance`:626 | Python `_insert_operation`:1455/1634; TS `insertOperation`:669/158; `memory_operations_v3` after telemetry adjustments | Same unconditional operation-key replay; key is explicit or derived from run ID. No stored-payload mismatch refusal. Excluded from recovery by the settled scope ruling. |
| 6 | `memory_core/store.py`, `add_relation` → `_append_relation_event`:1190 | `node/src/kernel.ts`, `relation("assert")`:517 | Python relation-event INSERT:1239; TS relation-event INSERT:549; `memory_relation_events_v4` | Explicit key hit returns prior event with `status="duplicate"`, without comparing requested weight/source/endpoints. With no key, latest-state no-op and derived sequence/key apply. No typed key/payload mismatch refusal. Excluded from recovery by the settled scope ruling. |
| 7 | `memory_core/store.py`, `retract_relation` → `_append_relation_event`:1190 | `node/src/kernel.ts`, `relation("retract")`:517 | Same relation-event INSERT as row 6 | Same explicit-key replay; absent key can return an inactive-relation no-op. No typed key/payload mismatch refusal. Excluded from recovery by the settled scope ruling. |
| 8 | `trajecta_identity/identity.py`, `identity_core_propose`:361 | `node/src/authority.ts`, `AuthorityV2.propose`:227 | Python proposal INSERT:368; TS proposal INSERT:231; `memory_core_proposals_v5` | Lookup is by the computed proposal SHA; existing row is decorated with `status="existing"`. Current-core merge/validation precedes lookup. No separate stored-payload mismatch refusal. Excluded from recovery by the settled scope ruling. |
| 9 | `trajecta_identity/identity.py`, `identity_core_apply`:647 → `_replay_in`:568 | `node/src/authority.ts`, `applyCore`:594 → `#replay`:439 | Authority operation (Python:700/1634, TS:645/571), proposal decision (Python:732, TS:668), receipt consumption (Python:742, TS:678) | Receipt binding integrity/purpose/profile/authority are checked BEFORE replay. Replay maps consumption → operation, refuses missing operation with `ReceiptIntegrityError`, then reconstructs its result. New-consumption path has proposal integrity/decision and base/current authority rules. Not a simple payload-match pre-check. Excluded from recovery; integrity-before-replay stays binding. |
| 10 | `trajecta_identity/identity.py`, `identity_retract`:753 → `_replay_in`:568 | `node/src/authority.ts`, `applyRetract`:805 → `#replay`:439 | Authority operation (Python:776/1634, TS:828/571), receipt consumption (Python:800, TS:856); bound invalidation/lifecycle | Same integrity-before-replay, consumption-to-operation mapping; new path checks pinned record and current revision. Extra authority/state semantics. Excluded from recovery; integrity-before-replay stays binding. |
| 11 | `trajecta_identity/identity.py`, `identity_close_legacy_discussion`:812 → `_replay_in`:568 | `node/src/authority.ts`, `applyLegacyClose`:866 → `#replay`:439 | Authority operation (Python:838/1634, TS:887/571), receipt consumption (Python:863, TS:922); bound relation event | Same integrity-before-replay mapping; new path checks latest active discussion relation/core binding/note digest. Extra authority/state semantics. Excluded from recovery; integrity-before-replay stays binding. |
| 12 | `memory_core/store.py`, `_migrate_v2`:1726 | `node/src/store.ts`, `migrateV2`:145 | Copy inserts into records/revisions/telemetry/evidence/link/relation/cue/operation/intake tables, then migration-marker INSERT (Python:2011, TS:349) | Migration-marker presence skips the whole migration. No matching-payload result or typed mismatch refusal. Initialization/migration state guard, not the intake replay rule; Explicitly excluded by the settled scope ruling. |
| 13 | `memory_core/store.py`, `_backfill_relation_events`:1289 | `node/src/store.ts`, `backfillRelations`:358 | Python:1302, TS:371; `memory_relation_events_v4` | Any existing endpoint/type tuple skips backfill; otherwise append derived migration keys for assert/retract history. No digest equality check/refusal. State-driven migration guard, not a durable-key replay rule. Explicitly excluded by the settled scope ruling. |

## Checked insert mechanisms that are not check-then-insert replay sites

- Receipt issuance: `IdentityMemory._issue_receipt` (`identity.py`:414/423)
  and `AuthorityV2.#issue` (`authority.ts`:287/294) do an atomic
  `INSERT ... ON CONFLICT(binding_sha256) DO NOTHING`, then select by binding
  SHA. There is no pre-check lookup. The public issue commands validate their
  target/current state and require TTY confirmation; those checks are not a
  receipt-key replay lookup. No new recovery law is inferred for PK collisions.
- Evidence insertion: Python `_insert_evidence`:1541/1557, TS
  `insertEvidence` (`kernel.ts`:60/88), authority `#insertEvidence`
  (`authority.ts`:533), and migration evidence copies use atomic
  `ON CONFLICT(identity_version,evidence_sha256) DO NOTHING`, then read the
  identity. No pre-check.
- Evidence links use `ON CONFLICT DO NOTHING` / `INSERT OR IGNORE`; cues use
  an atomic endpoint/profile/normalized-cue upsert. No prior-result replay.
- Revision/lifecycle writes and authority relation writes have derived keys,
  but no independent idempotency pre-check. Their enclosing operation/receipt
  guards are rows 2–5 and 9–11; they must not acquire a separate replay rule.
- v2 copy `INSERT OR IGNORE` statements and schema metadata upserts do not
  add an independent lookup/replay path beyond rows 12–13.
- `IdentityMemory._submit` (`identity.py`:1047) / `submit` (`identity.ts`:180)
  can skip by current record existence (`"exists"`), then delegate to intake;
  bootstrap, log-phase, log-fact and close-loop delegate to the inventoried
  writers. `apply_receipt` routes by receipt purpose, then delegates to rows
  9–11. These wrappers do not add an independent INSERT boundary.
- `MemoryRuntime.submit` and consumer bootstrap delegate to intake. WorkStore
  reads files only. Access rows deliberately have no replay (G12). Decay delegates
  maintenance and separately writes its activation sidecar (G9).
- File-existence refusals in migration/profile CLI paths are not durable-key
  SQLite idempotency sites. No P18 file replay is inferred.

## Durable keys, transaction status and expected concurrent result

Each paired row applies to both files/functions in the first table. A loser may
also return the settled `StoreBusy`; an `IntegrityError` is never an accepted
same-key result. The gate required by §3.5c holds the winner after its insert
and before commit, then starts the loser and releases the winner.

| Site | Durable key / pre-check / deciding write | Python BEGIN IMMEDIATE | TS BEGIN IMMEDIATE | Expected same-key concurrent result |
| --- | --- | --- | --- | --- |
| 1 intake submit | `idempotency_key`; prior intake lookup; received-intake INSERT | Separate G2 units; P18 recovery only on unit-2 rollback | Separate G2 units; P18 recovery only on unit-2 rollback | Exact fresh intake replay, including `received`; different SHA → existing typed ValueError |
| 2 create | operation `idempotency_key`; operation lookup; operation/record/revision INSERTs | Moved by P17 | Already inside | Prior operation result, unconditional G1 replay |
| 3 revise | operation `idempotency_key`; operation lookup; operation/revision INSERTs | Moved by P17 | Already inside | Prior operation result, unconditional G1 replay |
| 4 invalidate | operation `idempotency_key`; operation lookup; operation/lifecycle INSERTs | Moved by P17 | Already inside | Prior operation result, unconditional G1 replay |
| 5 maintenance | explicit key or `maintenance:<run_id>`; operation lookup; adjustments and operation INSERT | Moved by P17 | Already inside | Prior operation result, unconditional G1 replay |
| 6 add relation | event `idempotency_key`; event lookup; relation-event INSERT | Moved by P17 | Already inside | Prior event with `status="duplicate"` |
| 7 retract relation | event `idempotency_key`; event lookup; relation-event INSERT | Moved by P17 | Already inside | Prior event with `status="duplicate"` |
| 8 core proposal | `proposal_sha256` / derived proposal ID; SHA lookup; proposal INSERT | Already inside | Already inside | Decorated prior proposal with `status="existing"` |
| 9 core apply | receipt ID / `authority-v2:<receipt_id>`; validated consumption→operation lookup; operation, decision, consumption INSERTs | Already inside | Already inside | Existing receipt replay result after integrity/purpose/profile/authority checks |
| 10 retract consumer | receipt ID / `authority-v2:<receipt_id>`; validated consumption→operation lookup; operation, lifecycle, consumption INSERTs | Already inside | Already inside | Existing receipt replay result after integrity/purpose/profile/authority checks |
| 11 legacy close consumer | receipt ID / `authority-v2:<receipt_id>`; validated consumption→operation lookup; operation, relation event, consumption INSERTs | Already inside | Already inside | Existing receipt replay result after integrity/purpose/profile/authority checks |
| 12 v2 migration marker | `memory_meta_v3.key='migrated_from_v2'`; marker lookup; copied rows and marker INSERT | Moved by P17; caller initialize holds lock before `_migrate_v2` | Moved by P17; caller `migrateTo` holds lock before `migrateV2` | Skip an already migrated stream; existing migration result |
| 13 relation backfill | endpoint/type tuple; existing event lookup; derived migration event INSERTs | Moved by P17; initialize caller holds lock | Moved by P17; migrateTo caller holds lock | Existing tuple skips backfill |

### Initialization guard discovered during caller verification

The entry guard in Python `MemoryStore.initialize` originally called
`schema_info` before its write transaction. TypeScript `MemoryStore.initialize`
similarly called `schemaInfo` and returned `changed:false` outside a write
transaction. The Python legacy table-existence read was also before schema
execution and the later BEGIN. These are initialization state/existence guards,
so the final ruling explicitly requires their reads to move inside.

The local patch holds BEGIN before those reads and the schema
writes. Python executes complete schema statements individually: `executescript`
would implicitly commit the guard transaction. TS `DatabaseSync.exec` retains
its explicit transaction. No schema SQL is edited. The initialization header-byte impact is settled in PR #29 / Lam @ 1444390.
The readiness DELETE no-op and owned-WAL repair have focused regressions in
both runtimes. Foreign/future WAL is refused before SQLite opens it.

## Verification

- The public intake same/different/received schedules pass in both directions
  (three paired tests, six schedules). The loser equals the exact Python
  pre-check replay at the intermediate, only its unit-1 evidence commits, the
  final dumps match across directions, and the commit-unit Python witness,
  both doctors and transient-sidecar checks pass.
- All 10 excluded public-site families pass both directions (20 same-key gates).
  The winner pauses after its INSERT inside its immediate transaction. The
  loser signals immediately before its actual BEGIN, blocks until the holder
  releases, then returns the exact serial replay. Test-only SQL adapters assert
  the literal BEGIN IMMEDIATE boundary protects the pre-check and INSERT;
  receipt integrity-before-replay is unchanged. No recovery was added here.
- Focused R3 Python laws: 210 tests, including 33 intake/site tests and 16
  whole-file/hash-set proof tests. Focused Node: 12 tests. These include ready DELETE bytes/mtime/sidecars,
  owned-WAL conversion, foreign/future WAL refusal, unrelated constraint-unit
  propagation, no-row/wrong-derived-ID/non-constraint rethrow and rollback proof.
- Shared-key X: 18 schedules (three variants × three seeds × both directions),
  plus 18 distinct-key schedules, all 36 passed locally on macOS. X sequential
  passes 32 fixed matrix plans + 200 seeded plans (1,246 steps); the crash gate
  passes 78 real kills and opposite-runtime retries. Three-OS CI is reported
  independently from these local results.

### Settled initialization and derived-hash corpus impact

PR #29 (`1444390`) permits exactly header offsets 24–27 and 92–95, proved
by a whole-file masked byte comparison. PR #30 (`94ee892`) additionally
permits derived SHA references, each checked against the exact old/new
SQLite bytes that pass that same comparison. All validated hash tokens in
an expected JSON file are masked together; every remaining raw byte,
including whitespace and key order, must match. No runtime comparator or
CI full-tree diff is weakened.

The pinned seed-0 proof covers 129 changed checked-in SQLite files:
writes-v5 18, authority-v2 33, MCP-v1 26, CLI-v1 52. CLI additionally has
101 changed hash fields in 61 `expected.json` files. The old Python CLI
oracle is regenerated from implementation commit `2624e10` and must
match every existing non-MANIFEST payload before any reference is trusted.
For final-output hash fields, `tools/r3/fixture_references.py` retains the
actual transient oracle file bytes at the existing inspection seam in
both old and new generators; an input fixture hash is never substituted
for a written output hash. Each old/new commitment and whole-file mask
proof is checked by `tools/r3/fixture_headers.py`. This keeps unchanged,
backup and written classes intact. Both §6 acknowledgement carry lines,
and the new derived-hash line, name the settled Lam heads.

P18 proof failure also covers a failed fresh read: neither runtime can
establish the same-key row in that case, so it rethrows the original
unit-2 constraint. This is covered by a failing-then-passing regression
in each runtime. Recovery remains intake-only.
