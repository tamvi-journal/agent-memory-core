import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { asArray, asString, get, objectEntries, parseLossless } from "./json.ts";
import type { JsonValue, OrderedObject } from "./encoding.ts";
export class WorkStoreError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "WorkStoreError";
  }
}
const object = (v: JsonValue | undefined): OrderedObject => {
  objectEntries(v as JsonValue);
  return v as OrderedObject;
};
const plain = (v: JsonValue): any => {
  if (v === null || typeof v === "string" || typeof v === "boolean") return v;
  if (Array.isArray(v)) return v.map(plain);
  if (v.kind === "int") return Number(v.value);
  if (v.kind === "float") return v.value;
  return Object.fromEntries(v.entries.map(([k, i]) => [k, plain(i)]));
};
export class WorkStore {
  readonly root: string;
  constructor(root: string) {
    this.root = root;
  }
  state(): OrderedObject {
    const path = join(this.root, "state.json");
    if (!existsSync(path)) return object(parseLossless('{"schema":"trajecta.state/v1","work":[]}'));
    let state: OrderedObject;
    try {
      state = object(parseLossless(readFileSync(path, "utf8")));
    } catch {
      throw new WorkStoreError(`unreadable work state: ${path}`);
    }
    const work = get(state, "work");
    if (asString(get(state, "schema")) !== "trajecta.state/v1" || !Array.isArray(work))
      throw new WorkStoreError(`unsupported work state schema in ${path}: '${asString(get(state, "schema"))}'`);
    return state;
  }
  deltas(): OrderedObject[] {
    const path = join(this.root, "deltas.jsonl");
    if (!existsSync(path)) return [];
    const result: OrderedObject[] = [];
    for (const raw of readFileSync(path, "utf8").split(/\r?\n/u)) {
      const line = raw.trim();
      if (!line) continue;
      try {
        result.push(object(parseLossless(line)));
      } catch {
        continue;
      }
    }
    return result;
  }
  resolve(ref: string): Record<string, unknown> | null {
    ref = ref.trim();
    if (ref.startsWith("work:")) {
      for (const item of asArray(get(this.state(), "work"))) {
        const x = object(item);
        if (asString(get(x, "id")) === ref) return this.summarize(x);
      }
      return null;
    }
    if (ref.startsWith("delta:")) {
      for (const item of this.deltas()) {
        if (asString(get(item, "id")) === ref)
          return {
            ref,
            kind: "delta",
            delta_kind: plain(get(item, "kind")!),
            summary: asString(get(item, "summary")),
            revision: plain(get(item, "revision")!),
            created_at: plain(get(item, "createdAt")!),
            work: this.resolve(asString(get(item, "workId"))),
          };
      }
      return null;
    }
    return null;
  }
  workItems(): Record<string, unknown>[] {
    return asArray(get(this.state(), "work")).map((item) => this.summarize(object(item)));
  }
  missing(refs: string[]): string[] {
    return refs.filter((ref) => this.resolve(ref) === null);
  }
  summarize(item: OrderedObject): Record<string, unknown> {
    const active = asString(get(item, "activeBranchId"));
    const branch = (get(item, "branches") === undefined ? [] : asArray(get(item, "branches")))
      .map(object)
      .find((x) => asString(get(x, "id")) === active);
    return {
      ref: plain(get(item, "id")!),
      kind: "work",
      topic: asString(get(item, "topic")),
      goal: asString(get(item, "goal")),
      status: plain(get(item, "status")!),
      revision: plain(get(item, "revision")!),
      next_action: plain(get(item, "nextAction")!),
      open_loops: (get(item, "openLoops") === undefined ? [] : asArray(get(item, "openLoops"))).map(plain),
      active_branch: branch ? asString(get(branch, "label")) : null,
      updated_at: plain(get(item, "updatedAt")!),
    };
  }
}
