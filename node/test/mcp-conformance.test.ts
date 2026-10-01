import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { DatabaseSync } from "node:sqlite";
import {
  FileExistsError,
  IdentityMemory,
  InjectedClock,
  MemoryStore,
  compareCodePoint,
  floatHex,
  loadProfile,
  objectEntries,
  orderedObject,
  parseLossless,
  pyJsonDumps,
  type JsonValue,
  type OrderedObject,
} from "../src/index.ts";
import { McpServer, processFrame } from "../src/mcp.ts";
import { validateArguments } from "../src/mcp-schema.ts";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const GOLDEN = resolve(ROOT, "spec", "golden-mcp-v1");
const PROFILE = resolve(ROOT, "trajecta_identity", "profiles", "example", "profile.json");
const sha256 = (value: Buffer | string): string => createHash("sha256").update(value).digest("hex");

function object(value: JsonValue | undefined): OrderedObject {
  objectEntries(value as JsonValue);
  return value as OrderedObject;
}

function plain(value: JsonValue): any {
  if (value === null || typeof value === "boolean" || typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(plain);
  if (value.kind === "int") return Number(value.value);
  if (value.kind === "float") return value.value;
  return Object.fromEntries(value.entries.map(([key, item]) => [key, plain(item)]));
}

function canonicalPlain(value: any): string {
  if (value === null || typeof value === "boolean" || typeof value === "number" || typeof value === "string")
    return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalPlain).join(",")}]`;
  return `{${Object.keys(value)
    .sort(compareCodePoint)
    .map((key) => `${JSON.stringify(key)}:${canonicalPlain(value[key])}`)
    .join(",")}}`;
}

function dumpDatabase(path: string): any {
  const database = new DatabaseSync(path, { readOnly: true });
  try {
    const tables: Record<string, any[]> = {};
    const names = (
      database.prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").all() as any[]
    ).map((row) => String(row.name));
    for (const name of names) {
      const quoted = `"${name.replaceAll('"', '""')}"`;
      const columns = database.prepare(`PRAGMA table_info(${quoted})`).all() as any[];
      const primary = [...columns]
        .filter((column) => column.pk)
        .sort((left, right) => Number(left.pk) - Number(right.pk))
        .map((column) => String(column.name));
      const order = primary.length ? primary.map((column) => `"${column.replaceAll('"', '""')}"`).join(",") : "rowid";
      const floats = new Set(
        columns
          .filter((column) => String(column.type).toUpperCase().includes("REAL"))
          .map((column) => String(column.name)),
      );
      tables[name] = (database.prepare(`SELECT * FROM ${quoted} ORDER BY ${order}`).all() as any[]).map((row) =>
        Object.fromEntries(
          Object.entries(row).map(([key, value]) => [
            key,
            floats.has(key) && typeof value === "number"
              ? { repr: value.toString().includes(".") ? value.toString() : `${value}.0`, hex: floatHex(value) }
              : value,
          ]),
        ),
      );
    }
    return { tables };
  } finally {
    database.close();
  }
}

function workspace(t: { after(callback: () => void): void }, label: string): string {
  const path = mkdtempSync(resolve(ROOT, `.mcp-${label}-`));
  t.after(() => rmSync(path, { recursive: true, force: true }));
  return path;
}

function customWorkProfile(directory: string): string {
  const profile = object(parseLossless(readFileSync(PROFILE, "utf8")));
  const changed = orderedObject([
    ...profile.entries.map(([key, value]) => [key, key === "name" ? "mcp-work-error" : value] as [string, JsonValue]),
    ["work_root", "work"],
  ]);
  const path = resolve(directory, "profile.json");
  writeFileSync(path, pyJsonDumps(changed) + "\n");
  mkdirSync(resolve(directory, "work"));
  writeFileSync(resolve(directory, "work", "state.json"), '{"schema":"wrong","work":[]}\n');
  return path;
}

function runMcp(
  directory: string,
  profile: string,
  transcript: Buffer,
): Promise<{ stdout: Buffer; stderr: Buffer; code: number }> {
  return new Promise((done, reject) => {
    const child = spawn(
      process.execPath,
      [
        "--no-warnings",
        "--experimental-strip-types",
        resolve(ROOT, "node", "src", "mcp.ts"),
        "--profile",
        profile,
        "--db",
        "store.sqlite3",
      ],
      {
        cwd: directory,
        env: { ...process.env, TRAJECTA_IDENTITY_MCP_CLOCK_START: "2026-09-30T00:00:00.000000+00:00" },
        stdio: ["pipe", "pipe", "pipe"],
      },
    );
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    child.stdout.on("data", (chunk) => stdout.push(Buffer.from(chunk)));
    child.stderr.on("data", (chunk) => stderr.push(Buffer.from(chunk)));
    child.on("error", reject);
    child.on("close", (code) =>
      done({ stdout: Buffer.concat(stdout), stderr: Buffer.concat(stderr), code: code ?? -1 }),
    );
    child.stdin.end(transcript);
  });
}

