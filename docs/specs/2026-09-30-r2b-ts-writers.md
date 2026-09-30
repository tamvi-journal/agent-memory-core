# R2b — TypeScript writers (non-authority)

Status: settled by Lam review @ f881281 (Q1–Q6 and F1–F4 applied). Author: Aux (leader). Builds on R0 (`2026-09-30-r0-frozen-semantics.md`) and R2a (`2026-09-30-r2a-authority-v2.md`, merged as f5803ca).

## 0. Goal and scope

R2b gives `@trajecta/identity` every **write** in R0 §7 that R2a did not port, so that the TS runtime can run the identity product end to end without the Python writer. Python stays the oracle. Meaning does not change, except for the two v4-contract patches in §7 (P4, P5), which land in both runtimes at once.

**In scope**
- Kernel writes (R0 §7.1): `initialize`, `create_current`, `revise`, `invalidate`, evidence, lifecycle, telemetry, `semantic_hash`, `add_cue`, `add_relation`, `retract_relation`, `record_access`, `apply_maintenance`.
- Governance (R0 §7.2): `ValidatedIntake` and `GovernancePolicy`, and `MemoryRuntime.submit` with `pinned_guard`.
- Identity writers (R0 §7.3 minus authority): `bootstrap` (anchors, VHO seed, core seed, `skip_if_exists`), `log_phase`, `log_fact`, `close_loop`.
- Activation (R0 §7.4): recall tracking in `retrieve` (`record_access` and `apply_recall`), and `decay` with its sidecar.
- Migration: `migrate_to` from v2, v3 and v4 to v5 (see Q2), on a copy only.
- Read-only work-store bridge (`trajecta_identity/work.py`), used to validate `work_refs`.
- Replacing the R2a TS replay fixtures with real writers (R2a carry item 1).

**Out of scope**
- R2c: the TS MCP server, raw JSON-RPC boundary parsing, connection lifecycle and revalidation, and `readOnlyHint` semantics for `identity_retrieve`.
- R3: cross-runtime runs and the migration rehearsal on a copy of a real store.
- The gap fixes G1–G11: frozen now, fixed after R3, in both runtimes.
- `migrate_aml.py`, the AML import tool (see Q6).

## 1. Oracle and parity

- **Python is the oracle.** A new corpus, `spec/golden-writes-v5/`, has the same shape as `spec/golden-authority-v2/`: `script.json`, `store.sqlite3`, `dump.json` and `cases.jsonl` per scenario, plus a MANIFEST with provenance (`oracle_base_commit`, `oracle_semantics`, `oracle_sources`, `python`, `unicode`, `sqlite`).
- **Generation** runs from an empty store with the injected clocks (§4), under PYTHONHASHSEED 0, 1 and 4294967295. `dump.json` and `cases.jsonl` must be byte-identical across the three seeds. In CI the `.sqlite3` bytes are compared only inside the pinned `python:3.11.15` container; everywhere else the dumps and cases are the oracle, because SQLite page bytes depend on the SQLite build.
- **TS replay** runs each `script.json` against an empty v5 store through the **public** TS writers. It asserts:
  - every action result equals `cases.jsonl` exactly, including error type and message;
  - the canonical dump equals `dump.json` byte for byte, except for the decay tolerance in §6.
- **R0 and R2a corpora stay frozen.** The R2a TS replay must drop `seedBootstrap`, `seedPhase` and the direct `guardPinned` call, and drive `bootstrap`, `log_phase` and `MemoryRuntime.submit` through the same public TS writer API that product code uses. There are no hidden seed helpers. The existing `dump.json` files must still match byte for byte. That is the acceptance test for carry item 1.

## 2. Writer inventory (port list)

Each item is ported from the named Python source, with the same errors, the same messages and the same transaction boundaries.

