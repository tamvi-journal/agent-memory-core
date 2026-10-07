import assert from "node:assert/strict";
import { copyFileSync, mkdtempSync, readFileSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import test from "node:test";
import { InjectedClock, MemoryStore, orderedObject, pyFloat, pyInt } from "../src/index.ts";
import { MigrationRequired, ValueError } from "../src/errors.ts";

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
