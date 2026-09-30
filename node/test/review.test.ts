// Aux review (R1): behaviours the corpus does not exercise directly, pinned
// against values produced by the Python oracle.
import assert from "node:assert/strict";
import { copyFileSync, mkdtempSync, readFileSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import test from "node:test";
import { fileURLToPath } from "node:url";
import {
  IdentityMemory, MemoryStore, SchemaVersionError, canonicalJson, compareCodePoint, loadProfile, parseLossless, pyTitle,
  utcNowSeconds,
} from "../src/index.ts";
import { hooks } from "../src/internal-hooks.ts";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const GOLDEN = resolve(ROOT, "spec", "golden");
const PROFILE = loadProfile(resolve(ROOT, "trajecta_identity", "profiles", "example", "profile.json"));

test("status reports the schema state of stores it will not read, like Python schema_info", () => {
  // Python: IdentityMemory(...).status()["store"] on copies of these corpus stores.
  const expected: Record<string, string> = {
    "legacy-v3": "legacy-v3", "foreign-app": "incompatible", "future-schema": "incompatible", "empty-file": "unknown",
  };
  const directory = mkdtempSync(resolve(tmpdir(), "trajecta-r1-status-"));
  for (const [scenario, state] of Object.entries(expected)) {
    const source = resolve(GOLDEN, scenario, "store.sqlite3");
    const copy = resolve(directory, `${scenario}.sqlite3`);
    copyFileSync(source, copy);
    const before = readFileSync(copy);
    const memory = new IdentityMemory(PROFILE, copy);
    const status = memory.status() as Record<string, unknown>;
    memory.close();
    assert.equal(status.store, state, scenario);
    assert.deepEqual(status.records, {}, scenario);
    assert.deepEqual(readFileSync(copy), before, `${scenario} bytes unchanged`);
  }
  assert.deepEqual(readdirSync(directory).sort(), Object.keys(expected).map((name) => `${name}.sqlite3`).sort());
});

test("default clock matches Python isoformat(timespec='seconds') in UTC", () => {
  assert.match(utcNowSeconds(), /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$/u);
});

test("repeated JSON keys keep the first position and the last value, like Python json", () => {
  assert.equal(canonicalJson(parseLossless('{"a":1,"b":2,"a":3}'), false), '{"a":3,"b":2}');
});

test("section labels use Python str.title(), including the Unicode exceptions", () => {
  // Values printed by Python 3.11 (Unicode 14) str.title().
  const python: [string, string][] = [
    ["my domain", "My Domain"], ["work log", "Work Log"], ["phase2x", "Phase2X"], ["ABC def", "Abc Def"],
    ["déjà vu", "Déjà Vu"], ["o'neil", "O'Neil"], ["x y", "X Y"], ["đường ĐI", "Đường Đi"],
    ["ǄǅǆǇǈǉǊǋǌ", "ǅǆǆǉǉǉǌǌǌ"], ["ǆa", "ǅa"], ["x ǅB", "X ǅb"], ["straße", "Straße"],
    ["ﬁne ﬂow", "Fine Flow"], ["İstanbul", "İstanbul"], ["ᾳx", "ᾼx"], ["ªb Ⓐb ⓐb", "ªb Ⓐb Ⓐb"],
    ["ʰa", "ʰa"], ["ǲ dz", "ǲ Dz"],
  ];
  for (const [input, output] of python) assert.equal(pyTitle(input), output, input);
});

test("every code point in the frozen title table behaves as the table says", () => {
  // The table itself is checked against live Python 3.11 in tests/test_title_table.py.
  const table = JSON.parse(readFileSync(resolve(ROOT, "node", "tables", "py-title-u14.json"), "utf8"));
  const cased = new Set<number>();
  for (const [first, last] of table.cased as [number, number][]) for (let cp = first; cp <= last; cp++) cased.add(cp);
  const points = new Set<number>([...cased, ...Object.keys(table.title).map((k) => parseInt(k, 16)), ...Object.keys(table.lower).map((k) => parseInt(k, 16))]);
  for (const cp of points) {
    const char = String.fromCodePoint(cp);
    const key = cp.toString(16);
    assert.equal(pyTitle(char), table.title[key] ?? char, `title U+${key}`);
    assert.equal(pyTitle(`A${char}`), `A${table.lower[key] ?? char}`, `lower U+${key}`);
    assert.equal(pyTitle(`${char}a`).endsWith("a"), cased.has(cp), `cased U+${key}`);
  }
});

function sqliteFile(path: string, applicationId: number, userVersion: number): void {
  const database = new DatabaseSync(path);
  database.exec(`PRAGMA application_id=${applicationId}; PRAGMA user_version=${userVersion}; CREATE TABLE t(x);`);
  database.close();
}

for (const [label, applicationId, userVersion] of [["foreign", 1234, 4], ["future", 0x414d4333, 5]] as const) {
  test(`post-open recheck refuses a ${label} file swapped in after the header preflight`, () => {
    const directory = mkdtempSync(resolve(tmpdir(), "trajecta-r1-toctou-"));
    const database = resolve(directory, "store.sqlite3");
    const replacement = resolve(directory, "replacement.sqlite3");
    copyFileSync(resolve(GOLDEN, "recall", "store.sqlite3"), database);
    sqliteFile(replacement, applicationId, userVersion);
    hooks.beforeOpen = (path) => copyFileSync(replacement, path);
    try {
      assert.throws(() => new MemoryStore(database).currentView(), (error: Error) =>
        error instanceof SchemaVersionError && /newer than this runtime or belongs to another application/u.test(error.message));
      copyFileSync(resolve(GOLDEN, "recall", "store.sqlite3"), database);
      assert.equal(new MemoryStore(database).schemaInfo().state, "incompatible");
    } finally {
      hooks.beforeOpen = null;
    }
  });
}

test("code-point order puts U+E000 before astral characters (UTF-16 order would not)", () => {
  assert.ok(compareCodePoint("", "😀") < 0);
  assert.ok("" > "😀");
});