| Area | Python source | Notes |
|---|---|---|
| schema / initialize | `memory_core/store.py` `initialize`, `_bootstrap_writes` | same DDL (schema.sql, digest-checked), same PRAGMAs, writable only on `ready` or `uninitialized` |
| create / revise / invalidate | `store.py` | `_revise_in` and `_invalidate_in` already exist in TS from R2a and are reused |
| cues | `store.add_cue` | upsert on `(profile, cue_norm, target)` (G4) |
| relations | `store.add_relation`, `retract_relation` | no-op rule: weight within 1e-9 **and** the same source revision; weight in [0, 10]; `from != to` |
| access | `store.record_access` | gain in [0, 1]; `cue_sha256 = sha256(utf8(cue))` |
| maintenance | `store.apply_maintenance` | §7.1 rules; key `maintenance:{run_id}` (see G11) |
| intake | `memory_core/governance.py` | full port (Q1): default thresholds, every hold reason, protected domains, idempotency refusal on a different `proposal_sha256`; the identity runtime uses `SELF_AUTHORED_POLICY` |
| runtime | `memory_core/runtime.py` `submit`, `retrieve` (tracking), `apply_maintenance` | `doctor` belongs to R2c (G6) |
| identity | `trajecta_identity/identity.py` `bootstrap`, `log_phase`, `log_fact`, `close_loop`, `_submit`, `_relate`, `_cues`, `_compose`, `_require_existing`, `_evidence` | |
| VHO seed | `memory_core/vho.py` `vho_open_seed` | `VHO_VERSION`, adoption and notes from the profile |
| activation | `trajecta_identity/activation.py` `apply_recall`, `run_decay`, `state_of` | §5, §6 |
| work bridge | `trajecta_identity/work.py` | read-only; §8 |
| migration | `store.migrate_to`, `_migrate_v2`, v3 path, and the existing TS `migrateV4To` (generalized; the backup name follows the source version) | Q2 = yes; §11 guardrails |

## 3. Canonical forms added in R2b

These are the encodings the R2b writers use on top of R0 §4.

- **`py_json_dumps(value, sort_keys, separators=(", ", ": "), ensure_ascii=False)`.** This is Python's *default*, non-compact form. It is used by:
  - `_digest(*parts)`, as frozen in R0 §3 (sort_keys=True, `default=str`);
  - `_compose` for `Phase context: …` (sort_keys=True, ensure_ascii=False);
  - the decay sidecar (`{"last_decay_at": "<iso>"}`: default separators, no sort, ensure_ascii=True, and no trailing newline).

  Key order is by code point. Numbers are typed, as in R0 §4.2, so `1` and `1.0` differ, and they must come from the lossless AST, never from `JSON.parse`.
- **Numeric kind is part of the input (F1).** A plain JS `number` cannot tell JSON `1` from `1.0`, but the frozen law can: `_digest(title, summary, content, confidence)`, `hash_payload`, `_compose` `Phase context`, and the stored canonical JSON all depend on the Python type.
  - Any structured input that can reach a hash, an idempotency key or stored canonical JSON is a lossless `JsonValue` / `OrderedObject`, using the R1 and R2a `PyInt` and `PyFloat` types. That covers `phase_context`, `vho_stack`, evidence items, governance `changes`, and the evidence payload values.
  - Numeric parameters whose Python type affects dumps or keys (`confidence`, and numeric `changes` fields) accept a typed `PyInt` or `PyFloat` at the public writer layer.
  - An ergonomic overload may accept a JS `number`. It is then **always `PyFloat`**. Kind is never inferred from mathematical integrality.
  - The corpus parses `script.json` losslessly and passes the values through the **same public writer methods** that product code calls. There is no test-only internal AST path.
  - A corpus case proves the whole path: `confidence` as `PyInt(1)` vs `PyFloat(1.0)` gives different fact keys, exactly as in Python.
