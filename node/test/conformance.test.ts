import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { cpSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import {
  CAUSAL_RELATIONS,
  CueDrivenRetriever,
  IdentityMemory,
  IncompatibleJournalMode,
  MemoryStore,
  MigrationRequired,
  PacketRenderer,
  SchemaVersionError,
  floatHex,
  loadProfile,
  memoryProfile,
} from "../src/index.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..", "..");
const GOLDEN = resolve(ROOT, "spec", "golden");
const PROFILE = loadProfile(resolve(ROOT, "trajecta_identity", "profiles", "example", "profile.json"));

const sha256 = (path: string): string => createHash("sha256").update(readFileSync(path)).digest("hex");
const rows = (path: string): any[] =>
  readFileSync(path, "utf8")
    .trim()
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line));
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value));

test("conformance runs on the pinned Node major with node:sqlite", () => {
  assert.equal(process.versions.node.split(".")[0], "22");
  assert.match(process.versions.sqlite ?? "", /^3\./u);
});

test("golden manifest authenticates every corpus artifact and normalization table", () => {
  const manifest = JSON.parse(readFileSync(resolve(GOLDEN, "MANIFEST.json"), "utf8"));
  assert.equal(manifest.schema, "trajecta.golden-manifest/v1");
  const corpusFiles: string[] = [];
  const visit = (directory: string): void => {
    for (const name of readdirSync(directory).sort()) {
      const path = resolve(directory, name);
      if (statSync(path).isDirectory()) visit(path);
      else if (path !== resolve(GOLDEN, "MANIFEST.json"))
        corpusFiles.push(path.slice(GOLDEN.length + 1).replaceAll("\\", "/"));
    }
  };
  visit(GOLDEN);
  corpusFiles.push("tables/identity-v1.json", "tables/text-norm-v2.json", "tables/version-sensitive.json");
  assert.deepEqual(corpusFiles.sort(), Object.keys(manifest.files).sort(), "MANIFEST file inventory");
  for (const [relative, expected] of Object.entries(manifest.files)) {
    const path = relative.startsWith("tables/") ? resolve(ROOT, "memory_core", relative) : resolve(GOLDEN, relative);
    assert.equal(sha256(path), expected, relative);
  }
  assert.deepEqual(
    readFileSync(resolve(ROOT, "node", "schema.sql")),
    readFileSync(resolve(ROOT, "memory_core", "schema.sql")),
  );
  assert.equal(
    sha256(resolve(ROOT, "node", "schema.sql")),
    "a8f283d76ad0966ab82f395583e30849a007814e67eae554e3d967a75918c10c",
  );
  for (const table of ["identity-v1.json", "text-norm-v2.json", "version-sensitive.json"]) {
    assert.deepEqual(
      readFileSync(resolve(ROOT, "node", "tables", table)),
      readFileSync(resolve(ROOT, "memory_core", "tables", table)),
    );
  }
});

function hit(item: any): any {
  return {
    record_id: item.revision.record_id,
    score_hex: floatHex(item.score),
    reasons: item.reasons,
    history: item.history,
  };
}

function packetTime(packet: string): string {
  const match = /^Generated: (.+)$/mu.exec(packet);
  assert.ok(match, "packet has Generated line");
  return match[1];
}

function runOrdinary(database: string, expected: any): any {
  const input = expected.input;
  const profile = memoryProfile(PROFILE);
  const store = new MemoryStore(database);
  const options = {
    scope: input.scope,
    includeHistory: input.include_history,
    minAccessibility: 0.15,
    wakeRelationTypes: CAUSAL_RELATIONS,
  };
  const retriever = new CueDrivenRetriever(store, profile);
  const ranked = retriever.retrieve(input.cue, { ...options, limit: 1000, tokenBudget: 100000000 });
  const selected = retriever.retrieve(input.cue, { ...options, limit: input.limit, tokenBudget: input.token_budget });
  let packetText: string | null = null;
  let packetError: string | null = null;
  try {
    const time = expected.packet_text ? packetTime(expected.packet_text) : "unused";
    packetText = new PacketRenderer(profile, () => time).render(input.cue, selected, {
      scope: input.scope,
      surface: "golden",
      tokenBudget: input.token_budget,
    });
  } catch (error) {
    packetError = (error as Error).message;
  }
  let identityPacket: any;
  try {
    const time = expected.identity_packet_json?.packet ? packetTime(expected.identity_packet_json.packet) : "unused";
    const identity = new IdentityMemory(PROFILE, database, {
      surface: "golden",
      now: () => time,
      displayDatabase: "store.sqlite3",
    });
    identityPacket = identity.retrieve(input.cue, {
      limit: input.limit,
      tokenBudget: Math.max(2400, input.token_budget),
      includeHistory: input.include_history,
      track: false,
    });
    identity.close();
  } catch (error) {
    identityPacket = { error: (error as Error).name, message: (error as Error).message };
  }
  const identity = new IdentityMemory(PROFILE, database, {
    surface: "golden",
    now: () => "unused",
    displayDatabase: "store.sqlite3",
  });
  const actual = {
    label: expected.label,
    input: clone(input),
    current_view: store.currentView(),
    historical_view:
      expected.historical_view === null ? null : store.historicalView(expected.historical_view[0].record_id),
    ranked_hits: ranked.map(hit),
    selected_hits: selected.map(hit),
    packet_text: packetText,
    packet_error: packetError,
    identity_packet_json: identityPacket,
    status: identity.status(),
    timeline: identity.timeline(),
    open_discussions: identity.openDiscussions(),
    open_loops: identity.openLoops(),
  };
  identity.close();
  store.close();
  return actual;
}

