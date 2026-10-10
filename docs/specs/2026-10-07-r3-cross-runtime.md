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
| Single-transaction kernel or authority write (revise, relation event, receipt issue or consume) | The dump equals the state before the step or the state after it. Nothing in between. |
| **G2** intake `submit` (Codex @ df5b755) | `submit` is **not** one transaction (G2, frozen since R0). Its commits depend on the evaluated outcome (Codex review on #26): **materialized path** — (1) evidence capture (one transaction for all items), (2) the received-intake row, (3) kernel materialization, (4) evidence linking, only when the result has a non-null `target_revision_id` (otherwise no commit), (5) the `materialized` decision; **`held` / `rejected` / `no_op` path** — (1) evidence capture, (2) the received-intake row, (3) the decision. The boundaries are exactly the commits that run on that path; X derives them from the Python oracle and never invents a boundary for a skipped step. A kill between commits leaves exactly the state after the last committed sub-transaction, for example +1 evidence row and +1 intake row with status `received`. Both runtimes must leave the **same** intermediate dump at every boundary. |
| **G9** `decay` | The maintenance DB commit may exist while the activation sidecar is still stale or missing. This is the exact intermediate state frozen in R2b. |
| **G10** `log_phase` / `log_fact` | The submit may be committed while some relations or cues are not. This is the exact intermediate state frozen in R2b. |
| **G12** tracked `retrieve` | One or more `record_access` commits may exist while the remaining accesses and `apply_recall` do not. This is the exact intermediate state frozen in R2b. |

- **G2, G9, G10 and G12.** These are the crash law already frozen in R0 and R2b, not a new tolerance.
  - G10's first sub-transaction is the whole intake `submit`, which is itself the G2 sequence above.
  - For each of these classes, X asserts the exact intermediate state at every boundary, identical in both runtimes.
- **Retry law for every compound class.**
  - After a kill at boundary k in runtime A, the same call is retried with the same arguments in runtime B.
  - The resulting dump must equal the dump a **Python** retry produces from the same intermediate store.
  - That retry follows the frozen G1 replay and idempotency behavior, whatever it is. It is not "fixed" here. This includes G12's non-converging retry.
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
     - G2 intake `submit`: evidence capture, received intake, then either materialization, evidence linking (only with a non-null `target_revision_id`) and decision, or decision alone for `held` / `rejected` / `no_op`;
     - G10 `log_phase` and `log_fact`: the submit (itself the G2 sequence), then each relation or cue commit;
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
| CLI argv, stdin and env | Seeded mutations of the R2d scenarios: integer tokens over the frozen tables, option permutations, profile JSON mutations, confirmation bytes | Exit code, stdout and stderr (last-line `<prog>: error:` prefix for usage errors, §3.1b), and the file tree under §5.2 |
| Kernel library API (TS module versus Python module, the same process model as the R2b writes corpus) | Seeded intake proposals and evidence lists, including the R2b carry: present-null, non-string, nested and oversized evidence metadata | Outcome (`Name: message` or success) and dump |

**Generation caps (F3).** Every generator enforces hard caps, recorded in the corpus metadata:
- MCP: at most 64 KiB per case, nesting depth at most 32, and at most 16 frames per case.
- CLI: at most 8 KiB of argv plus stdin per case.
- Kernel: at most 64 KiB per proposal and at most 32 evidence items.
- Every boundary has a fixed number of cases per seed.

Within these caps, Z stays a differential protocol test. It does not turn into stack or memory stress outside the contract.

### 3.1b Usage-error prefix (Codex @ 6782110; amends R2d §3.1)

**Finding.** `timeline --limit 1.0` (also `retrieve --limit 1.0` and `timeline --limit 1e2`) exits 2 in both runtimes with an unchanged tree. But the last stderr lines differ:
- Python: `trajecta-identity timeline: error: …`
- TS: `trajecta-identity: error: …`

R2d §3.1 froze the literal prefix `trajecta-identity: error:`. Python never met it for errors raised by a subcommand parser. The cli-v1 generator hid this, because it **wrote the constant** for every exit-2 case instead of checking Python's output (`tools/golden_cli/generate.py`, usage mode). That is a generator defect, not an oracle behavior.

**Law (no oracle patch; Python stays as it is).**
- The frozen part of a usage error is the argparse prefix `<prog>: error:` of the parser that raised it:
  - `trajecta-identity: error:` for the root parser (no command, unknown command, unrecognized arguments);
  - `trajecta-identity <command>: error:` for a command's own parser (a missing or invalid option, a mutually exclusive group, and so on). There is one level of commands, so there is no deeper prog.
- TS must emit the **same** prefix as Python. That means it must raise from the same parser, which is a grammar property. The text after `error:` stays unfrozen.
- Exit 2, and usage writes nothing, as before.

**Corpus repair (declared old-corpus impact).**
- The cli-v1 generator extracts Python's actual last non-empty stderr line and asserts that it starts with `<prog>: error:` for a known prog. It stores exactly that prefix, never a constant.
- The only bytes that may change are the stored `stderr` of exit-2 cases raised by a command parser (for example `usage-04`, `usage-05`, `integer-04`/`05`/`06`/`10`/`11`), plus the MANIFEST provenance. Every other byte stays identical. The commit lists each changed case.
- The TS replay compares the stored prefix exactly. If TS emits the wrong prefix today, fix TS; never relax the comparator.
- Z's CLI boundary uses the same law.

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

**Triggered (Codex @ fa21042).** A Python MCP `identity_log_phase` call, made while a TS writer held an open transaction, returned `RuntimeError: internal error` after about 5.28 s. Its stderr ended with `sqlite3.OperationalError: database is locked`. That is the untyped trigger. P16 is therefore settled as follows (Lam ACKed it in substance @ a86799b, with this timing correction):

- **Name and message.** `StoreBusy`, with the fixed message `store is busy; retry later`. The message never contains a path, a SQL statement or the SQLite text.
  - Python: `class StoreBusy(RuntimeError)` in `memory_core.store`.
  - TS: `class StoreBusy extends Error` with the same name.
- **What maps to it.** Exactly these conditions, and nothing else:
  - SQLite primary result code `SQLITE_BUSY` (5) or `SQLITE_LOCKED` (6), extended codes included (`code & 0xff`), raised on open, `BEGIN`/`BEGIN IMMEDIATE`, any statement, or `COMMIT`.
  - Python reads `sqlite3.Error.sqlite_errorcode` (3.11+). On 3.10, where that attribute is missing, an `OperationalError` maps only when its message is exactly `database is locked`, `database table is locked` or `database schema is locked`.
  - TS reads the `errcode` that `node:sqlite` reports.
  - Every other SQLite error keeps its current behavior.
- **Busy timeout (configuration, not semantics).**
  - Both runtimes configure a **5000 ms** SQLite busy timeout on every relevant connection: the Python default `sqlite3.connect(timeout=5.0)`, and TS `timeout: 5000`.
  - Neither runtime adds an application-level retry beyond SQLite's own busy handling.
  - Any `SQLITE_BUSY` or `SQLITE_LOCKED` result that reaches the runtime maps to `StoreBusy`, however much time has passed. SQLite may return `BUSY` without calling the busy handler (to avoid a deadlock, for example), and `LOCKED` is a different lock class.
  - **Timing is not part of the `StoreBusy` contract.**
- **State on failure.** The failing transaction is rolled back, so nothing it wrote remains.
  - A single-transaction write leaves the store exactly as it was before the step.
  - A compound operation (G10, G12) can fail between sub-transactions. It then leaves exactly the frozen R2b intermediate state for that point. This is the same law as the §2.2 crash classes, and P16 does not change it.
- **Surfaces.**
  - MCP: added to the R2c §5.4 public names in both runtimes, so the tool result is `{"type":"text","text":"StoreBusy: store is busy; retry later"}` with `isError: true`. The JSON-RPC envelope is unchanged and the process keeps serving.
  - CLI: added to the P13 public set, so stderr is `StoreBusy: store is busy; retry later` with exit 1.
- **Tests.**
  - One deterministic corpus case per surface (MCP in `golden-mcp-v1`, CLI in `golden-cli-v1`), built the way Codex reproduced it. A process pauses inside a write transaction at an existing test seam. No manual SQL lock is taken and no timeout is overridden.
  - The holder stays paused **until the victim has returned `StoreBusy`**, and only then is released. A fixed sleep is never used, because one could release the lock around 4.9 s and turn the expected error into a success.
  - **A bidirectional proof** lives in X §2.3: a Python victim against a TS holder, and a TS victim against a Python holder.
  - The §2.3 concurrency law then accepts `StoreBusy` as the only public busy outcome.
  - Only for that canonical `SQLITE_BUSY` corpus scenario, where the holder outlasts the configured timeout, the wait is smoke-checked as `≥ 4.5 s`. This bound is never asserted for `SQLITE_LOCKED` or for extended-code paths.

### 3.5b P17: sequence allocation inside the write transaction (oracle patch, both runtimes)

**Finding (Codex @ df5b755).** A deterministic public MCP schedule runs two `close-loop` calls on the same relation:
- Python reads the active relation and computes `sequence_number = 2` **before** its deferred write transaction starts.
- TS enters `BEGIN IMMEDIATE`, commits sequence 2, and returns `retracted`.
- Python resumes and fails on `UNIQUE(relation_id, sequence_number)`, which surfaces as `RuntimeError: internal error`.

Two overlapping `revise` calls hit the same race on `UNIQUE(revision_number)`. That breaks §2.3 law 1, and P16 cannot cover it, because these are constraint errors, not BUSY.

**Law.** Every read-compute-write allocation of a per-record sequence number takes place **inside one `BEGIN IMMEDIATE` transaction** that starts before the read. This covers the relation event `sequence_number`, the revision `revision_number`, and every other MAX+1 or "current state" read that decides a write. TS already behaves this way, so the patch moves Python to the same boundary.
- Codex lists every such site in both runtimes first: file, function, the read, and the write it decides. Then it patches only those sites.
- **Not changed:** the compound boundaries of G2, G9, G10 and G12, the G1 replay behavior, and the public results of sequential calls. Every existing corpus payload stays byte-identical.
- **Concurrent outcome.** The second caller waits on the lock. Within the busy timeout it then reads the committed state and returns that state's ordinary result, for example a typed "already closed" error or a valid sequence 3, exactly as a serial call would. If the wait exceeds the busy timeout, it returns `StoreBusy` (P16). A constraint error never reaches the public surface.

**Tests.** The Codex schedules become deterministic regressions in both directions (Python victim / TS holder, and TS victim / Python holder) for `close-loop` and `revise`. Each result must equal one serial order (§2.3), with no `IntegrityError` and no untyped error.

### 3.6 Backup mtime resolution (Codex @ bef36ce; amends R2d §3.7 and §5.2)

**Finding.** X found that a TS `migrate-to` backup does not reproduce the source mtime to the nanosecond. Example: a source with `mtime_ns = 1700000000123456789` gave a TS backup of `…122999000`. TS passed millisecond `Date`s to `utimesSync`. Python's `copy2` keeps every nanosecond.

**Platform limit.** Node cannot set file times to the nanosecond. `fs.utimes*` takes a double in seconds, and on Linux it lands at microsecond resolution. Exact nanosecond inheritance is therefore impossible for TS. This is a limit of the platform, not a bug to tolerate.

**Law.** "Inherits the source mtime" means
`floor(backup.mtime_ns / 1000) == floor(source.mtime_ns / 1000)`,
that is, the same whole microsecond. It applies only to the `migrate-to` backup, and atime is not compared. Python's `copy2` may keep a finer value; only the microsecond is compared.

**Common supported domain (oracle patch, both runtimes).** Backup inheritance is defined only for a source mtime with `0 ≤ mtime_ns < 2^32 · 10^9`, that is, from the Unix epoch up to just before 2106-02-07 06:28:16 UTC. Below 2^32 s a binary64 value has a spacing under 0.5 µs, which leaves a safe margin around the +500 ns midpoint used below. Above it some microseconds cannot be represented at all; at 2^33 s the spacing is about 1.9 µs.
- Both runtimes check the source mtime **before writing the backup**, at the position fixed by the order below. Outside the domain they raise the same public `ValueError("source mtime is outside the supported backup range")`. The source is untouched, and neither a backup nor a target exists.
- There is no Python/TS divergence here, and no new gap. R4 may lift the bound if Node gains a nanosecond time API.

**Setting the backup mtime.**
- Python keeps `copy2`.
- TS reads the source with `statSync(path, { bigint: true })` and sets the time to the **middle** of the source microsecond, `sec + (µs·1000 + 500) / 1e9`, so double rounding stays away from the microsecond boundary.

**Verification and cleanup (both runtimes).** Setting the time and reading it back are part of creating a *valid* backup.
- After the copy, both runtimes read the backup's mtime back and check the microsecond.
- If the time cannot be set, or the read-back does not match (for example on a filesystem whose time resolution is coarser than 1 µs), the invocation:
  - deletes the backup it just created;
  - leaves no target;
  - leaves the source law intact: unchanged bytes, mtime, mode and sidecar inventory (atime and ctime are not contract fields, since reading or copying can change them on some filesystems);
  - raises the public `ValueError("backup mtime could not be preserved to a microsecond")`, never an untyped error.

**Full refusal order (P14 order kept; Lam @ a70dea9).** Both runtimes follow it exactly:
1. Target exists → `FileExistsError(target)`.
2. Source missing → the existing missing-source branch.
3. Legacy source, and the backup equals the target → `ValueError("backup path must differ from target")`.
4. Backup exists → `FileExistsError(backup)`.
5. Source mtime outside the common domain → `ValueError("source mtime is outside the supported backup range")`.
6. Copy the backup, then set its mtime and read it back.
7. Verification fails → delete this invocation's backup (target absent, source law intact) → `ValueError("backup mtime could not be preserved to a microsecond")`.
8. P14's post-backup `target.exists()` alias check.
9. The migration block.

**Evidence and proof.** In the container, 20,000 random modern times on Linux with Node 22 all landed on the correct microsecond. That is supporting evidence only. The proof is the boundary tests and the read-back verification.

**Unaffected.** The `unchanged` class (R2d §5.2) still compares the runner's own before and after `stat` exactly. No runtime ever writes those times, so nothing is lost there.

**Tests (all three OSes for the real-precision cases).**
- Modern nanosecond source values: exactly on a microsecond boundary, then +1 ns, +499 ns, +500 ns, +999 ns, and the last nanosecond before the next microsecond.
- **Supported domain edges:** the epoch exactly (`mtime_ns = 0`) and the last supported nanosecond (`2^32·10^9 − 1`). For each, `migrate-to` proceeds through the normal legacy path:
  - the backup is byte-identical to the source;
  - its mtime matches the source to the whole microsecond;
  - no range `ValueError` is raised.
- **Unsupported domain edges:** the first unsupported value (`2^32·10^9`), and a pre-epoch value where the host can create one. For each, both runtimes:
  - raise exactly `ValueError("source mtime is outside the supported backup range")`;
  - leave the source law intact;
  - create no backup and no target.
- Verification failure: an injected fault at the time-set or read-back seam, after the backup exists. The test asserts that the source law holds, the backup is gone, the target is absent, and the public error is raised. It runs in both runtimes.
- Order composition, in both runtimes, each with an unsupported mtime:
  - with an existing target → `FileExistsError(target)`;
  - with backup equal to target → the backup-path `ValueError`;
  - with an existing backup → `FileExistsError(backup)`;
  - with every earlier gate clear → the supported-range `ValueError`.
- The R2d corpus fixtures with whole-second mtimes stay as they are.

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
- **Corpora:** each reviewed, minimized mismatch is added to the corpus it belongs to. Existing payloads change only through a declared oracle patch (P15, P16 if it is triggered, or P17) that comes with its own new cases, or through the declared generator repair of §3.1b, which is limited to the stderr bytes it names plus MANIFEST provenance.
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
- **Codex @ 6782110, settled with Lam @ 8e0ac5c:** usage-error prefix per raising parser; cli-v1 generator repair (§3.1b).
- **Codex @ df5b755 (settled, #26):**
  - the G2 intake `submit` crash class, with exact intermediates and the Python-retry law (§2.2);
  - P17, sequence allocation inside `BEGIN IMMEDIATE` (§3.5b).
- **Codex @ bef36ce, settled with Lam @ 5b6dd56:**
  - a backup inherits its source mtime to the same microsecond (§3.6);
  - a common supported domain, [epoch, 2^32 s), is a both-runtime oracle patch, so there is no G15;
  - the post-copy read-back runs in both runtimes, with cleanup and a public `ValueError`.
