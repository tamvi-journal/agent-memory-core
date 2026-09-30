import { codePointSlice, compareCodePoint, pyFixed, pyStrip } from "./encoding.ts";
import type { MemoryProfile } from "./profile.ts";
import type { MemoryHit } from "./retrieval.ts";

export const PACKET_BUDGET_ESTIMATOR = "deterministic-utf8-quarter/v1";
export const estimatePacketTokens = (text: string): number => Math.max(1, Math.ceil(Buffer.byteLength(text, "utf8") / 4));

const field = (hit: MemoryHit, key: string): string => String(hit.revision[key] ?? "");

export class PacketRenderer {
  readonly profile: MemoryProfile;
  readonly now: () => string;

  constructor(profile: MemoryProfile, now: () => string) { this.profile = profile; this.now = now; }

  render(query: string, hits: MemoryHit[], options: { scope: string; surface: string; compact?: boolean; tokenBudget?: number }): string {
    const compact = options.compact ?? true;
    const tokenBudget = options.tokenBudget ?? 1800;
    if (tokenBudget < 64) throw new RangeError("token_budget must be at least 64");
    const prefix = [
      `# ${this.profile.packetTitle}`, "", `Generated: ${this.now()}`,
      `Cue: ${codePointSlice(query, 240)}`, `Scope: ${options.scope}`, `Surface: ${options.surface}`,
      `Budget: ${tokenBudget} (${PACKET_BUDGET_ESTIMATOR})`, "",
      "> Memory is evidence and orientation, not authority.",
      "> Current revisions are shown by default; history is cue-driven.", "",
    ];
    const suffix = this.profile.instructions.length ? ["## Execution instruction", "", ...this.profile.instructions] : [];
    const mandatory = this.join([...prefix, ...suffix]);
    if (estimatePacketTokens(mandatory) > tokenBudget) throw new RangeError("token_budget is too small for packet framing and instructions");
    let selected: MemoryHit[] = [];
    for (const hit of hits) {
      const proposed = [...selected, hit];
      const candidate = this.join([...prefix, ...this.body(proposed, compact), ...suffix]);
      if (estimatePacketTokens(candidate) <= tokenBudget) selected = proposed;
    }
    const packet = this.join([...prefix, ...this.body(selected, compact), ...suffix]);
    if (estimatePacketTokens(packet) > tokenBudget) throw new Error("packet renderer violated its hard budget");
    return packet;
  }

  private join(lines: string[]): string { return pyStrip(lines.join("\n")) + "\n"; }

  private body(hits: MemoryHit[], compact: boolean): string[] {
    const groups = new Map<string, MemoryHit[]>();
    for (const hit of hits) {
      const domain = field(hit, "domain");
      if (!groups.has(domain)) groups.set(domain, []);
      groups.get(domain)!.push(hit);
    }
    const ordered = [...this.profile.sectionOrder];
    ordered.push(...[...groups.keys()].filter((domain) => !ordered.includes(domain)).sort(compareCodePoint));
    const lines: string[] = [];
    for (const domain of ordered) {
      const domainHits = groups.get(domain) ?? [];
      if (!domainHits.length) continue;
      const label = this.profile.sectionLabels[domain] ?? domain.replaceAll("_", " ").replace(/(^|\s)([a-z])/g, (_, space, char) => space + char.toUpperCase());
      lines.push(`## ${label}`, "");
      for (const hit of domainHits) lines.push(...this.hitBlock(hit, compact));
    }
    return lines;
  }

  private hitBlock(hit: MemoryHit, compact: boolean): string[] {
    const content = field(hit, "content");
    const summary = field(hit, "summary");
    const lines = [`### ${field(hit, "title")}`, summary || codePointSlice(content, 350)];
    if (!compact && content && content !== summary) lines.push(content);
    lines.push(`- memory_id: \`${field(hit, "record_id")}\` | revision: \`${field(hit, "revision_id")}\` | class: ${field(hit, "record_class")} | confidence: ${pyFixed(Number(hit.revision.confidence), 2)}`);
    lines.push("- retrieval: " + hit.reasons.slice(0, 5).join(", "));
    if (hit.history.length) {
      lines.push(`- history: ${hit.history.length} revisions`);
      for (const old of hit.history) lines.push(`  - r${old.revision_number} [${old.revision_status}]: ${old.summary || old.title}`);
    }
    lines.push("");
    return lines;
  }
}
