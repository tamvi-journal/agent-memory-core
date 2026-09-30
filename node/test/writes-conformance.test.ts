import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { hooks } from "../src/internal-hooks.ts";
import { DatabaseSync } from "node:sqlite";
import {
  IdentityMemory,
  InjectedClock,
  MemoryStore,
  ValidatedIntake,
  DEFAULT_POLICY,
  canonicalJson,
  compareCodePoint,
  floatHex,
  loadProfile,
  orderedObject,
  parseLossless,
  type JsonValue,
  type OrderedObject,
} from "../src/index.ts";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const GOLDEN = resolve(ROOT, "spec", "golden-writes-v5");
const PROFILE_PATH = resolve(ROOT, "trajecta_identity", "profiles", "example", "profile.json");
const sha256 = (v: Buffer | string) => createHash("sha256").update(v).digest("hex");
function plain(value: JsonValue): any {
  if (value === null || typeof value === "boolean" || typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(plain);
  if (value.kind === "int") return Number(value.value);
  if (value.kind === "float") return value.value;
  return Object.fromEntries(value.entries.map(([k, v]) => [k, plain(v)]));
}
function native(value: JsonValue): any {
  if (value === null || typeof value === "boolean" || typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(native);
  if (value.kind === "int" || value.kind === "float") return value;
  return Object.fromEntries(value.entries.map(([k, v]) => [k, native(v)]));
}
function canonicalPlain(value: any): string {
  if (value === null || typeof value === "boolean" || typeof value === "number" || typeof value === "string")
    return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalPlain).join(",")}]`;
  return `{${Object.keys(value)
    .sort(compareCodePoint)
    .map((k) => `${JSON.stringify(k)}:${canonicalPlain(value[k])}`)
    .join(",")}}`;
}
function dumpDatabase(path: string): any {
  const database = new DatabaseSync(path, { readOnly: true });
  try {
    const tables: Record<string, any[]> = {};
    const names = (
      database.prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").all() as any[]
    ).map((row) => String(row.name));
    for (const name of names) {
      const quoted = `"${name.replaceAll('"', '""')}"`;
      const columns = database.prepare(`PRAGMA table_info(${quoted})`).all() as any[];
      const primary = [...columns]
        .filter((column) => column.pk)
        .sort((a, b) => Number(a.pk) - Number(b.pk))
        .map((column) => String(column.name));
      const order = primary.length ? primary.map((column) => `"${column.replaceAll('"', '""')}"`).join(",") : "rowid";
      const floats = new Set(
        columns
          .filter((column) => String(column.type).toUpperCase().includes("REAL"))
          .map((column) => String(column.name)),
      );
      tables[name] = (database.prepare(`SELECT * FROM ${quoted} ORDER BY ${order}`).all() as any[]).map((row) =>
        Object.fromEntries(
          Object.entries(row).map(([key, value]) => [
            key,
            floats.has(key) && typeof value === "number"
              ? { repr: value.toString().includes(".") ? value.toString() : `${value}.0`, hex: floatHex(value) }
              : value,
          ]),
        ),
      );
    }
    return { tables };
  } finally {
    database.close();
  }
}
function mapPhase(args: any) {
  return {
    title: args.title,
    summary: args.summary,
    content: args.content,
    follows: args.follows,
    causedBy: args.caused_by,
    dependsOn: args.depends_on,
    decidedBecause: args.decided_because,
    openLoop: args.open_loop,
    workRefs: args.work_refs,
    cues: args.cues,
    sourceRef: args.source_ref,
    confidence: args.confidence,
    phaseContext: args.phase_context,
    occurredAt: args.occurred_at,
    evidence: args.evidence,
  };
}
function mapFact(args: any) {
  return {
    title: args.title,
    summary: args.summary,
    content: args.content,
    causedBy: args.caused_by,
    dependsOn: args.depends_on,
    cues: args.cues,
    sourceRef: args.source_ref,
    confidence: args.confidence,
    evidence: args.evidence,
  };
}
function proposal(p: any): any {
  return {
    operation_type: p.operation_type,
    record_id: p.record_id,
    record_class: p.record_class,
    domain: p.domain,
    actor: p.actor,
    reason: p.reason,
    logic: p.logic,
    truth_basis: p.truth_basis,
    evidence: p.evidence,
    idempotency_key: p.idempotency_key,
    changes: p.changes,
    falsifier: p.falsifier,
    unresolved_conflict: p.unresolved_conflict,
    protected_effect: p.protected_effect,
    protected_authorized: p.protected_authorized,
  };
}
function errorName(e: any) {
  return e?.name === "MigrationRequired" ? "MigrationRequiredError" : (e?.name ?? e?.constructor?.name);
}
function fixtureWork(root: string, kind: string) {
  mkdirSync(root);
  if (kind === "wrong-schema") {
    writeFileSync(resolve(root, "state.json"), '{"schema":"wrong","work":[]}\n');
    return;
  }
  writeFileSync(
    resolve(root, "state.json"),
    '{"schema":"trajecta.state/v1","work":[{"id":"work:12345678-abcd","topic":"R2b","goal":"parity","status":"active","revision":3,"nextAction":"replay","openLoops":["crash"],"activeBranchId":"branch:one","branches":[{"id":"branch:one","label":"writers"}],"updatedAt":"2026-09-30T00:00:00Z"}]}\n',
  );
  writeFileSync(
    resolve(root, "deltas.jsonl"),
    '{"createdAt":"2026-09-30T00:00:01Z","id":"delta:abcdef12-3456","kind":"progress","revision":3,"summary":"ported","workId":"work:12345678-abcd"}\n{"torn":\n',
  );
}
function resolveSpecial(value: any, memory: IdentityMemory): any {
  if (value && typeof value === "object" && !Array.isArray(value) && "$current" in value)
    return memory.store.currentView(value.$current)[0].revision_id;
  if (Array.isArray(value)) return value.map((x) => resolveSpecial(x, memory));
  if (value && typeof value === "object" && !("kind" in value))
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, resolveSpecial(v, memory)]));
  return value;
}
function invoke(memory: IdentityMemory, call: string, raw: any) {
  const args = resolveSpecial(raw, memory);
  switch (call) {
    case "initialize":
      return memory.store.initialize();
    case "bootstrap":
      return memory.bootstrap();
    case "bootstrap_invalid": {
      const profile = loadProfile(PROFILE_PATH);
      const core = profile.ast.entries.find(([k]) => k === "core")![1] as OrderedObject;
      const bad = orderedObject(core.entries.map(([k, v]) => [k, k === "falsifier" ? "" : v]));
      profile.ast = orderedObject(profile.ast.entries.map(([k, v]) => [k, k === "core" ? bad : v]));
      return new IdentityMemory(profile, memory.store.path, {
        surface: "golden",
        clock: memory.clock,
        displayDatabase: "store.sqlite3",
      }).bootstrap();
    }
    case "log_phase":
      return memory.logPhase(args.event_id, mapPhase(args));
    case "log_fact":
      return memory.logFact(args.fact_id, mapFact(args));
    case "close_loop":
      return memory.closeLoop(args.record_id, args.note);
    case "runtime_submit": {
      let p = args.proposal;
      if (p.$pinned) {
        p = {
          operation_type: "create",
          record_id: "core",
          record_class: "belief",
          domain: "fact",
          actor: "golden",
          reason: "golden proposal",
          logic: "bounded fixture",
          truth_basis: "golden source",
          evidence: [
            {
              evidence_type: "synthetic",
              source_ref: "golden:pinned",
              content_summary: "pinned",
              confidence: { kind: "float", value: 0.9 },
              privacy_class: "synthetic",
            },
          ],
          idempotency_key: "pinned",
          changes: { title: "core", summary: "pinned" },
        };
      }
      return memory.runtime.submit(proposal(p));
    }
    case "intake_submit":
      return new ValidatedIntake(memory.store, { surface: "golden", policy: DEFAULT_POLICY }).submit(
        proposal(args.proposal),
      );
    case "add_relation":
      return memory.store.addRelation({
        relationId: args.relation_id,
        fromRecordId: args.from_record_id,
        toRecordId: args.to_record_id,
        relationType: args.relation_type,
        weight: args.weight,
      });
    case "add_cue":
      return memory.store.addCue({
        profile: args.profile,
        cue: args.cue,
        targetRecordId: args.target_record_id,
        weight: args.weight,
      });
    case "record_access":
      return memory.store.recordAccess({
        cue: args.cue,
        recordId: args.record_id,
        revisionId: args.revision_id,
        retrievalReason: args.retrieval_reason,
        rank: Number(args.rank?.value ?? args.rank),
        surface: args.surface,
        gain: args.gain,
      });
    case "maintenance": {
      const drift = args.$semantic_drift;
      if (drift)
        hooks.beforeMaintenanceHashCheck = (db: any) => {
          db.exec("DROP TRIGGER memory_revisions_v3_no_update");
          db.prepare("UPDATE memory_revisions_v3 SET content_sha256='drifted' WHERE record_id='phase:aa'").run();
        };
      try {
        return memory.store.applyMaintenance({
          runId: args.run_id,
          adjustments: args.adjustments.map((x: any) => ({
            recordId: x.record_id,
            field: x.field,
            oldValue: x.old_value,
            newValue: x.new_value,
          })),
          actor: args.actor,
          reason: args.reason,
        });
      } finally {
        hooks.beforeMaintenanceHashCheck = null;
      }
    }
    case "retrieve":
      return memory.retrieve(args.cue, { track: args.track, limit: Number(args.limit?.value ?? args.limit) });
    case "decay":
      return memory.decay(args.now);
    default:
      throw new Error(`unknown ${call}`);
  }
}

