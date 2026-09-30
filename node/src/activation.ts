import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, extname } from "node:path";
import { orderedObject, pyFloat, pyJsonDumps, pyRound } from "./encoding.ts";
import { asString, get, objectEntries, parseLossless } from "./json.ts";
import { isoFromMicros, parseIsoMicros, type Clock } from "./clock.ts";
import type { MemoryHit } from "./retrieval.ts";
import type { MemoryStore, Row } from "./store.ts";
import { hooks } from "./internal-hooks.ts";

export type ActivationPolicy = {
  newRecord: number;
  dormantBelow: number;
  fadingBelow: number;
  cap: number;
  directGain: number;
  graphGain: number;
  wakeTo: number;
  halfLifeDays: number;
};
export const DEFAULT_ACTIVATION: ActivationPolicy = {
  newRecord: 0.6,
  dormantBelow: 0.15,
  fadingBelow: 0.35,
  cap: 0.9,
  directGain: 0.08,
  graphGain: 0.03,
  wakeTo: 0.4,
  halfLifeDays: 21,
};
export function stateOf(row: Row, policy = DEFAULT_ACTIVATION, pinned: readonly string[] = []): string {
  if (pinned.includes(String(row.record_id))) return "pinned";
  const v = Number(row.accessibility);
  return v < policy.dormantBelow ? "dormant" : v < policy.fadingBelow ? "fading" : "active";
}
export function applyRecall(
  store: MemoryStore,
  hits: MemoryHit[],
  policy: ActivationPolicy,
  pinned: readonly string[],
  clock: Clock,
) {
  store.requireWritable();
  const adjustments: any[] = [];
  for (const hit of hits) {
    const id = String(hit.revision.record_id);
    if (pinned.includes(id)) continue;
    const current = store.currentView(id)[0];
    if (!current) continue;
    const value = Number(current.accessibility),
      direct = hit.reasons.some((r) => r.startsWith("cue:") || r.startsWith("lexical:"));
    let next =
      value >= policy.cap
        ? policy.cap
        : Math.min(policy.cap, value + (direct ? policy.directGain : policy.graphGain) * (1 - value / policy.cap));
    if (hit.reasons.some((r) => r.startsWith("woke:"))) next = Math.max(next, policy.wakeTo);
    if (Math.abs(next - value) > 1e-9)
      adjustments.push({ recordId: id, field: "accessibility", oldValue: value, newValue: pyRound(next, 6) });
  }
  if (adjustments.length)
    store.applyMaintenance({
      runId: `recall:${clock.micros()}`,
      adjustments,
      actor: "trajecta-identity",
      reason: "recall activation (diminishing gain, capped)",
      surface: "activation",
    });
  return adjustments;
}
function statePath(path: string): string {
  const ext = extname(path);
  return path.slice(0, path.length - ext.length) + ".activation.json";
}
export function runDecay(
  store: MemoryStore,
  policy: ActivationPolicy,
  pinned: readonly string[],
  now?: string,
): Record<string, unknown> {
  store.requireWritable();
  const moment = parseIsoMicros(now ?? new Date().toISOString().replace("Z", "+00:00")),
    path = statePath(store.path);
  let last: bigint | null = null;
  if (existsSync(path)) {
    const ast = parseLossless(readFileSync(path, "utf8"));
    objectEntries(ast);
    const value = get(ast as any, "last_decay_at");
    if (value) last = parseIsoMicros(asString(value));
  }
  const adjustments: any[] = [];
  const counts: Record<string, number> = {};
  for (const row of store.currentView()) {
    if (pinned.includes(String(row.record_id)) || row.domain === "anchor") continue;
    const created = parseIsoMicros(String(row.created_at)),
      access = row.last_accessed_at ? parseIsoMicros(String(row.last_accessed_at)) : null;
    const since = [last, access, created].filter((x): x is bigint => x !== null).reduce((a, b) => (a > b ? a : b));
    const days = Number(moment - since) / 1_000_000 / 86400,
      half = policy.halfLifeDays * (0.5 + Number(row.stability)),
      value = Number(row.accessibility),
      next = Math.min(policy.cap, value * Math.pow(0.5, Math.max(0, days) / half));
    if (Math.abs(next - value) > 1e-6)
      adjustments.push({
        recordId: String(row.record_id),
        field: "accessibility",
        oldValue: value,
        newValue: pyRound(next, 6),
      });
    const name = stateOf({ ...row, accessibility: next }, policy, pinned);
    counts[name] = (counts[name] ?? 0) + 1;
  }
  const stamp = isoFromMicros(moment, false);
  if (adjustments.length)
    store.applyMaintenance({
      runId: `decay:${stamp}`,
      adjustments,
      actor: "trajecta-identity",
      reason: "time decay (half-life scaled by stability) and cap",
      surface: "activation",
    });
  hooks.beforeDecaySidecarWrite?.();
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, pyJsonDumps(orderedObject([["last_decay_at", stamp]])));
  return { at: stamp, adjusted: adjustments.length, states: counts };
}
