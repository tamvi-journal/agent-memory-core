import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { copyFileSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { DatabaseSync } from "node:sqlite";
import {
  IdentityMemory, MemoryStore, canonicalJson, compareCodePoint, floatHex, hashPayload,
  loadProfile, orderedObject, parseLossless, pythonIndentedJson, type JsonValue, type OrderedObject,
} from "../src/index.ts";
import { hooks } from "../src/internal-hooks.ts";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const GOLDEN = resolve(ROOT, "spec", "golden-authority-v2");
const PROFILE = loadProfile(resolve(ROOT, "trajecta_identity", "profiles", "example", "profile.json"));
const sha256 = (value: Buffer | string): string => createHash("sha256").update(value).digest("hex");

class Clock {
  calls = 0;
  now = (): string => {
    const date = new Date(Date.UTC(2026, 8, 30, 0, 0, this.calls++));
    return date.toISOString().replace(".000Z", "+00:00");
  };
}

class TTY {
  stdinTTY: boolean;
  stdoutTTY: boolean;
  #value: string;
  constructor(value: string, present = true) { this.#value = value; this.stdinTTY = present; this.stdoutTTY = present; }
  read(): string { return `${this.#value}\n`; }
  write(_value: string): void {}
}

function plain(value: JsonValue): any {
  if (value === null || typeof value === "boolean" || typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(plain);
  if (value.kind === "int") return Number(value.value);
  if (value.kind === "float") return value.value;
  return Object.fromEntries(value.entries.map(([key, item]) => [key, plain(item)]));
}

const BOOTSTRAP = plain(parseLossless(readFileSync(resolve(GOLDEN, "negative-receipt-unknown", "dump.json"), "utf8")));

function astObject(value: JsonValue | undefined): OrderedObject {
  if (!value || Array.isArray(value) || typeof value !== "object" || value.kind !== "object") throw new TypeError("expected object");
  return value;
}

function astGet(value: OrderedObject, key: string): JsonValue | undefined {
  return value.entries.find(([name]) => name === key)?.[1];
}

function jsonValue(value: any): JsonValue {
  if (value === null || typeof value === "boolean" || typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(jsonValue);
  if (typeof value === "number") return Number.isInteger(value) ? { kind: "int", value: BigInt(value) } : { kind: "float", value };
  return orderedObject(Object.entries(value).map(([key, item]) => [key, jsonValue(item)]));
}

function canonicalPlain(value: any): string {
  if (value === null || typeof value === "boolean" || typeof value === "number" || typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalPlain).join(",")}]`;
  return `{${Object.keys(value).sort(compareCodePoint).map((key) => `${JSON.stringify(key)}:${canonicalPlain(value[key])}`).join(",")}}`;
}

function databaseValue(value: any): any {
  return value && typeof value === "object" && !Array.isArray(value) && "repr" in value && "hex" in value ? Number(value.repr) : value;
}

function insertRows(database: DatabaseSync, table: string, rows: any[]): void {
  for (const row of rows) {
    const columns = Object.keys(row);
    const quoted = columns.map((name) => `"${name.replaceAll('"', '""')}"`).join(",");
    database.prepare(`INSERT INTO "${table}"(${quoted}) VALUES(${columns.map(() => "?").join(",")})`).run(...columns.map((name) => databaseValue(row[name])));
  }
}

const INSERT_ORDER = [
  "memory_records_v3", "memory_revisions_v3", "memory_telemetry_v3", "memory_evidence_v3",
  "memory_revision_evidence_v3", "memory_relations_v3", "memory_relation_events_v4", "memory_cues_v3",
  "memory_operations_v3", "memory_lifecycle_events_v3", "memory_intake_v3", "memory_access_v3",
  "memory_core_proposals_v5", "memory_owner_receipts_v5", "memory_proposal_decisions_v5", "memory_receipt_consumptions_v5",
];

function seedBootstrap(store: MemoryStore): void {
  store.initialize();
  store.transaction((database) => {
    for (const table of INSERT_ORDER) insertRows(database, table, BOOTSTRAP.tables[table] ?? []);
  });
}

function seedPhase(store: MemoryStore, expectedDump: any): void {
  const tables = expectedDump.tables;
  const revision = tables.memory_revisions_v3.find((row: any) => row.record_id === "phase:retract-me");
  const revisionId = revision.revision_id;
  const selfEvidence = tables.memory_evidence_v3.find((row: any) => row.source_ref === "self:retract-me");
  const selected: Record<string, any[]> = {
    memory_records_v3: tables.memory_records_v3.filter((row: any) => row.record_id === "phase:retract-me"),
    memory_revisions_v3: [revision],
    memory_telemetry_v3: tables.memory_telemetry_v3.filter((row: any) => row.revision_id === revisionId),
    memory_evidence_v3: [selfEvidence],
    memory_revision_evidence_v3: tables.memory_revision_evidence_v3.filter((row: any) => row.revision_id === revisionId && row.evidence_id === selfEvidence.evidence_id),
    memory_cues_v3: tables.memory_cues_v3.filter((row: any) => row.target_record_id === "phase:retract-me"),
    memory_intake_v3: tables.memory_intake_v3.filter((row: any) => row.target_record_id === "phase:retract-me"),
    memory_operations_v3: tables.memory_operations_v3.filter((row: any) => row.target_record_id === "phase:retract-me" && row.operation_type === "create"),
    memory_lifecycle_events_v3: tables.memory_lifecycle_events_v3.filter((row: any) => row.record_id === "phase:retract-me" && row.lifecycle_state === "current"),
  };
  store.transaction((database) => { for (const table of INSERT_ORDER) insertRows(database, table, selected[table] ?? []); });
}

function dumpDatabase(path: string): any {
  const database = new DatabaseSync(path, { readOnly: true });
  try {
    const tables: Record<string, any[]> = {};
    const names = (database.prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").all() as any[]).map((row) => String(row.name));
    for (const name of names) {
      const quoted = `"${name.replaceAll('"', '""')}"`;
      const columns = database.prepare(`PRAGMA table_info(${quoted})`).all() as any[];
      const primary = [...columns].filter((column) => column.pk).sort((a, b) => Number(a.pk) - Number(b.pk)).map((column) => String(column.name));
      const order = primary.length ? primary.map((column) => `"${column.replaceAll('"', '""')}"`).join(",") : "rowid";
      const floats = new Set(columns.filter((column) => String(column.type).toUpperCase().includes("REAL")).map((column) => String(column.name)));
      tables[name] = (database.prepare(`SELECT * FROM ${quoted} ORDER BY ${order}`).all() as any[]).map((row) => Object.fromEntries(Object.entries(row).map(([key, value]) => [
        key, floats.has(key) && typeof value === "number" ? { repr: value.toString().includes(".") ? value.toString() : `${value}.0`, hex: floatHex(value) } : value,
      ])));
    }
    return { tables };
  } finally { database.close(); }
}

function operationCount(path: string): number {
  const database = new DatabaseSync(path, { readOnly: true });
  try { return Number((database.prepare("SELECT COUNT(*) AS n FROM memory_operations_v3").get() as any).n); }
  finally { database.close(); }
}

function dropGuards(database: DatabaseSync, table: string): void {
  database.exec(`DROP TRIGGER IF EXISTS ${table}_no_update; DROP TRIGGER IF EXISTS ${table}_no_delete`);
}

function mutate(path: string, callback: (database: DatabaseSync) => void): void {
  const database = new DatabaseSync(path, { enableForeignKeyConstraints: true });
  try { callback(database); } finally { database.close(); }
}

function receipt(memory: IdentityMemory, purpose: string, binding: OrderedObject, clock: Clock): Record<string, unknown> {
  const bindingJson = canonicalJson(binding);
  const digest = hashPayload(binding);
  const receiptId = `receipt:${digest.slice(0, 32)}`;
  memory.store.transaction((database) => database.prepare("INSERT INTO memory_owner_receipts_v5 VALUES(?,?,?,?,?,?,?,?,?)").run(
    receiptId, purpose, memory.profile.name, bindingJson, digest, clock.now(), memory.profile.owner, "owner", "tty-human-presence/v1",
  ));
  return memory.store.all("SELECT * FROM memory_owner_receipts_v5 WHERE receipt_id=?", receiptId)[0];
}

function resolveRefs(value: any, saved: Record<string, any>): any {
  if (value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).length === 1 && "$ref" in value) {
    return String(value.$ref).split(".").reduce((current, key) => current[key], saved as any);
  }
  if (Array.isArray(value)) return value.map((item) => resolveRefs(item, saved));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, resolveRefs(item, saved)]));
  return value;
}