const manifest = plain(parseLossless(readFileSync(resolve(GOLDEN, "MANIFEST.json"), "utf8")));
test("writes manifest authenticates every corpus file and table", () => {
  for (const [rel, digest] of Object.entries(manifest.files)) {
    const path = rel.startsWith("tables/") ? resolve(ROOT, "memory_core", rel) : resolve(GOLDEN, rel);
    assert.equal(sha256(readFileSync(path)), digest);
  }
});
for (const scenario of readdirSync(GOLDEN).sort(compareCodePoint)) {
  if (scenario === "MANIFEST.json" || scenario.startsWith("migration-") || scenario === "legacy-v4") continue;
  test(`writes corpus ${scenario}`, () => {
    const folder = resolve(GOLDEN, scenario),
      script = native(parseLossless(readFileSync(resolve(folder, "script.json"), "utf8"))),
      expected = plain(parseLossless(readFileSync(resolve(folder, "cases.jsonl"), "utf8")));
    const temp = mkdtempSync(resolve(tmpdir(), `r2b-${scenario}-`)),
      db = resolve(temp, "store.sqlite3"),
      work = resolve(temp, "work");
    if (script.work_fixture) fixtureWork(work, script.work_fixture);
    const memory = new IdentityMemory(loadProfile(PROFILE_PATH), db, {
      surface: "golden",
      clock: new InjectedClock(),
      displayDatabase: "store.sqlite3",
      workRoot: script.work_fixture ? work : null,
    });
    const results: any[] = [];
    for (let i = 0; i < script.actions.length; i++) {
      const action = script.actions[i],
        before = existsSync(db) ? readFileSync(db) : null,
        ops = existsSync(db) ? memory.store.all("SELECT * FROM memory_operations_v3").length : 0;
      try {
        const result = invoke(memory, action.call, action.arguments ?? {});
        if (action.expect_error) assert.fail(`expected ${action.expect_error}`);
        results.push({ index: i, call: action.call, result });
      } catch (error: any) {
        if (!action.expect_error) throw error;
        assert.equal(errorName(error), action.expect_error);
        memory.store.close();
        assert.deepEqual(existsSync(db) ? readFileSync(db) : null, before);
        assert.equal(memory.store.all("SELECT * FROM memory_operations_v3").length, ops);
        let message = String(error.message)
          .replaceAll(db, "store.sqlite3")
          .replaceAll(work, "<work-root>")
          .replaceAll("\\", "/");
        results.push({
          index: i,
          call: action.call,
          result: { error: action.expect_error, message, store_bytes_unchanged: true, no_operation: true },
        });
      }
    }
    memory.close();
    assert.equal(canonicalPlain({ label: scenario, results }), canonicalPlain(expected));
    assert.equal(
      canonicalPlain(dumpDatabase(db)),
      canonicalPlain(plain(parseLossless(readFileSync(resolve(folder, "dump.json"), "utf8")))),
    );
  });
}

