/** Test-only public inputs, with no cross-process commit-order assertions. */
import { writeSync, existsSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { MemoryStore } from "../../node/src/store.ts";
import { hooks } from "../../node/src/internal-hooks.ts";
import { ValidatedIntake } from "../../node/src/governance.ts";
import { parseLossless } from "../../node/src/json.ts";

function pause(): never {
  writeSync(2, "R3-CRASH-READY\n");
  for (;;) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0);
}
const writers = {
  createCurrent: "create_current",
  revise: "revise",
  invalidate: "invalidate",
  addRelation: "add_relation",
  retractRelation: "retract_relation",
  addCue: "add_cue",
  recordAccess: "record_access",
  applyMaintenance: "apply_maintenance",
};
function snake(value: any): any {
  if (Array.isArray(value)) return value.map(snake);
  if (!value || typeof value !== "object" || value.kind) return value;
  return Object.fromEntries(
    Object.entries(value)
      .filter(([, v]) => v !== undefined)
      .map(([key, v]) => [
        key.replace(/[A-Z]/gu, (c) => "_" + c.toLowerCase()),
        key === "evidence" || key === "changes" ? v : snake(v),
      ]),
  );
}
export function instrument(store: MemoryStore, clock: any, config: any, units: any[]): () => void {
  if (!config.capture && !config.crash && !config.crash_after_unit && !config.allocation_role) return () => {};
  const originalExec = DatabaseSync.prototype.exec;
  let scheduled = false;
  const resume = (role: string) => {
    writeSync(2, `R3-ALLOCATION-${role}\n`);
    while (!existsSync(config.allocation_release)) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 10);
  };
  if (config.allocation_role === "victim") {
    const original = DatabaseSync.prototype.exec;
    DatabaseSync.prototype.exec = function (sql: string) {
      if (!scheduled && sql === "BEGIN IMMEDIATE") {
        scheduled = true;
        resume("VICTIM");
      }
      return original.call(this, sql);
    };
  }
  hooks.afterCreateRevisionInsert = config.crash === "intake-revision" ? pause : null;
  hooks.afterAuthorityRevisionInsert = config.crash === "authority-revision" ? pause : null;
  let current: any = null;
  let committed = 0;
  const complete = (unit: any) => {
    if (config.capture) units.push(unit);
    committed++;
    if (Number(config.crash_after_unit?.value ?? config.crash_after_unit) === committed) pause();
  };
  let proposal: any = null,
    surface = "";
  const originalNormalized = (ValidatedIntake.prototype as any).normalized;
  (ValidatedIntake.prototype as any).normalized = function (value: any) {
    const result = originalNormalized.call(this, value);
    if (this.store === store) {
      proposal = result;
      surface = this.surface;
    }
    return result;
  };
  const originalTransaction = store.transaction.bind(store);
  store.transaction = (callback) => {
    if (current !== null || proposal === null) return originalTransaction(callback);
    const unit: any = { call: "", arguments: {}, surface, clocks: [] },
      statements: any[] = [];
    current = unit;
    let result;
    try {
      result = originalTransaction((db) =>
        callback(
          new Proxy(db, {
            get(target, key) {
              if (key === "prepare")
                return (sql: string) => {
                  const statement = target.prepare(sql);
                  return new Proxy(statement, {
                    get(stmt, method) {
                      const value = Reflect.get(stmt, method, stmt);
                      if (typeof value !== "function") return value;
                      return (...args: any[]) => {
                        if (method === "run") statements.push({ sql, args });
                        return Reflect.apply(value, stmt, args);
                      };
                    },
                  });
                };
              const value = Reflect.get(target, key, target);
              return typeof value === "function" ? value.bind(target) : value;
            },
          }),
        ),
      );
    } finally {
      current = null;
    }
    const first = statements[0];
    if (!first) throw new Error("unexpected empty intake transaction");
    if (first.sql.startsWith("INSERT INTO memory_evidence_v3")) {
      unit.call = "_capture_evidence";
      unit.arguments = { payload: proposal };
    } else if (first.sql.startsWith("INSERT INTO memory_intake_v3")) {
      const [intake_id, , target_record_id, proposal_sha256] = first.args;
      unit.call = "_insert_intake";
      unit.arguments = { intake_id, payload: proposal, target_record_id, proposal_sha256, evidence_ids: [] };
      // The INSERT carries the actual ordered evidence list as lossless JSON.
      unit.arguments.evidence_ids = parseLossless(first.args[4]);
    } else if (first.sql.startsWith("INSERT INTO memory_revision_evidence_v3")) {
      unit.call = "_link_evidence";
      unit.arguments = {
        revision_id: first.args[0],
        evidence_ids: statements.map((s) => s.args[1]),
        reason: first.args[3],
      };
    } else if (first.sql.startsWith("UPDATE memory_intake_v3")) {
      const [status, reason, target_record_id, operation_id, , intake_id] = first.args;
      unit.call = "_decide_intake";
      unit.arguments = { intake_id, status, reason, target_record_id, operation_id };
    } else throw new Error("unclassified intake transaction");
    complete(unit);
    return result;
  };
  const originalClock = store.clock;
  (store as any).clock = () => {
    if (config.allocation_role === "holder" && !scheduled && ["revise", "retract_relation"].includes(current?.call)) {
      scheduled = true;
      resume("HOLDER");
    }
    const value = originalClock();
    current?.clocks.push(value);
    return value;
  };
  for (const [method, call] of Object.entries(writers)) {
    const original = (store as any)[method].bind(store);
    (store as any)[method] = (args: any) => {
      const unit = { call, arguments: snake(args), clocks: [] };
      const parent = current;
      current = unit;
      let result;
      try {
        result = original(args);
      } finally {
        current = parent;
      }
      complete(unit);
      if (
        (config.crash === "g10" && method === "createCurrent") ||
        (config.crash === "g12" && method === "recordAccess") ||
        (config.crash === "g9" && method === "applyMaintenance")
      )
        pause();
      return result;
    };
  }
  if (config.crash === "transaction") {
    const original = store.transaction.bind(store);
    store.transaction = (callback) =>
      original((db) => {
        const result = callback(db);
        pause();
        return result;
      });
  }
  return () => {
    (ValidatedIntake.prototype as any).normalized = originalNormalized;
    DatabaseSync.prototype.exec = originalExec;
    hooks.afterCreateRevisionInsert = null;
    hooks.afterAuthorityRevisionInsert = null;
  };
}