function short(value: string): string { return value.split(":", 2)[1].slice(0, 12); }

function errorName(error: any): string {
  if (error?.name === "MigrationRequired") return "MigrationRequiredError";
  if (/append-only/u.test(String(error?.message))) return "IntegrityError";
  return error?.message === "crash after revision insert" ? "RuntimeError" : String(error?.name ?? "Error");
}

test("authority manifest authenticates every corpus file and frozen table", () => {
  const manifest = JSON.parse(readFileSync(resolve(GOLDEN, "MANIFEST.json"), "utf8"));
  const files: string[] = [];
  const visit = (directory: string): void => {
    for (const name of readdirSync(directory).sort(compareCodePoint)) {
      const path = resolve(directory, name);
      if (statSync(path).isDirectory()) visit(path);
      else if (path !== resolve(GOLDEN, "MANIFEST.json")) files.push(path.slice(GOLDEN.length + 1).replaceAll("\\", "/"));
    }
  };
  visit(GOLDEN);
  for (const name of readdirSync(resolve(ROOT, "memory_core", "tables")).filter((name) => name.endsWith(".json")).sort(compareCodePoint)) files.push(`tables/${name}`);
  assert.deepEqual(files.sort(compareCodePoint), Object.keys(manifest.files).sort(compareCodePoint));
  for (const [relative, digest] of Object.entries(manifest.files) as [string, string][]) {
    const path = relative.startsWith("tables/") ? resolve(ROOT, "memory_core", relative) : resolve(GOLDEN, relative);
    assert.equal(sha256(readFileSync(path)), digest, relative);
  }
});

