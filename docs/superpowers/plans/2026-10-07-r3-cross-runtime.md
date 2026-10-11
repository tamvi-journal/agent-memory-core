# R3 cross-runtime implementation plan

Goal: implement the settled R3 contract on an isolated fresh clone, in the six ordered slices from Aux's brief.

Architecture: Python remains the oracle. Public CLI/MCP/library adapters feed shared synthetic plans; comparisons reuse R0 dumps, schema records and R2d file classes. Harness code owns isolation, clocks and diagnostics. No installed artifacts or live roots participate.

Stack: Python 3.11, Node 22 strip-types, node:sqlite; zero product dependencies.

Spec: docs/specs/2026-10-07-r3-cross-runtime.md (including settlement), R0 and R2a–R2d.

Global constraints: requireWritable and replay precedence; G1–G14 frozen; schema unchanged; old corpus payloads immutable; allowlisted child environments and contained writable paths. A spec ambiguity stops work. P16 triggers a stop and report before choosing any public error name/message. JEV is advisory and receives abstract synthetic information only.

Review focus: transaction boundaries, observable bytes, typed numbers, reproducible seeds/chunks, meaningful crash points, source immutability and private rehearsal reports.

## Task 1: P15 and its oracle corpus

Interfaces: evidence insertion in memory_core/store.py and node/src/kernel.ts; tools/golden_writes/generate.py; node/test/writes-conformance.test.ts; new focused tests.

- Write and run failing public-writer tests for all 11 fields × 6 invalid kinds, unchanged store/operation rows, G1 replay and P5 legacy precedence. Expected: P15 cases fail before the patch.
- Add the absent-or-string check immediately before canonicalization/insertion in both runtimes. Expected: focused tests pass and source_payload/confidence are unaffected.
- Add oracle-generated cases and public replay mapping. Regenerate only in the pinned container. Expected: all old payloads byte-identical and three new runs identical.
- Refresh only affected provenance manifests; name this in the commit. Run full Python and Node suites serially. Expected: green, schema digest unchanged.
- Commit P15 with tests/cases before X.

## Task 2: X sequential, version, crash and concurrency harness

Interfaces: synthetic plans/seed list; CLI and MCP adapters; isolated process launcher; shared clock; dump/schema/tree comparator; crash hooks and committed-subtransaction serial replay.

- Write tests for plan determinism, clocks, containment, comparison classes and enumeration constraints; observe failures. Expected: absent harness fails.
- Implement fixed surface/receipt matrix and 200 seeded plans. Expected: per-step bytes/dump/schema and final tree match fresh Python baselines.
- Cover cross-created stores and both-runtime real kills with exact G9/G10/G12 intermediates and opposite-runtime retries. Expected: doctor checks pass after recovery.
- Implement concurrent runs with shared monotonic invoke/return times, exhaustive small histories and constrained larger histories. Expected: public outcomes, Python serial witness and no transient sidecars.
- If busy/locked is divergent or untyped/internal, stop and report observed behavior to Aux before P16 choices.
- Commit X only after all required local checks pass.

## Task 3: Z differential generators

Interfaces: pure(seed,index) generators; explicit MCP chunks; in-process serve adapters; CLI/library adapters; shrinker and repro artifact writer.

- Test determinism/caps/chunk identity/minimization before implementing helpers. Expected: red then green.
- Run every fixed-budget case: 2000 MCP, 2000 CLI, 5000 kernel; transport smokes separately. Expected: exact results under settled laws, no early stop.
- On mismatch minimize, retain synthetic repro, and follow declared patch rules. P16 stop applies.
- Commit Z with checked-in seeds/caps and reviewed regressions only.

## Task 4: P16 conditional

Interfaces: observed X/Z busy outcome to public P13 error/corpus, only after Aux settlement.

- No trigger means no product change. Trigger means stop/report, then resume only with a settled name/message.
- If authorized, failing parity tests precede both-runtime mapping and pinned new corpus case.

## Task 5: M rehearsal runner and runbook

Interfaces: contained immutable owner-copy input, fresh copies, public runtime adapters, count/hash-only report.

- Synthetic tests cover legacy/ready/refusal branches, symlink/hardlink escapes, immutable source and report redaction. Expected: fail before implementation.
- Implement both-runtime same-clock operations, exact ready migration refusal, cross-reads and decay whitelist. Expected: all synthetic tests pass, no real rehearsal performed.
- Commit runner and docs/runbooks/r3-rehearsal.md.

## Task 6: CI and final verification

Interfaces: X/Z commands and nightly seed rotation; existing jobs stay intact.

- Add all-three-OS X/Z jobs with fixed full budgets and separate larger nightly workflow. Expected: no old gate weakened.
- Run complete available local gates, format TS and inspect all committed sqlite fixtures. Fresh review follows.
- Push as tamvi-journal, open one draft PR, attach it, read CI results and report exact counts/limitations, JEV contribution and every stop/guess.