- **`round6(x)`** is Python `round(x, 6)`, which is correctly rounded half-even on the exact binary value. TS: `Number(pyFixed(x, 6))`, reusing the R0 BigInt `py_fixed`. A test covers round-half cases and values whose `toFixed` differs.
- **Slices** use code points: `reason[:300]` in `_relate`, and `proposal.reason[:300]` (already in R2a).
- **`_check_id`**: `^[A-Za-z0-9._:@-]{2,120}$`, ASCII only, the same message.
- **Work ref pattern**: `\b(?:work|delta):[0-9a-fA-F-]{8,}\b`, and the `Work refs (trajecta-work-memory): ` line, split on `,` and stripped with the §4.6 `str.strip`.
- **Timestamps**: see §4.

## 4. Clocks

- **Seconds clock** (`utc_now`): `YYYY-MM-DDTHH:MM:SS+00:00`, as in R0 §3.
- **Microsecond clock** (recall `run_id`): `datetime.now(utc).isoformat(timespec="microseconds")`, which always has six digits.
- **Decay `moment.isoformat()`**: microseconds are shown **only when non-zero**. `now` arguments are parsed with Python `fromisoformat` semantics after `Z → +00:00`; naive values are treated as UTC.
- **Elapsed days**: `(moment − since).total_seconds() / 86400.0`. `total_seconds` is an exact integer number of microseconds divided by 10⁶, correctly rounded. TS parses timestamps to integer microseconds itself, never through `Date`, which has millisecond precision.
- **Production TS microsecond clock (Q3, F4).**
  - The serialized value is always **UTC wall time** in Python's microsecond ISO shape, `YYYY-MM-DDTHH:MM:SS.ffffff+00:00`, with exactly six digits.
  - It is strictly increasing within one process. The algorithm, per process:
    ```
    candidate_us = BigInt(Date.now()) * 1000n
    emitted_us   = max(candidate_us, last_emitted_us + 1n)
    last_emitted_us = emitted_us
    ```
  - Raw `hrtime` is never serialized, and no `hrtime` bits are used as if they were wall microseconds. R2b uses no `hrtime` refinement at all.
  - When wall time moves backwards, the output keeps increasing by 1 µs per call until wall time catches up. When wall time jumps forward, the output follows it.
  - Collisions across processes or restarts remain G11.
  - Every clock (the seconds clock, the µs clock, and decay `now`) is injectable. The corpus uses injected clocks only.
  - Unit tests:
    - two or more calls within the same millisecond increase by at least 1 µs;
    - with wall time moving backwards, the output still strictly increases;
    - with a forward jump, the output follows it;
    - there are always six microsecond digits, including `.000000`;
    - an injected clock yields the exact corpus stamps.

## 5. Recall tracking (R0 §6.5 track, §7.4)

- `retrieve(track=true)` on a `ready` store:
  1. `record_access` per selected hit (rank, first 5 reasons joined with `,`, gain);
  2. then `apply_recall`: pinned records are skipped; a direct hit (a `cue:` or `lexical:` reason) gets the direct gain, otherwise the graph gain; the value is capped; `woke:` lifts it to `wake_to`; a change is recorded when `|Δ| > 1e-9`, with `new_value = round6`; one maintenance run `recall:{µs-stamp}`.
- On `legacy-v4`, tracking is off, as fixed in R2a f0c2428. The TS runtime must match this.
- **Ego guard.** Recall never touches stability; a negative test proves it.
- **G12 (frozen).** A tracked retrieval is several transactions: each `record_access` commits on its own, and `apply_recall` runs a separate maintenance transaction afterwards. The port keeps exactly these boundaries.

## 6. Decay

- The algorithm is R0 §7.4, exactly as `run_decay` implements it (pinned and `anchor` skipped; `since = max(last_run, last_accessed_at, created_at)`; `half_life = 21 × (0.5 + stability)`; `new = min(cap, v × 0.5^(days / half_life))`; recorded when `|Δ| > 1e-6`, with `round6`). The sidecar path is `<db stem>.activation.json`.
- **Tolerance (settled Q6 of R0).** `0.5 ** x` is libm `pow` in Python and V8's own `Math.pow` in TS, so they may differ in the last ulp. In the decay scenarios only:
  - only a **whitelist** of decay-derived numeric fields compares with |Δ| ≤ 1e-6, record by record: the maintenance adjustment `new_value`, and the resulting `accessibility` of the adjusted records. There is no generic "numbers within 1e-6" deep comparator; every other number, key and order is exact;
  - every discrete outcome compares exactly: which records are adjusted, the count, the states histogram, `run_id`, and the sidecar bytes.
