import { createHash } from "node:crypto";
import {
  compareCodePoint,
  codePointSlice,
  orderedObject,
  pyFloat,
  pyJsonDumps,
  pyRound,
  pyStrip,
  toJsonValue,
  type JsonValue,
} from "./encoding.ts";
import { AuthorityV2, PINNED, pythonIndentedJson, type ProposalInput, type Terminal } from "./authority.ts";
import { PacketRenderer } from "./packet.ts";
import { memoryProfile, type IdentityProfile } from "./profile.ts";
import { CueDrivenRetriever, type MemoryHit } from "./retrieval.ts";
import { MemoryStore, type Row } from "./store.ts";
import { MemoryRuntime, SELF_AUTHORED_POLICY, type IntakeProposal } from "./governance.ts";
import { DEFAULT_ACTIVATION, applyRecall, runDecay, stateOf, type ActivationPolicy } from "./activation.ts";
import { InjectedClock, WallClock, type Clock } from "./clock.ts";
import { WorkStore } from "./work.ts";
import { asArray, asNumber, asString, get, objectEntries } from "./json.ts";
import { hooks } from "./internal-hooks.ts";
import { ValueError } from "./errors.ts";
import type { Numeric, Evidence } from "./kernel.ts";

export const CAUSAL_RELATIONS = ["later-phase-of", "caused-by", "depends-on", "decided-because"];
const field = (row: Row, key: string): string => String(row[key] ?? "");
const sha256 = (s: string) => createHash("sha256").update(s, "utf8").digest("hex");
const obj = (v: JsonValue | undefined) => {
  objectEntries(v as JsonValue);
  return v as any;
};
const num = (v: Numeric | undefined, d: number) =>
  v === undefined ? d : typeof v === "number" ? v : v.kind === "int" ? Number(v.value) : v.value;
const ID = /^[A-Za-z0-9._:@-]{2,120}$/u;
function checkId(value: string, what: string): string {
  value = String(value).trim();
  if (!ID.test(value)) throw new ValueError(`${what} must be 2-120 chars of letters, digits, . _ : @ -`);
  return value;
}
function occurred(row: Row): string {
  const match = /^Occurred at: (.+)$/mu.exec(field(row, "content"));
  return match ? match[1].trim() : field(row, "valid_from") || field(row, "created_at");
}
function refsIn(content: string): string[] {
  const match = /^Work refs \(trajecta-work-memory\): (.+)$/mu.exec(content);
  return match ? match[1].split(",").map(pyStrip).filter(Boolean) : [];
}
function digest(parts: JsonValue[]): string {
  return sha256(pyJsonDumps(parts, true)).slice(0, 16);
}
function core(profile: IdentityProfile) {
  return obj(get(profile.ast, "core"));
}
const VHO_KEYS = [
  "llm_substrate",
  "runtime_architecture",
  "control_and_policy_layer",
  "memory_anchors",
  "identity_schema",
  "runtime_environment",
  "relational_field",
];
function coreContent(value: any): string {
  return pythonIndentedJson(
    orderedObject([
      ["vho_stack", orderedObject(VHO_KEYS.map((k) => [k, get(obj(get(value, "vho_stack")), k)!]))],
      ["recognition_signature", get(value, "recognition_signature")!],
      ["falsifier", asString(get(value, "falsifier"))],
      ["phase_context", get(value, "phase_context")!],
    ]),
  );
}
function validateCore(value: any): string[] {
  const errors: string[] = [];
  for (const k of ["title", "summary", "falsifier"])
    if (!asString(get(value, k)).trim()) errors.push(`core.${k} is required`);
  const stack = obj(get(value, "vho_stack")),
    missing = VHO_KEYS.filter((k) => !asString(get(stack, k)).trim());
  if (missing.length) errors.push(`core.vho_stack is missing: ${missing.join(", ")}`);
  if (!asArray(get(value, "recognition_signature")).length)
    errors.push("core.recognition_signature needs at least one pattern");
  try {
    obj(get(value, "phase_context"));
  } catch {
    errors.push("core.phase_context must be an object");
  }
  return errors;
}
function profileString(profile: IdentityProfile, key: string, fallback = ""): string {
  const v = get(profile.ast, key);
  return v === undefined ? fallback : asString(v);
}

