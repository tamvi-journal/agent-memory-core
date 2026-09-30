import { closeSync, copyFileSync, existsSync, mkdirSync, openSync, readFileSync, readSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { fileURLToPath } from "node:url";
import { IncompatibleJournalMode, MigrationRequired, PinnedRecordError, SchemaVersionError } from "./errors.ts";
import { hooks } from "./internal-hooks.ts";

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
    if (readSync(descriptor, header, 0, 100, 0) !== 100) throw new SchemaVersionError("memory database is not initialized");
  } finally { closeSync(descriptor); }
  if (!header.subarray(0, 16).equals(Buffer.from("SQLite format 3\0", "binary"))) {
    throw new SchemaVersionError("memory database is not initialized");
  }
  const wal = header[18] === 2 || header[19] === 2;
  if (wal) throw new IncompatibleJournalMode(`SQLite journal_mode=wal. ${REPAIR}`);
  return { applicationId: header.readInt32BE(68), userVersion: header.readInt32BE(60), wal, empty: false };
}

function inspect(database: DatabaseSync): Facts {
  const pragma = (name: string): number => Number((database.prepare(`PRAGMA ${name}`).get() as Record<string, unknown>)[name]);
  const tables = new Set((database.prepare("SELECT name FROM sqlite_master WHERE type='table'").all() as Row[]).map((row) => String(row.name)));
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
    case "ready": return;
    case "legacy-v4":
      if (!writable) return;
      throw new MigrationRequired("schema v4 store must be migrated to v5 before writing");
    case "incompatible": throw new SchemaVersionError("database is newer than this runtime or belongs to another application");
    case "legacy-v2": throw new MigrationRequired("legacy v2 store must be initialized or migrated before use");
    case "legacy-v3": throw new MigrationRequired("schema v3 store must be initialized or migrated to v5 before use");
    default: throw new SchemaVersionError("memory database is not initialized");
  }
}

function verifyConnection(database: DatabaseSync, writable: boolean): void {
  const foreignKeys = database.prepare("PRAGMA foreign_keys").get() as Record<string, unknown>;
  if (Number(foreignKeys.foreign_keys) !== 1) throw new Error("SQLite foreign_keys is not enabled");
  const mode = String((database.prepare("PRAGMA journal_mode").get() as Record<string, unknown>).journal_mode).toLowerCase();
  if (mode !== "delete") throw new IncompatibleJournalMode(`SQLite journal_mode=${mode}. ${REPAIR}`);
  assertSchema(inspect(database), writable);
}

export class MemoryStore {
  readonly path: string;
  readonly pinnedGuard: ReadonlySet<string>;
  #database: DatabaseSync | null = null;

  constructor(path: string, options: { pinnedGuard?: readonly string[] } = {}) {
    this.path = resolve(path);
    this.pinnedGuard = new Set(options.pinnedGuard ?? []);
  }

  exists(): boolean { return existsSync(this.path); }

  guardPinned(recordId: string): void {
    if (this.pinnedGuard.has(recordId)) throw new PinnedRecordError(`public writer refuses pinned record_id '${recordId}'`);
  }