- **Boundary fixtures** are required: a value whose |Δ| sits just above the 1e-6 threshold, and a `round6` half-way case. The generator asserts that each fixture keeps a margin of at least 1e-9 from the decision boundary, so an ulp difference in `pow` cannot flip a discrete outcome. Any fixture that fails the margin check is rejected at generation time.

- **G9 regression (frozen ordering).** One test proves that the maintenance commit happens before the sidecar write. If the sidecar write fails (for example, a read-only sidecar path), the committed DB state remains and the error surfaces. That is frozen as G9 in both runtimes; TS must not "fix" it on its own.

## 7. Legacy-v4 is not writable, on every write path (oracle patches P4, P5)

R2a Q1 says `legacy-v4` is readable and not writable. The R2b audit of the oracle found two write paths that break this.

- **P4 — decay writes its sidecar on a v4 store.** Reproduced on a copy of `spec/golden/identity-open`: `decay()` with no adjustments returns successfully and creates `store.activation.json` next to the v4 store. With adjustments it raises `MigrationRequired`, but only **after** the sidecar logic has run. Fix, in both runtimes: `decay` checks writability first and raises `MigrationRequired("schema v4 store must be migrated to v5 before writing")` before reading or writing the sidecar. The test copies a v4 store, calls `decay` with both a "no adjustment" and an "adjustment" moment, and asserts the error, identical bytes and an unchanged directory listing.
- **P5 — writer-entry gate (F2).** Every public mutation entry point calls `requireWritable()` **first**. That covers the identity writers (`log_phase`, `log_fact`, `close_loop`, `bootstrap`, `decay`), the runtime and intake `submit`, and the kernel `create_current`, `revise`, `invalidate`, `add_cue`, `add_relation`, `retract_relation`, `record_access` and `apply_maintenance`. The gate runs before:
  - any semantic `no_op`, `exists` or idempotent-replay shortcut: `log_fact` with the same content, `close_loop` when the loop is not open, `bootstrap` when everything exists, relation and cue no-ops, and a maintenance replay;
  - any DB or sidecar mutation, and any relation, cue or maintenance call.

  Otherwise a v4 store could get a successful mutation-API answer, which contradicts R2a Q1. The allowed states are:
  - `bootstrap`: `ready` or `uninitialized`;
  - every other mutation API: `ready` only.

  `retrieve` is a read surface, so on `legacy-v4` it keeps the R2a f0c2428 rule: tracking is turned off, and it does not refuse. Tests, on v4 copies, assert `MigrationRequired` with identical bytes and listing for:
  - each writer on a path that would mutate;
  - at least these no-op and exists paths: `log_fact` with the same content, `close_loop` when not open, a repeat `bootstrap`, and a relation no-op.

Neither patch changes any v5 result, so the R0 and R2a corpora are unaffected. The provenance `oracle_semantics` gains `P4-decay-v4-refusal` and `P5-writer-v4-precheck`.

## 8. Work-store bridge (read-only)

