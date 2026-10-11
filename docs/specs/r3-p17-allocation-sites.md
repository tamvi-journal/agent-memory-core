# P17 allocation inventory

Recorded before the P17 product patch, against main `6308e8c` plus the local R3 work.
The reads below decide the write in the same row. Caller transactions protect held-connection helpers.

| Python file / function | Read and decided write | TypeScript file / function | Before patch |
| --- | --- | --- | --- |
| `memory_core/store.py` / `create_current` | operation key and record existence → record, revision 1, operation and lifecycle | `node/src/kernel.ts` / `createCurrent` | Python deferred; TS `transaction` immediate |
| `memory_core/store.py` / `revise` | operation key and current revision → revision number +1, parent, operation and lifecycle | `node/src/kernel.ts` / `revise` | Python deferred; TS immediate |
| `memory_core/store.py` / `invalidate` | operation key and current revision → invalidation operation/lifecycle | `node/src/kernel.ts` / `invalidate` | Python deferred; TS immediate |
| `memory_core/store.py` / `_append_relation_event` | operation key and latest relation → no-op or sequence +1 event | `node/src/kernel.ts` / `relation` | Python deferred; TS immediate |
| `memory_core/store.py` / `record_access` | revision owner → access row and telemetry update | `node/src/kernel.ts` / `recordAccess` | Python deferred; TS immediate |
| `memory_core/store.py` / `apply_maintenance` | operation key and current revision/telemetry → adjustment and operation | `node/src/kernel.ts` / `applyMaintenance` | Python deferred; TS immediate |
| `memory_core/store.py` / `_append_lifecycle` | per-record MAX(sequence)+1 → lifecycle row | `node/src/kernel.ts` / `appendLifecycle` | Held kernel or authority transaction; Python kernel callers need patch |
| `memory_core/store.py` / `_revise_in` | current revision → revision +1 and lifecycle | `node/src/authority.ts` / `coreApply` | Both authority consumers already immediate |
| `memory_core/store.py` / `_invalidate_in` | current revision → evidence links and invalidated lifecycle | `node/src/authority.ts` / `retract` | Both authority consumers already immediate |
| `memory_core/store.py` / `_retract_relation_in` | latest relation supplied by consumer → sequence +1 retraction | `node/src/authority.ts` / `closeLegacyDiscussion` | Both consumers already immediate before fetching latest |
| `trajecta_identity/identity.py` / `core_propose` | current core and existing proposal hash → proposal bound to current revision | `node/src/authority.ts` / `corePropose` | Both already immediate |
| `trajecta_identity/identity.py` / `_issue_receipt` | binding conflict handled by INSERT/lookup → receipt | `node/src/authority.ts` / `issue` | Both already immediate |
| `trajecta_identity/identity.py` / `core_apply` | receipt integrity/replay, proposal decision and actual core → decision, revision/lifecycles, consumption | `node/src/authority.ts` / `coreApply` | Both already immediate; TS lifecycle MAX+1 is held here |
| `trajecta_identity/identity.py` / `retract` | receipt integrity/replay and current record → invalidation and consumption | `node/src/authority.ts` / `retract` | Both already immediate |
| `trajecta_identity/identity.py` / `close_legacy_discussion` | receipt integrity/replay, latest relation and current core → retraction and consumption | `node/src/authority.ts` / `closeLegacyDiscussion` | Both already immediate |
| `memory_core/store.py` / `_migrate_v2` | migrated marker and legacy history; lifecycle MAX+1 via `_append_lifecycle` → copied history and lifecycle | `node/src/store.ts` / `migrateV2` | Python implicit DML transaction; TS autocommit, including lifecycle MAX+1 |
| `memory_core/store.py` / `_backfill_relation_events` | legacy rows and existing event stream → sequence 1/2 backfill | `node/src/store.ts` / `backfillRelations` | Python implicit transaction or no transaction for first read; TS autocommit |

Patch scope: start `BEGIN IMMEDIATE` at the six Python public kernel allocation sites, before their first deciding read. Start it after schema execution and before migration/backfill reads on the migration connection, in both runtimes. Held authority helpers and ordinary TS kernel writers already meet P17 and receive no semantic change.

Excluded reads: retrieval/doctor/views, profile and work-file parsing, owner confirmation previews, intake existence/semantic evaluation before G2 commits, and activation calculations before their existing maintenance unit. These do not allocate a sequence in that read's transaction; combining them with later writes would alter the frozen G2/G9/G10/G12 boundaries. Cue upsert, evidence deduplication and intake insert/decision use single SQL writes or conflict clauses without a read-compute-write allocation. Their boundaries remain unchanged.
