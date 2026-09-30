import { compareCodePoint, pyFixed, pySplit } from "./encoding.ts";
import type { MemoryProfile } from "./profile.ts";
import { MemoryStore, type Row } from "./store.ts";
import { normalizeText, tokens } from "./text.ts";

export type MemoryHit = { revision: Row; score: number; reasons: string[]; history: Row[] };
export type RetrieveOptions = {
  scope?: string;
  limit?: number;
  tokenBudget?: number;
  includeHistory?: boolean | null;
  minAccessibility?: number | null;
  wakeOnDirectCue?: boolean;
  wakeRelationTypes?: string[];
};

const string = (row: Row, key: string): string => String(row[key] ?? "");
const number = (row: Row, key: string): number => Number(row[key]);

export class CueDrivenRetriever {
  readonly store: MemoryStore;
  readonly profile: MemoryProfile;

  constructor(store: MemoryStore, profile: MemoryProfile) {
    this.store = store;
    this.profile = profile;
  }

  retrieve(query: string, options: RetrieveOptions = {}): MemoryHit[] {
    const scope = options.scope ?? "global";
    const limit = options.limit ?? 10;
    const tokenBudget = options.tokenBudget ?? 1800;
    const minAccessibility = options.minAccessibility ?? null;
    const wakeOnDirectCue = options.wakeOnDirectCue ?? true;
    const wakeTypes = new Set(options.wakeRelationTypes ?? []);
    if (minAccessibility !== null && (minAccessibility < 0 || minAccessibility > 1)) {
      throw new RangeError("min_accessibility must be between 0 and 1");
    }
    const normalized = normalizeText(query);
    const queryTokens = new Set(tokens(query));
    const revisions = new Map<string, Row>();
    for (const row of this.store.currentView()) {
      if (
        row.authority_status !== "non_authoritative" &&
        row.record_class !== "unclassified" &&
        ["global", scope].includes(string(row, "scope"))
      ) {
        revisions.set(string(row, "record_id"), row);
      }
    }
    const scores = new Map([...revisions.keys()].map((id) => [id, 0]));
    const reasons = new Map([...revisions.keys()].map((id) => [id, [] as string[]]));
    const direct = new Set<string>();
    const bootstrap = new Set(this.profile.bootstrapRecordIds);
    const dormant = new Set<string>();
    if (minAccessibility !== null) {
      for (const [id, revision] of revisions) {
        if (!bootstrap.has(id) && number(revision, "accessibility") < minAccessibility) dormant.add(id);
      }
    }
    const wake = (id: string, why: string) => {
      dormant.delete(id);
      reasons.get(id)!.push(`woke:${why}`);
    };

    const cues: { cue: string; cue_norm: string; target_record_id: string; weight: number }[] = this.store
      .cueRows(this.profile.name, scope)
      .map((row) => ({
        cue: string(row, "cue"),
        cue_norm: normalizeText(string(row, "cue")),
        target_record_id: string(row, "target_record_id"),
        weight: number(row, "weight"),
      }));
    cues.push(
      ...this.profile.cueAliases.map(([cue, target, weight]) => ({
        cue,
        cue_norm: normalizeText(cue),
        target_record_id: target,
        weight,
      })),
    );
    const strongest = new Map<string, (typeof cues)[number]>();
    for (const cue of cues) {
      const key = `${cue.cue_norm}\0${cue.target_record_id}`;
      const current = strongest.get(key);
      if (!current || cue.weight > current.weight) strongest.set(key, cue);
    }
    for (const cue of strongest.values()) {
      const target = cue.target_record_id;
      if (!revisions.has(target) || (dormant.has(target) && !wakeOnDirectCue)) continue;
      const cueTokens = new Set(tokens(cue.cue_norm));
      let intersection = 0;
      for (const token of queryTokens) if (cueTokens.has(token)) intersection++;
      const exact = cue.cue_norm.length > 0 && normalized.includes(cue.cue_norm);
      const overlap = intersection / Math.max(1, cueTokens.size);
      if (exact || overlap >= 0.8) {
        const gain = cue.weight * (exact ? 1.45 : 0.9 * overlap);
        if (dormant.has(target)) wake(target, "direct-cue");
        scores.set(target, scores.get(target)! + gain);
        reasons.get(target)!.push(`cue:${cue.cue}`);
        direct.add(target);
      }
    }

    for (const [id, revision] of revisions) {
      if (dormant.has(id)) continue;
      const memoryTokens = new Set(
        tokens(`${string(revision, "title")}\n${string(revision, "summary")}\n${string(revision, "content")}`),
      );
      if (queryTokens.size && memoryTokens.size) {
        let overlapCount = 0;
        for (const token of queryTokens) if (memoryTokens.has(token)) overlapCount++;
        const overlap = overlapCount / Math.max(1, queryTokens.size);
        if (overlap) {
          scores.set(id, scores.get(id)! + Math.min(1, overlap) * 0.9);
          reasons.get(id)!.push(`lexical:${pyFixed(overlap, 2)}`);
        }
        const titleTokens = new Set(tokens(string(revision, "title")));
        let titleCount = 0;
        for (const token of queryTokens) if (titleTokens.has(token)) titleCount++;
        const titleOverlap = titleCount / Math.max(1, queryTokens.size);
        if (titleOverlap) {
          scores.set(id, scores.get(id)! + Math.min(1, titleOverlap) * 0.6);
          reasons.get(id)!.push(`title:${pyFixed(titleOverlap, 2)}`);
        }
      }
    }
    for (const id of this.profile.bootstrapRecordIds) {
      if (revisions.has(id)) {
        scores.set(id, scores.get(id)! + 0.34);
        reasons.get(id)!.push("bootstrap");
      }
    }

    const relations = this.store.activeRelationRows();
    let frontier = new Map([...direct].sort(compareCodePoint).map((id) => [id, scores.get(id)!]));
    for (const depth of [1, 2]) {
      const next = new Map<string, number>();
      for (const [source, activation] of frontier) {
        for (const relation of relations) {
          let target: string;
          if (relation.from_record_id === source) target = string(relation, "to_record_id");
          else if (relation.to_record_id === source) target = string(relation, "from_record_id");
          else continue;
          if (!revisions.has(target)) continue;
          if (dormant.has(target)) {
            if (!wakeTypes.has(string(relation, "relation_type"))) continue;
            wake(target, `relation:${relation.relation_type}`);
          }
          const factor = depth === 1 ? 0.46 : 0.46 * 0.46;
          const gain = activation * number(relation, "weight") * factor;
          if (gain < 0.05) continue;
          scores.set(target, scores.get(target)! + gain);
          reasons.get(target)!.push(`graph:${source}-[${relation.relation_type}]->${target}:d${depth}`);
          if (!next.has(target) || gain > next.get(target)!) next.set(target, gain);
        }
      }
      frontier = next;
    }

    for (const [id, revision] of revisions) {
      if (scores.get(id)! <= 0 || dormant.has(id)) continue;
      scores.set(id, scores.get(id)! + number(revision, "confidence") * 0.28);
      scores.set(id, scores.get(id)! + number(revision, "salience") * 0.14);
      scores.set(id, scores.get(id)! + number(revision, "stability") * 0.12);
      scores.set(id, scores.get(id)! + number(revision, "accessibility") * 0.08);
    }
    const ranked: MemoryHit[] = [];
    for (const [id, score] of scores) {
      if (score >= 0.24 && !dormant.has(id))
        ranked.push({ revision: revisions.get(id)!, score, reasons: reasons.get(id)!, history: [] });
    }
    ranked.sort(
      (a, b) => b.score - a.score || compareCodePoint(string(a.revision, "record_id"), string(b.revision, "record_id")),
    );
    const wantsHistory =
      options.includeHistory !== undefined && options.includeHistory !== null
        ? options.includeHistory
        : this.profile.historyMarkers.some((marker) => normalized.includes(normalizeText(marker)));
    const selected: MemoryHit[] = [];
    let spent = 0;
    for (const hit of ranked) {
      const estimate = Math.max(
        1,
        pySplit(string(hit.revision, "title") + string(hit.revision, "summary") + string(hit.revision, "content"))
          .length * 2,
      );
      if (selected.length && spent + estimate > tokenBudget) continue;
      if (selected.length >= limit) break;
      if (wantsHistory) hit.history = this.store.historicalView(string(hit.revision, "record_id"));
      selected.push(hit);
      spent += estimate;
    }
    return selected;
  }
}