- Port `WorkStore` exactly:
  - root from `$TRAJECTA_WORK_ROOT`, else the profile's `work_root`;
  - `state.json` must have schema `trajecta.state/v1` and a list `work`; a missing file means empty;
  - `deltas.jsonl` is read line by line; blank lines and **unparseable lines are skipped** (a torn final line belongs to the work store's own recovery);
  - `resolve`, `missing` and `summarize` have the same output shape and key order.
- JSON is parsed with the lossless parser (repeated key: first position, last value).
- It never writes. A test asserts that the work-store directory bytes and listing are unchanged after `log_phase` with `work_refs`.

## 9. Pinned guard in TS (R2a F6 carried)

- `pinned_guard` sits on the identity runtime's TS store and is checked in every public writer: `createCurrent`, `revise`, `invalidate`, `ValidatedIntake.submit`, `MemoryRuntime.submit`, and the identity methods.
- The two exceptions are the same as in Python:
  - `bootstrap`, through an internal bootstrap-writes context;
  - the R2a held-transaction authority materializers.
- The R2a scenarios `negative-writer-pinned` and `negative-writer-phase-fact-pinned` are replayed through these real writers.

## 10. Transactions, idempotency and crashes

- **Kernel operations:** one `BEGIN IMMEDIATE` each, exactly where Python has one.
- **G10 (new, frozen): compound identity writers are not atomic.** `log_phase` and `log_fact` run the intake submit, then each relation, then each cue, in separate transactions. A crash in between leaves the record without some of its links or cues. The oracle **converges under an identical retry**:
  - `log_phase` returns `exists` but re-applies links, the open loop and cues;
  - `log_fact` with the same content is a `no_op` that still re-links;
  - relations are no-ops when unchanged;
  - cues upsert.

  R2b freezes this behaviour; the fix (one transaction) comes after R3, with G2 (Q5).
- **Crash tests (TS, mandatory):** a real child-process kill (a test hook calling `process.exit` or `process.kill(process.pid, "SIGKILL")`, not a thrown error):
  1. mid-`create_current`, after the revision insert and before commit → reopen: no partial row;
  2. mid-`apply_maintenance`, after the first adjustment → reopen: no adjustment and no operation;
  3. a tracked `retrieve` killed after the first `record_access` commit and before the remaining accesses and `apply_recall` → reopen shows the oracle's partial committed access state (one access row, one count increment, no recall maintenance). An identical retry is **not** convergent: the test asserts the additional access rows and count on the retried hits, and that the final state differs from an uninterrupted run exactly by those extra accesses (G12);
  4. `log_phase` killed after the submit commit and before the first relation → reopen shows the record without links; an identical retry then yields current rows, active relations and cues semantically equal to an uninterrupted run (ignoring timestamps and clock-derived ids).

## 11. Corpus scenarios (`spec/golden-writes-v5/`, minimum)

- **Bootstrap:** fresh store; a second run (all `exists`); an invalid profile core (the `validate_core` error); VHO adoption and notes from the profile.
- **Phase:**
  - create, then repeat (`exists`);
  - `follows`, `caused_by` and `depends_on` links;
  - an unknown link id (error; no write);
  - `open_loop`;
  - cues, including the title cue and a blank cue skipped;
  - `decided_because`;
  - `phase_context` with typed numbers and non-ASCII text;
  - `occurred_at`;
  - an invalid `event_id`;
  - outside evidence items.
- **Work refs:** known work and delta refs; an unknown ref (error); a torn `deltas.jsonl` line; the wrong state schema (error); no work root (refs kept as text).
- **Fact:** create; the same content (`no_op`, re-links); a changed summary (`refine`, history kept); an int vs float confidence (different keys); a pinned-id fact (refused).
- **`close_loop`:** open, then close; close when not open (the oracle's result, frozen).
- **Intake** (Q1: driven through the default policy directly, as well as through identity's self-authored policy): each hold reason; a reused key with a different payload (refused); a semantic no-op.
- **Kernel:**
  - relation no-op and weight bounds;
  - `from == to`;
  - `record_access` gain bounds;
  - maintenance: `old_value` mismatch, clamp, skip within 1e-9, and a semantic-hash drift abort;
  - a cue upsert changing the weight.
- **Recall tracking:** direct vs graph gain, the cap, `woke:` to `wake_to`, pinned skipped, stability untouched, and two recalls with distinct µs run ids.
- **Decay:** first run (sidecar absent before the run; the oracle writes it after any successful run, even with zero adjustments); an incremental second run; a pinned or anchor record skipped; the cap enforced; the boundary fixtures of §6; a `now` with and without microseconds (the `isoformat` rule).
- **Legacy-v4:** P4 and P5 on a copy of a v4 store.
- **Migration (Q2 = yes):** v2 → v5, v3 → v5 and v4 → v5 on copies, with these guardrails:
  - the source is never mutated;
  - a WAL, foreign or future source fails before any target mutation;
  - `--dry-run` leaves no target, no backup and no sidecar;
  - a successful backup is byte-identical to the source;
  - the destination's semantic dump equals Python's;
  - the backup name is derived from the source's detected version, and never hard-codes `v4`.

## 12. Known gaps added (frozen now, fixed after R3, in both runtimes)

| # | Gap | Fix after R3 |
|---|---|---|
| G9 | The decay sidecar is written after the maintenance commit, outside the transaction. A crash in between leaves `last_decay_at` stale, so the next run decays the same interval twice. | derive `last_decay_at` from the latest committed `decay:` maintenance operation (in the DB); retire the sidecar |
| G10 | `log_phase` / `log_fact` are multiple transactions; they converge only under an identical retry | one `BEGIN IMMEDIATE` per compound write (together with G2) |
| G11 | The maintenance key is `maintenance:{run_id}`, and a replay returns the prior result without comparing (G1). Two recall runs with the same µs stamp silently drop the second run's adjustments. | the run id carries a per-run nonce, or the replay compares the adjustments (G1 fix) |
| G12 | A tracked `retrieve` commits each `record_access` separately, then `apply_recall` separately. A crash in between leaves partial access rows and counts with no recall maintenance, and an identical retry does **not** converge: it adds access again and moves `last_accessed_at`. | one tracked-retrieval write transaction, or access events that are idempotent and bound to the run (with G1, G2 and G11) |

## 13. CI and acceptance

- **Node 22 on Ubuntu, macOS and Windows:** the R0 read corpus, the R2a authority corpus (now through real writers), the R2b writes corpus, the crash tests, and the P4/P5 tests.
- **Python matrix:** unchanged, plus the P4/P5 tests and a generator test for the `golden-writes-v5` manifest.
- **Golden job:** regenerate `golden-writes-v5` under three hash seeds in the pinned container, and diff.
- **The PR is done when:**
  - every writes scenario replays exactly in TS (decay within §6);
  - the R2a replay no longer uses fixture seeds;
  - the crash tests pass on all three OSes;
  - there is no `JSON.parse` on any value that reaches a hash, a key or stored content;
  - schema.sql is unchanged (sha `a8f283d7…`).

## 14. Settlement (Lam, 2026-09-30, review @ f881281)

- **Q1 — yes, full governance.** Default thresholds, hold reasons, protected domains and idempotency refusal are frozen R0 law. The corpus drives the default policy directly. The identity runtime keeps `SELF_AUTHORED_POLICY`.
- **Q2 — yes, migration in R2b,** with the §11 guardrails. R3 rehearses with the TS writer.
- **Q3 — yes,** a strictly increasing µs UTC wall clock, with the exact contract of §4 (F4).
- **Q4 — yes, P4 and P5 now, in both runtimes.** P5 is an entry gate (F2).
- **Q5 — yes, freeze G10** and prove identical-retry convergence. **Add G12** for tracked retrieval, which is non-atomic and does not converge on retry. Both are fixed after R3, with G1 and G2 (G12 also with G11).
- **Q6 — yes,** `migrate_aml.py` stays Python through R2b and R3. R4 decides whether to ship, archive or port it.
- **F1** — numeric kind is preserved at the public writer layer (§3).
- **F2** — `requireWritable()` at every mutation entry, before no-op, exists and replay branches (§7 P5).
- **F3** — G12, with a crash and parity test (§10, §12).
- **F4** — the µs clock contract and its unit tests (§4).
- **Notes applied:**
  - the decay tolerance is a whitelist, compared record by record;
  - "first run" means the sidecar is absent before the run;
  - a G9 ordering regression test;
  - WorkStore duplicate keys follow Python (first position, last value);
  - the R2a replay goes through the public writers only.
