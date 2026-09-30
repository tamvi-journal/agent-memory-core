import { closeSync, existsSync, openSync, readSync, statSync } from "node:fs";
import { resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { IncompatibleJournalMode, MigrationRequired, SchemaVersionError } from "./errors.ts";
import { hooks } from "./internal-hooks.ts";

export const APPLICATION_ID = 0x414d4333;
export const SCHEMA_VERSION = 4;
export const LEGACY_V3_VERSION = 3;
const REPAIR = "Repair with: sqlite3 <store.sqlite3> 'PRAGMA journal_mode=DELETE;'";

export type Row = Record<string, string | number | null>;

type Header = { applicationId: number; userVersion: number; wal: boolean; empty: boolean };

/**
 * Decide from the file alone, before SQLite opens it (a read-only open of a
 * WAL database can leave -wal/-shm sidecars behind). Mirrors the Python
 * `_preflight(readonly=True)`: only the journal mode is refused here; the
 * ownership and version checks belong to the caller, as in Python.
 */
function readHeader(path: string): Header {
  const absolute = resolve(path);
  for (const suffix of ["-wal", "-shm"]) {
    if (existsSync(absolute + suffix)) throw new IncompatibleJournalMode(`SQLite WAL sidecar present. ${REPAIR}`);
  }
  const size = statSync(absolute).size;
  // An empty file is an empty SQLite database (application_id 0, user_version 0).
  if (size === 0) return { applicationId: 0, userVersion: 0, wal: false, empty: true };
  if (size < 100) throw new SchemaVersionError("memory database is not initialized");
  const descriptor = openSync(absolute, "r");
  const header = Buffer.alloc(100);
  try {
    if (readSync(descriptor, header, 0, 100, 0) !== 100) throw new SchemaVersionError("memory database is not initialized");
  } finally {
    closeSync(descriptor);
  }
  if (!header.subarray(0, 16).equals(Buffer.from("SQLite format 3\0", "binary"))) {
    throw new SchemaVersionError("memory database is not initialized");
  }
  const wal = header[18] === 2 || header[19] === 2;
  if (wal) throw new IncompatibleJournalMode(`SQLite journal_mode=wal. ${REPAIR}`);
  // PRAGMA user_version and application_id are signed 32-bit, as in Python.
  return { applicationId: header.readInt32BE(68), userVersion: header.readInt32BE(60), wal, empty: false };
}

/** The read path's schema assertion (Python `_assert_schema`), from the header. */
function preflight(path: string): Header {
  const header = readHeader(path);
  if (header.empty) throw new SchemaVersionError("memory database is not initialized");
  const { applicationId, userVersion } = header;
  if (userVersion > SCHEMA_VERSION || ![0, APPLICATION_ID].includes(applicationId)) {
    throw new SchemaVersionError("database is newer than this runtime or belongs to another application");
  }
  if (applicationId === APPLICATION_ID && userVersion === LEGACY_V3_VERSION) {
    throw new MigrationRequired("schema v3 store must be initialized or migrated to v4 before use");
  }
  return header;
}

type Facts = { applicationId: number; userVersion: number; tables: Set<unknown> };

/** PRAGMA application_id / user_version and table names, from an open database. */
function inspect(database: DatabaseSync): Facts {
  const pragma = (name: string): number => Number((database.prepare(`PRAGMA ${name}`).get() as Record<string, unknown>)[name]);
  const tables = new Set((database.prepare("SELECT name FROM sqlite_master WHERE type='table'").all() as Row[]).map((row) => row.name));
  return { applicationId: pragma("application_id"), userVersion: pragma("user_version"), tables };
}

/** Python `schema_info` state. */
export function classify({ applicationId, userVersion, tables }: Facts): string {
  if (applicationId === APPLICATION_ID && userVersion === SCHEMA_VERSION) return "ready";
  if (userVersion > SCHEMA_VERSION || ![0, APPLICATION_ID].includes(applicationId)) return "incompatible";
  if (tables.has("memory_records_v2")) return "legacy-v2";
  if (applicationId === APPLICATION_ID && userVersion === LEGACY_V3_VERSION) return "legacy-v3";
  return "unknown";
}

/** Python `_assert_schema`, with the corpus messages. */
function assertSchema(facts: Facts): void {
  switch (classify(facts)) {
    case "ready": return;
    case "incompatible": throw new SchemaVersionError("database is newer than this runtime or belongs to another application");
    case "legacy-v2": throw new MigrationRequired("legacy v2 store must be initialized or migrated before use");
    case "legacy-v3": throw new MigrationRequired("schema v3 store must be initialized or migrated to v4 before use");
    default: throw new SchemaVersionError("memory database is not initialized");
  }
}

export class MemoryStore {
  readonly path: string;
  #database: DatabaseSync | null = null;

  constructor(path: string) { this.path = resolve(path); }

  exists(): boolean { return existsSync(this.path); }

  open(): DatabaseSync | null {
    if (this.#database) return this.#database;
    if (!this.exists()) return null;
    // Stage 1, side-effect free: refuse WAL, foreign and future files from
    // the header before SQLite touches the file.
    preflight(this.path);
    hooks.beforeOpen?.(this.path);
    const database = new DatabaseSync(this.path, { readOnly: true, enableForeignKeyConstraints: true, timeout: 5000 });
    try {
      const foreignKeys = database.prepare("PRAGMA foreign_keys").get() as Record<string, unknown>;
      if (Number(foreignKeys.foreign_keys) !== 1) throw new Error("SQLite foreign_keys is not enabled");
      const mode = String((database.prepare("PRAGMA journal_mode").get() as Record<string, unknown>).journal_mode).toLowerCase();
      if (mode !== "delete") throw new IncompatibleJournalMode(`SQLite journal_mode=${mode}. ${REPAIR}`);
      // Stage 2, authoritative (Python `_assert_schema`): classify from the
      // database actually opened, not from the header read before the open.
      assertSchema(inspect(database));
      this.#database = database;
      return database;
    } catch (error) {
      database.close();
      throw error;
    }
  }

  close(): void { this.#database?.close(); this.#database = null; }

  #db(): DatabaseSync | null { return this.#database ?? this.open(); }

  #all(sql: string, ...params: (string | number)[]): Row[] {
    const database = this.#db();
    if (!database) return [];
    return (database.prepare(sql).all(...params) as Row[]).map((row) => Object.fromEntries(Object.entries(row)) as Row);
  }

  currentView(recordId?: string): Row[] {
    return recordId
      ? this.#all("SELECT * FROM memory_current_v3 WHERE record_id=?", recordId)
      : this.#all("SELECT * FROM memory_current_v3 ORDER BY domain,record_id");
  }

  historicalView(recordId: string): Row[] {
    return this.#all("SELECT * FROM memory_revision_state_v3 WHERE record_id=? ORDER BY revision_number", recordId);
  }

  evidenceForRevision(revisionId: string): Row[] {
    return this.#all(
      "SELECT e.*,l.stance,l.weight,l.reason AS link_reason FROM memory_revision_evidence_v3 l " +
      "JOIN memory_evidence_v3 e ON e.evidence_id=l.evidence_id WHERE l.revision_id=? " +
      "ORDER BY e.captured_at,e.evidence_id,l.stance", revisionId,
    );
  }

  cueRows(profile: string, scope: string): Row[] {
    return this.#all("SELECT * FROM memory_cues_v3 WHERE profile=? AND scope IN ('global',?) ORDER BY weight DESC,cue_id", profile, scope);
  }

  activeRelationRows(): Row[] {
    return this.#all("SELECT * FROM memory_relation_current_v4 ORDER BY relation_id");
  }

  relationHistory(from: string, to: string, type: string): Row[] {
    return this.#all(
      "SELECT * FROM memory_relation_events_v4 WHERE from_record_id=? AND to_record_id=? AND relation_type=? ORDER BY sequence_number",
      from, to, type,
    );
  }

  /**
   * Mirrors Python `schema_info`: reports the state (ready, incompatible,
   * legacy-v2, legacy-v3, unknown, uninitialized) instead of refusing, so
   * `status()` can describe a store it will not read. Only a WAL header or
   * sidecar is refused, before any open.
   */
  schemaInfo(): { application_id: number; user_version: number; state: string } {
    if (!this.exists()) return { application_id: 0, user_version: 0, state: "uninitialized" };
    readHeader(this.path);
    hooks.beforeOpen?.(this.path);
    const database = new DatabaseSync(this.path, { readOnly: true, timeout: 5000 });
    try {
      const facts = inspect(database);
      return { application_id: facts.applicationId, user_version: facts.userVersion, state: classify(facts) };
    } finally {
      database.close();
    }
  }
}
