import { createHash } from "node:crypto";
import type { DatabaseSync } from "node:sqlite";
import { canonicalJson, hashPayload, orderedObject, pyFloat, type JsonValue, type OrderedObject } from "./encoding.ts";
import {
  ConfirmationMismatch,
  HumanPresenceRequired,
  ProposalDecided,
  ProposalIntegrityError,
  ReceiptIntegrityError,
  ReceiptNotFound,
  StaleAuthority,
} from "./errors.ts";
import { asArray, asString, get, objectEntries, parseLossless } from "./json.ts";
import { hooks } from "./internal-hooks.ts";
import type { IdentityProfile } from "./profile.ts";
import { MemoryStore, type Row } from "./store.ts";
import { identitySegment } from "./evidence-identity.ts";

const VHO_KEYS = [
  "llm_substrate",
  "runtime_architecture",
  "control_and_policy_layer",
  "memory_anchors",
  "identity_schema",
  "runtime_environment",
  "relational_field",
];
export const PINNED = ["core", "vho-open-ontology-core", "anchor:discussions", "anchor:open-loops"] as const;

export type Terminal = { stdinTTY: boolean; stdoutTTY: boolean; read(): string; write?(value: string): void };
export type ProposalInput = {
  reason: string;
  phaseContext: OrderedObject;
  title?: string;
  summary?: string;
  vhoStack?: OrderedObject;
  recognitionSignature?: JsonValue[];
  falsifier?: string;
  sourceRef?: string;
};

const sha256 = (value: string): string => createHash("sha256").update(value, "utf8").digest("hex");
const object = (value: JsonValue | undefined): OrderedObject => {
  objectEntries(value as JsonValue);
  return value as OrderedObject;
};
const entry = (value: OrderedObject, key: string): JsonValue | undefined => get(value, key);
const asObject = (value: unknown): OrderedObject => object(value as JsonValue);
const str = (row: Row, key: string): string => String(row[key] ?? "");

function quote(value: string): string {
  return canonicalJson(value);
}
function jsonWith(value: JsonValue, indent: number | null, level = 0, sort = false): string {
  if (
    value === null ||
    typeof value === "boolean" ||
    typeof value === "string" ||
    (!Array.isArray(value) && value.kind !== "object")
  ) {
    return canonicalJson(value);
  }
  const pad = (n: number) => " ".repeat(n);
  if (Array.isArray(value)) {
    if (!value.length) return "[]";
    if (indent === null) return `[${value.map((item) => jsonWith(item, null, level, sort)).join(", ")}]`;
    return `[\n${value.map((item) => `${pad(level + indent)}${jsonWith(item, indent, level + indent, sort)}`).join(",\n")}\n${pad(level)}]`;
  }
  let entries = value.entries;
  if (sort) entries = [...entries].sort(([a], [b]) => Array.from(a).join("").localeCompare(Array.from(b).join("")));
  // canonicalJson already has the exact code-point sorter; derive its key order without locale.
  if (sort)
    entries = [...value.entries].sort(([a], [b]) => {
      const left = Array.from(a, (c) => c.codePointAt(0)!);
      const right = Array.from(b, (c) => c.codePointAt(0)!);
      for (let i = 0; i < Math.min(left.length, right.length); i++) if (left[i] !== right[i]) return left[i] - right[i];
      return left.length - right.length;
    });
  if (!entries.length) return "{}";
  if (indent === null)
    return `{${entries.map(([key, item]) => `${quote(key)}: ${jsonWith(item, null, level, sort)}`).join(", ")}}`;
  return `{\n${entries.map(([key, item]) => `${pad(level + indent)}${quote(key)}: ${jsonWith(item, indent, level + indent, sort)}`).join(",\n")}\n${pad(level)}}`;
}
export const pythonIndentedJson = (value: JsonValue): string => jsonWith(value, 1);
const pythonDefaultJson = (value: JsonValue, sort = false): string => jsonWith(value, null, 0, sort);

function valueFromRow(value: unknown, float = false): JsonValue {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") return float ? pyFloat(value) : { kind: "int", value: BigInt(value) };
  throw new TypeError("unsupported row value");
}

