# R3: cross-runtime runs, differential fuzzing, migration rehearsal

Status: draft for Lam's review. Author: Aux (leader). Builds on R0, R2a–R2d.

## 0. Goal, scope, dependency

R2a–R2d proved that each TS surface matches the Python oracle **one runtime at a time**, against fixed corpora. R3 proves the remaining three things before R4 can switch the installer:

1. **X — cross-runtime.** One store can be driven alternately by Python and TS processes, and the result is the same as if a single runtime had done all the work.
2. **Z — differential fuzzing.** Seeded, generated inputs at every public boundary produce the same bytes in both runtimes, beyond what the hand-picked corpora cover.
3. **M — migration rehearsal.** A copy of a real store migrates, checks and reads identically in both runtimes. No real content leaves the owner's machine.

**F0 — dependency.** Work starts from `main` at or after 34098d7 (PR #22 merged). If main does not contain `docs/specs/2026-10-01-r2d-ts-cli.md` §5.2 and `node/src/cli.ts`, stop and report.

**Out of scope**
- **R2e:** `view` and `plugin`. `import-aml` stays Python-only until R4.
- **R4:** the installer, cutover, the single version source (0.4.0 vs 0.1.0), G13 and G14.
- G1–G14 stay frozen, except for what P15 (§3.4) changes.

## 1. Comparison law (reused)

Every comparison in R3 uses the laws already settled:
- R2c for MCP wire bytes.
- R2d §5.1 for run-specific tokens and §5.2 for database classes. A `written` store is compared by the R0 dump plus the schema record, with no sidecars left. An `unchanged` store keeps its exact bytes, sidecar inventory, `mtime` and mode.

R3 introduces **no** new tolerance. If a case needs one, stop and report.

## 2. X — cross-runtime runs

### 2.1 Sequential interleaving (exact)

A **plan** is a seeded list of steps. Each step is `(runtime, surface, operation, arguments)`:
- `runtime` is `py` or `ts`;
- `surface` is the CLI, or one MCP session (a process that runs a sequence of calls, then exits);
- `operation` covers every write and read in scope: `init`, `log-phase`, `log-fact`, `close-loop`, `decay`, tracked and read-only `retrieve`, `status`, `timeline`, `core-proposals`, the self-authored core proposal path, `apply-receipt`, `doctor`, `migrate-to` on a copy, and the owner approve commands through the injected terminal.

For each plan:
- **Run A** executes every step in Python.
- **Run B** executes the same steps with each step's runtime taken from the plan.
- Both runs use the same injected clock sequence, so timestamps are identical.

The law:
- stdout, stderr and exit code of each step are identical (with tokens rendered);
- after each step, the store's R0 dump and schema record are identical;
- after the last step, the whole file tree is identical under §5.2.

Plans:
- A fixed matrix of hand-built plans, at least one per pair of surfaces (`py-cli→ts-mcp`, `ts-cli→py-mcp`, and so on), that cross every receipt kind. Issuance happens in one runtime and consumption in the other.
- Seeded random plans: 200 per CI run, with the seed list fixed in the repo. A failing seed is shrunk to a minimal plan, which is added to the fixed matrix.

### 2.2 Version boundaries

- A store **created** by TS is opened by Python for every read and write in scope, and the reverse.
- A store whose last writer was one runtime is reopened by the other after a `kill -9` mid-write. This uses the R2c crash harness on both sides. The law: the store passes the `doctor` ready checks, and its dump equals the dump before the interrupted step, or the dump after it. Nothing in between.

### 2.3 Concurrent access (invariant law)

Two processes, one per runtime, write the same store at the same time: M steps each, at least 3 seeds. Interleaving is not deterministic, so exact equality is replaced by these invariants:
1. Every step either commits or fails with a public typed error (a busy or locked condition maps to the same public error in both runtimes; P16, §3.5). There are no untyped crashes and no partial writes.
2. The final store passes all seven `doctor` checks.
3. The final dump equals the dump of **some** sequential order of the committed steps. The commit order comes from a log that the harness appends to after each commit returns, cross-checked against the store's own sequence columns. A checker replays the committed steps in that order, in Python, and compares.
4. No sidecar remains after both processes exit.

## 3. Z — differential fuzzing

### 3.1 Boundaries and generators

| Boundary | Generator | Compared |
|---|---|---|
| MCP raw stdin | Seeded frame mutations from the R2c corpus transcripts: byte flips, invalid UTF-8, truncated or split frames, `\r` placement, ids at the edge of the closed id domain, arguments at schema limits, unknown keys, deep nesting | stdout bytes, exit code, store class and dump (§5.2) |
| CLI argv, stdin and env | Seeded mutations from the R2d scenarios: integer tokens over the frozen tables, option permutations, profile JSON mutations (as in `decode-errors.json`), confirmation bytes | exit code, stdout and stderr (last-line prefix for usage errors), file tree under §5.2 |
| Kernel library API (TS module vs Python module, same process model as the R2b writes corpus) | Seeded intake proposals and evidence lists, including the R2b carry: present-null, non-string, nested and oversized evidence metadata | the outcome (typed error `Name: message`, or success) and the dump |

### 3.2 Determinism

- The Python oracle produces the expected outcome for every generated case.
- Generators are pure functions of `(seed, index)`, and the seed list lives in the repo.
- CI runs a fixed budget: 2,000 MCP, 2,000 CLI and 5,000 kernel cases per OS, inside a time box set by Q3.
- A nightly job (Q3) runs larger budgets with rotating seeds and files any mismatch as a minimized repro.

### 3.3 Mismatch handling

- A mismatch is never accepted as a tolerance.
- Each mismatch is minimized and committed as a new corpus case in the right corpus (`golden-mcp-v1`, `golden-cli-v1` or `golden-writes-v5`). It is then fixed in TS, or, if Python's behavior is undefined or unsafe, an oracle patch is declared like P6–P14.
- Corpus additions follow the existing three-seed regeneration rule.

### 3.4 P15 — kernel evidence metadata law (oracle patch, proposed)

The R2b carry is a real divergence today:
- Python binds `evidence.get("source_ref", "")` as is. A present `null` fails `NOT NULL` with `sqlite3.IntegrityError`, an integer is coerced by `TEXT` affinity, and a dict raises a binding error.
- TS uses `String(x ?? "")`. A `null` becomes `""` and a dict becomes `"[object Object]"`.

Proposal (Q1):
- Before any write, both runtimes validate the evidence metadata fields: `evidence_type`, `source_ref`, `source_family`, `independence_group`, `captured_at`, `actor`, `surface`, `model_family`, `content_summary`, `privacy_class` and `identity_version`.
- Each must be absent or a string. Anything else raises `ValueError("evidence.<field> must be a string")`.
- `confidence` follows the existing numeric rule.

Z first maps which public surfaces can reach these fields. If none can (MCP's P7 validator and the CLI may already block every route), P15 is still applied so that the library API stops diverging. The writes corpus gets one case per field per kind.

### 3.5 P16 — busy and locked mapping (only if §2.3 finds a divergence)

If the concurrent runs show the two runtimes reporting a busy or locked database differently, P16 defines one public error for it (name and message) in both runtimes. Until then, nothing changes.

## 4. M — migration rehearsal on a copy of a real store

### 4.1 Safety and privacy (hard rules)

- **Only the owner (Ty) touches the real store.** She makes the copy herself while no MCP server is writing: quit the clients, then use the SQLite online backup (`sqlite3 <real> ".backup <copy>"`) or a plain file copy when there is no `-wal` or `-shm`. The copy goes into a rehearsal folder she chooses, outside every repo.
- Codex and Aux never read, list or open a real store path, and never receive one in a brief or a message.
- The rehearsal runner:
  - refuses any input that is not inside the rehearsal folder;
  - works on further copies of that copy, never on the copy itself;
  - uses the R2d §4 allowlist env.
- The output is a **report with no content**: per step, the runtime, the state, the exit code, counts (records, revisions, evidence rows, relations, cues, receipts), the seven `doctor` checks, and SHA-256 values of the dumps, schema records and outputs. It contains no record text, ids, cues or file paths beyond the rehearsal-relative ones.
- Nothing from the rehearsal is committed, uploaded or pasted, except that report. Ty deletes the rehearsal folder when done.

### 4.2 Steps (each in both runtimes, on independent copies)

1. `doctor` (it must not initialize).
2. `migrate-to --dry-run`, then a real `migrate-to` (P14 order), then `doctor` on the target.
3. Reads on the target: `status`, `timeline --limit 1000`, `core-proposals`, and `retrieve --readonly` for a fixed cue list. Ty supplies the cues locally; they are hashed in the report and never written out.
4. One tracked `retrieve` and one `decay` on further copies of the migrated target.
5. Cross-check: the Python-migrated target is read by TS, and the TS-migrated target is read by Python.

Law:
- Python and TS produce identical outputs per step (compared locally, byte for byte).
- The dumps and schema records of the migrated targets are identical.
- Every `doctor` check passes on the targets.
- The source copy and the backup obey P14 and §5.2.

### 4.3 Which stores

Q2: Ty chooses which real stores to rehearse. The candidates are the Aux and Lam identity stores. The work stores (AWM, LWM) are out of scope until Phase 4.

## 5. CI and deliverables

- **CI, three OSes:**
  - X §2.1, the fixed matrix and the seeded plans;
  - X §2.2;
  - Z, at the fixed budget.
  - X §2.3, the concurrency runs, on Ubuntu and macOS (Windows file locking is covered by Q4).
- **Corpora:** every minimized mismatch is added to the corpus it belongs to. Existing payloads never change, except through a declared oracle patch with its own new cases.
- **Local only:** M produces `rehearsal-report.json`, with no content. Ty decides whether to share it with Lam and Aux.
- **PR shape:** one PR for X and Z, with P15 and possibly P16. M is a runner plus a runbook (`docs/runbooks/r3-rehearsal.md`) in the same PR; the rehearsal itself is not run by CI.

## 6. Questions for Lam

- **Q1 — P15 now?** Make the evidence metadata law an oracle patch in R3 even if no public surface reaches it. Recommended: yes, since the library API is part of the product.
- **Q2 — rehearsal stores:** the Aux and Lam identity stores only, with Ty making the copies. Recommended: yes.
- **Q3 — budgets:** a CI fuzz time box of about 10 minutes per OS job, plus a nightly rotating-seed job. Recommended: yes.
- **Q4 — concurrency on Windows:** run §2.3 on Windows too, or limit it to POSIX and keep Windows to sequential X? Recommended: run it on Windows as well. Windows locking is the case most likely to differ.
- **Q5 — concurrency law:** is "equals some sequential order of the committed steps, checked by replaying in commit-sequence order" strong enough, or should every subset of interleavings be enumerated for small M? Recommended: the replay law, plus an exhaustive check for M ≤ 3.