test("MCP manifest authenticates every corpus file, source and table", () => {
  const manifest = plain(parseLossless(readFileSync(resolve(GOLDEN, "MANIFEST.json"), "utf8")));
  for (const [relative, digest] of Object.entries(manifest.files as Record<string, string>)) {
    const path = relative.startsWith("tables/") ? resolve(ROOT, "memory_core", relative) : resolve(GOLDEN, relative);
    assert.equal(sha256(readFileSync(path)), digest, relative);
  }
  for (const [relative, digest] of Object.entries(manifest.oracle_sources as Record<string, string>))
    assert.equal(sha256(readFileSync(resolve(ROOT, relative))), digest, relative);
});

test("MCP argument defaults preserve numeric kinds and do not mutate the request", () => {
  const raw = object(parseLossless('{"cue":"copy-safe"}'));
  const before = pyJsonDumps(raw);
  const validated = validateArguments("identity_retrieve", raw);
  assert.equal(pyJsonDumps(raw), before);
  assert.deepEqual(plain(validated), { cue: "copy-safe", limit: 10, budget: 2400, track: true });
});

for (const name of readdirSync(GOLDEN)
  .filter((entry) => existsSync(resolve(GOLDEN, entry, "transcript.in")))
  .sort()) {
  test(`MCP corpus ${name}`, async (t) => {
    const scenario = resolve(GOLDEN, name);
    const directory = workspace(t, name);
    if (existsSync(resolve(scenario, "initial.sqlite3")))
      copyFileSync(resolve(scenario, "initial.sqlite3"), resolve(directory, "store.sqlite3"));
    if (existsSync(resolve(scenario, "initial.sqlite3-wal")))
      copyFileSync(resolve(scenario, "initial.sqlite3-wal"), resolve(directory, "store.sqlite3-wal"));
    const profile = name === "work-store-error" ? customWorkProfile(directory) : PROFILE;
    const result = await runMcp(directory, profile, readFileSync(resolve(scenario, "transcript.in")));
    assert.equal(result.code, 0, result.stderr.toString("utf8"));
    assert.deepEqual(result.stdout, readFileSync(resolve(scenario, "expected.out")));

    const database = resolve(directory, "store.sqlite3");
    if (existsSync(resolve(scenario, "absent"))) {
      assert.equal(existsSync(database), false);
      assert.equal(existsSync(database + "-wal"), false);
      assert.equal(existsSync(database + "-shm"), false);
    } else {
      assert.equal(existsSync(database), true);
      let dumpPath = database;
      if (existsSync(database + "-wal")) {
        dumpPath = resolve(directory, "dump.sqlite3");
        copyFileSync(database, dumpPath);
      }
      const actual = canonicalPlain(dumpDatabase(dumpPath)) + "\n";
      assert.equal(actual, readFileSync(resolve(scenario, "dump.json"), "utf8"));
      if (name !== "wal-sidecar") {
        assert.equal(existsSync(database + "-wal"), false);
        assert.equal(existsSync(database + "-shm"), false);
      }
      if (
        existsSync(resolve(scenario, "initial.sqlite3")) &&
        [
          "error-proposal-decided",
          "error-proposal-integrity",
          "error-receipt-integrity",
          "error-stale-authority",
          "foreign-store",
          "future-store",
          "legacy-v4",
          "wal-sidecar",
        ].includes(name)
      ) {
        assert.deepEqual(readFileSync(database), readFileSync(resolve(scenario, "initial.sqlite3")));
      }
    }
  });
}

function call(name: string, args = "{}"): Buffer {
  return Buffer.from(`{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"${name}","arguments":${args}}}`);
}

function server(database: string): McpServer {
  return new McpServer(
    new IdentityMemory(loadProfile(PROFILE), database, {
      surface: "mcp",
      displayDatabase: "store.sqlite3",
      clock: new InjectedClock(),
    }),
    "0.1.0",
  );
}

function bootstrap(database: string): void {
  const memory = new IdentityMemory(loadProfile(PROFILE), database, { clock: new InjectedClock() });
  memory.bootstrap();
  memory.close();
}