function coreFromProfile(profile: IdentityProfile): OrderedObject {
  return object(entry(profile.ast, "core"));
}
function coreContent(
  core: OrderedObject,
  phaseContext: OrderedObject,
  overrides: ProposalInput,
): { title: string; summary: string; content: string; object: OrderedObject } {
  const oldStack = object(entry(core, "vho_stack"));
  const patch = overrides.vhoStack;
  const stack = orderedObject(VHO_KEYS.map((key) => [key, entry(patch ?? oldStack, key) ?? entry(oldStack, key)!]));
  const recognition = overrides.recognitionSignature?.length
    ? overrides.recognitionSignature
    : asArray(entry(core, "recognition_signature"));
  const contentObject = orderedObject([
    ["vho_stack", stack],
    ["recognition_signature", recognition],
    ["falsifier", overrides.falsifier || asString(entry(core, "falsifier"))],
    ["phase_context", phaseContext],
  ]);
  return {
    title: overrides.title || asString(entry(core, "title")),
    summary: overrides.summary || asString(entry(core, "summary")),
    content: pythonIndentedJson(contentObject),
    object: contentObject,
  };
}

function validateCore(title: string, summary: string, content: OrderedObject): void {
  if (!title.trim()) throw new ProposalIntegrityError("core.title is required");
  if (!summary.trim()) throw new ProposalIntegrityError("core.summary is required");
  const stack = object(entry(content, "vho_stack"));
  for (const key of VHO_KEYS)
    if (!asString(entry(stack, key)).trim()) throw new ProposalIntegrityError(`core.vho_stack is missing: ${key}`);
  if (!asArray(entry(content, "recognition_signature")).length)
    throw new ProposalIntegrityError("core.recognition_signature needs at least one pattern");
  if (!asString(entry(content, "falsifier")).trim()) throw new ProposalIntegrityError("core.falsifier is required");
  object(entry(content, "phase_context"));
}

