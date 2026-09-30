import { compareCodePoint, pyRound, utcNowSeconds } from "./encoding.ts";
import { PacketRenderer } from "./packet.ts";
import { memoryProfile, type IdentityProfile } from "./profile.ts";
import { CueDrivenRetriever, type MemoryHit } from "./retrieval.ts";
import { MemoryStore, type Row } from "./store.ts";

export const CAUSAL_RELATIONS = ["later-phase-of", "caused-by", "depends-on", "decided-because"];
export const PINNED = ["core", "vho-open-ontology-core", "anchor:discussions", "anchor:open-loops"];

const field = (row: Row, key: string): string => String(row[key] ?? "");

function stateOf(row: Row): string {
  if (PINNED.includes(field(row, "record_id"))) return "pinned";
  const value = Number(row.accessibility);
  if (value < 0.15) return "dormant";
  if (value < 0.35) return "fading";
  return "active";
}

function occurred(row: Row): string {
  const match = /^Occurred at: (.+)$/mu.exec(field(row, "content"));
  return match ? match[1].trim() : field(row, "valid_from") || field(row, "created_at");
}

function workRefs(content: string): { ref: string; resolved: false }[] {
  const match = /^Work refs \(trajecta-work-memory\): (.+)$/mu.exec(content);
  if (!match) return [];
  return match[1].split(",").map((value) => value.trim()).filter(Boolean).map((ref) => ({ ref, resolved: false }));
}

export type IdentityOptions = { surface?: string; now?: () => string; displayDatabase?: string };

export class IdentityMemory {
  readonly profile: IdentityProfile;
  readonly store: MemoryStore;
  readonly surface: string;
  readonly retriever: CueDrivenRetriever;
  readonly renderer: PacketRenderer;
  readonly displayDatabase: string;

  constructor(profile: IdentityProfile, database: string, options: IdentityOptions = {}) {
    this.profile = profile;
    this.store = new MemoryStore(database);
    this.surface = options.surface ?? "local";
    const coreProfile = memoryProfile(profile);
    this.retriever = new CueDrivenRetriever(this.store, coreProfile);
    this.renderer = new PacketRenderer(coreProfile, options.now ?? utcNowSeconds);
    this.displayDatabase = options.displayDatabase ?? this.store.path;
  }

  close(): void { this.store.close(); }

  retrieve(cue: string, options: { limit?: number; tokenBudget?: number; includeHistory?: boolean | null; track?: false } = {}): Record<string, unknown> {
    const limit = options.limit ?? 10;
    const tokenBudget = options.tokenBudget ?? 2400;
    const hits = this.retriever.retrieve(cue, {
      limit, tokenBudget: Math.max(tokenBudget * 8, 20000), includeHistory: options.includeHistory,
      minAccessibility: 0.15, wakeRelationTypes: CAUSAL_RELATIONS,
    });
    const present = new Set(hits.map((hit) => field(hit.revision, "record_id")));
    for (const recordId of ["core", "vho-open-ontology-core"]) {
      if (!present.has(recordId)) {
        const row = this.store.currentView(recordId)[0];
        if (row) hits.push({ revision: row, score: 0, reasons: ["pinned"], history: [] });
      }
    }
    const ordered = [
      ...hits.filter((hit) => PINNED.includes(field(hit.revision, "record_id"))),
      ...hits.filter((hit) => !PINNED.includes(field(hit.revision, "record_id"))),
    ];
    const packet = this.renderer.render(cue, ordered, { scope: "global", surface: this.surface, compact: false, tokenBudget });
    let selfCount = 0;
    const items = ordered.map((hit) => {
      const revision = hit.revision;
      const evidence = this.store.evidenceForRevision(field(revision, "revision_id"));
      const selfAuthored = evidence.length > 0 && evidence.every((item) => item.evidence_type === "self_log");
      if (selfAuthored) selfCount++;
      return {
        record_id: revision.record_id, domain: revision.domain, title: revision.title, summary: revision.summary,
        revision: revision.revision_number, state: stateOf(revision), self_authored: selfAuthored,
        reasons: hit.reasons.slice(0, 6), work: workRefs(field(revision, "content")),
      };
    });
    return {
      schema: "trajecta-identity-packet/v1", profile: this.profile.name, cue, memory_decides_truth: false,
      open_discussions: this.openDiscussions(), open_loops: this.openLoops(),
      causal_neighbors: this.causalNeighbors(items.map((item) => String(item.record_id))), items,
      self_authored_share: items.length ? pyRound(selfCount / items.length, 2) : 0, packet,
    };
  }

  openDiscussions(): Record<string, unknown>[] {
    const result: Record<string, unknown>[] = [];
    for (const row of this.store.activeRelationRows()) {
      if (row.relation_type !== "awaiting-discussion") continue;
      const history = this.store.relationHistory(field(row, "from_record_id"), field(row, "to_record_id"), field(row, "relation_type"));
      const last = history.at(-1)!;
      result.push({ record_id: row.from_record_id, since: last.created_at, reason: last.reason });
    }
    return result;
  }

  openLoops(): Record<string, unknown>[] {
    const current = new Map(this.store.currentView().map((row) => [field(row, "record_id"), row]));
    const result: Record<string, unknown>[] = [];
    for (const row of this.store.activeRelationRows()) {
      const recordId = field(row, "from_record_id");
      if (row.relation_type === "open-loop" && current.has(recordId)) result.push({ record_id: recordId, title: current.get(recordId)!.title });
    }
    return result;
  }

  timeline(limit = 20): Record<string, unknown>[] {
    const phases = this.store.currentView().filter((row) => row.domain === "phase");
    // Python: sort(key=_occurred, reverse=True), stable, by code point.
    phases.sort((a, b) => compareCodePoint(occurred(b), occurred(a)));
    return phases.slice(0, Math.max(1, Math.min(limit, 200))).map((row) => ({
      record_id: row.record_id, at: occurred(row), title: row.title, summary: row.summary, state: stateOf(row),
    }));
  }

  status(): Record<string, unknown> {
    const info = this.store.schemaInfo();
    const rows = info.state === "ready" ? this.store.currentView() : [];
    const activation: Record<string, number> = {};
    const records: Record<string, number> = {};
    for (const row of rows) {
      if (row.domain === "anchor") continue;
      const state = stateOf(row);
      activation[state] = (activation[state] ?? 0) + 1;
      const domain = field(row, "domain");
      records[domain] = (records[domain] ?? 0) + 1;
    }
    return {
      schema: "trajecta-identity-status/v1", profile: this.profile.name, agent: this.profile.agent,
      db: this.displayDatabase, store: info.state,
      write_policy: "self-authored: phase append-only, fact revisable, core revisable + discuss",
      records, activation, open_discussions: rows.length ? this.openDiscussions().length : 0,
      open_loops: rows.length ? this.openLoops().length : 0, work_store: null,
    };
  }

  private causalNeighbors(recordIds: string[]): Record<string, string>[] {
    const wanted = new Set(recordIds);
    return this.store.activeRelationRows().filter((row) => CAUSAL_RELATIONS.includes(field(row, "relation_type")) &&
      (wanted.has(field(row, "from_record_id")) || wanted.has(field(row, "to_record_id"))))
      .map((row) => ({ from: field(row, "from_record_id"), relation: field(row, "relation_type"), to: field(row, "to_record_id") }));
  }
}

export type { MemoryHit };
