# R3: cross-runtime runs, differential fuzzing, migration rehearsal

Status: settled by Lam (APPROVED @ 392365f; Q1–Q5, F1–F5, R1–R2 applied; §6). Author: Aux (leader). Builds on R0 and R2a–R2d.

## 0. Goal, scope, dependency

R2a–R2d proved that each TS surface matches the Python oracle **one runtime at a time**, against fixed corpora. Before R4 can switch the installer, R3 proves three more things:

1. **X, cross-runtime:** Python and TS processes can drive one store alternately, and the result is the same as if a single runtime had done all the work.
2. **Z, differential fuzzing:** seeded, generated inputs at every public boundary produce the same bytes in both runtimes, beyond what the hand-picked corpora cover.
3. **M, migration rehearsal:** a copy of a real store migrates, checks and reads identically in both runtimes, and no real content leaves the owner's machine.

**F0, dependency.** Work starts from `main` at or after 34098d7 (PR #22 merged). If `main` does not contain `docs/specs/2026-10-01-r2d-ts-cli.md` §5.2 and `node/src/cli.ts`, stop and report.

**Out of scope**
- **R2e:** `view` and `plugin`. `import-aml` stays Python-only until R4.
- **R4:** the installer, the cutover, the single version source (0.4.0 versus 0.1.0), G13 and G14.
- **Frozen gaps:** G1–G14 stay frozen. P15 (§3.4) does not change them, as its regression test proves.

## 1. Comparison law (reused)

Every comparison reuses a settled law:
- R2c for MCP wire bytes.
- R2d §5.1 for run-specific tokens.
- R2d §5.2 for database classes:
  - a `written` store is compared by its R0 dump plus its schema record, with no transient SQLite sidecar (`-wal`, `-shm`, `-journal`) left behind;
  - an `unchanged` store keeps its exact bytes, its sidecar inventory, its `mtime` and its mode.

The only tolerance carried into R3 is the settled R2b §6 **decay whitelist**. It covers the maintenance adjustment `new_value` and the resulting `accessibility` of the adjusted records, compared record by record with |Δ| ≤ 1e-6. Everything else is exact.

It applies wherever a run includes `decay`: in X, in Z and in M. Any generated plan or case that contains `decay` must pass the R2b margin check, which the Python oracle runs when the case is generated. A case whose decay value lies within 1e-9 of a decision boundary (the 1e-6 threshold or a `round6` half-way point) is rejected and regenerated.

R3 adds **no** other tolerance. A case that seems to need one is a stop-and-report.

Every independent run starts from a **fresh, identical fixture**. Mutable stores are never reused between a baseline run and a mixed run. Runs may share exactly the same injected clock values.

## 2. X: cross-runtime runs

### 2.1 Sequential interleaving (exact)

A **plan** is a seeded list of steps. Each step is `(runtime, surface, operation, arguments)`:
- `runtime` is `py` or `ts`.
- `surface` is the CLI, or one MCP session (a process that runs a sequence of calls, then exits).
- `operation` covers every write and read in scope:
  - `init`, `log-phase`, `log-fact`, `close-loop`, `decay`;
  - tracked and read-only `retrieve`, `status`, `timeline`, `core-proposals`;
  - the self-authored core proposal path, `apply-receipt`, `doctor`, and `migrate-to` on a copy;
  - the owner approve commands, through the injected terminal.

For each plan there are two runs:
- **Run A** executes every step in Python, starting from fixture F.
- **Run B** starts from a fresh copy of the same F and executes the same steps, each in the runtime the plan names. It uses the same injected clock values.

The law:
- Each step has identical stdout, stderr and exit code (tokens rendered).
- After each step, the R0 dump and the schema record are identical.
- After the last step, the whole file tree is identical under §5.2.

The plans come from two sources:
- **A fixed matrix of hand-built plans.**
  - There is at least one plan per pair of surfaces (`py-cli→ts-mcp`, `ts-cli→py-mcp`, and so on).
  - Together the plans cover every receipt kind, each issued in one runtime and consumed in the other.
- **Seeded random plans.**
  - Each CI run executes 200 of them, from a seed list fixed in the repo.
  - A failing seed is shrunk to a minimal plan, which is added to the fixed matrix as a reviewed source change.

### 2.2 Version boundaries and crashes (F1)

- A store **created** by TS is opened by Python for every read and write in scope, and the reverse.
- **`kill -9` mid-write.** The R2c crash harness is used on both sides: the process is killed at each instrumented point, then the store is reopened and the operation retried from the **other** runtime. The law depends on the crash class:

| Crash class | Law after the kill |
|---|---|
| Single-transaction kernel or authority write (intake submit, revise, relation event, receipt issue or consume) | The dump equals the state before the step or the state after it. Nothing in between. |
| **G9** `decay` | The maintenance DB commit may exist while the activation sidecar is still stale or missing. This is the exact intermediate state frozen in R2b. |
| **G10** `log_phase` / `log_fact` | The submit may be committed while some relations or cues are not. This is the exact intermediate state frozen in R2b. |
| **G12** tracked `retrieve` | One or more `record_access` commits may exist while the remaining accesses and `apply_recall` do not. This is the exact intermediate state frozen in R2b. |

- **G9, G10 and G12.** These are the crash law already frozen in R2b, not a new tolerance. For each of these classes, X asserts the exact intermediate state R2b defined. It then retries from the other runtime and proves the R2b semantics, including the non-converging retry of G12.
- **Every class:** the store passes the `doctor` ready checks after the kill and after the retry.

### 2.3 Concurrent access (Q4, Q5)

Two processes, one per runtime, write the same store at the same time. Each runs M steps, with at least 3 seeds. This runs on Ubuntu, macOS **and Windows**.

**`decay` is excluded from concurrent plans.** A concurrent `decay` has two independent linearization points: the DB maintenance transaction and the later activation-sidecar write. Two processes can commit the DB in one order and overwrite the sidecar in the other order. Modeling that would need a separate sidecar-history law, and that is the G9 atomicity problem, which stays frozen beyond R3. `decay` remains covered by sequential X (§2.1), crash X (§2.2), Z and M.

Each operation is recorded with two times on a shared monotonic clock:
- `invoke_ts`, taken just before the operation is issued;
- `return_ts`, taken just after it returns.

The law has four parts:
1. **Every step commits or fails with a public typed error.** Busy and locked conditions follow P16 (§3.5).
2. **The final store passes** all seven `doctor` checks.
3. **Serializability at transaction boundaries.**
   - The serial unit is a **committed transaction**, not a public operation. Compound operations are split into their committed sub-transactions, in the order the frozen R2b behavior defines:
     - G10 `log_phase` and `log_fact`: the submit, then each relation or cue commit;
     - G12 tracked `retrieve`: each `record_access` commit, then `apply_recall`;
   - Another process's transaction may fall between two sub-transactions of one operation.
   - A candidate serial order must respect each process's program order and the real-time happens-before relation: if op a returned before op b was invoked, a comes before b.
   - For **M ≤ 3 operations per process**, every serial order of their sub-transactions is enumerated (sub-transaction order within each operation kept). The final dump must equal the dump of at least one of them, each replayed from a fresh fixture in Python.
   - For **M > 3**, a search finds **one** order that satisfies those constraints and the store-local ordering evidence (the per-record lifecycle and relation sequence numbers, and the receipt states). That order is replayed in Python, and the final dump must match.
   - A log the harness writes after each operation returns is **diagnostic only**. It is never the proof of commit order.
4. **No transient SQLite sidecar** (`-wal`, `-shm`, `-journal`) remains after both processes exit. The activation sidecar (`<db stem>.activation.json`) cannot appear here, because concurrent plans contain no `decay`.

## 3. Z: differential fuzzing

### 3.1 Boundaries and generators

| Boundary | Generator | Compared |
|---|---|---|
| MCP framing and dispatch (in-process serve harness, both runtimes) | Seeded mutations of the R2c corpus transcripts: byte flips, invalid UTF-8, `\r` placement, ids at the edge of the closed id domain, arguments at schema limits, unknown keys, nesting. **Each case carries an explicit `chunks: [bytes…]` partition** that is fed exactly to both harnesses (F3). | Output bytes, store class and dump (§5.2) |
| MCP real subprocess stdin | A fixed set of transport smokes (split writes, a final frame at EOF). These are kept separate from the chunk-partitioned fuzz. | stdout bytes, exit code |
| CLI argv, stdin and env | Seeded mutations of the R2d scenarios: integer tokens over the frozen tables, option permutations, profile JSON mutations, confirmation bytes | Exit code, stdout and stderr (last-line prefix for usage errors), and the file tree under §5.2 |
| Kernel library API (TS module versus Python module, the same process model as the R2b writes corpus) | Seeded intake proposals and evidence lists, including the R2b carry: present-null, non-string, nested and oversized evidence metadata | Outcome (`Name: message` or success) and dump |

**Generation caps (F3).** Every generator enforces hard caps, recorded in the corpus metadata:
- MCP: at most 64 KiB per case, nesting depth at most 32, and at most 16 frames per case.
- CLI: at most 8 KiB of argv plus stdin per case.
- Kernel: at most 64 KiB per proposal and at most 32 evidence items.
- Every boundary has a fixed number of cases per seed.

Within these caps, Z stays a differential protocol test. It does not turn into stack or memory stress outside the contract.

### 3.2 Determinism and budgets (Q3)

- The Python oracle produces the expected outcome for every generated case.
- Generators are pure functions of `(seed, index)`.
- **Fixed CI budgets are the contract:** per OS, 2,000 MCP cases, 2,000 CLI cases and 5,000 kernel cases. Every case must finish. The job timeout (about 10 minutes) is only a failure bound and never means stopping early.
- **Nightly run.** It uses larger budgets and rotating seeds. Each seed comes from a checked-in seed ring, or is derived from the UTC date with a fixed salt. The actual seeds are always written to the job log and the artifact.

### 3.3 Mismatch handling (zero tolerance)

The path for every mismatch is:

> mismatch → deterministic minimization → reviewable repro artifact → corpus regression → fix

- **The fix.** It goes into TS, or into a declared oracle patch if Python's behavior is undefined or unsafe.
- **Corpus additions.** These are reviewed source changes that follow the existing three-seed regeneration rule. Nightly jobs and bots **never** commit a corpus case on their own.

### 3.4 P15: evidence metadata law (oracle patch, both runtimes; Q1)

**Today the two runtimes diverge.**
- Python binds `evidence.get("source_ref", "")` exactly as given:
  - a present `null` fails `NOT NULL` with `sqlite3.IntegrityError`;
  - an integer is coerced by `TEXT` affinity;
  - a dict raises a binding error.
- TS uses `String(x ?? "")`: `null` becomes `""` and a dict becomes `"[object Object]"`.

**The law.** Each of these fields must be **absent or a string**:
- `evidence_type`, `source_ref`, `source_family`, `independence_group`, `captured_at`;
- `actor`, `surface`, `model_family`, `content_summary`, `privacy_class`, `identity_version`.

A present `null`, bool, int, float, array or object raises `ValueError("evidence.<field> must be a string")`.

**Not covered by P15.**
- `source_payload` is intentionally JSON-valued and takes part in hashing.
- `confidence` keeps its existing numeric law.

**Order.**
- The P5 `requireWritable()` check stays first.
- Existing replay, no-op and idempotency semantics are unchanged.
- The check runs immediately before evidence canonicalization and insertion. It is not a new entry precondition, and it does not bypass the frozen writer-entry or replay behavior.

**Tests.**
- The writes corpus gets one case per field per invalid kind.
- One regression reuses an **existing idempotency key with new, invalid evidence metadata**. It proves that P15 neither fixes G1 nor changes replay precedence. Whatever the frozen replay path returns today, it still returns.

Z also maps which public surfaces can reach these fields. P15 applies even if only the library API can reach them.

### 3.5 P16: busy and locked (mandatory if triggered; F2)

P16 becomes mandatory if §2.3 or Z shows **either** of these:
- Python and TS report a busy or locked database differently;
- **either** runtime reports busy or locked as an untyped or internal crash, even if both runtimes fail in the same bad way.

P16 then defines a single public error (name and message) for that condition in both runtimes, adds it to the P13 public set, and pins it with a corpus case. If neither trigger appears, nothing changes.

**Triggered (Codex @ fa21042).** A Python MCP `identity_log_phase` call, made while a TS writer held an open transaction, returned `RuntimeError: internal error` after about 5.28 s. Its stderr ended with `sqlite3.OperationalError: database is locked`. That is the untyped trigger. P16 is therefore settled as follows (Lam's ack needed):

- **Name and message.** `StoreBusy`, with the fixed message `store is busy; retry later`. The message never contains a path, a SQL statement or the SQLite text.
  - Python: `class StoreBusy(RuntimeError)` in `memory_core.store`.
  - TS: `class StoreBusy extends Error` with the same name.
- **What maps to it.** Exactly these conditions, and nothing else:
  - SQLite primary result code `SQLITE_BUSY` (5) or `SQLITE_LOCKED` (6), extended codes included (`code & 0xff`), raised on open, `BEGIN`/`BEGIN IMMEDIATE`, any statement, or `COMMIT`.
  - Python reads `sqlite3.Error.sqlite_errorcode` (3.11+). On 3.10, where that attribute is missing, an `OperationalError` maps only when its message is exactly `database is locked`, `database table is locked` or `database schema is locked`.
  - TS reads the `errcode` that `node:sqlite` reports.
  - Every other SQLite error keeps its current behavior.
- **Busy timeout.** Both runtimes wait exactly **5000 ms** before raising. This is the Python default `sqlite3.connect(timeout=5.0)`, and TS already passes `timeout: 5000`. Neither runtime retries on its own beyond that.
- **State on failure.** The failing transaction is rolled back, so nothing it wrote remains.
  - A single-transaction write leaves the store exactly as it was before the step.
  - A compound operation (G10, G12) can fail between sub-transactions. It then leaves exactly the frozen R2b intermediate state for that point. This is the same law as the §2.2 crash classes, and P16 does not change it.
- **Surfaces.**
  - MCP: added to the R2c §5.4 public names in both runtimes, so the tool result is `{"type":"text","text":"StoreBusy: store is busy; retry later"}` with `isError: true`. The JSON-RPC envelope is unchanged and the process keeps serving.
  - CLI: added to the P13 public set, so stderr is `StoreBusy: store is busy; retry later` with exit 1.
- **Tests.**
  - One deterministic corpus case per surface (MCP in `golden-mcp-v1`, CLI in `golden-cli-v1`), built the way Codex reproduced it. A process pauses inside a write transaction at an existing test seam; no manual SQL lock is taken and no timeout is overridden. The holder runs in the other runtime where the harness allows it.
  - The §2.3 concurrency law then accepts `StoreBusy` as the only public busy outcome.
  - The expected wait (about 5 s) is checked as `≥ 4.5 s`. This is a smoke bound, not a byte contract.

## 4. M: migration rehearsal on a copy of a real store

### 4.1 Safety and privacy (hard rules; F4, F5)

**The owner's copy.** Only the owner (Ty) touches a real store.
- She makes each copy herself, while no MCP server is writing. She quits the clients, then runs `sqlite3 <real> ".backup <copy>"`, or does a plain file copy when there is no `-wal` or `-shm`.
- The copy goes into a rehearsal folder she chooses, outside every repo. It is **immutable input** to the runner.
- Codex and Aux never read, list or open a real store path, and never receive one in a brief or a message.

**The runner's containment** is at least as strict as R2d:
- Every input, further copy, target and backup is resolved with `realpath` and must be contained in the selected rehearsal root. Symlink escapes are rejected.
- The source copy must be a regular file, with `nlink == 1` where the platform reports it.
- The source copy is hashed and `stat`-ed before and after the run, and must be unchanged.
- Every mutation happens only on further copies inside the rehearsal folder.
- The env is built from an explicit allowlist (R2d §4), with no inherited proxy or network configuration.

**The report.** It contains counts and hashes only:
- per step: the runtime, the state, the exit code, and counts (records, revisions, evidence rows, relations, cues, receipts);
- the seven `doctor` checks;
- SHA-256 values of dumps, schema records and outputs.

It never contains ids, titles, summaries, cue text, absolute paths, or any string derived from a record.

**Cues.** Cues are identified by `cue_index` and a count. Their hashes never appear in the report, because a low-entropy cue could be brute-forced from a plain hash. If local correlation is needed, the runner uses an ephemeral keyed HMAC whose key stays local and is never written to the report.

**After the run.** Nothing from the rehearsal is committed, uploaded or pasted, except the report. Ty deletes the rehearsal folder when she is done.

### 4.2 Steps (depend on the source state)

M is a **real-store compatibility rehearsal, plus a migration rehearsal when one applies**. Synthetic v2, v3 and v4 migrations are already covered by the corpora, so Ty never has to manufacture or downgrade a store to rehearse a migration.

**Clock.** Both runtimes get the **same injected clock sequence** (the clock injection already used by the R2b–R2d corpora), so clock-derived timestamps, access ids and decay run ids match. With wall-clock time, two correct runs would diverge.

Every step runs in both runtimes, on independent further copies.

1. **`doctor`** on a further copy of the source. It must not initialize the store. Record the state.
2. **Branch on that state.**
   - **`legacy-v2`, `legacy-v3` or `legacy-v4`:**
     1. `migrate-to --dry-run`, then a real `migrate-to` (P14 order), then `doctor` on the target.
     2. Reads on the target: `status`, `timeline --limit 1000`, `core-proposals`, and `retrieve --readonly` for a cue list Ty supplies locally.
     3. One tracked `retrieve` and one `decay`, each on a further copy of the migrated target.
     4. Cross-read: TS reads the Python-migrated target, and Python reads the TS-migrated target.
   - **`ready` (schema v5):** no migration is invented.
     1. On a further copy, pin the exact `migrate-to` refusal (`store state 'ready' is not an explicit migration source`), as a guardrail proof.
     2. Run the same reads, the tracked `retrieve` and the `decay` directly on fresh further copies of the ready source.
     3. Cross-runtime: a further copy written by one runtime (tracked `retrieve`, then `decay`) is read by the other.
   - **`unknown`, `incompatible`, or a WAL source:** stop. The report records only the state or the refusal. Nothing is mutated.

The law:
- The two runtimes produce identical outputs for each step. Bytes are compared locally, with the decay whitelist of §1.
- The stores each step writes have identical dumps and schema records.
- Every `doctor` check passes on every ready store the rehearsal produces or uses.
- On the legacy branch, the source copy and the backup obey P14 and §5.2.
- The report follows §4.1 (counts and hashes only) on every branch. Whichever branch runs, the state is the only new field.

**Acceptance.**
- If at least one of Ty's sources is legacy, M also proves a real-store migration.
- If every source is already ready (v5), M still proves real-store cross-runtime compatibility. R4 is **not** blocked just because no real legacy store remains.

### 4.3 Which stores (Q2)

Only the Aux and Lam identity stores are rehearsed, and Ty makes every source copy. The work stores (AWM, LWM) are out of R3.

## 5. CI and deliverables

- **CI on all three OSes:** X §2.1, §2.2 and §2.3, plus Z at its fixed budgets.
- **Corpora:** each reviewed, minimized mismatch is added to the corpus it belongs to. Existing payloads change only through a declared oracle patch (P15, or P16 if it is triggered) that comes with its own new cases.
- **Local only:** M produces `rehearsal-report.json` (counts and hashes, §4.1). Ty decides whether to share it.
- **PR shape:** one implementation PR covers X, Z, P15 (and P16 if triggered), the M runner, and `docs/runbooks/r3-rehearsal.md`. CI never runs the rehearsal itself.

## 6. Settlement (Lam, 2026-10-07, review @ f2ac000)

- **Q1, yes:** P15 is in scope even if only the library API reaches it. It excludes `source_payload`, keeps `confidence` numeric, keeps P5 first, runs just before canonicalization, and has a G1 replay-precedence regression.
- **Q2, yes:** the Aux and Lam identity stores only, with Ty making every source copy.
- **Q3, yes:** the fixed budgets are the contract, the time box is only a timeout, the nightly seeds are reproducible and logged, and nothing is ever committed automatically.
- **Q4, yes:** the concurrency runs include Windows.
- **Q5, changed:** commit order is proven by a serializability search under program order and real-time happens-before, exhaustive for M ≤ 3. A log written after each operation returns is diagnostic only.
- **F1:** the `kill -9` law depends on the crash class, and the frozen R2b intermediate states of G9, G10 and G12 are preserved.
- **F2:** P16 is triggered by a divergence **or** by any untyped busy or locked failure.
- **F3:** MCP fuzz cases carry explicit chunk partitions, and every generator has hard caps.
- **F4:** the rehearsal runner's containment is at least as strict as R2d, and the source copy is immutable.
- **F5:** the report contains no cue hash, only indices and counts, with an optional local HMAC.
- **Codex review @ a3c14b3 (needs Lam's ack):**
  - the concurrency law serializes committed sub-transactions (G9, G10, G12);
  - M uses one injected clock sequence for both runtimes;
  - the R2b decay whitelist and margin check apply wherever `decay` runs.
  - Lam ACKed these @ b499ef1.
- **Lam @ b499ef1:**
  - R1: `decay` is excluded from concurrent plans (G9 stays frozen), and law 4 covers only the transient SQLite sidecars.
  - R2: M branches on the source state (legacy → migrate, ready → compatibility only, otherwise stop). An all-v5 set of sources does not block R4.