test("MCP call boundaries observe file swap and deletion without recreating a read store", (t) => {
  const directory = workspace(t, "lifecycle-swap");
  const database = resolve(directory, "store.sqlite3");
  const replacement = resolve(directory, "replacement.sqlite3");
  bootstrap(database);
  bootstrap(replacement);
  const changed = new IdentityMemory(loadProfile(PROFILE), replacement, { clock: new InjectedClock() });
  changed.logPhase("swap", { title: "Swap", summary: "Replacement" });
  changed.close();

  const instance = server(database);
  assert.match(processFrame(instance, call("identity_status"))!, /"phase": 0|"records": \{"core"/u);
  copyFileSync(replacement, database);
  assert.match(processFrame(instance, call("identity_status"))!, /"phase": 1/u);
  unlinkSync(database);
  const listing = readdirSync(directory).sort();
  assert.match(processFrame(instance, call("identity_status"))!, /"store": "uninitialized"/u);
  assert.equal(existsSync(database), false);
  assert.deepEqual(readdirSync(directory).sort(), listing);
});

test("MCP call boundaries fail closed on foreign, future and newly appearing WAL state", (t) => {
  const directory = workspace(t, "lifecycle-refusal");
  const database = resolve(directory, "store.sqlite3");
  const clean = resolve(directory, "clean.sqlite3");
  bootstrap(database);
  copyFileSync(database, clean);
  const instance = server(database);
  processFrame(instance, call("identity_status"));

  for (const [name, pragma] of [
    ["foreign.sqlite3", "PRAGMA application_id=12345"],
    ["future.sqlite3", "PRAGMA user_version=99"],
  ] as const) {
    const fixture = resolve(directory, name);
    bootstrap(fixture);
    const sqlite = new DatabaseSync(fixture);
    sqlite.exec(pragma);
    sqlite.close();
    copyFileSync(fixture, database);
    const before = readFileSync(database);
    const listing = readdirSync(directory).sort();
    assert.match(
      processFrame(instance, call("identity_log_phase", '{"event_id":"blocked","title":"B","summary":"B"}'))!,
      /SchemaVersionError/u,
    );
    assert.deepEqual(readFileSync(database), before);
    assert.deepEqual(readdirSync(directory).sort(), listing);
  }

  copyFileSync(clean, database);
  processFrame(instance, call("identity_status"));
  writeFileSync(database + "-wal", "WAL fixture");
  const before = readFileSync(database);
  const listing = readdirSync(directory).sort();
  assert.match(processFrame(instance, call("identity_status"))!, /IncompatibleJournalMode/u);
  assert.deepEqual(readFileSync(database), before);
  assert.deepEqual(readdirSync(directory).sort(), listing);
  assert.equal(existsSync(database + "-shm"), false);
});

test("MCP process kill inside a writer transaction leaves no partial record", async (t) => {
  const directory = workspace(t, "crash");
  const database = resolve(directory, "store.sqlite3");
  bootstrap(database);
  const child = spawn(
    process.execPath,
    [
      "--no-warnings",
      "--experimental-strip-types",
      resolve(ROOT, "node", "test", "mcp-crash-child.ts"),
      "--profile",
      PROFILE,
      "--db",
      "store.sqlite3",
    ],
    {
      cwd: directory,
      env: { ...process.env, TRAJECTA_IDENTITY_MCP_CLOCK_START: "2026-09-30T00:00:00.000000+00:00" },
      stdio: ["pipe", "ignore", "pipe"],
    },
  );
  child.stdin.end(call("identity_log_fact", '{"fact_id":"crash","title":"Crash","summary":"Crash"}'));
  const code = await new Promise<number | null>((done) => child.on("close", done));
  assert.equal(code, 95);
  const reopened = new IdentityMemory(loadProfile(PROFILE), database, { clock: new InjectedClock() });
  assert.deepEqual(reopened.store.currentView("fact:crash"), []);
  assert.equal(reopened.store.schemaInfo().state, "ready");
  reopened.close();
  assert.equal(existsSync(database + "-wal"), false);
  assert.equal(existsSync(database + "-shm"), false);
});

test("migration existing paths have a public typed error and preserve both files", (t) => {
  const directory = workspace(t, "file-exists");
  const source = resolve(directory, "source.sqlite3");
  const target = resolve(directory, "target.sqlite3");
  bootstrap(source);
  bootstrap(target);
  const beforeSource = readFileSync(source);
  const beforeTarget = readFileSync(target);
  const store = new MemoryStore(source);
  assert.throws(() => store.migrateTo(target), FileExistsError);
  assert.deepEqual(readFileSync(source), beforeSource);
  assert.deepEqual(readFileSync(target), beforeTarget);
});

test("Python and Node package versions are pinned together", () => {
  const python = /__version__\s*=\s*"([^"]+)"/u.exec(
    readFileSync(resolve(ROOT, "trajecta_identity", "__init__.py"), "utf8"),
  )?.[1];
  const node = plain(parseLossless(readFileSync(resolve(ROOT, "node", "package.json"), "utf8"))).version;
  assert.equal(node, python);
});
