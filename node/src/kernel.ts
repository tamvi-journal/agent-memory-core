import { RuntimeError, ValueError } from "./errors.ts";
import { createHash } from "node:crypto";
import type { DatabaseSync } from "node:sqlite";
import {
  canonicalJson,
  hashPayload,
  orderedObject,
  pyFloat,
  pyInt,
  pyJsonDumps,
  toJsonValue,
  type JsonValue,
  type PyFloat,
  type PyInt,
} from "./encoding.ts";
import { parseLossless } from "./json.ts";
import { normalizeText } from "./text.ts";
import { hooks } from "./internal-hooks.ts";
import { canonicalEvidenceIdentity } from "./evidence-identity.ts";
import type { MemoryStore, Row } from "./store.ts";

export type Numeric = number | PyInt | PyFloat;
export type Evidence = Record<string, unknown>;
const sha256 = (text: string) => createHash("sha256").update(text, "utf8").digest("hex");
const numberOf = (value: Numeric | undefined, fallback = 0): number =>
  value === undefined
    ? fallback
    : typeof value === "number"
      ? value
      : value.kind === "int"
        ? Number(value.value)
        : value.value;
const clamp = (value: number) => Math.max(0, Math.min(1, value));
const row = (db: DatabaseSync, sql: string, ...args: (string | number | bigint)[]) =>
  db.prepare(sql).get(...args) as Row | undefined;
const rows = (db: DatabaseSync, sql: string, ...args: (string | number | bigint)[]) =>
  db.prepare(sql).all(...args) as Row[];

export function semanticHash(value: Record<string, unknown>): string {
  return hashPayload(
    orderedObject([
      ["title", String(value.title ?? "")],
      ["summary", String(value.summary ?? "")],
      ["content", String(value.content ?? "")],
      ["impact", String(value.impact ?? "")],
      ["confidence", pyFloat(numberOf(value.confidence as Numeric | undefined, 0.7))],
      ["authority_status", String(value.authority_status ?? "canonical_reference")],
    ]),
  );
}

export function insertEvidence(store: MemoryStore, db: DatabaseSync, source: Evidence): string {
  for (const field of [
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
  ]) {
    if (Object.hasOwn(source, field) && typeof source[field] !== "string")
      throw new ValueError(`evidence.${field} must be a string`);
  }
  // Python stores evidence.get("source_ref", "") as given (unstripped).
  const sourceRef = String(source.source_ref ?? "");
  const {
    identity_version: version,
    source_family: family,
    independence_group: group,
    source_sha256: sourceSha,
    evidence_sha256: evidenceSha,
  } = canonicalEvidenceIdentity(source);
  const evidenceId = `evidence:${version}:${evidenceSha.slice(0, 32)}`;
  db.prepare(
    "INSERT INTO memory_evidence_v3(evidence_id,identity_version,evidence_type,source_ref,source_family,independence_group,source_sha256,evidence_sha256,captured_at,actor,surface,model_family,content_summary,confidence,privacy_class) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(identity_version,evidence_sha256) DO NOTHING",
  ).run(
    evidenceId,
    version,
    String(source.evidence_type ?? "observation"),
    sourceRef,
    family,
    group,
    sourceSha,
    evidenceSha,
    String(source.captured_at || store.now()), // Python: evidence.get("captured_at") or utc_now()
    String(source.actor ?? ""),
    String(source.surface ?? ""),
    String(source.model_family ?? ""),
    String(source.content_summary ?? ""),
    clamp(numberOf(source.confidence as Numeric | undefined, 0.7)),
    String(source.privacy_class ?? "private"),
  );
  return evidenceId;
}

export function linkEvidence(
  db: DatabaseSync,
  revisionId: string,
  evidenceId: string,
  stance: string,
  reason: string,
): void {
  db.prepare(
    "INSERT INTO memory_revision_evidence_v3(revision_id,evidence_id,stance,weight,reason) VALUES(?,?,?,1.0,?) ON CONFLICT DO NOTHING",
  ).run(revisionId, evidenceId, stance, reason);
}