  createCurrent(recordId: string): never { this.guardPinned(recordId); throw new Error("createCurrent is reserved for R2b"); }
  revise(recordId: string): never { this.guardPinned(recordId); throw new Error("revise is reserved for R2b"); }
  invalidate(recordId: string): never { this.guardPinned(recordId); throw new Error("invalidate is reserved for R2b"); }

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
    } catch (error) { database.close(); throw error; }
  }

  close(): void { this.#database?.close(); this.#database = null; }

  initialize(): { application_id: number; user_version: number; state: string; changed: boolean } {
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
      if (current.state === "ready") return { ...current, changed: false };
    }
    mkdirSync(dirname(this.path), { recursive: true });
    const database = new DatabaseSync(this.path, { enableForeignKeyConstraints: true, timeout: 5000 });
    try {
      database.exec(readFileSync(SCHEMA_PATH, "utf8"));
      database.exec(`INSERT INTO memory_meta_v3(key,value) VALUES('schema_version','5') ON CONFLICT(key) DO UPDATE SET value=excluded.value; PRAGMA application_id=${APPLICATION_ID}; PRAGMA user_version=${SCHEMA_VERSION};`);
      const mode = String((database.prepare("PRAGMA journal_mode=DELETE").get() as Record<string, unknown>).journal_mode).toLowerCase();
      if (mode !== "delete") throw new IncompatibleJournalMode(`SQLite refused journal_mode=DELETE. ${REPAIR}`);
    } finally { database.close(); }
    return { ...this.schemaInfo(), changed: true };
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
    } finally { database.close(); }
  }

  migrateV4To(target: string, backup: string): MemoryStore {
    const facts = this.schemaInfo();
    if (facts.state !== "legacy-v4") throw new MigrationRequired(`store state '${facts.state}' is not a v4 migration source`);
    if (existsSync(target) || existsSync(backup)) throw new Error("migration target and backup must not exist");
    mkdirSync(dirname(resolve(target)), { recursive: true });
    copyFileSync(this.path, backup);
    copyFileSync(this.path, target);
    readHeader(target);
    const database = new DatabaseSync(target, { enableForeignKeyConstraints: true, timeout: 5000 });
    try {
      database.exec(readFileSync(SCHEMA_PATH, "utf8"));
      database.exec(`INSERT INTO memory_meta_v3(key,value) VALUES('schema_version','5') ON CONFLICT(key) DO UPDATE SET value=excluded.value; PRAGMA application_id=${APPLICATION_ID}; PRAGMA user_version=${SCHEMA_VERSION};`);
      const mode = String((database.prepare("PRAGMA journal_mode=DELETE").get() as Record<string, unknown>).journal_mode).toLowerCase();
      if (mode !== "delete") throw new IncompatibleJournalMode(`SQLite refused journal_mode=DELETE. ${REPAIR}`);
    } finally { database.close(); }
    return new MemoryStore(target, { pinnedGuard: [...this.pinnedGuard] });
  }

  #db(): DatabaseSync | null { return this.#database ?? this.open(); }
  all(sql: string, ...params: (string | number | bigint)[]): Row[] {
    const database = this.#db();
    if (!database) return [];
    return (database.prepare(sql).all(...params) as Row[]).map((row) => Object.fromEntries(Object.entries(row)) as Row);
  }

  currentView(recordId?: string): Row[] {
    return recordId ? this.all("SELECT * FROM memory_current_v3 WHERE record_id=?", recordId) : this.all("SELECT * FROM memory_current_v3 ORDER BY domain,record_id");
  }
  historicalView(recordId: string): Row[] { return this.all("SELECT * FROM memory_revision_state_v3 WHERE record_id=? ORDER BY revision_number", recordId); }
  evidenceForRevision(revisionId: string): Row[] {
    return this.all("SELECT e.*,l.stance,l.weight,l.reason AS link_reason FROM memory_revision_evidence_v3 l JOIN memory_evidence_v3 e ON e.evidence_id=l.evidence_id WHERE l.revision_id=? ORDER BY e.captured_at,e.evidence_id,l.stance", revisionId);
  }
  cueRows(profile: string, scope: string): Row[] { return this.all("SELECT * FROM memory_cues_v3 WHERE profile=? AND scope IN ('global',?) ORDER BY weight DESC,cue_id", profile, scope); }
  activeRelationRows(): Row[] { return this.all("SELECT * FROM memory_relation_current_v4 ORDER BY relation_id"); }
  relationHistory(from: string, to: string, type: string): Row[] { return this.all("SELECT * FROM memory_relation_events_v4 WHERE from_record_id=? AND to_record_id=? AND relation_type=? ORDER BY sequence_number", from, to, type); }

  schemaInfo(): { application_id: number; user_version: number; state: string } {
    if (!this.exists()) return { application_id: 0, user_version: 0, state: "uninitialized" };
    readHeader(this.path);
    hooks.beforeOpen?.(this.path);
    const database = new DatabaseSync(this.path, { readOnly: true, timeout: 5000 });
    try {
      const facts = inspect(database);
      const mode = String((database.prepare("PRAGMA journal_mode").get() as Record<string, unknown>).journal_mode).toLowerCase();
      if (mode !== "delete") throw new IncompatibleJournalMode(`SQLite journal_mode=${mode}. ${REPAIR}`);
      return { application_id: facts.applicationId, user_version: facts.userVersion, state: classify(facts) };
    } finally { database.close(); }
  }
}