function legacyV2(path: string): void {
  const db = new DatabaseSync(path);
  try {
    db.exec(`
 CREATE TABLE memory_records_v2(record_id TEXT PRIMARY KEY,record_class TEXT,domain TEXT,scope TEXT,record_status TEXT,created_at TEXT,created_by TEXT);
 CREATE TABLE memory_revisions_v2(revision_id TEXT PRIMARY KEY,record_id TEXT,parent_revision_id TEXT,revision_number INTEGER,title TEXT,summary TEXT,content TEXT,impact TEXT,confidence REAL,salience REAL,stability REAL,accessibility REAL,valid_from TEXT,valid_to TEXT,revision_status TEXT,authority_status TEXT,content_sha256 TEXT,created_at TEXT,created_by TEXT,surface TEXT,model_family TEXT,reason TEXT,idempotency_key TEXT);
 CREATE TABLE memory_evidence_v2(evidence_id TEXT PRIMARY KEY,evidence_type TEXT,source_ref TEXT,source_sha256 TEXT,captured_at TEXT,actor TEXT,surface TEXT,model_family TEXT,content_summary TEXT,confidence REAL,privacy_class TEXT);
 CREATE TABLE memory_revision_evidence_v2(revision_id TEXT,evidence_id TEXT,stance TEXT,weight REAL,reason TEXT);`);
    db.prepare("INSERT INTO memory_records_v2 VALUES(?,?,?,?,?,?,?)").run(
      "old-v2",
      "belief",
      "semantic",
      "global",
      "active",
      "2026-01-01T00:00:00+00:00",
      "legacy-agent",
    );
    db.prepare("INSERT INTO memory_revisions_v2 VALUES(" + Array(23).fill("?").join(",") + ")").run(
      "old-v2@r1",
      "old-v2",
      null,
      1,
      "Old v2",
      "Preserved v2",
      "",
      "",
      0.8,
      0.7,
      0.6,
      0.5,
      "2026-01-01T00:00:00+00:00",
      null,
      "current",
      "canonical_reference",
      "legacy-semantic-hash",
      "2026-01-01T00:00:00+00:00",
      "legacy-agent",
      "legacy",
      "",
      "legacy fixture",
      "legacy:create",
    );
    db.prepare("INSERT INTO memory_evidence_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?)").run(
      "legacy-evidence",
      "observation",
      "legacy:fixture",
      "legacy-source-hash",
      "2026-01-01T00:00:00+00:00",
      "legacy-agent",
      "legacy",
      "",
      "Legacy evidence",
      0.8,
      "synthetic",
    );
    db.prepare("INSERT INTO memory_revision_evidence_v2 VALUES(?,?,?,?,?)").run(
      "old-v2@r1",
      "legacy-evidence",
      "supports",
      1,
      "legacy fixture",
    );
  } finally {
    db.close();
  }
}
function legacyV3(path: string, clock: InjectedClock): void {
  const store = new MemoryStore(path, { now: () => clock.seconds() });
  store.initialize();
  store.createCurrent({
    recordId: "legacy",
    recordClass: "belief",
    domain: "misc",
    title: "Legacy v3",
    summary: "unmigrated",
    actor: "golden",
    reason: "fixture",
    evidence: { evidence_type: "synthetic", source_ref: "fixture:legacy", content_summary: "fixture", confidence: 1 },
    idempotencyKey: "fixture:legacy",
    accessibility: 0.6,
    stability: 0.5,
  });
  store.createCurrent({
    recordId: "legacy-target",
    recordClass: "belief",
    domain: "misc",
    title: "Old target",
    actor: "golden",
    reason: "fixture",
    evidence: {
      evidence_type: "synthetic",
      source_ref: "fixture:legacy-target",
      content_summary: "fixture",
      confidence: 1,
    },
    idempotencyKey: "fixture:legacy-target",
    accessibility: 0.6,
    stability: 0.5,
  });
  store.close();
  const db = new DatabaseSync(path);
  try {
    db.exec("DROP VIEW memory_relation_current_v4; DROP TABLE memory_relation_events_v4;");
    db.prepare(
      "INSERT INTO memory_relations_v3(relation_id,from_record_id,to_record_id,relation_type,weight,source_revision_id,status,created_at) VALUES('old-active','legacy','legacy-target','caused-by',0.7,NULL,'active','2026-09-01T00:00:00+00:00')",
    ).run();
    db.exec("PRAGMA user_version=3");
  } finally {
    db.close();
  }
}