function operationResult(db: DatabaseSync, operation: Row): Record<string, unknown> {
  const result: Record<string, unknown> = { ...operation };
  result.evidence_ids = plain(parseLossless(String(result.evidence_ids_json ?? "[]")));
  delete result.evidence_ids_json;
  result.details = plain(parseLossless(String(result.details_json ?? "{}")));
  delete result.details_json;
  result.revision = result.target_revision_id
    ? (row(db, "SELECT * FROM memory_revision_state_v3 WHERE revision_id=?", String(result.target_revision_id)) ?? null)
    : null;
  return result;
}
function plain(value: JsonValue): unknown {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (Array.isArray(value)) return value.map(plain);
  if (value.kind === "int") return Number(value.value);
  if (value.kind === "float") return value.value;
  return Object.fromEntries(value.entries.map(([k, v]) => [k, plain(v)]));
}

function insertOperation(
  store: MemoryStore,
  db: DatabaseSync,
  input: {
    type: string;
    actor: string;
    surface: string;
    recordId: string | null;
    revisionId: string | null;
    evidenceIds: string[];
    decision: string;
    reason: string;
    details: JsonValue;
    key: string;
  },
): Row {
  const id = `operation:${sha256(input.key).slice(0, 32)}`;
  db.prepare(
    "INSERT INTO memory_operations_v3(operation_id,operation_type,actor,surface,target_record_id,target_revision_id,evidence_ids_json,decision,reason,details_json,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
  ).run(
    id,
    input.type,
    input.actor,
    input.surface,
    input.recordId,
    input.revisionId,
    pyJsonDumps(input.evidenceIds),
    input.decision,
    input.reason,
    pyJsonDumps(input.details, true),
    input.key,
    store.now(),
  );
  return row(db, "SELECT * FROM memory_operations_v3 WHERE operation_id=?", id)!;
}

function lifecycle(
  store: MemoryStore,
  db: DatabaseSync,
  input: {
    recordId: string;
    revisionId: string;
    state: string;
    actor: string;
    surface: string;
    reason: string;
    operationId: string | null;
    key: string;
    effective: string;
  },
): void {
  const sequence = Number(
    row(
      db,
      "SELECT COALESCE(MAX(sequence_number),0)+1 AS n FROM memory_lifecycle_events_v3 WHERE record_id=?",
      input.recordId,
    )!.n,
  );
  db.prepare(
    "INSERT INTO memory_lifecycle_events_v3(lifecycle_event_id,record_id,revision_id,sequence_number,lifecycle_state,effective_at,actor,surface,reason,operation_id,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
  ).run(
    `lifecycle:${sha256(input.key).slice(0, 32)}`,
    input.recordId,
    input.revisionId,
    sequence,
    input.state,
    input.effective,
    input.actor,
    input.surface,
    input.reason,
    input.operationId,
    input.key,
    store.now(),
  );
}