function corpusError(error: unknown): { error: string; message: string } {
  if (error instanceof MigrationRequired) return { error: "MigrationRequiredError", message: error.message };
  if (error instanceof SchemaVersionError) return { error: "SchemaVersionError", message: error.message };
  throw error;
}

function runSpecial(database: string, expected: any): any {
  const store = new MemoryStore(database);
  try {
    if (expected.label === "migrated-v2")
      return { status: "migrated", current_view: store.currentView(), source_byte_identical: true };
    if (expected.label === "migrated-v3")
      return {
        status: "migrated",
        current_view: store.currentView(),
        active_relations: store.activeRelationRows(),
        source_byte_identical: true,
      };
    store.currentView();
    assert.fail("incompatible store unexpectedly opened");
  } catch (error) {
    const result: Record<string, unknown> = corpusError(error);
    if (expected.label === "legacy-v3") result.byte_identical_after_read = true;
    return result;
  } finally {
    store.close();
  }
}

const scenarios = readdirSync(GOLDEN, { withFileTypes: true })
  .filter((entry) => entry.isDirectory())
  .map((entry) => entry.name)
  .sort();
for (const scenario of scenarios) {
  const casePath = resolve(GOLDEN, scenario, "cases.jsonl");
  for (const expected of rows(casePath)) {
    test(`corpus ${scenario}/${expected.label}`, () => {
      const source = resolve(GOLDEN, scenario, "store.sqlite3");
      const sourceBefore = readFileSync(source);
      const directory = mkdtempSync(resolve(tmpdir(), "trajecta-r1-"));
      const database = resolve(directory, "store.sqlite3");
      cpSync(source, database);
      const copyBefore = readFileSync(database);
      try {
        const actual = expected.result === undefined ? runOrdinary(database, expected) : runSpecial(database, expected);
        const oracle = expected.result === undefined ? clone(expected) : expected.result;
        if (expected.result === undefined) {
          delete oracle.write_outcomes;
          // Additive-field rule (R2a §2.1). Closed list; same as tests/test_r0_corpus_on_v5.py.
          assert.equal(oracle.status.store, "ready");
          assert.notEqual(oracle.status.write_policy, "self-authored proposals; owner receipt controls canonical core");
          assert.equal("open_core_proposals" in oracle.identity_packet_json, false);
          assert.equal("open_core_proposals" in oracle.status, false);
          if (!("error" in oracle.identity_packet_json)) oracle.identity_packet_json.open_core_proposals = [];
          oracle.status.store = "legacy-v4";
          oracle.status.write_policy = "self-authored proposals; owner receipt controls canonical core";
          oracle.status.open_core_proposals = 0;
        }
        if (expected.label === "legacy-v3")
          assert.equal(oracle.message, "schema v3 store must be initialized or migrated to v4 before use");
        if (expected.label === "legacy-v3")
          oracle.message = "schema v3 store must be initialized or migrated to v5 before use";
        assert.deepEqual(actual, oracle);
        assert.deepEqual(readFileSync(database), copyBefore, "read changed the copied store");
        assert.deepEqual(readFileSync(source), sourceBefore, "read changed the checked-in store");
      } finally {
        rmSync(directory, { recursive: true, force: true });
      }
    });
  }
}

function snapshot(directory: string): Map<string, Buffer> {
  return new Map(
    readdirSync(directory)
      .sort()
      .map((name) => [name, readFileSync(resolve(directory, name))]),
  );
}

test("WAL header is refused before SQLite opens and fixture bytes stay unchanged", () => {
  const directory = mkdtempSync(resolve(tmpdir(), "trajecta-r1-wal-header-"));
  try {
    const database = resolve(directory, "store.sqlite3");
    const bytes = Buffer.from(readFileSync(resolve(GOLDEN, "recall", "store.sqlite3")));
    bytes[18] = 2;
    bytes[19] = 2;
    writeFileSync(database, bytes);
    const before = snapshot(directory);
    assert.throws(() => new MemoryStore(database).currentView(), IncompatibleJournalMode);
    assert.deepEqual(snapshot(directory), before);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

for (const suffix of ["-wal", "-shm"]) {
  test(`${suffix} sidecar is refused before SQLite opens and fixture bytes stay unchanged`, () => {
    const directory = mkdtempSync(resolve(tmpdir(), "trajecta-r1-wal-sidecar-"));
    try {
      const database = resolve(directory, "store.sqlite3");
      cpSync(resolve(GOLDEN, "recall", "store.sqlite3"), database);
      writeFileSync(database + suffix, Buffer.from(`fixture:${suffix}`));
      const before = snapshot(directory);
      assert.throws(() => new MemoryStore(database).currentView(), IncompatibleJournalMode);
      assert.deepEqual(snapshot(directory), before);
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });
}