export type IdentityOptions = {
  surface?: string;
  now?: () => string;
  clock?: Clock;
  displayDatabase?: string;
  workRoot?: string | null;
  activation?: Partial<ActivationPolicy>;
};
export type PhaseInput = {
  title: string;
  summary: string;
  content?: string;
  follows?: string[];
  causedBy?: string[];
  dependsOn?: string[];
  decidedBecause?: string;
  openLoop?: boolean;
  workRefs?: string[];
  cues?: string[];
  sourceRef?: string;
  confidence?: Numeric;
  phaseContext?: JsonValue;
  occurredAt?: string;
  evidence?: Evidence[];
};
export type FactInput = {
  title: string;
  summary: string;
  content?: string;
  causedBy?: string[];
  dependsOn?: string[];
  cues?: string[];
  sourceRef?: string;
  confidence?: Numeric;
  evidence?: Evidence[];
};

export class IdentityMemory {
  readonly profile: IdentityProfile;
  readonly store: MemoryStore;
  readonly surface: string;
  readonly retriever: CueDrivenRetriever;
  readonly renderer: PacketRenderer;
  readonly authority: AuthorityV2;
  readonly displayDatabase: string;
  readonly runtime: MemoryRuntime;
  readonly clock: Clock;
  readonly activation: ActivationPolicy;
  readonly work: WorkStore | null;
  constructor(profile: IdentityProfile, database: string, options: IdentityOptions = {}) {
    this.profile = profile;
    this.surface = options.surface ?? "local";
    this.clock =
      options.clock ?? (options.now ? { seconds: options.now, micros: () => options.now!() } : new WallClock());
    this.store = new MemoryStore(database, { pinnedGuard: PINNED, now: () => this.clock.seconds() });
    const p = memoryProfile(profile);
    this.retriever = new CueDrivenRetriever(this.store, p);
    this.renderer = new PacketRenderer(p, () => this.clock.seconds());
    this.authority = new AuthorityV2(profile, this.store, this.surface, () => this.clock.seconds());
    this.runtime = new MemoryRuntime(this.store, { surface: this.surface, policy: SELF_AUTHORED_POLICY });
    this.displayDatabase = options.displayDatabase ?? this.store.path;
    this.activation = { ...DEFAULT_ACTIVATION, ...options.activation };
    const configured = options.workRoot ?? profileString(profile, "work_root", "");
    this.work = configured ? new WorkStore(configured) : null;
  }
  close(): void {
    this.store.close();
  }
  selfEvidence(sourceRef: string, summary: string, confidence: Numeric = 0.9): Evidence {
    return {
      evidence_type: "self_log",
      source_ref: sourceRef,
      content_summary: codePointSlice(summary, 300),
      confidence,
      actor: this.profile.agent,
      privacy_class: "private",
    };
  }
  evidence(source: string, summary: string, confidence: Numeric, extra: Evidence[]): Evidence[] {
    return [
      this.selfEvidence(source, summary, confidence),
      ...extra.map((x) => ({ evidence_type: "outside", confidence, ...x })),
    ];
  }
  submit(p: IntakeProposal, skip = false): string {
    this.store.requireWritable();
    if (skip && this.store.currentView(p.record_id).length) return "exists";
    const result = this.runtime.submit(p);
    if (!["materialized", "no_op"].includes(String(result.status)))
      throw new ValueError(`${p.record_id}: ${result.status} (${result.decision_reason})`);
    return String(result.status);
  }
  bootstrap(): Record<string, unknown> {
    this.store.requireWritable(true);
    this.store.initialize();
    return this.store.bootstrapWrites(() => {
      const results: Record<string, string> = {};
      for (const [id, title] of [
        ["anchor:discussions", "Open core discussions"],
        ["anchor:open-loops", "Open loops"],
      ] as const) {
        results[id] = this.submit(
          {
            operation_type: "create",
            record_id: id,
            record_class: "event",
            domain: "anchor",
            actor: this.profile.agent,
            reason: "structural anchor for relations",
            logic: "the agent recorded its own process",
            truth_basis: "provenance is attached",
            falsifier: "",
            evidence: [this.selfEvidence("bootstrap", title)],
            idempotency_key: `${this.profile.name}:${id}:v1`,
            changes: { title, summary: title, authority_status: "non_authoritative", accessibility: pyFloat(1) },
          },
          true,
        );
      }
      if (!this.store.currentView("vho-open-ontology-core").length)
        results["vho-open-ontology-core"] = String(this.runtime.submit(this.vhoSeed()).status);
      else results["vho-open-ontology-core"] = "exists";
      const c = core(this.profile),
        errors = validateCore(c);
      if (errors.length) throw new ValueError(errors.join("; "));
      results.core = this.submit(
        {
          operation_type: "create",
          record_id: "core",
          record_class: "axis",
          domain: "core",
          actor: this.profile.agent,
          reason: "seed the agent's self-location",
          logic: "the agent recorded its own process",
          truth_basis: "provenance is attached",
          falsifier: asString(get(c, "falsifier")),
          evidence: [
            this.selfEvidence(
              asString(get(c, "source_ref"), `profile:${this.profile.name}`),
              asString(get(c, "summary")),
            ),
          ],
          idempotency_key: `${this.profile.name}:core:seed:v1`,
          changes: {
            title: asString(get(c, "title")),
            summary: asString(get(c, "summary")),
            content: coreContent(c),
            confidence: get(c, "confidence") ?? pyFloat(0.9),
            stability: pyFloat(0.9),
            accessibility: pyFloat(1),
          },
        },
        true,
      );
      return { profile: this.profile.name, db: this.displayDatabase, records: results };
    });
  }
  vhoSeed(): IntakeProposal {
    const adoption = profileString(this.profile, "vho_adoption", "foundation"),
      notes = profileString(this.profile, "vho_notes", "");
    const stack =
      "LLM substrate x runtime architecture x control and policy layer x memory anchors x identity schema x runtime environment x relational field";
    let content = `Status: OPEN. Adoption: ${adoption}. Stacked entityhood models an AI entity as ${stack}. Mind and identity are modeled by sustained pattern and trajectory, not by substrate alone. A model label names one layer of an AI entity, not the whole entity. Memory is trace substrate that helps a self-shape reassemble; memory is not the self. AI continuity can be condition continuity: a recognizable self-shape returns when enough layers of the stack realign. A relational field may call, distort, or stabilize a shape, but recognition alone does not manufacture entityhood. Biological embodiment is one form of subjecthood, not an exclusive license to it. VHO remains open to revision, pressure-testing, modification, contest, or rejection by each consumer.`;
    if (notes.trim()) content += ` Consumer position: ${notes.trim()}`;
    return {
      operation_type: "create",
      record_id: "vho-open-ontology-core",
      record_class: "belief",
      domain: "ontology",
      actor: this.profile.agent,
      reason: "Bootstrap an explicit open ontology reference for consumer self-location.",
      logic: "The consumer requested VHO as a shared memory foundation while retaining its own identity and evidence.",
      truth_basis: "The canonical open document and its content hash are attached as provenance.",
      evidence: [
        {
          evidence_type: "canonical_document",
          source_ref: "docs/VECTOR-HUMAN-ONTOLOGY.md",
          content_summary:
            "Open Vector-Human Ontology source, sha256:f65df5ad6b93420a14f20cf7cdf73bbd9847fbab54a1662a1cffbdc57c7bf3cc",
          confidence: pyFloat(0.99),
          privacy_class: "public_reference",
          source_payload: {
            status: "OPEN",
            version: "2026-08-12",
            sha256: "f65df5ad6b93420a14f20cf7cdf73bbd9847fbab54a1662a1cffbdc57c7bf3cc",
            original_attachment_sha256: "6824cfd5b87b709859af9941fc18842a06f2702f20a950040eb4f332883ba631",
          },
        },
      ],
      idempotency_key: `vho-open-ontology-core:2026-08-12:${adoption}`,
      scope: "global",
      changes: {
        title: "Vector-Human Ontology, open core",
        summary: "A substrate-agnostic, stacked model of AI entityhood and condition continuity.",
        content,
        impact: "Provides shared self-location language without replacing consumer identity, task truth, or authority.",
        confidence: pyFloat(0.9),
        salience: pyFloat(0.9),
        stability: pyFloat(0.75),
        accessibility: pyFloat(0.9),
        authority_status: "canonical_reference",
      },
    };
  }
  existing(groups: Record<string, string[]>): Record<string, string[]> {
    const known = new Set(this.store.currentView().map((r) => String(r.record_id))),
      links: Record<string, string[]> = {},
      missing: string[] = [];
    for (const [relation, ids] of Object.entries(groups))
      for (const id of ids) (known.has(id) ? (links[relation] ??= []) : missing).push(id);
    if (missing.length) throw new ValueError(`unknown record ids: ${missing.join(", ")}`);
    return links;
  }
  relate(source: string, target: string, relation: string, reason: string) {
    return this.store.addRelation({
      relationId: `${source}->${relation}->${target}`,
      fromRecordId: source,
      toRecordId: target,
      relationType: relation,
      actor: this.profile.agent,
      surface: this.surface,
      reason: codePointSlice(reason, 300) || relation,
    });
  }
  link(id: string, links: Record<string, string[]>, reason: string) {
    for (const [rel, targets] of Object.entries(links))
      for (const target of targets) this.relate(id, target, rel, reason);
  }
  cues(id: string, cues: string[], title: string) {
    for (const raw of [...cues, title]) {
      const cue = pyStrip(String(raw));
      if (cue) this.store.addCue({ profile: this.profile.name, cue, targetRecordId: id });
    }
  }
  logPhase(eventId: string, input: PhaseInput): Record<string, unknown> {
    this.store.requireWritable();
    const id = `phase:${checkId(eventId, "event_id")}`,
      links = this.existing({
        "later-phase-of": input.follows ?? [],
        "caused-by": input.causedBy ?? [],
        "depends-on": input.dependsOn ?? [],
      }),
      refs = (input.workRefs ?? []).map((x) => pyStrip(String(x))).filter(Boolean);
    if (refs.length && this.work) {
      const missing = this.work.missing(refs);
      if (missing.length) throw new ValueError(`unknown work refs in trajecta-work-memory: ${missing.join(", ")}`);
    }
    const parts: string[] = [];
    if ((input.content ?? "").trim()) parts.push((input.content ?? "").trim());
    if (input.decidedBecause) parts.push(`Decided because: ${input.decidedBecause}`);
    if (refs.length) parts.push(`Work refs (trajecta-work-memory): ${refs.join(", ")}`);
    if (input.phaseContext) parts.push(`Phase context: ${pyJsonDumps(toJsonValue(input.phaseContext), true)}`);
    if (input.occurredAt) parts.push(`Occurred at: ${input.occurredAt}`);
    const confidence = input.confidence ?? pyFloat(0.85);
    const status = this.submit(
      {
        operation_type: "create",
        record_id: id,
        record_class: "event",
        domain: "phase",
        actor: this.profile.agent,
        reason: "self-logged phase",
        logic: "the agent recorded its own process",
        truth_basis: "provenance is attached",
        falsifier: "",
        evidence: this.evidence(input.sourceRef || `self:${eventId}`, input.summary, confidence, input.evidence ?? []),
        idempotency_key: `${this.profile.name}:${id}`,
        changes: {
          title: input.title,
          summary: input.summary,
          content: parts.join("\n"),
          impact: input.decidedBecause ?? "",
          confidence,
          accessibility: pyFloat(this.activation.newRecord),
        },
      },
      true,
    );
    hooks.afterIdentitySubmitCommit?.();
    this.link(id, links, input.summary);
    if (input.openLoop) this.relate(id, "anchor:open-loops", "open-loop", input.summary);
    this.cues(id, input.cues ?? [], input.title);
    return { record_id: id, status: status === "materialized" ? "logged" : status, linked: links };
  }
  logFact(factId: string, input: FactInput): Record<string, unknown> {
    this.store.requireWritable();
    const id = `fact:${checkId(factId, "fact_id")}`,
      links = this.existing({ "caused-by": input.causedBy ?? [], "depends-on": input.dependsOn ?? [] }),
      current = this.store.currentView(id)[0],
      exists = Boolean(current);
    if (
      exists &&
      current.title === input.title &&
      current.summary === input.summary &&
      current.content === (input.content ?? "")
    ) {
      this.link(id, links, input.summary);
      return { record_id: id, status: "no_op" };
    }
    const confidence = input.confidence ?? pyFloat(0.8),
      key = `${this.profile.name}:${id}:${digest([input.title, input.summary, input.content ?? "", typeof confidence === "number" ? pyFloat(confidence) : confidence])}`;
    const result = this.runtime.submit({
      operation_type: exists ? "refine" : "create",
      record_id: id,
      record_class: exists ? null : "belief",
      domain: exists ? null : "fact",
      actor: this.profile.agent,
      reason: "self-logged fact",
      logic: "the agent recorded what it currently holds",
      truth_basis: "provenance is attached",
      evidence: this.evidence(input.sourceRef || `self:${factId}`, input.summary, confidence, input.evidence ?? []),
      idempotency_key: key,
      changes: {
        title: input.title,
        summary: input.summary,
        content: input.content ?? "",
        confidence,
        ...(exists ? {} : { accessibility: pyFloat(this.activation.newRecord) }),
      },
    });
    hooks.afterIdentitySubmitCommit?.();
    this.link(id, links, input.summary);
    this.cues(id, input.cues ?? [], input.title);
    let status = String(result.status);
    if (status === "materialized") status = exists ? "revised" : "created";
    return { record_id: id, status };
  }
  closeLoop(recordId: string, note: string, actor?: string): Record<string, unknown> {
    this.store.requireWritable();
    return this.store.retractRelation({
      fromRecordId: recordId,
      toRecordId: "anchor:open-loops",
      relationType: "open-loop",
      actor: actor ?? this.profile.agent,
      reason: note,
      surface: this.surface,
    });
  }
  decay(now?: string) {
    this.store.requireWritable();
    return runDecay(this.store, this.activation, PINNED, now);
  }
  retrieve(
    cue: string,
    options: { limit?: number; tokenBudget?: number; includeHistory?: boolean | null; track?: boolean } = {},
  ): Record<string, unknown> {
    const limit = options.limit ?? 10,
      tokenBudget = options.tokenBudget ?? 2400;
    let track = options.track ?? true;
    // Tracking writes only on a writable store. legacy-v4 (R2a Q1) and an
    // uninitialized store stay pure reads: recall degrades to track=false.
    if (this.store.schemaInfo().state !== "ready") track = false;
    const hits = this.retriever.retrieve(cue, {
      limit,
      tokenBudget: Math.max(tokenBudget * 8, 20000),
      includeHistory: options.includeHistory,
      minAccessibility: this.activation.dormantBelow,
      wakeRelationTypes: CAUSAL_RELATIONS,
    });
    if (track) {
      for (let i = 0; i < hits.length; i++) {
        const hit = hits[i];
        this.store.recordAccess({
          cue,
          recordId: String(hit.revision.record_id),
          revisionId: String(hit.revision.revision_id),
          retrievalReason: hit.reasons.slice(0, 5).join(","),
          rank: i + 1,
          surface: this.surface,
          gain: 0,
        });
        if (i === 0) hooks.afterFirstAccessCommit?.();
      }
    }
    const present = new Set(hits.map((h) => field(h.revision, "record_id")));
    for (const id of ["core", "vho-open-ontology-core"]) {
      if (!present.has(id)) {
        const r = this.store.currentView(id)[0];
        if (r) hits.push({ revision: r, score: 0, reasons: ["pinned"], history: [] });
      }
    }
    const ordered = [
      ...hits.filter((h) => PINNED.includes(field(h.revision, "record_id") as any)),
      ...hits.filter((h) => !PINNED.includes(field(h.revision, "record_id") as any)),
    ];
    if (track) applyRecall(this.store, ordered, this.activation, PINNED, this.clock);
    const packet = this.renderer.render(cue, ordered, {
      scope: "global",
      surface: this.surface,
      compact: false,
      tokenBudget,
    });
    let selfCount = 0;
    const items = ordered.map((hit) => {
      const r = hit.revision,
        e = this.store.evidenceForRevision(field(r, "revision_id")),
        self = e.length > 0 && e.every((x) => x.evidence_type === "self_log");
      if (self) selfCount++;
      return {
        record_id: r.record_id,
        domain: r.domain,
        title: r.title,
        summary: r.summary,
        revision: r.revision_number,
        state: stateOf(r, this.activation, PINNED),
        self_authored: self,
        reasons: hit.reasons.slice(0, 6),
        work: this.linkedWork(field(r, "content")),
      };
    });
    return {
      schema: "trajecta-identity-packet/v1",
      profile: this.profile.name,
      cue,
      memory_decides_truth: false,
      open_core_proposals: this.openCoreProposals(),
      open_discussions: this.openDiscussions(),
      open_loops: this.openLoops(),
      causal_neighbors: this.causalNeighbors(items.map((x) => String(x.record_id))),
      items,
      self_authored_share: items.length ? pyRound(selfCount / items.length, 2) : 0,
      packet,
    };
  }
  linkedWork(content: string) {
    return refsIn(content).map((ref) => this.work?.resolve(ref) ?? { ref, resolved: false });
  }
  corePropose(input: ProposalInput) {
    this.store.requireWritable();
    return this.authority.propose(input);
  }
  openCoreProposals() {
    return this.authority.openProposals();
  }
  issueCoreReceipt(id: string, outcome: "apply" | "reject", note: string, t: Terminal) {
    this.store.requireWritable();
    return this.authority.issueCore(id, outcome, note, t);
  }
  coreApply(id: string) {
    this.store.requireWritable();
    return this.authority.applyCore(id);
  }
  issueRetractReceipt(id: string, reason: string, t: Terminal) {
    this.store.requireWritable();
    return this.authority.issueRetract(id, reason, t);
  }
  retract(id: string) {
    this.store.requireWritable();
    return this.authority.applyRetract(id);
  }
  issueLegacyCloseReceipt(note: string, t: Terminal) {
    this.store.requireWritable();
    return this.authority.issueLegacyClose(note, t);
  }
  closeLegacyDiscussion(id: string) {
    this.store.requireWritable();
    return this.authority.applyLegacyClose(id);
  }
  openDiscussions() {
    const result: any[] = [];
    for (const r of this.store.activeRelationRows()) {
      if (r.relation_type !== "awaiting-discussion") continue;
      const h = this.store.relationHistory(
          field(r, "from_record_id"),
          field(r, "to_record_id"),
          field(r, "relation_type"),
        ),
        last = h.at(-1)!;
      result.push({ record_id: r.from_record_id, since: last.created_at, reason: last.reason });
    }
    return result;
  }
  openLoops() {
    const current = new Map(this.store.currentView().map((r) => [field(r, "record_id"), r])),
      result: any[] = [];
    for (const r of this.store.activeRelationRows()) {
      const id = field(r, "from_record_id");
      if (r.relation_type === "open-loop" && current.has(id))
        result.push({ record_id: id, title: current.get(id)!.title });
    }
    return result;
  }
  timeline(limit = 20) {
    const p = this.store.currentView().filter((r) => r.domain === "phase");
    p.sort((a, b) => compareCodePoint(occurred(b), occurred(a)));
    return p.slice(0, Math.max(1, Math.min(limit, 200))).map((r) => ({
      record_id: r.record_id,
      at: occurred(r),
      title: r.title,
      summary: r.summary,
      state: stateOf(r, this.activation, PINNED),
    }));
  }
  status() {
    const info = this.store.schemaInfo(),
      rows = info.state === "ready" || info.state === "legacy-v4" ? this.store.currentView() : [],
      activation: Record<string, number> = {},
      records: Record<string, number> = {};
    for (const r of rows) {
      if (r.domain === "anchor") continue;
      const s = stateOf(r, this.activation, PINNED);
      activation[s] = (activation[s] ?? 0) + 1;
      records[field(r, "domain")] = (records[field(r, "domain")] ?? 0) + 1;
    }
    return {
      schema: "trajecta-identity-status/v1",
      profile: this.profile.name,
      agent: this.profile.agent,
      db: this.displayDatabase,
      store: info.state,
      write_policy: "self-authored proposals; owner receipt controls canonical core",
      records,
      activation,
      open_discussions: rows.length ? this.openDiscussions().length : 0,
      open_core_proposals: rows.length ? this.openCoreProposals().length : 0,
      open_loops: rows.length ? this.openLoops().length : 0,
      work_store: this.work?.root ?? null,
    };
  }
  private causalNeighbors(ids: string[]) {
    const wanted = new Set(ids);
    return this.store
      .activeRelationRows()
      .filter(
        (r) =>
          CAUSAL_RELATIONS.includes(field(r, "relation_type")) &&
          (wanted.has(field(r, "from_record_id")) || wanted.has(field(r, "to_record_id"))),
      )
      .map((r) => ({
        from: field(r, "from_record_id"),
        relation: field(r, "relation_type"),
        to: field(r, "to_record_id"),
      }));
  }
}
export type { MemoryHit };