function bindingObject(entries: [string, JsonValue][]): OrderedObject {
  return orderedObject(entries);
}
function resultRow(row: Row): Record<string, unknown> {
  return Object.fromEntries(Object.entries(row));
}
function plainJson(value: JsonValue): unknown {
  if (value === null || typeof value === "boolean" || typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(plainJson);
  if (value.kind === "int") return Number(value.value);
  if (value.kind === "float") return value.value;
  return Object.fromEntries(value.entries.map(([key, item]) => [key, plainJson(item)]));
}
function one(database: DatabaseSync, sql: string, ...params: (string | number | bigint)[]): Row | undefined {
  return database.prepare(sql).get(...params) as Row | undefined;
}

export class AuthorityV2 {
  readonly profile: IdentityProfile;
  readonly store: MemoryStore;
  readonly surface: string;
  readonly now: () => string;

  constructor(profile: IdentityProfile, store: MemoryStore, surface: string, now: () => string) {
    this.profile = profile;
    this.store = store;
    this.surface = surface;
    this.now = now;
  }

  propose(input: ProposalInput): Record<string, unknown> {
    this.store.requireWritable();
    if (!input.reason.trim()) throw new Error("a core proposal needs a reason");
    if (!input.phaseContext.entries.length)
      throw new Error("a core proposal needs phase_context (model, harness, policies)");
    return this.store.transaction((database) => {
      const current = one(database, "SELECT * FROM memory_current_v3 WHERE record_id='core'");
      if (!current) throw new Error("core is not bootstrapped");
      const old = object(parseLossless(str(current, "content")));
      const sourceCore = orderedObject([
        ["title", str(current, "title")],
        ["summary", str(current, "summary")],
        ["vho_stack", entry(old, "vho_stack")!],
        ["recognition_signature", entry(old, "recognition_signature")!],
        ["falsifier", entry(old, "falsifier")!],
        ["phase_context", entry(old, "phase_context")!],
      ]);
      const built = coreContent(sourceCore, input.phaseContext, input);
      validateCore(built.title, built.summary, built.object);
      const contentSha = sha256(built.content);
      const phaseSha = hashPayload(input.phaseContext);
      const reasonSha = sha256(input.reason);
      const fields: [string, JsonValue][] = [
        ["profile", this.profile.name],
        ["record_id", "core"],
        ["base_core_revision_id", str(current, "revision_id")],
        ["title", built.title],
        ["summary", built.summary],
        ["content_sha256", contentSha],
        ["phase_context_sha256", phaseSha],
        ["reason_sha256", reasonSha],
      ];
      const sourceRef =
        input.sourceRef || `self:core-proposal:${sha256(hashPayload(orderedObject(fields))).slice(0, 32)}`;
      const proposalSha = hashPayload(orderedObject([...fields, ["source_ref", sourceRef]]));
      const proposalId = `core-proposal:${proposalSha.slice(0, 32)}`;
      const prior = one(database, "SELECT * FROM memory_core_proposals_v5 WHERE proposal_sha256=?", proposalSha);
      if (prior) return { ...resultRow(prior), status: "existing" };
      database
        .prepare(
          "INSERT INTO memory_core_proposals_v5(proposal_id,profile,record_id,base_core_revision_id,title,summary,content,content_sha256,phase_context_json,phase_context_sha256,reason,reason_sha256,source_ref,proposal_sha256,created_at,created_by,surface) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        )
        .run(
          proposalId,
          this.profile.name,
          "core",
          str(current, "revision_id"),
          built.title,
          built.summary,
          built.content,
          contentSha,
          canonicalJson(input.phaseContext),
          phaseSha,
          input.reason,
          reasonSha,
          sourceRef,
          proposalSha,
          this.now(),
          this.profile.agent,
          this.surface,
        );
      return {
        ...resultRow(one(database, "SELECT * FROM memory_core_proposals_v5 WHERE proposal_id=?", proposalId)!),
        status: "proposed",
      };
    });
  }

  openProposals(): Record<string, unknown>[] {
    const info = this.store.schemaInfo();
    if (info.state === "legacy-v4" || info.state !== "ready") return [];
    const current = this.store.currentView("core")[0];
    return this.store
      .all(
        "SELECT * FROM memory_open_core_proposals_v5 WHERE profile=? ORDER BY created_at,proposal_id",
        this.profile.name,
      )
      .map((row) => ({
        proposal_id: row.proposal_id,
        base_core_revision_id: row.base_core_revision_id,
        stale: row.base_core_revision_id !== current?.revision_id,
        created_at: row.created_at,
        created_by: row.created_by,
        reason: row.reason,
        title: row.title,
      }));
  }

  requireConfirmation(expected: string, terminal: Terminal): void {
    if (!terminal.stdinTTY || !terminal.stdoutTTY)
      throw new HumanPresenceRequired("receipt issuance requires interactive TTY stdin and stdout");
    terminal.write?.(`Type ${expected} to issue the owner receipt: `);
    if (terminal.read().replace(/\r?\n$/u, "") !== expected)
      throw new ConfirmationMismatch(`confirmation did not exactly match '${expected}'`);
  }

  #issue(purpose: string, binding: OrderedObject): Record<string, unknown> {
    const bindingJson = canonicalJson(binding);
    const digest = hashPayload(binding);
    const receiptId = `receipt:${digest.slice(0, 32)}`;
    return this.store.transaction((database) => {
      database
        .prepare(
          "INSERT INTO memory_owner_receipts_v5(receipt_id,purpose,profile,binding_json,binding_sha256,issued_at,issued_by,authority,guard) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(binding_sha256) DO NOTHING",
        )
        .run(
          receiptId,
          purpose,
          this.profile.name,
          bindingJson,
          digest,
          this.now(),
          this.profile.owner,
          "owner",
          "tty-human-presence/v1",
        );
      return resultRow(one(database, "SELECT * FROM memory_owner_receipts_v5 WHERE binding_sha256=?", digest)!);
    });
  }

  issueCore(
    proposalId: string,
    outcome: "apply" | "reject",
    decisionNote: string,
    terminal: Terminal,
  ): Record<string, unknown> {
    this.store.requireWritable();
    const proposal = this.store.all(
      "SELECT * FROM memory_core_proposals_v5 WHERE proposal_id=? AND profile=?",
      proposalId,
      this.profile.name,
    )[0];
    if (!proposal) throw new ProposalIntegrityError("proposal not found");
    if (this.store.all("SELECT 1 AS found FROM memory_proposal_decisions_v5 WHERE proposal_id=?", proposalId).length)
      throw new ProposalDecided("proposal is already decided");
    const current = this.store.currentView("core")[0];
    if (!current) throw new ProposalIntegrityError("core is not bootstrapped");
    if (outcome === "apply" && proposal.base_core_revision_id !== current.revision_id)
      throw new StaleAuthority("stale proposal cannot be issued an apply receipt");
    this.requireConfirmation(`${outcome.toUpperCase()} ${proposalId.split(":", 2)[1].slice(0, 12)}`, terminal);
    return this.#issue(
      "identity_core_revision",
      bindingObject([
        ["purpose", "identity_core_revision"],
        ["profile", this.profile.name],
        ["current_core_revision_id", str(current, "revision_id")],
        ["proposal_id", proposalId],
        ["proposal_sha256", str(proposal, "proposal_sha256")],
        ["content_sha256", str(proposal, "content_sha256")],
        ["phase_context_sha256", str(proposal, "phase_context_sha256")],
        ["reason_sha256", str(proposal, "reason_sha256")],
        ["source_ref", str(proposal, "source_ref")],
        ["outcome", outcome],
        ["decision_note", decisionNote],
        ["authority", "owner"],
      ]),
    );
  }

  issueRetract(recordId: string, reason: string, terminal: Terminal): Record<string, unknown> {
    this.store.requireWritable();
    if ((PINNED as readonly string[]).includes(recordId))
      throw new Error("core, ontology and anchors cannot be retracted");
    const current = this.store.currentView(recordId)[0];
    if (!current) throw new Error("current record not found");
    this.requireConfirmation(`RETRACT ${recordId}`, terminal);
    return this.#issue(
      "identity_retract",
      bindingObject([
        ["purpose", "identity_retract"],
        ["profile", this.profile.name],
        ["record_id", recordId],
        ["current_revision_id", str(current, "revision_id")],
        ["reason", reason],
        ["reason_sha256", sha256(reason)],
        ["authority", "owner"],
      ]),
    );
  }

  issueLegacyClose(note: string, terminal: Terminal): Record<string, unknown> {
    this.store.requireWritable();
    const current = this.store.currentView("core")[0];
    const relation = this.store.all(
      "SELECT * FROM memory_relation_events_v4 WHERE from_record_id='core' AND to_record_id='anchor:discussions' AND relation_type='awaiting-discussion' ORDER BY sequence_number DESC LIMIT 1",
    )[0];
    if (!current || !relation || relation.event_type !== "assert") throw new Error("no active legacy core discussion");
    this.requireConfirmation(`CLOSE ${str(relation, "relation_event_id").split(":", 2)[1].slice(0, 12)}`, terminal);
    return this.#issue(
      "identity_legacy_discussion_close",
      bindingObject([
        ["purpose", "identity_legacy_discussion_close"],
        ["profile", this.profile.name],
        ["core_revision_id", str(current, "revision_id")],
        ["relation_event_id", str(relation, "relation_event_id")],
        ["relation_source_revision_id", str(relation, "source_revision_id")],
        ["note", note],
        ["note_sha256", sha256(note)],
        ["authority", "owner"],
      ]),
    );
  }

  #receipt(database: DatabaseSync, receiptId: string, purpose: string): { row: Row; binding: OrderedObject } {
    const row = one(database, "SELECT * FROM memory_owner_receipts_v5 WHERE receipt_id=?", receiptId);
    if (!row) throw new ReceiptNotFound(`unknown receipt ${receiptId}`);
    let binding: OrderedObject;
    try {
      binding = object(parseLossless(str(row, "binding_json")));
    } catch (error) {
      throw new ReceiptIntegrityError("receipt binding_json is invalid");
    }
    const digest = hashPayload(binding);
    if (digest !== row.binding_sha256) throw new ReceiptIntegrityError("receipt binding_sha256 mismatch");
    if (receiptId !== `receipt:${digest.slice(0, 32)}`)
      throw new ReceiptIntegrityError("receipt_id does not match binding");
    if (
      row.purpose !== purpose ||
      row.profile !== this.profile.name ||
      row.authority !== "owner" ||
      asString(entry(binding, "purpose")) !== row.purpose ||
      asString(entry(binding, "profile")) !== row.profile ||
      asString(entry(binding, "authority")) !== row.authority
    ) {
      throw new ReceiptIntegrityError("receipt purpose, profile or authority mismatch");
    }
    return { row, binding };
  }

  #operationResult(database: DatabaseSync, operation: Row): Record<string, unknown> {
    const result: Record<string, unknown> = resultRow(operation);
    result.evidence_ids = plainJson(parseLossless(String(result.evidence_ids_json ?? "[]")));
    delete result.evidence_ids_json;
    result.details = plainJson(parseLossless(String(result.details_json ?? "{}")));
    delete result.details_json;
    result.revision = result.target_revision_id
      ? resultRow(
          one(
            database,
            "SELECT * FROM memory_revision_state_v3 WHERE revision_id=?",
            String(result.target_revision_id),
          )!,
        )
      : null;
    return result;
  }

  #replay(database: DatabaseSync, receiptId: string): Record<string, unknown> | null {
    const consumed = one(
      database,
      "SELECT operation_id FROM memory_receipt_consumptions_v5 WHERE receipt_id=?",
      receiptId,
    );
    if (!consumed) return null;
    const operation = one(
      database,
      "SELECT * FROM memory_operations_v3 WHERE operation_id=?",
      str(consumed, "operation_id"),
    );
    if (!operation) throw new ReceiptIntegrityError("consumption has no authority operation");
    return this.#operationResult(database, operation);
  }

  #proposal(database: DatabaseSync, binding: OrderedObject): Row {
    const proposalId = asString(entry(binding, "proposal_id"));
    const proposal = one(database, "SELECT * FROM memory_core_proposals_v5 WHERE proposal_id=?", proposalId);
    if (!proposal) throw new ProposalIntegrityError("proposal not found");
    let phase: OrderedObject;
    let content: OrderedObject;
    try {
      phase = object(parseLossless(str(proposal, "phase_context_json")));
      content = object(parseLossless(str(proposal, "content")));
    } catch {
      throw new ProposalIntegrityError("proposal JSON is invalid");
    }
    const contentSha = sha256(str(proposal, "content"));
    const phaseSha = hashPayload(phase);
    const reasonSha = sha256(str(proposal, "reason"));
    const fields = bindingObject([
      ["profile", str(proposal, "profile")],
      ["record_id", str(proposal, "record_id")],
      ["base_core_revision_id", str(proposal, "base_core_revision_id")],
      ["title", str(proposal, "title")],
      ["summary", str(proposal, "summary")],
      ["content_sha256", contentSha],
      ["phase_context_sha256", phaseSha],
      ["reason_sha256", reasonSha],
      ["source_ref", str(proposal, "source_ref")],
    ]);
    const proposalSha = hashPayload(fields);
    const checks: [string, string | number | bigint | null, string][] = [
      [contentSha, proposal.content_sha256, "content_sha256"],
      [phaseSha, proposal.phase_context_sha256, "phase_context_sha256"],
      [reasonSha, proposal.reason_sha256, "reason_sha256"],
      [proposalSha, proposal.proposal_sha256, "proposal_sha256"],
      [`core-proposal:${proposalSha.slice(0, 32)}`, proposal.proposal_id, "proposal_id"],
    ];
    for (const [actual, expected, label] of checks)
      if (actual !== expected) throw new ProposalIntegrityError(`proposal ${label} mismatch`);
    validateCore(str(proposal, "title"), str(proposal, "summary"), content);
    if (hashPayload(object(entry(content, "phase_context"))) !== phaseSha)
      throw new ProposalIntegrityError("phase_context in content does not match proposal");
    if (proposal.profile !== asString(entry(binding, "profile")))
      throw new ProposalIntegrityError("proposal profile does not match binding");
    for (const key of [
      "proposal_id",
      "proposal_sha256",
      "content_sha256",
      "phase_context_sha256",
      "reason_sha256",
      "source_ref",
    ]) {
      if (asString(entry(binding, key)) !== proposal[key])
        throw new ProposalIntegrityError(`binding ${key} does not match proposal`);
    }
    return proposal;
  }

  #insertEvidence(
    database: DatabaseSync,
    evidence: { type: string; sourceRef: string; summary: string; confidence: number; actor: string },
  ): string {
    const inferred = evidence.sourceRef.includes(":") ? evidence.sourceRef.split(":", 1)[0] : evidence.sourceRef;
    const sourceFamily = identitySegment(inferred, "unknown-source");
    const independenceGroup = identitySegment(evidence.sourceRef || sourceFamily, sourceFamily);
    const sourceSha = hashPayload(
      bindingObject([
        ["source_ref", evidence.sourceRef],
        ["content_summary", evidence.summary],
      ]),
    );
    const identity = bindingObject([
      ["identity_version", "evidence-v2"],
      ["source_family", sourceFamily],
      ["independence_group", independenceGroup],
      ["source_sha256", sourceSha],
    ]);
    const evidenceSha = hashPayload(identity);
    const evidenceId = `evidence:evidence-v2:${evidenceSha.slice(0, 32)}`;
    database
      .prepare(
        "INSERT INTO memory_evidence_v3(evidence_id,identity_version,evidence_type,source_ref,source_family,independence_group,source_sha256,evidence_sha256,captured_at,actor,surface,model_family,content_summary,confidence,privacy_class) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(identity_version,evidence_sha256) DO NOTHING",
      )
      .run(
        evidenceId,
        "evidence-v2",
        evidence.type,
        evidence.sourceRef,
        sourceFamily,
        independenceGroup,
        sourceSha,
        evidenceSha,
        this.now(),
        evidence.actor,
        "",
        "",
        evidence.summary,
        evidence.confidence,
        "private",
      );
    return evidenceId;
  }

  #insertOperation(
    database: DatabaseSync,
    values: {
      type: string;
      actor: string;
      recordId: string;
      revisionId: string | null;
      evidenceIds: string[];
      reason: string;
      details: OrderedObject;
      key: string;
    },
  ): Row {
    const operationId = `operation:${sha256(values.key).slice(0, 32)}`;
    database
      .prepare(
        "INSERT INTO memory_operations_v3(operation_id,operation_type,actor,surface,target_record_id,target_revision_id,evidence_ids_json,decision,reason,details_json,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
      )
      .run(
        operationId,
        values.type,
        values.actor,
        this.surface,
        values.recordId,
        values.revisionId,
        pythonDefaultJson(values.evidenceIds),
        "materialized",
        values.reason,
        pythonDefaultJson(values.details, true),
        values.key,
        this.now(),
      );
    return one(database, "SELECT * FROM memory_operations_v3 WHERE operation_id=?", operationId)!;
  }

  applyCore(receiptId: string): Record<string, unknown> {
    this.store.requireWritable();
    return this.store.transaction((database) => {
      const { row: receipt, binding } = this.#receipt(database, receiptId, "identity_core_revision");
      const replay = this.#replay(database, receiptId);
      if (replay) return replay;
      const proposal = this.#proposal(database, binding);
      if (
        one(
          database,
          "SELECT 1 AS found FROM memory_proposal_decisions_v5 WHERE proposal_id=?",
          str(proposal, "proposal_id"),
        )
      )
        throw new ProposalDecided("proposal is already decided");
      const current = one(database, "SELECT * FROM memory_current_v3 WHERE record_id='core'");
      if (!current || asString(entry(binding, "current_core_revision_id")) !== current.revision_id)
        throw new StaleAuthority("receipt current core does not match actual current core");
      const outcome = asString(entry(binding, "outcome"));
      if (!new Set(["apply", "reject"]).has(outcome)) throw new ReceiptIntegrityError("receipt outcome is invalid");
      if (outcome === "apply" && proposal.base_core_revision_id !== current.revision_id)
        throw new StaleAuthority("proposal base does not match current core");
      const key = `authority-v2:${receiptId}`;
      const operationId = `operation:${sha256(key).slice(0, 32)}`;
      const note = asString(entry(binding, "decision_note"));
      let revisionId: string | null = null;
      const evidenceIds: string[] = [];
      if (outcome === "apply") {
        evidenceIds.push(
          this.#insertEvidence(database, {
            type: "self_log",
            sourceRef: str(proposal, "source_ref"),
            summary: Array.from(str(proposal, "reason")).slice(0, 300).join(""),
            confidence: 0.9,
            actor: str(proposal, "created_by"),
          }),
        );
        evidenceIds.push(
          this.#insertEvidence(database, {
            type: "owner_receipt",
            sourceRef: receiptId,
            summary: note || outcome,
            confidence: 1,
            actor: str(receipt, "issued_by"),
          }),
        );
        revisionId = `core@r${Number(current.revision_number) + 1}-${sha256(key).slice(0, 12)}`;
      }
      const detailEntries: [string, JsonValue][] = [
        ["receipt_id", receiptId],
        ["proposal_id", str(proposal, "proposal_id")],
        ["outcome", outcome],
        ["decision_note", note],
      ];
      if (revisionId) detailEntries.push(["core_revision_id", revisionId]);
      const operation = this.#insertOperation(database, {
        type: "identity_core_revision",
        actor: str(receipt, "issued_by"),
        recordId: "core",
        revisionId: null,
        evidenceIds,
        reason: note || outcome,
        details: bindingObject(detailEntries),
        key,
      });
      if (outcome === "apply")
        this.#reviseCore(
          database,
          current,
          proposal,
          revisionId!,
          evidenceIds,
          operationId,
          key,
          str(receipt, "issued_by"),
        );
      database
        .prepare(
          "INSERT INTO memory_proposal_decisions_v5(proposal_id,receipt_id,outcome,core_revision_id,decided_at) VALUES(?,?,?,?,?)",
        )
        .run(
          str(proposal, "proposal_id"),
          receiptId,
          outcome === "apply" ? "applied" : "rejected",
          revisionId,
          this.now(),
        );
      database
        .prepare("INSERT INTO memory_receipt_consumptions_v5(receipt_id,operation_id,consumed_at) VALUES(?,?,?)")
        .run(receiptId, operationId, this.now());
      return this.#operationResult(database, operation);
    });
  }

  #reviseCore(
    database: DatabaseSync,
    current: Row,
    proposal: Row,
    revisionId: string,
    evidenceIds: string[],
    operationId: string,
    key: string,
    actor: string,
  ): void {
    const now = this.now();
    const semantic = bindingObject([
      ["title", str(proposal, "title")],
      ["summary", str(proposal, "summary")],
      ["content", str(proposal, "content")],
      ["impact", str(current, "impact")],
      ["confidence", pyFloat(Number(current.confidence))],
      ["authority_status", str(current, "authority_status")],
    ]);
    database
      .prepare(
        "INSERT INTO memory_revisions_v3(revision_id,record_id,parent_revision_id,revision_number,title,summary,content,impact,confidence,valid_from,authority_status,content_sha256,created_at,created_by,surface,model_family,reason,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
      )
      .run(
        revisionId,
        "core",
        current.revision_id,
        Number(current.revision_number) + 1,
        proposal.title,
        proposal.summary,
        proposal.content,
        current.impact,
        current.confidence,
        current.valid_from,
        current.authority_status,
        hashPayload(semantic),
        now,
        actor,
        this.surface,
        "",
        proposal.reason,
        key,
      );
    hooks.afterAuthorityRevisionInsert?.();
    database
      .prepare(
        "INSERT INTO memory_telemetry_v3(revision_id,salience,stability,accessibility,access_count,last_accessed_at,updated_at) VALUES(?,?,?,?,0,NULL,?)",
      )
      .run(revisionId, current.salience, current.stability, current.accessibility, now);
    for (const evidenceId of evidenceIds)
      database
        .prepare(
          "INSERT INTO memory_revision_evidence_v3(revision_id,evidence_id,stance,weight,reason) VALUES(?,?, 'supports',1.0,?)",
        )
        .run(revisionId, evidenceId, proposal.reason);
    this.#lifecycle(
      database,
      "core",
      str(current, "revision_id"),
      "superseded",
      actor,
      str(proposal, "reason"),
      operationId,
      `${key}:lifecycle:superseded`,
      now,
    );
    this.#lifecycle(
      database,
      "core",
      revisionId,
      "current",
      actor,
      str(proposal, "reason"),
      operationId,
      `${key}:lifecycle:current`,
      now,
    );
  }

  #lifecycle(
    database: DatabaseSync,
    recordId: string,
    revisionId: string,
    state: string,
    actor: string,
    reason: string,
    operationId: string,
    key: string,
    effectiveAt: string,
  ): void {
    const sequence = Number(
      one(
        database,
        "SELECT COALESCE(MAX(sequence_number),0)+1 AS n FROM memory_lifecycle_events_v3 WHERE record_id=?",
        recordId,
      )!.n,
    );
    database
      .prepare(
        "INSERT INTO memory_lifecycle_events_v3(lifecycle_event_id,record_id,revision_id,sequence_number,lifecycle_state,effective_at,actor,surface,reason,operation_id,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
      )
      .run(
        `lifecycle:${sha256(key).slice(0, 32)}`,
        recordId,
        revisionId,
        sequence,
        state,
        effectiveAt,
        actor,
        this.surface,
        reason,
        operationId,
        key,
        this.now(),
      );
  }

  applyRetract(receiptId: string): Record<string, unknown> {
    this.store.requireWritable();
    return this.store.transaction((database) => {
      const { row: receipt, binding } = this.#receipt(database, receiptId, "identity_retract");
      const replay = this.#replay(database, receiptId);
      if (replay) return replay;
      const recordId = asString(entry(binding, "record_id"));
      if ((PINNED as readonly string[]).includes(recordId))
        throw new ReceiptIntegrityError("retract receipt targets a pinned record");
      const current = one(database, "SELECT * FROM memory_current_v3 WHERE record_id=?", recordId);
      if (!current || current.revision_id !== asString(entry(binding, "current_revision_id")))
        throw new StaleAuthority("retract target revision is stale");
      const reason = asString(entry(binding, "reason"));
      if (sha256(reason) !== asString(entry(binding, "reason_sha256")))
        throw new ReceiptIntegrityError("retract reason digest mismatch");
      const key = `authority-v2:${receiptId}`;
      const evidenceId = this.#insertEvidence(database, {
        type: "owner_receipt",
        sourceRef: receiptId,
        summary: reason,
        confidence: 1,
        actor: str(receipt, "issued_by"),
      });
      const details = bindingObject([
        ["receipt_id", receiptId],
        ["record_id", recordId],
      ]);
      const operation = this.#insertOperation(database, {
        type: "identity_retract",
        actor: str(receipt, "issued_by"),
        recordId,
        revisionId: str(current, "revision_id"),
        evidenceIds: [evidenceId],
        reason,
        details,
        key,
      });
      database
        .prepare(
          "INSERT INTO memory_revision_evidence_v3(revision_id,evidence_id,stance,weight,reason) VALUES(?,?, 'contradicts',1.0,?)",
        )
        .run(current.revision_id, evidenceId, reason);
      const effective = this.now();
      this.#lifecycle(
        database,
        recordId,
        str(current, "revision_id"),
        "invalidated",
        str(receipt, "issued_by"),
        reason,
        str(operation, "operation_id"),
        `${key}:lifecycle:invalidated`,
        effective,
      );
      database
        .prepare("INSERT INTO memory_receipt_consumptions_v5 VALUES(?,?,?)")
        .run(receiptId, operation.operation_id, this.now());
      return this.#operationResult(database, operation);
    });
  }

  applyLegacyClose(receiptId: string): Record<string, unknown> {
    this.store.requireWritable();
    return this.store.transaction((database) => {
      const { row: receipt, binding } = this.#receipt(database, receiptId, "identity_legacy_discussion_close");
      const replay = this.#replay(database, receiptId);
      if (replay) return replay;
      const relation = one(
        database,
        "SELECT * FROM memory_relation_events_v4 WHERE from_record_id='core' AND to_record_id='anchor:discussions' AND relation_type='awaiting-discussion' ORDER BY sequence_number DESC LIMIT 1",
      );
      const current = one(database, "SELECT * FROM memory_current_v3 WHERE record_id='core'");
      if (
        !relation ||
        relation.event_type !== "assert" ||
        relation.relation_event_id !== asString(entry(binding, "relation_event_id")) ||
        relation.source_revision_id !== asString(entry(binding, "relation_source_revision_id")) ||
        !current ||
        current.revision_id !== asString(entry(binding, "core_revision_id"))
      )
        throw new StaleAuthority("legacy discussion binding is stale");
      const note = asString(entry(binding, "note"));
      if (sha256(note) !== asString(entry(binding, "note_sha256")))
        throw new ReceiptIntegrityError("legacy discussion note digest mismatch");
      const key = `authority-v2:${receiptId}`;
      const eventId = `relation-event:${sha256(key).slice(0, 32)}`;
      const operation = this.#insertOperation(database, {
        type: "identity_legacy_discussion_close",
        actor: str(receipt, "issued_by"),
        recordId: "core",
        revisionId: str(current, "revision_id"),
        evidenceIds: [],
        reason: note,
        details: bindingObject([
          ["receipt_id", receiptId],
          ["relation_event_id", eventId],
        ]),
        key,
      });
      database
        .prepare(
          "INSERT INTO memory_relation_events_v4(relation_event_id,relation_id,from_record_id,to_record_id,relation_type,sequence_number,event_type,weight,source_revision_id,evidence_id,actor,surface,reason,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        )
        .run(
          eventId,
          relation.relation_id,
          relation.from_record_id,
          relation.to_record_id,
          relation.relation_type,
          Number(relation.sequence_number) + 1,
          "retract",
          0,
          null,
          null,
          receipt.issued_by,
          this.surface,
          note,
          key,
          this.now(),
        );
      database
        .prepare("INSERT INTO memory_receipt_consumptions_v5 VALUES(?,?,?)")
        .run(receiptId, operation.operation_id, this.now());
      return this.#operationResult(database, operation);
    });
  }
}
