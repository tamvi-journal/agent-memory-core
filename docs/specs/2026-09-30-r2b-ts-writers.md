# R2b — TypeScript writers (non-authority)

Status: draft for Lam review. Author: Aux (leader). Builds on R0 (`2026-09-30-r0-frozen-semantics.md`) and R2a (`2026-09-30-r2a-authority-v2.md`, merged as f5803ca).

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
- **R0 and R2a corpora stay frozen.** The R2a TS replay must drop `seedBootstrap`, `seedPhase` and the direct `guardPinned` call, and drive `bootstrap`, `log_phase` and `MemoryRuntime.submit` for real. The existing `dump.json` files must still match byte for byte. That is the acceptance test for carry item 1.

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
| intake | `memory_core/governance.py` | thresholds, hold paths, idempotency refusal on a different `proposal_sha256` (see Q1) |
| runtime | `memory_core/runtime.py` `submit`, `retrieve` (tracking), `apply_maintenance` | `doctor` belongs to R2c (G6) |
| identity | `trajecta_identity/identity.py` `bootstrap`, `log_phase`, `log_fact`, `close_loop`, `_submit`, `_relate`, `_cues`, `_compose`, `_require_existing`, `_evidence` | |
| VHO seed | `memory_core/vho.py` `vho_open_seed` | `VHO_VERSION`, adoption and notes from the profile |
| activation | `trajecta_identity/activation.py` `apply_recall`, `run_decay`, `state_of` | §5, §6 |
| work bridge | `trajecta_identity/work.py` | read-only; §8 |
| migration | `store.migrate_to`, `_migrate_v2`, v3 path, and the existing TS `migrateV4To` | Q2 |

## 3. Canonical forms added in R2b

These are the encodings the R2b writers use on top of R0 §4.

- **`py_json_dumps(value, sort_keys, separators=(", ", ": "), ensure_ascii=False)`.** This is Python's *default*, non-compact form. It is used by:
  - `_digest(*parts)`, as frozen in R0 §3 (sort_keys=True, `default=str`);
  - `_compose` for `Phase context: …` (sort_keys=True, ensure_ascii=False);
  - the decay sidecar (`{"last_decay_at": "<iso>"}`: default separators, no sort, ensure_ascii=True, and no trailing newline).

  Key order is by code point. Numbers are typed, as in R0 §4.2, so `1` and `1.0` differ, and they must come from the lossless AST, never from `JSON.parse`.
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
- **Production TS microsecond clock**: `Date` alone has millisecond resolution, which makes G11 collisions far more likely than in Python. TS therefore uses wall-clock milliseconds plus a sub-millisecond part from `process.hrtime.bigint()`, and must be **strictly increasing within a process** (on a tie, add 1 µs). Every clock is injectable for the corpus.

## 5. Recall tracking (R0 §6.5 track, §7.4)

- `retrieve(track=true)` on a `ready` store:
  1. `record_access` per selected hit (rank, first 5 reasons joined with `,`, gain);
  2. then `apply_recall`: pinned records are skipped; a direct hit (a `cue:` or `lexical:` reason) gets the direct gain, otherwise the graph gain; the value is capped; `woke:` lifts it to `wake_to`; a change is recorded when `|Δ| > 1e-9`, with `new_value = round6`; one maintenance run `recall:{µs-stamp}`.
- On `legacy-v4`, tracking is off, as fixed in R2a f0c2428. The TS runtime must match this.
- **Ego guard.** Recall never touches stability; a negative test proves it.

## 6. Decay

- The algorithm is R0 §7.4, exactly as `run_decay` implements it (pinned and `anchor` skipped; `since = max(last_run, last_accessed_at, created_at)`; `half_life = 21 × (0.5 + stability)`; `new = min(cap, v × 0.5^(days / half_life))`; recorded when `|Δ| > 1e-6`, with `round6`). The sidecar path is `<db stem>.activation.json`.
- **Tolerance (settled Q6 of R0).** `0.5 ** x` is libm `pow` in Python and V8's own `Math.pow` in TS, so they may differ in the last ulp. In the decay scenarios only:
  - `new_value`, `accessibility` and the telemetry derived from it compare with |Δ| ≤ 1e-6;
  - every discrete outcome compares exactly: which records are adjusted, the count, the states histogram, `run_id`, and the sidecar bytes.
- **Boundary fixtures** are required: a value whose |Δ| sits just above the 1e-6 threshold, and a `round6` half-way case. The generator asserts that each fixture keeps a margin of at least 1e-9 from the decision boundary, so an ulp difference in `pow` cannot flip a discrete outcome. Any fixture that fails the margin check is rejected at generation time.

## 7. Legacy-v4 is not writable, on every write path (oracle patches P4, P5)