export type CreateInput = {
  recordId: string;
  recordClass: string;
  domain: string;
  title: string;
  summary?: string;
  content?: string;
  impact?: string;
  confidence?: Numeric;
  salience?: Numeric;
  stability?: Numeric;
  accessibility?: Numeric;
  authorityStatus?: string;
  scope?: string;
  actor: string;
  surface?: string;
  modelFamily?: string;
  reason: string;
  evidence: Evidence;
  idempotencyKey: string;
};
export function createCurrent(store: MemoryStore, input: CreateInput): Record<string, unknown> {
  store.requireWritable();
  store.guardPinned(input.recordId);
  return store.transaction((db) => {
    const prior = row(db, "SELECT * FROM memory_operations_v3 WHERE idempotency_key=?", input.idempotencyKey);
    if (prior) return operationResult(db, prior);
    if (row(db, "SELECT 1 AS found FROM memory_records_v3 WHERE record_id=?", input.recordId))
      throw new ValueError("record already exists");
    const now = store.now();
    db.prepare(
      "INSERT INTO memory_records_v3(record_id,record_class,domain,scope,created_at,created_by) VALUES(?,?,?,?,?,?)",
    ).run(input.recordId, input.recordClass, input.domain, input.scope ?? "global", now, input.actor);
    const evidenceId = insertEvidence(store, db, input.evidence);
    const revisionId = `${input.recordId}@r1-${sha256(input.idempotencyKey).slice(0, 12)}`;
    const revision = {
      title: input.title,
      summary: input.summary ?? "",
      content: input.content ?? "",
      impact: input.impact ?? "",
      confidence: clamp(numberOf(input.confidence, 0.7)),
      authority_status: input.authorityStatus ?? "canonical_reference",
    };
    db.prepare("INSERT INTO memory_revisions_v3 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)").run(
      revisionId,
      input.recordId,
      null,
      1,
      revision.title,
      revision.summary,
      revision.content,
      revision.impact,
      revision.confidence,
      now,
      revision.authority_status,
      semanticHash(revision),
      now,
      input.actor,
      input.surface ?? "",
      input.modelFamily ?? "",
      input.reason,
      input.idempotencyKey,
    );
    hooks.afterCreateRevisionInsert?.();
    db.prepare("INSERT INTO memory_telemetry_v3 VALUES(?,?,?,?,0,NULL,?)").run(
      revisionId,
      clamp(numberOf(input.salience, 0.5)),
      clamp(numberOf(input.stability, 0.5)),
      clamp(numberOf(input.accessibility, 0.5)),
      now,
    );
    linkEvidence(db, revisionId, evidenceId, "supports", input.reason);
    const op = insertOperation(store, db, {
      type: "create",
      actor: input.actor,
      surface: input.surface ?? "",
      recordId: input.recordId,
      revisionId,
      evidenceIds: [evidenceId],
      decision: "materialized",
      reason: input.reason,
      details: orderedObject([
        ["record_class", input.recordClass],
        ["domain", input.domain],
      ]),
      key: input.idempotencyKey,
    });
    lifecycle(store, db, {
      recordId: input.recordId,
      revisionId,
      state: "current",
      actor: input.actor,
      surface: input.surface ?? "",
      reason: input.reason,
      operationId: String(op.operation_id),
      key: `${input.idempotencyKey}:lifecycle:current`,
      effective: now,
    });
    return operationResult(db, op);
  });
}

export type ReviseInput = {
  recordId: string;
  operationType: "correct" | "refine" | "supersede";
  actor: string;
  reason: string;
  evidence: Evidence;
  idempotencyKey: string;
  changes: Record<string, unknown>;
  surface?: string;
  modelFamily?: string;
};
export function revise(store: MemoryStore, input: ReviseInput): Record<string, unknown> {
  store.requireWritable();
  store.guardPinned(input.recordId);
  if (!["correct", "refine", "supersede"].includes(input.operationType))
    throw new ValueError("unsupported semantic operation");
  const semantic = new Set(["title", "summary", "content", "impact", "confidence", "authority_status", "valid_from"]),
    telemetry = new Set(["salience", "stability", "accessibility"]);
  const unknown = Object.keys(input.changes)
    .filter((k) => !semantic.has(k) && !telemetry.has(k))
    .sort();
  if (unknown.length) throw new ValueError(`unsupported revision fields: ${unknown.join(", ")}`);
  return store.transaction((db) => {
    const prior = row(db, "SELECT * FROM memory_operations_v3 WHERE idempotency_key=?", input.idempotencyKey);
    if (prior) return operationResult(db, prior);
    const current = row(db, "SELECT * FROM memory_current_v3 WHERE record_id=?", input.recordId);
    if (!current) throw new ValueError("current record not found");
    const now = store.now(),
      n = Number(current.revision_number) + 1,
      revisionId = `${input.recordId}@r${n}-${sha256(input.idempotencyKey).slice(0, 12)}`;
    const value: Record<string, unknown> = {
      title: current.title,
      summary: current.summary,
      content: current.content,
      impact: current.impact,
      confidence: current.confidence,
      valid_from: current.valid_from,
      authority_status: current.authority_status,
      ...Object.fromEntries(Object.entries(input.changes).filter(([k]) => semantic.has(k))),
    };
    value.confidence = clamp(numberOf(value.confidence as Numeric));
    const evidenceId = insertEvidence(store, db, input.evidence);
    db.prepare("INSERT INTO memory_revisions_v3 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)").run(
      revisionId,
      input.recordId,
      current.revision_id,
      n,
      value.title,
      value.summary,
      value.content,
      value.impact,
      value.confidence,
      value.valid_from,
      value.authority_status,
      semanticHash(value),
      now,
      input.actor,
      input.surface ?? "",
      input.modelFamily ?? "",
      input.reason,
      input.idempotencyKey,
    );
    db.prepare("INSERT INTO memory_telemetry_v3 VALUES(?,?,?,?,0,NULL,?)").run(
      revisionId,
      clamp(numberOf(input.changes.salience as Numeric | undefined, Number(current.salience))),
      clamp(numberOf(input.changes.stability as Numeric | undefined, Number(current.stability))),
      clamp(numberOf(input.changes.accessibility as Numeric | undefined, Number(current.accessibility))),
      now,
    );
    linkEvidence(db, revisionId, evidenceId, "supports", input.reason);
    const op = insertOperation(store, db, {
      type: input.operationType,
      actor: input.actor,
      surface: input.surface ?? "",
      recordId: input.recordId,
      revisionId,
      evidenceIds: [evidenceId],
      decision: "materialized",
      reason: input.reason,
      details: orderedObject([["parent_revision_id", String(current.revision_id)]]),
      key: input.idempotencyKey,
    });
    lifecycle(store, db, {
      recordId: input.recordId,
      revisionId: String(current.revision_id),
      state: "superseded",
      actor: input.actor,
      surface: input.surface ?? "",
      reason: input.reason,
      operationId: String(op.operation_id),
      key: `${input.idempotencyKey}:lifecycle:superseded`,
      effective: now,
    });
    lifecycle(store, db, {
      recordId: input.recordId,
      revisionId,
      state: "current",
      actor: input.actor,
      surface: input.surface ?? "",
      reason: input.reason,
      operationId: String(op.operation_id),
      key: `${input.idempotencyKey}:lifecycle:current`,
      effective: now,
    });
    return operationResult(db, op);
  });
}

