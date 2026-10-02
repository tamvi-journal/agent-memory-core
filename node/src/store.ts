import {
  closeSync,
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  rmSync,
  utimesSync,
  openSync,
  readFileSync,
  readSync,
  statSync,
  unlinkSync,
} from "node:fs";
import { dirname, resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { fileURLToPath } from "node:url";
import {
  FileExistsError,
  IncompatibleJournalMode,
  MigrationRequired,
  PinnedRecordError,
  SchemaVersionError,
  ValueError,
} from "./errors.ts";
import { hooks } from "./internal-hooks.ts";
import { createHash } from "node:crypto";
import { hashPayload, orderedObject, pyJsonDumps, type JsonValue } from "./encoding.ts";
import { parseLossless } from "./json.ts";
import { identitySegment } from "./evidence-identity.ts";
import { utcNowSeconds } from "./encoding.ts";
import {
  addCue,
  addRelation,
  applyMaintenance,
  createCurrent,
  invalidate,
  recordAccess,
  retractRelation,
  revise,
  type Adjustment,
  type CreateInput,
  type RelationInput,
  type ReviseInput,
  type Numeric,
} from "./kernel.ts";

export const APPLICATION_ID = 0x414d4333;
export const SCHEMA_VERSION = 5;
export const READABLE_SCHEMA_VERSIONS = new Set([4, 5]);
export const WRITABLE_SCHEMA_VERSION = 5;
export const LEGACY_V4_VERSION = 4;
export const LEGACY_V3_VERSION = 3;
const REPAIR = "Repair with: sqlite3 <store.sqlite3> 'PRAGMA journal_mode=DELETE;'";
const SCHEMA_PATH = resolve(dirname(fileURLToPath(import.meta.url)), "..", "schema.sql");

export type Row = Record<string, string | number | bigint | null>;
type Header = { applicationId: number; userVersion: number; wal: boolean; empty: boolean };
type Facts = { applicationId: number; userVersion: number; tables: Set<string> };

function readHeader(path: string): Header {
  const absolute = resolve(path);
  for (const suffix of ["-wal", "-shm"]) {
    if (existsSync(absolute + suffix)) throw new IncompatibleJournalMode(`SQLite WAL sidecar present. ${REPAIR}`);
  }
  const size = statSync(absolute).size;
  if (size === 0) return { applicationId: 0, userVersion: 0, wal: false, empty: true };
  if (size < 100) throw new SchemaVersionError("memory database is not initialized");
  const descriptor = openSync(absolute, "r");
  const header = Buffer.alloc(100);
  try {
    if (readSync(descriptor, header, 0, 100, 0) !== 100)
      throw new SchemaVersionError("memory database is not initialized");
  } finally {
    closeSync(descriptor);
  }
  if (!header.subarray(0, 16).equals(Buffer.from("SQLite format 3\0", "binary"))) {
    throw new SchemaVersionError("memory database is not initialized");
  }
  const wal = header[18] === 2 || header[19] === 2;
  if (wal) throw new IncompatibleJournalMode(`SQLite journal_mode=wal. ${REPAIR}`);
  return { applicationId: header.readInt32BE(68), userVersion: header.readInt32BE(60), wal, empty: false };
}

function inspect(database: DatabaseSync): Facts {
  const pragma = (name: string): number =>
    Number((database.prepare(`PRAGMA ${name}`).get() as Record<string, unknown>)[name]);
  const tables = new Set(
    (database.prepare("SELECT name FROM sqlite_master WHERE type='table'").all() as Row[]).map((row) =>
      String(row.name),
    ),
  );
  return { applicationId: pragma("application_id"), userVersion: pragma("user_version"), tables };
}

export function classify({ applicationId, userVersion, tables }: Facts): string {
  if (applicationId === APPLICATION_ID && userVersion === SCHEMA_VERSION) return "ready";
  if (userVersion > SCHEMA_VERSION || ![0, APPLICATION_ID].includes(applicationId)) return "incompatible";
  if (applicationId === APPLICATION_ID && userVersion === LEGACY_V4_VERSION) return "legacy-v4";
  if (tables.has("memory_records_v2")) return "legacy-v2";
  if (applicationId === APPLICATION_ID && userVersion === LEGACY_V3_VERSION) return "legacy-v3";
  return "unknown";
}

function assertSchema(facts: Facts, writable: boolean): void {
  switch (classify(facts)) {
    case "ready":
      return;
    case "legacy-v4":
      if (!writable) return;
      throw new MigrationRequired("schema v4 store must be migrated to v5 before writing");
    case "incompatible":
      throw new SchemaVersionError("database is newer than this runtime or belongs to another application");
    case "legacy-v2":
      throw new MigrationRequired("legacy v2 store must be initialized or migrated before use");
    case "legacy-v3":
      throw new MigrationRequired("schema v3 store must be initialized or migrated to v5 before use");
    default:
      throw new SchemaVersionError("memory database is not initialized");
  }
}

function verifyConnection(database: DatabaseSync, writable: boolean): void {
  const foreignKeys = database.prepare("PRAGMA foreign_keys").get() as Record<string, unknown>;
  if (Number(foreignKeys.foreign_keys) !== 1) throw new Error("SQLite foreign_keys is not enabled");
  const mode = String(
    (database.prepare("PRAGMA journal_mode").get() as Record<string, unknown>).journal_mode,
  ).toLowerCase();
  if (mode !== "delete") throw new IncompatibleJournalMode(`SQLite journal_mode=${mode}. ${REPAIR}`);
  assertSchema(inspect(database), writable);
}

function hasTable(database: DatabaseSync, name: string): boolean {
  return Boolean(database.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?").get(name));
}
function plainJson(value: JsonValue): unknown {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (Array.isArray(value)) return value.map(plainJson);
  if (value.kind === "int") return Number(value.value);
  if (value.kind === "float") return value.value;
  return Object.fromEntries(value.entries.map(([k, v]) => [k, plainJson(v)]));
}
function migrateV2(database: DatabaseSync, now: () => string): void {
  if (database.prepare("SELECT value FROM memory_meta_v3 WHERE key='migrated_from_v2'").get()) return;
  const records = database.prepare("SELECT * FROM memory_records_v2 ORDER BY record_id").all() as Row[];
  for (const r of records)
    database
      .prepare(
        "INSERT OR IGNORE INTO memory_records_v3(record_id,record_class,domain,scope,created_at,created_by) VALUES(?,?,?,?,?,?)",
      )
      .run(r.record_id, r.record_class, r.domain, r.scope, r.created_at, r.created_by);
  const revisions = database
    .prepare("SELECT * FROM memory_revisions_v2 ORDER BY record_id,revision_number")
    .all() as Row[];
  for (const r of revisions) {
    database
      .prepare(
        "INSERT OR IGNORE INTO memory_revisions_v3(revision_id,record_id,parent_revision_id,revision_number,title,summary,content,impact,confidence,valid_from,authority_status,content_sha256,created_at,created_by,surface,model_family,reason,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
      )
      .run(
        r.revision_id,
        r.record_id,
        r.parent_revision_id,
        r.revision_number,
        r.title,
        r.summary,
        r.content,
        r.impact,
        r.confidence,
        r.valid_from,
        r.authority_status,
        r.content_sha256,
        r.created_at,
        r.created_by,
        r.surface,
        r.model_family,
        r.reason,
        r.idempotency_key,
      );
    database
      .prepare(
        "INSERT OR IGNORE INTO memory_telemetry_v3(revision_id,salience,stability,accessibility,access_count,last_accessed_at,updated_at) VALUES(?,?,?,?,0,NULL,?)",
      )
      .run(r.revision_id, r.salience, r.stability, r.accessibility, r.created_at);
  }
  const evidenceMap = new Map<string, string>();
  for (const r of database.prepare("SELECT * FROM memory_evidence_v2 ORDER BY evidence_id").all() as Row[]) {
    const sourceRef = String(r.source_ref ?? ""),
      family = identitySegment(sourceRef ? sourceRef.split(":", 1)[0] : "legacy", "legacy"),
      group = identitySegment(sourceRef || "legacy", family),
      version = "evidence-v2",
      sourceSha = String(r.source_sha256),
      evidenceSha = hashPayload(
        orderedObject([
          ["identity_version", version],
          ["source_family", family],
          ["independence_group", group],
          ["source_sha256", sourceSha],
        ]),
      ),
      id = `evidence:${version}:${evidenceSha.slice(0, 32)}`;
    database
      .prepare(
        "INSERT INTO memory_evidence_v3(evidence_id,identity_version,evidence_type,source_ref,source_family,independence_group,source_sha256,evidence_sha256,captured_at,actor,surface,model_family,content_summary,confidence,privacy_class) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(identity_version,evidence_sha256) DO NOTHING",
      )
      .run(
        id,
        version,
        r.evidence_type,
        r.source_ref,
        family,
        group,
        sourceSha,
        evidenceSha,
        r.captured_at,
        r.actor,
        r.surface,
        r.model_family,
        r.content_summary,
        r.confidence,
        r.privacy_class,
      );
    const canonical = database
      .prepare("SELECT evidence_id FROM memory_evidence_v3 WHERE identity_version=? AND evidence_sha256=?")
      .get(version, evidenceSha) as Row;
    evidenceMap.set(String(r.evidence_id), String(canonical.evidence_id));
  }
  if (hasTable(database, "memory_revision_evidence_v2"))
    for (const r of database
      .prepare("SELECT * FROM memory_revision_evidence_v2 ORDER BY revision_id,evidence_id,stance")
      .all() as Row[])
      database
        .prepare(
          "INSERT OR IGNORE INTO memory_revision_evidence_v3(revision_id,evidence_id,stance,weight,reason) VALUES(?,?,?,?,?)",
        )
        .run(r.revision_id, evidenceMap.get(String(r.evidence_id))!, r.stance, r.weight, r.reason);
  for (const [oldName, newName, fields] of [
    [
      "memory_relations_v2",
      "memory_relations_v3",
      "relation_id,from_record_id,to_record_id,relation_type,weight,source_revision_id,status,created_at",
    ],
    ["memory_cues_v2", "memory_cues_v3", "cue,cue_norm,cue_type,target_record_id,weight,scope,profile"],
    [
      "memory_access_v2",
      "memory_access_v3",
      "cue_sha256,record_id,revision_id,retrieval_reason,rank,surface,created_at",
    ],
  ] as const)
    if (hasTable(database, oldName))
      database.exec(`INSERT OR IGNORE INTO ${newName}(${fields}) SELECT ${fields} FROM ${oldName}`);
  for (const [oldName, newName] of [
    ["memory_operations_v2", "memory_operations_v3"],
    ["memory_intake_v2", "memory_intake_v3"],
  ] as const)
    if (hasTable(database, oldName)) {
      const rows = database
        .prepare(
          `SELECT * FROM ${oldName} ORDER BY created_at,${oldName.includes("operations") ? "operation_id" : "intake_id"}`,
        )
        .all() as Row[];
      for (const r of rows) {
        const ids = (plainJson(parseLossless(String(r.evidence_ids_json ?? "[]"))) as string[]).map(
          (x) => evidenceMap.get(x) ?? x,
        );
        if (oldName.includes("operations"))
          database
            .prepare(
              "INSERT OR IGNORE INTO memory_operations_v3(operation_id,operation_type,actor,surface,target_record_id,target_revision_id,evidence_ids_json,decision,reason,details_json,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            )
            .run(
              r.operation_id,
              r.operation_type,
              r.actor,
              r.surface,
              r.target_record_id,
              r.target_revision_id,
              pyJsonDumps(ids),
              r.decision,
              r.reason,
              r.details_json,
              r.idempotency_key,
              r.created_at,
            );
        else
          database
            .prepare(
              "INSERT OR IGNORE INTO memory_intake_v3(intake_id,operation_type,target_record_id,proposal_sha256,evidence_ids_json,status,decision_reason,operation_id,actor,surface,idempotency_key,created_at,decided_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            )
            .run(
              r.intake_id,
              r.operation_type,
              r.target_record_id,
              r.proposal_sha256,
              pyJsonDumps(ids),
              r.status,
              r.decision_reason,
              r.operation_id,
              r.actor,
              r.surface,
              r.idempotency_key,
              r.created_at,
              r.decided_at,
            );
      }
    }
  const grouped = new Map<string, Row[]>();
  for (const r of revisions) {
    const a = grouped.get(String(r.record_id)) ?? [];
    a.push(r);
    grouped.set(String(r.record_id), a);
  }
  for (const [recordId, history] of grouped)
    for (const r of history) {
      const append = (state: string, key: string, effective: string) => {
        const sequence = Number(
            (
              database
                .prepare(
                  "SELECT COALESCE(MAX(sequence_number),0)+1 AS n FROM memory_lifecycle_events_v3 WHERE record_id=?",
                )
                .get(recordId) as Row
            ).n,
          ),
          id = `lifecycle:${createHash("sha256").update(key).digest("hex").slice(0, 32)}`;
        database
          .prepare("INSERT INTO memory_lifecycle_events_v3 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)")
          .run(
            id,
            recordId,
            r.revision_id,
            sequence,
            state,
            effective,
            "memory-core-migrator",
            "migration",
            state === "current" ? "preserve v2 semantic chronology" : "preserve v2 terminal lifecycle state",
            null,
            key,
            now(),
          );
      };
      const base = `migration:v2:${r.revision_id}`;
      append("current", `${base}:current`, String(r.valid_from ?? r.created_at));
      if (r.revision_status !== "current")
        append(String(r.revision_status), `${base}:${r.revision_status}`, String(r.valid_to ?? r.created_at));
    }
  database.prepare("INSERT INTO memory_meta_v3(key,value) VALUES('migrated_from_v2',?)").run(now());
}
function backfillRelations(database: DatabaseSync): void {
  if (!hasTable(database, "memory_relations_v3")) return;
  for (const r of database
    .prepare("SELECT * FROM memory_relations_v3 ORDER BY created_at,relation_id")
    .all() as Row[]) {
    if (
      database
        .prepare(
          "SELECT 1 FROM memory_relation_events_v4 WHERE from_record_id=? AND to_record_id=? AND relation_type=?",
        )
        .get(r.from_record_id, r.to_record_id, r.relation_type)
    )
      continue;
    const events: [string, number][] = [["assert", Number(r.weight)]];
    if (r.status !== "active") events.push(["retract", 0]);
    for (let i = 0; i < events.length; i++) {
      const [event, weight] = events[i],
        key = `migrate-v3:${r.relation_id}:${event}`;
      database
        .prepare(
          "INSERT INTO memory_relation_events_v4(relation_event_id,relation_id,from_record_id,to_record_id,relation_type,sequence_number,event_type,weight,source_revision_id,evidence_id,actor,surface,reason,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?)",
        )
        .run(
          `relation-event:${createHash("sha256").update(key).digest("hex").slice(0, 32)}`,
          r.relation_id,
          r.from_record_id,
          r.to_record_id,
          r.relation_type,
          i + 1,
          event,
          weight,
          r.source_revision_id,
          "memory-core-migration",
          "migration",
          "carried from mutable v3 relation row",
          key,
          r.created_at,
        );
    }
  }
}

export class MemoryStore {
  readonly path: string;
  readonly pinnedGuard: ReadonlySet<string>;
  readonly clock: () => string;
  #database: DatabaseSync | null = null;
  #bootstrapDepth = 0;

  constructor(path: string, options: { pinnedGuard?: readonly string[]; now?: () => string } = {}) {
    this.path = resolve(path);
    this.pinnedGuard = new Set(options.pinnedGuard ?? []);
    this.clock = options.now ?? utcNowSeconds;
  }

  exists(): boolean {
    return existsSync(this.path);
  }

  guardPinned(recordId: string): void {
    if (this.#bootstrapDepth === 0 && this.pinnedGuard.has(recordId))
      throw new PinnedRecordError(`public writer refuses pinned record_id '${recordId}'`);
  }

  now(): string {
    return this.clock();
  }
  requireWritable(allowUninitialized = false): void {
    const state = this.schemaInfo().state;
    if (state === "ready" || (allowUninitialized && state === "uninitialized")) return;
    if (state === "legacy-v4") throw new MigrationRequired("schema v4 store must be migrated to v5 before writing");
    if (state === "legacy-v2" || state === "legacy-v3")
      throw new MigrationRequired(`${state} store requires migration`);
    if (state === "incompatible")
      throw new SchemaVersionError("database application_id/user_version is newer or foreign");
    throw new SchemaVersionError(`store state '${state}' is not writable`);
  }
  bootstrapWrites<T>(callback: () => T): T {
    this.#bootstrapDepth++;
    try {
      return callback();
    } finally {
      this.#bootstrapDepth--;
    }
  }

  createCurrent(input: CreateInput | string): Record<string, unknown> {
    if (typeof input === "string") {
      this.requireWritable();
      this.guardPinned(input);
      throw new Error("createCurrent requires writer input");
    }
    return createCurrent(this, input);
  }
  revise(input: ReviseInput | string): Record<string, unknown> {
    if (typeof input === "string") {
      this.requireWritable();
      this.guardPinned(input);
      throw new Error("revise requires writer input");
    }
    return revise(this, input);
  }
  invalidate(
    input:
      | {
          recordId: string;
          actor: string;
          reason: string;
          evidence: Record<string, unknown>;
          idempotencyKey: string;
          surface?: string;
        }
      | string,
  ): Record<string, unknown> {
    if (typeof input === "string") {
      this.requireWritable();
      this.guardPinned(input);
      throw new Error("invalidate requires writer input");
    }
    return invalidate(this, input);
  }
  addCue(input: {
    profile: string;
    cue: string;
    targetRecordId: string;
    weight?: Numeric;
    cueType?: string;
    scope?: string;
  }): null {
    return addCue(this, input);
  }
  addRelation(input: RelationInput): Record<string, unknown> {
    return addRelation(this, input);
  }
  retractRelation(input: RelationInput): Record<string, unknown> {
    return retractRelation(this, input);
  }
  recordAccess(input: {
    cue: string;
    recordId: string;
    revisionId: string;
    retrievalReason: string;
    rank: number;
    surface: string;
    gain?: Numeric;
  }): null {
    return recordAccess(this, input);
  }
  applyMaintenance(input: {
    runId: string;
    adjustments: Adjustment[];
    actor: string;
    reason: string;
    surface?: string;
    idempotencyKey?: string;
  }): Record<string, unknown> {
    return applyMaintenance(this, input);
  }

  open(): DatabaseSync | null {
    if (this.#database) return this.#database;
    if (!this.exists()) return null;
    const header = readHeader(this.path);
    if (header.empty) throw new SchemaVersionError("memory database is not initialized");
    if (header.userVersion > SCHEMA_VERSION || ![0, APPLICATION_ID].includes(header.applicationId)) {
      throw new SchemaVersionError("database is newer than this runtime or belongs to another application");
    }
    if (header.applicationId === APPLICATION_ID && header.userVersion === LEGACY_V3_VERSION) {
      throw new MigrationRequired("schema v3 store must be initialized or migrated to v5 before use");
    }
    hooks.beforeOpen?.(this.path);
    const database = new DatabaseSync(this.path, { readOnly: true, enableForeignKeyConstraints: true, timeout: 5000 });
    try {
      verifyConnection(database, false);
      this.#database = database;
      return database;
    } catch (error) {
      database.close();
      throw error;
    }
  }

  close(): void {
    this.#database?.close();
    this.#database = null;
  }

  initialize(): {
    application_id: number;
    user_version: number;
    state: string;
    changed: boolean;
    migrated_from: number | null;
  } {
    this.close();
    if (this.exists() && statSync(this.path).size > 0) {
      const header = readHeader(this.path);
      if (header.applicationId === APPLICATION_ID && header.userVersion === LEGACY_V4_VERSION) {
        throw new MigrationRequired("schema v4 store must be migrated to v5 before writing");
      }
      if (header.userVersion > SCHEMA_VERSION || ![0, APPLICATION_ID].includes(header.applicationId)) {
        throw new SchemaVersionError("database application_id/user_version is newer or foreign");
      }
      const current = this.schemaInfo();
      if (current.state === "ready") return { ...current, changed: false, migrated_from: null };
    }
    mkdirSync(dirname(this.path), { recursive: true });
    const database = new DatabaseSync(this.path, { enableForeignKeyConstraints: true, timeout: 5000 });
    try {
      database.exec(readFileSync(SCHEMA_PATH, "utf8"));
      database.exec(
        `INSERT INTO memory_meta_v3(key,value) VALUES('schema_version','5') ON CONFLICT(key) DO UPDATE SET value=excluded.value; PRAGMA application_id=${APPLICATION_ID}; PRAGMA user_version=${SCHEMA_VERSION};`,
      );
      const mode = String(
        (database.prepare("PRAGMA journal_mode=DELETE").get() as Record<string, unknown>).journal_mode,
      ).toLowerCase();
      if (mode !== "delete") throw new IncompatibleJournalMode(`SQLite refused journal_mode=DELETE. ${REPAIR}`);
    } finally {
      database.close();
    }
    return { ...this.schemaInfo(), changed: true, migrated_from: null };
  }

  transaction<T>(callback: (database: DatabaseSync) => T): T {
    this.initialize();
    this.close();
    readHeader(this.path);
    hooks.beforeOpen?.(this.path);
    const database = new DatabaseSync(this.path, { enableForeignKeyConstraints: true, timeout: 5000 });
    try {
      verifyConnection(database, true);
      database.exec("BEGIN IMMEDIATE");
      try {
        const result = callback(database);
        database.exec("COMMIT");
        return result;
      } catch (error) {
        database.exec("ROLLBACK");
        throw error;
      }
    } finally {
      database.close();
    }
  }

  migrateTo(
    target: string,
    options: { dryRun?: boolean; backupPath?: string } = {},
  ): MemoryStore | Record<string, unknown> {
    this.close();
    if (existsSync(target)) throw new FileExistsError(target);
    if (!this.exists()) {
      if (options.dryRun) return { state: "ready", from: "uninitialized", dry_run: true };
      const empty = new MemoryStore(target, { pinnedGuard: [...this.pinnedGuard], now: this.clock });
      empty.initialize();
      return empty;
    }
    const before = this.schemaInfo();
    if (!["legacy-v2", "legacy-v3", "legacy-v4"].includes(before.state))
      throw new MigrationRequired(`store state '${before.state}' is not an explicit migration source`);
    const version = before.state.slice(-1),
      backup = options.backupPath ?? `${target}.v${version}.bak`;
    const normcase = (path: string) => (process.platform === "win32" ? resolve(path).toLowerCase() : resolve(path));
    if (!options.dryRun) {
      if (normcase(backup) === normcase(target)) throw new ValueError("backup path must differ from target");
      if (existsSync(backup)) throw new FileExistsError(backup);
      mkdirSync(dirname(resolve(target)), { recursive: true });
      const sourceStat = statSync(this.path);
      copyFileSync(this.path, backup);
      utimesSync(backup, sourceStat.atime, sourceStat.mtime);
      // Outside cleanup: an aliased target must never cause backup deletion.
      if (existsSync(target)) throw new FileExistsError(target);
    }
    if (options.dryRun) mkdirSync(dirname(resolve(target)), { recursive: true });
    const rehearsal = options.dryRun ? mkdtempSync(resolve(dirname(target), "trajecta-migrate-dry-run-")) : null;
    const work = rehearsal ? resolve(rehearsal, "store.sqlite3") : target;
    try {
      copyFileSync(this.path, work);
      readHeader(work);
      const database = new DatabaseSync(work, { enableForeignKeyConstraints: true, timeout: 5000 });
      try {
        database.exec(readFileSync(SCHEMA_PATH, "utf8"));
        if (before.state === "legacy-v2") migrateV2(database, this.clock);
        backfillRelations(database);
        database
          .prepare(
            "INSERT INTO memory_meta_v3(key,value) VALUES('schema_version','5') ON CONFLICT(key) DO UPDATE SET value=excluded.value",
          )
          .run();
        database.exec(`PRAGMA application_id=${APPLICATION_ID}; PRAGMA user_version=${SCHEMA_VERSION};`);
        const mode = String((database.prepare("PRAGMA journal_mode=DELETE").get() as Row).journal_mode).toLowerCase();
        if (mode !== "delete") throw new IncompatibleJournalMode(`SQLite refused journal_mode=DELETE. ${REPAIR}`);
      } finally {
        database.close();
      }
      const migrated = new MemoryStore(work, { pinnedGuard: [...this.pinnedGuard], now: this.clock });
      if (migrated.schemaInfo().state !== "ready") throw new Error("migration copy did not reach schema v5");
      if (options.dryRun) {
        unlinkSync(work);
        return { state: "ready", from: before.state, dry_run: true };
      }
      return migrated;
    } catch (error) {
      if (existsSync(work)) unlinkSync(work);
      throw error;
    } finally {
      if (rehearsal) rmSync(rehearsal, { recursive: true, force: true });
    }
  }
  migrateV4To(target: string, backup: string): MemoryStore {
    return this.migrateTo(target, { backupPath: backup }) as MemoryStore;
  }

  #db(): DatabaseSync | null {
    return this.#database ?? this.open();
  }
  all(sql: string, ...params: (string | number | bigint)[]): Row[] {
    const database = this.#db();
    if (!database) return [];
    return (database.prepare(sql).all(...params) as Row[]).map((row) => Object.fromEntries(Object.entries(row)) as Row);
  }

  currentView(recordId?: string): Row[] {
    return recordId
      ? this.all("SELECT * FROM memory_current_v3 WHERE record_id=?", recordId)
      : this.all("SELECT * FROM memory_current_v3 ORDER BY domain,record_id");
  }
  historicalView(recordId: string): Row[] {
    return this.all("SELECT * FROM memory_revision_state_v3 WHERE record_id=? ORDER BY revision_number", recordId);
  }
  evidenceForRevision(revisionId: string): Row[] {
    return this.all(
      "SELECT e.*,l.stance,l.weight,l.reason AS link_reason FROM memory_revision_evidence_v3 l JOIN memory_evidence_v3 e ON e.evidence_id=l.evidence_id WHERE l.revision_id=? ORDER BY e.captured_at,e.evidence_id,l.stance",
      revisionId,
    );
  }
  cueRows(profile: string, scope: string): Row[] {
    return this.all(
      "SELECT * FROM memory_cues_v3 WHERE profile=? AND scope IN ('global',?) ORDER BY weight DESC,cue_id",
      profile,
      scope,
    );
  }
  activeRelationRows(): Row[] {
    return this.all("SELECT * FROM memory_relation_current_v4 ORDER BY relation_id");
  }
  relationHistory(from: string, to: string, type: string): Row[] {
    return this.all(
      "SELECT * FROM memory_relation_events_v4 WHERE from_record_id=? AND to_record_id=? AND relation_type=? ORDER BY sequence_number",
      from,
      to,
      type,
    );
  }

  schemaInfo(): { application_id: number; user_version: number; state: string } {
    if (!this.exists()) return { application_id: 0, user_version: 0, state: "uninitialized" };
    readHeader(this.path);
    hooks.beforeOpen?.(this.path);
    const database = new DatabaseSync(this.path, { readOnly: true, timeout: 5000 });
    try {
      const facts = inspect(database);
      const mode = String(
        (database.prepare("PRAGMA journal_mode").get() as Record<string, unknown>).journal_mode,
      ).toLowerCase();
      if (mode !== "delete") throw new IncompatibleJournalMode(`SQLite journal_mode=${mode}. ${REPAIR}`);
      return { application_id: facts.applicationId, user_version: facts.userVersion, state: classify(facts) };
    } finally {
      database.close();
    }
  }
}
