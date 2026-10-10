import assert from "node:assert/strict";
import fs, { copyFileSync, mkdtempSync, readFileSync, readdirSync, statSync } from "node:fs";
import { syncBuiltinESMExports } from "node:module";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import test from "node:test";
import { InjectedClock, MemoryStore, orderedObject, pyFloat, pyInt } from "../src/index.ts";
import { MigrationRequired, ValueError } from "../src/errors.ts";
import { ValidatedIntake, DEFAULT_POLICY } from "../src/governance.ts";

const FIELDS = [
  "evidence_type",
  "source_ref",
  "source_family",
  "independence_group",
  "captured_at",
  "actor",
  "surface",
  "model_family",
  "content_summary",
  "privacy_class",
  "identity_version",
];
const INVALID = [
  ["null", null],
  ["bool", true],
  ["int", pyInt(1n)],
  ["float", pyFloat(1)],
  ["array", []],
  ["object", {}],
] as const;
function fixture(legacy = false) {
  const root = mkdtempSync(resolve(tmpdir(), "r3-evidence-"));
  const path = resolve(root, "store.sqlite3");
  if (legacy) copyFileSync(resolve(import.meta.dirname, "../../spec/golden/identity-open/store.sqlite3"), path);
  const clock = new InjectedClock();
  const store = new MemoryStore(path, { now: () => clock.seconds() });
  if (!legacy) store.initialize();
  store.close();
  return { root, path, store };
}
const create = (store: MemoryStore, evidence: Record<string, unknown>) =>
  store.createCurrent({
    recordId: "p15",
    recordClass: "belief",
    domain: "fact",
    title: "P15",
    actor: "synthetic",
    reason: "metadata contract",
    evidence,
    idempotencyKey: "p15",
  });

test("R3 relation with absent endpoints preserves Python's native FK refusal", () => {
  const { path, store } = fixture();
  const before = readFileSync(path);
  assert.throws(
    () => store.addRelation({ relationId: "missing", fromRecordId: "a", toRecordId: "b", relationType: "supports" }),
    (error: unknown) =>
      error instanceof Error &&
      error.constructor.name === "IntegrityError" &&
      error.message === "FOREIGN KEY constraint failed",
  );
  store.close();
  assert.deepEqual(readFileSync(path), before);
});

// Z found nulls erased by intake's defaults before they reached P15 insertion.
for (const field of ["actor", "surface", "model_family", "privacy_class"])
  for (const [kind, value] of INVALID) {
    test(`P15 intake preserves present ${field}/${kind} until insertion validation`, () => {
      const { path, root, store } = fixture();
      const before = readFileSync(path);
      const intake = new ValidatedIntake(store, { surface: "golden", policy: DEFAULT_POLICY });
      assert.throws(
        () =>
          intake.submit({
            operation_type: "create",
            record_id: "synthetic-r3",
            record_class: "event",
            domain: "phase",
            actor: "synthetic",
            reason: "synthetic",
            logic: "synthetic",
            truth_basis: "synthetic",
            changes: { title: "Synthetic" },
            idempotency_key: "synthetic:r3",
            evidence: [
              { source_ref: "synthetic:r3", content_summary: "synthetic", confidence: pyFloat(0.9), [field]: value },
            ],
          }),
        (error: unknown) => error instanceof ValueError && error.message === `evidence.${field} must be a string`,
      );
      store.close();
      assert.deepEqual(readFileSync(path), before);
      assert.deepEqual(readdirSync(root), ["store.sqlite3"]);
    });
  }

for (const field of FIELDS)
  for (const [kind, value] of INVALID) {
    test(`P15 refuses ${field}/${kind} without committing`, () => {
      const { root, path, store } = fixture();
      const before = readFileSync(path);
      assert.throws(
        () => create(store, { [field]: value }),
        (error: unknown) => error instanceof ValueError && error.message === `evidence.${field} must be a string`,
      );
      store.close();
      assert.deepEqual(readFileSync(path), before);
      assert.deepEqual(readdirSync(root), ["store.sqlite3"]);
      assert.equal(store.all("SELECT * FROM memory_operations_v3").length, 0);
      store.close();
    });
  }
test("P15 preserves G1 replay before invalid new metadata", () => {
  const { path, store } = fixture();
  const first = create(store, { source_ref: "synthetic:original" });
  const before = readFileSync(path);
  assert.deepEqual(create(store, Object.fromEntries(FIELDS.map((field) => [field, null]))), first);
  store.close();
  assert.deepEqual(readFileSync(path), before);
});
test("P15 stays after the P5 writable gate", () => {
  const { root, path, store } = fixture(true);
  const before = readFileSync(path);
  assert.throws(() => create(store, { source_ref: null }), MigrationRequired);
  store.close();
  assert.deepEqual(readFileSync(path), before);
  assert.deepEqual(readdirSync(root), ["store.sqlite3"]);
});
test("P15 accepts absent/empty strings and JSON-valued source_payload", () => {
  const { store } = fixture();
  const result = create(store, {
    ...Object.fromEntries(FIELDS.map((field) => [field, ""])),
    source_payload: orderedObject([
      ["2", pyInt(1n)],
      ["1", [null, true, pyFloat(1)]],
    ]),
    confidence: pyFloat(0.9),
  });
  assert.equal((result.revision as any).record_id, "p15");
  store.close();
});

test("W1b Windows public migration never calls utimes or futimes", { skip: process.platform !== "win32" }, () => {
  const { root, path, store } = fixture(true);
  const backup = resolve(root, "backup.sqlite3");
  const target = resolve(root, "target.sqlite3");
  const sourceBytes = readFileSync(path);
  const before = statSync(path, { bigint: true });
  const savedUt = fs.utimesSync;
  const savedFut = fs.futimesSync;
  let setterCalls = 0;
  const refusedSetter = () => {
    setterCalls++;
    throw new Error("W1b forbids time setters for Windows backups");
  };
  fs.utimesSync = refusedSetter;
  fs.futimesSync = refusedSetter;
  syncBuiltinESMExports();
  try {
    const migrated = store.migrateTo(target, { backupPath: backup }) as MemoryStore;
    assert.equal(migrated.schemaInfo().state, "ready");
    migrated.close();
    assert.equal(setterCalls, 0);
    assert.deepEqual(readFileSync(backup), sourceBytes);
    assert.equal(statSync(backup, { bigint: true }).mtimeNs / 1000n, before.mtimeNs / 1000n);
    const after = statSync(path, { bigint: true });
    assert.deepEqual(readFileSync(path), sourceBytes);
    assert.equal(after.mtimeNs, before.mtimeNs);
    assert.equal(after.mode, before.mode);
    assert.deepEqual(readdirSync(root).sort(), ["backup.sqlite3", "store.sqlite3", "target.sqlite3"]);
  } finally {
    fs.utimesSync = savedUt;
    fs.futimesSync = savedFut;
    syncBuiltinESMExports();
    store.close();
  }
});