export function invalidate(
  store: MemoryStore,
  input: {
    recordId: string;
    actor: string;
    reason: string;
    evidence: Evidence;
    idempotencyKey: string;
    surface?: string;
  },
): Record<string, unknown> {
  store.requireWritable();
  store.guardPinned(input.recordId);
  return store.transaction((db) => {
    const prior = row(db, "SELECT * FROM memory_operations_v3 WHERE idempotency_key=?", input.idempotencyKey);
    if (prior) return operationResult(db, prior);
    const current = row(db, "SELECT * FROM memory_current_v3 WHERE record_id=?", input.recordId);
    if (!current) throw new ValueError("current record not found");
    const evidenceId = insertEvidence(store, db, input.evidence),
      now = store.now();
    const op = insertOperation(store, db, {
      type: "invalidate",
      actor: input.actor,
      surface: input.surface ?? "",
      recordId: input.recordId,
      revisionId: String(current.revision_id),
      evidenceIds: [evidenceId],
      decision: "materialized",
      reason: input.reason,
      details: orderedObject([]),
      key: input.idempotencyKey,
    });
    linkEvidence(db, String(current.revision_id), evidenceId, "contradicts", input.reason);
    lifecycle(store, db, {
      recordId: input.recordId,
      revisionId: String(current.revision_id),
      state: "invalidated",
      actor: input.actor,
      surface: input.surface ?? "",
      reason: input.reason,
      operationId: String(op.operation_id),
      key: `${input.idempotencyKey}:lifecycle:invalidated`,
      effective: now,
    });
    return operationResult(db, op);
  });
}

export function addCue(
  store: MemoryStore,
  input: { profile: string; cue: string; targetRecordId: string; weight?: Numeric; cueType?: string; scope?: string },
): null {
  store.requireWritable();
  store.transaction((db) => {
    db.prepare(
      "INSERT INTO memory_cues_v3(cue,cue_norm,cue_type,target_record_id,weight,scope,profile) VALUES(?,?,?,?,?,?,?) ON CONFLICT(profile,cue_norm,target_record_id) DO UPDATE SET cue=excluded.cue,cue_type=excluded.cue_type,weight=excluded.weight,scope=excluded.scope",
    ).run(
      input.cue,
      normalizeText(input.cue),
      input.cueType ?? "phrase",
      input.targetRecordId,
      numberOf(input.weight, 1),
      input.scope ?? "global",
      input.profile,
    );
  });
  return null;
}

