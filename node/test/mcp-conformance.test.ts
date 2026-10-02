import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  cpSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  realpathSync,
  rmSync,
  symlinkSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { DatabaseSync } from "node:sqlite";
import { createServer } from "node:http";
import { profileDb } from "../src/paths.ts";
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
import { assertMcpPaths, isolatedMcpEnv, profileNameFor } from "./mcp/isolation.ts";

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
  const path = realpathSync.native(mkdtempSync(resolve(ROOT, `.mcp-${label}-`)));
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
  extraEnv: Record<string, string> = {},
): Promise<{ stdout: Buffer; stderr: Buffer; code: number }> {
  const env = isolatedMcpEnv(directory, {
    TRAJECTA_IDENTITY_MCP_CLOCK_START: "2026-09-30T00:00:00.000000+00:00",
    ...extraEnv,
  });
  if (profile === PROFILE) profile = resolve(env.TRAJECTA_IDENTITY_PROFILES, "example/profile.json");
  assertMcpPaths(directory, env, profileNameFor(directory, profile, env), "store.sqlite3");
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
        env,
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

test("MCP runner allows only explicit env and refuses escaping writes before spawn", (t) => {
  const root = workspace(t, "isolation");
  const keys = ["HTTPS_PROXY", "TRAJECTA_WORK_ROOT", "TRAJECTA_IDENTITY_DATA_DIR", "TRAJECTA_IDENTITY_PROFILES"];
  const before = keys.map((key) => [key, process.env[key]] as const);
  try {
    for (const key of keys) process.env[key] = "synthetic-parent-value";
    const env = isolatedMcpEnv(root);
    assert.equal(env.HTTPS_PROXY, undefined);
    assert.equal(env.TRAJECTA_WORK_ROOT, undefined);
    for (const key of [
      "HOME",
      "USERPROFILE",
      "XDG_DATA_HOME",
      "LOCALAPPDATA",
      "APPDATA",
      "TRAJECTA_IDENTITY_DATA_DIR",
      "TRAJECTA_IDENTITY_PROFILES",
    ])
      assert(env[key].startsWith(root + (process.platform === "win32" ? "\\" : "/")));
    assert(existsSync(resolve(env.TRAJECTA_IDENTITY_PROFILES, "example/profile.json")));
    assertMcpPaths(root, env, "example", "store.sqlite3");
    assert.throws(() => isolatedMcpEnv(root, { HTTPS_PROXY: "synthetic" }), /unapproved child env/);
    assert.throws(() => assertMcpPaths(root, env, "example", "../outside.sqlite3"), /escapes MCP fixture/);
    assert.throws(
      () => assertMcpPaths(root, { ...env, TRAJECTA_IDENTITY_DATA_DIR: resolve(root, "..") }, "example"),
      /escapes MCP fixture/,
    );
    assert.throws(
      () => assertMcpPaths(root, { ...env, TRAJECTA_WORK_ROOT: "../work" }, "example"),
      /escapes MCP fixture/,
    );
    if (process.platform !== "win32") {
      const install = resolve(env.TRAJECTA_IDENTITY_DATA_DIR, "profiles/example");
      mkdirSync(install, { recursive: true });
      symlinkSync(resolve(root, "../synthetic-profile.json"), resolve(install, "profile.json"));
      assert.throws(() => assertMcpPaths(root, env, "example"), /escapes MCP fixture/);
    }
  } finally {
    for (const [key, value] of before) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
});

function runCommand(command: string, args: string[], root: string, env: Record<string, string>) {
  return new Promise<{ code: number | null; stdout: Buffer; stderr: Buffer }>((done, reject) => {
    const child = spawn(command, args, { cwd: root, env, stdio: ["pipe", "pipe", "pipe"] });
    const stdout: Buffer[] = [],
      stderr: Buffer[] = [];
    child.stdout.on("data", (chunk) => stdout.push(Buffer.from(chunk)));
    child.stderr.on("data", (chunk) => stderr.push(Buffer.from(chunk)));
    child.on("error", reject);
    child.on("close", (code) => done({ code, stdout: Buffer.concat(stdout), stderr: Buffer.concat(stderr) }));
    child.stdin.end();
  });
}

async function smokeMcp(
  t: { after(callback: () => void): void },
  root: string,
  env: Record<string, string>,
  command: string,
  args: string[],
  profileName: string,
  database: string,
  state: "ready" | "uninitialized",
): Promise<void> {
  const dbIndex = args.indexOf("--db");
  assertMcpPaths(root, env, profileName, dbIndex < 0 ? undefined : args[dbIndex + 1]);
  const before = existsSync(database) ? readFileSync(database) : undefined;
  const lastPath = resolve(env.TRAJECTA_IDENTITY_DATA_DIR, ".last-profile");
  const last = existsSync(lastPath) ? readFileSync(lastPath) : undefined;
  const child = spawn(command, args, { cwd: root, env, stdio: ["pipe", "pipe", "pipe"] });
  t.after(() => {
    if (child.exitCode === null && child.signalCode === null) child.kill();
  });
  const stderr: Buffer[] = [];
  child.stderr.on("data", (chunk) => stderr.push(Buffer.from(chunk)));
  const closed = new Promise<number | null>((done) => child.on("close", done));
  const responses = new Promise<any[]>((done, reject) => {
    let pending = "";
    child.stdout.on("data", (chunk) => {
      pending += Buffer.from(chunk).toString("utf8");
      const lines = pending.split("\n");
      if (lines.length >= 3) {
        try {
          done(lines.slice(0, 2).map((line) => plain(parseLossless(line))));
        } catch (error) {
          reject(error);
        }
      }
    });
    child.on("error", reject);
    child.on("close", () => reject(new Error(`MCP exited before replies: ${Buffer.concat(stderr).toString("utf8")}`)));
  });
  child.stdin.write(
    Buffer.from(
      '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}\n' +
        call("identity_status").toString("utf8") +
        "\n",
    ),
  );
  const [initialized, response] = await responses;
  assert.equal(child.exitCode, null, "generated launcher must remain alive while stdin stays open");
  assert.equal(child.signalCode, null);
  assert.equal(initialized.result.protocolVersion, "2025-06-18");
  assert.equal(response.result.isError, false);
  const status = response.result.structuredContent;
  assert.equal(status.profile, profileName);
  assert.equal(status.db, database);
  assert.equal(status.store, state);
  if (state === "ready") {
    const profile = profileNameFor(root, profileName, env);
    assert.equal(profile, profileName);
    const manifest = [
      resolve(env.TRAJECTA_IDENTITY_PROFILES, profileName, "profile.json"),
      resolve(env.TRAJECTA_IDENTITY_DATA_DIR, "profiles", profileName, "profile.json"),
    ].find(existsSync)!;
    const reference = new IdentityMemory(loadProfile(manifest), database, { surface: "mcp" });
    try {
      assert.deepEqual(status, reference.status());
    } finally {
      reference.close();
    }
  }
  child.stdin.end();
  assert.equal(await closed, 0, Buffer.concat(stderr).toString("utf8"));
  assert.deepEqual(existsSync(database) ? readFileSync(database) : undefined, before, "status never writes the store");
  assert.deepEqual(existsSync(lastPath) ? readFileSync(lastPath) : undefined, last, "MCP never remembers a choice");
}

for (const scenario of ["default-db", "explicit-db", "json-profile", "url-profile"]) {
  test(`setup launches its exact MCP config: ${scenario}`, { timeout: 15000 }, async (t) => {
    const root = workspace(t, `setup-${scenario}`);
    const env = isolatedMcpEnv(root);
    let name = "example",
      spec = "example";
    if (scenario === "json-profile" || scenario === "url-profile") {
      name = `setup-${scenario}`;
      const ast = object(parseLossless(readFileSync(PROFILE, "utf8")));
      const bytes = Buffer.from(
        pyJsonDumps(
          orderedObject(ast.entries.map(([key, value]) => [key, key === "name" ? name : value] as [string, JsonValue])),
        ),
      );
      if (scenario === "json-profile") {
        spec = resolve(root, "input.json");
        writeFileSync(spec, bytes);
      } else {
        const server = createServer((_request, response) => {
          response.writeHead(200, { "Content-Type": "application/json" });
          response.end(bytes);
        });
        await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
        t.after(() => server.close());
        const address = server.address();
        assert(address && typeof address !== "string");
        spec = `http://127.0.0.1:${address.port}/profile.json`;
      }
    }
    const explicit = scenario === "explicit-db";
    const database = explicit ? resolve(root, "explicit.sqlite3") : profileDb(name, env);
    // This includes the URL/JSON install destination before setup itself can write.
    assertMcpPaths(root, env, name, database);
    const setup = await runCommand(
      process.execPath,
      [
        "--experimental-strip-types",
        resolve(ROOT, "node/src/cli.ts"),
        "--profile",
        spec,
        ...(explicit ? ["--db", "explicit.sqlite3"] : []),
        "setup",
      ],
      root,
      env,
    );
    assert.equal(setup.code, 0, setup.stderr.toString("utf8"));
    const config = plain(parseLossless(setup.stdout.toString("utf8")));
    const launchers = Object.values(config.mcpServers) as { command: string; args: string[] }[];
    assert.equal(launchers.length, 1);
    const launcher = launchers[0];
    assert.equal(launcher.command, process.execPath);
    assert.equal(launcher.args[launcher.args.indexOf("--profile") + 1], name);
    if (explicit) assert.equal(launcher.args[launcher.args.indexOf("--db") + 1], database);
    else assert(!launcher.args.includes("--db"));
    assert(existsSync(database), "setup bootstraps the same store MCP must read");
    // Launch exactly the returned command+args; do not repair or replace any argument.
    await smokeMcp(t, root, env, launcher.command, launcher.args, name, database, "ready");
  });
}

for (const args of [[], ["--profile", "example"]]) {
  test(
    `direct MCP entry permits no --db and ${args.length ? "a named" : "an omitted"} profile`,
    { timeout: 15000 },
    async (t) => {
      const root = workspace(t, "direct-default");
      const env = isolatedMcpEnv(root);
      const database = profileDb("example", env);
      await smokeMcp(
        t,
        root,
        env,
        process.execPath,
        ["--experimental-strip-types", resolve(ROOT, "node/src/mcp.ts"), ...args],
        "example",
        database,
        "uninitialized",
      );
      assert(!existsSync(database));
    },
  );
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
    // Scenarios that need a profile, work roots or environment ship them as a fixture
    // generated by the oracle, so nothing is reconstructed here.
    let profile = name === "work-store-error" ? customWorkProfile(directory) : PROFILE;
    let extraEnv: Record<string, string> = {};
    if (existsSync(resolve(scenario, "fixture"))) {
      cpSync(resolve(scenario, "fixture"), directory, { recursive: true });
      const invocation = plain(parseLossless(readFileSync(resolve(scenario, "invocation.json"), "utf8")));
      profile = invocation.profile;
      extraEnv = invocation.env;
    }
    const result = await runMcp(directory, profile, readFileSync(resolve(scenario, "transcript.in")), extraEnv);
    assert.equal(result.code, 0, result.stderr.toString("utf8"));
    // Product code reports native paths (as Python does on each OS). Only the test maps the
    // OS separator of the relative work root back to the Linux-generated oracle.
    const stdout =
      process.platform === "win32"
        ? Buffer.from(result.stdout.toString("utf8").replaceAll("work\\\\state.json", "work/state.json"), "utf8")
        : result.stdout;
    assert.deepEqual(stdout, readFileSync(resolve(scenario, "expected.out")));

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
  const env = isolatedMcpEnv(directory, {
    TRAJECTA_IDENTITY_MCP_CLOCK_START: "2026-09-30T00:00:00.000000+00:00",
  });
  const profile = resolve(env.TRAJECTA_IDENTITY_PROFILES, "example/profile.json");
  assertMcpPaths(directory, env, profileNameFor(directory, profile, env), database);
  const child = spawn(
    process.execPath,
    [
      "--no-warnings",
      "--experimental-strip-types",
      resolve(ROOT, "node", "test", "mcp-crash-child.ts"),
      "--profile",
      profile,
      "--db",
      "store.sqlite3",
    ],
    {
      cwd: directory,
      env,
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