const scenarios = readdirSync(GOLDEN, { withFileTypes: true }).filter((entry) => entry.isDirectory()).map((entry) => entry.name).sort(compareCodePoint);
for (const scenario of scenarios) {
  test(`authority corpus ${scenario}`, () => {
    const directory = mkdtempSync(resolve(tmpdir(), "trajecta-r2a-node-"));
    let databasePath = resolve(directory, "store.sqlite3");
    const clock = new Clock();
    const expectedDump = plain(parseLossless(readFileSync(resolve(GOLDEN, scenario, "dump.json"), "utf8")));
    const expectedCase = JSON.parse(readFileSync(resolve(GOLDEN, scenario, "cases.jsonl"), "utf8"));
    const scriptAst = astObject(parseLossless(readFileSync(resolve(GOLDEN, scenario, "script.json"), "utf8")));
    const actions = astGet(scriptAst, "actions") as JsonValue[];
    const saved: Record<string, any> = {};
    const actualResults: any[] = [];
    let memory = new IdentityMemory(PROFILE, databasePath, { surface: "golden", now: clock.now, displayDatabase: "store.sqlite3" });
    const resetMemory = (): void => { memory.close(); memory = new IdentityMemory(PROFILE, databasePath, { surface: "golden", now: clock.now, displayDatabase: "store.sqlite3" }); };
    try {
      for (let index = 0; index < actions.length; index++) {
        const actionAst = astObject(actions[index]);
        const action = plain(actionAst);
        const args = resolveRefs(action.arguments ?? {}, saved);
        const before = memory.store.exists() ? readFileSync(databasePath) : null;
        const operations = memory.store.exists() && statSync(databasePath).size ? (() => { try { return operationCount(databasePath); } catch { return 0; } })() : 0;
        let result: any;
        try {
          switch (action.call) {
            case "bootstrap": seedBootstrap(memory.store); clock.calls = 28; result = { status: "bootstrapped" }; break;
            case "install_legacy_v4": memory.close(); copyFileSync(resolve(ROOT, action.source), databasePath); resetMemory(); result = null; break;
            case "migrate_v4_to_v5": {
              const target = resolve(directory, "migrated.sqlite3"); const backup = resolve(directory, "legacy.backup.sqlite3");
              memory.close(); const sourceBytes = readFileSync(databasePath); new MemoryStore(databasePath).migrateV4To(target, backup);
              assert.deepEqual(readFileSync(databasePath), sourceBytes, "migration source changed");
              assert.deepEqual(readFileSync(backup), sourceBytes, "migration backup is not byte-identical");
              databasePath = target; resetMemory(); result = null; break;
            }
            case "identity_core_propose": {
              const rawArgs = astObject(astGet(actionAst, "arguments"));
              const phase = astObject(astGet(rawArgs, "phase_context"));
              result = memory.corePropose({ reason: args.reason, phaseContext: phase, title: args.title, summary: args.summary,
                vhoStack: astGet(rawArgs, "vho_stack") ? astObject(astGet(rawArgs, "vho_stack")) : undefined,
                recognitionSignature: astGet(rawArgs, "recognition_signature") as JsonValue[] | undefined,
                falsifier: args.falsifier, sourceRef: args.source_ref });
              break;
            }
            case "owner_approve_core": {
              const expected = `${String(args.outcome).toUpperCase()} ${short(args.proposal_id)}`;
              const value = args.confirmation && args.confirmation !== "correct" ? args.confirmation : expected;
              result = memory.issueCoreReceipt(args.proposal_id, args.outcome, args.decision_note ?? "", new TTY(value, args.tty ?? true)); break;
            }
            case "identity_core_apply": result = memory.coreApply(args.receipt_id); break;
            case "fixture_log_phase": {
              if (memory.store.schemaInfo().state === "legacy-v4") memory.store.transaction(() => null);
              seedPhase(memory.store, expectedDump); clock.calls += 7; result = { linked: {}, record_id: "phase:retract-me", status: "logged" }; break;
            }
            case "owner_approve_retract": result = memory.issueRetractReceipt(args.record_id, args.reason, new TTY(`RETRACT ${args.record_id}`)); break;
            case "identity_retract": result = memory.retract(args.receipt_id); break;
            case "apply_retract_wrong_purpose": result = memory.retract(args.receipt_id); break;
            case "owner_close_legacy": {
              const events = memory.store.relationHistory("core", "anchor:discussions", "awaiting-discussion");
              result = memory.issueLegacyCloseReceipt(args.note, new TTY(`CLOSE ${short(String(events.at(-1)!.relation_event_id))}`)); break;
            }
            case "identity_close_legacy_discussion": result = memory.closeLegacyDiscussion(args.receipt_id); break;
            case "identity_status": result = memory.status(); (result as any).db = "store.sqlite3"; break;
            case "identity_packet": result = memory.retrieve(args.cue ?? "who are you", { track: false }); break;
            case "apply_core_as_profile": {
              const other = new IdentityMemory({ ...PROFILE, name: args.profile }, databasePath, { surface: "golden", now: clock.now });
              try { result = other.coreApply(args.receipt_id); } finally { other.close(); }
              break;
            }
            case "public_writer_pinned": result = (memory.store as any)[args.writer === "create" ? "createCurrent" : args.writer](args.record_id); break;
            case "runtime_submit_pinned": memory.store.guardPinned(args.record_id); result = null; break;
            case "raw_append_only": mutate(databasePath, (db) => db.exec(args.verb === "DELETE" ? `DELETE FROM ${args.table}` : `UPDATE ${args.table} SET rowid=rowid`)); result = null; break;
            case "tamper": mutate(databasePath, (db) => {
              const table = action.kind.startsWith("receipt-") ? "memory_owner_receipts_v5" : "memory_core_proposals_v5"; dropGuards(db, table);
              if (action.kind === "receipt-binding-json") db.prepare("UPDATE memory_owner_receipts_v5 SET binding_json=replace(binding_json, '\"decision_note\":\"\"', '\"decision_note\":\"tampered\"') WHERE receipt_id=?").run(args.receipt_id);
              if (action.kind === "receipt-binding-sha") db.prepare("UPDATE memory_owner_receipts_v5 SET binding_sha256=? WHERE receipt_id=?").run("0".repeat(64), args.receipt_id);
              if (action.kind === "proposal-content") db.prepare("UPDATE memory_core_proposals_v5 SET content=content||' ' WHERE proposal_id=?").run(args.proposal_id);
              if (action.kind === "proposal-phase-json") db.prepare("UPDATE memory_core_proposals_v5 SET phase_context_json=? WHERE proposal_id=?").run('{"model":"tampered"}', args.proposal_id);
              if (action.kind === "proposal-source-ref") db.prepare("UPDATE memory_core_proposals_v5 SET source_ref=source_ref||':tampered' WHERE proposal_id=?").run(args.proposal_id);
            }); result = null; break;
            case "forge_phase_mismatch": {
              const proposal = saved[action.proposal];
              const content = astObject(parseLossless(proposal.content));
              const changed = orderedObject(content.entries.map(([key, value]) => [key, key === "phase_context" ? orderedObject([["model", "different"]]) : value]));
              const changedText = pythonIndentedJson(changed); const contentSha = sha256(changedText);
              const fields = orderedObject([["profile", proposal.profile], ["record_id", proposal.record_id], ["base_core_revision_id", proposal.base_core_revision_id], ["title", proposal.title], ["summary", proposal.summary], ["content_sha256", contentSha], ["phase_context_sha256", proposal.phase_context_sha256], ["reason_sha256", proposal.reason_sha256], ["source_ref", proposal.source_ref]] as [string, JsonValue][]);
              const proposalSha = hashPayload(fields); const proposalId = `core-proposal:${proposalSha.slice(0, 32)}`;
              mutate(databasePath, (db) => { dropGuards(db, "memory_core_proposals_v5"); db.prepare("UPDATE memory_core_proposals_v5 SET proposal_id=?,content=?,content_sha256=?,proposal_sha256=? WHERE proposal_id=?").run(proposalId, changedText, contentSha, proposalSha, proposal.proposal_id); });
              result = receipt(memory, "identity_core_revision", orderedObject([["purpose", "identity_core_revision"], ["profile", PROFILE.name], ["current_core_revision_id", proposal.base_core_revision_id], ["proposal_id", proposalId], ["proposal_sha256", proposalSha], ["content_sha256", contentSha], ["phase_context_sha256", proposal.phase_context_sha256], ["reason_sha256", proposal.reason_sha256], ["source_ref", proposal.source_ref], ["outcome", "apply"], ["decision_note", ""], ["authority", "owner"]] as [string, JsonValue][]), clock); break;
            }
            case "forge_stale_base": {
              const proposal = saved[action.proposal]; const current = memory.store.currentView("core")[0];
              result = receipt(memory, "identity_core_revision", orderedObject([["purpose", "identity_core_revision"], ["profile", PROFILE.name], ["current_core_revision_id", String(current.revision_id)], ["proposal_id", proposal.proposal_id], ["proposal_sha256", proposal.proposal_sha256], ["content_sha256", proposal.content_sha256], ["phase_context_sha256", proposal.phase_context_sha256], ["reason_sha256", proposal.reason_sha256], ["source_ref", proposal.source_ref], ["outcome", "apply"], ["decision_note", "stale-base fixture"], ["authority", "owner"]] as [string, JsonValue][]), clock); break;
            }
            case "forge_cross_profile": {
              const proposal = saved[action.proposal];
              const fields = orderedObject([["profile", "foreign-profile"], ["record_id", proposal.record_id], ["base_core_revision_id", proposal.base_core_revision_id], ["title", proposal.title], ["summary", proposal.summary], ["content_sha256", proposal.content_sha256], ["phase_context_sha256", proposal.phase_context_sha256], ["reason_sha256", proposal.reason_sha256], ["source_ref", proposal.source_ref]] as [string, JsonValue][]);
              const proposalSha = hashPayload(fields); const proposalId = `core-proposal:${proposalSha.slice(0, 32)}`;
              mutate(databasePath, (db) => { dropGuards(db, "memory_core_proposals_v5"); db.prepare("UPDATE memory_core_proposals_v5 SET proposal_id=?,profile=?,proposal_sha256=? WHERE proposal_id=?").run(proposalId, "foreign-profile", proposalSha, proposal.proposal_id); });
              result = receipt(memory, "identity_core_revision", orderedObject([["purpose", "identity_core_revision"], ["profile", PROFILE.name], ["current_core_revision_id", proposal.base_core_revision_id], ["proposal_id", proposalId], ["proposal_sha256", proposalSha], ["content_sha256", proposal.content_sha256], ["phase_context_sha256", proposal.phase_context_sha256], ["reason_sha256", proposal.reason_sha256], ["source_ref", proposal.source_ref], ["outcome", "apply"], ["decision_note", ""], ["authority", "owner"]] as [string, JsonValue][]), clock); break;
            }
            case "crash_after_revision": hooks.afterAuthorityRevisionInsert = () => { throw new Error("crash after revision insert"); }; try { result = memory.coreApply(args.receipt_id); } finally { hooks.afterAuthorityRevisionInsert = null; } break;
            default: throw new Error(`unsupported authority corpus action ${action.call}`);
          }
          if (action.expect_error) assert.fail(`${action.call} did not raise ${action.expect_error}`);
        } catch (error) {
          hooks.afterAuthorityRevisionInsert = null;
          if (!action.expect_error) throw error;
          result = { error: errorName(error), message: (error as Error).message, no_operation: operationCount(databasePath) === operations, store_bytes_unchanged: before !== null && readFileSync(databasePath).equals(before) };
        }
        if (action.save) saved[action.save] = result;
        if (action.record !== false && action.call !== "tamper" && action.call !== "owner_close_legacy") actualResults.push({ call: action.call, index, result });
      }
      assert.deepEqual(actualResults, expectedCase.results, "discrete script outcomes");
      assert.equal(canonicalPlain(dumpDatabase(databasePath)) + "\n", readFileSync(resolve(GOLDEN, scenario, "dump.json"), "utf8"), "database dump");
    } finally { memory.close(); rmSync(directory, { recursive: true, force: true }); }
  });
}