export type RelationInput = {
  relationId?: string | null;
  fromRecordId: string;
  toRecordId: string;
  relationType: string;
  weight?: Numeric;
  sourceRevisionId?: string | null;
  actor?: string;
  surface?: string;
  reason?: string;
  evidence?: Evidence | null;
  idempotencyKey?: string | null;
};
function relation(store: MemoryStore, event: "assert" | "retract", input: RelationInput): Record<string, unknown> {
  store.requireWritable();
  const type = input.relationType.trim();
  if (!type) throw new ValueError("relation_type must be non-empty");
  if (input.fromRecordId === input.toRecordId) throw new ValueError("a relation needs two different records");
  const weight = numberOf(input.weight, event === "assert" ? 1 : 0);
  if (event === "assert" && (weight < 0 || weight > 10))
    throw new ValueError("relation weight must be between 0 and 10");
  return store.transaction((db) => {
    if (input.idempotencyKey) {
      const prior = row(db, "SELECT * FROM memory_relation_events_v4 WHERE idempotency_key=?", input.idempotencyKey);
      if (prior) return { ...prior, status: "duplicate" };
    }
    const latest = row(
      db,
      "SELECT * FROM memory_relation_events_v4 WHERE from_record_id=? AND to_record_id=? AND relation_type=? ORDER BY sequence_number DESC LIMIT 1",
      input.fromRecordId,
      input.toRecordId,
      type,
    );
    if (event === "retract" && (!latest || latest.event_type === "retract"))
      return { status: "no_op", reason: "relation is not active", relation_id: latest?.relation_id ?? null };
    if (
      event === "assert" &&
      latest?.event_type === "assert" &&
      Math.abs(Number(latest.weight) - weight) <= 1e-9 &&
      (latest.source_revision_id ?? null) === (input.sourceRevisionId ?? null)
    )
      return { ...latest, status: "no_op" };
    if (
      !row(db, "SELECT 1 AS found FROM memory_records_v3 WHERE record_id=?", input.fromRecordId) ||
      !row(db, "SELECT 1 AS found FROM memory_records_v3 WHERE record_id=?", input.toRecordId)
    )
      throw new ValueError("relation endpoints must exist");
    const stable = String(
        latest?.relation_id ?? input.relationId ?? `${input.fromRecordId}->${type}->${input.toRecordId}`,
      ),
      sequence = latest ? Number(latest.sequence_number) + 1 : 1;
    const actor = input.actor ?? "host",
      surface = input.surface ?? "",
      reason = input.reason ?? (event === "assert" ? "relation asserted" : "relation retracted");
    let evidenceId: null | string = null;
    if (input.evidence) evidenceId = insertEvidence(store, db, { ...input.evidence, actor, surface });
    const key = input.idempotencyKey ?? `relation:${stable}:${sequence}:${event}`,
      eventId = `relation-event:${sha256(key).slice(0, 32)}`;
    db.prepare("INSERT INTO memory_relation_events_v4 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)").run(
      eventId,
      stable,
      input.fromRecordId,
      input.toRecordId,
      type,
      sequence,
      event,
      weight,
      input.sourceRevisionId ?? null,
      evidenceId,
      actor,
      surface,
      reason,
      key,
      store.now(),
    );
    return {
      ...row(db, "SELECT * FROM memory_relation_events_v4 WHERE relation_event_id=?", eventId)!,
      status: event === "assert" ? "asserted" : "retracted",
    };
  });
}
export const addRelation = (store: MemoryStore, input: RelationInput) => relation(store, "assert", input);
export const retractRelation = (store: MemoryStore, input: RelationInput) =>
  relation(store, "retract", { ...input, weight: 0, sourceRevisionId: null });