for (const version of [2, 3, 4] as const)
  test(`writes corpus migration-v${version}`, () => {
    const scenario = `migration-v${version}`,
      folder = resolve(GOLDEN, scenario),
      temp = mkdtempSync(resolve(tmpdir(), `r2b-migrate-v${version}-`)),
      source = resolve(temp, `source-v${version}.sqlite3`),
      target = resolve(temp, "store.sqlite3"),
      backup = resolve(temp, `backup-v${version}.sqlite3`),
      dryTarget = resolve(temp, "dry.sqlite3"),
      clock = new InjectedClock();
    if (version === 2) legacyV2(source);
    else if (version === 3) legacyV3(source, clock);
    else cpSync(resolve(ROOT, "spec", "golden", "identity-open", "store.sqlite3"), source);
    const original = readFileSync(source),
      refusals: string[] = [];
    for (const kind of ["wal", "foreign", "future"]) {
      const guarded = resolve(temp, `guard-${kind}-v${version}.sqlite3`),
        guardTarget = resolve(temp, `guard-target-${kind}-v${version}.sqlite3`);
      cpSync(source, guarded);
      if (kind === "wal") writeFileSync(guarded + "-wal", "");
      else {
        const bytes = readFileSync(guarded);
        if (kind === "foreign") bytes.writeInt32BE(123, 68);
        else bytes.writeInt32BE(99, 60);
        writeFileSync(guarded, bytes);
      }
      const before = readFileSync(guarded);
      try {
        new MemoryStore(guarded).migrateTo(guardTarget);
        assert.fail(`${kind} migration source accepted`);
      } catch (error: any) {
        refusals.push(`${kind}:${errorName(error)}`);
      }
      assert.deepEqual(readFileSync(guarded), before);
      assert.equal(existsSync(guardTarget), false);
    }
    const store = new MemoryStore(source, { now: () => clock.seconds() }),
      dry = store.migrateTo(dryTarget, { dryRun: true }) as any;
    assert.equal(existsSync(dryTarget), false);
    const migrated = store.migrateTo(target, { backupPath: backup }) as MemoryStore;
    const result = {
      dry_run: dry,
      preflight_refusals: refusals,
      source_unchanged: readFileSync(source).equals(original),
      backup_name: `backup-v${version}.sqlite3`,
      backup_byte_identical: readFileSync(backup).equals(original),
      state: migrated.schemaInfo().state,
    };
    migrated.close();
    const expected = plain(parseLossless(readFileSync(resolve(folder, "cases.jsonl"), "utf8")));
    assert.equal(
      canonicalPlain({ label: scenario, results: [{ index: 0, call: "migrate", result }] }),
      canonicalPlain(expected),
    );
    assert.equal(
      canonicalPlain(dumpDatabase(target)),
      canonicalPlain(plain(parseLossless(readFileSync(resolve(folder, "dump.json"), "utf8")))),
    );
  });

