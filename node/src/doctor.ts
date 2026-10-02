import { MemoryStore, SCHEMA_VERSION } from "./store.ts";

export function doctor(store: MemoryStore): Record<string, unknown> {
  const info = store.schemaInfo();
  const state = info.state;
  const result: Record<string, unknown> = { schema: "memory-core-doctor/v2", state, passed: false };
  if (state === "uninitialized") return { ...result, state: "missing", action: "init" };
  if (state.startsWith("legacy-v")) return { ...result, action: "migrate-to" };
  if (state === "incompatible") store.all("SELECT 1"); // Existing fail-closed schema assertion.
  if (state !== "ready") return result;
  const foreignKeys = store.all("PRAGMA foreign_key_check");
  const checks = {
    integrity_ok: store.all("PRAGMA integrity_check")[0]?.integrity_check === "ok",
    foreign_keys_ok: foreignKeys.length === 0,
    one_current_revision_per_record:
      store.all("SELECT record_id,COUNT(*) AS count FROM memory_current_v3 GROUP BY record_id HAVING count>1")
        .length === 0,
    no_orphan_current_revision:
      store.all(
        "SELECT v.revision_id FROM memory_current_v3 v " +
          "LEFT JOIN memory_records_v3 r ON r.record_id=v.record_id WHERE r.record_id IS NULL",
      ).length === 0,
    relations_carried_to_event_stream:
      store.all(
        "SELECT r.relation_id FROM memory_relations_v3 r WHERE NOT EXISTS " +
          "(SELECT 1 FROM memory_relation_events_v4 e WHERE e.from_record_id=r.from_record_id " +
          "AND e.to_record_id=r.to_record_id AND e.relation_type=r.relation_type)",
      ).length === 0,
    schema_application_id: store.schemaInfo().application_id !== 0,
    schema_version_current: store.schemaInfo().user_version === SCHEMA_VERSION,
  };
  return { ...result, passed: Object.values(checks).every(Boolean), checks, foreign_key_errors: foreignKeys };
}