export function recordAccess(
  store: MemoryStore,
  input: {
    cue: string;
    recordId: string;
    revisionId: string;
    retrievalReason: string;
    rank: number;
    surface: string;
    gain?: Numeric;
  },
): null {
  store.requireWritable();
  const gain = numberOf(input.gain, 0.01);
  if (gain < 0 || gain > 1) throw new ValueError("access gain must be between 0 and 1");
  store.transaction((db) => {
    const owner = row(db, "SELECT record_id FROM memory_revisions_v3 WHERE revision_id=?", input.revisionId);
    if (!owner || owner.record_id !== input.recordId) throw new ValueError("revision does not belong to record");
    const now = store.now();
    db.prepare(
      "INSERT INTO memory_access_v3(cue_sha256,record_id,revision_id,retrieval_reason,rank,surface,created_at) VALUES(?,?,?,?,?,?,?)",
    ).run(sha256(input.cue), input.recordId, input.revisionId, input.retrievalReason, input.rank, input.surface, now);
    db.prepare(
      "UPDATE memory_telemetry_v3 SET accessibility=MIN(1.0,accessibility+?),access_count=access_count+1,last_accessed_at=?,updated_at=? WHERE revision_id=?",
    ).run(gain, now, now, input.revisionId);
  });
  return null;
}

export type Adjustment = { recordId: string; field: string; oldValue?: Numeric; newValue: Numeric };
export function applyMaintenance(
  store: MemoryStore,
  input: {
    runId: string;
    adjustments: Adjustment[];
    actor: string;
    reason: string;
    surface?: string;
    idempotencyKey?: string;
  },
): Record<string, unknown> {
  store.requireWritable();
  if (!input.runId.trim()) throw new ValueError("maintenance run_id is required");
  if (!input.reason.trim()) throw new ValueError("maintenance reason is required");
  const key = input.idempotencyKey ?? `maintenance:${input.runId}`;
  return store.transaction((db) => {
    const prior = row(db, "SELECT * FROM memory_operations_v3 WHERE idempotency_key=?", key);
    if (prior) return operationResult(db, prior);
    const applied: Record<string, unknown>[] = [];
    const hashes = new Map<string, string>();
    for (const adjustment of input.adjustments) {
      if (!adjustment.recordId.trim()) throw new ValueError("maintenance record_id is required");
      if (!["salience", "stability", "accessibility"].includes(adjustment.field))
        throw new ValueError(`unsupported maintenance field: ${adjustment.field || "<empty>"}`);
      const current = row(db, "SELECT * FROM memory_current_v3 WHERE record_id=?", adjustment.recordId);
      if (!current) throw new ValueError(`maintenance record not found: ${adjustment.recordId}`);
      const revisionId = String(current.revision_id),
        actual = Number(current[adjustment.field]);
      if (adjustment.oldValue !== undefined && Math.abs(actual - numberOf(adjustment.oldValue)) > 1e-6)
        throw new ValueError(`maintenance old_value mismatch for ${adjustment.recordId}.${adjustment.field}`);
      const next = clamp(numberOf(adjustment.newValue));
      if (Math.abs(next - actual) <= 1e-9) continue;
      hashes.set(revisionId, String(current.content_sha256));
      db.prepare(`UPDATE memory_telemetry_v3 SET ${adjustment.field}=?,updated_at=? WHERE revision_id=?`).run(
        next,
        store.now(),
        revisionId,
      );
      applied.push({
        record_id: adjustment.recordId,
        revision_id: revisionId,
        field: adjustment.field,
        old_value: actual,
        new_value: next,
      });
      if (applied.length === 1) hooks.afterFirstMaintenanceAdjustment?.();
    }
    hooks.beforeMaintenanceHashCheck?.(db);
    for (const [revisionId, expected] of hashes) {
      const actual = row(db, "SELECT content_sha256 FROM memory_revisions_v3 WHERE revision_id=?", revisionId);
      if (!actual || actual.content_sha256 !== expected)
        throw new RuntimeError("maintenance changed semantic content hash");
    }
    const details = orderedObject([
      ["run_id", input.runId],
      ["adjustments", toJsonValue(applied)],
      ["semantic_content_changed", false],
    ]);
    const oneApplied = applied.length === 1 ? applied[0] : null;
    const op = insertOperation(store, db, {
      type: "maintenance",
      actor: input.actor,
      surface: input.surface ?? "",
      recordId: oneApplied ? String(oneApplied.record_id) : null,
      revisionId: oneApplied ? String(oneApplied.revision_id) : null,
      evidenceIds: [],
      decision: applied.length ? "materialized" : "no_op",
      reason: input.reason,
      details,
      key,
    });
    return operationResult(db, op);
  });
}