test("writes corpus legacy-v4", () => {
  const folder = resolve(GOLDEN, "legacy-v4"),
    temp = mkdtempSync(resolve(tmpdir(), "r2b-legacy-v4-")),
    db = resolve(temp, "store.sqlite3");
  cpSync(resolve(ROOT, "spec", "golden", "identity-open", "store.sqlite3"), db);
  const memory = new IdentityMemory(loadProfile(PROFILE_PATH), db, {
    surface: "golden",
    clock: new InjectedClock(),
    displayDatabase: "store.sqlite3",
  });
  const calls: [string, () => unknown][] = [
    ["decay-zero", () => memory.decay("2026-09-30T00:00:00+00:00")],
    ["decay-adjust", () => memory.decay("2100-01-01T00:00:00+00:00")],
    ["bootstrap-exists", () => memory.bootstrap()],
    ["fact-noop", () => memory.logFact("belief", { title: "Held fact", summary: "after", content: "" })],
    ["close-noop", () => memory.closeLoop("phase:not-open", "done")],
    [
      "relation-noop",
      () =>
        memory.store.addRelation({
          relationId: "phase:first->open-loop->anchor:open-loops",
          fromRecordId: "phase:first",
          toRecordId: "anchor:open-loops",
          relationType: "open-loop",
          weight: 1,
        }),
    ],
    [
      "maintenance-replay",
      () =>
        memory.store.applyMaintenance({
          runId: "fixture:fact:belief:accessibility:0x1.3333333333333p-1",
          adjustments: [],
          actor: "golden",
          reason: "replay",
        }),
    ],
  ];
  const results: any[] = [];
  for (let i = 0; i < calls.length; i++) {
    const [call, fn] = calls[i],
      before = readFileSync(db),
      listing = readdirSync(temp).sort(compareCodePoint);
    assert.throws(fn, (e: any) => errorName(e) === "MigrationRequiredError");
    assert.deepEqual(readFileSync(db), before);
    assert.deepEqual(readdirSync(temp).sort(compareCodePoint), listing);
    results.push({
      index: i,
      call,
      result: {
        error: "MigrationRequiredError",
        message: "schema v4 store must be migrated to v5 before writing",
        store_bytes_unchanged: true,
        directory_unchanged: true,
        no_operation: true,
      },
    });
  }
  memory.close();
  const expected = plain(parseLossless(readFileSync(resolve(folder, "cases.jsonl"), "utf8")));
  assert.equal(canonicalPlain({ label: "legacy-v4", results }), canonicalPlain(expected));
  assert.deepEqual(readFileSync(db), readFileSync(resolve(folder, "store.sqlite3")));
});
