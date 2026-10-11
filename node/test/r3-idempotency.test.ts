import "../src/sqlite-warning.ts";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, readdirSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import test from "node:test";
import { MemoryStore } from "../src/store.ts";
import { SchemaVersionError } from "../src/errors.ts";

function fixture() {
  const root = mkdtempSync(resolve(tmpdir(), "r3-init-"));
  const path = resolve(root, "store.sqlite3");
  const store = new MemoryStore(path);
  store.initialize();
  return { root, path, store };
}
function snapshot(root: string, path: string) {
  return { bytes: readFileSync(path), mtime: statSync(path, { bigint: true }).mtimeNs, inventory: readdirSync(root) };
}
test("P17 ready DELETE initialization preserves bytes, mtime and sidecars", () => {
  const { root, path, store } = fixture();
  const before = snapshot(root, path);
  assert.equal(store.initialize().changed, false);
  assert.deepEqual(snapshot(root, path), before);
});
test("P17 owned WAL repair precedes the unchanged readiness guard", () => {
  const { root, path, store } = fixture();
  const db = new DatabaseSync(path);
  db.exec("PRAGMA journal_mode=WAL");
  db.close();
  assert.equal(store.initialize().changed, false);
  assert.deepEqual(readFileSync(path).subarray(18, 20), Buffer.from([1, 1]));
  assert.deepEqual(readdirSync(root), ["store.sqlite3"]);
});
for (const pragma of ["application_id=1234", "user_version=6"]) {
  test(`P17 foreign/future WAL refuses untouched: ${pragma}`, () => {
    const { root, path, store } = fixture();
    const db = new DatabaseSync(path);
    db.exec(`PRAGMA ${pragma}; PRAGMA journal_mode=WAL`);
    db.close();
    const before = snapshot(root, path);
    assert.throws(
      () => store.initialize(),
      (error: unknown) => (error as Error).constructor === SchemaVersionError,
    );
    assert.deepEqual(snapshot(root, path), before);
  });
}

const { ValidatedIntake } = await import("../src/governance.ts");
function proposal() {
  return {
    operation_type: "create",
    record_id: "p18",
    record_class: "event",
    domain: "phase",
    actor: "synthetic",
    reason: "R3",
    logic: "synthetic",
    truth_basis: "synthetic",
    changes: { title: "P18" },
    evidence: [{ source_ref: "synthetic:p18", content_summary: "synthetic", confidence: 0.9 }],
    idempotency_key: "p18",
  };
}
for (const unit of [1, 3, 4, 5]) {
  test(`P18 does not catch a constraint from unit ${unit}`, () => {
    const { store } = fixture();
    const api = new ValidatedIntake(store, { surface: "golden" });
    const error = Object.assign(new Error("synthetic constraint"), { errcode: 19 });
    const transact = store.transaction.bind(store);
    const all = store.all.bind(store);
    let calls = 0,
      lookups = 0;
    store.all = (sql, ...args) => {
      if (sql.includes("FROM memory_intake_v3 WHERE idempotency_key")) lookups++;
      return all(sql, ...args);
    };
    store.transaction = (action) => {
      if (++calls === unit) throw error;
      return transact(action);
    };
    assert.throws(
      () => api.submit(proposal()),
      (caught: unknown) => caught === error,
    );
    assert.equal(lookups, 1);
  });
}
for (const condition of ["no-row", "wrong-id", "not-constraint", "lookup-fails"]) {
  test(`P18 requires complete recovery proof: ${condition}`, () => {
    const { store } = fixture();
    const api = new ValidatedIntake(store, { surface: "golden" });
    const value = { ...proposal(), unresolved_conflict: true };
    if (condition === "lookup-fails") {
      const all = store.all.bind(store);
      let checks = 0;
      store.all = (sql, ...args) => {
        if (sql.includes("FROM memory_intake_v3 WHERE idempotency_key") && ++checks > 1)
          throw new Error("fresh lookup cannot prove recovery");
        return all(sql, ...args);
      };
    }
    if (condition === "wrong-id") {
      api.submit(value);
      store.transaction((db) => db.exec("UPDATE memory_intake_v3 SET intake_id='intake:wrong'"));
      const all = store.all.bind(store);
      let checks = 0;
      store.all = (sql, ...args) => {
        if (sql.includes("FROM memory_intake_v3 WHERE idempotency_key") && ++checks === 1) return [];
        return all(sql, ...args);
      };
    }
    const transact = store.transaction.bind(store);
    let calls = 0,
      original: unknown;
    store.transaction = (action) => {
      calls++;
      try {
        if (calls === 2 && condition !== "wrong-id") {
          return transact((db) => {
            db.exec("INSERT INTO memory_meta_v3 VALUES('p18-rollback-proof','temporary')");
            if (condition === "not-constraint") throw Object.assign(new Error("synthetic"), { errcode: 1 });
            db.exec("INSERT INTO memory_intake_v3(intake_id) VALUES('incomplete')");
            throw new Error("unreachable");
          });
        }
        return transact(action);
      } catch (error) {
        original = error;
        throw error;
      }
    };
    assert.throws(
      () => api.submit(value),
      (caught: unknown) => caught === original,
    );
    assert.equal(store.all("SELECT value FROM memory_meta_v3 WHERE key='p18-rollback-proof'").length, 0);
  });
}