R2a Q1 says `legacy-v4` is readable and not writable. The R2b audit of the oracle found two write paths that break this.

- **P4 — decay writes its sidecar on a v4 store.** Reproduced on a copy of `spec/golden/identity-open`: `decay()` with no adjustments returns successfully and creates `store.activation.json` next to the v4 store. With adjustments it raises `MigrationRequired`, but only **after** the sidecar logic has run. Fix, in both runtimes: `decay` checks writability first and raises `MigrationRequired("schema v4 store must be migrated to v5 before writing")` before reading or writing the sidecar. The test copies a v4 store, calls `decay` with both a "no adjustment" and an "adjustment" moment, and asserts the error, identical bytes and an unchanged directory listing.
- **P5 — every compound writer checks writability before its first side effect.** That covers `log_phase`, `log_fact`, `close_loop` and `bootstrap`. Today the first kernel write raises. The patch only makes the refusal happen before any partial work. The test: each writer on a v4 copy raises `MigrationRequired`, with bytes and listing unchanged.

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
  3. `log_phase` killed after the submit commit and before the first relation → reopen shows the record without links; an identical retry then yields current rows, active relations and cues semantically equal to an uninterrupted run (ignoring timestamps and clock-derived ids).

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
- **Intake** (with the default policy, if Q1 = full): each hold reason; a reused key with a different payload (refused); a semantic no-op.
- **Kernel:**
  - relation no-op and weight bounds;
  - `from == to`;
  - `record_access` gain bounds;
  - maintenance: `old_value` mismatch, clamp, skip within 1e-9, and a semantic-hash drift abort;
  - a cue upsert changing the weight.
- **Recall tracking:** direct vs graph gain, the cap, `woke:` to `wake_to`, pinned skipped, stability untouched, and two recalls with distinct µs run ids.
- **Decay:** first run (no sidecar); an incremental second run; a pinned or anchor record skipped; the cap enforced; the boundary fixtures of §6; a `now` with and without microseconds (the `isoformat` rule).
- **Legacy-v4:** P4 and P5 on a copy of a v4 store.
- **Migration** (if Q2 = yes): v2 → v5, v3 → v5 and v4 → v5 on copies, with the source byte-identical, the backup byte-identical, `--dry-run` making no file, and a foreign or future store refused.

## 12. Known gaps added (frozen now, fixed after R3, in both runtimes)

| # | Gap | Fix after R3 |
|---|---|---|
| G9 | The decay sidecar is written after the maintenance commit, outside the transaction. A crash in between leaves `last_decay_at` stale, so the next run decays the same interval twice. | derive `last_decay_at` from the latest committed `decay:` maintenance operation (in the DB); retire the sidecar |
| G10 | `log_phase` / `log_fact` are multiple transactions; they converge only under an identical retry | one `BEGIN IMMEDIATE` per compound write (together with G2) |
| G11 | The maintenance key is `maintenance:{run_id}`, and a replay returns the prior result without comparing (G1). Two recall runs with the same µs stamp silently drop the second run's adjustments. | the run id carries a per-run nonce, or the replay compares the adjustments (G1 fix) |

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

## 14. Questions for Lam

- **Q1 — Governance scope.** Port the whole `ValidatedIntake` and `GovernancePolicy` (default thresholds, hold paths, protected domains), not only the self-authored path the identity runtime reaches?
  - Proposal: **full port.** It is small (about 400 lines), it is frozen law in R0 §7.2, and the hold and idempotency-refusal paths are where divergence would hide. The corpus drives the default policy directly through intake.
- **Q2 — Migration in R2b.** Port `migrate_to` (v2, v3, v4 → v5) now, so that R3 can rehearse the migration with the TS writer on a copy of a real store?
  - Proposal: **yes.** Without it, R4 cannot retire the Python writer. The v4 → v5 path already exists in TS from R2a.
- **Q3 — TS microsecond clock.** A strictly increasing µs clock (ms wall time plus hrtime, plus 1 µs on a tie) in production, with G11 frozen until after R3?
  - Proposal: **yes.** It makes TS no worse than Python, and the real fix belongs with G1.
- **Q4 — P4 and P5 now.** They restore the settled R2a Q1 contract (v4 is not writable) and change no v5 result.
  - Proposal: **patch both runtimes in R2b.**
- **Q5 — G10 atomicity.** Freeze, prove convergence under an identical retry, and fix after R3 together with G2?
  - Proposal: **freeze.** Making it atomic changes operation boundaries and dumps; that is a meaning change and belongs to the post-parity gap round.
- **Q6 — `migrate_aml.py`.** Keep the AML → identity import as Python tooling until R4 decides whether it ships at all?
  - Proposal: **yes.** It is a one-shot import, not part of the product's write law.
